"""Atomic coordinator coverage for portability-v2 replacement imports."""

from __future__ import annotations

import io
import os
import uuid
from contextlib import contextmanager
from types import MappingProxyType, SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import FileAssetPurpose, FileAssetStatus, FileStorageBackend, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.portability import PortabilityImportReceipt
from vitals.models.tenancy import FileAsset
from vitals.operations.portability import import_v2
from vitals.persistence import file_storage
from vitals.services.portability.archive import write_inner_archive
from vitals.services.portability.archive_reader import open_validated_encrypted_archive
from vitals.services.portability.crypto import EncryptingWriter
from vitals.services.portability.receipts import ReceiptServiceError
from vitals.services.portability.record_decoder import decode_validated_record
from vitals.services.portability.replacement_apply import ReplacementApplyResult
from vitals.services.portability.resource_staging import (
    NewlyWrittenPrivateObject,
    StagedResourceMapping,
)
from vitals.services.portability.resources import ResourceLocations
from vitals.services.portability.schema import (
    PORTABILITY_SCHEMA_DESCRIPTOR,
    PORTABILITY_SCHEMA_DIGEST,
)


_PASSPHRASE = "synthetic coordinator test passphrase"


class _CountingSession(AsyncSession):
    commit_calls = 0

    async def commit(self) -> None:
        type(self).commit_calls += 1
        await super().commit()


class _CommitUnknownSession(AsyncSession):
    commit_calls = 0

    async def commit(self) -> None:
        type(self).commit_calls += 1
        await super().commit()
        raise OSError("synthetic lost commit acknowledgement")


def _session_factory(
    db_session,
    *,
    session_class: type[AsyncSession] = _CountingSession,
) -> async_sessionmaker[AsyncSession]:
    session_class.commit_calls = 0
    return async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=session_class,
    )


async def _roots(db_session, slug: str):
    owner = User(
        username=f"import-v2-owner-{slug}",
        normalized_username=f"import-v2-owner-{slug}",
        password_hash="$synthetic-import-v2",
        status=UserStatus.ACTIVE.value,
    )
    alternate_actor = User(
        username=f"import-v2-actor-{slug}",
        normalized_username=f"import-v2-actor-{slug}",
        password_hash="$synthetic-import-v2",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add_all((owner, alternate_actor))
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name=f"Synthetic import {slug}",
        timezone="Asia/Almaty",
    )
    db_session.add(subject)
    await db_session.commit()
    return owner, alternate_actor, subject


def _encrypted_empty_archive(tmp_path) -> bytes:
    tables = [
        {"name": name, "rows": []} for name in sorted(PORTABILITY_SCHEMA_DESCRIPTOR["insert_order"])
    ]
    graph = SimpleNamespace(
        manifest={
            "format": "vitals-portability-graph",
            "version": 2,
            "schema_digest": PORTABILITY_SCHEMA_DIGEST,
            "tables": tables,
            "connections": [],
            "resources": [],
            "totals": {
                "tables": len(tables),
                "rows": 0,
                "connections": 0,
                "resources": 0,
            },
        },
        prepared_resources=(),
    )
    plaintext = io.BytesIO()
    write_inner_archive(
        graph,
        plaintext,
        archive_id=uuid.uuid4(),
        record_ref=f"import_v2_{uuid.uuid4().hex}",
        locations=ResourceLocations(
            static_dir=str(tmp_path / "static"),
            private_root=str(tmp_path / "source-private"),
        ),
    )
    encrypted = io.BytesIO()
    with EncryptingWriter(encrypted, passphrase=_PASSPHRASE) as writer:
        writer.write(plaintext.getvalue())
    return encrypted.getvalue()


@contextmanager
def _archive_and_record(tmp_path):
    encrypted = _encrypted_empty_archive(tmp_path)
    with open_validated_encrypted_archive(
        io.BytesIO(encrypted),
        passphrase=_PASSPHRASE,
    ) as archive:
        yield archive, decode_validated_record(archive)


def _old_asset(subject, owner, storage_ref: str) -> FileAsset:
    return FileAsset(
        subject_id=subject.id,
        uploaded_by_user_id=owner.id,
        opaque_key=uuid.uuid4(),
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
        storage_ref=storage_ref,
        media_type="application/pdf",
        byte_size=12,
        sha256_hex="a" * 64,
        status=FileAssetStatus.ACTIVE.value,
    )


