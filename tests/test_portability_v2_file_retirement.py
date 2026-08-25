"""Two-phase old-file retirement after portability-v2 replacement."""

from __future__ import annotations

import os
import uuid
from dataclasses import FrozenInstanceError

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import FileAssetPurpose, FileAssetStatus, Source, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset
from vitals.persistence import file_storage
from vitals.services import file_asset_service
from vitals.services.portability.file_retirement import (
    FileRetirementError,
    prepare_old_file_retirement,
    purge_retired_files_post_commit,
)


_BODY = b"synthetic private medical object"
_SHA256 = "8e3ebdfc2179a5ac0c679f948858200b261c84ede2ea9b37019707f698c29234"


async def _other_subject(db_session, suffix: str):
    owner = User(
        username=f"retirement-owner-{suffix}",
        normalized_username=f"retirement-owner-{suffix}",
        password_hash="$synthetic-retirement",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(owner)
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name="Synthetic retirement subject",
        timezone="UTC",
    )
    db_session.add(subject)
    await db_session.flush()
    return owner, subject


async def _private_asset(
    db_session,
    roots,
    private_root: str,
    suffix: str,
) -> tuple[FileAsset, str]:
    storage_ref = f"labs/{suffix[:2]}/{suffix}.pdf"
    path = file_storage.write_private_file(private_root, storage_ref, _BODY)
    asset = await file_asset_service.register_private_local(
        db_session,
        subject_id=roots.subject_id,
        uploaded_by_user_id=roots.user_id,
        purpose=FileAssetPurpose.LAB_DOCUMENT,
        storage_ref=storage_ref,
        media_type="application/pdf",
        size_bytes=len(_BODY),
        content_sha256=_SHA256,
    )
    return asset, path


def _factory(db_session) -> async_sessionmaker[AsyncSession]:
    assert db_session.bind is not None
    return async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )


def _raw_reference(roots, asset: FileAsset, suffix: str) -> RawPayload:
    return RawPayload(
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id,
        file_asset_id=asset.id,
        domain="labs",
        source=Source.MANUAL.value,
        external_id=f"synthetic-retirement-{suffix}",
        payload={"synthetic": True},
    )


@pytest.mark.parametrize(
    ("subject_id", "asset_ids", "code"),
    [
        ("subject", (), "file_retirement_subject_invalid"),
        (uuid.UUID(int=0), (), "file_retirement_subject_invalid"),
        (uuid.uuid4(), "assets", "file_retirement_ids_invalid"),
        (uuid.uuid4(), (uuid.UUID(int=0),), "file_retirement_ids_invalid"),
    ],
)
async def test_invalid_control_inputs_fail_with_stable_codes(
    db_session,
    subject_id,
    asset_ids,
    code,
):
    with pytest.raises(FileRetirementError) as raised:
        await prepare_old_file_retirement(
            db_session,
            target_subject_id=subject_id,
            old_file_asset_ids=asset_ids,
        )
    assert raised.value.code == code


async def test_duplicate_or_missing_ids_fail_before_any_lifecycle_change(
    db_session,
    legacy_owner_roots,
    tmp_path,
):
    asset, path = await _private_asset(
        db_session,
        legacy_owner_roots,
        str(tmp_path / "private"),
        "duplicate",
    )
    await db_session.commit()

    with pytest.raises(FileRetirementError) as duplicated:
        await prepare_old_file_retirement(
            db_session,
            target_subject_id=legacy_owner_roots.subject_id,
            old_file_asset_ids=(asset.id, asset.id),
        )
    assert duplicated.value.code == "file_retirement_ids_invalid"

    with pytest.raises(FileRetirementError) as missing:
        await prepare_old_file_retirement(
            db_session,
            target_subject_id=legacy_owner_roots.subject_id,
            old_file_asset_ids=(asset.id, uuid.uuid4()),
        )
    assert missing.value.code == "file_retirement_scope_invalid"
    assert asset.status == FileAssetStatus.ACTIVE.value
    assert os.path.exists(path)


