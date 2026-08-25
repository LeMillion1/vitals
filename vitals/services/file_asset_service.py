"""Flush-only metadata lifecycle for subject-owned private files.

This service registers both legacy-local placeholders and fully described
private-local assets. It never reads file bytes, resolves configuration,
computes hashes, or performs network I/O. The caller owns the surrounding
transaction and every physical-file operation.
"""
from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import FileAssetPurpose, FileAssetStatus, FileStorageBackend
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import FileAsset

_PURPOSE_PREFIXES = {
    FileAssetPurpose.PROGRESS_PHOTO: "uploads/",
    FileAssetPurpose.LAB_DOCUMENT: "labs/",
    FileAssetPurpose.BODY_SCAN_DOCUMENT: "body/",
    FileAssetPurpose.CARE_MESSAGE_ATTACHMENT: "care/",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_STORAGE_REF_LENGTH = 512
_MAX_MEDIA_TYPE_LENGTH = 255
_MAX_BIGINT = 2**63 - 1


class FileAssetServiceError(Exception):
    """Base class for fail-closed file-asset metadata failures."""


class FileAssetValidationError(FileAssetServiceError):
    """A requested metadata value is not safe or canonical."""


class FileAssetSubjectNotFoundError(FileAssetServiceError):
    """The requested health-subject ownership root does not exist."""


class FileAssetUploaderNotFoundError(FileAssetServiceError):
    """The requested uploader identity does not exist."""


class FileAssetConflictError(FileAssetServiceError):
    """An immutable owner, purpose, uploader, or metadata value conflicts."""


class FileAssetNotFoundError(FileAssetServiceError):
    """No matching legacy-local asset exists in the requested subject scope."""


def local_asset_is_live(asset: FileAsset) -> bool:
    """Whether one local row has a coherent, servable backend/lifecycle pair."""

    return (
        asset.deleted_at is None
        and asset.purged_at is None
        and (
            (
                asset.storage_backend == FileStorageBackend.LEGACY_LOCAL.value
                and asset.status
                in {
                    FileAssetStatus.LEGACY_PLACEHOLDER.value,
                    FileAssetStatus.PENDING.value,
                }
            )
            or (
                asset.storage_backend == FileStorageBackend.PRIVATE_LOCAL.value
                and asset.status == FileAssetStatus.ACTIVE.value
                and asset.media_type is not None
                and asset.byte_size is not None
                and asset.sha256_hex is not None
            )
        )
    )


def local_asset_is_retired(asset: FileAsset) -> bool:
    """Whether a local row has one of the two coherent retired states."""

    return asset.storage_backend in {
        FileStorageBackend.LEGACY_LOCAL.value,
        FileStorageBackend.PRIVATE_LOCAL.value,
    } and (
        (
            asset.status == FileAssetStatus.DELETED.value
            and asset.deleted_at is not None
            and asset.purged_at is None
        )
        or (
            asset.status == FileAssetStatus.PURGED.value
            and asset.deleted_at is not None
            and asset.purged_at is not None
        )
    )


def _coerce_purpose(value: FileAssetPurpose | str) -> FileAssetPurpose:
    if not isinstance(value, (FileAssetPurpose, str)):
        raise FileAssetValidationError("purpose must be a FileAssetPurpose or string")
    try:
        return FileAssetPurpose(value)
    except ValueError as exc:
        raise FileAssetValidationError("purpose is not supported") from exc


def _validate_uuid(value: object, field_name: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, uuid.UUID):
        raise FileAssetValidationError(f"{field_name} must be a UUID")


def _validate_storage_ref(
    storage_ref: object,
    purpose: FileAssetPurpose,
) -> str:
    if not isinstance(storage_ref, str):
        raise FileAssetValidationError("storage_ref must be a string")
    if not storage_ref or len(storage_ref) > _MAX_STORAGE_REF_LENGTH:
        raise FileAssetValidationError("storage_ref has an invalid length")
    if storage_ref != storage_ref.strip():
        raise FileAssetValidationError("storage_ref must not have outer whitespace")
    if "\\" in storage_ref or "\x00" in storage_ref:
        raise FileAssetValidationError("storage_ref is not a safe POSIX path")
    if storage_ref.startswith("/"):
        raise FileAssetValidationError("storage_ref must be relative")
    if ".." in storage_ref:
        # Keep service validation at least as strict as the persisted CHECK;
        # this also avoids returning a raw IntegrityError for names such as
        # ``photo..jpg`` even though they are not traversal segments.
        raise FileAssetValidationError("storage_ref contains a forbidden sequence")
    if any(ord(character) < 32 or ord(character) == 127 for character in storage_ref):
        raise FileAssetValidationError("storage_ref contains a control character")

    segments = storage_ref.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise FileAssetValidationError("storage_ref is not canonical")
    if not storage_ref.startswith(_PURPOSE_PREFIXES[purpose]):
        raise FileAssetValidationError("storage_ref prefix does not match purpose")
    return storage_ref


def _validate_media_type(media_type: object) -> str | None:
    if media_type is None:
        return None
    if not isinstance(media_type, str):
        raise FileAssetValidationError("media_type must be a string or None")
    if (
        not media_type
        or len(media_type) > _MAX_MEDIA_TYPE_LENGTH
        or media_type != media_type.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in media_type)
    ):
        raise FileAssetValidationError("media_type is not canonical")
    return media_type


def _validate_size(size_bytes: object) -> int | None:
    if size_bytes is None:
        return None
    if (
        not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes < 0
        or size_bytes > _MAX_BIGINT
    ):
        raise FileAssetValidationError("size_bytes must be a non-negative bigint")
    return size_bytes


