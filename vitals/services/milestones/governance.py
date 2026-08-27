"""Ownership and prepared-write primitives for Milestones."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.milestones import Milestone
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine


class MilestoneOwnershipError(ValueError):
    """A goal card is outside, or corrupt within, the selected subject scope."""


def _subject_scope(subject_id: uuid.UUID):
    """A goal belongs to the person who set it."""

    return Milestone.subject_id == subject_id


async def _reject_partial_legacy_rows(session: AsyncSession) -> None:
    partial_id = await session.scalar(
        select(Milestone.id)
        .where(
            Milestone.subject_id.is_(None),
            Milestone.actor_user_id.is_not(None),
        )
        .limit(1)
    )
    if partial_id is not None:
        raise MilestoneOwnershipError("milestone has partial legacy ownership roots")


def _require_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: engine.PreparedConflictWrite,
) -> engine.ConflictWriteContext:
    """Bind one goal write to its subject and its conflict decision."""

    context = engine.require_prepared_identity(
        session,
        identity=identity,
        prepared=prepared,
    )
    if identity.actor_user_id is None:
        raise engine.ConflictPreparedWriteError("milestone writes require a human actor")
    return context


async def _lock_milestone_for_write(
    session: AsyncSession,
    milestone_id: int,
    *,
    identity: WriteIdentity,
    prepared: engine.PreparedConflictWrite,
) -> Milestone | None:
    _require_prepared_write(session, identity=identity, prepared=prepared)
    roots = (
        await session.execute(
            select(Milestone.subject_id, Milestone.actor_user_id).where(
                Milestone.id == milestone_id
            )
        )
    ).one_or_none()
    if roots is None:
        return None
    row_subject_id, row_actor_id = roots
    if row_subject_id is None and row_actor_id is not None:
        raise MilestoneOwnershipError("milestone has partial legacy ownership roots")
    if row_subject_id != identity.subject_id:
        return None

    row = await session.scalar(
        select(Milestone)
        .where(Milestone.id == milestone_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.subject_id != identity.subject_id:
        return None
    return row
