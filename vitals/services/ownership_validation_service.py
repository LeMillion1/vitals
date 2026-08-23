"""Stage-4 whole-lake ownership validation.

Every Stage-3 phase proved one table at a time.  This phase proves the lake as a
whole: that no row is missing the subject its contract requires, that no
ownership reference reaches outside the reviewed roots, that every child agrees
with its parent and every normalized fact with its raw payload, and that a
scoped read returns exactly what a legacy unscoped read returns.

The check inventory is derived from ``Base.metadata`` and the machine-readable
ownership registry rather than from a hand-kept list, so a newly added table or
ownership reference is validated the moment it exists.  The operation mutates no
health data: on PostgreSQL it additionally makes the Stage-4 subject-equality
foreign keys valid, which is a constraint-state change, not a data change.
Callers own commit or rollback.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from sqlalchemy import Table, and_, func, inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserStatus
from vitals.models.base import Base
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.ownership import OWNERSHIP_REGISTRY, OwnershipClass, TargetColumn
from vitals.services.body_scan_metric_ownership_backfill_service import (
    BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.body_scan_ownership_backfill_service import (
    BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.conflict_rule_ownership_backfill_service import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.garmin_weight_export_ownership_backfill_service import (
    GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.genetic_variant_ownership_backfill_service import (
    GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
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
from vitals.services.lab_result_ownership_backfill_service import (
    LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.normalized_ownership_backfill_service import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.services.notification_ownership_backfill_service import (
    NOTIFICATION_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
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
from vitals.services.system_alert_ownership_backfill_service import (
    SYSTEM_ALERT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.weekly_digest_ownership_backfill_service import (
    WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.services.weight_log_ownership_backfill_service import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.utils.timeutils import now_utc


OWNERSHIP_VALIDATION_PHASE = "stage4.whole_lake_validation.v1"

_PHASE_KEY = OWNERSHIP_VALIDATION_PHASE
_PAGE_SIZE = 1000
_POSTGRES_INTEGER_MAX = (1 << 31) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Every bounded Stage-3 phase that must be terminal before the lake is proved.
STAGE3_PHASES: tuple[str, ...] = (
    (RAW_OWNERSHIP_BACKFILL_PHASE,)
    + tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
    + tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(NOTIFICATION_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(SYSTEM_ALERT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
)

# The Stage-4 subject-equality references revision 0046 adds ``NOT VALID``.
SUBJECT_EQUALITY_CONSTRAINTS: Mapping[str, str] = MappingProxyType(
    {
        "body_scan_metrics": "fk_body_scan_metrics_scan_subject",
        "hevy_exercises": "fk_hevy_exercises_workout_subject",
        "hevy_sets": "fk_hevy_sets_exercise_subject",
        "hrt_compound_components": "fk_hrt_compound_components_compound_subject",
        "hrt_cycle_items": "fk_hrt_cycle_items_cycle_subject",
        "hrt_cycle_template_items": "fk_hrt_cycle_template_items_template_subject",
    }
)

# Classes whose rows are reachable through one health subject.  Account and
# platform control planes deliberately own no subject and are excluded.
_SUBJECT_SCOPED_CLASSES = frozenset(
    {
        OwnershipClass.SUBJECT_ROOT,
        OwnershipClass.SUBJECT_DATA,
        OwnershipClass.SUBJECT_CHILD,
        OwnershipClass.SUBJECT_OPTIONAL,
        OwnershipClass.SUBJECT_CONTROL,
        OwnershipClass.SUBJECT_CONTROL_CHILD,
        OwnershipClass.MIXED_CATALOG,
        OwnershipClass.MIXED_CATALOG_CHILD,
    }
)
_SUBJECT_TABLE = "health_subjects"
_USER_TABLE = "users"
_CONNECTION_TABLE = "integration_connections"
_FILE_TABLE = "file_assets"
_RAW_TABLE = "raw_payloads"
_CHECKPOINT_TABLE = "ownership_backfill_checkpoints"


class OwnershipValidationStatus(StrEnum):
    NOT_STARTED = "not_started"
    COMPLETED = "completed"


class OwnershipValidationError(RuntimeError):
    """Base class for fail-closed Stage-4 errors."""


class OwnershipValidationDependencyError(OwnershipValidationError):
    """A Stage-3 phase is absent, malformed, or not terminal."""


class OwnershipValidationIdentityError(OwnershipValidationError):
    """The exact-one reviewed owner graph is unavailable."""


class OwnershipValidationStateError(OwnershipValidationError):
    """The persisted checkpoint is inconsistent."""


class OwnershipValidationViolation(OwnershipValidationError):
    """The lake still contains at least one unproved ownership row."""


@dataclass(frozen=True, slots=True)
class OwnershipValidationResult:
    phase_key: str
    subject_id: uuid.UUID
    status: OwnershipValidationStatus
    tables_total: int
    checks_total: int
    rows_inspected: int
    violations_total: int
    validated_constraints: int
    graph_checksum: str

    @property
    def completed(self) -> bool:
        return self.status is OwnershipValidationStatus.COMPLETED

    def to_safe_dict(self) -> dict[str, str | int]:
        return {
            "phase_key": self.phase_key,
            "status": self.status.value,
            "tables_total": self.tables_total,
            "checks_total": self.checks_total,
            "rows_inspected": self.rows_inspected,
            "violations_total": self.violations_total,
            "validated_constraints": self.validated_constraints,
            "graph_checksum": self.graph_checksum,
        }


@dataclass(frozen=True, slots=True)
class _Scope:
    subject_id: uuid.UUID
    owner_user_id: uuid.UUID


def _valid_counter(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _POSTGRES_INTEGER_MAX
    )


def _extend(digest: str, values: Sequence[Any]) -> str:
    payload = json.dumps(
        [str(item) if isinstance(item, uuid.UUID) else item for item in values],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(bytes.fromhex(digest) + payload).hexdigest()


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
        raise OwnershipValidationIdentityError(
            "whole-lake validation requires exactly one health subject"
        )
    subject_id, owner_user_id = rows[0]
    owner_query = select(User.status).where(User.id == owner_user_id)
    if for_update:
        owner_query = owner_query.with_for_update()
    if await session.scalar(owner_query) != UserStatus.ACTIVE.value:
        raise OwnershipValidationIdentityError(
            "the sole health subject must have an active owner"
        )
    return _Scope(subject_id, owner_user_id)


async def _require_stage3_completed(
    session: AsyncSession, *, scope: _Scope
) -> None:
    rows = {
        row.phase_key: row
        for row in await session.execute(
            select(
                OwnershipBackfillCheckpoint.phase_key,
                OwnershipBackfillCheckpoint.subject_id,
                OwnershipBackfillCheckpoint.status,
            ).where(OwnershipBackfillCheckpoint.phase_key.in_(STAGE3_PHASES))
        )
    }
    if set(rows) != set(STAGE3_PHASES):
        raise OwnershipValidationDependencyError(
            "whole-lake validation requires every Stage-3 phase checkpoint"
        )
    for phase in STAGE3_PHASES:
        row = rows[phase]
        if row.subject_id != scope.subject_id:
            raise OwnershipValidationDependencyError(
                "a Stage-3 checkpoint belongs to another subject"
            )
        if row.status != "completed":
            # A restore-blocked or still-running phase means the lake is not
            # proved yet, whatever the rest of the graph looks like.
            raise OwnershipValidationDependencyError(
                "every Stage-3 phase must be completed before validation"
            )


def _subject_scoped_tables(present: frozenset[str] | None = None) -> tuple[Table, ...]:
    """The subject-scoped tables this validation is about.

    ``present`` is the set of tables the database actually has. It matters
    because this phase runs *before* the contract migration, against a lake at
    the pre-contract revision, while ``Base.metadata`` describes the schema at
    head. A table introduced by a later revision has no unowned history to
    prove — it is created with its ownership mandatory from the first row — so
    validating it would mean querying a relation that is not there yet.

    ``None`` means every registered table, which is what a caller inspecting the
    schema at head wants.
    """

    tables = []
    for name, table in sorted(Base.metadata.tables.items()):
        spec = OWNERSHIP_REGISTRY.get(name)
        if spec is None:
            raise OwnershipValidationStateError(
                "a persisted table is missing from the ownership registry"
            )
        if present is not None and name not in present:
            continue
        if spec.ownership in _SUBJECT_SCOPED_CLASSES:
            tables.append(table)
    return tuple(tables)


async def _present_tables(session: AsyncSession) -> frozenset[str]:
    """What the database in front of us actually holds, right now."""

    connection = await session.connection()
    names = await connection.run_sync(
        lambda sync_connection: inspect(sync_connection).get_table_names()
    )
    return frozenset(names)


def _root_references(table: Table) -> dict[str, list[str]]:
    """Group this table's ownership references by the root they must reach."""

    grouped: dict[str, list[str]] = {
        _USER_TABLE: [],
        _CONNECTION_TABLE: [],
        _FILE_TABLE: [],
        _RAW_TABLE: [],
    }
    for foreign_key in sorted(
        table.foreign_keys, key=lambda item: (item.parent.name, item.target_fullname)
    ):
        target_table, target_column = foreign_key.target_fullname.split(".", 1)
        if target_table not in grouped or target_column != "id":
            continue
        column = foreign_key.parent.name
        if column not in grouped[target_table]:
            grouped[target_table].append(column)
    return grouped


