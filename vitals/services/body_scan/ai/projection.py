"""Redacted body-scan AI availability projection."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import IntegrationConnectionStatus
from vitals.models.ai import AIPlatformQuotaPeriod, AISubjectQuotaPeriod
from vitals.models.tenancy import PlatformIntegrationConnection
from vitals.services.ai_gateway import contracts as ai_gateway_service_contracts
from vitals.utils.timeutils import now_utc

from .contracts import BodyScanAIAvailability, BodyScanAIAvailabilityCode
from .scope import _lock_owner

def _resolve_openrouter_credential(credential_ref: str) -> str | None:
    if credential_ref not in ai_gateway_service_contracts.ALLOWED_CREDENTIAL_REFS:
        return None
    value = load_config().openrouter_api_key.strip()
    return value or None


async def project_body_scan_ai_availability(
    session: AsyncSession,
    *,
    actor_username: str,
) -> BodyScanAIAvailability:
    """Project exact-owner gateway readiness without exposing limits or PHI."""

    subject, _owner, _identity = await _lock_owner(
        session,
        actor_username=actor_username,
    )
    billing_date = now_utc().date()
    roots = list(
        await session.scalars(
            select(PlatformIntegrationConnection)
            .where(
                PlatformIntegrationConnection.status
                == IntegrationConnectionStatus.ACTIVE.value
            )
            .limit(2)
        )
    )
    if len(roots) != 1 or _resolve_openrouter_credential(roots[0].credential_ref) is None:
        return BodyScanAIAvailability(False, BodyScanAIAvailabilityCode.NOT_CONFIGURED)
    platform_periods = list(
        await session.scalars(
            select(AIPlatformQuotaPeriod).where(
                AIPlatformQuotaPeriod.period_start <= billing_date,
                AIPlatformQuotaPeriod.period_end > billing_date,
            )
        )
    )
    subject_periods = list(
        await session.scalars(
            select(AISubjectQuotaPeriod).where(
                AISubjectQuotaPeriod.subject_id == subject.id,
                AISubjectQuotaPeriod.period_start <= billing_date,
                AISubjectQuotaPeriod.period_end > billing_date,
            )
        )
    )
    if (
        len(platform_periods) != 1
        or len(subject_periods) != 1
        or subject_periods[0].period_start != platform_periods[0].period_start
        or subject_periods[0].period_end != platform_periods[0].period_end
    ):
        return BodyScanAIAvailability(False, BodyScanAIAvailabilityCode.NOT_CONFIGURED)
    if any(
        row.reserved_cost_microunits + row.charged_cost_microunits
        >= row.cost_limit_microunits
        or row.reserved_units + row.charged_units >= row.unit_limit
        for row in (platform_periods[0], subject_periods[0])
    ):
        return BodyScanAIAvailability(False, BodyScanAIAvailabilityCode.QUOTA)
    return BodyScanAIAvailability(True, BodyScanAIAvailabilityCode.AVAILABLE)
