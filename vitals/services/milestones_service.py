"""Milestones (goal cards) service — module 10.

Goal cards are simple config rows (name, related domain, optional numeric target +
deadline, status). For a weight-domain goal with a numeric target we compute live
progress against the latest active weight; other domains just carry status. The
product is a navigator, so nothing here is enforced — a goal is context for the
weekly digest and a dashboard card.
"""
from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, MilestoneStatus
from vitals.models.milestones import Milestone
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.utils.timeutils import today_local


class MilestoneOwnershipError(ValueError):
    """A goal card is outside, or corrupt within, the selected subject scope."""


def _subject_scope(subject_id: uuid.UUID):
    """A goal belongs to the person who set it."""

    return Milestone.subject_id == subject_id


async def _reject_partial_legacy_rows(session: AsyncSession) -> None:
    partial_id = await session.scalar(
        select(Milestone.id)
        .where(
            Milestone.subject_id.is_(None),
            Milestone.actor_user_id.is_not(None),
        )
        .limit(1)
    )
    if partial_id is not None:
        raise MilestoneOwnershipError(
            "milestone has partial legacy ownership roots"
        )


def _require_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: engine.PreparedConflictWrite,
) -> engine.ConflictWriteContext:
    """Bind one goal write to its subject and its conflict decision."""

    context = engine.require_prepared_identity(
        session,
        identity=identity,
        prepared=prepared,
    )
    if identity.actor_user_id is None:
        raise engine.ConflictPreparedWriteError(
            "milestone writes require a human actor"
        )
    return context


async def _lock_milestone_for_write(
    session: AsyncSession,
    milestone_id: int,
    *,
    identity: WriteIdentity,
    prepared: engine.PreparedConflictWrite,
) -> Milestone | None:
    _require_prepared_write(session, identity=identity, prepared=prepared)
    roots = (
        await session.execute(
            select(Milestone.subject_id, Milestone.actor_user_id).where(
                Milestone.id == milestone_id
            )
        )
    ).one_or_none()
    if roots is None:
        return None
    row_subject_id, row_actor_id = roots
    if row_subject_id is None and row_actor_id is not None:
        raise MilestoneOwnershipError(
            "milestone has partial legacy ownership roots"
        )
    if row_subject_id != identity.subject_id:
        return None

    row = await session.scalar(
        select(Milestone)
        .where(Milestone.id == milestone_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.subject_id != identity.subject_id:
        return None
    return row


async def create_milestone(
    session: AsyncSession,
    *,
    name: str,
    domain: str = Domain.WEIGHT.value,
    target_value: Optional[float] = None,
    target_unit: Optional[str] = None,
    deadline: Optional[date_type] = None,
    note: Optional[str] = None,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Milestone:
    _require_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = Milestone(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        name=name,
        domain=domain,
        target_value=target_value,
        target_unit=target_unit,
        deadline=deadline,
        status=MilestoneStatus.ACTIVE.value,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


async def list_milestones(
    session: AsyncSession,
    *,
    status: Optional[str] = None,
    subject_id: uuid.UUID,
) -> Sequence[Milestone]:
    # A goal with an actor but no subject belongs to no root the scoped read
    # recognises; that is broken provenance, not merely somebody else's row.
    await _reject_partial_legacy_rows(session)
    stmt = select(Milestone).where(_subject_scope(subject_id))
    if status is not None:
        stmt = stmt.where(Milestone.status == status)
    stmt = stmt.order_by(Milestone.deadline.is_(None), Milestone.deadline, Milestone.id)
    result = await session.execute(stmt)
    return result.scalars().all()


async def set_status(
    session: AsyncSession,
    milestone_id: int,
    status: str,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[Milestone]:
    row = await _lock_milestone_for_write(
        session,
        milestone_id,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if row is None:
        return None
    row.status = status
    await session.flush()
    return row


# Sentinel so a caller can patch a single field to ``None`` (e.g. clear a
# deadline) without every other field being wiped — only args that differ from
# ``_UNSET`` are applied.
_UNSET: object = object()


async def update_milestone(
    session: AsyncSession,
    milestone_id: int,
    *,
    name: object = _UNSET,
    domain: object = _UNSET,
    target_value: object = _UNSET,
    target_unit: object = _UNSET,
    deadline: object = _UNSET,
    status: object = _UNSET,
    note: object = _UNSET,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[Milestone]:
    """Partial-update a goal card. Only fields explicitly passed are changed;
    the rest keep their current value (pass ``None`` to clear an optional field)."""
    row = await _lock_milestone_for_write(
        session,
        milestone_id,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if row is None:
        return None
    for attr, value in (
        ("name", name),
        ("domain", domain),
        ("target_value", target_value),
        ("target_unit", target_unit),
        ("deadline", deadline),
        ("status", status),
        ("note", note),
    ):
        if value is not _UNSET:
            setattr(row, attr, value)
    await session.flush()
    return row


async def delete_milestone(
    session: AsyncSession,
    milestone_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> bool:
    row = await _lock_milestone_for_write(
        session,
        milestone_id,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def _current_weight(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> Optional[float]:
    """Latest active weight, imported lazily to avoid a hard module dependency."""
    from vitals.services import weight_service

    weights = await weight_service.list_active_weights(
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
    from vitals.services import weight_service
    from vitals.services.modules_service import get_enabled_modules

    config = load_config()
    source_pref = config.body_fat_source or "latest"

    enabled = await get_enabled_modules(session, subject_id=subject_id)
    body_comp_enabled = enabled.get("body_comp", False)

    # 1. Fetch Navy measurements if not pinned to bia
    navy_val = None
    if source_pref in ("latest", "navy"):
        measurements = await weight_service.list_body_measurements(
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
        from vitals.services.body_scan import scans

        scan_rows = await scans.list_scans(
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


_WEIGHT_UNITS = {"kg", "кг"}
_PERCENT_UNITS = {"%", "pct", "percent", "процент", "проценты"}


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
    elif milestone.domain == Domain.BODY_COMPOSITION.value and milestone.target_value is not None and unit_ok:
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
    return [
        await progress(session, milestone, subject_id=subject_id)
        for milestone in rows
    ]