def _parent_references(
    table: Table, present: frozenset[str] | None = None
) -> list[tuple[str, str]]:
    """Return this table's single-column links to another subject-scoped table."""

    parents: list[tuple[str, str]] = []
    scoped = {item.name for item in _subject_scoped_tables(present)}
    for constraint in sorted(
        table.foreign_key_constraints, key=lambda item: str(item.elements[0].parent.name)
    ):
        if len(constraint.elements) != 1:
            continue
        element = constraint.elements[0]
        target_table, target_column = element.target_fullname.split(".", 1)
        if target_table == table.name or target_table not in scoped:
            continue
        if target_column != "id" or target_table in {
            _SUBJECT_TABLE,
            _CONNECTION_TABLE,
            _FILE_TABLE,
            _RAW_TABLE,
        }:
            continue
        parent = Base.metadata.tables[target_table]
        if "subject_id" not in parent.columns:
            continue
        pair = (element.parent.name, target_table)
        if pair not in parents:
            parents.append(pair)
    return parents


async def _count(session: AsyncSession, query) -> int:
    return int(await session.scalar(query) or 0)


async def _run_checks(
    session: AsyncSession, *, scope: _Scope
) -> tuple[int, int, int, str]:
    """Return (tables, checks, rows inspected, graph digest); raise on violation."""

    digest = _EMPTY_SHA256
    checks = 0
    rows_inspected = 0
    present = await _present_tables(session)
    tables = _subject_scoped_tables(present)
    subject_table = Base.metadata.tables[_SUBJECT_TABLE]
    connections = Base.metadata.tables[_CONNECTION_TABLE]
    files = Base.metadata.tables[_FILE_TABLE]
    raws = Base.metadata.tables[_RAW_TABLE]

    for table in tables:
        spec = OWNERSHIP_REGISTRY[table.name]
        total = await _count(session, select(func.count()).select_from(table))
        if not _valid_counter(total):
            raise OwnershipValidationStateError(
                "a validated table reports an implausible row count"
            )
        rows_inspected += total
        # The checkpoint table is the evidence store, not part of the health lake
        # the evidence describes, and this phase writes its own row into it.
        # Its ownership is still validated below; only the digest skips it.
        digestible = table.name != _CHECKPOINT_TABLE
        if digestible:
            digest = _extend(digest, [table.name, total])

        # An inherited child carries whatever its parent carries, so its
        # reachable parents decide whether a missing subject is a gap.
        parents = (
            _parent_references(table, present)
            if "subject_id" in table.columns
            else []
        )

        if table.name != _SUBJECT_TABLE and "subject_id" in table.columns:
            column = table.columns["subject_id"]
            owned = await _count(
                session,
                select(func.count())
                .select_from(table)
                .where(column == scope.subject_id),
            )
            foreign = await _count(
                session,
                select(func.count())
                .select_from(table)
                .where(column.is_not(None), column != scope.subject_id),
            )
            unowned = total - owned - foreign
            checks += 2
            if digestible:
                digest = _extend(digest, [table.name, "subject", owned, unowned])
            if foreign:
                raise OwnershipValidationViolation(
                    "a row references a health subject outside the reviewed scope"
                )
            if spec.subject is TargetColumn.REQUIRED and unowned:
                raise OwnershipValidationViolation(
                    "a row that requires a subject still has none"
                )
            # A platform catalog parent has no subject, and its inherited
            # components legitimately have none either: parent/child equality
            # below is the invariant, not the presence of a subject.  A child
            # with no reachable parent has nothing to inherit from, so there
            # its subject is still mandatory.
            if spec.subject is TargetColumn.INHERITED and unowned and not parents:
                raise OwnershipValidationViolation(
                    "an inherited child row still has no subject"
                )
            # The scoped shadow read must return exactly the legacy unscoped one
            # wherever the contract makes the subject mandatory.
            if spec.subject is TargetColumn.REQUIRED and owned != total:
                raise OwnershipValidationViolation(
                    "a scoped read does not return the whole legacy table"
                )

        grouped = _root_references(table)
        for column_name in grouped[_USER_TABLE]:
            column = table.columns[column_name]
            checks += 1
            if await _count(
                session,
                select(func.count())
                .select_from(table)
                .where(column.is_not(None), column != scope.owner_user_id),
            ):
                raise OwnershipValidationViolation(
                    "a row names an actor outside the reviewed owner boundary"
                )
        for column_name in grouped[_CONNECTION_TABLE]:
            column = table.columns[column_name]
            checks += 1
            if await _count(
                session,
                select(func.count())
                .select_from(table.join(connections, column == connections.c.id))
                .where(connections.c.subject_id != scope.subject_id),
            ) or await _count(
                session,
                select(func.count())
                .select_from(table)
                .where(
                    column.is_not(None),
                    ~select(1)
                    .where(connections.c.id == column)
                    .exists(),
                ),
            ):
                raise OwnershipValidationViolation(
                    "a row references a connection outside its subject"
                )
        for column_name in grouped[_FILE_TABLE]:
            column = table.columns[column_name]
            checks += 1
            if await _count(
                session,
                select(func.count())
                .select_from(table.join(files, column == files.c.id))
                .where(files.c.subject_id != scope.subject_id),
            ) or await _count(
                session,
                select(func.count())
                .select_from(table)
                .where(
                    column.is_not(None),
                    ~select(1).where(files.c.id == column).exists(),
                ),
            ):
                raise OwnershipValidationViolation(
                    "a row references a file asset outside its subject"
                )
        for column_name in grouped[_RAW_TABLE]:
            if table.name == _RAW_TABLE:
                continue
            column = table.columns[column_name]
            checks += 1
            if await _count(
                session,
                select(func.count())
                .select_from(table)
                .where(
                    column.is_not(None),
                    ~select(1).where(raws.c.id == column).exists(),
                ),
            ):
                raise OwnershipValidationViolation(
                    "a row references a raw payload that no longer exists"
                )
            if "subject_id" not in table.columns:
                continue
            if await _count(
                session,
                select(func.count())
                .select_from(table.join(raws, column == raws.c.id))
                .where(
                    table.columns["subject_id"].is_not(None),
                    raws.c.subject_id.is_distinct_from(
                        table.columns["subject_id"]
                    ),
                ),
            ):
                raise OwnershipValidationViolation(
                    "a normalized row and its raw payload name different subjects"
                )

        if "subject_id" not in table.columns:
            continue
        for column_name, parent_name in parents:
            parent = Base.metadata.tables[parent_name]
            column = table.columns[column_name]
            checks += 1
            if await _count(
                session,
                select(func.count())
                .select_from(table.join(parent, column == parent.c.id))
                .where(
                    or_(
                        and_(
                            table.columns["subject_id"].is_not(None),
                            parent.c.subject_id.is_distinct_from(
                                table.columns["subject_id"]
                            ),
                        ),
                        and_(
                            table.columns["subject_id"].is_(None),
                            parent.c.subject_id.is_not(None),
                            OWNERSHIP_REGISTRY[table.name].subject
                            is TargetColumn.INHERITED,
                        ),
                    )
                ),
            ):
                raise OwnershipValidationViolation(
                    "a child row and its parent name different subjects"
                )

    checks += 1
    if await _count(session, select(func.count()).select_from(subject_table)) != 1:
        raise OwnershipValidationIdentityError(
            "a second health subject exists before the reviewed writable gate"
        )
    return len(tables), checks, rows_inspected, digest


