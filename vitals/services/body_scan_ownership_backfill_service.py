"""Bounded Stage-3O ownership backfill for legacy body scans.

A historical scan proves a subject, and — when it kept a sheet image — a
legacy-local FileAsset locator, but it never proves who uploaded or authored
that image.  This service creates metadata only; it never reads, moves, deletes,
or hashes file bytes.  The vision-parser raw payload a scan links is validated
read-only rather than rewritten.  Callers own commit or rollback.
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

from sqlalchemy import Table, func, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import attributes
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.models.ai import AIInvocation
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.models.body_scan import BodyScan
from vitals.models.weight import ProgressPhoto
from vitals.services import file_asset_service
from vitals.services.conflict_rule_ownership_backfill_service import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.hevy_child_ownership_backfill_service import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.hrt_child_ownership_backfill_service import (
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.hrt_compound_ownership_backfill_service import (
    HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.normalized_ownership_backfill_service import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.services.provider_raw_ownership_backfill_service import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.genetic_variant_ownership_backfill_service import (
    GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.lab_result_ownership_backfill_service import (
    LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.progress_photo_ownership_backfill_service import (
    PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.raw_ownership_backfill_service import RAW_OWNERSHIP_BACKFILL_PHASE
from vitals.services.shared_report_ownership_backfill_service import (
    SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.weight_log_ownership_backfill_service import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.utils.timeutils import now_utc


BODY_SCAN_OWNERSHIP_BACKFILL_PHASE = "stage3.file_backed.body_scans.v1"
BODY_SCAN_OWNERSHIP_BACKFILL_TABLES = ("body_scans",)
BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES: Mapping[str, str] = (
    MappingProxyType(
        {
            "body_scans": (
                f"{BODY_SCAN_OWNERSHIP_BACKFILL_PHASE}.body_scans"
            )
        }
    )
)
DEFAULT_BODY_SCAN_OWNERSHIP_BACKFILL_BATCH_SIZE = 250
MAX_BODY_SCAN_OWNERSHIP_BACKFILL_BATCH_SIZE = 1000

_TABLE: Table = BodyScan.__table__
_PHASE_KEY = BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["body_scans"]
_PAGE_SIZE = 1000
_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Sheet uploads land under ``static/uploads`` as ``body/<hex>.<ext>``; the
# oldest rows predate that prefix and are relative to the same directory.  Both
# image and PDF sheets are accepted, matching the route's document allowlist.
_SCAN_KEY_RE = re.compile(
    r"^(?:uploads/|body/)?[a-z0-9][a-z0-9._-]*"
    r"\.(?:png|jpg|jpeg|webp|heic|heif|pdf)$"
)
_ROW_FIELDS = (
    "id",
    "subject_id",
    "actor_user_id",
    "file_asset_id",
    "date",
    "domain",
    "source",
    "device",
    "file_key",
    "raw_payload_id",
    "note",
    "created_at",
    "updated_at",
)
_DATA_FIELDS = tuple(
    field
    for field in _ROW_FIELDS
    if field not in {"subject_id", "actor_user_id", "file_asset_id"}
)
_HISTORICAL_CONNECTION_STATUSES = frozenset(
    status.value
    for status in IntegrationConnectionStatus
    if status is not IntegrationConnectionStatus.PENDING
)
_MANUAL_SOURCES = frozenset({Source.MANUAL.value})
_RAW_SOURCES = frozenset({Source.MCP.value, Source.BODY_SCAN.value})
_ALLOWED_SOURCES = _MANUAL_SOURCES | _RAW_SOURCES
_ASSET_FIELDS = (
    "id",
    "subject_id",
    "uploaded_by_user_id",
    "opaque_key",
    "purpose",
    "storage_backend",
    "storage_ref",
    "media_type",
    "byte_size",
    "sha256_hex",
    "status",
    "deleted_at",
    "purged_at",
    "created_at",
    "updated_at",
)
_B_PHASES = tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
_C_PHASES = tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_D_PHASES = tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_E_PHASES = tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_F_PHASES = tuple(HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_G_PHASES = tuple(CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_H_PHASES = tuple(PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_K_PHASES = tuple(SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_L_PHASES = tuple(WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_M_PHASES = tuple(LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_N_PHASES = tuple(GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_RESETTABLE_PHASES = _L_PHASES + _M_PHASES + _N_PHASES
_PRIOR_PHASES = (
    (RAW_OWNERSHIP_BACKFILL_PHASE,)
    + _B_PHASES
    + _C_PHASES
    + _D_PHASES
    + _E_PHASES
    + _F_PHASES
    + _G_PHASES
    + _H_PHASES
    + _K_PHASES
    + _L_PHASES
    + _M_PHASES
    + _N_PHASES
)


class BodyScanOwnershipBackfillStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"
    RESTORE_BLOCKED = "restore_blocked"


class BodyScanOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed Stage-3O errors."""


class BodyScanOwnershipBackfillValidationError(
    BodyScanOwnershipBackfillError, ValueError
):
    """A caller argument or persisted scalar is invalid."""


class BodyScanOwnershipBackfillIdentityError(
    BodyScanOwnershipBackfillError
):
    """The exact-one reviewed owner graph is unavailable."""


class BodyScanOwnershipBackfillDependencyError(
    BodyScanOwnershipBackfillError
):
    """A prerequisite checkpoint is absent, malformed, or nonterminal."""


class BodyScanOwnershipBackfillStateError(BodyScanOwnershipBackfillError):
    """Checkpoint progress, ownership, or a linked graph is inconsistent."""


class BodyScanOwnershipBackfillProvenanceError(
    BodyScanOwnershipBackfillError
):
    """A scan, file, or raw root has unreviewed provenance."""


class BodyScanOwnershipBackfillDuplicateError(
    BodyScanOwnershipBackfillError
):
    """A file key or file root is claimed by more than one fact."""


@dataclass(frozen=True, slots=True)
class BodyScanOwnershipBackfillPreflightResult:
    phase_key: str
    subject_id: uuid.UUID
    status: BodyScanOwnershipBackfillStatus
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
        return self.status is BodyScanOwnershipBackfillStatus.COMPLETED

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
class BodyScanOwnershipBackfillBatchResult(
    BodyScanOwnershipBackfillPreflightResult
):
    batch_table: str
    batch_scanned_rows: int
    batch_updated_rows: int
    batch_unchanged_rows: int

    @property
    def changed(self) -> bool:
        return self.batch_updated_rows > 0

    def to_safe_dict(self) -> dict[str, str | int]:
        result = BodyScanOwnershipBackfillPreflightResult.to_safe_dict(self)
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


def _valid_counter(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _POSTGRES_INTEGER_MAX
    )


def _validate_batch_size(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_BODY_SCAN_OWNERSHIP_BACKFILL_BATCH_SIZE
    ):
        raise BodyScanOwnershipBackfillValidationError(
            "batch_size must be an integer between 1 and 1000"
        )
    return value


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
        OwnershipBackfillCheckpoint.started_at,
        OwnershipBackfillCheckpoint.updated_at,
        OwnershipBackfillCheckpoint.completed_at,
    )


