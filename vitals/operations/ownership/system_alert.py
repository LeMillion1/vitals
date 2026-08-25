"""Bounded Stage-3T ownership backfill for optional-channel system alerts.

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

from sqlalchemy import Table, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes

from vitals.enums import (
    Severity,
    IntegrationConnectionStatus,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.ai import AIInvocation
from vitals.models.tenancy import IntegrationConnection
from vitals.services.tenancy_bootstrap import LEGACY_ACCOUNT_DISCRIMINATOR
from vitals.models.system_alert import SystemAlert
from vitals.services import alerts_service
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
from vitals.services.identity_service import acquire_identity_governance_lock
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
from vitals.operations.ownership.body_scan_metric import (
    BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.body_scan import (
    BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.genetic_variant import (
    GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.lab_result import (
    LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.garmin_weight_export import (
    GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.notification import (
    NOTIFICATION_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.weekly_digest import (
    WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.weight_log import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.utils.timeutils import now_utc


SYSTEM_ALERT_OWNERSHIP_BACKFILL_PHASE = (
    "stage3.subject_optional.system_alerts.v1"
)
SYSTEM_ALERT_OWNERSHIP_BACKFILL_TABLES = ("system_alerts",)
SYSTEM_ALERT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES: Mapping[str, str] = (
    MappingProxyType(
        {
            "system_alerts": (
                f"{SYSTEM_ALERT_OWNERSHIP_BACKFILL_PHASE}.system_alerts"
            )
        }
    )
)
DEFAULT_SYSTEM_ALERT_OWNERSHIP_BACKFILL_BATCH_SIZE = 250
MAX_SYSTEM_ALERT_OWNERSHIP_BACKFILL_BATCH_SIZE = 1000

_TABLE: Table = SystemAlert.__table__
_PHASE_KEY = SYSTEM_ALERT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["system_alerts"]
_PAGE_SIZE = 1000
_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ALERT_KEY_LENGTH = 128
_MAX_ENTITY_REF_LENGTH = 128
_AI_ALERT_KEY = "signal_parser_failed"
_CONFLICT_KEY_RE = re.compile(r"conflict:[1-9][0-9]*")
_HISTORICAL_CONNECTION_STATUSES = {
    IntegrationConnectionStatus.LEGACY.value,
    IntegrationConnectionStatus.ACTIVE.value,
    IntegrationConnectionStatus.DISABLED.value,
    IntegrationConnectionStatus.RETIRED.value,
}
_ROW_FIELDS = (
    "id",
    "subject_id",
    "integration_connection_id",
    "ai_invocation_id",
    "created_at",
    "domain",
    "severity",
    "message",
    "alert_key",
    "entity_ref",
    "override_at",
    "overridden_by_user_id",
    "resolved_at",
    "resolved_by_user_id",
)
_DATA_FIELDS = tuple(
    field
    for field in _ROW_FIELDS
    if field not in {"subject_id", "integration_connection_id"}
)
_INVOCATION_FIELDS = (
    "id",
    "subject_id",
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
_K_PHASES = tuple(SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_L_PHASES = tuple(WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_M_PHASES = tuple(LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_N_PHASES = tuple(GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_O_PHASES = tuple(BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_P_PHASES = tuple(BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_Q_PHASES = tuple(
    GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values()
)
_R_PHASES = tuple(WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_S_PHASES = tuple(NOTIFICATION_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
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
    + _O_PHASES
    + _P_PHASES
    + _Q_PHASES
    + _R_PHASES
    + _S_PHASES
)


class SystemAlertOwnershipBackfillStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"


class SystemAlertOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed Stage-3T errors."""


class SystemAlertOwnershipBackfillValidationError(
    SystemAlertOwnershipBackfillError, ValueError
):
    """A caller argument or persisted scalar is invalid."""


class SystemAlertOwnershipBackfillIdentityError(SystemAlertOwnershipBackfillError):
    """The exact-one reviewed owner graph is unavailable."""


