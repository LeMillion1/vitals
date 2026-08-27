"""Bounded read projections over Weight logs and noise markers."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date as date_type
from typing import Literal, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.weight import DOMAIN, NoiseMarker, WeightLog
from vitals.services.conflicts import engine
from vitals.utils.timeutils import today_local

from .logs import (
    _assert_weight_scope_integrity,
    _validate_persisted_weight_provenance,
    _weight_entity_key,
    _weight_scope_condition,
    get_active_weight,
)
from .noise import (
    _assert_noise_marker_scope_integrity,
    _noise_marker_scope_condition,
)


@dataclass(frozen=True, slots=True)
class WeightProjectionPoint:
    """The only Weight columns disclosed to clinical summary analytics."""

    date: date_type
    weight_kg: float


@dataclass(frozen=True, slots=True)
class WeightProjectionNoiseRange:
    """The only noise-marker columns needed by clinical summary analytics."""

    start_date: date_type
    end_date: date_type | None


@dataclass(frozen=True, slots=True)
class BoundedWeightProjection:
    """Bounded, metadata-only Weight history for an authorized projection."""

    rows: tuple[WeightProjectionPoint, ...]
    noise_markers: tuple[WeightProjectionNoiseRange, ...]
    history_truncated: bool
    noise_truncated: bool


_ProjectionScope = Literal["care", "emergency"]


async def _bounded_weight_projection(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    end: date_type,
    history_limit: int,
    noise_limit: int,
    scope: _ProjectionScope,
) -> BoundedWeightProjection:
    """Execute one of the two reviewed clinical read policies.

    Care summaries validate exact owner/provenance scope and only need noise
    ranges that overlap the returned history.  Emergency summaries deliberately
    read normalized, subject-owned columns only and retain their older contract:
    the most recent ranges through ``end`` count toward the cap even when they
    predate the bounded Weight history.  Keeping that distinction here prevents
    either audience from silently inheriting the other's disclosure semantics.
    """

    if not 1 <= history_limit <= 1000:
        raise ValueError("clinical Weight history_limit must be between 1 and 1000")
    if not 1 <= noise_limit <= 500:
        raise ValueError("clinical Weight noise_limit must be between 1 and 500")

    filters = (
        WeightLog.superseded.is_(False),
        WeightLog.domain == DOMAIN,
        WeightLog.date <= end,
    )
    if scope == "care":
        await _assert_weight_scope_integrity(
            session,
            subject_id=subject_id,
            evaluation_date=end,
            filters=filters,
        )
        weight_scope = _weight_scope_condition(
            subject_id=subject_id,
            evaluation_date=end,
        )
    else:
        weight_scope = WeightLog.subject_id == subject_id
    newest = list(
        (
            await session.execute(
                select(WeightLog.date, WeightLog.weight_kg)
                .where(weight_scope, *filters)
                .order_by(WeightLog.date.desc(), WeightLog.id.desc())
                .limit(history_limit + 1)
            )
        ).all()
    )
    history_truncated = len(newest) > history_limit
    rows = tuple(
        WeightProjectionPoint(date=row.date, weight_kg=row.weight_kg)
        for row in reversed(newest[:history_limit])
    )

    marker_filters = [NoiseMarker.start_date <= end]
    if scope == "care":
        marker_start = rows[0].date if rows else end
        marker_filters.insert(
            0,
            or_(NoiseMarker.end_date.is_(None), NoiseMarker.end_date >= marker_start),
        )
        await _assert_noise_marker_scope_integrity(
            session,
            subject_id=subject_id,
            filters=tuple(marker_filters),
        )
        marker_scope = _noise_marker_scope_condition(subject_id=subject_id)
        marker_order = (NoiseMarker.start_date, NoiseMarker.id)
    else:
        marker_scope = NoiseMarker.subject_id == subject_id
        marker_order = (NoiseMarker.start_date.desc(), NoiseMarker.id.desc())
    marker_rows = list(
        (
            await session.execute(
                select(NoiseMarker.start_date, NoiseMarker.end_date)
                .where(
                    NoiseMarker.domain == DOMAIN,
                    marker_scope,
                    *marker_filters,
                )
                .order_by(*marker_order)
                .limit(noise_limit + 1)
            )
        ).all()
    )
    noise_truncated = len(marker_rows) > noise_limit
    return BoundedWeightProjection(
        rows=rows,
        noise_markers=tuple(
            WeightProjectionNoiseRange(
                start_date=row.start_date,
                end_date=row.end_date,
            )
            for row in marker_rows[:noise_limit]
        ),
        history_truncated=history_truncated,
        noise_truncated=noise_truncated,
    )


async def care_weight_history(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    end: date_type,
    history_limit: int = 400,
    noise_limit: int = 100,
) -> BoundedWeightProjection:
    """Return recent active weights without reading linked raw payload bytes."""

    if not 1 <= history_limit <= 1000:
        raise ValueError("care Weight history_limit must be between 1 and 1000")
    if not 1 <= noise_limit <= 500:
        raise ValueError("care Weight noise_limit must be between 1 and 500")
    return await _bounded_weight_projection(
        session,
        subject_id=subject_id,
        end=end,
        history_limit=history_limit,
        noise_limit=noise_limit,
        scope="care",
    )


async def emergency_weight_history(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    end: date_type,
    history_limit: int = 400,
    noise_limit: int = 100,
) -> BoundedWeightProjection:
    """Return the exact column-minimal Weight slice reviewed for break-glass."""

    return await _bounded_weight_projection(
        session,
        subject_id=subject_id,
        end=end,
        history_limit=history_limit,
        noise_limit=noise_limit,
        scope="emergency",
    )


async def list_weight_notes(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: date_type | None = None,
    end: date_type | None = None,
    limit: int = 50,
) -> Sequence[WeightLog]:
    """Return scoped Weight rows carrying notes, including superseded history."""

    filters = [WeightLog.note.is_not(None), WeightLog.note != ""]
    if start is not None:
        filters.append(WeightLog.date >= start)
    if end is not None:
        filters.append(WeightLog.date <= end)
    scope = _weight_scope_condition(
        subject_id=subject_id,
        evaluation_date=end or start or today_local(),
    )
    await _assert_weight_scope_integrity(
        session,
        subject_id=subject_id,
        evaluation_date=end or start or today_local(),
        filters=tuple(filters),
    )
    rows = tuple(
        await session.scalars(
            select(WeightLog)
            .where(*filters, scope)
            .order_by(WeightLog.date.desc(), WeightLog.id.desc())
            .limit(limit)
        )
    )
    for row in rows:
        await _validate_persisted_weight_provenance(
            session,
            row,
            subject_id=subject_id,
        )
    return rows


async def resolve_active_scoped(
    session: AsyncSession,
    *,
    scope: engine.ConflictScope,
) -> list[dict]:
    """Return the selected subject's active weight on the evaluation date."""

    row = await get_active_weight(
        session,
        scope.evaluation_date,
        subject_id=scope.subject_id,
    )
    if row is None:
        return []
    return [
        {
            engine.CONFLICT_ENTITY_KEY: _weight_entity_key(row),
            "weight_kg": row.weight_kg,
            "source": row.source,
        }
    ]
