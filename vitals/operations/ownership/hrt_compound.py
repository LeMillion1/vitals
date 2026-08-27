"""Bounded Stage-3F ownership backfill for the mixed HRT compound catalog.

Curated YAML definitions and their components remain global.  Historical
manual/MCP compounds are adopted by the sole reviewed subject and their
components inherit that subject.  The service flushes but never commits.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import Counter
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

from vitals.enums import Domain, Source, UserStatus
from vitals.models.hrt import (
    HrtCompound,
    HrtCompoundComponent,
    HrtCycleItem,
    HrtCycleTemplateItem,
    HrtDose,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.operations.ownership.hevy_child import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.hrt_child import (
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.hrt.records import (
    HrtCatalogIntegrityError,
    _require_curated_compound_integrity,
)
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.operations.ownership.normalized import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.provider_raw import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.raw import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.utils.timeutils import now_utc


HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE = "stage3.mixed_catalog.hrt.v1"
DEFAULT_HRT_COMPOUND_OWNERSHIP_BACKFILL_BATCH_SIZE = 250
MAX_HRT_COMPOUND_OWNERSHIP_BACKFILL_BATCH_SIZE = 1000

_PAGE_SIZE = 1000
_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CUSTOM_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CUSTOM_SOURCES = frozenset({Source.MANUAL.value, Source.MCP.value})
_B_PHASES = tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
_C_PHASES = tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_D_PHASES = tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_E_PHASES = tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
_PRIOR_PHASES = (RAW_OWNERSHIP_BACKFILL_PHASE,) + _B_PHASES + _C_PHASES + _D_PHASES + _E_PHASES


class HrtCompoundOwnershipBackfillStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"


class HrtCompoundOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed Stage-3F errors."""


class HrtCompoundOwnershipBackfillValidationError(
    HrtCompoundOwnershipBackfillError, ValueError
):
    """A caller argument or persisted scalar is invalid."""


class HrtCompoundOwnershipBackfillIdentityError(HrtCompoundOwnershipBackfillError):
    """The exact-one legacy owner graph is unavailable."""


class HrtCompoundOwnershipBackfillDependencyError(HrtCompoundOwnershipBackfillError):
    """A prerequisite checkpoint is absent, malformed, or nonterminal."""


class HrtCompoundOwnershipBackfillStateError(HrtCompoundOwnershipBackfillError):
    """Checkpoint progress, ownership, or a consumer link is inconsistent."""


class HrtCompoundOwnershipBackfillProvenanceError(HrtCompoundOwnershipBackfillError):
    """A compound cannot be classified as curated or reviewed custom data."""


@dataclass(frozen=True, slots=True)
class HrtCompoundOwnershipBackfillPreflightResult:
    phase_key: str
    subject_id: uuid.UUID
    status: HrtCompoundOwnershipBackfillStatus
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
        return self.status is HrtCompoundOwnershipBackfillStatus.COMPLETED

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
class HrtCompoundOwnershipBackfillBatchResult(HrtCompoundOwnershipBackfillPreflightResult):
    batch_table: str
    batch_scanned_rows: int
    batch_updated_rows: int
    batch_unchanged_rows: int

    @property
    def changed(self) -> bool:
        return self.batch_updated_rows > 0

    def to_safe_dict(self) -> dict[str, str | int]:
        result = HrtCompoundOwnershipBackfillPreflightResult.to_safe_dict(self)
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

    @property
    def name(self) -> str:
        return self.table.name

    @property
    def phase_key(self) -> str:
        return f"{HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE}.{self.name}"


_TABLES = (_TableSpec(HrtCompound.__table__), _TableSpec(HrtCompoundComponent.__table__))
HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES = tuple(spec.name for spec in _TABLES)
HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES: Mapping[str, str] = MappingProxyType(
    {spec.name: spec.phase_key for spec in _TABLES}
)
_PHASE_KEYS = tuple(spec.phase_key for spec in _TABLES)
_ROOT_PHASE = HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hrt_compounds"]
_COMPONENT_PHASE = HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
    "hrt_compound_components"
]


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
class _Summary:
    spec: _TableSpec
    checkpoint: Any | None
    high_watermark: int
    snapshot_rows: int
    remaining_rows: int
    rows_above: int


def _validate_batch_size(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_HRT_COMPOUND_OWNERSHIP_BACKFILL_BATCH_SIZE
    ):
        raise HrtCompoundOwnershipBackfillValidationError(
            "batch_size must be an integer between 1 and 1000"
        )
    return value


