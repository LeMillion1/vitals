"""Flush-only replacement of one subject's portable v2 record graph.

All external choices must already be explicit: connection refs are canonical,
resources are registered FileAssets, and retained raw payload IDs come from the
replacement preflight.  This service revalidates those installation-local
identities, replaces only the target subject's portable rows, and flushes.  The
caller remains the sole owner of commit, rollback, and staged-file cleanup.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Protocol

from sqlalchemy import ColumnElement, delete, func, insert, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import FileAssetStatus, IntegrationConnectionStatus
from vitals.models import Base
from vitals.services.portability.connection_mapping import (
    CanonicalConnectionMapping,
    ConnectionMappingError,
    resolve_connection_mapping,
)
from vitals.services.portability.record_decoder import (
    DecodedRecord,
    DecodedResource,
    DecodedRow,
)
from vitals.services.portability.schema import (
    PORTABILITY_SCHEMA_DESCRIPTOR,
    PORTABILITY_SCHEMA_DIGEST,
)


_USABLE_CONNECTION_STATUSES: Final = frozenset(
    {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }
)
_SCHEMA_BY_TABLE: Final = {
    table["name"]: table for table in PORTABILITY_SCHEMA_DESCRIPTOR["tables"]
}
_INSERT_ORDER: Final = tuple(PORTABILITY_SCHEMA_DESCRIPTOR["insert_order"])
_DELETE_ORDER: Final = tuple(PORTABILITY_SCHEMA_DESCRIPTOR["delete_order"])


class ResourceBindingLike(Protocol):
    """A staged archive resource bound to installation-local metadata."""

    @property
    def file_asset_id(self) -> uuid.UUID: ...

    @property
    def storage_ref(self) -> str: ...


class ReplacementApplyError(RuntimeError):
    """A stable, PHI-free refusal to mutate the replacement transaction."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, slots=True)
class TableApplyCount:
    table: str
    rows: int


@dataclass(frozen=True, slots=True)
class ReplacementApplyResult:
    """Immutable mutation summary; file bytes remain outside this service."""

    row_ids_by_ref: Mapping[str, int]
    old_file_asset_ids: tuple[uuid.UUID, ...]
    deleted: tuple[TableApplyCount, ...]
    inserted: tuple[TableApplyCount, ...]

    @property
    def deleted_rows(self) -> int:
        return sum(item.rows for item in self.deleted)

    @property
    def inserted_rows(self) -> int:
        return sum(item.rows for item in self.inserted)

    @property
    def old_file_asset_count(self) -> int:
        return len(self.old_file_asset_ids)


@dataclass(frozen=True, slots=True)
class _ResourceBinding:
    ref: str
    asset_id: uuid.UUID
    storage_ref: str
    descriptor: DecodedResource


def _error(code: str, detail: str) -> ReplacementApplyError:
    return ReplacementApplyError(code, detail)


def _require_subject_id(value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise _error("replacement_subject_invalid", "target subject id must be a non-zero UUID")
    return value


def _retained_ids(raw: Sequence[int]) -> tuple[int, ...]:
    if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, Sequence):
        raise _error("retained_raw_ids_invalid", "retained raw payload IDs must be a sequence")
    values: list[int] = []
    for value in raw:
        if type(value) is not int or value <= 0:
            raise _error("retained_raw_ids_invalid", "retained raw payload ID is invalid")
        values.append(value)
    if len(values) != len(set(values)):
        raise _error("retained_raw_ids_invalid", "retained raw payload IDs are duplicated")
    return tuple(sorted(values))


