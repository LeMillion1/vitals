"""Validated v2 medical objects stage atomically outside the DB transaction."""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from vitals.enums import FileAssetPurpose, FileAssetStatus, FileStorageBackend, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import FileAsset
from vitals.persistence import file_storage
from vitals.services.portability.archive import write_inner_archive
from vitals.services.portability.archive_reader import open_validated_encrypted_archive
from vitals.services.portability.crypto import EncryptingWriter
from vitals.services.portability.record_decoder import DecodedRecord, DecodedResource
from vitals.services.portability import resource_staging
from vitals.services.portability.resource_staging import ResourceStagingError
from vitals.services.portability.resources import ResourceLocations
from vitals.services.portability.schema import PORTABILITY_SCHEMA_DIGEST


_PASSPHRASE = "correct horse battery staple"
_ARCHIVE_ID = uuid.UUID("12345678-1234-5678-9234-567812345678")
_RECORD_REF = "resource_staging"


async def _subject(session, slug: str) -> tuple[User, HealthSubject]:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=f"Synthetic {slug}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return user, subject


def _resource(
    ref: str,
    body: bytes,
    *,
    purpose: str,
    media_type: str,
) -> dict[str, object]:
    return {
        "ref": ref,
        "purpose": purpose,
        "media_type": media_type,
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
    resources: list[tuple[str, bytes, str, str]],
) -> bytes:
    source_root = str(tmp_path / f"source-{uuid.uuid4().hex}")
    public = []
    prepared = []
    rows = []
    stored_by_digest: dict[str, str] = {}
    for index, (ref, body, purpose, media_type) in enumerate(resources, start=1):
        digest = hashlib.sha256(body).hexdigest()
        storage_ref = stored_by_digest.get(digest)
        if storage_ref is None:
            storage_ref = f"labs/{index:02x}/source-{index}.pdf"
            file_storage.write_private_file(source_root, storage_ref, body)
            stored_by_digest[digest] = storage_ref
        public.append(
            _resource(
                ref,
                body,
                purpose=purpose,
                media_type=media_type,
            )
        )
        prepared.append(_prepared(ref, storage_ref, body))
        rows.append(
            {
                "ref": f"r{index:012d}",
                "values": {},
                "links": {"file_asset_id": ref},
            }
        )
    graph = SimpleNamespace(
        manifest={
            "format": "vitals-portability-graph",
            "version": 2,
            "schema_digest": PORTABILITY_SCHEMA_DIGEST,
            "tables": [{"name": "raw_payloads", "rows": rows}],
            "connections": [],
            "resources": public,
            "totals": {
                "tables": 1,
                "rows": len(rows),
                "connections": 0,
                "resources": len(public),
            },
        },
        prepared_resources=tuple(prepared),
    )
    plaintext = io.BytesIO()
    write_inner_archive(
        graph,
        plaintext,
        archive_id=_ARCHIVE_ID,
        record_ref=_RECORD_REF,
        locations=ResourceLocations(
            static_dir=str(tmp_path / "static"),
            private_root=source_root,
        ),
    )
    encrypted = io.BytesIO()
    with EncryptingWriter(encrypted, passphrase=_PASSPHRASE) as writer:
        writer.write(plaintext.getvalue())
    return encrypted.getvalue()


def _decoded(resources: list[tuple[str, bytes, str, str]]) -> DecodedRecord:
    descriptors = tuple(
        DecodedResource(
            ref=ref,
            purpose=purpose,
            media_type=media_type,
            byte_size=len(body),
            sha256_hex=hashlib.sha256(body).hexdigest(),
            object_path=f"objects/sha256/{hashlib.sha256(body).hexdigest()}",
        )
        for ref, body, purpose, media_type in resources
    )
    return DecodedRecord(
        record_ref=_RECORD_REF,
        schema_digest=PORTABILITY_SCHEMA_DIGEST,
        connections=(),
        resources=descriptors,
        tables=(),
        row_count=len(resources),
    )


def _medical_bodies() -> list[tuple[str, bytes, str, str]]:
    return [
        ("f00000001", b"%PDF-1.7\nmedical", "lab_document", "application/pdf"),
        ("f00000002", b"\x89PNG\r\n\x1a\nmedical", "lab_document", "image/png"),
        ("f00000003", b"\xff\xd8\xff\xe0medical", "lab_document", "image/jpeg"),
        ("f00000004", b"RIFF\x08\x00\x00\x00WEBPmedical", "lab_document", "image/webp"),
        (
            "f00000005",
            b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00medical",
            "lab_document",
            "image/heic",
        ),
        (
            "f00000006",
            b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00medical",
            "lab_document",
            "image/heif",
        ),
    ]


