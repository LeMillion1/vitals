"""Bounded Stage-3R ownership backfill for optional-channel weekly digests.

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

from sqlalchemy import Table, and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationStatus,
    DigestKind,
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.ai import AIInvocation
from vitals.models.tenancy import IntegrationConnection
from vitals.models.milestones import WeeklyDigest
from vitals.services.conflict_rule_ownership_backfill_service import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.day_context_ownership_backfill_service import (
    DAY_CONTEXT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
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
from vitals.services.progress_photo_ownership_backfill_service import (
    PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.provider_raw_ownership_backfill_service import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.raw_ownership_backfill_service import RAW_OWNERSHIP_BACKFILL_PHASE
from vitals.services.shared_report_ownership_backfill_service import (
    SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.body_scan_metric_ownership_backfill_service import (
    BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.body_scan_ownership_backfill_service import (
    BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.garmin_weight_export_ownership_backfill_service import (
    GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.genetic_variant_ownership_backfill_service import (
    GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.lab_result_ownership_backfill_service import (
    LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.signal_ownership_backfill_service import (
    SIGNAL_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.weight_log_ownership_backfill_service import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.utils.timeutils import now_utc


WEEKLY_DIGEST_OWNERSHIP_BACKFILL_PHASE = (
    "stage3.retained_artifact.weekly_digests.v1"
)
WEEKLY_DIGEST_OWNERSHIP_BACKFILL_TABLES = ("weekly_digests",)
WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES: Mapping[str, str] = (
    MappingProxyType(
        {
            "weekly_digests": (
                f"{WEEKLY_DIGEST_OWNERSHIP_BACKFILL_PHASE}.weekly_digests"
            )
        }
    )
)
DEFAULT_WEEKLY_DIGEST_OWNERSHIP_BACKFILL_BATCH_SIZE = 250
MAX_WEEKLY_DIGEST_OWNERSHIP_BACKFILL_BATCH_SIZE = 1000

_TABLE: Table = WeeklyDigest.__table__
_PHASE_KEY = WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["weekly_digests"]
_PAGE_SIZE = 1000
_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_SOURCES = frozenset(
    {
        Source.MANUAL.value,
        Source.MCP.value,
        Source.SCHEDULER.value,
        Source.SYSTEM.value,
    }
)
_ALLOWED_KINDS = frozenset(kind.value for kind in DigestKind)
_INVOCATION_PURPOSE_BY_KIND = {
    DigestKind.WEEKLY.value: AIInvocationPurpose.WEEKLY_DIGEST.value,
    DigestKind.DAILY_BRIEF.value: AIInvocationPurpose.DAILY_BRIEF.value,
}
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
    "integration_connection_id",
    "ai_invocation_id",
    "date",
    "domain",
    "source",
    "kind",
    "content",
    "context_json",
    "model",
    "created_at",
    "updated_at",
)
_DATA_FIELDS = tuple(
    field
    for field in _ROW_FIELDS
    if field not in {"subject_id", "actor_user_id", "integration_connection_id"}
)
_INVOCATION_FIELDS = (
    "id",
    "subject_id",
    "actor_user_id",
    "purpose",
    "status",
)
_CONNECTION_FIELDS = (
    "id",
    "subject_id",
    "provider",
    "connection_type",
    "status",
)
_B_PHASES = tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
_C_PHASES = tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_D_PHASES = tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_E_PHASES = tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_F_PHASES = tuple(HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_G_PHASES = tuple(CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_H_PHASES = tuple(PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_I_PHASES = tuple(DAY_CONTEXT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_J_PHASES = tuple(SIGNAL_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_K_PHASES = tuple(SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_L_PHASES = tuple(WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_M_PHASES = tuple(LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_N_PHASES = tuple(GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_O_PHASES = tuple(BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_P_PHASES = tuple(BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_Q_PHASES = tuple(
    GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values()
)
_PRIOR_PHASES = (
    (RAW_OWNERSHIP_BACKFILL_PHASE,)
    + _B_PHASES
    + _C_PHASES
    + _D_PHASES
    + _E_PHASES
    + _F_PHASES
    + _G_PHASES
    + _H_PHASES
    + _I_PHASES
    + _J_PHASES
    + _K_PHASES
    + _L_PHASES
    + _M_PHASES
    + _N_PHASES
    + _O_PHASES
    + _P_PHASES
    + _Q_PHASES
)


class WeeklyDigestOwnershipBackfillStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"


class WeeklyDigestOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed Stage-3R errors."""


