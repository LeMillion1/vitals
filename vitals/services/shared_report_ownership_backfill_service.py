"""Bounded Stage-3K ownership backfill for retained shared-report artifacts.

Historical reports prove only the sole reviewed subject.  Creator and revoker
actors, public capability data, and the frozen report snapshot remain exactly as
persisted.  Reusable functions flush but never commit.
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

from vitals.enums import UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.share import SharedReport
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
from vitals.services.signal_ownership_backfill_service import (
    SIGNAL_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.utils.timeutils import now_utc


SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE = (
    "stage3.retained_artifact.shared_reports.v1"
)
SHARED_REPORT_OWNERSHIP_BACKFILL_TABLES = ("shared_reports",)
SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES: Mapping[str, str] = (
    MappingProxyType(
        {
            "shared_reports": (
                f"{SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE}.shared_reports"
            )
        }
    )
)
DEFAULT_SHARED_REPORT_OWNERSHIP_BACKFILL_BATCH_SIZE = 250
MAX_SHARED_REPORT_OWNERSHIP_BACKFILL_BATCH_SIZE = 1000

_TABLE: Table = SharedReport.__table__
_PHASE_KEY = SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["shared_reports"]
_PAGE_SIZE = 1000
_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ROW_FIELDS = (
    "id",
    "subject_id",
    "created_by_user_id",
    "revoked_by_user_id",
    "token",
    "password_hash",
    "title",
    "preset",
    "domains",
    "period_start",
    "period_end",
    "labs_flagged_only",
    "note",
    "snapshot",
    "expires_at",
    "revoked_at",
    "opened_count",
    "last_opened_at",
    "created_at",
    "updated_at",
)
_DATA_FIELDS = (
    "id",
    "token",
    "password_hash",
    "title",
    "preset",
    "domains",
    "period_start",
    "period_end",
    "labs_flagged_only",
    "note",
    "snapshot",
    "expires_at",
    "revoked_at",
    "opened_count",
    "last_opened_at",
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
_I_PHASES = tuple(DAY_CONTEXT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_J_PHASES = tuple(SIGNAL_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
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
)


class SharedReportOwnershipBackfillStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"


class SharedReportOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed Stage-3K failures."""


class SharedReportOwnershipBackfillValidationError(
    SharedReportOwnershipBackfillError, ValueError
):
    """A caller argument or persisted scalar is invalid."""


class SharedReportOwnershipBackfillIdentityError(
    SharedReportOwnershipBackfillError
):
    """The exact-one reviewed owner graph is unavailable."""


class SharedReportOwnershipBackfillDependencyError(
    SharedReportOwnershipBackfillError
):
    """A prerequisite checkpoint is absent, malformed, or in the wrong mode."""


class SharedReportOwnershipBackfillStateError(SharedReportOwnershipBackfillError):
    """Checkpoint progress or a report ownership root is inconsistent."""


class SharedReportOwnershipBackfillProvenanceError(
    SharedReportOwnershipBackfillError
):
    """A shared report has unsupported persisted artifact provenance."""


@dataclass(frozen=True, slots=True)
class SharedReportHistoricalBridgeState:
    processed_high_watermark_id: int
    snapshot_high_watermark_id: int
    completed: bool


@dataclass(frozen=True, slots=True)
class SharedReportOwnershipBackfillPreflightResult:
    phase_key: str
    subject_id: uuid.UUID
    status: SharedReportOwnershipBackfillStatus
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
        return self.status is SharedReportOwnershipBackfillStatus.COMPLETED

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
class SharedReportOwnershipBackfillBatchResult(
    SharedReportOwnershipBackfillPreflightResult
):
    batch_table: str
    batch_scanned_rows: int
    batch_updated_rows: int
    batch_unchanged_rows: int

    @property
    def changed(self) -> bool:
        return self.batch_updated_rows > 0

    def to_safe_dict(self) -> dict[str, str | int]:
        result = SharedReportOwnershipBackfillPreflightResult.to_safe_dict(self)
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


