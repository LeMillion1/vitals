"""Focused contracts for legacy-local file metadata registration."""
from __future__ import annotations

import ast
import asyncio
import inspect
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import FileAsset
from vitals.services.files import lifecycle as file_lifecycle
from vitals.services.files import queries as file_queries
from vitals.services.files.contracts import (
    FileAssetConflictError,
    FileAssetNotFoundError,
    FileAssetSubjectNotFoundError,
    FileAssetUploaderNotFoundError,
    FileAssetValidationError,
)
from vitals.services.files.lifecycle import (
    mark_legacy_local_deleted,
    register_legacy_local,
)

_SHA256 = "a" * 64


async def _identity_graph(
    session: AsyncSession,
    slug: str,
) -> tuple[User, HealthSubject]:
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


async def _count_assets(session: AsyncSession) -> int:
    return int(
        await session.scalar(select(func.count()).select_from(FileAsset)) or 0
    )


@pytest.mark.asyncio
async def test_register_creates_exact_legacy_placeholder_metadata(db_session):
    uploader, subject = await _identity_graph(db_session, "file-owner")

    asset = await register_legacy_local(
        db_session,
        subject_id=subject.id,
        uploaded_by_user_id=uploader.id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref="uploads/synthetic-photo.webp",
        media_type="image/webp",
        size_bytes=1234,
        content_sha256=_SHA256,
    )

    assert isinstance(asset.id, uuid.UUID)
    assert isinstance(asset.opaque_key, uuid.UUID)
    assert asset.subject_id == subject.id
    assert asset.uploaded_by_user_id == uploader.id
    assert asset.purpose == FileAssetPurpose.PROGRESS_PHOTO.value
    assert asset.storage_backend == FileStorageBackend.LEGACY_LOCAL.value
    assert asset.storage_ref == "uploads/synthetic-photo.webp"
    assert asset.media_type == "image/webp"
    assert asset.byte_size == 1234
    assert asset.sha256_hex == _SHA256
    assert asset.status == FileAssetStatus.LEGACY_PLACEHOLDER.value
    assert asset.deleted_at is None
    assert asset.purged_at is None
    assert await db_session.get(FileAsset, asset.id) is asset


@pytest.mark.parametrize(
    ("purpose", "storage_ref"),
    [
        (FileAssetPurpose.PROGRESS_PHOTO, "uploads/photo.jpg"),
        (FileAssetPurpose.LAB_DOCUMENT, "labs/report.pdf"),
        (FileAssetPurpose.BODY_SCAN_DOCUMENT, "body/scan.pdf"),
    ],
)
@pytest.mark.asyncio
async def test_register_accepts_only_current_purpose_prefixes(
    db_session,
    purpose,
    storage_ref,
):
    _owner, subject = await _identity_graph(
        db_session,
        f"prefix-{purpose.value}",
    )

    asset = await register_legacy_local(
        db_session,
        subject_id=subject.id,
        uploaded_by_user_id=None,
        purpose=purpose.value,
        storage_ref=storage_ref,
    )

    assert asset.purpose == purpose.value
    assert asset.storage_ref == storage_ref


@pytest.mark.parametrize(
    ("purpose", "storage_ref"),
    [
        (FileAssetPurpose.PROGRESS_PHOTO, "labs/photo.jpg"),
        (FileAssetPurpose.LAB_DOCUMENT, "body/report.pdf"),
        (FileAssetPurpose.BODY_SCAN_DOCUMENT, "uploads/scan.pdf"),
    ],
)
@pytest.mark.asyncio
async def test_register_rejects_purpose_prefix_mismatch(
    db_session,
    purpose,
    storage_ref,
):
    _owner, subject = await _identity_graph(
        db_session,
        f"mismatch-{purpose.value}",
    )

    with pytest.raises(FileAssetValidationError, match="prefix"):
        await register_legacy_local(
            db_session,
            subject_id=subject.id,
            uploaded_by_user_id=None,
            purpose=purpose,
            storage_ref=storage_ref,
        )


