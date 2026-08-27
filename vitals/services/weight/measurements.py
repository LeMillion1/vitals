"""Body-measurement persistence and Navy-derived composition for Weight."""
from __future__ import annotations

import math
import uuid
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.analytics.navy import lean_body_mass_kg, navy_body_fat_pct
from vitals.enums import Domain, Source
from vitals.models.weight import DOMAIN, BodyMeasurement
from vitals.ownership import WriteIdentity
from vitals.services import health_profile_service
from vitals.services.conflicts import engine

from .contracts import BodyMeasurementDateOccupiedError, WeightOwnershipError
from .governance import (
    require_aux_prepared_write as _require_aux_prepared_write,
    require_evaluation_date as _require_evaluation_date,
)
from .logs import get_active_weight

_CIRCUMFERENCE_CM_RANGE = (10.0, 300.0)


def _check_range(
    name: str,
    value: Optional[float],
    bounds: tuple[float, float],
) -> Optional[float]:
    """Reject a non-finite or out-of-range number; allow omitted fields."""
    if value is None:
        return None
    low, high = bounds
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(
            f"{name} must be between {low:g} and {high:g} (got {value!r})"
        )
    return value


async def _body_config(
    session: AsyncSession, *, subject_id: uuid.UUID
) -> tuple[Optional[float], Optional[str]]:
    """Return the selected subject's height and sex for the Navy formula.

    An unfilled profile returns ``(None, None)`` so callers skip the estimate
    rather than substituting another person's or process-wide defaults.
    """

    profile = await health_profile_service.get_profile(
        session, subject_id=subject_id
    )
    if not profile.describes_a_body:
        return None, None
    return profile.height_cm, profile.sex


def _body_measurement_scope_condition(
    *,
    subject_id: uuid.UUID,
):
    from vitals.models.identity import HealthSubject

    owner_user_id = (
        select(HealthSubject.owner_user_id)
        .where(HealthSubject.id == subject_id)
        .scalar_subquery()
    )
    exact = and_(
        BodyMeasurement.domain == DOMAIN,
        BodyMeasurement.subject_id == subject_id,
        or_(
            BodyMeasurement.actor_user_id.is_(None),
            BodyMeasurement.actor_user_id == owner_user_id,
        ),
    )
    return exact


async def _assert_body_measurement_scope_integrity(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    filters: Sequence = (),
) -> None:
    valid = _body_measurement_scope_condition(
        subject_id=subject_id,
    )
    invalid = await session.scalar(
        select(BodyMeasurement.id)
        .where(
            # A row that names nobody is outside this scope entirely.
            BodyMeasurement.subject_id == subject_id,
            *filters,
            valid.is_not(True),
        )
        .limit(1)
    )
    if invalid is not None:
        raise WeightOwnershipError(
            "body measurement has partial or conflicting ownership provenance"
        )


async def _get_body_measurement_for_update(
    session: AsyncSession,
    measurement_id: int,
    *,
    subject_id: uuid.UUID,
) -> BodyMeasurement | None:
    stmt = select(BodyMeasurement).where(BodyMeasurement.id == measurement_id)
    await _assert_body_measurement_scope_integrity(
        session,
        subject_id=subject_id,
        filters=(BodyMeasurement.id == measurement_id,),
    )
    stmt = stmt.where(
        _body_measurement_scope_condition(
            subject_id=subject_id,
        )
    )
    return await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )


async def _get_body_measurement_for_date_update(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
) -> BodyMeasurement | None:
    stmt = select(BodyMeasurement).where(BodyMeasurement.date == on_date)
    await _assert_body_measurement_scope_integrity(
        session,
        subject_id=subject_id,
        filters=(BodyMeasurement.date == on_date,),
    )
    stmt = stmt.where(
        _body_measurement_scope_condition(
            subject_id=subject_id,
        )
    )
    return await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )


def _require_aux_source(source: str | Source) -> str:
    value = source.value if isinstance(source, Source) else source
    if value not in {Source.MANUAL.value, Source.MCP.value}:
        raise ValueError("body measurement/noise source must be manual or mcp")
    return value


