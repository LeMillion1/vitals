
"""Subject-scoped Skincare record queries."""
from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.skincare import SkincareLog, SkincareObservation, SkincareProduct
from vitals.services.skincare.governance import _get_log, _subject_scope


async def get_log(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
) -> Optional[SkincareLog]:
    return await _get_log(
        session,
        on_date,
        subject_id=subject_id,
        for_update=False,
    )

async def list_logs(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: date_type | None = None,
    end: date_type | None = None,
    has_note: bool = False,
    limit: int | None = None,
) -> Sequence[SkincareLog]:
    stmt = select(SkincareLog).where(_subject_scope(SkincareLog, subject_id))
    if start is not None:
        stmt = stmt.where(SkincareLog.date >= start)
    if end is not None:
        stmt = stmt.where(SkincareLog.date <= end)
    if has_note:
        stmt = stmt.where(SkincareLog.note.is_not(None), SkincareLog.note != "")
    stmt = stmt.order_by(SkincareLog.date.desc(), SkincareLog.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()

async def list_observations(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: date_type | None = None,
    end: date_type | None = None,
    limit: int | None = None,
) -> Sequence[SkincareObservation]:
    stmt = select(SkincareObservation).where(_subject_scope(SkincareObservation, subject_id))
    if start is not None:
        stmt = stmt.where(SkincareObservation.date >= start)
    if end is not None:
        stmt = stmt.where(SkincareObservation.date <= end)
    stmt = stmt.order_by(
        SkincareObservation.date.desc(),
        SkincareObservation.id.desc(),
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()

async def list_products(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    active_only: bool = False,
    limit: int | None = None,
) -> Sequence[SkincareProduct]:
    stmt = select(SkincareProduct).where(_subject_scope(SkincareProduct, subject_id))
    if active_only:
        stmt = stmt.where(SkincareProduct.active.is_(True))
    stmt = stmt.order_by(SkincareProduct.active.desc(), SkincareProduct.name)
    if limit is not None:
        if limit < 1:
            raise ValueError("skincare product limit must be positive")
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()
