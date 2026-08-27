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
from datetime import (
    date as date_type,
    datetime,
    time as time_type,
    timezone,
)
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    IntegrationConnectionType,
    IntegrationProvider,
    NotificationDeliveryStatus,
    Source,
)
from vitals.models.identity import HealthSubject
from vitals.models.proactive import Notification, NotificationDeliveryIntent
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.services.proactive.ownership import ProactiveOwnershipContext
from vitals.utils.timeutils import now_local, now_utc

logger = logging.getLogger(__name__)

from vitals.services.proactive.delivery.contracts import (
    CATEGORY_BRIEF,
    CATEGORY_ECHO,
    CATEGORY_EVENING,
    CATEGORY_NUDGE,
    CATEGORY_REPLY,
    CATEGORY_TEST,
    HISTORICAL_RECIPIENT_STATUSES,
    INITIATIVE_CATEGORIES,
    QUIET_END,
    QUIET_START,
    DeliveryIdempotencyConflictError,
    DeliveryPolicyUnavailableError,
    DeliveryPreparationScope,
    DeliveryScopeError,
    DeliveryStateError,
    _DELIVERY_CATEGORIES,
    _INBOUND_RAW_DOMAIN,
    _INITIATIVE_CLAIM_STATUSES,
    _IntentSnapshot,
    _OPAQUE_KEY_RE,
    _aware_utc,
    _intent_fingerprint,
    _opaque_key,
    _policy_clock,
    _snapshot_fingerprint,
    _valid_external_id,
)

from vitals.services.proactive.delivery.policy import (
    ProactiveOwnershipScopeError,
    _lock_historical_delivery_roots,
    _lock_live_delivery_authority,
    _require_ai_invocation_scope,
    _require_zero_subject_legacy_delivery,
    _validate_ownership,
    notification_ownership_scope,
)


def in_quiet_hours(
    at: time_type, *, start: time_type = QUIET_START, end: time_type = QUIET_END
) -> bool:
    """Is ``at`` inside the quiet window? Handles a window that wraps midnight,
    because the settings card lets the owner set exactly that."""
    if start == end:
        return False
    if start < end:
        return start <= at < end
    return at >= start or at < end


def _owned_notification_candidate_scope(
    ownership: ProactiveOwnershipContext,
):
    owned = and_(
        Notification.subject_id == ownership.subject_id,
        Notification.recipient_user_id == ownership.recipient_user_id,
        Notification.integration_connection_id.is_not(None),
        or_(
            Notification.actor_user_id.is_(None),
            Notification.actor_user_id == ownership.recipient_user_id,
        ),
    )
    if not ownership.include_legacy_unowned:
        return owned
    return or_(
        owned,
        and_(
            Notification.subject_id.is_(None),
            Notification.actor_user_id.is_(None),
            Notification.recipient_user_id.is_(None),
            Notification.integration_connection_id.is_(None),
        ),
    )


async def _assert_no_malformed_notification_candidates(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    predicates: tuple = (),
) -> None:
    malformed = or_(
        and_(
            Notification.subject_id == ownership.subject_id,
            or_(
                Notification.recipient_user_id.is_(None),
                Notification.integration_connection_id.is_(None),
                and_(
                    Notification.actor_user_id.is_not(None),
                    Notification.actor_user_id != ownership.recipient_user_id,
                    Notification.recipient_user_id == ownership.recipient_user_id,
                ),
            ),
        ),
        and_(
            Notification.subject_id.is_(None),
            or_(
                Notification.actor_user_id.is_not(None),
                Notification.recipient_user_id.is_not(None),
                Notification.integration_connection_id.is_not(None),
            ),
        ),
    )
    query = select(Notification.id).where(*predicates, malformed).limit(1)
    if await session.scalar(query) is not None:
        raise DeliveryStateError("notification candidate has malformed ownership")


