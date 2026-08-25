"""Private care files never become static paths or trust browser metadata."""

from __future__ import annotations

import io
import hashlib
import os
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from web.uploads import (
    IMAGE_EXTS,
    care_attachment_storage_ref,
    iter_verified_file,
    open_verified_file,
    prepare_medical_document,
    private_file_disk_path,
    write_private_file,
)


@pytest.mark.parametrize(
    "storage_ref",
    (
        "../outside.pdf",
        "care/../outside.pdf",
        "care//outside.pdf",
        "/absolute.pdf",
        "care\\outside.pdf",
        "care/evil\x00.pdf",
    ),
)
def test_private_storage_rejects_noncanonical_locators(tmp_path, storage_ref):
    with pytest.raises(ValueError):
        private_file_disk_path(str(tmp_path), storage_ref)


def test_private_storage_never_overwrites_existing_medical_bytes(tmp_path):
    storage_ref = care_attachment_storage_ref(".pdf")
    path = write_private_file(str(tmp_path), storage_ref, b"first")

    assert Path(path).read_bytes() == b"first"
    assert os.stat(path).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_private_file(str(tmp_path), storage_ref, b"replacement")
    assert Path(path).read_bytes() == b"first"


def test_private_storage_configuration_fails_closed_inside_static(monkeypatch):
    from web.config import get_web_config
    from web.templating import STATIC_DIR

    monkeypatch.setenv(
        "VITALS_PRIVATE_FILE_ROOT", os.path.join(STATIC_DIR, "private-medical")
    )
    with pytest.raises(RuntimeError, match="outside web/static"):
        get_web_config()


async def test_upload_metadata_comes_from_verified_bytes_not_the_browser():
    payload = b"%PDF-1.7\nsynthetic\n%%EOF\n"
    upload = UploadFile(
        filename="result.pdf",
        file=io.BytesIO(payload),
        headers={"content-type": "text/html"},
    )

    prepared = await prepare_medical_document(upload)

    assert prepared is not None
    assert prepared.media_type == "application/pdf"
    assert prepared.original_filename == "result.pdf"
    assert prepared.body == payload


async def test_image_mime_is_canonical_even_when_the_browser_claims_svg():
    payload = b"\xff\xd8\xffsynthetic-jpeg"
    upload = UploadFile(
        filename="progress.jpg",
        file=io.BytesIO(payload),
        headers={"content-type": "image/svg+xml"},
    )

    prepared = await prepare_medical_document(
        upload,
        allowed_extensions=IMAGE_EXTS,
    )

    assert prepared is not None
    assert prepared.media_type == "image/jpeg"
    assert prepared.body == payload


async def test_svg_bytes_disguised_as_a_jpeg_are_rejected():
    upload = UploadFile(
        filename="progress.jpg",
        file=io.BytesIO(b"<svg><script>alert(1)</script></svg>"),
        headers={"content-type": "image/svg+xml"},
    )

    with pytest.raises(HTTPException) as rejected:
        await prepare_medical_document(upload, allowed_extensions=IMAGE_EXTS)
    assert rejected.value.status_code == 415


def test_verified_reader_streams_the_descriptor_it_hashed(tmp_path):
    storage_ref = care_attachment_storage_ref(".pdf")
    original = b"%PDF-1.7\noriginal\n%%EOF\n"
    replacement = b"%PDF-1.7\nreplaced\n%%EOF\n"
    assert len(original) == len(replacement)
    path = write_private_file(str(tmp_path), storage_ref, original)
    verified = open_verified_file(
        storage_backend="private_local",
        storage_ref=storage_ref,
        static_dir=str(tmp_path / "static"),
        private_root=str(tmp_path),
        expected_size=len(original),
        expected_sha256=hashlib.sha256(original).hexdigest(),
    )

    os.unlink(path)
    write_private_file(str(tmp_path), storage_ref, replacement)

    assert b"".join(iter_verified_file(verified, chunk_size=5)) == original


async def test_client_paths_are_removed_from_the_display_filename():
    upload = UploadFile(
        filename=r"C:\fakepath\result.pdf",
        file=io.BytesIO(b"%PDF-1.7\nsynthetic\n%%EOF\n"),
    )

    prepared = await prepare_medical_document(upload)

    assert prepared is not None
    assert prepared.original_filename == "result.pdf"


async def test_an_allowed_extension_with_the_wrong_bytes_is_rejected():
    upload = UploadFile(
        filename="result.pdf",
        file=io.BytesIO(b"<html>not a report</html>"),
    )

    with pytest.raises(HTTPException) as rejected:
        await prepare_medical_document(upload)
    assert rejected.value.status_code == 415
