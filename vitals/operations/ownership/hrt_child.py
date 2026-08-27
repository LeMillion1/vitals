"""Bounded Stage-3C ownership backfill for HRT plan children.

Only ``hrt_cycle_items`` and ``hrt_cycle_template_items`` are safe in this
phase: their direct parents are already covered by Stage-3B.  The other nullable
children introduced by revision 0038 depend on provider, file/raw, or mixed
catalog parent phases and intentionally remain out of this closed catalog.

The service owns no transaction boundary.  One call advances at most one table
by one stable-PK batch, flushes, and leaves commit/rollback to the operator.
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
from types import MappingProxyType, SimpleNamespace
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
from vitals.models.hrt import (
    HrtCompound,
    HrtCycle,
    HrtCycleItem,
    HrtCycleTemplate,
    HrtCycleTemplateItem,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.tenancy import IntegrationConnection
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.operations.ownership.normalized import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.raw import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.utils.timeutils import now_utc

HRT_CHILD_OWNERSHIP_BACKFILL_PHASE = "stage3.inherited_children.hrt.v1"
DEFAULT_HRT_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE = 250
MAX_HRT_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE = 1000

_PREFLIGHT_PAGE_SIZE = 1000
_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_SIGNED_BIGINT_MAX = (1 << 63) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MANUAL_SOURCES = frozenset({Source.MANUAL.value, Source.MCP.value})
_KNOWN_CONNECTION_STATUSES = frozenset(
    item.value for item in IntegrationConnectionStatus
)
_KNOWN_CONNECTION_TYPES = frozenset(
    item.value for item in IntegrationConnectionType
)
_KNOWN_PROVIDERS = frozenset(item.value for item in IntegrationProvider)


class HrtChildOwnershipBackfillStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"


class HrtChildOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed Stage-3C errors."""


class HrtChildOwnershipBackfillValidationError(
    HrtChildOwnershipBackfillError, ValueError
):
    """A caller argument or persisted scalar is unsafe."""


class HrtChildOwnershipBackfillIdentityError(HrtChildOwnershipBackfillError):
    """The exact-one legacy subject/owner graph is unavailable."""


class HrtChildOwnershipBackfillDependencyError(HrtChildOwnershipBackfillError):
    """Stage-3A/Stage-3B prerequisite evidence is unavailable."""


class HrtChildOwnershipBackfillStateError(HrtChildOwnershipBackfillError):
    """Checkpoint progress or a parent/child ownership graph is inconsistent."""


class HrtChildOwnershipBackfillProvenanceError(HrtChildOwnershipBackfillError):
    """An HRT parent or linked compound has unreviewed provenance."""


@dataclass(frozen=True, slots=True)
class HrtChildOwnershipBackfillPreflightResult:
    phase_key: str
    subject_id: uuid.UUID
    status: HrtChildOwnershipBackfillStatus
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
        return self.status is HrtChildOwnershipBackfillStatus.COMPLETED

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
class HrtChildOwnershipBackfillBatchResult(
    HrtChildOwnershipBackfillPreflightResult
):
    batch_table: str
    batch_scanned_rows: int
    batch_updated_rows: int
    batch_unchanged_rows: int

    @property
    def changed(self) -> bool:
        return self.batch_updated_rows > 0

    def to_safe_dict(self) -> dict[str, str | int]:
        result = HrtChildOwnershipBackfillPreflightResult.to_safe_dict(self)
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
    parent_table: Table
    parent_fk: str
    validate_compound: bool = False

    @property
    def name(self) -> str:
        return self.table.name

    @property
    def phase_key(self) -> str:
        return f"{HRT_CHILD_OWNERSHIP_BACKFILL_PHASE}.{self.name}"


_TABLES: tuple[_TableSpec, ...] = (
    _TableSpec(
        HrtCycleItem.__table__,
        HrtCycle.__table__,
        "cycle_id",
        validate_compound=True,
    ),
    _TableSpec(
        HrtCycleTemplateItem.__table__,
        HrtCycleTemplate.__table__,
        "template_id",
    ),
)
HRT_CHILD_OWNERSHIP_BACKFILL_TABLES = tuple(spec.name for spec in _TABLES)
HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES: Mapping[str, str] = (
    MappingProxyType({spec.name: spec.phase_key for spec in _TABLES})
)
_PHASE_KEYS = tuple(spec.phase_key for spec in _TABLES)
_NORMALIZED_PHASE_KEYS = tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())


@dataclass(frozen=True, slots=True)
class _Scope:
    subject_id: uuid.UUID
    owner_user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _ChildLinkProjection:
    parent_id: int
    compound_id: int | None


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


