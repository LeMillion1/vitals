"""Data portability — full backup/restore + a curated LLM-ready export.

Two deliberately different shapes (they pull in opposite directions, so we don't
try to make one file serve both):

  * **Full backup** (:func:`export_full` plus the operational full-v1 restore
    coordinator) — a
    machine-round-trippable snapshot of portable health data (including the
    ``raw_payloads`` JSONB data-lake and internal ``id``s). Durable identity,
    authorization, audit, published-link control-plane tables, and tenant/private
    resource references are deliberately excluded. Import runs **replace** for
    portable data only, preserving business columns and primary keys. The whole
    thing rides the request's single transaction — any error rolls the DB back to
    where it started (the router owns the commit; we only ``flush``).

  * **LLM export** (:func:`export_llm`) — a flat, human-readable digest the owner
    pastes straight into a chat (Claude/ChatGPT). No raw dumps, no ids, no service
    tables, no secrets, no superseded rows.

Design notes:
  * There are no per-table Pydantic schemas in this project, so the backup walks
    the ORM generically via ``Base.metadata.sorted_tables`` (already FK-ordered).
    That auto-captures every new metric as the schema grows — the maximal-capture
    principle. Only the ``metadata`` envelope gets a Pydantic model.
  * ``app_settings`` rows whose key looks like a credential are dropped from the
    backup so the file can't leak tokens. (Real secrets live in ``.env``, which the
    DB export never touches — this is defence in depth for any future token row.)
    The import is the mirror of that rule: rows the export won't write, it won't
    delete or accept either, so a restore can neither drop a credential nor plant
    one. Without the mirror the two halves are asymmetric and a round trip through
    backup is lossy in precisely the rows being protected — which is how restoring
    a backup used to switch two-factor auth off.
  * Photo binaries are *not* in the backup — ``progress_photos`` rows carry only the
    ``file_key`` reference (files live on disk). Restore brings back the rows, not
    the images.
  * ``shared_reports`` is skipped by both halves. The generic table walk would
    otherwise sweep bcrypt password hashes and full copies of the medical record
    into every backup file, and a restore would *resurrect published links* —
    including ones that had been revoked or had expired — with live snapshots
    behind them. Same mirror rule as the secret settings: what the export won't
    write, the import won't delete or accept.
"""
from __future__ import annotations

