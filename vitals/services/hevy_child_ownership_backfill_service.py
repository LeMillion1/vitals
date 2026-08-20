"""Bounded Stage-3E ownership backfill for the Hevy child tree.

The two fixed tables are processed in structural order.  Exercises inherit the
exact subject/connection pair of their workout; sets inherit the same pair only
after their exercise checkpoint is complete and the workout grandparent has
been revalidated.  The service flushes but never commits.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from sqlalchemy import Table, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.hevy import HevyExercise, HevySet, HevyWorkout
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.services.hrt_child_ownership_backfill_service import (
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.normalized_ownership_backfill_service import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.services.provider_raw_ownership_backfill_service import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.raw_ownership_backfill_service import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.utils.timeutils import now_utc


HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE = "stage3.inherited_children.hevy.v1"
DEFAULT_HEVY_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE = 250
MAX_HEVY_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE = 1000

_PREFLIGHT_PAGE_SIZE = 1000
_FULL_ROW_MATERIALIZATION_SIZE = 1
_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KNOWN_CONNECTION_STATUSES = frozenset(item.value for item in IntegrationConnectionStatus)
_KNOWN_CONNECTION_TYPES = frozenset(item.value for item in IntegrationConnectionType)
_KNOWN_PROVIDERS = frozenset(item.value for item in IntegrationProvider)
_HEVY_PROVENANCE_STATUSES = frozenset(
    {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    }
)
_PRIOR_PHASES = (
    (RAW_OWNERSHIP_BACKFILL_PHASE,)
    + tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
    + tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
)


class HevyChildOwnershipBackfillStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"
    RESTORE_BLOCKED = "restore_blocked"


class HevyChildOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed Stage-3E failures."""


class HevyChildOwnershipBackfillValidationError(
    HevyChildOwnershipBackfillError, ValueError
):
    """A caller argument or persisted scalar is unsafe."""


class HevyChildOwnershipBackfillIdentityError(HevyChildOwnershipBackfillError):
    """The exact-one legacy owner/subject graph is unavailable."""


class HevyChildOwnershipBackfillDependencyError(HevyChildOwnershipBackfillError):
    """A prerequisite checkpoint is absent or malformed."""


class HevyChildOwnershipBackfillStateError(HevyChildOwnershipBackfillError):
    """Checkpoint progress or child inheritance is inconsistent."""


class HevyChildOwnershipBackfillProvenanceError(HevyChildOwnershipBackfillError):
    """The workout/raw/provider graph is not reviewed Hevy provenance."""


@dataclass(frozen=True, slots=True)
class HevyChildOwnershipBackfillPreflightResult:
    phase_key: str
    subject_id: uuid.UUID
    status: HevyChildOwnershipBackfillStatus
    tables_total: int
    completed_tables: int
    snapshot_rows: int
    scanned_rows: int
    updated_rows: int
    unchanged_rows: int
    remaining_rows: int
    rows_above_high_watermark: int
    data_checksum_before: str
    data_checksum_after: str
    ownership_checksum_after: str

    @property
    def completed(self) -> bool:
        return self.status is HevyChildOwnershipBackfillStatus.COMPLETED

    def to_safe_dict(self) -> dict[str, str | int]:
        return {
            "phase_key": self.phase_key,
            "status": self.status.value,
            "tables_total": self.tables_total,
            "completed_tables": self.completed_tables,
            "snapshot_rows": self.snapshot_rows,
            "scanned_rows": self.scanned_rows,
            "updated_rows": self.updated_rows,
            "unchanged_rows": self.unchanged_rows,
            "remaining_rows": self.remaining_rows,
            "rows_above_high_watermark": self.rows_above_high_watermark,
            "data_checksum_before": self.data_checksum_before,
            "data_checksum_after": self.data_checksum_after,
            "ownership_checksum_after": self.ownership_checksum_after,
        }


@dataclass(frozen=True, slots=True)
class HevyChildOwnershipBackfillBatchResult(HevyChildOwnershipBackfillPreflightResult):
    batch_table: str
    batch_scanned_rows: int
    batch_updated_rows: int
    batch_unchanged_rows: int

    @property
    def changed(self) -> bool:
        return self.batch_updated_rows > 0

    def to_safe_dict(self) -> dict[str, str | int]:
        result = HevyChildOwnershipBackfillPreflightResult.to_safe_dict(self)
        result.update(
            {
                "batch_table": self.batch_table,
                "batch_scanned_rows": self.batch_scanned_rows,
                "batch_updated_rows": self.batch_updated_rows,
                "batch_unchanged_rows": self.batch_unchanged_rows,
            }
        )
        return result


@dataclass(frozen=True, slots=True)
class _TableSpec:
    table: Table
    parent_fk: str

    @property
    def name(self) -> str:
        return self.table.name

    @property
    def phase_key(self) -> str:
        return f"{HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE}.{self.name}"


_TABLES = (
    _TableSpec(HevyExercise.__table__, "workout_id"),
    _TableSpec(HevySet.__table__, "exercise_id"),
)
HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES = tuple(spec.name for spec in _TABLES)
HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES: Mapping[str, str] = MappingProxyType(
    {spec.name: spec.phase_key for spec in _TABLES}
)
_PHASE_KEYS = tuple(spec.phase_key for spec in _TABLES)
_EXERCISE_PHASE = HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_exercises"]
_WORKOUT_PHASE = PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_workouts"]


@dataclass(frozen=True, slots=True)
class _Scope:
    subject_id: uuid.UUID
    owner_user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _CheckpointProjection:
    phase_key: str
    subject_id: uuid.UUID
    status: str
    scan_high_watermark_id: int
    snapshot_rows: int
    last_scanned_id: int
    scanned_rows: int
    updated_rows: int
    unchanged_rows: int
    data_checksum_before: str
    data_checksum_after: str
    ownership_checksum_after: str
    started_at: Any
    updated_at: Any
    completed_at: Any


@dataclass(frozen=True, slots=True)
class _TableSummary:
    spec: _TableSpec
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection | None
    high_watermark: int
    snapshot_rows: int
    remaining_rows: int
    rows_above: int


@dataclass(frozen=True, slots=True)
class _ChildLink:
    parent_id: int


@dataclass(frozen=True, slots=True)
class _Graph:
    children: Mapping[int, tuple[Any, ...]]
    exercises: Mapping[int, tuple[Any, ...]]
    workouts: Mapping[int, tuple[Any, ...]]
    raws: Mapping[int, tuple[Any, ...]]
    connections: Mapping[uuid.UUID, tuple[Any, ...]]


def _validate_batch_size(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_HEVY_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE
    ):
        raise HevyChildOwnershipBackfillValidationError(
            "batch_size must be an integer from 1 to "
            f"{MAX_HEVY_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE}"
        )
    return value


def _as_counter(value: Any, *, dependency: bool = False) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _POSTGRES_INTEGER_MAX
    ):
        error = (
            HevyChildOwnershipBackfillDependencyError
            if dependency
            else HevyChildOwnershipBackfillStateError
        )
        raise error("a checkpoint counter is not a PostgreSQL INTEGER value")
    return value


def _valid_lifecycle(checkpoint: Any) -> bool:
    if not isinstance(checkpoint.started_at, datetime) or not isinstance(
        checkpoint.updated_at, datetime
    ):
        return False
    if checkpoint.updated_at < checkpoint.started_at:
        return False
    return checkpoint.completed_at is None or (
        isinstance(checkpoint.completed_at, datetime)
        and checkpoint.completed_at >= checkpoint.started_at
    )