async def _load_checkpoints(
    session: AsyncSession, phases: tuple[str, ...], *, for_update: bool
) -> dict[str, Any]:
    if for_update:
        rows = list(
            await session.scalars(
                select(OwnershipBackfillCheckpoint)
                .where(OwnershipBackfillCheckpoint.phase_key.in_(phases))
                .order_by(OwnershipBackfillCheckpoint.phase_key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        return {row.phase_key: row for row in rows}
    rows = list(
        await session.execute(
            _checkpoint_select()
            .where(OwnershipBackfillCheckpoint.phase_key.in_(phases))
            .order_by(OwnershipBackfillCheckpoint.phase_key)
        )
    )
    return {row.phase_key: _CheckpointProjection(*row) for row in rows}


def _validate_checkpoint(checkpoint: Any, *, phase: str, subject_id: uuid.UUID) -> str:
    error = (
        BodyScanOwnershipBackfillDependencyError
        if phase in _PRIOR_PHASES
        else BodyScanOwnershipBackfillStateError
    )
    if checkpoint.phase_key != phase or checkpoint.subject_id != subject_id:
        raise error("an ownership checkpoint has the wrong phase or subject")
    if checkpoint.status not in {"running", "completed", "restore_blocked"}:
        raise error("an ownership checkpoint has an unknown status")
    counters = (
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
    )
    if not all(_valid_counter(value) for value in counters):
        raise error("an ownership checkpoint has invalid counters")
    if (
        checkpoint.snapshot_rows > checkpoint.scan_high_watermark_id
        or checkpoint.last_scanned_id > checkpoint.scan_high_watermark_id
        or checkpoint.scanned_rows > checkpoint.snapshot_rows
        or checkpoint.scanned_rows
        != checkpoint.updated_rows + checkpoint.unchanged_rows
    ):
        raise error("an ownership checkpoint has inconsistent counters")
    digests = (
        checkpoint.data_checksum_before,
        checkpoint.data_checksum_after,
        checkpoint.ownership_checksum_after,
    )
    if any(not isinstance(value, str) or not _SHA256_RE.fullmatch(value) for value in digests):
        raise error("an ownership checkpoint has an invalid checksum")
    if checkpoint.data_checksum_before != checkpoint.data_checksum_after:
        raise error("an ownership checkpoint has divergent data evidence")
    if (
        checkpoint.started_at is None
        or checkpoint.updated_at is None
        or checkpoint.updated_at < checkpoint.started_at
    ):
        raise error("an ownership checkpoint has invalid timestamps")
    if checkpoint.status == "completed":
        if (
            checkpoint.completed_at is None
            or checkpoint.completed_at < checkpoint.started_at
            or checkpoint.last_scanned_id != checkpoint.scan_high_watermark_id
            or checkpoint.scanned_rows != checkpoint.snapshot_rows
        ):
            raise error("a completed ownership checkpoint is malformed")
    elif checkpoint.completed_at is not None:
        raise error("a non-completed ownership checkpoint has completed_at")
    if checkpoint.status == "running" and (
        checkpoint.scan_high_watermark_id == 0 or checkpoint.snapshot_rows == 0
    ):
        raise error("a running ownership checkpoint must be nonempty")
    if checkpoint.status == "restore_blocked" and (
        checkpoint.scan_high_watermark_id == 0
        or checkpoint.snapshot_rows == 0
        or checkpoint.last_scanned_id != 0
        or checkpoint.scanned_rows != 0
        or checkpoint.updated_rows != 0
        or checkpoint.unchanged_rows != 0
        or any(value != _EMPTY_SHA256 for value in digests)
    ):
        raise error("a restore-blocked ownership checkpoint is malformed")
    return checkpoint.status


async def _load_scope(session: AsyncSession, *, for_update: bool) -> _Scope:
    if for_update:
        await acquire_identity_governance_lock(session)
    query = (
        select(HealthSubject.id, HealthSubject.owner_user_id)
        .order_by(HealthSubject.id)
        .limit(2)
    )
    if for_update:
        query = query.with_for_update()
    rows = list(await session.execute(query))
    if len(rows) != 1:
        raise BodyScanOwnershipBackfillIdentityError(
            "body-scan backfill requires exactly one health subject"
        )
    subject_id, owner_user_id = rows[0]
    owner_query = select(User.status).where(User.id == owner_user_id)
    if for_update:
        owner_query = owner_query.with_for_update()
    if await session.scalar(owner_query) != UserStatus.ACTIVE.value:
        raise BodyScanOwnershipBackfillIdentityError(
            "the sole health subject must have an active owner"
        )
    return _Scope(subject_id, owner_user_id)


def _exact_empty_completed(checkpoint: Any) -> bool:
    return (
        checkpoint.status == "completed"
        and checkpoint.scan_high_watermark_id == 0
        and checkpoint.snapshot_rows == 0
        and checkpoint.last_scanned_id == 0
        and checkpoint.scanned_rows == 0
        and checkpoint.updated_rows == 0
        and checkpoint.unchanged_rows == 0
        and checkpoint.data_checksum_before == _EMPTY_SHA256
        and checkpoint.data_checksum_after == _EMPTY_SHA256
        and checkpoint.ownership_checksum_after == _EMPTY_SHA256
    )


def _require_hevy_pair(checkpoints: Mapping[str, Any]) -> None:
    exercises = checkpoints[
        HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_exercises"]
    ]
    sets = checkpoints[HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_sets"]]
    pair = (exercises.status, sets.status)
    if pair not in {
        ("completed", "completed"),
        ("restore_blocked", "restore_blocked"),
        ("restore_blocked", "completed"),
    }:
        raise BodyScanOwnershipBackfillDependencyError(
            "Stage-3E checkpoint order is inconsistent"
        )
    if pair == ("restore_blocked", "completed") and not _exact_empty_completed(sets):
        raise BodyScanOwnershipBackfillDependencyError(
            "completed Hevy sets after a restore block must be exactly empty"
        )


def _require_hrt_compound_pair(checkpoints: Mapping[str, Any]) -> None:
    compounds = checkpoints[
        HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hrt_compounds"]
    ]
    components = checkpoints[
        HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
            "hrt_compound_components"
        ]
    ]
    pair = (compounds.status, components.status)
    if pair not in {
        ("running", "running"),
        ("running", "completed"),
        ("completed", "running"),
        ("completed", "completed"),
    }:
        raise BodyScanOwnershipBackfillDependencyError(
            "Stage-3F checkpoint order is inconsistent"
        )
    if pair == ("running", "completed") and not _exact_empty_completed(components):
        raise BodyScanOwnershipBackfillDependencyError(
            "completed HRT components before compounds must be exactly empty"
        )
    if (
        compounds.scan_high_watermark_id == 0
        and compounds.snapshot_rows == 0
        and not _exact_empty_completed(components)
    ):
        raise BodyScanOwnershipBackfillDependencyError(
            "an empty HRT compound checkpoint cannot have components"
        )


def _validate_own(checkpoint: Any | None, *, scope: _Scope) -> str | None:
    if checkpoint is None:
        return None
    return _validate_checkpoint(
        checkpoint,
        phase=_PHASE_KEY,
        subject_id=scope.subject_id,
    )


def _require_restore_dependencies(
    checkpoints: Mapping[str, Any],
    *,
    statuses: Mapping[str, str],
    allow_empty_raw_completed: bool,
) -> None:
    raw_status = statuses[RAW_OWNERSHIP_BACKFILL_PHASE]
    if raw_status not in {"completed", "restore_blocked"}:
        raise BodyScanOwnershipBackfillDependencyError(
            "Stage-3A is not restore-terminal"
        )
    if raw_status == "completed":
        if not allow_empty_raw_completed:
            if any(statuses[phase] != "completed" for phase in _PRIOR_PHASES[1:]):
                raise BodyScanOwnershipBackfillDependencyError(
                    "completed Stage-3A requires Stage-3B through Stage-3G completed"
                )
            return
        if not _exact_empty_completed(checkpoints[RAW_OWNERSHIP_BACKFILL_PHASE]):
            raise BodyScanOwnershipBackfillDependencyError(
                "restore mode requires completed Stage-3A to be exactly empty"
            )
    if any(statuses[p] not in {"running", "completed"} for p in _B_PHASES + _C_PHASES):
        raise BodyScanOwnershipBackfillDependencyError(
            "Stage-3B/3C restore state is invalid"
        )
    if any(
        statuses[p] not in {"completed", "restore_blocked"}
        for p in _D_PHASES + _E_PHASES
    ):
        raise BodyScanOwnershipBackfillDependencyError(
            "Stage-3D/3E restore state is invalid"
        )
    if any(
        statuses[p] == "completed" and not _exact_empty_completed(checkpoints[p])
        for p in _D_PHASES + _E_PHASES
    ):
        raise BodyScanOwnershipBackfillDependencyError(
            "completed Stage-3D/3E restore checkpoints must be exactly empty"
        )
    if any(
        statuses[p] not in {"running", "completed"}
        for p in _F_PHASES + _G_PHASES
    ):
        raise BodyScanOwnershipBackfillDependencyError(
            "Stage-3F/3G restore state is invalid"
        )
    if any(
        statuses[p] not in {"completed", "restore_blocked"} for p in _H_PHASES
    ):
        raise BodyScanOwnershipBackfillDependencyError(
            "Stage-3H restore state is invalid"
        )
    if any(
        statuses[p] == "completed" and not _exact_empty_completed(checkpoints[p])
        for p in _H_PHASES
    ):
        raise BodyScanOwnershipBackfillDependencyError(
            "a completed Stage-3H restore checkpoint must be exactly empty"
        )
    # Stage 3K is excluded from backup v1 entirely, so its retained checkpoint is
    # prepared or preserved rather than rebased onto incoming bounds.
    if any(
        statuses[p] not in {"running", "completed"}
        for p in _RESETTABLE_PHASES + _K_PHASES
    ):
        raise BodyScanOwnershipBackfillDependencyError(
            "Stage-3I through Stage-3N restore state is invalid"
        )
    _require_hevy_pair(checkpoints)
    _require_hrt_compound_pair(checkpoints)


def _require_dependencies(
    checkpoints: Mapping[str, Any],
    *,
    scope: _Scope,
    own_status: str | None,
) -> bool:
    if set(checkpoints) != set(_PRIOR_PHASES):
        raise BodyScanOwnershipBackfillDependencyError(
            "Stage-3A through Stage-3N checkpoints are incomplete"
        )
    statuses = {
        phase: _validate_checkpoint(
            checkpoints[phase], phase=phase, subject_id=scope.subject_id
        )
        for phase in _PRIOR_PHASES
    }
    if all(statuses[phase] == "completed" for phase in _PRIOR_PHASES):
        return False
    if own_status not in {"completed", "restore_blocked"}:
        raise BodyScanOwnershipBackfillDependencyError(
            "restore-mode Stage-3O requires its exact portability checkpoint"
        )
    allow_empty_raw = statuses[RAW_OWNERSHIP_BACKFILL_PHASE] == "completed"
    _require_restore_dependencies(
        checkpoints,
        statuses=statuses,
        allow_empty_raw_completed=allow_empty_raw,
    )
    return True


async def body_scan_historical_processed_bound(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> int | None:
    """Return the migrated historical prefix consumed by compatibility reads."""

    if not isinstance(subject_id, uuid.UUID):
        raise BodyScanOwnershipBackfillValidationError(
            "subject_id must be a UUID"
        )
    rows = await _load_checkpoints(session, (_PHASE_KEY,), for_update=False)
    checkpoint = rows.get(_PHASE_KEY)
    if checkpoint is None:
        return None
    status = _validate_checkpoint(
        checkpoint,
        phase=_PHASE_KEY,
        subject_id=subject_id,
    )
    if status == "running":
        return checkpoint.last_scanned_id
    if status == "completed":
        return checkpoint.scan_high_watermark_id
    return None


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BodyScanOwnershipBackfillStateError(
                "a body scan contains a non-finite number"
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
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise BodyScanOwnershipBackfillStateError(
        "a body-scan graph contains an unsupported value"
    )


def _extend(digest: str, values: list[Any]) -> str:
    payload = json.dumps(
        _canonical(values),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(bytes.fromhex(digest) + payload).hexdigest()


def _row_select():
    return select(*(_TABLE.c[field] for field in _ROW_FIELDS))


def _asset_select():
    table = FileAsset.__table__
    return select(*(table.c[field] for field in _ASSET_FIELDS))


def _row_values(row: Any) -> SimpleNamespace:
    mapping = row._mapping if hasattr(row, "_mapping") else row
    return SimpleNamespace(**{field: mapping[field] for field in _ROW_FIELDS})


def _asset_values(row: Any) -> SimpleNamespace:
    mapping = row._mapping if hasattr(row, "_mapping") else row
    return SimpleNamespace(**{field: mapping[field] for field in _ASSET_FIELDS})


def _data_envelope(row: Any) -> list[Any]:
    return ["body_scans", *[getattr(row, field) for field in _DATA_FIELDS]]


def _ownership_envelope(row: Any, asset: Any) -> list[Any]:
    return [
        "body_scans",
        row.id,
        row.subject_id,
        row.actor_user_id,
        row.file_asset_id,
        # A sheet-less scan legitimately has no file root to describe.
        *(
            [getattr(asset, field) for field in _ASSET_FIELDS]
            if asset is not None
            else [None] * len(_ASSET_FIELDS)
        ),
    ]


def _validate_file_key(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise BodyScanOwnershipBackfillProvenanceError(
            "body-scan file_key has an invalid shape"
        )
    if value != value.strip() or _SCAN_KEY_RE.fullmatch(value) is None:
        raise BodyScanOwnershipBackfillProvenanceError(
            "body-scan file_key is not an approved sheet document key"
        )
    if (
        value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or ".." in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(segment in {"", ".", ".."} for segment in value.split("/"))
    ):
        raise BodyScanOwnershipBackfillProvenanceError(
            "body-scan file_key is not a safe POSIX locator"
        )
    return value


async def _key_count(session: AsyncSession, file_key: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(_TABLE)
            .where(_TABLE.c.file_key == file_key)
        )
        or 0
    )


async def _asset_consumer_counts(
    session: AsyncSession,
    asset_id: uuid.UUID,
) -> tuple[int, int, int]:
    scan_count = int(
        await session.scalar(
            select(func.count())
            .select_from(_TABLE)
            .where(_TABLE.c.file_asset_id == asset_id)
        )
        or 0
    )
    raw_count = int(
        await session.scalar(
            select(func.count())
            .select_from(RawPayload)
            .where(RawPayload.file_asset_id == asset_id)
        )
        or 0
    )
    photo_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ProgressPhoto)
            .where(ProgressPhoto.file_asset_id == asset_id)
        )
        or 0
    )
    return scan_count, raw_count, photo_count


async def _assets_for_locator(
    session: AsyncSession,
    *,
    row: Any,
    for_update: bool,
) -> tuple[Any | None, Any | None]:
    predicates = [FileAsset.id == row.file_asset_id]
    if row.file_key is not None:
        predicates.append(FileAsset.storage_ref == row.file_key)
    query = (
        _asset_select()
        .where(or_(*predicates))
        .order_by(FileAsset.storage_backend, FileAsset.storage_ref, FileAsset.id)
    )
    if for_update:
        query = query.with_for_update()
    assets = [_asset_values(raw) for raw in await session.execute(query)]
    linked = next(
        (asset for asset in assets if asset.id == row.file_asset_id),
        None,
    )
    exact = [
        asset
        for asset in assets
        if row.file_key is not None
        and asset.storage_backend == FileStorageBackend.LEGACY_LOCAL.value
        and asset.storage_ref == row.file_key
    ]
    if len(exact) > 1:
        raise BodyScanOwnershipBackfillDuplicateError(
            "a body-scan locator has duplicate file roots"
        )
    return linked, exact[0] if exact else None


def _validate_common_asset(asset: Any, *, row: Any, scope: _Scope) -> None:
    if (
        asset.subject_id != scope.subject_id
        or asset.purpose != FileAssetPurpose.BODY_SCAN_DOCUMENT.value
        or asset.storage_backend != FileStorageBackend.LEGACY_LOCAL.value
        or asset.storage_ref != row.file_key
    ):
        raise BodyScanOwnershipBackfillProvenanceError(
            "body-scan FileAsset has conflicting ownership or provenance"
        )


async def _parser_invocation_counts(
    session: AsyncSession,
    *,
    scope: _Scope,
    raw_payload_id: int,
) -> tuple[int, int, bool]:
    rows = list(
        await session.execute(
            select(AIInvocation.subject_id, AIInvocation.status).where(
                AIInvocation.raw_payload_id == raw_payload_id,
                AIInvocation.purpose == AIInvocationPurpose.BODY_SCAN_PARSE.value,
            )
        )
    )
    total = len(rows)
    succeeded = sum(
        1 for _, status in rows if status == AIInvocationStatus.SUCCEEDED.value
    )
    same_subject = all(subject_id == scope.subject_id for subject_id, _ in rows)
    return total, succeeded, same_subject


async def _validate_raw(
    session: AsyncSession,
    *,
    row: Any,
    scope: _Scope,
    fact_is_unowned: bool,
    for_update: bool,
) -> None:
    """Validate the vision/MCP raw a scan links, without adopting its roots."""

    raw = (
        await session.execute(
            select(
                RawPayload.id,
                RawPayload.subject_id,
                RawPayload.actor_user_id,
                RawPayload.integration_connection_id,
                RawPayload.file_asset_id,
                RawPayload.domain,
                RawPayload.source,
            ).where(RawPayload.id == row.raw_payload_id)
        )
    ).one_or_none()
    if raw is None:
        raise BodyScanOwnershipBackfillStateError(
            "body scan references a missing raw payload"
        )
    expected_source = (
        Source.MCP.value if row.source == Source.MCP.value else Source.BODY_SCAN.value
    )
    if raw.domain != Domain.BODY_COMPOSITION.value or raw.source != expected_source:
        raise BodyScanOwnershipBackfillProvenanceError(
            "body-scan raw payload has an invalid domain or source"
        )
    raw_roots = (
        raw.subject_id,
        raw.actor_user_id,
        raw.integration_connection_id,
        raw.file_asset_id,
    )
    # Backup v1 restores the raw lake before Stage 3A runs again, so a still
    # fully-unowned raw is valid provenance for a still-unowned scan — and only
    # for one.  An adopted scan may never point at unowned raw history.
    raw_is_unowned = raw_roots == (None, None, None, None)
    if raw_is_unowned:
        if not fact_is_unowned:
            raise BodyScanOwnershipBackfillStateError(
                "an owned body scan links unowned raw provenance"
            )
    elif raw.subject_id != scope.subject_id:
        raise BodyScanOwnershipBackfillProvenanceError(
            "body-scan raw payload has foreign provenance"
        )
    elif raw.actor_user_id not in {None, scope.owner_user_id}:
        raise BodyScanOwnershipBackfillProvenanceError(
            "body-scan raw payload actor is outside the reviewed owner boundary"
        )
    if row.source == Source.MCP.value and (
        raw.integration_connection_id is not None or raw.file_asset_id is not None
    ):
        raise BodyScanOwnershipBackfillProvenanceError(
            "structured MCP body-scan lineage must be connection- and file-free"
        )
    total, succeeded, same_subject = await _parser_invocation_counts(
        session, scope=scope, raw_payload_id=raw.id
    )
    if not same_subject:
        raise BodyScanOwnershipBackfillStateError(
            "body-scan raw payload has a foreign parser invocation"
        )
    if row.source == Source.MCP.value or raw_is_unowned:
        if total != 0:
            raise BodyScanOwnershipBackfillProvenanceError(
                "this body-scan raw payload cannot claim a vision parser invocation"
            )
        return
    if raw.integration_connection_id is not None:
        # Reviewed subject-funded parser history: the gateway paid for the parse.
        connection = (
            await session.execute(
                select(
                    IntegrationConnection.subject_id,
                    IntegrationConnection.provider,
                    IntegrationConnection.connection_type,
                    IntegrationConnection.status,
                ).where(
                    IntegrationConnection.id == raw.integration_connection_id
                )
            )
        ).one_or_none()
        if (
            connection is None
            or connection.subject_id != scope.subject_id
            or connection.provider != IntegrationProvider.OPENROUTER.value
            or connection.connection_type
            != IntegrationConnectionType.AI_GATEWAY.value
            or connection.status not in _HISTORICAL_CONNECTION_STATUSES
        ):
            raise BodyScanOwnershipBackfillProvenanceError(
                "body-scan parser AI gateway provenance is invalid"
            )
        if total != 0:
            raise BodyScanOwnershipBackfillProvenanceError(
                "body-scan raw mixes subject and platform parser provenance"
            )
        return
    if raw.file_asset_id is None:
        # Pre-FileAsset history, and the shape a backup-v1 restore leaves once
        # C/F are stripped: valid history that may not forge a parser claim.
        if total != 0:
            raise BodyScanOwnershipBackfillProvenanceError(
                "fileless body-scan raw cannot claim a vision parser invocation"
            )
        return
    if succeeded != 1:
        raise BodyScanOwnershipBackfillProvenanceError(
            "platform body-scan raw lacks one successful parser invocation"
        )
    if raw.file_asset_id != row.file_asset_id:
        raise BodyScanOwnershipBackfillProvenanceError(
            "body-scan raw and fact name different file roots"
        )


async def _validate_row(
    session: AsyncSession,
    *,
    row: Any,
    scope: _Scope,
    allow_unowned: bool,
    allow_migrated: bool,
    for_update_assets: bool,
) -> tuple[str, Any | None]:
    if row.domain != Domain.BODY_COMPOSITION.value or (
        row.source not in _ALLOWED_SOURCES
    ):
        raise BodyScanOwnershipBackfillProvenanceError(
            "body scan has invalid domain or source"
        )
    if row.source in _MANUAL_SOURCES and (
        row.file_key is not None or row.raw_payload_id is not None
    ):
        raise BodyScanOwnershipBackfillProvenanceError(
            "manual body scans cannot claim file or raw provenance"
        )
    if row.source == Source.MCP.value and row.file_key is not None:
        raise BodyScanOwnershipBackfillProvenanceError(
            "structured MCP body scans require null file provenance"
        )
    if row.file_key is not None:
        _validate_file_key(row.file_key)
        if await _key_count(session, row.file_key) != 1:
            raise BodyScanOwnershipBackfillDuplicateError(
                "body-scan file_key is not unique"
            )
    linked, exact = await _assets_for_locator(
        session,
        row=row,
        for_update=for_update_assets,
    )

    roots = (row.subject_id, row.actor_user_id, row.file_asset_id)
    if roots == (None, None, None):
        if not allow_unowned:
            raise BodyScanOwnershipBackfillStateError(
                "an unowned body scan is outside the historical bridge"
            )
        if exact is not None:
            raise BodyScanOwnershipBackfillProvenanceError(
                "historical body scan conflicts with existing FileAsset metadata"
            )
        if row.raw_payload_id is not None:
            await _validate_raw(
                session,
                row=row,
                scope=scope,
                fact_is_unowned=True,
                for_update=for_update_assets,
            )
        return "unowned", None

    if row.subject_id != scope.subject_id:
        raise BodyScanOwnershipBackfillStateError(
            "body scan has partial or foreign ownership roots"
        )
    if row.file_key is None:
        if row.file_asset_id is not None:
            raise BodyScanOwnershipBackfillStateError(
                "a fileless body scan cannot claim a file root"
            )
    else:
        if row.file_asset_id is None:
            raise BodyScanOwnershipBackfillStateError(
                "an owned body scan with a sheet has no file root"
            )
        if linked is None or exact is None or linked.id != exact.id:
            raise BodyScanOwnershipBackfillProvenanceError(
                "body scan has a missing or conflicting FileAsset"
            )
        _validate_common_asset(linked, row=row, scope=scope)
        scan_count, raw_count, photo_count = await _asset_consumer_counts(
            session, linked.id
        )
        if scan_count != 1 or photo_count != 0 or raw_count > 1:
            raise BodyScanOwnershipBackfillDuplicateError(
                "body-scan FileAsset is not exclusive to one scan"
            )

    if row.raw_payload_id is not None:
        await _validate_raw(
            session,
            row=row,
            scope=scope,
            fact_is_unowned=False,
            for_update=for_update_assets,
        )
    elif row.source in _RAW_SOURCES and row.actor_user_id is not None:
        raise BodyScanOwnershipBackfillProvenanceError(
            "a live parsed body scan requires durable raw provenance"
        )

    if row.actor_user_id == scope.owner_user_id:
        if linked is not None and (
            linked.uploaded_by_user_id != scope.owner_user_id
            or linked.status
            not in {
                FileAssetStatus.LEGACY_PLACEHOLDER.value,
                FileAssetStatus.PENDING.value,
            }
            or linked.deleted_at is not None
            or linked.purged_at is not None
        ):
            raise BodyScanOwnershipBackfillProvenanceError(
                "live body-scan FileAsset has conflicting uploader or lifecycle"
            )
        return "live", linked

    if row.actor_user_id is None and allow_migrated:
        if linked is not None and (
            linked.uploaded_by_user_id is not None
            or linked.status != FileAssetStatus.LEGACY_PLACEHOLDER.value
            or linked.deleted_at is not None
            or linked.purged_at is not None
        ):
            raise BodyScanOwnershipBackfillProvenanceError(
                "historical body-scan FileAsset has conflicting provenance"
            )
        return "migrated", linked

    raise BodyScanOwnershipBackfillStateError(
        "body scan actor is outside the reviewed ownership boundary"
    )


def _row_policy(
    row_id: int,
    checkpoint: Any | None,
) -> tuple[bool, bool]:
    if checkpoint is None:
        return True, False
    if checkpoint.status == "running":
        if row_id <= checkpoint.last_scanned_id:
            return False, True
        if row_id <= checkpoint.scan_high_watermark_id:
            return True, False
        return False, False
    if checkpoint.status == "completed":
        return False, row_id <= checkpoint.scan_high_watermark_id
    return False, False


async def _validate_live_asset_bijection(
    session: AsyncSession,
    *,
    scope: _Scope,
) -> None:
    cursor: uuid.UUID | None = None
    while True:
        query = _asset_select().where(
            FileAsset.subject_id == scope.subject_id,
            FileAsset.purpose == FileAssetPurpose.BODY_SCAN_DOCUMENT.value,
        )
        if cursor is not None:
            query = query.where(FileAsset.id > cursor)
        rows = list(
            await session.execute(query.order_by(FileAsset.id).limit(_PAGE_SIZE))
        )
        if not rows:
            break
        for raw in rows:
            asset = _asset_values(raw)
            linked = int(
                await session.scalar(
                    select(func.count())
                    .select_from(_TABLE)
                    .where(_TABLE.c.file_asset_id == asset.id)
                )
                or 0
            )
            if asset.status in {
                FileAssetStatus.DELETED.value,
                FileAssetStatus.PURGED.value,
            }:
                if linked != 0:
                    raise BodyScanOwnershipBackfillStateError(
                        "a retired body-scan FileAsset remains linked"
                    )
            elif (
                asset.storage_backend != FileStorageBackend.LEGACY_LOCAL.value
                or asset.status
                not in {
                    FileAssetStatus.LEGACY_PLACEHOLDER.value,
                    FileAssetStatus.PENDING.value,
                }
                or linked != 1
            ):
                raise BodyScanOwnershipBackfillStateError(
                    "a live body-scan FileAsset is orphaned or unsupported"
                )
            cursor = asset.id


async def _scan_graph(
    session: AsyncSession,
    *,
    scope: _Scope,
    checkpoint: Any | None,
    low: int = 0,
    high: int | None = None,
    for_update: bool,
    digest: bool,
) -> tuple[int, str, str]:
    count = 0
    data = _EMPTY_SHA256
    ownership = _EMPTY_SHA256
    cursor = low
    while True:
        query = _row_select().where(_TABLE.c.id > cursor)
        if high is not None:
            query = query.where(_TABLE.c.id <= high)
        query = query.order_by(_TABLE.c.id).limit(_PAGE_SIZE)
        if for_update:
            query = query.with_for_update()
        rows = list(await session.execute(query))
        if not rows:
            break
        for raw in rows:
            row = _row_values(raw)
            allow_unowned, allow_migrated = _row_policy(row.id, checkpoint)
            kind, asset = await _validate_row(
                session,
                row=row,
                scope=scope,
                allow_unowned=allow_unowned,
                allow_migrated=allow_migrated,
                for_update_assets=for_update,
            )
            if digest:
                data = _extend(data, _data_envelope(row))
                if kind != "unowned":
                    ownership = _extend(ownership, _ownership_envelope(row, asset))
            cursor = row.id
            count += 1
    await _validate_live_asset_bijection(
        session,
        scope=scope,
    )
    return count, data, ownership


async def _bounds(session: AsyncSession) -> tuple[int, int]:
    high = int(await session.scalar(select(func.coalesce(func.max(_TABLE.c.id), 0))) or 0)
    count = int(
        await session.scalar(
            select(func.count()).select_from(_TABLE).where(_TABLE.c.id <= high)
        )
        or 0
    )
    return high, count


async def _remaining(session: AsyncSession, *, high: int, cursor: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(_TABLE)
            .where(_TABLE.c.id > cursor, _TABLE.c.id <= high)
        )
        or 0
    )


async def _after_graph_projection_for_test() -> None:
    """Tests replace this bounded race seam; production is a no-op."""


async def _graph_projection(
    session: AsyncSession,
    row_ids: list[int],
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    if not row_ids:
        return (), ()
    raw_rows = tuple(
        await session.execute(
            _row_select().where(_TABLE.c.id.in_(row_ids)).order_by(_TABLE.c.id)
        )
    )
    rows = tuple(tuple(raw) for raw in raw_rows)
    row_values = [_row_values(raw) for raw in raw_rows]
    refs = {row.file_key for row in row_values}
    asset_ids = {row.file_asset_id for row in row_values if row.file_asset_id is not None}
    raw_assets = tuple(
        await session.execute(
            _asset_select()
            .where(or_(FileAsset.id.in_(asset_ids), FileAsset.storage_ref.in_(refs)))
            .order_by(FileAsset.storage_backend, FileAsset.storage_ref, FileAsset.id)
        )
    )
    assets = tuple(tuple(raw) for raw in raw_assets)
    return rows, assets


async def _lock_projected_graph(
    session: AsyncSession,
    row_ids: list[int],
) -> None:
    if not row_ids:
        return
    rows = [_row_values(raw) for raw in await session.execute(
        _row_select().where(_TABLE.c.id.in_(row_ids)).order_by(_TABLE.c.id)
    )]
    refs = {row.file_key for row in rows}
    asset_ids = {row.file_asset_id for row in rows if row.file_asset_id is not None}
    list(
        await session.execute(
            _asset_select()
            .where(or_(FileAsset.id.in_(asset_ids), FileAsset.storage_ref.in_(refs)))
            .order_by(FileAsset.storage_backend, FileAsset.storage_ref, FileAsset.id)
            .with_for_update()
        )
    )
    list(
        await session.execute(
            _row_select()
            .where(_TABLE.c.id.in_(row_ids))
            .order_by(_TABLE.c.id)
            .with_for_update()
        )
    )


async def _load_row(session: AsyncSession, row_id: int) -> Any | None:
    raw = (
        await session.execute(_row_select().where(_TABLE.c.id == row_id))
    ).one_or_none()
    return _row_values(raw) if raw is not None else None


def _asset_from_orm(asset: FileAsset) -> SimpleNamespace:
    return SimpleNamespace(**{field: getattr(asset, field) for field in _ASSET_FIELDS})


async def _validate_restore_blocked_rows(
    session: AsyncSession,
    *,
    scope: _Scope,
    checkpoint: Any,
) -> None:
    count = 0
    high = 0
    duplicate_key = await session.scalar(
        select(_TABLE.c.file_key)
        .where(_TABLE.c.file_key.is_not(None))
        .group_by(_TABLE.c.file_key)
        .having(func.count() > 1)
        .limit(1)
    )
    if duplicate_key is not None:
        raise BodyScanOwnershipBackfillDuplicateError(
            "restored body scans contain duplicate file keys"
        )
    cursor = 0
    while True:
        rows = list(
            await session.execute(
                _row_select()
                .where(_TABLE.c.id > cursor)
                .order_by(_TABLE.c.id)
                .limit(_PAGE_SIZE)
            )
        )
        if not rows:
            break
        for raw in rows:
            row = _row_values(raw)
            if (
                row.subject_id != scope.subject_id
                or row.actor_user_id is not None
                or row.file_asset_id is not None
                or row.domain != Domain.BODY_COMPOSITION.value
                or row.source not in _ALLOWED_SOURCES
            ):
                raise BodyScanOwnershipBackfillStateError(
                    "restored body scan has an invalid v1 ownership shape"
                )
            if row.file_key is None:
                count += 1
                high = row.id
                cursor = row.id
                continue
            _validate_file_key(row.file_key)
            matches = [
                _asset_values(asset)
                for asset in await session.execute(
                    _asset_select().where(FileAsset.storage_ref == row.file_key)
                )
            ]
            for asset in matches:
                if (
                    asset.subject_id != scope.subject_id
                    or asset.purpose != FileAssetPurpose.BODY_SCAN_DOCUMENT.value
                    or asset.storage_backend
                    != FileStorageBackend.LEGACY_LOCAL.value
                    or asset.status != FileAssetStatus.DELETED.value
                    or asset.deleted_at is None
                    or asset.purged_at is not None
                    or asset.uploaded_by_user_id
                    not in {None, scope.owner_user_id}
                    or await _asset_consumer_counts(session, asset.id) != (0, 0, 0)
                ):
                    raise BodyScanOwnershipBackfillStateError(
                        "restored body scan conflicts with outgoing retired metadata"
                    )
            count += 1
            high = row.id
            cursor = row.id
    if count != checkpoint.snapshot_rows:
        raise BodyScanOwnershipBackfillStateError(
            "restore-blocked body-scan cardinality differs from the backup"
        )
    if high != checkpoint.scan_high_watermark_id:
        raise BodyScanOwnershipBackfillStateError(
            "restore-blocked body-scan high watermark differs from the backup"
        )


async def _status_result(
    session: AsyncSession,
    *,
    scope: _Scope,
    checkpoint: Any | None,
    validate: bool,
    for_update: bool,
) -> BodyScanOwnershipBackfillPreflightResult:
    if checkpoint is None:
        high, snapshot = await _bounds(session)
        status = BodyScanOwnershipBackfillStatus.NOT_STARTED
        scanned = updated = unchanged = 0
        remaining = snapshot
        rows_above = 0
        before = after = ownership = _EMPTY_SHA256
        completed_tables = 0
    else:
        high = checkpoint.scan_high_watermark_id
        snapshot = checkpoint.snapshot_rows
        status = BodyScanOwnershipBackfillStatus(checkpoint.status)
        scanned = checkpoint.scanned_rows
        updated = checkpoint.updated_rows
        unchanged = checkpoint.unchanged_rows
        remaining = await _remaining(
            session,
            high=high,
            cursor=checkpoint.last_scanned_id,
        )
        rows_above = int(
            await session.scalar(
                select(func.count()).select_from(_TABLE).where(_TABLE.c.id > high)
            )
            or 0
        )
        before = checkpoint.data_checksum_before
        after = checkpoint.data_checksum_after
        ownership = checkpoint.ownership_checksum_after
        completed_tables = int(status is BodyScanOwnershipBackfillStatus.COMPLETED)
    if validate:
        if status is BodyScanOwnershipBackfillStatus.RESTORE_BLOCKED:
            assert checkpoint is not None
            await _validate_restore_blocked_rows(
                session,
                scope=scope,
                checkpoint=checkpoint,
            )
        else:
            count, _data, _ownership = await _scan_graph(
                session,
                scope=scope,
                checkpoint=checkpoint,
                for_update=for_update,
                digest=False,
            )
            if checkpoint is not None and checkpoint.status == "running":
                frozen_count = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(_TABLE)
                        .where(_TABLE.c.id <= high)
                    )
                    or 0
                )
                if frozen_count != snapshot:
                    raise BodyScanOwnershipBackfillStateError(
                        "the running body-scan snapshot cardinality changed"
                    )
            if checkpoint is None and count != snapshot:
                raise BodyScanOwnershipBackfillStateError(
                    "body-scan preflight cardinality changed"
                )
    return BodyScanOwnershipBackfillPreflightResult(
        phase_key=BODY_SCAN_OWNERSHIP_BACKFILL_PHASE,
        subject_id=scope.subject_id,
        status=status,
        tables_total=1,
        completed_tables=completed_tables,
        snapshot_rows=snapshot,
        scanned_rows=scanned,
        updated_rows=updated,
        unchanged_rows=unchanged,
        remaining_rows=remaining,
        rows_above_high_watermark=rows_above,
        data_checksum_before=before,
        data_checksum_after=after,
        ownership_checksum_after=ownership,
    )


def _batch_result(
    result: BodyScanOwnershipBackfillPreflightResult,
    *,
    scanned: int,
    updated: int,
    unchanged: int,
) -> BodyScanOwnershipBackfillBatchResult:
    return BodyScanOwnershipBackfillBatchResult(
        **{
            field: getattr(result, field)
            for field in BodyScanOwnershipBackfillPreflightResult.__dataclass_fields__
        },
        batch_table="body_scans",
        batch_scanned_rows=scanned,
        batch_updated_rows=updated,
        batch_unchanged_rows=unchanged,
    )


async def preflight_body_scan_ownership_backfill(
    session: AsyncSession,
) -> BodyScanOwnershipBackfillPreflightResult:
    """Validate the fixed Stage-3O graph without mutation."""

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=False)
        own = await _load_checkpoints(session, (_PHASE_KEY,), for_update=False)
        checkpoint = own.get(_PHASE_KEY)
        own_status = _validate_own(checkpoint, scope=scope)
        dependencies = await _load_checkpoints(
            session, _PRIOR_PHASES, for_update=False
        )
        _require_dependencies(
            dependencies,
            scope=scope,
            own_status=own_status,
        )
        return await _status_result(
            session,
            scope=scope,
            checkpoint=checkpoint,
            validate=True,
            for_update=False,
        )


