"""Live progress projections for Milestone cards and digest context."""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain
from vitals.models.milestones import Milestone
from vitals.services.milestones.governance import MilestoneOwnershipError
from vitals.services.milestones.queries import list_milestones
from vitals.utils.timeutils import today_local

_WEIGHT_UNITS = {"kg", "кг"}
_PERCENT_UNITS = {"%", "pct", "percent", "процент", "проценты"}


async def _current_weight(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> Optional[float]:
    """Latest active weight, imported lazily to avoid a hard module dependency."""
    from vitals.services.weight import logs as weight_logs

    weights = await weight_logs.list_active_weights(
        session,
        subject_id=subject_id,
    )
    return weights[-1].weight_kg if weights else None


async def _current_body_fat(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> Optional[float]:
    """Latest active body fat percentage, either Navy or InBody (BIA) based on
    preference. A BIA scan is a direct measurement, so by default it outranks the
    Navy tape-formula estimate whenever body_comp is enabled and has one — Navy is
    only the fallback when there's no BIA data (not a "most recent date" contest)."""
    from vitals.config import load_config
    from vitals.services.weight import measurements as weight_measurements
    from vitals.services.modules_service import get_enabled_modules

    config = load_config()
    source_pref = config.body_fat_source or "latest"

    enabled = await get_enabled_modules(session, subject_id=subject_id)
    body_comp_enabled = enabled.get("body_comp", False)

    # 1. Fetch Navy measurements if not pinned to bia
    navy_val = None
    if source_pref in ("latest", "navy"):
        measurements = await weight_measurements.list_body_measurements(
            session,
            subject_id=subject_id,
        )
        # Find the latest measurement with body_fat_pct
        for m in reversed(measurements):
            if m.body_fat_pct is not None:
                navy_val = m.body_fat_pct
                break

    # 2. Fetch BIA scans if body_comp is enabled and not pinned to navy
    bia_val = None
    if body_comp_enabled and source_pref in ("latest", "bia"):
        from vitals.analytics import body_metrics
        from vitals.services.body_scan.scans import queries as body_scan_queries

        scan_rows = await body_scan_queries.list_scans(
            session,
            subject_id=subject_id,
        )
        for s in scan_rows:
            bf_val = body_metrics.body_fat_pct_from_scan(s.metrics)
            if bf_val is not None:
                bia_val = bf_val
                break

    # 3. Resolve based on preference
    if source_pref == "navy":
        return navy_val
    if source_pref == "bia":
        return bia_val

    # "latest" (default): BIA wins whenever it's available; Navy is the fallback.
    return bia_val if bia_val is not None else navy_val


def _unit_matches_domain(domain: str, target_unit: Optional[str]) -> bool:
    """Guard against a goal whose ``target_unit`` doesn't match what ``progress()``
    actually measures for its domain — e.g. a body-fat "%" target filed under
    ``domain="weight"`` (the create-goal form's unit field is free text, so nothing
    stops this at write time). Without this, ``current``/``remaining`` silently
    compare a percentage against a kilogram reading. No unit set at all is left
    permissive, since older goals predate this being checked."""
    if not target_unit:
        return True
    normalized = target_unit.strip().lower()
    if domain == Domain.WEIGHT.value:
        return normalized in _WEIGHT_UNITS
    if domain == Domain.BODY_COMPOSITION.value:
        return normalized in _PERCENT_UNITS
    return True


def _require_progress_scope(
    milestone: Milestone,
    *,
    subject_id: uuid.UUID,
) -> None:
    """Progress is computed from one person's weight; refuse another's goal."""

    if milestone.subject_id != subject_id:
        raise MilestoneOwnershipError("milestone belongs to another subject")


async def progress(
    session: AsyncSession,
    milestone: Milestone,
    *,
    subject_id: uuid.UUID,
) -> dict:
    """Live progress for a goal. Weight goals get current/remaining/pct vs target;
    others just echo status + days-to-deadline."""
    _require_progress_scope(milestone, subject_id=subject_id)
    today = today_local()
    days_left = (milestone.deadline - today).days if milestone.deadline else None
    out: dict = {
        "id": milestone.id,
        "name": milestone.name,
        "domain": milestone.domain,
        "status": milestone.status,
        "target_value": milestone.target_value,
        "target_unit": milestone.target_unit,
        "deadline": milestone.deadline.isoformat() if milestone.deadline else None,
        "days_left": days_left,
        "current": None,
        "remaining": None,
        "pct": None,
    }

    unit_ok = _unit_matches_domain(milestone.domain, milestone.target_unit)
    if milestone.domain == Domain.WEIGHT.value and milestone.target_value is not None and unit_ok:
        current = await _current_weight(session, subject_id=subject_id)
        if current is not None:
            out["current"] = round(current, 2)
            out["remaining"] = round(current - milestone.target_value, 2)
    elif (
        milestone.domain == Domain.BODY_COMPOSITION.value
        and milestone.target_value is not None
        and unit_ok
    ):
        current = await _current_body_fat(session, subject_id=subject_id)
        if current is not None:
            out["current"] = round(current, 2)
            out["remaining"] = round(current - milestone.target_value, 2)
    return out


async def dashboard_cards(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> list[dict]:
    """All goals with progress computed — the dashboard widget / reports list."""
    rows = await list_milestones(session, subject_id=subject_id)
    return [await progress(session, milestone, subject_id=subject_id) for milestone in rows]
