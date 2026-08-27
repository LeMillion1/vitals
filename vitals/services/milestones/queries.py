"""Subject-scoped Milestone queries."""

from __future__ import annotations

import uuid
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.milestones import Milestone
from vitals.services.milestones.governance import _reject_partial_legacy_rows, _subject_scope


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