def _validate_restore_bounds(snapshot_bounds: Any) -> tuple[int, int]:
    if not isinstance(snapshot_bounds, Mapping) or set(snapshot_bounds) != {
        "body_scans"
    }:
        raise BodyScanOwnershipBackfillValidationError(
            "snapshot_bounds must contain the exact body-scan table catalog"
        )
    pair = snapshot_bounds["body_scans"]
    if not isinstance(pair, tuple) or len(pair) != 2:
        raise BodyScanOwnershipBackfillValidationError(
            "the body-scan snapshot bound must be an exact pair"
        )
    high, count = pair
    if (
        not _valid_counter(high)
        or not _valid_counter(count)
        or count > high
        or (high == 0) != (count == 0)
    ):
        raise BodyScanOwnershipBackfillValidationError(
            "the body-scan snapshot bound is an invalid ID/count pair"
        )
    return high, count


def _relevant_asset_predicate():
    linked = select(_TABLE.c.id).where(_TABLE.c.file_asset_id == FileAsset.id).exists()
    exact_ref = select(_TABLE.c.id).where(_TABLE.c.file_key == FileAsset.storage_ref).exists()
    return or_(
        linked,
        exact_ref,
        (
            (FileAsset.purpose == FileAssetPurpose.BODY_SCAN_DOCUMENT.value)
            & (FileAsset.storage_backend == FileStorageBackend.LEGACY_LOCAL.value)
        ),
    )


