"""Superadmin-only control plane for the installation-wide AI gateway.

This module deliberately exposes no prompt, artifact, subject profile, provider
secret, or credential resolver value. It composes the lower-level gateway and
quota primitives under a transaction-bound ``PreparedPlatformAdmin``; every
mutation only flushes and the delivery boundary owns commit or rollback.

Lock order is identity governance -> admin User/role -> opaque S -> platform
root -> platform quota -> subject quota. There is no provider or other network
I/O in this control plane.
"""
from __future__ import annotations

from vitals.services.ai_gateway import config as ai_gateway_service_config
from vitals.services.ai_gateway import quota as ai_gateway_service_quota

import uuid
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import IntegrationConnectionStatus
from vitals.models.ai import AIPlatformQuotaPeriod, AISubjectQuotaPeriod
from vitals.models.identity import HealthSubject
from vitals.models.tenancy import PlatformIntegrationConnection
from vitals.services.platform import authorization as platform_authorization
from vitals.services.platform.authorization import PreparedPlatformAdmin

OPENROUTER_CREDENTIAL_REF = "env:VITALS_OPENROUTER_API_KEY"


class PlatformAIControlError(RuntimeError):
    """A fail-closed platform AI control operation was invalid."""


class GatewayTransitionAction(StrEnum):
    NO_CHANGE = "no_change"
    CREATED = "created"
    ROTATED = "rotated"
    ROTATED_DISABLED = "rotated_disabled"
    ENABLED = "enabled"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class GatewayState:
    status: str | None
    config_version: int | None


@dataclass(frozen=True, slots=True)
class PlatformQuotaState:
    period_start: date
    period_end: date
    cost_limit_microunits: int
    unit_limit: int
    reserved_cost_microunits: int
    charged_cost_microunits: int
    reserved_units: int
    charged_units: int


@dataclass(frozen=True, slots=True)
class SubjectQuotaState:
    subject_id: uuid.UUID
    period_start: date
    period_end: date
    cost_limit_microunits: int
    unit_limit: int
    reserved_cost_microunits: int
    charged_cost_microunits: int
    reserved_units: int
    charged_units: int


@dataclass(frozen=True, slots=True)
class PlatformAIControlSnapshot:
    gateway: GatewayState
    eligible_subject_ids: tuple[uuid.UUID, ...]
    platform_periods: tuple[PlatformQuotaState, ...]
    subject_periods: tuple[SubjectQuotaState, ...]


@dataclass(frozen=True, slots=True)
class GatewayTransition:
    action: GatewayTransitionAction
    status: str | None
    config_version: int | None


@dataclass(frozen=True, slots=True)
class AlignedQuotaResult:
    changed: bool
    platform: PlatformQuotaState
    subject: SubjectQuotaState


def _gateway_state(row: PlatformIntegrationConnection | None) -> GatewayState:
    return GatewayState(
        status=row.status if row is not None else None,
        config_version=row.config_version if row is not None else None,
    )


def _platform_quota_state(row: AIPlatformQuotaPeriod) -> PlatformQuotaState:
    return PlatformQuotaState(
        period_start=row.period_start,
        period_end=row.period_end,
        cost_limit_microunits=row.cost_limit_microunits,
        unit_limit=row.unit_limit,
        reserved_cost_microunits=row.reserved_cost_microunits,
        charged_cost_microunits=row.charged_cost_microunits,
        reserved_units=row.reserved_units,
        charged_units=row.charged_units,
    )


def _subject_quota_state(row: AISubjectQuotaPeriod) -> SubjectQuotaState:
    return SubjectQuotaState(
        subject_id=row.subject_id,
        period_start=row.period_start,
        period_end=row.period_end,
        cost_limit_microunits=row.cost_limit_microunits,
        unit_limit=row.unit_limit,
        reserved_cost_microunits=row.reserved_cost_microunits,
        charged_cost_microunits=row.charged_cost_microunits,
        reserved_units=row.reserved_units,
        charged_units=row.charged_units,
    )