async def test_missing_subject_fails_even_for_an_empty_retirement(db_session):
    with pytest.raises(FileRetirementError) as raised:
        await prepare_old_file_retirement(
            db_session,
            target_subject_id=uuid.uuid4(),
            old_file_asset_ids=(),
        )
    assert raised.value.code == "file_retirement_subject_not_found"


async def test_cross_subject_asset_refuses_the_entire_batch(
    db_session,
    legacy_owner_roots,
    tmp_path,
):
    asset, path = await _private_asset(
        db_session,
        legacy_owner_roots,
        str(tmp_path / "private"),
        "foreign-scope",
    )
    _owner, other = await _other_subject(db_session, "foreign-scope")
    await db_session.commit()

    with pytest.raises(FileRetirementError) as raised:
        await prepare_old_file_retirement(
            db_session,
            target_subject_id=other.id,
            old_file_asset_ids=(asset.id,),
        )

    assert raised.value.code == "file_retirement_scope_invalid"
    assert asset.status == FileAssetStatus.ACTIVE.value
    assert os.path.exists(path)


async def test_prepare_preserves_every_referenced_asset_and_only_soft_deletes_orphans(
    db_session,
    legacy_owner_roots,
    tmp_path,
):
    private_root = str(tmp_path / "private")
    shared, shared_path = await _private_asset(
        db_session, legacy_owner_roots, private_root, "shared"
    )
    orphan, orphan_path = await _private_asset(
        db_session, legacy_owner_roots, private_root, "orphan"
    )
    db_session.add(_raw_reference(legacy_owner_roots, shared, "shared"))
    await db_session.commit()

    plan = await prepare_old_file_retirement(
        db_session,
        target_subject_id=legacy_owner_roots.subject_id,
        old_file_asset_ids=(orphan.id, shared.id),
    )
    assert plan.retired_asset_ids == (orphan.id,)
    assert plan.preserved_referenced_asset_ids == (shared.id,)
    assert shared.status == FileAssetStatus.ACTIVE.value
    assert orphan.status == FileAssetStatus.DELETED.value
    assert orphan.deleted_at is not None and orphan.purged_at is None
    assert os.path.exists(shared_path)
    assert os.path.exists(orphan_path)
    with pytest.raises(FrozenInstanceError):
        plan.subject_id = uuid.uuid4()
    with pytest.raises(FrozenInstanceError):
        plan.objects[0].storage_ref = "labs/replaced.pdf"


async def test_prepare_is_flush_only_and_rollback_restores_metadata_and_bytes(
    db_session,
    legacy_owner_roots,
    tmp_path,
):
    private_root = str(tmp_path / "private")
    asset, path = await _private_asset(db_session, legacy_owner_roots, private_root, "rollback")
    asset_id = asset.id
    await db_session.commit()

    plan = await prepare_old_file_retirement(
        db_session,
        target_subject_id=legacy_owner_roots.subject_id,
        old_file_asset_ids=(asset.id,),
    )
    assert asset.status == FileAssetStatus.DELETED.value
    assert os.path.exists(path)

    with pytest.raises(FileRetirementError) as premature:
        await purge_retired_files_post_commit(
            _factory(db_session),
            plan=plan,
            static_dir=str(tmp_path / "static"),
            private_root=private_root,
        )
    assert premature.value.code == "file_retirement_not_committed"
    assert os.path.exists(path)

    await db_session.rollback()
    restored = await db_session.get(FileAsset, asset_id)
    assert restored is not None
    assert restored.status == FileAssetStatus.ACTIVE.value
    assert restored.deleted_at is None
    assert os.path.exists(path)

    report = await purge_retired_files_post_commit(
        _factory(db_session),
        plan=plan,
        static_dir=str(tmp_path / "static"),
        private_root=private_root,
    )
    assert report.complete is False
    assert report.failures[0].code == "purge_asset_not_retired"
    assert os.path.exists(path)


