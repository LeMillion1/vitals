from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import struct
import uuid
import warnings
import zipfile
from types import SimpleNamespace

import pytest

from vitals.enums import FileStorageBackend
from vitals.persistence.file_storage import write_private_file
from vitals.services.portability.archive import write_inner_archive
from vitals.services.portability.archive_reader import (
    ArchiveReadError,
    ArchiveReaderLimits,
    inspection,
    open_validated_encrypted_archive,
)
from vitals.services.portability.crypto import EncryptingWriter
from vitals.services.portability.resources import ResourceLocations
from vitals.services.portability.schema import PORTABILITY_SCHEMA_DIGEST


_PASSPHRASE = "correct horse battery staple"
_ARCHIVE_ID = uuid.UUID("12345678-1234-5678-9234-567812345678")
_RECORD_REF = "record_A-19"


class _NonSeekableSink:
    def __init__(self) -> None:
        self.body = bytearray()

    def write(self, body) -> int:
        self.body.extend(body)
        return len(body)

    def tell(self) -> int:
        return len(self.body)

    def flush(self) -> None:
        pass


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _resource(ref: str, body: bytes) -> dict[str, object]:
    return {
        "ref": ref,
        "purpose": "lab_document",
        "media_type": "application/pdf",
        "byte_size": len(body),
        "sha256_hex": hashlib.sha256(body).hexdigest(),
    }


def _prepared(ref: str, storage_ref: str, body: bytes):
    return SimpleNamespace(
        resource_ref=ref,
        file_asset_id=uuid.uuid4(),
        storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
        storage_ref=storage_ref,
        expected_byte_size=len(body),
        expected_sha256_hex=hashlib.sha256(body).hexdigest(),
    )


def _plain_archive(tmp_path) -> tuple[bytes, bytes]:
    resource_body = b"synthetic private health document"
    storage_ref = "labs/aa/reader.pdf"
    locations = ResourceLocations(
        static_dir=str(tmp_path / "static"),
        private_root=str(tmp_path / "private"),
    )
    write_private_file(locations.private_root, storage_ref, resource_body)
    graph = SimpleNamespace(
        manifest={
            "format": "vitals-portability-graph",
            "version": 2,
            "schema_digest": PORTABILITY_SCHEMA_DIGEST,
            "tables": [
                {
                    "name": "weight_logs",
                    "rows": [
                        {
                            "ref": "r000000000001",
                            "values": {
                                "date": "2026-08-25",
                                "weight_kg": "72.5",
                            },
                            "links": {
                                "file_asset_id": "f00000001",
                                "integration_connection_id": "c00000001",
                            },
                        }
                    ],
                }
            ],
            "connections": [
                {
                    "ref": "c00000001",
                    "provider": "garmin",
                    "connection_type": "oauth",
                }
            ],
            "resources": [_resource("f00000001", resource_body)],
            "totals": {
                "tables": 1,
                "rows": 1,
                "connections": 1,
                "resources": 1,
            },
        },
        prepared_resources=(
            _prepared("f00000001", storage_ref, resource_body),
        ),
    )
    output = io.BytesIO()
    write_inner_archive(
        graph,
        output,
        archive_id=_ARCHIVE_ID,
        record_ref=_RECORD_REF,
        locations=locations,
    )
    return output.getvalue(), resource_body


def _encrypt(plaintext: bytes, *, passphrase: str = _PASSPHRASE) -> bytes:
    output = io.BytesIO()
    with EncryptingWriter(output, passphrase=passphrase) as writer:
        writer.write(plaintext)
    return output.getvalue()


def _entries(plaintext: bytes) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(plaintext)) as archive:
        return [(info.filename, archive.read(info)) for info in archive.infolist()]