def _record_rows(record: DecodedRecord) -> tuple[DecodedRow, ...]:
    if not isinstance(record, DecodedRecord):
        raise TypeError("record must be a DecodedRecord")
    if record.schema_digest != PORTABILITY_SCHEMA_DIGEST:
        raise _error("replacement_schema_invalid", "decoded record schema is unsupported")
    if tuple(table.name for table in record.tables) != _INSERT_ORDER:
        raise _error("replacement_tables_invalid", "decoded tables are incomplete or unordered")

    rows: list[DecodedRow] = []
    refs: set[str] = set()
    used_resource_refs: set[str] = set()
    used_connection_refs: set[str] = set()
    resource_refs = {resource.ref for resource in record.resources}
    connection_refs = {connection.ref for connection in record.connections}
    if len(resource_refs) != len(record.resources) or len(connection_refs) != len(
        record.connections
    ):
        raise _error("replacement_descriptors_invalid", "decoded descriptors are duplicated")
    for table in record.tables:
        schema = _SCHEMA_BY_TABLE[table.name]
        value_columns = {item["name"]: item for item in schema["value_columns"]}
        link_columns = {item["name"]: item for item in schema["links"]}
        for row in table.rows:
            if not isinstance(row, DecodedRow) or row.table != table.name or row.ref in refs:
                raise _error("replacement_rows_invalid", "decoded row identity is invalid")
            refs.add(row.ref)
            expected_values = set(value_columns)
            conditional = {
                item["name"]: item.get("conditional_rebuild")
                for item in schema["value_columns"]
                if item.get("conditional_rebuild") is not None
            }
            for name, mode in conditional.items():
                if mode != "resource_storage_ref_when_file_link":
                    raise _error("replacement_schema_invalid", "unknown rebuilt value mode")
                if "file_asset_id" in row.links:
                    expected_values.remove(name)
            if set(row.values) != expected_values or not set(row.links).issubset(link_columns):
                raise _error("replacement_rows_invalid", "decoded row fields are not exact")
            required_links = {
                name for name, item in link_columns.items() if item["required"] is True
            }
            if not required_links.issubset(row.links):
                raise _error("replacement_rows_invalid", "decoded row lacks a required link")
            for name, link in row.links.items():
                schema_link = link_columns[name]
                if (
                    link.column != name
                    or link.kind != schema_link["kind"]
                    or link.target_table != schema_link["target_table"]
                    or link.target_column != schema_link["target_column"]
                    or link.required is not schema_link["required"]
                ):
                    raise _error("replacement_rows_invalid", "decoded link contract changed")
                if link.kind == "connection" and link.target_ref not in connection_refs:
                    raise _error("replacement_rows_invalid", "decoded connection link is unknown")
                if link.kind == "resource" and link.target_ref not in resource_refs:
                    raise _error("replacement_rows_invalid", "decoded resource link is unknown")
                if link.kind == "connection":
                    used_connection_refs.add(link.target_ref)
                elif link.kind == "resource":
                    used_resource_refs.add(link.target_ref)
            rows.append(row)
    if len(rows) != record.row_count:
        raise _error("replacement_rows_invalid", "decoded row total is invalid")
    row_by_ref = {row.ref: row for row in rows}
    insert_position = {name: index for index, name in enumerate(_INSERT_ORDER)}
    for row in rows:
        for link in row.links.values():
            if link.kind == "row":
                target = row_by_ref.get(link.target_ref)
                if (
                    target is None
                    or target.table != link.target_table
                    or insert_position[target.table] >= insert_position[row.table]
                ):
                    raise _error("replacement_rows_invalid", "decoded row link target is invalid")
    if used_resource_refs != resource_refs or used_connection_refs != connection_refs:
        raise _error("replacement_descriptors_invalid", "decoded descriptors are not used exactly")
    return tuple(rows)


def _resource_bindings(
    record: DecodedRecord,
    raw_mapping: Mapping[str, ResourceBindingLike],
) -> dict[str, _ResourceBinding]:
    if not isinstance(raw_mapping, Mapping):
        raise _error("resource_bindings_invalid", "resource bindings must be a mapping")
    descriptors = {resource.ref: resource for resource in record.resources}
    if set(raw_mapping) != set(descriptors):
        raise _error("resource_bindings_incomplete", "resource bindings must cover every resource")
    result: dict[str, _ResourceBinding] = {}
    for ref in sorted(descriptors):
        binding = raw_mapping[ref]
        try:
            asset_id = binding.file_asset_id
            storage_ref = binding.storage_ref
        except AttributeError as exc:
            raise _error(
                "resource_binding_invalid", "a resource binding has invalid fields"
            ) from exc
        if not isinstance(asset_id, uuid.UUID) or asset_id.int == 0:
            raise _error("resource_binding_invalid", "bound file asset id is invalid")
        if (
            type(storage_ref) is not str
            or not storage_ref
            or storage_ref != storage_ref.strip()
            or storage_ref.startswith("/")
            or ".." in storage_ref
        ):
            raise _error("resource_binding_invalid", "bound resource locator is invalid")
        result[ref] = _ResourceBinding(
            ref=ref,
            asset_id=asset_id,
            storage_ref=storage_ref,
            descriptor=descriptors[ref],
        )
    return result


def _subject_predicates(subject_id: uuid.UUID) -> dict[str, ColumnElement[bool]]:
    resolved: dict[str, ColumnElement[bool]] = {}
    resolving: set[str] = set()

    def resolve(table_name: str) -> ColumnElement[bool]:
        existing = resolved.get(table_name)
        if existing is not None:
            return existing
        if table_name in resolving:
            raise _error("replacement_schema_invalid", "portable ownership graph is cyclic")
        resolving.add(table_name)
        schema = _SCHEMA_BY_TABLE[table_name]
        table = Base.metadata.tables[table_name]
        inherited = schema["ownership"]["subject"] == "inherited"
        if inherited:
            parents = [
                link
                for link in schema["links"]
                if link["kind"] == "row" and link["required"] is True
            ]
            if len(parents) != 1:
                raise _error("replacement_schema_invalid", "inherited owner is ambiguous")
            parent_link = parents[0]
            parent_name = parent_link["target_table"]
            parent_table = Base.metadata.tables[parent_name]
            predicate = table.c[parent_link["name"]].in_(
                select(parent_table.c[parent_link["target_column"]]).where(resolve(parent_name))
            )
        else:
            predicate = table.c.subject_id == subject_id
        resolving.remove(table_name)
        resolved[table_name] = predicate
        return predicate

    for name in _INSERT_ORDER:
        resolve(name)
    return resolved


async def _validate_subject_and_scopes(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    predicates: Mapping[str, ColumnElement[bool]],
    retained_ids: tuple[int, ...],
) -> None:
    subject = Base.metadata.tables["health_subjects"]
    locked_subject_id = await session.scalar(
        select(subject.c.id).where(subject.c.id == subject_id).with_for_update()
    )
    if locked_subject_id != subject_id:
        raise _error("replacement_subject_missing", "target subject does not exist")

    for table_name, schema in _SCHEMA_BY_TABLE.items():
        if schema["ownership"]["subject"] != "inherited":
            continue
        table = Base.metadata.tables[table_name]
        cross_count = int(
            await session.scalar(
                select(func.count())
                .select_from(table)
                .where(
                    predicates[table_name],
                    table.c.subject_id.is_not(None),
                    table.c.subject_id != subject_id,
                )
            )
            or 0
        )
        direct_count = int(
            await session.scalar(
                select(func.count()).select_from(table).where(table.c.subject_id == subject_id)
            )
            or 0
        )
        reachable_direct_count = int(
            await session.scalar(
                select(func.count())
                .select_from(table)
                .where(predicates[table_name], table.c.subject_id == subject_id)
            )
            or 0
        )
        if cross_count or direct_count != reachable_direct_count:
            raise _error(
                "replacement_scope_corrupt",
                f"table {table_name!r} has incoherent subject ownership",
            )

    if retained_ids:
        raw_table = Base.metadata.tables["raw_payloads"]
        rows = (
            await session.execute(
                select(raw_table.c.id, raw_table.c.subject_id)
                .where(raw_table.c.id.in_(retained_ids))
                .with_for_update()
            )
        ).all()
        if len(rows) != len(retained_ids) or any(row.subject_id != subject_id for row in rows):
            raise _error(
                "retained_raw_scope_invalid",
                "a retained raw payload is missing or belongs to another subject",
            )


async def _validate_connections(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    record: DecodedRecord,
    mapping: CanonicalConnectionMapping,
) -> dict[str, uuid.UUID]:
    if not isinstance(mapping, CanonicalConnectionMapping):
        raise TypeError("connection_mapping must be a CanonicalConnectionMapping")
    if mapping.target_subject_id != subject_id:
        raise _error(
            "connection_mapping_subject_invalid", "connection mapping targets another subject"
        )
    try:
        canonical = await resolve_connection_mapping(
            session,
            target_subject_id=subject_id,
            archive_connections=record.connections,
            connection_ids_by_ref=dict(mapping),
        )
    except ConnectionMappingError as exc:
        raise _error("connection_mapping_invalid", "connection mapping is no longer valid") from exc
    if canonical != mapping:
        raise _error("connection_mapping_invalid", "connection mapping is not canonical")
    by_ref = dict(mapping)
    if by_ref:
        table = Base.metadata.tables["integration_connections"]
        rows = (
            await session.execute(
                select(
                    table.c.id,
                    table.c.subject_id,
                    table.c.provider,
                    table.c.connection_type,
                    table.c.status,
                )
                .where(table.c.id.in_(by_ref.values()))
                .with_for_update()
            )
        ).all()
        expected = {
            connection.ref: (connection.provider, connection.connection_type)
            for connection in record.connections
        }
        rows_by_id = {row.id: row for row in rows}
        if len(rows_by_id) != len(by_ref):
            raise _error("connection_mapping_invalid", "a mapped connection is missing")
        for ref, connection_id in by_ref.items():
            row = rows_by_id[connection_id]
            if (
                row.subject_id != subject_id
                or row.status not in _USABLE_CONNECTION_STATUSES
                or (row.provider, row.connection_type) != expected[ref]
            ):
                raise _error("connection_mapping_invalid", "a mapped connection is out of scope")
    return by_ref