async def _validate_stage4_constraints(session: AsyncSession) -> int:
    """Make the Stage-4 subject-equality references valid on PostgreSQL."""

    if session.get_bind().dialect.name != "postgresql":
        return 0
    validated = 0
    for table_name, constraint_name in SUBJECT_EQUALITY_CONSTRAINTS.items():
        await session.execute(
            _validate_constraint_statement(table_name, constraint_name)
        )
        validated += 1
    return validated


def _validate_constraint_statement(table_name: str, constraint_name: str):
    from sqlalchemy import text

    if not table_name.isidentifier() or not constraint_name.isidentifier():
        raise OwnershipValidationStateError(
            "a Stage-4 constraint name is not a plain identifier"
        )
    return text(
        f'ALTER TABLE "{table_name}" VALIDATE CONSTRAINT "{constraint_name}"'
    )


def _result(
    *,
    scope: _Scope,
    status: OwnershipValidationStatus,
    tables: int,
    checks: int,
    rows_inspected: int,
    validated_constraints: int,
    digest: str,
) -> OwnershipValidationResult:
    return OwnershipValidationResult(
        phase_key=OWNERSHIP_VALIDATION_PHASE,
        subject_id=scope.subject_id,
        status=status,
        tables_total=tables,
        checks_total=checks,
        rows_inspected=min(rows_inspected, _POSTGRES_INTEGER_MAX),
        violations_total=0,
        validated_constraints=validated_constraints,
        graph_checksum=digest,
    )