def _zip_info(
    name: str,
    *,
    compression: int = zipfile.ZIP_STORED,
    mode: int = stat.S_IFREG | 0o600,
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = mode << 16
    info.compress_type = compression
    return info


def _repack(
    entries: list[tuple[str, bytes]],
    *,
    compression: int = zipfile.ZIP_STORED,
    modes: dict[str, int] | None = None,
) -> bytes:
    destination = _NonSeekableSink()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(
            destination,
            mode="w",
            compression=compression,
            allowZip64=True,
        ) as archive:
            for name, body in entries:
                mode = (modes or {}).get(name, stat.S_IFREG | 0o600)
                with archive.open(
                    _zip_info(name, compression=compression, mode=mode),
                    mode="w",
                    force_zip64=True,
                ) as member:
                    member.write(body)
    return bytes(destination.body)


def _manifest_and_entries(plaintext: bytes):
    entries = _entries(plaintext)
    return json.loads(entries[0][1]), entries


def _refresh_record_digest(manifest: dict) -> None:
    record = manifest["records"][0]
    record_body = {
        key: record[key]
        for key in (
            "connections",
            "resources",
            "schema_digest",
            "tables",
            "totals",
        )
    }
    record["record_digest"] = hashlib.sha256(_canonical(record_body)).hexdigest()


def _replace_manifest(entries, manifest: dict) -> list[tuple[str, bytes]]:
    return [("manifest.json", _canonical(manifest) + b"\n"), *entries[1:]]


def _replace_table(
    plaintext: bytes,
    table_body: bytes,
    *,
    rows: int = 1,
) -> bytes:
    manifest, entries = _manifest_and_entries(plaintext)
    table = manifest["records"][0]["tables"][0]
    table["rows"] = rows
    table["byte_size"] = len(table_body)
    table["sha256_hex"] = hashlib.sha256(table_body).hexdigest()
    manifest["records"][0]["totals"]["rows"] = rows
    _refresh_record_digest(manifest)
    entries = _replace_manifest(entries, manifest)
    entries[1] = (entries[1][0], table_body)
    return _repack(entries)


def _assert_rejected(plaintext: bytes, *, limits: ArchiveReaderLimits | None = None):
    kwargs = {} if limits is None else {"limits": limits}
    with pytest.raises(ArchiveReadError):
        with open_validated_encrypted_archive(
            io.BytesIO(_encrypt(plaintext)),
            passphrase=_PASSPHRASE,
            **kwargs,
        ):
            pytest.fail("invalid archive was yielded")


def test_valid_encrypted_roundtrip_inspection_and_anonymous_spool(tmp_path):
    plaintext, _resource_body = _plain_archive(tmp_path)
    encrypted = _encrypt(plaintext)
    handle = None

    with open_validated_encrypted_archive(
        io.BytesIO(encrypted), passphrase=_PASSPHRASE
    ) as validated:
        handle = validated._plaintext_spool
        result = inspection(validated)
        assert result.archive_id == _ARCHIVE_ID
        assert result.manifest_digest == hashlib.sha256(
            _entries(plaintext)[0][1]
        ).hexdigest()
        assert result.record_ref == _RECORD_REF
        assert len(result.record_digest) == 64
        assert result.schema_digest == PORTABILITY_SCHEMA_DIGEST
        assert (
            result.table_count,
            result.row_count,
            result.connection_count,
            result.resource_count,
        ) == (1, 1, 1, 1)
        assert result.plaintext_bytes == len(plaintext)
        spool_stat = os.fstat(handle.fileno())
        assert stat.S_IMODE(spool_stat.st_mode) == 0o600
        assert spool_stat.st_nlink == 0
        assert not handle.closed

    assert handle is not None and handle.closed


@pytest.mark.parametrize("passphrase", ["wrong password value", _PASSPHRASE])
def test_wrong_password_or_ciphertext_tamper_never_yields(tmp_path, passphrase):
    plaintext, _ = _plain_archive(tmp_path)
    encrypted = bytearray(_encrypt(plaintext))
    if passphrase == _PASSPHRASE:
        encrypted[-20] ^= 0x01
    yielded = False
    with pytest.raises(ArchiveReadError) as raised:
        with open_validated_encrypted_archive(
            io.BytesIO(encrypted), passphrase=passphrase
        ):
            yielded = True
    assert raised.value.code == "encrypted_archive_invalid"
    assert not yielded


@pytest.mark.parametrize("unsafe_name", ["../escape", "/absolute", "a\\b", "a/./b"])
def test_traversal_and_noncanonical_names_are_rejected(tmp_path, unsafe_name):
    plaintext, _ = _plain_archive(tmp_path)
    entries = _entries(plaintext)
    entries.append((unsafe_name, b"extra"))
    _assert_rejected(_repack(entries))


def test_duplicate_member_name_is_rejected(tmp_path):
    plaintext, _ = _plain_archive(tmp_path)
    entries = _entries(plaintext)
    entries.append(entries[-1])
    _assert_rejected(_repack(entries))


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o600, stat.S_IFDIR | 0o700, stat.S_IFCHR | 0o600])
def test_symlink_directory_and_device_members_are_rejected(tmp_path, mode):
    plaintext, _ = _plain_archive(tmp_path)
    entries = _entries(plaintext)
    target = entries[-1][0]
    _assert_rejected(_repack(entries, modes={target: mode}))