async def _validate_resources(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    bindings: Mapping[str, _ResourceBinding],
) -> None:
    if not bindings:
        return
    table = Base.metadata.tables["file_assets"]
    rows = (
        await session.execute(
            select(
                table.c.id,
                table.c.subject_id,
                table.c.purpose,
                table.c.storage_ref,
                table.c.media_type,
                table.c.byte_size,
                table.c.sha256_hex,
                table.c.status,
                table.c.deleted_at,
                table.c.purged_at,
            )
            .where(table.c.id.in_(binding.asset_id for binding in bindings.values()))
            .with_for_update()
        )
    ).all()
    rows_by_id = {row.id: row for row in rows}
    if len(rows_by_id) != len({binding.asset_id for binding in bindings.values()}):
        raise _error("resource_binding_invalid", "a bound file asset does not exist")
    for binding in bindings.values():
        row = rows_by_id[binding.asset_id]
        descriptor = binding.descriptor
        if (
            row.subject_id != subject_id
            or row.status != FileAssetStatus.ACTIVE.value
            or row.deleted_at is not None
            or row.purged_at is not None
            or row.storage_ref != binding.storage_ref
            or row.purpose != descriptor.purpose
            or row.media_type != descriptor.media_type
            or row.byte_size != descriptor.byte_size
            or row.sha256_hex != descriptor.sha256_hex
        ):
            raise _error(
                "resource_binding_invalid", "a bound file asset is out of scope or mismatched"
            )


async def _old_file_assets(
    session: AsyncSession,
    *,
    predicates: Mapping[str, ColumnElement[bool]],
    retained_ids: tuple[int, ...],
) -> tuple[uuid.UUID, ...]:
    result: set[uuid.UUID] = set()
    for table_name, schema in _SCHEMA_BY_TABLE.items():
        if not any(link["kind"] == "resource" for link in schema["links"]):
            continue
        table = Base.metadata.tables[table_name]
        predicate = predicates[table_name]
        if table_name == "raw_payloads" and retained_ids:
            predicate = predicate & table.c.id.not_in(retained_ids)
        rows = await session.scalars(
            select(table.c.file_asset_id).where(
                predicate,
                table.c.file_asset_id.is_not(None),
            )
        )
        result.update(rows)
    if retained_ids:
        raw_table = Base.metadata.tables["raw_payloads"]
        retained_assets = await session.scalars(
            select(raw_table.c.file_asset_id).where(
                raw_table.c.id.in_(retained_ids),
                raw_table.c.file_asset_id.is_not(None),
            )
        )
        result.difference_update(retained_assets)
    return tuple(sorted(result, key=lambda item: item.hex))


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _insert_values(
    row: DecodedRow,
    schema: Mapping[str, Any],
    *,
    subject_id: uuid.UUID,
    row_ids: Mapping[str, int],
    connections: Mapping[str, uuid.UUID],
    resources: Mapping[str, _ResourceBinding],
) -> dict[str, object]:
    values = {name: _thaw(value) for name, value in row.values.items()}
    values["subject_id"] = subject_id
    for suppressed in schema["suppressed_fields"]:
        values[suppressed["name"]] = None
    for name, link in row.links.items():
        if link.kind == "row":
            try:
                values[name] = row_ids[link.target_ref]
            except KeyError:
                raise _error(
                    "replacement_dependency_missing", "row dependency is not inserted"
                ) from None
        elif link.kind == "connection":
            values[name] = connections[link.target_ref]
        elif link.kind == "resource":
            values[name] = resources[link.target_ref].asset_id
        else:
            raise _error("replacement_rows_invalid", "decoded link kind is invalid")
    for rebuilt in schema["rebuilt_fields"]:
        name = rebuilt["name"]
        mode = rebuilt["mode"]
        resource = (
            resources.get(row.links["file_asset_id"].target_ref)
            if "file_asset_id" in row.links
            else None
        )
        if mode == "target_subject":
            values[name] = subject_id
        elif mode == "resource_storage_ref":
            if resource is None:
                raise _error("replacement_resource_missing", "required resource locator is missing")
            values[name] = resource.storage_ref
        elif mode == "resource_storage_ref_or_null":
            values[name] = None if resource is None else resource.storage_ref
        elif mode == "resource_storage_ref_when_file_link":
            if resource is not None:
                values[name] = resource.storage_ref
        else:
            raise _error("replacement_schema_invalid", "unknown rebuilt field mode")
    return values


