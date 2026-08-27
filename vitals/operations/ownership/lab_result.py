"""Bounded Stage-3M ownership backfill for optional-channel lab results.

Historical rows prove only the sole reviewed subject.  Actor and provider
provenance remain exactly as persisted; this service never infers either root,
nor does it copy a connection down from the raw payload a fact already links.
Callers own commit or rollback.
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
from enum import StrEnum
from types import MappingProxyType, SimpleNamespace
from typing import Any

from sqlalchemy import Table, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    LabFlag,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.ai import AIInvocation
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.models.labs import LabResult
from vitals.operations.ownership.conflict_rule import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.hevy_child import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.hrt_child import (
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.hrt_compound import (
    HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.operations.ownership.normalized import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.progress_photo import (
    PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.provider_raw import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.raw import RAW_OWNERSHIP_BACKFILL_PHASE
from vitals.operations.ownership.shared_report import (
    SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.weight_log import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.utils.timeutils import now_utc


LAB_RESULT_OWNERSHIP_BACKFILL_PHASE = "stage3.raw_linked_facts.lab_results.v1"
LAB_RESULT_OWNERSHIP_BACKFILL_TABLES = ("lab_results",)
LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES: Mapping[str, str] = (
    MappingProxyType(
        {
            "lab_results": (
                f"{LAB_RESULT_OWNERSHIP_BACKFILL_PHASE}.lab_results"
            )
        }
    )
)
DEFAULT_LAB_RESULT_OWNERSHIP_BACKFILL_BATCH_SIZE = 250
MAX_LAB_RESULT_OWNERSHIP_BACKFILL_BATCH_SIZE = 1000

_TABLE: Table = LabResult.__table__
_PHASE_KEY = LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["lab_results"]
_PAGE_SIZE = 1000
_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Manual and MCP results are the owner speaking through two surfaces; parsed
# results are derived from an uploaded document.  Any other source is unreviewed.
_MANUAL_SOURCES = {
    Source.MANUAL.value,
    Source.MCP.value,
}
_ALLOWED_SOURCES = _MANUAL_SOURCES | {Source.LAB_PARSER.value}
_ALLOWED_FLAGS = {flag.value for flag in LabFlag}
_MAX_MARKER_LENGTH = 128
_HISTORICAL_CONNECTION_STATUSES = {
    IntegrationConnectionStatus.LEGACY.value,
    IntegrationConnectionStatus.ACTIVE.value,
    IntegrationConnectionStatus.DISABLED.value,
    IntegrationConnectionStatus.RETIRED.value,
}
_ROW_FIELDS = (
    "id",
    "subject_id",
    "actor_user_id",
    "date",
    "domain",
    "source",
    "marker",
    "value",
    "unit",
    "ref_low",
    "ref_high",
    "flag",
    "lab_name",
    "note",
    "raw_payload_id",
    "created_at",
    "updated_at",
)
_DATA_FIELDS = tuple(
    field for field in _ROW_FIELDS if field not in {"subject_id", "actor_user_id"}
)
_CONNECTION_FIELDS = (
    "id",
    "subject_id",
    "provider",
    "connection_type",
    "status",
)
_RAW_FIELDS = (
    "id",
    "subject_id",
    "actor_user_id",
    "integration_connection_id",
    "file_asset_id",
    "domain",
    "source",
    "external_id",
    "processed_at",
)
_FILE_FIELDS = (
    "id",
    "subject_id",
    "uploaded_by_user_id",
    "purpose",
    "storage_ref",
    "status",
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
)


class LabResultOwnershipBackfillStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"


class LabResultOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed Stage-3M errors."""


class LabResultOwnershipBackfillValidationError(
    LabResultOwnershipBackfillError, ValueError
):
    """A caller argument or persisted scalar is invalid."""


class LabResultOwnershipBackfillIdentityError(LabResultOwnershipBackfillError):
    """The exact-one reviewed owner graph is unavailable."""


class LabResultOwnershipBackfillDependencyError(
    LabResultOwnershipBackfillError
):
    """A prerequisite checkpoint is absent, malformed, or in the wrong mode."""


class LabResultOwnershipBackfillStateError(LabResultOwnershipBackfillError):
    """Checkpoint progress or an ownership root is inconsistent."""


class LabResultOwnershipBackfillProvenanceError(
    LabResultOwnershipBackfillError
):
    """A lab result row has unsupported persisted provenance."""