def _after_asset_key(*, backend: str, storage_ref: str, asset_id: uuid.UUID):
    return or_(
        FileAsset.storage_backend > backend,
        (
            (FileAsset.storage_backend == backend)
            & (FileAsset.storage_ref > storage_ref)
        ),
        (
            (FileAsset.storage_backend == backend)
            & (FileAsset.storage_ref == storage_ref)
            & (FileAsset.id > asset_id)
        ),
    )


async def _scan_projection_digest(session: AsyncSession) -> tuple[int, str]:
    count = 0
    digest = _EMPTY_SHA256
    cursor = 0
    while True:
        rows = list(
            await session.execute(
                _row_select()
                .where(_TABLE.c.id > cursor)
                .order_by(_TABLE.c.id)
                .limit(_PAGE_SIZE)
            )
        )
        if not rows:
            break
        for raw in rows:
            row = _row_values(raw)
            digest = _extend(digest, [getattr(row, field) for field in _ROW_FIELDS])
            count += 1
            cursor = row.id
    return count, digest


async def _asset_projection_digest(session: AsyncSession) -> tuple[int, str]:
    count = 0
    digest = _EMPTY_SHA256
    cursor: tuple[str, str, uuid.UUID] | None = None
    while True:
        query = _asset_select().where(_relevant_asset_predicate())
        if cursor is not None:
            query = query.where(
                _after_asset_key(
                    backend=cursor[0],
                    storage_ref=cursor[1],
                    asset_id=cursor[2],
                )
            )
        rows = list(
            await session.execute(
                query.order_by(
                    FileAsset.storage_backend,
                    FileAsset.storage_ref,
                    FileAsset.id,
                ).limit(_PAGE_SIZE)
            )
        )
        if not rows:
            break
        for raw in rows:
            asset = _asset_values(raw)
            digest = _extend(digest, [getattr(asset, field) for field in _ASSET_FIELDS])
            count += 1
            cursor = (asset.storage_backend, asset.storage_ref, asset.id)
    return count, digest


