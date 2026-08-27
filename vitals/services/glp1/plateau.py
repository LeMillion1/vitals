
"""GLP-1 plateau evaluation and alert reconciliation."""
from __future__ import annotations

from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.alerts import lifecycle as alerts_service_lifecycle

import uuid
from datetime import date as date_type
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.analytics.regression import fit_trend
from vitals.enums import Domain, Severity
from vitals.i18n import t
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.glp1.queries import active_dose_phase
from vitals.services.glp1.writes import (
    _require_evaluation_date,
    _require_scoped_prepared_write,
)
from vitals.services.weight.logs import list_active_weights
from vitals.services.weight.noise import noise_ranges
from vitals.utils.timeutils import today_local

PLATEAU_ALERT_KEY = "glp1.plateau"
PLATEAU_MIN_DAYS = 14
PLATEAU_SLOPE_THRESHOLD = -0.1


async def evaluate_plateau(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    on_date: Optional[date_type] = None,
    scope: engine.ConflictScope | None = None,
) -> Optional[dict]:
    """Pure read: is the current dose plateaued? Returns a context dict
    (drug, dose, days_on_dose, slope_per_week) when a plateau is detected on the
    current phase, else ``None``. Writes nothing.

    A plateau is a fact about one person's dose and one person's weight trend.
    ``scope`` carries that subject on the conflict path; a composition caller
    that has no conflict decision passes ``subject_id`` directly.
    """

    if scope is not None and scope.subject_id != subject_id:
        raise engine.ConflictPreparedWriteError(
            "plateau subject does not match the prepared conflict scope"
        )
    today = scope.evaluation_date if scope is not None else (on_date or today_local())
    if on_date is not None and on_date != today:
        raise engine.ConflictPreparedWriteError(
            "plateau date does not match the prepared conflict scope"
        )
    phase = await active_dose_phase(
        session,
        on_date=today,
        subject_id=subject_id,
    )
    if phase is None:
        return None

    days_on_dose = (today - phase.start_date).days
    if days_on_dose < PLATEAU_MIN_DAYS:
        return None

    weights = await list_active_weights(
        session,
        start=phase.start_date,
        end=today,
        subject_id=subject_id,
    )
    points = [(w.date, w.weight_kg) for w in weights]
    ranges = await noise_ranges(
        session,
        subject_id=subject_id,
        start=phase.start_date,
        end=today,
    )
    trend = fit_trend(points, exclude=ranges)
    if trend is None:
        return None

    if trend.slope_per_week >= PLATEAU_SLOPE_THRESHOLD:
        return {
            "drug": phase.drug,
            "dose_mg": phase.dose_mg,
            "days_on_dose": days_on_dose,
            "slope_per_week": round(trend.slope_per_week, 3),
        }
    return None

async def refresh_plateau_alert(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[object]:
    """Raise a ``note`` alert while the current dose is plateaued; resolve it once
    progress resumes (or the dose changes). Idempotent — safe on every dashboard
    load / scheduler tick. Respects same-day dismissal like the noise alert."""
    write_context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if on_date is not None:
        _require_evaluation_date(write_context, on_date)
    plateau = await evaluate_plateau(
        session,
        on_date=on_date,
        subject_id=write_context.identity.subject_id,
        scope=write_context.scope,
    )

    system_identity = WriteIdentity(write_context.identity.subject_id, None)
    alert_context = alerts_service_contracts.HealthAlertContext(system_identity)
    alert_bridge = (
        alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED
        if write_context.scope.include_legacy_unowned
        else alerts_service_contracts.LegacyAlertBridge.REJECT
    )
    if plateau is not None:
        if await alerts_service_lifecycle.was_scoped_dismissed_today(
            session,
            context=alert_context,
            alert_key=PLATEAU_ALERT_KEY,
            entity_ref="",
            on_date=write_context.evaluation_date,
            legacy_bridge=alert_bridge,
        ):
            return None
        message = t(
            "alert.glp1_plateau",
            drug=plateau["drug"],
            dose=plateau["dose_mg"],
            days=plateau["days_on_dose"],
            slope=plateau["slope_per_week"],
        )
        return await alerts_service_lifecycle.raise_scoped_alert(
            session,
            context=alert_context,
            domain=Domain.GLP1,
            severity=Severity.NOTE,
            message=message,
            alert_key=PLATEAU_ALERT_KEY,
            legacy_bridge=alert_bridge,
        )
    return await alerts_service_lifecycle.resolve_scoped_by_key(
        session,
        context=alert_context,
        alert_key=PLATEAU_ALERT_KEY,
        legacy_bridge=alert_bridge,
    )