class SystemAlertOwnershipBackfillDependencyError(
    SystemAlertOwnershipBackfillError
):
    """A prerequisite checkpoint is absent, malformed, or in the wrong mode."""


class SystemAlertOwnershipBackfillStateError(SystemAlertOwnershipBackfillError):
    """Checkpoint progress or an ownership root is inconsistent."""


class SystemAlertOwnershipBackfillProvenanceError(
    SystemAlertOwnershipBackfillError
):
    """A weight row has unsupported persisted provenance."""


@dataclass(frozen=True, slots=True)
class SystemAlertOwnershipBackfillPreflightResult:
    phase_key: str
    subject_id: uuid.UUID
    status: SystemAlertOwnershipBackfillStatus
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
        return self.status is SystemAlertOwnershipBackfillStatus.COMPLETED

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
class SystemAlertOwnershipBackfillBatchResult(
    SystemAlertOwnershipBackfillPreflightResult
):
    batch_table: str
    batch_scanned_rows: int
    batch_updated_rows: int
    batch_unchanged_rows: int

    @property
    def changed(self) -> bool:
        return self.batch_updated_rows > 0

    def to_safe_dict(self) -> dict[str, str | int]:
        result = SystemAlertOwnershipBackfillPreflightResult.to_safe_dict(self)
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
        or not 1 <= value <= MAX_SYSTEM_ALERT_OWNERSHIP_BACKFILL_BATCH_SIZE
    ):
        raise SystemAlertOwnershipBackfillValidationError(
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
        SystemAlertOwnershipBackfillDependencyError
        if phase in _PRIOR_PHASES
        else SystemAlertOwnershipBackfillStateError
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
        raise SystemAlertOwnershipBackfillIdentityError(
            "system alert backfill requires exactly one health subject"
        )
    subject_id, owner_user_id = rows[0]
    owner_query = select(User.status).where(User.id == owner_user_id)
    if for_update:
        owner_query = owner_query.with_for_update()
    if await session.scalar(owner_query) != UserStatus.ACTIVE.value:
        raise SystemAlertOwnershipBackfillIdentityError(
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
                raise SystemAlertOwnershipBackfillDependencyError(
                    f"{label} restore checkpoint state is invalid"
                )

    require((RAW_OWNERSHIP_BACKFILL_PHASE,), "restore_blocked", "Stage-3A")
    require(_B_PHASES + _C_PHASES, "running", "Stage-3B/3C")
    require(_D_PHASES + _E_PHASES, "restore_blocked", "Stage-3D/3E")
    require(_F_PHASES + _G_PHASES, "running", "Stage-3F/3G")
    require(_H_PHASES, "restore_blocked", "Stage-3H")
    require(
        _L_PHASES + _M_PHASES + _N_PHASES + _P_PHASES,
        "running",
        "Stage-3I through Stage-3P resettable phases",
    )
    require(_O_PHASES + _Q_PHASES, "restore_blocked", "Stage-3O/3Q")
    # Stage 3R and Stage 3S are excluded from backup v1, so their retained
    # checkpoints are prepared or preserved rather than rebased.
    for phase in _R_PHASES + _S_PHASES:
        if checkpoints[phase].status not in {"running", "completed"}:
            raise SystemAlertOwnershipBackfillDependencyError(
                "a retained Stage-3R/3S checkpoint state is invalid"
            )
    # Stage 3K is excluded from backup v1 entirely, so its retained checkpoint is
    # prepared or preserved rather than rebased onto incoming bounds.
    for phase in _K_PHASES:
        if checkpoints[phase].status not in {"running", "completed"}:
            raise SystemAlertOwnershipBackfillDependencyError(
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
        raise SystemAlertOwnershipBackfillDependencyError(
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
        raise SystemAlertOwnershipBackfillDependencyError(
            "Stage-3F restore checkpoint order is inconsistent"
        )


def _validate_own(checkpoint: Any | None, *, scope: _Scope) -> str | None:
    if checkpoint is None:
        return None
    status = _validate_checkpoint(
        checkpoint, phase=_PHASE_KEY, subject_id=scope.subject_id
    )
    if status == "restore_blocked":
        raise SystemAlertOwnershipBackfillStateError(
            "Stage-3T checkpoints cannot be restore-blocked"
        )
    return status


def _require_dependencies(
    checkpoints: Mapping[str, Any], *, scope: _Scope, own_exists: bool
) -> bool:
    if set(checkpoints) != set(_PRIOR_PHASES):
        raise SystemAlertOwnershipBackfillDependencyError(
            "Stage-3A through Stage-3S checkpoints are incomplete"
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
        raise SystemAlertOwnershipBackfillDependencyError(
            "restore-mode Stage-3T requires its exact portability checkpoint"
        )
    _require_restore_dependencies(checkpoints)
    return True


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SystemAlertOwnershipBackfillProvenanceError(
                "system alert contains a non-finite JSON number"
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
            raise SystemAlertOwnershipBackfillProvenanceError(
                "system alert JSON object keys must be strings"
            )
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise SystemAlertOwnershipBackfillProvenanceError(
        "system alert contains an unsupported JSON value"
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
    return ["system_alerts", *[getattr(row, field) for field in _DATA_FIELDS]]


def _ownership_envelope(row: Any) -> list[Any]:
    return [
        "system_alerts",
        row.id,
        row.subject_id,
        row.integration_connection_id,
        row.ai_invocation_id,
    ]


def _same_values(left: Any, right: Any, fields: tuple[str, ...]) -> bool:
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _validate_fact_values(row: Any) -> None:
    """Reject an alert whose reviewed ladder shape cannot be trusted."""

    if not isinstance(row.created_at, datetime):
        raise SystemAlertOwnershipBackfillProvenanceError(
            "system alert has an invalid creation timestamp"
        )
    if row.severity not in {item.value for item in Severity}:
        raise SystemAlertOwnershipBackfillProvenanceError(
            "system alert has an unsupported severity"
        )
    if not isinstance(row.message, str) or not row.message.strip():
        raise SystemAlertOwnershipBackfillProvenanceError(
            "system alert has no message"
        )
    if (
        not isinstance(row.alert_key, str)
        or not row.alert_key.strip()
        or len(row.alert_key) > _MAX_ALERT_KEY_LENGTH
    ):
        raise SystemAlertOwnershipBackfillProvenanceError(
            "system alert has an invalid key"
        )
    if (
        not isinstance(row.entity_ref, str)
        or len(row.entity_ref) > _MAX_ENTITY_REF_LENGTH
    ):
        raise SystemAlertOwnershipBackfillProvenanceError(
            "system alert has an invalid entity reference"
        )
    if row.override_at is None and row.overridden_by_user_id is not None:
        raise SystemAlertOwnershipBackfillProvenanceError(
            "system alert names an override actor without an override"
        )
    if row.resolved_at is None and row.resolved_by_user_id is not None:
        raise SystemAlertOwnershipBackfillProvenanceError(
            "system alert names a resolution actor without a resolution"
        )


def _alert_class(alert_key: str) -> tuple[str, IntegrationProvider | None]:
    """Classify one key through the writer's own reviewed allowlist.

    A health key wins over the historical provider registry: the same
    ``signal_parser_failed`` key is registered under OpenRouter only so old
    subject-connection alerts stay resolvable, while the schema requires the
    connection root to be null wherever a platform invocation funds the parse.
    """

    if alert_key in alerts_service.HEALTH_ALERT_KEYS or _CONFLICT_KEY_RE.fullmatch(
        alert_key
    ):
        # A conflict alert names one subject-owned or curated rule; the writer
        # classifies it as health for exactly that reason.
        return "health", None
    for provider, keys in alerts_service.PROVIDER_ALERT_KEYS.items():
        if alert_key in keys:
            return "provider", provider
    if alerts_service.is_platform_alert_key(alert_key):
        return "platform", None
    raise SystemAlertOwnershipBackfillProvenanceError(
        "system alert key is outside the reviewed ownership allowlist"
    )


async def _reviewed_provider_root(
    session: AsyncSession, *, scope: _Scope, provider: IntegrationProvider
) -> Any:
    """Return the exact reviewed legacy connection one provider alert describes."""

    connection_type = alerts_service.PROVIDER_ALERT_CONNECTION_TYPES[provider]
    table = IntegrationConnection.__table__
    rows = list(
        await session.execute(
            select(
                *(table.c[field] for field in _CONNECTION_FIELDS),
                table.c.external_account_discriminator,
            )
            .where(
                table.c.subject_id == scope.subject_id,
                table.c.provider == provider.value,
                table.c.connection_type == connection_type.value,
            )
            .order_by(table.c.id)
            .limit(2)
        )
    )
    if len(rows) != 1:
        raise SystemAlertOwnershipBackfillStateError(
            "a provider alert has no unambiguous connection root"
        )
    root = rows[0]
    if (
        root.external_account_discriminator != LEGACY_ACCOUNT_DISCRIMINATOR
        or root.status not in _HISTORICAL_CONNECTION_STATUSES
    ):
        raise SystemAlertOwnershipBackfillStateError(
            "the sole provider connection is not the reviewed legacy root"
        )
    return _connection_values(root)


def _validate_connection(
    connection: Any, *, scope: _Scope, provider: IntegrationProvider
) -> None:
    connection_type = alerts_service.PROVIDER_ALERT_CONNECTION_TYPES[provider]
    if (
        connection.subject_id != scope.subject_id
        or connection.provider != provider.value
        or connection.connection_type != connection_type.value
        or connection.status not in _HISTORICAL_CONNECTION_STATUSES
    ):
        raise SystemAlertOwnershipBackfillProvenanceError(
            "system alert has invalid provider connection provenance"
        )


def _validate_row(
    row: Any,
    *,
    scope: _Scope,
    connections: Mapping[uuid.UUID, Any],
    invocations: Mapping[uuid.UUID, Any],
    historical: bool,
    allow_unowned: bool,
) -> tuple[bool, IntegrationProvider | None]:
    """Validate one alert and return whether adoption is required, plus its class."""

    if not isinstance(row.id, int) or isinstance(row.id, bool) or row.id <= 0:
        raise SystemAlertOwnershipBackfillValidationError(
            "system alert has an invalid primary key"
        )
    _validate_fact_values(row)
    kind, provider = _alert_class(row.alert_key)

    for actor in (row.overridden_by_user_id, row.resolved_by_user_id):
        if actor not in {None, scope.owner_user_id}:
            raise SystemAlertOwnershipBackfillStateError(
                "system alert lifecycle actor is outside the reviewed boundary"
            )

    if kind == "platform":
        # An installation-wide alert legitimately owns neither root, so it is
        # never adopted and must never acquire one.
        if row.subject_id is not None or row.integration_connection_id is not None:
            raise SystemAlertOwnershipBackfillStateError(
                "a platform alert cannot claim subject or connection ownership"
            )
        if row.ai_invocation_id is not None:
            raise SystemAlertOwnershipBackfillProvenanceError(
                "a platform alert cannot claim a platform AI invocation"
            )
        return False, None

    if kind == "health":
        if row.integration_connection_id is not None:
            raise SystemAlertOwnershipBackfillStateError(
                "a health alert cannot claim a provider connection"
            )
        needs_adoption = row.subject_id is None
    else:
        if row.subject_id not in {None, scope.subject_id}:
            raise SystemAlertOwnershipBackfillStateError(
                "a provider alert belongs to another subject"
            )
        # Backup v1 rebinds S but strips C, so a subject-bound connection-less
        # provider alert is a restored row this phase still has to complete.
        needs_adoption = row.integration_connection_id is None

    if needs_adoption:
        if not allow_unowned:
            raise SystemAlertOwnershipBackfillStateError(
                "an unowned system alert is outside the historical bridge"
            )
        if row.ai_invocation_id is not None:
            raise SystemAlertOwnershipBackfillStateError(
                "an unowned system alert cannot claim a platform invocation"
            )
        return True, provider

    if row.subject_id != scope.subject_id:
        raise SystemAlertOwnershipBackfillStateError(
            "system alert belongs to another subject"
        )
    if provider is not None:
        connection = connections.get(row.integration_connection_id)
        if connection is None:
            raise SystemAlertOwnershipBackfillStateError(
                "system alert references a missing provider connection"
            )
        _validate_connection(connection, scope=scope, provider=provider)
    if row.ai_invocation_id is not None:
        if row.alert_key != _AI_ALERT_KEY or not row.entity_ref.strip():
            raise SystemAlertOwnershipBackfillProvenanceError(
                "only a parser alert with an entity reference may name an invocation"
            )
        invocation = invocations.get(row.ai_invocation_id)
        if invocation is None:
            raise SystemAlertOwnershipBackfillStateError(
                "system alert references a missing platform invocation"
            )
        if invocation.subject_id != scope.subject_id:
            raise SystemAlertOwnershipBackfillStateError(
                "system alert links a platform invocation of another subject"
            )
    return False, provider


async def _after_system_alerts_projection_for_test() -> None:
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


async def _lock_projected_graph(
    session: AsyncSession,
    *,
    projected_rows: Mapping[int, Any],
    projected_connections: Mapping[uuid.UUID, Any],
) -> tuple[dict[int, Any], dict[uuid.UUID, Any]]:
    locked_connections = await _lock_projected_connections(
        session, projected_connections
    )
    locked_rows = await _lock_projected_rows(session, projected_rows)
    return locked_rows, locked_connections


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
            raise SystemAlertOwnershipBackfillStateError(
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
        raise SystemAlertOwnershipBackfillStateError(
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
    if invoke_race_hook:
        await _after_system_alerts_projection_for_test()
    locked = await _lock_projected_graph(
        session,
        projected_rows=projected_rows,
        projected_connections=projected_connections,
    )
    invocations = await _project_invocations(
        session,
        {
            row.ai_invocation_id
            for row in locked[0].values()
            if row.ai_invocation_id is not None
        },
    )
    return (*locked, invocations)


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
    raise SystemAlertOwnershipBackfillStateError(
        "Stage-3T checkpoint has an unsupported state"
    )


async def _referenced_connection_digest(
    session: AsyncSession,
    *,
    low: int,
    high: int | None,
    lock_connections: bool,
) -> tuple[int, str]:
    """Page the referenced destination set, locking it before any outbox row."""

    count = 0
    digest = _EMPTY_SHA256
    cursor: uuid.UUID | None = None
    while True:
        query = select(_TABLE.c.integration_connection_id.label("connection_id")).where(
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
            raise SystemAlertOwnershipBackfillStateError(
                "a system alert references a missing destination account"
            )
        if lock_connections:
            await _lock_projected_connections(session, projected)
        for connection_id in connection_ids:
            digest = _extend(
                digest, ["system_alerts_connection", connection_id]
            )
            count += 1
        cursor = connection_ids[-1]
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
    if for_update:
        locked_ref_count, locked_ref_digest = await _referenced_connection_digest(
            session,
            low=low,
            high=high,
            lock_connections=True,
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
            raise SystemAlertOwnershipBackfillStateError(
                "a projected system alert page changed during validation"
            )
        for row_id in ids:
            row = rows[row_id]
            historical, allow_unowned = _row_policy(row.id, checkpoint)
            needs_adoption, provider = _validate_row(
                row,
                scope=scope,
                connections=connections,
                invocations=invocations,
                historical=historical,
                allow_unowned=allow_unowned,
            )
            if needs_adoption:
                if checkpoint is not None and (
                    checkpoint.status == "completed"
                    or row.id <= checkpoint.last_scanned_id
                ):
                    raise SystemAlertOwnershipBackfillStateError(
                        "a processed system alert row remained unowned"
                    )
                if provider is not None:
                    # Prove the reviewed connection root exists before the
                    # operator reaches its first mutating batch.
                    await _reviewed_provider_root(
                        session, scope=scope, provider=provider
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
            raise SystemAlertOwnershipBackfillStateError(
                "system alert connection references changed during validation"
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
        raise SystemAlertOwnershipBackfillValidationError(
            "system alert snapshot bounds are invalid"
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
) -> SystemAlertOwnershipBackfillPreflightResult:
    if checkpoint is None:
        high, snapshot = await _bounds(session)
        status = SystemAlertOwnershipBackfillStatus.NOT_STARTED
        scanned = updated = unchanged = rows_above = 0
        remaining = snapshot
        before = after = ownership = _EMPTY_SHA256
        completed = False
    else:
        high, snapshot = (
            checkpoint.scan_high_watermark_id,
            checkpoint.snapshot_rows,
        )
        status = SystemAlertOwnershipBackfillStatus(checkpoint.status)
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
        completed = status is SystemAlertOwnershipBackfillStatus.COMPLETED
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
                raise SystemAlertOwnershipBackfillStateError(
                    "the alert ladder snapshot cardinality changed"
                )
    return SystemAlertOwnershipBackfillPreflightResult(
        phase_key=SYSTEM_ALERT_OWNERSHIP_BACKFILL_PHASE,
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
    result: SystemAlertOwnershipBackfillPreflightResult,
    *,
    scanned: int,
    updated: int,
    unchanged: int,
) -> SystemAlertOwnershipBackfillBatchResult:
    return SystemAlertOwnershipBackfillBatchResult(
        **{
            field: getattr(result, field)
            for field in SystemAlertOwnershipBackfillPreflightResult.__dataclass_fields__
        },
        batch_table="system_alerts",
        batch_scanned_rows=scanned,
        batch_updated_rows=updated,
        batch_unchanged_rows=unchanged,
    )


async def preflight_system_alert_ownership_backfill(
    session: AsyncSession,
) -> SystemAlertOwnershipBackfillPreflightResult:
    """Validate the fixed Stage-3T graph without mutation."""

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
        or set(snapshot_bounds) != {"system_alerts"}
    ):
        raise SystemAlertOwnershipBackfillValidationError(
            "snapshot_bounds must contain the exact alert ladder table catalog"
        )
    pair = snapshot_bounds["system_alerts"]
    if not isinstance(pair, tuple) or len(pair) != 2:
        raise SystemAlertOwnershipBackfillValidationError(
            "the alert ladder snapshot bound must be an exact pair"
        )
    high, count = pair
    if (
        not _valid_counter(high)
        or not _valid_counter(count)
        or count > high
        or (high == 0) != (count == 0)
    ):
        raise SystemAlertOwnershipBackfillValidationError(
            "the alert ladder snapshot bound is an invalid ID/count pair"
        )
    return high, count


async def reset_system_alert_ownership_backfill_for_portability_v1_restore(
    session: AsyncSession,
    *,
    snapshot_bounds: Mapping[str, tuple[int, int]],
) -> None:
    """Reset Stage-3T before the caller atomically replaces portable data."""

    high, count = _validate_restore_bounds(snapshot_bounds)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependencies = await _load_checkpoints(
            session, _PRIOR_PHASES, for_update=True
        )
        if set(dependencies) != set(_PRIOR_PHASES):
            raise SystemAlertOwnershipBackfillDependencyError(
                "Stage-3A through Stage-3S checkpoints are incomplete"
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
    session: AsyncSession,
    row_id: int,
    subject_id: uuid.UUID,
    connection_id: uuid.UUID | None,
) -> None:
    cached = session.identity_map.get((SystemAlert, (row_id,), None))
    if cached is not None:
        attributes.set_committed_value(cached, "subject_id", subject_id)
        attributes.set_committed_value(
            cached, "integration_connection_id", connection_id
        )


async def run_system_alert_ownership_backfill_batch(
    session: AsyncSession,
    *,
    batch_size: int = DEFAULT_SYSTEM_ALERT_OWNERSHIP_BACKFILL_BATCH_SIZE,
) -> SystemAlertOwnershipBackfillBatchResult:
    """Advance the fixed alert ladder by at most one primary-key batch."""

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
        roots: dict[IntegrationProvider, Any] = {}
        for row_id in ids:
            row = rows[row_id]
            needs_adoption, provider = _validate_row(
                row,
                scope=scope,
                connections=connections,
                invocations=invocations,
                historical=True,
                allow_unowned=True,
            )
            before = _extend(before, _data_envelope(row))
            if needs_adoption:
                root = None
                if provider is not None:
                    root = roots.get(provider)
                    if root is None:
                        root = await _reviewed_provider_root(
                            session, scope=scope, provider=provider
                        )
                        roots[provider] = root
                        connections[root.id] = root
                subject_predicate = (
                    _TABLE.c.subject_id.is_(None)
                    if root is None
                    else or_(
                        _TABLE.c.subject_id.is_(None),
                        _TABLE.c.subject_id == scope.subject_id,
                    )
                )
                result = await session.execute(
                    update(_TABLE)
                    .where(
                        _TABLE.c.id == row_id,
                        subject_predicate,
                        _TABLE.c.integration_connection_id.is_(None),
                    )
                    .values(
                        subject_id=scope.subject_id,
                        integration_connection_id=(
                            None if root is None else root.id
                        ),
                    )
                )
                if result.rowcount != 1:
                    raise SystemAlertOwnershipBackfillStateError(
                        "system alert ownership changed during adoption"
                    )
                _set_cached_subject(
                    session,
                    row_id,
                    scope.subject_id,
                    None if root is None else root.id,
                )
                updated_count += 1
            else:
                unchanged_count += 1
            current_raw = await session.execute(
                _row_select().where(_TABLE.c.id == row_id).with_for_update()
            )
            current_result = current_raw.one_or_none()
            if current_result is None:
                raise SystemAlertOwnershipBackfillStateError(
                    "a system alert disappeared during adoption"
                )
            current = _row_values(current_result)
            if (
                current.integration_connection_id is not None
                and current.integration_connection_id not in connections
            ):
                raise SystemAlertOwnershipBackfillStateError(
                    "system alert connection changed during adoption"
                )
            if _validate_row(
                current,
                scope=scope,
                connections=connections,
                invocations=invocations,
                historical=True,
                allow_unowned=False,
            )[0]:
                raise SystemAlertOwnershipBackfillStateError(
                    "a processed system alert remained unowned"
                )
            after = _extend(after, _data_envelope(current))
            ownership = _extend(ownership, _ownership_envelope(current))
        if before != after:
            raise SystemAlertOwnershipBackfillStateError(
                "system alert data changed while ownership was backfilled"
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
                raise SystemAlertOwnershipBackfillStateError(
                    "the alert ladder snapshot changed during finalization"
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
    "SYSTEM_ALERT_OWNERSHIP_BACKFILL_PHASE",
    "SYSTEM_ALERT_OWNERSHIP_BACKFILL_TABLES",
    "SYSTEM_ALERT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES",
    "DEFAULT_SYSTEM_ALERT_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "MAX_SYSTEM_ALERT_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "SystemAlertOwnershipBackfillStatus",
    "SystemAlertOwnershipBackfillError",
    "SystemAlertOwnershipBackfillValidationError",
    "SystemAlertOwnershipBackfillIdentityError",
    "SystemAlertOwnershipBackfillDependencyError",
    "SystemAlertOwnershipBackfillStateError",
    "SystemAlertOwnershipBackfillProvenanceError",
    "SystemAlertOwnershipBackfillPreflightResult",
    "SystemAlertOwnershipBackfillBatchResult",
    "preflight_system_alert_ownership_backfill",
    "run_system_alert_ownership_backfill_batch",
    "reset_system_alert_ownership_backfill_for_portability_v1_restore",
]