async def _lock_current_graph_bounded(session: AsyncSession) -> None:
    photo_projection = await _scan_projection_digest(session)
    asset_projection = await _asset_projection_digest(session)
    asset_cursor: tuple[str, str, uuid.UUID] | None = None
    while True:
        query = _asset_select().where(_relevant_asset_predicate())
        if asset_cursor is not None:
            query = query.where(
                _after_asset_key(
                    backend=asset_cursor[0],
                    storage_ref=asset_cursor[1],
                    asset_id=asset_cursor[2],
                )
            )
        rows = list(
            await session.execute(
                query.order_by(
                    FileAsset.storage_backend,
                    FileAsset.storage_ref,
                    FileAsset.id,
                )
                .limit(_PAGE_SIZE)
                .with_for_update()
            )
        )
        if not rows:
            break
        last_asset = _asset_values(rows[-1])
        asset_cursor = (
            last_asset.storage_backend,
            last_asset.storage_ref,
            last_asset.id,
        )
    cursor = 0
    while True:
        rows = list(
            await session.execute(
                _row_select()
                .where(_TABLE.c.id > cursor)
                .order_by(_TABLE.c.id)
                .limit(_PAGE_SIZE)
                .with_for_update()
            )
        )
        if not rows:
            break
        cursor = _row_values(rows[-1]).id
    if (
        await _scan_projection_digest(session) != photo_projection
        or await _asset_projection_digest(session) != asset_projection
    ):
        raise BodyScanOwnershipBackfillStateError(
            "the body-scan graph changed while portability locks were acquired"
        )