def _validate_checkpoint_shape(
    checkpoint: Any,
    *,
    expected_phase: str,
    subject_id: uuid.UUID,
    dependency: bool,
    restore: bool = False,
) -> str:
    error = (
        HevyChildOwnershipBackfillDependencyError
        if dependency
        else HevyChildOwnershipBackfillStateError
    )
    if checkpoint.phase_key != expected_phase or checkpoint.subject_id != subject_id:
        raise error("an ownership checkpoint has the wrong phase or subject")
    allowed = {"completed"}
    if not dependency:
        allowed.update({"running", "restore_blocked"})
    elif restore:
        allowed.update({"running", "restore_blocked"})
    if checkpoint.status not in allowed or not _valid_lifecycle(checkpoint):
        raise error("an ownership checkpoint has an invalid lifecycle")
    high = _as_counter(checkpoint.scan_high_watermark_id, dependency=dependency)
    snapshot = _as_counter(checkpoint.snapshot_rows, dependency=dependency)
    last = _as_counter(checkpoint.last_scanned_id, dependency=dependency)
    scanned = _as_counter(checkpoint.scanned_rows, dependency=dependency)
    updated = _as_counter(checkpoint.updated_rows, dependency=dependency)
    unchanged = _as_counter(checkpoint.unchanged_rows, dependency=dependency)
    if (
        snapshot > high
        or last > high
        or scanned > snapshot
        or scanned != updated + unchanged
    ):
        raise error("an ownership checkpoint has inconsistent counters")
    for digest in (
        checkpoint.data_checksum_before,
        checkpoint.data_checksum_after,
        checkpoint.ownership_checksum_after,
    ):
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise error("an ownership checkpoint has invalid checksum evidence")
    if checkpoint.status in {"running", "completed"} and (
        checkpoint.data_checksum_before != checkpoint.data_checksum_after
    ):
        raise error("an ownership checkpoint has divergent data evidence")
    if checkpoint.status == "completed" and (
        last != high or scanned != snapshot or checkpoint.completed_at is None
    ):
        raise error("a completed ownership checkpoint is incomplete")
    if checkpoint.status == "running" and checkpoint.completed_at is not None:
        raise error("a running ownership checkpoint is terminal")
    if checkpoint.status == "restore_blocked" and (
        high == 0
        or snapshot == 0
        or last != 0
        or scanned != 0
        or updated != 0
        or unchanged != 0
        or checkpoint.data_checksum_before != _EMPTY_SHA256
        or checkpoint.data_checksum_after != _EMPTY_SHA256
        or checkpoint.ownership_checksum_after != _EMPTY_SHA256
        or checkpoint.completed_at is not None
    ):
        raise error("a restore-blocked ownership checkpoint is malformed")
    if dependency and checkpoint.status == "running" and (
        not restore or high == 0 or snapshot == 0
    ):
        raise error("a dependency checkpoint is not terminal")
    return checkpoint.status


