"""Derived alerts for Weight noise markers."""
from __future__ import annotations

from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.alerts import lifecycle as alerts_service_lifecycle

from datetime import date as date_type
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Severity
from vitals.i18n import t
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.utils.timeutils import today_local

from .governance import (
    require_aux_prepared_write as _require_aux_prepared_write,
    require_evaluation_date as _require_evaluation_date,
)
from .noise import list_noise_markers

NOISE_ALERT_KEY = "weight.noisy_period_active"


async def refresh_noise_alert(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[object]:
    """Raise an ``info`` alert while today sits inside a noise range; resolve it
    once it doesn't. Idempotent (safe to call on every dashboard load / tick)."""
    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    today = on_date or today_local()
    _require_evaluation_date(context, today)
    # The alerts domain still offers a compatibility bridge; the weight domain
    # no longer needs one of its own, so this is read off the capability.
    alert_bridge = (
        alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED
        if context.legacy_bridge
        is engine.LegacyConflictBridge.FULLY_UNOWNED
        else alerts_service_contracts.LegacyAlertBridge.REJECT
    )
    active_reason = None
    for marker in await list_noise_markers(
        session,
        subject_id=identity.subject_id,
    ):
        end = marker.end_date
        if (end is None and today >= marker.start_date) or (
            end is not None and marker.start_date <= today <= end
        ):
            active_reason = marker.reason
            break

    if active_reason is not None:
        # Don't re-raise if the user already dismissed this alert today — it will
        # reappear automatically the next calendar day.
        system_context = alerts_service_contracts.HealthAlertContext(
            WriteIdentity(context.identity.subject_id, None)
        )
        if await alerts_service_lifecycle.was_scoped_dismissed_today(
            session,
            context=system_context,
            alert_key=NOISE_ALERT_KEY,
            entity_ref="",
            on_date=today,
            legacy_bridge=alert_bridge,
        ):
            return None
        return await alerts_service_lifecycle.raise_scoped_alert(
            session,
            context=system_context,
            domain=Domain.WEIGHT,
            severity=Severity.INFO,
            message=t("alert.weight_noisy", reason=active_reason),
            alert_key=NOISE_ALERT_KEY,
            legacy_bridge=alert_bridge,
        )
    system_context = alerts_service_contracts.HealthAlertContext(
        WriteIdentity(context.identity.subject_id, None)
    )
    return await alerts_service_lifecycle.resolve_scoped_by_key(
        session,
        context=system_context,
        alert_key=NOISE_ALERT_KEY,
        legacy_bridge=alert_bridge,
    )