async def _retire_linked_assets_bounded(
    session: AsyncSession,
    *,
    scope: _Scope,
) -> None:
    cursor = 0
    while True:
        rows = list(
            await session.execute(
                _row_select()
                .where(_TABLE.c.id > cursor)
                .order_by(_TABLE.c.id)
                .limit(_PAGE_SIZE)
            )
        )
        if not rows:
            break
        for raw in rows:
            row = _row_values(raw)
            if row.file_asset_id is not None:
                await file_asset_service.mark_legacy_local_deleted(
                    session,
                    file_asset_id=row.file_asset_id,
                    subject_id=scope.subject_id,
                    purged=False,
                )
            cursor = row.id


async def block_body_scan_ownership_backfill_for_portability_v1_restore(
    session: AsyncSession,
    *,
    snapshot_bounds: Mapping[str, tuple[int, int]],
) -> None:
    """Retire outgoing photo metadata and record backup-v1 provenance loss."""

    high, count = _validate_restore_bounds(snapshot_bounds)
    reset_at = now_utc().replace(microsecond=0)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependencies = await _load_checkpoints(
            session, _PRIOR_PHASES, for_update=True
        )
        if set(dependencies) != set(_PRIOR_PHASES):
            raise BodyScanOwnershipBackfillDependencyError(
                "Stage-3A through Stage-3N checkpoints are incomplete"
            )
        statuses = {
            phase: _validate_checkpoint(
                dependencies[phase], phase=phase, subject_id=scope.subject_id
            )
            for phase in _PRIOR_PHASES
        }
        _require_restore_dependencies(
            dependencies,
            statuses=statuses,
            allow_empty_raw_completed=(
                statuses[RAW_OWNERSHIP_BACKFILL_PHASE] == "completed"
            ),
        )
        own = await _load_checkpoints(session, (_PHASE_KEY,), for_update=True)
        checkpoint = own.get(_PHASE_KEY)
        own_status = _validate_own(checkpoint, scope=scope)
        await _lock_current_graph_bounded(session)
        if own_status == "restore_blocked":
            assert checkpoint is not None
            await _validate_restore_blocked_rows(
                session,
                scope=scope,
                checkpoint=checkpoint,
            )
        else:
            await _scan_graph(
                session,
                scope=scope,
                checkpoint=checkpoint,
                for_update=True,
                digest=False,
            )
            await _retire_linked_assets_bounded(session, scope=scope)
        empty = (high, count) == (0, 0)
        if checkpoint is None:
            checkpoint = OwnershipBackfillCheckpoint(
                phase_key=_PHASE_KEY,
                subject_id=scope.subject_id,
            )
            session.add(checkpoint)
        checkpoint.status = "completed" if empty else "restore_blocked"
        checkpoint.scan_high_watermark_id = high
        checkpoint.snapshot_rows = count
        checkpoint.last_scanned_id = 0
        checkpoint.scanned_rows = 0
        checkpoint.updated_rows = 0
        checkpoint.unchanged_rows = 0
        checkpoint.data_checksum_before = _EMPTY_SHA256
        checkpoint.data_checksum_after = _EMPTY_SHA256
        checkpoint.ownership_checksum_after = _EMPTY_SHA256
        checkpoint.started_at = reset_at
        checkpoint.updated_at = reset_at
        checkpoint.completed_at = reset_at if empty else None
        await session.flush()