async def _validate_notification_read_graph(
    session: AsyncSession,
    *,
    row: Notification,
    ownership: ProactiveOwnershipContext,
) -> bool:
    """Validate one candidate journal; False means a different owned recipient."""

    fully_legacy = (
        row.subject_id is None
        and row.actor_user_id is None
        and row.recipient_user_id is None
        and row.integration_connection_id is None
    )
    if fully_legacy:
        if row.delivery_intent_id is not None or row.ai_invocation_id is not None:
            raise DeliveryStateError("legacy notification has owned provenance links")
        return ownership.include_legacy_unowned
    if row.subject_id is None:
        raise DeliveryStateError("notification has partial legacy ownership")
    if row.subject_id != ownership.subject_id:
        return False
    if row.recipient_user_id is None or row.integration_connection_id is None:
        raise DeliveryStateError("notification has partial subject ownership")
    if row.recipient_user_id != ownership.recipient_user_id:
        return False
    if row.actor_user_id not in {None, ownership.recipient_user_id}:
        raise DeliveryStateError("notification actor is outside the current scope")
    connection = await session.scalar(
        select(IntegrationConnection)
        .where(IntegrationConnection.id == row.integration_connection_id)
        .execution_options(populate_existing=True)
    )
    if (
        connection is None
        or connection.subject_id != row.subject_id
        or connection.provider != row.channel
        or connection.connection_type != IntegrationConnectionType.RECIPIENT.value
        or connection.status not in HISTORICAL_RECIPIENT_STATUSES
    ):
        raise DeliveryStateError("notification connection graph is inconsistent")
    if row.delivery_intent_id is not None:
        snapshots = list(
            await session.execute(
                _intent_snapshot_select().where(
                    NotificationDeliveryIntent.id == row.delivery_intent_id
                )
            )
        )
        if len(snapshots) != 1:
            raise DeliveryStateError("linked delivery intent does not exist")
        intent = await _lock_and_validate_intent_snapshot(
            session,
            snapshot=_snapshot_from_row(snapshots[0]),
            ownership=ownership,
        )
        if (
            row.subject_id != intent.subject_id
            or row.recipient_user_id != intent.recipient_user_id
            or row.actor_user_id != intent.actor_user_id
            or row.integration_connection_id != intent.integration_connection_id
            or row.ai_invocation_id != intent.ai_invocation_id
            or row.category != intent.category
            or row.channel != intent.channel
            or row.dedupe_key != intent.idempotency_key
        ):
            raise DeliveryStateError("notification/intent graph is inconsistent")
    elif row.ai_invocation_id is not None:
        # Legacy, unlinked journals still need the complete AI provenance
        # contract: exact S/Q, category-specific purpose, Telegram source,
        # terminal status, and a persisted raw input.  A UUID-shaped link alone
        # must never make a row trusted reply context.
        try:
            raw_payload_id = await _require_ai_invocation_scope(
                session,
                ownership=ownership,
                category=row.category,
                ai_invocation_id=row.ai_invocation_id,
            )
        except (ProactiveOwnershipScopeError, TypeError, ValueError):
            raise DeliveryStateError("notification AI graph is inconsistent") from None
        if raw_payload_id is None:
            raise DeliveryStateError("notification AI graph is inconsistent")
        raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_payload_id)
            .execution_options(populate_existing=True)
        )
        if (
            raw is None
            or raw.subject_id != row.subject_id
            or raw.actor_user_id != row.recipient_user_id
            or raw.integration_connection_id is None
            or raw.file_asset_id is not None
            or raw.domain != _INBOUND_RAW_DOMAIN
            or raw.source != Source.TELEGRAM.value
        ):
            raise DeliveryStateError("notification AI raw graph is inconsistent")
        raw_connection = await session.scalar(
            select(IntegrationConnection)
            .where(IntegrationConnection.id == raw.integration_connection_id)
            .execution_options(populate_existing=True)
        )
        if (
            raw_connection is None
            or raw_connection.subject_id != row.subject_id
            or raw_connection.provider != IntegrationProvider.TELEGRAM.value
            or raw_connection.connection_type != IntegrationConnectionType.RECIPIENT.value
            or raw_connection.status not in HISTORICAL_RECIPIENT_STATUSES
        ):
            raise DeliveryStateError("notification AI raw connection is inconsistent")
        if row.category == CATEGORY_REPLY and row.payload != {
            "content_redacted": True,
            "raw_payload_id": raw_payload_id,
        }:
            raise DeliveryStateError(
                "notification AI reply payload is not an exact redacted marker"
            )
    return True


