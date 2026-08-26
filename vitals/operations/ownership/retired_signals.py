"""Attribute the two retired Telegram fact tables before revision 0049.

Revision 0058 removes these tables, but revision 0049 still has to establish the
ownership contract they were part of.  This bounded bridge changes only
``subject_id`` and exists solely for an existing revision-0034 lake crossing
that boundary.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Integer, String, Uuid, column, func, select, table, update
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.services.identity_service import acquire_identity_governance_lock


RETIRED_SIGNAL_OWNERSHIP_BACKFILL_PHASE = "stage3.retired_signals.v1"
RETIRED_SIGNAL_OWNERSHIP_BACKFILL_TABLES = ("signals", "day_context")
DEFAULT_RETIRED_SIGNAL_OWNERSHIP_BACKFILL_BATCH_SIZE = 250
MAX_RETIRED_SIGNAL_OWNERSHIP_BACKFILL_BATCH_SIZE = 1000

_SIGNALS = table(
    "signals",
    column("id", Integer),
    column("domain", String),
    column("source", String),
    column("subject_id", Uuid(as_uuid=True)),
)
_DAY_CONTEXT = table(
    "day_context",
    column("id", Integer),
    column("domain", String),
    column("source", String),
    column("subject_id", Uuid(as_uuid=True)),
)
_TABLES = (_SIGNALS, _DAY_CONTEXT)


class RetiredSignalOwnershipBackfillError(RuntimeError):
    """The retired-table bridge cannot make a safe ownership decision."""


class RetiredSignalOwnershipBackfillValidationError(
    RetiredSignalOwnershipBackfillError, ValueError
):
    """The requested batch bound is invalid."""


class RetiredSignalOwnershipBackfillIdentityError(
    RetiredSignalOwnershipBackfillError
):
    """The database does not have the exact legacy owner graph."""


class RetiredSignalOwnershipBackfillProvenanceError(
    RetiredSignalOwnershipBackfillError
):
    """A retired row has a shape outside the reviewed legacy writers."""


@dataclass(frozen=True, slots=True)
class RetiredSignalOwnershipBackfillResult:
    status: str
    snapshot_rows: int
    scanned_rows: int
    updated_rows: int
    remaining_rows: int
    batch_table: str | None

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    def to_safe_dict(self) -> dict[str, str | int | None]:
        return {
            "phase_key": RETIRED_SIGNAL_OWNERSHIP_BACKFILL_PHASE,
            "status": self.status,
            "tables_total": len(_TABLES),
            "snapshot_rows": self.snapshot_rows,
            "scanned_rows": self.scanned_rows,
            "updated_rows": self.updated_rows,
            "remaining_rows": self.remaining_rows,
            "batch_table": self.batch_table,
        }


def _batch_size(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_RETIRED_SIGNAL_OWNERSHIP_BACKFILL_BATCH_SIZE
    ):
        raise RetiredSignalOwnershipBackfillValidationError(
            "batch_size must be an integer from 1 to "
            f"{MAX_RETIRED_SIGNAL_OWNERSHIP_BACKFILL_BATCH_SIZE}"
        )
    return value


async def _sole_subject(session: AsyncSession) -> uuid.UUID:
    rows = list(
        await session.execute(
            select(HealthSubject.id, User.status)
            .join(User, User.id == HealthSubject.owner_user_id)
            .order_by(HealthSubject.id)
            .limit(2)
        )
    )
    if len(rows) != 1 or rows[0].status != UserStatus.ACTIVE.value:
        raise RetiredSignalOwnershipBackfillIdentityError(
            "retired signal ownership requires exactly one active subject owner"
        )
    return rows[0].id


async def _validate_rows(session: AsyncSession, *, subject_id: uuid.UUID) -> None:
    foreign = 0
    invalid = 0
    for target in _TABLES:
        foreign += int(
            await session.scalar(
                select(func.count())
                .select_from(target)
                .where(
                    target.c.subject_id.is_not(None),
                    target.c.subject_id != subject_id,
                )
            )
            or 0
        )
    invalid += int(
        await session.scalar(
            select(func.count())
            .select_from(_SIGNALS)
            .where(
                (_SIGNALS.c.domain != "signals")
                | (_SIGNALS.c.source != "telegram")
            )
        )
        or 0
    )
    invalid += int(
        await session.scalar(
            select(func.count())
            .select_from(_DAY_CONTEXT)
            .where(
                (_DAY_CONTEXT.c.domain != "signals")
                | (_DAY_CONTEXT.c.source.not_in(("manual", "template")))
            )
        )
        or 0
    )
    if foreign or invalid:
        raise RetiredSignalOwnershipBackfillProvenanceError(
            "retired signal rows are outside the reviewed legacy ownership shape"
        )


async def _counts(session: AsyncSession) -> tuple[int, int]:
    total = 0
    remaining = 0
    for target in _TABLES:
        total += int(
            await session.scalar(select(func.count()).select_from(target)) or 0
        )
        remaining += int(
            await session.scalar(
                select(func.count())
                .select_from(target)
                .where(target.c.subject_id.is_(None))
            )
            or 0
        )
    return total, remaining


async def inspect_retired_signal_ownership(
    session: AsyncSession,
) -> RetiredSignalOwnershipBackfillResult:
    subject_id = await _sole_subject(session)
    await _validate_rows(session, subject_id=subject_id)
    total, remaining = await _counts(session)
    return RetiredSignalOwnershipBackfillResult(
        status="completed" if remaining == 0 else "not_started",
        snapshot_rows=total,
        scanned_rows=total - remaining,
        updated_rows=0,
        remaining_rows=remaining,
        batch_table=None,
    )


async def run_retired_signal_ownership_backfill_batch(
    session: AsyncSession, *, batch_size: int
) -> RetiredSignalOwnershipBackfillResult:
    size = _batch_size(batch_size)
    await acquire_identity_governance_lock(session)
    subject_id = await _sole_subject(session)
    await _validate_rows(session, subject_id=subject_id)
    batch_table = None
    updated_rows = 0
    for target in _TABLES:
        ids = list(
            await session.scalars(
                select(target.c.id)
                .where(target.c.subject_id.is_(None))
                .order_by(target.c.id)
                .limit(size)
                .with_for_update()
            )
        )
        if not ids:
            continue
        batch_table = target.name
        result = await session.execute(
            update(target)
            .where(target.c.id.in_(ids), target.c.subject_id.is_(None))
            .values(subject_id=subject_id)
        )
        updated_rows = int(result.rowcount or 0)
        if updated_rows != len(ids):
            raise RetiredSignalOwnershipBackfillError(
                "a retired signal row changed while its owner was assigned"
            )
        break
    await session.flush()
    await _validate_rows(session, subject_id=subject_id)
    total, remaining = await _counts(session)
    return RetiredSignalOwnershipBackfillResult(
        status="completed" if remaining == 0 else "running",
        snapshot_rows=total,
        scanned_rows=total - remaining,
        updated_rows=updated_rows,
        remaining_rows=remaining,
        batch_table=batch_table,
    )


__all__ = [
    "DEFAULT_RETIRED_SIGNAL_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "MAX_RETIRED_SIGNAL_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "RETIRED_SIGNAL_OWNERSHIP_BACKFILL_PHASE",
    "RETIRED_SIGNAL_OWNERSHIP_BACKFILL_TABLES",
    "RetiredSignalOwnershipBackfillError",
    "RetiredSignalOwnershipBackfillResult",
    "inspect_retired_signal_ownership",
    "run_retired_signal_ownership_backfill_batch",
]
