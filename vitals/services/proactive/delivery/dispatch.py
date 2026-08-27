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

import asyncio
import logging
import uuid
import weakref
from datetime import (
    datetime,
    timezone,
)
from time import monotonic

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    NotificationDeliveryErrorCode,
    NotificationDeliveryStatus,
)
from vitals.models.proactive import Notification, NotificationDeliveryIntent
from vitals.services.proactive.channels import (
    BoundNotifier,
    BoundNotifierResolver,
    DeliveryEndpointBinding,
    resolve_legacy_bound_notifier,
)
from vitals.services.proactive.ownership import ProactiveOwnershipContext
from vitals.utils.timeutils import now_utc

logger = logging.getLogger(__name__)

from vitals.services.proactive.delivery.contracts import (
    CATEGORY_NUDGE,
    DISPATCHING_STALE_AFTER,
    INITIATIVE_CATEGORIES,
    DeliveryCapabilityError,
    DeliveryCompletion,
    DeliveryDispatchLease,
    DeliveryScopeError,
    DeliveryStateError,
    PreparedDeliveryIntent,
    _COMPLETION_SEAL,
    _LEASE_SEAL,
    _aware_utc,
    _intent_fingerprint,
    _policy_clock,
    _prepared_fingerprint_matches,
    _prepared_is_valid,
    _register_transaction_outcome,
    _same_binding,
    _snapshot_from_fingerprint,
    _valid_external_id,
)

from vitals.services.proactive.delivery.policy import (
    _load_locked_delivery_policy,
    _lock_historical_delivery_roots,
    _lock_live_delivery_authority,
    _lock_raw_and_ai_provenance,
    _locked_signals_module_enabled,
)

from vitals.services.proactive.delivery.queries import (
    _initiative_claim_is_within_budget,
    in_quiet_hours,
)

from vitals.services.proactive.delivery.preparation import (
    _cancel_pending_intent,
)


def _bind_prepared_start(
    prepared: PreparedDeliveryIntent,
    *,
    session: AsyncSession,
    lease: DeliveryDispatchLease | None,
) -> None:
    prepared_ref = weakref.ref(prepared)
    lease_ref = weakref.ref(lease) if lease is not None else None
    object.__setattr__(prepared, "_finalizing", True)

    def after_commit() -> None:
        target = prepared_ref()
        if target is not None:
            object.__setattr__(target, "_consumed", True)
            object.__setattr__(target, "_finalizing", False)
            target._clear_payload()
            object.__setattr__(target, "_session", None)
        lease_target = lease_ref() if lease_ref is not None else None
        if lease_target is not None:
            object.__setattr__(lease_target, "_armed", True)

    def after_rollback() -> None:
        target = prepared_ref()
        if target is not None:
            object.__setattr__(target, "_finalizing", False)
        lease_target = lease_ref() if lease_ref is not None else None
        if lease_target is not None:
            lease_target._invalidate()

    _register_transaction_outcome(
        session,
        on_commit=after_commit,
        on_rollback=after_rollback,
    )


