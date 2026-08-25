"""Shared upload validation — extension allowlist + size cap.

The routers share three independent risks:

  * **Stored-file type.** Legacy uploads land below ``static/uploads`` and a
    future routing regression must still not turn ``.html``/``.svg`` into a
    same-origin script. New private-local files are download-only, but their
    bytes still have to match the allowed image/PDF extension.
  * **Unbounded memory.** ``await file.read()`` slurps the whole body into RAM
    with no ceiling (no reverse proxy in front). We read in chunks and abort once
    the cap is exceeded.
  * **Path identity.** Private-local locators are random, canonical, contained
    below an absolute non-static root, and never overwrite an existing object.

Routers pass their own allowlist (images, docs, json, vcf) and size cap.
"""
from __future__ import annotations

import codecs
import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import PurePath
from typing import AsyncIterator

from fastapi import HTTPException, UploadFile, status

# Per-kind extension allowlists (lower-case, with the leading dot).
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"})
DOC_EXTS = IMAGE_EXTS | frozenset({".pdf"})
JSON_EXTS = frozenset({".json"})
VCF_EXTS = frozenset({".vcf", ".txt"})

# Default body cap (images / json). VCF gets a larger one (consumer genomes).
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
VCF_MAX_BYTES = 100 * 1024 * 1024

_CHUNK = 1024 * 1024

_DOCUMENT_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


@dataclass(frozen=True, slots=True)
class PreparedMedicalDocument:
    """Validated bytes and canonical metadata for one private upload."""

    original_filename: str
    extension: str
    media_type: str
    body: bytes
    sha256_hex: str

    @property
    def byte_size(self) -> int:
        return len(self.body)


def legacy_upload_disk_path(static_dir: str, storage_ref: str) -> str:
    """Resolve a validated legacy ``FileAsset.storage_ref`` under uploads.

    Progress-photo references historically include the leading ``uploads/``;
    lab/body references are already relative to that directory.  The realpath
    containment check protects delete/cleanup callers from a forged legacy DB
    value as well as from symlinks leaving the private tree.
    """

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


def care_attachment_storage_ref(extension: str) -> str:
    """Mint a locator unrelated to patient, thread, filename, or download URL."""

    if extension not in DOC_EXTS:
        raise ValueError("unsupported care attachment extension")
    key = uuid.uuid4().hex
    return f"care/{key[:2]}/{key}{extension}"


def write_private_file(private_root: str, storage_ref: str, body: bytes) -> str:
    """Atomically write owner-only bytes and return their validated path."""

    path = private_file_disk_path(private_root, storage_ref)
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    # Resolve again after creating the parent, so a configured path containing a
    # symlink cannot leave the private tree between validation and the write.
    path = private_file_disk_path(private_root, storage_ref)
    fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        # Generated names make collisions fantastically unlikely, but a
        # collision must fail rather than replace existing medical bytes.
        os.link(temporary, path)
        os.unlink(temporary)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return path


def file_sha256_hex(path: str) -> str:
    """Hash a private file in bounded chunks before serving it."""

    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def file_ext(filename: str | None) -> str:
    """Lower-cased extension (with dot) of *filename*, or ``''`` when absent."""
    return os.path.splitext(filename or "")[1].lower()


def validate_extension(filename: str | None, allowed: frozenset[str]) -> str:
    """Return the (validated, lower-cased) extension or raise HTTP 415."""
    ext = file_ext(filename)
    if ext not in allowed:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type: {ext or 'unknown'}",
        )
    return ext


def _too_large(max_bytes: int) -> HTTPException:
    # 413 Content Too Large (literal to stay version-agnostic across the Starlette
    # constant rename).
    return HTTPException(
        status_code=413,
        detail=f"File too large (max {max_bytes // (1024 * 1024)} MB)",
    )


async def read_capped(file: UploadFile, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    """Read the upload in chunks, raising HTTP 413 once it exceeds ``max_bytes``
    (so a multi-GB body can't OOM the worker)."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail=f"File too large (max {max_bytes // (1024 * 1024)} MB)")
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_original_filename(filename: str | None) -> str:
    """Keep a display name, never a client-supplied path or control bytes."""

    if not isinstance(filename, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Attachment filename is missing",
        )
    # Browsers normally send only a basename; hostile clients may send POSIX or
    # Windows paths. Normalise both separators before taking the last segment.
    name = PurePath(filename.replace("\\", "/")).name.strip()
    if (
        not name
        or len(name) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Attachment filename is invalid",
        )
    return name


def _has_expected_signature(body: bytes, extension: str) -> bool:
    if extension == ".pdf":
        return body.startswith(b"%PDF-")
    if extension == ".png":
        return body.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in {".jpg", ".jpeg"}:
        return body.startswith(b"\xff\xd8\xff")
    if extension == ".webp":
        return len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP"
    if extension in {".heic", ".heif"}:
        return (
            len(body) >= 16
            and body[4:8] == b"ftyp"
            and any(
                brand in body[8:32]
                for brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1")
            )
        )
    return False


async def prepare_medical_document(
    file: UploadFile | None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> PreparedMedicalDocument | None:
    """Validate one optional image/PDF without trusting its declared MIME type."""

    if file is None or not file.filename:
        return None
    original_filename = _safe_original_filename(file.filename)
    extension = validate_extension(original_filename, DOC_EXTS)
    body = await read_capped(file, max_bytes=max_bytes)
    if not _has_expected_signature(body, extension):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File content does not match its extension",
        )
    return PreparedMedicalDocument(
        original_filename=original_filename,
        extension=extension,
        media_type=_DOCUMENT_MEDIA_TYPES[extension],
        body=body,
        sha256_hex=hashlib.sha256(body).hexdigest(),
    )


async def iter_lines_capped(
    file: UploadFile, *, max_bytes: int = DEFAULT_MAX_BYTES, encoding: str = "utf-8"
) -> AsyncIterator[str]:
    """Yield decoded text lines from an upload without buffering the whole body.

    Reads in chunks, decodes incrementally (UTF-8-safe across chunk boundaries),
    and emits complete lines as they arrive — so a 100 MB VCF never holds more
    than one chunk plus a partial line in memory, instead of ~3 full copies
    (bytes + decoded str + StringIO). Raises HTTP 413 once the raw bytes exceed
    ``max_bytes``. Lines are yielded without their trailing newline."""
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    total = 0
    pending = ""
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise _too_large(max_bytes)
        pending += decoder.decode(chunk)
        if "\n" in pending:
            *lines, pending = pending.split("\n")
            for line in lines:
                yield line
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending
