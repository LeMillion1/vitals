"""Private medical-file primitives shared by HTTP and operator boundaries.

This module knows paths, descriptors, hashes and atomic filesystem writes.  It
does not know FastAPI, sessions or business authorization.  Callers must first
resolve a subject-owned :class:`FileAsset`; raw storage locators must never be
accepted from a browser as authorization.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO, Iterator

from vitals.enums import FileAssetPurpose, FileStorageBackend

CHUNK_SIZE = 1024 * 1024
_PRIVATE_EXTENSIONS = frozenset(
    {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"}
)
_PURPOSE_PREFIXES = {
    FileAssetPurpose.PROGRESS_PHOTO: "uploads",
    FileAssetPurpose.LAB_DOCUMENT: "labs",
    FileAssetPurpose.BODY_SCAN_DOCUMENT: "body",
    FileAssetPurpose.CARE_MESSAGE_ATTACHMENT: "care",
}


@dataclass(slots=True)
class VerifiedPrivateFile:
    """One integrity-checked descriptor, ready to stream without reopening."""

    stream: BinaryIO
    byte_size: int


@dataclass(frozen=True, slots=True)
class CopiedPrivateFile:
    """Canonical metadata produced while copying one already-open source."""

    path: str
    byte_size: int
    sha256_hex: str


def legacy_upload_disk_path(static_dir: str, storage_ref: str) -> str:
    """Resolve a legacy locator below ``web/static/uploads``."""

    if not isinstance(storage_ref, str) or not storage_ref:
        raise ValueError("storage_ref must be a non-empty string")
    relative = (
        storage_ref.removeprefix("uploads/")
        if storage_ref.startswith("uploads/")
        else storage_ref
    )
    uploads_root = os.path.realpath(os.path.join(static_dir, "uploads"))
    path = os.path.realpath(os.path.join(uploads_root, relative))
    if not path.startswith(uploads_root + os.sep):
        raise ValueError("storage_ref leaves the private uploads directory")
    return path


def _legacy_ref_parts(storage_ref: str) -> tuple[str, ...]:
    """Validate a historical uploads locator without following path links."""

    if not isinstance(storage_ref, str) or not storage_ref:
        raise ValueError("storage_ref must be a non-empty string")
    relative = storage_ref.removeprefix("uploads/")
    if (
        relative != relative.strip()
        or relative.startswith("/")
        or "\\" in relative
        or "\x00" in relative
        or any(ord(character) < 32 or ord(character) == 127 for character in relative)
    ):
        raise ValueError("legacy storage_ref must be a canonical uploads path")
    parts = tuple(relative.split("/"))
    if any(segment in {"", ".", ".."} for segment in parts):
        raise ValueError("legacy storage_ref must be a canonical uploads path")
    return parts


def private_file_disk_path(private_root: str, storage_ref: str) -> str:
    """Resolve a canonical private-local locator below an absolute root."""

    if not isinstance(private_root, str) or not os.path.isabs(private_root):
        raise ValueError("private file root must be an absolute path")
    if not isinstance(storage_ref, str) or not storage_ref:
        raise ValueError("storage_ref must be a non-empty string")
    if (
        storage_ref != storage_ref.strip()
        or storage_ref.startswith("/")
        or "\\" in storage_ref
        or "\x00" in storage_ref
        or ".." in storage_ref
        or any(ord(character) < 32 or ord(character) == 127 for character in storage_ref)
        or any(segment in {"", ".", ".."} for segment in storage_ref.split("/"))
    ):
        raise ValueError("storage_ref must be a canonical private path")
    root = os.path.realpath(private_root)
    path = os.path.realpath(os.path.join(root, storage_ref))
    if not path.startswith(root + os.sep):
        raise ValueError("storage_ref leaves the private file root")
    return path


def _private_ref_parts(storage_ref: str) -> tuple[str, ...]:
    """Validate one locator without resolving or following filesystem links."""

    if not isinstance(storage_ref, str) or not storage_ref:
        raise ValueError("storage_ref must be a non-empty string")
    if (
        storage_ref != storage_ref.strip()
        or storage_ref.startswith("/")
        or "\\" in storage_ref
        or "\x00" in storage_ref
        or ".." in storage_ref
        or any(ord(character) < 32 or ord(character) == 127 for character in storage_ref)
    ):
        raise ValueError("storage_ref must be a canonical private path")
    parts = tuple(storage_ref.split("/"))
    if any(segment in {"", ".", ".."} for segment in parts):
        raise ValueError("storage_ref must be a canonical private path")
    return parts


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _secure_private_parent(
    private_root: str,
    storage_ref: str,
    *,
    create: bool,
) -> tuple[int, str, str]:
    """Open each private path component without following a symlink.

    Every storage-owned directory is forced to owner-only mode. Directory
    creation and traversal use ``dir_fd`` so swapping a parent pathname cannot
    redirect the later file publish outside the descriptor we inspected.
    """

    if not isinstance(private_root, str) or not os.path.isabs(private_root):
        raise ValueError("private file root must be an absolute path")
    parts = _private_ref_parts(storage_ref)
    root = os.path.abspath(private_root)
    if create:
        try:
            os.makedirs(root, mode=0o700, exist_ok=True)
        except FileExistsError:
            raise ValueError("private file root is not a directory") from None
    try:
        root_stat = os.lstat(root)
    except FileNotFoundError:
        raise ValueError("private file root does not exist") from None
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("private file root must be a real directory")

    current_fd = os.open(root, _directory_open_flags())
    try:
        os.fchmod(current_fd, 0o700)
        for segment in parts[:-1]:
            created = False
            if create:
                try:
                    os.mkdir(segment, mode=0o700, dir_fd=current_fd)
                    created = True
                except FileExistsError:
                    pass
            child_fd = os.open(segment, _directory_open_flags(), dir_fd=current_fd)
            child_stat = os.fstat(child_fd)
            if not stat.S_ISDIR(child_stat.st_mode):
                os.close(child_fd)
                raise ValueError("private path component is not a directory")
            os.fchmod(child_fd, 0o700)
            if created:
                os.fsync(current_fd)
            os.close(current_fd)
            current_fd = child_fd
        return current_fd, parts[-1], os.path.join(root, *parts)
    except BaseException:
        os.close(current_fd)
        raise


def _new_private_temp(parent_fd: int, prefix: str) -> tuple[int, str]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _attempt in range(16):
        name = f".{prefix}-{uuid.uuid4().hex}"
        try:
            return os.open(name, flags, 0o600, dir_fd=parent_fd), name
        except FileExistsError:
            continue
    raise FileExistsError("could not reserve a private temporary name")


def _open_private_file(private_root: str, storage_ref: str) -> int:
    parent_fd, name, _path = _secure_private_parent(
        private_root, storage_ref, create=False
    )
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        return os.open(name, flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _secure_legacy_parent(static_dir: str, storage_ref: str) -> tuple[int, str]:
    """Open a legacy upload parent without accepting symlink traversal."""

    parts = _legacy_ref_parts(storage_ref)
    uploads_root = os.path.abspath(os.path.join(static_dir, "uploads"))
    try:
        root_stat = os.lstat(uploads_root)
    except FileNotFoundError:
        raise ValueError("legacy uploads root does not exist") from None
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("legacy uploads root must be a real directory")
    current_fd = os.open(uploads_root, _directory_open_flags())
    try:
        for segment in parts[:-1]:
            child_fd = os.open(segment, _directory_open_flags(), dir_fd=current_fd)
            child_stat = os.fstat(child_fd)
            if not stat.S_ISDIR(child_stat.st_mode):
                os.close(child_fd)
                raise ValueError("legacy path component is not a directory")
            os.close(current_fd)
            current_fd = child_fd
        return current_fd, parts[-1]
    except BaseException:
        os.close(current_fd)
        raise


def _open_legacy_file(static_dir: str, storage_ref: str) -> int:
    parent_fd, name = _secure_legacy_parent(static_dir, storage_ref)
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        return os.open(name, flags, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def stored_file_disk_path(
    *,
    storage_backend: str,
    storage_ref: str,
    static_dir: str,
    private_root: str,
) -> str:
    """Resolve one supported local backend without exposing its locator."""

    if storage_backend == FileStorageBackend.LEGACY_LOCAL.value:
        return legacy_upload_disk_path(static_dir, storage_ref)
    if storage_backend == FileStorageBackend.PRIVATE_LOCAL.value:
        return private_file_disk_path(private_root, storage_ref)
    raise ValueError("unsupported private-file storage backend")


def remove_stored_file(
    *,
    storage_backend: str,
    storage_ref: str,
    static_dir: str,
    private_root: str,
) -> None:
    """Idempotently remove one local object through its backend boundary."""

    if storage_backend == FileStorageBackend.PRIVATE_LOCAL.value:
        try:
            parent_fd, name, _path = _secure_private_parent(
                private_root, storage_ref, create=False
            )
        except FileNotFoundError:
            return
        try:
            try:
                os.unlink(name, dir_fd=parent_fd)
            except FileNotFoundError:
                return
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return
    if storage_backend == FileStorageBackend.LEGACY_LOCAL.value:
        try:
            parent_fd, name = _secure_legacy_parent(static_dir, storage_ref)
        except FileNotFoundError:
            return
        try:
            try:
                os.unlink(name, dir_fd=parent_fd)
            except FileNotFoundError:
                return
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return
    raise ValueError("unsupported private-file storage backend")


def private_storage_ref(
    purpose: FileAssetPurpose | str,
    extension: str,
) -> str:
    """Mint a random locator unrelated to owner, filename or download URL."""

    try:
        normalized_purpose = FileAssetPurpose(purpose)
    except (TypeError, ValueError) as exc:
        raise ValueError("unsupported private file purpose") from exc
    if extension not in _PRIVATE_EXTENSIONS:
        raise ValueError("unsupported private file extension")
    key = uuid.uuid4().hex
    prefix = _PURPOSE_PREFIXES[normalized_purpose]
    return f"{prefix}/{key[:2]}/{key}{extension}"


def _validated_expected_metadata(
    expected_size: int | None,
    expected_sha256: str | None,
) -> None:
    if expected_size is not None and (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
    ):
        raise ValueError("stored file has invalid size metadata")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise ValueError("stored file has invalid digest metadata")


def open_verified_file(
    *,
    storage_backend: str,
    storage_ref: str,
    static_dir: str,
    private_root: str,
    expected_size: int | None,
    expected_sha256: str | None,
) -> VerifiedPrivateFile:
    """Verify and return the same descriptor that delivery will stream."""

    _validated_expected_metadata(expected_size, expected_sha256)
    if storage_backend == FileStorageBackend.PRIVATE_LOCAL.value:
        descriptor = _open_private_file(private_root, storage_ref)
    elif storage_backend == FileStorageBackend.LEGACY_LOCAL.value:
        descriptor = _open_legacy_file(static_dir, storage_ref)
    else:
        raise ValueError("unsupported private-file storage backend")
    stream = os.fdopen(descriptor, "rb")
    try:
        file_stat = os.fstat(stream.fileno())
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("stored object is not a regular file")
        if expected_size is not None and file_stat.st_size != expected_size:
            raise ValueError("stored file size does not match metadata")
        if expected_sha256 is not None:
            digest = hashlib.sha256()
            while chunk := stream.read(CHUNK_SIZE):
                digest.update(chunk)
            if not hmac.compare_digest(digest.hexdigest(), expected_sha256):
                raise ValueError("stored file digest does not match metadata")
            stream.seek(0)
        return VerifiedPrivateFile(stream=stream, byte_size=file_stat.st_size)
    except BaseException:
        stream.close()
        raise


def iter_verified_file(
    verified: VerifiedPrivateFile,
    *,
    chunk_size: int = CHUNK_SIZE,
) -> Iterator[bytes]:
    """Stream a verified descriptor in bounded chunks and always close it."""

    try:
        while chunk := verified.stream.read(chunk_size):
            yield chunk
    finally:
        verified.stream.close()


def _publish_temporary(parent_fd: int, temporary: str, destination: str) -> None:
    """Publish by hard link, refusing to replace an existing destination."""

    linked = False
    try:
        os.link(
            temporary,
            destination,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        linked = True
        os.unlink(temporary, dir_fd=parent_fd)
        # File fsync makes the payload durable; directory fsync makes the final
        # name durable. Both are needed before metadata may commit.
        os.fsync(parent_fd)
    except BaseException:
        # No database call has happened yet. A final name that could not be made
        # durable is not a commit ambiguity; remove it so a failed request cannot
        # strand untracked PHI. The caller still owns temporary-name cleanup.
        if linked:
            try:
                os.unlink(destination, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError:
                pass
        raise


def write_private_file(private_root: str, storage_ref: str, body: bytes) -> str:
    """Atomically write owner-only bytes; an existing destination is an error."""

    parent_fd, destination, path = _secure_private_parent(
        private_root, storage_ref, create=True
    )
    try:
        fd, temporary = _new_private_temp(parent_fd, "incoming")
    except BaseException:
        os.close(parent_fd)
        raise
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        _publish_temporary(parent_fd, temporary, destination)
        temporary = ""
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if temporary:
                os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(parent_fd)
    return path


def copy_legacy_file_to_private(
    *,
    static_dir: str,
    legacy_storage_ref: str,
    private_root: str,
    private_storage_ref: str,
    expected_size: int | None,
    expected_sha256: str | None,
) -> CopiedPrivateFile:
    """Copy, hash and verify one legacy object from a single open descriptor.

    The source is never deleted.  The destination is published only after the
    copied bytes, size and optional persisted digest agree.  A destination
    collision raises ``FileExistsError`` and never overwrites medical bytes.
    """

    _validated_expected_metadata(expected_size, expected_sha256)
    parent_fd, destination_name, destination = _secure_private_parent(
        private_root, private_storage_ref, create=True
    )

    try:
        source_fd = _open_legacy_file(static_dir, legacy_storage_ref)
        source = os.fdopen(source_fd, "rb")
    except BaseException:
        os.close(parent_fd)
        raise
    temporary_fd = -1
    temporary = ""
    try:
        source_stat = os.fstat(source.fileno())
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError("legacy object is not a regular file")
        if expected_size is not None and source_stat.st_size != expected_size:
            raise ValueError("legacy file size does not match metadata")

        temporary_fd, temporary = _new_private_temp(parent_fd, "relocating")
        os.fchmod(temporary_fd, 0o600)
        digest = hashlib.sha256()
        copied = 0
        with os.fdopen(temporary_fd, "wb") as target:
            temporary_fd = -1
            while chunk := source.read(CHUNK_SIZE):
                digest.update(chunk)
                target.write(chunk)
                copied += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        final_source_stat = os.fstat(source.fileno())
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(
            getattr(source_stat, field) != getattr(final_source_stat, field)
            for field in stable_fields
        ):
            raise ValueError("legacy file changed while it was copied")
        if copied != source_stat.st_size:
            raise ValueError("legacy file changed while it was copied")
        sha256_hex = digest.hexdigest()
        if expected_sha256 is not None and not hmac.compare_digest(
            sha256_hex, expected_sha256
        ):
            raise ValueError("legacy file digest does not match metadata")
        _publish_temporary(parent_fd, temporary, destination_name)
        temporary = ""
        return CopiedPrivateFile(destination, copied, sha256_hex)
    finally:
        source.close()
        if temporary_fd >= 0:
            try:
                os.close(temporary_fd)
            except OSError:
                pass
        if temporary:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def extension_for_relocation(storage_ref: str) -> str:
    """Return a reviewed medical extension from one canonical legacy locator."""

    extension = PurePosixPath(storage_ref).suffix.lower()
    if extension not in _PRIVATE_EXTENSIONS:
        raise ValueError("legacy file extension is not eligible for relocation")
    return extension


__all__ = [
    "CHUNK_SIZE",
    "CopiedPrivateFile",
    "VerifiedPrivateFile",
    "copy_legacy_file_to_private",
    "extension_for_relocation",
    "iter_verified_file",
    "legacy_upload_disk_path",
    "open_verified_file",
    "private_file_disk_path",
    "private_storage_ref",
    "remove_stored_file",
    "stored_file_disk_path",
    "write_private_file",
]
