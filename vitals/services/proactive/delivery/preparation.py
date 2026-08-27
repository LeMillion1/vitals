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
    datetime,
)
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    NotificationDeliveryErrorCode,
    NotificationDeliveryStatus,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.proactive import NotificationDeliveryIntent
from vitals.models.tenancy import IntegrationConnection
from vitals.services.proactive.channels import (
    BoundNotifier,
    Buttons,
    canonicalize_buttons,
    canonicalize_text,
)
from vitals.services.proactive.ownership import ProactiveOwnershipContext

logger = logging.getLogger(__name__)

from vitals.services.proactive.delivery.contracts import (
    CATEGORY_ECHO,
    CATEGORY_NUDGE,
    CATEGORY_REPLY,
    INITIATIVE_CATEGORIES,
    DeliveryCapabilityError,
    DeliveryIdempotencyConflictError,
    DeliveryPolicyUnavailableError,
    DeliveryPreparationScope,
    DeliveryScopeError,
    DeliveryStateError,
    PreparedDeliveryIntent,
    _DeliveryPolicy,
    _LockedDeliveryAuthority,
    _PREPARATION_SCOPE_SEAL,
    _aware_utc,
    _binding_for,
    _opaque_key,
    _policy_clock,
    _same_binding,
)

from vitals.services.proactive.delivery.policy import (
    _load_locked_delivery_policy,
    _lock_live_delivery_authority,
    _lock_raw_and_ai_provenance,
    _locked_signals_module_enabled,
    _read_delivery_policy,
    _validate_ownership,
)

from vitals.services.proactive.delivery.queries import (
    _existing_intent_metadata,
    _initiative_claim_count,
    _matching_journal_claim,
    _preparation_scope_fingerprint,
    _reconciliation_now,
    _validate_delivery_category,
    _validate_legacy_dedupe_key,
    _validate_linked_journal_for_intent,
    _validate_reply_target,
    in_quiet_hours,
)


