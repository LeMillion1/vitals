"""Deterministic, streaming inner ZIP64 archives for portability v2.

This module writes only the authenticated container's plaintext payload.  It
does not parse imports and accepts a non-seekable output, including the v2
``EncryptingWriter``, so plaintext ZIP bytes never need a filesystem spool.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import stat
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO, Final, Protocol

from vitals.services.portability import contract
from vitals.services.portability.resources import (
    DEFAULT_RESOURCE_LIMITS,
    ResourceArchiveError,
    ResourceLimits,
    ResourceLocations,
    ResourcePlanItem,
    build_resource_plan,
    copy_verified_resource,
    verify_duplicate_resources,
)


_TABLE_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_ROW_REF_RE: Final = re.compile(r"r[0-9]{12}\Z")
_CONNECTION_REF_RE: Final = re.compile(r"c[0-9]{8}\Z")
_RECORD_REF_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_FIXED_ZIP_TIME: Final = (1980, 1, 1, 0, 0, 0)
_GRAPH_KEYS: Final = frozenset(
    {"format", "version", "tables", "connections", "resources", "totals"}
)
_GRAPH_FORMAT: Final = "vitals-portability-graph"
_INNER_FORMAT: Final = "vitals-portability-archive"
_FORMAT_VERSION: Final = 2


class PreparedGraphLike(Protocol):
    manifest: dict[str, Any]
    prepared_resources: tuple[object, ...]


class ArchiveBuildError(ValueError):
    """The public graph, an archive cap, or destination failed closed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    """Hard caps applied before and during inner-archive serialization."""

    max_tables: int = 64
    max_rows: int = 1_000_000
    max_connections: int = 128
    max_table_bytes: int = 1024 * 1024 * 1024
    max_manifest_bytes: int = 16 * 1024 * 1024
    max_archive_bytes: int = contract.MAX_PLAINTEXT_BYTES
    resources: ResourceLimits = DEFAULT_RESOURCE_LIMITS

    def __post_init__(self) -> None:
        for name in (
            "max_tables",
            "max_rows",
            "max_connections",
            "max_table_bytes",
            "max_manifest_bytes",
            "max_archive_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.resources, ResourceLimits):
            raise TypeError("resources must be ResourceLimits")
        if self.max_archive_bytes > contract.MAX_PLAINTEXT_BYTES:
            raise ValueError("max_archive_bytes exceeds the encrypted-container cap")


DEFAULT_ARCHIVE_LIMITS: Final = ArchiveLimits()


@dataclass(frozen=True, slots=True)
class _PreparedTable:
    name: str
    path: str
    lines: tuple[bytes, ...]
    byte_size: int
    sha256_hex: str


def _error(code: str, detail: str) -> ArchiveBuildError:
    return ArchiveBuildError(code, detail)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _error("archive_json_invalid", "the public graph is not canonical JSON") from exc


def _row_line(row: Mapping[str, Any]) -> bytes:
    return _canonical_json(row) + b"\n"


def _prepare_tables(
    raw_tables: object,
    *,
    record_ref: str,
    limits: ArchiveLimits,
) -> tuple[_PreparedTable, ...]:
    if not isinstance(raw_tables, list):
        raise _error("archive_graph_invalid", "tables must be a list")
    if len(raw_tables) > limits.max_tables:
        raise _error("archive_table_count_exceeded", "table count exceeds the hard limit")

    names: set[str] = set()
    row_refs: set[str] = set()
    total_rows = 0
    prepared: list[_PreparedTable] = []
    for table in raw_tables:
        if not isinstance(table, Mapping) or frozenset(table) != {"name", "rows"}:
            raise _error("archive_graph_invalid", "a table descriptor has invalid fields")
        name = table["name"]
        rows = table["rows"]
        if not isinstance(name, str) or _TABLE_NAME_RE.fullmatch(name) is None:
            raise _error("archive_table_name_invalid", "a table name is not path-safe")
        if name in names:
            raise _error("archive_table_duplicate", "a table name is duplicated")
        names.add(name)
        if not isinstance(rows, list):
            raise _error("archive_graph_invalid", "table rows must be a list")
        total_rows += len(rows)
        if total_rows > limits.max_rows:
            raise _error("archive_row_count_exceeded", "row count exceeds the hard limit")

        digest = hashlib.sha256()
        byte_size = 0
        stable_lines: list[bytes] = []
        for row in rows:
            if not isinstance(row, Mapping) or not {"ref", "values"}.issubset(row):
                raise _error("archive_graph_invalid", "a row has invalid fields")
            if frozenset(row) - {"ref", "values", "links"}:
                raise _error("archive_graph_invalid", "a row has unknown fields")
            row_ref = row["ref"]
            if not isinstance(row_ref, str) or _ROW_REF_RE.fullmatch(row_ref) is None:
                raise _error("archive_row_ref_invalid", "a row ref is invalid")
            if row_ref in row_refs:
                raise _error("archive_row_ref_duplicate", "a row ref is duplicated")
            row_refs.add(row_ref)
            if not isinstance(row["values"], Mapping):
                raise _error("archive_graph_invalid", "row values must be an object")
            if "links" in row and not isinstance(row["links"], Mapping):
                raise _error("archive_graph_invalid", "row links must be an object")
            line = _row_line(row)
            byte_size += len(line)
            if byte_size > limits.max_table_bytes:
                raise _error("archive_table_bytes_exceeded", "table bytes exceed the hard limit")
            digest.update(line)
            stable_lines.append(line)
        prepared.append(
            _PreparedTable(
                name=name,
                path=f"records/{record_ref}/tables/{name}.jsonl",
                lines=tuple(stable_lines),
                byte_size=byte_size,
                sha256_hex=digest.hexdigest(),
            )
        )
    return tuple(sorted(prepared, key=lambda table: table.name))


def _resource_manifest(
    raw_resources: list[object], plan: tuple[ResourcePlanItem, ...]
) -> list[dict[str, Any]]:
    object_path_by_ref = {item.resource_ref: item.object_path for item in plan}
    result: list[dict[str, Any]] = []
    for resource in raw_resources:
        # build_resource_plan has already validated exact keys and ref types.
        public = copy.deepcopy(dict(resource))  # type: ignore[arg-type]
        public["object_path"] = object_path_by_ref[public["ref"]]
        result.append(public)
    return sorted(result, key=lambda item: item["ref"])


def _connection_manifest(
    raw_connections: object, *, max_connections: int
) -> list[dict[str, str]]:
    if not isinstance(raw_connections, list):
        raise _error("archive_graph_invalid", "connections must be a list")
    if len(raw_connections) > max_connections:
        raise _error(
            "archive_connection_count_exceeded",
            "connection count exceeds the hard limit",
        )
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_connections:
        if not isinstance(raw, Mapping) or frozenset(raw) != {
            "ref",
            "provider",
            "connection_type",
        }:
            raise _error(
                "archive_connection_invalid",
                "a connection descriptor has invalid fields",
            )
        ref = raw["ref"]
        provider = raw["provider"]
        connection_type = raw["connection_type"]
        if type(ref) is not str or _CONNECTION_REF_RE.fullmatch(ref) is None:
            raise _error("archive_connection_invalid", "a connection ref is invalid")
        if ref in seen:
            raise _error("archive_connection_duplicate", "a connection ref is duplicated")
        if (
            type(provider) is not str
            or not provider.strip()
            or provider != provider.strip()
            or len(provider) > 32
            or type(connection_type) is not str
            or not connection_type.strip()
            or connection_type != connection_type.strip()
            or len(connection_type) > 32
        ):
            raise _error(
                "archive_connection_invalid",
                "a logical connection descriptor is invalid",
            )
        seen.add(ref)
        result.append(
            {
                "ref": ref,
                "provider": provider,
                "connection_type": connection_type,
            }
        )
    return sorted(result, key=lambda item: item["ref"])


def _inner_manifest(
    graph_manifest: Mapping[str, Any],
    tables: tuple[_PreparedTable, ...],
    plan: tuple[ResourcePlanItem, ...],
    *,
    archive_id: uuid.UUID,
    record_ref: str,
    limits: ArchiveLimits,
) -> dict[str, Any]:
    connections = _connection_manifest(
        graph_manifest.get("connections"),
        max_connections=limits.max_connections,
    )
    totals = graph_manifest.get("totals")
    raw_resources = graph_manifest.get("resources")
    if not isinstance(totals, Mapping):
        raise _error("archive_graph_invalid", "graph metadata is invalid")
    if not isinstance(raw_resources, list):  # validated by resource plan, for typing
        raise _error("archive_graph_invalid", "resources must be a list")
    table_rows = sum(len(table.lines) for table in tables)
    expected_totals = {
        "tables": len(tables),
        "rows": table_rows,
        "connections": len(connections),
        "resources": len(plan),
    }
    if dict(totals) != expected_totals:
        raise _error("archive_totals_mismatch", "graph totals do not match its contents")
    record_body = {
        "connections": connections,
        "resources": _resource_manifest(raw_resources, plan),
        "tables": [
            {
                "name": table.name,
                "path": table.path,
                "rows": len(table.lines),
                "byte_size": table.byte_size,
                "sha256_hex": table.sha256_hex,
            }
            for table in tables
        ],
        "totals": expected_totals,
    }
    record_digest = hashlib.sha256(_canonical_json(record_body)).hexdigest()
    return {
        "format": _INNER_FORMAT,
        "version": _FORMAT_VERSION,
        "archive_id": str(archive_id),
        "graph_format": graph_manifest["format"],
        "graph_version": graph_manifest["version"],
        "records": [
            {
                "ref": record_ref,
                "record_digest": record_digest,
                **record_body,
            }
        ],
        "totals": {"records": 1},
    }


class _NonSeekableCappedWriter:
    """Make ZIP output deterministic and enforce the plaintext container cap."""

    def __init__(self, destination: BinaryIO, *, limit: int) -> None:
        self.destination = destination
        self.limit = limit
        self.position = 0

    def tell(self) -> int:
        return self.position

    def seekable(self) -> bool:
        return False

    def write(self, body: bytes | bytearray | memoryview) -> int:
        data = memoryview(body)
        if self.position + len(data) > self.limit:
            raise _error("archive_bytes_exceeded", "inner archive exceeds the hard limit")
        original_size = len(data)
        while data:
            written = self.destination.write(data)
            if written is None:
                raise OSError("archive destination did not report a completed write")
            if type(written) is not int or written <= 0 or written > len(data):
                raise OSError("archive destination refused data")
            self.position += written
            data = data[written:]
        return original_size

    def flush(self) -> None:
        flush = getattr(self.destination, "flush", None)
        if flush is not None:
            flush()


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=_FIXED_ZIP_TIME)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.compress_type = zipfile.ZIP_STORED
    return info


