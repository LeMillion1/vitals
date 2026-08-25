from __future__ import annotations

import hashlib
import io
import json
import stat
import uuid
import zipfile
from types import SimpleNamespace

import pytest

from vitals.enums import FileStorageBackend
from vitals.persistence.file_storage import write_private_file
from vitals.services.portability.archive import (
    ArchiveBuildError,
    ArchiveLimits,
    write_inner_archive,
)
from vitals.services.portability.crypto import EncryptingWriter, decrypt_stream
from vitals.services.portability.graph import build_subject_graph
from vitals.services.portability.resources import ResourceLocations


_ARCHIVE_ID = uuid.UUID("12345678-1234-5678-9234-567812345678")
_RECORD_REF = "record_A-19"


class _PartialNonSeekable:
    def __init__(self) -> None:
        self.body = bytearray()

    def write(self, body) -> int:
        accepted = min(3, len(body))
        self.body.extend(bytes(body[:accepted]))
        return accepted

    def flush(self) -> None:
        pass


class _MutatingNonSeekable(_PartialNonSeekable):
    def __init__(self, callback) -> None:
        super().__init__()
        self.callback = callback
        self.mutated = False

    def write(self, body) -> int:
        if not self.mutated:
            self.callback()
            self.mutated = True
        return super().write(body)


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
        file_asset_id="private-database-identity",
        storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
        storage_ref=storage_ref,
        expected_byte_size=len(body),
        expected_sha256_hex=hashlib.sha256(body).hexdigest(),
    )


def _graph(*, resources=(), prepared=(), table_name="weight_logs"):
    rows = [
        {
            "ref": "r000000000001",
            "values": {"date": "2026-08-25", "weight_kg": "72.5"},
            "links": {"file_asset_id": "f00000001"},
        }
    ]
    manifest = {
        "format": "vitals-portability-graph",
        "version": 2,
        "tables": [{"name": table_name, "rows": rows}],
        "connections": [],
        "resources": list(resources),
        "totals": {
            "tables": 1,
            "rows": 1,
            "connections": 0,
            "resources": len(resources),
        },
    }
    return SimpleNamespace(manifest=manifest, prepared_resources=tuple(prepared))


def _locations(tmp_path):
    return ResourceLocations(
        static_dir=str(tmp_path / "static"),
        private_root=str(tmp_path / "private"),
    )


def _write_archive(graph, destination, *, locations, limits=None):
    kwargs = {
        "archive_id": _ARCHIVE_ID,
        "record_ref": _RECORD_REF,
        "locations": locations,
    }
    if limits is not None:
        kwargs["limits"] = limits
    return write_inner_archive(graph, destination, **kwargs)


def test_archive_is_deterministic_zip64_on_nonseekable_output(tmp_path):
    body = b"synthetic health document"
    storage_ref = "labs/aa/document.pdf"
    locations = _locations(tmp_path)
    write_private_file(locations.private_root, storage_ref, body)
    graph = _graph(
        resources=[_resource("f00000001", body)],
        prepared=[_prepared("f00000001", storage_ref, body)],
    )

    first = _PartialNonSeekable()
    second = _PartialNonSeekable()
    first_size = _write_archive(graph, first, locations=locations)
    second_size = _write_archive(graph, second, locations=locations)

    assert first_size == second_size == len(first.body)
    assert bytes(first.body) == bytes(second.body)
    with zipfile.ZipFile(io.BytesIO(first.body)) as archive:
        digest = hashlib.sha256(body).hexdigest()
        assert archive.namelist() == [
            "manifest.json",
            f"records/{_RECORD_REF}/tables/weight_logs.jsonl",
            f"objects/sha256/{digest}",
        ]
        assert archive.read(f"objects/sha256/{digest}") == body
        assert json.loads(
            archive.read(f"records/{_RECORD_REF}/tables/weight_logs.jsonl")
        ) == {
            "links": {"file_asset_id": "f00000001"},
            "ref": "r000000000001",
            "values": {"date": "2026-08-25", "weight_kg": "72.5"},
        }
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert stat.S_IMODE(info.external_attr >> 16) == 0o600
            assert stat.S_ISREG(info.external_attr >> 16)
            assert info.compress_type == zipfile.ZIP_STORED
            assert info.extract_version >= 45


async def test_archive_consumes_the_real_prepared_subject_graph(
    db_session,
    legacy_owner_roots,
    tmp_path,
):
    graph = await build_subject_graph(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
    )
    output = io.BytesIO()

    _write_archive(graph, output, locations=_locations(tmp_path))

    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        record = manifest["records"][0]
        assert record["totals"] == graph.manifest["totals"]
        assert len(record["tables"]) == len(graph.manifest["tables"])


def test_manifest_replaces_rows_with_digested_jsonl_and_never_leaks_locators(tmp_path):
    body = b"private bytes"
    storage_ref = "labs/bb/private-secret.pdf"
    locations = _locations(tmp_path)
    write_private_file(locations.private_root, storage_ref, body)
    graph = _graph(
        resources=[_resource("f00000001", body)],
        prepared=[_prepared("f00000001", storage_ref, body)],
    )
    output = io.BytesIO()

    _write_archive(graph, output, locations=locations)

    assert storage_ref.encode() not in output.getvalue()
    assert b"private-database-identity" not in output.getvalue()
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["archive_id"] == str(_ARCHIVE_ID)
        assert manifest["records"][0]["ref"] == _RECORD_REF
        assert len(manifest["records"][0]["record_digest"]) == 64
        record = manifest["records"][0]
        record_body = {
            key: record[key]
            for key in ("connections", "resources", "tables", "totals")
        }
        encoded_record = json.dumps(
            record_body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        assert record["record_digest"] == hashlib.sha256(encoded_record).hexdigest()
        table = manifest["records"][0]["tables"][0]
        table_body = archive.read(table["path"])
        assert "rows" in table and isinstance(table["rows"], int)
        assert table["sha256_hex"] == hashlib.sha256(table_body).hexdigest()
        resource = manifest["records"][0]["resources"][0]
        assert resource["object_path"].startswith("objects/sha256/")
        assert "storage_ref" not in resource


def test_archive_streams_directly_into_authenticated_encryption(tmp_path):
    encrypted = io.BytesIO()
    with EncryptingWriter(encrypted, passphrase="correct horse battery staple") as writer:
        _write_archive(_graph(), writer, locations=_locations(tmp_path))

    plaintext = io.BytesIO()
    decrypt_stream(
        io.BytesIO(encrypted.getvalue()),
        plaintext,
        passphrase="correct horse battery staple",
    )
    with zipfile.ZipFile(io.BytesIO(plaintext.getvalue())) as archive:
        assert archive.namelist() == [
            "manifest.json",
            f"records/{_RECORD_REF}/tables/weight_logs.jsonl",
        ]


def test_equal_resources_are_verified_but_stored_once(tmp_path):
    body = b"same synthetic bytes"
    locations = _locations(tmp_path)
    refs = ("labs/aa/first.pdf", "labs/bb/second.pdf")
    for storage_ref in refs:
        write_private_file(locations.private_root, storage_ref, body)
    resources = [
        _resource("f00000001", body),
        _resource("f00000002", body),
    ]
    prepared = [
        _prepared("f00000001", refs[0], body),
        _prepared("f00000002", refs[1], body),
    ]
    graph = _graph(resources=resources, prepared=prepared)
    output = io.BytesIO()

    _write_archive(graph, output, locations=locations)

    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
        objects = [name for name in archive.namelist() if name.startswith("objects/")]
        manifest = json.loads(archive.read("manifest.json"))
        assert len(objects) == 1
        assert {
            resource["object_path"]
            for resource in manifest["records"][0]["resources"]
        } == set(objects)


def test_corrupt_duplicate_prevents_any_archive_result(tmp_path):
    body = b"same expected bytes"
    locations = _locations(tmp_path)
    write_private_file(locations.private_root, "labs/aa/first.pdf", body)
    write_private_file(locations.private_root, "labs/bb/second.pdf", b"corrupt duplicate")
    graph = _graph(
        resources=[_resource("f00000001", body), _resource("f00000002", body)],
        prepared=[
            _prepared("f00000001", "labs/aa/first.pdf", body),
            _prepared("f00000002", "labs/bb/second.pdf", body),
        ],
    )
    output = io.BytesIO()

    with pytest.raises(ArchiveBuildError) as raised:
        _write_archive(graph, output, locations=locations)
    assert raised.value.code == "resource_integrity_failed"
    assert output.getvalue() == b""


def test_table_name_cannot_create_a_traversal_entry(tmp_path):
    graph = _graph(table_name="../escape")
    with pytest.raises(ArchiveBuildError) as raised:
        _write_archive(graph, io.BytesIO(), locations=_locations(tmp_path))
    assert raised.value.code == "archive_table_name_invalid"


@pytest.mark.parametrize("record_ref", ["", "records/subject", "record ref", "r" * 129])
def test_record_ref_is_an_opaque_path_safe_receipt_key(tmp_path, record_ref):
    with pytest.raises(ArchiveBuildError) as raised:
        write_inner_archive(
            _graph(),
            io.BytesIO(),
            archive_id=_ARCHIVE_ID,
            record_ref=record_ref,
            locations=_locations(tmp_path),
        )
    assert raised.value.code == "archive_record_ref_invalid"


def test_graph_top_level_and_connection_descriptors_are_exact(tmp_path):
    graph = _graph()
    graph.manifest["private_locator"] = "must-not-pass"
    with pytest.raises(ArchiveBuildError) as raised:
        _write_archive(graph, io.BytesIO(), locations=_locations(tmp_path))
    assert raised.value.code == "archive_graph_invalid"

    graph = _graph()
    graph.manifest["connections"] = [
        {
            "ref": "c00000001",
            "provider": "garmin",
            "connection_type": "oauth",
            "credential_ref": "private",
        }
    ]
    graph.manifest["totals"]["connections"] = 1
    with pytest.raises(ArchiveBuildError) as raised:
        _write_archive(graph, io.BytesIO(), locations=_locations(tmp_path))
    assert raised.value.code == "archive_connection_invalid"


def test_graph_format_version_and_connection_count_are_capped(tmp_path):
    graph = _graph()
    graph.manifest["version"] = 3
    with pytest.raises(ArchiveBuildError) as raised:
        _write_archive(graph, io.BytesIO(), locations=_locations(tmp_path))
    assert raised.value.code == "archive_graph_version_invalid"

    graph = _graph()
    graph.manifest["connections"] = [
        {
            "ref": "c00000001",
            "provider": "garmin",
            "connection_type": "oauth",
        }
    ]
    graph.manifest["totals"]["connections"] = 1
    with pytest.raises(ArchiveBuildError) as raised:
        _write_archive(
            graph,
            io.BytesIO(),
            locations=_locations(tmp_path),
            limits=ArchiveLimits(max_connections=0),
        )
    assert raised.value.code == "archive_connection_count_exceeded"


def test_jsonl_bytes_are_frozen_before_output_can_mutate_graph(tmp_path):
    graph = _graph()
    original_value = graph.manifest["tables"][0]["rows"][0]["values"]["weight_kg"]

    def mutate_graph():
        graph.manifest["tables"][0]["rows"][0]["values"]["weight_kg"] = "999"

    output = _MutatingNonSeekable(mutate_graph)
    _write_archive(graph, output, locations=_locations(tmp_path))

    with zipfile.ZipFile(io.BytesIO(output.body)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        table = manifest["records"][0]["tables"][0]
        table_body = archive.read(table["path"])
        assert json.loads(table_body)["values"]["weight_kg"] == original_value
        assert table["sha256_hex"] == hashlib.sha256(table_body).hexdigest()


def test_duplicate_row_ref_is_rejected(tmp_path):
    graph = _graph()
    graph.manifest["tables"].append(
        {
            "name": "sleep_logs",
            "rows": [
                {
                    "ref": "r000000000001",
                    "values": {"date": "2026-08-25"},
                }
            ],
        }
    )
    graph.manifest["totals"]["tables"] = 2
    graph.manifest["totals"]["rows"] = 2
    with pytest.raises(ArchiveBuildError) as raised:
        _write_archive(graph, io.BytesIO(), locations=_locations(tmp_path))
    assert raised.value.code == "archive_row_ref_duplicate"


def test_archive_output_cap_aborts_without_a_central_directory(tmp_path):
    graph = _graph()
    output = io.BytesIO()
    with pytest.raises(ArchiveBuildError) as raised:
        _write_archive(
            graph,
            output,
            locations=_locations(tmp_path),
            limits=ArchiveLimits(max_archive_bytes=100),
        )
    assert raised.value.code == "archive_bytes_exceeded"
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(output.getvalue()))


def test_manifest_cap_is_checked_before_destination_is_touched(tmp_path):
    graph = _graph()
    output = io.BytesIO()
    with pytest.raises(ArchiveBuildError) as raised:
        _write_archive(
            graph,
            output,
            locations=_locations(tmp_path),
            limits=ArchiveLimits(max_manifest_bytes=10),
        )
    assert raised.value.code == "archive_manifest_bytes_exceeded"
    assert output.getvalue() == b""