@dataclass(frozen=True, slots=True)
class LabResultOwnershipBackfillPreflightResult:
    phase_key: str
    subject_id: uuid.UUID
    status: LabResultOwnershipBackfillStatus
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
        return self.status is LabResultOwnershipBackfillStatus.COMPLETED

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
class LabResultOwnershipBackfillBatchResult(
    LabResultOwnershipBackfillPreflightResult
):
    batch_table: str
    batch_scanned_rows: int
    batch_updated_rows: int
    batch_unchanged_rows: int

    @property
    def changed(self) -> bool:
        return self.batch_updated_rows > 0

    def to_safe_dict(self) -> dict[str, str | int]:
        result = LabResultOwnershipBackfillPreflightResult.to_safe_dict(self)
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
        or not 1 <= value <= MAX_LAB_RESULT_OWNERSHIP_BACKFILL_BATCH_SIZE
    ):
        raise LabResultOwnershipBackfillValidationError(
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
        LabResultOwnershipBackfillDependencyError
        if phase in _PRIOR_PHASES
        else LabResultOwnershipBackfillStateError
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
        raise LabResultOwnershipBackfillIdentityError(
            "lab result backfill requires exactly one health subject"
        )
    subject_id, owner_user_id = rows[0]
    owner_query = select(User.status).where(User.id == owner_user_id)
    if for_update:
        owner_query = owner_query.with_for_update()
    if await session.scalar(owner_query) != UserStatus.ACTIVE.value:
        raise LabResultOwnershipBackfillIdentityError(
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


def _exact_nonempty_running(checkpoint: Any) -> bool:
    return (
        checkpoint.status == "running"
        and checkpoint.scan_high_watermark_id > 0
        and checkpoint.snapshot_rows > 0
        and checkpoint.last_scanned_id == 0
        and checkpoint.scanned_rows == 0
        and checkpoint.updated_rows == 0
        and checkpoint.unchanged_rows == 0
        and checkpoint.data_checksum_before == _EMPTY_SHA256
        and checkpoint.data_checksum_after == _EMPTY_SHA256
        and checkpoint.ownership_checksum_after == _EMPTY_SHA256
    )


def _require_restore_dependencies(checkpoints: Mapping[str, Any]) -> None:
    def require(phases: tuple[str, ...], nonempty_status: str, label: str) -> None:
        for phase in phases:
            checkpoint = checkpoints[phase]
            if nonempty_status == "running" and _exact_nonempty_running(checkpoint):
                continue
            if (
                nonempty_status == "restore_blocked"
                and checkpoint.status == "restore_blocked"
            ):
                continue
            if not _exact_empty_completed(checkpoint):
                raise LabResultOwnershipBackfillDependencyError(
                    f"{label} restore checkpoint state is invalid"
                )

    require((RAW_OWNERSHIP_BACKFILL_PHASE,), "restore_blocked", "Stage-3A")
    require(_B_PHASES + _C_PHASES, "running", "Stage-3B/3C")
    require(_D_PHASES + _E_PHASES, "restore_blocked", "Stage-3D/3E")
    require(_F_PHASES + _G_PHASES, "running", "Stage-3F/3G")
    require(_H_PHASES, "restore_blocked", "Stage-3H")
    require(_L_PHASES, "running", "Stage-3L")
    # Stage 3K is excluded from backup v1 entirely, so its retained checkpoint is
    # prepared or preserved rather than rebased onto incoming bounds.
    for phase in _K_PHASES:
        if checkpoints[phase].status not in {"running", "completed"}:
            raise LabResultOwnershipBackfillDependencyError(
                "Stage-3K retained checkpoint state is invalid"
            )

    exercises = checkpoints[
        HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_exercises"]
    ]
    sets = checkpoints[HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_sets"]]
    if (exercises.status, sets.status) not in {
        ("restore_blocked", "restore_blocked"),
        ("restore_blocked", "completed"),
        ("completed", "completed"),
    }:
        raise LabResultOwnershipBackfillDependencyError(
            "Stage-3E restore checkpoint order is inconsistent"
        )

    compounds = checkpoints[
        HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hrt_compounds"]
    ]
    components = checkpoints[
        HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
            "hrt_compound_components"
        ]
    ]
    if (compounds.status, components.status) not in {
        ("running", "running"),
        ("running", "completed"),
        ("completed", "completed"),
    }:
        raise LabResultOwnershipBackfillDependencyError(
            "Stage-3F restore checkpoint order is inconsistent"
        )


def _validate_own(checkpoint: Any | None, *, scope: _Scope) -> str | None:
    if checkpoint is None:
        return None
    status = _validate_checkpoint(
        checkpoint, phase=_PHASE_KEY, subject_id=scope.subject_id
    )
    if status == "restore_blocked":
        raise LabResultOwnershipBackfillStateError(
            "Stage-3M checkpoints cannot be restore-blocked"
        )
    return status


def _require_dependencies(
    checkpoints: Mapping[str, Any], *, scope: _Scope, own_exists: bool
) -> bool:
    if set(checkpoints) != set(_PRIOR_PHASES):
        raise LabResultOwnershipBackfillDependencyError(
            "Stage-3A through Stage-3L checkpoints are incomplete"
        )
    statuses = {
        phase: _validate_checkpoint(
            checkpoints[phase], phase=phase, subject_id=scope.subject_id
        )
        for phase in _PRIOR_PHASES
    }
    if all(status == "completed" for status in statuses.values()):
        return False
    if not own_exists:
        raise LabResultOwnershipBackfillDependencyError(
            "restore-mode Stage-3M requires its exact portability checkpoint"
        )
    _require_restore_dependencies(checkpoints)
    return True


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise LabResultOwnershipBackfillProvenanceError(
                "lab result contains a non-finite JSON number"
            )
        return ["float", value.hex()]
    if isinstance(value, uuid.UUID):
        return ["uuid", str(value)]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat()]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, time):
        return ["time", value.isoformat()]
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise LabResultOwnershipBackfillProvenanceError(
                "lab result JSON object keys must be strings"
            )
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise LabResultOwnershipBackfillProvenanceError(
        "lab result contains an unsupported JSON value"
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


def _connection_select():
    table = IntegrationConnection.__table__
    return select(*(table.c[field] for field in _CONNECTION_FIELDS))


def _raw_select():
    table = RawPayload.__table__
    return select(*(table.c[field] for field in _RAW_FIELDS))


def _values(row: Any, fields: tuple[str, ...]) -> SimpleNamespace:
    mapping = row._mapping if hasattr(row, "_mapping") else row
    return SimpleNamespace(**{field: mapping[field] for field in fields})


def _row_values(row: Any) -> SimpleNamespace:
    return _values(row, _ROW_FIELDS)


def _connection_values(row: Any) -> SimpleNamespace:
    return _values(row, _CONNECTION_FIELDS)


def _raw_values(row: Any) -> SimpleNamespace:
    return _values(row, _RAW_FIELDS)


def _data_envelope(row: Any) -> list[Any]:
    return ["lab_results", *[getattr(row, field) for field in _DATA_FIELDS]]


def _ownership_envelope(row: Any) -> list[Any]:
    return [
        "lab_results",
        row.id,
        row.subject_id,
        row.actor_user_id,
        row.raw_payload_id,
    ]


def _same_values(left: Any, right: Any, fields: tuple[str, ...]) -> bool:
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _validate_fact_values(row: Any) -> None:
    """Reject a result whose reviewed business shape cannot be trusted."""

    if not isinstance(row.date, date):
        raise LabResultOwnershipBackfillProvenanceError(
            "lab result has an invalid date"
        )
    if (
        not isinstance(row.marker, str)
        or not row.marker.strip()
        or len(row.marker) > _MAX_MARKER_LENGTH
    ):
        raise LabResultOwnershipBackfillProvenanceError(
            "lab result has an invalid marker"
        )
    for field in ("value", "ref_low", "ref_high"):
        number = getattr(row, field)
        if field != "value" and number is None:
            continue
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
        ):
            raise LabResultOwnershipBackfillProvenanceError(
                "lab result has a missing or non-finite measurement"
            )
    if row.flag is not None and row.flag not in _ALLOWED_FLAGS:
        raise LabResultOwnershipBackfillProvenanceError(
            "lab result has an unsupported range flag"
        )
    for field in ("unit", "lab_name", "note"):
        text_value = getattr(row, field)
        if text_value is not None and not isinstance(text_value, str):
            raise LabResultOwnershipBackfillProvenanceError(
                "lab result has an invalid text field"
            )


