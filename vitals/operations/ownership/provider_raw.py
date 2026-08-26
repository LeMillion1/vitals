"""Bounded ownership backfill for reviewed provider/raw-linked top-level rows.

Stage-3D derives only subject and integration-connection ownership from each
row's exact linked Stage-3A raw payload.  Historical actor attribution is never
invented or rewritten.  The service owns no transaction boundary: one call
advances at most one fixed table by one batch and leaves commit to its caller.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
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
from vitals.models.garmin import GarminActivity, GarminDaily, GarminIntraday
from vitals.models.hevy import HevyExercise, HevySet, HevyWorkout
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.operations.ownership.hrt_child import (
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.normalized import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.raw import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.tenancy_bootstrap import LEGACY_ACCOUNT_DISCRIMINATOR
from vitals.utils.timeutils import now_utc


PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE = "stage3.provider_raw_linked.v1"
DEFAULT_PROVIDER_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE = 250
MAX_PROVIDER_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE = 1000

_PREFLIGHT_PAGE_SIZE = 1000
_FULL_ROW_MATERIALIZATION_SIZE = 1
_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_CONNECTION_STATUSES = frozenset(
    {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    }
)
_PRIOR_CHECKPOINT_PHASES = tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values()) + tuple(
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values()
)


class ProviderRawOwnershipBackfillStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"
    RESTORE_BLOCKED = "restore_blocked"


class ProviderRawOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed Stage-3D failures."""


class ProviderRawOwnershipBackfillValidationError(
    ProviderRawOwnershipBackfillError, ValueError
):
    """A caller argument or persisted scalar is outside the frozen contract."""


class ProviderRawOwnershipBackfillIdentityError(
    ProviderRawOwnershipBackfillError
):
    """The sole active owner/subject graph is unavailable."""


class ProviderRawOwnershipBackfillDependencyError(
    ProviderRawOwnershipBackfillError
):
    """The exact completed Stage-3A checkpoint is unavailable."""


class ProviderRawOwnershipBackfillStateError(
    ProviderRawOwnershipBackfillError
):
    """Checkpoint progress or normalized ownership is inconsistent."""


class ProviderRawOwnershipBackfillProvenanceError(
    ProviderRawOwnershipBackfillError
):
    """A normalized/raw/connection provenance graph is not reviewed."""


class ProviderRawOwnershipBackfillDuplicateError(
    ProviderRawOwnershipBackfillError
):
    """Rows collide under a reviewed future scoped natural key."""


@dataclass(frozen=True, slots=True)
class ProviderRawOwnershipBackfillPreflightResult:
    phase_key: str
    subject_id: uuid.UUID
    status: ProviderRawOwnershipBackfillStatus
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
        return self.status is ProviderRawOwnershipBackfillStatus.COMPLETED

    def to_safe_dict(self) -> dict[str, str | int]:
        """Project control-plane counters and digests, never row/root IDs."""

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
class ProviderRawOwnershipBackfillBatchResult(
    ProviderRawOwnershipBackfillPreflightResult
):
    batch_table: str
    batch_scanned_rows: int
    batch_updated_rows: int
    batch_unchanged_rows: int

    @property
    def changed(self) -> bool:
        return self.batch_updated_rows > 0

    def to_safe_dict(self) -> dict[str, str | int]:
        projection = ProviderRawOwnershipBackfillPreflightResult.to_safe_dict(self)
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
    model: Any
    provider: IntegrationProvider
    allowed_sources: frozenset[str]
    raw_external_kind: str
    natural_key: str | None

    @property
    def table(self) -> Table:
        return self.model.__table__

    @property
    def name(self) -> str:
        return self.table.name

    @property
    def phase_key(self) -> str:
        return f"{PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE}.{self.name}"


_TABLES: tuple[_TableSpec, ...] = (
    _TableSpec(
        GarminDaily,
        IntegrationProvider.GARMIN,
        frozenset(
            {Source.GARMIN_API.value, Source.HEALTH_AUTO_EXPORT.value}
        ),
        "daily",
        "date",
    ),
    _TableSpec(
        GarminActivity,
        IntegrationProvider.GARMIN,
        frozenset({Source.GARMIN_API.value}),
        "activity",
        "external_id",
    ),
    _TableSpec(
        GarminIntraday,
        IntegrationProvider.GARMIN,
        frozenset({Source.GARMIN_API.value}),
        "daily",
        None,
    ),
    _TableSpec(
        HevyWorkout,
        IntegrationProvider.HEVY,
        frozenset({Source.HEVY_API.value}),
        "hevy",
        "external_id",
    ),
)

PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES = tuple(spec.name for spec in _TABLES)
PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES = MappingProxyType(
    {spec.name: spec.phase_key for spec in _TABLES}
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


def _validate_batch_size(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_PROVIDER_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE
    ):
        raise ProviderRawOwnershipBackfillValidationError(
            "batch_size must be an integer from 1 to "
            f"{MAX_PROVIDER_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE}"
        )
    return value


def _as_nonnegative_int(value: Any, *, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _POSTGRES_INTEGER_MAX
    ):
        raise ProviderRawOwnershipBackfillStateError(
            f"checkpoint {field} is not a PostgreSQL INTEGER count"
        )
    return value


def _dependency_nonnegative_int(value: Any, *, field: str) -> int:
    try:
        return _as_nonnegative_int(value, field=field)
    except ProviderRawOwnershipBackfillStateError:
        raise ProviderRawOwnershipBackfillDependencyError(
            f"dependency checkpoint {field} is invalid"
        ) from None


def _valid_lifecycle(
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection,
) -> bool:
    started_at = checkpoint.started_at
    updated_at = checkpoint.updated_at
    completed_at = checkpoint.completed_at
    if not isinstance(started_at, datetime) or not isinstance(updated_at, datetime):
        return False
    if updated_at < started_at:
        return False
    return completed_at is None or (
        isinstance(completed_at, datetime) and completed_at >= started_at
    )


async def _load_scope(session: AsyncSession, *, for_update: bool) -> _Scope:
    stmt = (
        select(
            HealthSubject.id,
            HealthSubject.owner_user_id,
            User.status,
        )
        .join(User, User.id == HealthSubject.owner_user_id)
        .order_by(HealthSubject.id)
        .limit(2)
    )
    if for_update:
        stmt = stmt.with_for_update(of=(HealthSubject, User))
    rows = list(await session.execute(stmt))
    if len(rows) != 1:
        raise ProviderRawOwnershipBackfillIdentityError(
            "Stage-3D requires exactly one health subject"
        )
    subject_id, owner_user_id, status = rows[0]
    if (
        not isinstance(subject_id, uuid.UUID)
        or not isinstance(owner_user_id, uuid.UUID)
        or status != UserStatus.ACTIVE.value
    ):
        raise ProviderRawOwnershipBackfillIdentityError(
            "Stage-3D requires one active authoritative owner"
        )
    return _Scope(subject_id=subject_id, owner_user_id=owner_user_id)


def _checkpoint_projection(row: Any) -> _CheckpointProjection:
    return _CheckpointProjection(
        phase_key=row.phase_key,
        subject_id=row.subject_id,
        status=row.status,
        scan_high_watermark_id=row.scan_high_watermark_id,
        snapshot_rows=row.snapshot_rows,
        last_scanned_id=row.last_scanned_id,
        scanned_rows=row.scanned_rows,
        updated_rows=row.updated_rows,
        unchanged_rows=row.unchanged_rows,
        data_checksum_before=row.data_checksum_before,
        data_checksum_after=row.data_checksum_after,
        ownership_checksum_after=row.ownership_checksum_after,
        started_at=row.started_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


async def _load_raw_dependency(
    session: AsyncSession, *, for_update: bool
) -> OwnershipBackfillCheckpoint | _CheckpointProjection | None:
    stmt = select(OwnershipBackfillCheckpoint).where(
        OwnershipBackfillCheckpoint.phase_key == RAW_OWNERSHIP_BACKFILL_PHASE
    ).execution_options(populate_existing=True)
    if for_update:
        stmt = stmt.with_for_update()
    row = await session.scalar(stmt)
    if row is None or for_update:
        return row
    return _checkpoint_projection(row)


def _require_completed_dependency(
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection | None,
    *,
    scope: _Scope,
) -> None:
    if checkpoint is None:
        raise ProviderRawOwnershipBackfillDependencyError(
            "Stage-3A raw ownership backfill has not started"
        )
    if checkpoint.phase_key != RAW_OWNERSHIP_BACKFILL_PHASE:
        raise ProviderRawOwnershipBackfillDependencyError(
            "Stage-3A raw ownership checkpoint has the wrong phase"
        )
    if checkpoint.subject_id != scope.subject_id:
        raise ProviderRawOwnershipBackfillDependencyError(
            "Stage-3A raw ownership belongs to another subject"
        )
    if checkpoint.status != "completed":
        raise ProviderRawOwnershipBackfillDependencyError(
            "Stage-3A raw ownership must be completed"
        )
    snapshot = _dependency_nonnegative_int(
        checkpoint.snapshot_rows, field="snapshot_rows"
    )
    scanned = _dependency_nonnegative_int(
        checkpoint.scanned_rows, field="scanned_rows"
    )
    updated = _dependency_nonnegative_int(
        checkpoint.updated_rows, field="updated_rows"
    )
    unchanged = _dependency_nonnegative_int(
        checkpoint.unchanged_rows, field="unchanged_rows"
    )
    high = _dependency_nonnegative_int(
        checkpoint.scan_high_watermark_id, field="scan_high_watermark_id"
    )
    last = _dependency_nonnegative_int(
        checkpoint.last_scanned_id, field="last_scanned_id"
    )
    if (
        snapshot != scanned
        or scanned != updated + unchanged
        or high != last
        or snapshot > high
        or checkpoint.completed_at is None
        or checkpoint.data_checksum_before != checkpoint.data_checksum_after
        or not _valid_lifecycle(checkpoint)
    ):
        raise ProviderRawOwnershipBackfillDependencyError(
            "Stage-3A raw ownership checkpoint is incomplete"
        )
    for digest in (
        checkpoint.data_checksum_before,
        checkpoint.data_checksum_after,
        checkpoint.ownership_checksum_after,
    ):
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ProviderRawOwnershipBackfillDependencyError(
                "Stage-3A raw ownership checkpoint has invalid evidence"
            )


def _require_restore_dependency(
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection | None,
    *,
    scope: _Scope,
) -> None:
    if checkpoint is None or checkpoint.phase_key != RAW_OWNERSHIP_BACKFILL_PHASE:
        raise ProviderRawOwnershipBackfillDependencyError(
            "Stage-3A raw ownership checkpoint is unavailable"
        )
    if checkpoint.subject_id != scope.subject_id:
        raise ProviderRawOwnershipBackfillDependencyError(
            "Stage-3A raw ownership belongs to another subject"
        )
    if checkpoint.status == "completed":
        _require_completed_dependency(checkpoint, scope=scope)
        return
    if checkpoint.status != "restore_blocked":
        raise ProviderRawOwnershipBackfillDependencyError(
            "Stage-3A raw ownership is not restore-terminal"
        )
    high = _dependency_nonnegative_int(
        checkpoint.scan_high_watermark_id, field="scan_high_watermark_id"
    )
    snapshot = _dependency_nonnegative_int(
        checkpoint.snapshot_rows, field="snapshot_rows"
    )
    if (
        high == 0
        or snapshot == 0
        or snapshot > high
        or checkpoint.last_scanned_id != 0
        or checkpoint.scanned_rows != 0
        or checkpoint.updated_rows != 0
        or checkpoint.unchanged_rows != 0
        or checkpoint.data_checksum_before != _EMPTY_SHA256
        or checkpoint.data_checksum_after != _EMPTY_SHA256
        or checkpoint.ownership_checksum_after != _EMPTY_SHA256
        or checkpoint.completed_at is not None
        or not _valid_lifecycle(checkpoint)
    ):
        raise ProviderRawOwnershipBackfillDependencyError(
            "Stage-3A restore-blocked checkpoint is malformed"
        )


async def _load_prior_dependencies(
    session: AsyncSession,
    *,
    for_update: bool,
) -> dict[str, OwnershipBackfillCheckpoint | _CheckpointProjection]:
    stmt = (
        select(OwnershipBackfillCheckpoint)
        .where(OwnershipBackfillCheckpoint.phase_key.in_(_PRIOR_CHECKPOINT_PHASES))
        .order_by(OwnershipBackfillCheckpoint.phase_key)
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    rows = list(await session.scalars(stmt))
    return {
        row.phase_key: row if for_update else _checkpoint_projection(row)
        for row in rows
    }


def _require_prior_dependencies(
    checkpoints: dict[
        str, OwnershipBackfillCheckpoint | _CheckpointProjection
    ],
    *,
    scope: _Scope,
    restore: bool,
) -> None:
    if set(checkpoints) != set(_PRIOR_CHECKPOINT_PHASES):
        raise ProviderRawOwnershipBackfillDependencyError(
            "Stage-3B/3C ownership checkpoints are incomplete"
        )
    allowed_statuses = {"running", "completed"} if restore else {"completed"}
    for phase in _PRIOR_CHECKPOINT_PHASES:
        checkpoint = checkpoints[phase]
        if checkpoint.phase_key != phase:
            raise ProviderRawOwnershipBackfillDependencyError(
                "a Stage-3B/3C checkpoint phase drifted"
            )
        if checkpoint.subject_id != scope.subject_id:
            raise ProviderRawOwnershipBackfillDependencyError(
                "a Stage-3B/3C checkpoint belongs to another subject"
            )
        if checkpoint.status not in allowed_statuses:
            raise ProviderRawOwnershipBackfillDependencyError(
                "Stage-3B/3C ownership is not in the required state"
            )
        if not _valid_lifecycle(checkpoint):
            raise ProviderRawOwnershipBackfillDependencyError(
                "a Stage-3B/3C checkpoint has an invalid lifecycle"
            )
        high = _dependency_nonnegative_int(
            checkpoint.scan_high_watermark_id,
            field="scan_high_watermark_id",
        )
        snapshot = _dependency_nonnegative_int(
            checkpoint.snapshot_rows,
            field="snapshot_rows",
        )
        last = _dependency_nonnegative_int(
            checkpoint.last_scanned_id,
            field="last_scanned_id",
        )
        scanned = _dependency_nonnegative_int(
            checkpoint.scanned_rows,
            field="scanned_rows",
        )
        updated = _dependency_nonnegative_int(
            checkpoint.updated_rows,
            field="updated_rows",
        )
        unchanged = _dependency_nonnegative_int(
            checkpoint.unchanged_rows,
            field="unchanged_rows",
        )
        if (
            snapshot > high
            or last > high
            or scanned > snapshot
            or scanned != updated + unchanged
        ):
            raise ProviderRawOwnershipBackfillDependencyError(
                "a Stage-3B/3C checkpoint has inconsistent counters"
            )
        for digest in (
            checkpoint.data_checksum_before,
            checkpoint.data_checksum_after,
            checkpoint.ownership_checksum_after,
        ):
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise ProviderRawOwnershipBackfillDependencyError(
                    "a Stage-3B/3C checkpoint has invalid evidence"
                )
        if checkpoint.data_checksum_before != checkpoint.data_checksum_after:
            raise ProviderRawOwnershipBackfillDependencyError(
                "a Stage-3B/3C checkpoint has divergent data evidence"
            )
        if checkpoint.status == "completed" and (
            last != high
            or scanned != snapshot
            or checkpoint.completed_at is None
        ):
            raise ProviderRawOwnershipBackfillDependencyError(
                "a completed Stage-3B/3C checkpoint is incomplete"
            )
        if checkpoint.status == "running" and checkpoint.completed_at is not None:
            raise ProviderRawOwnershipBackfillDependencyError(
                "a running Stage-3B/3C checkpoint is terminal"
            )
        if restore and checkpoint.status == "running" and (
            high == 0 or snapshot == 0
        ):
            raise ProviderRawOwnershipBackfillDependencyError(
                "a restore-running Stage-3B/3C checkpoint must be nonempty"
            )


async def _load_checkpoints(
    session: AsyncSession, *, for_update: bool
) -> dict[str, OwnershipBackfillCheckpoint | _CheckpointProjection]:
    phases = tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    stmt = (
        select(OwnershipBackfillCheckpoint)
        .where(OwnershipBackfillCheckpoint.phase_key.in_(phases))
        .order_by(OwnershipBackfillCheckpoint.phase_key)
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    rows = list(await session.scalars(stmt))
    return {
        row.phase_key: row if for_update else _checkpoint_projection(row)
        for row in rows
    }


def _validate_checkpoint(
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection,
    *,
    spec: _TableSpec,
    scope: _Scope,
) -> None:
    if checkpoint.phase_key != spec.phase_key:
        raise ProviderRawOwnershipBackfillStateError(
            "provider/raw checkpoint phase drifted"
        )
    if checkpoint.subject_id != scope.subject_id:
        raise ProviderRawOwnershipBackfillStateError(
            "provider/raw checkpoint belongs to another subject"
        )
    if checkpoint.status not in {"running", "completed", "restore_blocked"}:
        raise ProviderRawOwnershipBackfillStateError(
            "provider/raw checkpoint has an unsupported state"
        )
    if not _valid_lifecycle(checkpoint):
        raise ProviderRawOwnershipBackfillStateError(
            "provider/raw checkpoint has an invalid lifecycle"
        )
    high = _as_nonnegative_int(
        checkpoint.scan_high_watermark_id, field="scan_high_watermark_id"
    )
    snapshot = _as_nonnegative_int(
        checkpoint.snapshot_rows, field="snapshot_rows"
    )
    last = _as_nonnegative_int(checkpoint.last_scanned_id, field="last_scanned_id")
    scanned = _as_nonnegative_int(checkpoint.scanned_rows, field="scanned_rows")
    updated = _as_nonnegative_int(checkpoint.updated_rows, field="updated_rows")
    unchanged = _as_nonnegative_int(
        checkpoint.unchanged_rows, field="unchanged_rows"
    )
    if (
        last > high
        or snapshot > high
        or scanned > snapshot
        or scanned != updated + unchanged
    ):
        raise ProviderRawOwnershipBackfillStateError(
            "provider/raw checkpoint counters are inconsistent"
        )
    for digest in (
        checkpoint.data_checksum_before,
        checkpoint.data_checksum_after,
        checkpoint.ownership_checksum_after,
    ):
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ProviderRawOwnershipBackfillStateError(
                "provider/raw checkpoint digest is invalid"
            )
    if (
        checkpoint.status in {"running", "completed"}
        and checkpoint.data_checksum_before != checkpoint.data_checksum_after
    ):
        raise ProviderRawOwnershipBackfillStateError(
            "provider/raw checkpoint data evidence diverged"
        )
    if checkpoint.status == "completed" and (
        last != high or scanned != snapshot or checkpoint.completed_at is None
    ):
        raise ProviderRawOwnershipBackfillStateError(
            "completed provider/raw checkpoint is incomplete"
        )
    if checkpoint.status == "running" and checkpoint.completed_at is not None:
        raise ProviderRawOwnershipBackfillStateError(
            "running provider/raw checkpoint has a completion timestamp"
        )
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
        raise ProviderRawOwnershipBackfillStateError(
            "restore-blocked provider/raw checkpoint is malformed"
        )


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProviderRawOwnershipBackfillStateError(
                "a reviewed row contains a non-finite float"
            )
        return ["float", value.hex()]
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    if isinstance(value, uuid.UUID):
        return ["uuid", str(value)]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat()]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, time):
        return ["time", value.isoformat()]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, (list, tuple)):
        return ["array", [_canonical_value(item) for item in value]]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ProviderRawOwnershipBackfillStateError(
                "a reviewed row contains a non-string JSON key"
            )
        return [
            "object",
            [[key, _canonical_value(value[key])] for key in sorted(value)],
        ]
    raise ProviderRawOwnershipBackfillStateError(
        "a reviewed row cannot be represented canonically"
    )


def _extend_checksum(previous: str, envelope: Any) -> str:
    if _SHA256_RE.fullmatch(previous) is None:
        raise ProviderRawOwnershipBackfillStateError(
            "rolling checksum state is invalid"
        )
    try:
        encoded = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProviderRawOwnershipBackfillStateError(
            "a reviewed row cannot be represented canonically"
        ) from exc
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
            if column.name
            not in {
                "subject_id",
                "actor_user_id",
                "integration_connection_id",
            }
        ],
    ]