def _valid_counter(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _POSTGRES_INTEGER_MAX


def _validate_checkpoint(checkpoint: Any, *, phase: str, subject_id: uuid.UUID) -> str:
    error = HrtCompoundOwnershipBackfillDependencyError if phase in _PRIOR_PHASES else HrtCompoundOwnershipBackfillStateError
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
        or checkpoint.scanned_rows != checkpoint.updated_rows + checkpoint.unchanged_rows
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
    if checkpoint.started_at is None or checkpoint.updated_at is None or checkpoint.updated_at < checkpoint.started_at:
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
            raise HrtCompoundOwnershipBackfillIdentityError(
                "HRT compound backfill requires exactly one health subject"
            )
        subject_id, owner_user_id = subjects[0].id, subjects[0].owner_user_id
        owner = await session.scalar(
            select(User)
            .where(User.id == owner_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        status = owner.status if owner is not None else None
    else:
        subjects = list(
            await session.execute(
                select(HealthSubject.id, HealthSubject.owner_user_id)
                .order_by(HealthSubject.id)
                .limit(2)
            )
        )
        if len(subjects) != 1:
            raise HrtCompoundOwnershipBackfillIdentityError(
                "HRT compound backfill requires exactly one health subject"
            )
        subject_id, owner_user_id = subjects[0]
        status = await session.scalar(select(User.status).where(User.id == owner_user_id))
    if status != UserStatus.ACTIVE.value:
        raise HrtCompoundOwnershipBackfillIdentityError(
            "the sole health subject must have an active owner"
        )
    return _Scope(subject_id, owner_user_id)


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


async def _load_checkpoints(session: AsyncSession, phases: tuple[str, ...], *, for_update: bool) -> dict[str, Any]:
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


def _require_own_group(checkpoints: Mapping[str, Any], *, scope: _Scope) -> None:
    if checkpoints and set(checkpoints) != set(_PHASE_KEYS):
        raise HrtCompoundOwnershipBackfillStateError(
            "the HRT compound checkpoint group is partial"
        )
    for phase, checkpoint in checkpoints.items():
        status = _validate_checkpoint(checkpoint, phase=phase, subject_id=scope.subject_id)
        if status == "restore_blocked":
            raise HrtCompoundOwnershipBackfillStateError(
                "Stage-3F checkpoints cannot be restore-blocked"
            )
    if not checkpoints:
        return
    roots, components = checkpoints[_ROOT_PHASE], checkpoints[_COMPONENT_PHASE]
    pair = (roots.status, components.status)
    if pair not in {
        ("running", "running"),
        ("running", "completed"),
        ("completed", "running"),
        ("completed", "completed"),
    }:
        raise HrtCompoundOwnershipBackfillStateError(
            "the HRT compound checkpoint order is inconsistent"
        )
    root_empty = roots.scan_high_watermark_id == 0 and roots.snapshot_rows == 0
    component_empty = (
        components.scan_high_watermark_id == 0
        and components.snapshot_rows == 0
    )
    if root_empty and not component_empty:
        raise HrtCompoundOwnershipBackfillStateError(
            "an empty HRT compound checkpoint cannot have components"
        )
    if pair == ("running", "completed") and not (
        component_empty
        and components.last_scanned_id == 0
        and components.scanned_rows == 0
        and components.updated_rows == 0
        and components.unchanged_rows == 0
        and components.data_checksum_before == _EMPTY_SHA256
        and components.ownership_checksum_after == _EMPTY_SHA256
    ):
        raise HrtCompoundOwnershipBackfillStateError(
            "components cannot complete before compounds unless exactly empty"
        )


def _require_dependencies(
    dependencies: Mapping[str, Any], *, scope: _Scope, own_group_exists: bool
) -> bool:
    if set(dependencies) != set(_PRIOR_PHASES):
        raise HrtCompoundOwnershipBackfillDependencyError(
            "Stage-3A through Stage-3E checkpoints are incomplete"
        )
    statuses: dict[str, str] = {}
    for phase in _PRIOR_PHASES:
        statuses[phase] = _validate_checkpoint(
            dependencies[phase], phase=phase, subject_id=scope.subject_id
        )
    raw_status = statuses[RAW_OWNERSHIP_BACKFILL_PHASE]
    if raw_status == "completed":
        if any(statuses[phase] != "completed" for phase in _PRIOR_PHASES[1:]):
            raise HrtCompoundOwnershipBackfillDependencyError(
                "Stage-3A through Stage-3E must be completed"
            )
        return False
    if raw_status != "restore_blocked" or not own_group_exists:
        raise HrtCompoundOwnershipBackfillDependencyError(
            "restore-mode Stage-3F requires its exact reset checkpoint group"
        )
    if any(statuses[phase] not in {"running", "completed"} for phase in _B_PHASES + _C_PHASES):
        raise HrtCompoundOwnershipBackfillDependencyError(
            "Stage-3B/3C restore checkpoints must be running or completed"
        )
    if any(statuses[phase] not in {"completed", "restore_blocked"} for phase in _D_PHASES + _E_PHASES):
        raise HrtCompoundOwnershipBackfillDependencyError(
            "Stage-3D/3E restore checkpoints must be terminal"
        )
    _require_hevy_dependency_pair(dependencies)
    return True


def _exact_empty_completed_checkpoint(checkpoint: Any) -> bool:
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


def _require_hevy_dependency_pair(dependencies: Mapping[str, Any]) -> None:
    """Apply Stage-3E's own parent-before-child lifecycle algebra."""

    exercise = dependencies[
        HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_exercises"]
    ]
    sets = dependencies[
        HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["hevy_sets"]
    ]
    pair = (exercise.status, sets.status)
    if pair not in {
        ("completed", "completed"),
        ("restore_blocked", "restore_blocked"),
        ("restore_blocked", "completed"),
    }:
        raise HrtCompoundOwnershipBackfillDependencyError(
            "Stage-3E checkpoint order is inconsistent"
        )
    if pair == ("restore_blocked", "completed") and not (
        _exact_empty_completed_checkpoint(sets)
    ):
        raise HrtCompoundOwnershipBackfillDependencyError(
            "completed Hevy sets after a restore block must be exactly empty"
        )


def _catalog() -> dict[str, dict]:
    from vitals.services.hrt.catalog import load_compound_catalog

    return dict(load_compound_catalog())


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HrtCompoundOwnershipBackfillStateError(
                "an HRT compound row contains a non-finite number"
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
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    raise HrtCompoundOwnershipBackfillStateError(
        "an HRT compound row contains an unsupported value"
    )


def _extend(digest: str, values: list[Any]) -> str:
    payload = json.dumps(_canonical(values), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(bytes.fromhex(digest) + payload.encode()).hexdigest()


def _mapping(row: Any) -> Mapping[str, Any]:
    return row._mapping if hasattr(row, "_mapping") else row


def _data_envelope(spec: _TableSpec, row: Any) -> list[Any]:
    values = _mapping(row)
    excluded = {"subject_id"}
    if spec.table is HrtCompound.__table__:
        excluded.add("actor_user_id")
    return [spec.name] + [
        [column.name, values[column.name]]
        for column in spec.table.columns
        if column.name not in excluded
    ]


def _custom_ownership_envelope(spec: _TableSpec, row: Any, *, custom: bool) -> list[Any] | None:
    if not custom:
        return None
    values = _mapping(row)
    if spec.table is HrtCompound.__table__:
        return [spec.name, values["id"], values["subject_id"], values["actor_user_id"]]
    return [spec.name, values["id"], values["compound_id"], values["subject_id"]]


def _compound_values(row: HrtCompound) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in HrtCompound.__table__.columns}


def _component_values(row: HrtCompoundComponent) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in HrtCompoundComponent.__table__.columns}


def _classify_compound(
    row: HrtCompound, *, scope: _Scope, historical: bool
) -> tuple[bool, bool]:
    catalog = _catalog()
    if row.domain != Domain.HRT.value:
        raise HrtCompoundOwnershipBackfillProvenanceError(
            "an HRT compound has an unexpected domain"
        )
    if row.source == Source.SYSTEM.value:
        if row.subject_id is not None or row.actor_user_id is not None:
            raise HrtCompoundOwnershipBackfillStateError(
                "a curated HRT compound must remain global"
            )
        if row.key not in catalog:
            raise HrtCompoundOwnershipBackfillProvenanceError(
                "a system HRT compound is absent from the checked-in catalog"
            )
        try:
            _require_curated_compound_integrity(row)
        except HrtCatalogIntegrityError as exc:
            raise HrtCompoundOwnershipBackfillProvenanceError(
                "a curated HRT compound failed catalog integrity"
            ) from exc
        return False, False
    if row.source not in _CUSTOM_SOURCES:
        raise HrtCompoundOwnershipBackfillProvenanceError(
            "a custom HRT compound has an unexpected source"
        )
    if not isinstance(row.key, str) or _CUSTOM_KEY_RE.fullmatch(row.key) is None:
        raise HrtCompoundOwnershipBackfillProvenanceError(
            "a custom HRT compound key is not a canonical slug"
        )
    if row.key in catalog:
        raise HrtCompoundOwnershipBackfillProvenanceError(
            "a custom HRT compound collides with a curated key"
        )
    if historical:
        if row.subject_id is None and row.actor_user_id is None:
            return True, True
        if row.subject_id == scope.subject_id and row.actor_user_id in {
            None,
            scope.owner_user_id,
        }:
            return True, False
        if row.subject_id is None or row.actor_user_id is None:
            raise HrtCompoundOwnershipBackfillStateError(
                "a historical custom HRT compound has partial ownership"
            )
        raise HrtCompoundOwnershipBackfillStateError(
            "a historical custom HRT compound has foreign ownership"
        )
    if row.subject_id != scope.subject_id or row.actor_user_id != scope.owner_user_id:
        raise HrtCompoundOwnershipBackfillStateError(
            "a live custom HRT compound lacks exact ownership"
        )
    return True, False


def _expected_components(definition: dict) -> Counter[tuple[str, str]]:
    return Counter(
        (str(component["ester"]), float(component["mg"]).hex())
        for component in definition.get("components") or ()
    )


async def _validate_component_catalog(session: AsyncSession, *, for_update: bool) -> None:
    catalog = _catalog()
    last_root_id = 0
    while True:
        root_stmt = (
            select(*HrtCompound.__table__.columns)
            .where(
                HrtCompound.id > last_root_id,
                HrtCompound.source == Source.SYSTEM.value,
            )
            .order_by(HrtCompound.id)
            .limit(_PAGE_SIZE)
        )
        if for_update:
            root_stmt = root_stmt.with_for_update()
        roots = [
            SimpleNamespace(**row._mapping)
            for row in await session.execute(root_stmt)
        ]
        if not roots:
            return
        for root in roots:
            _classify_compound(
                root,
                scope=SimpleNamespace(subject_id=None, owner_user_id=None),
                historical=False,
            )
            expected = _expected_components(catalog[root.key])
            actual = Counter({pair: 0 for pair in expected})
            last_component_id = 0
            while True:
                component_stmt = (
                    select(*HrtCompoundComponent.__table__.columns)
                    .where(
                        HrtCompoundComponent.compound_id == root.id,
                        HrtCompoundComponent.id > last_component_id,
                    )
                    .order_by(HrtCompoundComponent.id)
                    .limit(_PAGE_SIZE)
                )
                if for_update:
                    component_stmt = component_stmt.with_for_update()
                components = [
                    SimpleNamespace(**row._mapping)
                    for row in await session.execute(component_stmt)
                ]
                if not components:
                    break
                for component in components:
                    if component.subject_id is not None or not math.isfinite(
                        component.mg
                    ):
                        raise HrtCompoundOwnershipBackfillStateError(
                            "a curated HRT component has invalid global ownership"
                        )
                    pair = (component.ester, float(component.mg).hex())
                    if pair not in expected:
                        raise HrtCompoundOwnershipBackfillProvenanceError(
                            "curated HRT components differ from the checked-in catalog"
                        )
                    actual[pair] += 1
                last_component_id = components[-1].id
                if len(components) < _PAGE_SIZE:
                    break
            if actual != expected:
                raise HrtCompoundOwnershipBackfillProvenanceError(
                    "curated HRT components differ from the checked-in catalog"
                )
        last_root_id = roots[-1].id
        if len(roots) < _PAGE_SIZE:
            return


async def _reject_duplicate_keys(session: AsyncSession) -> None:
    duplicate = await session.scalar(
        select(HrtCompound.key)
        .group_by(HrtCompound.subject_id, HrtCompound.key)
        .having(func.count() > 1)
        .limit(1)
    )
    if duplicate is not None:
        raise HrtCompoundOwnershipBackfillStateError(
            "HRT compounds contain a duplicate scoped key"
        )


async def _validate_consumers(session: AsyncSession, *, scope: _Scope, for_update: bool) -> None:
    for model in (HrtDose, HrtCycleItem):
        last_id = 0
        while True:
            stmt = (
                select(
                    model.id,
                    model.compound_id,
                    model.compound_key,
                    model.subject_id,
                )
                .where(model.id > last_id, model.compound_id.is_not(None))
                .order_by(model.id)
                .limit(_PAGE_SIZE)
            )
            if for_update:
                stmt = stmt.with_for_update()
            rows = [
                SimpleNamespace(**row._mapping)
                for row in await session.execute(stmt)
            ]
            if not rows:
                break
            for row in rows:
                root = await _load_root(
                    session, row.compound_id, for_update=False
                )
                if root is None:
                    raise HrtCompoundOwnershipBackfillStateError(
                        "an HRT consumer references a missing compound"
                    )
                if row.compound_key != root.key:
                    raise HrtCompoundOwnershipBackfillStateError(
                        "an HRT consumer compound snapshot key is inconsistent"
                    )
                if row.subject_id != scope.subject_id:
                    raise HrtCompoundOwnershipBackfillStateError(
                        "an HRT consumer is outside the subject scope"
                    )
                custom, _changed = _classify_compound(
                    root, scope=scope, historical=True
                )
                if custom and root.subject_id not in {None, scope.subject_id}:
                    raise HrtCompoundOwnershipBackfillStateError(
                        "an HRT consumer references a foreign custom compound"
                    )
            last_id = rows[-1].id
            if len(rows) < _PAGE_SIZE:
                break
    last_id = 0
    while True:
        stmt = (
            select(HrtCycleTemplateItem.id, HrtCycleTemplateItem.subject_id)
            .where(HrtCycleTemplateItem.id > last_id)
            .order_by(HrtCycleTemplateItem.id)
            .limit(_PAGE_SIZE)
        )
        if for_update:
            stmt = stmt.with_for_update()
        items = [
            SimpleNamespace(**row._mapping)
            for row in await session.execute(stmt)
        ]
        if not items:
            return
        for item in items:
            if item.subject_id != scope.subject_id:
                raise HrtCompoundOwnershipBackfillStateError(
                    "an HRT template item is outside the subject scope"
                )
        last_id = items[-1].id
        if len(items) < _PAGE_SIZE:
            return


async def _max_id(session: AsyncSession, spec: _TableSpec) -> int:
    value = await session.scalar(select(func.max(spec.table.c.id)))
    if value is None:
        return 0
    if not _valid_counter(value):
        raise HrtCompoundOwnershipBackfillStateError("an HRT catalog primary key is invalid")
    return value


async def _count_to(session: AsyncSession, spec: _TableSpec, high: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(spec.table).where(spec.table.c.id <= high)
        )
        or 0
    )


async def _remaining(session: AsyncSession, spec: _TableSpec, high: int, cursor: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(spec.table)
            .where(spec.table.c.id > cursor, spec.table.c.id <= high)
        )
        or 0
    )


async def _load_root(session: AsyncSession, root_id: int, *, for_update: bool) -> HrtCompound | None:
    stmt = select(*HrtCompound.__table__.columns).where(HrtCompound.id == root_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).one_or_none()
    return SimpleNamespace(**row._mapping) if row is not None else None


async def _load_component(session: AsyncSession, component_id: int, *, for_update: bool) -> HrtCompoundComponent | None:
    stmt = select(*HrtCompoundComponent.__table__.columns).where(
        HrtCompoundComponent.id == component_id
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).one_or_none()
    return SimpleNamespace(**row._mapping) if row is not None else None


def _set_cached_component_subject(
    session: AsyncSession,
    component_id: int,
    subject_id: uuid.UUID,
) -> None:
    """Reflect only the committed ownership write in an already-cached component."""

    identity = session.sync_session.identity_key(HrtCompoundComponent, component_id)
    cached = session.sync_session.identity_map.get(identity)
    if cached is not None:
        attributes.set_committed_value(cached, "subject_id", subject_id)


async def _lock_all_ids_bounded(session: AsyncSession, table: Table) -> None:
    """Lock a complete table while materializing at most one fixed page."""

    last_id = 0
    while True:
        ids = list(
            await session.scalars(
                select(table.c.id)
                .where(table.c.id > last_id)
                .order_by(table.c.id)
                .limit(_PAGE_SIZE)
                .with_for_update()
            )
        )
        if not ids:
            return
        last_id = ids[-1]
        if len(ids) < _PAGE_SIZE:
            return


async def _after_compound_projection_for_test() -> None:
    return None


async def _after_compound_roots_locked_for_test() -> None:
    return None


async def _after_component_parents_locked_for_test() -> None:
    return None


async def _linked_projection_digest(
    session: AsyncSession,
    *,
    table: Table,
    root_ids: list[int],
    columns: tuple[Any, ...],
    for_update: bool,
) -> tuple[int, str]:
    """Hash a complete root-linked projection one bounded keyset page at a time."""

    count = 0
    digest = _EMPTY_SHA256
    last_id = 0
    while True:
        stmt = (
            select(*columns)
            .where(table.c.id > last_id, table.c.compound_id.in_(root_ids))
            .order_by(table.c.id)
            .limit(_PAGE_SIZE)
        )
        if for_update:
            stmt = stmt.with_for_update()
        rows = list(await session.execute(stmt))
        if not rows:
            return count, digest
        for row in rows:
            digest = _extend(digest, [table.name, *tuple(row)])
        count += len(rows)
        last_id = rows[-1][0]
        if len(rows) < _PAGE_SIZE:
            return count, digest


async def _lock_root_batch_graph(session: AsyncSession, root_ids: list[int]) -> dict[str, Any]:
    projected_roots = {
        row.id: tuple(row)
        for row in await session.execute(
            select(
                HrtCompound.id,
                HrtCompound.key,
                HrtCompound.domain,
                HrtCompound.source,
                HrtCompound.subject_id,
                HrtCompound.actor_user_id,
            ).where(HrtCompound.id.in_(root_ids)).order_by(HrtCompound.id)
        )
    }
    consumer_projection: dict[str, tuple[int, str]] = {}
    for model in (HrtDose, HrtCycleItem):
        consumer_projection[model.__tablename__] = await _linked_projection_digest(
            session,
            table=model.__table__,
            root_ids=root_ids,
            columns=(model.id, model.compound_id, model.compound_key, model.subject_id),
            for_update=False,
        )
    component_projection = await _linked_projection_digest(
        session,
        table=HrtCompoundComponent.__table__,
        root_ids=root_ids,
        columns=(HrtCompoundComponent.id, HrtCompoundComponent.compound_id),
        for_update=False,
    )
    await _after_compound_projection_for_test()
    locked_roots: dict[int, HrtCompound] = {}
    for root_id in root_ids:
        root = await _load_root(session, root_id, for_update=True)
        if root is None or (
            root.id,
            root.key,
            root.domain,
            root.source,
            root.subject_id,
            root.actor_user_id,
        ) != projected_roots.get(root_id):
            raise HrtCompoundOwnershipBackfillStateError(
                "an HRT compound classification changed after projection"
            )
        locked_roots[root_id] = root
    await _after_compound_roots_locked_for_test()
    locked_components = await _linked_projection_digest(
        session,
        table=HrtCompoundComponent.__table__,
        root_ids=root_ids,
        columns=(HrtCompoundComponent.id, HrtCompoundComponent.compound_id),
        for_update=True,
    )
    if locked_components != component_projection:
        raise HrtCompoundOwnershipBackfillStateError(
            "an HRT component parent link changed after projection"
        )
    for model in (HrtDose, HrtCycleItem):
        projected = consumer_projection[model.__tablename__]
        locked = await _linked_projection_digest(
            session,
            table=model.__table__,
            root_ids=root_ids,
            columns=(model.id, model.compound_id, model.compound_key, model.subject_id),
            for_update=True,
        )
        if locked != projected:
            raise HrtCompoundOwnershipBackfillStateError(
                "an HRT consumer link changed after compound locking"
            )
    return {"roots": locked_roots}


async def _lock_component_batch_graph(session: AsyncSession, ids: list[int]) -> dict[int, tuple[int, HrtCompound]]:
    projected = {
        row.id: row.compound_id
        for row in await session.execute(
            select(HrtCompoundComponent.id, HrtCompoundComponent.compound_id)
            .where(HrtCompoundComponent.id.in_(ids))
            .order_by(HrtCompoundComponent.id)
        )
    }
    parent_ids = sorted(set(projected.values()))
    parents: dict[int, HrtCompound] = {}
    for parent_id in parent_ids:
        parent = await _load_root(session, parent_id, for_update=True)
        if parent is None:
            raise HrtCompoundOwnershipBackfillStateError(
                "an HRT component references a missing compound"
            )
        parents[parent_id] = parent
    await _after_component_parents_locked_for_test()
    result: dict[int, tuple[int, HrtCompound]] = {}
    for component_id in ids:
        component = await _load_component(session, component_id, for_update=True)
        if component is None:
            raise HrtCompoundOwnershipBackfillStateError(
                "an HRT component disappeared after parent locking"
            )
        if component.compound_id != projected.get(component_id):
            raise HrtCompoundOwnershipBackfillStateError(
                "an HRT component parent changed after projection"
            )
        result[component_id] = (component.compound_id, parents[component.compound_id])
    return result


async def _validate_current_graph(
    session: AsyncSession,
    *,
    scope: _Scope,
    for_update: bool,
    strict_components: bool,
) -> None:
    await _reject_duplicate_keys(session)
    last_root_id = 0
    while True:
        stmt = (
            select(*HrtCompound.__table__.columns)
            .where(HrtCompound.id > last_root_id)
            .order_by(HrtCompound.id)
            .limit(_PAGE_SIZE)
        )
        if for_update:
            stmt = stmt.with_for_update()
        roots = [
            SimpleNamespace(**row._mapping)
            for row in await session.execute(stmt)
        ]
        if not roots:
            break
        for root in roots:
            # Frozen high-water marks, not presence of S, distinguish historical
            # rows. `_scan_live_tail` applies the strict rule above each HWM.
            _classify_compound(root, scope=scope, historical=True)
        last_root_id = roots[-1].id
        if len(roots) < _PAGE_SIZE:
            break
    await _validate_component_catalog(session, for_update=for_update)
    last_component_id = 0
    while True:
        stmt = (
            select(*HrtCompoundComponent.__table__.columns)
            .where(HrtCompoundComponent.id > last_component_id)
            .order_by(HrtCompoundComponent.id)
            .limit(_PAGE_SIZE)
        )
        if for_update:
            stmt = stmt.with_for_update()
        components = [
            SimpleNamespace(**row._mapping)
            for row in await session.execute(stmt)
        ]
        if not components:
            break
        for component in components:
            parent = await _load_root(
                session, component.compound_id, for_update=False
            )
            if parent is None:
                raise HrtCompoundOwnershipBackfillStateError(
                    "an HRT component references a missing compound"
                )
            custom, _ = _classify_compound(parent, scope=scope, historical=True)
            expected = scope.subject_id if custom else None
            permitted = {expected}
            if custom and not strict_components:
                permitted.add(None)
            if component.subject_id not in permitted:
                raise HrtCompoundOwnershipBackfillStateError(
                    "an HRT component does not inherit its compound ownership"
                )
        last_component_id = components[-1].id
        if len(components) < _PAGE_SIZE:
            break
    await _validate_consumers(session, scope=scope, for_update=for_update)


async def _scan_live_tail(session: AsyncSession, *, spec: _TableSpec, high: int, scope: _Scope, for_update: bool) -> int:
    count = 0
    last_id = high
    while True:
        stmt = (
            select(spec.table.c.id)
            .where(spec.table.c.id > last_id)
            .order_by(spec.table.c.id)
            .limit(_PAGE_SIZE)
        )
        if for_update:
            stmt = stmt.with_for_update()
        ids = list(await session.scalars(stmt))
        if not ids:
            return count
        for row_id in ids:
            if spec.table is HrtCompound.__table__:
                root = await _load_root(session, row_id, for_update=for_update)
                assert root is not None
                _classify_compound(root, scope=scope, historical=False)
            else:
                component = await _load_component(
                    session, row_id, for_update=for_update
                )
                assert component is not None
                parent = await _load_root(
                    session, component.compound_id, for_update=for_update
                )
                if parent is None:
                    raise HrtCompoundOwnershipBackfillStateError(
                        "a live HRT component has no parent"
                    )
                custom, _ = _classify_compound(
                    parent, scope=scope, historical=True
                )
                if component.subject_id != (
                    scope.subject_id if custom else None
                ):
                    raise HrtCompoundOwnershipBackfillStateError(
                        "a live HRT component lacks exact inherited ownership"
                    )
        count += len(ids)
        last_id = ids[-1]
        if len(ids) < _PAGE_SIZE:
            return count


async def _rehash_snapshot(
    session: AsyncSession,
    *,
    spec: _TableSpec,
    checkpoint: Any,
    scope: _Scope,
    require_data: bool,
    for_update: bool,
) -> None:
    data = _EMPTY_SHA256
    ownership = _EMPTY_SHA256
    count = 0
    last_id = 0
    while True:
        stmt = (
            select(spec.table.c.id)
            .where(
                spec.table.c.id > last_id,
                spec.table.c.id <= checkpoint.scan_high_watermark_id,
            )
            .order_by(spec.table.c.id)
            .limit(_PAGE_SIZE)
        )
        if for_update:
            stmt = stmt.with_for_update()
        ids = list(await session.scalars(stmt))
        if not ids:
            break
        for row_id in ids:
            if spec.table is HrtCompound.__table__:
                row = await _load_root(session, row_id, for_update=for_update)
                assert row is not None
                custom, changed = _classify_compound(row, scope=scope, historical=True)
                if changed:
                    raise HrtCompoundOwnershipBackfillStateError(
                        "a completed custom HRT compound requires ownership repair"
                    )
                values = _compound_values(row)
            else:
                row = await _load_component(session, row_id, for_update=for_update)
                assert row is not None
                parent = await _load_root(session, row.compound_id, for_update=for_update)
                if parent is None:
                    raise HrtCompoundOwnershipBackfillStateError("an HRT component has no parent")
                custom, _ = _classify_compound(parent, scope=scope, historical=True)
                expected = scope.subject_id if custom else None
                if row.subject_id != expected:
                    raise HrtCompoundOwnershipBackfillStateError(
                        "a completed HRT component changed ownership"
                    )
                values = _component_values(row)
            if require_data:
                data = _extend(data, _data_envelope(spec, values))
            envelope = _custom_ownership_envelope(spec, values, custom=custom)
            if envelope is not None:
                ownership = _extend(ownership, envelope)
            count += 1
        last_id = ids[-1]
        if len(ids) < _PAGE_SIZE:
            break
    if require_data and (
        count != checkpoint.snapshot_rows
        or data != checkpoint.data_checksum_before
        or data != checkpoint.data_checksum_after
    ):
        raise HrtCompoundOwnershipBackfillStateError(
            "HRT compound data changed during the maintenance window"
        )
    if ownership != checkpoint.ownership_checksum_after:
        raise HrtCompoundOwnershipBackfillStateError(
            "completed custom HRT ownership changed"
        )


async def _summaries(
    session: AsyncSession,
    *,
    scope: _Scope,
    checkpoints: Mapping[str, Any],
    completed_group: bool,
    validate: bool,
    for_update: bool,
    require_data: bool = False,
) -> list[_Summary]:
    if validate:
        await _validate_current_graph(
            session,
            scope=scope,
            for_update=for_update,
            strict_components=completed_group,
        )
    result: list[_Summary] = []
    for spec in _TABLES:
        checkpoint = checkpoints.get(spec.phase_key)
        if checkpoint is None:
            high = await _max_id(session, spec)
            snapshot = await _count_to(session, spec, high)
            remaining = snapshot
        else:
            high, snapshot = checkpoint.scan_high_watermark_id, checkpoint.snapshot_rows
            remaining = 0 if completed_group else await _remaining(session, spec, high, checkpoint.last_scanned_id)
        rows_above = await _scan_live_tail(
            session, spec=spec, high=high, scope=scope, for_update=for_update
        ) if validate else int(
            await session.scalar(
                select(func.count()).select_from(spec.table).where(spec.table.c.id > high)
            ) or 0
        )
        if validate and checkpoint is not None and checkpoint.status == "completed":
            await _rehash_snapshot(
                session,
                spec=spec,
                checkpoint=checkpoint,
                scope=scope,
                require_data=require_data,
                for_update=for_update,
            )
        result.append(_Summary(spec, checkpoint, high, snapshot, remaining, rows_above))
    return result


def _aggregate(summaries: list[_Summary], field: str) -> str:
    digest = _EMPTY_SHA256
    for summary in summaries:
        value = getattr(summary.checkpoint, field) if summary.checkpoint is not None else _EMPTY_SHA256
        digest = _extend(digest, [summary.spec.name, value])
    return digest


def _result(scope: _Scope, summaries: list[_Summary]) -> HrtCompoundOwnershipBackfillPreflightResult:
    checkpoints = [summary.checkpoint for summary in summaries if summary.checkpoint is not None]
    completed = sum(checkpoint.status == "completed" for checkpoint in checkpoints)
    status = (
        HrtCompoundOwnershipBackfillStatus.COMPLETED
        if len(checkpoints) == len(_TABLES) and completed == len(_TABLES)
        else HrtCompoundOwnershipBackfillStatus.RUNNING
        if checkpoints
        else HrtCompoundOwnershipBackfillStatus.NOT_STARTED
    )
    return HrtCompoundOwnershipBackfillPreflightResult(
        phase_key=HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE,
        subject_id=scope.subject_id,
        status=status,
        tables_total=len(_TABLES),
        completed_tables=completed,
        snapshot_rows=sum(summary.snapshot_rows for summary in summaries),
        scanned_rows=sum(checkpoint.scanned_rows for checkpoint in checkpoints),
        updated_rows=sum(checkpoint.updated_rows for checkpoint in checkpoints),
        unchanged_rows=sum(checkpoint.unchanged_rows for checkpoint in checkpoints),
        remaining_rows=sum(summary.remaining_rows for summary in summaries),
        rows_above_high_watermark=sum(summary.rows_above for summary in summaries),
        data_checksum_before=_aggregate(summaries, "data_checksum_before"),
        data_checksum_after=_aggregate(summaries, "data_checksum_after"),
        ownership_checksum_after=_aggregate(summaries, "ownership_checksum_after"),
    )


def _batch(result: HrtCompoundOwnershipBackfillPreflightResult, *, table: str, scanned: int, updated: int, unchanged: int) -> HrtCompoundOwnershipBackfillBatchResult:
    return HrtCompoundOwnershipBackfillBatchResult(
        **{
            field: getattr(result, field)
            for field in HrtCompoundOwnershipBackfillPreflightResult.__dataclass_fields__
        },
        batch_table=table,
        batch_scanned_rows=scanned,
        batch_updated_rows=updated,
        batch_unchanged_rows=unchanged,
    )


async def preflight_hrt_compound_ownership_backfill(
    session: AsyncSession,
) -> HrtCompoundOwnershipBackfillPreflightResult:
    """Validate the fixed Stage-3F graph without mutation."""

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=False)
        checkpoints = await _load_checkpoints(session, _PHASE_KEYS, for_update=False)
        _require_own_group(checkpoints, scope=scope)
        dependencies = await _load_checkpoints(session, _PRIOR_PHASES, for_update=False)
        _require_dependencies(dependencies, scope=scope, own_group_exists=bool(checkpoints))
        completed = bool(checkpoints) and all(row.status == "completed" for row in checkpoints.values())
        summaries = await _summaries(
            session,
            scope=scope,
            checkpoints=checkpoints,
            completed_group=completed,
            validate=True,
            for_update=False,
        )
        return _result(scope, summaries)