async def get_platform_ai_control_snapshot(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
) -> PlatformAIControlSnapshot:
    """Return redacted control state after an exact live admin check."""

    platform_authorization.require_prepared_platform_admin(session, prepared)
    current = await session.scalar(
        select(PlatformIntegrationConnection)
        .where(PlatformIntegrationConnection.status != IntegrationConnectionStatus.RETIRED.value)
        .limit(1)
    )
    subject_ids = tuple(
        await session.scalars(select(HealthSubject.id).order_by(HealthSubject.id))
    )
    platform_rows = tuple(
        await session.scalars(
            select(AIPlatformQuotaPeriod).order_by(
                AIPlatformQuotaPeriod.period_start,
                AIPlatformQuotaPeriod.period_end,
            )
        )
    )
    subject_rows = tuple(
        await session.scalars(
            select(AISubjectQuotaPeriod).order_by(
                AISubjectQuotaPeriod.subject_id,
                AISubjectQuotaPeriod.period_start,
                AISubjectQuotaPeriod.period_end,
            )
        )
    )
    return PlatformAIControlSnapshot(
        gateway=_gateway_state(current),
        eligible_subject_ids=subject_ids,
        platform_periods=tuple(_platform_quota_state(row) for row in platform_rows),
        subject_periods=tuple(_subject_quota_state(row) for row in subject_rows),
    )