async def test_success_commits_receipt_once_and_atomically_retires_old_files(
    db_session,
    tmp_path,
    monkeypatch,
):
    owner, _, subject = await _roots(db_session, "success")
    old_asset = _old_asset(subject, owner, "labs/aa/old.pdf")
    db_session.add(old_asset)
    await db_session.commit()
    factory = _session_factory(db_session)

    async def apply_stub(session, **_kwargs):
        target = await session.get(HealthSubject, subject.id)
        assert target is not None
        target.display_name = "Committed replacement"
        await session.flush()
        return ReplacementApplyResult(
            row_ids_by_ref=MappingProxyType({}),
            old_file_asset_ids=(old_asset.id,),
            deleted=(),
            inserted=(),
        )

    monkeypatch.setattr(import_v2, "apply_record_replacement", apply_stub)
    operation_id = uuid.uuid4()
    private_root = str(tmp_path / "target-private")
    with _archive_and_record(tmp_path) as (archive, record):
        result = await import_v2.import_validated_record_v2(
            factory,
            archive=archive,
            record=record,
            target_subject_id=subject.id,
            actor_user_id=owner.id,
            operation_id=operation_id,
            connection_ids_by_ref={},
            private_root=private_root,
        )

    assert result.receipt.created is True
    assert result.replayed is False
    assert _CountingSession.commit_calls == 1
    assert result.retirement_plan is not None
    assert result.retirement_plan.retired_asset_ids == (old_asset.id,)
    assert (
        result.retirement_plan.objects[0].storage_backend == FileStorageBackend.PRIVATE_LOCAL.value
    )
    assert result.retirement_plan.objects[0].storage_ref == "labs/aa/old.pdf"
    async with factory() as check:
        assert (
            await check.scalar(
                select(HealthSubject.display_name).where(HealthSubject.id == subject.id)
            )
            == "Committed replacement"
        )
        assert await check.scalar(select(func.count()).select_from(PortabilityImportReceipt)) == 1
        retired = await check.get(FileAsset, old_asset.id)
        assert retired is not None
        assert retired.status == FileAssetStatus.DELETED.value


async def test_exact_replay_short_circuits_before_mapping_staging_or_mutation(
    db_session,
    tmp_path,
    monkeypatch,
):
    owner, _, subject = await _roots(db_session, "replay")
    factory = _session_factory(db_session)
    operation_id = uuid.uuid4()
    private_root = str(tmp_path / "target-private")
    with _archive_and_record(tmp_path) as (archive, record):
        first = await import_v2.import_validated_record_v2(
            factory,
            archive=archive,
            record=record,
            target_subject_id=subject.id,
            actor_user_id=owner.id,
            operation_id=operation_id,
            connection_ids_by_ref={},
            private_root=private_root,
        )

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("replay reached a mutation-stage dependency")

        monkeypatch.setattr(import_v2, "resolve_connection_mapping", forbidden)
        monkeypatch.setattr(import_v2, "prepare_replacement_preflight", forbidden)
        monkeypatch.setattr(import_v2, "stage_record_resources", forbidden)
        monkeypatch.setattr(import_v2, "apply_record_replacement", forbidden)
        replay = await import_v2.import_validated_record_v2(
            factory,
            archive=archive,
            record=record,
            target_subject_id=subject.id,
            actor_user_id=owner.id,
            operation_id=operation_id,
            connection_ids_by_ref={},
            private_root=private_root,
        )

    assert replay.replayed is True
    assert replay.receipt.receipt_id == first.receipt.receipt_id
    assert replay.apply_result is None
    assert replay.newly_written_objects == ()
    assert replay.retirement_plan is None
    assert _CountingSession.commit_calls == 1