def test_compressed_member_is_rejected(tmp_path):
    plaintext, _ = _plain_archive(tmp_path)
    _assert_rejected(_repack(_entries(plaintext), compression=zipfile.ZIP_DEFLATED))


@pytest.mark.parametrize("mutation", ["extra", "missing", "reordered"])
def test_extra_missing_and_reordered_members_are_rejected(tmp_path, mutation):
    plaintext, _ = _plain_archive(tmp_path)
    entries = _entries(plaintext)
    if mutation == "extra":
        entries.append(("objects/sha256/" + "0" * 64, b""))
    elif mutation == "missing":
        entries.pop()
    else:
        entries[1], entries[2] = entries[2], entries[1]
    _assert_rejected(_repack(entries))


def test_noncanonical_and_duplicate_manifest_json_are_rejected(tmp_path):
    plaintext, _ = _plain_archive(tmp_path)
    manifest, entries = _manifest_and_entries(plaintext)
    pretty = json.dumps(manifest, indent=2, ensure_ascii=False).encode() + b"\n"
    _assert_rejected(_repack([("manifest.json", pretty), *entries[1:]]))

    duplicate = b'{"format":"duplicate",' + entries[0][1][1:]
    _assert_rejected(_repack([("manifest.json", duplicate), *entries[1:]]))


def test_duplicate_row_json_key_is_rejected(tmp_path):
    plaintext, _ = _plain_archive(tmp_path)
    duplicate = (
        b'{"links":{"file_asset_id":"f00000001",'
        b'"integration_connection_id":"c00000001"},'
        b'"ref":"r000000000001","values":{},"values":{}}\n'
    )
    _assert_rejected(_replace_table(plaintext, duplicate))


def test_row_depth_node_and_byte_caps_are_enforced(tmp_path):
    plaintext, _ = _plain_archive(tmp_path)
    nested: object = "leaf"
    for _ in range(12):
        nested = [nested]
    row = {
        "links": {
            "file_asset_id": "f00000001",
            "integration_connection_id": "c00000001",
        },
        "ref": "r000000000001",
        "values": {"nested": nested},
    }
    deep_plaintext = _replace_table(plaintext, _canonical(row) + b"\n")
    _assert_rejected(
        deep_plaintext,
        limits=ArchiveReaderLimits(max_json_depth=8),
    )

    nodes_plaintext = _replace_table(
        plaintext,
        _canonical({**row, "values": {"many": list(range(20))}}) + b"\n",
    )
    _assert_rejected(
        nodes_plaintext,
        limits=ArchiveReaderLimits(max_json_nodes=10),
    )

    byte_plaintext = _replace_table(
        plaintext,
        _canonical({**row, "values": {"large": "x" * 100}}) + b"\n",
    )
    _assert_rejected(
        byte_plaintext,
        limits=ArchiveReaderLimits(max_row_bytes=50, max_table_bytes=1000),
    )


@pytest.mark.parametrize(
    "failure",
    ["record_digest", "totals", "record_ref", "archive_id", "schema", "record_count"],
)
def test_manifest_digest_totals_and_identity_fail_closed(tmp_path, failure):
    plaintext, _ = _plain_archive(tmp_path)
    manifest, entries = _manifest_and_entries(plaintext)
    record = manifest["records"][0]
    if failure == "record_digest":
        record["record_digest"] = "0" * 64
    elif failure == "totals":
        record["totals"]["rows"] = 2
        _refresh_record_digest(manifest)
    elif failure == "record_ref":
        record["ref"] = "../unsafe"
        _refresh_record_digest(manifest)
    elif failure == "archive_id":
        manifest["archive_id"] = "not-a-uuid"
    elif failure == "schema":
        manifest["schema_digest"] = "0" * 64
    else:
        manifest["records"].append(record.copy())
        manifest["totals"]["records"] = 2
    _assert_rejected(_repack(_replace_manifest(entries, manifest)))