def _write_entry(archive: zipfile.ZipFile, path: str, chunks: object) -> None:
    with archive.open(_zip_info(path), mode="w", force_zip64=True) as destination:
        if isinstance(chunks, bytes):
            destination.write(chunks)
            return
        for chunk in chunks:  # type: ignore[union-attr]
            destination.write(chunk)


def write_inner_archive(
    graph: PreparedGraphLike,
    destination: BinaryIO,
    *,
    archive_id: uuid.UUID,
    record_ref: str,
    locations: ResourceLocations,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> int:
    """Write a deterministic ZIP64 stream and return its plaintext byte count.

    On any exception the destination contains an incomplete artifact and must be
    discarded.  The ZIP central directory is not emitted on failures controlled
    by this function.
    """

    if not isinstance(limits, ArchiveLimits):
        raise TypeError("limits must be ArchiveLimits")
    if not isinstance(locations, ResourceLocations):
        raise TypeError("locations must be ResourceLocations")
    if not isinstance(archive_id, uuid.UUID):
        raise TypeError("archive_id must be a UUID")
    if not isinstance(record_ref, str) or _RECORD_REF_RE.fullmatch(record_ref) is None:
        raise _error("archive_record_ref_invalid", "record_ref is not an opaque safe token")
    try:
        graph_manifest = graph.manifest
        prepared_resources = graph.prepared_resources
    except AttributeError as exc:
        raise _error("archive_graph_invalid", "prepared graph has invalid fields") from exc
    if not isinstance(graph_manifest, Mapping):
        raise _error("archive_graph_invalid", "graph manifest must be an object")
    if frozenset(graph_manifest) != _GRAPH_KEYS:
        raise _error("archive_graph_invalid", "graph manifest has invalid fields")
    if (
        graph_manifest["format"] != _GRAPH_FORMAT
        or type(graph_manifest["version"]) is not int
        or graph_manifest["version"] != _FORMAT_VERSION
    ):
        raise _error("archive_graph_version_invalid", "graph format is unsupported")

    tables = _prepare_tables(
        graph_manifest.get("tables"), record_ref=record_ref, limits=limits
    )
    try:
        plan = build_resource_plan(
            graph_manifest.get("resources"),
            prepared_resources,
            limits=limits.resources,
        )
        # A duplicate digest is stored once, but every referenced asset must
        # still independently pass its own metadata and descriptor verification.
        verify_duplicate_resources(plan, locations=locations)
    except ResourceArchiveError as exc:
        raise _error(exc.code, "a graph resource could not be archived") from exc

    manifest = _inner_manifest(
        graph_manifest,
        tables,
        plan,
        archive_id=archive_id,
        record_ref=record_ref,
        limits=limits,
    )
    manifest_body = _canonical_json(manifest) + b"\n"
    if len(manifest_body) > limits.max_manifest_bytes:
        raise _error("archive_manifest_bytes_exceeded", "manifest exceeds the hard limit")

    writer = _NonSeekableCappedWriter(destination, limit=limits.max_archive_bytes)
    archive = zipfile.ZipFile(writer, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True)
    try:
        _write_entry(archive, "manifest.json", manifest_body)
        for table in tables:
            _write_entry(archive, table.path, table.lines)
        first_by_digest: dict[str, ResourcePlanItem] = {}
        for item in plan:
            first_by_digest.setdefault(item.sha256_hex, item)
        for digest in sorted(first_by_digest):
            item = first_by_digest[digest]
            with archive.open(
                _zip_info(item.object_path), mode="w", force_zip64=True
            ) as resource_destination:
                try:
                    copy_verified_resource(
                        item,
                        resource_destination,
                        locations=locations,
                    )
                except ResourceArchiveError as exc:
                    raise _error(exc.code, "a graph resource could not be archived") from exc
    except BaseException:
        # ZipFile.close() would otherwise emit a valid-looking central directory
        # for a prefix.  Detach the caller-owned destination so this artifact is
        # unambiguously incomplete and the original error is preserved.
        archive.fp = None
        raise
    archive.close()
    return writer.position


__all__ = [
    "ArchiveBuildError",
    "ArchiveLimits",
    "DEFAULT_ARCHIVE_LIMITS",
    "PreparedGraphLike",
    "write_inner_archive",
]