def _validate_restore_bounds(snapshot_bounds: Any) -> dict[str, tuple[int, int]]:
    if not isinstance(snapshot_bounds, Mapping) or set(snapshot_bounds) != set(HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES):
        raise HrtCompoundOwnershipBackfillValidationError(
            "snapshot_bounds must contain the exact HRT compound table catalog"
        )
    result: dict[str, tuple[int, int]] = {}
    for name in HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES:
        pair = snapshot_bounds[name]
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise HrtCompoundOwnershipBackfillValidationError(
                "each HRT compound snapshot bound must be an exact pair"
            )
        high, count = pair
        if (
            not _valid_counter(high)
            or not _valid_counter(count)
            or count > high
            or (high == 0) != (count == 0)
        ):
            raise HrtCompoundOwnershipBackfillValidationError(
                "HRT compound snapshot bounds are invalid ID/count pairs"
            )
        result[name] = (high, count)
    if result["hrt_compounds"] == (0, 0) and result[
        "hrt_compound_components"
    ] != (0, 0):
        raise HrtCompoundOwnershipBackfillValidationError(
            "an empty HRT compound snapshot cannot contain components"
        )
    return result


async def reset_hrt_compound_backfill_for_portability_v1_restore(
    session: AsyncSession,
    *,
    snapshot_bounds: Mapping[str, tuple[int, int]],
) -> None:
    """Reset both Stage-3F checkpoints before the caller atomically replaces data."""

    bounds = _validate_restore_bounds(snapshot_bounds)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        dependencies = await _load_checkpoints(session, _PRIOR_PHASES, for_update=True)
        # A reset is the operation that creates the required own restore group.
        if set(dependencies) != set(_PRIOR_PHASES):
            raise HrtCompoundOwnershipBackfillDependencyError(
                "Stage-3A through Stage-3E checkpoints are incomplete"
            )
        for phase in _PRIOR_PHASES:
            _validate_checkpoint(dependencies[phase], phase=phase, subject_id=scope.subject_id)
        raw = dependencies[RAW_OWNERSHIP_BACKFILL_PHASE].status
        if raw not in {"completed", "restore_blocked"}:
            raise HrtCompoundOwnershipBackfillDependencyError("Stage-3A is not restore-terminal")
        if any(dependencies[p].status not in {"running", "completed"} for p in _B_PHASES + _C_PHASES):
            raise HrtCompoundOwnershipBackfillDependencyError("Stage-3B/3C restore state is invalid")
        if any(dependencies[p].status not in {"completed", "restore_blocked"} for p in _D_PHASES + _E_PHASES):
            raise HrtCompoundOwnershipBackfillDependencyError("Stage-3D/3E restore state is invalid")
        _require_hevy_dependency_pair(dependencies)
        checkpoints = await _load_checkpoints(session, _PHASE_KEYS, for_update=True)
        _require_own_group(checkpoints, scope=scope)
        # The portability transaction holds governance; lock both current tables
        # before their later delete/reload without trusting their old graph.
        for spec in _TABLES:
            await _lock_all_ids_bounded(session, spec.table)
        for spec in _TABLES:
            high, count = bounds[spec.name]
            status = "completed" if (high, count) == (0, 0) else "running"
            checkpoint = checkpoints.get(spec.phase_key)
            if checkpoint is None:
                checkpoint = OwnershipBackfillCheckpoint(
                    phase_key=spec.phase_key,
                    subject_id=scope.subject_id,
                    status=status,
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
                    completed_at=func.now() if status == "completed" else None,
                )
                session.add(checkpoint)
                checkpoints[spec.phase_key] = checkpoint
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