async def test_committed_plan_purges_with_an_independent_retryable_checkpoint(
    db_session,
    legacy_owner_roots,
    tmp_path,
):
    private_root = str(tmp_path / "private")
    asset, path = await _private_asset(db_session, legacy_owner_roots, private_root, "committed")
    await db_session.commit()
    plan = await prepare_old_file_retirement(
        db_session,
        target_subject_id=legacy_owner_roots.subject_id,
        old_file_asset_ids=(asset.id,),
    )
    await db_session.commit()

    report = await purge_retired_files_post_commit(
        _factory(db_session),
        plan=plan,
        static_dir=str(tmp_path / "static"),
        private_root=private_root,
    )

    assert report.complete is True
    assert report.purged_asset_ids == (asset.id,)
    assert report.failures == ()
    assert not os.path.exists(path)
    await db_session.refresh(asset)
    assert asset.status == FileAssetStatus.PURGED.value
    assert asset.purged_at is not None

    repeated = await purge_retired_files_post_commit(
        _factory(db_session),
        plan=plan,
        static_dir=str(tmp_path / "static"),
        private_root=private_root,
    )
    assert repeated.complete is True
    assert repeated.purged_asset_ids == ()
    assert repeated.already_purged_asset_ids == (asset.id,)


async def test_physical_failure_leaves_deleted_checkpoint_and_retry_succeeds(
    db_session,
    legacy_owner_roots,
    tmp_path,
    monkeypatch,
):
    private_root = str(tmp_path / "private")
    asset, path = await _private_asset(db_session, legacy_owner_roots, private_root, "retry")
    await db_session.commit()
    plan = await prepare_old_file_retirement(
        db_session,
        target_subject_id=legacy_owner_roots.subject_id,
        old_file_asset_ids=(asset.id,),
    )
    await db_session.commit()
    original_remove = file_storage.remove_stored_file

    def fail_remove(**_kwargs):
        raise OSError("synthetic purge failure")

    monkeypatch.setattr(file_storage, "remove_stored_file", fail_remove)
    failed = await purge_retired_files_post_commit(
        _factory(db_session),
        plan=plan,
        static_dir=str(tmp_path / "static"),
        private_root=private_root,
    )
    assert failed.complete is False
    assert failed.failures[0].code == "purge_operation_failed"
    assert os.path.exists(path)
    await db_session.refresh(asset)
    assert asset.status == FileAssetStatus.DELETED.value
    assert asset.purged_at is None

    monkeypatch.setattr(file_storage, "remove_stored_file", original_remove)
    retried = await purge_retired_files_post_commit(
        _factory(db_session),
        plan=plan,
        static_dir=str(tmp_path / "static"),
        private_root=private_root,
    )
    assert retried.complete is True
    assert retried.purged_asset_ids == (asset.id,)
    assert not os.path.exists(path)


async def test_postcommit_recheck_preserves_a_new_reference(
    db_session,
    legacy_owner_roots,
    tmp_path,
):
    private_root = str(tmp_path / "private")
    asset, path = await _private_asset(
        db_session, legacy_owner_roots, private_root, "late-reference"
    )
    await db_session.commit()
    plan = await prepare_old_file_retirement(
        db_session,
        target_subject_id=legacy_owner_roots.subject_id,
        old_file_asset_ids=(asset.id,),
    )
    await db_session.commit()
    db_session.add(_raw_reference(legacy_owner_roots, asset, "late"))
    await db_session.commit()

    report = await purge_retired_files_post_commit(
        _factory(db_session),
        plan=plan,
        static_dir=str(tmp_path / "static"),
        private_root=private_root,
    )

    assert report.complete is False
    assert report.failures[0].code == "purge_asset_referenced"
    assert os.path.exists(path)
    await db_session.refresh(asset)
    assert asset.status == FileAssetStatus.DELETED.value