def _validate_batch_size(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_HRT_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE
    ):
        raise HrtChildOwnershipBackfillValidationError(
            "batch_size must be an integer between 1 and "
            f"{MAX_HRT_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE}"
        )
    return value


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
            raise HrtChildOwnershipBackfillIdentityError(
                "HRT child backfill requires exactly one health subject"
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
            raise HrtChildOwnershipBackfillIdentityError(
                "HRT child backfill requires exactly one health subject"
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
        raise HrtChildOwnershipBackfillIdentityError(
            "the sole health subject must have an active owner"
        )
    for connection in connections:
        if connection.subject_id != subject_id:
            raise HrtChildOwnershipBackfillIdentityError(
                "an integration connection belongs to a foreign subject"
            )
        if (
            connection.provider not in _KNOWN_PROVIDERS
            or connection.connection_type not in _KNOWN_CONNECTION_TYPES
            or connection.status not in _KNOWN_CONNECTION_STATUSES
        ):
            raise HrtChildOwnershipBackfillIdentityError(
                "an integration connection has an unknown persisted mapping"
            )
    return _Scope(subject_id=subject_id, owner_user_id=owner_user_id)


def _checkpoint_select():
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


def _projection(row: Any) -> _CheckpointProjection:
    return _CheckpointProjection(*row)


async def _load_dependencies(
    session: AsyncSession, *, for_update: bool
) -> dict[str, OwnershipBackfillCheckpoint | _CheckpointProjection]:
    phases = (RAW_OWNERSHIP_BACKFILL_PHASE, *_NORMALIZED_PHASE_KEYS)
    if for_update:
        # Lock one by one because PostgreSQL may ignore an IN-list/order hint.
        # The dependency order is raw, then Stage-3B's frozen table order.
        ordered: dict[str, OwnershipBackfillCheckpoint] = {}
        for phase in phases:
            row = await session.scalar(
                select(OwnershipBackfillCheckpoint)
                .where(OwnershipBackfillCheckpoint.phase_key == phase)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is not None:
                ordered[phase] = row
        return ordered
    rows = list(
        await session.execute(
            _checkpoint_select().where(
                OwnershipBackfillCheckpoint.phase_key.in_(phases)
            )
        )
    )
    return {row.phase_key: _projection(row) for row in rows}


async def _load_checkpoints(
    session: AsyncSession, *, for_update: bool
) -> dict[str, OwnershipBackfillCheckpoint | _CheckpointProjection]:
    if for_update:
        rows: dict[str, OwnershipBackfillCheckpoint] = {}
        for phase in _PHASE_KEYS:
            row = await session.scalar(
                select(OwnershipBackfillCheckpoint)
                .where(OwnershipBackfillCheckpoint.phase_key == phase)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is not None:
                rows[phase] = row
        return rows
    projections = list(
        await session.execute(
            _checkpoint_select().where(
                OwnershipBackfillCheckpoint.phase_key.in_(_PHASE_KEYS)
            )
        )
    )
    return {row.phase_key: _projection(row) for row in projections}


def _validate_checkpoint_shape(
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection,
    *,
    subject_id: uuid.UUID,
    expected_phase: str,
    dependency: bool,
    allow_running: bool,
    allow_restore_blocked: bool = False,
) -> str:
    error = (
        HrtChildOwnershipBackfillDependencyError
        if dependency
        else HrtChildOwnershipBackfillStateError
    )
    label = "ownership prerequisite" if dependency else "HRT child checkpoint"
    if checkpoint.phase_key != expected_phase:
        raise error(f"{label} has an unexpected phase")
    if checkpoint.subject_id != subject_id:
        raise error(f"{label} belongs to another subject")
    allowed_statuses = {"completed"}
    if allow_running:
        allowed_statuses.add("running")
    if allow_restore_blocked:
        allowed_statuses.add("restore_blocked")
    if checkpoint.status not in allowed_statuses:
        raise error(f"{label} is not in an allowed state")
    counters = (
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
    )
    if not all(_valid_counter(value) for value in counters):
        raise error(f"{label} has invalid counters")
    if (
        checkpoint.last_scanned_id > checkpoint.scan_high_watermark_id
        or checkpoint.snapshot_rows > checkpoint.scan_high_watermark_id
        or checkpoint.scanned_rows > checkpoint.snapshot_rows
        or checkpoint.scanned_rows
        != checkpoint.updated_rows + checkpoint.unchanged_rows
    ):
        raise error(f"{label} has inconsistent progress")
    digests = (
        checkpoint.data_checksum_before,
        checkpoint.data_checksum_after,
        checkpoint.ownership_checksum_after,
    )
    if any(
        not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
        for value in digests
    ) or checkpoint.data_checksum_before != checkpoint.data_checksum_after:
        raise error(f"{label} has invalid checksums")
    if checkpoint.status == "completed":
        if (
            checkpoint.completed_at is None
            or checkpoint.last_scanned_id != checkpoint.scan_high_watermark_id
            or checkpoint.scanned_rows != checkpoint.snapshot_rows
        ):
            raise error(f"completed {label} is incomplete")
    elif checkpoint.status == "restore_blocked":
        if (
            checkpoint.completed_at is not None
            or checkpoint.last_scanned_id != 0
            or checkpoint.scanned_rows != 0
            or checkpoint.updated_rows != 0
            or checkpoint.unchanged_rows != 0
            or any(value != _EMPTY_SHA256 for value in digests)
        ):
            raise error(f"restore-blocked {label} is inconsistent")
    elif checkpoint.completed_at is not None:
        raise error(f"running {label} has a completion timestamp")
    return checkpoint.status


def _require_apply_dependencies(
    dependencies: Mapping[
        str, OwnershipBackfillCheckpoint | _CheckpointProjection
    ],
    *,
    subject_id: uuid.UUID,
) -> None:
    expected = {RAW_OWNERSHIP_BACKFILL_PHASE, *_NORMALIZED_PHASE_KEYS}
    if set(dependencies) != expected:
        raise HrtChildOwnershipBackfillDependencyError(
            "completed Stage-3A and all Stage-3B checkpoints are required"
        )
    for phase in (RAW_OWNERSHIP_BACKFILL_PHASE, *_NORMALIZED_PHASE_KEYS):
        _validate_checkpoint_shape(
            dependencies[phase],
            subject_id=subject_id,
            expected_phase=phase,
            dependency=True,
            allow_running=False,
        )


def _require_restore_dependencies(
    dependencies: Mapping[
        str, OwnershipBackfillCheckpoint | _CheckpointProjection
    ],
    *,
    subject_id: uuid.UUID,
) -> None:
    expected = {RAW_OWNERSHIP_BACKFILL_PHASE, *_NORMALIZED_PHASE_KEYS}
    if set(dependencies) != expected:
        raise HrtChildOwnershipBackfillDependencyError(
            "terminal raw and reset Stage-3B checkpoints are required"
        )
    _validate_checkpoint_shape(
        dependencies[RAW_OWNERSHIP_BACKFILL_PHASE],
        subject_id=subject_id,
        expected_phase=RAW_OWNERSHIP_BACKFILL_PHASE,
        dependency=True,
        allow_running=False,
        allow_restore_blocked=True,
    )
    for phase in _NORMALIZED_PHASE_KEYS:
        _validate_checkpoint_shape(
            dependencies[phase],
            subject_id=subject_id,
            expected_phase=phase,
            dependency=True,
            allow_running=True,
        )


def _parent_high_watermark(
    spec: _TableSpec,
    dependencies: Mapping[
        str, OwnershipBackfillCheckpoint | _CheckpointProjection
    ],
) -> int:
    return dependencies[
        NORMALIZED_MANUAL_CHECKPOINT_PHASES[spec.parent_table.name]
    ].scan_high_watermark_id


async def _max_id(session: AsyncSession, spec: _TableSpec) -> int:
    value = await session.scalar(select(func.max(spec.table.c.id)))
    if value is None:
        return 0
    if not _valid_counter(value) or value > _POSTGRES_INTEGER_MAX:
        raise HrtChildOwnershipBackfillStateError(
            "an HRT child has an invalid primary key"
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
    session: AsyncSession,
    *,
    spec: _TableSpec,
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection,
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
    if snapshot != checkpoint.snapshot_rows:
        raise HrtChildOwnershipBackfillStateError(
            "an HRT child checkpoint snapshot count drifted"
        )
    if prefix != checkpoint.scanned_rows:
        raise HrtChildOwnershipBackfillStateError(
            "an HRT child checkpoint prefix count drifted"
        )
    if checkpoint.scanned_rows + remaining != checkpoint.snapshot_rows:
        raise HrtChildOwnershipBackfillStateError(
            "an HRT child checkpoint no longer matches its snapshot"
        )
    return remaining


def _validate_parent(
    row: Any,
    *,
    scope: _Scope,
    parent_high_watermark: int,
) -> None:
    if row is None:
        raise HrtChildOwnershipBackfillStateError(
            "an HRT child references a missing parent"
        )
    if row.domain != Domain.HRT.value or row.source not in _MANUAL_SOURCES:
        raise HrtChildOwnershipBackfillProvenanceError(
            "an HRT child parent has unreviewed provenance"
        )
    if row.subject_id != scope.subject_id:
        raise HrtChildOwnershipBackfillStateError(
            "an HRT child parent lacks exact subject ownership"
        )
    if row.actor_user_id not in (None, scope.owner_user_id):
        raise HrtChildOwnershipBackfillStateError(
            "an HRT child parent has foreign actor provenance"
        )
    if (
        row.id > parent_high_watermark
        and row.actor_user_id != scope.owner_user_id
    ):
        raise HrtChildOwnershipBackfillStateError(
            "a live HRT child parent lacks exact actor provenance"
        )


async def _load_parent(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    parent_id: int,
    scope: _Scope,
    parent_high_watermark: int,
    for_update: bool,
) -> Any:
    stmt = select(
        spec.parent_table.c.id,
        spec.parent_table.c.subject_id,
        spec.parent_table.c.actor_user_id,
        spec.parent_table.c.domain,
        spec.parent_table.c.source,
    ).where(spec.parent_table.c.id == parent_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).one_or_none()
    _validate_parent(
        row,
        scope=scope,
        parent_high_watermark=parent_high_watermark,
    )
    return row


async def _validate_compound(
    session: AsyncSession,
    *,
    row: Any,
    scope: _Scope,
    historical: bool,
    for_update: bool,
) -> None:
    compound_id = row._mapping["compound_id"]
    if compound_id is None:
        return
    stmt = select(*HrtCompound.__table__.c).where(
        HrtCompound.id == compound_id
    )
    if for_update:
        stmt = stmt.with_for_update()
    compound_row = (await session.execute(stmt)).one_or_none()
    if compound_row is None:
        raise HrtChildOwnershipBackfillStateError(
            "an HRT cycle item references a missing compound"
        )
    # Validate a detached read projection so this read-only secondary gate
    # cannot refresh over or otherwise mutate a caller's ORM identity-map row.
    compound = SimpleNamespace(**compound_row._mapping)
    if compound.key != row._mapping["compound_key"]:
        raise HrtChildOwnershipBackfillStateError(
            "an HRT cycle item compound link disagrees with its snapshot key"
        )
    if compound.domain != Domain.HRT.value:
        raise HrtChildOwnershipBackfillProvenanceError(
            "an HRT cycle item compound has an unexpected domain"
        )
    if compound.source == Source.SYSTEM.value:
        if compound.subject_id is not None or compound.actor_user_id is not None:
            raise HrtChildOwnershipBackfillStateError(
                "a system HRT compound must remain globally owned"
            )
        from vitals.services.hrt.records import (
            HrtCatalogIntegrityError,
            _require_curated_compound_integrity,
        )

        try:
            _require_curated_compound_integrity(compound)
        except HrtCatalogIntegrityError as exc:
            raise HrtChildOwnershipBackfillProvenanceError(
                "a system HRT compound failed reviewed catalog integrity"
            ) from exc
        return
    if compound.source not in _MANUAL_SOURCES:
        raise HrtChildOwnershipBackfillProvenanceError(
            "an HRT cycle item compound has an unexpected source"
        )
    if compound.subject_id is None and compound.actor_user_id is None:
        # Mixed-catalog ownership is a later phase. Exact-one legacy custom
        # compounds are permitted transitionally for historical children only
        # and are never mutated here. Strict live-tail consumers cannot resolve
        # an unowned custom root without the legacy bridge.
        if historical:
            return
        raise HrtChildOwnershipBackfillStateError(
            "a live HRT cycle item references an unowned custom compound"
        )
    if compound.subject_id == scope.subject_id and compound.actor_user_id in (
        None,
        scope.owner_user_id,
    ):
        if historical or compound.actor_user_id == scope.owner_user_id:
            return
        raise HrtChildOwnershipBackfillStateError(
            "a live HRT cycle item compound lacks exact actor provenance"
        )
    if compound.subject_id is None or compound.actor_user_id is None:
        raise HrtChildOwnershipBackfillStateError(
            "an HRT cycle item compound has partial ownership"
        )
    raise HrtChildOwnershipBackfillStateError(
        "an HRT cycle item compound has foreign ownership"
    )


async def _project_child_links(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    child_ids: list[int],
) -> dict[int, _ChildLinkProjection]:
    if not child_ids:
        return {}
    columns = [spec.table.c.id, spec.table.c[spec.parent_fk]]
    if spec.validate_compound:
        columns.append(spec.table.c.compound_id)
    rows = (
        await session.execute(
            select(*columns)
            .where(spec.table.c.id.in_(child_ids))
            .order_by(spec.table.c.id)
        )
    ).all()
    result = {
        row._mapping["id"]: _ChildLinkProjection(
            parent_id=row._mapping[spec.parent_fk],
            compound_id=(
                row._mapping["compound_id"]
                if spec.validate_compound
                else None
            ),
        )
        for row in rows
    }
    if tuple(result) != tuple(child_ids):
        raise HrtChildOwnershipBackfillStateError(
            "a reviewed HRT child disappeared before link projection"
        )
    return result


async def _lock_compounds_for_links(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    links: Mapping[int, _ChildLinkProjection],
    for_update: bool,
) -> None:
    """Lock optional compound roots after cycle parents and before children."""

    if not spec.validate_compound or not links:
        return
    compound_ids = sorted(
        {
            link.compound_id
            for link in links.values()
            if link.compound_id is not None
        }
    )
    if not compound_ids:
        return
    stmt = (
        select(HrtCompound.id)
        .where(HrtCompound.id.in_(compound_ids))
        .order_by(HrtCompound.id)
    )
    if for_update:
        stmt = stmt.with_for_update()
    locked = list(await session.scalars(stmt))
    if locked != compound_ids:
        raise HrtChildOwnershipBackfillStateError(
            "an HRT cycle item references a missing compound"
        )


def _require_projected_child_links(
    spec: _TableSpec,
    row: Any,
    expected: _ChildLinkProjection,
) -> None:
    actual = _ChildLinkProjection(
        parent_id=row._mapping[spec.parent_fk],
        compound_id=(
            row._mapping["compound_id"] if spec.validate_compound else None
        ),
    )
    if actual != expected:
        raise HrtChildOwnershipBackfillStateError(
            "an HRT child parent/compound link changed after root locking"
        )


def _classify_child(
    row: Any,
    *,
    scope: _Scope,
    high_watermark: int,
) -> bool:
    subject_id = row._mapping["subject_id"]
    if row._mapping["id"] <= high_watermark:
        if subject_id is None:
            return True
        if subject_id == scope.subject_id:
            return False
        raise HrtChildOwnershipBackfillStateError(
            "a historical HRT child has foreign ownership"
        )
    if subject_id != scope.subject_id:
        raise HrtChildOwnershipBackfillStateError(
            "an HRT child above the high-water mark lacks exact ownership"
        )
    return False


async def _load_full_child(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    row_id: int,
    for_update: bool,
) -> Any:
    stmt = select(spec.table).where(spec.table.c.id == row_id).limit(1)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise HrtChildOwnershipBackfillStateError(
            "a reviewed HRT child disappeared before processing"
        )
    return row


async def _validate_child_row(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    row: Any,
    scope: _Scope,
    high_watermark: int,
    parent_high_watermark: int,
    for_update: bool,
) -> bool:
    await _load_parent(
        session,
        spec=spec,
        parent_id=row._mapping[spec.parent_fk],
        scope=scope,
        parent_high_watermark=parent_high_watermark,
        for_update=for_update,
    )
    if spec.validate_compound:
        await _validate_compound(
            session,
            row=row,
            scope=scope,
            historical=row._mapping["id"] <= high_watermark,
            for_update=for_update,
        )
    return _classify_child(row, scope=scope, high_watermark=high_watermark)


async def _scan_table(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    scope: _Scope,
    high_watermark: int,
    parent_high_watermark: int,
    checkpoint_cursor: int | None,
    for_update: bool,
    start_after: int = 0,
    expected_ownership_checksum: str | None = None,
) -> tuple[int, str]:
    last_id = start_after
    rows_above = 0
    ownership_checksum = _EMPTY_SHA256
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
        # Parent-before-child is the durable row-lock order. Child ids are read
        # unlocked first, then every full row is re-read under lock.
        links = await _project_child_links(
            session, spec=spec, child_ids=ids
        )
        parent_ids = sorted({link.parent_id for link in links.values()})
        for parent_id in parent_ids:
            await _load_parent(
                session,
                spec=spec,
                parent_id=parent_id,
                scope=scope,
                parent_high_watermark=parent_high_watermark,
                for_update=for_update,
            )
        await _lock_compounds_for_links(
            session,
            spec=spec,
            links=links,
            for_update=for_update,
        )
        for row_id in ids:
            row = await _load_full_child(
                session, spec=spec, row_id=row_id, for_update=for_update
            )
            _require_projected_child_links(spec, row, links[row_id])
            changed = await _validate_child_row(
                session,
                spec=spec,
                row=row,
                scope=scope,
                high_watermark=high_watermark,
                parent_high_watermark=parent_high_watermark,
                # Parent was locked first; avoid a second lock statement.
                for_update=False,
            )
            if (
                checkpoint_cursor is not None
                and row_id <= checkpoint_cursor
                and changed
            ):
                raise HrtChildOwnershipBackfillStateError(
                    "a previously scanned HRT child requires ownership repair"
                )
            if row_id > high_watermark:
                rows_above += 1
            elif expected_ownership_checksum is not None:
                ownership_checksum = _extend_checksum(
                    ownership_checksum, _ownership_envelope(spec, row)
                )
        last_id = ids[-1]
        if len(ids) < _PREFLIGHT_PAGE_SIZE:
            break
    if (
        expected_ownership_checksum is not None
        and ownership_checksum != expected_ownership_checksum
    ):
        raise HrtChildOwnershipBackfillStateError(
            "completed HRT child ownership changed"
        )
    return rows_above, ownership_checksum


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HrtChildOwnershipBackfillStateError(
                "an HRT child contains a non-finite number"
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
    raise HrtChildOwnershipBackfillStateError(
        "an HRT child contains an unsupported persisted value"
    )


def _extend_checksum(previous: str, envelope: list[Any]) -> str:
    encoded = json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
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
            if column.name != "subject_id"
        ],
    ]


def _ownership_envelope(spec: _TableSpec, row: Any) -> list[Any]:
    subject_id = row._mapping["subject_id"]
    return [
        spec.name,
        row._mapping["id"],
        str(subject_id) if subject_id is not None else None,
    ]


async def _verify_final_snapshot(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    scope: _Scope,
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection,
    parent_high_watermark: int,
    for_update: bool,
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
        links = await _project_child_links(
            session, spec=spec, child_ids=ids
        )
        parent_ids = sorted({link.parent_id for link in links.values()})
        for parent_id in parent_ids:
            await _load_parent(
                session,
                spec=spec,
                parent_id=parent_id,
                scope=scope,
                parent_high_watermark=parent_high_watermark,
                for_update=for_update,
            )
        await _lock_compounds_for_links(
            session,
            spec=spec,
            links=links,
            for_update=for_update,
        )
        for row_id in ids:
            row = await _load_full_child(
                session, spec=spec, row_id=row_id, for_update=for_update
            )
            _require_projected_child_links(spec, row, links[row_id])
            changed = await _validate_child_row(
                session,
                spec=spec,
                row=row,
                scope=scope,
                high_watermark=checkpoint.scan_high_watermark_id,
                parent_high_watermark=parent_high_watermark,
                for_update=False,
            )
            if changed:
                raise HrtChildOwnershipBackfillStateError(
                    "final HRT child snapshot still requires ownership repair"
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
        raise HrtChildOwnershipBackfillStateError(
            "final HRT child snapshot count changed"
        )
    if require_data_checksum and (
        before != checkpoint.data_checksum_before
        or before != checkpoint.data_checksum_after
    ):
        raise HrtChildOwnershipBackfillStateError(
            "HRT child data changed during the maintenance window"
        )
    if ownership != checkpoint.ownership_checksum_after:
        raise HrtChildOwnershipBackfillStateError(
            "completed HRT child ownership changed"
        )


async def _summaries(
    session: AsyncSession,
    *,
    scope: _Scope,
    checkpoints: Mapping[
        str, OwnershipBackfillCheckpoint | _CheckpointProjection
    ],
    dependencies: Mapping[
        str, OwnershipBackfillCheckpoint | _CheckpointProjection
    ],
    validate_rows: bool,
    for_update: bool,
    verify_completed: bool,
    require_completed_data_checksum: bool = False,
) -> list[_TableSummary]:
    result: list[_TableSummary] = []
    for spec in _TABLES:
        parent_dependency = dependencies[
            NORMALIZED_MANUAL_CHECKPOINT_PHASES[spec.parent_table.name]
        ]
        parent_high_watermark = parent_dependency.scan_high_watermark_id
        checkpoint = checkpoints.get(spec.phase_key)
        status: str | None = None
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
                dependency=False,
                allow_running=True,
            )
            high_watermark = checkpoint.scan_high_watermark_id
            snapshot_rows = checkpoint.snapshot_rows
            cursor = checkpoint.last_scanned_id
            remaining = await _validate_counts(
                session, spec=spec, checkpoint=checkpoint
            )
        if validate_rows:
            rows_above, _digest = await _scan_table(
                session,
                spec=spec,
                scope=scope,
                high_watermark=high_watermark,
                parent_high_watermark=parent_high_watermark,
                checkpoint_cursor=cursor,
                for_update=for_update,
                expected_ownership_checksum=(
                    checkpoint.ownership_checksum_after
                    if checkpoint is not None and status == "completed"
                    else None
                ),
            )
        else:
            rows_above = int(
                await session.scalar(
                    select(func.count())
                    .select_from(spec.table)
                    .where(spec.table.c.id > high_watermark)
                )
                or 0
            )
        if verify_completed and checkpoint is not None and status == "completed":
            await _verify_final_snapshot(
                session,
                spec=spec,
                scope=scope,
                checkpoint=checkpoint,
                parent_high_watermark=parent_high_watermark,
                for_update=for_update,
                require_data_checksum=require_completed_data_checksum,
            )
        result.append(
            _TableSummary(
                spec=spec,
                checkpoint=checkpoint,
                high_watermark=high_watermark,
                snapshot_rows=snapshot_rows,
                remaining_rows=remaining,
                rows_above=rows_above,
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
) -> HrtChildOwnershipBackfillPreflightResult:
    checkpoints = [s.checkpoint for s in summaries if s.checkpoint is not None]
    completed = sum(cp.status == "completed" for cp in checkpoints)
    status = (
        HrtChildOwnershipBackfillStatus.COMPLETED
        if completed == len(_TABLES)
        else (
            HrtChildOwnershipBackfillStatus.RUNNING
            if checkpoints
            else HrtChildOwnershipBackfillStatus.NOT_STARTED
        )
    )
    return HrtChildOwnershipBackfillPreflightResult(
        phase_key=HRT_CHILD_OWNERSHIP_BACKFILL_PHASE,
        subject_id=scope.subject_id,
        status=status,
        tables_total=len(_TABLES),
        completed_tables=completed,
        snapshot_rows=sum(s.snapshot_rows for s in summaries),
        scanned_rows=sum(
            s.checkpoint.scanned_rows if s.checkpoint is not None else 0
            for s in summaries
        ),
        updated_rows=sum(
            s.checkpoint.updated_rows if s.checkpoint is not None else 0
            for s in summaries
        ),
        unchanged_rows=sum(
            s.checkpoint.unchanged_rows if s.checkpoint is not None else 0
            for s in summaries
        ),
        remaining_rows=sum(s.remaining_rows for s in summaries),
        rows_above_high_watermark=sum(s.rows_above for s in summaries),
        data_checksum_before=_aggregate_digest(summaries, "data_checksum_before"),
        data_checksum_after=_aggregate_digest(summaries, "data_checksum_after"),
        ownership_checksum_after=_aggregate_digest(
            summaries, "ownership_checksum_after"
        ),
    )


def _batch_result(
    aggregate: HrtChildOwnershipBackfillPreflightResult,
    *,
    batch_table: str,
    scanned: int,
    updated: int,
    unchanged: int,
) -> HrtChildOwnershipBackfillBatchResult:
    return HrtChildOwnershipBackfillBatchResult(
        **{
            field: getattr(aggregate, field)
            for field in HrtChildOwnershipBackfillPreflightResult.__dataclass_fields__
        },
        batch_table=batch_table,
        batch_scanned_rows=scanned,
        batch_updated_rows=updated,
        batch_unchanged_rows=unchanged,
    )


async def preflight_hrt_child_ownership_backfill(
    session: AsyncSession,
) -> HrtChildOwnershipBackfillPreflightResult:
    """Validate the fixed Stage-3C graph without mutating session state."""

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=False)
        dependencies = await _load_dependencies(session, for_update=False)
        _require_apply_dependencies(dependencies, subject_id=scope.subject_id)
        checkpoints = await _load_checkpoints(session, for_update=False)
        summaries = await _summaries(
            session,
            scope=scope,
            checkpoints=checkpoints,
            dependencies=dependencies,
            validate_rows=True,
            for_update=False,
            verify_completed=True,
            require_completed_data_checksum=False,
        )
        return _result(scope=scope, summaries=summaries)


def _validate_restore_bounds(
    snapshot_bounds: Mapping[str, tuple[int, int]],
) -> dict[str, tuple[int, int]]:
    if not isinstance(snapshot_bounds, Mapping) or set(snapshot_bounds) != set(
        HRT_CHILD_OWNERSHIP_BACKFILL_TABLES
    ):
        raise HrtChildOwnershipBackfillValidationError(
            "snapshot_bounds must contain the exact HRT child table catalog"
        )
    result: dict[str, tuple[int, int]] = {}
    for table_name in HRT_CHILD_OWNERSHIP_BACKFILL_TABLES:
        pair = snapshot_bounds[table_name]
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise HrtChildOwnershipBackfillValidationError(
                "each snapshot bound must be an exact pair"
            )
        high_watermark, snapshot_rows = pair
        if (
            isinstance(high_watermark, bool)
            or not isinstance(high_watermark, int)
            or isinstance(snapshot_rows, bool)
            or not isinstance(snapshot_rows, int)
            or not 0 <= high_watermark <= _POSTGRES_INTEGER_MAX
            or not 0 <= snapshot_rows <= _POSTGRES_INTEGER_MAX
            or snapshot_rows > high_watermark
            or (high_watermark == 0) != (snapshot_rows == 0)
        ):
            raise HrtChildOwnershipBackfillValidationError(
                "snapshot bounds are not valid PostgreSQL INTEGER ID/count pairs"
            )
        result[table_name] = pair
    return result


async def _lock_current_graph(session: AsyncSession, *, scope: _Scope) -> None:
    del scope  # Existing rows are replaced later in the same transaction.
    for spec in _TABLES:
        # Full replacement must not trust or inspect the old graph. Lock only
        # stable identifiers, in parent -> optional compound -> child order.
        list(
            await session.scalars(
                select(spec.parent_table.c.id)
                .order_by(spec.parent_table.c.id)
                .with_for_update()
            )
        )
        if spec.validate_compound:
            compound_ids = list(
                await session.scalars(
                    select(spec.table.c.compound_id)
                    .where(spec.table.c.compound_id.is_not(None))
                    .distinct()
                    .order_by(spec.table.c.compound_id)
                )
            )
            if compound_ids:
                list(
                    await session.scalars(
                        select(HrtCompound.id)
                        .where(HrtCompound.id.in_(compound_ids))
                        .order_by(HrtCompound.id)
                        .with_for_update()
                    )
                )
        list(
            await session.scalars(
                select(spec.table.c.id)
                .order_by(spec.table.c.id)
                .with_for_update()
            )
        )


async def reset_hrt_child_backfill_for_portability_v1_restore(
    session: AsyncSession,
    *,
    snapshot_bounds: Mapping[str, tuple[int, int]],
) -> None:
    """Rebase exactly two checkpoints before an atomic backup-v1 replacement."""

    bounds = _validate_restore_bounds(snapshot_bounds)
    reset_at = now_utc().replace(microsecond=0)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependencies = await _load_dependencies(session, for_update=True)
        _require_restore_dependencies(dependencies, subject_id=scope.subject_id)
        checkpoints = await _load_checkpoints(session, for_update=True)
        for spec in _TABLES:
            existing = checkpoints.get(spec.phase_key)
            if existing is not None:
                _validate_checkpoint_shape(
                    existing,
                    subject_id=scope.subject_id,
                    expected_phase=spec.phase_key,
                    dependency=False,
                    allow_running=True,
                )
        await _lock_current_graph(session, scope=scope)
        for spec in _TABLES:
            high_watermark, snapshot_rows = bounds[spec.name]
            status = (
                HrtChildOwnershipBackfillStatus.COMPLETED
                if (high_watermark, snapshot_rows) == (0, 0)
                else HrtChildOwnershipBackfillStatus.RUNNING
            )
            checkpoint = checkpoints.get(spec.phase_key)
            if checkpoint is None:
                checkpoint = OwnershipBackfillCheckpoint(
                    phase_key=spec.phase_key,
                    subject_id=scope.subject_id,
                    status=status.value,
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
                        if status is HrtChildOwnershipBackfillStatus.COMPLETED
                        else None
                    ),
                )
                session.add(checkpoint)
            checkpoint.status = status.value
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
                if status is HrtChildOwnershipBackfillStatus.COMPLETED
                else None
            )
        await session.flush()


async def run_hrt_child_ownership_backfill_batch(
    session: AsyncSession,
    *,
    batch_size: int,
) -> HrtChildOwnershipBackfillBatchResult:
    """Advance the first incomplete fixed HRT child table by one PK batch."""

    size = _validate_batch_size(batch_size)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependencies = await _load_dependencies(session, for_update=True)
        _require_apply_dependencies(dependencies, subject_id=scope.subject_id)
        checkpoints = await _load_checkpoints(session, for_update=True)
        summaries = await _summaries(
            session,
            scope=scope,
            checkpoints=checkpoints,
            dependencies=dependencies,
            validate_rows=False,
            for_update=False,
            verify_completed=False,
        )
        # Every appended tail must already satisfy strict dual-write even while
        # a different fixed table is the current batch target.
        for summary in summaries:
            if summary.rows_above:
                await _scan_table(
                    session,
                    spec=summary.spec,
                    scope=scope,
                    high_watermark=summary.high_watermark,
                    parent_high_watermark=_parent_high_watermark(
                        summary.spec, dependencies
                    ),
                    checkpoint_cursor=None,
                    for_update=True,
                    start_after=summary.high_watermark,
                )
        target = next(
            (
                summary
                for summary in summaries
                if summary.checkpoint is None
                or summary.checkpoint.status != "completed"
            ),
            None,
        )
        if target is None:
            checked = await _summaries(
                session,
                scope=scope,
                checkpoints=checkpoints,
                dependencies=dependencies,
                validate_rows=True,
                for_update=True,
                verify_completed=True,
                require_completed_data_checksum=False,
            )
            aggregate = _result(scope=scope, summaries=checked)
            return _batch_result(
                aggregate,
                batch_table=_TABLES[-1].name,
                scanned=0,
                updated=0,
                unchanged=0,
            )
        checkpoint = target.checkpoint
        if checkpoint is None:
            checkpoint = OwnershipBackfillCheckpoint(
                phase_key=target.spec.phase_key,
                subject_id=scope.subject_id,
                status="running",
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
        links = await _project_child_links(
            session, spec=target.spec, child_ids=batch_ids
        )
        parent_ids = sorted({link.parent_id for link in links.values()})
        for parent_id in parent_ids:
            await _load_parent(
                session,
                spec=target.spec,
                parent_id=parent_id,
                scope=scope,
                parent_high_watermark=_parent_high_watermark(
                    target.spec, dependencies
                ),
                for_update=True,
            )
        await _lock_compounds_for_links(
            session,
            spec=target.spec,
            links=links,
            for_update=True,
        )

    before = checkpoint.data_checksum_before
    after = checkpoint.data_checksum_after
    ownership = checkpoint.ownership_checksum_after
    updated_rows = 0
    unchanged_rows = 0
    target_parent_high_watermark = _parent_high_watermark(
        target.spec, dependencies
    )
    for row_id in batch_ids:
        row = await _load_full_child(
            session, spec=target.spec, row_id=row_id, for_update=True
        )
        _require_projected_child_links(target.spec, row, links[row_id])
        changed = await _validate_child_row(
            session,
            spec=target.spec,
            row=row,
            scope=scope,
            high_watermark=checkpoint.scan_high_watermark_id,
            parent_high_watermark=target_parent_high_watermark,
            for_update=False,
        )
        before = _extend_checksum(before, _data_envelope(target.spec, row))
        if changed:
            values: dict[str, Any] = {"subject_id": scope.subject_id}
            if "updated_at" in target.spec.table.c:
                values["updated_at"] = row._mapping["updated_at"]
            await session.execute(
                update(target.spec.table)
                .where(target.spec.table.c.id == row_id)
                .values(**values)
            )
            updated_rows += 1
        else:
            unchanged_rows += 1
        current = await _load_full_child(
            session, spec=target.spec, row_id=row_id, for_update=True
        )
        _require_projected_child_links(
            target.spec, current, links[row_id]
        )
        await _validate_child_row(
            session,
            spec=target.spec,
            row=current,
            scope=scope,
            high_watermark=checkpoint.scan_high_watermark_id,
            parent_high_watermark=target_parent_high_watermark,
            for_update=False,
        )
        after = _extend_checksum(after, _data_envelope(target.spec, current))
        ownership = _extend_checksum(
            ownership, _ownership_envelope(target.spec, current)
        )
    if before != after:
        raise HrtChildOwnershipBackfillStateError(
            "HRT child data changed while ownership was backfilled"
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
            parent_high_watermark=target_parent_high_watermark,
            for_update=True,
            require_data_checksum=True,
        )
        checkpoint.last_scanned_id = checkpoint.scan_high_watermark_id
        checkpoint.status = "completed"
        checkpoint.completed_at = now_utc()
    await session.flush()

    refreshed: list[_TableSummary] = []
    for summary in summaries:
        if summary.spec.name == target.spec.name:
            refreshed.append(
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
            refreshed.append(summary)
    aggregate = _result(scope=scope, summaries=refreshed)
    if aggregate.completed:
        checked = await _summaries(
            session,
            scope=scope,
            checkpoints=checkpoints,
            dependencies=dependencies,
            validate_rows=True,
            for_update=True,
            verify_completed=True,
            require_completed_data_checksum=True,
        )
        aggregate = _result(scope=scope, summaries=checked)
    return _batch_result(
        aggregate,
        batch_table=target.spec.name,
        scanned=len(batch_ids),
        updated=updated_rows,
        unchanged=unchanged_rows,
    )


__all__ = [
    "DEFAULT_HRT_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES",
    "HRT_CHILD_OWNERSHIP_BACKFILL_PHASE",
    "HRT_CHILD_OWNERSHIP_BACKFILL_TABLES",
    "HrtChildOwnershipBackfillBatchResult",
    "HrtChildOwnershipBackfillDependencyError",
    "HrtChildOwnershipBackfillError",
    "HrtChildOwnershipBackfillIdentityError",
    "HrtChildOwnershipBackfillPreflightResult",
    "HrtChildOwnershipBackfillProvenanceError",
    "HrtChildOwnershipBackfillStateError",
    "HrtChildOwnershipBackfillStatus",
    "HrtChildOwnershipBackfillValidationError",
    "MAX_HRT_CHILD_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "preflight_hrt_child_ownership_backfill",
    "reset_hrt_child_backfill_for_portability_v1_restore",
    "run_hrt_child_ownership_backfill_batch",
]