@pytest.mark.parametrize(
    "storage_ref",
    [
        "",
        "/uploads/photo.jpg",
        "uploads//photo.jpg",
        "uploads/./photo.jpg",
        "uploads/../photo.jpg",
        "uploads/photo..jpg",
        "uploads/photo.jpg/",
        "uploads\\photo.jpg",
        "uploads/\x00photo.jpg",
        " uploads/photo.jpg",
        "uploads/photo.jpg ",
        "uploads/photo\n.jpg",
        ".",
        "..",
    ],
)
@pytest.mark.asyncio
async def test_register_rejects_unsafe_or_noncanonical_storage_refs(
    db_session,
    storage_ref,
):
    _owner, subject = await _identity_graph(db_session, "unsafe-path")

    with pytest.raises(FileAssetValidationError):
        await register_legacy_local(
            db_session,
            subject_id=subject.id,
            uploaded_by_user_id=None,
            purpose=FileAssetPurpose.PROGRESS_PHOTO,
            storage_ref=storage_ref,
        )

    assert await _count_assets(db_session) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"purpose": "unknown"},
        {"purpose": object()},
        {"storage_ref": 7},
        {"storage_ref": "uploads/" + "x" * 505},
        {"media_type": ""},
        {"media_type": " image/jpeg"},
        {"media_type": "image/\x00jpeg"},
        {"media_type": "x" * 256},
        {"size_bytes": -1},
        {"size_bytes": True},
        {"size_bytes": 2**63},
        {"content_sha256": "A" * 64},
        {"content_sha256": "g" * 64},
        {"content_sha256": "a" * 63},
    ],
)
@pytest.mark.asyncio
async def test_register_rejects_invalid_metadata_before_write(db_session, changes):
    _owner, subject = await _identity_graph(db_session, "invalid-metadata")
    values = {
        "subject_id": subject.id,
        "uploaded_by_user_id": None,
        "purpose": FileAssetPurpose.PROGRESS_PHOTO,
        "storage_ref": "uploads/valid.jpg",
        "media_type": None,
        "size_bytes": None,
        "content_sha256": None,
    }
    values.update(changes)

    with pytest.raises(FileAssetValidationError):
        await register_legacy_local(db_session, **values)

    assert await _count_assets(db_session) == 0


@pytest.mark.asyncio
async def test_register_validates_subject_and_optional_uploader(db_session):
    _owner, subject = await _identity_graph(db_session, "reference-owner")

    with pytest.raises(FileAssetSubjectNotFoundError):
        await register_legacy_local(
            db_session,
            subject_id=uuid.uuid4(),
            uploaded_by_user_id=None,
            purpose=FileAssetPurpose.PROGRESS_PHOTO,
            storage_ref="uploads/missing-subject.jpg",
        )
    with pytest.raises(FileAssetUploaderNotFoundError):
        await register_legacy_local(
            db_session,
            subject_id=subject.id,
            uploaded_by_user_id=uuid.uuid4(),
            purpose=FileAssetPurpose.PROGRESS_PHOTO,
            storage_ref="uploads/missing-uploader.jpg",
        )

    assert await _count_assets(db_session) == 0


@pytest.mark.asyncio
async def test_register_is_idempotent_and_only_fills_null_content_metadata(db_session):
    _owner, subject = await _identity_graph(db_session, "idempotent-owner")
    later_uploader, _later_subject = await _identity_graph(
        db_session,
        "later-uploader",
    )

    first = await register_legacy_local(
        db_session,
        subject_id=subject.id,
        uploaded_by_user_id=None,
        purpose=FileAssetPurpose.LAB_DOCUMENT,
        storage_ref="labs/idempotent.pdf",
    )
    original_id = first.id
    original_opaque_key = first.opaque_key
    second = await register_legacy_local(
        db_session,
        subject_id=subject.id,
        uploaded_by_user_id=later_uploader.id,
        purpose=FileAssetPurpose.LAB_DOCUMENT,
        storage_ref="labs/idempotent.pdf",
        media_type="application/pdf",
        size_bytes=321,
        content_sha256=_SHA256,
    )
    third = await register_legacy_local(
        db_session,
        subject_id=subject.id,
        uploaded_by_user_id=None,
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        storage_ref="labs/idempotent.pdf",
        media_type="application/pdf",
        size_bytes=321,
        content_sha256=_SHA256,
    )

    assert first is second is third
    assert third.id == original_id
    assert third.opaque_key == original_opaque_key
    assert third.uploaded_by_user_id is None
    assert third.media_type == "application/pdf"
    assert third.byte_size == 321
    assert third.sha256_hex == _SHA256
    assert await _count_assets(db_session) == 1