async def _create_group(session: AsyncSession, *, scope: _Scope, summaries: list[_Summary], checkpoints: dict[str, Any]) -> None:
    if checkpoints:
        return
    for summary in summaries:
        empty = (summary.high_watermark, summary.snapshot_rows) == (0, 0)
        checkpoint = OwnershipBackfillCheckpoint(
            phase_key=summary.spec.phase_key,
            subject_id=scope.subject_id,
            status="completed" if empty else "running",
            scan_high_watermark_id=summary.high_watermark,
            snapshot_rows=summary.snapshot_rows,
            last_scanned_id=0,
            scanned_rows=0,
            updated_rows=0,
            unchanged_rows=0,
            data_checksum_before=_EMPTY_SHA256,
            data_checksum_after=_EMPTY_SHA256,
            ownership_checksum_after=_EMPTY_SHA256,
            # Use one database clock for all lifecycle fields. SQLite's
            # CURRENT_TIMESTAMP has second precision, so mixing it with a
            # client-side microsecond timestamp can violate timestamp ordering
            # during an immediate first batch.
            started_at=func.now(),
            updated_at=func.now(),
            completed_at=func.now() if empty else None,
        )
        session.add(checkpoint)
        checkpoints[summary.spec.phase_key] = checkpoint
    await session.flush()