def _validate_gateway_connection(connection: Any, *, scope: _Scope) -> None:
    """The only reviewed lab connection root is the subject OpenRouter gateway."""

    if (
        connection.subject_id != scope.subject_id
        or connection.provider != IntegrationProvider.OPENROUTER.value
        or connection.connection_type != IntegrationConnectionType.AI_GATEWAY.value
        or connection.status not in _HISTORICAL_CONNECTION_STATUSES
    ):
        raise LabResultOwnershipBackfillProvenanceError(
            "lab parser raw payload has invalid AI gateway provenance"
        )


def _validate_document(asset: Any, *, scope: _Scope, raw: Any) -> None:
    if (
        asset.subject_id != scope.subject_id
        or asset.purpose != FileAssetPurpose.LAB_DOCUMENT.value
        or asset.uploaded_by_user_id not in {None, scope.owner_user_id}
        or asset.storage_ref != raw.external_id
    ):
        raise LabResultOwnershipBackfillProvenanceError(
            "lab parser file provenance is inconsistent"
        )


def _validate_raw(
    raw: Any,
    *,
    scope: _Scope,
    connections: Mapping[uuid.UUID, Any],
    files: Mapping[uuid.UUID, Any],
    parser_invocations: Mapping[int, tuple[int, int, bool]],
    source: str,
    fact_is_unowned: bool,
) -> None:
    """Validate the reviewed raw root a lab result links, without adopting it."""

    if raw.domain != Domain.LABS.value or raw.source != source:
        raise LabResultOwnershipBackfillProvenanceError(
            "lab result raw payload has an invalid domain or source"
        )
    raw_roots = (
        raw.subject_id,
        raw.actor_user_id,
        raw.integration_connection_id,
        raw.file_asset_id,
    )
    # Backup v1 restores the raw lake before Stage 3A runs again, so a still
    # fully-unowned raw is valid provenance for a still-unowned fact — and only
    # for one.  An adopted fact may never point at unowned raw history.
    raw_is_unowned = raw_roots == (None, None, None, None)
    if raw_is_unowned:
        if not fact_is_unowned:
            raise LabResultOwnershipBackfillStateError(
                "an owned lab result links unowned raw provenance"
            )
    else:
        if raw.subject_id != scope.subject_id:
            raise LabResultOwnershipBackfillProvenanceError(
                "lab result raw payload has foreign provenance"
            )
        if raw.actor_user_id not in {None, scope.owner_user_id}:
            raise LabResultOwnershipBackfillProvenanceError(
                "lab result raw payload actor is outside the reviewed owner boundary"
            )
    total, succeeded, same_subject = parser_invocations.get(raw.id, (0, 0, True))
    if not same_subject:
        raise LabResultOwnershipBackfillStateError(
            "lab result raw payload has a foreign parser invocation"
        )
    if source in _MANUAL_SOURCES:
        if raw.integration_connection_id is not None or raw.file_asset_id is not None:
            raise LabResultOwnershipBackfillProvenanceError(
                "manual or MCP lab provenance cannot carry connection or file roots"
            )
        if total != 0:
            raise LabResultOwnershipBackfillProvenanceError(
                "manual or MCP lab raw payload cannot claim a document parser"
            )
        return
    if raw_is_unowned:
        if total != 0:
            raise LabResultOwnershipBackfillStateError(
                "unowned lab parser raw cannot claim a document parser invocation"
            )
        return
    if raw.integration_connection_id is not None:
        # Reviewed subject-funded parser history: the gateway paid for the parse,
        # so no platform invocation may exist and no file root was registered.
        connection = connections.get(raw.integration_connection_id)
        if connection is None:
            raise LabResultOwnershipBackfillStateError(
                "lab result raw payload references a missing gateway connection"
            )
        _validate_gateway_connection(connection, scope=scope)
        if total != 0:
            raise LabResultOwnershipBackfillProvenanceError(
                "lab parser raw mixes subject and platform AI provenance"
            )
        if raw.file_asset_id is not None:
            raise LabResultOwnershipBackfillProvenanceError(
                "subject-funded lab parser history cannot claim a file root"
            )
        return
    if raw.file_asset_id is None:
        # Pre-FileAsset legacy history, and the shape a backup-v1 restore leaves
        # behind once F/C are stripped.  Registering the document is Stage-3A and
        # PR-06 work; this phase only refuses a forged parser claim.
        if total != 0:
            raise LabResultOwnershipBackfillProvenanceError(
                "fileless lab parser raw cannot claim a document parser invocation"
            )
        return
    asset = files.get(raw.file_asset_id)
    if asset is None:
        raise LabResultOwnershipBackfillStateError(
            "lab result raw payload references a missing document asset"
        )
    _validate_document(asset, scope=scope, raw=raw)
    if succeeded != 1:
        raise LabResultOwnershipBackfillProvenanceError(
            "platform lab parser raw lacks one successful AI invocation"
        )


