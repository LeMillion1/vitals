"""Shared contract and safety policy for legacy portability-v1 archives."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Date, DateTime, Time, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

import vitals.models  # noqa: F401 -- register the complete generic backup graph
from vitals.i18n import t
from vitals.models.base import Base
from vitals.ownership import OWNERSHIP_REGISTRY, OwnershipClass, TargetColumn
from vitals.services.labs.markers import normalize_marker_key

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