async def apply_gateway_configuration(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
    configuration_changed: bool,
    credential_available: bool,
    desired_enabled: bool | None = None,
    changed_fields: frozenset[str] = frozenset(),
) -> GatewayTransition:
    """Create, rotate, enable, or disable one immutable gateway root.

    ``desired_enabled=None`` preserves the current kill-switch state. A disabled
    root is therefore rotated and immediately disabled when configuration changes.
    """

    platform_authorization.require_prepared_platform_admin(session, prepared)
    if not isinstance(configuration_changed, bool):
        raise TypeError("configuration_changed must be a bool")
    if not isinstance(credential_available, bool):
        raise TypeError("credential_available must be a bool")
    if desired_enabled is not None and not isinstance(desired_enabled, bool):
        raise TypeError("desired_enabled must be a bool or None")
    reviewed_fields = frozenset(
        platform_authorization.validate_openrouter_changed_fields(changed_fields)
    )
    if configuration_changed != bool(reviewed_fields):
        raise PlatformAIControlError(
            "configuration change must name its reviewed changed fields"
        )

    current = await session.scalar(
        select(PlatformIntegrationConnection)
        .where(PlatformIntegrationConnection.status != IntegrationConnectionStatus.RETIRED.value)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current is not None and current.status not in {
        IntegrationConnectionStatus.ACTIVE.value,
        IntegrationConnectionStatus.DISABLED.value,
    }:
        raise PlatformAIControlError("platform AI gateway lifecycle is invalid")

    needs_credential = current is None or configuration_changed or desired_enabled is True
    if needs_credential and not credential_available:
        raise PlatformAIControlError("platform AI credential is not configured")

    action = GatewayTransitionAction.NO_CHANGE
    result = current
    if current is None:
        if desired_enabled is False and not configuration_changed:
            return GatewayTransition(action=action, status=None, config_version=None)
        result = await ai_gateway_service_config.create_gateway(
            session,
            prepared=prepared,
            external_account_discriminator=uuid.uuid4().hex,
            credential_ref=OPENROUTER_CREDENTIAL_REF,
        )
        action = GatewayTransitionAction.CREATED
    elif configuration_changed:
        was_disabled = current.status == IntegrationConnectionStatus.DISABLED.value
        result = await ai_gateway_service_config.rotate_gateway(
            session,
            prepared=prepared,
            external_account_discriminator=uuid.uuid4().hex,
            credential_ref=OPENROUTER_CREDENTIAL_REF,
        )
        if desired_enabled is False or (desired_enabled is None and was_disabled):
            result = await ai_gateway_service_config.disable_gateway(
                session,
                prepared=prepared,
            )
            action = GatewayTransitionAction.ROTATED_DISABLED
        elif was_disabled and desired_enabled is True:
            action = GatewayTransitionAction.ENABLED
        else:
            action = GatewayTransitionAction.ROTATED
    elif desired_enabled is True and current.status == IntegrationConnectionStatus.DISABLED.value:
        result = await ai_gateway_service_config.rotate_gateway(
            session,
            prepared=prepared,
            external_account_discriminator=uuid.uuid4().hex,
            credential_ref=OPENROUTER_CREDENTIAL_REF,
        )
        action = GatewayTransitionAction.ENABLED
    elif desired_enabled is False and current.status == IntegrationConnectionStatus.ACTIVE.value:
        result = await ai_gateway_service_config.disable_gateway(session, prepared=prepared)
        action = GatewayTransitionAction.DISABLED

    if action is not GatewayTransitionAction.NO_CHANGE:
        result_code = {
            GatewayTransitionAction.CREATED: "gateway_created",
            GatewayTransitionAction.ROTATED: "gateway_rotated",
            GatewayTransitionAction.ROTATED_DISABLED: "gateway_rotated_disabled",
            GatewayTransitionAction.ENABLED: "gateway_enabled",
            GatewayTransitionAction.DISABLED: "gateway_disabled",
        }[action]
        await platform_authorization.record_openrouter_configuration_change(
            session,
            prepared=prepared,
            changed_fields=reviewed_fields,
            result_code=result_code,
        )

    state = _gateway_state(result)
    return GatewayTransition(
        action=action,
        status=state.status,
        config_version=state.config_version,
    )


async def configure_aligned_quota_period(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
    subject_id: uuid.UUID,
    period_start: date,
    period_end: date,
    platform_cost_limit_microunits: int,
    platform_unit_limit: int,
    subject_cost_limit_microunits: int,
    subject_unit_limit: int,
) -> AlignedQuotaResult:
    """Configure one platform period and an exactly aligned opaque-S budget."""

    platform_authorization.require_prepared_platform_admin(session, prepared)
    if not isinstance(subject_id, uuid.UUID):
        raise TypeError("subject_id must be a UUID")
    if (
        isinstance(platform_cost_limit_microunits, int)
        and isinstance(subject_cost_limit_microunits, int)
        and subject_cost_limit_microunits > platform_cost_limit_microunits
    ) or (
        isinstance(platform_unit_limit, int)
        and isinstance(subject_unit_limit, int)
        and subject_unit_limit > platform_unit_limit
    ):
        raise ValueError("subject AI quota cannot exceed the platform quota")

    # Acquire S before either quota table. This is an ID-only projection: the
    # platform role receives no subject profile or health-data capability.
    locked_subject_id = await session.scalar(
        select(HealthSubject.id)
        .where(HealthSubject.id == subject_id)
        .with_for_update()
    )
    if locked_subject_id is None:
        raise PlatformAIControlError("quota subject does not exist")

    existing_platform = await session.scalar(
        select(AIPlatformQuotaPeriod)
        .where(
            AIPlatformQuotaPeriod.period_start == period_start,
            AIPlatformQuotaPeriod.period_end == period_end,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    existing_subject = await session.scalar(
        select(AISubjectQuotaPeriod)
        .where(
            AISubjectQuotaPeriod.subject_id == subject_id,
            AISubjectQuotaPeriod.period_start == period_start,
            AISubjectQuotaPeriod.period_end == period_end,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    changed = (
        existing_platform is None
        or existing_platform.cost_limit_microunits != platform_cost_limit_microunits
        or existing_platform.unit_limit != platform_unit_limit
        or existing_subject is None
        or existing_subject.cost_limit_microunits != subject_cost_limit_microunits
        or existing_subject.unit_limit != subject_unit_limit
    )

    platform = await ai_gateway_service_quota.configure_platform_quota_period(
        session,
        prepared=prepared,
        period_start=period_start,
        period_end=period_end,
        cost_limit_microunits=platform_cost_limit_microunits,
        unit_limit=platform_unit_limit,
    )
    subject = await ai_gateway_service_quota.configure_subject_quota_period(
        session,
        prepared=prepared,
        subject_id=subject_id,
        period_start=period_start,
        period_end=period_end,
        cost_limit_microunits=subject_cost_limit_microunits,
        unit_limit=subject_unit_limit,
    )
    if changed:
        await platform_authorization.record_openrouter_configuration_change(
            session,
            prepared=prepared,
            changed_fields=("platform_quota", "subject_quota"),
            result_code="quota_configured",
        )
    return AlignedQuotaResult(
        changed=changed,
        platform=_platform_quota_state(platform),
        subject=_subject_quota_state(subject),
    )


__all__ = [
    "AlignedQuotaResult",
    "GatewayState",
    "GatewayTransition",
    "GatewayTransitionAction",
    "OPENROUTER_CREDENTIAL_REF",
    "PlatformAIControlError",
    "PlatformAIControlSnapshot",
    "PlatformQuotaState",
    "SubjectQuotaState",
    "apply_gateway_configuration",
    "configure_aligned_quota_period",
    "get_platform_ai_control_snapshot",
]
