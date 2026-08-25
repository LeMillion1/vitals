from __future__ import annotations

import hashlib
from io import BytesIO
from types import SimpleNamespace

import pytest

from vitals.enums import FileStorageBackend
from vitals.persistence.file_storage import write_private_file
from vitals.services.portability.resources import (
    ResourceArchiveError,
    ResourceLimits,
    ResourceLocations,
    build_resource_plan,
    copy_verified_resource,
)


def _public(ref: str, body: bytes) -> dict[str, object]:
    return {
        "ref": ref,
        "purpose": "lab_document",
        "media_type": "application/pdf",
        "byte_size": len(body),
        "sha256_hex": hashlib.sha256(body).hexdigest(),
    }


def _prepared(ref: str, storage_ref: str, body: bytes, **overrides):
    values = {
        "resource_ref": ref,
        "storage_backend": FileStorageBackend.PRIVATE_LOCAL.value,
        "storage_ref": storage_ref,
        "expected_byte_size": len(body),
        "expected_sha256_hex": hashlib.sha256(body).hexdigest(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _locations(tmp_path) -> ResourceLocations:
    return ResourceLocations(
        static_dir=str(tmp_path / "static"),
        private_root=str(tmp_path / "private"),
    )


def test_plan_joins_public_metadata_without_serializing_private_locator(tmp_path):
    body = b"synthetic medical document"
    prepared = _prepared("f00000001", "labs/aa/document.pdf", body)

    plan = build_resource_plan([_public("f00000001", body)], [prepared])

    assert len(plan) == 1
    assert plan[0].resource_ref == "f00000001"
    assert plan[0].object_path == f"objects/sha256/{hashlib.sha256(body).hexdigest()}"
    assert "labs/aa/document.pdf" not in repr(_public("f00000001", body))


@pytest.mark.parametrize(
    ("public", "prepared", "code"),
    [
        ([], [_prepared("f00000001", "labs/aa/a.pdf", b"a")], "resource_plan_incomplete"),
        (
            [_public("f00000001", b"a")],
            [_prepared("f00000001", "labs/aa/a.pdf", b"b")],
            "resource_metadata_mismatch",
        ),
        (
            [dict(_public("f00000001", b"a"), storage_ref="secret")],
            [_prepared("f00000001", "labs/aa/a.pdf", b"a")],
            "resource_manifest_invalid",
        ),
    ],
)
def test_plan_fails_closed_on_incomplete_or_private_public_metadata(
    public, prepared, code
):
    with pytest.raises(ResourceArchiveError) as raised:
        build_resource_plan(public, prepared)
    assert raised.value.code == code


def test_plan_applies_individual_and_total_byte_caps():
    first = _public("f00000001", b"12")
    second = _public("f00000002", b"34")
    prepared = [
        _prepared("f00000001", "labs/aa/a.pdf", b"12"),
        _prepared("f00000002", "labs/bb/b.pdf", b"34"),
    ]
    with pytest.raises(ResourceArchiveError) as raised:
        build_resource_plan(
            [first, second],
            prepared,
            limits=ResourceLimits(
                max_resources=2,
                max_resource_bytes=2,
                max_total_resource_bytes=3,
            ),
        )
    assert raised.value.code == "resource_total_exceeded"


def test_deduplicated_digest_cannot_claim_conflicting_sizes():
    digest = hashlib.sha256(b"same digest token").hexdigest()
    public = [
        {**_public("f00000001", b"a"), "sha256_hex": digest},
        {**_public("f00000002", b"bb"), "sha256_hex": digest},
    ]
    prepared = [
        _prepared(
            "f00000001",
            "labs/aa/a.pdf",
            b"a",
            expected_sha256_hex=digest,
        ),
        _prepared(
            "f00000002",
            "labs/bb/b.pdf",
            b"bb",
            expected_sha256_hex=digest,
        ),
    ]

    with pytest.raises(ResourceArchiveError) as raised:
        build_resource_plan(public, prepared)
    assert raised.value.code == "resource_digest_metadata_conflict"


def test_prepared_iterable_is_stopped_at_its_own_count_cap():
    prepared = _prepared("f00000001", "labs/aa/a.pdf", b"a")
    with pytest.raises(ResourceArchiveError) as raised:
        build_resource_plan(
            [],
            iter([prepared]),
            limits=ResourceLimits(
                max_resources=0,
                max_resource_bytes=0,
                max_total_resource_bytes=0,
            ),
        )
    assert raised.value.code == "resource_count_exceeded"


def test_copy_recomputes_integrity_from_verified_private_descriptor(tmp_path):
    body = b"synthetic verified bytes"
    locations = _locations(tmp_path)
    storage_ref = "labs/aa/verified.pdf"
    write_private_file(locations.private_root, storage_ref, body)
    item = build_resource_plan(
        [_public("f00000001", body)],
        [_prepared("f00000001", storage_ref, body)],
    )[0]
    output = BytesIO()

    assert copy_verified_resource(item, output, locations=locations, chunk_size=3) == len(body)
    assert output.getvalue() == body


def test_copy_refuses_bytes_changed_after_graph_preparation(tmp_path):
    expected = b"expected"
    locations = _locations(tmp_path)
    storage_ref = "labs/aa/changed.pdf"
    write_private_file(locations.private_root, storage_ref, b"modified")
    item = build_resource_plan(
        [_public("f00000001", expected)],
        [_prepared("f00000001", storage_ref, expected)],
    )[0]

    with pytest.raises(ResourceArchiveError) as raised:
        copy_verified_resource(item, BytesIO(), locations=locations)
    assert raised.value.code == "resource_integrity_failed"


def test_copy_refuses_symlinked_private_path(tmp_path):
    locations = _locations(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pdf").write_bytes(b"secret")
    private_root = tmp_path / "private"
    private_root.mkdir()
    (private_root / "labs").symlink_to(outside, target_is_directory=True)
    body = b"secret"
    item = build_resource_plan(
        [_public("f00000001", body)],
        [_prepared("f00000001", "labs/secret.pdf", body)],
    )[0]

    with pytest.raises(ResourceArchiveError) as raised:
        copy_verified_resource(item, BytesIO(), locations=locations)
    assert raised.value.code == "resource_integrity_failed"


def test_object_store_key_is_never_reinterpreted_as_a_local_path():
    body = b"object"
    prepared = _prepared(
        "f00000001",
        "bucket/private-object",
        body,
        storage_backend=FileStorageBackend.OBJECT_STORE.value,
    )
    with pytest.raises(ResourceArchiveError) as raised:
        build_resource_plan([_public("f00000001", body)], [prepared])
    assert raised.value.code == "resource_backend_unsupported"
