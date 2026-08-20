"""Stage-2 ownership, lifecycle, and delivery boundaries for ProgressPhoto."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    Source,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import FileAsset
from vitals.models.weight import ProgressPhoto
from vitals.ownership import WriteIdentity
from vitals.services import (
    conflict_engine,
    file_asset_service,
    timeline_service,
    weight_service,
)
from vitals.utils.timeutils import now_local


PHOTO_DATE = date(2026, 8, 20)
OTHER_DATE = date(2026, 8, 21)


def _identity(legacy_owner_roots) -> WriteIdentity:
    return WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )


def _context(
    identity: WriteIdentity,
    *,
    on_date: date = PHOTO_DATE,
    legacy: bool = False,
) -> conflict_engine.ConflictWriteContext:
    return conflict_engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=on_date,
        legacy_bridge=(
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
            if legacy
            else conflict_engine.LegacyConflictBridge.REJECT
        ),
    )


async def _prepared(
    session: AsyncSession,
    identity: WriteIdentity,
    *,
    on_date: date = PHOTO_DATE,
    legacy: bool = False,
):
    return await conflict_engine.prepare_scoped_write(
        session,
        context=_context(identity, on_date=on_date, legacy=legacy),
    )


async def _asset(
    session: AsyncSession,
    identity: WriteIdentity,
    suffix: str,
) -> FileAsset:
    return await file_asset_service.register_legacy_local(
        session,
        subject_id=identity.subject_id,
        uploaded_by_user_id=identity.actor_user_id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref=f"uploads/synthetic-{suffix}.png",
        media_type="image/png",
        size_bytes=23,
        content_sha256="a" * 64,
    )


async def _owned_photo(
    session: AsyncSession,
    identity: WriteIdentity,
    suffix: str,
    *,
    on_date: date = PHOTO_DATE,
    note: str | None = None,
) -> tuple[FileAsset, ProgressPhoto]:
    asset = await _asset(session, identity, suffix)
    photo = await weight_service.add_progress_photo(
        session,
        on_date=on_date,
        note=note,
        identity=identity,
        file_asset_id=asset.id,
        prepared_conflict_write=await _prepared(
            session,
            identity,
            on_date=on_date,
        ),
    )
    return asset, photo


async def _new_owner(
    session: AsyncSession,
    slug: str,
) -> tuple[User, HealthSubject, WriteIdentity]:
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
    return user, subject, WriteIdentity(subject.id, user.id)


async def test_create_stamps_exact_s_a_f_and_derives_the_key(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    asset = await _asset(db_session, identity, "exact")

    photo = await weight_service.add_progress_photo(
        db_session,
        on_date=PHOTO_DATE,
        note="synthetic exact graph",
        identity=identity,
        file_asset_id=asset.id,
        prepared_conflict_write=await _prepared(db_session, identity),
    )

    assert (
        photo.subject_id,
        photo.actor_user_id,
        photo.file_asset_id,
        photo.file_key,
        photo.domain,
        photo.source,
    ) == (
        identity.subject_id,
        identity.actor_user_id,
        asset.id,
        asset.storage_ref,
        Domain.WEIGHT.value,
        Source.MANUAL.value,
    )

    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="file key conflicts",
    ):
        await weight_service.add_progress_photo(
            db_session,
            on_date=PHOTO_DATE,
            file_key="uploads/client-substitution.png",
            identity=identity,
            file_asset_id=asset.id,
            prepared_conflict_write=await _prepared(db_session, identity),
        )


async def test_one_file_asset_cannot_back_two_progress_photo_facts(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    asset, first = await _owned_photo(db_session, identity, "exclusive")

    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="already has a fact",
    ):
        await weight_service.add_progress_photo(
            db_session,
            on_date=OTHER_DATE,
            identity=identity,
            file_asset_id=asset.id,
            prepared_conflict_write=await _prepared(
                db_session,
                identity,
                on_date=OTHER_DATE,
            ),
        )

    assert await db_session.scalar(
        select(func.count()).select_from(ProgressPhoto)
    ) == 1
    assert await db_session.get(ProgressPhoto, first.id) is first


async def test_create_rejects_active_non_owner_actor_even_with_matching_asset(
    db_session,
    legacy_owner_roots,
):
    owner_identity = _identity(legacy_owner_roots)
    foreign_user, _, _ = await _new_owner(db_session, "photo-non-owner-actor")
    forged_identity = WriteIdentity(
        owner_identity.subject_id,
        foreign_user.id,
    )
    asset = await _asset(db_session, forged_identity, "non-owner-actor")

    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="owner",
    ):
        await weight_service.add_progress_photo(
            db_session,
            on_date=PHOTO_DATE,
            identity=forged_identity,
            file_asset_id=asset.id,
            prepared_conflict_write=await _prepared(
                db_session,
                forged_identity,
            ),
        )
    assert await db_session.scalar(
        select(func.count()).select_from(ProgressPhoto)
    ) == 0


@pytest.mark.parametrize(
    "broken_part",
    [
        "photo_missing_actor",
        "photo_missing_file",
        "asset_missing_uploader",
        "asset_wrong_purpose",
        "asset_wrong_backend",
        "asset_retired",
        "asset_wrong_key",
        "asset_cross_subject",
    ],
)
async def test_exact_s_a_f_graph_rejects_partial_and_cross_root_rows(
    db_session,
    legacy_owner_roots,
    broken_part,
):
    identity = _identity(legacy_owner_roots)
    foreign_user, foreign_subject, _ = await _new_owner(
        db_session,
        f"photo-graph-{broken_part}",
    )
    asset, photo = await _owned_photo(db_session, identity, broken_part)

    if broken_part == "photo_missing_actor":
        photo.actor_user_id = None
    elif broken_part == "photo_missing_file":
        photo.file_asset_id = None
    elif broken_part == "asset_missing_uploader":
        asset.uploaded_by_user_id = None
    elif broken_part == "asset_wrong_purpose":
        asset.purpose = FileAssetPurpose.LAB_DOCUMENT.value
    elif broken_part == "asset_wrong_backend":
        asset.status = FileAssetStatus.PENDING.value
        asset.storage_backend = FileStorageBackend.PRIVATE_LOCAL.value
    elif broken_part == "asset_retired":
        asset.status = FileAssetStatus.DELETED.value
        asset.deleted_at = now_local()
    elif broken_part == "asset_wrong_key":
        asset.storage_ref = f"uploads/synthetic-other-{broken_part}.png"
    elif broken_part == "asset_cross_subject":
        asset.subject_id = foreign_subject.id
        asset.uploaded_by_user_id = foreign_user.id
    await db_session.commit()

    with pytest.raises(weight_service.ProgressPhotoOwnershipError):
        await weight_service.list_progress_photos(
            db_session,
            subject_id=identity.subject_id,
            include_legacy_unowned=True,
        )


@pytest.mark.parametrize("partial_root", ["actor", "file"])
async def test_legacy_bridge_accepts_only_fully_null_s_a_f(
    db_session,
    legacy_owner_roots,
    partial_root,
):
    identity = _identity(legacy_owner_roots)
    asset = await _asset(db_session, identity, f"partial-{partial_root}")
    partial = ProgressPhoto(
        actor_user_id=(identity.actor_user_id if partial_root == "actor" else None),
        file_asset_id=(asset.id if partial_root == "file" else None),
        date=PHOTO_DATE,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        file_key=asset.storage_ref,
    )
    db_session.add(partial)
    await db_session.commit()

    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="partial legacy ownership roots",
    ):
        await weight_service.list_progress_photos(
            db_session,
            subject_id=identity.subject_id,
            include_legacy_unowned=True,
        )


async def test_fully_null_legacy_photo_is_visible_and_deletable_only_via_bridge(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    legacy = ProgressPhoto(
        date=PHOTO_DATE,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        file_key="uploads/synthetic-legacy.png",
        note="synthetic legacy",
    )
    db_session.add(legacy)
    await db_session.commit()

    assert await weight_service.list_progress_photos(
        db_session,
        subject_id=identity.subject_id,
    ) == []
    visible = await weight_service.list_progress_photos(
        db_session,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
    )
    assert [row.id for row in visible] == [legacy.id]

    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await weight_service.delete_progress_photo(
            db_session,
            legacy.id,
            identity=identity,
            include_legacy_unowned=True,
            prepared_conflict_write=await _prepared(db_session, identity),
        )

    receipt = await weight_service.delete_progress_photo(
        db_session,
        legacy.id,
        identity=identity,
        include_legacy_unowned=True,
        prepared_conflict_write=await _prepared(
            db_session,
            identity,
            legacy=True,
        ),
    )
    assert receipt == weight_service.ProgressPhotoDeletion(
        file_key="uploads/synthetic-legacy.png",
        file_asset_id=None,
    )


@pytest.mark.parametrize(
    "shadow_state",
    ["deleted", "purged", "wrong_purpose", "valid_live"],
)
async def test_fully_null_legacy_photo_with_same_key_asset_fails_closed(
    auth_client,
    db_session,
    legacy_owner_roots,
    tmp_path,
    monkeypatch,
    shadow_state,
):
    from web import main as web_main

    identity = _identity(legacy_owner_roots)
    route_key = f"synthetic-legacy-shadow-{shadow_state}.png"
    file_key = f"uploads/{route_key}"
    contents = b"synthetic shadowed legacy photo"
    (tmp_path / route_key).write_bytes(contents)
    monkeypatch.setattr(web_main, "UPLOADS_DIR", str(tmp_path))

    asset = await file_asset_service.register_legacy_local(
        db_session,
        subject_id=identity.subject_id,
        uploaded_by_user_id=identity.actor_user_id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref=file_key,
        media_type="image/png",
        size_bytes=len(contents),
        content_sha256="c" * 64,
    )
    if shadow_state == "wrong_purpose":
        asset.purpose = FileAssetPurpose.LAB_DOCUMENT.value
    elif shadow_state in {"deleted", "purged"}:
        asset.status = (
            FileAssetStatus.PURGED.value
            if shadow_state == "purged"
            else FileAssetStatus.DELETED.value
        )
        asset.deleted_at = now_local()
        if shadow_state == "purged":
            asset.purged_at = asset.deleted_at
    await db_session.flush()
    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="conflicts with file-asset metadata",
    ):
        await weight_service.add_progress_photo(
            db_session,
            on_date=PHOTO_DATE,
            file_key=file_key,
        )

    # Persist a pre-hardening shape so read/download/delete are verified too.
    legacy = ProgressPhoto(
        date=PHOTO_DATE,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        file_key=file_key,
    )
    db_session.add(legacy)
    await db_session.commit()
    expected_asset_state = (
        asset.subject_id,
        asset.uploaded_by_user_id,
        asset.purpose,
        asset.storage_backend,
        asset.storage_ref,
        asset.status,
        asset.deleted_at,
        asset.purged_at,
    )

    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="conflicts with file-asset metadata",
    ):
        await weight_service.list_progress_photos(
            db_session,
            subject_id=identity.subject_id,
            include_legacy_unowned=True,
        )

    response = await auth_client.get(f"/static/uploads/{route_key}")
    assert response.status_code == 404
    assert contents not in response.content

    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="conflicts with file-asset metadata",
    ):
        await weight_service.delete_progress_photo(db_session, legacy.id)

    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="conflicts with file-asset metadata",
    ):
        await weight_service.delete_progress_photo(
            db_session,
            legacy.id,
            identity=identity,
            include_legacy_unowned=True,
            prepared_conflict_write=await _prepared(
                db_session,
                identity,
                legacy=True,
            ),
        )

    persisted_photo_roots = (
        await db_session.execute(
            select(
                ProgressPhoto.subject_id,
                ProgressPhoto.actor_user_id,
                ProgressPhoto.file_asset_id,
                ProgressPhoto.file_key,
            ).where(ProgressPhoto.id == legacy.id)
        )
    ).one()
    persisted_asset_state = (
        await db_session.execute(
            select(
                FileAsset.subject_id,
                FileAsset.uploaded_by_user_id,
                FileAsset.purpose,
                FileAsset.storage_backend,
                FileAsset.storage_ref,
                FileAsset.status,
                FileAsset.deleted_at,
                FileAsset.purged_at,
            ).where(FileAsset.id == asset.id)
        )
    ).one()
    assert persisted_photo_roots == (None, None, None, file_key)
    assert tuple(persisted_asset_state[:6]) == expected_asset_state[:6]
    for actual_at, expected_at in zip(
        persisted_asset_state[6:],
        expected_asset_state[6:],
        strict=True,
    ):
        if expected_at is None:
            assert actual_at is None
        else:
            assert actual_at is not None
            assert actual_at.timestamp() == pytest.approx(expected_at.timestamp())


@pytest.mark.parametrize("route_prefix", ["labs", "body"])
async def test_legacy_nested_photo_without_document_metadata_remains_valid(
    db_session,
    legacy_owner_roots,
    route_prefix,
):
    identity = _identity(legacy_owner_roots)
    file_key = f"uploads/{route_prefix}/synthetic-valid-legacy-nested.png"
    photo = await weight_service.add_progress_photo(
        db_session,
        on_date=PHOTO_DATE,
        file_key=file_key,
    )

    visible = await weight_service.list_progress_photos(
        db_session,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
    )
    assert visible == [photo]
    assert (photo.subject_id, photo.actor_user_id, photo.file_asset_id) == (
        None,
        None,
        None,
    )


async def test_prepared_capability_is_required_and_checked_before_file_resolution(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    _, _, foreign = await _new_owner(db_session, "photo-capability-foreign")
    asset = await _asset(db_session, identity, "capability-required")
    wrong = await _prepared(db_session, foreign)

    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await weight_service.add_progress_photo(
            db_session,
            on_date=PHOTO_DATE,
            identity=identity,
            file_asset_id=asset.id,
        )
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await weight_service.add_progress_photo(
            db_session,
            on_date=PHOTO_DATE,
            identity=identity,
            file_asset_id=foreign.subject_id,
            prepared_conflict_write=wrong,
        )
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await weight_service.delete_progress_photo(
            db_session,
            999_999,
            identity=identity,
            prepared_conflict_write=wrong,
        )


async def test_delete_requires_subject_owner_actor_for_exact_and_legacy_rows(
    db_session,
    legacy_owner_roots,
):
    owner = _identity(legacy_owner_roots)
    asset, exact = await _owned_photo(db_session, owner, "delete-owner-only")
    legacy = ProgressPhoto(
        date=PHOTO_DATE,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        file_key="uploads/synthetic-delete-owner-only-legacy.png",
    )
    db_session.add(legacy)
    foreign_user = User(
        username="photo-delete-non-owner",
        normalized_username="photo-delete-non-owner",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(foreign_user)
    await db_session.commit()

    forged = WriteIdentity(owner.subject_id, foreign_user.id)
    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="owner",
    ):
        await weight_service.delete_progress_photo(
            db_session,
            exact.id,
            identity=forged,
            prepared_conflict_write=await _prepared(db_session, forged),
        )

    system = WriteIdentity(owner.subject_id, None)
    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="owner",
    ):
        await weight_service.delete_progress_photo(
            db_session,
            exact.id,
            identity=system,
            prepared_conflict_write=await _prepared(db_session, system),
        )
    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="owner",
    ):
        await weight_service.delete_progress_photo(
            db_session,
            legacy.id,
            identity=system,
            include_legacy_unowned=True,
            prepared_conflict_write=await _prepared(
                db_session,
                system,
                legacy=True,
            ),
        )

    assert await db_session.get(ProgressPhoto, exact.id) is exact
    assert await db_session.get(ProgressPhoto, legacy.id) is legacy
    persisted_asset = await db_session.get(FileAsset, asset.id)
    assert persisted_asset is asset
    assert persisted_asset.status == FileAssetStatus.LEGACY_PLACEHOLDER.value


async def test_delete_returns_frozen_receipt_and_retires_asset_atomically(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    asset, photo = await _owned_photo(db_session, identity, "delete-atomic")
    asset_id, photo_id, file_key = asset.id, photo.id, photo.file_key
    await db_session.commit()

    receipt = await weight_service.delete_progress_photo(
        db_session,
        photo_id,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    )

    assert receipt == weight_service.ProgressPhotoDeletion(file_key, asset_id)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        receipt.file_key = "uploads/mutated.png"  # type: ignore[misc]
    assert await db_session.get(ProgressPhoto, photo_id) is None
    retired = await db_session.get(FileAsset, asset_id)
    assert retired is not None
    assert retired.status == FileAssetStatus.DELETED.value
    assert retired.deleted_at is not None and retired.purged_at is None

    await db_session.rollback()
    restored_photo = await db_session.get(ProgressPhoto, photo_id)
    restored_asset = await db_session.get(FileAsset, asset_id)
    assert restored_photo is not None
    assert restored_asset is not None
    assert restored_asset.status == FileAssetStatus.LEGACY_PLACEHOLDER.value
    assert restored_asset.deleted_at is None and restored_asset.purged_at is None


async def test_delete_failure_rolls_back_fact_and_file_lifecycle_together(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    identity = _identity(legacy_owner_roots)
    asset, photo = await _owned_photo(db_session, identity, "delete-failure")
    asset_id, photo_id = asset.id, photo.id
    await db_session.commit()
    original_mark = file_asset_service.mark_legacy_local_deleted

    async def fail_after_lifecycle(*args, **kwargs):
        await original_mark(*args, **kwargs)
        raise RuntimeError("synthetic failure after file lifecycle transition")

    monkeypatch.setattr(
        file_asset_service,
        "mark_legacy_local_deleted",
        fail_after_lifecycle,
    )
    with pytest.raises(RuntimeError, match="synthetic failure"):
        await weight_service.delete_progress_photo(
            db_session,
            photo_id,
            identity=identity,
            prepared_conflict_write=await _prepared(db_session, identity),
        )
    await db_session.rollback()

    restored_photo = await db_session.get(ProgressPhoto, photo_id)
    restored_asset = await db_session.get(FileAsset, asset_id)
    assert restored_photo is not None
    assert restored_asset is not None
    assert restored_asset.status == FileAssetStatus.LEGACY_PLACEHOLDER.value
    assert restored_asset.deleted_at is None


async def test_timeline_uses_validated_ranged_marker_projection(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    _, inside = await _owned_photo(
        db_session,
        identity,
        "timeline-inside",
        on_date=PHOTO_DATE,
        note="synthetic checkpoint",
    )
    await _owned_photo(
        db_session,
        identity,
        "timeline-outside",
        on_date=OTHER_DATE,
    )
    await db_session.commit()

    events = await timeline_service.list_events(
        db_session,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
        start=PHOTO_DATE,
        end=PHOTO_DATE,
    )
    markers = [event for event in events if event.kind == "photo"]
    assert [(event.ref, event.detail) for event in markers] == [
        (f"progress_photo:{inside.id}", "synthetic checkpoint")
    ]
    assert "image" not in markers[0].to_dict()
    assert inside.file_key not in str(markers[0].to_dict())

    inside.actor_user_id = None
    await db_session.commit()
    with pytest.raises(weight_service.ProgressPhotoOwnershipError):
        await timeline_service.list_events(
            db_session,
            subject_id=identity.subject_id,
            include_legacy_unowned=True,
            start=PHOTO_DATE,
            end=PHOTO_DATE,
        )


async def test_progress_photo_download_requires_auth_and_validated_subject_graph(
    auth_client,
    db_session,
    legacy_owner_roots,
    tmp_path,
    monkeypatch,
):
    from httpx import ASGITransport, AsyncClient

    from web import main as web_main

    identity = _identity(legacy_owner_roots)
    route_key = "synthetic-protected.png"
    file_key = f"uploads/{route_key}"
    contents = b"synthetic progress-photo bytes"
    path = tmp_path / route_key
    path.write_bytes(contents)
    monkeypatch.setattr(web_main, "UPLOADS_DIR", str(tmp_path))

    async with AsyncClient(
        transport=ASGITransport(app=web_main.app),
        base_url="http://test",
        follow_redirects=False,
    ) as anonymous_client:
        unauthenticated = await anonymous_client.get(
            f"/static/uploads/{route_key}"
        )
    assert unauthenticated.status_code == 401
    assert contents not in unauthenticated.content

    unregistered = await auth_client.get(f"/static/uploads/{route_key}")
    assert unregistered.status_code == 404
    assert contents not in unregistered.content

    asset = await file_asset_service.register_legacy_local(
        db_session,
        subject_id=identity.subject_id,
        uploaded_by_user_id=identity.actor_user_id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref=file_key,
        media_type="image/png",
        size_bytes=len(contents),
        content_sha256="b" * 64,
    )
    photo = await weight_service.add_progress_photo(
        db_session,
        on_date=PHOTO_DATE,
        identity=identity,
        file_asset_id=asset.id,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    await db_session.commit()

    authorized = await auth_client.get(f"/static/uploads/{route_key}")
    assert authorized.status_code == 200
    assert authorized.content == contents
    assert "no-store" in authorized.headers["cache-control"]

    photo.actor_user_id = None
    await db_session.commit()
    invalid_graph = await auth_client.get(f"/static/uploads/{route_key}")
    assert invalid_graph.status_code == 404
    assert contents not in invalid_graph.content


@pytest.mark.parametrize("route_prefix", ["labs", "body"])
@pytest.mark.parametrize(
    "graph_state",
    ["valid", "wrong_purpose", "deleted", "purged", "alias_collision"],
)
async def test_prefixed_progress_photo_download_uses_photo_graph_authorization(
    auth_client,
    db_session,
    legacy_owner_roots,
    tmp_path,
    monkeypatch,
    route_prefix,
    graph_state,
):
    from web import main as web_main

    identity = _identity(legacy_owner_roots)
    route_key = f"{route_prefix}/synthetic-photo-{graph_state}.png"
    file_key = f"uploads/{route_key}"
    contents = b"synthetic prefixed progress photo"
    path = tmp_path / route_key
    path.parent.mkdir(parents=True)
    path.write_bytes(contents)
    monkeypatch.setattr(web_main, "UPLOADS_DIR", str(tmp_path))

    asset = await file_asset_service.register_legacy_local(
        db_session,
        subject_id=identity.subject_id,
        uploaded_by_user_id=identity.actor_user_id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref=file_key,
        media_type="image/png",
        size_bytes=len(contents),
        content_sha256="d" * 64,
    )
    photo = await weight_service.add_progress_photo(
        db_session,
        on_date=PHOTO_DATE,
        identity=identity,
        file_asset_id=asset.id,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    alias_asset = None
    if graph_state == "wrong_purpose":
        asset.purpose = FileAssetPurpose.LAB_DOCUMENT.value
    elif graph_state in {"deleted", "purged"}:
        asset.status = (
            FileAssetStatus.PURGED.value
            if graph_state == "purged"
            else FileAssetStatus.DELETED.value
        )
        asset.deleted_at = now_local()
        if graph_state == "purged":
            asset.purged_at = asset.deleted_at
    elif graph_state == "alias_collision":
        alias_asset = await file_asset_service.register_legacy_local(
            db_session,
            subject_id=identity.subject_id,
            uploaded_by_user_id=identity.actor_user_id,
            purpose=(
                FileAssetPurpose.LAB_DOCUMENT
                if route_prefix == "labs"
                else FileAssetPurpose.BODY_SCAN_DOCUMENT
            ),
            storage_ref=route_key,
            media_type="image/png",
            size_bytes=len(contents),
            content_sha256="e" * 64,
        )
    await db_session.commit()

    response = await auth_client.get(f"/static/uploads/{route_key}")
    if graph_state == "valid":
        assert response.status_code == 200
        assert response.content == contents
        assert "no-store" in response.headers["cache-control"]
    else:
        assert response.status_code == 404
        assert contents not in response.content
    if graph_state == "alias_collision":
        assert alias_asset is not None
        with pytest.raises(
            weight_service.ProgressPhotoOwnershipError,
            match="aliases document file metadata",
        ):
            await weight_service.delete_progress_photo(
                db_session,
                photo.id,
                identity=identity,
                prepared_conflict_write=await _prepared(db_session, identity),
            )
        assert path.read_bytes() == contents
        assert await db_session.get(ProgressPhoto, photo.id) is not None
        assert await db_session.get(FileAsset, alias_asset.id) is not None


@pytest.mark.parametrize("route_prefix", ["labs", "body"])
async def test_legacy_prefixed_photo_create_and_delete_reject_document_disk_alias(
    db_session,
    legacy_owner_roots,
    route_prefix,
):
    identity = _identity(legacy_owner_roots)
    route_key = f"{route_prefix}/synthetic-legacy-delete-alias.png"
    file_key = f"uploads/{route_key}"
    asset = await file_asset_service.register_legacy_local(
        db_session,
        subject_id=identity.subject_id,
        uploaded_by_user_id=identity.actor_user_id,
        purpose=(
            FileAssetPurpose.LAB_DOCUMENT
            if route_prefix == "labs"
            else FileAssetPurpose.BODY_SCAN_DOCUMENT
        ),
        storage_ref=route_key,
        media_type="image/png",
        size_bytes=1,
        content_sha256="f" * 64,
    )
    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="conflicts with file-asset metadata",
    ):
        await weight_service.add_progress_photo(
            db_session,
            on_date=PHOTO_DATE,
            file_key=file_key,
        )

    # Simulate a pre-hardening row so both compatibility delete paths are also
    # proven incapable of removing the shared document bytes.
    photo = ProgressPhoto(
        date=PHOTO_DATE,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        file_key=file_key,
    )
    db_session.add(photo)
    await db_session.commit()

    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="conflicts with file-asset metadata",
    ):
        await weight_service.delete_progress_photo(db_session, photo.id)
    with pytest.raises(
        weight_service.ProgressPhotoOwnershipError,
        match="aliases document file metadata",
    ):
        await weight_service.delete_progress_photo(
            db_session,
            photo.id,
            identity=identity,
            include_legacy_unowned=True,
            prepared_conflict_write=await _prepared(
                db_session,
                identity,
                legacy=True,
            ),
        )

    assert await db_session.get(ProgressPhoto, photo.id) is not None
    assert await db_session.get(FileAsset, asset.id) is not None


@pytest.mark.integration
async def test_postgres_concurrent_same_file_creates_leave_one_fact(
    db_session,
    legacy_owner_roots,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    identity = _identity(legacy_owner_roots)
    asset = await _asset(db_session, identity, "concurrent-same-file")
    asset_id = asset.id
    await db_session.commit()

    session_a = factory()
    prepared_a = await _prepared(session_a, identity)
    await weight_service.add_progress_photo(
        session_a,
        on_date=PHOTO_DATE,
        identity=identity,
        file_asset_id=asset_id,
        prepared_conflict_write=prepared_a,
    )

    async def writer_b() -> str:
        async with factory() as session_b:
            try:
                prepared_b = await _prepared(session_b, identity)
                await weight_service.add_progress_photo(
                    session_b,
                    on_date=PHOTO_DATE,
                    identity=identity,
                    file_asset_id=asset_id,
                    prepared_conflict_write=prepared_b,
                )
                await session_b.commit()
            except weight_service.ProgressPhotoOwnershipError:
                await session_b.rollback()
                return "rejected"
        return "created"

    task_b = asyncio.create_task(writer_b())
    await asyncio.sleep(0.25)
    assert not task_b.done(), "writer B must wait on prepared subject governance"
    await session_a.commit()
    await session_a.close()
    assert await asyncio.wait_for(task_b, timeout=5) == "rejected"

    async with factory() as verify:
        rows = list(
            await verify.scalars(
                select(ProgressPhoto).where(ProgressPhoto.file_asset_id == asset_id)
            )
        )
        persisted_asset = await verify.get(FileAsset, asset_id)
    assert len(rows) == 1
    assert persisted_asset is not None
    assert persisted_asset.status == FileAssetStatus.LEGACY_PLACEHOLDER.value