@pytest.mark.asyncio
async def test_all_medical_formats_stream_to_canonical_private_assets(db_session, tmp_path):
    actor, subject = await _subject(db_session, "stage-medical-formats")
    resources = _medical_bodies()
    encrypted = _encrypted_archive(tmp_path, resources)
    private_root = str(tmp_path / "target")

    with open_validated_encrypted_archive(io.BytesIO(encrypted), passphrase=_PASSPHRASE) as archive:
        staged = await resource_staging.stage_record_resources(
            db_session,
            archive=archive,
            record=_decoded(resources),
            target_subject_id=subject.id,
            actor_user_id=actor.id,
            private_root=private_root,
        )

    assert tuple(staged) == tuple(resource[0] for resource in resources)
    assert len(staged.newly_written_objects) == len(resources)
    expected_extensions = (".pdf", ".png", ".jpg", ".webp", ".heic", ".heif")
    assets = list((await db_session.scalars(select(FileAsset))).all())
    assert len(assets) == len(resources)
    for (ref, body, purpose, media_type), extension in zip(
        resources, expected_extensions, strict=True
    ):
        binding = staged[ref]
        asset = await db_session.get(FileAsset, binding.file_asset_id)
        assert asset is not None
        assert asset.subject_id == subject.id
        assert asset.uploaded_by_user_id == actor.id
        assert asset.purpose == purpose
        assert asset.media_type == media_type
        assert binding.storage_ref.endswith(extension)
        path = file_storage.private_file_disk_path(private_root, binding.storage_ref)
        assert Path(path).read_bytes() == body
        assert os.stat(path).st_mode & 0o777 == 0o600
    assert db_session.in_transaction()
    assert tuple(item.storage_ref for item in staged.newly_written_objects) == tuple(
        binding.storage_ref for binding in staged.bindings
    )
    assert tuple(item.resource_ref for item in staged.newly_written_objects) == tuple(staged)
    with pytest.raises(FrozenInstanceError):
        staged.bindings = ()


@pytest.mark.asyncio
async def test_deduplicated_archive_object_creates_distinct_assets_and_files(db_session, tmp_path):
    actor, subject = await _subject(db_session, "stage-deduplicated")
    body = b"%PDF-1.7\nsame medical object"
    resources = [
        ("f00000001", body, "lab_document", "application/pdf"),
        ("f00000002", body, "lab_document", "application/pdf"),
    ]
    encrypted = _encrypted_archive(tmp_path, resources)
    private_root = str(tmp_path / "target")

    with open_validated_encrypted_archive(io.BytesIO(encrypted), passphrase=_PASSPHRASE) as archive:
        staged = await resource_staging.stage_record_resources(
            db_session,
            archive=archive,
            record=_decoded(resources),
            target_subject_id=subject.id,
            actor_user_id=actor.id,
            private_root=private_root,
        )

    assert staged["f00000001"].file_asset_id != staged["f00000002"].file_asset_id
    assert staged["f00000001"].storage_ref != staged["f00000002"].storage_ref
    assert len(staged.newly_written_objects) == 2
    assert {item.sha256_hex for item in staged.newly_written_objects} == {
        hashlib.sha256(body).hexdigest()
    }


@pytest.mark.asyncio
async def test_each_resource_purpose_mints_its_own_canonical_prefix(db_session, tmp_path):
    actor, subject = await _subject(db_session, "stage-purpose-prefixes")
    resources = [
        ("f00000001", b"%PDF-1.7\nprogress", "progress_photo", "application/pdf"),
        ("f00000002", b"%PDF-1.7\nlab", "lab_document", "application/pdf"),
        ("f00000003", b"%PDF-1.7\nbody", "body_scan_document", "application/pdf"),
        (
            "f00000004",
            b"%PDF-1.7\ncare",
            "care_message_attachment",
            "application/pdf",
        ),
    ]
    encrypted = _encrypted_archive(tmp_path, resources)
    private_root = str(tmp_path / "target")

    with open_validated_encrypted_archive(io.BytesIO(encrypted), passphrase=_PASSPHRASE) as archive:
        staged = await resource_staging.stage_record_resources(
            db_session,
            archive=archive,
            record=_decoded(resources),
            target_subject_id=subject.id,
            actor_user_id=actor.id,
            private_root=private_root,
        )

    assert tuple(binding.storage_ref.split("/", 1)[0] for binding in staged.bindings) == (
        "uploads",
        "labs",
        "body",
        "care",
    )


@pytest.mark.asyncio
async def test_magic_failure_removes_every_object_written_by_this_call(db_session, tmp_path):
    actor, subject = await _subject(db_session, "stage-magic-cleanup")
    resources = [
        ("f00000001", b"%PDF-1.7\nvalid", "lab_document", "application/pdf"),
        ("f00000002", b"not a png", "lab_document", "image/png"),
    ]
    encrypted = _encrypted_archive(tmp_path, resources)
    private_root = str(tmp_path / "target")

    with open_validated_encrypted_archive(io.BytesIO(encrypted), passphrase=_PASSPHRASE) as archive:
        with pytest.raises(ResourceStagingError) as raised:
            await resource_staging.stage_record_resources(
                db_session,
                archive=archive,
                record=_decoded(resources),
                target_subject_id=subject.id,
                actor_user_id=actor.id,
                private_root=private_root,
            )
    assert raised.value.code == "resource_magic_invalid"
    assert not list((tmp_path / "target").rglob("*.pdf"))
    # The service deliberately does not roll back flushed metadata.
    assert len((await db_session.scalars(select(FileAsset))).all()) == 1
    assert db_session.in_transaction()


