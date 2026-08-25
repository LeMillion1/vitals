"""Build the public, subject-scoped row graph for portability format v2.

This module deliberately stops at the boundary between database discovery and
archive resource streaming.  The returned ``manifest`` is safe to serialize;
the separately returned prepared resource handles contain the private locators
that a later, trusted layer needs in order to read bytes.

The ownership registry is the allowlist.  A table is not exported merely
because it happens to have a ``subject_id`` column, and a newly added table
cannot silently enter the archive before it has been classified there.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Final

from sqlalchemy import ColumnElement, Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import FileAssetPurpose, FileAssetStatus, FileStorageBackend
from vitals.models import Base
from vitals.ownership import (
    OWNERSHIP_REGISTRY,
    OwnershipClass,
    OwnershipSpec,
    TargetColumn,
)
from vitals.services.portability.schema import (
    EXPLICIT_EXCLUDED_TABLES,
    PORTABILITY_SCHEMA_DIGEST,
    SUBJECT_GRAPH_CLASSES,
    SchemaContractError,
    expected_resource_purpose,
    portable_tables,
)


FORMAT_NAME: Final = "vitals-portability-graph"
FORMAT_VERSION: Final = 2

# These look like ordinary subject data in the historical registry, but are not
# portable facts.  The provider outbox is local delivery state; system alerts
# are derived/control state.  Keep this explicit even if the registry is later
# tightened so a regression cannot put either back into an archive.
EXCLUDED_PORTABLE_TABLES: Final = EXPLICIT_EXCLUDED_TABLES

_SUBJECT_GRAPH_CLASSES: Final = SUBJECT_GRAPH_CLASSES

_PRIVATE_IDENTITY_COLUMNS: Final = frozenset(
    {
        "subject_id",
        "actor_user_id",
        "created_by_user_id",
        "uploaded_by_user_id",
        "requested_by_user_id",
        "recipient_user_id",
        "revoked_by_user_id",
        "resolved_by_user_id",
        "overridden_by_user_id",
        "integration_connection_id",
        "file_asset_id",
        "opaque_key",
        "storage_ref",
        "credential_ref",
    }
)


class GraphBuildError(RuntimeError):
    """A fail-closed graph contract violation.

    ``code`` is stable and contains no row value, UUID, path, or other private
    detail, so an outer boundary may safely log it.  Exception messages name at
    most a reviewed table/column from SQLAlchemy metadata.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, slots=True)