async def sent_today(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    ownership: ProactiveOwnershipContext | None = None,
) -> int:
    """How much of today's budget is spent (self-initiated messages only)."""
    on_date = on_date or now_local().date()
    _validate_ownership(ownership)
    if ownership is None:
        await _require_zero_subject_legacy_delivery(session)
        return int(
            await session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.category.in_(INITIATIVE_CATEGORIES),
                    func.date(Notification.sent_at) == on_date,
                )
            )
            or 0
        )
    journal_predicates = (
        Notification.category.in_(INITIATIVE_CATEGORIES),
        func.date(Notification.sent_at) == on_date,
    )
    await _assert_no_malformed_notification_candidates(
        session,
        ownership=ownership,
        predicates=journal_predicates,
    )
    journal_rows = list(
        await session.scalars(
            select(Notification)
            .where(*journal_predicates, _owned_notification_candidate_scope(ownership))
            .order_by(Notification.id)
        )
    )
    count = 0
    for row in journal_rows:
        if row.delivery_intent_id is not None:
            # The durable intent is counted below, so its journal cannot consume
            # the budget twice.
            await _validate_notification_read_graph(
                session,
                row=row,
                ownership=ownership,
            )
            continue
        if await _validate_notification_read_graph(
            session,
            row=row,
            ownership=ownership,
        ):
            count += 1
    intent_rows = list(
        await session.execute(
            _intent_snapshot_select().where(
                NotificationDeliveryIntent.subject_id == ownership.subject_id,
                NotificationDeliveryIntent.recipient_user_id == ownership.recipient_user_id,
                NotificationDeliveryIntent.category.in_(INITIATIVE_CATEGORIES),
                NotificationDeliveryIntent.policy_date == on_date,
                NotificationDeliveryIntent.status.in_(_INITIATIVE_CLAIM_STATUSES),
            )
        )
    )
    for raw_snapshot in intent_rows:
        await _lock_and_validate_intent_snapshot(
            session,
            snapshot=_snapshot_from_row(raw_snapshot),
            ownership=ownership,
        )
        count += 1
    return count


async def find_sent(
    session: AsyncSession,
    external_id: str,
    *,
    ownership: ProactiveOwnershipContext | None = None,
) -> Optional[Notification]:
    """The journal row for a message id — how an incoming reply finds the context
    it is replying to."""
    _validate_ownership(ownership)
    if ownership is None:
        await _require_zero_subject_legacy_delivery(session)
        return await session.scalar(
            select(Notification)
            .where(Notification.external_id == str(external_id))
            .order_by(Notification.id.desc())
            .limit(1)
        )
    external_predicate = Notification.external_id == str(external_id)
    await _assert_no_malformed_notification_candidates(
        session,
        ownership=ownership,
        predicates=(external_predicate,),
    )
    rows = list(
        await session.scalars(
            select(Notification)
            .where(
                external_predicate,
                _owned_notification_candidate_scope(ownership),
            )
            .order_by(Notification.id.desc())
        )
    )
    for row in rows:
        if await _validate_notification_read_graph(
            session,
            row=row,
            ownership=ownership,
        ):
            return row
    return None


async def recent_sent(
    session: AsyncSession,
    *,
    limit: int = 3,
    ownership: ProactiveOwnershipContext | None = None,
) -> list[Notification]:
    """The last few messages we sent, oldest first — the context a question typed
    without Telegram's Reply has to be read against.

    Almost nothing is typed as a reply on mobile, so «что за ключ странный на
    второе» arrived with no message attached and was answered against the morning
    brief's JSON: the bot could not see the echo it had sent a minute earlier and
    guessed the owner meant the 2nd of the month.
    """
    _validate_ownership(ownership)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if ownership is None:
        await _require_zero_subject_legacy_delivery(session)
        result = await session.scalars(
            select(Notification).order_by(Notification.id.desc()).limit(limit)
        )
        return list(reversed(result.all()))
    await _assert_no_malformed_notification_candidates(
        session,
        ownership=ownership,
    )
    rows = list(
        await session.scalars(
            select(Notification)
            .where(_owned_notification_candidate_scope(ownership))
            .order_by(Notification.id.desc())
            .limit(limit)
        )
    )
    validated: list[Notification] = []
    for row in rows:
        if await _validate_notification_read_graph(
            session,
            row=row,
            ownership=ownership,
        ):
            validated.append(row)
            if len(validated) == limit:
                break
    return list(reversed(validated))


