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
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.ai import (
    AIPlatformQuotaPeriod,
    AISubjectQuotaPeriod,
)
from vitals.models.identity import HealthSubject
from vitals.services.platform_admin_service import (
    PreparedPlatformAdmin,
    require_prepared_platform_admin,
)

from vitals.services.ai_gateway.contracts import (
    AIGatewayConfigurationError,
    AIQuotaImmutableError,
    _validate_nonnegative_integer,
    _validate_period,
)

from vitals.services.ai_gateway.config import (
    _ensure_nonoverlapping_period,
    _quota_period_is_used,
)


async def configure_platform_quota_period(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
    period_start: date,
    period_end: date,
    cost_limit_microunits: int,
    unit_limit: int,
) -> AIPlatformQuotaPeriod:
    """Configure numeric platform capacity without granting subject access."""

    actor_id = require_prepared_platform_admin(session, prepared)
    _validate_period(period_start, period_end)
    _validate_nonnegative_integer(cost_limit_microunits, "cost_limit_microunits")
    _validate_nonnegative_integer(unit_limit, "unit_limit")
    row = await _ensure_nonoverlapping_period(
        session,
        AIPlatformQuotaPeriod,
        subject_id=None,
        period_start=period_start,
        period_end=period_end,
    )
    if row is None:
        row = AIPlatformQuotaPeriod(
            period_start=period_start,
            period_end=period_end,
            cost_limit_microunits=cost_limit_microunits,
            unit_limit=unit_limit,
            configured_by_user_id=actor_id,
        )
        session.add(row)
    elif row.cost_limit_microunits != cost_limit_microunits or row.unit_limit != unit_limit:
        if await _quota_period_is_used(session, row):
            raise AIQuotaImmutableError("a used AI quota period is immutable")
        row.cost_limit_microunits = cost_limit_microunits
        row.unit_limit = unit_limit
        row.configured_by_user_id = actor_id
    await session.flush()
    return row


async def configure_subject_quota_period(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
    subject_id: uuid.UUID,
    period_start: date,
    period_end: date,
    cost_limit_microunits: int,
    unit_limit: int,
) -> AISubjectQuotaPeriod:
    """Configure capacity by opaque S only; no subject profile is returned."""

    actor_id = require_prepared_platform_admin(session, prepared)
    if not isinstance(subject_id, uuid.UUID):
        raise TypeError("subject_id must be a UUID")
    _validate_period(period_start, period_end)
    _validate_nonnegative_integer(cost_limit_microunits, "cost_limit_microunits")
    _validate_nonnegative_integer(unit_limit, "unit_limit")
    subject_exists = await session.scalar(
        select(HealthSubject.id).where(HealthSubject.id == subject_id).with_for_update()
    )
    if subject_exists is None:
        raise AIGatewayConfigurationError("quota subject does not exist")
    platform_period = await session.scalar(
        select(AIPlatformQuotaPeriod)
        .where(
            AIPlatformQuotaPeriod.period_start == period_start,
            AIPlatformQuotaPeriod.period_end == period_end,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if platform_period is None:
        raise AIGatewayConfigurationError(
            "subject AI quota period must align to an existing platform period"
        )
    row = await _ensure_nonoverlapping_period(
        session,
        AISubjectQuotaPeriod,
        subject_id=subject_id,
        period_start=period_start,
        period_end=period_end,
    )
    if row is None:
        row = AISubjectQuotaPeriod(
            subject_id=subject_id,
            period_start=period_start,
            period_end=period_end,
            cost_limit_microunits=cost_limit_microunits,
            unit_limit=unit_limit,
            configured_by_user_id=actor_id,
        )
        session.add(row)
    elif row.cost_limit_microunits != cost_limit_microunits or row.unit_limit != unit_limit:
        if await _quota_period_is_used(session, row):
            raise AIQuotaImmutableError("a used AI quota period is immutable")
        row.cost_limit_microunits = cost_limit_microunits
        row.unit_limit = unit_limit
        row.configured_by_user_id = actor_id
    await session.flush()
    return row
