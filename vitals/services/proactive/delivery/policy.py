"""The gate every outgoing message passes through: may this be sent, and was it.

Five rules, in the order they're checked:

1. **No channel** → nothing happens (the app before the bot exists).
2. **Module off** → nothing happens either: switching ``signals`` off in Settings
   is the emergency switch, and it has to silence the bot without a deploy.
3. **Dedupe.** A ``dedupe_key`` that's already in the journal means this exact
   message went out; a re-run of the job is a no-op, not a second ping.
4. **Quiet hours** hold back *nudges* — the bot's own idea of a good moment. The
   brief and the evening block go out at a time the owner typed by hand into the
   same settings card, so silencing them by quiet hours is one field quietly
   cancelling another with no way to see which won.
5. **The daily budget** (also from the settings card) covers all three
   self-initiated categories — the brief, the evening block, nudges.

   Answers to the owner (``reply``, ``echo``) are deliberately exempt. Counting
   them would mean that after the fourth thing you logged, the bot stops replying
   to you — which reads as a broken bot, not as a budget. This is the single
   easiest rule in the whole feature to get wrong, so it lives in one ``frozenset``
   right here rather than at each call site.

A send that fails at the transport is logged and swallowed: the caller's DB work
(the signals it just parsed, the digest it just stored) must not be rolled back
because Telegram had a bad minute, and an un-sent message writes no journal row,
so it costs nothing from the budget either.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import (
    datetime,
    time as time_type,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.ai import AIInvocation
from vitals.models.identity import HealthSubject, User
from vitals.models.proactive import Notification, NotificationDeliveryIntent
from vitals.models.raw_payload import RawPayload
from vitals.models.scoped_settings import (
    IntegrationConnectionSetting,
)
from vitals.models.tenancy import IntegrationConnection
from vitals.services.identity.contracts import PreIdentityCompatibilityError
from vitals.services.identity.governance import (
    acquire_identity_governance_lock,
    authorize_pre_identity_compatibility_transaction,
)
from vitals.services.proactive.preferences import codec as preference_codec
from vitals.services.proactive.preferences import contracts as preference_contracts
from vitals.services.proactive.preferences import queries as preference_queries
from vitals.services.proactive.channels import (
    LEGACY_TELEGRAM_CREDENTIAL_REF,
)
from vitals.services.proactive.ownership import ProactiveOwnershipContext

logger = logging.getLogger(__name__)

from vitals.services.proactive.delivery.contracts import (
    CATEGORY_ECHO,
    CATEGORY_REPLY,
    HISTORICAL_RECIPIENT_STATUSES,
    DeliveryCapabilityError,
    DeliveryPolicyUnavailableError,
    DeliveryScopeError,
    DurableDeliveryRequiredError,
    _DeliveryPolicy,
    _INBOUND_RAW_DOMAIN,
    _IntentSnapshot,
    _LockedDeliveryAuthority,
    _binding_for,
)


async def _lock_live_delivery_authority(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    actor_user_id: uuid.UUID | None,
) -> _LockedDeliveryAuthority:
    """Lock governance -> sole S -> sorted users -> exact current Telegram C."""

    _validate_ownership(ownership, actor_user_id=actor_user_id)
    await acquire_identity_governance_lock(session)
    subject_rows = list(
        await session.scalars(
            select(HealthSubject)
            .order_by(HealthSubject.id)
            .limit(2)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(subject_rows) != 1:
        raise DeliveryPolicyUnavailableError(
            "legacy Telegram delivery requires exactly one health subject"
        )
    subject = subject_rows[0]
    if subject.id != ownership.subject_id or subject.owner_user_id != ownership.recipient_user_id:
        raise DeliveryScopeError("delivery subject and recipient are not authorized")
    try:
        subject_timezone = ZoneInfo(subject.timezone)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise DeliveryPolicyUnavailableError("health subject timezone is invalid") from exc

    user_ids = sorted(
        {
            ownership.recipient_user_id,
            *({actor_user_id} if actor_user_id is not None else set()),
        },
        key=str,
    )
    users = list(
        await session.scalars(
            select(User)
            .where(User.id.in_(user_ids))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(users) != len(user_ids) or any(row.status != UserStatus.ACTIVE.value for row in users):
        raise DeliveryScopeError("delivery recipient or actor is not active")

    connections = list(
        await session.scalars(
            select(IntegrationConnection)
            .where(
                IntegrationConnection.subject_id == ownership.subject_id,
                IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
                IntegrationConnection.connection_type == IntegrationConnectionType.RECIPIENT.value,
            )
            .order_by(IntegrationConnection.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    known_statuses = {item.value for item in IntegrationConnectionStatus}
    if any(row.status not in known_statuses for row in connections):
        raise DeliveryScopeError("Telegram recipient has unknown lifecycle state")
    current = [
        row for row in connections if row.status != IntegrationConnectionStatus.RETIRED.value
    ]
    if len(current) != 1 or current[0].id != ownership.connection_id:
        raise DeliveryScopeError("Telegram recipient connection is not exact/current")
    connection = current[0]
    if connection.status not in {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }:
        raise DeliveryScopeError("Telegram recipient connection is inactive")
    if connection.credential_ref != LEGACY_TELEGRAM_CREDENTIAL_REF:
        raise DeliveryScopeError("Telegram credential resolver is unavailable")
    return _LockedDeliveryAuthority(
        binding=_binding_for(ownership),
        timezone=subject_timezone,
        credential_ref=connection.credential_ref,
        telegram_connection_ids=frozenset(row.id for row in connections),
    )


async def _lock_historical_delivery_roots(
    session: AsyncSession,
    *,
    intent: NotificationDeliveryIntent | _IntentSnapshot,
) -> None:
    """Lock frozen provenance without applying post-send live authorization."""

    # Freeze identity/tenancy graph writers before projecting the raw row's
    # historical connection.  The projection stays intentionally unlocked so
    # the canonical row-lock order remains governance -> S -> users -> Cs -> raw.
    await acquire_identity_governance_lock(session)
    raw_connection_id: uuid.UUID | None = None
    if intent.raw_payload_id is not None:
        with session.no_autoflush:
            raw_connection_id = await session.scalar(
                select(RawPayload.integration_connection_id).where(
                    RawPayload.id == intent.raw_payload_id
                )
            )
        if raw_connection_id is None:
            raise DeliveryScopeError("delivery raw provenance no longer exists")
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == intent.subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if subject is None:
        raise DeliveryScopeError("delivery subject no longer exists")
    user_ids = sorted(
        {
            intent.recipient_user_id,
            *({intent.actor_user_id} if intent.actor_user_id is not None else set()),
        },
        key=str,
    )
    users = list(
        await session.scalars(
            select(User)
            .where(User.id.in_(user_ids))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(users) != len(user_ids):
        raise DeliveryScopeError("delivery recipient or actor no longer exists")
    connection_ids = sorted(
        {
            intent.integration_connection_id,
            *({raw_connection_id} if raw_connection_id is not None else set()),
        },
        key=str,
    )
    connections = list(
        await session.scalars(
            select(IntegrationConnection)
            .where(IntegrationConnection.id.in_(connection_ids))
            .order_by(IntegrationConnection.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(connections) != len(connection_ids) or any(
        row.subject_id != intent.subject_id
        or row.provider != intent.channel
        or row.connection_type != IntegrationConnectionType.RECIPIENT.value
        or row.status not in HISTORICAL_RECIPIENT_STATUSES
        for row in connections
    ):
        raise DeliveryScopeError("historical delivery connection is invalid")
    await _lock_raw_and_ai_provenance(
        session,
        intent=intent,
        telegram_connection_ids=frozenset(connection_ids),
    )


async def _lock_raw_and_ai_provenance(
    session: AsyncSession,
    *,
    intent: NotificationDeliveryIntent | _IntentSnapshot,
    telegram_connection_ids: frozenset[uuid.UUID],
) -> None:
    if intent.raw_payload_id is not None:
        raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == intent.raw_payload_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            raw is None
            or raw.subject_id != intent.subject_id
            or raw.actor_user_id != intent.recipient_user_id
            or raw.integration_connection_id not in telegram_connection_ids
            or raw.file_asset_id is not None
            or raw.domain != _INBOUND_RAW_DOMAIN
            or raw.source != Source.TELEGRAM.value
        ):
            raise DeliveryScopeError("delivery raw provenance is invalid")
    if intent.ai_invocation_id is None:
        return
    invocation = await session.scalar(
        select(AIInvocation)
        .where(AIInvocation.id == intent.ai_invocation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    expected_purpose = (
        AIInvocationPurpose.SIGNAL_PARSE.value
        if intent.category == CATEGORY_ECHO
        else AIInvocationPurpose.QUESTION_REPLY.value
    )
    allowed_statuses = {
        AIInvocationStatus.SUCCEEDED.value,
        AIInvocationStatus.FAILED.value,
        AIInvocationStatus.AMBIGUOUS.value,
    }
    if intent.category == CATEGORY_REPLY:
        allowed_statuses.add(AIInvocationStatus.CANCELLED.value)
    if (
        invocation is None
        or invocation.subject_id != intent.subject_id
        or invocation.actor_user_id != intent.recipient_user_id
        or invocation.raw_payload_id != intent.raw_payload_id
        or invocation.purpose != expected_purpose
        or invocation.source != AIInvocationSource.TELEGRAM.value
        or invocation.status not in allowed_statuses
    ):
        raise DeliveryScopeError("delivery AI provenance is invalid")


async def _locked_signals_module_enabled(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> bool:
    """Read only the already-scoped module row after the subject lock."""

    del session, subject_id
    # The proactive layer had a master switch: the ``signals`` module, which was
    # also the free-text capture domain. Both are gone, and nothing replaced the
    # switch — the layer's own preferences say what it sends and how often, and a
    # second switch above them was only ever an emergency stop for a bot that no
    # longer exists.
    return True


async def _load_locked_delivery_policy(
    session: AsyncSession,
    *,
    authority: _LockedDeliveryAuthority,
) -> _DeliveryPolicy:
    """Load the reviewed scoped policy projection without lock-order inversion."""

    # The connection root is already locked. Lock the exact scoped policy row as
    # well so a composed domain mutation and final intent reservation observe one
    # frozen policy even when the caller keeps this outer transaction open.
    await session.scalar(
        select(IntegrationConnectionSetting.integration_connection_id)
        .where(
            IntegrationConnectionSetting.integration_connection_id
            == authority.binding.integration_connection_id,
            IntegrationConnectionSetting.key == preference_contracts.TELEGRAM_DELIVERY_POLICY_KEY,
        )
        .with_for_update()
    )
    return await _read_delivery_policy(session, authority=authority)


async def _read_delivery_policy(
    session: AsyncSession,
    *,
    authority: _LockedDeliveryAuthority,
) -> _DeliveryPolicy:
    """Reproject the exact policy whose row/root is already locked."""

    getter = getattr(preference_queries, "get_locked_delivery_policy", None)
    if getter is None:
        raise DeliveryPolicyUnavailableError(
            "subject-scoped delivery policy service is unavailable"
        )
    raw = await getter(
        session,
        subject_id=authority.binding.subject_id,
        recipient_user_id=authority.binding.recipient_user_id,
        integration_connection_id=authority.binding.integration_connection_id,
    )
    try:
        quiet_start = raw.quiet_start
        quiet_end = raw.quiet_end
        daily_budget = raw.daily_budget
    except AttributeError:
        if not isinstance(raw, dict):
            raise DeliveryPolicyUnavailableError("delivery policy projection is invalid")
        try:
            quiet_start = raw["quiet_start"]
            quiet_end = raw["quiet_end"]
            daily_budget = raw["daily_budget"]
        except KeyError as exc:
            raise DeliveryPolicyUnavailableError("delivery policy projection is invalid") from exc
    try:
        return _DeliveryPolicy(
            daily_budget=daily_budget,
            quiet_start=(
                quiet_start if isinstance(quiet_start, time_type) else preference_codec.as_time(quiet_start)
            ),
            quiet_end=(quiet_end if isinstance(quiet_end, time_type) else preference_codec.as_time(quiet_end)),
        )
    except (TypeError, ValueError) as exc:
        raise DeliveryPolicyUnavailableError("delivery policy projection is invalid") from exc


def _validate_ownership(
    ownership: ProactiveOwnershipContext | None,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    if ownership is not None and not isinstance(ownership, ProactiveOwnershipContext):
        raise TypeError("ownership must be a ProactiveOwnershipContext or None")
    if actor_user_id is not None and not isinstance(actor_user_id, uuid.UUID):
        raise TypeError("actor_user_id must be a UUID or None")
    if ownership is None and actor_user_id is not None:
        raise ValueError("actor_user_id requires explicit proactive ownership")
    if (
        ownership is not None
        and actor_user_id is not None
        and actor_user_id != ownership.recipient_user_id
    ):
        raise ValueError("proactive delivery actor must be the recipient user")


def notification_ownership_scope(
    ownership: ProactiveOwnershipContext,
    *,
    connection_scoped: bool = True,
):
    if not isinstance(ownership, ProactiveOwnershipContext):
        raise TypeError("ownership must be a ProactiveOwnershipContext")
    connection_filters = [
        IntegrationConnection.id == Notification.integration_connection_id,
        IntegrationConnection.subject_id == ownership.subject_id,
        IntegrationConnection.connection_type == IntegrationConnectionType.RECIPIENT.value,
        IntegrationConnection.status.in_(HISTORICAL_RECIPIENT_STATUSES),
        IntegrationConnection.provider == Notification.channel,
    ]
    if connection_scoped:
        connection_filters.append(IntegrationConnection.id == ownership.connection_id)
    valid_connection = (
        select(IntegrationConnection.id).where(*connection_filters).correlate(Notification).exists()
    )
    owned = and_(
        Notification.subject_id == ownership.subject_id,
        Notification.recipient_user_id == ownership.recipient_user_id,
        or_(
            Notification.actor_user_id.is_(None),
            Notification.actor_user_id == ownership.recipient_user_id,
        ),
        valid_connection,
    )
    if ownership.include_legacy_unowned:
        owned = or_(
            owned,
            and_(
                Notification.subject_id.is_(None),
                Notification.actor_user_id.is_(None),
                Notification.recipient_user_id.is_(None),
                Notification.integration_connection_id.is_(None),
            ),
        )
    return owned


class NotificationOwnershipConflictError(RuntimeError):
    """A global legacy dedupe key is already owned by another scope."""


class ProactiveOwnershipScopeError(ValueError):
    """A delivery context does not resolve to the legacy owner/channel graph."""


@dataclass(frozen=True, slots=True)
class _PreparedDelivery:
    """Policy-approved message that may cross the network without a DB session."""

    text: str = field(repr=False)
    category: str
    dedupe_key: str | None
    buttons: tuple[tuple[str, str], ...] | None
    reply_to: str | None
    sent_at: datetime
    channel: str
    ownership: ProactiveOwnershipContext | None
    actor_user_id: uuid.UUID | None
    ai_invocation_id: uuid.UUID | None
    redact_journal_content: bool
    journal_raw_payload_id: int | None
    _session: AsyncSession = field(repr=False, compare=False)
    _transaction: object = field(repr=False, compare=False)

    def __reduce__(self):
        raise TypeError("_PreparedDelivery is not pickleable")


@dataclass(frozen=True, slots=True)
class _DeliveredMessage:
    """Successful transport result, including channels without a message id."""

    external_id: str | None


async def _require_ownership_scope(
    session: AsyncSession,
    ownership: ProactiveOwnershipContext,
    *,
    channel: str,
) -> None:
    """Revalidate the legacy recipient and channel before network delivery.

    Owner-as-recipient is a Stage-2 compatibility invariant.  A future care-team
    delivery model must replace it with an explicit recipient/access binding.
    Historical reads may retain inactive provenance, but a live delivery is
    allowed only through a legacy-compatible or active recipient connection.
    """

    subject = (
        await session.execute(
            select(HealthSubject.owner_user_id, User.status)
            .join(User, User.id == HealthSubject.owner_user_id)
            .where(HealthSubject.id == ownership.subject_id)
        )
    ).one_or_none()
    if subject is None:
        raise ProactiveOwnershipScopeError("proactive delivery subject or owner does not exist")
    owner_user_id, owner_status = subject
    if owner_user_id != ownership.recipient_user_id:
        raise ProactiveOwnershipScopeError("proactive recipient is not the legacy subject owner")
    if owner_status != UserStatus.ACTIVE.value:
        raise ProactiveOwnershipScopeError("proactive recipient identity is not active")

    if not isinstance(channel, str) or not channel.strip():
        raise ProactiveOwnershipScopeError("proactive delivery channel is invalid")

    connection = (
        await session.execute(
            select(
                IntegrationConnection.provider,
                IntegrationConnection.connection_type,
                IntegrationConnection.status,
            ).where(
                IntegrationConnection.id == ownership.connection_id,
                IntegrationConnection.subject_id == ownership.subject_id,
            )
        )
    ).one_or_none()
    if connection is None:
        raise ProactiveOwnershipScopeError(
            "proactive delivery connection does not match the subject"
        )
    provider, connection_type, status = connection
    if provider != channel:
        raise ProactiveOwnershipScopeError(
            "proactive notifier channel does not match its connection provider"
        )
    if connection_type != IntegrationConnectionType.RECIPIENT.value:
        raise ProactiveOwnershipScopeError(
            "proactive delivery connection is not a recipient binding"
        )
    known_statuses = {item.value for item in IntegrationConnectionStatus}
    if status not in known_statuses:
        raise ProactiveOwnershipScopeError(
            "proactive delivery connection has unknown lifecycle state"
        )
    if status not in {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }:
        raise ProactiveOwnershipScopeError("inactive proactive delivery connection cannot send")


async def _require_ai_invocation_scope(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext | None,
    category: str,
    ai_invocation_id: uuid.UUID | None,
) -> int | None:
    if ai_invocation_id is None:
        return None
    if not isinstance(ai_invocation_id, uuid.UUID):
        raise TypeError("ai_invocation_id must be a UUID or None")
    if ownership is None or category not in {CATEGORY_ECHO, CATEGORY_REPLY}:
        raise ProactiveOwnershipScopeError("AI delivery provenance requires an owned reply or echo")
    expected_purpose = (
        AIInvocationPurpose.SIGNAL_PARSE
        if category == CATEGORY_ECHO
        else AIInvocationPurpose.QUESTION_REPLY
    )
    allowed_statuses = {
        AIInvocationStatus.SUCCEEDED.value,
        AIInvocationStatus.FAILED.value,
        AIInvocationStatus.AMBIGUOUS.value,
    }
    if expected_purpose is AIInvocationPurpose.QUESTION_REPLY:
        # A reply reservation cancelled before provider I/O still owns the one
        # deterministic fallback journal for its raw question.
        allowed_statuses.add(AIInvocationStatus.CANCELLED.value)
    row = (
        await session.execute(
            select(
                AIInvocation.subject_id,
                AIInvocation.actor_user_id,
                AIInvocation.purpose,
                AIInvocation.source,
                AIInvocation.status,
                AIInvocation.raw_payload_id,
            ).where(AIInvocation.id == ai_invocation_id)
        )
    ).one_or_none()
    if row is None:
        raise ProactiveOwnershipScopeError("AI delivery invocation does not exist")
    subject_id, actor_user_id, purpose, source, status, raw_payload_id = row
    if (
        subject_id != ownership.subject_id
        or actor_user_id != ownership.recipient_user_id
        or purpose != expected_purpose.value
        or source != AIInvocationSource.TELEGRAM.value
        or status not in allowed_statuses
        or raw_payload_id is None
    ):
        raise ProactiveOwnershipScopeError("AI delivery invocation provenance is invalid")
    return raw_payload_id


async def _require_redacted_reply_raw_scope(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    invocation_raw_payload_id: int | None,
) -> None:
    """Prove the JSON journal marker points at the exact owned Telegram raw."""

    row = (
        await session.execute(
            select(
                RawPayload.subject_id,
                RawPayload.actor_user_id,
                RawPayload.file_asset_id,
                RawPayload.domain,
                RawPayload.source,
                RawPayload.processed_at,
                IntegrationConnection.subject_id,
                IntegrationConnection.provider,
                IntegrationConnection.connection_type,
                IntegrationConnection.status,
            )
            .join(
                IntegrationConnection,
                IntegrationConnection.id == RawPayload.integration_connection_id,
            )
            .where(RawPayload.id == raw_payload_id)
        )
    ).one_or_none()
    if row is None:
        raise ProactiveOwnershipScopeError("redacted reply raw provenance does not exist")
    (
        raw_subject_id,
        raw_actor_user_id,
        raw_file_asset_id,
        raw_domain,
        raw_source,
        raw_processed_at,
        connection_subject_id,
        connection_provider,
        connection_type,
        connection_status,
    ) = row
    if (
        raw_subject_id != ownership.subject_id
        or raw_actor_user_id != ownership.recipient_user_id
        or raw_file_asset_id is not None
        or raw_domain != _INBOUND_RAW_DOMAIN
        or raw_source != Source.TELEGRAM.value
        or raw_processed_at is None
        or connection_subject_id != ownership.subject_id
        or connection_provider != IntegrationProvider.TELEGRAM.value
        or connection_type != IntegrationConnectionType.RECIPIENT.value
        or connection_status not in HISTORICAL_RECIPIENT_STATUSES
        or (invocation_raw_payload_id is not None and invocation_raw_payload_id != raw_payload_id)
    ):
        raise ProactiveOwnershipScopeError("redacted reply raw provenance is invalid")


async def _require_zero_subject_legacy_delivery(session: AsyncSession) -> object:
    """Authorize one root transaction for the zero-subject compatibility path.

    PostgreSQL's advisory governance lock serializes identity bootstrap. SQLite
    has no advisory locks and ignores ``FOR UPDATE``, so its equivalent is a
    fresh ``BEGIN IMMEDIATE`` held through provider I/O and journaling.  An
    arbitrary pre-open transaction is rejected: otherwise a caller could check
    zero subjects without holding the lock that freezes a concurrent bootstrap.
    """

    try:
        transaction = await authorize_pre_identity_compatibility_transaction(session)
    except PreIdentityCompatibilityError:
        raise DurableDeliveryRequiredError(
            "zero-subject delivery requires a fresh guarded transaction"
        ) from None

    sync_session = session.sync_session
    if transaction is not sync_session.get_transaction():
        raise DeliveryCapabilityError("legacy delivery lost its guarded root transaction")
    identity_types = (User, HealthSubject, IntegrationConnection)
    pending_identity = (
        tuple(sync_session.new) + tuple(sync_session.dirty) + tuple(sync_session.deleted)
    )
    if any(isinstance(row, identity_types) for row in pending_identity):
        raise DurableDeliveryRequiredError("zero-subject delivery rejects pending identity changes")
    return transaction
