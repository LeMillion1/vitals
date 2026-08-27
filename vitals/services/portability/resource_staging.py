"""Stage validated portability-v2 medical resources into private storage.

The archive reader authenticates and bounds the container; this boundary still
rechecks the medical file signature and independently streams, counts and hashes
every object before registering flush-only metadata.  Physical files cannot join
the database transaction, so callers own post-rollback cleanup using the exact
``newly_written_objects`` returned on success.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import BinaryIO, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import FileAssetPurpose, FileStorageBackend
from vitals.models.identity import HealthSubject, User
from vitals.persistence import file_storage
from vitals.services.files import lifecycle as file_lifecycle
from vitals.services.portability.archive_reader import (
    ValidatedArchive,
    open_validated_resource,
    validated_record_manifest,
)
from vitals.services.portability.record_decoder import DecodedRecord, DecodedResource
from vitals.services.portability.schema import PORTABILITY_SCHEMA_DIGEST


_MEDIA_EXTENSIONS: Final = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}
_HEADER_BYTES: Final = 32


class ResourceStagingError(RuntimeError):
    """A resource could not be staged without trusting private input detail."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        newly_written_objects: tuple["NewlyWrittenPrivateObject", ...] = (),
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.newly_written_objects = newly_written_objects


@dataclass(frozen=True, slots=True)
class NewlyWrittenPrivateObject:
    """One object this call created and a rollback boundary may remove."""

    resource_ref: str
    storage_ref: str
    byte_size: int
    sha256_hex: str

    @property
    def storage_backend(self) -> str:
        return FileStorageBackend.PRIVATE_LOCAL.value


@dataclass(frozen=True, slots=True)
class StagedResourceBinding:
    """One archive resource ref resolved to newly registered local metadata."""

    ref: str
    file_asset_id: uuid.UUID
    storage_ref: str
    purpose: str
    media_type: str
    byte_size: int
    sha256_hex: str


@dataclass(frozen=True, slots=True)
class StagedResourceMapping(Mapping[str, StagedResourceBinding]):
    """Immutable ref-sorted bindings plus the exact physical write set."""

    bindings: tuple[StagedResourceBinding, ...]
    newly_written_objects: tuple[NewlyWrittenPrivateObject, ...]

    def __getitem__(self, ref: str) -> StagedResourceBinding:
        for binding in self.bindings:
            if binding.ref == ref:
                return binding
        raise KeyError(ref)

    def __iter__(self) -> Iterator[str]:
        return (binding.ref for binding in self.bindings)

    def __len__(self) -> int:
        return len(self.bindings)


class _ReplayingStream:
    """Replay an inspected prefix, then continue from the same archive stream."""

    def __init__(self, prefix: bytes, source: BinaryIO) -> None:
        self._prefix = prefix
        self._offset = 0
        self._source = source

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if size < 0:
            raise ValueError("staging stream requires a bounded read size")
        remaining = self._prefix[self._offset :]
        prefix_part = remaining[:size]
        self._offset += len(prefix_part)
        if len(prefix_part) == size:
            return prefix_part
        tail = self._source.read(size - len(prefix_part))
        if not isinstance(tail, (bytes, bytearray, memoryview)):
            raise TypeError("archive resource stream is not binary")
        if len(tail) > size - len(prefix_part):
            raise ValueError("archive resource stream exceeded the requested read size")
        return prefix_part + bytes(tail)


def _error(code: str, detail: str) -> ResourceStagingError:
    return ResourceStagingError(code, detail)


