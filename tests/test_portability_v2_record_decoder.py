"""Validated v2 rows are decoded only through the pinned schema contract."""

from __future__ import annotations

import hashlib
import io
import uuid
from types import SimpleNamespace

import pytest

from vitals.enums import FileStorageBackend
from vitals.persistence.file_storage import write_private_file
from vitals.services.portability.archive import write_inner_archive
from vitals.services.portability.archive_reader import open_validated_encrypted_archive
from vitals.services.portability.crypto import EncryptingWriter
from vitals.services.portability.record_decoder import (
    RecordDecodeError,
    decode_validated_record,
)
from vitals.services.portability.resources import ResourceLocations
from vitals.services.portability.schema import (
    PORTABILITY_SCHEMA_DESCRIPTOR,
    PORTABILITY_SCHEMA_DIGEST,
)


_PASSPHRASE = "correct horse battery staple"
_ARCHIVE_ID = uuid.UUID("12345678-1234-5678-9234-567812345678")
_RECORD_REF = "record_decoder"
_SCHEMA_TABLES = {table["name"]: table for table in PORTABILITY_SCHEMA_DESCRIPTOR["tables"]}


def _codec_value(column: dict) -> object:
    codec = column["type"]["codec"]
    if codec == "boolean":
        return False
    if codec == "integer":
        return 7
    if codec == "float":
        return 1.5
    if codec == "decimal_string":
        return "1.5"
    if codec == "string":
        return "x"
    if codec == "date_iso8601":
        return "2026-08-25"
    if codec == "datetime_iso8601":
        return "2026-08-25T12:34:56"
    if codec == "time_iso8601":
        return "12:34:56"
    if codec == "json":
        return {"nested": [1, True, None, {"key": "value"}]}
    raise AssertionError(f"unhandled test codec {codec}")


def _row(
    table_name: str,
    ref: str,
    *,
    links: dict[str, str] | None = None,
) -> dict[str, object]:
    schema = _SCHEMA_TABLES[table_name]
    values = {column["name"]: _codec_value(column) for column in schema["value_columns"]}
    if table_name == "raw_payloads":
        values["domain"] = "labs"
        values["source"] = "manual"
        if links and "file_asset_id" in links:
            values.pop("external_id")
    row: dict[str, object] = {"ref": ref, "values": values}
    if links:
        row["links"] = links
    return row


def _resource(ref: str, body: bytes, *, purpose: str = "progress_photo") -> dict[str, object]:
    return {
        "ref": ref,
        "purpose": purpose,
        "media_type": "image/jpeg",
        "byte_size": len(body),
        "sha256_hex": hashlib.sha256(body).hexdigest(),
    }


def _prepared(ref: str, storage_ref: str, body: bytes) -> SimpleNamespace:
    return SimpleNamespace(
        resource_ref=ref,
        file_asset_id=uuid.uuid4(),
        storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
        storage_ref=storage_ref,
        expected_byte_size=len(body),
        expected_sha256_hex=hashlib.sha256(body).hexdigest(),
    )


def _encrypted_archive(
    tmp_path,
    rows_by_table: dict[str, list[dict[str, object]]],
    *,
    connections: list[dict[str, str]] | None = None,
    resources: list[dict[str, object]] | None = None,
    prepared_resources: tuple[SimpleNamespace, ...] = (),
    omit_table: str | None = None,
) -> bytes:
    tables = [
        {"name": name, "rows": rows_by_table.get(name, [])}
        for name in sorted(_SCHEMA_TABLES)
        if name != omit_table
    ]
    connections = connections or []
    resources = resources or []
    graph = SimpleNamespace(
        manifest={
            "format": "vitals-portability-graph",
            "version": 2,
            "schema_digest": PORTABILITY_SCHEMA_DIGEST,
            "tables": tables,
            "connections": connections,
            "resources": resources,
            "totals": {
                "tables": len(tables),
                "rows": sum(len(table["rows"]) for table in tables),
                "connections": len(connections),
                "resources": len(resources),
            },
        },
        prepared_resources=prepared_resources,
    )
    locations = ResourceLocations(
        static_dir=str(tmp_path / "static"),
        private_root=str(tmp_path / "private"),
    )
    plaintext = io.BytesIO()
    write_inner_archive(
        graph,
        plaintext,
        archive_id=_ARCHIVE_ID,
        record_ref=_RECORD_REF,
        locations=locations,
    )
    encrypted = io.BytesIO()
    with EncryptingWriter(encrypted, passphrase=_PASSPHRASE) as writer:
        writer.write(plaintext.getvalue())
    return encrypted.getvalue()


def _decode(tmp_path, rows_by_table, **kwargs):
    encrypted = _encrypted_archive(tmp_path, rows_by_table, **kwargs)
    with open_validated_encrypted_archive(io.BytesIO(encrypted), passphrase=_PASSPHRASE) as archive:
        return decode_validated_record(archive)


def _decoded_row(record, table_name: str):
    table = next(table for table in record.tables if table.name == table_name)
    assert len(table.rows) == 1
    return table.rows[0]


def test_decodes_types_links_and_dependency_order_to_immutable_rows(tmp_path):
    raw = _row("raw_payloads", "r000000000001")
    weight = _row(
        "weight_logs",
        "r000000000002",
        links={"raw_payload_id": "r000000000001"},
    )

    record = _decode(
        tmp_path,
        {"raw_payloads": [raw], "weight_logs": [weight]},
    )

    assert record.record_ref == _RECORD_REF
    assert record.schema_digest == PORTABILITY_SCHEMA_DIGEST
    assert record.row_count == 2
    table_names = [table.name for table in record.tables]
    assert table_names == PORTABILITY_SCHEMA_DESCRIPTOR["insert_order"]
    assert table_names.index("raw_payloads") < table_names.index("weight_logs")
    raw_decoded = _decoded_row(record, "raw_payloads")
    weight_decoded = _decoded_row(record, "weight_logs")
    assert raw_decoded.values["fetched_at"].isoformat() == "2026-08-25T12:34:56"
    assert weight_decoded.values["date"].isoformat() == "2026-08-25"
    assert weight_decoded.links["raw_payload_id"].target_table == "raw_payloads"
    assert weight_decoded.links["raw_payload_id"].target_ref == raw_decoded.ref
    with pytest.raises(TypeError):
        raw_decoded.values["payload"]["new"] = "not mutable"
    with pytest.raises(TypeError):
        weight_decoded.values["weight_kg"] = 5.0


def test_output_contains_complete_immutable_connection_and_resource_descriptors(tmp_path):
    body = b"synthetic progress photo"
    storage_ref = "progress/aa/photo.jpg"
    locations = ResourceLocations(
        static_dir=str(tmp_path / "static"),
        private_root=str(tmp_path / "private"),
    )
    write_private_file(locations.private_root, storage_ref, body)
    weight = _row(
        "weight_logs",
        "r000000000001",
        links={"integration_connection_id": "c00000001"},
    )
    photo = _row(
        "progress_photos",
        "r000000000002",
        links={"file_asset_id": "f00000001"},
    )
    encrypted = _encrypted_archive(
        tmp_path,
        {"weight_logs": [weight], "progress_photos": [photo]},
        connections=[
            {
                "ref": "c00000001",
                "provider": "garmin",
                "connection_type": "oauth",
            }
        ],
        resources=[_resource("f00000001", body)],
        prepared_resources=(_prepared("f00000001", storage_ref, body),),
    )

    with open_validated_encrypted_archive(io.BytesIO(encrypted), passphrase=_PASSPHRASE) as archive:
        record = decode_validated_record(archive)

    assert record.connections[0].ref == "c00000001"
    assert record.connections[0].provider == "garmin"
    resource = record.resources[0]
    digest = hashlib.sha256(body).hexdigest()
    assert (
        resource.ref,
        resource.purpose,
        resource.media_type,
        resource.byte_size,
        resource.sha256_hex,
        resource.object_path,
    ) == (
        "f00000001",
        "progress_photo",
        "image/jpeg",
        len(body),
        digest,
        f"objects/sha256/{digest}",
    )
    assert _decoded_row(record, "progress_photos").links["file_asset_id"].target_ref == resource.ref