@pytest.mark.asyncio
async def test_register_rejects_uploader_conflict_without_rewriting_history(db_session):
    first_uploader, subject = await _identity_graph(db_session, "first-uploader")
    second_uploader, _other_subject = await _identity_graph(
        db_session,
        "second-uploader",
    )
    asset = await register_legacy_local(
        db_session,
        subject_id=subject.id,
        uploaded_by_user_id=first_uploader.id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref="uploads/uploader-history.jpg",
    )

    with pytest.raises(FileAssetConflictError, match="uploader"):
        await register_legacy_local(
            db_session,
            subject_id=subject.id,
            uploaded_by_user_id=second_uploader.id,
            purpose=FileAssetPurpose.PROGRESS_PHOTO,
            storage_ref="uploads/uploader-history.jpg",
        )

    assert asset.uploaded_by_user_id == first_uploader.id


@pytest.mark.asyncio
async def test_register_rejects_metadata_conflict_without_partial_enrichment(
    db_session,
):
    _owner, subject = await _identity_graph(db_session, "metadata-conflict")
    asset = await register_legacy_local(
        db_session,
        subject_id=subject.id,
        uploaded_by_user_id=None,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref="uploads/conflicting.jpg",
        size_bytes=10,
    )

    with pytest.raises(FileAssetConflictError, match="byte_size"):
        await register_legacy_local(
            db_session,
            subject_id=subject.id,
            uploaded_by_user_id=None,
            purpose=FileAssetPurpose.PROGRESS_PHOTO,
            storage_ref="uploads/conflicting.jpg",
            media_type="image/jpeg",
            size_bytes=11,
            content_sha256=_SHA256,
        )

    assert asset.media_type is None
    assert asset.byte_size == 10
    assert asset.sha256_hex is None


@pytest.mark.asyncio
async def test_register_rejects_cross_subject_and_cross_purpose_key_reuse(db_session):
    _first_owner, first_subject = await _identity_graph(db_session, "asset-first")
    _second_owner, second_subject = await _identity_graph(db_session, "asset-second")
    await register_legacy_local(
        db_session,
        subject_id=first_subject.id,
        uploaded_by_user_id=None,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref="uploads/owned.jpg",
    )

    with pytest.raises(FileAssetConflictError, match="owner or purpose"):
        await register_legacy_local(
            db_session,
            subject_id=second_subject.id,
            uploaded_by_user_id=None,
            purpose=FileAssetPurpose.PROGRESS_PHOTO,
            storage_ref="uploads/owned.jpg",
        )

    inconsistent_purpose = FileAsset(
        subject_id=first_subject.id,
        uploaded_by_user_id=None,
        opaque_key=uuid.uuid4(),
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        storage_ref="uploads/purpose-conflict.jpg",
        status=FileAssetStatus.LEGACY_PLACEHOLDER.value,
    )
    db_session.add(inconsistent_purpose)
    await db_session.flush()
    with pytest.raises(FileAssetConflictError, match="owner or purpose"):
        await register_legacy_local(
            db_session,
            subject_id=first_subject.id,
            uploaded_by_user_id=None,
            purpose=FileAssetPurpose.PROGRESS_PHOTO,
            storage_ref="uploads/purpose-conflict.jpg",
        )

    assert await _count_assets(db_session) == 2


@pytest.mark.asyncio
async def test_register_is_flush_only(db_session):
    _owner, subject = await _identity_graph(db_session, "flush-only")
    subject_id = subject.id
    await db_session.commit()

    await register_legacy_local(
        db_session,
        subject_id=subject_id,
        uploaded_by_user_id=None,
        purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT,
        storage_ref="body/rollback.pdf",
    )
    await db_session.rollback()

    assert await _count_assets(db_session) == 0
    assert await db_session.get(HealthSubject, subject_id) is not None


