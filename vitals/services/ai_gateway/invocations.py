"""Platform-funded AI control plane, hard quotas, and at-most-once dispatch.

Database rows contain only authorization/provenance/accounting metadata. Prompt,
response, provider secrets, and exception text exist only in short-lived in-memory
objects. Every mutating database function flushes; its caller owns commit.

Lock order is identity governance -> S -> A -> raw provenance -> platform root ->
platform quota -> subject quota -> invocation. No provider await is permitted
until the issuing start-dispatch transaction has committed.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
)
from vitals.models.ai import (
    AIInvocation,
)
from vitals.ownership import WriteIdentity
from vitals.utils.timeutils import now_utc

from vitals.services.ai_gateway.contracts import (
    AIGatewayAuthorizationError,
    AIIdempotencyConflictError,
    AIInvocationStateError,
    AIQuotaExceededError,
    AIReservationResult,
    _InvocationKey,
    _as_purpose,
    _as_source,
    _clean_string,
    _validate_raw_binding,
    _validate_reservation,
)

from vitals.services.ai_gateway.config import (
    _has_capacity,
    _lock_current_quota_rows,
    _lock_current_root,
    _lock_raw_payload_scope,
    _lock_subject_authority,
)


async def reserve_ai_invocation(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    purpose: AIInvocationPurpose | str,
    source: AIInvocationSource | str,
    model: str,
    idempotency_key: str,
    reserved_cost_microunits: int,
    reserved_units: int,
    raw_payload_id: int | None = None,
) -> AIReservationResult:
    """Authorize exact S and reserve both hard ledgers in one short transaction."""

    _validate_reservation(reserved_cost_microunits, reserved_units)
    purpose_value = _as_purpose(purpose)
    raw_payload_value = _validate_raw_binding(purpose_value, raw_payload_id)
    source_value = _as_source(source)
    model_value = _clean_string(model, "model", 128)
    key_value = _clean_string(idempotency_key, "idempotency_key", 128)
    if not isinstance(identity, WriteIdentity):
        raise TypeError("identity must be a WriteIdentity")
    if (source_value is AIInvocationSource.SCHEDULER and identity.actor_user_id is not None) or (
        source_value is not AIInvocationSource.SCHEDULER and identity.actor_user_id is None
    ):
        raise AIGatewayAuthorizationError("AI invocation source does not match actor provenance")
    await _lock_subject_authority(session, identity)
    await _lock_raw_payload_scope(
        session,
        raw_payload_id=raw_payload_value,
        subject_id=identity.subject_id,
    )
    root = await _lock_current_root(session)
    billing_date = now_utc().date()
    platform_quota, subject_quota = await _lock_current_quota_rows(
        session,
        subject_id=identity.subject_id,
        billing_date=billing_date,
    )
    existing = await session.scalar(
        select(AIInvocation)
        .where(
            AIInvocation.subject_id == identity.subject_id,
            AIInvocation.purpose == purpose_value.value,
            AIInvocation.idempotency_key == key_value,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        expected_fingerprint = (
            identity.subject_id,
            identity.actor_user_id,
            raw_payload_value,
            purpose_value.value,
            source_value.value,
            model_value,
            key_value,
            reserved_cost_microunits,
            reserved_units,
            root.id,
            root.config_version,
            platform_quota.period_start,
            platform_quota.period_end,
        )
        actual_fingerprint = (
            existing.subject_id,
            existing.actor_user_id,
            existing.raw_payload_id,
            existing.purpose,
            existing.source,
            existing.model,
            existing.idempotency_key,
            existing.reserved_cost_microunits,
            existing.reserved_units,
            existing.platform_integration_connection_id,
            existing.config_version,
            existing.quota_period_start,
            existing.quota_period_end,
        )
        if actual_fingerprint != expected_fingerprint:
            raise AIIdempotencyConflictError(
                "AI idempotency key is bound to a different call fingerprint"
            )
        status = AIInvocationStatus(existing.status)
        return AIReservationResult(
            invocation_id=existing.id,
            status=status,
            created=False,
            dispatchable=status is AIInvocationStatus.PREPARED,
        )
    for quota in (platform_quota, subject_quota):
        if not _has_capacity(
            quota,
            cost_microunits=reserved_cost_microunits,
            units=reserved_units,
        ):
            raise AIQuotaExceededError("AI quota cannot cover the reservation")
    invocation = AIInvocation(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        raw_payload_id=raw_payload_value,
        platform_integration_connection_id=root.id,
        purpose=purpose_value.value,
        source=source_value.value,
        model=model_value,
        config_version=root.config_version,
        idempotency_key=key_value,
        quota_period_start=platform_quota.period_start,
        quota_period_end=platform_quota.period_end,
        reserved_cost_microunits=reserved_cost_microunits,
        reserved_units=reserved_units,
        charged_cost_microunits=0,
        charged_units=0,
        status=AIInvocationStatus.PREPARED.value,
    )
    for quota in (platform_quota, subject_quota):
        quota.reserved_cost_microunits += reserved_cost_microunits
        quota.reserved_units += reserved_units
    session.add(invocation)
    await session.flush()
    return AIReservationResult(
        invocation_id=invocation.id,
        status=AIInvocationStatus.PREPARED,
        created=True,
        dispatchable=True,
    )


async def _invocation_key(session: AsyncSession, invocation_id: uuid.UUID) -> _InvocationKey:
    if not isinstance(invocation_id, uuid.UUID):
        raise TypeError("invocation_id must be a UUID")
    with session.no_autoflush:
        row = (
            await session.execute(
                select(
                    AIInvocation.id,
                    AIInvocation.subject_id,
                    AIInvocation.actor_user_id,
                    AIInvocation.raw_payload_id,
                    AIInvocation.platform_integration_connection_id,
                    AIInvocation.config_version,
                    AIInvocation.purpose,
                    AIInvocation.source,
                    AIInvocation.model,
                    AIInvocation.idempotency_key,
                    AIInvocation.quota_period_start,
                    AIInvocation.quota_period_end,
                    AIInvocation.reserved_cost_microunits,
                    AIInvocation.reserved_units,
                ).where(AIInvocation.id == invocation_id)
            )
        ).one_or_none()
    if row is None:
        raise AIInvocationStateError("AI invocation does not exist")
    return _InvocationKey(*row)