def test_file_backed_raw_locator_is_absent_and_resource_purpose_is_schema_checked(
    tmp_path,
):
    body = b"synthetic lab document"
    storage_ref = "labs/aa/result.pdf"
    locations = ResourceLocations(
        static_dir=str(tmp_path / "static"),
        private_root=str(tmp_path / "private"),
    )
    write_private_file(locations.private_root, storage_ref, body)
    raw = _row(
        "raw_payloads",
        "r000000000001",
        links={"file_asset_id": "f00000001"},
    )
    assert "external_id" not in raw["values"]
    common = {
        "resources": [_resource("f00000001", body, purpose="lab_document")],
        "prepared_resources": (_prepared("f00000001", storage_ref, body),),
    }

    record = _decode(tmp_path, {"raw_payloads": [raw]}, **common)
    assert "external_id" not in _decoded_row(record, "raw_payloads").values

    wrong = {
        **common,
        "resources": [_resource("f00000001", body, purpose="progress_photo")],
    }
    with pytest.raises(RecordDecodeError) as raised:
        _decode(tmp_path, {"raw_payloads": [raw]}, **wrong)
    assert raised.value.code == "resource_purpose_invalid"

    no_file = _row("raw_payloads", "r000000000002")
    no_file["values"].pop("external_id")
    with pytest.raises(RecordDecodeError) as raised:
        _decode(tmp_path, {"raw_payloads": [no_file]})
    assert raised.value.code == "value_fields_invalid"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda values: values.__setitem__("future", "x"), "value_fields_invalid"),
        (lambda values: values.pop("weight_kg"), "value_fields_invalid"),
        (lambda values: values.__setitem__("weight_kg", "72.5"), "value_type_invalid"),
        (lambda values: values.__setitem__("superseded", 0), "value_type_invalid"),
        (lambda values: values.__setitem__("date", "20260825"), "value_type_invalid"),
        (lambda values: values.__setitem__("domain", "x" * 33), "value_range_invalid"),
        (lambda values: values.__setitem__("created_at", None), "value_null_invalid"),
        (lambda values: values.__setitem__("subject_id", str(uuid.uuid4())), "field_forbidden"),
        (lambda values: values.__setitem__("id", 99), "field_forbidden"),
    ],
)
def test_unknown_missing_invalid_or_private_values_fail_closed(tmp_path, mutation, code):
    row = _row("weight_logs", "r000000000001")
    mutation(row["values"])

    with pytest.raises(RecordDecodeError) as raised:
        _decode(tmp_path, {"weight_logs": [row]})
    assert raised.value.code == code


def test_unknown_and_required_links_fail_closed(tmp_path):
    unknown = _row("weight_logs", "r000000000001")
    unknown["links"] = {"future_id": "r000000000001"}
    with pytest.raises(RecordDecodeError) as raised:
        _decode(tmp_path, {"weight_logs": [unknown]})
    assert raised.value.code == "link_fields_invalid"

    metric = _row("body_scan_metrics", "r000000000001")
    with pytest.raises(RecordDecodeError) as raised:
        _decode(tmp_path, {"body_scan_metrics": [metric]})
    assert raised.value.code == "required_link_missing"


def test_row_links_must_target_the_schema_declared_table(tmp_path):
    raw = _row("raw_payloads", "r000000000001")
    weight = _row("weight_logs", "r000000000002")
    scan = _row(
        "body_scans",
        "r000000000003",
        links={"raw_payload_id": "r000000000002"},
    )

    with pytest.raises(RecordDecodeError) as raised:
        _decode(
            tmp_path,
            {"raw_payloads": [raw], "weight_logs": [weight], "body_scans": [scan]},
        )
    assert raised.value.code == "row_link_target_invalid"


def test_partial_table_snapshot_is_not_import_decodable(tmp_path):
    encrypted = _encrypted_archive(
        tmp_path,
        {"weight_logs": [_row("weight_logs", "r000000000001")]},
        omit_table="annotations",
    )
    with open_validated_encrypted_archive(io.BytesIO(encrypted), passphrase=_PASSPHRASE) as archive:
        with pytest.raises(RecordDecodeError) as raised:
            decode_validated_record(archive)
    assert raised.value.code == "table_set_invalid"


def test_validated_archive_must_still_be_inside_its_reader_context(tmp_path):
    encrypted = _encrypted_archive(tmp_path, {})
    with open_validated_encrypted_archive(io.BytesIO(encrypted), passphrase=_PASSPHRASE) as archive:
        pass

    with pytest.raises(RecordDecodeError) as raised:
        decode_validated_record(archive)
    assert raised.value.code == "archive_context_closed"


def test_decoder_rejects_non_validated_objects():
    with pytest.raises(TypeError, match="ValidatedArchive"):
        decode_validated_record(SimpleNamespace())