def _ownership_envelope(spec: _TableSpec, row: Any) -> list[Any]:
    actor_user_id = row._mapping.get("actor_user_id")
    return [
        spec.name,
        row._mapping["id"],
        str(row._mapping["subject_id"])
        if row._mapping["subject_id"] is not None
        else None,
        str(actor_user_id) if actor_user_id is not None else None,
        str(row._mapping["integration_connection_id"])
        if row._mapping["integration_connection_id"] is not None
        else None,
    ]


async def _load_full_row(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    row_id: int,
) -> Any:
    row = (
        await session.execute(
            select(spec.table)
            .where(spec.table.c.id == row_id)
            .limit(_FULL_ROW_MATERIALIZATION_SIZE)
        )
    ).one_or_none()
    if row is None:
        raise ProviderRawOwnershipBackfillStateError(
            "a reviewed normalized row disappeared"
        )
    return row


async def _load_raw_graph(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    row: Any,
) -> tuple[Any, Any]:
    raw_id = row._mapping["raw_payload_id"]
    if isinstance(raw_id, bool) or not isinstance(raw_id, int):
        raise ProviderRawOwnershipBackfillProvenanceError(
            "a provider row lacks an exact raw payload link"
        )
    result = (
        await session.execute(
            select(RawPayload, IntegrationConnection)
            .join(
                IntegrationConnection,
                IntegrationConnection.id == RawPayload.integration_connection_id,
            )
            .where(RawPayload.id == raw_id)
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if result is None:
        raise ProviderRawOwnershipBackfillProvenanceError(
            "a provider row has a missing raw/connection root"
        )
    return result[0], result[1]


def _normalized_external(
    spec: _TableSpec,
    row: Any,
    *,
    raw_source: str,
) -> str:
    if spec.raw_external_kind == "daily":
        value = row._mapping["date"]
        if not isinstance(value, date) or isinstance(value, datetime):
            raise ProviderRawOwnershipBackfillProvenanceError(
                "a Garmin daily-linked row has an invalid date"
            )
        prefix = "hae:" if raw_source == Source.HEALTH_AUTO_EXPORT.value else "daily:"
        return f"{prefix}{value.isoformat()}"
    external = row._mapping["external_id"]
    if not isinstance(external, str) or not external or external.strip() != external:
        raise ProviderRawOwnershipBackfillProvenanceError(
            "a provider row has a non-canonical external id"
        )
    if spec.raw_external_kind == "activity":
        return f"activity:{external}"
    return external


def _payload_external_id(spec: _TableSpec, payload: dict[str, Any]) -> str | None:
    if spec.raw_external_kind == "activity":
        return str(
            payload.get("activityId") or payload.get("activityid") or ""
        ).strip()
    if spec.raw_external_kind == "hevy":
        return str(payload.get("id") or "").strip()
    return None


def _validate_graph(
    *,
    spec: _TableSpec,
    row: Any,
    raw: RawPayload,
    connection: IntegrationConnection,
    scope: _Scope,
    historical: bool,
) -> bool:
    if row._mapping["domain"] != (
        Domain.GARMIN.value
        if spec.provider is IntegrationProvider.GARMIN
        else Domain.WORKOUTS.value
    ):
        raise ProviderRawOwnershipBackfillProvenanceError(
            "a provider row has an unexpected domain"
        )
    source = row._mapping["source"]
    if source not in spec.allowed_sources:
        raise ProviderRawOwnershipBackfillProvenanceError(
            "a provider row has an unexpected source"
        )
    if raw.domain != row._mapping["domain"]:
        raise ProviderRawOwnershipBackfillProvenanceError(
            "normalized and raw provenance do not match"
        )
    historical_hae_bridge = (
        spec.model is GarminDaily
        and historical
        and source == Source.HEALTH_AUTO_EXPORT.value
        and raw.source == Source.GARMIN_API.value
    )
    if raw.source != source and not historical_hae_bridge:
        raise ProviderRawOwnershipBackfillProvenanceError(
            "normalized and raw sources do not match"
        )
    expected_external = _normalized_external(
        spec,
        row,
        raw_source=raw.source,
    )
    if raw.external_id != expected_external:
        raise ProviderRawOwnershipBackfillProvenanceError(
            "normalized and raw natural keys do not match"
        )
    if raw.file_asset_id is not None or not isinstance(raw.payload, dict):
        raise ProviderRawOwnershipBackfillProvenanceError(
            "provider account raw provenance is not an exact JSON root"
        )
    payload_external = _payload_external_id(spec, raw.payload)
    if payload_external is not None:
        normalized_external = row._mapping["external_id"]
        if not payload_external or payload_external != normalized_external:
            raise ProviderRawOwnershipBackfillProvenanceError(
                "raw payload identity does not match its normalized row"
            )
    if (
        raw.subject_id != scope.subject_id
        or not isinstance(raw.integration_connection_id, uuid.UUID)
        or raw.actor_user_id not in {None, scope.owner_user_id}
    ):
        raise ProviderRawOwnershipBackfillProvenanceError(
            "linked raw ownership is not the reviewed Stage-3A root"
        )
    if (
        connection.id != raw.integration_connection_id
        or connection.subject_id != scope.subject_id
        or connection.provider != spec.provider.value
        or connection.connection_type != IntegrationConnectionType.ACCOUNT.value
        or connection.status not in _ALLOWED_CONNECTION_STATUSES
    ):
        raise ProviderRawOwnershipBackfillProvenanceError(
            "linked raw connection is not the reviewed provider account"
        )
    subject_id = row._mapping["subject_id"]
    connection_id = row._mapping["integration_connection_id"]
    actor_id = row._mapping.get("actor_user_id")
    if actor_id not in {None, scope.owner_user_id}:
        raise ProviderRawOwnershipBackfillProvenanceError(
            "normalized actor is not the sole owner or actorless"
        )
    if subject_id is None and connection_id is None:
        if actor_id is not None:
            raise ProviderRawOwnershipBackfillStateError(
                "an actor-only provider row cannot be adopted"
            )
        if connection.external_account_discriminator != LEGACY_ACCOUNT_DISCRIMINATOR:
            raise ProviderRawOwnershipBackfillProvenanceError(
                "historical adoption requires the reviewed legacy account root"
            )
        return True
    if subject_id == raw.subject_id and connection_id is None:
        if not historical:
            raise ProviderRawOwnershipBackfillStateError(
                "a live provider row lacks its integration connection"
            )
        if connection.external_account_discriminator != LEGACY_ACCOUNT_DISCRIMINATOR:
            raise ProviderRawOwnershipBackfillProvenanceError(
                "historical adoption requires the reviewed legacy account root"
            )
        return True
    if subject_id == raw.subject_id and connection_id == raw.integration_connection_id:
        return False
    if subject_id is None or connection_id is None:
        raise ProviderRawOwnershipBackfillStateError(
            "a provider row has partial subject/connection ownership"
        )
    raise ProviderRawOwnershipBackfillStateError(
        "a provider row has foreign subject/connection ownership"
    )


def _validate_child_shape(
    *,
    child_subject_id: uuid.UUID | None,
    child_connection_id: uuid.UUID | None,
    parent_subject_id: uuid.UUID,
    parent_connection_id: uuid.UUID,
) -> None:
    if (child_subject_id, child_connection_id) in {
        (None, None),
        (parent_subject_id, None),
        (parent_subject_id, parent_connection_id),
    }:
        return
    if child_subject_id is None or child_connection_id is None:
        raise ProviderRawOwnershipBackfillStateError(
            "a Hevy child has unsafe partial ownership"
        )
    raise ProviderRawOwnershipBackfillStateError(
        "a Hevy child has foreign ownership"
    )


async def _validate_hevy_children(
    session: AsyncSession,
    *,
    workout_id: int,
    subject_id: uuid.UUID,
    connection_id: uuid.UUID,
    for_update: bool,
) -> None:
    exercise_stmt = (
        select(
            HevyExercise.id,
            HevyExercise.subject_id,
            HevyExercise.integration_connection_id,
        )
        .where(HevyExercise.workout_id == workout_id)
        .order_by(HevyExercise.id)
    )
    if for_update:
        exercise_stmt = exercise_stmt.with_for_update()
    exercises = list(await session.execute(exercise_stmt))
    exercise_ids: list[int] = []
    for exercise in exercises:
        _validate_child_shape(
            child_subject_id=exercise.subject_id,
            child_connection_id=exercise.integration_connection_id,
            parent_subject_id=subject_id,
            parent_connection_id=connection_id,
        )
        exercise_ids.append(exercise.id)
    if not exercise_ids:
        return
    set_stmt = (
        select(
            HevySet.id,
            HevySet.subject_id,
            HevySet.integration_connection_id,
        )
        .where(HevySet.exercise_id.in_(exercise_ids))
        .order_by(HevySet.id)
    )
    if for_update:
        set_stmt = set_stmt.with_for_update()
    for set_row in await session.execute(set_stmt):
        _validate_child_shape(
            child_subject_id=set_row.subject_id,
            child_connection_id=set_row.integration_connection_id,
            parent_subject_id=subject_id,
            parent_connection_id=connection_id,
        )


async def _row_plan(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    row_id: int,
    scope: _Scope,
    historical: bool,
    lock_children: bool = False,
) -> tuple[Any, RawPayload, bool]:
    row = await _load_full_row(session, spec=spec, row_id=row_id)
    raw, connection = await _load_raw_graph(session, spec=spec, row=row)
    changed = _validate_graph(
        spec=spec,
        row=row,
        raw=raw,
        connection=connection,
        scope=scope,
        historical=historical,
    )
    if spec.model is HevyWorkout:
        await _validate_hevy_children(
            session,
            workout_id=row_id,
            subject_id=raw.subject_id,
            connection_id=raw.integration_connection_id,
            for_update=lock_children,
        )
    return row, raw, changed


async def _load_intraday_batch_graphs(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    row_ids: list[int],
) -> tuple[list[Any], dict[int, tuple[RawPayload, IntegrationConnection]]]:
    """Load one locked intraday page and its repeated roots in bounded queries."""

    rows = list(
        await session.execute(
            select(spec.table)
            .where(spec.table.c.id.in_(row_ids))
            .order_by(spec.table.c.id)
        )
    )
    if [row._mapping["id"] for row in rows] != row_ids:
        raise ProviderRawOwnershipBackfillStateError(
            "an intraday row disappeared after its graph was locked"
        )
    raw_ids = sorted({row._mapping["raw_payload_id"] for row in rows})
    if any(isinstance(raw_id, bool) or not isinstance(raw_id, int) for raw_id in raw_ids):
        raise ProviderRawOwnershipBackfillProvenanceError(
            "an intraday row lacks an exact raw payload link"
        )
    graph_rows = list(
        await session.execute(
            select(RawPayload, IntegrationConnection)
            .join(
                IntegrationConnection,
                IntegrationConnection.id == RawPayload.integration_connection_id,
            )
            .where(RawPayload.id.in_(raw_ids))
            .order_by(RawPayload.id)
            .execution_options(populate_existing=True)
        )
    )
    graphs = {raw.id: (raw, connection) for raw, connection in graph_rows}
    if sorted(graphs) != raw_ids:
        raise ProviderRawOwnershipBackfillProvenanceError(
            "an intraday row has a missing raw/connection root"
        )
    return rows, graphs


def _validate_intraday_batch(
    *,
    spec: _TableSpec,
    rows: list[Any],
    graphs: dict[int, tuple[RawPayload, IntegrationConnection]],
    scope: _Scope,
    historical: bool,
) -> list[tuple[Any, RawPayload, bool]]:
    plans: list[tuple[Any, RawPayload, bool]] = []
    for row in rows:
        raw, connection = graphs[row._mapping["raw_payload_id"]]
        changed = _validate_graph(
            spec=spec,
            row=row,
            raw=raw,
            connection=connection,
            scope=scope,
            historical=historical,
        )
        plans.append((row, raw, changed))
    return plans


async def _run_locked_intraday_batch(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    row_ids: list[int],
    scope: _Scope,
    before: str,
    after: str,
    ownership: str,
) -> tuple[str, str, str, int, int]:
    """Attribute a high-volume intraday page without per-sample round trips."""

    rows, graphs = await _load_intraday_batch_graphs(
        session, spec=spec, row_ids=row_ids
    )
    plans = _validate_intraday_batch(
        spec=spec,
        rows=rows,
        graphs=graphs,
        scope=scope,
        historical=True,
    )
    update_groups: dict[tuple[uuid.UUID, uuid.UUID], list[int]] = {}
    unchanged_rows = 0
    for row, raw, changed in plans:
        before = _extend_checksum(before, _data_envelope(spec, row))
        if changed:
            update_groups.setdefault(
                (raw.subject_id, raw.integration_connection_id), []
            ).append(row._mapping["id"])
        else:
            unchanged_rows += 1
    for (subject_id, connection_id), ids in sorted(
        update_groups.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
    ):
        values: dict[str, Any] = {
            "subject_id": subject_id,
            "integration_connection_id": connection_id,
        }
        if "updated_at" in spec.table.c:
            values["updated_at"] = spec.table.c.updated_at
        await session.execute(
            update(spec.table).where(spec.table.c.id.in_(ids)).values(**values)
        )
    after_rows, after_graphs = await _load_intraday_batch_graphs(
        session, spec=spec, row_ids=row_ids
    )
    for row, _raw, still_changed in _validate_intraday_batch(
        spec=spec,
        rows=after_rows,
        graphs=after_graphs,
        scope=scope,
        historical=True,
    ):
        if still_changed:
            raise ProviderRawOwnershipBackfillStateError(
                "intraday ownership update did not become strict"
            )
        after = _extend_checksum(after, _data_envelope(spec, row))
        ownership = _extend_checksum(ownership, _ownership_envelope(spec, row))
    return (
        before,
        after,
        ownership,
        sum(len(ids) for ids in update_groups.values()),
        unchanged_rows,
    )


async def _lock_graph_for_ids(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    row_ids: list[int],
) -> None:
    if not row_ids:
        return
    expected_row_ids = sorted(set(row_ids))
    if expected_row_ids != row_ids:
        raise ProviderRawOwnershipBackfillStateError(
            "normalized lock targets are not canonical"
        )
    normalized_stmt = (
        select(spec.table.c.id, spec.table.c.raw_payload_id)
        .where(spec.table.c.id.in_(expected_row_ids))
        .order_by(spec.table.c.id)
    )
    normalized_before = [tuple(row) for row in await session.execute(normalized_stmt)]
    if [row[0] for row in normalized_before] != expected_row_ids:
        raise ProviderRawOwnershipBackfillStateError(
            "a normalized provider row disappeared before projection"
        )
    projected_raw_ids = [row[1] for row in normalized_before]
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in projected_raw_ids
    ):
        raise ProviderRawOwnershipBackfillProvenanceError(
            "a provider row lacks an exact raw payload link"
        )
    raw_ids = sorted(set(projected_raw_ids))
    raw_stmt = (
        select(
            RawPayload.id,
            RawPayload.subject_id,
            RawPayload.actor_user_id,
            RawPayload.integration_connection_id,
            RawPayload.domain,
            RawPayload.source,
            RawPayload.external_id,
            RawPayload.file_asset_id,
        )
        .where(RawPayload.id.in_(raw_ids))
        .order_by(RawPayload.id)
    )
    raw_before = [tuple(row) for row in await session.execute(raw_stmt)]
    if [row[0] for row in raw_before] != raw_ids:
        raise ProviderRawOwnershipBackfillProvenanceError(
            "a linked raw payload disappeared before projection"
        )
    projected_connection_ids = [row[3] for row in raw_before]
    if not projected_connection_ids or any(
        not isinstance(value, uuid.UUID) for value in projected_connection_ids
    ):
        raise ProviderRawOwnershipBackfillProvenanceError(
            "a provider row lacks an exact connection-rooted raw link"
        )
    connection_ids = sorted(set(projected_connection_ids))
    connection_stmt = (
        select(
            IntegrationConnection.id,
            IntegrationConnection.subject_id,
            IntegrationConnection.provider,
            IntegrationConnection.connection_type,
            IntegrationConnection.external_account_discriminator,
            IntegrationConnection.status,
        )
        .where(IntegrationConnection.id.in_(connection_ids))
        .order_by(IntegrationConnection.id)
    )
    connection_before = [
        tuple(row) for row in await session.execute(connection_stmt)
    ]
    if [row[0] for row in connection_before] != connection_ids:
        raise ProviderRawOwnershipBackfillProvenanceError(
            "a linked provider connection disappeared before projection"
        )
    locked_connections = [
        tuple(row)
        for row in await session.execute(connection_stmt.with_for_update())
    ]
    if locked_connections != connection_before:
        raise ProviderRawOwnershipBackfillProvenanceError(
            "a linked provider connection changed before locking"
        )
    locked_raw = [
        tuple(row) for row in await session.execute(raw_stmt.with_for_update())
    ]
    if locked_raw != raw_before:
        raise ProviderRawOwnershipBackfillProvenanceError(
            "a linked raw payload changed before locking"
        )
    locked_rows = [
        tuple(row)
        for row in await session.execute(normalized_stmt.with_for_update())
    ]
    if locked_rows != normalized_before:
        raise ProviderRawOwnershipBackfillStateError(
            "a normalized provider/raw link changed before locking"
        )
    normalized_after = [
        tuple(row) for row in await session.execute(normalized_stmt)
    ]
    raw_after = [tuple(row) for row in await session.execute(raw_stmt)]
    connection_after = [
        tuple(row) for row in await session.execute(connection_stmt)
    ]
    if normalized_after != normalized_before:
        raise ProviderRawOwnershipBackfillStateError(
            "a normalized provider/raw link changed across locking"
        )
    if raw_after != raw_before or connection_after != connection_before:
        raise ProviderRawOwnershipBackfillProvenanceError(
            "a provider raw/connection root changed across locking"
        )


async def _max_id(session: AsyncSession, spec: _TableSpec) -> int:
    value = int(await session.scalar(select(func.max(spec.table.c.id))) or 0)
    return _as_nonnegative_int(value, field="high_watermark")


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
        last_scanned=checkpoint.last_scanned_id,
    )
    if snapshot != checkpoint.snapshot_rows or prefix != checkpoint.scanned_rows:
        raise ProviderRawOwnershipBackfillStateError(
            "a provider/raw checkpoint cardinality drifted"
        )
    if checkpoint.scanned_rows + remaining != checkpoint.snapshot_rows:
        raise ProviderRawOwnershipBackfillStateError(
            "a provider/raw checkpoint no longer matches its snapshot"
        )
    return remaining


