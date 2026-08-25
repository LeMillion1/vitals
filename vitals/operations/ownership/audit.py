"""Stage-5A audit of the reviewed scoped-key cutover.

Global uniqueness and a second subject cannot coexist: one weight per date and
one Garmin activity per external id are true of one person, not of a platform.
Stage 5 replaces each legacy global key with the scoped key that expresses what
it actually meant, and this phase is the gate that runs first.

It proves, read-only, that the replacement is safe to install: that no existing
row would collide under the proposed scoped key, and — just as important — that
no row is missing the scope the key depends on, because a scoped unique index
over a null scope column silently degenerates into the global key it replaced.

Nothing here creates, drops, or rewrites anything.  Its output is durable
evidence that the cutover migration may run, and a digest of the exact lake that
evidence describes.  Callers own commit or rollback.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import Table, and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.base import Base
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.scoped_keys import SCOPED_KEYS, ScopedIndex, ScopedKeySpec
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.operations.ownership.validate import (
    OwnershipValidationError,
    OwnershipValidationStatus,
    preflight_ownership_validation,
)
from vitals.utils.timeutils import now_utc


SCOPED_KEY_AUDIT_PHASE = "stage5.scoped_key_audit.v1"

_PHASE_KEY = SCOPED_KEY_AUDIT_PHASE
_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class ScopedKeyAuditStatus(StrEnum):
    NOT_STARTED = "not_started"
    COMPLETED = "completed"


class ScopedKeyAuditError(RuntimeError):
    """Base class for fail-closed Stage-5A errors."""


class ScopedKeyAuditDependencyError(ScopedKeyAuditError):
    """Stage 4 has not proved this exact lake."""


class ScopedKeyAuditStateError(ScopedKeyAuditError):
    """The persisted checkpoint or the reviewed catalog is inconsistent."""


class ScopedKeyAuditCollision(ScopedKeyAuditError):
    """The lake cannot satisfy a proposed scoped key as it stands."""


@dataclass(frozen=True, slots=True)
class ScopedKeyAuditResult:
    phase_key: str
    subject_id: uuid.UUID
    status: ScopedKeyAuditStatus
    legacy_keys_total: int
    scoped_indexes_total: int
    rows_inspected: int
    collisions_total: int
    unscoped_rows_total: int
    audit_checksum: str

    @property
    def completed(self) -> bool:
        return self.status is ScopedKeyAuditStatus.COMPLETED

    def to_safe_dict(self) -> dict[str, str | int]:
        return {
            "phase_key": self.phase_key,
            "status": self.status.value,
            "legacy_keys_total": self.legacy_keys_total,
            "scoped_indexes_total": self.scoped_indexes_total,
            "rows_inspected": self.rows_inspected,
            "collisions_total": self.collisions_total,
            "unscoped_rows_total": self.unscoped_rows_total,
            "audit_checksum": self.audit_checksum,
        }


def _valid_counter(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _POSTGRES_INTEGER_MAX
    )


def _extend(digest: str, values: Sequence[Any]) -> str:
    payload = json.dumps(
        [str(item) if isinstance(item, uuid.UUID) else item for item in values],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(bytes.fromhex(digest) + payload).hexdigest()


def _predicate(index: ScopedIndex | ScopedKeySpec, *, dialect: str):
    """Return the dialect's partial-index predicate, or ``None`` if total."""

    if isinstance(index, ScopedIndex):
        clause = (
            index.postgresql_predicate
            if dialect == "postgresql"
            else index.sqlite_predicate
        )
    else:
        clause = (
            index.legacy_postgresql_predicate
            if dialect == "postgresql"
            else index.legacy_sqlite_predicate
        )
    return None if clause is None else text(clause)


def _table_of(spec: ScopedKeySpec) -> Table:
    table = Base.metadata.tables.get(spec.table)
    if table is None:
        raise ScopedKeyAuditStateError(
            "the reviewed scoped-key catalog names a table that does not exist"
        )
    return table


async def _count(session: AsyncSession, query) -> int:
    value = await session.scalar(query)
    return int(value or 0)