async def already_sent(
    session: AsyncSession,
    dedupe_key: str,
    *,
    ownership: ProactiveOwnershipContext | None = None,
    legacy_dedupe_key: str | None = None,
) -> bool:
    _validate_ownership(ownership)
    if ownership is None:
        await _require_zero_subject_legacy_delivery(session)
        keys = {dedupe_key}
        if legacy_dedupe_key is not None:
            keys.add(_validate_legacy_dedupe_key(legacy_dedupe_key))
        return (
            await session.scalar(select(Notification.id).where(Notification.dedupe_key.in_(keys)))
            is not None
        )
    if ownership is not None and _OPAQUE_KEY_RE.fullmatch(dedupe_key):
        if await delivery_claim_exists(
            session,
            idempotency_key=dedupe_key,
            ownership=ownership,
        ):
            return True
    keys = {dedupe_key}
    if legacy_dedupe_key is not None:
        keys.add(_validate_legacy_dedupe_key(legacy_dedupe_key))
    key_predicate = Notification.dedupe_key.in_(keys)
    await _assert_no_malformed_notification_candidates(
        session,
        ownership=ownership,
        predicates=(key_predicate,),
    )
    rows = list(
        await session.scalars(
            select(Notification)
            .where(
                key_predicate,
                _owned_notification_candidate_scope(ownership),
            )
            .order_by(Notification.id)
        )
    )
    for row in rows:
        if await _validate_notification_read_graph(
            session,
            row=row,
            ownership=ownership,
        ):
            return True
    return False


async def _initiative_claim_count(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    policy_date: date_type,
) -> int:
    intent_count = await session.scalar(
        select(func.count())
        .select_from(NotificationDeliveryIntent)
        .where(
            NotificationDeliveryIntent.subject_id == ownership.subject_id,
            NotificationDeliveryIntent.recipient_user_id == ownership.recipient_user_id,
            NotificationDeliveryIntent.policy_date == policy_date,
            NotificationDeliveryIntent.category.in_(INITIATIVE_CATEGORIES),
            NotificationDeliveryIntent.status.in_(_INITIATIVE_CLAIM_STATUSES),
        )
    )
    journal_scope = or_(
        and_(
            Notification.subject_id == ownership.subject_id,
            Notification.recipient_user_id == ownership.recipient_user_id,
            or_(
                Notification.actor_user_id.is_(None),
                Notification.actor_user_id == ownership.recipient_user_id,
            ),
        ),
        and_(
            ownership.include_legacy_unowned,
            Notification.subject_id.is_(None),
            Notification.actor_user_id.is_(None),
            Notification.recipient_user_id.is_(None),
            Notification.integration_connection_id.is_(None),
        ),
    )
    journal_count = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.delivery_intent_id.is_(None),
            Notification.category.in_(INITIATIVE_CATEGORIES),
            func.date(Notification.sent_at) == policy_date,
            journal_scope,
        )
    )
    return int(intent_count or 0) + int(journal_count or 0)


async def _initiative_claim_is_within_budget(
    session: AsyncSession,
    *,
    intent: NotificationDeliveryIntent,
    ownership: ProactiveOwnershipContext,
    daily_budget: int,
) -> bool:
    journal_scope = or_(
        and_(
            Notification.subject_id == ownership.subject_id,
            Notification.recipient_user_id == ownership.recipient_user_id,
            or_(
                Notification.actor_user_id.is_(None),
                Notification.actor_user_id == ownership.recipient_user_id,
            ),
        ),
        and_(
            ownership.include_legacy_unowned,
            Notification.subject_id.is_(None),
            Notification.actor_user_id.is_(None),
            Notification.recipient_user_id.is_(None),
            Notification.integration_connection_id.is_(None),
        ),
    )
    legacy_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.delivery_intent_id.is_(None),
                Notification.category.in_(INITIATIVE_CATEGORIES),
                func.date(Notification.sent_at) == intent.policy_date,
                journal_scope,
            )
        )
        or 0
    )
    claim_ids = list(
        await session.scalars(
            select(NotificationDeliveryIntent.id)
            .where(
                NotificationDeliveryIntent.subject_id == intent.subject_id,
                NotificationDeliveryIntent.recipient_user_id == intent.recipient_user_id,
                NotificationDeliveryIntent.policy_date == intent.policy_date,
                NotificationDeliveryIntent.category.in_(INITIATIVE_CATEGORIES),
                NotificationDeliveryIntent.status.in_(_INITIATIVE_CLAIM_STATUSES),
            )
            .order_by(
                NotificationDeliveryIntent.policy_at,
                NotificationDeliveryIntent.id,
            )
        )
    )
    try:
        rank = claim_ids.index(intent.id) + 1
    except ValueError:
        raise DeliveryStateError("initiative claim is missing from its budget") from None
    return legacy_count + rank <= daily_budget