def _effective_measurement_values(
    row: BodyMeasurement | None,
    *,
    neck_cm: Optional[float],
    waist_cm: Optional[float],
    hips_cm: Optional[float],
    note: Optional[str],
    partial: bool,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    if not partial or row is None:
        return neck_cm, waist_cm, hips_cm, note
    return (
        neck_cm if neck_cm is not None else row.neck_cm,
        waist_cm if waist_cm is not None else row.waist_cm,
        hips_cm if hips_cm is not None else row.hips_cm,
        note if note is not None else row.note,
    )


async def _apply_body_measurement_values(
    session: AsyncSession,
    row: BodyMeasurement,
    *,
    on_date: date_type,
    neck_cm: Optional[float],
    waist_cm: Optional[float],
    hips_cm: Optional[float],
    note: Optional[str],
    subject_id: uuid.UUID,
) -> None:
    height_cm, sex = await _body_config(session, subject_id=subject_id)
    body_fat_pct = None
    if neck_cm and waist_cm and height_cm is not None and sex is not None:
        try:
            body_fat_pct = navy_body_fat_pct(
                waist_cm=waist_cm,
                neck_cm=neck_cm,
                height_cm=height_cm,
                sex=sex,
                hips_cm=hips_cm,
            )
        except ValueError:
            body_fat_pct = None

    lbm_kg = None
    if body_fat_pct is not None:
        active = await get_active_weight(
            session,
            on_date,
            subject_id=subject_id,
        )
        if active is not None:
            lbm_kg = lean_body_mass_kg(active.weight_kg, body_fat_pct)

    row.date = on_date
    row.neck_cm = neck_cm
    row.waist_cm = waist_cm
    row.hips_cm = hips_cm
    row.body_fat_pct = body_fat_pct
    row.lbm_kg = lbm_kg
    row.note = note


async def _enforce_body_measurement_write(
    session: AsyncSession,
    *,
    context: engine.ConflictWriteContext | None,
    prepared_conflict_write: engine.PreparedConflictWrite | None,
    on_date: date_type,
    override: bool,
) -> None:
    proposed = {"measurement": True}
    assert prepared_conflict_write is not None
    await engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.WEIGHT,
        proposed_state=proposed,
        override=override,
        entity_ref=f"body_measurement:{on_date.isoformat()}",
    )


