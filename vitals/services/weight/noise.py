"""Noise-marker ownership, queries, ranges, and mutations for Weight."""
from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.weight import DOMAIN, NoiseMarker
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine

from .contracts import WeightOwnershipError
from .governance import require_aux_prepared_write as _require_aux_prepared_write


def _require_aux_source(source: str | Source) -> str:
    value = source.value if isinstance(source, Source) else source
    if value not in {Source.MANUAL.value, Source.MCP.value}:
        raise ValueError("body measurement/noise source must be manual or mcp")
    return value


def _noise_marker_scope_condition(
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
        NoiseMarker.domain == DOMAIN,
        NoiseMarker.subject_id == subject_id,
        or_(
            NoiseMarker.actor_user_id.is_(None),
            NoiseMarker.actor_user_id == owner_user_id,
        ),
    )
    return exact


async def _assert_noise_marker_scope_integrity(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    filters: Sequence = (),
) -> None:
    valid = _noise_marker_scope_condition(
        subject_id=subject_id,
    )
    invalid = await session.scalar(
        select(NoiseMarker.id)
        .where(
            # A row that names nobody is outside this scope entirely.
            NoiseMarker.subject_id == subject_id,
            *filters,
            valid.is_not(True),
        )
        .limit(1)
    )
    if invalid is not None:
        raise WeightOwnershipError(
            "noise marker has partial or conflicting ownership provenance"
        )


async def _get_noise_marker_for_update(
    session: AsyncSession,
    marker_id: int,
    *,
    subject_id: uuid.UUID,
) -> NoiseMarker | None:
    stmt = select(NoiseMarker).where(NoiseMarker.id == marker_id)
    await _assert_noise_marker_scope_integrity(
        session,
        subject_id=subject_id,
        filters=(NoiseMarker.id == marker_id,),
    )
    stmt = stmt.where(
        _noise_marker_scope_condition(
            subject_id=subject_id,
        )
    )
    return await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )


async def add_noise_marker(
    session: AsyncSession,
    *,
    start_date: date_type,
    end_date: Optional[date_type] = None,
    reason: str,
    direction: Optional[str] = None,
    source: str | Source = Source.MANUAL.value,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> NoiseMarker:
    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    source_value = _require_aux_source(source)
    reason = reason.strip()
    if not reason:
        raise ValueError("noise marker reason must not be blank")
    if end_date is not None and end_date < start_date:
        raise ValueError("noise marker end_date must not precede start_date")
    if direction not in {None, "up", "down", "neutral"}:
        raise ValueError("noise marker direction must be up, down, neutral, or null")
    marker = NoiseMarker(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=DOMAIN,
        source=source_value,
        start_date=start_date,
        end_date=end_date,
        reason=reason,
        direction=direction,
    )
    session.add(marker)
    await session.flush()

    from .alerts import refresh_noise_alert

    await refresh_noise_alert(
        session,
        on_date=context.evaluation_date,
        identity=identity,
        prepared_conflict_write=prepared_conflict_write,
    )
    return marker


async def list_noise_markers(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: date_type | None = None,
    end: date_type | None = None,
) -> Sequence[NoiseMarker]:
    stmt = select(NoiseMarker).where(NoiseMarker.domain == DOMAIN)
    filters = []
    if start is not None:
        filters.append(
            or_(NoiseMarker.end_date.is_(None), NoiseMarker.end_date >= start)
        )
    if end is not None:
        filters.append(NoiseMarker.start_date <= end)
    await _assert_noise_marker_scope_integrity(
        session,
        subject_id=subject_id,
        filters=tuple(filters),
    )
    stmt = stmt.where(
        _noise_marker_scope_condition(
            subject_id=subject_id,
        )
    )
    if filters:
        stmt = stmt.where(*filters)
    result = await session.execute(stmt.order_by(NoiseMarker.start_date))
    return result.scalars().all()


async def noise_ranges(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: date_type | None = None,
    end: date_type | None = None,
) -> list[tuple[date_type, Optional[date_type]]]:
    markers = await list_noise_markers(
        session,
        subject_id=subject_id,
        start=start,
        end=end,
    )
    return [(marker.start_date, marker.end_date) for marker in markers]


async def delete_noise_marker(
    session: AsyncSession,
    marker_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> bool:
    """Delete a noise marker record by ID."""
    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_noise_marker_for_update(
        session,
        marker_id,
        subject_id=identity.subject_id,
    )
    if not row:
        return False
    await session.delete(row)
    await session.flush()

    from .alerts import refresh_noise_alert

    await refresh_noise_alert(
        session,
        on_date=context.evaluation_date,
        identity=identity,
        prepared_conflict_write=prepared_conflict_write,
    )
    return True