def _validate_own(checkpoint: Any, *, scope: _Scope) -> None:
    if checkpoint.phase_key != _PHASE_KEY or checkpoint.subject_id != scope.subject_id:
        raise OwnershipValidationStateError(
            "the validation checkpoint has the wrong phase or subject"
        )
    if checkpoint.status != "completed":
        raise OwnershipValidationStateError(
            "the validation checkpoint has an unsupported status"
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
        raise OwnershipValidationStateError(
            "the validation checkpoint has invalid counters"
        )
    if (
        checkpoint.updated_rows != 0
        or checkpoint.scanned_rows != checkpoint.unchanged_rows
        or checkpoint.last_scanned_id != checkpoint.scan_high_watermark_id
        or checkpoint.scanned_rows != checkpoint.snapshot_rows
    ):
        raise OwnershipValidationStateError(
            "the validation checkpoint has inconsistent counters"
        )
    digests = (
        checkpoint.data_checksum_before,
        checkpoint.data_checksum_after,
        checkpoint.ownership_checksum_after,
    )
    if any(
        not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
        for value in digests
    ):
        raise OwnershipValidationStateError(
            "the validation checkpoint has an invalid checksum"
        )
    if len(set(digests)) != 1:
        raise OwnershipValidationStateError(
            "the validation checkpoint has divergent evidence"
        )
    if checkpoint.completed_at is None or checkpoint.started_at is None:
        raise OwnershipValidationStateError(
            "the validation checkpoint has invalid timestamps"
        )


async def _load_checkpoint(
    session: AsyncSession, *, for_update: bool
) -> OwnershipBackfillCheckpoint | None:
    query = select(OwnershipBackfillCheckpoint).where(
        OwnershipBackfillCheckpoint.phase_key == _PHASE_KEY
    )
    if for_update:
        query = query.with_for_update().execution_options(populate_existing=True)
    return await session.scalar(query)


async def preflight_ownership_validation(
    session: AsyncSession,
) -> OwnershipValidationResult:
    """Prove the whole lake read-only, without recording or mutating anything."""

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=False)
        await _require_stage3_completed(session, scope=scope)
        checkpoint = await _load_checkpoint(session, for_update=False)
        if checkpoint is not None:
            _validate_own(checkpoint, scope=scope)
        tables, checks, rows, digest = await _run_checks(session, scope=scope)
        if checkpoint is not None and checkpoint.ownership_checksum_after != digest:
            # The lake is still valid, but it is not the lake the recorded
            # evidence describes: the operator has to record it again.
            return _result(
                scope=scope,
                status=OwnershipValidationStatus.NOT_STARTED,
                tables=tables,
                checks=checks,
                rows_inspected=rows,
                validated_constraints=0,
                digest=digest,
            )
        return _result(
            scope=scope,
            status=(
                OwnershipValidationStatus.COMPLETED
                if checkpoint is not None
                else OwnershipValidationStatus.NOT_STARTED
            ),
            tables=tables,
            checks=checks,
            rows_inspected=rows,
            validated_constraints=(
                len(SUBJECT_EQUALITY_CONSTRAINTS) if checkpoint is not None else 0
            ),
            digest=digest,
        )