async def _audit_index(
    session: AsyncSession,
    *,
    table: Table,
    index: ScopedIndex,
    dialect: str,
) -> tuple[int, int, int]:
    """Return (rows in scope, colliding groups, rows missing their scope)."""

    for column_name in index.columns:
        if column_name not in table.columns:
            raise ScopedKeyAuditStateError(
                "a proposed scoped key names a column that does not exist"
            )
    columns = [table.columns[name] for name in index.columns]
    predicate = _predicate(index, dialect=dialect)

    scope_query = select(func.count()).select_from(table)
    if predicate is not None:
        scope_query = scope_query.where(predicate)
    in_scope = await _count(session, scope_query)

    # A scoped unique index treats a null key column as distinct from every
    # other row, so only fully-populated keys can collide.
    populated = and_(*[column.is_not(None) for column in columns])
    grouped = select(*columns).select_from(table).where(populated)
    if predicate is not None:
        grouped = grouped.where(predicate)
    grouped = grouped.group_by(*columns).having(func.count() > 1)
    collisions = await _count(
        session,
        select(func.count()).select_from(grouped.subquery()),
    )

    missing_scope = 0
    if index.required_scope_column is not None:
        # Without its scope the row falls outside the scoped index entirely and
        # keeps no uniqueness at all: the cutover would quietly lose the rule.
        scope_column = table.columns[index.required_scope_column]
        missing_query = (
            select(func.count()).select_from(table).where(scope_column.is_(None))
        )
        if predicate is not None:
            missing_query = missing_query.where(predicate)
        missing_scope = await _count(session, missing_query)

    for value in (in_scope, collisions, missing_scope):
        if not _valid_counter(value):
            raise ScopedKeyAuditStateError(
                "a scoped-key audit query returned an implausible count"
            )
    return in_scope, collisions, missing_scope


async def _run_audit(session: AsyncSession) -> tuple[int, int, int, str]:
    """Return (scoped indexes, rows inspected, unscoped rows, digest)."""

    bind = session.get_bind()
    dialect = bind.dialect.name
    digest = _EMPTY_SHA256
    indexes = 0
    rows_inspected = 0
    unscoped_total = 0

    for spec in SCOPED_KEYS:
        table = _table_of(spec)
        for column_name in spec.legacy_columns:
            if column_name not in table.columns:
                raise ScopedKeyAuditStateError(
                    "a reviewed legacy key names a column that does not exist"
                )
        digest = _extend(digest, [spec.table, spec.legacy_name, spec.scope.value])
        for index in spec.replacements:
            indexes += 1
            in_scope, collisions, missing_scope = await _audit_index(
                session, table=table, index=index, dialect=dialect
            )
            rows_inspected += in_scope
            unscoped_total += missing_scope
            digest = _extend(
                digest, [index.name, in_scope, collisions, missing_scope]
            )
            if collisions:
                raise ScopedKeyAuditCollision(
                    "rows in this lake would collide under a proposed scoped key"
                )
            if missing_scope:
                raise ScopedKeyAuditCollision(
                    "a row would fall outside its proposed scoped key entirely"
                )
    return indexes, rows_inspected, unscoped_total, digest


async def _require_stage4_completed(session: AsyncSession) -> uuid.UUID:
    """Require Stage 4 to have proved *this* lake, not merely to have run."""

    try:
        validation = await preflight_ownership_validation(session)
    except OwnershipValidationError as exc:
        raise ScopedKeyAuditDependencyError(
            "the scoped-key audit requires a proved whole-lake ownership graph"
        ) from exc
    if validation.status is not OwnershipValidationStatus.COMPLETED:
        # Either the evidence was never recorded, or it no longer describes this
        # lake: in both cases Stage 4 has to be recorded again first.
        raise ScopedKeyAuditDependencyError(
            "the scoped-key audit requires current recorded Stage-4 evidence"
        )
    return validation.subject_id


def _validate_own(checkpoint: Any, *, subject_id: uuid.UUID) -> None:
    if checkpoint.phase_key != _PHASE_KEY or checkpoint.subject_id != subject_id:
        raise ScopedKeyAuditStateError(
            "the scoped-key audit checkpoint has the wrong phase or subject"
        )
    if checkpoint.status != ScopedKeyAuditStatus.COMPLETED.value:
        raise ScopedKeyAuditStateError(
            "the scoped-key audit checkpoint has an unsupported status"
        )
    counters = (
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
    )
    if not all(_valid_counter(value) for value in counters):
        raise ScopedKeyAuditStateError(
            "the scoped-key audit checkpoint has invalid counters"
        )
    if (
        checkpoint.updated_rows != 0
        or checkpoint.scanned_rows != checkpoint.unchanged_rows
        or checkpoint.scanned_rows != checkpoint.snapshot_rows
    ):
        raise ScopedKeyAuditStateError(
            "the scoped-key audit checkpoint has inconsistent counters"
        )
    digests = (
        checkpoint.data_checksum_before,
        checkpoint.data_checksum_after,
        checkpoint.ownership_checksum_after,
    )
    if len(set(digests)) != 1:
        raise ScopedKeyAuditStateError(
            "the scoped-key audit checkpoint has divergent evidence"
        )
    if checkpoint.started_at is None or checkpoint.completed_at is None:
        raise ScopedKeyAuditStateError(
            "the scoped-key audit checkpoint has invalid timestamps"
        )


