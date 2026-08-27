"""Platform-funded AI control plane, hard quotas, and at-most-once dispatch.

Database rows contain only authorization/provenance/accounting metadata. Prompt,
response, provider secrets, and exception text exist only in short-lived in-memory
objects. Every mutating database function flushes; its caller owns commit.

Lock order is identity governance -> S -> A -> raw provenance -> platform root ->
platform quota -> subject quota -> invocation. No provider await is permitted
until the issuing start-dispatch transaction has committed.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationStatus,
)
from vitals.models.ai import (
    AIInvocation,
)
from vitals.models.identity import HealthSubject
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.utils.timeutils import now_utc

from vitals.services.ai_gateway.contracts import (
    AIInvocationStateError,
    _validate_aware_utc,
)

from vitals.services.ai_gateway.config import (
    _lock_exact_root,
    _lock_quota_rows,
    _lock_raw_payload_scope,
)

from vitals.services.ai_gateway.invocations import (
    _invocation_key,
)


async def reconcile_stale_dispatches(
    session: AsyncSession,
    *,
    stale_before: datetime,
    limit: int = 100,
) -> int:
    """Mark stale paid dispatches ambiguous without any provider activity."""

    _validate_aware_utc(stale_before, "stale_before")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    await acquire_identity_governance_lock(session)
    candidate_ids = list(
        await session.scalars(
            select(AIInvocation.id)
            .where(
                AIInvocation.status == AIInvocationStatus.DISPATCHING.value,
                AIInvocation.started_at < stale_before,
            )
            .order_by(
                AIInvocation.quota_period_start,
                AIInvocation.quota_period_end,
                AIInvocation.subject_id,
                AIInvocation.id,
            )
            .limit(limit)
        )
    )
    changed = 0
    for invocation_id in candidate_ids:
        snapshot = await _invocation_key(session, invocation_id)
        await session.scalar(
            select(HealthSubject).where(HealthSubject.id == snapshot.subject_id).with_for_update()
        )
        await _lock_raw_payload_scope(
            session,
            raw_payload_id=snapshot.raw_payload_id,
            subject_id=snapshot.subject_id,
        )
        await _lock_exact_root(session, snapshot, require_active=False)
        await _lock_quota_rows(
            session,
            subject_id=snapshot.subject_id,
            period_start=snapshot.quota_period_start,
            period_end=snapshot.quota_period_end,
        )
        invocation = await session.scalar(
            select(AIInvocation)
            .where(AIInvocation.id == invocation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            invocation is None
            or invocation.status != AIInvocationStatus.DISPATCHING.value
            or invocation.started_at is None
        ):
            continue
        invocation.status = AIInvocationStatus.AMBIGUOUS.value
        invocation.error_code = AIInvocationErrorCode.TIMEOUT.value
        invocation.finished_at = now_utc()
        changed += 1
    if changed:
        await session.flush()
    return changed


async def reconcile_stale_reservations(
    session: AsyncSession,
    *,
    stale_before: datetime,
    limit: int = 100,
    error_code: AIInvocationErrorCode = AIInvocationErrorCode.CANCELLED_BY_POLICY,
) -> int:
    """Release abandoned prepared reservations without provider activity."""

    _validate_aware_utc(stale_before, "stale_before")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if not isinstance(error_code, AIInvocationErrorCode):
        raise TypeError("error_code must be an AIInvocationErrorCode")
    await acquire_identity_governance_lock(session)
    candidate_ids = list(
        await session.scalars(
            select(AIInvocation.id)
            .where(
                AIInvocation.status == AIInvocationStatus.PREPARED.value,
                AIInvocation.created_at < stale_before,
            )
            .order_by(
                AIInvocation.quota_period_start,
                AIInvocation.quota_period_end,
                AIInvocation.subject_id,
                AIInvocation.id,
            )
            .limit(limit)
        )
    )
    changed = 0
    for invocation_id in candidate_ids:
        key = await _invocation_key(session, invocation_id)
        await session.scalar(
            select(HealthSubject).where(HealthSubject.id == key.subject_id).with_for_update()
        )
        await _lock_raw_payload_scope(
            session,
            raw_payload_id=key.raw_payload_id,
            subject_id=key.subject_id,
        )
        await _lock_exact_root(session, key, require_active=False)
        platform_quota, subject_quota = await _lock_quota_rows(
            session,
            subject_id=key.subject_id,
            period_start=key.quota_period_start,
            period_end=key.quota_period_end,
        )
        invocation = await session.scalar(
            select(AIInvocation)
            .where(AIInvocation.id == invocation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if invocation is None or invocation.status != AIInvocationStatus.PREPARED.value:
            continue
        for quota in (platform_quota, subject_quota):
            if (
                quota.reserved_cost_microunits < invocation.reserved_cost_microunits
                or quota.reserved_units < invocation.reserved_units
            ):
                raise AIInvocationStateError("AI reservation accounting is inconsistent")
            quota.reserved_cost_microunits -= invocation.reserved_cost_microunits
            quota.reserved_units -= invocation.reserved_units
        invocation.status = AIInvocationStatus.CANCELLED.value
        invocation.error_code = error_code.value
        invocation.finished_at = now_utc()
        changed += 1
    if changed:
        await session.flush()
    return changed