async def test_failure_rolls_back_database_and_removes_every_staged_object(
    db_session,
    tmp_path,
    monkeypatch,
):
    owner, _, subject = await _roots(db_session, "rollback")
    original_name = subject.display_name
    factory = _session_factory(db_session)
    private_root = str(tmp_path / "target-private")
    storage_ref = "labs/aa/staged.pdf"
    body = b"%PDF-1.7\nsynthetic"

    async def stage_stub(*_args, **_kwargs):
        file_storage.write_private_file(private_root, storage_ref, body)
        return StagedResourceMapping(
            bindings=(),
            newly_written_objects=(
                NewlyWrittenPrivateObject(
                    resource_ref="f00000001",
                    storage_ref=storage_ref,
                    byte_size=len(body),
                    sha256_hex="b" * 64,
                ),
            ),
        )

    async def apply_failure(session, **_kwargs):
        target = await session.get(HealthSubject, subject.id)
        assert target is not None
        target.display_name = "Must roll back"
        await session.flush()
        raise RuntimeError("synthetic apply failure")

    monkeypatch.setattr(import_v2, "stage_record_resources", stage_stub)
    monkeypatch.setattr(import_v2, "apply_record_replacement", apply_failure)
    with _archive_and_record(tmp_path) as (archive, record):
        with pytest.raises(RuntimeError, match="synthetic apply failure"):
            await import_v2.import_validated_record_v2(
                factory,
                archive=archive,
                record=record,
                target_subject_id=subject.id,
                actor_user_id=owner.id,
                operation_id=uuid.uuid4(),
                connection_ids_by_ref={},
                private_root=private_root,
            )

    path = file_storage.private_file_disk_path(private_root, storage_ref)
    assert not os.path.exists(path)
    assert _CountingSession.commit_calls == 0
    async with factory() as check:
        assert (
            await check.scalar(
                select(HealthSubject.display_name).where(HealthSubject.id == subject.id)
            )
            == original_name
        )
        assert await check.scalar(select(func.count()).select_from(PortabilityImportReceipt)) == 0


async def test_mismatched_replay_fails_before_mapping_or_mutation(
    db_session,
    tmp_path,
    monkeypatch,
):
    owner, alternate_actor, subject = await _roots(db_session, "mismatch")
    factory = _session_factory(db_session)
    operation_id = uuid.uuid4()
    private_root = str(tmp_path / "target-private")
    with _archive_and_record(tmp_path) as (archive, record):
        await import_v2.import_validated_record_v2(
            factory,
            archive=archive,
            record=record,
            target_subject_id=subject.id,
            actor_user_id=owner.id,
            operation_id=operation_id,
            connection_ids_by_ref={},
            private_root=private_root,
        )

        async def forbidden(*_args, **_kwargs):
            raise AssertionError("mismatched replay reached mutation dependencies")

        monkeypatch.setattr(import_v2, "resolve_connection_mapping", forbidden)
        monkeypatch.setattr(import_v2, "prepare_replacement_preflight", forbidden)
        monkeypatch.setattr(import_v2, "stage_record_resources", forbidden)
        monkeypatch.setattr(import_v2, "apply_record_replacement", forbidden)
        with pytest.raises(ReceiptServiceError) as raised:
            await import_v2.import_validated_record_v2(
                factory,
                archive=archive,
                record=record,
                target_subject_id=subject.id,
                actor_user_id=alternate_actor.id,
                operation_id=operation_id,
                connection_ids_by_ref={},
                private_root=private_root,
            )

    assert raised.value.code == "receipt_metadata_mismatch"
    assert _CountingSession.commit_calls == 1


async def test_lost_commit_acknowledgement_requires_fresh_authoritative_receipt(
    db_session,
    tmp_path,
):
    owner, _, subject = await _roots(db_session, "commit-unknown")
    factory = _session_factory(db_session, session_class=_CommitUnknownSession)
    operation_id = uuid.uuid4()
    with _archive_and_record(tmp_path) as (archive, record):
        result = await import_v2.import_validated_record_v2(
            factory,
            archive=archive,
            record=record,
            target_subject_id=subject.id,
            actor_user_id=owner.id,
            operation_id=operation_id,
            connection_ids_by_ref={},
            private_root=str(tmp_path / "target-private"),
        )

    assert result.replayed is True
    assert result.receipt.request.operation_id == operation_id
    assert _CommitUnknownSession.commit_calls == 1
    async with factory() as check:
        assert await check.scalar(select(func.count()).select_from(PortabilityImportReceipt)) == 1