class GraphLimits:
    """Hard caps applied before a public graph object is returned."""

    max_tables: int = 64
    max_rows_per_table: int = 250_000
    max_total_rows: int = 1_000_000
    max_connections: int = 128
    max_resources: int = 100_000

    def __post_init__(self) -> None:
        for name in (
            "max_tables",
            "max_rows_per_table",
            "max_total_rows",
            "max_connections",
            "max_resources",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


DEFAULT_GRAPH_LIMITS: Final = GraphLimits()


@dataclass(frozen=True, slots=True)
class PreparedFileResource:
    """Private file handle for the later trusted archive-resource layer.

    None of these values belongs in the manifest.  ``resource_ref`` is the only
    join key shared with public graph data.
    """

    resource_ref: str
    file_asset_id: uuid.UUID
    storage_backend: str
    storage_ref: str
    expected_byte_size: int
    expected_sha256_hex: str


@dataclass(frozen=True, slots=True)
class PreparedSubjectGraph:
    """A JSON-safe public manifest plus private prepared file handles."""

    manifest: dict[str, Any]
    prepared_resources: tuple[PreparedFileResource, ...]


@dataclass(frozen=True, slots=True)
class _LoadedRow:
    table: Table
    values: Mapping[str, Any]
    row_ref: str


def _error(code: str, detail: str) -> GraphBuildError:
    return GraphBuildError(code, detail)


def _portable_tables() -> tuple[tuple[Table, OwnershipSpec], ...]:
    """Return the complete, reviewed v2 table set or fail on registry drift."""
    try:
        return portable_tables(registry=OWNERSHIP_REGISTRY)
    except SchemaContractError as exc:
        raise _error(exc.code, str(exc)) from exc


def _primary_key(row: Mapping[str, Any], table: Table) -> tuple[Any, ...]:
    values = tuple(row[column.name] for column in table.primary_key.columns)
    if any(value is None for value in values):
        raise _error(
            "null_primary_key", f"table {table.name!r} contains a null primary key"
        )
    return values


def _json_safe(value: Any, *, path: str) -> Any:
    """Encode one ordinary value without lossy/repr-based fallbacks."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _error("non_json_value", f"{path} contains a non-finite float")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise _error("non_json_value", f"{path} contains a non-finite decimal")
        # A string preserves the exact database decimal; the importer has the
        # SQLAlchemy column type and can restore it without a binary float hop.
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_safe(value.value, path=path)
    if isinstance(value, uuid.UUID):
        # No database UUID is an ordinary v2 value today.  Silently stringifying
        # a future one would turn a local identity or locator into archive data.
        raise _error("private_uuid_value", f"{path} contains an unclassified UUID")
    if isinstance(value, Mapping):
        encoded: dict[str, Any] = {}
        if any(not isinstance(key, str) for key in value):
            raise _error("non_json_value", f"{path} contains a non-string key")
        for key in sorted(value):
            encoded[key] = _json_safe(value[key], path=f"{path}.{key}")
        return encoded
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        return [
            _json_safe(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise _error(
        "non_json_value",
        f"{path} contains unsupported value type {type(value).__name__}",
    )


def _foreign_key_targets(table: Table) -> dict[str, tuple[str, str]]:
    """Map each local FK column to its one meaningful target.

    Composite ownership constraints often repeat the same local column in a
    second FK.  Repeating the same target is harmless; disagreeing targets are
    not something the generic archive graph can safely guess about.
    """

    targets: dict[str, tuple[str, str]] = {}
    for foreign_key in table.foreign_keys:
        local = foreign_key.parent.name
        target = (foreign_key.column.table.name, foreign_key.column.name)
        previous = targets.get(local)
        if previous is not None and previous != target:
            # ``subject_id`` intentionally participates in both the subject-root
            # FK and parent/subject composite FKs.  It is rebound by the import
            # boundary and never appears as a graph value or row link.
            if local == "subject_id":
                continue
            raise _error(
                "ambiguous_foreign_key",
                f"table {table.name!r} column {local!r} has ambiguous FK targets",
            )
        targets[local] = target
    return targets


def _inheritance_parent(
    table: Table, *, portable_names: set[str]
) -> tuple[str, str, str]:
    """Return ``(local column, parent table, parent PK column)`` for a child.

    An ownership child has one mandatory portable parent edge.  Other portable
    edges (for example an optional HRT compound reference) are data links, not
    its ownership boundary.  Deriving this from reviewed FK nullability avoids
    another table-name registry while still failing if the schema is ambiguous.
    """

    candidates: set[tuple[str, str, str]] = set()
    for foreign_key in table.foreign_keys:
        local = foreign_key.parent
        target_table = foreign_key.column.table
        target_column = foreign_key.column
        if (
            local.name != "subject_id"
            and not local.nullable
            and target_table.name in portable_names
            and target_column.primary_key
        ):
            candidates.add((local.name, target_table.name, target_column.name))
    if len(candidates) != 1:
        raise _error(
            "ownership_parent_ambiguous",
            f"inherited table {table.name!r} has {len(candidates)} ownership parents",
        )
    return next(iter(candidates))


def _subject_predicates(
    portable: tuple[tuple[Table, OwnershipSpec], ...],
    *,
    subject_id: uuid.UUID,
) -> dict[str, ColumnElement[bool]]:
    """Build subject reachability predicates, including nullable child stamps."""

    by_name = {table.name: (table, spec) for table, spec in portable}
    portable_names = set(by_name)
    resolved: dict[str, ColumnElement[bool]] = {}
    resolving: set[str] = set()

    def resolve(table_name: str) -> ColumnElement[bool]:
        existing = resolved.get(table_name)
        if existing is not None:
            return existing
        if table_name in resolving:
            raise _error("ownership_parent_cycle", "portable ownership graph is cyclic")
        resolving.add(table_name)
        table, spec = by_name[table_name]
        if spec.subject is TargetColumn.INHERITED or spec.ownership in {
            OwnershipClass.SUBJECT_CHILD,
            OwnershipClass.MIXED_CATALOG_CHILD,
        }:
            local_name, parent_name, parent_pk_name = _inheritance_parent(
                table, portable_names=portable_names
            )
            parent_table = by_name[parent_name][0]
            predicate = table.c[local_name].in_(
                select(parent_table.c[parent_pk_name]).where(resolve(parent_name))
            )
        else:
            predicate = table.c.subject_id == subject_id
        resolving.remove(table_name)
        resolved[table_name] = predicate
        return predicate

    for name in sorted(by_name):
        resolve(name)
    return resolved


def _required_reference(
    spec: OwnershipSpec, *, kind: str, row_value: Any, table_name: str
) -> None:
    target = spec.connection if kind == "connection" else spec.file_asset
    if target is TargetColumn.REQUIRED and row_value is None:
        raise _error(
            f"required_{kind}_missing",
            f"table {table_name!r} has a missing required {kind} reference",
        )


async def _load_connections(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    connection_ids: set[uuid.UUID],
    limits: GraphLimits,
) -> tuple[dict[uuid.UUID, str], list[dict[str, Any]]]:
    if len(connection_ids) > limits.max_connections:
        raise _error("connection_limit_exceeded", "connection limit exceeded")
    if not connection_ids:
        return {}, []

    table = Base.metadata.tables["integration_connections"]
    result = await session.execute(
        select(
            table.c.id,
            table.c.subject_id,
            table.c.provider,
            table.c.connection_type,
        ).where(table.c.id.in_(connection_ids))
    )
    rows = list(result.mappings())
    by_id = {row["id"]: row for row in rows}
    if set(by_id) != connection_ids:
        raise _error("connection_dangling", "a connection reference is dangling")
    if any(row["subject_id"] != subject_id for row in rows):
        raise _error(
            "connection_cross_subject", "a connection belongs to another subject"
        )

    descriptors: dict[tuple[str, str], uuid.UUID] = {}
    for row in rows:
        provider = row["provider"]
        connection_type = row["connection_type"]
        if (
            not isinstance(provider, str)
            or not provider.strip()
            or not isinstance(connection_type, str)
            or not connection_type.strip()
        ):
            raise _error(
                "connection_descriptor_invalid",
                "a connection has an invalid logical descriptor",
            )
        key = (provider, connection_type)
        if key in descriptors and descriptors[key] != row["id"]:
            raise _error(
                "connection_descriptor_ambiguous",
                "two connections share the same portable logical descriptor",
            )
        descriptors[key] = row["id"]

    ordered = sorted(rows, key=lambda row: (row["provider"], row["connection_type"]))
    refs: dict[uuid.UUID, str] = {}
    public: list[dict[str, Any]] = []
    for index, row in enumerate(ordered, start=1):
        connection_ref = f"c{index:08d}"
        refs[row["id"]] = connection_ref
        public.append(
            {
                "ref": connection_ref,
                "provider": row["provider"],
                "connection_type": row["connection_type"],
            }
        )
    return refs, public


def _validate_asset(row: Mapping[str, Any], *, subject_id: uuid.UUID) -> None:
    if row["subject_id"] != subject_id:
        raise _error("resource_cross_subject", "a file asset belongs to another subject")
    if row["status"] != FileAssetStatus.ACTIVE.value:
        raise _error("resource_not_live", "a referenced file asset is not active")
    if row["deleted_at"] is not None or row["purged_at"] is not None:
        raise _error("resource_not_live", "a referenced file asset is retired")
    if row["storage_backend"] not in {
        FileStorageBackend.PRIVATE_LOCAL.value,
        FileStorageBackend.OBJECT_STORE.value,
    }:
        raise _error(
            "resource_backend_invalid", "a live file asset uses an invalid backend"
        )
    if row["purpose"] not in {purpose.value for purpose in FileAssetPurpose}:
        raise _error("resource_metadata_invalid", "file purpose is invalid")
    storage_ref = row["storage_ref"]
    if (
        not isinstance(storage_ref, str)
        or not storage_ref.strip()
        or storage_ref.startswith("/")
        or ".." in storage_ref
    ):
        raise _error("resource_locator_invalid", "a file asset locator is invalid")
    media_type = row["media_type"]
    byte_size = row["byte_size"]
    sha256_hex = row["sha256_hex"]
    if not isinstance(media_type, str) or not media_type.strip():
        raise _error("resource_metadata_invalid", "file media_type is invalid")
    if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
        raise _error("resource_metadata_invalid", "file byte_size is invalid")
    if (
        not isinstance(sha256_hex, str)
        or len(sha256_hex) != 64
        or sha256_hex != sha256_hex.lower()
        or any(character not in "0123456789abcdef" for character in sha256_hex)
    ):
        raise _error("resource_metadata_invalid", "file sha256 is invalid")


async def _load_resources(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    file_asset_ids: set[uuid.UUID],
    expected_purposes: Mapping[uuid.UUID, frozenset[str]],
    limits: GraphLimits,
) -> tuple[
    dict[uuid.UUID, str],
    list[dict[str, Any]],
    tuple[PreparedFileResource, ...],
]:
    if len(file_asset_ids) > limits.max_resources:
        raise _error("resource_limit_exceeded", "resource limit exceeded")
    if not file_asset_ids:
        return {}, [], ()

    table = Base.metadata.tables["file_assets"]
    result = await session.execute(select(table).where(table.c.id.in_(file_asset_ids)))
    rows = list(result.mappings())
    by_id = {row["id"]: row for row in rows}
    if set(by_id) != file_asset_ids:
        raise _error("resource_dangling", "a file asset reference is dangling")
    for row in rows:
        _validate_asset(row, subject_id=subject_id)
        if expected_purposes[row["id"]] != frozenset({row["purpose"]}):
            raise _error(
                "resource_purpose_mismatch",
                "a file asset purpose does not match its portable row",
            )

    ordered = sorted(
        rows,
        key=lambda row: (
            row["purpose"],
            row["media_type"],
            row["sha256_hex"],
            row["byte_size"],
            row["id"],
        ),
    )
    refs: dict[uuid.UUID, str] = {}
    public: list[dict[str, Any]] = []
    prepared: list[PreparedFileResource] = []
    for index, row in enumerate(ordered, start=1):
        resource_ref = f"f{index:08d}"
        refs[row["id"]] = resource_ref
        public.append(
            {
                "ref": resource_ref,
                "purpose": row["purpose"],
                "media_type": row["media_type"],
                "byte_size": row["byte_size"],
                "sha256_hex": row["sha256_hex"],
            }
        )
        prepared.append(
            PreparedFileResource(
                resource_ref=resource_ref,
                file_asset_id=row["id"],
                storage_backend=row["storage_backend"],
                storage_ref=row["storage_ref"],
                expected_byte_size=row["byte_size"],
                expected_sha256_hex=row["sha256_hex"],
            )
        )
    return refs, public, tuple(prepared)


async def build_subject_graph(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    limits: GraphLimits = DEFAULT_GRAPH_LIMITS,
) -> PreparedSubjectGraph:
    """Prepare the strict v2 graph for exactly ``subject_id``.

    The function performs no commit and reads no file bytes.  It returns no
    partial value: row, table, connection, and resource limits plus every graph
    edge are validated before the public manifest is assembled.
    """

    if not isinstance(subject_id, uuid.UUID):
        raise TypeError("subject_id must be a UUID")
    if not isinstance(limits, GraphLimits):
        raise TypeError("limits must be GraphLimits")

    subject_table = Base.metadata.tables["health_subjects"]
    subject_count = await session.scalar(
        select(func.count()).select_from(subject_table).where(subject_table.c.id == subject_id)
    )
    if subject_count != 1:
        raise _error("subject_not_found", "the requested subject does not exist")

    portable = _portable_tables()
    if len(portable) > limits.max_tables:
        raise _error("table_limit_exceeded", "portable table limit exceeded")

    predicates = _subject_predicates(portable, subject_id=subject_id)
    expected_counts: dict[str, int] = {}
    total_rows = 0
    for table, _spec in portable:
        count = int(
            await session.scalar(
                select(func.count())
                .select_from(table)
                .where(predicates[table.name])
            )
            or 0
        )
        if count > limits.max_rows_per_table:
            raise _error(
                "table_row_limit_exceeded",
                f"table {table.name!r} exceeds the per-table row limit",
            )
        total_rows += count
        if total_rows > limits.max_total_rows:
            raise _error("total_row_limit_exceeded", "total row limit exceeded")
        expected_counts[table.name] = count

    loaded_by_table: dict[str, list[_LoadedRow]] = {}
    row_lookup: dict[tuple[str, tuple[Any, ...]], str] = {}
    next_row_ref = 1
    for table, _spec in portable:
        result = await session.execute(
            select(table)
            .where(predicates[table.name])
            .order_by(*table.primary_key.columns)
        )
        mappings = list(result.mappings())
        if len(mappings) != expected_counts[table.name]:
            raise _error(
                "graph_changed_during_build",
                f"table {table.name!r} changed while the graph was built",
            )
        loaded: list[_LoadedRow] = []
        for values in mappings:
            row_subject = values["subject_id"]
            if row_subject is not None and row_subject != subject_id:
                raise _error(
                    "inherited_row_cross_subject",
                    f"table {table.name!r} has a child stamped for another subject",
                )
            key = _primary_key(values, table)
            lookup_key = (table.name, key)
            if lookup_key in row_lookup:
                raise _error(
                    "duplicate_primary_key",
                    f"table {table.name!r} contains a duplicate primary key",
                )
            row_ref = f"r{next_row_ref:012d}"
            next_row_ref += 1
            row_lookup[lookup_key] = row_ref
            loaded.append(_LoadedRow(table=table, values=values, row_ref=row_ref))
        loaded_by_table[table.name] = loaded

        # A child explicitly stamped for this subject must be reachable from
        # this subject's selected parent.  Otherwise filtering only by parent
        # would turn a corrupt cross-subject/dangling row into silent data loss.
        spec = OWNERSHIP_REGISTRY[table.name]
        if spec.subject is TargetColumn.INHERITED or spec.ownership in {
            OwnershipClass.SUBJECT_CHILD,
            OwnershipClass.MIXED_CATALOG_CHILD,
        }:
            direct_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.c.subject_id == subject_id)
                )
                or 0
            )
            included_direct_count = sum(
                loaded.values["subject_id"] == subject_id for loaded in loaded
            )
            if direct_count != included_direct_count:
                raise _error(
                    "inherited_row_unreachable",
                    f"table {table.name!r} has an unreachable subject child",
                )

    # Collect non-portable logical roots only after the portable row graph is
    # complete.  IDs remain local dictionary keys and never become manifest data.
    connection_ids: set[uuid.UUID] = set()
    file_asset_ids: set[uuid.UUID] = set()
    expected_file_purposes: dict[uuid.UUID, set[str]] = {}
    for table, spec in portable:
        for loaded in loaded_by_table[table.name]:
            connection_value = loaded.values.get("integration_connection_id")
            file_value = loaded.values.get("file_asset_id")
            _required_reference(
                spec,
                kind="connection",
                row_value=connection_value,
                table_name=table.name,
            )
            _required_reference(
                spec,
                kind="file",
                row_value=file_value,
                table_name=table.name,
            )
            if connection_value is not None:
                if not isinstance(connection_value, uuid.UUID):
                    raise _error(
                        "connection_reference_invalid",
                        f"table {table.name!r} has a non-UUID connection reference",
                    )
                connection_ids.add(connection_value)
            if file_value is not None:
                if not isinstance(file_value, uuid.UUID):
                    raise _error(
                        "resource_reference_invalid",
                        f"table {table.name!r} has a non-UUID resource reference",
                    )
                file_asset_ids.add(file_value)
                try:
                    expected_purpose = expected_resource_purpose(
                        table.name,
                        loaded.values,
                    )
                except SchemaContractError as exc:
                    raise _error(exc.code, str(exc)) from exc
                expected_file_purposes.setdefault(file_value, set()).add(
                    expected_purpose
                )
            if table.name in {"progress_photos", "body_scans"}:
                legacy_file_key = loaded.values.get("file_key")
                if legacy_file_key and file_value is None:
                    raise _error(
                        "legacy_file_without_resource",
                        f"table {table.name!r} has a file_key without a file asset",
                    )

    connection_refs, public_connections = await _load_connections(
        session,
        subject_id=subject_id,
        connection_ids=connection_ids,
        limits=limits,
    )
    resource_refs, public_resources, prepared_resources = await _load_resources(
        session,
        subject_id=subject_id,
        file_asset_ids=file_asset_ids,
        expected_purposes={
            asset_id: frozenset(purposes)
            for asset_id, purposes in expected_file_purposes.items()
        },
        limits=limits,
    )

    public_tables: list[dict[str, Any]] = []
    portable_names = set(loaded_by_table)
    for table, _spec in portable:
        foreign_targets = _foreign_key_targets(table)
        public_rows: list[dict[str, Any]] = []
        for loaded in loaded_by_table[table.name]:
            ordinary: dict[str, Any] = {}
            links: dict[str, str] = {}
            primary_names = {column.name for column in table.primary_key.columns}
            for column in table.columns:
                name = column.name
                value = loaded.values[name]
                if name in primary_names or name in _PRIVATE_IDENTITY_COLUMNS:
                    continue
                if name == "file_key":
                    continue

                target = foreign_targets.get(name)
                if target is not None:
                    target_table, target_column = target
                    if target_table in portable_names:
                        if value is None:
                            if not column.nullable:
                                raise _error(
                                    "required_foreign_key_missing",
                                    f"table {table.name!r} column {name!r} is null",
                                )
                            continue
                        target_model = Base.metadata.tables[target_table]
                        if target_column not in {
                            pk.name for pk in target_model.primary_key.columns
                        }:
                            raise _error(
                                "non_primary_foreign_key",
                                f"table {table.name!r} column {name!r} targets a non-PK",
                            )
                        target_key = (target_table, (value,))
                        target_ref = row_lookup.get(target_key)
                        if target_ref is None:
                            raise _error(
                                "foreign_key_dangling",
                                f"table {table.name!r} column {name!r} leaves the subject graph",
                            )
                        links[name] = target_ref
                    # Foreign keys into identity/control roots are deliberately
                    # not transported.  Connection/file links were collected by
                    # their explicit registry contract above.
                    continue

                if table.name == "raw_payloads" and name == "external_id" and loaded.values.get(
                    "file_asset_id"
                ) is not None:
                    # File imports reconstruct this from their new resource
                    # locator; exporting the old key would disclose the legacy
                    # path and recreate a stale locator.
                    continue
                ordinary[name] = _json_safe(
                    value, path=f"{table.name}.{name}"
                )

            connection_value = loaded.values.get("integration_connection_id")
            if connection_value is not None:
                links["integration_connection_id"] = connection_refs[connection_value]
            file_value = loaded.values.get("file_asset_id")
            if file_value is not None:
                links["file_asset_id"] = resource_refs[file_value]

            public_row: dict[str, Any] = {
                "ref": loaded.row_ref,
                "values": ordinary,
            }
            if links:
                public_row["links"] = dict(sorted(links.items()))
            public_rows.append(public_row)
        public_tables.append({"name": table.name, "rows": public_rows})

    manifest: dict[str, Any] = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "schema_digest": PORTABILITY_SCHEMA_DIGEST,
        "tables": public_tables,
        "connections": public_connections,
        "resources": public_resources,
        "totals": {
            "tables": len(public_tables),
            "rows": total_rows,
            "connections": len(public_connections),
            "resources": len(public_resources),
        },
    }
    # This is both a final strict-encoding assertion and a defense against a
    # future edit adding a private dataclass/UUID value to the manifest shape.
    manifest = _json_safe(manifest, path="manifest")
    return PreparedSubjectGraph(
        manifest=manifest,
        prepared_resources=prepared_resources,
    )


__all__ = [
    "DEFAULT_GRAPH_LIMITS",
    "EXCLUDED_PORTABLE_TABLES",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "GraphBuildError",
    "GraphLimits",
    "PreparedFileResource",
    "PreparedSubjectGraph",
    "build_subject_graph",
]
