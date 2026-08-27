"""Flush-only metadata lifecycle for subject-owned private files.

This service registers both legacy-local placeholders and fully described
private-local assets. It never reads file bytes, resolves configuration,
computes hashes, or performs network I/O. The caller owns the surrounding
transaction and every physical-file operation.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import FileAssetPurpose, FileAssetStatus, FileStorageBackend
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import FileAsset
from vitals.services.files.contracts import (
    FileAssetConflictError,
    FileAssetNotFoundError,
    FileAssetServiceError,
    FileAssetSubjectNotFoundError,
    FileAssetUploaderNotFoundError,
    FileAssetValidationError,
    coerce_purpose,
    local_asset_is_live,
    validate_media_type,
    validate_sha256,
    validate_size,
    validate_storage_ref,
    validate_uuid,
)


async def _validate_references(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    uploaded_by_user_id: uuid.UUID | None,
) -> None:
    persisted_subject_id = await session.scalar(
        select(HealthSubject.id).where(HealthSubject.id == subject_id)
    )
    if persisted_subject_id is None:
        raise FileAssetSubjectNotFoundError("health subject does not exist")

    if uploaded_by_user_id is not None:
        persisted_uploader_id = await session.scalar(
            select(User.id).where(User.id == uploaded_by_user_id)
        )
        if persisted_uploader_id is None:
            raise FileAssetUploaderNotFoundError("uploader identity does not exist")


async def _find_existing_legacy_local(
    session: AsyncSession,
    storage_ref: str,
) -> FileAsset | None:
    return await session.scalar(
        select(FileAsset)
        .where(
            FileAsset.storage_backend == FileStorageBackend.LEGACY_LOCAL.value,
            FileAsset.storage_ref == storage_ref,
        )
        .with_for_update()
    )


async def _find_existing_private_local(
    session: AsyncSession,
    storage_ref: str,
) -> FileAsset | None:
    return await session.scalar(
        select(FileAsset)
        .where(
            FileAsset.storage_backend == FileStorageBackend.PRIVATE_LOCAL.value,
            FileAsset.storage_ref == storage_ref,
        )
        .with_for_update()
    )


async def _reconcile_existing(
    session: AsyncSession,
    asset: FileAsset,
    *,
    subject_id: uuid.UUID,
    uploaded_by_user_id: uuid.UUID | None,
    purpose: FileAssetPurpose,
    media_type: str | None,
    size_bytes: int | None,
    content_sha256: str | None,
) -> FileAsset:
    if asset.subject_id != subject_id or asset.purpose != purpose.value:
        raise FileAssetConflictError("storage_ref belongs to another owner or purpose")
    if asset.status in {
        FileAssetStatus.DELETED.value,
        FileAssetStatus.PURGED.value,
    }:
        raise FileAssetConflictError("storage_ref belongs to a retired file asset")
    if (
        uploaded_by_user_id is not None
        and asset.uploaded_by_user_id is not None
        and asset.uploaded_by_user_id != uploaded_by_user_id
    ):
        raise FileAssetConflictError("storage_ref has a different uploader history")

    proposed = {
        "media_type": media_type,
        "byte_size": size_bytes,
        "sha256_hex": content_sha256,
    }
    for field_name, value in proposed.items():
        persisted = getattr(asset, field_name)
        if value is not None and persisted is not None and persisted != value:
            raise FileAssetConflictError(
                f"storage_ref has conflicting {field_name} metadata"
            )

    changed = False
    for field_name, value in proposed.items():
        if value is not None and getattr(asset, field_name) is None:
            setattr(asset, field_name, value)
            changed = True
    if changed:
        await session.flush()
    return asset


async def register_legacy_local(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    uploaded_by_user_id: uuid.UUID | None,
    purpose: FileAssetPurpose | str,
    storage_ref: str,
    media_type: str | None = None,
    size_bytes: int | None = None,
    content_sha256: str | None = None,
) -> FileAsset:
    """Register one existing legacy-local file without touching its bytes.

    The natural key is ``(legacy_local, storage_ref)``.  Re-registration for the
    same subject and purpose returns the original row, may enrich only null
    content metadata, and never rewrites uploader history.  The function flushes
    but never commits.
    """

    validate_uuid(subject_id, "subject_id")
    validate_uuid(uploaded_by_user_id, "uploaded_by_user_id", nullable=True)
    normalized_purpose = coerce_purpose(purpose)
    normalized_storage_ref = validate_storage_ref(storage_ref, normalized_purpose)
    normalized_media_type = validate_media_type(media_type)
    normalized_size = validate_size(size_bytes)
    normalized_sha256 = validate_sha256(content_sha256)

    await _validate_references(
        session,
        subject_id=subject_id,
        uploaded_by_user_id=uploaded_by_user_id,
    )

    existing = await _find_existing_legacy_local(session, normalized_storage_ref)
    if existing is not None:
        return await _reconcile_existing(
            session,
            existing,
            subject_id=subject_id,
            uploaded_by_user_id=uploaded_by_user_id,
            purpose=normalized_purpose,
            media_type=normalized_media_type,
            size_bytes=normalized_size,
            content_sha256=normalized_sha256,
        )

    asset = FileAsset(
        subject_id=subject_id,
        uploaded_by_user_id=uploaded_by_user_id,
        opaque_key=uuid.uuid4(),
        purpose=normalized_purpose.value,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        storage_ref=normalized_storage_ref,
        media_type=normalized_media_type,
        byte_size=normalized_size,
        sha256_hex=normalized_sha256,
        status=FileAssetStatus.LEGACY_PLACEHOLDER.value,
    )
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite does not necessarily emit a real outer BEGIN for the preceding
        # reads.  Releasing its first SAVEPOINT can therefore persist the row and
        # violate this service's flush-only contract.  Production PostgreSQL uses
        # the recoverable race path below; the compatibility path flushes in the
        # caller's ordinary transaction.
        session.add(asset)
        await session.flush()
        return asset

    try:
        async with session.begin_nested():
            session.add(asset)
            await session.flush()
    except IntegrityError as exc:
        # A concurrent transaction may have won the natural-key insert.  The
        # SAVEPOINT keeps the caller's transaction usable while we re-read the
        # now-authoritative row and apply the same fail-closed reconciliation.
        existing = await _find_existing_legacy_local(session, normalized_storage_ref)
        if existing is None:
            raise FileAssetConflictError(
                "legacy-local asset could not be registered"
            ) from exc
        return await _reconcile_existing(
            session,
            existing,
            subject_id=subject_id,
            uploaded_by_user_id=uploaded_by_user_id,
            purpose=normalized_purpose,
            media_type=normalized_media_type,
            size_bytes=normalized_size,
            content_sha256=normalized_sha256,
        )
    return asset


async def register_private_local(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    uploaded_by_user_id: uuid.UUID,
    purpose: FileAssetPurpose | str,
    storage_ref: str,
    media_type: str,
    size_bytes: int,
    content_sha256: str,
) -> FileAsset:
    """Register bytes already written below the configured private root.

    Active private-local rows require complete integrity metadata. The function
    only records that metadata and flushes; the delivery boundary owns the disk
    write, transaction commit, and cleanup if either fails.
    """

    validate_uuid(subject_id, "subject_id")
    validate_uuid(uploaded_by_user_id, "uploaded_by_user_id")
    normalized_purpose = coerce_purpose(purpose)
    normalized_storage_ref = validate_storage_ref(storage_ref, normalized_purpose)
    normalized_media_type = validate_media_type(media_type)
    normalized_size = validate_size(size_bytes)
    normalized_sha256 = validate_sha256(content_sha256)
    if normalized_media_type is None:
        raise FileAssetValidationError("media_type is required for an active file")
    if normalized_size is None:
        raise FileAssetValidationError("size_bytes is required for an active file")
    if normalized_sha256 is None:
        raise FileAssetValidationError(
            "content_sha256 is required for an active file"
        )

    await _validate_references(
        session,
        subject_id=subject_id,
        uploaded_by_user_id=uploaded_by_user_id,
    )
    existing = await _find_existing_private_local(session, normalized_storage_ref)
    if existing is not None:
        reconciled = await _reconcile_existing(
            session,
            existing,
            subject_id=subject_id,
            uploaded_by_user_id=uploaded_by_user_id,
            purpose=normalized_purpose,
            media_type=normalized_media_type,
            size_bytes=normalized_size,
            content_sha256=normalized_sha256,
        )
        if not local_asset_is_live(reconciled):
            raise FileAssetConflictError(
                "private storage_ref has an invalid lifecycle state"
            )
        return reconciled
    asset = FileAsset(
        subject_id=subject_id,
        uploaded_by_user_id=uploaded_by_user_id,
        opaque_key=uuid.uuid4(),
        purpose=normalized_purpose.value,
        storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
        storage_ref=normalized_storage_ref,
        media_type=normalized_media_type,
        byte_size=normalized_size,
        sha256_hex=normalized_sha256,
        status=FileAssetStatus.ACTIVE.value,
    )
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        session.add(asset)
        await session.flush()
        return asset
    try:
        async with session.begin_nested():
            session.add(asset)
            await session.flush()
    except IntegrityError as exc:
        existing = await _find_existing_private_local(
            session, normalized_storage_ref
        )
        if existing is None:
            raise FileAssetConflictError(
                "private-local asset could not be registered"
            ) from exc
        reconciled = await _reconcile_existing(
            session,
            existing,
            subject_id=subject_id,
            uploaded_by_user_id=uploaded_by_user_id,
            purpose=normalized_purpose,
            media_type=normalized_media_type,
            size_bytes=normalized_size,
            content_sha256=normalized_sha256,
        )
        if not local_asset_is_live(reconciled):
            raise FileAssetConflictError(
                "private storage_ref has an invalid lifecycle state"
            )
        return reconciled
    return asset


async def mark_legacy_local_deleted(
    session: AsyncSession,
    *,
    file_asset_id: uuid.UUID,
    subject_id: uuid.UUID,
    purged: bool,
) -> FileAsset:
    """Soft-delete or purge one subject-scoped legacy-local metadata row.

    Purging is monotonic and preserves any earlier deletion timestamp.  Neither
    transition deletes the row or touches the physical file.  The function
    flushes but never commits.
    """

    validate_uuid(file_asset_id, "file_asset_id")
    validate_uuid(subject_id, "subject_id")
    if not isinstance(purged, bool):
        raise FileAssetValidationError("purged must be a bool")

    asset = await session.scalar(
        select(FileAsset)
        .where(
            FileAsset.id == file_asset_id,
            FileAsset.subject_id == subject_id,
            FileAsset.storage_backend == FileStorageBackend.LEGACY_LOCAL.value,
        )
        .with_for_update()
    )
    if asset is None:
        raise FileAssetNotFoundError(
            "legacy-local asset does not exist in subject scope"
        )

    if asset.status == FileAssetStatus.PURGED.value:
        return asset
    if not purged and asset.status == FileAssetStatus.DELETED.value:
        return asset
    if asset.status not in {
        FileAssetStatus.LEGACY_PLACEHOLDER.value,
        FileAssetStatus.PENDING.value,
        FileAssetStatus.DELETED.value,
    }:
        raise FileAssetConflictError(
            "legacy-local asset has an invalid lifecycle state"
        )

    database_now = await session.scalar(select(func.now()))
    if database_now is None:  # pragma: no cover - supported DBs always return now()
        raise FileAssetServiceError("database did not return a lifecycle timestamp")

    if asset.deleted_at is None:
        asset.deleted_at = database_now
    if purged:
        asset.purged_at = database_now
        asset.status = FileAssetStatus.PURGED.value
    else:
        asset.purged_at = None
        asset.status = FileAssetStatus.DELETED.value
    await session.flush()
    return asset


async def mark_local_deleted(
    session: AsyncSession,
    *,
    file_asset_id: uuid.UUID,
    subject_id: uuid.UUID,
    purged: bool,
) -> FileAsset:
    """Retire subject-scoped bytes in either supported local backend.

    This is metadata-only and flush-only.  The delivery boundary commits the
    soft deletion before removing bytes, then records the monotonic purge after
    deletion succeeds.  ``mark_legacy_local_deleted`` remains the deliberately
    narrow compatibility API used by ownership backfills.
    """

    validate_uuid(file_asset_id, "file_asset_id")
    validate_uuid(subject_id, "subject_id")
    if not isinstance(purged, bool):
        raise FileAssetValidationError("purged must be a bool")

    asset = await session.scalar(
        select(FileAsset)
        .where(
            FileAsset.id == file_asset_id,
            FileAsset.subject_id == subject_id,
            FileAsset.storage_backend.in_(
                (
                    FileStorageBackend.LEGACY_LOCAL.value,
                    FileStorageBackend.PRIVATE_LOCAL.value,
                )
            ),
        )
        .with_for_update()
    )
    if asset is None:
        raise FileAssetNotFoundError("local asset does not exist in subject scope")
    if asset.status == FileAssetStatus.PURGED.value:
        return asset
    if not purged and asset.status == FileAssetStatus.DELETED.value:
        return asset
    if asset.status not in {
        FileAssetStatus.LEGACY_PLACEHOLDER.value,
        FileAssetStatus.PENDING.value,
        FileAssetStatus.ACTIVE.value,
        FileAssetStatus.DELETED.value,
    }:
        raise FileAssetConflictError("local asset has an invalid lifecycle state")

    database_now = await session.scalar(select(func.now()))
    if database_now is None:  # pragma: no cover
        raise FileAssetServiceError("database did not return a lifecycle timestamp")
    if asset.deleted_at is None:
        asset.deleted_at = database_now
    if purged:
        asset.purged_at = database_now
        asset.status = FileAssetStatus.PURGED.value
    else:
        asset.purged_at = None
        asset.status = FileAssetStatus.DELETED.value
    await session.flush()
    return asset


__all__ = [
    "mark_local_deleted",
    "mark_legacy_local_deleted",
    "register_private_local",
    "register_legacy_local",
]