async def _create_checkpoint(
    session: AsyncSession,
    *,
    scope: _Scope,
    high: int,
    count: int,
) -> OwnershipBackfillCheckpoint:
    empty = (high, count) == (0, 0)
    checkpoint = OwnershipBackfillCheckpoint(
        phase_key=_PHASE_KEY,
        subject_id=scope.subject_id,
        status="completed" if empty else "running",
        scan_high_watermark_id=high,
        snapshot_rows=count,
        last_scanned_id=0,
        scanned_rows=0,
        updated_rows=0,
        unchanged_rows=0,
        data_checksum_before=_EMPTY_SHA256,
        data_checksum_after=_EMPTY_SHA256,
        ownership_checksum_after=_EMPTY_SHA256,
        started_at=func.now(),
        updated_at=func.now(),
        completed_at=func.now() if empty else None,
    )
    session.add(checkpoint)
    await session.flush()
    return checkpoint


def _set_cached_scan_roots(
    session: AsyncSession,
    *,
    row_id: int,
    subject_id: uuid.UUID,
    file_asset_id: uuid.UUID,
) -> None:
    cached = session.identity_map.get((BodyScan, (row_id,), None))
    if cached is not None:
        attributes.set_committed_value(cached, "subject_id", subject_id)
        attributes.set_committed_value(cached, "file_asset_id", file_asset_id)


async def _create_fresh_placeholder(
    session: AsyncSession,
    *,
    scope: _Scope,
    file_key: str,
) -> FileAsset:
    """Create, but never reconcile or reuse, the reviewed historical file root."""

    asset = FileAsset(
        subject_id=scope.subject_id,
        uploaded_by_user_id=None,
        opaque_key=uuid.uuid4(),
        purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT.value,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        storage_ref=file_key,
        media_type=None,
        byte_size=None,
        sha256_hex=None,
        status=FileAssetStatus.LEGACY_PLACEHOLDER.value,
    )
    if session.get_bind().dialect.name != "postgresql":
        session.add(asset)
        await session.flush()
        return asset
    try:
        async with session.begin_nested():
            session.add(asset)
            await session.flush()
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate not in {"23505", "40P01"}:
            raise
        raise BodyScanOwnershipBackfillDuplicateError(
            "a body-scan FileAsset appeared during placeholder creation"
        ) from exc
    return asset


