"""Data portability — full backup/restore + a curated LLM-ready export.

Two deliberately different shapes (they pull in opposite directions, so we don't
try to make one file serve both):

  * **Full backup** (:func:`export_full` / :func:`import_full`) — a
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
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Date, DateTime, Time, func, inspect, select, text
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
from vitals.models.signals import DayContext, Signal
from vitals.models.skincare import SkincareLog, SkincareObservation
from vitals.models.supplements import Supplement
from vitals.models.timeline import Annotation
from vitals.models.weight import BodyMeasurement, NoiseMarker, WeightLog
from vitals.ownership import (
    OWNERSHIP_REGISTRY,
    OwnershipClass,
    TargetColumn,
)
from vitals.i18n import t
from vitals.services.conflict_rule_ownership_backfill_service import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_TABLES,
    ConflictRuleOwnershipBackfillError,
    preflight_conflict_rule_ownership_backfill,
    reset_conflict_rule_backfill_for_portability_v1_restore,
)
from vitals.services.conflict_catalog import (
    ConflictCatalogCollisionError,
    sync_catalog as sync_conflict_catalog,
)
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.hevy_child_ownership_backfill_service import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES,
    HevyChildOwnershipBackfillError,
    block_hevy_child_ownership_backfill_for_portability_v1_restore,
)
from vitals.services.hrt_child_ownership_backfill_service import (
    HRT_CHILD_OWNERSHIP_BACKFILL_TABLES,
    HrtChildOwnershipBackfillError,
    reset_hrt_child_backfill_for_portability_v1_restore,
)
from vitals.services.hrt_compound_ownership_backfill_service import (
    HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES,
    HrtCompoundOwnershipBackfillError,
    reset_hrt_compound_backfill_for_portability_v1_restore,
)
from vitals.services.normalized_ownership_backfill_service import (
    NORMALIZED_MANUAL_TABLES,
    NormalizedOwnershipBackfillError,
    reset_normalized_manual_backfill_for_portability_v1_restore,
)
from vitals.services.provider_raw_ownership_backfill_service import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES,
    ProviderRawOwnershipBackfillError,
    block_provider_raw_ownership_backfill_for_portability_v1_restore,
)
from vitals.services.raw_ownership_backfill_service import (
    RawOwnershipBackfillError,
    block_raw_ownership_backfill_for_portability_v1_restore,
)
from vitals.services.signals_service import normalize_key
from vitals.utils.timeutils import now_local

# Bump when the on-disk shape changes in a backward-incompatible way.
BACKUP_VERSION = "1.0"
KIND_FULL = "full_backup"
KIND_LLM = "llm_export"

# An ``app_settings`` key is treated as a secret (and dropped from the backup) when
# it contains any of these substrings — forward-looking guard for token rows.
_SECRET_KEY_MARKERS = ("token", "secret", "password", "api_key", "apikey", "credential")
_POSTGRES_INTEGER_MAX = 2_147_483_647

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
        "file_asset_id",
        "uploaded_by_user_id",
        "credential_ref",
        "storage_ref",
        "opaque_key",
    }
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
    "signals", "day_context", "body_scans", "milestones", "noise_markers",
)


class PortabilityError(Exception):
    """Raised on a malformed/invalid backup file. The router turns it into a clean
    HTTP 400 (never a silent failure or a leaked DB error)."""


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
        raise _contract_error("portability.error.v1_multi_subject")
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
    """The backup envelope. Extra keys are ignored so older/newer files still load."""

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


async def export_full(session: AsyncSession) -> dict[str, Any]:
    """Snapshot portable tables into ``{table_name: [rows]}`` plus metadata.

    Tables are walked in FK order (``sorted_tables``); ``app_settings`` secret-ish
    rows and ownership/private-resource plumbing are dropped. The result is a
    plain dict ready for ``json.dumps``.
    """
    local_subject_id = await _single_local_subject_id(session)
    out: dict[str, Any] = {
        "metadata": {
            "version": BACKUP_VERSION,
            "kind": KIND_FULL,
            "exported_at": now_local().isoformat(timespec="seconds"),
            "timezone": os.getenv("VITALS_TIMEZONE", "Europe/Chisinau"),
        }
    }

    for table in Base.metadata.sorted_tables:
        if table.name in _EXCLUDED_TABLES:
            continue
        result = await session.execute(select(table))
        column_names = [
            name
            for name in table.columns.keys()
            if name not in GENERIC_OUTPUT_SUPPRESSED_COLUMNS
        ]
        rows: list[dict[str, Any]] = []
        for mapping in result.mappings().all():
            if table.name == "app_settings" and _is_secret_setting_key(mapping.get("key")):
                continue
            row = {col: _serialize_value(mapping[col]) for col in column_names}
            if "subject_id" in table.columns:
                subject_bound = mapping["subject_id"] is not None
                if local_subject_id is None and subject_bound:
                    raise _contract_error("portability.error.v1_missing_subject")
                row[_SUBJECT_BOUND_MARKER] = subject_bound
            rows.append(row)
        out[table.name] = rows

    return out


# ── Full backup: import (replace) ──────────────────────────────────────────────


def _validate_payload(payload: Any) -> BackupMetadata:
    """Structural validation. Raises :class:`PortabilityError` with a clear message
    on anything malformed — no silent acceptance of junk."""
    if not isinstance(payload, dict):
        raise PortabilityError(t("import.error.not_json_obj"))
    if "metadata" not in payload:
        raise PortabilityError(t("import.error.no_metadata"))
    try:
        meta = BackupMetadata.model_validate(payload["metadata"])
    except ValidationError as exc:
        raise PortabilityError(t("import.error.bad_metadata", msg=exc.errors()[0].get("msg", exc)))

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
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3B table bounds before any restore mutation."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in NORMALIZED_MANUAL_TABLES:
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
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3C child bounds before any restore mutation."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in HRT_CHILD_OWNERSHIP_BACKFILL_TABLES:
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
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3D provider-table bounds before restore mutation."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES:
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
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3E Hevy-child bounds before restore mutation."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES:
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
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3F mixed HRT catalog bounds before replacement."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES:
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
) -> dict[str, tuple[int, int]]:
    """Return exact Stage-3G conflict-rule bounds before replacement."""

    bounds: dict[str, tuple[int, int]] = {}
    for table_name in CONFLICT_RULE_OWNERSHIP_BACKFILL_TABLES:
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


async def _refuse_retained_raw_references(session: AsyncSession) -> None:
    """Fail before mutation when retained control state still binds any raw."""

    for table_name in _RETAINED_RAW_REFERENCE_TABLES:
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


async def import_full(session: AsyncSession, payload: Any) -> ImportStats:
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
    raw_high_watermark, raw_snapshot_rows = _raw_replacement_snapshot_bounds(
        payload
    )
    normalized_snapshot_bounds = (
        _normalized_manual_replacement_snapshot_bounds(payload)
    )
    hrt_child_snapshot_bounds = _hrt_child_replacement_snapshot_bounds(payload)
    provider_raw_snapshot_bounds = _provider_raw_replacement_snapshot_bounds(
        payload
    )
    hevy_child_snapshot_bounds = _hevy_child_replacement_snapshot_bounds(payload)
    hrt_compound_snapshot_bounds = _hrt_compound_replacement_snapshot_bounds(
        payload
    )
    conflict_rule_snapshot_bounds = _conflict_rule_replacement_snapshot_bounds(
        payload
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
        await _refuse_retained_raw_references(session)
        if local_subject_id is not None:
            try:
                await block_raw_ownership_backfill_for_portability_v1_restore(
                    session,
                    scan_high_watermark_id=raw_high_watermark,
                    snapshot_rows=raw_snapshot_rows,
                )
            except RawOwnershipBackfillError as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="raw ownership restore block was rejected",
                ) from exc
            try:
                await reset_normalized_manual_backfill_for_portability_v1_restore(
                    session,
                    snapshot_bounds=normalized_snapshot_bounds,
                )
            except NormalizedOwnershipBackfillError as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="normalized ownership restore reset was rejected",
                ) from exc
            try:
                await reset_hrt_child_backfill_for_portability_v1_restore(
                    session,
                    snapshot_bounds=hrt_child_snapshot_bounds,
                )
            except HrtChildOwnershipBackfillError as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="HRT child ownership restore reset was rejected",
                ) from exc
            try:
                await block_provider_raw_ownership_backfill_for_portability_v1_restore(
                    session,
                    snapshot_bounds=provider_raw_snapshot_bounds,
                )
            except ProviderRawOwnershipBackfillError as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="provider ownership restore block was rejected",
                ) from exc
            try:
                await block_hevy_child_ownership_backfill_for_portability_v1_restore(
                    session,
                    snapshot_bounds=hevy_child_snapshot_bounds,
                )
            except HevyChildOwnershipBackfillError as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="Hevy child ownership restore block was rejected",
                ) from exc
            try:
                await reset_hrt_compound_backfill_for_portability_v1_restore(
                    session,
                    snapshot_bounds=hrt_compound_snapshot_bounds,
                )
            except HrtCompoundOwnershipBackfillError as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="HRT compound ownership restore reset was rejected",
                ) from exc
            try:
                await reset_conflict_rule_backfill_for_portability_v1_restore(
                    session,
                    snapshot_bounds=conflict_rule_snapshot_bounds,
                )
            except ConflictRuleOwnershipBackfillError as exc:
                raise _contract_error(
                    "import.error.generic",
                    exc="conflict-rule ownership restore reset was rejected",
                ) from exc
        preserved = await _secret_settings(session)

        # Wipe in reverse FK order so child rows go before the parents they reference.
        for table in reversed(Base.metadata.sorted_tables):
            if table.name in _EXCLUDED_TABLES:
                continue
            await session.execute(table.delete())

        counts: dict[str, int] = {}
        # Reload in FK order, preserving ids and business columns. Ownership and
        # private-resource references are assigned by a trusted tenancy-aware
        # boundary, never accepted from a portable v1 file.
        for table in Base.metadata.sorted_tables:
            if table.name in _EXCLUDED_TABLES:
                continue
            rows = payload.get(table.name)
            if table.name == "app_settings":
                rows = [r for r in rows or () if not _is_secret_setting_key(r.get("key"))]
            if not rows:
                continue
            columns = table.columns
            records = [
                {
                    key: _deserialize_value(columns[key].type, val)
                    for key, val in row.items()
                    if key in columns  # tolerate columns dropped in a later schema
                    if key not in GENERIC_OUTPUT_SUPPRESSED_COLUMNS
                }
                for row in rows
            ]
            if "subject_id" in columns and local_subject_id is not None:
                for row, record in zip(rows, records, strict=True):
                    record["subject_id"] = (
                        local_subject_id
                        if _subject_rebind_required(table.name, row)
                        else None
                    )
            await session.execute(table.insert(), records)
            counts[table.name] = len(records)

        if preserved:
            await session.execute(Base.metadata.tables["app_settings"].insert(), preserved)

        # Explicit portable IDs are loaded before current checked-in catalog rows.
        # Advance PostgreSQL sequences first so catalog insertion cannot collide,
        # then validate the complete current safety catalog in this same restore
        # transaction.  A stale/omitted catalog is upgraded atomically; protected
        # code collisions or retained orphan alert references roll everything back.
        await _reset_sequences(session)
        try:
            await sync_conflict_catalog(session)
            if local_subject_id is not None:
                await preflight_conflict_rule_ownership_backfill(session)
        except (
            ConflictCatalogCollisionError,
            ConflictRuleOwnershipBackfillError,
        ) as exc:
            raise _contract_error(
                "import.error.generic",
                exc="conflict-rule catalog validation rejected the portable restore",
            ) from exc
        await _reset_sequences(session)
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


async def _reset_sequences(session: AsyncSession) -> None:
    """Advance sequences for restored portable tables; no-op on SQLite."""
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    for table in Base.metadata.sorted_tables:
        if table.name in _EXCLUDED_TABLES or "id" not in table.columns:
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
    domains: Sequence[str] | None = None,
    since: date | None = None,
) -> dict[str, Any]:
    """Curated, flat, secret-free digest grouped by domain — paste-into-chat ready.

    Both filters default to off, so the web download stays the whole history. The
    MCP tool narrows instead: a chat asking about this month should not be handed
    years of daily Garmin rows. ``domains`` names top-level blocks of the result
    (``weight_history``, ``biomarkers``, …); ``since`` drops rows that ended before
    that date.
    """
    out: dict[str, Any] = {"profile": _llm_profile()}

    # Weight — active rows only (superseded duplicates are noise for analysis).
    weights = (
        await session.execute(
            select(WeightLog).where(WeightLog.superseded.is_(False)).order_by(WeightLog.date)
        )
    ).scalars().all()
    out["weight_history"] = [
        _compact({"date": w.date.isoformat(), "weight_kg": w.weight_kg, "note": w.note})
        for w in weights
    ]

    measurements = (
        await session.execute(select(BodyMeasurement).order_by(BodyMeasurement.date))
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
            select(BodyScan)
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
        await session.execute(select(NoiseMarker).order_by(NoiseMarker.start_date))
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
        await session.execute(select(Injection).order_by(Injection.date))
    ).scalars().all()
    out["glp1_injections"] = [
        _compact({"date": i.date.isoformat(), "drug": i.drug, "dose_mg": i.dose_mg, "site": i.site})
        for i in injections
    ]
    phases = (
        await session.execute(select(DosePhase).order_by(DosePhase.start_date))
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
        await session.execute(select(SideEffect).order_by(SideEffect.date))
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
        await session.execute(select(HrtDose).order_by(HrtDose.date, HrtDose.id))
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
            select(HrtCycle)
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
        await session.execute(select(HrtSideEffect).order_by(HrtSideEffect.date))
    ).scalars().all()
    out["hrt_side_effects"] = [
        _compact(
            {"date": e.date.isoformat(), "effect_type": e.effect_type, "severity": e.severity}
        )
        for e in hrt_effects
    ]
    hrt_templates = (
        await session.execute(
            select(HrtCycleTemplate)
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
        await session.execute(select(LabResult).order_by(LabResult.date, LabResult.marker))
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
        await session.execute(select(GarminDaily).order_by(GarminDaily.date))
    ).scalars().all()
    out["garmin_daily"] = [_row_dump(g) for g in garmin]
    activities = (
        await session.execute(
            select(GarminActivity).order_by(GarminActivity.date, GarminActivity.id)
        )
    ).scalars().all()
    out["garmin_activities"] = [_row_dump(a) for a in activities]

    # Nutrition.
    meals = (
        await session.execute(select(MealLog).order_by(MealLog.date))
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
        await session.execute(select(Supplement).order_by(Supplement.name))
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
        await session.execute(select(GeneticVariant).order_by(GeneticVariant.gene))
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
        await session.execute(select(SkincareLog).order_by(SkincareLog.date))
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
        await session.execute(select(SkincareObservation).order_by(SkincareObservation.date))
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
        await session.execute(select(Milestone).order_by(Milestone.id))
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
        await session.execute(select(WeeklyDigest).order_by(WeeklyDigest.date))
    ).scalars().all()
    out["weekly_digests"] = [
        _compact({"date": d.date.isoformat(), "content": d.content}) for d in digests
    ]

    # Timeline — manual annotations (derived events already surface through
    # their own domain's block above, so they aren't repeated here).
    annotations = (
        await session.execute(select(Annotation).order_by(Annotation.date))
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

    # Signals — the "how it actually felt" layer, and the day's context. This is
    # the block that lets the model answer *why* a Garmin number moved, so keys go
    # out canonical (aliases folded on read) rather than in whatever spelling the
    # parser happened to use. Misparsed batches stay out: they're key-registry
    # material, not evidence.
    signals = (
        await session.execute(
            select(Signal).where(Signal.misparse.is_(False)).order_by(Signal.date)
        )
    ).scalars().all()
    out["signals"] = [
        _compact(
            {
                "date": s.date.isoformat(),
                "kind": s.kind,
                "key": normalize_key(s.key),
                "value": s.value_num,
                "unit": s.unit,
                "at_time": s.at_time.isoformat() if s.at_time else None,
                "note": s.note,
            }
        )
        for s in signals
    ]
    contexts = (
        await session.execute(select(DayContext).order_by(DayContext.date))
    ).scalars().all()
    out["day_context"] = [
        _compact({"date": c.date.isoformat(), "answers": c.answers, "source": c.source})
        for c in contexts
    ]

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
