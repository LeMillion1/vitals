"""Bounded ownership backfill for independent normalized manual/MCP tables.

This Stage-3B phase deliberately excludes raw/provider/file roots, mixed-source
tables, generated artifacts, settings, and child mutation.  Two HRT parent
tables are included only with read-only child/compound graph gates.  It owns no
transaction boundary: mutation advances one fixed table by at most one batch,
flushes, and leaves commit/rollback to the operator boundary.
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
from vitals.models.glp1 import DosePhase, Injection, SideEffect
from vitals.models.hrt import (
    HrtCompound,
    HrtCycle,
    HrtCycleItem,
    HrtCycleTemplate,
    HrtCycleTemplateItem,
    HrtDose,
    HrtSideEffect,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.labs import LabMarker
from vitals.models.milestones import Milestone
from vitals.models.nutrition import MealLog
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.skincare import (
    SkincareLog,
    SkincareObservation,
    SkincareProduct,
)
from vitals.models.supplements import Supplement
from vitals.models.tenancy import IntegrationConnection
from vitals.models.timeline import Annotation
from vitals.models.weight import BodyMeasurement, NoiseMarker
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.raw_ownership_backfill_service import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.utils.timeutils import now_utc

NORMALIZED_MANUAL_BACKFILL_PHASE = "stage3.normalized_manual.v1"
DEFAULT_NORMALIZED_OWNERSHIP_BACKFILL_BATCH_SIZE = 250
MAX_NORMALIZED_OWNERSHIP_BACKFILL_BATCH_SIZE = 1000

_PREFLIGHT_PAGE_SIZE = 1000
_FULL_ROW_MATERIALIZATION_SIZE = 1
_SIGNED_BIGINT_MAX = (1 << 63) - 1
_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KNOWN_DOMAINS = frozenset(item.value for item in Domain)
_USER_DOMAINS = _KNOWN_DOMAINS - {Domain.SYSTEM.value}
_MANUAL_SOURCES = frozenset({Source.MANUAL.value, Source.MCP.value})
_KNOWN_CONNECTION_STATUSES = frozenset(
    item.value for item in IntegrationConnectionStatus
)
_KNOWN_CONNECTION_TYPES = frozenset(
    item.value for item in IntegrationConnectionType
)
_KNOWN_PROVIDERS = frozenset(item.value for item in IntegrationProvider)


class NormalizedOwnershipBackfillStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"


class NormalizedOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed Stage-3B errors."""


class NormalizedOwnershipBackfillValidationError(
    NormalizedOwnershipBackfillError, ValueError
):
    """A caller argument or persisted scalar is unsafe."""


class NormalizedOwnershipBackfillIdentityError(NormalizedOwnershipBackfillError):
    """The canonical sole-subject/active-owner graph is unavailable."""


class NormalizedOwnershipBackfillDependencyError(
    NormalizedOwnershipBackfillError
):
    """The exact completed Stage-3A prerequisite is unavailable."""


class NormalizedOwnershipBackfillStateError(NormalizedOwnershipBackfillError):
    """Checkpoint progress or an ownership shape is inconsistent."""


class NormalizedOwnershipBackfillProvenanceError(
    NormalizedOwnershipBackfillError
):
    """A reviewed table contains an unexpected domain/source value."""


@dataclass(frozen=True, slots=True)
class NormalizedOwnershipBackfillPreflightResult:
    phase_key: str
    subject_id: uuid.UUID
    status: NormalizedOwnershipBackfillStatus
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
        return self.status is NormalizedOwnershipBackfillStatus.COMPLETED

    def to_safe_dict(self) -> dict[str, str | int]:
        """Return control-plane data only, without subject/row/cursor IDs."""

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
class NormalizedOwnershipBackfillBatchResult(
    NormalizedOwnershipBackfillPreflightResult
):
    batch_table: str
    batch_scanned_rows: int
    batch_updated_rows: int
    batch_unchanged_rows: int

    @property
    def changed(self) -> bool:
        return self.batch_updated_rows > 0

    def to_safe_dict(self) -> dict[str, str | int]:
        projection = NormalizedOwnershipBackfillPreflightResult.to_safe_dict(self)
        projection.update(
            {
                "batch_table": self.batch_table,
                "batch_scanned_rows": self.batch_scanned_rows,
                "batch_updated_rows": self.batch_updated_rows,
                "batch_unchanged_rows": self.batch_unchanged_rows,
            }
        )
        return projection


@dataclass(frozen=True, slots=True)
class _TableSpec:
    table: Table
    allowed_domains: frozenset[str] | None
    allowed_sources: frozenset[str] | None
    allow_actorless_live: bool = False

    @property
    def name(self) -> str:
        return self.table.name

    @property
    def phase_key(self) -> str:
        return f"{NORMALIZED_MANUAL_BACKFILL_PHASE}.{self.name}"


def _spec(
    model: Any,
    *,
    domains: frozenset[str] | None = None,
    sources: frozenset[str] | None = None,
    allow_actorless_live: bool = False,
) -> _TableSpec:
    return _TableSpec(
        table=model.__table__,
        allowed_domains=domains,
        allowed_sources=sources,
        allow_actorless_live=allow_actorless_live,
    )