async def run_body_scan_ownership_backfill_batch(
    session: AsyncSession,
    *,
    batch_size: int,
) -> BodyScanOwnershipBackfillBatchResult:
    """Advance the fixed body-scan table by at most one PK batch."""

    size = _validate_batch_size(batch_size)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependencies = await _load_checkpoints(
            session, _PRIOR_PHASES, for_update=True
        )
        own = await _load_checkpoints(session, (_PHASE_KEY,), for_update=True)
        checkpoint = own.get(_PHASE_KEY)
        own_status = _validate_own(checkpoint, scope=scope)
        _require_dependencies(
            dependencies,
            scope=scope,
            own_status=own_status,
        )
        if own_status == "restore_blocked":
            raise BodyScanOwnershipBackfillStateError(
                "body-scan ownership is blocked pending portability restore"
            )
        if own_status == "completed":
            result = await _status_result(
                session,
                scope=scope,
                checkpoint=checkpoint,
                validate=True,
                for_update=True,
            )
            return _batch_result(result, scanned=0, updated=0, unchanged=0)
        await _scan_graph(
            session,
            scope=scope,
            checkpoint=checkpoint,
            for_update=False,
            digest=False,
        )
        if checkpoint is None:
            high, count = await _bounds(session)
            checkpoint = await _create_checkpoint(
                session,
                scope=scope,
                high=high,
                count=count,
            )
            if checkpoint.status == "completed":
                result = await _status_result(
                    session,
                    scope=scope,
                    checkpoint=checkpoint,
                    validate=True,
                    for_update=True,
                )
                return _batch_result(result, scanned=0, updated=0, unchanged=0)

        # Rows inserted after the frozen snapshot are strict live writes.
        await _scan_graph(
            session,
            scope=scope,
            checkpoint=checkpoint,
            low=checkpoint.scan_high_watermark_id,
            high=None,
            for_update=True,
            digest=False,
        )
        ids = list(
            await session.scalars(
                select(_TABLE.c.id)
                .where(
                    _TABLE.c.id > checkpoint.last_scanned_id,
                    _TABLE.c.id <= checkpoint.scan_high_watermark_id,
                )
                .order_by(_TABLE.c.id)
                .limit(size)
            )
        )
        projected = await _graph_projection(session, ids)
        await _after_graph_projection_for_test()
        await _lock_projected_graph(session, ids)
        if await _graph_projection(session, ids) != projected:
            raise BodyScanOwnershipBackfillStateError(
                "the projected body-scan graph changed before it was locked"
            )

    before = checkpoint.data_checksum_before
    after = checkpoint.data_checksum_after
    ownership = checkpoint.ownership_checksum_after
    updated_count = 0
    unchanged_count = 0
    for row_id in ids:
        row = await _load_row(session, row_id)
        if row is None:
            raise BodyScanOwnershipBackfillStateError(
                "a projected body scan disappeared"
            )
        kind, asset = await _validate_row(
            session,
            row=row,
            scope=scope,
            allow_unowned=True,
            allow_migrated=False,
            for_update_assets=True,
        )
        before = _extend(before, _data_envelope(row))
        if kind == "unowned":
            # Only a scan that kept its sheet gets a metadata-only file root; a
            # manual or structured scan has no image to register.
            created = (
                await _create_fresh_placeholder(
                    session,
                    scope=scope,
                    file_key=row.file_key,
                )
                if row.file_key is not None
                else None
            )
            result = await session.execute(
                update(_TABLE)
                .where(
                    _TABLE.c.id == row_id,
                    _TABLE.c.subject_id.is_(None),
                    _TABLE.c.actor_user_id.is_(None),
                    _TABLE.c.file_asset_id.is_(None),
                )
                .values(
                    subject_id=scope.subject_id,
                    file_asset_id=None if created is None else created.id,
                    updated_at=row.updated_at,
                )
            )
            if result.rowcount != 1:
                raise BodyScanOwnershipBackfillStateError(
                    "a body-scan ownership root changed during adoption"
                )
            _set_cached_scan_roots(
                session,
                row_id=row_id,
                subject_id=scope.subject_id,
                file_asset_id=None if created is None else created.id,
            )
            updated_count += 1
        elif kind == "live":
            unchanged_count += 1
        else:
            raise BodyScanOwnershipBackfillStateError(
                "an already migrated scan appeared beyond the processed prefix"
            )
        current = await _load_row(session, row_id)
        if current is None:
            raise BodyScanOwnershipBackfillStateError(
                "a body scan disappeared during adoption"
            )
        current_kind, current_asset = await _validate_row(
            session,
            row=current,
            scope=scope,
            allow_unowned=False,
            allow_migrated=True,
            for_update_assets=True,
        )
        if current_kind not in {"migrated", "live"} or (
            (current_asset is None) != (current.file_key is None)
        ):
            raise BodyScanOwnershipBackfillStateError(
                "a body scan remained outside the reviewed ownership graph"
            )
        after = _extend(after, _data_envelope(current))
        ownership = _extend(
            ownership,
            _ownership_envelope(current, current_asset),
        )
    if before != after:
        raise BodyScanOwnershipBackfillStateError(
            "body-scan data changed while ownership was backfilled"
        )
    checkpoint.scanned_rows += len(ids)
    checkpoint.updated_rows += updated_count
    checkpoint.unchanged_rows += unchanged_count
    checkpoint.data_checksum_before = before
    checkpoint.data_checksum_after = after
    checkpoint.ownership_checksum_after = ownership
    if ids:
        checkpoint.last_scanned_id = ids[-1]
    remaining = await _remaining(
        session,
        high=checkpoint.scan_high_watermark_id,
        cursor=checkpoint.last_scanned_id,
    )
    if remaining == 0:
        await session.flush()
        current_count, current_data, current_ownership = await _scan_graph(
            session,
            scope=scope,
            checkpoint=checkpoint,
            low=0,
            high=checkpoint.scan_high_watermark_id,
            for_update=True,
            digest=True,
        )
        if (
            current_count != checkpoint.snapshot_rows
            or current_data != checkpoint.data_checksum_before
            or current_data != checkpoint.data_checksum_after
            or current_ownership != checkpoint.ownership_checksum_after
        ):
            raise BodyScanOwnershipBackfillStateError(
                "the body-scan snapshot changed during finalization"
            )
        checkpoint.last_scanned_id = checkpoint.scan_high_watermark_id
        checkpoint.status = "completed"
        checkpoint.completed_at = now_utc()
    await session.flush()
    own = await _load_checkpoints(session, (_PHASE_KEY,), for_update=True)
    checkpoint = own[_PHASE_KEY]
    _validate_own(checkpoint, scope=scope)
    result = await _status_result(
        session,
        scope=scope,
        checkpoint=checkpoint,
        validate=checkpoint.status == "completed",
        for_update=checkpoint.status == "completed",
    )
    return _batch_result(
        result,
        scanned=len(ids),
        updated=updated_count,
        unchanged=unchanged_count,
    )


__all__ = [
    "DEFAULT_BODY_SCAN_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "MAX_BODY_SCAN_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES",
    "BODY_SCAN_OWNERSHIP_BACKFILL_PHASE",
    "BODY_SCAN_OWNERSHIP_BACKFILL_TABLES",
    "BodyScanOwnershipBackfillBatchResult",
    "BodyScanOwnershipBackfillDependencyError",
    "BodyScanOwnershipBackfillDuplicateError",
    "BodyScanOwnershipBackfillError",
    "BodyScanOwnershipBackfillIdentityError",
    "BodyScanOwnershipBackfillPreflightResult",
    "BodyScanOwnershipBackfillProvenanceError",
    "BodyScanOwnershipBackfillStateError",
    "BodyScanOwnershipBackfillStatus",
    "BodyScanOwnershipBackfillValidationError",
    "block_body_scan_ownership_backfill_for_portability_v1_restore",
    "preflight_body_scan_ownership_backfill",
    "body_scan_historical_processed_bound",
    "run_body_scan_ownership_backfill_batch",
]