async def run_hrt_compound_ownership_backfill_batch(
    session: AsyncSession,
    *,
    batch_size: int,
) -> HrtCompoundOwnershipBackfillBatchResult:
    """Advance the first incomplete fixed table by at most one PK batch."""

    size = _validate_batch_size(batch_size)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        checkpoints = await _load_checkpoints(session, _PHASE_KEYS, for_update=True)
        _require_own_group(checkpoints, scope=scope)
        dependencies = await _load_checkpoints(session, _PRIOR_PHASES, for_update=True)
        _require_dependencies(dependencies, scope=scope, own_group_exists=bool(checkpoints))
        completed = bool(checkpoints) and all(row.status == "completed" for row in checkpoints.values())
        if completed:
            summaries = await _summaries(
                session,
                scope=scope,
                checkpoints=checkpoints,
                completed_group=True,
                validate=True,
                for_update=True,
            )
            return _batch(_result(scope, summaries), table=_TABLES[-1].name, scanned=0, updated=0, unchanged=0)
        summaries = await _summaries(
            session,
            scope=scope,
            checkpoints=checkpoints,
            completed_group=False,
            validate=True,
            for_update=False,
        )
        await _create_group(session, scope=scope, summaries=summaries, checkpoints=checkpoints)
        target = next(
            summary for summary in summaries if checkpoints[summary.spec.phase_key].status != "completed"
        )
        if target.spec.table is HrtCompoundComponent.__table__ and checkpoints[_ROOT_PHASE].status != "completed":
            raise HrtCompoundOwnershipBackfillStateError(
                "HRT components require completed compounds"
            )
        checkpoint = checkpoints[target.spec.phase_key]
        # Every appended row is strict live data and is never adopted.
        for summary in summaries:
            if summary.rows_above:
                await _scan_live_tail(
                    session,
                    spec=summary.spec,
                    high=checkpoints[summary.spec.phase_key].scan_high_watermark_id,
                    scope=scope,
                    for_update=True,
                )
        ids = list(
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
        graph = (
            await _lock_root_batch_graph(session, ids)
            if target.spec.table is HrtCompound.__table__
            else await _lock_component_batch_graph(session, ids)
        )

    before = checkpoint.data_checksum_before
    after = checkpoint.data_checksum_after
    ownership = checkpoint.ownership_checksum_after
    updated_count = 0
    unchanged_count = 0
    for row_id in ids:
        if target.spec.table is HrtCompound.__table__:
            row = await _load_root(session, row_id, for_update=True)
            if row is None:
                raise HrtCompoundOwnershipBackfillStateError("an HRT compound disappeared")
            projected = graph["roots"][row_id]
            if row.key != projected.key or row.source != projected.source or row.domain != projected.domain:
                raise HrtCompoundOwnershipBackfillStateError("an HRT compound changed while locked")
            custom, changed = _classify_compound(row, scope=scope, historical=True)
            values_before = _compound_values(row)
            before = _extend(before, _data_envelope(target.spec, values_before))
            if changed:
                await session.execute(
                    update(HrtCompound)
                    .where(HrtCompound.id == row_id)
                    .values(subject_id=scope.subject_id, updated_at=row.updated_at)
                )
                row = await _load_root(session, row_id, for_update=True)
                assert row is not None
                updated_count += 1
            else:
                unchanged_count += 1
            custom, changed = _classify_compound(row, scope=scope, historical=True)
            if changed:
                raise HrtCompoundOwnershipBackfillStateError("an HRT compound remained unowned")
            values_after = _compound_values(row)
        else:
            row = await _load_component(session, row_id, for_update=True)
            if row is None:
                raise HrtCompoundOwnershipBackfillStateError("an HRT component disappeared")
            parent_id, parent = graph[row_id]
            if row.compound_id != parent_id:
                raise HrtCompoundOwnershipBackfillStateError("an HRT component parent changed")
            custom, _ = _classify_compound(parent, scope=scope, historical=True)
            expected = scope.subject_id if custom else None
            values_before = _component_values(row)
            before = _extend(before, _data_envelope(target.spec, values_before))
            if row.subject_id is None and expected is not None:
                await session.execute(
                    update(HrtCompoundComponent)
                    .where(HrtCompoundComponent.id == row_id)
                    .values(subject_id=expected, updated_at=row.updated_at)
                )
                _set_cached_component_subject(session, row_id, expected)
                row = await _load_component(session, row_id, for_update=True)
                assert row is not None
                updated_count += 1
            elif row.subject_id == expected:
                unchanged_count += 1
            elif row.subject_id is None or expected is None:
                raise HrtCompoundOwnershipBackfillStateError(
                    "a historical HRT component has partial inherited ownership"
                )
            else:
                raise HrtCompoundOwnershipBackfillStateError(
                    "a historical HRT component has foreign ownership"
                )
            if row.compound_id != parent_id or row.subject_id != expected:
                raise HrtCompoundOwnershipBackfillStateError(
                    "an HRT component changed during ownership update"
                )
            values_after = _component_values(row)
        after = _extend(after, _data_envelope(target.spec, values_after))
        envelope = _custom_ownership_envelope(target.spec, values_after, custom=custom)
        if envelope is not None:
            ownership = _extend(ownership, envelope)
    if before != after:
        raise HrtCompoundOwnershipBackfillStateError(
            "HRT compound data changed while ownership was backfilled"
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
        session, target.spec, checkpoint.scan_high_watermark_id, checkpoint.last_scanned_id
    )
    if remaining == 0:
        await session.flush()
        await _rehash_snapshot(
            session,
            spec=target.spec,
            checkpoint=checkpoint,
            scope=scope,
            require_data=True,
            for_update=True,
        )
        checkpoint.last_scanned_id = checkpoint.scan_high_watermark_id
        checkpoint.status = "completed"
        checkpoint.completed_at = now_utc()
    await session.flush()
    checkpoints = await _load_checkpoints(session, _PHASE_KEYS, for_update=True)
    _require_own_group(checkpoints, scope=scope)
    complete = all(row.status == "completed" for row in checkpoints.values())
    summaries = await _summaries(
        session,
        scope=scope,
        checkpoints=checkpoints,
        completed_group=complete,
        validate=complete,
        for_update=complete,
        require_data=complete,
    )
    return _batch(
        _result(scope, summaries),
        table=target.spec.name,
        scanned=len(ids),
        updated=updated_count,
        unchanged=unchanged_count,
    )


__all__ = [
    "DEFAULT_HRT_COMPOUND_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES",
    "HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE",
    "HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES",
    "MAX_HRT_COMPOUND_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "HrtCompoundOwnershipBackfillBatchResult",
    "HrtCompoundOwnershipBackfillDependencyError",
    "HrtCompoundOwnershipBackfillError",
    "HrtCompoundOwnershipBackfillIdentityError",
    "HrtCompoundOwnershipBackfillPreflightResult",
    "HrtCompoundOwnershipBackfillProvenanceError",
    "HrtCompoundOwnershipBackfillStateError",
    "HrtCompoundOwnershipBackfillStatus",
    "HrtCompoundOwnershipBackfillValidationError",
    "preflight_hrt_compound_ownership_backfill",
    "reset_hrt_compound_backfill_for_portability_v1_restore",
    "run_hrt_compound_ownership_backfill_batch",
]
