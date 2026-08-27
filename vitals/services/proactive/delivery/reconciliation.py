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
from datetime import (
    datetime,
    timezone,
)

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    NotificationDeliveryErrorCode,
    NotificationDeliveryStatus,
)
from vitals.models.proactive import NotificationDeliveryIntent
from vitals.persistence.rls import enter_platform_scope
from vitals.utils.timeutils import now_utc

logger = logging.getLogger(__name__)

from vitals.services.proactive.delivery.contracts import (
    CATEGORY_ECHO,
    CATEGORY_REPLY,
    DISPATCHING_STALE_AFTER,
    PENDING_STALE_AFTER,
    RECONCILIATION_BATCH_SIZE,
    _aware_utc,
    _intent_fingerprint,
    _snapshot_fingerprint,
)

from vitals.services.proactive.delivery.policy import (
    _lock_historical_delivery_roots,
)

from vitals.services.proactive.delivery.queries import (
    _intent_snapshot_select,
    _reconciliation_limit,
    _reconciliation_now,
    _snapshot_from_row,
)
from vitals.services.proactive.delivery.preparation import _cancel_pending_intent


async def reconcile_stale_pending_deliveries(
    session: AsyncSession,
    *,
    stale_before: datetime,
    limit: int = RECONCILIATION_BATCH_SIZE,
) -> int:
    """Cancel a bounded page of PENDING rows; never reconstruct payload/send."""

    cutoff = _reconciliation_now(stale_before)
    batch_limit = _reconciliation_limit(limit)
    candidates = list(
        await session.execute(
            _intent_snapshot_select()
            .where(
                NotificationDeliveryIntent.status == NotificationDeliveryStatus.PENDING.value,
                NotificationDeliveryIntent.updated_at < cutoff,
                or_(
                    NotificationDeliveryIntent.raw_payload_id.is_(None),
                    NotificationDeliveryIntent.category.notin_({CATEGORY_REPLY, CATEGORY_ECHO}),
                ),
            )
            .order_by(
                NotificationDeliveryIntent.subject_id,
                NotificationDeliveryIntent.id,
            )
            .limit(batch_limit)
        )
    )
    changed = 0
    completed_at = now_utc().astimezone(timezone.utc)
    for candidate in candidates:
        snapshot = _snapshot_from_row(candidate)
        await _lock_historical_delivery_roots(session, intent=snapshot)
        intent = await session.scalar(
            select(NotificationDeliveryIntent)
            .where(NotificationDeliveryIntent.id == snapshot.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            intent is None
            or _intent_fingerprint(intent) != _snapshot_fingerprint(snapshot)
            or intent.status != NotificationDeliveryStatus.PENDING.value
            or _aware_utc(intent.updated_at) >= cutoff
            or (
                intent.raw_payload_id is not None
                and intent.category in {CATEGORY_REPLY, CATEGORY_ECHO}
            )
        ):
            continue
        _cancel_pending_intent(
            intent,
            completed_at=completed_at,
            error_code=NotificationDeliveryErrorCode.STALE_PENDING,
        )
        changed += 1
    await session.flush()
    return changed


async def reconcile_stale_delivery_dispatches(
    session: AsyncSession,
    *,
    stale_before: datetime,
    limit: int = RECONCILIATION_BATCH_SIZE,
) -> int:
    """Close a bounded page of uncertain provider attempts as AMBIGUOUS."""

    cutoff = _reconciliation_now(stale_before)
    batch_limit = _reconciliation_limit(limit)
    candidates = list(
        await session.execute(
            _intent_snapshot_select()
            .where(
                NotificationDeliveryIntent.status == NotificationDeliveryStatus.DISPATCHING.value,
                NotificationDeliveryIntent.dispatch_started_at < cutoff,
            )
            .order_by(
                NotificationDeliveryIntent.subject_id,
                NotificationDeliveryIntent.id,
            )
            .limit(batch_limit)
        )
    )
    changed = 0
    completed_at = now_utc().astimezone(timezone.utc)
    for candidate in candidates:
        snapshot = _snapshot_from_row(candidate)
        await _lock_historical_delivery_roots(session, intent=snapshot)
        intent = await session.scalar(
            select(NotificationDeliveryIntent)
            .where(NotificationDeliveryIntent.id == snapshot.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            intent is None
            or _intent_fingerprint(intent) != _snapshot_fingerprint(snapshot)
            or intent.status != NotificationDeliveryStatus.DISPATCHING.value
            or intent.dispatch_started_at is None
            or _aware_utc(intent.dispatch_started_at) >= cutoff
        ):
            continue
        intent.status = NotificationDeliveryStatus.AMBIGUOUS.value
        intent.completed_at = max(
            completed_at,
            _aware_utc(intent.dispatch_started_at),
        )
        intent.error_code = NotificationDeliveryErrorCode.STALE_DISPATCH.value
        changed += 1
    await session.flush()
    return changed


async def delivery_reconciliation_job(session_factory, redis=None) -> None:
    """Shared-scheduler entry point; bounded and deliberately provider-free."""

    del redis
    current = now_utc().astimezone(timezone.utc)
    async with session_factory() as session:
        # Sweeps every subject's stalled deliveries, so it belongs to nobody in
        # particular and row security would otherwise match no row at all.
        await enter_platform_scope(session)
        await reconcile_stale_pending_deliveries(
            session,
            stale_before=current - PENDING_STALE_AFTER,
        )
        await reconcile_stale_delivery_dispatches(
            session,
            stale_before=current - DISPATCHING_STALE_AFTER,
        )
        await session.commit()
