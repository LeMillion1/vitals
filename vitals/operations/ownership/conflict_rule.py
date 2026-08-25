"""Bounded Stage-3G ownership backfill for the mixed conflict-rule catalog.

Checked-in YAML definitions remain global.  Historical ad-hoc definitions are
adopted by the sole reviewed subject.  Callers own commit or rollback; this
service only flushes.
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
from sqlalchemy.orm import attributes

from vitals.enums import Domain, RuleType, Severity, UserStatus
from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.scoped_settings import SubjectSetting
from vitals.models.system_alert import SystemAlert
from vitals.services.conflict_activation_service import SETTING_KEY, SETTING_VERSION
from vitals.services.conflict_catalog import load_rule_catalog
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
from vitals.operations.ownership.provider_raw import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.raw import RAW_OWNERSHIP_BACKFILL_PHASE
from vitals.utils.timeutils import now_utc


CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE = "stage3.mixed_catalog.conflict_rules.v1"
CONFLICT_RULE_OWNERSHIP_BACKFILL_TABLES = ("conflict_rules",)
CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES: Mapping[str, str] = (
    MappingProxyType(
        {
            "conflict_rules": (
                f"{CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE}.conflict_rules"
            )
        }
    )
)
DEFAULT_CONFLICT_RULE_OWNERSHIP_BACKFILL_BATCH_SIZE = 250
MAX_CONFLICT_RULE_OWNERSHIP_BACKFILL_BATCH_SIZE = 1000

_TABLE: Table = ConflictRule.__table__
_PHASE_KEY = CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["conflict_rules"]
_PAGE_SIZE = 1000
_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CATALOG_FIELDS = (
    "rule_type",
    "domain_a",
    "condition_a",
    "domain_b",
    "condition_b",
    "severity",
    "message",
    "params",
    "category",
    "source",
    "evidence",
)
_ROW_FIELDS = (
    "id",
    "subject_id",
    "code",
    *_CATALOG_FIELDS,
    "active",
    "created_at",
    "updated_at",
)
_DATA_FIELDS = (
    "id",
    "code",
    *_CATALOG_FIELDS,
    "active",
    "created_at",
    "updated_at",
)
_B_PHASES = tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
_C_PHASES = tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_D_PHASES = tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_E_PHASES = tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_F_PHASES = tuple(HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_PRIOR_PHASES = (
    (RAW_OWNERSHIP_BACKFILL_PHASE,)
    + _B_PHASES
    + _C_PHASES
    + _D_PHASES
    + _E_PHASES
    + _F_PHASES
)


class ConflictRuleOwnershipBackfillStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"


class ConflictRuleOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed Stage-3G errors."""


class ConflictRuleOwnershipBackfillValidationError(
    ConflictRuleOwnershipBackfillError, ValueError
):
    """A caller argument or persisted scalar is invalid."""


class ConflictRuleOwnershipBackfillIdentityError(ConflictRuleOwnershipBackfillError):
    """The exact-one reviewed owner graph is unavailable."""


class ConflictRuleOwnershipBackfillDependencyError(
    ConflictRuleOwnershipBackfillError
):
    """A prerequisite checkpoint is absent, malformed, or nonterminal."""


class ConflictRuleOwnershipBackfillStateError(ConflictRuleOwnershipBackfillError):
    """Checkpoint progress, ownership, or a consumer link is inconsistent."""


class ConflictRuleOwnershipBackfillProvenanceError(
    ConflictRuleOwnershipBackfillError
):
    """A rule cannot be classified as curated or reviewed custom data."""


@dataclass(frozen=True, slots=True)
class ConflictRuleOwnershipBackfillPreflightResult:
    phase_key: str
    subject_id: uuid.UUID
    status: ConflictRuleOwnershipBackfillStatus
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
        return self.status is ConflictRuleOwnershipBackfillStatus.COMPLETED

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
class ConflictRuleOwnershipBackfillBatchResult(
    ConflictRuleOwnershipBackfillPreflightResult
):
    batch_table: str
    batch_scanned_rows: int
    batch_updated_rows: int
    batch_unchanged_rows: int

    @property
    def changed(self) -> bool:
        return self.batch_updated_rows > 0

    def to_safe_dict(self) -> dict[str, str | int]:
        result = ConflictRuleOwnershipBackfillPreflightResult.to_safe_dict(self)
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
        or not 1 <= value <= MAX_CONFLICT_RULE_OWNERSHIP_BACKFILL_BATCH_SIZE
    ):
        raise ConflictRuleOwnershipBackfillValidationError(
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
        ConflictRuleOwnershipBackfillDependencyError
        if phase in _PRIOR_PHASES
        else ConflictRuleOwnershipBackfillStateError
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
    for digest in (
        checkpoint.data_checksum_before,
        checkpoint.data_checksum_after,
        checkpoint.ownership_checksum_after,
    ):
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
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
        or checkpoint.data_checksum_before != _EMPTY_SHA256
        or checkpoint.data_checksum_after != _EMPTY_SHA256
        or checkpoint.ownership_checksum_after != _EMPTY_SHA256
    ):
        raise error("a restore-blocked ownership checkpoint is malformed")
    return checkpoint.status


