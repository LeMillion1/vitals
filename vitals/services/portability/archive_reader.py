"""Authenticated, strict inspection of portability-v2 export archives.

The standard-library :class:`zipfile.ZipFile` requires a seekable source.  The
reader therefore decrypts into ``tempfile.TemporaryFile`` with mode ``0600``.
On the supported Unix production platforms this is an anonymous/unlinked
plaintext spool: it has no application-visible path, is never persisted or
renamed, and is closed on context-manager exit.  Authentication completes
before the decryptor releases any plaintext into that spool.

This module only validates and inspects.  It never calls ``extract()``, creates
filesystem paths, opens a database session, or mutates application state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import tempfile
import uuid
import zipfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, BinaryIO, Final, Iterator

from vitals.services.portability import contract
from vitals.services.portability.crypto import PortabilityCryptoError, decrypt_stream
from vitals.services.portability.schema import (
    PORTABILITY_SCHEMA_DESCRIPTOR,
    PORTABILITY_SCHEMA_DIGEST,
)


_INNER_FORMAT: Final = "vitals-portability-archive"
_GRAPH_FORMAT: Final = "vitals-portability-graph"
_FORMAT_VERSION: Final = 2
_FIXED_ZIP_TIME: Final = (1980, 1, 1, 0, 0, 0)
_REGULAR_0600: Final = stat.S_IFREG | 0o600

_RECORD_REF_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ROW_REF_RE: Final = re.compile(r"r[0-9]{12}\Z")
_CONNECTION_REF_RE: Final = re.compile(r"c[0-9]{8}\Z")
_RESOURCE_REF_RE: Final = re.compile(r"f[0-9]{8}\Z")
_TABLE_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_LINK_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_SCHEMA_TABLE_NAMES: Final = frozenset(
    table["name"] for table in PORTABILITY_SCHEMA_DESCRIPTOR["tables"]
)

_TOP_KEYS: Final = frozenset(
    {
        "format",
        "version",
        "archive_id",
        "graph_format",
        "graph_version",
        "records",
        "schema_digest",
        "totals",
    }
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
_CONNECTION_KEYS: Final = frozenset({"ref", "provider", "connection_type"})
_RESOURCE_KEYS: Final = frozenset(
    {
        "ref",
        "purpose",
        "media_type",
        "byte_size",
        "sha256_hex",
        "object_path",
    }
)
_TABLE_KEYS: Final = frozenset(
    {"name", "path", "rows", "byte_size", "sha256_hex"}
)
_RECORD_TOTAL_KEYS: Final = frozenset(
    {"tables", "rows", "connections", "resources"}
)

_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")
_CENTRAL_HEADER = struct.Struct("<IHHHHHHIIIHHHHHII")
_DATA_DESCRIPTOR = struct.Struct("<IIQQ")
_EOCD = struct.Struct("<IHHHHIIH")
_ZIP64_LOCATOR = struct.Struct("<IIQI")
_ZIP64_EOCD = struct.Struct("<IQHHIIQQQQ")

_LOCAL_SIGNATURE: Final = 0x04034B50
_CENTRAL_SIGNATURE: Final = 0x02014B50
_DESCRIPTOR_SIGNATURE: Final = 0x08074B50
_EOCD_SIGNATURE: Final = 0x06054B50
_ZIP64_LOCATOR_SIGNATURE: Final = 0x07064B50
_ZIP64_EOCD_SIGNATURE: Final = 0x06064B50
_ZIP64_LOCAL_EXTRA: Final = struct.pack("<HHQQ", 0x0001, 16, 0, 0)


class ArchiveReadError(ValueError):
    """An encrypted container or inner export failed strict validation."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, slots=True)