def _validate_sha256(content_sha256: object) -> str | None:
    if content_sha256 is None:
        return None
    if not isinstance(content_sha256, str) or _SHA256_RE.fullmatch(
        content_sha256
    ) is None:
        raise FileAssetValidationError(
            "content_sha256 must be 64 lowercase hexadecimal characters"
        )
    return content_sha256


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

    _validate_uuid(subject_id, "subject_id")
    _validate_uuid(uploaded_by_user_id, "uploaded_by_user_id", nullable=True)
    normalized_purpose = _coerce_purpose(purpose)
    normalized_storage_ref = _validate_storage_ref(storage_ref, normalized_purpose)
    normalized_media_type = _validate_media_type(media_type)
    normalized_size = _validate_size(size_bytes)
    normalized_sha256 = _validate_sha256(content_sha256)

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

    _validate_uuid(subject_id, "subject_id")
    _validate_uuid(uploaded_by_user_id, "uploaded_by_user_id")
    normalized_purpose = _coerce_purpose(purpose)
    normalized_storage_ref = _validate_storage_ref(storage_ref, normalized_purpose)
    normalized_media_type = _validate_media_type(media_type)
    normalized_size = _validate_size(size_bytes)
    normalized_sha256 = _validate_sha256(content_sha256)
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

    _validate_uuid(file_asset_id, "file_asset_id")
    _validate_uuid(subject_id, "subject_id")
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

    _validate_uuid(file_asset_id, "file_asset_id")
    _validate_uuid(subject_id, "subject_id")
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


#: Lifecycle states whose bytes may still be handed to somebody. A deleted or
#: purged asset is not a 403 and not an error — it simply has no download, the
#: same answer a key that never existed gets.
SERVABLE_STATUSES = frozenset(
    {
        FileAssetStatus.LEGACY_PLACEHOLDER.value,
        FileAssetStatus.PENDING.value,
        FileAssetStatus.ACTIVE.value,
    }
)


async def resolve_for_download(
    session: AsyncSession,
    *,
    opaque_key: uuid.UUID,
    subject_id: uuid.UUID,
) -> FileAsset:
    """Find one servable asset by its rotatable key, inside one subject.

    The subject is part of the lookup rather than a check afterwards, so a key
    belonging to somebody else is indistinguishable from a key that does not
    exist. That matters more here than elsewhere: the caller is holding a URL,
    and "this file exists but is not yours" tells them their guess was right.

    Nothing about the file's own state changes the answer either — deleted,
    purged, or never-seen all raise the same not-found. The route above turns
    every one of them into the same 404.
    """

    _validate_uuid(opaque_key, "opaque_key")
    _validate_uuid(subject_id, "subject_id")

    asset = await session.scalar(
        select(FileAsset).where(
            FileAsset.opaque_key == opaque_key,
            FileAsset.subject_id == subject_id,
            FileAsset.status.in_(sorted(SERVABLE_STATUSES)),
        )
    )
    if asset is None:
        raise FileAssetNotFoundError("no servable asset for that key in this scope")
    return asset


async def resolve_local_asset(
    session: AsyncSession,
    *,
    file_asset_id: uuid.UUID,
    subject_id: uuid.UUID,
) -> FileAsset:
    """Resolve local storage metadata by an already-authorized asset id."""

    _validate_uuid(file_asset_id, "file_asset_id")
    _validate_uuid(subject_id, "subject_id")
    asset = await session.scalar(
        select(FileAsset).where(
            FileAsset.id == file_asset_id,
            FileAsset.subject_id == subject_id,
            FileAsset.storage_backend.in_(
                (
                    FileStorageBackend.LEGACY_LOCAL.value,
                    FileStorageBackend.PRIVATE_LOCAL.value,
                )
            ),
        )
    )
    if asset is None:
        raise FileAssetNotFoundError("local asset does not exist in subject scope")
    return asset


async def opaque_keys_for(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    file_asset_ids,
) -> dict[uuid.UUID, uuid.UUID]:
    """Map asset ids to download keys for one page, in one query.

    A page shows many photos and scans, and each carries an asset id rather than
    a URL. Resolving them one at a time is an N+1 against a table that is
    already indexed for exactly this, and a lazy relationship would be worse —
    it loads inside the template render, where an async session has no greenlet
    to load in.

    An id that is missing from the result is not an error: the asset is deleted,
    purged, or somebody else's. The caller renders no link, which is the same
    thing the download route would conclude one round trip later.
    """

    _validate_uuid(subject_id, "subject_id")
    wanted = {value for value in file_asset_ids if value is not None}
    for value in wanted:
        _validate_uuid(value, "file_asset_id")
    if not wanted:
        return {}

    rows = await session.execute(
        select(FileAsset.id, FileAsset.opaque_key).where(
            FileAsset.id.in_(sorted(wanted)),
            FileAsset.subject_id == subject_id,
            FileAsset.status.in_(sorted(SERVABLE_STATUSES)),
        )
    )
    return {row.id: row.opaque_key for row in rows}


__all__ = [
    "SERVABLE_STATUSES",
    "FileAssetConflictError",
    "FileAssetNotFoundError",
    "FileAssetServiceError",
    "FileAssetSubjectNotFoundError",
    "FileAssetUploaderNotFoundError",
    "FileAssetValidationError",
    "local_asset_is_live",
    "local_asset_is_retired",
    "mark_local_deleted",
    "mark_legacy_local_deleted",
    "opaque_keys_for",
    "register_private_local",
    "register_legacy_local",
    "resolve_local_asset",
    "resolve_for_download",
]