async def _load_scope(session: AsyncSession, *, for_update: bool) -> _Scope:
    if for_update:
        await acquire_identity_governance_lock(session)
        rows = list(
            await session.execute(
                select(HealthSubject.id, HealthSubject.owner_user_id)
                .order_by(HealthSubject.id)
                .limit(2)
                .with_for_update()
            )
        )
    else:
        rows = list(
            await session.execute(
                select(HealthSubject.id, HealthSubject.owner_user_id)
                .order_by(HealthSubject.id)
                .limit(2)
            )
        )
    if len(rows) != 1:
        raise ConflictRuleOwnershipBackfillIdentityError(
            "conflict-rule backfill requires exactly one health subject"
        )
    subject_id, owner_user_id = rows[0]
    query = select(User.status).where(User.id == owner_user_id)
    if for_update:
        query = query.with_for_update()
    if await session.scalar(query) != UserStatus.ACTIVE.value:
        raise ConflictRuleOwnershipBackfillIdentityError(
            "the sole health subject must have an active owner"
        )
    return _Scope(subject_id)


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
        raise ConflictRuleOwnershipBackfillDependencyError(
            "Stage-3E checkpoint order is inconsistent"
        )
    if pair == ("restore_blocked", "completed") and not _exact_empty_completed(sets):
        raise ConflictRuleOwnershipBackfillDependencyError(
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
        raise ConflictRuleOwnershipBackfillDependencyError(
            "Stage-3F checkpoint order is inconsistent"
        )
    if pair == ("running", "completed") and not _exact_empty_completed(components):
        raise ConflictRuleOwnershipBackfillDependencyError(
            "completed HRT components before compounds must be exactly empty"
        )
    if (
        compounds.scan_high_watermark_id == 0
        and compounds.snapshot_rows == 0
        and not _exact_empty_completed(components)
    ):
        raise ConflictRuleOwnershipBackfillDependencyError(
            "an empty HRT compound checkpoint cannot have components"
        )


def _validate_own(checkpoint: Any | None, *, scope: _Scope) -> None:
    if checkpoint is None:
        return
    status = _validate_checkpoint(checkpoint, phase=_PHASE_KEY, subject_id=scope.subject_id)
    if status == "restore_blocked":
        raise ConflictRuleOwnershipBackfillStateError(
            "Stage-3G checkpoints cannot be restore-blocked"
        )


def _require_dependencies(
    checkpoints: Mapping[str, Any], *, scope: _Scope, own_exists: bool
) -> bool:
    if set(checkpoints) != set(_PRIOR_PHASES):
        raise ConflictRuleOwnershipBackfillDependencyError(
            "Stage-3A through Stage-3F checkpoints are incomplete"
        )
    statuses = {
        phase: _validate_checkpoint(
            checkpoints[phase], phase=phase, subject_id=scope.subject_id
        )
        for phase in _PRIOR_PHASES
    }
    if statuses[RAW_OWNERSHIP_BACKFILL_PHASE] == "completed":
        if all(statuses[phase] == "completed" for phase in _PRIOR_PHASES[1:]):
            return False
        if own_exists and _exact_empty_completed(
            checkpoints[RAW_OWNERSHIP_BACKFILL_PHASE]
        ):
            _require_restore_dependencies(
                checkpoints,
                statuses=statuses,
                allow_empty_raw_completed=True,
            )
            return True
        raise ConflictRuleOwnershipBackfillDependencyError(
            "Stage-3A through Stage-3F must be completed"
        )
    if statuses[RAW_OWNERSHIP_BACKFILL_PHASE] != "restore_blocked" or not own_exists:
        raise ConflictRuleOwnershipBackfillDependencyError(
            "restore-mode Stage-3G requires its exact reset checkpoint"
        )
    _require_restore_dependencies(
        checkpoints, statuses=statuses, allow_empty_raw_completed=False
    )
    return True


def _require_restore_dependencies(
    checkpoints: Mapping[str, Any],
    *,
    statuses: Mapping[str, str],
    allow_empty_raw_completed: bool,
) -> None:
    if statuses[RAW_OWNERSHIP_BACKFILL_PHASE] not in {
        "completed",
        "restore_blocked",
    }:
        raise ConflictRuleOwnershipBackfillDependencyError(
            "Stage-3A is not restore-terminal"
        )
    if statuses[RAW_OWNERSHIP_BACKFILL_PHASE] == "completed":
        if allow_empty_raw_completed and not _exact_empty_completed(
            checkpoints[RAW_OWNERSHIP_BACKFILL_PHASE]
        ):
            raise ConflictRuleOwnershipBackfillDependencyError(
                "Stage-3G restore reset requires completed Stage-3A to be exactly empty"
            )
        if not allow_empty_raw_completed:
            if any(statuses[phase] != "completed" for phase in _PRIOR_PHASES[1:]):
                raise ConflictRuleOwnershipBackfillDependencyError(
                    "completed Stage-3A requires Stage-3B through Stage-3F completed"
                )
            return
    if any(statuses[p] not in {"running", "completed"} for p in _B_PHASES + _C_PHASES):
        raise ConflictRuleOwnershipBackfillDependencyError(
            "Stage-3B/3C restore state is invalid"
        )
    if any(statuses[p] not in {"completed", "restore_blocked"} for p in _D_PHASES + _E_PHASES):
        raise ConflictRuleOwnershipBackfillDependencyError(
            "Stage-3D/3E restore state is invalid"
        )
    if any(
        statuses[p] == "completed" and not _exact_empty_completed(checkpoints[p])
        for p in _D_PHASES
    ):
        raise ConflictRuleOwnershipBackfillDependencyError(
            "completed Stage-3D restore checkpoints must be exactly empty"
        )
    if any(
        statuses[p] == "completed" and not _exact_empty_completed(checkpoints[p])
        for p in _E_PHASES
    ):
        raise ConflictRuleOwnershipBackfillDependencyError(
            "completed Stage-3E restore checkpoints must be exactly empty"
        )
    if any(statuses[p] not in {"running", "completed"} for p in _F_PHASES):
        raise ConflictRuleOwnershipBackfillDependencyError(
            "Stage-3F restore state is invalid"
        )
    _require_hevy_pair(checkpoints)
    _require_hrt_compound_pair(checkpoints)


def _catalog() -> dict[str, dict[str, Any]]:
    return {entry["code"]: dict(entry) for entry in load_rule_catalog()}


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConflictRuleOwnershipBackfillStateError(
                "a conflict rule contains a non-finite number"
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
    raise ConflictRuleOwnershipBackfillStateError(
        "a conflict rule contains an unsupported value"
    )


def _extend(digest: str, values: list[Any]) -> str:
    payload = json.dumps(
        _canonical(values), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(bytes.fromhex(digest) + payload).hexdigest()


def _row_values(row: Any) -> SimpleNamespace:
    mapping = row._mapping if hasattr(row, "_mapping") else row
    return SimpleNamespace(**{field: mapping[field] for field in _ROW_FIELDS})


def _classify(
    row: Any,
    *,
    scope: _Scope,
    catalog: Mapping[str, Mapping[str, Any]],
    historical: bool,
) -> tuple[bool, bool]:
    """Return ``(custom, needs_subject_adoption)``."""

    definition = catalog.get(row.code) if isinstance(row.code, str) else None
    if definition is not None:
        if row.subject_id is not None:
            raise ConflictRuleOwnershipBackfillProvenanceError(
                "a subject-owned rule cannot claim curated catalog provenance"
            )
        if any(
            getattr(row, field) != definition.get(field) for field in _CATALOG_FIELDS
        ):
            raise ConflictRuleOwnershipBackfillProvenanceError(
                "a curated conflict rule differs from the checked-in catalog"
            )
        if type(row.active) is not bool:
            raise ConflictRuleOwnershipBackfillProvenanceError(
                "a curated conflict rule has a malformed active flag"
            )
        return False, False
    if row.code is not None:
        if not isinstance(row.code, str) or not row.code.strip() or row.code != row.code.strip():
            raise ConflictRuleOwnershipBackfillProvenanceError(
                "a custom conflict rule has a malformed code"
            )
        if row.subject_id is None:
            raise ConflictRuleOwnershipBackfillProvenanceError(
                "an unrecognized global rule cannot claim catalog provenance"
            )
    try:
        RuleType(row.rule_type)
        Domain(row.domain_a)
        Domain(row.domain_b)
        Severity(row.severity)
    except (TypeError, ValueError) as exc:
        raise ConflictRuleOwnershipBackfillProvenanceError(
            "a custom conflict rule has an invalid engine enum"
        ) from exc
    if type(row.condition_a) is not dict or type(row.condition_b) is not dict:
        raise ConflictRuleOwnershipBackfillProvenanceError(
            "a custom conflict rule must contain object conditions"
        )
    if row.params is not None and type(row.params) is not dict:
        raise ConflictRuleOwnershipBackfillProvenanceError(
            "a custom conflict rule has malformed params"
        )
    if not isinstance(row.message, str) or not row.message.strip():
        raise ConflictRuleOwnershipBackfillProvenanceError(
            "a custom conflict rule has a blank message"
        )
    if type(row.active) is not bool:
        raise ConflictRuleOwnershipBackfillProvenanceError(
            "a custom conflict rule has a malformed active flag"
        )
    if row.subject_id == scope.subject_id:
        return True, False
    if row.subject_id is not None:
        raise ConflictRuleOwnershipBackfillStateError(
            "a custom conflict rule belongs to another subject"
        )
    if historical and row.code is None:
        return True, True
    raise ConflictRuleOwnershipBackfillProvenanceError(
        "an unowned custom conflict rule is not eligible for adoption"
    )


def _data_envelope(row: Any) -> list[Any]:
    return ["conflict_rules", *[getattr(row, field) for field in _DATA_FIELDS]]


def _ownership_envelope(row: Any, *, custom: bool) -> list[Any] | None:
    return ["conflict_rules", row.id, row.subject_id] if custom else None


async def _validate_consumers(
    session: AsyncSession,
    *,
    scope: _Scope,
    catalog: Mapping[str, Mapping[str, Any]],
    for_update: bool,
) -> None:
    query = select(SubjectSetting.subject_id, SubjectSetting.value).where(
        SubjectSetting.key == SETTING_KEY
    )
    if for_update:
        query = query.with_for_update()
    rows = list(await session.execute(query.limit(2)))
    if len(rows) > 1:
        raise ConflictRuleOwnershipBackfillStateError(
            "conflict-rule activation has duplicate subject consumers"
        )
    for subject_id, value in rows:
        if subject_id != scope.subject_id:
            raise ConflictRuleOwnershipBackfillStateError(
                "conflict-rule activation belongs to another subject"
            )
        if type(value) is not dict or set(value) != {"v", "disabled_codes"}:
            raise ConflictRuleOwnershipBackfillStateError(
                "conflict-rule activation has a malformed document"
            )
        codes = value["disabled_codes"]
        if (
            type(value["v"]) is not int
            or value["v"] != SETTING_VERSION
            or type(codes) is not list
            or any(type(code) is not str for code in codes)
            or codes != sorted(codes)
            or len(codes) != len(set(codes))
            or any(code not in catalog for code in codes)
        ):
            raise ConflictRuleOwnershipBackfillStateError(
                "conflict-rule activation has invalid catalog references"
            )
    cursor = 0
    while True:
        alert_query = (
            select(
                SystemAlert.id,
                SystemAlert.subject_id,
                SystemAlert.integration_connection_id,
                SystemAlert.domain,
                SystemAlert.alert_key,
            )
            .where(
                SystemAlert.id > cursor,
                SystemAlert.alert_key.like("conflict:%"),
            )
            .order_by(SystemAlert.id)
            .limit(_PAGE_SIZE)
        )
        projected_alerts = list(await session.execute(alert_query))
        if not projected_alerts:
            break
        rule_ids: list[int] = []
        for alert in projected_alerts:
            suffix = alert.alert_key.removeprefix("conflict:")
            if not suffix.isascii() or not suffix.isdigit() or suffix.startswith("0"):
                raise ConflictRuleOwnershipBackfillStateError(
                    "a conflict alert has a malformed rule reference"
                )
            rule_ids.append(int(suffix))
        rule_query = (
            select(
                ConflictRule.id,
                ConflictRule.subject_id,
                ConflictRule.code,
                ConflictRule.domain_a,
                ConflictRule.domain_b,
            )
            .where(ConflictRule.id.in_(sorted(set(rule_ids))))
            .order_by(ConflictRule.id)
        )
        if for_update:
            rule_query = rule_query.with_for_update()
        rules = {
            rule.id: rule for rule in await session.execute(rule_query)
        }
        if for_update:
            await _after_conflict_alert_rules_locked_for_test()
            locked_alerts = list(
                await session.execute(
                    select(
                        SystemAlert.id,
                        SystemAlert.subject_id,
                        SystemAlert.integration_connection_id,
                        SystemAlert.domain,
                        SystemAlert.alert_key,
                    )
                    .where(
                        SystemAlert.id.in_([alert.id for alert in projected_alerts])
                    )
                    .order_by(SystemAlert.id)
                    .with_for_update()
                )
            )
            if locked_alerts != projected_alerts:
                raise ConflictRuleOwnershipBackfillStateError(
                    "a projected conflict alert changed before it was locked"
                )
            alerts = locked_alerts
        else:
            alerts = projected_alerts
        for alert, rule_id in zip(alerts, rule_ids, strict=True):
            rule = rules.get(rule_id)
            if rule is None:
                raise ConflictRuleOwnershipBackfillStateError(
                    "a conflict alert references a missing rule"
                )
            if (
                alert.subject_id not in {None, scope.subject_id}
                or alert.integration_connection_id is not None
                or rule.subject_id not in {None, scope.subject_id}
                or alert.domain not in {rule.domain_a, rule.domain_b}
                or (
                    rule.subject_id is None
                    and rule.code is not None
                    and rule.code not in catalog
                )
            ):
                raise ConflictRuleOwnershipBackfillStateError(
                    "a conflict alert has incompatible rule scope or domain"
                )
            cursor = alert.id


async def _after_conflict_alert_rules_locked_for_test() -> None:
    """Tests replace this rule-before-alert race seam; production is a no-op."""


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


async def _after_rule_projection_for_test() -> None:
    """Tests replace this bounded race seam; production is a no-op."""


def _row_select():
    return select(*(_TABLE.c[field] for field in _ROW_FIELDS))


async def _load_row(session: AsyncSession, row_id: int, *, for_update: bool) -> Any | None:
    query = _row_select().where(_TABLE.c.id == row_id)
    if for_update:
        query = query.with_for_update()
    row = (await session.execute(query)).first()
    return _row_values(row) if row is not None else None


async def _scan(
    session: AsyncSession,
    *,
    low: int,
    high: int | None,
    scope: _Scope,
    catalog: Mapping[str, Mapping[str, Any]],
    historical: bool,
    for_update: bool,
    data_digest: bool,
) -> tuple[int, str, str]:
    cursor = low
    count = 0
    data = _EMPTY_SHA256
    ownership = _EMPTY_SHA256
    while True:
        query = _row_select().where(_TABLE.c.id > cursor).order_by(_TABLE.c.id).limit(_PAGE_SIZE)
        if high is not None:
            query = query.where(_TABLE.c.id <= high)
        if for_update:
            query = query.with_for_update()
        rows = list(await session.execute(query))
        if not rows:
            break
        for raw in rows:
            row = _row_values(raw)
            custom, adopt = _classify(
                row, scope=scope, catalog=catalog, historical=historical
            )
            if adopt:
                raise ConflictRuleOwnershipBackfillStateError(
                    "a completed conflict-rule snapshot contains unowned custom data"
                )
            if data_digest:
                data = _extend(data, _data_envelope(row))
            envelope = _ownership_envelope(row, custom=custom)
            if envelope is not None:
                ownership = _extend(ownership, envelope)
            cursor = row.id
            count += 1
    return count, data, ownership


async def _validate_current_catalog(
    session: AsyncSession,
    *,
    scope: _Scope,
    catalog: Mapping[str, Mapping[str, Any]],
    high: int | None,
    completed: bool,
    for_update: bool,
) -> None:
    seen: set[str] = set()
    cursor = 0
    while True:
        query = _row_select().where(_TABLE.c.id > cursor).order_by(_TABLE.c.id).limit(_PAGE_SIZE)
        if for_update:
            query = query.with_for_update()
        rows = list(await session.execute(query))
        if not rows:
            break
        for raw in rows:
            row = _row_values(raw)
            historical = high is None or row.id <= high
            custom, adopt = _classify(
                row, scope=scope, catalog=catalog, historical=historical
            )
            if adopt and completed:
                raise ConflictRuleOwnershipBackfillStateError(
                    "a completed conflict-rule snapshot contains unowned custom data"
                )
            if not custom:
                if row.code in seen:
                    raise ConflictRuleOwnershipBackfillProvenanceError(
                        "the curated conflict-rule catalog contains duplicate codes"
                    )
                seen.add(row.code)
            cursor = row.id
    missing = set(catalog) - seen
    if missing:
        raise ConflictRuleOwnershipBackfillProvenanceError(
            "the persisted conflict-rule catalog is incomplete"
        )


async def _status_result(
    session: AsyncSession,
    *,
    scope: _Scope,
    checkpoint: Any | None,
    catalog: Mapping[str, Mapping[str, Any]],
    validate: bool,
    for_update: bool,
    require_data: bool = False,
) -> ConflictRuleOwnershipBackfillPreflightResult:
    if checkpoint is None:
        high, snapshot = await _bounds(session)
        remaining = snapshot
        rows_above = 0
        status = ConflictRuleOwnershipBackfillStatus.NOT_STARTED
        scanned = updated = unchanged = 0
        before = after = ownership = _EMPTY_SHA256
        completed_tables = 0
        completed = False
    else:
        high, snapshot = checkpoint.scan_high_watermark_id, checkpoint.snapshot_rows
        remaining = await _remaining(
            session, high=high, cursor=checkpoint.last_scanned_id
        )
        rows_above = int(
            await session.scalar(
                select(func.count()).select_from(_TABLE).where(_TABLE.c.id > high)
            )
            or 0
        )
        status = ConflictRuleOwnershipBackfillStatus(checkpoint.status)
        scanned, updated, unchanged = (
            checkpoint.scanned_rows,
            checkpoint.updated_rows,
            checkpoint.unchanged_rows,
        )
        before, after, ownership = (
            checkpoint.data_checksum_before,
            checkpoint.data_checksum_after,
            checkpoint.ownership_checksum_after,
        )
        completed = status is ConflictRuleOwnershipBackfillStatus.COMPLETED
        completed_tables = int(completed)
    if validate:
        await _validate_current_catalog(
            session,
            scope=scope,
            catalog=catalog,
            high=high if checkpoint is not None else None,
            completed=completed,
            for_update=for_update,
        )
        await _validate_consumers(
            session, scope=scope, catalog=catalog, for_update=for_update
        )
        if checkpoint is not None and not completed:
            current_snapshot = int(
                await session.scalar(
                    select(func.count())
                    .select_from(_TABLE)
                    .where(_TABLE.c.id <= high)
                )
                or 0
            )
            if current_snapshot != snapshot:
                raise ConflictRuleOwnershipBackfillStateError(
                    "the conflict-rule snapshot cardinality changed"
                )
        if checkpoint is not None and completed:
            _, data, frozen_ownership = await _scan(
                session,
                low=0,
                high=high,
                scope=scope,
                catalog=catalog,
                historical=True,
                for_update=for_update,
                data_digest=require_data,
            )
            if frozen_ownership != ownership:
                raise ConflictRuleOwnershipBackfillStateError(
                    "completed custom ownership evidence changed"
                )
            if require_data and (data != before or data != after):
                raise ConflictRuleOwnershipBackfillStateError(
                    "conflict-rule data changed during finalization"
                )
    return ConflictRuleOwnershipBackfillPreflightResult(
        phase_key=CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE,
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
    result: ConflictRuleOwnershipBackfillPreflightResult,
    *,
    scanned: int,
    updated: int,
    unchanged: int,
) -> ConflictRuleOwnershipBackfillBatchResult:
    return ConflictRuleOwnershipBackfillBatchResult(
        **{
            field: getattr(result, field)
            for field in ConflictRuleOwnershipBackfillPreflightResult.__dataclass_fields__
        },
        batch_table="conflict_rules",
        batch_scanned_rows=scanned,
        batch_updated_rows=updated,
        batch_unchanged_rows=unchanged,
    )


async def preflight_conflict_rule_ownership_backfill(
    session: AsyncSession,
) -> ConflictRuleOwnershipBackfillPreflightResult:
    """Validate the fixed Stage-3G graph without mutation."""

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=False)
        own = await _load_checkpoints(session, (_PHASE_KEY,), for_update=False)
        checkpoint = own.get(_PHASE_KEY)
        _validate_own(checkpoint, scope=scope)
        dependencies = await _load_checkpoints(session, _PRIOR_PHASES, for_update=False)
        _require_dependencies(
            dependencies, scope=scope, own_exists=checkpoint is not None
        )
        return await _status_result(
            session,
            scope=scope,
            checkpoint=checkpoint,
            catalog=_catalog(),
            validate=True,
            for_update=False,
        )


def _validate_restore_bounds(snapshot_bounds: Any) -> tuple[int, int]:
    if (
        not isinstance(snapshot_bounds, Mapping)
        or set(snapshot_bounds) != {"conflict_rules"}
    ):
        raise ConflictRuleOwnershipBackfillValidationError(
            "snapshot_bounds must contain the exact conflict-rule table catalog"
        )
    pair = snapshot_bounds["conflict_rules"]
    if not isinstance(pair, tuple) or len(pair) != 2:
        raise ConflictRuleOwnershipBackfillValidationError(
            "the conflict-rule snapshot bound must be an exact pair"
        )
    high, count = pair
    if (
        not _valid_counter(high)
        or not _valid_counter(count)
        or count > high
        or (high == 0) != (count == 0)
    ):
        raise ConflictRuleOwnershipBackfillValidationError(
            "the conflict-rule snapshot bound is an invalid ID/count pair"
        )
    return high, count


async def _lock_all_ids_bounded(session: AsyncSession) -> None:
    cursor = 0
    while True:
        ids = list(
            await session.scalars(
                select(_TABLE.c.id)
                .where(_TABLE.c.id > cursor)
                .order_by(_TABLE.c.id)
                .limit(_PAGE_SIZE)
                .with_for_update()
            )
        )
        if not ids:
            return
        cursor = ids[-1]


async def reset_conflict_rule_backfill_for_portability_v1_restore(
    session: AsyncSession,
    *,
    snapshot_bounds: Mapping[str, tuple[int, int]],
) -> None:
    """Reset Stage-3G before the caller atomically replaces portable data."""

    high, count = _validate_restore_bounds(snapshot_bounds)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependencies = await _load_checkpoints(session, _PRIOR_PHASES, for_update=True)
        if set(dependencies) != set(_PRIOR_PHASES):
            raise ConflictRuleOwnershipBackfillDependencyError(
                "Stage-3A through Stage-3F checkpoints are incomplete"
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
            allow_empty_raw_completed=True,
        )
        own = await _load_checkpoints(session, (_PHASE_KEY,), for_update=True)
        checkpoint = own.get(_PHASE_KEY)
        _validate_own(checkpoint, scope=scope)
        await _lock_all_ids_bounded(session)
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


def _set_cached_subject(session: AsyncSession, row_id: int, subject_id: uuid.UUID) -> None:
    cached = session.identity_map.get((ConflictRule, (row_id,), None))
    if cached is not None:
        attributes.set_committed_value(cached, "subject_id", subject_id)


async def run_conflict_rule_ownership_backfill_batch(
    session: AsyncSession,
    *,
    batch_size: int,
) -> ConflictRuleOwnershipBackfillBatchResult:
    """Advance the fixed conflict-rule table by at most one PK batch."""

    size = _validate_batch_size(batch_size)
    catalog = _catalog()
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        own = await _load_checkpoints(session, (_PHASE_KEY,), for_update=True)
        checkpoint = own.get(_PHASE_KEY)
        _validate_own(checkpoint, scope=scope)
        dependencies = await _load_checkpoints(session, _PRIOR_PHASES, for_update=True)
        _require_dependencies(
            dependencies, scope=scope, own_exists=checkpoint is not None
        )
        if checkpoint is not None and checkpoint.status == "completed":
            result = await _status_result(
                session,
                scope=scope,
                checkpoint=checkpoint,
                catalog=catalog,
                validate=True,
                for_update=True,
            )
            return _batch_result(result, scanned=0, updated=0, unchanged=0)
        await _validate_current_catalog(
            session,
            scope=scope,
            catalog=catalog,
            high=checkpoint.scan_high_watermark_id if checkpoint is not None else None,
            completed=False,
            for_update=False,
        )
        await _validate_consumers(
            session, scope=scope, catalog=catalog, for_update=True
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
                    catalog=catalog,
                    validate=True,
                    for_update=True,
                )
                return _batch_result(result, scanned=0, updated=0, unchanged=0)
        # Above-HWM rows are live and can never use the historical bridge.
        await _scan(
            session,
            low=checkpoint.scan_high_watermark_id,
            high=None,
            scope=scope,
            catalog=catalog,
            historical=False,
            for_update=True,
            data_digest=False,
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
        projected = {
            row.id: row
            for row in (
                _row_values(raw)
                for raw in await session.execute(
                    _row_select().where(_TABLE.c.id.in_(ids)).order_by(_TABLE.c.id)
                )
            )
        }
        await _after_rule_projection_for_test()

    before = checkpoint.data_checksum_before
    after = checkpoint.data_checksum_after
    ownership = checkpoint.ownership_checksum_after
    updated_count = 0
    unchanged_count = 0
    for row_id in ids:
        row = await _load_row(session, row_id, for_update=True)
        if row is None:
            raise ConflictRuleOwnershipBackfillStateError(
                "a projected conflict rule disappeared"
            )
        prior = projected.get(row_id)
        if prior is None or any(
            getattr(row, field) != getattr(prior, field) for field in _ROW_FIELDS
        ):
            raise ConflictRuleOwnershipBackfillStateError(
                "a projected conflict rule changed before it was locked"
            )
        custom, adopt = _classify(
            row, scope=scope, catalog=catalog, historical=True
        )
        before = _extend(before, _data_envelope(row))
        if adopt:
            await session.execute(
                update(_TABLE)
                .where(_TABLE.c.id == row_id, _TABLE.c.subject_id.is_(None))
                .values(subject_id=scope.subject_id, updated_at=row.updated_at)
            )
            _set_cached_subject(session, row_id, scope.subject_id)
            updated_count += 1
        else:
            unchanged_count += 1
        current = await _load_row(session, row_id, for_update=True)
        if current is None:
            raise ConflictRuleOwnershipBackfillStateError(
                "a conflict rule disappeared during adoption"
            )
        custom, needs_adoption = _classify(
            current, scope=scope, catalog=catalog, historical=True
        )
        if needs_adoption:
            raise ConflictRuleOwnershipBackfillStateError(
                "a custom conflict rule remained unowned"
            )
        after = _extend(after, _data_envelope(current))
        envelope = _ownership_envelope(current, custom=custom)
        if envelope is not None:
            ownership = _extend(ownership, envelope)
    if before != after:
        raise ConflictRuleOwnershipBackfillStateError(
            "conflict-rule data changed while ownership was backfilled"
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
        current_count, data, current_ownership = await _scan(
            session,
            low=0,
            high=checkpoint.scan_high_watermark_id,
            scope=scope,
            catalog=catalog,
            historical=True,
            for_update=True,
            data_digest=True,
        )
        if (
            current_count != checkpoint.snapshot_rows
            or data != checkpoint.data_checksum_before
            or data != checkpoint.data_checksum_after
            or current_ownership != checkpoint.ownership_checksum_after
        ):
            raise ConflictRuleOwnershipBackfillStateError(
                "the conflict-rule snapshot changed during finalization"
            )
        await _validate_current_catalog(
            session,
            scope=scope,
            catalog=catalog,
            high=checkpoint.scan_high_watermark_id,
            completed=True,
            for_update=True,
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
        catalog=catalog,
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
    "CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES",
    "CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE",
    "CONFLICT_RULE_OWNERSHIP_BACKFILL_TABLES",
    "DEFAULT_CONFLICT_RULE_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "MAX_CONFLICT_RULE_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "ConflictRuleOwnershipBackfillBatchResult",
    "ConflictRuleOwnershipBackfillDependencyError",
    "ConflictRuleOwnershipBackfillError",
    "ConflictRuleOwnershipBackfillIdentityError",
    "ConflictRuleOwnershipBackfillPreflightResult",
    "ConflictRuleOwnershipBackfillProvenanceError",
    "ConflictRuleOwnershipBackfillStateError",
    "ConflictRuleOwnershipBackfillStatus",
    "ConflictRuleOwnershipBackfillValidationError",
    "preflight_conflict_rule_ownership_backfill",
    "reset_conflict_rule_backfill_for_portability_v1_restore",
    "run_conflict_rule_ownership_backfill_batch",
]