async def apply_record_replacement(
    session: AsyncSession,
    *,
    target_subject_id: uuid.UUID,
    record: DecodedRecord,
    connection_mapping: CanonicalConnectionMapping,
    resource_bindings: Mapping[str, ResourceBindingLike],
    retained_raw_payload_ids: Sequence[int],
) -> ReplacementApplyResult:
    """Replace one subject graph, flush, and leave transaction outcome to caller."""

    if not isinstance(session, AsyncSession):
        raise TypeError("session must be an AsyncSession")
    subject_id = _require_subject_id(target_subject_id)
    _record_rows(record)
    retained_ids = _retained_ids(retained_raw_payload_ids)
    resources = _resource_bindings(record, resource_bindings)
    predicates = _subject_predicates(subject_id)
    try:
        with session.no_autoflush:
            await _validate_subject_and_scopes(
                session,
                subject_id=subject_id,
                predicates=predicates,
                retained_ids=retained_ids,
            )
            connections = await _validate_connections(
                session,
                subject_id=subject_id,
                record=record,
                mapping=connection_mapping,
            )
            await _validate_resources(
                session,
                subject_id=subject_id,
                bindings=resources,
            )
            old_file_asset_ids = await _old_file_assets(
                session,
                predicates=predicates,
                retained_ids=retained_ids,
            )

            deleted: list[TableApplyCount] = []
            for table_name in _DELETE_ORDER:
                table = Base.metadata.tables[table_name]
                predicate = predicates[table_name]
                if table_name == "raw_payloads" and retained_ids:
                    predicate = predicate & table.c.id.not_in(retained_ids)
                result = await session.execute(delete(table).where(predicate))
                deleted.append(TableApplyCount(table=table_name, rows=result.rowcount))

            row_ids: dict[str, int] = {}
            inserted: list[TableApplyCount] = []
            rows_by_table = {table.name: table.rows for table in record.tables}
            for table_name in _INSERT_ORDER:
                table = Base.metadata.tables[table_name]
                schema = _SCHEMA_BY_TABLE[table_name]
                primary = next(iter(table.primary_key.columns))
                count = 0
                for row in rows_by_table[table_name]:
                    values = _insert_values(
                        row,
                        schema,
                        subject_id=subject_id,
                        row_ids=row_ids,
                        connections=connections,
                        resources=resources,
                    )
                    new_id = (
                        await session.execute(insert(table).values(**values).returning(primary))
                    ).scalar_one()
                    if type(new_id) is not int or new_id <= 0:
                        raise _error(
                            "replacement_primary_key_invalid", "database returned an invalid PK"
                        )
                    row_ids[row.ref] = new_id
                    count += 1
                inserted.append(TableApplyCount(table=table_name, rows=count))
        await session.flush()
    except ReplacementApplyError:
        raise
    except SQLAlchemyError as exc:
        raise _error(
            "replacement_database_rejected", "database rejected record replacement"
        ) from exc
    return ReplacementApplyResult(
        row_ids_by_ref=MappingProxyType(dict(sorted(row_ids.items()))),
        old_file_asset_ids=old_file_asset_ids,
        deleted=tuple(deleted),
        inserted=tuple(inserted),
    )


__all__ = [
    "ReplacementApplyError",
    "ReplacementApplyResult",
    "ResourceBindingLike",
    "TableApplyCount",
    "apply_record_replacement",
]