async def _load_scope(session: AsyncSession, *, for_update: bool) -> _Scope:
    if for_update:
        await acquire_identity_governance_lock(session)
        subject_stmt = (
            select(HealthSubject)
            .order_by(HealthSubject.id)
            .limit(2)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        subjects = list(await session.scalars(subject_stmt))
        if len(subjects) != 1:
            raise HevyChildOwnershipBackfillIdentityError(
                "Hevy child backfill requires exactly one health subject"
            )
        subject_id, owner_user_id = subjects[0].id, subjects[0].owner_user_id
        owner = await session.scalar(
            select(User)
            .where(User.id == owner_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        connections = list(
            await session.scalars(
                select(IntegrationConnection)
                .order_by(IntegrationConnection.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        owner_status = owner.status if owner is not None else None
    else:
        rows = list(
            await session.execute(
                select(HealthSubject.id, HealthSubject.owner_user_id)
                .order_by(HealthSubject.id)
                .limit(2)
            )
        )
        if len(rows) != 1:
            raise HevyChildOwnershipBackfillIdentityError(
                "Hevy child backfill requires exactly one health subject"
            )
        subject_id, owner_user_id = rows[0]
        owner_status = await session.scalar(
            select(User.status).where(User.id == owner_user_id)
        )
        connections = list(
            await session.execute(
                select(
                    IntegrationConnection.subject_id,
                    IntegrationConnection.provider,
                    IntegrationConnection.connection_type,
                    IntegrationConnection.status,
                ).order_by(IntegrationConnection.id)
            )
        )
    if owner_status != UserStatus.ACTIVE.value:
        raise HevyChildOwnershipBackfillIdentityError(
            "the sole health subject must have an active owner"
        )
    for connection in connections:
        if connection.subject_id != subject_id:
            raise HevyChildOwnershipBackfillIdentityError(
                "an integration connection belongs to another subject"
            )
        if (
            connection.provider not in _KNOWN_PROVIDERS
            or connection.connection_type not in _KNOWN_CONNECTION_TYPES
            or connection.status not in _KNOWN_CONNECTION_STATUSES
        ):
            raise HevyChildOwnershipBackfillIdentityError(
                "an integration connection has an unknown mapping"
            )
    return _Scope(subject_id=subject_id, owner_user_id=owner_user_id)


def _checkpoint_columns():
    return select(
        OwnershipBackfillCheckpoint.phase_key,
        OwnershipBackfillCheckpoint.subject_id,
        OwnershipBackfillCheckpoint.status,
        OwnershipBackfillCheckpoint.scan_high_watermark_id,
        OwnershipBackfillCheckpoint.snapshot_rows,
        OwnershipBackfillCheckpoint.last_scanned_id,
        OwnershipBackfillCheckpoint.scanned_rows,
        OwnershipBackfillCheckpoint.updated_rows,
        OwnershipBackfillCheckpoint.unchanged_rows,
        OwnershipBackfillCheckpoint.data_checksum_before,
        OwnershipBackfillCheckpoint.data_checksum_after,
        OwnershipBackfillCheckpoint.ownership_checksum_after,
        OwnershipBackfillCheckpoint.started_at,
        OwnershipBackfillCheckpoint.updated_at,
        OwnershipBackfillCheckpoint.completed_at,
    )


async def _load_checkpoint_group(
    session: AsyncSession, *, phases: tuple[str, ...], for_update: bool
) -> dict[str, OwnershipBackfillCheckpoint | _CheckpointProjection]:
    if for_update:
        stmt = (
            select(OwnershipBackfillCheckpoint)
            .where(OwnershipBackfillCheckpoint.phase_key.in_(phases))
            .order_by(OwnershipBackfillCheckpoint.phase_key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        rows = list(await session.scalars(stmt))
        return {row.phase_key: row for row in rows}
    stmt = (
        _checkpoint_columns()
        .where(OwnershipBackfillCheckpoint.phase_key.in_(phases))
        .order_by(OwnershipBackfillCheckpoint.phase_key)
    )
    rows = list(await session.execute(stmt))
    return {row.phase_key: _CheckpointProjection(*row) for row in rows}


def _require_own_checkpoint_group(
    checkpoints: Mapping[str, Any], *, scope: _Scope
) -> None:
    """Reject torn or reverse-ordered durable Stage-3E control state."""

    phases = set(checkpoints)
    if phases and phases != set(_PHASE_KEYS):
        raise HevyChildOwnershipBackfillStateError(
            "the Hevy child checkpoint group is partial"
        )
    for spec in _TABLES:
        checkpoint = checkpoints.get(spec.phase_key)
        if checkpoint is not None:
            _validate_checkpoint_shape(
                checkpoint,
                expected_phase=spec.phase_key,
                subject_id=scope.subject_id,
                dependency=False,
            )
    if checkpoints:
        exercise = checkpoints[_EXERCISE_PHASE]
        sets = checkpoints[
            HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_sets"]
        ]
        pair = (exercise.status, sets.status)
        allowed_pairs = {
            ("running", "running"),
            ("completed", "running"),
            ("completed", "completed"),
            ("restore_blocked", "restore_blocked"),
            ("restore_blocked", "completed"),
        }
        if pair not in allowed_pairs:
            raise HevyChildOwnershipBackfillStateError(
                "the Hevy child checkpoint order is inconsistent"
            )
        if pair == ("restore_blocked", "completed") and not (
            sets.scan_high_watermark_id == 0
            and sets.snapshot_rows == 0
            and sets.last_scanned_id == 0
            and sets.scanned_rows == 0
            and sets.updated_rows == 0
            and sets.unchanged_rows == 0
            and sets.data_checksum_before == _EMPTY_SHA256
            and sets.data_checksum_after == _EMPTY_SHA256
            and sets.ownership_checksum_after == _EMPTY_SHA256
        ):
            raise HevyChildOwnershipBackfillStateError(
                "completed Hevy sets after a restore block must be exactly empty"
            )


def _require_dependencies(
    dependencies: Mapping[str, Any], *, scope: _Scope, restore: bool
) -> None:
    if set(dependencies) != set(_PRIOR_PHASES):
        raise HevyChildOwnershipBackfillDependencyError(
            "Stage-3A through Stage-3D checkpoints are incomplete"
        )
    for phase in _PRIOR_PHASES:
        checkpoint = dependencies[phase]
        _validate_checkpoint_shape(
            checkpoint,
            expected_phase=phase,
            subject_id=scope.subject_id,
            dependency=True,
            restore=restore,
        )
        if restore:
            if phase == RAW_OWNERSHIP_BACKFILL_PHASE and checkpoint.status not in {
                "completed",
                "restore_blocked",
            }:
                raise HevyChildOwnershipBackfillDependencyError(
                    "Stage-3A is not restore-terminal"
                )
            if phase in PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values():
                if checkpoint.status not in {"completed", "restore_blocked"}:
                    raise HevyChildOwnershipBackfillDependencyError(
                        "Stage-3D is not restore-terminal"
                    )
            elif phase != RAW_OWNERSHIP_BACKFILL_PHASE and checkpoint.status not in {
                "running",
                "completed",
            }:
                raise HevyChildOwnershipBackfillDependencyError(
                    "Stage-3B/3C restore state must be running or completed"
                )
        elif checkpoint.status != "completed":
            raise HevyChildOwnershipBackfillDependencyError(
                "Stage-3A through Stage-3D must be completed"
            )


def _max_id_from_dependency(dependencies: Mapping[str, Any]) -> int:
    return dependencies[_WORKOUT_PHASE].scan_high_watermark_id


def _row_tuple(row: Any) -> tuple[Any, ...]:
    return tuple(row)


def _row_mapping(table: Table, row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip((column.name for column in table.columns), row, strict=True))


async def _select_rows(
    session: AsyncSession,
    table: Table,
    ids: list[Any],
    *,
    for_update: bool,
    one_at_a_time: bool = False,
) -> dict[Any, tuple[Any, ...]]:
    if not ids:
        return {}
    result: dict[Any, tuple[Any, ...]] = {}
    groups = [[value] for value in ids] if one_at_a_time else [ids]
    for group in groups:
        stmt = (
            select(*table.c)
            .where(table.c.id.in_(group))
            .order_by(table.c.id)
            .execution_options(populate_existing=True)
        )
        if one_at_a_time:
            stmt = stmt.limit(_FULL_ROW_MATERIALIZATION_SIZE)
        if for_update:
            stmt = stmt.with_for_update()
        for row in await session.execute(stmt):
            result[row._mapping["id"]] = _row_tuple(row)
    if tuple(result) != tuple(ids):
        raise HevyChildOwnershipBackfillStateError(
            f"a reviewed {table.name} row disappeared while locking"
        )
    return result


async def _project_graph(
    session: AsyncSession, *, spec: _TableSpec, child_ids: list[int]
) -> _Graph:
    child_links_stmt = (
        select(spec.table.c.id, spec.table.c[spec.parent_fk])
        .where(spec.table.c.id.in_(child_ids))
        .order_by(spec.table.c.id)
    )
    child_links = {
        row._mapping["id"]: tuple(row)
        for row in await session.execute(child_links_stmt)
    }
    if tuple(child_links) != tuple(child_ids):
        raise HevyChildOwnershipBackfillStateError(
            "a reviewed Hevy child disappeared before link projection"
        )
    if spec.table is HevyExercise.__table__:
        exercises: dict[int, tuple[Any, ...]] = {}
        workout_ids = sorted({row[1] for row in child_links.values()})
    else:
        exercise_ids = sorted({row[1] for row in child_links.values()})
        exercises = await _select_rows(
            session,
            HevyExercise.__table__,
            exercise_ids,
            for_update=False,
        )
        workout_ids = sorted(
            {
                _row_mapping(HevyExercise.__table__, row)["workout_id"]
                for row in exercises.values()
            }
        )
    workouts = await _select_rows(
        session, HevyWorkout.__table__, workout_ids, for_update=False
    )
    raw_ids: list[int] = []
    for row in workouts.values():
        raw_id = _row_mapping(HevyWorkout.__table__, row)["raw_payload_id"]
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            raise HevyChildOwnershipBackfillProvenanceError(
                "a Hevy workout lacks an exact raw payload link"
            )
        raw_ids.append(raw_id)
    raw_ids = sorted(set(raw_ids))
    raws = await _select_rows(
        session,
        RawPayload.__table__,
        raw_ids,
        for_update=False,
        one_at_a_time=True,
    )
    connection_ids: list[uuid.UUID] = []
    for row in raws.values():
        connection_id = _row_mapping(RawPayload.__table__, row)[
            "integration_connection_id"
        ]
        if not isinstance(connection_id, uuid.UUID):
            raise HevyChildOwnershipBackfillProvenanceError(
                "a Hevy raw payload lacks an exact connection root"
            )
        connection_ids.append(connection_id)
    connection_ids = sorted(set(connection_ids))
    connections = await _select_rows(
        session,
        IntegrationConnection.__table__,
        connection_ids,
        for_update=False,
    )
    return _Graph(
        children=child_links,
        exercises=exercises,
        workouts=workouts,
        raws=raws,
        connections=connections,
    )


async def _after_workout_roots_locked_for_test() -> None:
    """A no-op seam used only by deterministic two-session race tests."""


async def _after_parent_exercises_locked_for_test() -> None:
    """A no-op seam used only by deterministic two-session race tests."""


async def _lock_graph_for_ids(
    session: AsyncSession, *, spec: _TableSpec, child_ids: list[int]
) -> _Graph:
    if child_ids != sorted(set(child_ids)):
        raise HevyChildOwnershipBackfillStateError(
            "Hevy child lock targets are not canonical"
        )
    graph = await _project_graph(session, spec=spec, child_ids=child_ids)
    connection_ids = list(graph.connections)
    raw_ids = list(graph.raws)
    workout_ids = list(graph.workouts)
    locked_connections = await _select_rows(
        session,
        IntegrationConnection.__table__,
        connection_ids,
        for_update=True,
    )
    if locked_connections != graph.connections:
        raise HevyChildOwnershipBackfillProvenanceError(
            "a Hevy connection root changed before locking"
        )
    locked_raws = await _select_rows(
        session,
        RawPayload.__table__,
        raw_ids,
        for_update=True,
        one_at_a_time=True,
    )
    if locked_raws != graph.raws:
        raise HevyChildOwnershipBackfillProvenanceError(
            "a Hevy raw root changed before locking"
        )
    locked_workouts = await _select_rows(
        session,
        HevyWorkout.__table__,
        workout_ids,
        for_update=True,
    )
    if locked_workouts != graph.workouts:
        raise HevyChildOwnershipBackfillProvenanceError(
            "a Hevy workout root changed before locking"
        )
    await _after_workout_roots_locked_for_test()
    if spec.table is HevySet.__table__:
        exercise_ids = list(graph.exercises)
        locked_exercises = await _select_rows(
            session,
            HevyExercise.__table__,
            exercise_ids,
            for_update=True,
        )
        if locked_exercises != graph.exercises:
            raise HevyChildOwnershipBackfillStateError(
                "a Hevy exercise/workout link changed before locking"
            )
        await _after_parent_exercises_locked_for_test()
    locked_children = list(
        await session.execute(
            select(spec.table.c.id, spec.table.c[spec.parent_fk])
            .where(spec.table.c.id.in_(child_ids))
            .order_by(spec.table.c.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    locked_links = {row._mapping["id"]: tuple(row) for row in locked_children}
    if locked_links != graph.children:
        raise HevyChildOwnershipBackfillStateError(
            "a Hevy child parent link changed after root locking"
        )
    # Re-read every projection after all locks.  This catches a committed switch
    # between the initial projection and the corresponding FOR UPDATE statement.
    current = await _project_graph(session, spec=spec, child_ids=child_ids)
    if current != graph:
        raise HevyChildOwnershipBackfillStateError(
            "the Hevy inheritance graph changed across canonical locking"
        )
    return graph


def _validate_connection(
    row: tuple[Any, ...], *, scope: _Scope, connection_id: uuid.UUID
) -> None:
    connection = _row_mapping(IntegrationConnection.__table__, row)
    if (
        connection["id"] != connection_id
        or connection["subject_id"] != scope.subject_id
        or connection["provider"] != IntegrationProvider.HEVY.value
        or connection["connection_type"]
        != IntegrationConnectionType.ACCOUNT.value
        or connection["status"] not in _HEVY_PROVENANCE_STATUSES
    ):
        raise HevyChildOwnershipBackfillProvenanceError(
            "a Hevy child does not resolve to a reviewed account connection"
        )


def _validate_workout_graph(
    graph: _Graph,
    *,
    workout_id: int,
    scope: _Scope,
) -> tuple[uuid.UUID, uuid.UUID]:
    workout_row = graph.workouts.get(workout_id)
    if workout_row is None:
        raise HevyChildOwnershipBackfillStateError(
            "a Hevy child references a missing workout"
        )
    workout = _row_mapping(HevyWorkout.__table__, workout_row)
    if (
        workout["subject_id"] != scope.subject_id
        or not isinstance(workout["integration_connection_id"], uuid.UUID)
    ):
        raise HevyChildOwnershipBackfillStateError(
            "a Hevy workout lacks exact subject/connection ownership"
        )
    connection_id = workout["integration_connection_id"]
    if workout["actor_user_id"] not in {None, scope.owner_user_id}:
        raise HevyChildOwnershipBackfillProvenanceError(
            "a Hevy workout has foreign actor provenance"
        )
    if (
        workout["domain"] != Domain.WORKOUTS.value
        or workout["source"] != Source.HEVY_API.value
        or not isinstance(workout["external_id"], str)
        or not workout["external_id"]
    ):
        raise HevyChildOwnershipBackfillProvenanceError(
            "a Hevy workout has unreviewed provenance"
        )
    raw_id = workout["raw_payload_id"]
    raw_row = graph.raws.get(raw_id)
    if raw_row is None:
        raise HevyChildOwnershipBackfillProvenanceError(
            "a Hevy workout raw link is missing"
        )
    raw = _row_mapping(RawPayload.__table__, raw_row)
    if (
        raw["subject_id"] != scope.subject_id
        or raw["integration_connection_id"] != connection_id
        or raw["actor_user_id"] not in {None, scope.owner_user_id}
        or raw["domain"] != Domain.WORKOUTS.value
        or raw["source"] != Source.HEVY_API.value
        or raw["external_id"] != workout["external_id"]
        or raw["file_asset_id"] is not None
        or not isinstance(raw["payload"], dict)
        or str(raw["payload"].get("id") or "").strip()
        != workout["external_id"]
    ):
        raise HevyChildOwnershipBackfillProvenanceError(
            "a Hevy workout/raw link failed canonical provenance validation"
        )
    connection_row = graph.connections.get(connection_id)
    if connection_row is None:
        raise HevyChildOwnershipBackfillProvenanceError(
            "a Hevy workout connection root is missing"
        )
    _validate_connection(connection_row, scope=scope, connection_id=connection_id)
    return scope.subject_id, connection_id


def _classify_ownership(
    *,
    row: Mapping[str, Any],
    expected: tuple[uuid.UUID, uuid.UUID],
    high_watermark: int,
) -> bool:
    actual = (row["subject_id"], row["integration_connection_id"])
    historical = row["id"] <= high_watermark
    if actual == expected:
        return False
    if historical and actual in {(None, None), (expected[0], None)}:
        return True
    if not historical:
        raise HevyChildOwnershipBackfillStateError(
            "a Hevy child above the high-water mark lacks exact S+C"
        )
    if actual[0] is None or actual[1] is None:
        raise HevyChildOwnershipBackfillStateError(
            "a historical Hevy child has unsafe partial ownership"
        )
    raise HevyChildOwnershipBackfillStateError(
        "a historical Hevy child has foreign ownership"
    )


def _validate_row(
    *,
    spec: _TableSpec,
    row: Mapping[str, Any],
    graph: _Graph,
    scope: _Scope,
    high_watermark: int,
    exercise_high_watermark: int,
    require_exact_exercise: bool,
) -> bool:
    if spec.table is HevyExercise.__table__:
        workout_id = row["workout_id"]
    else:
        exercise_row = graph.exercises.get(row["exercise_id"])
        if exercise_row is None:
            raise HevyChildOwnershipBackfillStateError(
                "a Hevy set references a missing exercise"
            )
        exercise = _row_mapping(HevyExercise.__table__, exercise_row)
        workout_id = exercise["workout_id"]
    expected = _validate_workout_graph(
        graph, workout_id=workout_id, scope=scope
    )
    if spec.table is HevySet.__table__:
        exercise_changed = _classify_ownership(
            row=exercise,
            expected=expected,
            high_watermark=exercise_high_watermark,
        )
        if require_exact_exercise and exercise_changed:
            raise HevyChildOwnershipBackfillStateError(
                "Hevy sets cannot advance before exact exercise ownership"
            )
    return _classify_ownership(
        row=row,
        expected=expected,
        high_watermark=high_watermark,
    )


async def _load_full_child(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    row_id: int,
    for_update: bool,
) -> Any:
    stmt = (
        select(*spec.table.c)
        .where(spec.table.c.id == row_id)
        .limit(_FULL_ROW_MATERIALIZATION_SIZE)
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise HevyChildOwnershipBackfillStateError(
            "a reviewed Hevy child disappeared before processing"
        )
    return row


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HevyChildOwnershipBackfillStateError(
                "a Hevy child contains a non-finite number"
            )
        return ["float", value.hex()]
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    if isinstance(value, uuid.UUID):
        return ["uuid", str(value)]
    if isinstance(value, (date, datetime, time)):
        return [type(value).__name__, value.isoformat()]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, Mapping):
        return [
            "mapping",
            [
                [str(key), _canonical_value(item)]
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            ],
        ]
    if isinstance(value, (list, tuple)):
        return ["sequence", [_canonical_value(item) for item in value]]
    raise HevyChildOwnershipBackfillStateError(
        "a Hevy child contains an unsupported persisted value"
    )


def _extend_checksum(previous: str, envelope: Any) -> str:
    encoded = json.dumps(
        envelope, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(bytes.fromhex(previous))
    digest.update(len(encoded).to_bytes(8, byteorder="big", signed=False))
    digest.update(encoded)
    return digest.hexdigest()


def _data_envelope(spec: _TableSpec, row: Any) -> list[Any]:
    return [
        spec.name,
        [
            [column.name, _canonical_value(row._mapping[column.name])]
            for column in spec.table.columns
            if column.name not in {"subject_id", "integration_connection_id"}
        ],
    ]


def _ownership_envelope(spec: _TableSpec, row: Any) -> list[Any]:
    subject_id = row._mapping["subject_id"]
    connection_id = row._mapping["integration_connection_id"]
    return [
        spec.name,
        row._mapping["id"],
        str(subject_id) if subject_id is not None else None,
        str(connection_id) if connection_id is not None else None,
    ]


async def _max_id(session: AsyncSession, spec: _TableSpec) -> int:
    return _as_counter(
        int(await session.scalar(select(func.max(spec.table.c.id))) or 0)
    )


async def _count_to(
    session: AsyncSession, spec: _TableSpec, *, high_watermark: int
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(spec.table)
            .where(spec.table.c.id <= high_watermark)
        )
        or 0
    )


async def _remaining(
    session: AsyncSession,
    spec: _TableSpec,
    *,
    high_watermark: int,
    cursor: int,
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(spec.table)
            .where(spec.table.c.id > cursor, spec.table.c.id <= high_watermark)
        )
        or 0
    )


async def _validate_counts(
    session: AsyncSession, *, spec: _TableSpec, checkpoint: Any
) -> int:
    snapshot = await _count_to(
        session, spec, high_watermark=checkpoint.scan_high_watermark_id
    )
    prefix = await _count_to(
        session, spec, high_watermark=checkpoint.last_scanned_id
    )
    remaining = await _remaining(
        session,
        spec,
        high_watermark=checkpoint.scan_high_watermark_id,
        cursor=checkpoint.last_scanned_id,
    )
    if snapshot != checkpoint.snapshot_rows or prefix != checkpoint.scanned_rows:
        raise HevyChildOwnershipBackfillStateError(
            "a Hevy child checkpoint cardinality drifted"
        )
    if checkpoint.scanned_rows + remaining != checkpoint.snapshot_rows:
        raise HevyChildOwnershipBackfillStateError(
            "a Hevy child checkpoint no longer matches its snapshot"
        )
    return remaining


def _exercise_high_watermark(
    checkpoints: Mapping[str, Any], summaries: list[_TableSummary] | None = None
) -> int:
    checkpoint = checkpoints.get(_EXERCISE_PHASE)
    if checkpoint is not None:
        return checkpoint.scan_high_watermark_id
    if summaries:
        return next(
            item.high_watermark
            for item in summaries
            if item.spec.table is HevyExercise.__table__
        )
    return 0


async def _scan_table(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    scope: _Scope,
    high_watermark: int,
    exercise_high_watermark: int,
    checkpoint_cursor: int | None,
    for_update: bool,
    require_exact_exercise: bool,
    start_after: int = 0,
    expected_ownership_checksum: str | None = None,
) -> tuple[int, str]:
    last_id = start_after
    rows_above = 0
    ownership = _EMPTY_SHA256
    while True:
        ids = list(
            await session.scalars(
                select(spec.table.c.id)
                .where(spec.table.c.id > last_id)
                .order_by(spec.table.c.id)
                .limit(_PREFLIGHT_PAGE_SIZE)
            )
        )
        if not ids:
            break
        graph = (
            await _lock_graph_for_ids(session, spec=spec, child_ids=ids)
            if for_update
            else await _project_graph(session, spec=spec, child_ids=ids)
        )
        for row_id in ids:
            row = await _load_full_child(
                session, spec=spec, row_id=row_id, for_update=for_update
            )
            if (row._mapping["id"], row._mapping[spec.parent_fk]) != graph.children[
                row_id
            ]:
                raise HevyChildOwnershipBackfillStateError(
                    "a Hevy child parent link changed after graph projection"
                )
            changed = _validate_row(
                spec=spec,
                row=row._mapping,
                graph=graph,
                scope=scope,
                high_watermark=high_watermark,
                exercise_high_watermark=exercise_high_watermark,
                require_exact_exercise=require_exact_exercise,
            )
            if checkpoint_cursor is not None and row_id <= checkpoint_cursor and changed:
                raise HevyChildOwnershipBackfillStateError(
                    "a previously scanned Hevy child requires ownership repair"
                )
            if row_id > high_watermark:
                rows_above += 1
            elif expected_ownership_checksum is not None:
                ownership = _extend_checksum(
                    ownership, _ownership_envelope(spec, row)
                )
        last_id = ids[-1]
        if len(ids) < _PREFLIGHT_PAGE_SIZE:
            break
    if expected_ownership_checksum is not None and (
        ownership != expected_ownership_checksum
    ):
        raise HevyChildOwnershipBackfillStateError(
            "completed Hevy child ownership changed"
        )
    return rows_above, ownership


async def _verify_final_snapshot(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    scope: _Scope,
    checkpoint: Any,
    exercise_high_watermark: int,
    require_data_checksum: bool,
) -> None:
    before = _EMPTY_SHA256
    ownership = _EMPTY_SHA256
    scanned = 0
    last_id = 0
    while True:
        ids = list(
            await session.scalars(
                select(spec.table.c.id)
                .where(
                    spec.table.c.id > last_id,
                    spec.table.c.id <= checkpoint.scan_high_watermark_id,
                )
                .order_by(spec.table.c.id)
                .limit(_PREFLIGHT_PAGE_SIZE)
            )
        )
        if not ids:
            break
        graph = await _lock_graph_for_ids(session, spec=spec, child_ids=ids)
        for row_id in ids:
            row = await _load_full_child(
                session, spec=spec, row_id=row_id, for_update=True
            )
            if (row._mapping["id"], row._mapping[spec.parent_fk]) != graph.children[
                row_id
            ]:
                raise HevyChildOwnershipBackfillStateError(
                    "a Hevy child link changed during final verification"
                )
            changed = _validate_row(
                spec=spec,
                row=row._mapping,
                graph=graph,
                scope=scope,
                high_watermark=checkpoint.scan_high_watermark_id,
                exercise_high_watermark=exercise_high_watermark,
                require_exact_exercise=spec.table is HevySet.__table__,
            )
            if changed:
                raise HevyChildOwnershipBackfillStateError(
                    "final Hevy child snapshot still requires repair"
                )
            before = _extend_checksum(before, _data_envelope(spec, row))
            ownership = _extend_checksum(
                ownership, _ownership_envelope(spec, row)
            )
            scanned += 1
        last_id = ids[-1]
        if len(ids) < _PREFLIGHT_PAGE_SIZE:
            break
    if scanned != checkpoint.snapshot_rows:
        raise HevyChildOwnershipBackfillStateError(
            "final Hevy child snapshot count changed"
        )
    if require_data_checksum and (
        before != checkpoint.data_checksum_before
        or before != checkpoint.data_checksum_after
    ):
        raise HevyChildOwnershipBackfillStateError(
            "Hevy child data changed during the maintenance window"
        )
    if ownership != checkpoint.ownership_checksum_after:
        raise HevyChildOwnershipBackfillStateError(
            "completed Hevy child ownership changed"
        )


async def _summaries(
    session: AsyncSession,
    *,
    scope: _Scope,
    checkpoints: Mapping[str, Any],
    validate_rows: bool,
    for_update: bool,
    verify_completed: bool,
    require_completed_data_checksum: bool = False,
    volatile_completed_group: bool = False,
) -> list[_TableSummary]:
    prelim: list[_TableSummary] = []
    for spec in _TABLES:
        checkpoint = checkpoints.get(spec.phase_key)
        if checkpoint is None:
            high = await _max_id(session, spec)
            snapshot = await _count_to(session, spec, high_watermark=high)
            remaining = snapshot
        else:
            _validate_checkpoint_shape(
                checkpoint,
                expected_phase=spec.phase_key,
                subject_id=scope.subject_id,
                dependency=False,
            )
            high = checkpoint.scan_high_watermark_id
            snapshot = checkpoint.snapshot_rows
            remaining = (
                0
                if volatile_completed_group and checkpoint.status == "completed"
                else await _validate_counts(
                    session, spec=spec, checkpoint=checkpoint
                )
            )
        prelim.append(
            _TableSummary(spec, checkpoint, high, snapshot, remaining, 0)
        )
    exercise_high = _exercise_high_watermark(checkpoints, prelim)
    exercise_completed = (
        checkpoints.get(_EXERCISE_PHASE) is not None
        and checkpoints[_EXERCISE_PHASE].status == "completed"
    )
    result: list[_TableSummary] = []
    for summary in prelim:
        checkpoint = summary.checkpoint
        if validate_rows:
            rows_above, _ = await _scan_table(
                session,
                spec=summary.spec,
                scope=scope,
                high_watermark=summary.high_watermark,
                exercise_high_watermark=exercise_high,
                checkpoint_cursor=(
                    checkpoint.last_scanned_id if checkpoint is not None else None
                ),
                for_update=for_update,
                require_exact_exercise=(
                    summary.spec.table is HevySet.__table__ and exercise_completed
                ),
                expected_ownership_checksum=(
                    checkpoint.ownership_checksum_after
                    if checkpoint is not None
                    and checkpoint.status == "completed"
                    and not volatile_completed_group
                    else None
                ),
            )
        else:
            rows_above = int(
                await session.scalar(
                    select(func.count())
                    .select_from(summary.spec.table)
                    .where(summary.spec.table.c.id > summary.high_watermark)
                )
                or 0
            )
        if (
            verify_completed
            and checkpoint is not None
            and checkpoint.status == "completed"
            and not volatile_completed_group
        ):
            await _verify_final_snapshot(
                session,
                spec=summary.spec,
                scope=scope,
                checkpoint=checkpoint,
                exercise_high_watermark=exercise_high,
                require_data_checksum=require_completed_data_checksum,
            )
        result.append(
            _TableSummary(
                summary.spec,
                checkpoint,
                summary.high_watermark,
                summary.snapshot_rows,
                summary.remaining_rows,
                rows_above,
            )
        )
    return result


def _aggregate_digest(summaries: list[_TableSummary], field: str) -> str:
    digest = _EMPTY_SHA256
    for summary in summaries:
        value = (
            getattr(summary.checkpoint, field)
            if summary.checkpoint is not None
            else _EMPTY_SHA256
        )
        digest = _extend_checksum(digest, [summary.spec.name, value])
    return digest


def _result(
    *, scope: _Scope, summaries: list[_TableSummary]
) -> HevyChildOwnershipBackfillPreflightResult:
    checkpoints = [row.checkpoint for row in summaries if row.checkpoint is not None]
    if any(row.status == "restore_blocked" for row in checkpoints):
        status = HevyChildOwnershipBackfillStatus.RESTORE_BLOCKED
    elif len(checkpoints) == len(_TABLES) and all(
        row.status == "completed" for row in checkpoints
    ):
        status = HevyChildOwnershipBackfillStatus.COMPLETED
    elif checkpoints:
        status = HevyChildOwnershipBackfillStatus.RUNNING
    else:
        status = HevyChildOwnershipBackfillStatus.NOT_STARTED
    return HevyChildOwnershipBackfillPreflightResult(
        phase_key=HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE,
        subject_id=scope.subject_id,
        status=status,
        tables_total=len(_TABLES),
        completed_tables=sum(row.status == "completed" for row in checkpoints),
        snapshot_rows=sum(row.snapshot_rows for row in summaries),
        scanned_rows=sum(row.scanned_rows for row in checkpoints),
        updated_rows=sum(row.updated_rows for row in checkpoints),
        unchanged_rows=sum(row.unchanged_rows for row in checkpoints),
        remaining_rows=sum(row.remaining_rows for row in summaries),
        rows_above_high_watermark=sum(row.rows_above for row in summaries),
        data_checksum_before=_aggregate_digest(summaries, "data_checksum_before"),
        data_checksum_after=_aggregate_digest(summaries, "data_checksum_after"),
        ownership_checksum_after=_aggregate_digest(
            summaries, "ownership_checksum_after"
        ),
    )


def _batch_result(
    result: HevyChildOwnershipBackfillPreflightResult,
    *,
    table: str,
    scanned: int,
    updated: int,
    unchanged: int,
) -> HevyChildOwnershipBackfillBatchResult:
    return HevyChildOwnershipBackfillBatchResult(
        **{
            field: getattr(result, field)
            for field in HevyChildOwnershipBackfillPreflightResult.__dataclass_fields__
        },
        batch_table=table,
        batch_scanned_rows=scanned,
        batch_updated_rows=updated,
        batch_unchanged_rows=unchanged,
    )


def _completed_group(checkpoints: Mapping[str, Any]) -> bool:
    return len(checkpoints) == len(_TABLES) and all(
        checkpoint.status == "completed" for checkpoint in checkpoints.values()
    )


async def preflight_hevy_child_ownership_backfill(
    session: AsyncSession,
) -> HevyChildOwnershipBackfillPreflightResult:
    """Validate the fixed Stage-3E graph without mutating session state."""

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=False)
        dependencies = await _load_checkpoint_group(
            session, phases=_PRIOR_PHASES, for_update=False
        )
        checkpoints = await _load_checkpoint_group(
            session, phases=_PHASE_KEYS, for_update=False
        )
        _require_own_checkpoint_group(checkpoints, scope=scope)
        blocked = any(
            checkpoint.status == "restore_blocked"
            for checkpoint in checkpoints.values()
        )
        completed_group = _completed_group(checkpoints)
        empty_completed_group = completed_group and all(
            checkpoint.snapshot_rows == 0 for checkpoint in checkpoints.values()
        )
        _require_dependencies(
            dependencies,
            scope=scope,
            restore=blocked or empty_completed_group,
        )
        summaries = await _summaries(
            session,
            scope=scope,
            checkpoints=checkpoints,
            validate_rows=not blocked,
            for_update=False,
            verify_completed=not blocked,
            volatile_completed_group=completed_group,
        )
        return _result(scope=scope, summaries=summaries)


def _validate_snapshot_bounds(
    snapshot_bounds: Mapping[str, tuple[int, int]],
) -> dict[str, tuple[int, int]]:
    if not isinstance(snapshot_bounds, Mapping) or set(snapshot_bounds) != set(
        HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES
    ):
        raise HevyChildOwnershipBackfillValidationError(
            "snapshot_bounds must contain the exact Hevy child table catalog"
        )
    result: dict[str, tuple[int, int]] = {}
    for table_name in HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES:
        pair = snapshot_bounds[table_name]
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise HevyChildOwnershipBackfillValidationError(
                "each Hevy child snapshot bound must be an exact pair"
            )
        high, count = pair
        if (
            isinstance(high, bool)
            or not isinstance(high, int)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= high <= _POSTGRES_INTEGER_MAX
            or not 0 <= count <= _POSTGRES_INTEGER_MAX
            or count > high
            or (high == 0) != (count == 0)
        ):
            raise HevyChildOwnershipBackfillValidationError(
                "Hevy child snapshot bounds are invalid PostgreSQL INTEGER pairs"
            )
        result[table_name] = pair
    return result


async def _lock_current_graph_for_restore(session: AsyncSession) -> None:
    raw_ids = sorted(
        {
            value
            for value in await session.scalars(
                select(HevyWorkout.raw_payload_id)
                .where(HevyWorkout.raw_payload_id.is_not(None))
                .distinct()
            )
            if isinstance(value, int) and not isinstance(value, bool)
        }
    )
    if raw_ids:
        list(
            await session.scalars(
                select(RawPayload.id)
                .where(RawPayload.id.in_(raw_ids))
                .order_by(RawPayload.id)
                .with_for_update()
            )
        )
    for table in (
        HevyWorkout.__table__,
        HevyExercise.__table__,
        HevySet.__table__,
    ):
        list(
            await session.scalars(
                select(table.c.id).order_by(table.c.id).with_for_update()
            )
        )


async def block_hevy_child_ownership_backfill_for_portability_v1_restore(
    session: AsyncSession,
    *,
    snapshot_bounds: Mapping[str, tuple[int, int]],
) -> None:
    """Record backup-v1's loss of Hevy child connection provenance."""

    bounds = _validate_snapshot_bounds(snapshot_bounds)
    reset_at = now_utc().replace(microsecond=0)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependencies = await _load_checkpoint_group(
            session, phases=_PRIOR_PHASES, for_update=True
        )
        _require_dependencies(dependencies, scope=scope, restore=True)
        checkpoints = await _load_checkpoint_group(
            session, phases=_PHASE_KEYS, for_update=True
        )
        _require_own_checkpoint_group(checkpoints, scope=scope)
        await _lock_current_graph_for_restore(session)
        for spec in _TABLES:
            high, count = bounds[spec.name]
            empty = (high, count) == (0, 0)
            checkpoint = checkpoints.get(spec.phase_key)
            if checkpoint is None:
                checkpoint = OwnershipBackfillCheckpoint(
                    phase_key=spec.phase_key,
                    subject_id=scope.subject_id,
                    status="completed" if empty else "restore_blocked",
                    scan_high_watermark_id=high,
                    snapshot_rows=count,
                    last_scanned_id=high if empty else 0,
                    scanned_rows=count if empty else 0,
                    updated_rows=0,
                    unchanged_rows=count if empty else 0,
                    data_checksum_before=_EMPTY_SHA256,
                    data_checksum_after=_EMPTY_SHA256,
                    ownership_checksum_after=_EMPTY_SHA256,
                    started_at=reset_at,
                    updated_at=reset_at,
                    completed_at=reset_at if empty else None,
                )
                session.add(checkpoint)
                checkpoints[spec.phase_key] = checkpoint
            checkpoint.subject_id = scope.subject_id
            checkpoint.status = "completed" if empty else "restore_blocked"
            checkpoint.scan_high_watermark_id = high
            checkpoint.snapshot_rows = count
            checkpoint.last_scanned_id = high if empty else 0
            checkpoint.scanned_rows = count if empty else 0
            checkpoint.updated_rows = 0
            checkpoint.unchanged_rows = count if empty else 0
            checkpoint.data_checksum_before = _EMPTY_SHA256
            checkpoint.data_checksum_after = _EMPTY_SHA256
            checkpoint.ownership_checksum_after = _EMPTY_SHA256
            checkpoint.started_at = reset_at
            checkpoint.updated_at = reset_at
            checkpoint.completed_at = reset_at if empty else None
        await session.flush()


async def _create_group_checkpoints(
    session: AsyncSession,
    *,
    scope: _Scope,
    summaries: list[_TableSummary],
    checkpoints: dict[str, Any],
) -> None:
    if checkpoints:
        return
    for summary in summaries:
        checkpoint = OwnershipBackfillCheckpoint(
            phase_key=summary.spec.phase_key,
            subject_id=scope.subject_id,
            status="running",
            scan_high_watermark_id=summary.high_watermark,
            snapshot_rows=summary.snapshot_rows,
            last_scanned_id=0,
            scanned_rows=0,
            updated_rows=0,
            unchanged_rows=0,
            data_checksum_before=_EMPTY_SHA256,
            data_checksum_after=_EMPTY_SHA256,
            ownership_checksum_after=_EMPTY_SHA256,
            completed_at=None,
        )
        session.add(checkpoint)
        checkpoints[summary.spec.phase_key] = checkpoint
    await session.flush()


def _expected_pair_for_row(
    *, spec: _TableSpec, row: Mapping[str, Any], graph: _Graph, scope: _Scope
) -> tuple[uuid.UUID, uuid.UUID]:
    if spec.table is HevyExercise.__table__:
        workout_id = row["workout_id"]
    else:
        exercise_row = graph.exercises.get(row["exercise_id"])
        if exercise_row is None:
            raise HevyChildOwnershipBackfillStateError(
                "a Hevy set references a missing exercise"
            )
        workout_id = _row_mapping(HevyExercise.__table__, exercise_row)[
            "workout_id"
        ]
    return _validate_workout_graph(graph, workout_id=workout_id, scope=scope)


async def run_hevy_child_ownership_backfill_batch(
    session: AsyncSession,
    *,
    batch_size: int,
) -> HevyChildOwnershipBackfillBatchResult:
    """Advance the first incomplete fixed Hevy child table by one PK batch."""

    size = _validate_batch_size(batch_size)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependencies = await _load_checkpoint_group(
            session, phases=_PRIOR_PHASES, for_update=True
        )
        _require_dependencies(dependencies, scope=scope, restore=False)
        checkpoints = await _load_checkpoint_group(
            session, phases=_PHASE_KEYS, for_update=True
        )
        _require_own_checkpoint_group(checkpoints, scope=scope)
        if any(
            checkpoint.status == "restore_blocked"
            for checkpoint in checkpoints.values()
        ):
            raise HevyChildOwnershipBackfillStateError(
                "Hevy child ownership is blocked by backup-v1 restore"
            )
        completed_group = _completed_group(checkpoints)
        summaries = await _summaries(
            session,
            scope=scope,
            checkpoints=checkpoints,
            validate_rows=False,
            for_update=False,
            verify_completed=False,
            volatile_completed_group=completed_group,
        )
        if completed_group:
            checked = await _summaries(
                session,
                scope=scope,
                checkpoints=checkpoints,
                validate_rows=True,
                for_update=True,
                verify_completed=False,
                volatile_completed_group=True,
            )
            return _batch_result(
                _result(scope=scope, summaries=checked),
                table=_TABLES[-1].name,
                scanned=0,
                updated=0,
                unchanged=0,
            )
        await _create_group_checkpoints(
            session,
            scope=scope,
            summaries=summaries,
            checkpoints=checkpoints,
        )
        # Refresh summaries against the now-frozen two-table bounds.
        summaries = await _summaries(
            session,
            scope=scope,
            checkpoints=checkpoints,
            validate_rows=False,
            for_update=False,
            verify_completed=False,
        )
        exercise_high = checkpoints[_EXERCISE_PHASE].scan_high_watermark_id
        exercise_completed = checkpoints[_EXERCISE_PHASE].status == "completed"
        for summary in summaries:
            if summary.rows_above:
                await _scan_table(
                    session,
                    spec=summary.spec,
                    scope=scope,
                    high_watermark=summary.high_watermark,
                    exercise_high_watermark=exercise_high,
                    checkpoint_cursor=None,
                    for_update=True,
                    require_exact_exercise=(
                        summary.spec.table is HevySet.__table__
                        and exercise_completed
                    ),
                    start_after=summary.high_watermark,
                )
        target = next(
            summary
            for summary in summaries
            if summary.checkpoint.status != "completed"
        )
        if target.spec.table is HevySet.__table__ and not exercise_completed:
            raise HevyChildOwnershipBackfillStateError(
                "Hevy set phase cannot precede exercise completion"
            )
        checkpoint = target.checkpoint
        batch_ids = list(
            await session.scalars(
                select(target.spec.table.c.id)
                .where(
                    target.spec.table.c.id > checkpoint.last_scanned_id,
                    target.spec.table.c.id <= checkpoint.scan_high_watermark_id,
                )
                .order_by(target.spec.table.c.id)
                .limit(size)
            )
        )
        graph = await _lock_graph_for_ids(
            session, spec=target.spec, child_ids=batch_ids
        )

    before = checkpoint.data_checksum_before
    after = checkpoint.data_checksum_after
    ownership = checkpoint.ownership_checksum_after
    updated_rows = 0
    unchanged_rows = 0
    for row_id in batch_ids:
        row = await _load_full_child(
            session, spec=target.spec, row_id=row_id, for_update=True
        )
        if (row._mapping["id"], row._mapping[target.spec.parent_fk]) != graph.children[
            row_id
        ]:
            raise HevyChildOwnershipBackfillStateError(
                "a Hevy child parent link changed after graph locking"
            )
        changed = _validate_row(
            spec=target.spec,
            row=row._mapping,
            graph=graph,
            scope=scope,
            high_watermark=checkpoint.scan_high_watermark_id,
            exercise_high_watermark=exercise_high,
            require_exact_exercise=target.spec.table is HevySet.__table__,
        )
        before = _extend_checksum(before, _data_envelope(target.spec, row))
        if changed:
            expected = _expected_pair_for_row(
                spec=target.spec, row=row._mapping, graph=graph, scope=scope
            )
            values: dict[str, Any] = {
                "subject_id": expected[0],
                "integration_connection_id": expected[1],
            }
            if "updated_at" in target.spec.table.c:
                values["updated_at"] = row._mapping["updated_at"]
            model = (
                HevyExercise
                if target.spec.table is HevyExercise.__table__
                else HevySet
            )
            await session.execute(
                update(model)
                .where(model.id == row_id)
                .values(**values)
            )
            await session.get(
                model,
                row_id,
                populate_existing=True,
                with_for_update=True,
            )
            updated_rows += 1
        else:
            unchanged_rows += 1
        current = await _load_full_child(
            session, spec=target.spec, row_id=row_id, for_update=True
        )
        if (current._mapping["id"], current._mapping[target.spec.parent_fk]) != (
            graph.children[row_id]
        ):
            raise HevyChildOwnershipBackfillStateError(
                "a Hevy child parent link changed during ownership update"
            )
        if _validate_row(
            spec=target.spec,
            row=current._mapping,
            graph=graph,
            scope=scope,
            high_watermark=checkpoint.scan_high_watermark_id,
            exercise_high_watermark=exercise_high,
            require_exact_exercise=target.spec.table is HevySet.__table__,
        ):
            raise HevyChildOwnershipBackfillStateError(
                "a Hevy child remained unowned after update"
            )
        after = _extend_checksum(after, _data_envelope(target.spec, current))
        ownership = _extend_checksum(
            ownership, _ownership_envelope(target.spec, current)
        )
    if before != after:
        raise HevyChildOwnershipBackfillStateError(
            "Hevy child data changed while ownership was backfilled"
        )
    checkpoint.scanned_rows += len(batch_ids)
    checkpoint.updated_rows += updated_rows
    checkpoint.unchanged_rows += unchanged_rows
    checkpoint.data_checksum_before = before
    checkpoint.data_checksum_after = after
    checkpoint.ownership_checksum_after = ownership
    if batch_ids:
        checkpoint.last_scanned_id = batch_ids[-1]
    remaining = await _remaining(
        session,
        target.spec,
        high_watermark=checkpoint.scan_high_watermark_id,
        cursor=checkpoint.last_scanned_id,
    )
    if remaining == 0:
        await session.flush()
        await _validate_counts(session, spec=target.spec, checkpoint=checkpoint)
        await _verify_final_snapshot(
            session,
            spec=target.spec,
            scope=scope,
            checkpoint=checkpoint,
            exercise_high_watermark=exercise_high,
            require_data_checksum=True,
        )
        checkpoint.last_scanned_id = checkpoint.scan_high_watermark_id
        checkpoint.status = "completed"
        checkpoint.completed_at = now_utc()
    await session.flush()

    # Timestamp ``onupdate`` values are server-populated and therefore expired by
    # the flush.  Reload under the already-held checkpoint locks before the
    # synchronous shape validator reads them; this also defeats a stale identity
    # map supplied by the caller.
    checkpoints = await _load_checkpoint_group(
        session, phases=_PHASE_KEYS, for_update=True
    )

    refreshed = await _summaries(
        session,
        scope=scope,
        checkpoints=checkpoints,
        validate_rows=False,
        for_update=False,
        verify_completed=False,
    )
    aggregate = _result(scope=scope, summaries=refreshed)
    if aggregate.completed:
        checked = await _summaries(
            session,
            scope=scope,
            checkpoints=checkpoints,
            validate_rows=True,
            for_update=True,
            verify_completed=True,
            require_completed_data_checksum=True,
        )
        aggregate = _result(scope=scope, summaries=checked)
    return _batch_result(
        aggregate,
        table=target.spec.name,
        scanned=len(batch_ids),
        updated=updated_rows,
        unchanged=unchanged_rows,
    )


__all__ = [
    "DEFAULT_HEVY_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES",
    "HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE",
    "HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES",
    "MAX_HEVY_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "HevyChildOwnershipBackfillBatchResult",
    "HevyChildOwnershipBackfillDependencyError",
    "HevyChildOwnershipBackfillError",
    "HevyChildOwnershipBackfillIdentityError",
    "HevyChildOwnershipBackfillPreflightResult",
    "HevyChildOwnershipBackfillProvenanceError",
    "HevyChildOwnershipBackfillStateError",
    "HevyChildOwnershipBackfillStatus",
    "HevyChildOwnershipBackfillValidationError",
    "block_hevy_child_ownership_backfill_for_portability_v1_restore",
    "preflight_hevy_child_ownership_backfill",
    "run_hevy_child_ownership_backfill_batch",
]