async def _load_checkpoint(
    session: AsyncSession, *, for_update: bool
) -> OwnershipBackfillCheckpoint | None:
    query = select(OwnershipBackfillCheckpoint).where(
        OwnershipBackfillCheckpoint.phase_key == _PHASE_KEY
    )
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(query)


def _result(
    *,
    subject_id: uuid.UUID,
    status: ScopedKeyAuditStatus,
    indexes: int,
    rows_inspected: int,
    unscoped_rows: int,
    digest: str,
) -> ScopedKeyAuditResult:
    return ScopedKeyAuditResult(
        phase_key=_PHASE_KEY,
        subject_id=subject_id,
        status=status,
        legacy_keys_total=len(SCOPED_KEYS),
        scoped_indexes_total=indexes,
        rows_inspected=min(rows_inspected, _POSTGRES_INTEGER_MAX),
        collisions_total=0,
        unscoped_rows_total=unscoped_rows,
        audit_checksum=digest,
    )


async def preflight_scoped_key_audit(session: AsyncSession) -> ScopedKeyAuditResult:
    """Prove the cutover is installable, without recording anything."""

    with session.no_autoflush:
        subject_id = await _require_stage4_completed(session)
        checkpoint = await _load_checkpoint(session, for_update=False)
        if checkpoint is not None:
            _validate_own(checkpoint, subject_id=subject_id)
        indexes, rows, unscoped, digest = await _run_audit(session)
        recorded = (
            checkpoint is not None
            and checkpoint.ownership_checksum_after == digest
        )
        return _result(
            subject_id=subject_id,
            status=(
                ScopedKeyAuditStatus.COMPLETED
                if recorded
                else ScopedKeyAuditStatus.NOT_STARTED
            ),
            indexes=indexes,
            rows_inspected=rows,
            unscoped_rows=unscoped,
            digest=digest,
        )


async def run_scoped_key_audit(session: AsyncSession) -> ScopedKeyAuditResult:
    """Prove the cutover is installable and record the reviewed evidence."""

    with session.no_autoflush:
        await acquire_identity_governance_lock(session)
        subject_id = await _require_stage4_completed(session)
        checkpoint = await _load_checkpoint(session, for_update=True)
        if checkpoint is not None:
            _validate_own(checkpoint, subject_id=subject_id)
        indexes, rows, unscoped, digest = await _run_audit(session)
        stamp = now_utc().replace(microsecond=0)
        if checkpoint is None:
            checkpoint = OwnershipBackfillCheckpoint(
                phase_key=_PHASE_KEY,
                subject_id=subject_id,
                started_at=stamp,
            )
            session.add(checkpoint)
        bounded = min(rows, _POSTGRES_INTEGER_MAX)
        checkpoint.status = ScopedKeyAuditStatus.COMPLETED.value
        checkpoint.scan_high_watermark_id = bounded
        checkpoint.snapshot_rows = bounded
        checkpoint.last_scanned_id = bounded
        checkpoint.scanned_rows = bounded
        checkpoint.updated_rows = 0
        checkpoint.unchanged_rows = bounded
        checkpoint.data_checksum_before = digest
        checkpoint.data_checksum_after = digest
        checkpoint.ownership_checksum_after = digest
        checkpoint.updated_at = stamp
        checkpoint.completed_at = stamp
        await session.flush()
        return _result(
            subject_id=subject_id,
            status=ScopedKeyAuditStatus.COMPLETED,
            indexes=indexes,
            rows_inspected=rows,
            unscoped_rows=unscoped,
            digest=digest,
        )


__all__ = [
    "SCOPED_KEY_AUDIT_PHASE",
    "ScopedKeyAuditStatus",
    "ScopedKeyAuditError",
    "ScopedKeyAuditDependencyError",
    "ScopedKeyAuditStateError",
    "ScopedKeyAuditCollision",
    "ScopedKeyAuditResult",
    "preflight_scoped_key_audit",
    "run_scoped_key_audit",
]