@pytest.mark.asyncio
async def test_lifecycle_is_scoped_idempotent_monotonic_and_never_hard_deletes(
    db_session,
):
    _owner, subject = await _identity_graph(db_session, "lifecycle-owner")
    _other_owner, other_subject = await _identity_graph(db_session, "lifecycle-other")
    asset = await register_legacy_local(
        db_session,
        subject_id=subject.id,
        uploaded_by_user_id=None,
        purpose=FileAssetPurpose.LAB_DOCUMENT,
        storage_ref="labs/lifecycle.pdf",
    )

    with pytest.raises(FileAssetNotFoundError):
        await mark_legacy_local_deleted(
            db_session,
            file_asset_id=asset.id,
            subject_id=other_subject.id,
            purged=False,
        )

    deleted = await mark_legacy_local_deleted(
        db_session,
        file_asset_id=asset.id,
        subject_id=subject.id,
        purged=False,
    )
    deleted_at = deleted.deleted_at
    assert deleted.status == FileAssetStatus.DELETED.value
    assert deleted_at is not None
    assert deleted.purged_at is None

    deleted_again = await mark_legacy_local_deleted(
        db_session,
        file_asset_id=asset.id,
        subject_id=subject.id,
        purged=False,
    )
    assert deleted_again.deleted_at == deleted_at
    assert deleted_again.purged_at is None

    purged = await mark_legacy_local_deleted(
        db_session,
        file_asset_id=asset.id,
        subject_id=subject.id,
        purged=True,
    )
    purged_at = purged.purged_at
    assert purged.status == FileAssetStatus.PURGED.value
    assert purged.deleted_at == deleted_at
    assert purged_at is not None
    assert purged_at >= deleted_at

    purged_again = await mark_legacy_local_deleted(
        db_session,
        file_asset_id=asset.id,
        subject_id=subject.id,
        purged=True,
    )
    delete_after_purge = await mark_legacy_local_deleted(
        db_session,
        file_asset_id=asset.id,
        subject_id=subject.id,
        purged=False,
    )
    assert purged_again.purged_at == purged_at
    assert delete_after_purge.status == FileAssetStatus.PURGED.value
    assert delete_after_purge.deleted_at == deleted_at
    assert delete_after_purge.purged_at == purged_at
    assert await db_session.get(FileAsset, asset.id) is asset
    assert await _count_assets(db_session) == 1


@pytest.mark.asyncio
async def test_direct_purge_sets_both_lifecycle_timestamps(db_session):
    _owner, subject = await _identity_graph(db_session, "direct-purge")
    asset = await register_legacy_local(
        db_session,
        subject_id=subject.id,
        uploaded_by_user_id=None,
        purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT,
        storage_ref="body/direct-purge.pdf",
    )

    result = await mark_legacy_local_deleted(
        db_session,
        file_asset_id=asset.id,
        subject_id=subject.id,
        purged=True,
    )

    assert result.status == FileAssetStatus.PURGED.value
    assert result.deleted_at is not None
    assert result.purged_at == result.deleted_at


@pytest.mark.parametrize("purged", [False, True])
@pytest.mark.asyncio
async def test_register_never_reuses_or_resurrects_retired_asset(
    db_session,
    purged,
):
    _owner, subject = await _identity_graph(db_session, f"no-resurrection-{purged}")
    asset = await register_legacy_local(
        db_session,
        subject_id=subject.id,
        uploaded_by_user_id=None,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref=f"uploads/retired-{purged}.jpg",
    )
    await mark_legacy_local_deleted(
        db_session,
        file_asset_id=asset.id,
        subject_id=subject.id,
        purged=purged,
    )
    before = (asset.status, asset.deleted_at, asset.purged_at, asset.media_type)

    with pytest.raises(FileAssetConflictError, match="retired"):
        await register_legacy_local(
            db_session,
            subject_id=subject.id,
            uploaded_by_user_id=None,
            purpose=FileAssetPurpose.PROGRESS_PHOTO,
            storage_ref=f"uploads/retired-{purged}.jpg",
            media_type="image/jpeg",
        )

    assert (asset.status, asset.deleted_at, asset.purged_at, asset.media_type) == before
    assert await _count_assets(db_session) == 1


