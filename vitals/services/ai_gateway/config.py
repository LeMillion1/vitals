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

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.ai import (
    AIInvocation,
    AIPlatformQuotaPeriod,
    AISubjectQuotaPeriod,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import PlatformIntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.platform_admin_service import (
    PreparedPlatformAdmin,
    require_prepared_platform_admin,
)
from vitals.utils.timeutils import now_utc

from vitals.services.ai_gateway.contracts import (
    MAX_SIGNED_BIGINT,
    AIGatewayAuthorizationError,
    AIGatewayConfigurationError,
    AIQuotaImmutableError,
    _InvocationKey,
    _clean_string,
    _credential_ref,
)


async def _lock_subject_authority(
    session: AsyncSession,
    identity: WriteIdentity,
) -> HealthSubject:
    if not isinstance(identity, WriteIdentity):
        raise TypeError("identity must be a WriteIdentity")
    await acquire_identity_governance_lock(session)
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == identity.subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if subject is None:
        raise AIGatewayAuthorizationError("AI subject authorization failed")
    if identity.actor_user_id is None:
        return subject
    actor = await session.scalar(
        select(User)
        .where(User.id == identity.actor_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        actor is None
        or actor.status != UserStatus.ACTIVE.value
        or subject.owner_user_id != actor.id
    ):
        raise AIGatewayAuthorizationError("AI subject authorization failed")
    return subject


async def _lock_raw_payload_scope(
    session: AsyncSession,
    *,
    raw_payload_id: int | None,
    subject_id: uuid.UUID,
) -> None:
    """Lock only an opaque raw-row projection and prove its exact S."""

    if raw_payload_id is None:
        return
    row = (
        await session.execute(
            select(RawPayload.id, RawPayload.subject_id)
            .where(RawPayload.id == raw_payload_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None or row.subject_id != subject_id:
        raise AIGatewayAuthorizationError("AI raw payload authorization failed")


async def _lock_current_root(session: AsyncSession) -> PlatformIntegrationConnection:
    root = await session.scalar(
        select(PlatformIntegrationConnection)
        .where(
            PlatformIntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
            PlatformIntegrationConnection.connection_type
            == IntegrationConnectionType.AI_GATEWAY.value,
            PlatformIntegrationConnection.status == IntegrationConnectionStatus.ACTIVE.value,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if root is None:
        raise AIGatewayConfigurationError("active platform AI gateway is required")
    return root


async def _lock_exact_root(
    session: AsyncSession,
    invocation: AIInvocation | _InvocationKey,
    *,
    require_active: bool,
) -> PlatformIntegrationConnection:
    root = await session.scalar(
        select(PlatformIntegrationConnection)
        .where(PlatformIntegrationConnection.id == invocation.platform_integration_connection_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        root is None
        or root.config_version != invocation.config_version
        or (require_active and root.status != IntegrationConnectionStatus.ACTIVE.value)
    ):
        raise AIGatewayConfigurationError("exact active platform AI gateway provenance is required")
    return root


async def _lock_quota_rows(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    period_start: date,
    period_end: date,
) -> tuple[AIPlatformQuotaPeriod, AISubjectQuotaPeriod]:
    platform = await session.scalar(
        select(AIPlatformQuotaPeriod)
        .where(
            AIPlatformQuotaPeriod.period_start == period_start,
            AIPlatformQuotaPeriod.period_end == period_end,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if platform is None:
        raise AIGatewayConfigurationError("exact platform AI quota period is required")
    subject = await session.scalar(
        select(AISubjectQuotaPeriod)
        .where(
            AISubjectQuotaPeriod.subject_id == subject_id,
            AISubjectQuotaPeriod.period_start == period_start,
            AISubjectQuotaPeriod.period_end == period_end,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if subject is None:
        raise AIGatewayConfigurationError("exact subject AI quota period is required")
    return platform, subject


async def _lock_current_quota_rows(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    billing_date: date,
) -> tuple[AIPlatformQuotaPeriod, AISubjectQuotaPeriod]:
    platform_rows = list(
        await session.scalars(
            select(AIPlatformQuotaPeriod)
            .where(
                AIPlatformQuotaPeriod.period_start <= billing_date,
                AIPlatformQuotaPeriod.period_end > billing_date,
            )
            .order_by(
                AIPlatformQuotaPeriod.period_start,
                AIPlatformQuotaPeriod.period_end,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(platform_rows) != 1:
        raise AIGatewayConfigurationError(
            "current UTC date requires exactly one platform AI quota period"
        )
    platform = platform_rows[0]
    subject_rows = list(
        await session.scalars(
            select(AISubjectQuotaPeriod)
            .where(
                AISubjectQuotaPeriod.subject_id == subject_id,
                AISubjectQuotaPeriod.period_start <= billing_date,
                AISubjectQuotaPeriod.period_end > billing_date,
            )
            .order_by(
                AISubjectQuotaPeriod.period_start,
                AISubjectQuotaPeriod.period_end,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(subject_rows) != 1:
        raise AIGatewayConfigurationError(
            "current UTC date requires exactly one subject AI quota period"
        )
    subject = subject_rows[0]
    if subject.period_start != platform.period_start or subject.period_end != platform.period_end:
        raise AIGatewayConfigurationError(
            "subject AI quota period must align to the platform period"
        )
    return platform, subject


def _has_capacity(row, *, cost_microunits: int, units: int) -> bool:
    used_cost = row.reserved_cost_microunits + row.charged_cost_microunits
    used_units = row.reserved_units + row.charged_units
    if used_cost > MAX_SIGNED_BIGINT - cost_microunits or used_units > MAX_SIGNED_BIGINT - units:
        return False
    return (
        used_cost + cost_microunits <= row.cost_limit_microunits
        and used_units + units <= row.unit_limit
    )


async def create_gateway(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
    external_account_discriminator: str,
    credential_ref: str,
) -> PlatformIntegrationConnection:
    """Create the first active root under platform-superadmin control only."""

    actor_id = require_prepared_platform_admin(session, prepared)
    discriminator = _clean_string(
        external_account_discriminator, "external_account_discriminator", 128
    )
    resolver_ref = _credential_ref(credential_ref)
    current = await session.scalar(
        select(PlatformIntegrationConnection)
        .where(PlatformIntegrationConnection.status != "retired")
        .with_for_update()
    )
    if current is not None:
        raise AIGatewayConfigurationError("a current platform AI gateway exists")
    root = PlatformIntegrationConnection(
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=discriminator,
        credential_ref=resolver_ref,
        status=IntegrationConnectionStatus.ACTIVE.value,
        config_version=1,
        configured_by_user_id=actor_id,
    )
    session.add(root)
    await session.flush()
    return root


async def rotate_gateway(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
    external_account_discriminator: str,
    credential_ref: str,
) -> PlatformIntegrationConnection:
    """Atomically retire the immutable current root and insert its replacement."""

    actor_id = require_prepared_platform_admin(session, prepared)
    discriminator = _clean_string(
        external_account_discriminator, "external_account_discriminator", 128
    )
    resolver_ref = _credential_ref(credential_ref)
    current = await session.scalar(
        select(PlatformIntegrationConnection)
        .where(PlatformIntegrationConnection.status != "retired")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current is None:
        raise AIGatewayConfigurationError("a current platform AI gateway is required")
    current.status = IntegrationConnectionStatus.RETIRED.value
    current.retired_at = now_utc()
    await session.flush()
    replacement = PlatformIntegrationConnection(
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=discriminator,
        credential_ref=resolver_ref,
        status=IntegrationConnectionStatus.ACTIVE.value,
        config_version=current.config_version + 1,
        configured_by_user_id=actor_id,
    )
    session.add(replacement)
    await session.flush()
    return replacement


async def disable_gateway(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
) -> PlatformIntegrationConnection:
    """Disable fresh dispatch without changing immutable root identity."""

    require_prepared_platform_admin(session, prepared)
    current = await session.scalar(
        select(PlatformIntegrationConnection)
        .where(PlatformIntegrationConnection.status != "retired")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current is None:
        raise AIGatewayConfigurationError("a current platform AI gateway is required")
    current.status = IntegrationConnectionStatus.DISABLED.value
    await session.flush()
    return current


async def _ensure_nonoverlapping_period(
    session: AsyncSession,
    model,
    *,
    subject_id: uuid.UUID | None,
    period_start: date,
    period_end: date,
):
    """Lock the periods this one would straddle and refuse a partial overlap.

    ``subject_id`` is mandatory and pairs with ``model``: a subject period is
    always looked up inside one person's ledger, and ``None`` belongs to the
    platform table, which has no subject column to scope by. Passing ``None``
    for a subject model would compare one person's budget against everybody's,
    so it is refused rather than silently widened.
    """

    if subject_id is None:
        if model is not AIPlatformQuotaPeriod:
            raise AIGatewayConfigurationError(
                "a subject quota period must name the subject it belongs to"
            )
    elif model is AIPlatformQuotaPeriod:
        raise AIGatewayConfigurationError("the platform quota ledger belongs to no subject")
    query = select(model).where(
        model.period_start < period_end,
        model.period_end > period_start,
    )
    if subject_id is not None:
        query = query.where(model.subject_id == subject_id)
    rows = list(await session.scalars(query.with_for_update()))
    for row in rows:
        if row.period_start != period_start or row.period_end != period_end:
            raise AIQuotaImmutableError("AI quota periods must not overlap")
    return rows[0] if rows else None


async def _quota_period_is_used(
    session: AsyncSession,
    row: AIPlatformQuotaPeriod | AISubjectQuotaPeriod,
) -> bool:
    if any(
        (
            row.reserved_cost_microunits,
            row.charged_cost_microunits,
            row.reserved_units,
            row.charged_units,
        )
    ):
        return True
    query = select(AIInvocation.id).where(
        AIInvocation.quota_period_start == row.period_start,
        AIInvocation.quota_period_end == row.period_end,
    )
    if isinstance(row, AISubjectQuotaPeriod):
        query = query.where(AIInvocation.subject_id == row.subject_id)
    return await session.scalar(query.limit(1)) is not None