async def upsert_body_measurement(
    session: AsyncSession,
    *,
    on_date: date_type,
    neck_cm: Optional[float] = None,
    waist_cm: Optional[float] = None,
    hips_cm: Optional[float] = None,
    note: Optional[str] = None,
    source: str | Source = Source.MANUAL.value,
    override: bool = False,
    partial: bool = True,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> BodyMeasurement:
    """Create/update the day's measurement and (re)derive body-fat % + LBM."""
    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    _require_evaluation_date(context, on_date)
    source_value = _require_aux_source(source)
    _check_range("neck_cm", neck_cm, _CIRCUMFERENCE_CM_RANGE)
    _check_range("waist_cm", waist_cm, _CIRCUMFERENCE_CM_RANGE)
    _check_range("hips_cm", hips_cm, _CIRCUMFERENCE_CM_RANGE)
    row = await _get_body_measurement_for_date_update(
        session,
        on_date,
        subject_id=identity.subject_id,
    )
    effective_neck, effective_waist, effective_hips, effective_note = (
        _effective_measurement_values(
            row,
            neck_cm=neck_cm,
            waist_cm=waist_cm,
            hips_cm=hips_cm,
            note=note,
            partial=partial,
        )
    )
    await _enforce_body_measurement_write(
        session,
        context=context,
        prepared_conflict_write=prepared_conflict_write,
        on_date=on_date,
        override=override,
    )

    if row is None:
        row = BodyMeasurement(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            date=on_date,
            domain=DOMAIN,
            source=source_value,
        )
        session.add(row)

    await _apply_body_measurement_values(
        session,
        row,
        on_date=on_date,
        neck_cm=effective_neck,
        waist_cm=effective_waist,
        hips_cm=effective_hips,
        note=effective_note,
        subject_id=identity.subject_id,
    )
    await session.flush()
    return row


async def _recompute_lbm_for_date(
    session: AsyncSession,
    on_date: date_type,
    weight_kg: float,
    *,
    subject_id: uuid.UUID,
) -> None:
    """Refresh a measurement's LBM after the day's active weight changes."""
    stmt = select(BodyMeasurement).where(BodyMeasurement.date == on_date)
    scope = _body_measurement_scope_condition(
        subject_id=subject_id,
    )
    invalid = await session.scalar(
        select(BodyMeasurement.id)
        .where(
            BodyMeasurement.date == on_date,
            # A row that names nobody is outside this scope entirely.
            BodyMeasurement.subject_id == subject_id,
            scope.is_not(True),
        )
        .limit(1)
    )
    if invalid is not None:
        raise WeightOwnershipError(
            "body measurement has partial ownership provenance"
        )
    stmt = stmt.where(scope)
    result = await session.execute(
        stmt.with_for_update().execution_options(populate_existing=True)
    )
    row = result.scalar_one_or_none()
    if row is not None and row.body_fat_pct is not None:
        row.lbm_kg = lean_body_mass_kg(weight_kg, row.body_fat_pct)
        await session.flush()


async def _recompute_lbm_for_date_null(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
) -> None:
    """Clear LBM for a date because no active weight log remains."""
    stmt = select(BodyMeasurement).where(BodyMeasurement.date == on_date)
    scope = _body_measurement_scope_condition(
        subject_id=subject_id,
    )
    invalid = await session.scalar(
        select(BodyMeasurement.id)
        .where(
            BodyMeasurement.date == on_date,
            # A row that names nobody is outside this scope entirely.
            BodyMeasurement.subject_id == subject_id,
            scope.is_not(True),
        )
        .limit(1)
    )
    if invalid is not None:
        raise WeightOwnershipError(
            "body measurement has partial ownership provenance"
        )
    stmt = stmt.where(scope)
    result = await session.execute(
        stmt.with_for_update().execution_options(populate_existing=True)
    )
    row = result.scalar_one_or_none()
    if row is not None:
        row.lbm_kg = None
        await session.flush()


async def list_body_measurements(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: date_type | None = None,
    end: date_type | None = None,
    has_note: bool = False,
    limit: int | None = None,
) -> Sequence[BodyMeasurement]:
    filters = []
    if start is not None:
        filters.append(BodyMeasurement.date >= start)
    if end is not None:
        filters.append(BodyMeasurement.date <= end)
    if has_note:
        filters.extend((BodyMeasurement.note.is_not(None), BodyMeasurement.note != ""))
    stmt = select(BodyMeasurement).where(*filters)
    await _assert_body_measurement_scope_integrity(
        session,
        subject_id=subject_id,
        filters=tuple(filters),
    )
    stmt = stmt.where(
        _body_measurement_scope_condition(
            subject_id=subject_id,
        )
    )
    stmt = stmt.order_by(BodyMeasurement.date)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def delete_body_measurement(
    session: AsyncSession,
    measurement_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> bool:
    """Delete a body measurement record by ID."""
    _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_body_measurement_for_update(
        session,
        measurement_id,
        subject_id=identity.subject_id,
    )
    if not row:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def update_body_measurement(
    session: AsyncSession,
    measurement_id: int,
    *,
    on_date: date_type,
    neck_cm: Optional[float] = None,
    waist_cm: Optional[float] = None,
    hips_cm: Optional[float] = None,
    note: Optional[str] = None,
    override: bool = False,
    partial: bool = True,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[BodyMeasurement]:
    """Edit an existing body measurement, optionally moving its date."""
    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    _require_evaluation_date(context, on_date)
    _check_range("neck_cm", neck_cm, _CIRCUMFERENCE_CM_RANGE)
    _check_range("waist_cm", waist_cm, _CIRCUMFERENCE_CM_RANGE)
    _check_range("hips_cm", hips_cm, _CIRCUMFERENCE_CM_RANGE)

    row = await _get_body_measurement_for_update(
        session,
        measurement_id,
        subject_id=identity.subject_id,
    )
    if not row:
        return None

    if row.date != on_date:
        occupied = await _get_body_measurement_for_date_update(
            session,
            on_date,
            subject_id=identity.subject_id,
        )
        if occupied is not None and occupied.id != row.id:
            raise BodyMeasurementDateOccupiedError(
                "body-measurement destination date already has a row"
            )

    effective_neck, effective_waist, effective_hips, effective_note = (
        _effective_measurement_values(
            row,
            neck_cm=neck_cm,
            waist_cm=waist_cm,
            hips_cm=hips_cm,
            note=note,
            partial=partial,
        )
    )
    await _enforce_body_measurement_write(
        session,
        context=context,
        prepared_conflict_write=prepared_conflict_write,
        on_date=on_date,
        override=override,
    )
    if row.subject_id is None and identity is not None:
        row.subject_id = identity.subject_id
    await _apply_body_measurement_values(
        session,
        on_date=on_date,
        row=row,
        neck_cm=effective_neck,
        waist_cm=effective_waist,
        hips_cm=effective_hips,
        note=effective_note,
        subject_id=identity.subject_id,
    )
    await session.flush()
    return row


async def update_body_measurement_note(
    session: AsyncSession,
    measurement_id: int,
    *,
    note: str,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> BodyMeasurement | None:
    """Update only a measurement note inside one prepared subject scope."""

    _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_body_measurement_for_update(
        session,
        measurement_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return None
    if row.subject_id is None:
        row.subject_id = identity.subject_id
    row.note = note
    await session.flush()
    return row
