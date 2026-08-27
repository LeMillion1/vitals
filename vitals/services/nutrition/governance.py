
"""Conflict and row-locking primitives shared by Nutrition leaves."""
from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.nutrition import MealLog
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine

_DAY_ENTITY_PREFIX = "nutrition-day"


def _day_entity_key(on_date: date_type) -> str:
    return f"{_DAY_ENTITY_PREFIX}:{on_date.isoformat()}"

def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: engine.PreparedConflictWrite,
) -> engine.ConflictWriteContext:
    """Prove the write names a subject and the decision that authorized it."""

    if identity is None or prepared is None:
        raise engine.ConflictPreparedWriteError(
            "scoped nutrition writes require identity and a prepared conflict write"
        )
    return engine.require_prepared_identity(
        session,
        prepared=prepared,
        identity=identity,
    )

def _require_evaluation_date(
    context: engine.ConflictWriteContext,
    on_date: date_type,
) -> None:
    if context.evaluation_date != on_date:
        raise engine.ConflictPreparedWriteError(
            "nutrition write date does not match prepared conflict evaluation date"
        )

def _meal_subject_scope(subject_id: uuid.UUID):
    return MealLog.subject_id == subject_id

def _meal_by_id_stmt(meal_id: int, *, subject_id: uuid.UUID):
    return select(MealLog).where(
        MealLog.id == meal_id,
        _meal_subject_scope(subject_id),
    )

async def _get_meal_for_update(
    session: AsyncSession,
    meal_id: int,
    *,
    subject_id: uuid.UUID,
) -> Optional[MealLog]:
    return await session.scalar(
        _meal_by_id_stmt(meal_id, subject_id=subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