async def _reject_future_key_duplicates(
    session: AsyncSession, *, spec: _TableSpec | None = None
) -> None:
    for candidate in _TABLES:
        if spec is not None and candidate is not spec:
            continue
        if candidate.natural_key is None:
            continue
        key_column = candidate.table.c[candidate.natural_key]
        duplicate = (
            await session.execute(
                select(func.count())
                .select_from(candidate.table)
                .join(
                    RawPayload,
                    RawPayload.id == candidate.table.c.raw_payload_id,
                )
                .group_by(RawPayload.integration_connection_id, key_column)
                .having(func.count() > 1)
                .limit(1)
            )
        ).first()
        if duplicate is not None:
            raise ProviderRawOwnershipBackfillDuplicateError(
                "a reviewed provider table has a duplicate future scoped key"
            )


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
    last_id = start_after
    rows_above = 0
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
            return rows_above
        if for_update:
            await _lock_graph_for_ids(session, spec=spec, row_ids=ids)
        if spec.model is GarminIntraday:
            rows, graphs = await _load_intraday_batch_graphs(
                session, spec=spec, row_ids=ids
            )
            plans = []
            for row in rows:
                raw, connection = graphs[row._mapping["raw_payload_id"]]
                plans.append(
                    (
                        row,
                        raw,
                        _validate_graph(
                            spec=spec,
                            row=row,
                            raw=raw,
                            connection=connection,
                            scope=scope,
                            historical=row._mapping["id"] <= high_watermark,
                        ),
                    )
                )
        else:
            plans = []
            for row_id in ids:
                plans.append(
                    await _row_plan(
                        session,
                        spec=spec,
                        row_id=row_id,
                        scope=scope,
                        historical=row_id <= high_watermark,
                        lock_children=for_update,
                    )
                )
        for row, _raw, changed in plans:
            row_id = row._mapping["id"]
            if (
                checkpoint_cursor is not None
                and row_id <= checkpoint_cursor
                and changed
            ):
                raise ProviderRawOwnershipBackfillStateError(
                    "a previously scanned provider row requires repair"
                )
            if row_id > high_watermark:
                if changed:
                    raise ProviderRawOwnershipBackfillStateError(
                        "a row above the high-water mark lacks strict S+C"
                    )
                rows_above += 1
        last_id = ids[-1]
        if len(ids) < _PREFLIGHT_PAGE_SIZE:
            return rows_above