async def lock_delivery_preparation_scope(
    session: AsyncSession,
    notifier: BoundNotifier | None,
    *,
    category: str,
    ownership: ProactiveOwnershipContext,
    actor_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> DeliveryPreparationScope | None:
    """Prelock every live root needed by a composed delivery T1 transaction.

    Callers that must atomically persist deterministic domain state and a PENDING
    delivery intent acquire this scope before taking any raw/domain locks, then
    pass it to :func:`prepare_delivery_intent` in the same exact outer
    transaction. No rendered payload is retained here.
    """

    if not isinstance(session, AsyncSession):
        raise TypeError("session must be an AsyncSession")
    if session.in_nested_transaction():
        raise DeliveryCapabilityError("delivery preparation scope requires an outer transaction")
    if notifier is None or not isinstance(notifier, BoundNotifier):
        return None
    category = _validate_delivery_category(category)
    _validate_ownership(ownership, actor_user_id=actor_user_id)
    if ownership is None:
        raise TypeError("delivery preparation scope requires ownership")
    expected_binding = _binding_for(ownership)
    if notifier.channel != expected_binding.channel or not _same_binding(
        notifier.binding, expected_binding
    ):
        raise DeliveryScopeError("notifier binding does not match delivery ownership")

    authority = await _lock_live_delivery_authority(
        session,
        ownership=ownership,
        actor_user_id=actor_user_id,
    )
    if not _same_binding(authority.binding, notifier.binding):
        raise DeliveryScopeError("notifier binding is not the current endpoint")
    module_enabled = await _locked_signals_module_enabled(
        session,
        subject_id=ownership.subject_id,
    )
    policy = await _load_locked_delivery_policy(session, authority=authority)
    policy_at, local_at = _policy_clock(
        timezone_value=authority.timezone,
        now=now,
    )
    return DeliveryPreparationScope._issue(
        session=session,
        authority=authority,
        module_enabled=module_enabled,
        policy=policy,
        policy_at=policy_at,
        local_at=local_at,
        category=category,
        ownership=ownership,
        actor_user_id=actor_user_id,
    )


async def _revalidate_delivery_preparation_scope(
    session: AsyncSession,
    scope: DeliveryPreparationScope,
) -> None:
    """Reproject already-locked authority and policy without taking earlier locks."""

    # The strict policy projection intentionally uses ``no_autoflush``. Flush the
    # caller's composed domain state and any same-session root/policy mutation now
    # so revalidation can never authorize the intent from a stale ORM snapshot.
    await session.flush()
    subject_rows = list(
        await session.scalars(
            select(HealthSubject)
            .order_by(HealthSubject.id)
            .limit(2)
            .execution_options(populate_existing=True)
        )
    )
    if len(subject_rows) != 1:
        raise DeliveryScopeError("delivery preparation subject scope changed")
    subject = subject_rows[0]
    if (
        subject.id != scope._ownership_subject_id
        or subject.owner_user_id != scope._ownership_recipient_user_id
    ):
        raise DeliveryScopeError("delivery preparation subject authority changed")
    try:
        subject_timezone = ZoneInfo(subject.timezone)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise DeliveryScopeError("delivery preparation timezone changed") from exc
    if subject_timezone.key != scope._authority.timezone.key:
        raise DeliveryScopeError("delivery preparation timezone changed")

    user_ids = sorted(
        {
            scope._ownership_recipient_user_id,
            *({scope._actor_user_id} if scope._actor_user_id is not None else set()),
        },
        key=str,
    )
    users = list(
        await session.scalars(
            select(User)
            .where(User.id.in_(user_ids))
            .order_by(User.id)
            .execution_options(populate_existing=True)
        )
    )
    if len(users) != len(user_ids) or any(row.status != UserStatus.ACTIVE.value for row in users):
        raise DeliveryScopeError("delivery preparation user authority changed")

    connections = list(
        await session.scalars(
            select(IntegrationConnection)
            .where(
                IntegrationConnection.subject_id == scope._ownership_subject_id,
                IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
                IntegrationConnection.connection_type == IntegrationConnectionType.RECIPIENT.value,
            )
            .order_by(IntegrationConnection.id)
            .execution_options(populate_existing=True)
        )
    )
    known_statuses = {item.value for item in IntegrationConnectionStatus}
    current = [
        row for row in connections if row.status != IntegrationConnectionStatus.RETIRED.value
    ]
    if (
        frozenset(row.id for row in connections) != scope._authority.telegram_connection_ids
        or any(row.status not in known_statuses for row in connections)
        or len(current) != 1
        or current[0].id != scope._ownership_connection_id
        or current[0].status
        not in {
            IntegrationConnectionStatus.LEGACY.value,
            IntegrationConnectionStatus.ACTIVE.value,
        }
        or current[0].credential_ref != scope._authority.credential_ref
    ):
        raise DeliveryScopeError("delivery preparation connection authority changed")

    # The module re-check stood here — the layer's master switch could be turned
    # off between preparing a send and committing it, and a message already
    # composed had to stop. There is no switch any more (see
    # ``_locked_signals_module_enabled``), so there is nothing to have changed.
    policy = await _read_delivery_policy(session, authority=scope._authority)
    if policy != scope._policy:
        raise DeliveryPolicyUnavailableError(
            "Telegram delivery policy changed during delivery preparation"
        )


async def _consume_delivery_preparation_scope(
    session: AsyncSession,
    scope: DeliveryPreparationScope,
    *,
    notifier: BoundNotifier,
    category: str,
    ownership: ProactiveOwnershipContext,
    actor_user_id: uuid.UUID | None,
    now: datetime | None,
) -> tuple[_LockedDeliveryAuthority, bool, _DeliveryPolicy, datetime, datetime]:
    if (
        not isinstance(scope, DeliveryPreparationScope)
        or scope._seal is not _PREPARATION_SCOPE_SEAL
        or scope._consumed
        or scope._session is not session
        or scope._transaction is None
        or session.sync_session.get_transaction() is not scope._transaction
        or session.in_nested_transaction()
        or scope._fingerprint != _preparation_scope_fingerprint(scope)
    ):
        raise DeliveryCapabilityError(
            "delivery preparation scope is forged, expired, or already consumed"
        )
    expected_binding = _binding_for(ownership)
    if (
        scope._ownership_subject_id != ownership.subject_id
        or scope._ownership_recipient_user_id != ownership.recipient_user_id
        or scope._ownership_connection_id != ownership.connection_id
        or scope._include_legacy_unowned != ownership.include_legacy_unowned
        or scope._actor_user_id != actor_user_id
        or scope._category != category
        or notifier.channel != expected_binding.channel
        or not _same_binding(notifier.binding, expected_binding)
        or not _same_binding(scope._authority.binding, expected_binding)
    ):
        raise DeliveryCapabilityError("delivery preparation scope does not match the continuation")
    if now is not None:
        policy_at, local_at = _policy_clock(
            timezone_value=scope._authority.timezone,
            now=now,
        )
        if policy_at != scope._policy_at or local_at != scope._local_at:
            raise DeliveryCapabilityError("delivery preparation clock changed after prelock")
    values = (
        scope._authority,
        scope._module_enabled,
        scope._policy,
        scope._policy_at,
        scope._local_at,
    )
    scope._invalidate()
    await _revalidate_delivery_preparation_scope(session, scope)
    return values


async def prepare_delivery_intent(
    session: AsyncSession,
    notifier: BoundNotifier | None,
    *,
    text: str,
    category: str,
    idempotency_key: str,
    policy_key: str | None = None,
    legacy_dedupe_key: str | None = None,
    buttons: Optional[Buttons] = None,
    reply_to: str | None = None,
    now: datetime | None = None,
    ownership: ProactiveOwnershipContext,
    actor_user_id: uuid.UUID | None = None,
    raw_payload_id: int | None = None,
    ai_invocation_id: uuid.UUID | None = None,
    redact_journal_content: bool = False,
    preparation_scope: DeliveryPreparationScope | None = None,
) -> PreparedDeliveryIntent | None:
    """T1: reserve one fresh, non-PHI, subject-scoped outbound claim.

    The returned capability is armed only by the caller's successful outer
    commit.  An existing intent in any state suppresses dispatch; its missing
    payload can never be reconstructed from caller-supplied text.
    """

    if session.in_nested_transaction():
        raise DeliveryCapabilityError("delivery preparation requires an outer transaction")
    if notifier is None or not isinstance(notifier, BoundNotifier):
        if preparation_scope is not None:
            raise DeliveryCapabilityError(
                "scoped delivery continuation requires an exact bound notifier"
            )
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    text = canonicalize_text(text)
    frozen_buttons = canonicalize_buttons(buttons)
    category = _validate_delivery_category(category)
    idempotency_key = _opaque_key(
        idempotency_key,
        field_name="idempotency_key",
    )
    if category == CATEGORY_NUDGE:
        if policy_key is None:
            raise ValueError("nudge delivery requires policy_key")
        policy_key = _opaque_key(policy_key, field_name="policy_key")
    elif policy_key is not None:
        raise ValueError("policy_key is allowed only for nudge delivery")
    legacy_dedupe_key = _validate_legacy_dedupe_key(legacy_dedupe_key)
    reply_to = _validate_reply_target(reply_to)
    if raw_payload_id is not None and (
        isinstance(raw_payload_id, bool)
        or not isinstance(raw_payload_id, int)
        or raw_payload_id < 1
    ):
        raise ValueError("raw_payload_id must be a positive integer or None")
    if raw_payload_id is not None and category not in {CATEGORY_REPLY, CATEGORY_ECHO}:
        raise DeliveryScopeError("raw provenance is allowed only for reply or echo")
    if ai_invocation_id is not None and raw_payload_id is None:
        raise DeliveryScopeError("AI delivery provenance requires a raw payload")
    if not isinstance(redact_journal_content, bool):
        raise TypeError("redact_journal_content must be a bool")
    if redact_journal_content and (category != CATEGORY_REPLY or raw_payload_id is None):
        raise DeliveryScopeError("redacted delivery journals require a raw-backed reply")
    if ai_invocation_id is not None and category == CATEGORY_REPLY and not redact_journal_content:
        raise DeliveryScopeError("AI question replies require a redacted journal")
    _validate_ownership(ownership, actor_user_id=actor_user_id)
    expected_binding = _binding_for(ownership)
    if notifier is not None and (
        notifier.channel != expected_binding.channel
        or not _same_binding(notifier.binding, expected_binding)
    ):
        raise DeliveryScopeError("notifier binding does not match delivery ownership")

    if preparation_scope is None:
        preparation_scope = await lock_delivery_preparation_scope(
            session,
            notifier,
            category=category,
            ownership=ownership,
            actor_user_id=actor_user_id,
            now=now,
        )
        if preparation_scope is None:
            return None
    (
        authority,
        module_enabled,
        policy,
        policy_at,
        local_at,
    ) = await _consume_delivery_preparation_scope(
        session,
        preparation_scope,
        notifier=notifier,
        category=category,
        ownership=ownership,
        actor_user_id=actor_user_id,
        now=now,
    )
    provisional = NotificationDeliveryIntent(
        subject_id=ownership.subject_id,
        recipient_user_id=ownership.recipient_user_id,
        actor_user_id=actor_user_id,
        integration_connection_id=ownership.connection_id,
        raw_payload_id=raw_payload_id,
        ai_invocation_id=ai_invocation_id,
        category=category,
        channel=authority.binding.channel,
        idempotency_key=idempotency_key,
        policy_key=policy_key,
        policy_at=policy_at,
        policy_date=local_at.date(),
        status=NotificationDeliveryStatus.PENDING.value,
    )
    await _lock_raw_and_ai_provenance(
        session,
        intent=provisional,
        telegram_connection_ids=authority.telegram_connection_ids,
    )

    existing = await session.scalar(
        select(NotificationDeliveryIntent)
        .where(
            NotificationDeliveryIntent.subject_id == ownership.subject_id,
            NotificationDeliveryIntent.recipient_user_id == ownership.recipient_user_id,
            NotificationDeliveryIntent.idempotency_key == idempotency_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    expected_metadata = _existing_intent_metadata(provisional)
    if existing is not None:
        if _existing_intent_metadata(existing) != expected_metadata:
            raise DeliveryIdempotencyConflictError("delivery idempotency key metadata conflicts")
        if existing.integration_connection_id not in authority.telegram_connection_ids:
            raise DeliveryStateError("existing delivery intent has invalid historical connection")
        await _validate_linked_journal_for_intent(session, intent=existing)
        return None
    if raw_payload_id is not None:
        raw_claims = list(
            await session.scalars(
                select(NotificationDeliveryIntent)
                .where(
                    NotificationDeliveryIntent.raw_payload_id == raw_payload_id,
                    NotificationDeliveryIntent.category == category,
                )
                .order_by(NotificationDeliveryIntent.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(raw_claims) > 1:
            raise DeliveryStateError("raw payload already has multiple delivery claims")
        if raw_claims:
            raw_claim = raw_claims[0]
            if (
                raw_claim.subject_id != ownership.subject_id
                or raw_claim.recipient_user_id != ownership.recipient_user_id
                or raw_claim.actor_user_id != actor_user_id
                or raw_claim.ai_invocation_id != ai_invocation_id
                or raw_claim.channel != authority.binding.channel
                or raw_claim.integration_connection_id not in authority.telegram_connection_ids
            ):
                raise DeliveryIdempotencyConflictError("raw delivery occurrence metadata conflicts")
            await _validate_linked_journal_for_intent(
                session,
                intent=raw_claim,
            )
            return None
    if ai_invocation_id is not None:
        existing_ai = await session.scalar(
            select(NotificationDeliveryIntent)
            .where(NotificationDeliveryIntent.ai_invocation_id == ai_invocation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing_ai is not None:
            if _existing_intent_metadata(existing_ai) != expected_metadata:
                raise DeliveryIdempotencyConflictError(
                    "AI invocation is bound to a different delivery intent"
                )
            if existing_ai.integration_connection_id not in authority.telegram_connection_ids:
                raise DeliveryStateError(
                    "existing AI delivery intent has invalid historical connection"
                )
            await _validate_linked_journal_for_intent(
                session,
                intent=existing_ai,
            )
            return None
    if (
        await _matching_journal_claim(
            session,
            ownership=ownership,
            actor_user_id=actor_user_id,
            category=category,
            channel=authority.binding.channel,
            idempotency_key=idempotency_key,
            legacy_dedupe_key=legacy_dedupe_key,
            raw_payload_id=raw_payload_id,
            ai_invocation_id=ai_invocation_id,
        )
        is not None
    ):
        return None

    if not module_enabled:
        if raw_payload_id is not None and category in {CATEGORY_REPLY, CATEGORY_ECHO}:
            _cancel_pending_intent(
                provisional,
                completed_at=policy_at,
                error_code=NotificationDeliveryErrorCode.CANCELLED_BY_POLICY,
            )
            session.add(provisional)
            await session.flush()
        return None
    if category == CATEGORY_NUDGE and in_quiet_hours(
        local_at.time().replace(tzinfo=None),
        start=policy.quiet_start,
        end=policy.quiet_end,
    ):
        return None
    if (
        category in INITIATIVE_CATEGORIES
        and await _initiative_claim_count(
            session,
            ownership=ownership,
            policy_date=local_at.date(),
        )
        >= policy.daily_budget
    ):
        return None

    session.add(provisional)
    await session.flush()
    return PreparedDeliveryIntent._issue(
        session=session,
        intent=provisional,
        binding=authority.binding,
        text=text,
        buttons=frozen_buttons,
        reply_to=reply_to,
        sent_at=local_at.replace(tzinfo=None),
        redact_journal_content=redact_journal_content,
        journal_raw_payload_id=(raw_payload_id if redact_journal_content else None),
    )


async def rearm_stale_raw_delivery_intent(
    session: AsyncSession,
    notifier: BoundNotifier | None,
    *,
    text: str,
    category: str,
    idempotency_key: str,
    stale_before: datetime,
    legacy_dedupe_key: str | None = None,
    buttons: Optional[Buttons] = None,
    reply_to: str | None = None,
    now: datetime | None = None,
    ownership: ProactiveOwnershipContext,
    actor_user_id: uuid.UUID | None = None,
    raw_payload_id: int,
    ai_invocation_id: uuid.UUID | None = None,
    redact_journal_content: bool = False,
) -> PreparedDeliveryIntent | None:
    """Reissue T1 memory state for one abandoned raw-backed PENDING claim.

    The intent deliberately contains no rendered payload, so ordinary duplicate
    preparation can never resume it. This narrower recovery seam is available
    only to deterministic inbound reply/echo handlers after the row has become
    stale. It revalidates the complete live and historical graph under the
    canonical locks, and the returned capability is armed only by this root
    transaction's commit just like a freshly inserted T1 capability.
    """

    if session.in_nested_transaction():
        raise DeliveryCapabilityError("delivery re-arm requires an outer transaction")
    if notifier is not None and not isinstance(notifier, BoundNotifier):
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    text = canonicalize_text(text)
    frozen_buttons = canonicalize_buttons(buttons)
    if category not in {CATEGORY_REPLY, CATEGORY_ECHO}:
        raise DeliveryScopeError("only raw-backed reply or echo may be re-armed")
    idempotency_key = _opaque_key(
        idempotency_key,
        field_name="idempotency_key",
    )
    legacy_dedupe_key = _validate_legacy_dedupe_key(legacy_dedupe_key)
    reply_to = _validate_reply_target(reply_to)
    if (
        isinstance(raw_payload_id, bool)
        or not isinstance(raw_payload_id, int)
        or raw_payload_id < 1
    ):
        raise ValueError("raw_payload_id must be a positive integer")
    if ai_invocation_id is not None and not isinstance(ai_invocation_id, uuid.UUID):
        raise TypeError("ai_invocation_id must be a UUID or None")
    if not isinstance(redact_journal_content, bool):
        raise TypeError("redact_journal_content must be a bool")
    if redact_journal_content and category != CATEGORY_REPLY:
        raise DeliveryScopeError("redacted delivery journals require a raw-backed reply")
    if ai_invocation_id is not None and category == CATEGORY_REPLY and not redact_journal_content:
        raise DeliveryScopeError("AI question replies require a redacted journal")
    _validate_ownership(ownership, actor_user_id=actor_user_id)
    expected_binding = _binding_for(ownership)
    if notifier is not None and (
        notifier.channel != expected_binding.channel
        or not _same_binding(notifier.binding, expected_binding)
    ):
        raise DeliveryScopeError("notifier binding does not match delivery ownership")

    cutoff = _reconciliation_now(stale_before)
    authority = await _lock_live_delivery_authority(
        session,
        ownership=ownership,
        actor_user_id=actor_user_id,
    )
    if notifier is not None and not _same_binding(authority.binding, notifier.binding):
        raise DeliveryScopeError("notifier binding is not the current endpoint")
    module_enabled = await _locked_signals_module_enabled(
        session,
        subject_id=ownership.subject_id,
    )
    rearmed_at, local_at = _policy_clock(
        timezone_value=authority.timezone,
        now=now,
    )
    if cutoff > rearmed_at:
        raise ValueError("stale_before cannot be later than the re-arm instant")
    if module_enabled:
        if notifier is None:
            # Missing transport is recoverable. It cannot authorize a payload
            # capability, but it must not erase the stale claim either.
            return None
        # The strict scoped policy is part of live delivery authority even
        # though reply/echo are exempt from quiet hours and initiative budget.
        await _load_locked_delivery_policy(session, authority=authority)

    provisional = NotificationDeliveryIntent(
        subject_id=ownership.subject_id,
        recipient_user_id=ownership.recipient_user_id,
        actor_user_id=actor_user_id,
        integration_connection_id=ownership.connection_id,
        raw_payload_id=raw_payload_id,
        ai_invocation_id=ai_invocation_id,
        category=category,
        channel=authority.binding.channel,
        idempotency_key=idempotency_key,
        policy_key=None,
        policy_at=rearmed_at,
        policy_date=local_at.date(),
        status=NotificationDeliveryStatus.PENDING.value,
    )
    # Raw/AI rows are locked before the intent row. The raw may legitimately
    # point at a retired Telegram recipient connection after C rotation.
    await _lock_raw_and_ai_provenance(
        session,
        intent=provisional,
        telegram_connection_ids=authority.telegram_connection_ids,
    )

    candidate_predicates = [
        and_(
            NotificationDeliveryIntent.subject_id == ownership.subject_id,
            NotificationDeliveryIntent.recipient_user_id == ownership.recipient_user_id,
            NotificationDeliveryIntent.idempotency_key == idempotency_key,
        ),
        and_(
            NotificationDeliveryIntent.raw_payload_id == raw_payload_id,
            NotificationDeliveryIntent.category == category,
        ),
    ]
    if ai_invocation_id is not None:
        candidate_predicates.append(NotificationDeliveryIntent.ai_invocation_id == ai_invocation_id)
    candidates = list(
        await session.scalars(
            select(NotificationDeliveryIntent)
            .where(or_(*candidate_predicates))
            .order_by(NotificationDeliveryIntent.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if not candidates:
        return None
    if len(candidates) != 1:
        raise DeliveryIdempotencyConflictError("raw delivery recovery resolves to multiple claims")
    intent = candidates[0]
    if (
        intent.subject_id != ownership.subject_id
        or intent.recipient_user_id != ownership.recipient_user_id
        or intent.actor_user_id != actor_user_id
        or intent.raw_payload_id != raw_payload_id
        or intent.ai_invocation_id != ai_invocation_id
        or intent.category != category
        or intent.channel != authority.binding.channel
        or intent.idempotency_key != idempotency_key
        or intent.policy_key is not None
    ):
        raise DeliveryIdempotencyConflictError("raw delivery recovery metadata conflicts")
    if intent.integration_connection_id not in authority.telegram_connection_ids:
        raise DeliveryScopeError("raw delivery recovery has an invalid historical connection")
    await _validate_linked_journal_for_intent(session, intent=intent)
    if intent.status == NotificationDeliveryStatus.PENDING.value:
        if any(
            value is not None
            for value in (
                intent.lease_token,
                intent.dispatch_started_at,
                intent.completed_at,
                intent.error_code,
            )
        ):
            raise DeliveryStateError("pending delivery intent lifecycle is malformed")
        if _aware_utc(intent.updated_at) >= cutoff:
            return None
    elif intent.status == NotificationDeliveryStatus.CANCELLED.value:
        if (
            intent.error_code
            not in {
                NotificationDeliveryErrorCode.STALE_PENDING.value,
                NotificationDeliveryErrorCode.SCOPE_INVALID.value,
            }
            or intent.lease_token is not None
            or intent.dispatch_started_at is not None
            or intent.completed_at is None
        ):
            raise DeliveryStateError("cancelled delivery intent is not safe to re-arm")
        if (
            max(
                _aware_utc(intent.updated_at),
                _aware_utc(intent.completed_at),
            )
            >= cutoff
        ):
            return None
    else:
        raise DeliveryStateError("dispatching or terminal delivery intent cannot be re-armed")
    if (
        await _matching_journal_claim(
            session,
            ownership=ownership,
            actor_user_id=actor_user_id,
            category=category,
            channel=authority.binding.channel,
            idempotency_key=idempotency_key,
            legacy_dedupe_key=legacy_dedupe_key,
            raw_payload_id=raw_payload_id,
            ai_invocation_id=ai_invocation_id,
        )
        is not None
    ):
        return None
    if not module_enabled:
        _cancel_pending_intent(
            intent,
            completed_at=rearmed_at,
            error_code=NotificationDeliveryErrorCode.CANCELLED_BY_POLICY,
        )
        await session.flush()
        return None

    # No provider attempt has started, so a historical C may be rebound to the
    # exact current recipient. Refreshing updated_at also gives a committed
    # recovery capability its own abandonment window if the process crashes
    # again before T2.
    intent.integration_connection_id = authority.binding.integration_connection_id
    intent.policy_at = rearmed_at
    intent.policy_date = local_at.date()
    intent.status = NotificationDeliveryStatus.PENDING.value
    intent.lease_token = None
    intent.dispatch_started_at = None
    intent.completed_at = None
    intent.error_code = None
    intent.updated_at = rearmed_at
    await session.flush()
    return PreparedDeliveryIntent._issue(
        session=session,
        intent=intent,
        binding=authority.binding,
        text=text,
        buttons=frozen_buttons,
        reply_to=reply_to,
        sent_at=local_at.replace(tzinfo=None),
        redact_journal_content=redact_journal_content,
        journal_raw_payload_id=(raw_payload_id if redact_journal_content else None),
    )


def _cancel_pending_intent(
    intent: NotificationDeliveryIntent,
    *,
    completed_at: datetime,
    error_code: NotificationDeliveryErrorCode,
) -> None:
    intent.status = NotificationDeliveryStatus.CANCELLED.value
    intent.lease_token = None
    intent.dispatch_started_at = None
    intent.completed_at = completed_at
    intent.error_code = error_code.value