async def start_delivery_dispatch(
    session: AsyncSession,
    prepared: PreparedDeliveryIntent,
    *,
    now: datetime | None = None,
    notifier_resolver: BoundNotifierResolver = resolve_legacy_bound_notifier,
) -> DeliveryDispatchLease | None:
    """T2: freshly reauthorize policy/C and commit-arm one dispatch lease."""

    if session.in_nested_transaction():
        raise DeliveryCapabilityError("start dispatch requires an outer transaction")
    if not _prepared_is_valid(prepared) or not _prepared_fingerprint_matches(prepared):
        raise DeliveryCapabilityError(
            "prepared delivery is stale, uncommitted, forged, or consumed"
        )
    if not callable(notifier_resolver):
        raise TypeError("notifier_resolver must be synchronous and callable")
    snapshot = _snapshot_from_fingerprint(prepared._fingerprint)
    ownership = ProactiveOwnershipContext(
        subject_id=snapshot.subject_id,
        recipient_user_id=snapshot.recipient_user_id,
        connection_id=snapshot.integration_connection_id,
        include_legacy_unowned=True,
    )
    authority = await _lock_live_delivery_authority(
        session,
        ownership=ownership,
        actor_user_id=snapshot.actor_user_id,
    )
    module_enabled = await _locked_signals_module_enabled(
        session,
        subject_id=snapshot.subject_id,
    )
    policy = await _load_locked_delivery_policy(session, authority=authority)
    current_at, current_local = _policy_clock(
        timezone_value=authority.timezone,
        now=now,
    )
    await _lock_raw_and_ai_provenance(
        session,
        intent=snapshot,
        telegram_connection_ids=authority.telegram_connection_ids,
    )
    intent = await session.scalar(
        select(NotificationDeliveryIntent)
        .where(NotificationDeliveryIntent.id == snapshot.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if intent is None or _intent_fingerprint(intent) != prepared._fingerprint:
        raise DeliveryCapabilityError("prepared delivery provenance no longer matches")
    if intent.status != NotificationDeliveryStatus.PENDING.value:
        raise DeliveryStateError("delivery intent cannot obtain another lease")

    cancelled = (
        not module_enabled
        or (intent.category in INITIATIVE_CATEGORIES and current_local.date() != intent.policy_date)
        or (
            intent.category == CATEGORY_NUDGE
            and in_quiet_hours(
                current_local.time().replace(tzinfo=None),
                start=policy.quiet_start,
                end=policy.quiet_end,
            )
        )
    )
    if not cancelled and intent.category in INITIATIVE_CATEGORIES:
        cancelled = not await _initiative_claim_is_within_budget(
            session,
            intent=intent,
            ownership=ownership,
            daily_budget=policy.daily_budget,
        )
    if cancelled:
        _cancel_pending_intent(
            intent,
            completed_at=current_at,
            error_code=NotificationDeliveryErrorCode.CANCELLED_BY_POLICY,
        )
        await session.flush()
        _bind_prepared_start(prepared, session=session, lease=None)
        return None

    notifier = notifier_resolver(authority.binding, authority.credential_ref)
    if notifier is None:
        _cancel_pending_intent(
            intent,
            completed_at=current_at,
            error_code=NotificationDeliveryErrorCode.SCOPE_INVALID,
        )
        await session.flush()
        _bind_prepared_start(prepared, session=session, lease=None)
        return None
    if (
        not isinstance(notifier, BoundNotifier)
        or notifier.channel != authority.binding.channel
        or not _same_binding(notifier.binding, authority.binding)
    ):
        raise DeliveryScopeError("resolved notifier is not bound to exact S/Q/C")

    lease_token = uuid.uuid4()
    intent.status = NotificationDeliveryStatus.DISPATCHING.value
    intent.lease_token = lease_token
    intent.dispatch_started_at = current_at
    intent.completed_at = None
    intent.error_code = None
    await session.flush()
    lease = DeliveryDispatchLease._issue(
        session=session,
        prepared=prepared,
        lease_token=lease_token,
        notifier=notifier,
        sent_at=current_local.replace(tzinfo=None),
    )
    _bind_prepared_start(prepared, session=session, lease=lease)
    return lease


async def dispatch_delivery(
    lease: DeliveryDispatchLease,
) -> DeliveryCompletion:
    """Consume one committed lease and perform exactly one Telegram call."""

    if (
        not isinstance(lease, DeliveryDispatchLease)
        or lease._seal is not _LEASE_SEAL
        or not lease._armed
        or lease._consumed
        or lease._session is None
        or lease._notifier is None
        or lease._text is None
    ):
        raise DeliveryCapabilityError("delivery lease is stale, uncommitted, forged, or consumed")
    snapshot = _snapshot_from_fingerprint(lease._fingerprint)
    if not _same_binding(
        lease._binding,
        DeliveryEndpointBinding(
            subject_id=snapshot.subject_id,
            recipient_user_id=snapshot.recipient_user_id,
            integration_connection_id=snapshot.integration_connection_id,
            channel=snapshot.channel,
        ),
    ):
        raise DeliveryCapabilityError("delivery lease fingerprint is invalid")
    issuing_session = lease._session
    if issuing_session.in_transaction():
        raise DeliveryCapabilityError("Telegram call cannot span a DB transaction")

    notifier = lease._notifier
    if monotonic() - lease._issued_monotonic >= DISPATCHING_STALE_AFTER.total_seconds():
        lease._invalidate()
        logger.warning("delivery ended ambiguously (code=stale_dispatch)")
        return DeliveryCompletion._issue(
            lease=lease,
            status=NotificationDeliveryStatus.AMBIGUOUS,
            error_code=NotificationDeliveryErrorCode.STALE_DISPATCH,
            external_id=None,
            text=None,
            buttons=None,
        )
    if (
        not isinstance(notifier, BoundNotifier)
        or notifier.channel != lease._binding.channel
        or not _same_binding(notifier.binding, lease._binding)
    ):
        lease._invalidate()
        logger.warning("delivery ended ambiguously (code=internal_error)")
        return DeliveryCompletion._issue(
            lease=lease,
            status=NotificationDeliveryStatus.AMBIGUOUS,
            error_code=NotificationDeliveryErrorCode.INTERNAL_ERROR,
            external_id=None,
            text=None,
            buttons=None,
        )
    text = lease._text
    buttons = lease._buttons
    reply_to = lease._reply_to
    object.__setattr__(lease, "_consumed", True)
    object.__setattr__(lease, "_notifier", None)
    object.__setattr__(lease, "_text", None)
    object.__setattr__(lease, "_buttons", None)
    object.__setattr__(lease, "_reply_to", None)
    object.__setattr__(lease, "_session", None)
    try:
        external_id = await notifier.send(
            text,
            buttons=buttons,
            reply_to=reply_to,
        )
    except asyncio.CancelledError:
        logger.warning("delivery ended ambiguously (code=transport_error)")
        return DeliveryCompletion._issue(
            lease=lease,
            status=NotificationDeliveryStatus.AMBIGUOUS,
            error_code=NotificationDeliveryErrorCode.TRANSPORT_ERROR,
            external_id=None,
            text=None,
            buttons=None,
        )
    except Exception:
        logger.warning("delivery ended ambiguously (code=transport_error)")
        return DeliveryCompletion._issue(
            lease=lease,
            status=NotificationDeliveryStatus.AMBIGUOUS,
            error_code=NotificationDeliveryErrorCode.TRANSPORT_ERROR,
            external_id=None,
            text=None,
            buttons=None,
        )
    valid_external_id = _valid_external_id(external_id)
    if valid_external_id is None:
        logger.warning("delivery ended ambiguously (code=invalid_response)")
        return DeliveryCompletion._issue(
            lease=lease,
            status=NotificationDeliveryStatus.AMBIGUOUS,
            error_code=NotificationDeliveryErrorCode.INVALID_RESPONSE,
            external_id=None,
            text=None,
            buttons=None,
        )
    return DeliveryCompletion._issue(
        lease=lease,
        status=NotificationDeliveryStatus.SENT,
        error_code=None,
        external_id=valid_external_id,
        text=text,
        buttons=buttons,
    )


def _completion_is_valid(completion: object) -> bool:
    if (
        not isinstance(completion, DeliveryCompletion)
        or completion._seal is not _COMPLETION_SEAL
        or completion._consumed
        or completion._finalizing
        or not isinstance(completion._lease_token, uuid.UUID)
    ):
        return False
    if completion._status is NotificationDeliveryStatus.SENT:
        return bool(
            completion._error_code is None
            and _valid_external_id(completion._external_id) is not None
            and completion._text is not None
        )
    return bool(
        completion._status is NotificationDeliveryStatus.AMBIGUOUS
        and completion._error_code
        in {
            NotificationDeliveryErrorCode.TRANSPORT_ERROR,
            NotificationDeliveryErrorCode.INVALID_RESPONSE,
            NotificationDeliveryErrorCode.STALE_DISPATCH,
            NotificationDeliveryErrorCode.INTERNAL_ERROR,
        }
        and completion._external_id is None
        and completion._text is None
        and completion._buttons is None
    )


def _journal_payload(completion: DeliveryCompletion) -> dict:
    if completion._redact_journal_content:
        return {
            "content_redacted": True,
            "raw_payload_id": completion._journal_raw_payload_id,
        }
    return {
        "text": completion._text,
        "buttons": (
            [list(button) for button in completion._buttons] if completion._buttons else None
        ),
    }


def _journal_matches_completion(
    row: Notification,
    *,
    intent: NotificationDeliveryIntent,
    completion: DeliveryCompletion,
) -> bool:
    return (
        row.delivery_intent_id == intent.id
        and row.subject_id == intent.subject_id
        and row.recipient_user_id == intent.recipient_user_id
        and row.actor_user_id == intent.actor_user_id
        and row.integration_connection_id == intent.integration_connection_id
        and row.ai_invocation_id == intent.ai_invocation_id
        and row.category == intent.category
        and row.channel == intent.channel
        and row.dedupe_key == intent.idempotency_key
        and row.external_id == completion._external_id
        and row.sent_at == completion._sent_at
        and row.payload == _journal_payload(completion)
    )


async def finalize_delivery(
    session: AsyncSession,
    completion: DeliveryCompletion,
) -> Notification | None:
    """T3: atomically close the frozen intent and journal a confirmed send."""

    if session.in_nested_transaction():
        raise DeliveryCapabilityError("delivery finalization requires an outer transaction")
    if not _completion_is_valid(completion):
        raise DeliveryCapabilityError("delivery completion is forged or consumed")
    snapshot = _snapshot_from_fingerprint(completion._fingerprint)
    await _lock_historical_delivery_roots(session, intent=snapshot)
    intent = await session.scalar(
        select(NotificationDeliveryIntent)
        .where(NotificationDeliveryIntent.id == snapshot.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if intent is None or _intent_fingerprint(intent) != completion._fingerprint:
        raise DeliveryCapabilityError("delivery completion provenance does not match")
    if intent.lease_token != completion._lease_token:
        raise DeliveryCapabilityError("delivery completion lease token does not match")

    if intent.status != NotificationDeliveryStatus.DISPATCHING.value:
        if intent.status == completion._status.value:
            existing = await session.scalar(
                select(Notification)
                .where(Notification.delivery_intent_id == intent.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if completion._status is NotificationDeliveryStatus.SENT:
                if (
                    intent.error_code is not None
                    or existing is None
                    or not _journal_matches_completion(
                        existing,
                        intent=intent,
                        completion=completion,
                    )
                ):
                    raise DeliveryStateError("sent delivery has no exact linked journal")
            elif (
                existing is not None
                or completion._error_code is None
                or intent.error_code != completion._error_code.value
            ):
                object.__setattr__(completion, "_consumed", True)
                object.__setattr__(completion, "_text", None)
                object.__setattr__(completion, "_buttons", None)
                raise DeliveryStateError("ambiguous delivery terminal state does not match")
            completion._bind_finalization(session)
            return existing
        # A reconciler may have conservatively closed DISPATCHING while this
        # provider result was in flight.  Never turn that into a second send or
        # retain its plaintext completion indefinitely.
        object.__setattr__(completion, "_consumed", True)
        object.__setattr__(completion, "_text", None)
        object.__setattr__(completion, "_buttons", None)
        raise DeliveryStateError("delivery intent is already terminal")

    if intent.dispatch_started_at is None:
        raise DeliveryStateError("dispatching delivery has no start timestamp")
    completed_at = max(
        now_utc().astimezone(timezone.utc),
        _aware_utc(intent.dispatch_started_at),
    )
    intent.status = completion._status.value
    intent.completed_at = completed_at
    intent.error_code = completion._error_code.value if completion._error_code is not None else None
    journal: Notification | None = None
    if completion._status is NotificationDeliveryStatus.SENT:
        external_id = _valid_external_id(completion._external_id)
        if external_id is None:
            raise DeliveryCapabilityError("successful delivery id is invalid")
        journal = Notification(
            delivery_intent_id=intent.id,
            subject_id=intent.subject_id,
            actor_user_id=intent.actor_user_id,
            recipient_user_id=intent.recipient_user_id,
            integration_connection_id=intent.integration_connection_id,
            sent_at=completion._sent_at,
            category=intent.category,
            dedupe_key=intent.idempotency_key,
            channel=intent.channel,
            external_id=external_id,
            ai_invocation_id=intent.ai_invocation_id,
            payload=_journal_payload(completion),
        )
        session.add(journal)
    await session.flush()
    completion._bind_finalization(session)
    return journal