class ArchiveReaderLimits:
    """Hard resource and JSON-complexity caps for untrusted archives."""

    max_archive_bytes: int = contract.MAX_PLAINTEXT_BYTES
    max_entries: int = 100_065
    max_manifest_bytes: int = 16 * 1024 * 1024
    max_tables: int = 64
    max_rows: int = 1_000_000
    max_connections: int = 128
    max_resources: int = 100_000
    max_table_bytes: int = 1024 * 1024 * 1024
    max_row_bytes: int = 1024 * 1024 * 1024
    max_resource_bytes: int = 1024 * 1024 * 1024
    max_total_resource_bytes: int = contract.MAX_PLAINTEXT_BYTES
    max_json_depth: int = 64
    max_json_nodes: int = 25_000_000
    max_path_depth: int = 8

    def __post_init__(self) -> None:
        for name in (
            "max_archive_bytes",
            "max_entries",
            "max_manifest_bytes",
            "max_tables",
            "max_rows",
            "max_connections",
            "max_resources",
            "max_table_bytes",
            "max_row_bytes",
            "max_resource_bytes",
            "max_total_resource_bytes",
            "max_json_depth",
            "max_json_nodes",
            "max_path_depth",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.max_archive_bytes > contract.MAX_PLAINTEXT_BYTES:
            raise ValueError("max_archive_bytes exceeds the encrypted-container cap")
        if self.max_total_resource_bytes > self.max_archive_bytes:
            raise ValueError("resource-byte cap exceeds the archive-byte cap")
        if self.max_row_bytes > self.max_table_bytes:
            raise ValueError("row-byte cap exceeds the table-byte cap")


DEFAULT_READER_LIMITS: Final = ArchiveReaderLimits()


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    """Small, immutable metadata safe to show before any import decision."""

    archive_id: uuid.UUID
    manifest_digest: str
    record_ref: str
    record_digest: str
    schema_digest: str | None
    table_count: int
    row_count: int
    connection_count: int
    resource_count: int
    plaintext_bytes: int


@dataclass(frozen=True, slots=True)
class ValidatedArchive:
    """Fully validated archive kept alive only inside the reader context."""

    archive_id: uuid.UUID
    manifest_digest: str
    record_ref: str
    record_digest: str
    schema_digest: str | None
    table_count: int
    row_count: int
    connection_count: int
    resource_count: int
    plaintext_bytes: int
    _record_manifest: Mapping[str, Any] = field(repr=False, compare=False)
    _limits: ArchiveReaderLimits = field(repr=False, compare=False)
    _table_names: frozenset[str] = field(repr=False, compare=False)
    _resource_digests: frozenset[str] = field(repr=False, compare=False)
    _zip_file: zipfile.ZipFile = field(repr=False, compare=False)
    _plaintext_spool: BinaryIO = field(repr=False, compare=False)


@dataclass(slots=True)
class _JsonBudget:
    remaining_nodes: int
    max_depth: int

    def consume(self, value: object) -> None:
        stack: list[tuple[object, int]] = [(value, 1)]
        while stack:
            node, depth = stack.pop()
            if depth > self.max_depth:
                raise _error("json_depth_exceeded", "JSON nesting exceeds the hard limit")
            self.remaining_nodes -= 1
            if self.remaining_nodes < 0:
                raise _error("json_nodes_exceeded", "JSON nodes exceed the hard limit")
            if isinstance(node, Mapping):
                self.remaining_nodes -= len(node)
                if self.remaining_nodes < 0:
                    raise _error("json_nodes_exceeded", "JSON nodes exceed the hard limit")
                stack.extend((child, depth + 1) for child in node.values())
            elif isinstance(node, list):
                stack.extend((child, depth + 1) for child in node)


@dataclass(frozen=True, slots=True)
class _TableDescriptor:
    name: str
    path: str
    rows: int
    byte_size: int
    sha256_hex: str


@dataclass(frozen=True, slots=True)
class _ResourceDescriptor:
    ref: str
    byte_size: int
    sha256_hex: str
    object_path: str


class _DuplicateJsonKey(ValueError):
    pass


class _CappedPlaintextWriter:
    def __init__(self, destination: BinaryIO, *, limit: int) -> None:
        self.destination = destination
        self.limit = limit
        self.written = 0

    def write(self, body: bytes | bytearray | memoryview) -> int:
        size = len(body)
        if self.written + size > self.limit:
            raise OSError("plaintext archive exceeds the reader limit")
        view = memoryview(body)
        while view:
            written = self.destination.write(view)
            if written is None or type(written) is not int or not 1 <= written <= len(view):
                raise OSError("plaintext spool refused data")
            self.written += written
            view = view[written:]
        return size


def _error(code: str, detail: str) -> ArchiveReadError:
    return ArchiveReadError(code, detail)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise _error("json_invalid", "archive JSON is invalid") from None


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _parse_canonical_json(body: bytes, *, budget: _JsonBudget) -> Any:
    try:
        value = json.loads(
            body,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        ValueError,
        RecursionError,
    ):
        raise _error("json_invalid", "archive JSON is invalid") from None
    if _canonical_json(value) != body:
        raise _error("json_noncanonical", "archive JSON is not canonical")
    budget.consume(value)
    return value


def _require_object(value: object, keys: frozenset[str], *, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != keys:
        raise _error(code, "archive object has invalid fields")
    return value


def _nonnegative_int(value: object, *, code: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise _error(code, "archive integer is invalid or exceeds a hard limit")
    return value


def _digest(value: object, *, code: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise _error(code, "archive digest is invalid")
    return value


def _canonical_text(value: object, *, maximum: int, code: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _error(code, "archive text is not canonical")
    return value


def _read_exact(source: BinaryIO, offset: int, size: int) -> bytes:
    source.seek(offset)
    body = source.read(size)
    if not isinstance(body, bytes) or len(body) != size:
        raise _error("zip_structure_invalid", "ZIP structure is truncated")
    return body


def _directory_geometry(source: BinaryIO, entry_count: int) -> tuple[int, int]:
    file_size = os.fstat(source.fileno()).st_size
    if file_size < _EOCD.size:
        raise _error("zip_structure_invalid", "ZIP end record is missing")
    eocd_offset = file_size - _EOCD.size
    values = _EOCD.unpack(_read_exact(source, eocd_offset, _EOCD.size))
    (
        signature,
        disk,
        central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        comment_size,
    ) = values
    if (
        signature != _EOCD_SIGNATURE
        or disk != 0
        or central_disk != 0
        or comment_size != 0
    ):
        raise _error("zip_structure_invalid", "ZIP end record is invalid")

    sentinel = (
        entries_on_disk == 0xFFFF
        or total_entries == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    )
    central_end = eocd_offset
    if sentinel:
        locator_offset = eocd_offset - _ZIP64_LOCATOR.size
        if locator_offset < 0:
            raise _error("zip_structure_invalid", "ZIP64 locator is missing")
        locator = _ZIP64_LOCATOR.unpack(
            _read_exact(source, locator_offset, _ZIP64_LOCATOR.size)
        )
        locator_signature, zip64_disk, zip64_offset, disk_count = locator
        if (
            locator_signature != _ZIP64_LOCATOR_SIGNATURE
            or zip64_disk != 0
            or disk_count != 1
            or zip64_offset + _ZIP64_EOCD.size != locator_offset
        ):
            raise _error("zip_structure_invalid", "ZIP64 locator is invalid")
        zip64 = _ZIP64_EOCD.unpack(
            _read_exact(source, zip64_offset, _ZIP64_EOCD.size)
        )
        (
            zip64_signature,
            record_size,
            made_by,
            required_version,
            zip64_disk_number,
            zip64_central_disk,
            zip64_entries_disk,
            zip64_entries_total,
            zip64_central_size,
            zip64_central_offset,
        ) = zip64
        if (
            zip64_signature != _ZIP64_EOCD_SIGNATURE
            or record_size != 44
            or made_by != 45
            or required_version != 45
            or zip64_disk_number != 0
            or zip64_central_disk != 0
            or zip64_entries_disk != zip64_entries_total
        ):
            raise _error("zip_structure_invalid", "ZIP64 end record is invalid")
        if entries_on_disk != 0xFFFF and entries_on_disk != zip64_entries_disk:
            raise _error("zip_structure_invalid", "ZIP entry counts disagree")
        if total_entries != 0xFFFF and total_entries != zip64_entries_total:
            raise _error("zip_structure_invalid", "ZIP entry counts disagree")
        if central_size != 0xFFFFFFFF and central_size != zip64_central_size:
            raise _error("zip_structure_invalid", "ZIP central sizes disagree")
        if central_offset != 0xFFFFFFFF and central_offset != zip64_central_offset:
            raise _error("zip_structure_invalid", "ZIP central offsets disagree")
        total_entries = zip64_entries_total
        central_size = zip64_central_size
        central_offset = zip64_central_offset
        central_end = zip64_offset

    if entries_on_disk not in {entry_count, 0xFFFF} or total_entries != entry_count:
        raise _error("zip_structure_invalid", "ZIP entry count is invalid")
    if central_offset + central_size != central_end:
        raise _error("zip_structure_invalid", "ZIP central directory range is invalid")
    return central_offset, central_size


def _safe_member_name(name: object, *, limits: ArchiveReaderLimits) -> str:
    if type(name) is not str or not name or len(name) > 512:
        raise _error("zip_member_name_invalid", "ZIP member name is invalid")
    try:
        encoded = name.encode("ascii")
    except UnicodeEncodeError:
        raise _error("zip_member_name_invalid", "ZIP member name is not ASCII") from None
    del encoded
    if (
        name.startswith("/")
        or name.endswith("/")
        or "\\" in name
        or "\x00" in name
    ):
        raise _error("zip_member_name_invalid", "ZIP member name is unsafe")
    parts = name.split("/")
    if len(parts) > limits.max_path_depth or any(part in {"", ".", ".."} for part in parts):
        raise _error("zip_member_name_invalid", "ZIP member path is unsafe")
    return name


def _validate_central_directory(
    source: BinaryIO,
    infos: list[zipfile.ZipInfo],
    *,
    central_offset: int,
    central_size: int,
) -> None:
    cursor = central_offset
    for info in infos:
        fields = _CENTRAL_HEADER.unpack(
            _read_exact(source, cursor, _CENTRAL_HEADER.size)
        )
        (
            signature,
            made_by,
            required_version,
            flags,
            compression,
            modified_time,
            modified_date,
            crc,
            compressed_size,
            file_size,
            name_size,
            extra_size,
            comment_size,
            disk,
            internal_attributes,
            external_attributes,
            local_offset,
        ) = fields
        name = _read_exact(source, cursor + _CENTRAL_HEADER.size, name_size)
        if (
            signature != _CENTRAL_SIGNATURE
            or made_by != (3 << 8 | 45)
            or required_version != 45
            or flags != 0x0008
            or compression != zipfile.ZIP_STORED
            or modified_time != 0
            or modified_date != 33
            or crc != info.CRC
            or compressed_size != info.compress_size
            or file_size != info.file_size
            or name != info.filename.encode("ascii")
            or extra_size != 0
            or comment_size != 0
            or disk != 0
            or internal_attributes != 0
            or external_attributes != (_REGULAR_0600 << 16)
            or local_offset != info.header_offset
        ):
            raise _error("zip_structure_invalid", "ZIP central header is invalid")
        cursor += _CENTRAL_HEADER.size + name_size
    if cursor != central_offset + central_size:
        raise _error("zip_structure_invalid", "ZIP central directory has hidden data")


def _validate_local_ranges(
    source: BinaryIO,
    infos: list[zipfile.ZipInfo],
    *,
    central_offset: int,
) -> None:
    cursor = 0
    for info in infos:
        if info.header_offset != cursor:
            raise _error("zip_range_invalid", "ZIP member ranges overlap or contain gaps")
        fields = _LOCAL_HEADER.unpack(_read_exact(source, cursor, _LOCAL_HEADER.size))
        (
            signature,
            required_version,
            flags,
            compression,
            modified_time,
            modified_date,
            crc,
            compressed_size,
            file_size,
            name_size,
            extra_size,
        ) = fields
        name_offset = cursor + _LOCAL_HEADER.size
        name = _read_exact(source, name_offset, name_size)
        extra = _read_exact(source, name_offset + name_size, extra_size)
        if (
            signature != _LOCAL_SIGNATURE
            or required_version != 45
            or flags != 0x0008
            or compression != zipfile.ZIP_STORED
            or modified_time != 0
            or modified_date != 33
            or crc != 0
            or compressed_size != 0xFFFFFFFF
            or file_size != 0xFFFFFFFF
            or name != info.filename.encode("ascii")
            or extra != _ZIP64_LOCAL_EXTRA
        ):
            raise _error("zip_structure_invalid", "ZIP local header is invalid")
        data_start = name_offset + name_size + extra_size
        descriptor_offset = data_start + info.compress_size
        descriptor = _DATA_DESCRIPTOR.unpack(
            _read_exact(source, descriptor_offset, _DATA_DESCRIPTOR.size)
        )
        descriptor_signature, descriptor_crc, compressed, uncompressed = descriptor
        if (
            descriptor_signature != _DESCRIPTOR_SIGNATURE
            or descriptor_crc != info.CRC
            or compressed != info.compress_size
            or uncompressed != info.file_size
        ):
            raise _error("zip_structure_invalid", "ZIP data descriptor is invalid")
        cursor = descriptor_offset + _DATA_DESCRIPTOR.size
    if cursor != central_offset:
        raise _error("zip_range_invalid", "ZIP local member range is invalid")


def _validate_zip_structure(
    source: BinaryIO,
    archive: zipfile.ZipFile,
    *,
    limits: ArchiveReaderLimits,
) -> list[zipfile.ZipInfo]:
    infos = archive.infolist()
    if not 1 <= len(infos) <= limits.max_entries:
        raise _error("zip_entry_count_exceeded", "ZIP entry count is invalid or capped")
    if archive.comment:
        raise _error("zip_structure_invalid", "ZIP comments are not supported")
    names: set[str] = set()
    total_sizes = 0
    for info in infos:
        name = _safe_member_name(info.filename, limits=limits)
        if name in names:
            raise _error("zip_member_duplicate", "ZIP member name is duplicated")
        names.add(name)
        total_sizes += info.file_size
        if (
            total_sizes > limits.max_archive_bytes
            or info.file_size < 0
            or info.compress_size != info.file_size
            or info.flag_bits != 0x0008
            or info.compress_type != zipfile.ZIP_STORED
            or info.date_time != _FIXED_ZIP_TIME
            or info.create_system != 3
            or info.create_version != 45
            or info.extract_version != 45
            or info.extra
            or info.comment
            or (info.external_attr >> 16) != _REGULAR_0600
        ):
            raise _error("zip_member_invalid", "ZIP member metadata is unsupported")
    central_offset, central_size = _directory_geometry(source, len(infos))
    _validate_central_directory(
        source,
        infos,
        central_offset=central_offset,
        central_size=central_size,
    )
    _validate_local_ranges(source, infos, central_offset=central_offset)
    return infos


def _read_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum: int,
) -> bytes:
    if info.file_size > maximum:
        raise _error("zip_member_bytes_exceeded", "ZIP member exceeds the hard limit")
    try:
        with archive.open(info, mode="r") as source:
            chunks: list[bytes] = []
            total = 0
            while chunk := source.read(min(1024 * 1024, maximum - total + 1)):
                total += len(chunk)
                if total > maximum:
                    raise _error(
                        "zip_member_bytes_exceeded", "ZIP member exceeds the hard limit"
                    )
                chunks.append(chunk)
    except (zipfile.BadZipFile, RuntimeError, OSError):
        raise _error("zip_member_integrity_failed", "ZIP member failed CRC or size checks") from None
    return b"".join(chunks)


def _parse_manifest(
    body: bytes,
    *,
    budget: _JsonBudget,
    limits: ArchiveReaderLimits,
) -> tuple[
    Mapping[str, Any],
    Mapping[str, Any],
    uuid.UUID,
    str,
    str,
    str | None,
    tuple[_TableDescriptor, ...],
    tuple[_ResourceDescriptor, ...],
    frozenset[str],
]:
    if not body.endswith(b"\n") or body.endswith(b"\n\n"):
        raise _error("manifest_noncanonical", "manifest must have one final newline")
    value = _parse_canonical_json(body[:-1], budget=budget)
    if not isinstance(value, Mapping):
        raise _error("manifest_invalid", "manifest must be an object")
    if frozenset(value) != _TOP_KEYS:
        raise _error("manifest_invalid", "manifest has invalid fields")
    schema_digest = _digest(value["schema_digest"], code="schema_digest_invalid")
    if schema_digest != PORTABILITY_SCHEMA_DIGEST:
        raise _error("schema_digest_invalid", "schema digest is not the reviewed contract")
    if (
        value["format"] != _INNER_FORMAT
        or type(value["version"]) is not int
        or value["version"] != _FORMAT_VERSION
        or value["graph_format"] != _GRAPH_FORMAT
        or type(value["graph_version"]) is not int
        or value["graph_version"] != _FORMAT_VERSION
        or value["totals"] != {"records": 1}
        or not isinstance(value["records"], list)
        or len(value["records"]) != 1
    ):
        raise _error("manifest_invalid", "manifest version or record count is invalid")
    try:
        archive_id = uuid.UUID(value["archive_id"])
    except (AttributeError, TypeError, ValueError):
        raise _error("archive_id_invalid", "archive_id is invalid") from None
    if str(archive_id) != value["archive_id"]:
        raise _error("archive_id_invalid", "archive_id is not canonical")

    record = _require_object(value["records"][0], _RECORD_KEYS, code="record_invalid")
    record_ref = record["ref"]
    if type(record_ref) is not str or _RECORD_REF_RE.fullmatch(record_ref) is None:
        raise _error("record_ref_invalid", "record ref is invalid")
    record_digest = _digest(record["record_digest"], code="record_digest_invalid")
    if record["schema_digest"] != schema_digest:
        raise _error("schema_digest_invalid", "record and manifest schema digests differ")

    connections = record["connections"]
    if not isinstance(connections, list) or len(connections) > limits.max_connections:
        raise _error("connections_invalid", "connection count is invalid or capped")
    connection_refs: list[str] = []
    previous_ref = ""
    for raw in connections:
        connection = _require_object(raw, _CONNECTION_KEYS, code="connection_invalid")
        ref = connection["ref"]
        if type(ref) is not str or _CONNECTION_REF_RE.fullmatch(ref) is None or ref <= previous_ref:
            raise _error("connection_invalid", "connections are invalid or unsorted")
        _canonical_text(connection["provider"], maximum=32, code="connection_invalid")
        _canonical_text(
            connection["connection_type"], maximum=32, code="connection_invalid"
        )
        connection_refs.append(ref)
        previous_ref = ref

    resources = record["resources"]
    if not isinstance(resources, list) or len(resources) > limits.max_resources:
        raise _error("resources_invalid", "resource count is invalid or capped")
    resource_descriptors: list[_ResourceDescriptor] = []
    logical_resource_bytes = 0
    previous_ref = ""
    digest_sizes: dict[str, int] = {}
    for raw in resources:
        resource = _require_object(raw, _RESOURCE_KEYS, code="resource_invalid")
        ref = resource["ref"]
        if type(ref) is not str or _RESOURCE_REF_RE.fullmatch(ref) is None or ref <= previous_ref:
            raise _error("resource_invalid", "resources are invalid or unsorted")
        _canonical_text(resource["purpose"], maximum=64, code="resource_invalid")
        _canonical_text(resource["media_type"], maximum=255, code="resource_invalid")
        byte_size = _nonnegative_int(
            resource["byte_size"],
            code="resource_bytes_exceeded",
            maximum=limits.max_resource_bytes,
        )
        sha256_hex = _digest(resource["sha256_hex"], code="resource_digest_invalid")
        object_path = resource["object_path"]
        if object_path != f"objects/sha256/{sha256_hex}":
            raise _error("resource_invalid", "resource object path is invalid")
        existing_size = digest_sizes.setdefault(sha256_hex, byte_size)
        if existing_size != byte_size:
            raise _error("resource_invalid", "equal resource digests have different sizes")
        logical_resource_bytes += byte_size
        if logical_resource_bytes > limits.max_total_resource_bytes:
            raise _error("resource_bytes_exceeded", "resource bytes exceed the hard limit")
        resource_descriptors.append(
            _ResourceDescriptor(
                ref=ref,
                byte_size=byte_size,
                sha256_hex=sha256_hex,
                object_path=object_path,
            )
        )
        previous_ref = ref

    tables = record["tables"]
    if not isinstance(tables, list) or len(tables) > limits.max_tables:
        raise _error("tables_invalid", "table count is invalid or capped")
    table_descriptors: list[_TableDescriptor] = []
    previous_name = ""
    declared_rows = 0
    for raw in tables:
        table = _require_object(raw, _TABLE_KEYS, code="table_invalid")
        name = table["name"]
        if (
            type(name) is not str
            or _TABLE_NAME_RE.fullmatch(name) is None
            or name not in _SCHEMA_TABLE_NAMES
            or name <= previous_name
        ):
            raise _error("table_invalid", "tables are invalid or unsorted")
        path = table["path"]
        if path != f"records/{record_ref}/tables/{name}.jsonl":
            raise _error("table_invalid", "table path is invalid")
        rows = _nonnegative_int(
            table["rows"], code="row_count_exceeded", maximum=limits.max_rows
        )
        declared_rows += rows
        if declared_rows > limits.max_rows:
            raise _error("row_count_exceeded", "row count exceeds the hard limit")
        byte_size = _nonnegative_int(
            table["byte_size"],
            code="table_bytes_exceeded",
            maximum=limits.max_table_bytes,
        )
        table_descriptors.append(
            _TableDescriptor(
                name=name,
                path=path,
                rows=rows,
                byte_size=byte_size,
                sha256_hex=_digest(table["sha256_hex"], code="table_digest_invalid"),
            )
        )
        previous_name = name

    totals = _require_object(record["totals"], _RECORD_TOTAL_KEYS, code="totals_invalid")
    expected_totals = {
        "tables": len(table_descriptors),
        "rows": declared_rows,
        "connections": len(connection_refs),
        "resources": len(resource_descriptors),
    }
    if dict(totals) != expected_totals:
        raise _error("totals_invalid", "record totals do not match descriptors")
    record_body = {
        "connections": connections,
        "resources": resources,
        "schema_digest": schema_digest,
        "tables": tables,
        "totals": totals,
    }
    if hashlib.sha256(_canonical_json(record_body)).hexdigest() != record_digest:
        raise _error("record_digest_invalid", "record digest does not match descriptors")
    return (
        value,
        record,
        archive_id,
        record_ref,
        record_digest,
        schema_digest,
        tuple(table_descriptors),
        tuple(resource_descriptors),
        frozenset(connection_refs),
    )


def _validate_table_entries(
    archive: zipfile.ZipFile,
    info_by_name: Mapping[str, zipfile.ZipInfo],
    tables: tuple[_TableDescriptor, ...],
    *,
    connection_refs: frozenset[str],
    resource_refs: frozenset[str],
    budget: _JsonBudget,
    limits: ArchiveReaderLimits,
) -> tuple[frozenset[str], frozenset[str]]:
    row_refs: set[str] = set()
    pending_row_links: list[str] = []
    used_connections: set[str] = set()
    used_resources: set[str] = set()
    actual_total_rows = 0
    for table in tables:
        info = info_by_name[table.path]
        if info.file_size != table.byte_size:
            raise _error("table_size_invalid", "table size disagrees with the manifest")
        digest = hashlib.sha256()
        actual_bytes = 0
        actual_rows = 0
        try:
            with archive.open(info, mode="r") as source:
                while True:
                    line = source.readline(limits.max_row_bytes + 1)
                    if not line:
                        break
                    actual_bytes += len(line)
                    if (
                        len(line) > limits.max_row_bytes
                        or actual_bytes > limits.max_table_bytes
                        or not line.endswith(b"\n")
                    ):
                        raise _error("row_bytes_exceeded", "JSONL row exceeds a hard limit")
                    digest.update(line)
                    row = _parse_canonical_json(line[:-1], budget=budget)
                    if not isinstance(row, Mapping) or frozenset(row) not in {
                        frozenset({"ref", "values"}),
                        frozenset({"ref", "values", "links"}),
                    }:
                        raise _error("row_invalid", "JSONL row has invalid fields")
                    ref = row["ref"]
                    if type(ref) is not str or _ROW_REF_RE.fullmatch(ref) is None:
                        raise _error("row_ref_invalid", "row ref is invalid")
                    if ref in row_refs:
                        raise _error("row_ref_duplicate", "row ref is duplicated")
                    row_refs.add(ref)
                    if not isinstance(row["values"], Mapping):
                        raise _error("row_invalid", "row values must be an object")
                    links = row.get("links", {})
                    if not isinstance(links, Mapping):
                        raise _error("row_invalid", "row links must be an object")
                    for link_name, target in links.items():
                        if (
                            type(link_name) is not str
                            or _LINK_NAME_RE.fullmatch(link_name) is None
                            or type(target) is not str
                        ):
                            raise _error("row_link_invalid", "row link is invalid")
                        if link_name == "integration_connection_id":
                            if _CONNECTION_REF_RE.fullmatch(target) is None:
                                raise _error("row_link_invalid", "connection link is invalid")
                            used_connections.add(target)
                        elif link_name == "file_asset_id":
                            if _RESOURCE_REF_RE.fullmatch(target) is None:
                                raise _error("row_link_invalid", "resource link is invalid")
                            used_resources.add(target)
                        else:
                            if _ROW_REF_RE.fullmatch(target) is None:
                                raise _error("row_link_invalid", "row link is invalid")
                            pending_row_links.append(target)
                    actual_rows += 1
                    actual_total_rows += 1
                    if actual_total_rows > limits.max_rows:
                        raise _error("row_count_exceeded", "row count exceeds the hard limit")
        except ArchiveReadError:
            raise
        except (zipfile.BadZipFile, RuntimeError, OSError):
            raise _error("zip_member_integrity_failed", "table failed CRC or size checks") from None
        if (
            actual_rows != table.rows
            or actual_bytes != table.byte_size
            or digest.hexdigest() != table.sha256_hex
        ):
            raise _error("table_digest_invalid", "table content does not match its descriptor")
    if not set(pending_row_links).issubset(row_refs):
        raise _error("row_link_dangling", "a row link is dangling")
    if used_connections != set(connection_refs):
        raise _error("connection_link_orphan", "connection descriptors and links differ")
    if used_resources != set(resource_refs):
        raise _error("resource_link_orphan", "resource descriptors and links differ")
    return frozenset(used_connections), frozenset(used_resources)


def _validate_objects(
    archive: zipfile.ZipFile,
    info_by_name: Mapping[str, zipfile.ZipInfo],
    resources: tuple[_ResourceDescriptor, ...],
    *,
    limits: ArchiveReaderLimits,
) -> None:
    by_digest: dict[str, _ResourceDescriptor] = {}
    for resource in resources:
        by_digest.setdefault(resource.sha256_hex, resource)
    physical_total = 0
    for digest_hex in sorted(by_digest):
        resource = by_digest[digest_hex]
        info = info_by_name[resource.object_path]
        if info.file_size != resource.byte_size:
            raise _error("resource_size_invalid", "object size disagrees with the manifest")
        physical_total += resource.byte_size
        if physical_total > limits.max_total_resource_bytes:
            raise _error("resource_bytes_exceeded", "object bytes exceed the hard limit")
        digest = hashlib.sha256()
        total = 0
        try:
            with archive.open(info, mode="r") as source:
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > limits.max_resource_bytes:
                        raise _error(
                            "resource_bytes_exceeded", "object exceeds the hard limit"
                        )
                    digest.update(chunk)
        except ArchiveReadError:
            raise
        except (zipfile.BadZipFile, RuntimeError, OSError):
            raise _error("zip_member_integrity_failed", "object failed CRC or size checks") from None
        if total != resource.byte_size or digest.hexdigest() != digest_hex:
            raise _error("resource_digest_invalid", "object does not match its descriptor")


def _validate_inner_archive(
    spool: BinaryIO,
    *,
    plaintext_bytes: int,
    limits: ArchiveReaderLimits,
) -> ValidatedArchive:
    try:
        archive = zipfile.ZipFile(spool, mode="r")
    except (zipfile.BadZipFile, OSError, ValueError):
        raise _error("zip_invalid", "inner archive is not a valid ZIP") from None
    try:
        infos = _validate_zip_structure(spool, archive, limits=limits)
        if infos[0].filename != "manifest.json":
            raise _error("zip_order_invalid", "manifest.json must be the first member")
        manifest_body = _read_member(
            archive,
            infos[0],
            maximum=limits.max_manifest_bytes,
        )
        budget = _JsonBudget(
            remaining_nodes=limits.max_json_nodes,
            max_depth=limits.max_json_depth,
        )
        (
            _manifest,
            record,
            archive_id,
            record_ref,
            record_digest,
            schema_digest,
            tables,
            resources,
            connection_refs,
        ) = _parse_manifest(manifest_body, budget=budget, limits=limits)
        expected_names = ["manifest.json"]
        expected_names.extend(table.path for table in tables)
        expected_names.extend(
            f"objects/sha256/{digest}"
            for digest in sorted({resource.sha256_hex for resource in resources})
        )
        if [info.filename for info in infos] != expected_names:
            raise _error(
                "zip_members_mismatch",
                "ZIP members are missing, extra, duplicated, or out of order",
            )
        info_by_name = {info.filename: info for info in infos}
        resource_refs = frozenset(resource.ref for resource in resources)
        _validate_table_entries(
            archive,
            info_by_name,
            tables,
            connection_refs=connection_refs,
            resource_refs=resource_refs,
            budget=budget,
            limits=limits,
        )
        _validate_objects(archive, info_by_name, resources, limits=limits)
        totals = record["totals"]
        return ValidatedArchive(
            archive_id=archive_id,
            manifest_digest=hashlib.sha256(manifest_body).hexdigest(),
            record_ref=record_ref,
            record_digest=record_digest,
            schema_digest=schema_digest,
            table_count=totals["tables"],
            row_count=totals["rows"],
            connection_count=totals["connections"],
            resource_count=totals["resources"],
            plaintext_bytes=plaintext_bytes,
            _record_manifest=_freeze_json(record),
            _limits=limits,
            _table_names=frozenset(table.name for table in tables),
            _resource_digests=frozenset(
                resource.sha256_hex for resource in resources
            ),
            _zip_file=archive,
            _plaintext_spool=spool,
        )
    except BaseException:
        archive.close()
        raise


@contextmanager
def open_validated_encrypted_archive(
    source: BinaryIO,
    passphrase: str,
    limits: ArchiveReaderLimits = DEFAULT_READER_LIMITS,
) -> Iterator[ValidatedArchive]:
    """Authenticate, anonymously spool, strictly validate, and inspect one export.

    The yielded handle and its anonymous plaintext spool are valid only inside
    this context.  Wrong passwords and ciphertext tampering fail before yield.
    """

    if not isinstance(limits, ArchiveReaderLimits):
        raise TypeError("limits must be ArchiveReaderLimits")
    with tempfile.TemporaryFile(mode="w+b") as plaintext_spool:
        os.fchmod(plaintext_spool.fileno(), 0o600)
        spool_stat = os.fstat(plaintext_spool.fileno())
        if (
            not stat.S_ISREG(spool_stat.st_mode)
            or stat.S_IMODE(spool_stat.st_mode) != 0o600
            or spool_stat.st_nlink != 0
        ):
            raise _error(
                "plaintext_spool_unsafe",
                "plaintext spool is not anonymous and owner-only",
            )
        capped = _CappedPlaintextWriter(
            plaintext_spool,
            limit=limits.max_archive_bytes,
        )
        try:
            decrypt_stream(source, capped, passphrase=passphrase)
        except PortabilityCryptoError:
            raise _error(
                "encrypted_archive_invalid", "invalid encrypted portability archive"
            ) from None
        plaintext_spool.flush()
        plaintext_spool.seek(0)
        validated = _validate_inner_archive(
            plaintext_spool,
            plaintext_bytes=capped.written,
            limits=limits,
        )
        try:
            yield validated
        finally:
            validated._zip_file.close()


def inspection(archive: ValidatedArchive) -> ArchiveInspection:
    """Return pure immutable inspection metadata without reading archive bytes."""

    if not isinstance(archive, ValidatedArchive):
        raise TypeError("archive must be a ValidatedArchive")
    return ArchiveInspection(
        archive_id=archive.archive_id,
        manifest_digest=archive.manifest_digest,
        record_ref=archive.record_ref,
        record_digest=archive.record_digest,
        schema_digest=archive.schema_digest,
        table_count=archive.table_count,
        row_count=archive.row_count,
        connection_count=archive.connection_count,
        resource_count=archive.resource_count,
        plaintext_bytes=archive.plaintext_bytes,
    )


def _open_validated_member(
    archive: ValidatedArchive,
    path: str,
) -> BinaryIO:
    if not isinstance(archive, ValidatedArchive):
        raise TypeError("archive must be a ValidatedArchive")
    if archive._plaintext_spool.closed or archive._zip_file.fp is None:
        raise _error(
            "archive_context_closed",
            "validated archive members are only available inside the reader context",
        )
    try:
        return archive._zip_file.open(path, mode="r")
    except (KeyError, RuntimeError, ValueError, zipfile.BadZipFile):
        raise _error(
            "validated_member_unavailable",
            "validated archive member is unavailable",
        ) from None


@contextmanager
def open_validated_table(
    archive: ValidatedArchive,
    table_name: str,
) -> Iterator[BinaryIO]:
    """Open one declared, already validated JSONL table inside its context."""

    if type(table_name) is not str or table_name not in archive._table_names:
        raise _error("validated_table_unknown", "table is not declared in the record")
    source = _open_validated_member(
        archive,
        f"records/{archive.record_ref}/tables/{table_name}.jsonl",
    )
    try:
        yield source
    finally:
        source.close()


@contextmanager
def open_validated_resource(
    archive: ValidatedArchive,
    sha256_hex: str,
) -> Iterator[BinaryIO]:
    """Open one declared, already validated content-addressed resource."""

    if type(sha256_hex) is not str or sha256_hex not in archive._resource_digests:
        raise _error(
            "validated_resource_unknown",
            "resource digest is not declared in the record",
        )
    source = _open_validated_member(archive, f"objects/sha256/{sha256_hex}")
    try:
        yield source
    finally:
        source.close()


def validated_record_manifest(archive: ValidatedArchive) -> Mapping[str, Any]:
    """Return the recursively immutable manifest for the one validated record."""

    if not isinstance(archive, ValidatedArchive):
        raise TypeError("archive must be a ValidatedArchive")
    return archive._record_manifest


def iter_validated_table_rows(
    archive: ValidatedArchive,
    table_name: str,
) -> Iterator[Mapping[str, Any]]:
    """Yield immutable canonical rows from one declared table inside the context."""

    if not isinstance(archive, ValidatedArchive):
        raise TypeError("archive must be a ValidatedArchive")
    budget = _JsonBudget(
        remaining_nodes=archive._limits.max_json_nodes,
        max_depth=archive._limits.max_json_depth,
    )
    with open_validated_table(archive, table_name) as source:
        while True:
            line = source.readline(archive._limits.max_row_bytes + 1)
            if not line:
                break
            if len(line) > archive._limits.max_row_bytes or not line.endswith(b"\n"):
                raise _error("row_bytes_exceeded", "JSONL row exceeds a hard limit")
            value = _parse_canonical_json(line[:-1], budget=budget)
            if not isinstance(value, Mapping):  # already guaranteed by inspection
                raise _error("row_invalid", "JSONL row is no longer an object")
            yield _freeze_json(value)


__all__ = [
    "ArchiveInspection",
    "ArchiveReadError",
    "ArchiveReaderLimits",
    "DEFAULT_READER_LIMITS",
    "ValidatedArchive",
    "inspection",
    "iter_validated_table_rows",
    "open_validated_resource",
    "open_validated_table",
    "open_validated_encrypted_archive",
    "validated_record_manifest",
]
