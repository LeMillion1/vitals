"""Prepared, subject-scoped Milestone commands."""

from __future__ import annotations

from datetime import date as date_type
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, MilestoneStatus
from vitals.models.milestones import Milestone
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.milestones.governance import (
    _lock_milestone_for_write,
    _require_prepared_write,
)

_UNSET: object = object()


async def create_milestone(
    session: AsyncSession,
    *,
    name: str,
    domain: str = Domain.WEIGHT.value,
    target_value: Optional[float] = None,
    target_unit: Optional[str] = None,
    deadline: Optional[date_type] = None,
    note: Optional[str] = None,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Milestone:
    _require_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = Milestone(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        name=name,
        domain=domain,
        target_value=target_value,
        target_unit=target_unit,
        deadline=deadline,
        status=MilestoneStatus.ACTIVE.value,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


async def set_status(
    session: AsyncSession,
    milestone_id: int,
    status: str,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[Milestone]:
    row = await _lock_milestone_for_write(
        session,
        milestone_id,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if row is None:
        return None
    row.status = status
    await session.flush()
    return row


async def update_milestone(
    session: AsyncSession,
    milestone_id: int,
    *,
    name: object = _UNSET,
    domain: object = _UNSET,
    target_value: object = _UNSET,
    target_unit: object = _UNSET,
    deadline: object = _UNSET,
    status: object = _UNSET,
    note: object = _UNSET,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[Milestone]:
    """Partial-update a goal card. Only fields explicitly passed are changed;
    the rest keep their current value (pass ``None`` to clear an optional field)."""
    row = await _lock_milestone_for_write(
        session,
        milestone_id,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if row is None:
        return None
    for attr, value in (
        ("name", name),
        ("domain", domain),
        ("target_value", target_value),
        ("target_unit", target_unit),
        ("deadline", deadline),
        ("status", status),
        ("note", note),
    ):
        if value is not _UNSET:
            setattr(row, attr, value)
    await session.flush()
    return row


async def delete_milestone(
    session: AsyncSession,
    milestone_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> bool:
    row = await _lock_milestone_for_write(
        session,
        milestone_id,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