@pytest.mark.asyncio
async def test_service_does_not_read_files_hash_config_env_or_network(
    db_session,
    monkeypatch,
):
    _owner, subject = await _identity_graph(db_session, "no-io")
    subject_id = subject.id
    await db_session.commit()

    source_tree = ast.parse(inspect.getsource(file_lifecycle))
    forbidden_modules = {"hashlib", "os", "pathlib", "socket", "vitals.config"}
    imported_modules: set[str] = set()
    forbidden_calls: list[str] = []
    for node in ast.walk(source_tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                call_name = node.func.attr
            else:
                continue
            if call_name in {"getenv", "load_config", "open", "sha256", "stat"}:
                forbidden_calls.append(call_name)

    assert forbidden_modules.isdisjoint(imported_modules)
    assert forbidden_calls == []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("file asset metadata service attempted external I/O")

    # A module-local sentinel catches future unqualified calls without replacing
    # process-global helpers that SQLAlchemy, asyncpg, or pytest legitimately use.
    for name in ("getenv", "load_config", "open", "sha256", "stat"):
        monkeypatch.setattr(file_lifecycle, name, forbidden, raising=False)

    asset = await register_legacy_local(
        db_session,
        subject_id=subject_id,
        uploaded_by_user_id=None,
        purpose=FileAssetPurpose.LAB_DOCUMENT,
        storage_ref="labs/no-io.pdf",
        content_sha256=_SHA256,
    )
    await mark_legacy_local_deleted(
        db_session,
        file_asset_id=asset.id,
        subject_id=subject_id,
        purged=True,
    )

    assert asset.status == FileAssetStatus.PURGED.value


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_concurrent_same_key_returns_one_authoritative_asset(
    db_session,
    monkeypatch,
):
    _owner, subject = await _identity_graph(db_session, "concurrent-file")
    subject_id = subject.id
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    original_find = file_lifecycle._find_existing_legacy_local
    first_reads = 0
    first_reads_lock = asyncio.Lock()
    both_read_missing = asyncio.Event()

    async def synchronized_find(session, storage_ref):
        nonlocal first_reads
        row = await original_find(session, storage_ref)
        if row is None:
            async with first_reads_lock:
                if first_reads < 2:
                    first_reads += 1
                    if first_reads == 2:
                        both_read_missing.set()
                    should_wait = True
                else:
                    should_wait = False
            if should_wait:
                await asyncio.wait_for(both_read_missing.wait(), timeout=5)
        return row

    monkeypatch.setattr(
        file_lifecycle,
        "_find_existing_legacy_local",
        synchronized_find,
    )

    async def register_and_commit() -> uuid.UUID:
        async with factory() as session:
            asset = await register_legacy_local(
                session,
                subject_id=subject_id,
                uploaded_by_user_id=None,
                purpose=FileAssetPurpose.PROGRESS_PHOTO,
                storage_ref="uploads/concurrent.jpg",
                media_type="image/jpeg",
                size_bytes=17,
                content_sha256=_SHA256,
            )
            asset_id = asset.id
            await session.commit()
            return asset_id

    first_id, second_id = await asyncio.gather(
        register_and_commit(),
        register_and_commit(),
    )

    assert first_id == second_id
    async with factory() as session:
        assert await _count_assets(session) == 1
        persisted = await session.scalar(select(FileAsset))
        assert persisted is not None
        assert persisted.id == first_id
        assert persisted.subject_id == subject_id
        assert persisted.status == FileAssetStatus.LEGACY_PLACEHOLDER.value


# ── Resolving a download ─────────────────────────────────────────────────────
# The lookup is what stands between a URL somebody is holding and somebody
# else's medical file. Both functions below take the subject as part of the
# query rather than checking it afterwards, and the difference is not stylistic:
# a check afterwards has to decide what to say about a row it just read.


@pytest.mark.asyncio
async def test_a_download_key_only_resolves_inside_its_own_subject(db_session):
    """Another subject's key is not merely refused — it is not found."""

    mine_user, mine = await _identity_graph(db_session, "download-mine")
    theirs_user, theirs = await _identity_graph(db_session, "download-theirs")

    ours = await register_legacy_local(
        db_session,
        subject_id=mine.id,
        uploaded_by_user_id=mine_user.id,
        purpose=FileAssetPurpose.LAB_DOCUMENT,
        storage_ref="labs/mine.pdf",
        media_type="application/pdf",
        size_bytes=10,
        content_sha256=_SHA256,
    )
    hers = await register_legacy_local(
        db_session,
        subject_id=theirs.id,
        uploaded_by_user_id=theirs_user.id,
        purpose=FileAssetPurpose.LAB_DOCUMENT,
        storage_ref="labs/theirs.pdf",
        media_type="application/pdf",
        size_bytes=10,
        content_sha256=_SHA256,
    )

    found = await file_queries.resolve_for_download(
        db_session, opaque_key=ours.opaque_key, subject_id=mine.id
    )
    assert found.id == ours.id

    # The real key of a real file, asked for by the wrong subject.
    with pytest.raises(FileAssetNotFoundError):
        await file_queries.resolve_for_download(
            db_session, opaque_key=hers.opaque_key, subject_id=mine.id
        )
    # And a key that never existed, which must be indistinguishable from it.
    with pytest.raises(FileAssetNotFoundError):
        await file_queries.resolve_for_download(
            db_session, opaque_key=uuid.uuid4(), subject_id=mine.id
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("purged", [False, True])
async def test_a_retired_asset_resolves_to_nothing(db_session, purged):
    """Deleted and purged are lifecycle states, not error messages."""

    uploader, subject = await _identity_graph(db_session, f"retired-{int(purged)}")
    asset = await register_legacy_local(
        db_session,
        subject_id=subject.id,
        uploaded_by_user_id=uploader.id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref="uploads/retired.webp",
        media_type="image/webp",
        size_bytes=10,
        content_sha256=_SHA256,
    )
    await mark_legacy_local_deleted(
        db_session,
        file_asset_id=asset.id,
        subject_id=subject.id,
        purged=purged,
    )

    with pytest.raises(FileAssetNotFoundError):
        await file_queries.resolve_for_download(
            db_session, opaque_key=asset.opaque_key, subject_id=subject.id
        )


@pytest.mark.asyncio
async def test_the_page_lookup_returns_only_this_subjects_live_assets(db_session):
    """One query for a whole page, and it cannot leak into the next subject.

    A page renders many rows, each carrying an asset id it read from its own
    scoped table. Passing an id that turns out to belong elsewhere should
    produce no URL rather than one that works.
    """

    mine_user, mine = await _identity_graph(db_session, "page-mine")
    theirs_user, theirs = await _identity_graph(db_session, "page-theirs")

    live = await register_legacy_local(
        db_session,
        subject_id=mine.id,
        uploaded_by_user_id=mine_user.id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref="uploads/live.webp",
        media_type="image/webp",
        size_bytes=10,
        content_sha256=_SHA256,
    )
    retired = await register_legacy_local(
        db_session,
        subject_id=mine.id,
        uploaded_by_user_id=mine_user.id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref="uploads/retired-page.webp",
        media_type="image/webp",
        size_bytes=10,
        content_sha256=_SHA256,
    )
    await mark_legacy_local_deleted(
        db_session, file_asset_id=retired.id, subject_id=mine.id, purged=False
    )
    hers = await register_legacy_local(
        db_session,
        subject_id=theirs.id,
        uploaded_by_user_id=theirs_user.id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref="uploads/hers.webp",
        media_type="image/webp",
        size_bytes=10,
        content_sha256=_SHA256,
    )

    resolved = await file_queries.opaque_keys_for(
        db_session,
        subject_id=mine.id,
        file_asset_ids=[live.id, retired.id, hers.id, None, uuid.uuid4()],
    )
    assert resolved == {live.id: live.opaque_key}

    assert await file_queries.opaque_keys_for(
        db_session, subject_id=mine.id, file_asset_ids=[]
    ) == {}
    assert await file_queries.opaque_keys_for(
        db_session, subject_id=mine.id, file_asset_ids=[None, None]
    ) == {}


@pytest.mark.asyncio
async def test_the_download_lookup_refuses_a_value_that_is_not_a_uuid(db_session):
    """A malformed key is a validation failure, not a query with a cast error."""

    _, subject = await _identity_graph(db_session, "download-malformed")
    for value in ("not-a-uuid", 7, None, ""):
        with pytest.raises(FileAssetValidationError):
            await file_queries.resolve_for_download(
                db_session, opaque_key=value, subject_id=subject.id
            )