def _existing_intent_metadata(intent: NotificationDeliveryIntent) -> tuple:
    # Connection rotation alone must not reopen an occurrence.  The unique key
    # is S/Q-scoped, while every other caller-controlled provenance field must
    # remain identical.
    metadata = (
        intent.subject_id,
        intent.recipient_user_id,
        intent.actor_user_id,
        intent.raw_payload_id,
        intent.ai_invocation_id,
        intent.category,
        intent.channel,
        intent.idempotency_key,
        intent.policy_key,
    )
    if intent.category in INITIATIVE_CATEGORIES:
        return (*metadata, intent.policy_date)
    return metadata


async def _matching_journal_claim(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    actor_user_id: uuid.UUID | None,
    category: str,
    channel: str,
    idempotency_key: str,
    legacy_dedupe_key: str | None,
    raw_payload_id: int | None,
    ai_invocation_id: uuid.UUID | None,
) -> Notification | None:
    keys = {idempotency_key}
    if legacy_dedupe_key is not None:
        keys.add(legacy_dedupe_key)
    predicates = [Notification.dedupe_key.in_(keys)]
    if ai_invocation_id is not None:
        predicates.append(Notification.ai_invocation_id == ai_invocation_id)
    if raw_payload_id is not None:
        predicates.append(
            and_(
                Notification.category == category,
                Notification.payload["raw_payload_id"].as_integer() == raw_payload_id,
            )
        )
    candidate_scope = or_(
        Notification.subject_id == ownership.subject_id,
        and_(
            Notification.subject_id.is_(None),
            or_(
                Notification.actor_user_id.is_not(None),
                Notification.recipient_user_id.is_not(None),
                Notification.integration_connection_id.is_not(None),
                and_(
                    Notification.actor_user_id.is_(None),
                    Notification.recipient_user_id.is_(None),
                    Notification.integration_connection_id.is_(None),
                ),
            ),
        ),
    )
    rows = list(
        await session.scalars(
            select(Notification)
            .where(or_(*predicates), candidate_scope)
            .order_by(Notification.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    for row in rows:
        fully_legacy = (
            row.subject_id is None
            and row.actor_user_id is None
            and row.recipient_user_id is None
            and row.integration_connection_id is None
        )
        if fully_legacy:
            if not ownership.include_legacy_unowned:
                continue
            if row.category != category or row.channel != channel:
                raise DeliveryIdempotencyConflictError(
                    "legacy notification claim metadata conflicts"
                )
            return row
        if row.subject_id is None:
            raise DeliveryIdempotencyConflictError(
                "notification claim has partial legacy ownership"
            )
        if row.recipient_user_id is None or row.integration_connection_id is None:
            raise DeliveryIdempotencyConflictError(
                "notification claim has partial subject ownership"
            )
        if (
            row.subject_id != ownership.subject_id
            or row.recipient_user_id != ownership.recipient_user_id
        ):
            continue
        valid_owned = await session.scalar(
            select(Notification.id).where(
                Notification.id == row.id,
                notification_ownership_scope(
                    ownership,
                    connection_scoped=False,
                ),
            )
        )
        if valid_owned is None:
            raise DeliveryIdempotencyConflictError(
                "notification claim has invalid owned provenance"
            )
        if (
            row.actor_user_id != actor_user_id
            or row.category != category
            or row.channel != channel
            or (ai_invocation_id is not None and row.ai_invocation_id != ai_invocation_id)
        ):
            raise DeliveryIdempotencyConflictError("notification claim metadata conflicts")
        return row
    return None


def _validate_legacy_dedupe_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("legacy_dedupe_key must be a non-blank bounded string")
    return value


def _validate_reply_target(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value)
    if not normalized.isdigit() or int(normalized) <= 0 or len(normalized) > 64:
        raise ValueError("reply_to must be a positive Telegram message id")
    return normalized


def _validate_delivery_category(category: str) -> str:
    if category not in _DELIVERY_CATEGORIES:
        raise ValueError("delivery category is invalid")
    return category


def _preparation_scope_fingerprint(scope: DeliveryPreparationScope) -> tuple:
    authority = scope._authority
    return (
        scope._ownership_subject_id,
        scope._ownership_recipient_user_id,
        scope._ownership_connection_id,
        scope._include_legacy_unowned,
        scope._actor_user_id,
        scope._category,
        authority.binding,
        authority.timezone.key,
        authority.credential_ref,
        authority.telegram_connection_ids,
        scope._module_enabled,
        scope._policy,
        scope._policy_at,
        scope._local_at,
    )


async def delivery_claim_exists(
    session: AsyncSession,
    *,
    idempotency_key: str,
    ownership: ProactiveOwnershipContext,
) -> bool:
    """Return whether any durable terminal or in-flight occurrence exists."""

    _validate_ownership(ownership)
    key = _opaque_key(idempotency_key, field_name="idempotency_key")
    rows = list(
        await session.execute(
            _intent_snapshot_select().where(
                NotificationDeliveryIntent.subject_id == ownership.subject_id,
                NotificationDeliveryIntent.recipient_user_id == ownership.recipient_user_id,
                NotificationDeliveryIntent.idempotency_key == key,
            )
        )
    )
    if not rows:
        return False
    if len(rows) != 1:
        raise DeliveryStateError("idempotency key has multiple delivery claims")
    snapshot = _snapshot_from_row(rows[0])
    await _lock_and_validate_intent_snapshot(
        session,
        snapshot=snapshot,
        ownership=ownership,
    )
    return True


async def confirmed_delivery_journal(
    session: AsyncSession,
    *,
    idempotency_key: str,
    category: str,
    ownership: ProactiveOwnershipContext,
    legacy_dedupe_key: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Notification | None:
    """Return exact evidence that an occurrence is durably SENT.

    Unlike :func:`delivery_claim_exists`, PENDING, DISPATCHING, AMBIGUOUS, and
    CANCELLED claims return ``None``. This is the sequencing gate for products
    whose second message must never leapfrog an uncertain first message.
    """

    _validate_ownership(ownership, actor_user_id=actor_user_id)
    key = _opaque_key(idempotency_key, field_name="idempotency_key")
    if category not in {
        CATEGORY_BRIEF,
        CATEGORY_EVENING,
        CATEGORY_NUDGE,
        CATEGORY_REPLY,
        CATEGORY_ECHO,
        CATEGORY_TEST,
    }:
        raise ValueError("delivery category is invalid")
    legacy_key = _validate_legacy_dedupe_key(legacy_dedupe_key)
    rows = list(
        await session.execute(
            _intent_snapshot_select().where(
                NotificationDeliveryIntent.subject_id == ownership.subject_id,
                NotificationDeliveryIntent.recipient_user_id == ownership.recipient_user_id,
                NotificationDeliveryIntent.idempotency_key == key,
            )
        )
    )
    if len(rows) > 1:
        raise DeliveryStateError("idempotency key has multiple delivery claims")
    if rows:
        intent = await _lock_and_validate_intent_snapshot(
            session,
            snapshot=_snapshot_from_row(rows[0]),
            ownership=ownership,
        )
        if (
            intent.actor_user_id != actor_user_id
            or intent.category != category
            or intent.channel != IntegrationProvider.TELEGRAM.value
        ):
            raise DeliveryIdempotencyConflictError("delivery idempotency key metadata conflicts")
        if intent.status != NotificationDeliveryStatus.SENT.value:
            return None
        return await _validate_linked_journal_for_intent(session, intent=intent)

    legacy_journal = await _matching_journal_claim(
        session,
        ownership=ownership,
        actor_user_id=actor_user_id,
        category=category,
        channel=IntegrationProvider.TELEGRAM.value,
        idempotency_key=key,
        legacy_dedupe_key=legacy_key,
        raw_payload_id=None,
        ai_invocation_id=None,
    )
    if legacy_journal is None or _valid_external_id(legacy_journal.external_id) is None:
        return None
    return legacy_journal


async def delivery_claim_for_raw(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    category: str,
    ownership: ProactiveOwnershipContext,
) -> NotificationDeliveryIntent | None:
    """Return the newest exact S/Q raw-backed claim for recovery decisions."""

    _validate_ownership(ownership)
    if (
        isinstance(raw_payload_id, bool)
        or not isinstance(raw_payload_id, int)
        or raw_payload_id < 1
    ):
        raise ValueError("raw_payload_id must be a positive integer")
    if category not in {CATEGORY_REPLY, CATEGORY_ECHO}:
        raise ValueError("raw delivery claims are reply or echo only")
    rows = list(
        await session.execute(
            _intent_snapshot_select()
            .where(
                NotificationDeliveryIntent.subject_id == ownership.subject_id,
                NotificationDeliveryIntent.recipient_user_id == ownership.recipient_user_id,
                NotificationDeliveryIntent.raw_payload_id == raw_payload_id,
                NotificationDeliveryIntent.category == category,
            )
            .order_by(NotificationDeliveryIntent.id)
        )
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise DeliveryStateError("raw payload has multiple delivery claims")
    snapshot = _snapshot_from_row(rows[0])
    return await _lock_and_validate_intent_snapshot(
        session,
        snapshot=snapshot,
        ownership=ownership,
    )


async def delivery_policy_claimed_since(
    session: AsyncSession,
    *,
    policy_key: str,
    not_before: datetime,
    ownership: ProactiveOwnershipContext,
    legacy_dedupe_prefix: str | None = None,
) -> bool:
    """Whether a nudge policy has a certain or uncertain recent initiative claim."""

    _validate_ownership(ownership)
    key = _opaque_key(policy_key, field_name="policy_key")
    if (
        not isinstance(not_before, datetime)
        or not_before.tzinfo is None
        or not_before.utcoffset() is None
    ):
        raise ValueError("not_before must be timezone-aware")
    cutoff = not_before.astimezone(timezone.utc)
    # This check is a reservation gate, not an eventually-consistent report.
    # Hold the canonical subject/current-recipient locks through the query and
    # the caller's immediately-following T1 in the same outer transaction so
    # two distinct hourly occurrence keys cannot both clear one cooldown.
    await _lock_live_delivery_authority(
        session,
        ownership=ownership,
        actor_user_id=None,
    )
    rows = list(
        await session.execute(
            _intent_snapshot_select()
            .where(
                NotificationDeliveryIntent.subject_id == ownership.subject_id,
                NotificationDeliveryIntent.recipient_user_id == ownership.recipient_user_id,
                NotificationDeliveryIntent.policy_key == key,
                NotificationDeliveryIntent.status.in_(_INITIATIVE_CLAIM_STATUSES),
                NotificationDeliveryIntent.policy_at >= cutoff,
            )
            .order_by(NotificationDeliveryIntent.policy_at.desc())
        )
    )
    for raw_snapshot in rows:
        await _lock_and_validate_intent_snapshot(
            session,
            snapshot=_snapshot_from_row(raw_snapshot),
            ownership=ownership,
        )
    if rows:
        return True
    if legacy_dedupe_prefix is None:
        return False
    if (
        not isinstance(legacy_dedupe_prefix, str)
        or not legacy_dedupe_prefix
        or len(legacy_dedupe_prefix) > 128
    ):
        raise ValueError("legacy_dedupe_prefix is invalid")
    escaped_legacy_prefix = (
        legacy_dedupe_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == ownership.subject_id)
        .execution_options(populate_existing=True)
    )
    if subject is None:
        raise DeliveryScopeError("delivery subject does not exist")
    try:
        zone = ZoneInfo(subject.timezone)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise DeliveryPolicyUnavailableError("health subject timezone is invalid") from exc
    local_cutoff = cutoff.astimezone(zone).replace(tzinfo=None)
    legacy_predicates = (
        Notification.dedupe_key.like(
            f"{escaped_legacy_prefix}%",
            escape="\\",
        ),
        Notification.sent_at >= local_cutoff,
    )
    await _assert_no_malformed_notification_candidates(
        session,
        ownership=ownership,
        predicates=legacy_predicates,
    )
    journals = list(
        await session.scalars(
            select(Notification)
            .where(
                *legacy_predicates,
                _owned_notification_candidate_scope(ownership),
            )
            .order_by(Notification.sent_at.desc(), Notification.id.desc())
        )
    )
    for row in journals:
        if await _validate_notification_read_graph(
            session,
            row=row,
            ownership=ownership,
        ):
            return True
    return False


async def delivery_policy_clock(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Freeze one instant and its subject-local representation under S/C locks.

    Rule engines use this before calculating cooldown cutoffs so a subject
    timezone change cannot race the subsequent claim check and T1 reservation.
    The first value is aware UTC; the second is aware subject-local time.
    """

    _validate_ownership(ownership)
    authority = await _lock_live_delivery_authority(
        session,
        ownership=ownership,
        actor_user_id=None,
    )
    return _policy_clock(timezone_value=authority.timezone, now=now)


def _reconciliation_now(value: datetime | None) -> datetime:
    current = value or now_utc()
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("reconciliation now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _reconciliation_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise ValueError("reconciliation limit must be between 1 and 1000")
    return value


def _snapshot_from_row(row) -> _IntentSnapshot:
    values = tuple(row)
    if len(values) != 13:
        raise DeliveryStateError("delivery reconciliation snapshot is invalid")
    mutable = list(values)
    mutable[11] = _aware_utc(mutable[11])
    return _IntentSnapshot(*mutable)


def _intent_snapshot_select():
    return select(
        NotificationDeliveryIntent.id,
        NotificationDeliveryIntent.subject_id,
        NotificationDeliveryIntent.recipient_user_id,
        NotificationDeliveryIntent.actor_user_id,
        NotificationDeliveryIntent.integration_connection_id,
        NotificationDeliveryIntent.raw_payload_id,
        NotificationDeliveryIntent.ai_invocation_id,
        NotificationDeliveryIntent.category,
        NotificationDeliveryIntent.channel,
        NotificationDeliveryIntent.idempotency_key,
        NotificationDeliveryIntent.policy_key,
        NotificationDeliveryIntent.policy_at,
        NotificationDeliveryIntent.policy_date,
    )


async def _validate_linked_journal_for_intent(
    session: AsyncSession,
    *,
    intent: NotificationDeliveryIntent,
) -> Notification | None:
    rows = list(
        await session.scalars(
            select(Notification)
            .where(Notification.delivery_intent_id == intent.id)
            .order_by(Notification.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(rows) > 1:
        raise DeliveryStateError("delivery intent has multiple linked journals")
    row = rows[0] if rows else None
    if intent.status == NotificationDeliveryStatus.SENT.value:
        if row is None:
            raise DeliveryStateError("sent delivery intent has no linked journal")
        if (
            row.subject_id != intent.subject_id
            or row.recipient_user_id != intent.recipient_user_id
            or row.actor_user_id != intent.actor_user_id
            or row.integration_connection_id != intent.integration_connection_id
            or row.ai_invocation_id != intent.ai_invocation_id
            or row.category != intent.category
            or row.channel != intent.channel
            or row.dedupe_key != intent.idempotency_key
            or _valid_external_id(row.external_id) is None
        ):
            raise DeliveryStateError("linked delivery journal graph is inconsistent")
        return row
    if row is not None:
        raise DeliveryStateError("non-sent delivery intent has a linked journal")
    return None


async def _lock_and_validate_intent_snapshot(
    session: AsyncSession,
    *,
    snapshot: _IntentSnapshot,
    ownership: ProactiveOwnershipContext | None = None,
) -> NotificationDeliveryIntent:
    if ownership is not None and (
        snapshot.subject_id != ownership.subject_id
        or snapshot.recipient_user_id != ownership.recipient_user_id
        or snapshot.actor_user_id not in {None, ownership.recipient_user_id}
    ):
        raise DeliveryStateError("delivery claim is outside the authorized scope")
    await _lock_historical_delivery_roots(session, intent=snapshot)
    intent = await session.scalar(
        select(NotificationDeliveryIntent)
        .where(NotificationDeliveryIntent.id == snapshot.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if intent is None or _intent_fingerprint(intent) != _snapshot_fingerprint(snapshot):
        raise DeliveryStateError("delivery claim changed during graph validation")
    try:
        NotificationDeliveryStatus(intent.status)
    except ValueError as exc:
        raise DeliveryStateError("delivery claim has an unknown status") from exc
    await _validate_linked_journal_for_intent(session, intent=intent)
    return intent