# Order is a durable operator contract.  The two HRT parents are followed by
# their read-only graph gates before independent facts are processed.
_NORMALIZED_TABLES: tuple[_TableSpec, ...] = (
    _spec(
        HrtCycle,
        domains=frozenset({Domain.HRT.value}),
        sources=_MANUAL_SOURCES,
    ),
    _spec(
        HrtCycleTemplate,
        domains=frozenset({Domain.HRT.value}),
        sources=_MANUAL_SOURCES,
    ),
    _spec(Annotation, domains=_USER_DOMAINS, sources=_MANUAL_SOURCES),
    _spec(
        BodyMeasurement,
        domains=frozenset({Domain.WEIGHT.value}),
        sources=_MANUAL_SOURCES,
    ),
    _spec(
        DosePhase,
        domains=frozenset({Domain.GLP1.value}),
        sources=_MANUAL_SOURCES,
    ),
    _spec(
        Injection,
        domains=frozenset({Domain.GLP1.value}),
        sources=_MANUAL_SOURCES,
    ),
    _spec(
        SideEffect,
        domains=frozenset({Domain.GLP1.value}),
        sources=_MANUAL_SOURCES,
    ),
    _spec(
        HrtDose,
        domains=frozenset({Domain.HRT.value}),
        sources=_MANUAL_SOURCES,
    ),
    _spec(
        HrtSideEffect,
        domains=frozenset({Domain.HRT.value}),
        sources=_MANUAL_SOURCES,
    ),
    _spec(
        LabMarker,
        domains=frozenset({Domain.LABS.value}),
        allow_actorless_live=True,
    ),
    _spec(
        MealLog,
        domains=frozenset({Domain.NUTRITION.value}),
        sources=_MANUAL_SOURCES,
    ),
    _spec(Milestone, domains=_USER_DOMAINS),
    _spec(
        NoiseMarker,
        domains=frozenset({Domain.WEIGHT.value}),
        sources=_MANUAL_SOURCES,
    ),
    _spec(
        SkincareLog,
        domains=frozenset({Domain.SKINCARE.value}),
        sources=_MANUAL_SOURCES,
    ),
    _spec(
        SkincareObservation,
        domains=frozenset({Domain.SKINCARE.value}),
        sources=_MANUAL_SOURCES,
    ),
    _spec(SkincareProduct),
    _spec(
        Supplement,
        domains=frozenset({Domain.SUPPLEMENTS.value}),
        sources=_MANUAL_SOURCES,
    ),
)
NORMALIZED_MANUAL_TABLES = tuple(spec.name for spec in _NORMALIZED_TABLES)
# Compatibility alias for the already-frozen operator implementation.
NORMALIZED_OWNERSHIP_BACKFILL_TABLES = NORMALIZED_MANUAL_TABLES
_PHASE_KEYS = tuple(spec.phase_key for spec in _NORMALIZED_TABLES)
NORMALIZED_MANUAL_CHECKPOINT_PHASES: Mapping[str, str] = MappingProxyType(
    {spec.name: spec.phase_key for spec in _NORMALIZED_TABLES}
)


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
    completed_at: Any


@dataclass(frozen=True, slots=True)
class _TableSummary:
    spec: _TableSpec
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection | None
    high_watermark: int
    snapshot_rows: int
    remaining_rows: int
    rows_above: int


def _validate_batch_size(batch_size: object) -> int:
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= MAX_NORMALIZED_OWNERSHIP_BACKFILL_BATCH_SIZE
    ):
        raise NormalizedOwnershipBackfillValidationError(
            "batch_size must be an integer between 1 and "
            f"{MAX_NORMALIZED_OWNERSHIP_BACKFILL_BATCH_SIZE}"
        )
    return batch_size


def _valid_counter(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 <= value <= _SIGNED_BIGINT_MAX
    )


