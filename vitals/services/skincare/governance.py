
"""Conflict and row-locking primitives shared by Skincare leaves."""
from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.skincare import SkincareLog
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine

FLAGS = (
    "retinoid", "azelaic", "peel", "niacinamide_spf", "moisturizer",
    "vitamin_c", "benzoyl_peroxide",
)
_DAY_ENTITY_PREFIX = "skincare-day"


def _day_entity_key(on_date: date_type) -> str:
    return f"{_DAY_ENTITY_PREFIX}:{on_date.isoformat()}"

def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: engine.PreparedConflictWrite,
) -> engine.ConflictWriteContext:
    """Bind one skincare write to its subject and its conflict decision."""

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
            "skincare write date does not match prepared conflict evaluation date"
        )

def _subject_scope(model, subject_id: uuid.UUID):
    """A routine and the skin it is written about belong to one person."""

    return model.subject_id == subject_id

async def _get_log(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
    for_update: bool,
) -> Optional[SkincareLog]:
    stmt = (
        select(SkincareLog)
        .where(SkincareLog.date == on_date)
        .where(_subject_scope(SkincareLog, subject_id))
    )
    stmt = stmt.order_by(SkincareLog.id).limit(2)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    rows = list(await session.scalars(stmt))
    if len(rows) > 1:
        raise engine.ConflictScopeError(
            "multiple skincare logs match one subject and date"
        )
    return rows[0] if rows else None

async def _get_owned_row_for_update(
    session: AsyncSession,
    model,
    row_id: int,
    *,
    subject_id: uuid.UUID,
):
    stmt = (
        select(model)
        .where(model.id == row_id)
        .where(_subject_scope(model, subject_id))
    )
    return await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )
