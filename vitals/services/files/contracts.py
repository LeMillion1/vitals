"""Validation contracts and lifecycle predicates for private file assets."""
from __future__ import annotations

import re
import uuid

from vitals.enums import FileAssetPurpose, FileAssetStatus, FileStorageBackend
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
    """No matching local asset exists in the requested subject scope."""


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


def coerce_purpose(value: FileAssetPurpose | str) -> FileAssetPurpose:
    if not isinstance(value, (FileAssetPurpose, str)):
        raise FileAssetValidationError("purpose must be a FileAssetPurpose or string")
    try:
        return FileAssetPurpose(value)
    except ValueError as exc:
        raise FileAssetValidationError("purpose is not supported") from exc


def validate_uuid(value: object, field_name: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, uuid.UUID):
        raise FileAssetValidationError(f"{field_name} must be a UUID")


def validate_storage_ref(storage_ref: object, purpose: FileAssetPurpose) -> str:
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
        raise FileAssetValidationError("storage_ref contains a forbidden sequence")
    if any(ord(character) < 32 or ord(character) == 127 for character in storage_ref):
        raise FileAssetValidationError("storage_ref contains a control character")
    segments = storage_ref.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise FileAssetValidationError("storage_ref is not canonical")
    if not storage_ref.startswith(_PURPOSE_PREFIXES[purpose]):
        raise FileAssetValidationError("storage_ref prefix does not match purpose")
    return storage_ref


def validate_media_type(media_type: object) -> str | None:
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


def validate_size(size_bytes: object) -> int | None:
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


def validate_sha256(content_sha256: object) -> str | None:
    if content_sha256 is None:
        return None
    if not isinstance(content_sha256, str) or _SHA256_RE.fullmatch(content_sha256) is None:
        raise FileAssetValidationError(
            "content_sha256 must be 64 lowercase hexadecimal characters"
        )
    return content_sha256


__all__ = [
    "FileAssetConflictError",
    "FileAssetNotFoundError",
    "FileAssetServiceError",
    "FileAssetSubjectNotFoundError",
    "FileAssetUploaderNotFoundError",
    "FileAssetValidationError",
    "coerce_purpose",
    "local_asset_is_live",
    "local_asset_is_retired",
    "validate_media_type",
    "validate_sha256",
    "validate_size",
    "validate_storage_ref",
    "validate_uuid",
]