async def run_ownership_validation(
    session: AsyncSession,
) -> OwnershipValidationResult:
    """Prove the whole lake and record the reviewed evidence.

    On PostgreSQL this additionally makes the Stage-4 subject-equality foreign
    keys valid, which is the only state this operation changes.
    """

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        await _require_stage3_completed(session, scope=scope)
        checkpoint = await _load_checkpoint(session, for_update=True)
        if checkpoint is not None:
            _validate_own(checkpoint, scope=scope)
        tables, checks, rows, digest = await _run_checks(session, scope=scope)
        validated = await _validate_stage4_constraints(session)
        stamp = now_utc().replace(microsecond=0)
        if checkpoint is None:
            checkpoint = OwnershipBackfillCheckpoint(
                phase_key=_PHASE_KEY,
                subject_id=scope.subject_id,
                started_at=stamp,
            )
            session.add(checkpoint)
        bounded = min(rows, _POSTGRES_INTEGER_MAX)
        checkpoint.status = "completed"
        checkpoint.scan_high_watermark_id = bounded
        checkpoint.snapshot_rows = bounded
        checkpoint.last_scanned_id = bounded
        checkpoint.scanned_rows = bounded
        checkpoint.updated_rows = 0
        checkpoint.unchanged_rows = bounded
        checkpoint.data_checksum_before = digest
        checkpoint.data_checksum_after = digest
        checkpoint.ownership_checksum_after = digest
        checkpoint.updated_at = stamp
        checkpoint.completed_at = stamp
        await session.flush()
        return _result(
            scope=scope,
            status=OwnershipValidationStatus.COMPLETED,
            tables=tables,
            checks=checks,
            rows_inspected=rows,
            validated_constraints=validated or len(SUBJECT_EQUALITY_CONSTRAINTS),
            digest=digest,
        )


__all__ = [
    "OWNERSHIP_VALIDATION_PHASE",
    "STAGE3_PHASES",
    "SUBJECT_EQUALITY_CONSTRAINTS",
    "OwnershipValidationStatus",
    "OwnershipValidationError",
    "OwnershipValidationDependencyError",
    "OwnershipValidationIdentityError",
    "OwnershipValidationStateError",
    "OwnershipValidationViolation",
    "OwnershipValidationResult",
    "preflight_ownership_validation",
    "run_ownership_validation",
]
