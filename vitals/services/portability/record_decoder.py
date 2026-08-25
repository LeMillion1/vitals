"""Strict, schema-aware decoding of validated portability-v2 record rows.

This layer is intentionally pure.  It consumes an authenticated
``ValidatedArchive`` while its reader context is alive, decodes row values into
their reviewed Python types, verifies typed graph edges, and returns a deeply
immutable snapshot.  It never accepts a database session, writes a resource,
or mutates application state.
"""

from __future__ import annotations

import copy
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Final

from vitals.services.portability.archive_reader import (
    ArchiveReadError,
    ValidatedArchive,
    iter_validated_table_rows,
    validated_record_manifest,
)
from vitals.services.portability.schema import (
    PORTABILITY_SCHEMA_DESCRIPTOR,
    PORTABILITY_SCHEMA_DIGEST,
    schema_digest,
)


_RECORD_KEYS: Final = frozenset(
    {
        "ref",
        "record_digest",
        "connections",
        "resources",
        "schema_digest",
        "tables",
        "totals",
    }
)
_ROW_KEYS: Final = (
    frozenset({"ref", "values"}),
    frozenset({"ref", "values", "links"}),
)


class RecordDecodeError(ValueError):
    """A validated container violates the reviewed row-level schema contract."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, slots=True)
class DecodedConnection:
    ref: str
    provider: str
    connection_type: str


@dataclass(frozen=True, slots=True)
class DecodedResource:
    ref: str
    purpose: str
    media_type: str
    byte_size: int
    sha256_hex: str
    object_path: str


@dataclass(frozen=True, slots=True)
class DecodedLink:
    column: str
    kind: str
    target_ref: str
    target_table: str
    target_column: str
    required: bool


@dataclass(frozen=True, slots=True)
class DecodedRow:
    table: str
    ref: str
    values: Mapping[str, Any]
    links: Mapping[str, DecodedLink]


@dataclass(frozen=True, slots=True)
class DecodedTable:
    name: str
    rows: tuple[DecodedRow, ...]


@dataclass(frozen=True, slots=True)
class DecodedRecord:
    """Deeply immutable rows ordered for dependency-safe insertion."""

    record_ref: str
    schema_digest: str
    connections: tuple[DecodedConnection, ...]
    resources: tuple[DecodedResource, ...]
    tables: tuple[DecodedTable, ...]
    row_count: int


def _error(code: str, detail: str) -> RecordDecodeError:
    return RecordDecodeError(code, detail)


def _freeze_json(value: object, *, path: str) -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _error("value_invalid", f"{path} contains a non-finite JSON number")
        return value
    if type(value) in {list, tuple}:
        return tuple(
            _freeze_json(item, path=f"{path}[{index}]") for index, item in enumerate(value)
        )
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _error("value_invalid", f"{path} contains a non-string JSON key")
            frozen[key] = _freeze_json(item, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    raise _error("value_invalid", f"{path} contains a non-JSON value")


def _parse_date(value: object, *, path: str) -> date:
    if type(value) is not str:
        raise _error("value_type_invalid", f"{path} must use the date codec")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise _error("value_type_invalid", f"{path} must use the date codec") from None
    if result.isoformat() != value:
        raise _error("value_type_invalid", f"{path} is not canonical ISO 8601")
    return result


def _parse_datetime(value: object, descriptor: Mapping[str, Any], *, path: str) -> datetime:
    if type(value) is not str:
        raise _error("value_type_invalid", f"{path} must use the datetime codec")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise _error("value_type_invalid", f"{path} must use the datetime codec") from None
    if result.isoformat() != value:
        raise _error("value_type_invalid", f"{path} is not canonical ISO 8601")
    timezone_expected = descriptor.get("timezone")
    if type(timezone_expected) is not bool:
        raise _error("schema_codec_invalid", "datetime codec has invalid parameters")
    if (result.tzinfo is not None) != timezone_expected:
        raise _error("value_type_invalid", f"{path} has invalid timezone semantics")
    return result


def _parse_time(value: object, descriptor: Mapping[str, Any], *, path: str) -> time:
    if type(value) is not str:
        raise _error("value_type_invalid", f"{path} must use the time codec")
    try:
        result = time.fromisoformat(value)
    except ValueError:
        raise _error("value_type_invalid", f"{path} must use the time codec") from None
    if result.isoformat() != value:
        raise _error("value_type_invalid", f"{path} is not canonical ISO 8601")
    timezone_expected = descriptor.get("timezone")
    if type(timezone_expected) is not bool:
        raise _error("schema_codec_invalid", "time codec has invalid parameters")
    if (result.tzinfo is not None) != timezone_expected:
        raise _error("value_type_invalid", f"{path} has invalid timezone semantics")
    return result


def _decode_nonnull(value: object, descriptor: Mapping[str, Any], *, path: str) -> object:
    codec = descriptor.get("codec")
    if codec == "boolean":
        if type(value) is not bool:
            raise _error("value_type_invalid", f"{path} must be a boolean")
        return value
    if codec == "integer":
        bits = descriptor.get("bits")
        if type(bits) is not int or bits <= 0 or type(value) is not int:
            raise _error("value_type_invalid", f"{path} must be an integer")
        minimum = -(2 ** (bits - 1))
        maximum = 2 ** (bits - 1) - 1
        if not minimum <= value <= maximum:
            raise _error("value_range_invalid", f"{path} exceeds its integer range")
        return value
    if codec == "float":
        if type(value) is not float or not math.isfinite(value):
            raise _error("value_type_invalid", f"{path} must be a finite float")
        return value
    if codec == "decimal_string":
        if type(value) is not str:
            raise _error("value_type_invalid", f"{path} must be a decimal string")
        try:
            result = Decimal(value)
        except InvalidOperation:
            raise _error("value_type_invalid", f"{path} must be a decimal string") from None
        if not result.is_finite() or str(result) != value:
            raise _error("value_type_invalid", f"{path} must be a canonical decimal")
        return result
    if codec == "string":
        if type(value) is not str:
            raise _error("value_type_invalid", f"{path} must be a string")
        length = descriptor.get("length")
        if length is not None and (type(length) is not int or length <= 0 or len(value) > length):
            raise _error("value_range_invalid", f"{path} exceeds its string limit")
        return value
    if codec == "date_iso8601":
        return _parse_date(value, path=path)
    if codec == "datetime_iso8601":
        return _parse_datetime(value, descriptor, path=path)
    if codec == "time_iso8601":
        return _parse_time(value, descriptor, path=path)
    if codec == "uuid":
        if type(value) is not str:
            raise _error("value_type_invalid", f"{path} must be a UUID")
        try:
            result = uuid.UUID(value)
        except ValueError:
            raise _error("value_type_invalid", f"{path} must be a UUID") from None
        if str(result) != value:
            raise _error("value_type_invalid", f"{path} must be a canonical UUID")
        return result
    if codec == "json":
        return _freeze_json(value, path=path)
    raise _error("schema_codec_invalid", "schema contains an unsupported value codec")


def _decode_value(value: object, column: Mapping[str, Any], *, table: str) -> object:
    name = column["name"]
    if value is None:
        if column["nullable"] is not True:
            raise _error("value_null_invalid", f"{table}.{name} may not be null")
        return None
    return _decode_nonnull(value, column["type"], path=f"{table}.{name}")


def _load_schema_tables() -> tuple[dict[str, Mapping[str, Any]], tuple[str, ...]]:
    if schema_digest(PORTABILITY_SCHEMA_DESCRIPTOR) != PORTABILITY_SCHEMA_DIGEST:
        raise _error("schema_digest_mismatch", "runtime schema descriptor changed")
    descriptor = copy.deepcopy(PORTABILITY_SCHEMA_DESCRIPTOR)
    tables = descriptor.get("tables")
    insert_order = descriptor.get("insert_order")
    if not isinstance(tables, list) or not isinstance(insert_order, list):
        raise _error("schema_invalid", "runtime schema descriptor is invalid")
    by_name = {table["name"]: table for table in tables}
    if len(by_name) != len(tables) or set(by_name) != set(insert_order):
        raise _error("schema_invalid", "runtime schema table inventory is invalid")
    return by_name, tuple(insert_order)


_SCHEMA_BY_TABLE, _INSERT_ORDER = _load_schema_tables()


def _manifest(archive: ValidatedArchive) -> Mapping[str, Any]:
    try:
        record = validated_record_manifest(archive)
    except ArchiveReadError as exc:
        raise _error(exc.code, str(exc)) from exc
    except (KeyError, TypeError):
        raise _error("validated_archive_unavailable", "validated archive cannot be read") from None
    if not isinstance(record, Mapping) or frozenset(record) != _RECORD_KEYS:
        raise _error("manifest_invalid", "validated archive record is invalid")
    if (
        record.get("schema_digest") != PORTABILITY_SCHEMA_DIGEST
        or archive.schema_digest != PORTABILITY_SCHEMA_DIGEST
        or record.get("ref") != archive.record_ref
        or record.get("record_digest") != archive.record_digest
    ):
        raise _error("manifest_identity_mismatch", "validated archive identity changed")
    return record


def _descriptors(
    record: Mapping[str, Any],
) -> tuple[
    tuple[DecodedConnection, ...],
    tuple[DecodedResource, ...],
    dict[str, DecodedResource],
    dict[str, Mapping[str, Any]],
]:
    connections = tuple(
        DecodedConnection(
            ref=item["ref"],
            provider=item["provider"],
            connection_type=item["connection_type"],
        )
        for item in record["connections"]
    )
    resources = tuple(
        DecodedResource(
            ref=item["ref"],
            purpose=item["purpose"],
            media_type=item["media_type"],
            byte_size=item["byte_size"],
            sha256_hex=item["sha256_hex"],
            object_path=item["object_path"],
        )
        for item in record["resources"]
    )
    resource_by_ref = {item.ref: item for item in resources}
    table_descriptors = {item["name"]: item for item in record["tables"]}
    return connections, resources, resource_by_ref, table_descriptors


def _expected_value_names(
    schema: Mapping[str, Any], links: Mapping[str, object]
) -> tuple[set[str], set[str]]:
    required: set[str] = set()
    forbidden: set[str] = set()
    for column in schema["value_columns"]:
        name = column["name"]
        conditional = column.get("conditional_rebuild")
        if conditional is None:
            required.add(name)
        elif conditional == "resource_storage_ref_when_file_link":
            if "file_asset_id" in links:
                forbidden.add(name)
            else:
                required.add(name)
        else:
            raise _error("schema_invalid", "schema has an unknown conditional rebuild")
    return required, forbidden


def _resource_purpose(link_schema: Mapping[str, Any], values: Mapping[str, object]) -> str:
    rule = link_schema.get("purpose_rule")
    if not isinstance(rule, Mapping):
        raise _error("schema_invalid", "resource link has no purpose rule")
    if rule.get("mode") == "fixed" and type(rule.get("purpose")) is str:
        return rule["purpose"]
    if rule.get("mode") == "value_map":
        column = rule.get("column")
        mapping = rule.get("mapping")
        if type(column) is str and isinstance(mapping, Mapping):
            purpose = mapping.get(values.get(column))
            if type(purpose) is str:
                return purpose
    raise _error("resource_purpose_invalid", "row has no reviewed resource purpose")


def _decode_row(
    raw: object,
    *,
    table_name: str,
    schema: Mapping[str, Any],
    connection_refs: frozenset[str],
    resource_by_ref: Mapping[str, DecodedResource],
) -> DecodedRow:
    if not isinstance(raw, Mapping) or frozenset(raw) not in _ROW_KEYS:
        raise _error("row_invalid", f"table {table_name!r} has an invalid row shape")
    ref = raw["ref"]
    raw_values = raw["values"]
    raw_links = raw.get("links", {})
    if (
        type(ref) is not str
        or not isinstance(raw_values, Mapping)
        or not isinstance(raw_links, Mapping)
    ):
        raise _error("row_invalid", f"table {table_name!r} has an invalid row")

    value_schema = {item["name"]: item for item in schema["value_columns"]}
    link_schema = {item["name"]: item for item in schema["links"]}
    required_values, conditionally_forbidden = _expected_value_names(schema, raw_links)
    value_names = set(raw_values)
    forbidden_names = {
        item["name"]
        for category in (
            schema["primary_key"]["columns"],
            schema["rebuilt_fields"],
            schema["suppressed_fields"],
        )
        for item in category
    } - set(value_schema)
    leaked = value_names & (forbidden_names | conditionally_forbidden)
    if leaked:
        raise _error("field_forbidden", f"table {table_name!r} contains a rebuilt/private field")
    if value_names != required_values:
        raise _error("value_fields_invalid", f"table {table_name!r} value fields are not exact")

    link_names = set(raw_links)
    if link_names & forbidden_names:
        raise _error("field_forbidden", f"table {table_name!r} links a rebuilt/private field")
    if not link_names.issubset(link_schema):
        raise _error("link_fields_invalid", f"table {table_name!r} has an unknown link")
    required_links = {name for name, item in link_schema.items() if item["required"] is True}
    if not required_links.issubset(link_names):
        raise _error("required_link_missing", f"table {table_name!r} lacks a required link")

    decoded_values = {
        name: _decode_value(raw_values[name], value_schema[name], table=table_name)
        for name in sorted(required_values)
    }
    decoded_links: dict[str, DecodedLink] = {}
    for name in sorted(link_names):
        target_ref = raw_links[name]
        item = link_schema[name]
        if type(target_ref) is not str or not target_ref.startswith(item["ref_kind"]):
            raise _error("link_ref_invalid", f"table {table_name!r} has an invalid link ref")
        kind = item["kind"]
        if kind == "connection":
            if target_ref not in connection_refs:
                raise _error("link_ref_invalid", "connection link is not declared")
        elif kind == "resource":
            resource = resource_by_ref.get(target_ref)
            if resource is None:
                raise _error("link_ref_invalid", "resource link is not declared")
            if resource.purpose != _resource_purpose(item, decoded_values):
                raise _error(
                    "resource_purpose_invalid", "resource purpose disagrees with row schema"
                )
        elif kind != "row":
            raise _error("schema_invalid", "schema has an unknown link kind")
        decoded_links[name] = DecodedLink(
            column=name,
            kind=kind,
            target_ref=target_ref,
            target_table=item["target_table"],
            target_column=item["target_column"],
            required=item["required"],
        )
    return DecodedRow(
        table=table_name,
        ref=ref,
        values=MappingProxyType(decoded_values),
        links=MappingProxyType(decoded_links),
    )


def decode_validated_record(archive: ValidatedArchive) -> DecodedRecord:
    """Decode one complete personal snapshot without any external mutation."""

    if not isinstance(archive, ValidatedArchive):
        raise TypeError("archive must be a ValidatedArchive")
    record = _manifest(archive)
    connections, resources, resource_by_ref, table_descriptors = _descriptors(record)
    if set(table_descriptors) != set(_SCHEMA_BY_TABLE):
        raise _error("table_set_invalid", "record must contain the complete portable table set")

    connection_refs = frozenset(item.ref for item in connections)
    rows_by_table: dict[str, list[DecodedRow]] = {}
    row_index: dict[str, DecodedRow] = {}
    for table_name in sorted(table_descriptors):
        table_descriptor = table_descriptors[table_name]
        rows: list[DecodedRow] = []
        try:
            for raw in iter_validated_table_rows(archive, table_name):
                row = _decode_row(
                    raw,
                    table_name=table_name,
                    schema=_SCHEMA_BY_TABLE[table_name],
                    connection_refs=connection_refs,
                    resource_by_ref=resource_by_ref,
                )
                if row.ref in row_index:
                    raise _error("row_ref_duplicate", "row ref is duplicated")
                row_index[row.ref] = row
                rows.append(row)
        except RecordDecodeError:
            raise
        except ArchiveReadError as exc:
            raise _error(exc.code, str(exc)) from exc
        if len(rows) != table_descriptor["rows"]:
            raise _error("row_count_invalid", f"table {table_name!r} row count changed")
        rows_by_table[table_name] = rows

    insert_position = {name: index for index, name in enumerate(_INSERT_ORDER)}
    for table_name, rows in rows_by_table.items():
        for row in rows:
            for link in row.links.values():
                if link.kind != "row":
                    continue
                target = row_index.get(link.target_ref)
                if target is None or target.table != link.target_table:
                    raise _error("row_link_target_invalid", "row link targets the wrong table")
                if insert_position[target.table] >= insert_position[table_name]:
                    raise _error("dependency_order_invalid", "row link violates dependency order")

    tables = tuple(
        DecodedTable(name=name, rows=tuple(rows_by_table[name])) for name in _INSERT_ORDER
    )
    row_count = sum(len(table.rows) for table in tables)
    if (
        row_count != archive.row_count
        or len(connections) != archive.connection_count
        or len(resources) != archive.resource_count
        or len(tables) != archive.table_count
    ):
        raise _error("decoded_totals_invalid", "decoded record totals disagree with inspection")
    return DecodedRecord(
        record_ref=archive.record_ref,
        schema_digest=PORTABILITY_SCHEMA_DIGEST,
        connections=connections,
        resources=resources,
        tables=tables,
        row_count=row_count,
    )


__all__ = [
    "DecodedConnection",
    "DecodedLink",
    "DecodedRecord",
    "DecodedResource",
    "DecodedRow",
    "DecodedTable",
    "RecordDecodeError",
    "decode_validated_record",
]