def _validate_row(
    row: Any,
    *,
    scope: _Scope,
    connections: Mapping[uuid.UUID, Any],
    raws: Mapping[int, Any],
    files: Mapping[uuid.UUID, Any],
    parser_invocations: Mapping[int, tuple[int, int, bool]],
    historical: bool,
    allow_unowned: bool,
) -> bool:
    """Validate one row and return whether sole-subject adoption is required."""

    if row.domain != Domain.LABS.value or row.source not in _ALLOWED_SOURCES:
        raise LabResultOwnershipBackfillProvenanceError(
            "lab result has invalid domain or source"
        )
    if not isinstance(row.id, int) or isinstance(row.id, bool) or row.id <= 0:
        raise LabResultOwnershipBackfillValidationError(
            "lab result has an invalid primary key"
        )
    _validate_fact_values(row)

    roots = (row.subject_id, row.actor_user_id)
    needs_adoption = roots == (None, None)
    if needs_adoption:
        if not allow_unowned:
            raise LabResultOwnershipBackfillStateError(
                "an unowned lab result is outside the historical bridge"
            )
    elif row.subject_id != scope.subject_id:
        raise LabResultOwnershipBackfillStateError(
            "lab result has partial or foreign ownership roots"
        )
    if not needs_adoption and row.actor_user_id not in {None, scope.owner_user_id}:
        raise LabResultOwnershipBackfillStateError(
            "lab result actor is outside the reviewed ownership boundary"
        )

    if row.raw_payload_id is None:
        # A rawless result is legitimate for every reviewed source: the writer
        # accepts a parsed panel typed in by hand, and older parses predate the
        # raw-first boundary.  Registering that provenance is Stage-3A work.
        return needs_adoption
    raw = raws.get(row.raw_payload_id)
    if raw is None:
        raise LabResultOwnershipBackfillStateError(
            "lab result references a missing raw payload"
        )
    _validate_raw(
        raw,
        scope=scope,
        connections=connections,
        files=files,
        parser_invocations=parser_invocations,
        source=row.source,
        fact_is_unowned=needs_adoption,
    )
    # The fact carries no actor of its own beyond the owner boundary above, so
    # the only cross-root rule is that an adopted history may not disagree with
    # a raw payload that already names the owner.
    if not historical and row.actor_user_id != raw.actor_user_id:
        raise LabResultOwnershipBackfillProvenanceError(
            "live lab result and raw payload have different actor roots"
        )
    return needs_adoption


async def _after_lab_results_projection_for_test() -> None:
    """Deterministic seam for real PostgreSQL lock/recheck tests."""


async def _project_connections(
    session: AsyncSession, connection_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Any]:
    if not connection_ids:
        return {}
    rows = await session.execute(
        _connection_select()
        .where(IntegrationConnection.id.in_(connection_ids))
        .order_by(IntegrationConnection.id)
    )
    return {row.id: row for row in map(_connection_values, rows)}


async def _project_raws(
    session: AsyncSession, raw_payload_ids: set[int]
) -> dict[int, Any]:
    if not raw_payload_ids:
        return {}
    rows = await session.execute(
        _raw_select().where(RawPayload.id.in_(raw_payload_ids)).order_by(RawPayload.id)
    )
    return {row.id: row for row in map(_raw_values, rows)}


async def _project_parser_invocation_scope(
    session: AsyncSession, *, scope: _Scope, raws: Mapping[int, Any]
) -> dict[int, tuple[int, int, bool]]:
    """Return per-raw document-parser counts restricted to the reviewed subject."""

    if not raws:
        return {}
    rows = await session.execute(
        select(
            AIInvocation.raw_payload_id,
            AIInvocation.subject_id,
            AIInvocation.status,
        )
        .where(
            AIInvocation.raw_payload_id.in_(set(raws)),
            AIInvocation.purpose
            == AIInvocationPurpose.LAB_DOCUMENT_PARSE.value,
        )
        .order_by(AIInvocation.raw_payload_id, AIInvocation.id)
    )
    counts: dict[int, tuple[int, int, bool]] = {
        raw_payload_id: (0, 0, True) for raw_payload_id in raws
    }
    for raw_payload_id, subject_id, status in rows:
        total, succeeded, same_subject = counts[int(raw_payload_id)]
        counts[int(raw_payload_id)] = (
            total + 1,
            succeeded + int(status == AIInvocationStatus.SUCCEEDED.value),
            same_subject and subject_id == scope.subject_id,
        )
    return counts


def _file_select():
    table = FileAsset.__table__
    return select(*(table.c[field] for field in _FILE_FIELDS))


def _file_values(row: Any) -> SimpleNamespace:
    return _values(row, _FILE_FIELDS)


async def _project_files(
    session: AsyncSession, file_asset_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Any]:
    if not file_asset_ids:
        return {}
    rows = await session.execute(
        _file_select()
        .where(FileAsset.id.in_(file_asset_ids))
        .order_by(FileAsset.id)
    )
    return {row.id: row for row in map(_file_values, rows)}


def _referenced_file_ids(raws: Mapping[int, Any]) -> set[uuid.UUID]:
    return {
        raw.file_asset_id
        for raw in raws.values()
        if raw.file_asset_id is not None
    }


def _referenced_connection_ids(raws: Mapping[int, Any]) -> set[uuid.UUID]:
    return {
        raw.integration_connection_id
        for raw in raws.values()
        if raw.integration_connection_id is not None
    }