def _validate_batch_size(value: Any) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_SHARED_REPORT_OWNERSHIP_BACKFILL_BATCH_SIZE
    ):
        raise SharedReportOwnershipBackfillValidationError(
            "batch_size must be an integer between 1 and "
            f"{MAX_SHARED_REPORT_OWNERSHIP_BACKFILL_BATCH_SIZE}"
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
    session: AsyncSession,
    phases: tuple[str, ...],
    *,
    for_update: bool,
) -> dict[str, Any]:
    ordered = tuple(sorted(phases))
    if for_update:
        rows = list(
            await session.scalars(
                select(OwnershipBackfillCheckpoint)
                .where(OwnershipBackfillCheckpoint.phase_key.in_(ordered))
                .order_by(OwnershipBackfillCheckpoint.phase_key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        return {row.phase_key: row for row in rows}
    rows = list(
        await session.execute(
            _checkpoint_select()
            .where(OwnershipBackfillCheckpoint.phase_key.in_(ordered))
            .order_by(OwnershipBackfillCheckpoint.phase_key)
        )
    )
    return {row.phase_key: _CheckpointProjection(*row) for row in rows}


def _validate_checkpoint(
    checkpoint: Any,
    *,
    phase: str,
    subject_id: uuid.UUID,
) -> str:
    error = (
        SharedReportOwnershipBackfillDependencyError
        if phase in _PRIOR_PHASES
        else SharedReportOwnershipBackfillStateError
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
    if any(
        not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
        for value in digests
    ):
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
        checkpoint.scan_high_watermark_id == 0
        or checkpoint.snapshot_rows == 0
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


def _validate_own(checkpoint: Any | None, *, scope: _Scope) -> str | None:
    if checkpoint is None:
        return None
    status = _validate_checkpoint(
        checkpoint,
        phase=_PHASE_KEY,
        subject_id=scope.subject_id,
    )
    if status == "restore_blocked":
        raise SharedReportOwnershipBackfillStateError(
            "Stage-3K checkpoints cannot be restore-blocked"
        )
    return status


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
        raise SharedReportOwnershipBackfillIdentityError(
            "shared-report backfill requires exactly one health subject"
        )
    subject_id, owner_user_id = rows[0]
    owner_query = select(User.status).where(User.id == owner_user_id)
    if for_update:
        owner_query = owner_query.with_for_update()
    if await session.scalar(owner_query) != UserStatus.ACTIVE.value:
        raise SharedReportOwnershipBackfillIdentityError(
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
    def require_resettable(phases: tuple[str, ...], label: str) -> None:
        for phase in phases:
            checkpoint = checkpoints[phase]
            if checkpoint.status == "completed" or _exact_nonempty_running(
                checkpoint
            ):
                continue
            raise SharedReportOwnershipBackfillDependencyError(
                f"{label} restore checkpoint state is invalid"
            )

    def require_blocked(phases: tuple[str, ...], label: str) -> None:
        for phase in phases:
            checkpoint = checkpoints[phase]
            if checkpoint.status in {"completed", "restore_blocked"}:
                continue
            raise SharedReportOwnershipBackfillDependencyError(
                f"{label} restore checkpoint state is invalid"
            )

    require_blocked((RAW_OWNERSHIP_BACKFILL_PHASE,), "Stage-3A")
    require_resettable(_B_PHASES + _C_PHASES, "Stage-3B/3C")
    require_blocked(_D_PHASES + _E_PHASES, "Stage-3D/3E")
    require_resettable(_F_PHASES + _G_PHASES, "Stage-3F/3G")
    require_blocked(_H_PHASES, "Stage-3H")
    require_resettable(_I_PHASES + _J_PHASES, "Stage-3I/3J")

    exercises = checkpoints[
        HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_exercises"]
    ]
    sets = checkpoints[
        HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_sets"]
    ]
    valid_hevy_pair = (
        (exercises.status, sets.status)
        in {
            ("restore_blocked", "restore_blocked"),
            ("completed", "restore_blocked"),
            ("completed", "completed"),
        }
        or (
            exercises.status == "restore_blocked"
            and _exact_empty_completed(sets)
        )
    )
    if not valid_hevy_pair or (
        _exact_empty_completed(exercises)
        and not _exact_empty_completed(sets)
    ):
        raise SharedReportOwnershipBackfillDependencyError(
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
    valid_hrt_pair = (
        (compounds.status, components.status)
        in {
            ("running", "running"),
            ("completed", "running"),
            ("completed", "completed"),
        }
        or (
            compounds.status == "running"
            and _exact_empty_completed(components)
        )
    )
    if not valid_hrt_pair or (
        _exact_empty_completed(compounds)
        and not _exact_empty_completed(components)
    ):
        raise SharedReportOwnershipBackfillDependencyError(
            "Stage-3F restore checkpoint order is inconsistent"
        )


def _require_dependencies(
    checkpoints: Mapping[str, Any],
    *,
    scope: _Scope,
    own_exists: bool,
) -> bool:
    if set(checkpoints) != set(_PRIOR_PHASES):
        raise SharedReportOwnershipBackfillDependencyError(
            "Stage-3A through Stage-3J checkpoints are incomplete"
        )
    statuses = {
        phase: _validate_checkpoint(
            checkpoints[phase], phase=phase, subject_id=scope.subject_id
        )
        for phase in _PRIOR_PHASES
    }
    if all(status == "completed" for status in statuses.values()):
        _require_restore_dependencies(checkpoints)
        return False
    if not own_exists:
        raise SharedReportOwnershipBackfillDependencyError(
            "restore-mode Stage-3K requires its exact retained-artifact checkpoint"
        )
    _require_restore_dependencies(checkpoints)
    return True


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SharedReportOwnershipBackfillProvenanceError(
                "shared report contains a non-finite JSON number"
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
            raise SharedReportOwnershipBackfillProvenanceError(
                "shared report JSON object keys must be strings"
            )
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise SharedReportOwnershipBackfillProvenanceError(
        "shared report contains an unsupported persisted value"
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


def _row_values(row: Any) -> SimpleNamespace:
    mapping = row._mapping if hasattr(row, "_mapping") else row
    return SimpleNamespace(**{field: mapping[field] for field in _ROW_FIELDS})


def _same_row(left: Any, right: Any) -> bool:
    return all(getattr(left, field) == getattr(right, field) for field in _ROW_FIELDS)


def _data_envelope(row: Any) -> list[Any]:
    return ["shared_reports", *[getattr(row, field) for field in _DATA_FIELDS]]


def _ownership_envelope(row: Any) -> list[Any]:
    return [
        "shared_reports",
        row.id,
        row.subject_id,
        row.created_by_user_id,
        row.revoked_by_user_id,
    ]


def _row_policy(row_id: int, checkpoint: Any | None) -> tuple[bool, bool]:
    """Return ``(historical, allow_unowned)`` for one current row."""

    if checkpoint is None:
        return True, True
    if checkpoint.status == "running":
        if row_id <= checkpoint.last_scanned_id:
            return True, False
        if row_id <= checkpoint.scan_high_watermark_id:
            return True, True
        return False, False
    if checkpoint.status == "completed":
        if row_id <= checkpoint.scan_high_watermark_id:
            return True, False
        return False, False
    raise SharedReportOwnershipBackfillStateError(
        "Stage-3K checkpoint has an unsupported state"
    )


def _validate_row(
    row: Any,
    *,
    scope: _Scope,
    historical: bool,
    allow_unowned: bool,
) -> bool:
    if row.revoked_by_user_id is not None and row.revoked_at is None:
        raise SharedReportOwnershipBackfillProvenanceError(
            "shared report has a revocation actor without a timestamp"
        )
    if row.subject_id is None:
        if row.created_by_user_id is not None or row.revoked_by_user_id is not None:
            raise SharedReportOwnershipBackfillStateError(
                "shared report has partial legacy ownership roots"
            )
        if historical and allow_unowned:
            return True
        raise SharedReportOwnershipBackfillStateError(
            "a migrated shared report remained unowned"
        )
    if row.subject_id != scope.subject_id:
        raise SharedReportOwnershipBackfillStateError(
            "shared report belongs to another subject"
        )
    for actor_id in (row.created_by_user_id, row.revoked_by_user_id):
        if actor_id is not None and actor_id != scope.owner_user_id:
            raise SharedReportOwnershipBackfillStateError(
                "shared report actor does not own its subject"
            )
    if not historical and row.created_by_user_id != scope.owner_user_id:
        raise SharedReportOwnershipBackfillProvenanceError(
            "a live shared report requires its owner creator"
        )
    if not historical and (row.revoked_at is None) != (
        row.revoked_by_user_id is None
    ):
        raise SharedReportOwnershipBackfillProvenanceError(
            "a live shared report has inconsistent revocation provenance"
        )
    return False


async def _bounds(session: AsyncSession) -> tuple[int, int]:
    high, count = (
        await session.execute(
            select(func.coalesce(func.max(_TABLE.c.id), 0), func.count())
        )
    ).one()
    high, count = int(high), int(count)
    if not _valid_counter(high) or not _valid_counter(count) or count > high:
        raise SharedReportOwnershipBackfillValidationError(
            "shared-report snapshot bounds are invalid"
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


def _set_cached_subject(
    session: AsyncSession,
    row_id: int,
    subject_id: uuid.UUID,
) -> None:
    cached = session.identity_map.get((SharedReport, (row_id,), None))
    if cached is not None:
        attributes.set_committed_value(cached, "subject_id", subject_id)


async def _project_and_lock_id(
    session: AsyncSession,
    row_id: int,
    *,
    invoke_race_hook: bool,
) -> Any:
    """Project and lock one row without retaining another report snapshot."""

    projected_raw = await session.execute(
        _row_select().where(_TABLE.c.id == row_id)
    )
    projected_result = projected_raw.one_or_none()
    if projected_result is None:
        raise SharedReportOwnershipBackfillStateError(
            "a projected shared report disappeared"
        )
    projected = _row_values(projected_result)
    if invoke_race_hook:
        await _after_shared_report_projection_for_test()
    locked_raw = await session.execute(
        _row_select().where(_TABLE.c.id == row_id).with_for_update()
    )
    locked_result = locked_raw.one_or_none()
    if locked_result is None:
        raise SharedReportOwnershipBackfillStateError(
            "a projected shared report disappeared before it was locked"
        )
    locked = _row_values(locked_result)
    if not _same_row(projected, locked):
        raise SharedReportOwnershipBackfillStateError(
            "a projected shared report changed before it was locked"
        )
    return locked


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
        for row_id in ids:
            projected_raw = await session.execute(
                _row_select().where(_TABLE.c.id == row_id)
            )
            projected_result = projected_raw.one_or_none()
            if projected_result is None:
                raise SharedReportOwnershipBackfillStateError(
                    "a shared-report page changed during validation"
                )
            projected = _row_values(projected_result)
            if for_update:
                locked_raw = await session.execute(
                    _row_select().where(_TABLE.c.id == row_id).with_for_update()
                )
                locked_result = locked_raw.one_or_none()
                if locked_result is None:
                    raise SharedReportOwnershipBackfillStateError(
                        "a shared report disappeared before it was locked"
                    )
                row = _row_values(locked_result)
                if not _same_row(projected, row):
                    raise SharedReportOwnershipBackfillStateError(
                        "a shared report changed before it was locked"
                    )
            else:
                row = projected
            historical, allow_unowned = _row_policy(row.id, checkpoint)
            needs_adoption = _validate_row(
                row,
                scope=scope,
                historical=historical,
                allow_unowned=allow_unowned,
            )
            if needs_adoption and checkpoint is not None and (
                checkpoint.status == "completed"
                or row.id <= checkpoint.last_scanned_id
            ):
                raise SharedReportOwnershipBackfillStateError(
                    "a processed shared report remained unowned"
                )
            if digest:
                data = _extend(data, _data_envelope(row))
                if not needs_adoption:
                    ownership = _extend(ownership, _ownership_envelope(row))
            count += 1
            cursor = row.id
    return count, data, ownership


async def _status_result(
    session: AsyncSession,
    *,
    scope: _Scope,
    checkpoint: Any | None,
    validate: bool,
    for_update: bool,
) -> SharedReportOwnershipBackfillPreflightResult:
    if checkpoint is None:
        high, snapshot = await _bounds(session)
        status = SharedReportOwnershipBackfillStatus.NOT_STARTED
        scanned = updated = unchanged = rows_above = 0
        remaining = snapshot
        before = after = ownership = _EMPTY_SHA256
        completed = False
    else:
        high = checkpoint.scan_high_watermark_id
        snapshot = checkpoint.snapshot_rows
        status = SharedReportOwnershipBackfillStatus(checkpoint.status)
        scanned, updated, unchanged = (
            checkpoint.scanned_rows,
            checkpoint.updated_rows,
            checkpoint.unchanged_rows,
        )
        remaining = (
            0
            if status is SharedReportOwnershipBackfillStatus.COMPLETED
            else await _remaining(
                session,
                high=high,
                cursor=checkpoint.last_scanned_id,
            )
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
        completed = status is SharedReportOwnershipBackfillStatus.COMPLETED
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
                raise SharedReportOwnershipBackfillStateError(
                    "the running shared-report snapshot cardinality changed"
                )
    return SharedReportOwnershipBackfillPreflightResult(
        phase_key=SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE,
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
    result: SharedReportOwnershipBackfillPreflightResult,
    *,
    scanned: int,
    updated: int,
    unchanged: int,
) -> SharedReportOwnershipBackfillBatchResult:
    return SharedReportOwnershipBackfillBatchResult(
        **{
            field: getattr(result, field)
            for field in SharedReportOwnershipBackfillPreflightResult.__dataclass_fields__
        },
        batch_table="shared_reports",
        batch_scanned_rows=scanned,
        batch_updated_rows=updated,
        batch_unchanged_rows=unchanged,
    )


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
        completed_at=now_utc() if empty else None,
    )
    session.add(checkpoint)
    await session.flush()
    return checkpoint


async def preflight_shared_report_ownership_backfill(
    session: AsyncSession,
) -> SharedReportOwnershipBackfillPreflightResult:
    """Validate the fixed Stage-3K retained-artifact graph without mutation."""

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=False)
        own = await _load_checkpoints(session, (_PHASE_KEY,), for_update=False)
        checkpoint = own.get(_PHASE_KEY)
        _validate_own(checkpoint, scope=scope)
        dependencies = await _load_checkpoints(
            session, _PRIOR_PHASES, for_update=False
        )
        _require_dependencies(
            dependencies,
            scope=scope,
            own_exists=checkpoint is not None,
        )
        return await _status_result(
            session,
            scope=scope,
            checkpoint=checkpoint,
            validate=True,
            for_update=False,
        )


async def run_shared_report_ownership_backfill_batch(
    session: AsyncSession,
    *,
    batch_size: int = DEFAULT_SHARED_REPORT_OWNERSHIP_BACKFILL_BATCH_SIZE,
) -> SharedReportOwnershipBackfillBatchResult:
    """Advance the retained shared-report table by at most one PK batch."""

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
            dependencies,
            scope=scope,
            own_exists=checkpoint is not None,
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
        before = checkpoint.data_checksum_before
        after = checkpoint.data_checksum_after
        ownership = checkpoint.ownership_checksum_after
        updated_count = 0
        unchanged_count = 0
        for index, row_id in enumerate(ids):
            row = await _project_and_lock_id(
                session,
                row_id,
                invoke_race_hook=index == 0,
            )
            needs_adoption = _validate_row(
                row,
                scope=scope,
                historical=True,
                allow_unowned=True,
            )
            before = _extend(before, _data_envelope(row))
            if needs_adoption:
                mutation = await session.execute(
                    update(_TABLE)
                    .where(
                        _TABLE.c.id == row_id,
                        _TABLE.c.subject_id.is_(None),
                        _TABLE.c.created_by_user_id.is_(None),
                        _TABLE.c.revoked_by_user_id.is_(None),
                    )
                    .values(
                        subject_id=scope.subject_id,
                        updated_at=row.updated_at,
                    )
                )
                if mutation.rowcount != 1:
                    raise SharedReportOwnershipBackfillStateError(
                        "shared-report ownership changed during adoption"
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
                raise SharedReportOwnershipBackfillStateError(
                    "a shared report disappeared during adoption"
                )
            current = _row_values(current_result)
            if _validate_row(
                current,
                scope=scope,
                historical=True,
                allow_unowned=False,
            ):
                raise SharedReportOwnershipBackfillStateError(
                    "a processed shared report remained unowned"
                )
            after = _extend(after, _data_envelope(current))
            ownership = _extend(ownership, _ownership_envelope(current))
        if before != after:
            raise SharedReportOwnershipBackfillStateError(
                "shared-report artifact data changed during ownership backfill"
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
                raise SharedReportOwnershipBackfillStateError(
                    "the shared-report snapshot changed during finalization"
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


async def prepare_shared_report_ownership_backfill_for_portability_v1_restore(
    session: AsyncSession,
) -> None:
    """Prepare retained Stage-3K evidence before portable tables are replaced."""

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependencies = await _load_checkpoints(
            session, _PRIOR_PHASES, for_update=True
        )
        if set(dependencies) != set(_PRIOR_PHASES):
            raise SharedReportOwnershipBackfillDependencyError(
                "Stage-3A through Stage-3J checkpoints are incomplete"
            )
        for phase in _PRIOR_PHASES:
            _validate_checkpoint(
                dependencies[phase],
                phase=phase,
                subject_id=scope.subject_id,
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
                raise SharedReportOwnershipBackfillStateError(
                    "retained shared-report bounds changed during restore preparation"
                )
            await _create_checkpoint(
                session,
                scope=scope,
                high=high,
                count=count,
            )
        else:
            await _status_result(
                session,
                scope=scope,
                checkpoint=checkpoint,
                validate=True,
                for_update=True,
            )
        await session.flush()


async def shared_report_historical_bridge_state(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> SharedReportHistoricalBridgeState:
    """Return the bounded legacy bridge authorized by the exact K checkpoint."""

    if not isinstance(subject_id, uuid.UUID):
        raise SharedReportOwnershipBackfillValidationError(
            "subject_id must be a UUID"
        )
    rows = await _load_checkpoints(session, (_PHASE_KEY,), for_update=False)
    checkpoint = rows.get(_PHASE_KEY)
    if checkpoint is None:
        return SharedReportHistoricalBridgeState(0, 0, False)
    scope = _Scope(subject_id=subject_id, owner_user_id=uuid.UUID(int=0))
    status = _validate_own(checkpoint, scope=scope)
    assert status is not None
    snapshot_high = checkpoint.scan_high_watermark_id
    if status == "completed":
        return SharedReportHistoricalBridgeState(snapshot_high, snapshot_high, True)
    return SharedReportHistoricalBridgeState(
        checkpoint.last_scanned_id,
        snapshot_high,
        False,
    )


async def _after_shared_report_projection_for_test() -> None:
    return None


__all__ = [
    "DEFAULT_SHARED_REPORT_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "MAX_SHARED_REPORT_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES",
    "SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE",
    "SHARED_REPORT_OWNERSHIP_BACKFILL_TABLES",
    "SharedReportHistoricalBridgeState",
    "SharedReportOwnershipBackfillBatchResult",
    "SharedReportOwnershipBackfillDependencyError",
    "SharedReportOwnershipBackfillError",
    "SharedReportOwnershipBackfillIdentityError",
    "SharedReportOwnershipBackfillPreflightResult",
    "SharedReportOwnershipBackfillProvenanceError",
    "SharedReportOwnershipBackfillStateError",
    "SharedReportOwnershipBackfillStatus",
    "SharedReportOwnershipBackfillValidationError",
    "preflight_shared_report_ownership_backfill",
    "prepare_shared_report_ownership_backfill_for_portability_v1_restore",
    "run_shared_report_ownership_backfill_batch",
    "shared_report_historical_bridge_state",
]