def _require_uuid(value: object, *, field: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise _error("resource_staging_invalid", f"{field} must be a non-zero UUID")
    return value


def _extension(resource: DecodedResource) -> str:
    extension = _MEDIA_EXTENSIONS.get(resource.media_type)
    if extension is None:
        raise _error(
            "resource_media_type_unsupported",
            "a resource media type is not an allowlisted medical format",
        )
    try:
        FileAssetPurpose(resource.purpose)
    except ValueError:
        raise _error(
            "resource_purpose_unsupported",
            "a resource purpose is not a supported private-file purpose",
        ) from None
    return extension


def _read_header(source: BinaryIO) -> bytes:
    chunks: list[bytes] = []
    remaining = _HEADER_BYTES
    while remaining:
        chunk = source.read(remaining)
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise _error("resource_stream_invalid", "archive resource stream is not binary")
        if not chunk:
            break
        body = bytes(chunk)
        if len(body) > remaining:
            raise _error(
                "resource_stream_invalid",
                "archive resource stream exceeded the requested read size",
            )
        chunks.append(body)
        remaining -= len(body)
    return b"".join(chunks)


def _medical_magic_matches(header: bytes, media_type: str) -> bool:
    if media_type == "application/pdf":
        return header.startswith(b"%PDF-")
    if media_type == "image/png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return header.startswith(b"\xff\xd8\xff")
    if media_type == "image/webp":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    if media_type in {"image/heic", "image/heif"}:
        return (
            len(header) >= 16
            and header[4:8] == b"ftyp"
            and any(
                brand in header[8:32]
                for brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1")
            )
        )
    return False


def _resource_identity(resource: DecodedResource) -> tuple[object, ...]:
    return (
        resource.ref,
        resource.purpose,
        resource.media_type,
        resource.byte_size,
        resource.sha256_hex,
        resource.object_path,
    )


def _validate_archive_record_pair(
    archive: ValidatedArchive,
    record: DecodedRecord,
) -> tuple[DecodedResource, ...]:
    if not isinstance(archive, ValidatedArchive):
        raise TypeError("archive must be a ValidatedArchive")
    if not isinstance(record, DecodedRecord):
        raise TypeError("record must be a DecodedRecord")
    if (
        record.record_ref != archive.record_ref
        or record.schema_digest != PORTABILITY_SCHEMA_DIGEST
        or archive.schema_digest != PORTABILITY_SCHEMA_DIGEST
        or len(record.resources) != archive.resource_count
    ):
        raise _error(
            "resource_record_mismatch",
            "decoded resources do not belong to the validated archive",
        )

    manifest = validated_record_manifest(archive)
    manifest_identities = tuple(
        (
            item["ref"],
            item["purpose"],
            item["media_type"],
            item["byte_size"],
            item["sha256_hex"],
            item["object_path"],
        )
        for item in manifest["resources"]
    )
    resources = tuple(record.resources)
    if tuple(_resource_identity(resource) for resource in resources) != manifest_identities:
        raise _error(
            "resource_record_mismatch",
            "decoded resources differ from the validated archive manifest",
        )
    return resources


def _remove_new_objects(
    objects: tuple[NewlyWrittenPrivateObject, ...],
    *,
    private_root: str,
) -> tuple[NewlyWrittenPrivateObject, ...]:
    failed: list[NewlyWrittenPrivateObject] = []
    for item in reversed(objects):
        try:
            file_storage.remove_stored_file(
                storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
                storage_ref=item.storage_ref,
                static_dir=private_root,
                private_root=private_root,
            )
        except Exception:
            failed.append(item)
    return tuple(reversed(failed))


async def _validate_targets(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> None:
    with session.no_autoflush:
        subject_exists = await session.scalar(
            select(HealthSubject.id).where(HealthSubject.id == subject_id)
        )
        actor_exists = await session.scalar(select(User.id).where(User.id == actor_user_id))
    if subject_exists is None or actor_exists is None:
        raise _error(
            "resource_target_missing",
            "the target subject or staging actor does not exist",
        )


async def stage_record_resources(
    session: AsyncSession,
    *,
    archive: ValidatedArchive,
    record: DecodedRecord,
    target_subject_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    private_root: str,
) -> StagedResourceMapping:
    """Stage every decoded resource and flush new subject-owned FileAssets.

    The function never commits or rolls back.  If it raises after a metadata row
    was flushed, the caller must roll back that database transaction; all physical
    objects successfully published by this call have already been removed unless
    the raised cleanup error reports otherwise.
    """

    subject_id = _require_uuid(target_subject_id, field="target subject id")
    uploader_id = _require_uuid(actor_user_id, field="actor user id")
    if type(private_root) is not str or not os.path.isabs(private_root):
        raise _error("private_root_invalid", "private_root must be an absolute filesystem path")
    resources = _validate_archive_record_pair(archive, record)
    await _validate_targets(
        session,
        subject_id=subject_id,
        actor_user_id=uploader_id,
    )

    bindings: list[StagedResourceBinding] = []
    written: list[NewlyWrittenPrivateObject] = []
    try:
        for resource in resources:
            extension = _extension(resource)
            purpose = FileAssetPurpose(resource.purpose)
            storage_ref = file_storage.private_storage_ref(purpose, extension)
            with open_validated_resource(archive, resource.sha256_hex) as source:
                header = _read_header(source)
                if not _medical_magic_matches(header, resource.media_type):
                    raise _error(
                        "resource_magic_invalid",
                        "resource bytes do not match the declared medical media type",
                    )
                copied = file_storage.copy_stream_to_private(
                    source=_ReplayingStream(header, source),
                    private_root=private_root,
                    private_storage_ref=storage_ref,
                    expected_size=resource.byte_size,
                    expected_sha256=resource.sha256_hex,
                )
            new_object = NewlyWrittenPrivateObject(
                resource_ref=resource.ref,
                storage_ref=storage_ref,
                byte_size=copied.byte_size,
                sha256_hex=copied.sha256_hex,
            )
            written.append(new_object)
            asset = await file_lifecycle.register_private_local(
                session,
                subject_id=subject_id,
                uploaded_by_user_id=uploader_id,
                purpose=purpose,
                storage_ref=storage_ref,
                media_type=resource.media_type,
                size_bytes=copied.byte_size,
                content_sha256=copied.sha256_hex,
            )
            bindings.append(
                StagedResourceBinding(
                    ref=resource.ref,
                    file_asset_id=asset.id,
                    storage_ref=storage_ref,
                    purpose=resource.purpose,
                    media_type=resource.media_type,
                    byte_size=copied.byte_size,
                    sha256_hex=copied.sha256_hex,
                )
            )
    except BaseException as exc:
        written_tuple = tuple(written)
        cleanup_failed = _remove_new_objects(written_tuple, private_root=private_root)
        if cleanup_failed:
            raise ResourceStagingError(
                "resource_cleanup_failed",
                "staging failed and one or more new private objects require cleanup",
                newly_written_objects=cleanup_failed,
            ) from exc
        if isinstance(exc, ResourceStagingError):
            raise
        if isinstance(exc, Exception):
            raise ResourceStagingError(
                "resource_staging_failed", "a resource could not be staged safely"
            ) from exc
        raise

    return StagedResourceMapping(
        bindings=tuple(bindings),
        newly_written_objects=tuple(written),
    )


__all__ = [
    "NewlyWrittenPrivateObject",
    "ResourceStagingError",
    "StagedResourceBinding",
    "StagedResourceMapping",
    "stage_record_resources",
]
