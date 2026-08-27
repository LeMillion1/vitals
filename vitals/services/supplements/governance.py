"""Prepared-write and row-locking primitives for Supplements."""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.supplements import Supplement
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine


def _proposed(key: str, active: bool, timing_slot: Optional[str] = None) -> dict:
    return {"key": key, "active": active, "timing_slot": timing_slot}


def _supplement_subject_scope(subject_id: uuid.UUID):
    """Restrict a supplement query to one person's regimen."""

    return Supplement.subject_id == subject_id


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: engine.PreparedConflictWrite,
) -> engine.ConflictWriteContext:
    """Bind one supplement write to its subject and its conflict decision."""

    return engine.require_prepared_identity(
        session,
        prepared=prepared,
        identity=identity,
    )


def _supplement_by_id_stmt(supplement_id: int, *, subject_id: uuid.UUID):
    return (
        select(Supplement)
        .where(Supplement.id == supplement_id)
        .where(_supplement_subject_scope(subject_id))
    )


async def _get_supplement_for_update(
    session: AsyncSession,
    supplement_id: int,
    *,
    subject_id: uuid.UUID,
) -> Optional[Supplement]:
    return await session.scalar(
        _supplement_by_id_stmt(supplement_id, subject_id=subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
