"""Validated import paths for legacy portability-v1 archives.

Reusable operations flush but never commit. The destructive full replacement is
entered only through the ownership coordinator, which supplies the reviewed
preflight and checkpoint hooks.
"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import ValidationError
from sqlalchemy import column as sql_column, func, select, table as sql_table, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.i18n import t
from vitals.models.base import Base
from vitals.ownership_transition.portability_v1 import PortabilityV1OwnershipHooks
from vitals.services.conflicts.catalog import sync_catalog as sync_conflict_catalog
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.portability.v1_contract import (
    BACKUP_VERSION,
    GENERIC_OUTPUT_SUPPRESSED_COLUMNS,
    BackupMetadata,
    ImportStats,
    PortabilityError,
    _EXCLUDED_TABLES,
    _POSTGRES_INTEGER_MAX,
    _RETAINED_RAW_REFERENCE_TABLES,
    _SUBJECT_BOUND_MARKER,
    _contract_error,
    _deserialize_value,
    _is_secret_setting_key,
    _live_schema_columns,
    _refuse_unrestorable_v1_rows,
    _single_local_subject_id,
    _subject_marker,
    _subject_rebind_required,
    _upgrade_lab_marker_identity,
)
from vitals.services.portability.v1_export import (
    CATALOG_NATURAL_KEYS,
    KIND_SUBJECT,
    PORTABLE_REFERENCES,
    _REFERENCE_MARKER,
)

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
