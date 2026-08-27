
"""Subject-scoped Nutrition record queries."""
from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.nutrition import MealLog
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.nutrition.governance import (
    _get_meal_for_update,
    _meal_subject_scope,
    _require_scoped_prepared_write,
)


async def get_meal_for_update(
    session: AsyncSession,
    meal_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[MealLog]:
    """Lock and refresh a scoped meal for a caller-side partial merge."""

    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    return await _get_meal_for_update(
        session,
        meal_id,
        subject_id=identity.subject_id,
    )

async def list_meals_for_date(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
) -> Sequence[MealLog]:
    """List one day's meals inside an exact subject boundary."""
    stmt = select(MealLog).where(MealLog.date == on_date)
    stmt = stmt.where(_meal_subject_scope(subject_id))
    result = await session.execute(
        stmt.order_by(MealLog.eaten_at.asc().nulls_last(), MealLog.id)
        .execution_options(populate_existing=True)
    )
    return result.scalars().all()

async def list_meals(
    session: AsyncSession,
    *,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    subject_id: uuid.UUID,
    name_query: str | None = None,
    has_note: bool = False,
    limit: int | None = None,
) -> Sequence[MealLog]:
    """List meals inside one exact subject scope."""
    stmt = select(MealLog)
    stmt = stmt.where(_meal_subject_scope(subject_id))
    if start is not None:
        stmt = stmt.where(MealLog.date >= start)
    if end is not None:
        stmt = stmt.where(MealLog.date <= end)
    if name_query:
        stmt = stmt.where(MealLog.name.ilike(f"%{name_query}%"))
    if has_note:
        stmt = stmt.where(MealLog.note.isnot(None), MealLog.note != "")
    stmt = stmt.order_by(
        MealLog.date.desc(),
        MealLog.eaten_at.asc().nulls_last(),
        MealLog.id,
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()