class WeeklyDigestOwnershipBackfillValidationError(
    WeeklyDigestOwnershipBackfillError, ValueError
):
    """A caller argument or persisted scalar is invalid."""


class WeeklyDigestOwnershipBackfillIdentityError(WeeklyDigestOwnershipBackfillError):
    """The exact-one reviewed owner graph is unavailable."""


class WeeklyDigestOwnershipBackfillDependencyError(
    WeeklyDigestOwnershipBackfillError
):
    """A prerequisite checkpoint is absent, malformed, or in the wrong mode."""


class WeeklyDigestOwnershipBackfillStateError(WeeklyDigestOwnershipBackfillError):
    """Checkpoint progress or an ownership root is inconsistent."""


class WeeklyDigestOwnershipBackfillProvenanceError(
    WeeklyDigestOwnershipBackfillError
):
    """A weight row has unsupported persisted provenance."""


@dataclass(frozen=True, slots=True)
class WeeklyDigestOwnershipBackfillPreflightResult:
    phase_key: str
    subject_id: uuid.UUID
    status: WeeklyDigestOwnershipBackfillStatus
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
        return self.status is WeeklyDigestOwnershipBackfillStatus.COMPLETED

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
class WeeklyDigestOwnershipBackfillBatchResult(
    WeeklyDigestOwnershipBackfillPreflightResult
):
    batch_table: str
    batch_scanned_rows: int
    batch_updated_rows: int
    batch_unchanged_rows: int

    @property
    def changed(self) -> bool:
        return self.batch_updated_rows > 0

    def to_safe_dict(self) -> dict[str, str | int]:
        result = WeeklyDigestOwnershipBackfillPreflightResult.to_safe_dict(self)
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
        or not 1 <= value <= MAX_WEEKLY_DIGEST_OWNERSHIP_BACKFILL_BATCH_SIZE
    ):
        raise WeeklyDigestOwnershipBackfillValidationError(
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
        WeeklyDigestOwnershipBackfillDependencyError
        if phase in _PRIOR_PHASES
        else WeeklyDigestOwnershipBackfillStateError
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
        raise WeeklyDigestOwnershipBackfillIdentityError(
            "weekly digest backfill requires exactly one health subject"
        )
    subject_id, owner_user_id = rows[0]
    owner_query = select(User.status).where(User.id == owner_user_id)
    if for_update:
        owner_query = owner_query.with_for_update()
    if await session.scalar(owner_query) != UserStatus.ACTIVE.value:
        raise WeeklyDigestOwnershipBackfillIdentityError(
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
                raise WeeklyDigestOwnershipBackfillDependencyError(
                    f"{label} restore checkpoint state is invalid"
                )

    require((RAW_OWNERSHIP_BACKFILL_PHASE,), "restore_blocked", "Stage-3A")
    require(_B_PHASES + _C_PHASES, "running", "Stage-3B/3C")
    require(_D_PHASES + _E_PHASES, "restore_blocked", "Stage-3D/3E")
    require(_F_PHASES + _G_PHASES, "running", "Stage-3F/3G")
    require(_H_PHASES, "restore_blocked", "Stage-3H")
    require(
        _I_PHASES + _J_PHASES + _L_PHASES + _M_PHASES + _N_PHASES + _P_PHASES,
        "running",
        "Stage-3I through Stage-3P resettable phases",
    )
    require(_O_PHASES + _Q_PHASES, "restore_blocked", "Stage-3O/3Q")
    # Stage 3K is excluded from backup v1 entirely, so its retained checkpoint is
    # prepared or preserved rather than rebased onto incoming bounds.
    for phase in _K_PHASES:
        if checkpoints[phase].status not in {"running", "completed"}:
            raise WeeklyDigestOwnershipBackfillDependencyError(
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
        raise WeeklyDigestOwnershipBackfillDependencyError(
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
        raise WeeklyDigestOwnershipBackfillDependencyError(
            "Stage-3F restore checkpoint order is inconsistent"
        )


def _validate_own(checkpoint: Any | None, *, scope: _Scope) -> str | None:
    if checkpoint is None:
        return None
    status = _validate_checkpoint(
        checkpoint, phase=_PHASE_KEY, subject_id=scope.subject_id
    )
    if status == "restore_blocked":
        raise WeeklyDigestOwnershipBackfillStateError(
            "Stage-3R checkpoints cannot be restore-blocked"
        )
    return status


def _require_dependencies(
    checkpoints: Mapping[str, Any], *, scope: _Scope, own_exists: bool
) -> bool:
    if set(checkpoints) != set(_PRIOR_PHASES):
        raise WeeklyDigestOwnershipBackfillDependencyError(
            "Stage-3A through Stage-3Q checkpoints are incomplete"
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
        raise WeeklyDigestOwnershipBackfillDependencyError(
            "restore-mode Stage-3R requires its exact portability checkpoint"
        )
    _require_restore_dependencies(checkpoints)
    return True


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WeeklyDigestOwnershipBackfillProvenanceError(
                "weekly digest contains a non-finite JSON number"
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
            raise WeeklyDigestOwnershipBackfillProvenanceError(
                "weekly digest JSON object keys must be strings"
            )
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise WeeklyDigestOwnershipBackfillProvenanceError(
        "weekly digest contains an unsupported JSON value"
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


def _values(row: Any, fields: tuple[str, ...]) -> SimpleNamespace:
    mapping = row._mapping if hasattr(row, "_mapping") else row
    return SimpleNamespace(**{field: mapping[field] for field in fields})


def _row_values(row: Any) -> SimpleNamespace:
    return _values(row, _ROW_FIELDS)


def _connection_values(row: Any) -> SimpleNamespace:
    return _values(row, _CONNECTION_FIELDS)


def _data_envelope(row: Any) -> list[Any]:
    return ["weekly_digests", *[getattr(row, field) for field in _DATA_FIELDS]]


def _ownership_envelope(row: Any) -> list[Any]:
    return [
        "weekly_digests",
        row.id,
        row.subject_id,
        row.actor_user_id,
        row.integration_connection_id,
        row.ai_invocation_id,
    ]


def _same_values(left: Any, right: Any, fields: tuple[str, ...]) -> bool:
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _validate_fact_values(row: Any) -> None:
    """Reject a digest whose reviewed artifact shape cannot be trusted."""

    if not isinstance(row.date, date):
        raise WeeklyDigestOwnershipBackfillProvenanceError(
            "weekly digest has an invalid date"
        )
    if row.kind not in _ALLOWED_KINDS:
        raise WeeklyDigestOwnershipBackfillProvenanceError(
            "weekly digest has an unsupported kind"
        )
    if not isinstance(row.content, str) or not row.content.strip():
        raise WeeklyDigestOwnershipBackfillProvenanceError(
            "weekly digest has no narrative content"
        )
    if row.model is not None and not isinstance(row.model, str):
        raise WeeklyDigestOwnershipBackfillProvenanceError(
            "weekly digest has an invalid model record"
        )


def _validate_connection(connection: Any, *, scope: _Scope) -> None:
    """The only reviewed digest connection is the subject OpenRouter gateway."""

    if (
        connection.subject_id != scope.subject_id
        or connection.provider != IntegrationProvider.OPENROUTER.value
        or connection.connection_type != IntegrationConnectionType.AI_GATEWAY.value
        or connection.status not in _HISTORICAL_CONNECTION_STATUSES
    ):
        raise WeeklyDigestOwnershipBackfillProvenanceError(
            "weekly digest has invalid AI gateway provenance"
        )


def _validate_invocation(invocation: Any, *, row: Any, scope: _Scope) -> None:
    if invocation.subject_id != scope.subject_id:
        raise WeeklyDigestOwnershipBackfillStateError(
            "weekly digest links a platform invocation of another subject"
        )
    if invocation.actor_user_id not in {None, scope.owner_user_id}:
        raise WeeklyDigestOwnershipBackfillStateError(
            "weekly digest invocation actor is outside the reviewed boundary"
        )
    expected = _INVOCATION_PURPOSE_BY_KIND.get(row.kind)
    if expected is not None and invocation.purpose != expected:
        raise WeeklyDigestOwnershipBackfillProvenanceError(
            "weekly digest invocation purpose does not match its kind"
        )
    if invocation.status != AIInvocationStatus.SUCCEEDED.value:
        raise WeeklyDigestOwnershipBackfillProvenanceError(
            "weekly digest links an invocation that never succeeded"
        )


def _validate_row(
    row: Any,
    *,
    scope: _Scope,
    connections: Mapping[uuid.UUID, Any],
    invocations: Mapping[uuid.UUID, Any],
    historical: bool,
    allow_unowned: bool,
) -> bool:
    """Validate one digest and return whether sole-subject adoption is required."""

    if row.domain != Domain.MILESTONES.value or row.source not in _ALLOWED_SOURCES:
        raise WeeklyDigestOwnershipBackfillProvenanceError(
            "weekly digest has invalid domain or source"
        )
    if not isinstance(row.id, int) or isinstance(row.id, bool) or row.id <= 0:
        raise WeeklyDigestOwnershipBackfillValidationError(
            "weekly digest has an invalid primary key"
        )
    _validate_fact_values(row)

    roots = (
        row.subject_id,
        row.actor_user_id,
        row.integration_connection_id,
    )
    needs_adoption = roots == (None, None, None)
    if needs_adoption:
        if not allow_unowned:
            raise WeeklyDigestOwnershipBackfillStateError(
                "an unowned weekly digest is outside the historical bridge"
            )
        if row.ai_invocation_id is not None:
            raise WeeklyDigestOwnershipBackfillStateError(
                "an unowned weekly digest cannot claim a platform invocation"
            )
        return True
    if row.subject_id != scope.subject_id:
        raise WeeklyDigestOwnershipBackfillStateError(
            "weekly digest has partial or foreign ownership roots"
        )
    if row.actor_user_id not in {None, scope.owner_user_id}:
        raise WeeklyDigestOwnershipBackfillStateError(
            "weekly digest actor is outside the reviewed ownership boundary"
        )
    if row.integration_connection_id is not None:
        connection = connections.get(row.integration_connection_id)
        if connection is None:
            raise WeeklyDigestOwnershipBackfillStateError(
                "weekly digest references a missing AI gateway connection"
            )
        _validate_connection(connection, scope=scope)
        # Subject-funded history and platform funding are mutually exclusive;
        # the model check constraint says the same thing in the schema.
        if row.ai_invocation_id is not None:
            raise WeeklyDigestOwnershipBackfillProvenanceError(
                "weekly digest mixes subject and platform AI funding"
            )
    if row.ai_invocation_id is not None:
        invocation = invocations.get(row.ai_invocation_id)
        if invocation is None:
            raise WeeklyDigestOwnershipBackfillStateError(
                "weekly digest references a missing platform invocation"
            )
        _validate_invocation(invocation, row=row, scope=scope)
    elif not historical and row.integration_connection_id is None:
        # Above the frozen watermark every generated artifact is funded through
        # the platform gateway.
        raise WeeklyDigestOwnershipBackfillProvenanceError(
            "a live weekly digest has no reviewed AI funding provenance"
        )
    return False


async def _after_weekly_digests_projection_for_test() -> None:
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


async def _project_invocations(
    session: AsyncSession, invocation_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Any]:
    if not invocation_ids:
        return {}
    table = AIInvocation.__table__
    rows = await session.execute(
        select(*(table.c[field] for field in _INVOCATION_FIELDS))
        .where(table.c.id.in_(invocation_ids))
        .order_by(table.c.id)
    )
    return {row.id: _values(row, _INVOCATION_FIELDS) for row in rows}


async def _lock_projected_invocations(
    session: AsyncSession,
    projected: Mapping[uuid.UUID, Any],
) -> dict[uuid.UUID, Any]:
    invocation_ids = set(projected)
    if not invocation_ids:
        return {}
    table = AIInvocation.__table__
    locked_raw = await session.execute(
        select(*(table.c[field] for field in _INVOCATION_FIELDS))
        .where(table.c.id.in_(invocation_ids))
        .order_by(table.c.id)
        .with_for_update()
    )
    locked = {row.id: _values(row, _INVOCATION_FIELDS) for row in locked_raw}
    if set(locked) != invocation_ids or any(
        not _same_values(locked[key], projected[key], _INVOCATION_FIELDS)
        for key in invocation_ids
    ):
        raise WeeklyDigestOwnershipBackfillStateError(
            "a projected platform invocation changed before it was locked"
        )
    return locked


async def _lock_projected_graph(
    session: AsyncSession,
    *,
    projected_rows: Mapping[int, Any],
    projected_connections: Mapping[uuid.UUID, Any],
    projected_invocations: Mapping[uuid.UUID, Any],
) -> tuple[dict[int, Any], dict[uuid.UUID, Any], dict[uuid.UUID, Any]]:
    locked_connections = await _lock_projected_connections(
        session, projected_connections
    )
    locked_invocations = await _lock_projected_invocations(
        session, projected_invocations
    )
    locked_rows = await _lock_projected_rows(session, projected_rows)
    return locked_rows, locked_connections, locked_invocations


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
            raise WeeklyDigestOwnershipBackfillStateError(
                "a projected provider connection changed before it was locked"
            )
    else:
        locked_connections = {}
    return locked_connections


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
        raise WeeklyDigestOwnershipBackfillStateError(
            "a projected weight changed before it was locked"
        )
    return locked_rows


async def _project_and_lock_ids(
    session: AsyncSession,
    ids: list[int],
    *,
    scope: _Scope,
    invoke_race_hook: bool,
) -> tuple[dict[int, Any], dict[uuid.UUID, Any], dict[uuid.UUID, Any]]:
    if not ids:
        return {}, {}, {}
    raw_rows = await session.execute(
        _row_select().where(_TABLE.c.id.in_(ids)).order_by(_TABLE.c.id)
    )
    projected_rows = {row.id: row for row in map(_row_values, raw_rows)}
    projected_connections = await _project_connections(
        session,
        {
            row.integration_connection_id
            for row in projected_rows.values()
            if row.integration_connection_id is not None
        },
    )
    projected_invocations = await _project_invocations(
        session,
        {
            row.ai_invocation_id
            for row in projected_rows.values()
            if row.ai_invocation_id is not None
        },
    )
    if invoke_race_hook:
        await _after_weekly_digests_projection_for_test()
    return await _lock_projected_graph(
        session,
        projected_rows=projected_rows,
        projected_connections=projected_connections,
        projected_invocations=projected_invocations,
    )


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
    raise WeeklyDigestOwnershipBackfillStateError(
        "Stage-3R checkpoint has an unsupported state"
    )


async def _referenced_connection_digest(
    session: AsyncSession,
    *,
    low: int,
    high: int | None,
    lock_connections: bool,
) -> tuple[int, str]:
    """Page the referenced gateway set, locking it before any digest row."""

    count = 0
    digest = _EMPTY_SHA256
    cursor: uuid.UUID | None = None
    while True:
        query = select(_TABLE.c.integration_connection_id).where(
            _TABLE.c.id > low,
            _TABLE.c.integration_connection_id.is_not(None),
        )
        if high is not None:
            query = query.where(_TABLE.c.id <= high)
        if cursor is not None:
            query = query.where(_TABLE.c.integration_connection_id > cursor)
        connection_ids = list(
            await session.scalars(
                query.distinct()
                .order_by(_TABLE.c.integration_connection_id)
                .limit(_PAGE_SIZE)
            )
        )
        if not connection_ids:
            break
        projected = await _project_connections(session, set(connection_ids))
        if set(projected) != set(connection_ids):
            raise WeeklyDigestOwnershipBackfillStateError(
                "a weekly digest references a missing AI gateway connection"
            )
        if lock_connections:
            await _lock_projected_connections(session, projected)
        for connection_id in connection_ids:
            digest = _extend(digest, ["weekly_digests_connection", connection_id])
            count += 1
        cursor = connection_ids[-1]
    return count, digest


async def _referenced_invocation_digest(
    session: AsyncSession,
    *,
    low: int,
    high: int | None,
    lock_invocations: bool,
) -> tuple[int, str]:
    count = 0
    digest = _EMPTY_SHA256
    cursor: uuid.UUID | None = None
    while True:
        query = select(_TABLE.c.ai_invocation_id).where(
            _TABLE.c.id > low,
            _TABLE.c.ai_invocation_id.is_not(None),
        )
        if high is not None:
            query = query.where(_TABLE.c.id <= high)
        if cursor is not None:
            query = query.where(_TABLE.c.ai_invocation_id > cursor)
        invocation_ids = list(
            await session.scalars(
                query.distinct()
                .order_by(_TABLE.c.ai_invocation_id)
                .limit(_PAGE_SIZE)
            )
        )
        if not invocation_ids:
            break
        projected = await _project_invocations(session, set(invocation_ids))
        if set(projected) != set(invocation_ids):
            raise WeeklyDigestOwnershipBackfillStateError(
                "a weekly digest references a missing platform invocation"
            )
        if lock_invocations:
            await _lock_projected_invocations(session, projected)
        for invocation_id in invocation_ids:
            digest = _extend(
                digest,
                [
                    "weekly_digests_invocation",
                    invocation_id,
                    projected[invocation_id].subject_id,
                ],
            )
            count += 1
        cursor = invocation_ids[-1]
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
    locked_inv_count = 0
    locked_inv_digest = _EMPTY_SHA256
    if for_update:
        locked_ref_count, locked_ref_digest = await _referenced_connection_digest(
            session,
            low=low,
            high=high,
            lock_connections=True,
        )
        locked_inv_count, locked_inv_digest = await _referenced_invocation_digest(
            session,
            low=low,
            high=high,
            lock_invocations=True,
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
        raw_rows = await session.execute(
            _row_select().where(_TABLE.c.id.in_(ids)).order_by(_TABLE.c.id)
        )
        projected_rows = {row.id: row for row in map(_row_values, raw_rows)}
        connections = await _project_connections(
            session,
            {
                row.integration_connection_id
                for row in projected_rows.values()
                if row.integration_connection_id is not None
            },
        )
        invocations = await _project_invocations(
            session,
            {
                row.ai_invocation_id
                for row in projected_rows.values()
                if row.ai_invocation_id is not None
            },
        )
        rows = (
            await _lock_projected_rows(session, projected_rows)
            if for_update
            else projected_rows
        )
        if set(rows) != set(ids):
            raise WeeklyDigestOwnershipBackfillStateError(
                "a projected weekly digest page changed during validation"
            )
        for row_id in ids:
            row = rows[row_id]
            historical, allow_unowned = _row_policy(row.id, checkpoint)
            needs_adoption = _validate_row(
                row,
                scope=scope,
                connections=connections,
                invocations=invocations,
                historical=historical,
                allow_unowned=allow_unowned,
            )
            if needs_adoption and checkpoint is not None and (
                checkpoint.status == "completed"
                or row.id <= checkpoint.last_scanned_id
            ):
                raise WeeklyDigestOwnershipBackfillStateError(
                    "a processed weekly digest row remained unowned"
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
            raise WeeklyDigestOwnershipBackfillStateError(
                "weekly digest gateway references changed during validation"
            )
        current_inv_count, current_inv_digest = await _referenced_invocation_digest(
            session,
            low=low,
            high=high,
            lock_invocations=False,
        )
        if (
            current_inv_count != locked_inv_count
            or current_inv_digest != locked_inv_digest
        ):
            raise WeeklyDigestOwnershipBackfillStateError(
                "weekly digest invocation references changed during validation"
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
        raise WeeklyDigestOwnershipBackfillValidationError(
            "weekly digest snapshot bounds are invalid"
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
) -> WeeklyDigestOwnershipBackfillPreflightResult:
    if checkpoint is None:
        high, snapshot = await _bounds(session)
        status = WeeklyDigestOwnershipBackfillStatus.NOT_STARTED
        scanned = updated = unchanged = rows_above = 0
        remaining = snapshot
        before = after = ownership = _EMPTY_SHA256
        completed = False
    else:
        high, snapshot = (
            checkpoint.scan_high_watermark_id,
            checkpoint.snapshot_rows,
        )
        status = WeeklyDigestOwnershipBackfillStatus(checkpoint.status)
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
        completed = status is WeeklyDigestOwnershipBackfillStatus.COMPLETED
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
                raise WeeklyDigestOwnershipBackfillStateError(
                    "the weight snapshot cardinality changed"
                )
    return WeeklyDigestOwnershipBackfillPreflightResult(
        phase_key=WEEKLY_DIGEST_OWNERSHIP_BACKFILL_PHASE,
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
    result: WeeklyDigestOwnershipBackfillPreflightResult,
    *,
    scanned: int,
    updated: int,
    unchanged: int,
) -> WeeklyDigestOwnershipBackfillBatchResult:
    return WeeklyDigestOwnershipBackfillBatchResult(
        **{
            field: getattr(result, field)
            for field in WeeklyDigestOwnershipBackfillPreflightResult.__dataclass_fields__
        },
        batch_table="weekly_digests",
        batch_scanned_rows=scanned,
        batch_updated_rows=updated,
        batch_unchanged_rows=unchanged,
    )


async def preflight_weekly_digest_ownership_backfill(
    session: AsyncSession,
) -> WeeklyDigestOwnershipBackfillPreflightResult:
    """Validate the fixed Stage-3R graph without mutation."""

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


async def prepare_weekly_digest_ownership_backfill_for_portability_v1_restore(
    session: AsyncSession,
) -> None:
    """Prepare or preserve retained Stage-3R evidence before replacement.

    Backup v1 neither exports nor replaces digests, so this phase never accepts
    incoming bounds: it validates the retained local artifacts and, on a first
    restore, freezes them as its own reviewed snapshot.
    """

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependencies = await _load_checkpoints(
            session, _PRIOR_PHASES, for_update=True
        )
        if set(dependencies) != set(_PRIOR_PHASES):
            raise WeeklyDigestOwnershipBackfillDependencyError(
                "Stage-3A through Stage-3Q checkpoints are incomplete"
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
            high, count = await _bounds(session)
            await _scan_current(
                session,
                scope=scope,
                checkpoint=None,
                high=high,
                for_update=True,
                digest=False,
            )
            if await _bounds(session) != (high, count):
                raise WeeklyDigestOwnershipBackfillStateError(
                    "retained weekly-digest bounds changed during restore preparation"
                )
            await _create_checkpoint(session, scope=scope, high=high, count=count)
        else:
            # A portability replacement may not conceal drift in the retained
            # artifacts it is not allowed to carry.
            await _status_result(
                session,
                scope=scope,
                checkpoint=checkpoint,
                validate=True,
                for_update=True,
            )
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
    cached = session.identity_map.get((WeeklyDigest, (row_id,), None))
    if cached is not None:
        attributes.set_committed_value(cached, "subject_id", subject_id)


async def run_weekly_digest_ownership_backfill_batch(
    session: AsyncSession,
    *,
    batch_size: int = DEFAULT_WEEKLY_DIGEST_OWNERSHIP_BACKFILL_BATCH_SIZE,
) -> WeeklyDigestOwnershipBackfillBatchResult:
    """Advance the fixed weight table by at most one primary-key batch."""

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
        rows, connections, invocations = await _project_and_lock_ids(
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
                invocations=invocations,
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
                        _TABLE.c.integration_connection_id.is_(None),
                    )
                    .values(subject_id=scope.subject_id, updated_at=row.updated_at)
                )
                if result.rowcount != 1:
                    raise WeeklyDigestOwnershipBackfillStateError(
                        "weekly digest ownership changed during adoption"
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
                raise WeeklyDigestOwnershipBackfillStateError(
                    "a weekly digest disappeared during adoption"
                )
            current = _row_values(current_result)
            current_connections = connections
            if (
                current.integration_connection_id is not None
                and current.integration_connection_id not in current_connections
            ):
                raise WeeklyDigestOwnershipBackfillStateError(
                    "weekly digest provider connection changed during adoption"
                )
            if _validate_row(
                current,
                scope=scope,
                connections=current_connections,
                invocations=invocations,
                historical=True,
                allow_unowned=False,
            ):
                raise WeeklyDigestOwnershipBackfillStateError(
                    "a processed weight remained unowned"
                )
            after = _extend(after, _data_envelope(current))
            ownership = _extend(ownership, _ownership_envelope(current))
        if before != after:
            raise WeeklyDigestOwnershipBackfillStateError(
                "weekly digest data changed while ownership was backfilled"
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
                raise WeeklyDigestOwnershipBackfillStateError(
                    "the weight snapshot changed during finalization"
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
    "WEEKLY_DIGEST_OWNERSHIP_BACKFILL_PHASE",
    "WEEKLY_DIGEST_OWNERSHIP_BACKFILL_TABLES",
    "WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES",
    "DEFAULT_WEEKLY_DIGEST_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "MAX_WEEKLY_DIGEST_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "WeeklyDigestOwnershipBackfillStatus",
    "WeeklyDigestOwnershipBackfillError",
    "WeeklyDigestOwnershipBackfillValidationError",
    "WeeklyDigestOwnershipBackfillIdentityError",
    "WeeklyDigestOwnershipBackfillDependencyError",
    "WeeklyDigestOwnershipBackfillStateError",
    "WeeklyDigestOwnershipBackfillProvenanceError",
    "WeeklyDigestOwnershipBackfillPreflightResult",
    "WeeklyDigestOwnershipBackfillBatchResult",
    "preflight_weekly_digest_ownership_backfill",
    "run_weekly_digest_ownership_backfill_batch",
    "prepare_weekly_digest_ownership_backfill_for_portability_v1_restore",
]