def test_table_must_belong_to_the_pinned_schema_contract(tmp_path):
    plaintext, _ = _plain_archive(tmp_path)
    manifest, entries = _manifest_and_entries(plaintext)
    table = manifest["records"][0]["tables"][0]
    table["name"] = "invented_health_table"
    table["path"] = f"records/{_RECORD_REF}/tables/invented_health_table.jsonl"
    _refresh_record_digest(manifest)
    entries = _replace_manifest(entries, manifest)
    entries[1] = (table["path"], entries[1][1])
    _assert_rejected(_repack(entries))


@pytest.mark.parametrize("descriptor", ["connection", "resource"])
def test_logical_descriptors_cannot_carry_private_fields(tmp_path, descriptor):
    plaintext, _ = _plain_archive(tmp_path)
    manifest, entries = _manifest_and_entries(plaintext)
    record = manifest["records"][0]
    if descriptor == "connection":
        record["connections"][0]["credential_ref"] = "private-secret"
    else:
        record["resources"][0]["storage_ref"] = "labs/private.pdf"
    _refresh_record_digest(manifest)
    _assert_rejected(_repack(_replace_manifest(entries, manifest)))


def test_row_refs_are_unique_across_the_record(tmp_path):
    plaintext, _ = _plain_archive(tmp_path)
    _manifest, entries = _manifest_and_entries(plaintext)
    duplicated_rows = entries[1][1] * 2
    _assert_rejected(_replace_table(plaintext, duplicated_rows, rows=2))


@pytest.mark.parametrize("failure", ["dangling_row", "orphan_connection", "orphan_resource"])
def test_dangling_and_orphan_links_are_rejected(tmp_path, failure):
    plaintext, _ = _plain_archive(tmp_path)
    manifest, entries = _manifest_and_entries(plaintext)
    row = json.loads(entries[1][1])
    if failure == "dangling_row":
        row["links"]["parent_id"] = "r999999999999"
    elif failure == "orphan_connection":
        del row["links"]["integration_connection_id"]
    else:
        del row["links"]["file_asset_id"]
    _assert_rejected(_replace_table(plaintext, _canonical(row) + b"\n"))


def test_table_and_resource_hashes_are_recomputed(tmp_path):
    plaintext, _ = _plain_archive(tmp_path)
    entries = _entries(plaintext)
    table_body = bytearray(entries[1][1])
    table_body[-2] ^= 0x01
    entries[1] = (entries[1][0], bytes(table_body))
    _assert_rejected(_repack(entries))

    entries = _entries(plaintext)
    resource_body = bytearray(entries[2][1])
    resource_body[0] ^= 0x01
    entries[2] = (entries[2][0], bytes(resource_body))
    _assert_rejected(_repack(entries))


@pytest.mark.parametrize("forgery", ["central_size", "local_name", "descriptor_crc", "encrypted"])
def test_header_size_range_crc_and_encryption_forgeries_are_rejected(tmp_path, forgery):
    plaintext, _ = _plain_archive(tmp_path)
    forged = bytearray(plaintext)
    central = forged.find(b"PK\x01\x02")
    assert central > 0
    if forgery == "central_size":
        size = struct.unpack_from("<I", forged, central + 20)[0]
        struct.pack_into("<I", forged, central + 20, size + 1)
    elif forgery == "local_name":
        forged[30] = ord("n")
    elif forgery == "descriptor_crc":
        descriptor = forged.find(b"PK\x07\x08")
        assert descriptor > 0
        forged[descriptor + 4] ^= 0x01
    else:
        local_flags = struct.unpack_from("<H", forged, 6)[0]
        central_flags = struct.unpack_from("<H", forged, central + 8)[0]
        struct.pack_into("<H", forged, 6, local_flags | 0x0001)
        struct.pack_into("<H", forged, central + 8, central_flags | 0x0001)
    _assert_rejected(bytes(forged))


def test_manifest_and_entry_count_caps_apply_before_content_use(tmp_path):
    plaintext, _ = _plain_archive(tmp_path)
    _assert_rejected(
        plaintext,
        limits=ArchiveReaderLimits(max_entries=2),
    )
    _assert_rejected(
        plaintext,
        limits=ArchiveReaderLimits(max_manifest_bytes=32),
    )
    _assert_rejected(
        plaintext,
        limits=ArchiveReaderLimits(
            max_archive_bytes=len(plaintext) - 1,
            max_total_resource_bytes=len(plaintext) - 1,
        ),
    )