async def _verify_final_snapshot(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    scope: _Scope,
    checkpoint: OwnershipBackfillCheckpoint | _CheckpointProjection,
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
        if for_update:
            await _lock_graph_for_ids(session, spec=spec, row_ids=ids)
        if spec.model is GarminIntraday:
            rows, graphs = await _load_intraday_batch_graphs(
                session, spec=spec, row_ids=ids
            )
            plans = _validate_intraday_batch(
                spec=spec,
                rows=rows,
                graphs=graphs,
                scope=scope,
                historical=True,
            )
        else:
            plans = []
            for row_id in ids:
                plans.append(
                    await _row_plan(
                        session,
                        spec=spec,
                        row_id=row_id,
                        scope=scope,
                        historical=True,
                        lock_children=for_update,
                    )
                )
        for row, _raw, changed in plans:
            if changed:
                raise ProviderRawOwnershipBackfillStateError(
                    "a completed provider snapshot requires ownership repair"
                )
            before = _extend_checksum(before, _data_envelope(spec, row))
            ownership = _extend_checksum(
                ownership, _ownership_envelope(spec, row)
            )
        scanned += len(ids)
        last_id = ids[-1]
        if len(ids) < _PREFLIGHT_PAGE_SIZE:
            break
    if scanned != checkpoint.snapshot_rows:
        raise ProviderRawOwnershipBackfillStateError(
            "final provider snapshot cardinality drifted"
        )
    if require_data_checksum and (
        before != checkpoint.data_checksum_before
        or before != checkpoint.data_checksum_after
    ):
        raise ProviderRawOwnershipBackfillStateError(
            "provider data changed during the maintenance window"
        )
    if ownership != checkpoint.ownership_checksum_after:
        raise ProviderRawOwnershipBackfillStateError(
            "completed provider ownership changed"
        )


async def _summaries(
    session: AsyncSession,
    *,
    scope: _Scope,
    checkpoints: dict[str, OwnershipBackfillCheckpoint | _CheckpointProjection],
    for_update: bool,
    validate_rows: bool,
    verify_completed: bool,
    require_completed_data_checksum: bool = False,
    relax_completed_intraday: bool = False,
) -> list[_TableSummary]:
    blocked_group = any(
        checkpoint.status == "restore_blocked"
        for checkpoint in checkpoints.values()
    )
    if not blocked_group:
        await _reject_future_key_duplicates(session)
    result: list[_TableSummary] = []
    for spec in _TABLES:
        checkpoint = checkpoints.get(spec.phase_key)
        relaxed_intraday = False
        if checkpoint is None:
            high = await _max_id(session, spec)
            snapshot = await _count_to(session, spec, high_watermark=high)
            remaining = snapshot
            cursor = None
        else:
            _validate_checkpoint(checkpoint, spec=spec, scope=scope)
            high = checkpoint.scan_high_watermark_id
            snapshot = checkpoint.snapshot_rows
            relaxed_intraday = (
                relax_completed_intraday
                and spec.model is GarminIntraday
                and checkpoint.status == "completed"
            )
            if checkpoint.status == "restore_blocked":
                remaining = checkpoint.snapshot_rows
                cursor = None
            elif relaxed_intraday:
                remaining = 0
                cursor = None
            else:
                remaining = await _validate_counts(
                    session, spec=spec, checkpoint=checkpoint
                )
                cursor = checkpoint.last_scanned_id
        if validate_rows:
            rows_above = await _scan_table(
                session,
                spec=spec,
                scope=scope,
                high_watermark=(0 if relaxed_intraday else high),
                checkpoint_cursor=cursor,
                for_update=for_update,
            )
        else:
            rows_above = int(
                await session.scalar(
                    select(func.count())
                    .select_from(spec.table)
                    .where(spec.table.c.id > high)
                )
                or 0
            )
        if (
            verify_completed
            and checkpoint is not None
            and checkpoint.status == "completed"
            and not relaxed_intraday
        ):
            await _verify_final_snapshot(
                session,
                spec=spec,
                scope=scope,
                checkpoint=checkpoint,
                for_update=for_update,
                require_data_checksum=require_completed_data_checksum,
            )
        result.append(
            _TableSummary(
                spec=spec,
                checkpoint=checkpoint,
                high_watermark=high,
                snapshot_rows=snapshot,
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
) -> ProviderRawOwnershipBackfillPreflightResult:
    checkpoints = [row.checkpoint for row in summaries if row.checkpoint is not None]
    completed = sum(row.status == "completed" for row in checkpoints)
    status = (
        ProviderRawOwnershipBackfillStatus.RESTORE_BLOCKED
        if any(row.status == "restore_blocked" for row in checkpoints)
        else (
            ProviderRawOwnershipBackfillStatus.COMPLETED
            if completed == len(_TABLES)
            else (
                ProviderRawOwnershipBackfillStatus.RUNNING
                if checkpoints
                else ProviderRawOwnershipBackfillStatus.NOT_STARTED
            )
        )
    )
    return ProviderRawOwnershipBackfillPreflightResult(
        phase_key=PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE,
        subject_id=scope.subject_id,
        status=status,
        tables_total=len(_TABLES),
        completed_tables=completed,
        snapshot_rows=sum(row.snapshot_rows for row in summaries),
        scanned_rows=sum(
            row.checkpoint.scanned_rows if row.checkpoint is not None else 0
            for row in summaries
        ),
        updated_rows=sum(
            row.checkpoint.updated_rows if row.checkpoint is not None else 0
            for row in summaries
        ),
        unchanged_rows=sum(
            row.checkpoint.unchanged_rows if row.checkpoint is not None else 0
            for row in summaries
        ),
        remaining_rows=sum(row.remaining_rows for row in summaries),
        rows_above_high_watermark=sum(row.rows_above for row in summaries),
        data_checksum_before=_aggregate_digest(summaries, "data_checksum_before"),
        data_checksum_after=_aggregate_digest(summaries, "data_checksum_after"),
        ownership_checksum_after=_aggregate_digest(
            summaries, "ownership_checksum_after"
        ),
    )


def _batch_result(
    aggregate: ProviderRawOwnershipBackfillPreflightResult,
    *,
    batch_table: str,
    scanned: int,
    updated: int,
    unchanged: int,
) -> ProviderRawOwnershipBackfillBatchResult:
    return ProviderRawOwnershipBackfillBatchResult(
        **{
            field: getattr(aggregate, field)
            for field in (
                ProviderRawOwnershipBackfillPreflightResult.__dataclass_fields__
            )
        },
        batch_table=batch_table,
        batch_scanned_rows=scanned,
        batch_updated_rows=updated,
        batch_unchanged_rows=unchanged,
    )


async def preflight_provider_raw_ownership_backfill(
    session: AsyncSession,
) -> ProviderRawOwnershipBackfillPreflightResult:
    """Validate the fixed provider/raw graph without mutating session state."""

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=False)
        dependency = await _load_raw_dependency(session, for_update=False)
        prior_dependencies = await _load_prior_dependencies(
            session, for_update=False
        )
        checkpoints = await _load_checkpoints(session, for_update=False)
        restore_blocked = any(
            checkpoint.status == "restore_blocked"
            for checkpoint in checkpoints.values()
        )
        completed_group = (
            len(checkpoints) == len(_TABLES)
            and all(
                checkpoint.status == "completed"
                for checkpoint in checkpoints.values()
            )
        )
        empty_completed_group = completed_group and all(
            checkpoint.snapshot_rows == 0
            for checkpoint in checkpoints.values()
        )
        if restore_blocked or empty_completed_group:
            _require_restore_dependency(dependency, scope=scope)
            _require_prior_dependencies(
                prior_dependencies, scope=scope, restore=True
            )
        else:
            _require_completed_dependency(dependency, scope=scope)
            _require_prior_dependencies(
                prior_dependencies, scope=scope, restore=False
            )
        summaries = await _summaries(
            session,
            scope=scope,
            checkpoints=checkpoints,
            for_update=False,
            validate_rows=not restore_blocked,
            verify_completed=not restore_blocked,
            relax_completed_intraday=completed_group,
        )
        return _result(scope=scope, summaries=summaries)


def _validate_snapshot_bounds(
    snapshot_bounds: Any,
) -> dict[str, tuple[int, int]]:
    if not isinstance(snapshot_bounds, dict) or set(snapshot_bounds) != set(
        PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES
    ):
        raise ProviderRawOwnershipBackfillValidationError(
            "snapshot_bounds must contain the exact provider table catalog"
        )
    result: dict[str, tuple[int, int]] = {}
    for table_name in PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES:
        pair = snapshot_bounds[table_name]
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ProviderRawOwnershipBackfillValidationError(
                "each provider snapshot bound must be an exact pair"
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
            raise ProviderRawOwnershipBackfillValidationError(
                "provider snapshot bounds are invalid PostgreSQL INTEGER pairs"
            )
        result[table_name] = pair
    return result


async def _lock_current_provider_graph(session: AsyncSession) -> None:
    raw_ids: set[int] = set()
    for spec in _TABLES:
        raw_ids.update(
            value
            for value in await session.scalars(
                select(spec.table.c.raw_payload_id)
                .where(spec.table.c.raw_payload_id.is_not(None))
                .distinct()
            )
            if isinstance(value, int) and not isinstance(value, bool)
        )
    ordered_raw_ids = sorted(raw_ids)
    if ordered_raw_ids:
        connection_ids = sorted(
            {
                value
                for value in await session.scalars(
                    select(RawPayload.integration_connection_id).where(
                        RawPayload.id.in_(ordered_raw_ids),
                        RawPayload.integration_connection_id.is_not(None),
                    )
                )
                if isinstance(value, uuid.UUID)
            }
        )
        if connection_ids:
            list(
                await session.scalars(
                    select(IntegrationConnection.id)
                    .where(IntegrationConnection.id.in_(connection_ids))
                    .order_by(IntegrationConnection.id)
                    .with_for_update()
                )
            )
        list(
            await session.scalars(
                select(RawPayload.id)
                .where(RawPayload.id.in_(ordered_raw_ids))
                .order_by(RawPayload.id)
                .with_for_update()
            )
        )
    for spec in _TABLES:
        list(
            await session.scalars(
                select(spec.table.c.id)
                .order_by(spec.table.c.id)
                .with_for_update()
            )
        )
    for child in (HevyExercise.__table__, HevySet.__table__):
        list(
            await session.scalars(
                select(child.c.id).order_by(child.c.id).with_for_update()
            )
        )


async def block_provider_raw_ownership_backfill_for_portability_v1_restore(
    session: AsyncSession,
    *,
    snapshot_bounds: dict[str, tuple[int, int]],
) -> None:
    """Record that backup-v1 stripped provider ownership provenance.

    This private portability boundary runs before atomic replacement.  Empty
    incoming tables are proven complete; every non-empty table is terminally
    blocked until a future provenance-bearing restore or reviewed remap exists.
    """

    bounds = _validate_snapshot_bounds(snapshot_bounds)
    reset_at = now_utc().replace(microsecond=0)
    with session.no_autoflush:
        await acquire_identity_governance_lock(session)
        scope = await _load_scope(session, for_update=True)
        dependency = await _load_raw_dependency(session, for_update=True)
        _require_restore_dependency(dependency, scope=scope)
        prior_dependencies = await _load_prior_dependencies(
            session, for_update=True
        )
        _require_prior_dependencies(
            prior_dependencies, scope=scope, restore=True
        )
        checkpoints = await _load_checkpoints(session, for_update=True)
        for spec in _TABLES:
            existing = checkpoints.get(spec.phase_key)
            if existing is not None:
                _validate_checkpoint(existing, spec=spec, scope=scope)
        await _lock_current_provider_graph(session)
        for spec in _TABLES:
            high_watermark, snapshot_rows = bounds[spec.name]
            empty = (high_watermark, snapshot_rows) == (0, 0)
            checkpoint = checkpoints.get(spec.phase_key)
            if checkpoint is None:
                checkpoint = OwnershipBackfillCheckpoint(
                    phase_key=spec.phase_key,
                    subject_id=scope.subject_id,
                    status=("completed" if empty else "restore_blocked"),
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
                    completed_at=reset_at if empty else None,
                )
                session.add(checkpoint)
            checkpoint.subject_id = scope.subject_id
            checkpoint.status = "completed" if empty else "restore_blocked"
            checkpoint.scan_high_watermark_id = high_watermark
            checkpoint.snapshot_rows = snapshot_rows
            checkpoint.last_scanned_id = high_watermark if empty else 0
            checkpoint.scanned_rows = snapshot_rows if empty else 0
            checkpoint.updated_rows = 0
            checkpoint.unchanged_rows = snapshot_rows if empty else 0
            checkpoint.data_checksum_before = _EMPTY_SHA256
            checkpoint.data_checksum_after = _EMPTY_SHA256
            checkpoint.ownership_checksum_after = _EMPTY_SHA256
            checkpoint.started_at = reset_at
            checkpoint.updated_at = reset_at
            checkpoint.completed_at = reset_at if empty else None
        await session.flush()


async def run_provider_raw_ownership_backfill_batch(
    session: AsyncSession,
    *,
    batch_size: int,
) -> ProviderRawOwnershipBackfillBatchResult:
    """Advance the first incomplete fixed provider table by one PK batch."""

    size = _validate_batch_size(batch_size)
    with session.no_autoflush:
        await acquire_identity_governance_lock(session)
        scope = await _load_scope(session, for_update=True)
        dependency = await _load_raw_dependency(session, for_update=True)
        _require_completed_dependency(dependency, scope=scope)
        prior_dependencies = await _load_prior_dependencies(
            session, for_update=True
        )
        _require_prior_dependencies(
            prior_dependencies, scope=scope, restore=False
        )
        checkpoints = await _load_checkpoints(session, for_update=True)
        if any(
            checkpoint.status == "restore_blocked"
            for checkpoint in checkpoints.values()
        ):
            raise ProviderRawOwnershipBackfillStateError(
                "provider/raw ownership is blocked by backup-v1 restore"
            )
        completed_group_on_entry = (
            len(checkpoints) == len(_TABLES)
            and all(
                checkpoint.status == "completed"
                for checkpoint in checkpoints.values()
            )
        )
        summaries = await _summaries(
            session,
            scope=scope,
            checkpoints=checkpoints,
            for_update=False,
            validate_rows=False,
            verify_completed=False,
            relax_completed_intraday=completed_group_on_entry,
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
                for_update=True,
                validate_rows=True,
                verify_completed=True,
                relax_completed_intraday=completed_group_on_entry,
            )
            return _batch_result(
                _result(scope=scope, summaries=checked),
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
        await _reject_future_key_duplicates(session, spec=target.spec)
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
                    target.spec.table.c.id <= checkpoint.scan_high_watermark_id,
                )
                .order_by(target.spec.table.c.id)
                .limit(size)
            )
        )
        await _lock_graph_for_ids(session, spec=target.spec, row_ids=batch_ids)

    before = checkpoint.data_checksum_before
    after = checkpoint.data_checksum_after
    ownership = checkpoint.ownership_checksum_after
    if target.spec.model is GarminIntraday:
        (
            before,
            after,
            ownership,
            updated_rows,
            unchanged_rows,
        ) = await _run_locked_intraday_batch(
            session,
            spec=target.spec,
            row_ids=batch_ids,
            scope=scope,
            before=before,
            after=after,
            ownership=ownership,
        )
    else:
        updated_rows = 0
        unchanged_rows = 0
        for row_id in batch_ids:
            before_row, raw, changed = await _row_plan(
                session,
                spec=target.spec,
                row_id=row_id,
                scope=scope,
                historical=True,
                lock_children=True,
            )
            before = _extend_checksum(
                before, _data_envelope(target.spec, before_row)
            )
            if changed:
                values: dict[str, Any] = {
                    "subject_id": raw.subject_id,
                    "integration_connection_id": raw.integration_connection_id,
                }
                if "updated_at" in target.spec.table.c:
                    values["updated_at"] = before_row._mapping["updated_at"]
                await session.execute(
                    update(target.spec.table)
                    .where(target.spec.table.c.id == row_id)
                    .values(**values)
                )
                updated_rows += 1
            else:
                unchanged_rows += 1
            after_row, _raw, still_changed = await _row_plan(
                session,
                spec=target.spec,
                row_id=row_id,
                scope=scope,
                historical=True,
                lock_children=True,
            )
            if still_changed:
                raise ProviderRawOwnershipBackfillStateError(
                    "provider ownership update did not become strict"
                )
            after = _extend_checksum(
                after, _data_envelope(target.spec, after_row)
            )
            ownership = _extend_checksum(
                ownership, _ownership_envelope(target.spec, after_row)
            )
    if before != after:
        raise ProviderRawOwnershipBackfillStateError(
            "provider data changed while ownership was backfilled"
        )
    scanned = len(batch_ids)
    checkpoint.scanned_rows += scanned
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
        last_scanned=checkpoint.last_scanned_id,
    )
    if remaining == 0:
        await session.flush()
        await _validate_counts(session, spec=target.spec, checkpoint=checkpoint)
        await _verify_final_snapshot(
            session,
            spec=target.spec,
            scope=scope,
            checkpoint=checkpoint,
            for_update=True,
            require_data_checksum=True,
        )
        checkpoint.last_scanned_id = checkpoint.scan_high_watermark_id
        checkpoint.status = "completed"
        checkpoint.completed_at = now_utc()
    await session.flush()
    await session.refresh(checkpoint)

    refreshed = [
        _TableSummary(
            spec=summary.spec,
            checkpoint=checkpoint,
            high_watermark=checkpoint.scan_high_watermark_id,
            snapshot_rows=checkpoint.snapshot_rows,
            remaining_rows=remaining,
            rows_above=summary.rows_above,
        )
        if summary.spec is target.spec
        else summary
        for summary in summaries
    ]
    aggregate = _result(scope=scope, summaries=refreshed)
    if aggregate.completed:
        final = await _summaries(
            session,
            scope=scope,
            checkpoints=checkpoints,
            for_update=True,
            validate_rows=True,
            verify_completed=True,
            require_completed_data_checksum=True,
            relax_completed_intraday=False,
        )
        aggregate = _result(scope=scope, summaries=final)
    return _batch_result(
        aggregate,
        batch_table=target.spec.name,
        scanned=scanned,
        updated=updated_rows,
        unchanged=unchanged_rows,
    )


__all__ = [
    "DEFAULT_PROVIDER_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "MAX_PROVIDER_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES",
    "PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE",
    "PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES",
    "ProviderRawOwnershipBackfillBatchResult",
    "ProviderRawOwnershipBackfillDependencyError",
    "ProviderRawOwnershipBackfillDuplicateError",
    "ProviderRawOwnershipBackfillError",
    "ProviderRawOwnershipBackfillIdentityError",
    "ProviderRawOwnershipBackfillPreflightResult",
    "ProviderRawOwnershipBackfillProvenanceError",
    "ProviderRawOwnershipBackfillStateError",
    "ProviderRawOwnershipBackfillStatus",
    "ProviderRawOwnershipBackfillValidationError",
    "block_provider_raw_ownership_backfill_for_portability_v1_restore",
    "preflight_provider_raw_ownership_backfill",
    "run_provider_raw_ownership_backfill_batch",
]
