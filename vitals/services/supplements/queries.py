"""Subject-scoped Supplement catalog queries."""

from __future__ import annotations

import uuid
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.supplements import Supplement
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.supplements.governance import (
    _get_supplement_for_update,
    _require_scoped_prepared_write,
    _supplement_by_id_stmt,
    _supplement_subject_scope,
)


async def get_supplement_for_update(
    session: AsyncSession,
    supplement_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[Supplement]:
    """Lock and refresh one scoped row for a caller-side partial update merge."""

    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    return await _get_supplement_for_update(
        session,
        supplement_id,
        subject_id=identity.subject_id,
    )


async def list_supplements(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    active_only: bool = False,
    limit: int | None = None,
) -> Sequence[Supplement]:
    """Return one person's regimen. A regimen without a person is not a thing."""

    stmt = select(Supplement).where(_supplement_subject_scope(subject_id))
    if active_only:
        stmt = stmt.where(Supplement.active.is_(True))
    stmt = stmt.order_by(Supplement.active.desc(), Supplement.name)
    if limit is not None:
        if limit < 1:
            raise ValueError("supplement limit must be positive")
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_supplement(
    session: AsyncSession,
    supplement_id: int,
    *,
    subject_id: uuid.UUID,
) -> Optional[Supplement]:
    return await session.scalar(_supplement_by_id_stmt(supplement_id, subject_id=subject_id))