async def _lock_projected_graph(
    session: AsyncSession,
    *,
    projected_rows: Mapping[int, Any],
    projected_connections: Mapping[uuid.UUID, Any],
    projected_raws: Mapping[int, Any],
) -> tuple[dict[int, Any], dict[uuid.UUID, Any], dict[int, Any]]:
    locked_connections = await _lock_projected_connections(
        session, projected_connections
    )
    locked_raws = await _lock_projected_raws(session, projected_raws)
    locked_rows = await _lock_projected_rows(session, projected_rows)
    return locked_rows, locked_connections, locked_raws


async def _lock_projected_connections(
    session: AsyncSession,
    projected_connections: Mapping[uuid.UUID, Any],
) -> dict[uuid.UUID, Any]:
    connection_ids = set(projected_connections)
    if connection_ids:
        locked_raw = await session.execute(
            _connection_select()
            .where(IntegrationConnection.id.in_(connection_ids))
            .order_by(IntegrationConnection.id)
            .with_for_update()
        )
        locked_connections = {
            row.id: row for row in map(_connection_values, locked_raw)
        }
        if set(locked_connections) != connection_ids or any(
            not _same_values(
                locked_connections[key], projected_connections[key], _CONNECTION_FIELDS
            )
            for key in connection_ids
        ):
            raise LabResultOwnershipBackfillStateError(
                "a projected provider connection changed before it was locked"
            )
    else:
        locked_connections = {}
    return locked_connections


async def _lock_projected_raws(
    session: AsyncSession,
    projected_raws: Mapping[int, Any],
) -> dict[int, Any]:
    raw_payload_ids = set(projected_raws)
    if not raw_payload_ids:
        return {}
    locked = await session.execute(
        _raw_select()
        .where(RawPayload.id.in_(raw_payload_ids))
        .order_by(RawPayload.id)
        .with_for_update()
    )
    locked_raws = {row.id: row for row in map(_raw_values, locked)}
    if set(locked_raws) != raw_payload_ids or any(
        not _same_values(locked_raws[key], projected_raws[key], _RAW_FIELDS)
        for key in raw_payload_ids
    ):
        raise LabResultOwnershipBackfillStateError(
            "a projected lab result raw payload changed before it was locked"
        )
    return locked_raws


async def _lock_projected_rows(
    session: AsyncSession,
    projected_rows: Mapping[int, Any],
) -> dict[int, Any]:
    row_ids = tuple(sorted(projected_rows))
    if not row_ids:
        return {}
    locked_raw = await session.execute(
        _row_select()
        .where(_TABLE.c.id.in_(row_ids))
        .order_by(_TABLE.c.id)
        .with_for_update()
    )
    locked_rows = {row.id: row for row in map(_row_values, locked_raw)}
    if set(locked_rows) != set(projected_rows) or any(
        not _same_values(locked_rows[key], projected_rows[key], _ROW_FIELDS)
        for key in projected_rows
    ):
        raise LabResultOwnershipBackfillStateError(
            "a projected lab result changed before it was locked"
        )
    return locked_rows


async def _project_and_lock_ids(
    session: AsyncSession,
    ids: list[int],
    *,
    scope: _Scope,
    invoke_race_hook: bool,
) -> tuple[
    dict[int, Any],
    dict[uuid.UUID, Any],
    dict[int, Any],
    dict[uuid.UUID, Any],
    dict[int, tuple[int, int, bool]],
]:
    if not ids:
        return {}, {}, {}, {}, {}
    raw_rows = await session.execute(
        _row_select().where(_TABLE.c.id.in_(ids)).order_by(_TABLE.c.id)
    )
    projected_rows = {row.id: row for row in map(_row_values, raw_rows)}
    raw_payload_ids = {row.raw_payload_id for row in projected_rows.values() if row.raw_payload_id is not None}
    projected_raws = await _project_raws(session, raw_payload_ids)
    projected_connections = await _project_connections(
        session, _referenced_connection_ids(projected_raws)
    )
    if invoke_race_hook:
        await _after_lab_results_projection_for_test()
    locked = await _lock_projected_graph(
        session,
        projected_rows=projected_rows,
        projected_connections=projected_connections,
        projected_raws=projected_raws,
    )
    files = await _project_files(session, _referenced_file_ids(locked[2]))
    parser_invocations = await _project_parser_invocation_scope(
        session, scope=scope, raws=locked[2]
    )
    return (*locked, files, parser_invocations)


def _row_policy(row_id: int, checkpoint: Any | None) -> tuple[bool, bool]:
    """Return the historical and unowned-bridge classifications for one row."""

    if checkpoint is None:
        return True, True
    if checkpoint.status == "running":
        if row_id <= checkpoint.last_scanned_id:
            return True, False
        if row_id <= checkpoint.scan_high_watermark_id:
            return True, True
        return False, False
    if checkpoint.status == "completed":
        return row_id <= checkpoint.scan_high_watermark_id, False
    raise LabResultOwnershipBackfillStateError(
        "Stage-3M checkpoint has an unsupported state"
    )


async def _referenced_connection_digest(
    session: AsyncSession,
    *,
    low: int,
    high: int | None,
    lock_connections: bool,
) -> tuple[int, str]:
    """Page the referenced C set, optionally locking it before any fact row."""

    count = 0
    digest = _EMPTY_SHA256
    cursor: uuid.UUID | None = None
    raw_table = RawPayload.__table__
    raw_refs = (
        select(raw_table.c.integration_connection_id.label("connection_id"))
        .select_from(
            _TABLE.join(raw_table, _TABLE.c.raw_payload_id == raw_table.c.id)
        )
        .where(
            _TABLE.c.id > low,
            raw_table.c.integration_connection_id.is_not(None),
        )
    )
    if high is not None:
        raw_refs = raw_refs.where(_TABLE.c.id <= high)
    refs = raw_refs.distinct().subquery()
    while True:
        query = select(refs.c.connection_id)
        if cursor is not None:
            query = query.where(refs.c.connection_id > cursor)
        connection_ids = list(
            await session.scalars(
                query.order_by(refs.c.connection_id)
                .limit(_PAGE_SIZE)
            )
        )
        if not connection_ids:
            break
        projected = await _project_connections(session, set(connection_ids))
        if set(projected) != set(connection_ids):
            raise LabResultOwnershipBackfillStateError(
                "a lab result references a missing provider connection"
            )
        if lock_connections:
            await _lock_projected_connections(session, projected)
        for connection_id in connection_ids:
            digest = _extend(digest, ["lab_results_connection", connection_id])
            count += 1
        cursor = connection_ids[-1]
    return count, digest