@pytest.mark.asyncio
async def test_registration_failure_cleans_bytes_without_deleting_old_assets(
    db_session, tmp_path, monkeypatch
):
    actor, subject = await _subject(db_session, "stage-registration-cleanup")
    old_ref = "labs/aa/old.pdf"
    old_body = b"%PDF-1.7\nold"
    private_root = str(tmp_path / "target")
    file_storage.write_private_file(private_root, old_ref, old_body)
    old_asset = FileAsset(
        subject_id=subject.id,
        uploaded_by_user_id=actor.id,
        opaque_key=uuid.uuid4(),
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
        storage_ref=old_ref,
        media_type="application/pdf",
        byte_size=len(old_body),
        sha256_hex=hashlib.sha256(old_body).hexdigest(),
        status=FileAssetStatus.ACTIVE.value,
    )
    db_session.add(old_asset)
    await db_session.flush()
    resources = [("f00000001", b"%PDF-1.7\nnew", "lab_document", "application/pdf")]
    encrypted = _encrypted_archive(tmp_path, resources)

    async def fail_registration(*_args, **_kwargs):
        raise RuntimeError("synthetic registration failure")

    monkeypatch.setattr(
        resource_staging.file_asset_service,
        "register_private_local",
        fail_registration,
    )

    with open_validated_encrypted_archive(io.BytesIO(encrypted), passphrase=_PASSPHRASE) as archive:
        with pytest.raises(ResourceStagingError) as raised:
            await resource_staging.stage_record_resources(
                db_session,
                archive=archive,
                record=_decoded(resources),
                target_subject_id=subject.id,
                actor_user_id=actor.id,
                private_root=private_root,
            )
    assert raised.value.code == "resource_staging_failed"
    assert Path(file_storage.private_file_disk_path(private_root, old_ref)).read_bytes() == old_body
    assert (await db_session.get(FileAsset, old_asset.id)) is old_asset
    written_files = [path for path in (tmp_path / "target").rglob("*") if path.is_file()]
    assert written_files == [tmp_path / "target" / old_ref]


@pytest.mark.asyncio
async def test_write_list_supports_cleanup_after_caller_rolls_back(db_session, tmp_path):
    actor, subject = await _subject(db_session, "stage-caller-rollback")
    resources = [("f00000001", b"%PDF-1.7\nrollback", "lab_document", "application/pdf")]
    encrypted = _encrypted_archive(tmp_path, resources)
    private_root = str(tmp_path / "target")

    with open_validated_encrypted_archive(io.BytesIO(encrypted), passphrase=_PASSPHRASE) as archive:
        staged = await resource_staging.stage_record_resources(
            db_session,
            archive=archive,
            record=_decoded(resources),
            target_subject_id=subject.id,
            actor_user_id=actor.id,
            private_root=private_root,
        )
    written = staged.newly_written_objects
    assert len(written) == 1

    await db_session.rollback()
    for item in written:
        file_storage.remove_stored_file(
            storage_backend=item.storage_backend,
            storage_ref=item.storage_ref,
            static_dir=private_root,
            private_root=private_root,
        )

    assert await db_session.scalar(select(FileAsset.id)) is None
    assert not [path for path in (tmp_path / "target").rglob("*") if path.is_file()]


@pytest.mark.asyncio
async def test_archive_record_pair_and_absolute_root_fail_before_writing(db_session, tmp_path):
    actor, subject = await _subject(db_session, "stage-preflight")
    resources = [("f00000001", b"%PDF-1.7\nvalid", "lab_document", "application/pdf")]
    encrypted = _encrypted_archive(tmp_path, resources)

    with open_validated_encrypted_archive(io.BytesIO(encrypted), passphrase=_PASSPHRASE) as archive:
        with pytest.raises(ResourceStagingError) as relative:
            await resource_staging.stage_record_resources(
                db_session,
                archive=archive,
                record=_decoded(resources),
                target_subject_id=subject.id,
                actor_user_id=actor.id,
                private_root="relative/private",
            )
        mismatched = replace(_decoded(resources), record_ref="another_record")
        with pytest.raises(ResourceStagingError) as pair:
            await resource_staging.stage_record_resources(
                db_session,
                archive=archive,
                record=mismatched,
                target_subject_id=subject.id,
                actor_user_id=actor.id,
                private_root=str(tmp_path / "target"),
            )
    assert relative.value.code == "private_root_invalid"
    assert pair.value.code == "resource_record_mismatch"
    assert not (tmp_path / "target").exists()
