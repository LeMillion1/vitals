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
from dataclasses import dataclass
from pathlib import PurePath
from typing import AsyncIterator

from fastapi import HTTPException, UploadFile, status

from vitals.enums import FileAssetPurpose
from vitals.persistence import file_storage as _file_storage

# Compatibility imports for existing HTTP callers. Filesystem implementation
# belongs to the core persistence boundary, not to FastAPI upload validation.
VerifiedPrivateFile = _file_storage.VerifiedPrivateFile
iter_verified_file = _file_storage.iter_verified_file
legacy_upload_disk_path = _file_storage.legacy_upload_disk_path
open_verified_file = _file_storage.open_verified_file
private_file_disk_path = _file_storage.private_file_disk_path
private_storage_ref = _file_storage.private_storage_ref
remove_stored_file = _file_storage.remove_stored_file
stored_file_disk_path = _file_storage.stored_file_disk_path
write_private_file = _file_storage.write_private_file

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
SAFE_MEDICAL_MEDIA_TYPES = frozenset(_DOCUMENT_MEDIA_TYPES.values())


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


def safe_medical_media_type(media_type: str | None) -> str:
    """Return only a content type that cannot turn a stored upload into HTML/SVG."""

    if media_type in SAFE_MEDICAL_MEDIA_TYPES:
        return media_type
    return "application/octet-stream"


def care_attachment_storage_ref(extension: str) -> str:
    """Mint a locator unrelated to patient, thread, filename, or download URL."""

    return private_storage_ref(FileAssetPurpose.CARE_MESSAGE_ATTACHMENT, extension)


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
    allowed_extensions: frozenset[str] = DOC_EXTS,
) -> PreparedMedicalDocument | None:
    """Validate one optional image/PDF without trusting its declared MIME type."""

    if file is None or not file.filename:
        return None
    original_filename = _safe_original_filename(file.filename)
    extension = validate_extension(original_filename, allowed_extensions)
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