async def _load_scope(session: AsyncSession, *, for_update: bool) -> _Scope:
    if for_update:
        await acquire_identity_governance_lock(session)
        subjects = list(
            await session.scalars(
                select(HealthSubject)
                .order_by(HealthSubject.id)
                .limit(2)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(subjects) != 1:
            raise NormalizedOwnershipBackfillIdentityError(
                "normalized ownership backfill requires exactly one health subject"
            )
        subject_id = subjects[0].id
        owner_user_id = subjects[0].owner_user_id
        owner = await session.scalar(
            select(User)
            .where(User.id == owner_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        owner_status = owner.status if owner is not None else None
        connections: list[Any] = list(
            await session.scalars(
                select(IntegrationConnection)
                .order_by(IntegrationConnection.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
    else:
        subjects = list(
            await session.execute(
                select(HealthSubject.id, HealthSubject.owner_user_id)
                .order_by(HealthSubject.id)
                .limit(2)
            )
        )
        if len(subjects) != 1:
            raise NormalizedOwnershipBackfillIdentityError(
                "normalized ownership backfill requires exactly one health subject"
            )
        subject_id, owner_user_id = subjects[0]
        owner_row = (
            await session.execute(
                select(User.id, User.status).where(User.id == owner_user_id)
            )
        ).one_or_none()
        owner_status = owner_row.status if owner_row is not None else None
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
        raise NormalizedOwnershipBackfillIdentityError(
            "the sole health subject must have an active owner"
        )
    for connection in connections:
        if connection.subject_id != subject_id:
            raise NormalizedOwnershipBackfillIdentityError(
                "an integration connection belongs to a foreign subject"
            )
        if (
            connection.provider not in _KNOWN_PROVIDERS
            or connection.connection_type not in _KNOWN_CONNECTION_TYPES
            or connection.status not in _KNOWN_CONNECTION_STATUSES
        ):
            raise NormalizedOwnershipBackfillIdentityError(
                "an integration connection has an unknown persisted mapping"
            )
    return _Scope(subject_id=subject_id, owner_user_id=owner_user_id)


def _checkpoint_projection(row: Any) -> _CheckpointProjection:
    return _CheckpointProjection(*row)


async def _load_dependency_checkpoint(
    session: AsyncSession, *, for_update: bool
) -> OwnershipBackfillCheckpoint | _CheckpointProjection | None:
    if for_update:
        return await session.scalar(
            select(OwnershipBackfillCheckpoint)
            .where(
                OwnershipBackfillCheckpoint.phase_key
                == RAW_OWNERSHIP_BACKFILL_PHASE
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    row = (
        await session.execute(
            _checkpoint_projection_select().where(
                OwnershipBackfillCheckpoint.phase_key
                == RAW_OWNERSHIP_BACKFILL_PHASE
            )
        )
    ).one_or_none()
    return _checkpoint_projection(row) if row is not None else None


def _checkpoint_projection_select():
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
        OwnershipBackfillCheckpoint.completed_at,
    )


def _validate_checkpoint_shape(
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection,
    *,
    subject_id: uuid.UUID,
    expected_phase: str,
    dependency: bool = False,
) -> NormalizedOwnershipBackfillStatus:
    error_type = (
        NormalizedOwnershipBackfillDependencyError
        if dependency
        else NormalizedOwnershipBackfillStateError
    )
    label = "raw ownership prerequisite" if dependency else "normalized checkpoint"
    if checkpoint.phase_key != expected_phase:
        raise error_type(f"{label} has an unexpected phase")
    if checkpoint.subject_id != subject_id:
        raise error_type(f"{label} belongs to another subject")
    if checkpoint.status == "restore_blocked":
        if dependency:
            raise error_type("raw ownership prerequisite is restore-blocked")
        raise error_type("normalized checkpoint has an unsupported status")
    try:
        status = NormalizedOwnershipBackfillStatus(checkpoint.status)
    except ValueError as exc:
        raise error_type(f"{label} has an unknown status") from exc
    if status is NormalizedOwnershipBackfillStatus.NOT_STARTED:
        raise error_type("not_started is not a persisted checkpoint status")
    counters = (
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
    )
    if not all(_valid_counter(value) for value in counters):
        raise error_type(f"{label} has invalid counters")
    if checkpoint.last_scanned_id > checkpoint.scan_high_watermark_id:
        raise error_type(f"{label} cursor exceeds its high-water mark")
    if checkpoint.snapshot_rows > checkpoint.scan_high_watermark_id:
        raise error_type(f"{label} snapshot exceeds its high-water mark")
    if checkpoint.scanned_rows > checkpoint.snapshot_rows:
        raise error_type(f"{label} scanned count exceeds its snapshot")
    if checkpoint.scanned_rows != checkpoint.updated_rows + checkpoint.unchanged_rows:
        raise error_type(f"{label} counters do not balance")
    digests = (
        checkpoint.data_checksum_before,
        checkpoint.data_checksum_after,
        checkpoint.ownership_checksum_after,
    )
    if any(
        not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
        for value in digests
    ):
        raise error_type(f"{label} has an invalid SHA-256 digest")
    if checkpoint.data_checksum_before != checkpoint.data_checksum_after:
        raise error_type(f"{label} data checksums differ")
    if status is NormalizedOwnershipBackfillStatus.COMPLETED:
        if (
            checkpoint.completed_at is None
            or checkpoint.last_scanned_id != checkpoint.scan_high_watermark_id
            or checkpoint.scanned_rows != checkpoint.snapshot_rows
        ):
            raise error_type(f"completed {label} is incomplete")
    elif checkpoint.completed_at is not None:
        raise error_type(f"running {label} has a completion timestamp")
    return status


def _require_completed_dependency(
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection | None,
    *,
    subject_id: uuid.UUID,
) -> None:
    if checkpoint is None:
        raise NormalizedOwnershipBackfillDependencyError(
            "completed raw ownership prerequisite is required"
        )
    status = _validate_raw_dependency(
        checkpoint,
        subject_id=subject_id,
        allow_restore_blocked=False,
    )
    if status != "completed":
        raise NormalizedOwnershipBackfillDependencyError(
            "raw ownership prerequisite is not completed"
        )


def _validate_raw_dependency(
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection,
    *,
    subject_id: uuid.UUID,
    allow_restore_blocked: bool,
) -> str:
    if checkpoint.phase_key != RAW_OWNERSHIP_BACKFILL_PHASE:
        raise NormalizedOwnershipBackfillDependencyError(
            "raw ownership prerequisite has an unexpected phase"
        )
    if checkpoint.subject_id != subject_id:
        raise NormalizedOwnershipBackfillDependencyError(
            "raw ownership prerequisite belongs to another subject"
        )
    if checkpoint.status not in {"running", "completed", "restore_blocked"}:
        raise NormalizedOwnershipBackfillDependencyError(
            "raw ownership prerequisite has an unknown status"
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
        raise NormalizedOwnershipBackfillDependencyError(
            "raw ownership prerequisite has invalid counters"
        )
    if (
        checkpoint.last_scanned_id > checkpoint.scan_high_watermark_id
        or checkpoint.snapshot_rows > checkpoint.scan_high_watermark_id
        or checkpoint.scanned_rows > checkpoint.snapshot_rows
        or checkpoint.scanned_rows
        != checkpoint.updated_rows + checkpoint.unchanged_rows
    ):
        raise NormalizedOwnershipBackfillDependencyError(
            "raw ownership prerequisite has inconsistent progress"
        )
    digests = (
        checkpoint.data_checksum_before,
        checkpoint.data_checksum_after,
        checkpoint.ownership_checksum_after,
    )
    if any(
        not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
        for value in digests
    ) or checkpoint.data_checksum_before != checkpoint.data_checksum_after:
        raise NormalizedOwnershipBackfillDependencyError(
            "raw ownership prerequisite has invalid checksums"
        )
    if checkpoint.status == "completed":
        if (
            checkpoint.completed_at is None
            or checkpoint.last_scanned_id != checkpoint.scan_high_watermark_id
            or checkpoint.scanned_rows != checkpoint.snapshot_rows
        ):
            raise NormalizedOwnershipBackfillDependencyError(
                "completed raw ownership prerequisite is incomplete"
            )
        return checkpoint.status
    if checkpoint.status == "restore_blocked":
        if not allow_restore_blocked:
            raise NormalizedOwnershipBackfillDependencyError(
                "raw ownership prerequisite is restore-blocked"
            )
        if (
            checkpoint.completed_at is not None
            or checkpoint.last_scanned_id != 0
            or checkpoint.scanned_rows != 0
            or checkpoint.updated_rows != 0
            or checkpoint.unchanged_rows != 0
            or any(value != _EMPTY_SHA256 for value in digests)
        ):
            raise NormalizedOwnershipBackfillDependencyError(
                "restore-blocked raw ownership prerequisite is inconsistent"
            )
        return checkpoint.status
    if checkpoint.completed_at is not None:
        raise NormalizedOwnershipBackfillDependencyError(
            "running raw ownership prerequisite has a completion timestamp"
        )
    return checkpoint.status


async def _load_table_checkpoints(
    session: AsyncSession, *, for_update: bool
) -> dict[str, OwnershipBackfillCheckpoint | _CheckpointProjection]:
    if for_update:
        rows = list(
            await session.scalars(
                select(OwnershipBackfillCheckpoint)
                .where(OwnershipBackfillCheckpoint.phase_key.in_(_PHASE_KEYS))
                .order_by(OwnershipBackfillCheckpoint.phase_key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        return {row.phase_key: row for row in rows}
    rows = list(
        await session.execute(
            _checkpoint_projection_select()
            .where(OwnershipBackfillCheckpoint.phase_key.in_(_PHASE_KEYS))
            .order_by(OwnershipBackfillCheckpoint.phase_key)
        )
    )
    return {
        row.phase_key: _checkpoint_projection(row)
        for row in rows
    }


async def _max_id(session: AsyncSession, spec: _TableSpec) -> int:
    value = await session.scalar(select(func.max(spec.table.c.id)))
    if value is None:
        return 0
    if not _valid_counter(value):
        raise NormalizedOwnershipBackfillStateError(
            "a reviewed table has an invalid primary key"
        )
    return value


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


async def _remaining_rows(
    session: AsyncSession,
    spec: _TableSpec,
    *,
    high_watermark: int,
    last_scanned: int,
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(spec.table)
            .where(
                spec.table.c.id > last_scanned,
                spec.table.c.id <= high_watermark,
            )
        )
        or 0
    )


def _validate_provenance(spec: _TableSpec, row: Any) -> None:
    if (
        spec.allowed_domains is not None
        and row.domain not in spec.allowed_domains
    ):
        raise NormalizedOwnershipBackfillProvenanceError(
            "a reviewed table row has an unexpected domain"
        )
    if (
        spec.allowed_sources is not None
        and row.source not in spec.allowed_sources
    ):
        raise NormalizedOwnershipBackfillProvenanceError(
            "a reviewed table row has an unexpected source"
        )


def _classify_ownership(
    row: Any,
    *,
    spec: _TableSpec,
    scope: _Scope,
    high_watermark: int,
) -> bool:
    historical = row.id <= high_watermark
    subject_id = row.subject_id
    actor_user_id = row.actor_user_id
    if historical:
        if subject_id is None and actor_user_id is None:
            return True
        if subject_id == scope.subject_id and actor_user_id in (
            None,
            scope.owner_user_id,
        ):
            return False
        if subject_id is None or actor_user_id is None:
            raise NormalizedOwnershipBackfillStateError(
                "a historical row has partial ownership"
            )
        raise NormalizedOwnershipBackfillStateError(
            "a historical row has foreign ownership"
        )
    valid_live_actor = actor_user_id == scope.owner_user_id or (
        spec.allow_actorless_live and actor_user_id is None
    )
    if subject_id != scope.subject_id or not valid_live_actor:
        raise NormalizedOwnershipBackfillStateError(
            "a row above the high-water mark lacks exact live ownership"
        )
    return False


async def _scan_table(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    scope: _Scope,
    high_watermark: int,
    checkpoint_cursor: int | None,
    for_update: bool,
    start_after: int = 0,
) -> int:
    columns = [
        spec.table.c.id,
        spec.table.c.subject_id,
        spec.table.c.actor_user_id,
    ]
    if spec.allowed_domains is not None:
        columns.append(spec.table.c.domain)
    if spec.allowed_sources is not None:
        columns.append(spec.table.c.source)
    last_id = start_after
    rows_above = 0
    while True:
        stmt = (
            select(*columns)
            .where(spec.table.c.id > last_id)
            .order_by(spec.table.c.id)
            .limit(_PREFLIGHT_PAGE_SIZE)
        )
        if for_update:
            stmt = stmt.with_for_update()
        rows = list(await session.execute(stmt))
        if not rows:
            break
        for row in rows:
            _validate_provenance(spec, row)
            changed = _classify_ownership(
                row,
                spec=spec,
                scope=scope,
                high_watermark=high_watermark,
            )
            if (
                checkpoint_cursor is not None
                and row.id <= checkpoint_cursor
                and changed
            ):
                raise NormalizedOwnershipBackfillStateError(
                    "a previously scanned row requires ownership repair"
                )
            if row.id > high_watermark:
                rows_above += 1
        last_id = rows[-1].id
        if len(rows) < _PREFLIGHT_PAGE_SIZE:
            break
    return rows_above


async def _count_rows_above(
    session: AsyncSession,
    spec: _TableSpec,
    *,
    high_watermark: int,
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(spec.table)
            .where(spec.table.c.id > high_watermark)
        )
        or 0
    )


async def _validate_checkpoint_counts(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection,
) -> int:
    snapshot_rows = await _count_to(
        session,
        spec,
        high_watermark=checkpoint.scan_high_watermark_id,
    )
    prefix_rows = await _count_to(
        session,
        spec,
        high_watermark=checkpoint.last_scanned_id,
    )
    remaining = await _remaining_rows(
        session,
        spec,
        high_watermark=checkpoint.scan_high_watermark_id,
        last_scanned=checkpoint.last_scanned_id,
    )
    if snapshot_rows != checkpoint.snapshot_rows:
        raise NormalizedOwnershipBackfillStateError(
            "a normalized checkpoint snapshot count drifted"
        )
    if prefix_rows != checkpoint.scanned_rows:
        raise NormalizedOwnershipBackfillStateError(
            "a normalized checkpoint prefix count drifted"
        )
    if checkpoint.scanned_rows + remaining != checkpoint.snapshot_rows:
        raise NormalizedOwnershipBackfillStateError(
            "a normalized checkpoint no longer matches its snapshot"
        )
    return remaining


async def _reject_future_key_duplicates(
    session: AsyncSession,
    *,
    table_name: str | None = None,
) -> None:
    gates = (
        (SkincareLog.__table__, SkincareLog.__table__.c.date),
        (BodyMeasurement.__table__, BodyMeasurement.__table__.c.date),
        (LabMarker.__table__, LabMarker.__table__.c.name),
    )
    for table, key_column in gates:
        if table_name is not None and table.name != table_name:
            continue
        duplicate = (
            await session.execute(
                select(func.count())
                .select_from(table)
                .group_by(key_column)
                .having(func.count() > 1)
                .limit(1)
            )
        ).first()
        if duplicate is not None:
            raise NormalizedOwnershipBackfillStateError(
                "a reviewed table has a duplicate future scoped key"
            )


async def _validate_child_subjects(
    session: AsyncSession,
    *,
    table: Table,
    scope: _Scope,
    for_update: bool,
) -> None:
    last_id = 0
    while True:
        stmt = (
            select(table.c.id, table.c.subject_id)
            .where(table.c.id > last_id)
            .order_by(table.c.id)
            .limit(_PREFLIGHT_PAGE_SIZE)
        )
        if for_update:
            stmt = stmt.with_for_update()
        rows = list(await session.execute(stmt))
        if not rows:
            return
        for row in rows:
            if row.subject_id not in (None, scope.subject_id):
                raise NormalizedOwnershipBackfillStateError(
                    "an HRT child row belongs to a foreign subject"
                )
        last_id = rows[-1].id
        if len(rows) < _PREFLIGHT_PAGE_SIZE:
            return


async def _validate_hrt_compound_parents(
    session: AsyncSession,
    *,
    scope: _Scope,
    for_update: bool,
) -> None:
    compound_ids = list(
        await session.scalars(
            select(HrtDose.compound_id)
            .where(HrtDose.compound_id.is_not(None))
            .distinct()
            .order_by(HrtDose.compound_id)
        )
    )
    for compound_id in compound_ids:
        stmt = (
            select(
                HrtCompound.id,
                HrtCompound.domain,
                HrtCompound.source,
                HrtCompound.subject_id,
                HrtCompound.actor_user_id,
            )
            .where(HrtCompound.id == compound_id)
            .limit(1)
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = (await session.execute(stmt)).one_or_none()
        if row is None:
            raise NormalizedOwnershipBackfillStateError(
                "an HRT dose references a missing compound"
            )
        if row.domain != Domain.HRT.value:
            raise NormalizedOwnershipBackfillProvenanceError(
                "an HRT dose compound has an unexpected domain"
            )
        if row.source == Source.SYSTEM.value:
            if row.subject_id is not None or row.actor_user_id is not None:
                raise NormalizedOwnershipBackfillStateError(
                    "a system HRT compound must remain globally owned"
                )
            continue
        if row.source not in _MANUAL_SOURCES:
            raise NormalizedOwnershipBackfillProvenanceError(
                "an HRT dose compound has an unexpected source"
            )
        if row.subject_id is None and row.actor_user_id is None:
            continue
        if row.subject_id == scope.subject_id and row.actor_user_id in (
            None,
            scope.owner_user_id,
        ):
            continue
        if row.subject_id is None or row.actor_user_id is None:
            raise NormalizedOwnershipBackfillStateError(
                "an HRT dose compound has partial ownership"
            )
        raise NormalizedOwnershipBackfillStateError(
            "an HRT dose compound has foreign ownership"
        )


async def _validate_cross_table_gates(
    session: AsyncSession,
    *,
    scope: _Scope,
    for_update: bool,
) -> None:
    await _reject_future_key_duplicates(session)
    await _validate_child_subjects(
        session,
        table=HrtCycleItem.__table__,
        scope=scope,
        for_update=for_update,
    )
    await _validate_child_subjects(
        session,
        table=HrtCycleTemplateItem.__table__,
        scope=scope,
        for_update=for_update,
    )
    await _validate_hrt_compound_parents(
        session,
        scope=scope,
        for_update=for_update,
    )


async def _validate_target_cross_table_gates(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    scope: _Scope,
) -> None:
    """Validate only graph/key gates owned by the table advanced this batch."""

    if spec.name in {"skincare_logs", "body_measurements", "lab_markers"}:
        await _reject_future_key_duplicates(session, table_name=spec.name)
    if spec.name == "hrt_cycles":
        await _validate_child_subjects(
            session,
            table=HrtCycleItem.__table__,
            scope=scope,
            for_update=False,
        )
    elif spec.name == "hrt_cycle_templates":
        await _validate_child_subjects(
            session,
            table=HrtCycleTemplateItem.__table__,
            scope=scope,
            for_update=False,
        )
    elif spec.name == "hrt_doses":
        await _validate_hrt_compound_parents(
            session,
            scope=scope,
            for_update=False,
        )


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NormalizedOwnershipBackfillStateError(
                "a reviewed row contains a non-finite number"
            )
        return ["float", value.hex()]
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    if isinstance(value, uuid.UUID):
        return ["uuid", str(value)]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat(timespec="microseconds")]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, time):
        return ["time", value.isoformat(timespec="microseconds")]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, (list, tuple)):
        return ["list", [_canonical_value(item) for item in value]]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise NormalizedOwnershipBackfillStateError(
                "a reviewed row has a non-string JSON key"
            )
        return [
            "object",
            [[key, _canonical_value(value[key])] for key in sorted(value)],
        ]
    raise NormalizedOwnershipBackfillStateError(
        "a reviewed row cannot be represented canonically"
    )


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NormalizedOwnershipBackfillStateError(
            "a reviewed row cannot be represented canonically"
        ) from exc


def _extend_checksum(previous: str, envelope: Any) -> str:
    if _SHA256_RE.fullmatch(previous) is None:
        raise NormalizedOwnershipBackfillStateError(
            "rolling checksum state is invalid"
        )
    encoded = _canonical_json(envelope)
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
            if column.name not in {"subject_id", "actor_user_id"}
        ],
    ]


def _ownership_envelope(spec: _TableSpec, row: Any) -> list[Any]:
    return [
        spec.name,
        row._mapping["id"],
        str(row._mapping["subject_id"])
        if row._mapping["subject_id"] is not None
        else None,
        str(row._mapping["actor_user_id"])
        if row._mapping["actor_user_id"] is not None
        else None,
    ]


async def _load_full_row(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    row_id: int,
    for_update: bool,
) -> Any:
    stmt = (
        select(spec.table)
        .where(spec.table.c.id == row_id)
        .limit(_FULL_ROW_MATERIALIZATION_SIZE)
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise NormalizedOwnershipBackfillStateError(
            "a locked reviewed row disappeared before processing"
        )
    return row


async def _verify_final_snapshot_checksums(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection,
    for_update: bool,
    require_data_checksum: bool = True,
) -> None:
    data_checksum = _EMPTY_SHA256
    ownership_checksum = _EMPTY_SHA256
    scanned = 0
    last_id = 0
    while True:
        id_stmt = (
            select(spec.table.c.id)
            .where(
                spec.table.c.id > last_id,
                spec.table.c.id <= checkpoint.scan_high_watermark_id,
            )
            .order_by(spec.table.c.id)
            .limit(_PREFLIGHT_PAGE_SIZE)
        )
        if for_update:
            id_stmt = id_stmt.with_for_update()
        ids = list(await session.scalars(id_stmt))
        if not ids:
            break
        for row_id in ids:
            row = await _load_full_row(
                session,
                spec=spec,
                row_id=row_id,
                for_update=for_update,
            )
            data_checksum = _extend_checksum(
                data_checksum, _data_envelope(spec, row)
            )
            ownership_checksum = _extend_checksum(
                ownership_checksum, _ownership_envelope(spec, row)
            )
        scanned += len(ids)
        last_id = ids[-1]
        if len(ids) < _PREFLIGHT_PAGE_SIZE:
            break
    if scanned != checkpoint.snapshot_rows:
        raise NormalizedOwnershipBackfillStateError(
            "final normalized snapshot cardinality drifted"
        )
    if require_data_checksum and (
        data_checksum != checkpoint.data_checksum_before
        or data_checksum != checkpoint.data_checksum_after
    ):
        raise NormalizedOwnershipBackfillStateError(
            "normalized data changed across ownership backfill"
        )
    if ownership_checksum != checkpoint.ownership_checksum_after:
        raise NormalizedOwnershipBackfillStateError(
            "normalized ownership changed across backfill"
        )


async def _summarize_tables(
    session: AsyncSession,
    *,
    scope: _Scope,
    checkpoints: dict[
        str, OwnershipBackfillCheckpoint | _CheckpointProjection
    ],
    for_update: bool,
    validate_rows: bool = True,
    verify_completed_checksums: bool = False,
    require_completed_data_checksum: bool = False,
) -> list[_TableSummary]:
    summaries: list[_TableSummary] = []
    if validate_rows:
        await _validate_cross_table_gates(
            session,
            scope=scope,
            for_update=for_update,
        )
    for spec in _NORMALIZED_TABLES:
        checkpoint = checkpoints.get(spec.phase_key)
        if checkpoint is None:
            high_watermark = await _max_id(session, spec)
            snapshot_rows = await _count_to(
                session, spec, high_watermark=high_watermark
            )
            cursor = None
            remaining = snapshot_rows
        else:
            status = _validate_checkpoint_shape(
                checkpoint,
                subject_id=scope.subject_id,
                expected_phase=spec.phase_key,
            )
            high_watermark = checkpoint.scan_high_watermark_id
            snapshot_rows = checkpoint.snapshot_rows
            cursor = checkpoint.last_scanned_id
            remaining = await _validate_checkpoint_counts(
                session, spec=spec, checkpoint=checkpoint
            )
        if validate_rows:
            rows_above = await _scan_table(
                session,
                spec=spec,
                scope=scope,
                high_watermark=high_watermark,
                checkpoint_cursor=cursor,
                for_update=for_update,
            )
        else:
            rows_above = await _count_rows_above(
                session,
                spec,
                high_watermark=high_watermark,
            )
        if (
            verify_completed_checksums
            and checkpoint is not None
            and status is NormalizedOwnershipBackfillStatus.COMPLETED
        ):
            await _verify_final_snapshot_checksums(
                session,
                spec=spec,
                checkpoint=checkpoint,
                for_update=for_update,
                require_data_checksum=require_completed_data_checksum,
            )
        summaries.append(
            _TableSummary(
                spec=spec,
                checkpoint=checkpoint,
                high_watermark=high_watermark,
                snapshot_rows=snapshot_rows,
                remaining_rows=remaining,
                rows_above=rows_above,
            )
        )
    return summaries


def _aggregate_digest(summaries: list[_TableSummary], field_name: str) -> str:
    digest = _EMPTY_SHA256
    for summary in summaries:
        checkpoint = summary.checkpoint
        value = (
            getattr(checkpoint, field_name)
            if checkpoint is not None
            else _EMPTY_SHA256
        )
        digest = _extend_checksum(digest, [summary.spec.name, value])
    return digest


def _result_from_summaries(
    *, scope: _Scope, summaries: list[_TableSummary]
) -> NormalizedOwnershipBackfillPreflightResult:
    checkpoints = [
        summary.checkpoint
        for summary in summaries
        if summary.checkpoint is not None
    ]
    completed_tables = sum(
        checkpoint.status == NormalizedOwnershipBackfillStatus.COMPLETED.value
        for checkpoint in checkpoints
    )
    if completed_tables == len(_NORMALIZED_TABLES):
        status = NormalizedOwnershipBackfillStatus.COMPLETED
    elif checkpoints:
        status = NormalizedOwnershipBackfillStatus.RUNNING
    else:
        status = NormalizedOwnershipBackfillStatus.NOT_STARTED
    return NormalizedOwnershipBackfillPreflightResult(
        phase_key=NORMALIZED_MANUAL_BACKFILL_PHASE,
        subject_id=scope.subject_id,
        status=status,
        tables_total=len(_NORMALIZED_TABLES),
        completed_tables=completed_tables,
        snapshot_rows=sum(summary.snapshot_rows for summary in summaries),
        scanned_rows=sum(
            summary.checkpoint.scanned_rows
            if summary.checkpoint is not None
            else 0
            for summary in summaries
        ),
        updated_rows=sum(
            summary.checkpoint.updated_rows
            if summary.checkpoint is not None
            else 0
            for summary in summaries
        ),
        unchanged_rows=sum(
            summary.checkpoint.unchanged_rows
            if summary.checkpoint is not None
            else 0
            for summary in summaries
        ),
        remaining_rows=sum(summary.remaining_rows for summary in summaries),
        rows_above_high_watermark=sum(
            summary.rows_above for summary in summaries
        ),
        data_checksum_before=_aggregate_digest(
            summaries, "data_checksum_before"
        ),
        data_checksum_after=_aggregate_digest(summaries, "data_checksum_after"),
        ownership_checksum_after=_aggregate_digest(
            summaries, "ownership_checksum_after"
        ),
    )


def _batch_result(
    aggregate: NormalizedOwnershipBackfillPreflightResult,
    *,
    batch_table: str,
    batch_scanned_rows: int,
    batch_updated_rows: int,
    batch_unchanged_rows: int,
) -> NormalizedOwnershipBackfillBatchResult:
    return NormalizedOwnershipBackfillBatchResult(
        phase_key=aggregate.phase_key,
        subject_id=aggregate.subject_id,
        status=aggregate.status,
        tables_total=aggregate.tables_total,
        completed_tables=aggregate.completed_tables,
        snapshot_rows=aggregate.snapshot_rows,
        scanned_rows=aggregate.scanned_rows,
        updated_rows=aggregate.updated_rows,
        unchanged_rows=aggregate.unchanged_rows,
        remaining_rows=aggregate.remaining_rows,
        rows_above_high_watermark=aggregate.rows_above_high_watermark,
        data_checksum_before=aggregate.data_checksum_before,
        data_checksum_after=aggregate.data_checksum_after,
        ownership_checksum_after=aggregate.ownership_checksum_after,
        batch_table=batch_table,
        batch_scanned_rows=batch_scanned_rows,
        batch_updated_rows=batch_updated_rows,
        batch_unchanged_rows=batch_unchanged_rows,
    )


async def preflight_normalized_ownership_backfill(
    session: AsyncSession,
) -> NormalizedOwnershipBackfillPreflightResult:
    """Validate and project Stage-3B without flushing or mutating state."""

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=False)
        dependency = await _load_dependency_checkpoint(
            session, for_update=False
        )
        _require_completed_dependency(dependency, subject_id=scope.subject_id)
        checkpoints = await _load_table_checkpoints(session, for_update=False)
        summaries = await _summarize_tables(
            session,
            scope=scope,
            checkpoints=checkpoints,
            for_update=False,
            verify_completed_checksums=True,
        )
        return _result_from_summaries(scope=scope, summaries=summaries)


def _validate_restore_snapshot_bounds(
    snapshot_bounds: Mapping[str, tuple[int, int]],
) -> dict[str, tuple[int, int]]:
    if not isinstance(snapshot_bounds, Mapping):
        raise NormalizedOwnershipBackfillValidationError(
            "snapshot_bounds must be an exact table mapping"
        )
    supplied = set(snapshot_bounds)
    expected = set(NORMALIZED_MANUAL_TABLES)
    if supplied != expected:
        raise NormalizedOwnershipBackfillValidationError(
            "snapshot_bounds must contain the exact normalized table catalog"
        )
    validated: dict[str, tuple[int, int]] = {}
    for table_name in NORMALIZED_MANUAL_TABLES:
        pair = snapshot_bounds[table_name]
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise NormalizedOwnershipBackfillValidationError(
                "each snapshot bound must be an exact (high_watermark, count) pair"
            )
        high_watermark, snapshot_rows = pair
        if (
            isinstance(high_watermark, bool)
            or not isinstance(high_watermark, int)
            or isinstance(snapshot_rows, bool)
            or not isinstance(snapshot_rows, int)
            or not 0 <= high_watermark <= _POSTGRES_INTEGER_MAX
            or not 0 <= snapshot_rows <= _POSTGRES_INTEGER_MAX
        ):
            raise NormalizedOwnershipBackfillValidationError(
                "snapshot bounds must be nonnegative PostgreSQL INTEGER values"
            )
        if snapshot_rows > high_watermark:
            raise NormalizedOwnershipBackfillValidationError(
                "snapshot row count cannot exceed its high-water mark"
            )
        if (high_watermark == 0) != (snapshot_rows == 0):
            raise NormalizedOwnershipBackfillValidationError(
                "an empty incoming table must use the exact 0/0 bound"
            )
        validated[table_name] = (high_watermark, snapshot_rows)
    return validated


async def _lock_current_normalized_rows(session: AsyncSession) -> None:
    for spec in _NORMALIZED_TABLES:
        last_id = 0
        while True:
            ids = list(
                await session.scalars(
                    select(spec.table.c.id)
                    .where(spec.table.c.id > last_id)
                    .order_by(spec.table.c.id)
                    .limit(_PREFLIGHT_PAGE_SIZE)
                    .with_for_update()
                )
            )
            if not ids:
                break
            last_id = ids[-1]
            if len(ids) < _PREFLIGHT_PAGE_SIZE:
                break


async def reset_normalized_manual_backfill_for_portability_v1_restore(
    session: AsyncSession,
    *,
    snapshot_bounds: Mapping[str, tuple[int, int]],
) -> None:
    """Reset Stage-3B checkpoints for an atomic portability-v1 replacement.

    The caller has already validated the incoming fixed-table snapshots and
    replaces the corresponding rows later in this same transaction.  Existing
    row ownership is deliberately neither trusted nor inspected at this full
    replacement boundary.
    """

    bounds = _validate_restore_snapshot_bounds(snapshot_bounds)
    reset_at = now_utc().replace(microsecond=0)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependency = await _load_dependency_checkpoint(session, for_update=True)
        if dependency is None:
            raise NormalizedOwnershipBackfillDependencyError(
                "terminal raw ownership prerequisite is required"
            )
        raw_status = _validate_raw_dependency(
            dependency,
            subject_id=scope.subject_id,
            allow_restore_blocked=True,
        )
        if raw_status not in {"completed", "restore_blocked"}:
            raise NormalizedOwnershipBackfillDependencyError(
                "raw ownership prerequisite is not terminal"
            )
        checkpoints = await _load_table_checkpoints(session, for_update=True)
        for spec in _NORMALIZED_TABLES:
            existing = checkpoints.get(spec.phase_key)
            if existing is not None:
                _validate_checkpoint_shape(
                    existing,
                    subject_id=scope.subject_id,
                    expected_phase=spec.phase_key,
                )
        await _lock_current_normalized_rows(session)

        for spec in _NORMALIZED_TABLES:
            high_watermark, snapshot_rows = bounds[spec.name]
            target_status = (
                NormalizedOwnershipBackfillStatus.COMPLETED
                if high_watermark == 0
                else NormalizedOwnershipBackfillStatus.RUNNING
            )
            checkpoint = checkpoints.get(spec.phase_key)
            if checkpoint is None:
                checkpoint = OwnershipBackfillCheckpoint(
                    phase_key=spec.phase_key,
                    subject_id=scope.subject_id,
                    status=target_status.value,
                    scan_high_watermark_id=high_watermark,
                    snapshot_rows=snapshot_rows,
                    last_scanned_id=0,
                    scanned_rows=0,
                    updated_rows=0,
                    unchanged_rows=0,
                    data_checksum_before=_EMPTY_SHA256,
                    data_checksum_after=_EMPTY_SHA256,
                    ownership_checksum_after=_EMPTY_SHA256,
                    started_at=reset_at,
                    updated_at=reset_at,
                    completed_at=(
                        reset_at
                        if target_status
                        is NormalizedOwnershipBackfillStatus.COMPLETED
                        else None
                    ),
                )
                session.add(checkpoint)
                checkpoints[spec.phase_key] = checkpoint
            checkpoint.status = target_status.value
            checkpoint.scan_high_watermark_id = high_watermark
            checkpoint.snapshot_rows = snapshot_rows
            checkpoint.last_scanned_id = 0
            checkpoint.scanned_rows = 0
            checkpoint.updated_rows = 0
            checkpoint.unchanged_rows = 0
            checkpoint.data_checksum_before = _EMPTY_SHA256
            checkpoint.data_checksum_after = _EMPTY_SHA256
            checkpoint.ownership_checksum_after = _EMPTY_SHA256
            checkpoint.started_at = reset_at
            checkpoint.updated_at = reset_at
            checkpoint.completed_at = (
                reset_at
                if target_status is NormalizedOwnershipBackfillStatus.COMPLETED
                else None
            )
        await session.flush()


async def run_normalized_ownership_backfill_batch(
    session: AsyncSession,
    *,
    batch_size: int,
) -> NormalizedOwnershipBackfillBatchResult:
    """Advance the first incomplete fixed table by at most one PK batch."""

    size = _validate_batch_size(batch_size)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependency = await _load_dependency_checkpoint(session, for_update=True)
        _require_completed_dependency(dependency, subject_id=scope.subject_id)
        checkpoints = await _load_table_checkpoints(session, for_update=True)
        summaries = await _summarize_tables(
            session,
            scope=scope,
            checkpoints=checkpoints,
            # Keep each ordinary batch bounded to its target table. The
            # checkpoint graph is locked above; full provenance scans belong
            # to explicit preflight and the one final group transition.
            for_update=False,
            validate_rows=False,
        )
        for summary in summaries:
            if summary.rows_above == 0:
                continue
            await _scan_table(
                session,
                spec=summary.spec,
                scope=scope,
                high_watermark=summary.high_watermark,
                checkpoint_cursor=None,
                for_update=True,
                start_after=summary.high_watermark,
            )
        target = next(
            (
                summary
                for summary in summaries
                if summary.checkpoint is None
                or summary.checkpoint.status
                != NormalizedOwnershipBackfillStatus.COMPLETED.value
            ),
            None,
        )
        if target is None:
            summaries = await _summarize_tables(
                session,
                scope=scope,
                checkpoints=checkpoints,
                for_update=True,
                validate_rows=True,
                verify_completed_checksums=True,
            )
            aggregate = _result_from_summaries(
                scope=scope, summaries=summaries
            )
            return _batch_result(
                aggregate,
                batch_table=_NORMALIZED_TABLES[-1].name,
                batch_scanned_rows=0,
                batch_updated_rows=0,
                batch_unchanged_rows=0,
            )

        checkpoint = target.checkpoint
        if checkpoint is None:
            checkpoint = OwnershipBackfillCheckpoint(
                phase_key=target.spec.phase_key,
                subject_id=scope.subject_id,
                status=NormalizedOwnershipBackfillStatus.RUNNING.value,
                scan_high_watermark_id=target.high_watermark,
                snapshot_rows=target.snapshot_rows,
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
            await session.flush()
            checkpoints[target.spec.phase_key] = checkpoint
            target = _TableSummary(
                spec=target.spec,
                checkpoint=checkpoint,
                high_watermark=target.high_watermark,
                snapshot_rows=target.snapshot_rows,
                remaining_rows=target.remaining_rows,
                rows_above=target.rows_above,
            )

        await _validate_target_cross_table_gates(
            session,
            spec=target.spec,
            scope=scope,
        )
        # Historical rows are validated one-by-one as they enter the bounded
        # batch. Rows appended after the frozen HWM must already satisfy the
        # live dual-write contract and are never adopted.
        await _scan_table(
            session,
            spec=target.spec,
            scope=scope,
            high_watermark=checkpoint.scan_high_watermark_id,
            checkpoint_cursor=None,
            for_update=True,
            start_after=checkpoint.scan_high_watermark_id,
        )

        batch_ids = list(
            await session.scalars(
                select(target.spec.table.c.id)
                .where(
                    target.spec.table.c.id > checkpoint.last_scanned_id,
                    target.spec.table.c.id
                    <= checkpoint.scan_high_watermark_id,
                )
                .order_by(target.spec.table.c.id)
                .limit(size)
                .with_for_update()
            )
        )

    before_checksum = checkpoint.data_checksum_before
    after_checksum = checkpoint.data_checksum_after
    ownership_checksum = checkpoint.ownership_checksum_after
    batch_updated = 0
    batch_unchanged = 0
    for row_id in batch_ids:
        before_row = await _load_full_row(
            session,
            spec=target.spec,
            row_id=row_id,
            for_update=True,
        )
        _validate_provenance(target.spec, before_row)
        changed = _classify_ownership(
            before_row,
            spec=target.spec,
            scope=scope,
            high_watermark=checkpoint.scan_high_watermark_id,
        )
        before_checksum = _extend_checksum(
            before_checksum, _data_envelope(target.spec, before_row)
        )
        if changed:
            values: dict[str, Any] = {"subject_id": scope.subject_id}
            if "updated_at" in target.spec.table.c:
                values["updated_at"] = before_row._mapping["updated_at"]
            await session.execute(
                update(target.spec.table)
                .where(target.spec.table.c.id == row_id)
                .values(**values)
            )
            batch_updated += 1
        else:
            batch_unchanged += 1
        after_row = await _load_full_row(
            session,
            spec=target.spec,
            row_id=row_id,
            for_update=True,
        )
        _validate_provenance(target.spec, after_row)
        after_checksum = _extend_checksum(
            after_checksum, _data_envelope(target.spec, after_row)
        )
        ownership_checksum = _extend_checksum(
            ownership_checksum,
            _ownership_envelope(target.spec, after_row),
        )

    if before_checksum != after_checksum:
        raise NormalizedOwnershipBackfillStateError(
            "normalized data changed while ownership was backfilled"
        )
    batch_scanned = len(batch_ids)
    checkpoint.scanned_rows += batch_scanned
    checkpoint.updated_rows += batch_updated
    checkpoint.unchanged_rows += batch_unchanged
    checkpoint.data_checksum_before = before_checksum
    checkpoint.data_checksum_after = after_checksum
    checkpoint.ownership_checksum_after = ownership_checksum
    if batch_ids:
        checkpoint.last_scanned_id = batch_ids[-1]

    remaining = await _remaining_rows(
        session,
        target.spec,
        high_watermark=checkpoint.scan_high_watermark_id,
        last_scanned=checkpoint.last_scanned_id,
    )
    if remaining == 0:
        await session.flush()
        await _validate_checkpoint_counts(
            session, spec=target.spec, checkpoint=checkpoint
        )
        await _verify_final_snapshot_checksums(
            session,
            spec=target.spec,
            checkpoint=checkpoint,
            for_update=True,
        )
        checkpoint.last_scanned_id = checkpoint.scan_high_watermark_id
        checkpoint.status = NormalizedOwnershipBackfillStatus.COMPLETED.value
        checkpoint.completed_at = now_utc()
    await session.flush()

    # Rebuild the aggregate from the locked in-memory checkpoint graph.  No
    # second mutation can pass governance concurrently on PostgreSQL.
    refreshed_summaries: list[_TableSummary] = []
    for summary in summaries:
        if summary.spec.name == target.spec.name:
            refreshed_summaries.append(
                _TableSummary(
                    spec=summary.spec,
                    checkpoint=checkpoint,
                    high_watermark=checkpoint.scan_high_watermark_id,
                    snapshot_rows=checkpoint.snapshot_rows,
                    remaining_rows=remaining,
                    rows_above=summary.rows_above,
                )
            )
        else:
            refreshed_summaries.append(summary)
    aggregate = _result_from_summaries(
        scope=scope, summaries=refreshed_summaries
    )
    if aggregate.status is NormalizedOwnershipBackfillStatus.COMPLETED:
        refreshed_summaries = await _summarize_tables(
            session,
            scope=scope,
            checkpoints=checkpoints,
            for_update=True,
            validate_rows=True,
            verify_completed_checksums=True,
            require_completed_data_checksum=True,
        )
        aggregate = _result_from_summaries(
            scope=scope,
            summaries=refreshed_summaries,
        )
    return _batch_result(
        aggregate,
        batch_table=target.spec.name,
        batch_scanned_rows=batch_scanned,
        batch_updated_rows=batch_updated,
        batch_unchanged_rows=batch_unchanged,
    )


__all__ = [
    "DEFAULT_NORMALIZED_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "MAX_NORMALIZED_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "NORMALIZED_MANUAL_BACKFILL_PHASE",
    "NORMALIZED_MANUAL_CHECKPOINT_PHASES",
    "NORMALIZED_MANUAL_TABLES",
    "NORMALIZED_OWNERSHIP_BACKFILL_TABLES",
    "NormalizedOwnershipBackfillBatchResult",
    "NormalizedOwnershipBackfillDependencyError",
    "NormalizedOwnershipBackfillError",
    "NormalizedOwnershipBackfillIdentityError",
    "NormalizedOwnershipBackfillPreflightResult",
    "NormalizedOwnershipBackfillProvenanceError",
    "NormalizedOwnershipBackfillStateError",
    "NormalizedOwnershipBackfillStatus",
    "NormalizedOwnershipBackfillValidationError",
    "preflight_normalized_ownership_backfill",
    "reset_normalized_manual_backfill_for_portability_v1_restore",
    "run_normalized_ownership_backfill_batch",
]
