"""Focused SQLite/PostgreSQL contracts for Stage-3H progress-photo ownership."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    Source,
)
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.tenancy import FileAsset
from vitals.models.weight import ProgressPhoto
from vitals.services import file_asset_service
from vitals.operations.ownership import progress_photo as service
from vitals.operations.ownership.conflict_rule import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.hevy_child import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.hrt_child import (
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.hrt_compound import (
    HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.normalized import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.provider_raw import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
)
from vitals.operations.ownership.raw import RAW_OWNERSHIP_BACKFILL_PHASE


# Every test here writes or inspects a row with no owner, which is the whole
# subject of the ownership backfill: these services exist to give such rows an
# owner. The application can no longer produce that state, so this module asks
# for the schema as it stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract


_EMPTY = hashlib.sha256(b"").hexdigest()
_STAMP = datetime(2020, 1, 1, tzinfo=UTC)
_PRIOR_PHASES = (
    (RAW_OWNERSHIP_BACKFILL_PHASE,)
    + tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
    + tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
    + tuple(CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
)


def _checkpoint(phase: str, subject_id: uuid.UUID) -> OwnershipBackfillCheckpoint:
    return OwnershipBackfillCheckpoint(
        phase_key=phase,
        subject_id=subject_id,
        status="completed",
        scan_high_watermark_id=0,
        snapshot_rows=0,
        last_scanned_id=0,
        scanned_rows=0,
        updated_rows=0,
        unchanged_rows=0,
        data_checksum_before=_EMPTY,
        data_checksum_after=_EMPTY,
        ownership_checksum_after=_EMPTY,
        started_at=_STAMP,
        updated_at=_STAMP,
        completed_at=_STAMP,
    )


async def _ready(session, roots):
    checkpoints = [_checkpoint(phase, roots.subject_id) for phase in _PRIOR_PHASES]
    session.add_all(checkpoints)
    await session.flush()
    return {checkpoint.phase_key: checkpoint for checkpoint in checkpoints}


def _legacy(key: str, *, subject_id=None, actor_user_id=None, file_asset_id=None):
    return ProgressPhoto(
        subject_id=subject_id,
        actor_user_id=actor_user_id,
        file_asset_id=file_asset_id,
        date=date(2026, 1, 2),
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        file_key=key,
        note="synthetic",
    )


async def _finish(session, *, batch_size=250):
    for _ in range(10):
        result = await service.run_progress_photo_ownership_backfill_batch(
            session, batch_size=batch_size
        )
        if result.completed:
            return result
    raise AssertionError("Stage-3H did not complete")


def test_public_contract_is_fixed():
    assert service.PROGRESS_PHOTO_OWNERSHIP_BACKFILL_PHASE == (
        "stage3.file_backed.progress_photos.v1"
    )
    assert service.PROGRESS_PHOTO_OWNERSHIP_BACKFILL_TABLES == ("progress_photos",)
    assert [status.value for status in service.ProgressPhotoOwnershipBackfillStatus] == [
        "not_started",
        "running",
        "completed",
        "restore_blocked",
    ]
    with pytest.raises(TypeError):
        service.PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["x"] = "x"  # type: ignore[index]


@pytest.mark.asyncio
async def test_backfill_creates_fresh_actorless_placeholder_without_data_drift(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    photo = _legacy("uploads/synthetic-history.png")
    db_session.add(photo)
    await db_session.flush()
    timestamps = (photo.created_at, photo.updated_at)

    result = await _finish(db_session)
    await db_session.refresh(photo)
    asset = await db_session.get(FileAsset, photo.file_asset_id)

    assert result.completed and result.updated_rows == 1
    assert photo.subject_id == legacy_owner_roots.subject_id
    assert photo.actor_user_id is None
    assert (photo.created_at, photo.updated_at) == timestamps
    assert asset is not None
    assert asset.uploaded_by_user_id is None
    assert (
        asset.purpose,
        asset.storage_backend,
        asset.storage_ref,
        asset.status,
        asset.media_type,
        asset.byte_size,
        asset.sha256_hex,
    ) == (
        FileAssetPurpose.PROGRESS_PHOTO.value,
        FileStorageBackend.LEGACY_LOCAL.value,
        photo.file_key,
        FileAssetStatus.LEGACY_PLACEHOLDER.value,
        None,
        None,
        None,
    )
    assert "subject_id" not in result.to_safe_dict()


@pytest.mark.asyncio
async def test_stop_resume_and_processed_bound(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    db_session.add_all(
        [_legacy("uploads/a.png"), _legacy("uploads/b.jpeg")]
    )
    await db_session.flush()

    first = await service.run_progress_photo_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert first.status is service.ProgressPhotoOwnershipBackfillStatus.RUNNING
    assert await service.progress_photo_historical_processed_bound(
        db_session, subject_id=legacy_owner_roots.subject_id
    ) == 1
    final = await _finish(db_session, batch_size=1)
    assert final.completed and final.scanned_rows == 2
    assert await service.progress_photo_historical_processed_bound(
        db_session, subject_id=legacy_owner_roots.subject_id
    ) == 2
    repeat = await service.run_progress_photo_ownership_backfill_batch(
        db_session, batch_size=1
    )
    assert repeat.batch_scanned_rows == 0


@pytest.mark.asyncio
async def test_existing_exact_asset_is_never_reused(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    await file_asset_service.register_legacy_local(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        uploaded_by_user_id=None,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref="uploads/existing.png",
    )
    db_session.add(_legacy("uploads/existing.png"))
    await db_session.flush()
    with pytest.raises(service.ProgressPhotoOwnershipBackfillProvenanceError):
        await service.preflight_progress_photo_ownership_backfill(db_session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "uploads/nested/photo.png",
        "uploads/photo.PNG",
        "uploads/labs/photo.png",
        "body/photo.png",
        "uploads/photo.pdf",
        "uploads/../photo.png",
    ],
)
async def test_only_root_level_lowercase_image_keys_are_accepted(
    db_session, legacy_owner_roots, key
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_legacy(key))
    await db_session.flush()
    with pytest.raises(service.ProgressPhotoOwnershipBackfillProvenanceError):
        await service.preflight_progress_photo_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_partial_roots_and_duplicate_keys_fail(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(
        _legacy(
            "uploads/partial.png",
            subject_id=legacy_owner_roots.subject_id,
        )
    )
    await db_session.flush()
    with pytest.raises(service.ProgressPhotoOwnershipBackfillStateError):
        await service.preflight_progress_photo_ownership_backfill(db_session)


@pytest.mark.asyncio
async def test_completed_graph_is_volatile_but_live_orphan_fails(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_legacy("uploads/old.png"))
    await db_session.flush()
    await _finish(db_session)
    photo = await db_session.scalar(select(ProgressPhoto))
    asset = await db_session.get(FileAsset, photo.file_asset_id)
    await db_session.delete(photo)
    await db_session.flush()
    with pytest.raises(service.ProgressPhotoOwnershipBackfillStateError, match="orphaned"):
        await service.preflight_progress_photo_ownership_backfill(db_session)
    asset.status = FileAssetStatus.DELETED.value
    asset.deleted_at = datetime.now(UTC)
    await db_session.flush()
    assert (await service.preflight_progress_photo_ownership_backfill(db_session)).completed


@pytest.mark.asyncio
async def test_portability_block_retires_outgoing_and_validates_imported_shape(
    db_session, legacy_owner_roots
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_legacy("uploads/portable.png"))
    await db_session.flush()
    await _finish(db_session)
    photo = await db_session.scalar(select(ProgressPhoto))
    outgoing_asset_id = photo.file_asset_id

    await service.block_progress_photo_ownership_backfill_for_portability_v1_restore(
        db_session,
        snapshot_bounds={"progress_photos": (photo.id, 1)},
    )
    asset = await db_session.get(FileAsset, outgoing_asset_id)
    assert asset.status == FileAssetStatus.DELETED.value
    await db_session.execute(delete(ProgressPhoto))
    imported = _legacy(
        "uploads/portable.png",
        subject_id=legacy_owner_roots.subject_id,
    )
    imported.id = photo.id
    db_session.add(imported)
    await db_session.flush()
    result = await service.preflight_progress_photo_ownership_backfill(db_session)
    assert result.status is service.ProgressPhotoOwnershipBackfillStatus.RESTORE_BLOCKED
    with pytest.raises(service.ProgressPhotoOwnershipBackfillStateError, match="blocked"):
        await service.run_progress_photo_ownership_backfill_batch(
            db_session, batch_size=1
        )


@pytest.mark.asyncio
async def test_empty_portability_snapshot_completes(db_session, legacy_owner_roots):
    await _ready(db_session, legacy_owner_roots)
    await service.block_progress_photo_ownership_backfill_for_portability_v1_restore(
        db_session,
        snapshot_bounds={"progress_photos": (0, 0)},
    )
    result = await service.preflight_progress_photo_ownership_backfill(db_session)
    assert result.completed and result.snapshot_rows == 0


@pytest.mark.asyncio
async def test_dependency_and_batch_validation_fail_closed(db_session, legacy_owner_roots):
    with pytest.raises(service.ProgressPhotoOwnershipBackfillDependencyError):
        await service.preflight_progress_photo_ownership_backfill(db_session)
    with pytest.raises(service.ProgressPhotoOwnershipBackfillValidationError):
        await service.run_progress_photo_ownership_backfill_batch(
            db_session, batch_size=True
        )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "race",
    ["photo_key", "photo_delete", "duplicate_photo", "asset_lifecycle"],
)
async def test_postgres_projected_graph_races_roll_back_without_progress(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    race,
):
    await _ready(db_session, legacy_owner_roots)
    if race == "asset_lifecycle":
        asset = await file_asset_service.register_legacy_local(
            db_session,
            subject_id=legacy_owner_roots.subject_id,
            uploaded_by_user_id=legacy_owner_roots.user_id,
            purpose=FileAssetPurpose.PROGRESS_PHOTO,
            storage_ref="uploads/race.png",
        )
        photo = _legacy(
            asset.storage_ref,
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
            file_asset_id=asset.id,
        )
    else:
        asset = None
        photo = _legacy("uploads/race.png")
    db_session.add(photo)
    await db_session.commit()
    row_id = photo.id
    asset_id = asset.id if asset is not None else None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    projected = asyncio.Event()
    writer_done = asyncio.Event()

    async def pause():
        projected.set()
        await asyncio.wait_for(writer_done.wait(), timeout=15)

    monkeypatch.setattr(service, "_after_graph_projection_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await service.run_progress_photo_ownership_backfill_batch(
                    session, batch_size=1000
                )
            except Exception as exc:
                await session.rollback()
                return exc
            await session.commit()
            return None

    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(projected.wait(), timeout=15)
        async with factory() as writer:
            if race == "photo_key":
                await writer.execute(
                    update(ProgressPhoto)
                    .where(ProgressPhoto.id == row_id)
                    .values(file_key="uploads/switched.png")
                )
            elif race == "photo_delete":
                await writer.execute(
                    delete(ProgressPhoto).where(ProgressPhoto.id == row_id)
                )
            elif race == "duplicate_photo":
                writer.add(_legacy("uploads/race.png"))
            else:
                await writer.execute(
                    update(FileAsset)
                    .where(FileAsset.id == asset_id)
                    .values(
                        status=FileAssetStatus.DELETED.value,
                        deleted_at=datetime.now(UTC),
                    )
                )
            await writer.commit()
        writer_done.set()
        error = await asyncio.wait_for(task, timeout=15)
    finally:
        writer_done.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert isinstance(
        error,
        (
            service.ProgressPhotoOwnershipBackfillStateError,
            service.ProgressPhotoOwnershipBackfillDuplicateError,
        ),
    )
    async with factory() as verify:
        checkpoint = await verify.get(
            OwnershipBackfillCheckpoint,
            service.PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "progress_photos"
            ],
        )
        assert checkpoint is None
        persisted = await verify.get(ProgressPhoto, row_id)
        if persisted is not None:
            assert persisted.subject_id == (
                legacy_owner_roots.subject_id
                if race == "asset_lifecycle"
                else None
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_same_ref_asset_insert_is_serialized_by_owner_lock(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _ready(db_session, legacy_owner_roots)
    db_session.add(_legacy("uploads/serialized.png"))
    await db_session.commit()
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    projected = asyncio.Event()
    writer_attempted = asyncio.Event()

    async def pause():
        projected.set()
        await asyncio.wait_for(writer_attempted.wait(), timeout=15)

    monkeypatch.setattr(service, "_after_graph_projection_for_test", pause)

    async def worker():
        async with factory() as session:
            try:
                await service.run_progress_photo_ownership_backfill_batch(
                    session, batch_size=1000
                )
            except Exception as exc:
                await session.rollback()
                return exc
            await session.commit()
            return None

    async def writer():
        async with factory() as session:
            session.add(
                FileAsset(
                    subject_id=legacy_owner_roots.subject_id,
                    uploaded_by_user_id=None,
                    opaque_key=uuid.uuid4(),
                    purpose=FileAssetPurpose.PROGRESS_PHOTO.value,
                    storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
                    storage_ref="uploads/serialized.png",
                    status=FileAssetStatus.LEGACY_PLACEHOLDER.value,
                )
            )
            writer_attempted.set()
            try:
                await session.commit()
            except Exception as exc:
                await session.rollback()
                return exc
            return None

    worker_task = asyncio.create_task(worker())
    await asyncio.wait_for(projected.wait(), timeout=15)
    writer_task = asyncio.create_task(writer())
    worker_error = await asyncio.wait_for(worker_task, timeout=15)
    writer_error = await asyncio.wait_for(writer_task, timeout=15)

    if worker_error is None:
        assert isinstance(writer_error, DBAPIError)
    else:
        assert isinstance(
            worker_error,
            service.ProgressPhotoOwnershipBackfillDuplicateError,
        )
        assert writer_error is None
    async with factory() as verify:
        checkpoint = await verify.get(
            OwnershipBackfillCheckpoint,
            service.PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
                "progress_photos"
            ],
        )
        photo = await verify.scalar(select(ProgressPhoto))
        if worker_error is None:
            assert checkpoint is not None and checkpoint.status == "completed"
            assert photo.subject_id == legacy_owner_roots.subject_id
            assert photo.file_asset_id is not None
        else:
            assert checkpoint is None
            assert photo.subject_id is None and photo.file_asset_id is None
        assert int(
            await verify.scalar(
                select(func.count())
                .select_from(FileAsset)
                .where(FileAsset.storage_ref == "uploads/serialized.png")
            )
            or 0
        ) == 1