import os
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import (
    Date,
    DateTime,
    Time,
    column as sql_column,
    func,
    inspect,
    select,
    table as sql_table,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import vitals.models  # noqa: F401 -- register the complete generic backup graph
from vitals.models.base import Base
from vitals.models.body_scan import BodyScan
from vitals.models.garmin import GarminActivity, GarminDaily
from vitals.models.genetics import GeneticVariant
from vitals.models.glp1 import DosePhase, Injection, SideEffect
from vitals.models.hevy import HevyExercise, HevySet, HevyWorkout
from vitals.models.hrt import HrtCycle, HrtCycleTemplate, HrtDose, HrtSideEffect
from vitals.models.labs import LabResult
from vitals.models.milestones import Milestone, WeeklyDigest
from vitals.models.nutrition import MealLog
from vitals.models.skincare import SkincareLog, SkincareObservation
from vitals.models.supplements import Supplement
from vitals.models.timeline import Annotation
from vitals.models.weight import BodyMeasurement, NoiseMarker, WeightLog
from vitals.ownership import (
    OWNERSHIP_REGISTRY,
    OwnershipClass,
    TargetColumn,
)
from vitals.ownership_transition.portability_v1 import PortabilityV1OwnershipHooks
from vitals.i18n import t
from vitals.services.conflict_catalog import (
    sync_catalog as sync_conflict_catalog,
)
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.labs_service import normalize_marker_key
from vitals.utils.timeutils import now_local

# Bump when the on-disk shape changes in a backward-incompatible way.
BACKUP_VERSION = "1.0"
KIND_FULL = "full_backup"
KIND_LLM = "llm_export"

# An ``app_settings`` key is treated as a secret (and dropped from the backup) when
# it contains any of these substrings — forward-looking guard for token rows.
_SECRET_KEY_MARKERS = ("token", "secret", "password", "api_key", "apikey", "credential")
_POSTGRES_INTEGER_MAX = 2_147_483_647


def _upgrade_lab_marker_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Upgrade older v1 lab rows to the canonical-key shape without losing text.

    Backup v1 is schema-evolving and older archives predate marker keys.  Work on
    shallow row copies so validation/import never mutates the caller's object.
    A subject export contains one record, and full-v1 is already restricted to
    one subject, so grouping by key here cannot cross a tenant boundary.
    """

    marker_rows = payload.get("lab_markers")
    result_rows = payload.get("lab_results")
    if not marker_rows and not result_rows:
        return payload

    upgraded = dict(payload)
    markers = [dict(row) for row in (marker_rows or [])]
    results = [dict(row) for row in (result_rows or [])]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in markers:
        name = row.get("name")
        if not isinstance(name, str) or not normalize_marker_key(name):
            raise _contract_error("import.error.generic", exc="invalid lab marker name")
        key = row.get("normalized_name") or normalize_marker_key(name)
        if not isinstance(key, str) or key != normalize_marker_key(name):
            raise _contract_error(
                "import.error.generic", exc="invalid lab marker normalized key"
            )
        row["normalized_name"] = key
        groups[key].append(row)

    for result in results:
        marker = result.get("marker")
        if not isinstance(marker, str) or not normalize_marker_key(marker):
            raise _contract_error("import.error.generic", exc="invalid lab result marker")
        key = result.get("marker_key") or normalize_marker_key(marker)
        if not isinstance(key, str) or key != normalize_marker_key(
            result.get("marker_original", marker)
        ):
            raise _contract_error("import.error.generic", exc="invalid lab result key")

    canonical_names: dict[str, str] = {}
    for key, rows in groups.items():
        explicit = [row for row in rows if row.get("is_canonical") is True]
        if any(
            "is_canonical" in row and not isinstance(row["is_canonical"], bool)
            for row in rows
        ) or len(explicit) > 1:
            raise _contract_error(
                "import.error.generic", exc="ambiguous canonical lab marker"
            )
        if explicit:
            winner = explicit[0]
        else:
            winner = max(
                rows,
                key=lambda row: (
                    str(row.get("updated_at") or ""),
                    -(
                        row["id"]
                        if isinstance(row.get("id"), int)
                        and not isinstance(row.get("id"), bool)
                        else 0
                    ),
                ),
            )
        for row in rows:
            row["is_canonical"] = row is winner
        canonical_names[key] = str(winner["name"])

    # Migration 0077 walks historical results by primary key and lets the first
    # spelling establish presentation when no catalog row exists.  Archives can
    # arrive in any JSON list order, so repeat that rule explicitly here: lowest
    # integer id first, with archive order as the deterministic fallback for an
    # older/non-standard row whose id has not reached structural validation yet.
    ordered_results = sorted(
        enumerate(results),
        key=lambda item: (
            item[1].get("id")
            if isinstance(item[1].get("id"), int)
            and not isinstance(item[1].get("id"), bool)
            else _POSTGRES_INTEGER_MAX + 1,
            item[0],
        ),
    )
    for _, row in ordered_results:
        marker = row["marker"]
        key = row.get("marker_key") or normalize_marker_key(marker)
        canonical_names.setdefault(key, marker)

    for row in results:
        marker = row.get("marker")
        if not isinstance(marker, str) or not normalize_marker_key(marker):
            raise _contract_error("import.error.generic", exc="invalid lab result marker")
        original = row.get("marker_original", marker)
        key = row.get("marker_key") or normalize_marker_key(marker)
        if (
            not isinstance(original, str)
            or not isinstance(key, str)
            or key != normalize_marker_key(original)
        ):
            raise _contract_error(
                "import.error.generic", exc="unresolved lab marker identity"
            )
        row["marker_original"] = original
        row["marker_key"] = key
        row["marker"] = canonical_names.get(key, marker)

    upgraded["lab_markers"] = markers
    upgraded["lab_results"] = results
    return upgraded

# Cross-surface ownership and private-resource plumbing.  These fields are set by
# trusted tenant/storage boundaries, not transported by v1 backups, generic MCP
# responses, or the digest pasted into an LLM.  Keep this an explicit
# deny-by-name policy: a suffix rule such as ``*_id`` would also erase useful
# vendor/business identifiers (for example ``exercise_template_id``).
GENERIC_OUTPUT_SUPPRESSED_COLUMNS = frozenset(
    {
        "subject_id",
        "actor_user_id",
        "created_by_user_id",
        "revoked_by_user_id",
        "overridden_by_user_id",
        "resolved_by_user_id",
        "recipient_user_id",
        "requested_by_user_id",
        "integration_connection_id",
        "ai_invocation_id",
        "delivery_intent_id",
        "file_asset_id",
        "uploaded_by_user_id",
        "credential_ref",
        "storage_ref",
        "opaque_key",
    }
)


def _v1_required_suppressed_columns(table: Any) -> tuple[str, ...]:
    """Return required ownership/resource columns v1 cannot reconstruct.

    Personal v1 archives deliberately suppress tenant identities and private
    resource locators.  The subject itself is the one exception: import binds
    it from the authenticated boundary.  Any other suppressed ``NOT NULL``
    column without a database default makes a carried row impossible to insert.

    Derive this from the reviewed portability registry and live SQLAlchemy
    schema so a future required actor, connection, or file root fails closed
    without another table-name denylist.
    """

    spec = OWNERSHIP_REGISTRY.get(table.name)
    if spec is None or not spec.user_portable or "subject_id" not in table.columns:
        return ()
    return tuple(
        column.name
        for column in table.columns
        if column.name != "subject_id"
        and column.name in GENERIC_OUTPUT_SUPPRESSED_COLUMNS
        and not column.nullable
        and column.default is None
        and column.server_default is None
    )


def _refuse_unrestorable_v1_rows(table: Any, rows: Sequence[Any]) -> None:
    """Refuse a non-empty personal section v1 cannot faithfully reload."""

    required = _v1_required_suppressed_columns(table)
    if rows and required:
        raise _contract_error(
            "portability.error.v1_unportable_reference",
            table=table.name,
            column=required[0],
        )


# Backup v1 is intentionally single-subject and never transports a subject UUID.
# This row-level marker preserves the distinction between a subject-bound row and
# a legitimate global row in mixed/optional tables without making a local UUID
# portable.  The name is reserved and accepted from imports only as a real bool.
_SUBJECT_BOUND_MARKER = "_vitals_subject_bound"

# The reviewed ownership registry is the source of truth in both directions.
# A newly added table cannot drift into a backup merely because this service's
# generic metadata walk discovered it before a hand-maintained denylist did.
_EXCLUDED_TABLES = frozenset(
    table_name
    for table_name, spec in OWNERSHIP_REGISTRY.items()
    if not spec.user_portable
)

# These retained control-plane tables have RESTRICT provenance FKs into the
# portable raw lake.  Backup v1 cannot replace those roots without either
# deleting durable control state or silently rebinding it to different payloads.
_RETAINED_RAW_REFERENCE_TABLES = (
    "ai_invocations",
    "notification_delivery_intents",
)

_LABELED_TABLES = (
    "weight_logs", "body_measurements", "progress_photos", "hevy_workouts",
    "garmin_daily", "garmin_activities", "lab_results", "glp1_injections",
    "glp1_side_effects", "meal_logs", "supplements", "genetic_variants",
    "skincare_logs", "weekly_digests", "annotations",
    "hrt_doses", "hrt_cycles", "hrt_side_effects",
    "body_scans", "milestones", "noise_markers",
)


class PortabilityError(Exception):
    """Raised on a malformed/invalid backup file. The router turns it into a clean
    HTTP 400 (never a silent failure or a leaked DB error)."""


class MultiSubjectBackupError(PortabilityError):
    """The installation holds several subjects; format v1 describes one.

    Separated from the rest because it says something different. Every other
    :class:`PortabilityError` is about the file — malformed, mislabelled, a
    reference that goes nowhere — and the answer is to fix the file. This one is
    about the installation, the file is fine, and the answer is a different
    export. A router cannot tell those apart from a translated string, so the
    distinction lives in the type rather than in prose.
    """


def _contract_error(message_key: str, **params: Any) -> PortabilityError:
    """Build a localized backup-v1 contract violation."""

    return PortabilityError(t(message_key, **params))


async def _single_local_subject_id(session: AsyncSession) -> Any | None:
    """Return the sole local subject, or ``None`` for a legacy/pre-bootstrap DB.

    Some compatibility tests and pre-identity databases do not have the identity
    table at all, so probe the schema before selecting.  Reading at most two rows
    is sufficient: backup v1 cannot safely represent a multi-subject database and
    must fail before export reads or import mutation begins.
    """

    subject_table = Base.metadata.tables.get("health_subjects")
    if subject_table is None:
        return None

    connection = await session.connection()
    has_subject_table = await connection.run_sync(
        lambda sync_connection: inspect(sync_connection).has_table(
            subject_table.name
        )
    )
    if not has_subject_table:
        return None

    subject_ids = tuple(
        await session.scalars(select(subject_table.c.id).limit(2))
    )
    if len(subject_ids) > 1:
        raise MultiSubjectBackupError(t("portability.error.v1_multi_subject"))
    return subject_ids[0] if subject_ids else None


def _subject_marker(row: dict[str, Any]) -> bool:
    """Read a marker already shape-validated by :func:`_validate_payload`."""

    return row.get(_SUBJECT_BOUND_MARKER) is True


def _subject_rebind_required(table_name: str, row: dict[str, Any]) -> bool:
    """Whether one imported row must bind to the authoritative local subject."""

    spec = OWNERSHIP_REGISTRY[table_name]
    if spec.subject is TargetColumn.REQUIRED:
        return True
    if spec.ownership is OwnershipClass.SUBJECT_CHILD:
        return True
    if (
        spec.subject in {TargetColumn.MIXED, TargetColumn.OPTIONAL}
        or spec.ownership is OwnershipClass.MIXED_CATALOG_CHILD
    ):
        return _subject_marker(row)
    raise _contract_error(
        "portability.error.v1_unknown_subject_policy", table=table_name
    )


class BackupMetadata(BaseModel):
    """The backup envelope for the one format this importer implements.

    Extra metadata keys remain forward-compatible, but the format version is an
    exact contract: a v1 importer must never guess how to interpret another
    version's table graph.
    """

    model_config = ConfigDict(extra="ignore")

    version: str
    kind: str | None = None
    exported_at: str | None = None
    timezone: str | None = None


@dataclass
class ImportStats:
    """Per-table row counts of what was loaded, for the success message."""

    counts: dict[str, int]

    def total(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        parts: list[str] = []
        leftover = 0
        for table, count in self.counts.items():
            if count <= 0:
                continue
            if table in _LABELED_TABLES:
                parts.append(f"{count} {t('import.label.' + table)}")
            else:
                leftover += count
        if leftover:
            parts.append(t("import.summary_extra", n=leftover))
        if not parts:
            return t("import.summary_empty")
        return t("import.summary_prefix") + ", ".join(parts) + "."


# ── Value (de)serialization ────────────────────────────────────────────────────


def _serialize_value(value: Any) -> Any:
    """ORM value → JSON-safe value. ISO strings for temporals, float for Decimal;
    dicts/lists (JSONB) and scalars pass through."""
    # datetime must be checked before date (datetime subclasses date).
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _deserialize_value(col_type: Any, value: Any) -> Any:
    """JSON value → ORM value, driven by the column's SQLAlchemy type. Only temporal
    columns need coercing back from ISO strings; JSON/bool/number/text pass through."""
    if value is None:
        return None
    if isinstance(col_type, DateTime):
        return datetime.fromisoformat(value) if isinstance(value, str) else value
    if isinstance(col_type, Date):
        return date.fromisoformat(value) if isinstance(value, str) else value
    if isinstance(col_type, Time):
        return time.fromisoformat(value) if isinstance(value, str) else value
    return value


def _is_secret_setting_key(key: str) -> bool:
    low = str(key).lower()
    return any(marker in low for marker in _SECRET_KEY_MARKERS)


# ── Full backup: export ────────────────────────────────────────────────────────


async def _live_schema_columns(
    session: AsyncSession,
) -> Mapping[str, frozenset[str]]:
    """Return the tables and columns installed in this database.

    Full-v1 is also the safety snapshot used by the staged ownership cutover.
    During that cutover the application can legitimately be newer than the
    deliberately paused schema. Reflecting once lets that path capture the
    complete installed shape without selecting columns from a later migration.
    """

    connection = await session.connection()

    def inspect_columns(sync_connection: Any) -> dict[str, frozenset[str]]:
        inspector = inspect(sync_connection)
        return {
            table_name: frozenset(
                column["name"] for column in inspector.get_columns(table_name)
            )
            for table_name in inspector.get_table_names()
        }

    return MappingProxyType(await connection.run_sync(inspect_columns))


async def export_full(session: AsyncSession) -> dict[str, Any]:
    """Snapshot portable tables into ``{table_name: [rows]}`` plus metadata.

    Tables are walked in FK order (``sorted_tables``); ``app_settings`` secret-ish
    rows and ownership/private-resource plumbing are dropped. The result is a
    plain dict ready for ``json.dumps``.
    """
    local_subject_id = await _single_local_subject_id(session)
    live_schema = await _live_schema_columns(session)
    out: dict[str, Any] = {
        "metadata": {
            "version": BACKUP_VERSION,
            "kind": KIND_FULL,
            "exported_at": now_local().isoformat(timespec="seconds"),
            "timezone": os.getenv("VITALS_TIMEZONE", "Europe/Chisinau"),
        }
    }

    for table in Base.metadata.sorted_tables:
        if table.name in _EXCLUDED_TABLES or table.name not in live_schema:
            continue
        installed_columns = live_schema[table.name]
        selected_columns = tuple(
            column for column in table.columns if column.name in installed_columns
        )
        result = await session.execute(select(*selected_columns))
        column_names = [
            name
            for name in table.columns.keys()
            if name in installed_columns
            if name not in GENERIC_OUTPUT_SUPPRESSED_COLUMNS
        ]
        rows: list[dict[str, Any]] = []
        for mapping in result.mappings().all():
            if table.name == "app_settings" and _is_secret_setting_key(mapping.get("key")):
                continue
            row = {col: _serialize_value(mapping[col]) for col in column_names}
            if "subject_id" in installed_columns:
                subject_bound = mapping["subject_id"] is not None
                if local_subject_id is None and subject_bound:
                    raise _contract_error("portability.error.v1_missing_subject")
                row[_SUBJECT_BOUND_MARKER] = subject_bound
            rows.append(row)
        out[table.name] = rows

    return out


#: What a subject export is, so the whole-database importer can tell them apart.
#: Feeding one to the full-v1 restore coordinator would wipe every table for
#: everybody and then
#: restore one person into the hole — a plausible mistake with an implausible
#: blast radius, so the two shapes are named rather than merely shaped.
KIND_SUBJECT = "subject_export"


#: Where one exported row points at another. Derived from the schema rather than
#: listed, for the same reason the table walk is: a reference added later must
#: not silently become an id from one installation pasted into another.
#:
#: ``subject_id`` legs of composite foreign keys are excluded — the subject is
#: assigned by the boundary on import, never carried, so it is not a reference
#: that needs resolving.
def _portable_references() -> Mapping[str, Mapping[str, str]]:
    references: dict[str, dict[str, str]] = {}
    for table in Base.metadata.sorted_tables:
        if table.name in _EXCLUDED_TABLES or "subject_id" not in table.columns:
            continue
        for foreign_key in table.foreign_keys:
            column = foreign_key.parent.name
            target = foreign_key.column.table.name
            if column == "subject_id" or target == table.name:
                continue
            if target in _EXCLUDED_TABLES or "subject_id" not in (
                Base.metadata.tables[target].columns
            ):
                continue
            references.setdefault(table.name, {})[column] = target
    return MappingProxyType(
        {name: MappingProxyType(columns) for name, columns in references.items()}
    )


PORTABLE_REFERENCES = _portable_references()

#: A reference can point at a row a personal export does not carry: the
#: installation's shared catalog, which lives under a NULL subject and is seeded
#: by the receiver's own migrations. An id would be meaningless there — two
#: installations number their catalogs independently — so those references
#: travel as the target's natural key and are resolved on arrival.
CATALOG_NATURAL_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "hrt_compounds": "key",
    }
)

#: Reserved row key carrying the natural keys above. Same reservation rule as
#: the subject marker: accepted only in the shape this module writes.
_REFERENCE_MARKER = "_vitals_refs"


async def export_subject(
    session: AsyncSession, *, subject_id: Any
) -> dict[str, Any]:
    """Snapshot exactly one subject's portable rows, and nothing else.

    Different question from :func:`export_full`, which answers "what is in this
    installation". This one answers "what is mine", and the difference is not
    only a ``WHERE`` clause:

    ``app_settings`` is left out entirely. It is the installation's
    configuration, not a person's — the timezone the scheduler runs on, the
    modules that are switched on for the deployment. Carrying it in a personal
    export would make the file a way to reconfigure whatever imports it.

    Rows with a NULL subject are left out for the same reason. In the mixed
    tables those are the installation's curated catalog — the safety rules, the
    compound reference data — which the receiving installation has its own copy
    of, seeded by its own migrations. Including them would let a personal export
    overwrite somebody's safety catalog.

    What remains is the subject's own rows, with ownership and private-resource
    columns suppressed exactly as the full backup suppresses them: those are
    assigned by a trusted boundary on the way back in, never read from a file.
    """

    if subject_id is None:
        raise _contract_error("portability.error.v1_missing_subject")

    out: dict[str, Any] = {
        "metadata": {
            "version": BACKUP_VERSION,
            "kind": KIND_SUBJECT,
            "exported_at": now_local().isoformat(timespec="seconds"),
            "timezone": os.getenv("VITALS_TIMEZONE", "Europe/Chisinau"),
        }
    }

    carried: dict[str, set[Any]] = {}
    for table in Base.metadata.sorted_tables:
        if table.name in _EXCLUDED_TABLES:
            continue
        if "subject_id" not in table.columns:
            # Installation configuration, not this person's record.
            continue
        result = await session.execute(
            select(table).where(table.c.subject_id == subject_id)
        )
        column_names = [
            name
            for name in table.columns.keys()
            if name not in GENERIC_OUTPUT_SUPPRESSED_COLUMNS
        ]
        mappings = result.mappings().all()
        _refuse_unrestorable_v1_rows(table, mappings)
        carried[table.name] = {mapping["id"] for mapping in mappings}
        out[table.name] = [
            {col: _serialize_value(mapping[col]) for col in column_names}
            for mapping in mappings
        ]

    await _describe_outbound_references(session, out, carried)
    return out


async def _describe_outbound_references(
    session: AsyncSession,
    snapshot: dict[str, Any],
    carried: dict[str, set[Any]],
) -> None:
    """Give every reference that leaves the file a name it can be found by.

    Inside the file an id is fine: the importer renumbers both ends together.
    A reference *out* of it is a different thing — it points at the
    installation's shared catalog, which the receiving installation seeded for
    itself and numbered its own way. Carrying the integer would either dangle or,
    worse, land on an unrelated row that happens to hold that number.

    So those travel as the target's natural key. A reference that is neither in
    the file nor resolvable to a natural key is refused here rather than written
    out to fail on arrival, where the person holding the file can do nothing
    about it.
    """

    for table_name, columns in PORTABLE_REFERENCES.items():
        rows = snapshot.get(table_name)
        if not rows:
            continue
        for row in rows:
            descriptors: dict[str, Any] = {}
            for column, target in columns.items():
                value = row.get(column)
                if value is None or value in carried.get(target, ()):
                    continue
                natural_key = CATALOG_NATURAL_KEYS.get(target)
                if natural_key is None:
                    raise _contract_error(
                        "portability.error.v1_unportable_reference",
                        table=table_name,
                        column=column,
                    )
                target_table = Base.metadata.tables[target]
                key_value = await session.scalar(
                    select(target_table.c[natural_key]).where(
                        target_table.c.id == value
                    )
                )
                if key_value is None:
                    raise _contract_error(
                        "portability.error.v1_unportable_reference",
                        table=table_name,
                        column=column,
                    )
                descriptors[column] = {"table": target, "key": key_value}
                # The id is local to this installation and means nothing
                # elsewhere; the descriptor replaces it rather than joining it.
                row[column] = None
            if descriptors:
                row[_REFERENCE_MARKER] = descriptors


# ── Full backup: import (replace) ──────────────────────────────────────────────


def _validate_payload(payload: Any) -> BackupMetadata:
    """Structural validation. Raises :class:`PortabilityError` with a clear message
    on anything malformed — no silent acceptance of junk."""
    meta = _validate_v1_metadata(payload)

    # A subject export and a whole-database backup are both valid JSON with the
    # same envelope and overlapping table names, and this importer replaces
    # every portable table for everybody. Loading one as the other would empty
    # the database and put one person back into it. The kinds are therefore
    # checked rather than inferred — an older file with no kind at all is still
    # accepted, because that is what a v1 backup looks like.
    if meta.kind == KIND_SUBJECT:
        raise _contract_error("portability.error.v1_subject_export_is_not_a_backup")

    known = set(Base.metadata.tables.keys())
    for key, value in payload.items():
        if key == "metadata":
            continue
        if key not in known:
            raise PortabilityError(t("import.error.unknown_table", key=key))
        if not isinstance(value, list):
            raise PortabilityError(t("import.error.not_list", key=key))
        for i, item in enumerate(value):
            if not isinstance(item, dict):
                raise PortabilityError(t("import.error.not_object", i=i, key=key))
            if _SUBJECT_BOUND_MARKER in item:
                marker = item[_SUBJECT_BOUND_MARKER]
                table = Base.metadata.tables[key]
                if (
                    key in _EXCLUDED_TABLES
                    or "subject_id" not in table.columns
                    or type(marker) is not bool
                ):
                    raise _contract_error(
                        "portability.error.v1_bad_marker",
                        i=i,
                        table=key,
                    )
    return meta


def _raw_replacement_snapshot_bounds(payload: dict[str, Any]) -> tuple[int, int]:
    """Validate portable raw PKs and return the immutable snapshot bounds."""

    raw_rows = payload.get("raw_payloads") or ()
    high_watermark = 0
    for index, row in enumerate(raw_rows):
        raw_id = row.get("id")
        if (
            not isinstance(raw_id, int)
            or isinstance(raw_id, bool)
            or not 1 <= raw_id <= _POSTGRES_INTEGER_MAX
        ):
            raise _contract_error(
                "import.error.generic",
                exc=(
                    "raw_payloads record "
                    f"#{index} must carry a positive integer id within the "
                    "PostgreSQL INTEGER range"
                ),
            )
        high_watermark = max(high_watermark, raw_id)
    return high_watermark, len(raw_rows)


def _normalized_manual_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3B table bounds before any restore mutation."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


def _hrt_child_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3C child bounds before any restore mutation."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


def _provider_raw_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3D provider-table bounds before restore mutation."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


def _hevy_child_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3E Hevy-child bounds before restore mutation."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


def _hrt_compound_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3F mixed HRT catalog bounds before replacement."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


def _conflict_rule_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3G conflict-rule bounds before replacement."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


def _progress_photo_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3H progress-photo bounds before replacement."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


def _weight_log_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3L weight-log bounds before replacement."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


def _lab_result_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3M lab-result bounds before replacement."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


def _genetic_variant_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3N genetic-variant bounds before replacement."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


def _body_scan_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3O body-scan bounds before replacement."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


def _body_scan_metric_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3P body-scan metric bounds before replacement."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


def _garmin_weight_export_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3Q Garmin outbox bounds before replacement."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


def _system_alert_replacement_snapshot_bounds(
    payload: dict[str, Any],
    *,
    table_names: tuple[str, ...],
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3T system-alert bounds before replacement."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in table_names:
        rows = payload.get(table_name) or ()
        high_watermark = 0
        for index, row in enumerate(rows):
            row_id = row.get("id")
            if (
                not isinstance(row_id, int)
                or isinstance(row_id, bool)
                or not 1 <= row_id <= _POSTGRES_INTEGER_MAX
            ):
                raise _contract_error(
                    "import.error.generic",
                    exc=(
                        f"{table_name} record #{index} must carry a positive "
                        "integer id within the PostgreSQL INTEGER range"
                    ),
                )
            high_watermark = max(high_watermark, row_id)
        bounds[table_name] = (high_watermark, len(rows))
    return bounds


async def _refuse_retained_raw_references(
    session: AsyncSession,
    *,
    live_schema: Mapping[str, frozenset[str]] | None = None,
) -> None:
    """Fail before mutation when retained control state still binds any raw."""

    if live_schema is None:
        live_schema = await _live_schema_columns(session)
    for table_name in _RETAINED_RAW_REFERENCE_TABLES:
        if table_name not in live_schema:
            continue
        table = Base.metadata.tables[table_name]
        has_reference = await session.scalar(
            select(
                select(1)
                .select_from(table)
                .where(table.c.raw_payload_id.is_not(None))
                .exists()
            )
        )
        if has_reference:
            raise _contract_error(
                "import.error.generic",
                exc=(
                    "raw replacement is blocked by retained "
                    "control-plane provenance"
                ),
            )


async def _secret_settings(session: AsyncSession) -> list[dict[str, Any]]:
    """The ``app_settings`` rows a backup never carries, read back verbatim so the
    restore can put them down again after the wipe.

    Same predicate as the exporter, deliberately: one list of markers, checked in
    Python. Expressed as a SQL ``LIKE`` per marker it would be a second copy, and
    the day a marker is added the two halves would quietly disagree.
    """
    table = Base.metadata.tables["app_settings"]
    result = await session.execute(select(table))
    return [
        dict(mapping)
        for mapping in result.mappings().all()
        if _is_secret_setting_key(mapping.get("key"))
    ]


async def _import_full_with_ownership_hooks(
    session: AsyncSession,
    payload: Any,
    *,
    hooks: PortabilityV1OwnershipHooks,
    live_schema: Mapping[str, frozenset[str]] | None = None,
) -> ImportStats:
    """Replace portable data with the file's contents, in the caller's transaction.

    Deletes every portable table (children first), reloads each present portable
    table preserving primary keys (parents first), then fixes Postgres identity
    sequences. Only ``flush`` — the router commits, so any raised error rolls
    everything back.

    **What the export refuses to write, the import refuses to delete or accept.**
    ``export_full`` drops secret-ish ``app_settings`` keys, so a wipe-and-reload
    would silently lose exactly the rows it was protecting — today that is the 2FA
    secret, and losing it turns the second factor off with no code presented and
    nothing on screen to say so. Those rows are therefore carried across the wipe,
    and rows with the same keys arriving *in* the file are ignored: a backup must
    not be a way to plant a credential either.

    ``_EXCLUDED_TABLES`` follows the same rule from the other side: those tables
    are neither wiped nor loaded, so a restore cannot republish a revoked report,
    replace its authorizing owner, plant roles or support access, or rewrite the
    audit trail.
    """
    _validate_payload(payload)
    if live_schema is None:
        live_schema = await _live_schema_columns(session)
    payload = _upgrade_lab_marker_identity(payload)
    raw_high_watermark, raw_snapshot_rows = _raw_replacement_snapshot_bounds(
        payload
    )
    normalized_snapshot_bounds = _normalized_manual_replacement_snapshot_bounds(
        payload, table_names=hooks.table_groups["normalized"]
    )
    hrt_child_snapshot_bounds = _hrt_child_replacement_snapshot_bounds(
        payload, table_names=hooks.table_groups["hrt_child"]
    )
    provider_raw_snapshot_bounds = _provider_raw_replacement_snapshot_bounds(
        payload, table_names=hooks.table_groups["provider_raw"]
    )
    hevy_child_snapshot_bounds = _hevy_child_replacement_snapshot_bounds(
        payload, table_names=hooks.table_groups["hevy_child"]
    )
    hrt_compound_snapshot_bounds = _hrt_compound_replacement_snapshot_bounds(
        payload, table_names=hooks.table_groups["hrt_compound"]
    )
    conflict_rule_snapshot_bounds = _conflict_rule_replacement_snapshot_bounds(
        payload, table_names=hooks.table_groups["conflict_rule"]
    )
    progress_photo_snapshot_bounds = _progress_photo_replacement_snapshot_bounds(
        payload, table_names=hooks.table_groups["progress_photo"]
    )
    weight_log_snapshot_bounds = _weight_log_replacement_snapshot_bounds(
        payload, table_names=hooks.table_groups["weight_log"]
    )
    lab_result_snapshot_bounds = _lab_result_replacement_snapshot_bounds(
        payload, table_names=hooks.table_groups["lab_result"]
    )
    genetic_variant_snapshot_bounds = _genetic_variant_replacement_snapshot_bounds(
        payload, table_names=hooks.table_groups["genetic_variant"]
    )
    body_scan_snapshot_bounds = _body_scan_replacement_snapshot_bounds(
        payload, table_names=hooks.table_groups["body_scan"]
    )
    body_scan_metric_snapshot_bounds = (
        _body_scan_metric_replacement_snapshot_bounds(
            payload, table_names=hooks.table_groups["body_scan_metric"]
        )
    )
    garmin_weight_export_snapshot_bounds = (
        _garmin_weight_export_replacement_snapshot_bounds(
            payload, table_names=hooks.table_groups["garmin_weight_export"]
        )
    )
    system_alert_snapshot_bounds = _system_alert_replacement_snapshot_bounds(
        payload, table_names=hooks.table_groups["system_alert"]
    )

    try:
        # Freeze identity before deriving the local subject and keep governance
        # through every provenance preflight and replacement mutation.  This
        # closes both zero-subject/bootstrap and retained-reference writer races;
        # the block service's re-acquisition is transaction-reentrant.
        await acquire_identity_governance_lock(session)
        local_subject_id = await _single_local_subject_id(session)
        if local_subject_id is None:
            has_bound_marker = any(
                _subject_marker(row)
                for table_name, rows in payload.items()
                if table_name != "metadata"
                and table_name not in _EXCLUDED_TABLES
                and "subject_id" in Base.metadata.tables[table_name].columns
                for row in rows
            )
            if has_bound_marker:
                raise _contract_error("portability.error.v1_missing_subject")
        await _refuse_retained_raw_references(session, live_schema=live_schema)
        if local_subject_id is not None:
            try:
                await hooks.block_raw(
                    session,
                    scan_high_watermark_id=raw_high_watermark,
                    snapshot_rows=raw_snapshot_rows,
                )
            except hooks.raw_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="raw ownership restore block was rejected",
                ) from exc
            try:
                await hooks.reset_normalized(
                    session,
                    snapshot_bounds=normalized_snapshot_bounds,
                )
            except hooks.normalized_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="normalized ownership restore reset was rejected",
                ) from exc
            try:
                await hooks.reset_hrt_child(
                    session,
                    snapshot_bounds=hrt_child_snapshot_bounds,
                )
            except hooks.hrt_child_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="HRT child ownership restore reset was rejected",
                ) from exc
            try:
                await hooks.block_provider_raw(
                    session,
                    snapshot_bounds=provider_raw_snapshot_bounds,
                )
            except hooks.provider_raw_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="provider ownership restore block was rejected",
                ) from exc
            try:
                await hooks.block_hevy_child(
                    session,
                    snapshot_bounds=hevy_child_snapshot_bounds,
                )
            except hooks.hevy_child_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="Hevy child ownership restore block was rejected",
                ) from exc
            try:
                await hooks.reset_hrt_compound(
                    session,
                    snapshot_bounds=hrt_compound_snapshot_bounds,
                )
            except hooks.hrt_compound_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="HRT compound ownership restore reset was rejected",
                ) from exc
            try:
                await hooks.reset_conflict_rule(
                    session,
                    snapshot_bounds=conflict_rule_snapshot_bounds,
                )
            except hooks.conflict_rule_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="conflict-rule ownership restore reset was rejected",
                ) from exc
            try:
                await hooks.block_progress_photo(
                    session,
                    snapshot_bounds=progress_photo_snapshot_bounds,
                )
            except hooks.progress_photo_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="progress-photo ownership restore block was rejected",
                ) from exc
            try:
                await (
                    hooks.prepare_shared_report(
                        session
                    )
                )
            except hooks.shared_report_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="shared-report ownership restore preparation was rejected",
                ) from exc
            try:
                await hooks.reset_weight_log(
                    session,
                    snapshot_bounds=weight_log_snapshot_bounds,
                )
            except hooks.weight_log_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="weight-log ownership restore reset was rejected",
                ) from exc
            try:
                await hooks.reset_lab_result(
                    session,
                    snapshot_bounds=lab_result_snapshot_bounds,
                )
            except hooks.lab_result_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="lab-result ownership restore reset was rejected",
                ) from exc
            try:
                await (
                    hooks.reset_genetic_variant(
                        session,
                        snapshot_bounds=genetic_variant_snapshot_bounds,
                    )
                )
            except hooks.genetic_variant_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="genetic-variant ownership restore reset was rejected",
                ) from exc
            try:
                await hooks.block_body_scan(
                    session,
                    snapshot_bounds=body_scan_snapshot_bounds,
                )
            except hooks.body_scan_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="body-scan ownership restore block was rejected",
                ) from exc
            try:
                await (
                    hooks.reset_body_scan_metric(
                        session,
                        snapshot_bounds=body_scan_metric_snapshot_bounds,
                    )
                )
            except hooks.body_scan_metric_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="body-scan metric ownership restore reset was rejected",
                ) from exc
            try:
                await (
                    hooks.block_garmin_weight_export(
                        session,
                        snapshot_bounds=garmin_weight_export_snapshot_bounds,
                    )
                )
            except hooks.garmin_weight_export_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="Garmin outbox ownership restore block was rejected",
                ) from exc
            try:
                await (
                    hooks.prepare_weekly_digest(
                        session
                    )
                )
            except hooks.weekly_digest_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="weekly-digest ownership restore preparation was rejected",
                ) from exc
            try:
                await (
                    hooks.prepare_notification(
                        session
                    )
                )
            except hooks.notification_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="notification ownership restore preparation was rejected",
                ) from exc
            try:
                await hooks.reset_system_alert(
                    session,
                    snapshot_bounds=system_alert_snapshot_bounds,
                )
            except hooks.system_alert_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="system-alert ownership restore reset was rejected",
                ) from exc
        preserved = await _secret_settings(session)

        # Wipe in reverse FK order so child rows go before the parents they reference.
        for table in reversed(Base.metadata.sorted_tables):
            if table.name in _EXCLUDED_TABLES or table.name not in live_schema:
                continue
            await session.execute(table.delete())

        counts: dict[str, int] = {}
        # Reload in FK order, preserving ids and business columns. Ownership and
        # private-resource references are assigned by a trusted tenancy-aware
        # boundary, never accepted from a portable v1 file.
        for table in Base.metadata.sorted_tables:
            if table.name in _EXCLUDED_TABLES or table.name not in live_schema:
                continue
            rows = payload.get(table.name)
            if table.name == "app_settings":
                rows = [r for r in rows or () if not _is_secret_setting_key(r.get("key"))]
            if not rows:
                continue
            columns = table.columns
            installed_columns = live_schema[table.name]
            records = [
                {
                    key: _deserialize_value(columns[key].type, val)
                    for key, val in row.items()
                    if key in columns  # tolerate columns dropped in a later schema
                    if key in installed_columns
                    if key not in GENERIC_OUTPUT_SUPPRESSED_COLUMNS
                }
                for row in rows
            ]
            if "subject_id" in installed_columns and local_subject_id is not None:
                for row, record in zip(rows, records, strict=True):
                    record["subject_id"] = (
                        local_subject_id
                        if _subject_rebind_required(table.name, row)
                        else None
                    )
            # Use a lightweight physical table for staged-schema restores.
            # Inserting through the newer ORM Table would run Python defaults
            # for columns that do not exist yet in the paused revision.
            insert_target = sql_table(
                table.name,
                *(
                    sql_column(item.name, item.type)
                    for item in table.columns
                    if item.name in installed_columns
                ),
                schema=table.schema,
            )
            await session.execute(insert_target.insert(), records)
            counts[table.name] = len(records)

        if preserved:
            await session.execute(Base.metadata.tables["app_settings"].insert(), preserved)

        # Explicit portable IDs are loaded before current checked-in catalog rows.
        # Advance PostgreSQL sequences first so catalog insertion cannot collide,
        # then validate the complete current safety catalog in this same restore
        # transaction.  A stale/omitted catalog is upgraded atomically; protected
        # code collisions or retained orphan alert references roll everything back.
        await _reset_sequences(session, live_schema=live_schema)
        try:
            await sync_conflict_catalog(session)
            if local_subject_id is not None:
                await hooks.preflight_conflict_rule(session)
        except hooks.conflict_rule_error as exc:
            raise _contract_error(
                "import.error.generic",
                exc="conflict-rule catalog validation rejected the portable restore",
            ) from exc
        if local_subject_id is not None:
            try:
                await hooks.preflight_progress_photo(session)
            except hooks.progress_photo_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="progress-photo validation rejected the portable restore",
                ) from exc
            try:
                await hooks.preflight_shared_report(session)
            except hooks.shared_report_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="shared-report validation rejected the portable restore",
                ) from exc
            try:
                await hooks.preflight_weight_log(session)
            except hooks.weight_log_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="weight-log validation rejected the portable restore",
                ) from exc
            try:
                await hooks.preflight_lab_result(session)
            except hooks.lab_result_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="lab-result validation rejected the portable restore",
                ) from exc
            try:
                await hooks.preflight_genetic_variant(session)
            except hooks.genetic_variant_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="genetic-variant validation rejected the portable restore",
                ) from exc
            try:
                await hooks.preflight_body_scan(session)
            except hooks.body_scan_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="body-scan validation rejected the portable restore",
                ) from exc
            try:
                await hooks.preflight_body_scan_metric(session)
            except hooks.body_scan_metric_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="body-scan metric validation rejected the portable restore",
                ) from exc
            try:
                await hooks.preflight_garmin_weight_export(session)
            except hooks.garmin_weight_export_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="Garmin outbox validation rejected the portable restore",
                ) from exc
            try:
                await hooks.preflight_weekly_digest(session)
            except hooks.weekly_digest_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="weekly-digest validation rejected the portable restore",
                ) from exc
            try:
                await hooks.preflight_notification(session)
            except hooks.notification_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="notification validation rejected the portable restore",
                ) from exc
            try:
                await hooks.preflight_system_alert(session)
            except hooks.system_alert_error as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="system-alert validation rejected the portable restore",
                ) from exc
        await _reset_sequences(session, live_schema=live_schema)
        await session.flush()
    except PortabilityError:
        raise
    except SQLAlchemyError as exc:
        # Surface a clean message instead of a raw driver error; the transaction is
        # rolled back by the router's session dependency.
        raise PortabilityError(
            t(
                "import.error.generic",
                exc="database rejected the portable restore",
            )
        ) from exc

    return ImportStats(counts=counts)


async def _reset_sequences(
    session: AsyncSession,
    *,
    live_schema: Mapping[str, frozenset[str]] | None = None,
) -> None:
    """Advance sequences for restored portable tables; no-op on SQLite."""
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    if live_schema is None:
        live_schema = await _live_schema_columns(session)
    for table in Base.metadata.sorted_tables:
        if (
            table.name in _EXCLUDED_TABLES
            or table.name not in live_schema
            or "id" not in live_schema[table.name]
        ):
            continue
        seq = (
            await session.execute(
                text("SELECT pg_get_serial_sequence(:tbl, 'id')"), {"tbl": table.name}
            )
        ).scalar()
        if not seq:
            continue
        max_id = (await session.execute(select(func.max(table.columns["id"])))).scalar()
        if max_id is not None:
            await session.execute(
                text("SELECT setval(:seq, :val, true)"), {"seq": seq, "val": int(max_id)}
            )


# ── Subject-scoped import ──────────────────────────────────────────────────────


def _validate_subject_payload(payload: Any) -> BackupMetadata:
    """Structural validation for a personal export, and only for one."""

    meta = _validate_v1_metadata(payload)
    if meta.kind != KIND_SUBJECT:
        # The mirror of the guard in ``_validate_payload``. A whole-database
        # backup loaded here would be silently truncated to one subject's worth
        # of itself, which looks like a successful restore and is not one.
        raise _contract_error("portability.error.v1_not_a_subject_export")

    for key, value in payload.items():
        if key == "metadata":
            continue
        table = Base.metadata.tables.get(key)
        if (
            table is None
            or key in _EXCLUDED_TABLES
            or "subject_id" not in table.columns
        ):
            raise PortabilityError(t("import.error.unknown_table", key=key))
        if not isinstance(value, list):
            raise PortabilityError(t("import.error.not_list", key=key))
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise PortabilityError(
                    t("import.error.not_object", i=index, key=key)
                )
            descriptors = item.get(_REFERENCE_MARKER)
            if descriptors is None:
                continue
            columns = PORTABLE_REFERENCES.get(key, {})
            if not isinstance(descriptors, dict) or any(
                column not in columns
                or not isinstance(descriptor, dict)
                or descriptor.get("table") != columns[column]
                or not isinstance(descriptor.get("key"), str)
                for column, descriptor in descriptors.items()
            ):
                raise _contract_error(
                    "portability.error.v1_bad_reference", table=key
                )
        _refuse_unrestorable_v1_rows(table, value)
    return meta


def _validate_v1_metadata(payload: Any) -> BackupMetadata:
    """Validate the common v1 envelope before an importer can mutate state."""

    if not isinstance(payload, dict):
        raise PortabilityError(t("import.error.not_json_obj"))
    if "metadata" not in payload:
        raise PortabilityError(t("import.error.no_metadata"))
    metadata = payload["metadata"]
    if isinstance(metadata, dict):
        version = metadata.get("version")
        if type(version) is not str or version != BACKUP_VERSION:
            raise _contract_error(
                "portability.error.v1_unsupported_version",
                expected=BACKUP_VERSION,
            )
    try:
        return BackupMetadata.model_validate(metadata)
    except ValidationError as exc:
        raise PortabilityError(
            t("import.error.bad_metadata", msg=exc.errors()[0].get("msg", exc))
        ) from exc


async def _resolve_catalog_reference(
    session: AsyncSession,
    *,
    target: str,
    key: str,
    subject_id: Any,
) -> Any:
    """Find the local row a travelling natural key names, or refuse.

    A travelling name always came from the installation's catalog: a reference
    to the subject's *own* row never leaves the file, because the export carries
    every row the subject owns and the importer renumbers both ends together. So
    the NULL-subject catalog is asked first, and answering with a personal row of
    the same name would silently re-point the reference at a different thing.

    The subject's own rows are the fallback rather than the answer, for the
    cross-installation case: the receiver organises its catalog its own way, the
    name is not in it, and the person recreated the entry themselves. That is the
    only reading left, and refusing it would make the file unimportable for a
    reason its holder could fix but not diagnose.
    """

    table = Base.metadata.tables[target]
    natural_key = CATALOG_NATURAL_KEYS[target]
    for scope in (table.c.subject_id.is_(None), table.c.subject_id == subject_id):
        found = await session.scalar(
            select(table.c.id).where(table.c[natural_key] == key, scope)
        )
        if found is not None:
            return found
    return None


async def import_subject(
    session: AsyncSession, payload: Any, *, subject_id: Any
) -> ImportStats:
    """Replace one subject's portable rows with the file's, and nobody else's.

    Different operation from the full-v1 restore coordinator in the one way
    that matters: the delete is scoped. A full restore empties every portable
    table and is
    correct only for a whole-database backup; running it per person would take
    the installation down to restore one record.

    Primary keys are *not* preserved, and that is forced rather than chosen.
    Every portable table here numbers its rows with an integer sequence, so one
    subject's row 5 and another's row 5 both exist; carrying ids across would
    collide with rows this operation is not allowed to touch. Rows are therefore
    inserted fresh and the references between them rewritten through a map built
    as each parent lands — which is why the walk is in foreign-key order.

    References that leave the file were resolved to a natural key on the way out
    and are looked up again here. One that does not resolve is refused: the
    alternative is dropping it, and a dose that quietly forgets which compound
    it was is worse than an import that did not happen.

    Flushes, never commits. The caller owns the transaction, so any refusal
    below leaves the subject exactly as it was.
    """

    _validate_subject_payload(payload)
    payload = _upgrade_lab_marker_identity(payload)
    if subject_id is None:
        raise _contract_error("portability.error.v1_missing_subject")

    await acquire_identity_governance_lock(session)

    scoped = [
        table
        for table in Base.metadata.sorted_tables
        if table.name not in _EXCLUDED_TABLES and "subject_id" in table.columns
    ]

    # Children first, so a row never outlives the parent it points at.
    for table in reversed(scoped):
        await session.execute(
            table.delete().where(table.c.subject_id == subject_id)
        )

    remapped: dict[str, dict[Any, Any]] = {}
    counts: dict[str, int] = {}

    # Parents first, so every reference has already been renumbered.
    for table in scoped:
        rows = payload.get(table.name)
        if not rows:
            continue
        references = PORTABLE_REFERENCES.get(table.name, {})
        columns = table.columns
        for row in rows:
            record = {
                key: _deserialize_value(columns[key].type, value)
                for key, value in row.items()
                if key in columns
                if key not in GENERIC_OUTPUT_SUPPRESSED_COLUMNS
                if key != "id"
            }
            for column, target in references.items():
                original = row.get(column)
                if original is not None:
                    resolved = remapped.get(target, {}).get(original)
                    if resolved is None:
                        raise _contract_error(
                            "portability.error.v1_unresolved_reference",
                            table=table.name,
                            column=column,
                            key=original,
                        )
                    record[column] = resolved
                    continue
                descriptor = (row.get(_REFERENCE_MARKER) or {}).get(column)
                if descriptor is None:
                    record[column] = None
                    continue
                found = await _resolve_catalog_reference(
                    session,
                    target=target,
                    key=descriptor["key"],
                    subject_id=subject_id,
                )
                if found is None:
                    raise _contract_error(
                        "portability.error.v1_unresolved_reference",
                        table=table.name,
                        column=column,
                        key=descriptor["key"],
                    )
                record[column] = found

            # Ownership is assigned here and nowhere else. A file cannot name
            # the subject it lands in, which is what stops one from landing in
            # somebody else's.
            record["subject_id"] = subject_id
            inserted = await session.execute(
                table.insert().values(**record).returning(table.c.id)
            )
            new_id = inserted.scalar_one()
            original_id = row.get("id")
            if original_id is not None:
                remapped.setdefault(table.name, {})[original_id] = new_id
            counts[table.name] = counts.get(table.name, 0) + 1

    await session.flush()
    await _reset_sequences(session)
    return ImportStats(counts)


# ── LLM export ─────────────────────────────────────────────────────────────────


def _llm_profile() -> dict[str, Any]:
    """Owner context (from .env) so the LLM reads the data with the right frame."""
    return {
        "height_cm": os.getenv("VITALS_HEIGHT_CM") or "190",
        "sex": os.getenv("VITALS_SEX") or "male",
        "age": os.getenv("VITALS_USER_AGE") or "18",
        "program": os.getenv("VITALS_USER_PROGRAM") or "",
        "goals": os.getenv("VITALS_USER_GOALS") or "",
        "timezone": os.getenv("VITALS_TIMEZONE", "Europe/Chisinau"),
        "exported_at": now_local().isoformat(timespec="seconds"),
        "units": {"weight": "kg", "distance": "m", "energy": "kcal"},
        "note": (
            "Экспорт данных здоровья одного пользователя (Vitals) для анализа LLM. "
            "Даты в ISO 8601, вес в кг. Это навигатор для поддержки решений, не врач."
        ),
    }


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    """Drop empty (None / "") fields so the digest stays terse for a chat window."""
    return {k: v for k, v in row.items() if v is not None and v != ""}


# Plumbing an LLM has no use for: ids, FK links and row bookkeeping.
_LLM_SKIP_COLUMNS = frozenset(
    {
        "id",
        "raw_payload_id",
        "raw_id",
        "weight_log_id",
        "domain",
        "source",
        "external_id",
        "created_at",
        "updated_at",
    }
) | GENERIC_OUTPUT_SUPPRESSED_COLUMNS


def _row_dump(obj: Any) -> dict[str, Any]:
    """Every mapped column of a row except the plumbing above.

    Used for the wide Garmin tables, where hand-listing fields meant two thirds of
    the captured metrics never reached the export — and a new column would have
    silently missed it too.
    """
    return _compact(
        {
            name: _serialize_value(getattr(obj, name))
            for name in obj.__table__.columns.keys()
            if name not in _LLM_SKIP_COLUMNS
        }
    )


def _row_within(row: dict[str, Any], since_iso: str) -> bool:
    """Keep a row that is not entirely older than ``since``.

    Three shapes go through here. A point in time (``date``) is compared directly.
    A period (``start_date``) survives on its ``end_date``, and an *open* period —
    a dose phase or cycle that started years ago and is running today — has no
    ``end_date`` at all, so it stays whatever its start says. Catalog rows
    (supplements, genetics, cycle templates) carry no date and always stay: a stack
    list is current state, not history.
    """
    end = row.get("end_date")
    if "start_date" in row:
        return end is None or end >= since_iso
    day = row.get("date")
    if day is None:
        return True
    return (end or day) >= since_iso


async def export_llm(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    domains: Sequence[str] | None = None,
    since: date | None = None,
) -> dict[str, Any]:
    """Curated, flat, secret-free digest grouped by domain — paste-into-chat ready.

    Both filters default to off, so the web download stays the whole history. The
    MCP tool narrows instead: a chat asking about this month should not be handed
    years of daily Garmin rows. ``domains`` names top-level blocks of the result
    (``weight_history``, ``biomarkers``, …); ``since`` drops rows that ended before
    that date.

    **The subject is mandatory, and every read below is filtered by it.** Twenty-two
    selects here had no subject at all: written when the installation held one
    person, correct then, and a cross-subject export the moment it held two. The
    MCP ``export_everything`` tool resolved a scope before calling this and then
    handed it nothing — so the whole lake came back, everybody's rows in one
    LLM-ready document, which is the worst possible shape for that mistake to
    take.

    No default, deliberately. An omittable scope is exactly what
    ``vitals/legacy_scope.py`` exists to keep out of this codebase, and this
    function is the reason why.
    """

    if not isinstance(subject_id, uuid.UUID) or subject_id.int == 0:
        raise PortabilityError("export_llm requires the subject it is about")
    out: dict[str, Any] = {"profile": _llm_profile()}

    # Weight — active rows only (superseded duplicates are noise for analysis).
    weights = (
        await session.execute(
            select(WeightLog).where(WeightLog.subject_id == subject_id).where(WeightLog.superseded.is_(False)).order_by(WeightLog.date)
        )
    ).scalars().all()
    out["weight_history"] = [
        _compact({"date": w.date.isoformat(), "weight_kg": w.weight_kg, "note": w.note})
        for w in weights
    ]

    measurements = (
        await session.execute(select(BodyMeasurement).where(BodyMeasurement.subject_id == subject_id).order_by(BodyMeasurement.date))
    ).scalars().all()
    out["body_measurements"] = [
        _compact(
            {
                "date": m.date.isoformat(),
                "waist_cm": m.waist_cm,
                "neck_cm": m.neck_cm,
                "hips_cm": m.hips_cm,
                "body_fat_pct": m.body_fat_pct,
                "lbm_kg": m.lbm_kg,
            }
        )
        for m in measurements
    ]

    # Body composition — BIA/InBody scans with every captured metric per scan
    # (the body_comp domain; complements the Navy body_fat_pct/lbm above).
    scans = (
        await session.execute(
            select(BodyScan).where(BodyScan.subject_id == subject_id)
            .options(selectinload(BodyScan.metrics))
            .order_by(BodyScan.date, BodyScan.id)
        )
    ).scalars().all()
    out["body_scans"] = [
        _compact(
            {
                "date": s.date.isoformat(),
                "device": s.device,
                "note": s.note,
                "metrics": [
                    _compact(
                        {
                            "metric": m.metric_key,
                            "value": m.value,
                            "unit": m.unit,
                            "segment": m.segment,
                        }
                    )
                    for m in s.metrics
                ],
            }
        )
        for s in scans
    ]

    noise = (
        await session.execute(select(NoiseMarker).where(NoiseMarker.subject_id == subject_id).order_by(NoiseMarker.start_date))
    ).scalars().all()
    out["noise_periods"] = [
        _compact(
            {
                "start_date": n.start_date.isoformat(),
                "end_date": n.end_date.isoformat() if n.end_date else None,
                "reason": n.reason,
            }
        )
        for n in noise
    ]

    # GLP-1 protocol.
    injections = (
        await session.execute(select(Injection).where(Injection.subject_id == subject_id).order_by(Injection.date))
    ).scalars().all()
    out["glp1_injections"] = [
        _compact({"date": i.date.isoformat(), "drug": i.drug, "dose_mg": i.dose_mg, "site": i.site})
        for i in injections
    ]
    phases = (
        await session.execute(select(DosePhase).where(DosePhase.subject_id == subject_id).order_by(DosePhase.start_date))
    ).scalars().all()
    out["glp1_dose_phases"] = [
        _compact(
            {
                "start_date": p.start_date.isoformat(),
                "end_date": p.end_date.isoformat() if p.end_date else None,
                "drug": p.drug,
                "dose_mg": p.dose_mg,
            }
        )
        for p in phases
    ]
    effects = (
        await session.execute(select(SideEffect).where(SideEffect.subject_id == subject_id).order_by(SideEffect.date))
    ).scalars().all()
    out["glp1_side_effects"] = [
        _compact(
            {"date": e.date.isoformat(), "effect_type": e.effect_type, "severity": e.severity}
        )
        for e in effects
    ]

    # HRT / TRT protocol — doses (with grey-market provenance), cycles with their
    # per-compound plans, side effects, and the user's saved cycle templates.
    hrt_doses = (
        await session.execute(select(HrtDose).where(HrtDose.subject_id == subject_id).order_by(HrtDose.date, HrtDose.id))
    ).scalars().all()
    out["hrt_doses"] = [
        _compact(
            {
                "date": d.date.isoformat(),
                "compound": d.compound_key,
                "dose": d.dose,
                "unit": d.unit,
                "volume_ml": d.volume_ml,
                "brand": d.brand,
                "lab": d.lab,
                "batch": d.batch,
                "site": d.site,
                "note": d.note,
            }
        )
        for d in hrt_doses
    ]
    hrt_cycles = (
        await session.execute(
            select(HrtCycle).where(HrtCycle.subject_id == subject_id)
            .options(selectinload(HrtCycle.items))
            .order_by(HrtCycle.start_date, HrtCycle.id)
        )
    ).scalars().all()
    out["hrt_cycles"] = [
        _compact(
            {
                "start_date": c.start_date.isoformat(),
                "end_date": c.end_date.isoformat() if c.end_date else None,
                "kind": c.kind,
                "name": c.name,
                "note": c.note,
                "items": [
                    _compact(
                        {
                            "compound": it.compound_key,
                            "unit": it.unit,
                            "start_offset_days": it.start_offset_days or None,
                            "schedule": it.schedule,
                        }
                    )
                    for it in c.items
                ],
            }
        )
        for c in hrt_cycles
    ]
    hrt_effects = (
        await session.execute(select(HrtSideEffect).where(HrtSideEffect.subject_id == subject_id).order_by(HrtSideEffect.date))
    ).scalars().all()
    out["hrt_side_effects"] = [
        _compact(
            {"date": e.date.isoformat(), "effect_type": e.effect_type, "severity": e.severity}
        )
        for e in hrt_effects
    ]
    hrt_templates = (
        await session.execute(
            select(HrtCycleTemplate).where(HrtCycleTemplate.subject_id == subject_id)
            .options(selectinload(HrtCycleTemplate.items))
            .order_by(HrtCycleTemplate.name)
        )
    ).scalars().all()
    out["hrt_cycle_templates"] = [
        _compact(
            {
                "name": tp.name,
                "kind": tp.kind,
                "note": tp.note,
                "items": [
                    _compact(
                        {
                            "compound": it.compound_key,
                            "unit": it.unit,
                            "start_offset_days": it.start_offset_days or None,
                            "schedule": it.schedule,
                        }
                    )
                    for it in tp.items
                ],
            }
        )
        for tp in hrt_templates
    ]

    # Labs.
    labs = (
        await session.execute(select(LabResult).where(LabResult.subject_id == subject_id).order_by(LabResult.date, LabResult.marker))
    ).scalars().all()
    out["biomarkers"] = [
        _compact(
            {
                "date": r.date.isoformat(),
                "marker": r.marker,
                "value": r.value,
                "unit": r.unit,
                "ref_low": r.ref_low,
                "ref_high": r.ref_high,
                "flag": r.flag,
            }
        )
        for r in labs
    ]

    # Workouts — rebuild the Hevy tree (workout → exercises → sets) without ids.
    out["workouts"] = await _llm_workouts(session)

    # Garmin — the whole daily row (sleep phases, HRV, stress, load, …) and the
    # whole activity row (HR zones, splits, training effect, …). The tall
    # ``garmin_intraday`` sample table stays out: ~3k samples a day would bury the
    # rest of the digest, and the daily row already carries its summaries.
    garmin = (
        await session.execute(select(GarminDaily).where(GarminDaily.subject_id == subject_id).order_by(GarminDaily.date))
    ).scalars().all()
    out["garmin_daily"] = [_row_dump(g) for g in garmin]
    activities = (
        await session.execute(
            select(GarminActivity).where(GarminActivity.subject_id == subject_id).order_by(GarminActivity.date, GarminActivity.id)
        )
    ).scalars().all()
    out["garmin_activities"] = [_row_dump(a) for a in activities]

    # Nutrition.
    meals = (
        await session.execute(select(MealLog).where(MealLog.subject_id == subject_id).order_by(MealLog.date))
    ).scalars().all()
    out["nutrition"] = [
        _compact(
            {
                "date": m.date.isoformat(),
                "name": m.name,
                "calories": m.calories,
                "protein_g": m.protein_g,
                "fat_g": m.fat_g,
                "carbs_g": m.carbs_g,
            }
        )
        for m in meals
    ]

    # Reference catalogs.
    supplements = (
        await session.execute(select(Supplement).where(Supplement.subject_id == subject_id).order_by(Supplement.name))
    ).scalars().all()
    out["supplements"] = [
        _compact(
            {
                "name": s.name,
                "dose": s.dose,
                "timing": s.timing,
                "evidence": s.evidence,
                "active": s.active,
                "contraindications": s.contraindications,
            }
        )
        for s in supplements
    ]
    variants = (
        await session.execute(select(GeneticVariant).where(GeneticVariant.subject_id == subject_id).order_by(GeneticVariant.gene))
    ).scalars().all()
    out["genetics"] = [
        _compact(
            {
                "gene": v.gene,
                "rsid": v.rsid,
                "genotype": v.genotype,
                "impact": v.impact,
                "interpretation": v.interpretation,
            }
        )
        for v in variants
    ]

    # Skincare logs + observations.
    sk_logs = (
        await session.execute(select(SkincareLog).where(SkincareLog.subject_id == subject_id).order_by(SkincareLog.date))
    ).scalars().all()
    out["skincare_logs"] = [
        _compact(
            {
                "date": s.date.isoformat(),
                "retinoid": s.retinoid,
                "azelaic": s.azelaic,
                "peel": s.peel,
                "niacinamide_spf": s.niacinamide_spf,
                "moisturizer": s.moisturizer,
                "vitamin_c": s.vitamin_c,
                "benzoyl_peroxide": s.benzoyl_peroxide,
            }
        )
        for s in sk_logs
    ]
    sk_obs = (
        await session.execute(select(SkincareObservation).where(SkincareObservation.subject_id == subject_id).order_by(SkincareObservation.date))
    ).scalars().all()
    out["skincare_observations"] = [
        _compact(
            {
                "date": o.date.isoformat(),
                "inflammation": o.inflammation,
                "pih": o.pih,
                "zone": o.zone,
            }
        )
        for o in sk_obs
    ]

    # Goals + generated narratives.
    milestones = (
        await session.execute(select(Milestone).where(Milestone.subject_id == subject_id).order_by(Milestone.id))
    ).scalars().all()
    out["milestones"] = [
        _compact(
            {
                "name": m.name,
                "domain": m.domain,
                "target_value": m.target_value,
                "target_unit": m.target_unit,
                "deadline": m.deadline.isoformat() if m.deadline else None,
                "status": m.status,
            }
        )
        for m in milestones
    ]
    digests = (
        await session.execute(select(WeeklyDigest).where(WeeklyDigest.subject_id == subject_id).order_by(WeeklyDigest.date))
    ).scalars().all()
    out["weekly_digests"] = [
        _compact({"date": d.date.isoformat(), "content": d.content}) for d in digests
    ]

    # Timeline — manual annotations (derived events already surface through
    # their own domain's block above, so they aren't repeated here).
    annotations = (
        await session.execute(select(Annotation).where(Annotation.subject_id == subject_id).order_by(Annotation.date))
    ).scalars().all()
    out["timeline_annotations"] = [
        _compact(
            {
                "date": a.date.isoformat(),
                "end_date": a.end_date.isoformat() if a.end_date else None,
                "domain": a.domain,
                "kind": a.kind,
                "title": a.title,
                "note": a.note,
            }
        )
        for a in annotations
    ]

    # ``signals`` stood here — the "how it actually felt" layer, parsed out of
    # chat messages. It is gone, and so is the block: a backup written now
    # carries no key for it. An older backup that has one is read and ignored,
    # which is what ``_UNKNOWN_TABLES_ARE_IGNORED`` below is for.

    # Narrowing happens on the assembled digest rather than in each of the twenty
    # queries above: the rows are already flat dicts with their dates, so one pass
    # here filters every block — including ones added later, which a per-query
    # ``where`` would have missed.
    if domains is not None:
        unknown = [d for d in domains if d not in out]
        if unknown:
            blocks = ", ".join(k for k in out if k != "profile")
            raise ValueError(f"unknown domains {unknown}; available: {blocks}")
        out = {k: v for k, v in out.items() if k == "profile" or k in domains}
    if since is not None:
        since_iso = since.isoformat()
        out = {
            k: [r for r in v if _row_within(r, since_iso)] if isinstance(v, list) else v
            for k, v in out.items()
        }

    return out


async def _llm_workouts(session: AsyncSession) -> list[dict[str, Any]]:
    """Assemble Hevy workouts with their exercises and sets, id-free, in two passes
    (no N+1): load all rows, then group children by parent in Python."""
    workouts = (
        await session.execute(select(HevyWorkout).order_by(HevyWorkout.date))
    ).scalars().all()
    exercises = (
        await session.execute(
            select(HevyExercise).order_by(HevyExercise.workout_id, HevyExercise.exercise_index)
        )
    ).scalars().all()
    sets = (
        await session.execute(
            select(HevySet).order_by(HevySet.exercise_id, HevySet.set_index)
        )
    ).scalars().all()

    sets_by_exercise: dict[int, list] = defaultdict(list)
    for s in sets:
        sets_by_exercise[s.exercise_id].append(s)
    exercises_by_workout: dict[int, list] = defaultdict(list)
    for e in exercises:
        exercises_by_workout[e.workout_id].append(e)

    result: list[dict[str, Any]] = []
    for w in workouts:
        result.append(
            _compact(
                {
                    "date": w.date.isoformat(),
                    "title": w.title,
                    "program": w.program,
                    "duration_min": round(w.duration_seconds / 60, 1)
                    if w.duration_seconds
                    else None,
                    "exercises": [
                        _compact(
                            {
                                "title": ex.title,
                                "sets": [
                                    _compact(
                                        {
                                            "weight_kg": st.weight_kg,
                                            "reps": st.reps,
                                            "rpe": st.rpe,
                                            "set_type": st.set_type
                                            if st.set_type != "normal"
                                            else None,
                                        }
                                    )
                                    for st in sets_by_exercise.get(ex.id, [])
                                ],
                            }
                        )
                        for ex in exercises_by_workout.get(w.id, [])
                    ],
                }
            )
        )
    return result