async def _referenced_raw_digest(
    session: AsyncSession,
    *,
    low: int,
    high: int | None,
    lock_raws: bool,
) -> tuple[int, str]:
    count = 0
    digest = _EMPTY_SHA256
    cursor = 0
    while True:
        query = select(_TABLE.c.raw_payload_id).where(
            _TABLE.c.id > low,
            _TABLE.c.raw_payload_id.is_not(None),
            _TABLE.c.raw_payload_id > cursor,
        )
        if high is not None:
            query = query.where(_TABLE.c.id <= high)
        raw_payload_ids = list(
            await session.scalars(
                query.distinct().order_by(_TABLE.c.raw_payload_id).limit(_PAGE_SIZE)
            )
        )
        if not raw_payload_ids:
            break
        projected = await _project_raws(session, set(raw_payload_ids))
        if set(projected) != set(raw_payload_ids):
            raise LabResultOwnershipBackfillStateError(
                "a lab result references a missing raw payload"
            )
        if lock_raws:
            await _lock_projected_raws(session, projected)
        for raw_payload_id in raw_payload_ids:
            digest = _extend(digest, ["lab_results_raw", raw_payload_id])
            count += 1
        cursor = raw_payload_ids[-1]
    return count, digest


async def _scan_current(
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
    locked_ref_count = 0
    locked_ref_digest = _EMPTY_SHA256
    locked_raw_count = 0
    locked_raw_digest = _EMPTY_SHA256
    if for_update:
        locked_ref_count, locked_ref_digest = await _referenced_connection_digest(
            session,
            low=low,
            high=high,
            lock_connections=True,
        )
        locked_raw_count, locked_raw_digest = await _referenced_raw_digest(
            session,
            low=low,
            high=high,
            lock_raws=True,
        )
    while True:
        query = (
            select(_TABLE.c.id)
            .where(_TABLE.c.id > cursor)
            .order_by(_TABLE.c.id)
            .limit(_PAGE_SIZE)
        )
        if high is not None:
            query = query.where(_TABLE.c.id <= high)
        ids = list(await session.scalars(query))
        if not ids:
            break
        if for_update:
            raw_rows = await session.execute(
                _row_select().where(_TABLE.c.id.in_(ids)).order_by(_TABLE.c.id)
            )
            projected_rows = {row.id: row for row in map(_row_values, raw_rows)}
            raws = await _project_raws(
                session,
                {
                    row.raw_payload_id
                    for row in projected_rows.values()
                    if row.raw_payload_id is not None
                },
            )
            connections = await _project_connections(
                session, _referenced_connection_ids(raws)
            )
            rows = await _lock_projected_rows(session, projected_rows)
        else:
            raw_rows = await session.execute(
                _row_select().where(_TABLE.c.id.in_(ids)).order_by(_TABLE.c.id)
            )
            rows = {row.id: row for row in map(_row_values, raw_rows)}
            raws = await _project_raws(
                session,
                {
                    row.raw_payload_id
                    for row in rows.values()
                    if row.raw_payload_id is not None
                },
            )
            connections = await _project_connections(
                session, _referenced_connection_ids(raws)
            )
        if set(rows) != set(ids):
            raise LabResultOwnershipBackfillStateError(
                "a projected lab result page changed during validation"
            )
        files = await _project_files(session, _referenced_file_ids(raws))
        parser_invocations = await _project_parser_invocation_scope(
            session, scope=scope, raws=raws
        )
        for row_id in ids:
            row = rows[row_id]
            historical, allow_unowned = _row_policy(row.id, checkpoint)
            needs_adoption = _validate_row(
                row,
                scope=scope,
                connections=connections,
                raws=raws,
                files=files,
                parser_invocations=parser_invocations,
                historical=historical,
                allow_unowned=allow_unowned,
            )
            if needs_adoption and checkpoint is not None and (
                checkpoint.status == "completed"
                or row.id <= checkpoint.last_scanned_id
            ):
                raise LabResultOwnershipBackfillStateError(
                    "a processed lab result row remained unowned"
                )
            if digest:
                data = _extend(data, _data_envelope(row))
                if not needs_adoption:
                    ownership = _extend(ownership, _ownership_envelope(row))
            cursor = row.id
            count += 1
    if for_update:
        current_ref_count, current_ref_digest = await _referenced_connection_digest(
            session,
            low=low,
            high=high,
            lock_connections=False,
        )
        if (
            current_ref_count != locked_ref_count
            or current_ref_digest != locked_ref_digest
        ):
            raise LabResultOwnershipBackfillStateError(
                "lab result provider references changed during validation"
            )
        current_raw_count, current_raw_digest = await _referenced_raw_digest(
            session,
            low=low,
            high=high,
            lock_raws=False,
        )
        if current_raw_count != locked_raw_count or current_raw_digest != locked_raw_digest:
            raise LabResultOwnershipBackfillStateError(
                "lab result raw references changed during validation"
            )
        return count, data, ownership


async def _bounds(session: AsyncSession) -> tuple[int, int]:
    high, count = (
        await session.execute(
            select(func.coalesce(func.max(_TABLE.c.id), 0), func.count())
        )
    ).one()
    high, count = int(high), int(count)
    if not _valid_counter(high) or not _valid_counter(count) or count > high:
        raise LabResultOwnershipBackfillValidationError(
            "lab result snapshot bounds are invalid"
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


async def _status_result(
    session: AsyncSession,
    *,
    scope: _Scope,
    checkpoint: Any | None,
    validate: bool,
    for_update: bool,
) -> LabResultOwnershipBackfillPreflightResult:
    if checkpoint is None:
        high, snapshot = await _bounds(session)
        status = LabResultOwnershipBackfillStatus.NOT_STARTED
        scanned = updated = unchanged = rows_above = 0
        remaining = snapshot
        before = after = ownership = _EMPTY_SHA256
        completed = False
    else:
        high, snapshot = (
            checkpoint.scan_high_watermark_id,
            checkpoint.snapshot_rows,
        )
        status = LabResultOwnershipBackfillStatus(checkpoint.status)
        scanned, updated, unchanged = (
            checkpoint.scanned_rows,
            checkpoint.updated_rows,
            checkpoint.unchanged_rows,
        )
        remaining = await _remaining(
            session, high=high, cursor=checkpoint.last_scanned_id
        )
        rows_above = int(
            await session.scalar(
                select(func.count()).select_from(_TABLE).where(_TABLE.c.id > high)
            )
            or 0
        )
        before, after, ownership = (
            checkpoint.data_checksum_before,
            checkpoint.data_checksum_after,
            checkpoint.ownership_checksum_after,
        )
        completed = status is LabResultOwnershipBackfillStatus.COMPLETED
    if validate:
        await _scan_current(
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
                raise LabResultOwnershipBackfillStateError(
                    "the lab result snapshot cardinality changed"
                )
    return LabResultOwnershipBackfillPreflightResult(
        phase_key=LAB_RESULT_OWNERSHIP_BACKFILL_PHASE,
        subject_id=scope.subject_id,
        status=status,
        tables_total=1,
        completed_tables=int(completed),
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
    result: LabResultOwnershipBackfillPreflightResult,
    *,
    scanned: int,
    updated: int,
    unchanged: int,
) -> LabResultOwnershipBackfillBatchResult:
    return LabResultOwnershipBackfillBatchResult(
        **{
            field: getattr(result, field)
            for field in LabResultOwnershipBackfillPreflightResult.__dataclass_fields__
        },
        batch_table="lab_results",
        batch_scanned_rows=scanned,
        batch_updated_rows=updated,
        batch_unchanged_rows=unchanged,
    )


async def preflight_lab_result_ownership_backfill(
    session: AsyncSession,
) -> LabResultOwnershipBackfillPreflightResult:
    """Validate the fixed Stage-3M graph without mutation."""

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=False)
        own = await _load_checkpoints(session, (_PHASE_KEY,), for_update=False)
        checkpoint = own.get(_PHASE_KEY)
        _validate_own(checkpoint, scope=scope)
        dependencies = await _load_checkpoints(
            session, _PRIOR_PHASES, for_update=False
        )
        _require_dependencies(
            dependencies, scope=scope, own_exists=checkpoint is not None
        )
        return await _status_result(
            session,
            scope=scope,
            checkpoint=checkpoint,
            validate=True,
            for_update=False,
        )


def _validate_restore_bounds(snapshot_bounds: Any) -> tuple[int, int]:
    if (
        not isinstance(snapshot_bounds, Mapping)
        or set(snapshot_bounds) != {"lab_results"}
    ):
        raise LabResultOwnershipBackfillValidationError(
            "snapshot_bounds must contain the exact weight table catalog"
        )
    pair = snapshot_bounds["lab_results"]
    if not isinstance(pair, tuple) or len(pair) != 2:
        raise LabResultOwnershipBackfillValidationError(
            "the lab result snapshot bound must be an exact pair"
        )
    high, count = pair
    if (
        not _valid_counter(high)
        or not _valid_counter(count)
        or count > high
        or (high == 0) != (count == 0)
    ):
        raise LabResultOwnershipBackfillValidationError(
            "the lab result snapshot bound is an invalid ID/count pair"
        )
    return high, count


async def reset_lab_result_ownership_backfill_for_portability_v1_restore(
    session: AsyncSession,
    *,
    snapshot_bounds: Mapping[str, tuple[int, int]],
) -> None:
    """Reset Stage-3M before the caller atomically replaces portable data."""

    high, count = _validate_restore_bounds(snapshot_bounds)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependencies = await _load_checkpoints(
            session, _PRIOR_PHASES, for_update=True
        )
        if set(dependencies) != set(_PRIOR_PHASES):
            raise LabResultOwnershipBackfillDependencyError(
                "Stage-3A through Stage-3L checkpoints are incomplete"
            )
        for phase in _PRIOR_PHASES:
            _validate_checkpoint(
                dependencies[phase], phase=phase, subject_id=scope.subject_id
            )
        _require_restore_dependencies(dependencies)
        own = await _load_checkpoints(session, (_PHASE_KEY,), for_update=True)
        checkpoint = own.get(_PHASE_KEY)
        _validate_own(checkpoint, scope=scope)
        if checkpoint is None:
            await _scan_current(
                session,
                scope=scope,
                checkpoint=None,
                for_update=True,
                digest=False,
            )
        else:
            # A portability replacement is allowed to reset progress, not to
            # conceal drift in the outgoing checkpoint evidence.
            await _status_result(
                session,
                scope=scope,
                checkpoint=checkpoint,
                validate=True,
                for_update=True,
            )
        status = "completed" if (high, count) == (0, 0) else "running"
        if checkpoint is None:
            checkpoint = OwnershipBackfillCheckpoint(
                phase_key=_PHASE_KEY,
                subject_id=scope.subject_id,
            )
            session.add(checkpoint)
        checkpoint.status = status
        checkpoint.scan_high_watermark_id = high
        checkpoint.snapshot_rows = count
        checkpoint.last_scanned_id = 0
        checkpoint.scanned_rows = 0
        checkpoint.updated_rows = 0
        checkpoint.unchanged_rows = 0
        checkpoint.data_checksum_before = _EMPTY_SHA256
        checkpoint.data_checksum_after = _EMPTY_SHA256
        checkpoint.ownership_checksum_after = _EMPTY_SHA256
        checkpoint.started_at = func.now()
        checkpoint.updated_at = func.now()
        checkpoint.completed_at = func.now() if status == "completed" else None
        await session.flush()


async def _create_checkpoint(
    session: AsyncSession, *, scope: _Scope, high: int, count: int
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


def _set_cached_subject(
    session: AsyncSession, row_id: int, subject_id: uuid.UUID
) -> None:
    cached = session.identity_map.get((LabResult, (row_id,), None))
    if cached is not None:
        attributes.set_committed_value(cached, "subject_id", subject_id)


async def run_lab_result_ownership_backfill_batch(
    session: AsyncSession,
    *,
    batch_size: int = DEFAULT_LAB_RESULT_OWNERSHIP_BACKFILL_BATCH_SIZE,
) -> LabResultOwnershipBackfillBatchResult:
    """Advance the fixed lab result table by at most one primary-key batch."""

    size = _validate_batch_size(batch_size)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependencies = await _load_checkpoints(
            session, _PRIOR_PHASES, for_update=True
        )
        own = await _load_checkpoints(session, (_PHASE_KEY,), for_update=True)
        checkpoint = own.get(_PHASE_KEY)
        _validate_own(checkpoint, scope=scope)
        _require_dependencies(
            dependencies, scope=scope, own_exists=checkpoint is not None
        )
        if checkpoint is not None and checkpoint.status == "completed":
            result = await _status_result(
                session,
                scope=scope,
                checkpoint=checkpoint,
                validate=True,
                for_update=True,
            )
            return _batch_result(result, scanned=0, updated=0, unchanged=0)
        await _scan_current(
            session,
            scope=scope,
            checkpoint=checkpoint,
            for_update=False,
            digest=False,
        )
        if checkpoint is None:
            high, count = await _bounds(session)
            checkpoint = await _create_checkpoint(
                session, scope=scope, high=high, count=count
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
        (
            rows,
            connections,
            raws,
            files,
            parser_invocations,
        ) = await _project_and_lock_ids(
            session, ids, scope=scope, invoke_race_hook=True
        )

        before = checkpoint.data_checksum_before
        after = checkpoint.data_checksum_after
        ownership = checkpoint.ownership_checksum_after
        updated_count = 0
        unchanged_count = 0
        for row_id in ids:
            row = rows[row_id]
            needs_adoption = _validate_row(
                row,
                scope=scope,
                connections=connections,
                raws=raws,
                files=files,
                parser_invocations=parser_invocations,
                historical=True,
                allow_unowned=True,
            )
            before = _extend(before, _data_envelope(row))
            if needs_adoption:
                result = await session.execute(
                    update(_TABLE)
                    .where(
                        _TABLE.c.id == row_id,
                        _TABLE.c.subject_id.is_(None),
                        _TABLE.c.actor_user_id.is_(None),
                    )
                    .values(subject_id=scope.subject_id, updated_at=row.updated_at)
                )
                if result.rowcount != 1:
                    raise LabResultOwnershipBackfillStateError(
                        "lab result ownership changed during adoption"
                    )
                _set_cached_subject(session, row_id, scope.subject_id)
                updated_count += 1
            else:
                unchanged_count += 1
            current_raw = await session.execute(
                _row_select().where(_TABLE.c.id == row_id).with_for_update()
            )
            current_result = current_raw.one_or_none()
            if current_result is None:
                raise LabResultOwnershipBackfillStateError(
                    "a lab result disappeared during adoption"
                )
            current = _row_values(current_result)
            if _validate_row(
                current,
                scope=scope,
                connections=connections,
                raws=raws,
                files=files,
                parser_invocations=parser_invocations,
                historical=True,
                allow_unowned=False,
            ):
                raise LabResultOwnershipBackfillStateError(
                    "a processed lab result remained unowned"
                )
            after = _extend(after, _data_envelope(current))
            ownership = _extend(ownership, _ownership_envelope(current))
        if before != after:
            raise LabResultOwnershipBackfillStateError(
                "lab result data changed while ownership was backfilled"
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
            frozen_count, data, current_ownership = await _scan_current(
                session,
                scope=scope,
                checkpoint=checkpoint,
                high=checkpoint.scan_high_watermark_id,
                for_update=True,
                digest=True,
            )
            if (
                frozen_count != checkpoint.snapshot_rows
                or data != checkpoint.data_checksum_before
                or data != checkpoint.data_checksum_after
                or current_ownership != checkpoint.ownership_checksum_after
            ):
                raise LabResultOwnershipBackfillStateError(
                    "the lab result snapshot changed during finalization"
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
    "LAB_RESULT_OWNERSHIP_BACKFILL_PHASE",
    "LAB_RESULT_OWNERSHIP_BACKFILL_TABLES",
    "LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES",
    "DEFAULT_LAB_RESULT_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "MAX_LAB_RESULT_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "LabResultOwnershipBackfillStatus",
    "LabResultOwnershipBackfillError",
    "LabResultOwnershipBackfillValidationError",
    "LabResultOwnershipBackfillIdentityError",
    "LabResultOwnershipBackfillDependencyError",
    "LabResultOwnershipBackfillStateError",
    "LabResultOwnershipBackfillProvenanceError",
    "LabResultOwnershipBackfillPreflightResult",
    "LabResultOwnershipBackfillBatchResult",
    "preflight_lab_result_ownership_backfill",
    "run_lab_result_ownership_backfill_batch",
    "reset_lab_result_ownership_backfill_for_portability_v1_restore",
]
