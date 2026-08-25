from __future__ import annotations

import hashlib
import io
import os
import stat
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from vitals.enums import (
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    Source,
)
from vitals.models.identity import AuditEvent
from vitals.models.tenancy import FileAsset
from vitals.models.weight import ProgressPhoto
from vitals.operations import file_storage_relocation as relocation
from vitals.persistence import file_storage
from vitals.services import file_asset_service
from scripts import relocate_private_files as relocation_cli


async def _legacy_photo(
    session,
    roots,
    *,
    static_dir,
    name: str,
    body: bytes,
) -> tuple[FileAsset, ProgressPhoto, str]:
    storage_ref = f"uploads/{name}.png"
    source = static_dir / "uploads" / f"{name}.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(body)
    asset = await file_asset_service.register_legacy_local(
        session,
        subject_id=roots.subject_id,
        uploaded_by_user_id=roots.user_id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref=storage_ref,
        media_type="image/png",
        size_bytes=len(body),
        content_sha256=hashlib.sha256(body).hexdigest(),
    )
    photo = ProgressPhoto(
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id,
        file_asset_id=asset.id,
        date=date(2026, 8, 25),
        domain="weight",
        source=Source.MANUAL.value,
        file_key=storage_ref,
        note=None,
    )
    session.add(photo)
    await session.commit()
    return asset, photo, str(source)


async def test_relocation_resumes_and_is_idempotent(
    db_session,
    session_factory,
    legacy_owner_roots,
    tmp_path,
):
    static_dir = tmp_path / "static"
    private_root = tmp_path / "private"
    first, first_photo, first_source = await _legacy_photo(
        db_session,
        legacy_owner_roots,
        static_dir=static_dir,
        name="first",
        body=b"first-private-health-bytes",
    )
    second, second_photo, second_source = await _legacy_photo(
        db_session,
        legacy_owner_roots,
        static_dir=static_dir,
        name="second",
        body=b"second-private-health-bytes",
    )
    identities = (
        (first.id, first_photo.id, first_source),
        (second.id, second_photo.id, second_source),
    )

    first_run = await relocation.relocate(
        session_factory,
        static_dir=str(static_dir),
        private_root=str(private_root),
        batch_size=1,
    )
    assert first_run.relocated_assets == 1
    assert first_run.remaining_assets == 1

    second_run = await relocation.relocate(
        session_factory,
        static_dir=str(static_dir),
        private_root=str(private_root),
        batch_size=1,
    )
    assert second_run.relocated_assets == 1
    assert second_run.remaining_assets == 0

    third_run = await relocation.relocate(
        session_factory,
        static_dir=str(static_dir),
        private_root=str(private_root),
        batch_size=1,
    )
    assert third_run.relocated_assets == 0
    assert third_run.remaining_assets == 0

    for asset_id, photo_id, source in identities:
        asset = await db_session.get(FileAsset, asset_id, populate_existing=True)
        photo = await db_session.get(ProgressPhoto, photo_id, populate_existing=True)
        assert asset.storage_backend == FileStorageBackend.PRIVATE_LOCAL.value
        assert asset.status == FileAssetStatus.ACTIVE.value
        assert photo.file_key == asset.storage_ref
        assert os.path.isfile(
            file_storage.private_file_disk_path(
                str(private_root), asset.storage_ref
            )
        )
        # Cleanup is intentionally a later checkpoint; relocation never risks
        # deleting the only copy before/around an ambiguous commit.
        assert os.path.isfile(source)
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == relocation.EVENT_TYPE)
        )
        == 2
    )


async def test_relocation_commit_ambiguity_preserves_both_copies(
    db_session,
    session_factory,
    legacy_owner_roots,
    tmp_path,
    monkeypatch,
):
    static_dir = tmp_path / "static"
    private_root = tmp_path / "private"
    asset, _photo, source = await _legacy_photo(
        db_session,
        legacy_owner_roots,
        static_dir=static_dir,
        name="ambiguous",
        body=b"ambiguous-private-health-bytes",
    )
    asset_id = asset.id
    real_commit = db_session.commit

    async def commit_then_lose_ack():
        await real_commit()
        raise RuntimeError("synthetic lost commit acknowledgement")

    monkeypatch.setattr(db_session, "commit", commit_then_lose_ack)
    with pytest.raises(relocation.FileStorageCommitAmbiguous):
        await relocation.relocate(
            session_factory,
            static_dir=str(static_dir),
            private_root=str(private_root),
            batch_size=1,
        )

    stored = await db_session.get(FileAsset, asset_id, populate_existing=True)
    assert stored.storage_backend == FileStorageBackend.PRIVATE_LOCAL.value
    assert os.path.isfile(source)
    assert os.path.isfile(
        file_storage.private_file_disk_path(str(private_root), stored.storage_ref)
    )


async def test_relocation_precommit_failure_removes_destination(
    db_session,
    session_factory,
    legacy_owner_roots,
    tmp_path,
    monkeypatch,
):
    static_dir = tmp_path / "static"
    private_root = tmp_path / "private"
    asset, _photo, source = await _legacy_photo(
        db_session,
        legacy_owner_roots,
        static_dir=static_dir,
        name="rollback",
        body=b"rollback-private-health-bytes",
    )
    asset_id = asset.id
    real_flush = db_session.flush

    async def flush_then_fail(*args, **kwargs):
        await real_flush(*args, **kwargs)
        raise RuntimeError("synthetic precommit failure")

    monkeypatch.setattr(db_session, "flush", flush_then_fail)
    with pytest.raises(RuntimeError, match="precommit"):
        await relocation.relocate(
            session_factory,
            static_dir=str(static_dir),
            private_root=str(private_root),
            batch_size=1,
        )

    stored = await db_session.get(FileAsset, asset_id, populate_existing=True)
    assert stored.storage_backend == FileStorageBackend.LEGACY_LOCAL.value
    assert os.path.isfile(source)
    assert not [path for path in private_root.rglob("*") if path.is_file()]


def test_private_write_rejects_intermediate_symlink_and_hardens_modes(tmp_path):
    private_root = tmp_path / "private"
    outside = tmp_path / "outside"
    private_root.mkdir(mode=0o755)
    outside.mkdir()
    (private_root / "labs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        file_storage.write_private_file(
            str(private_root), "labs/aa/unsafe.png", b"private"
        )
    assert not list(outside.iterdir())

    (private_root / "labs").unlink()
    path = file_storage.write_private_file(
        str(private_root), "labs/aa/safe.png", b"private"
    )
    assert stat.S_IMODE(os.stat(private_root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(private_root / "labs").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(private_root / "labs" / "aa").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_backend_delete_refuses_intermediate_symlink(tmp_path):
    private_root = tmp_path / "private"
    static_dir = tmp_path / "static"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "safe.png"
    outside_file.write_bytes(b"outside")
    path = file_storage.write_private_file(
        str(private_root), "labs/aa/safe.png", b"private"
    )
    real_labs = private_root / "real-labs"
    (private_root / "labs").rename(real_labs)
    (private_root / "labs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        file_storage.remove_stored_file(
            storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
            storage_ref="labs/aa/safe.png",
            static_dir=str(static_dir),
            private_root=str(private_root),
        )

    assert outside_file.read_bytes() == b"outside"
    assert os.path.isfile(real_labs / "aa" / "safe.png")
    assert path.endswith("labs/aa/safe.png")


def test_copy_detects_null_hash_source_mutation(tmp_path, monkeypatch):
    static_dir = tmp_path / "static"
    source = static_dir / "uploads" / "mutable.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"mutable-private-health-bytes")
    private_root = tmp_path / "private"
    real_fstat = file_storage.os.fstat
    regular_calls = 0

    def changed_second_regular_fstat(fd):
        nonlocal regular_calls
        result = real_fstat(fd)
        if stat.S_ISREG(result.st_mode):
            regular_calls += 1
            if regular_calls == 2:
                # ``os.stat_result`` tuple lacks nanosecond fields on some
                # platforms, so proxy only the exact attributes the guard reads.
                class Changed:
                    pass

                changed = Changed()
                for name in (
                    "st_mode",
                    "st_dev",
                    "st_ino",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                ):
                    setattr(changed, name, getattr(result, name))
                changed.st_ctime_ns += 1
                return changed
        return result

    monkeypatch.setattr(file_storage.os, "fstat", changed_second_regular_fstat)
    with pytest.raises(ValueError, match="changed while"):
        file_storage.copy_legacy_file_to_private(
            static_dir=str(static_dir),
            legacy_storage_ref="uploads/mutable.png",
            private_root=str(private_root),
            private_storage_ref="uploads/aa/private.png",
            expected_size=None,
            expected_sha256=None,
        )
    assert not [path for path in private_root.rglob("*") if path.is_file()]


def test_publish_directory_fsync_failure_leaves_no_final_or_temp_file(
    tmp_path, monkeypatch
):
    private_root = tmp_path / "private"
    real_fsync = file_storage.os.fsync
    destination = private_root / "labs" / "aa" / "durable.png"
    failed = False

    def fail_publish_fsync_once(fd):
        nonlocal failed
        # Fail only after the final hard link exists. Directory creation and
        # payload fsyncs still succeed; cleanup unlink + directory fsync follow.
        if not failed and stat.S_ISDIR(os.fstat(fd).st_mode) and destination.exists():
            failed = True
            raise OSError("synthetic directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(file_storage.os, "fsync", fail_publish_fsync_once)
    with pytest.raises(OSError, match="directory fsync"):
        file_storage.write_private_file(
            str(private_root), "labs/aa/durable.png", b"private"
        )
    assert not [path for path in private_root.rglob("*") if path.is_file()]
    assert failed


@pytest.mark.parametrize("failure", ["short", "long", "digest", "nonbinary"])
def test_import_stream_copy_fails_closed_without_publishing(tmp_path, failure):
    body = b"synthetic-portability-health-document"
    expected_size = len(body)
    expected_sha256 = hashlib.sha256(body).hexdigest()
    source_body = body
    source = io.BytesIO(source_body)
    if failure == "short":
        source = io.BytesIO(body[:-1])
    elif failure == "long":
        source = io.BytesIO(body + b"x")
    elif failure == "digest":
        source = io.BytesIO(b"x" * len(body))
    elif failure == "nonbinary":
        source = io.StringIO(body.decode())

    private_root = tmp_path / "private"
    with pytest.raises((TypeError, ValueError)):
        file_storage.copy_stream_to_private(
            source=source,
            private_root=str(private_root),
            private_storage_ref="labs/aa/import.pdf",
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    assert not [path for path in private_root.rglob("*") if path.is_file()]
    assert not source.closed


def test_import_stream_copy_is_bounded_durable_and_never_overwrites(tmp_path):
    body = b"synthetic-portability-health-document"
    source = io.BytesIO(body)
    private_root = tmp_path / "private"
    copied = file_storage.copy_stream_to_private(
        source=source,
        private_root=str(private_root),
        private_storage_ref="labs/aa/import.pdf",
        expected_size=len(body),
        expected_sha256=hashlib.sha256(body).hexdigest(),
    )

    assert Path(copied.path).read_bytes() == body
    assert copied.byte_size == len(body)
    assert copied.sha256_hex == hashlib.sha256(body).hexdigest()
    assert stat.S_IMODE(os.stat(copied.path).st_mode) == 0o600
    assert not source.closed

    with pytest.raises(FileExistsError):
        file_storage.copy_stream_to_private(
            source=io.BytesIO(b"replacement"),
            private_root=str(private_root),
            private_storage_ref="labs/aa/import.pdf",
            expected_size=len(b"replacement"),
            expected_sha256=hashlib.sha256(b"replacement").hexdigest(),
        )
    assert Path(copied.path).read_bytes() == body


def test_operator_cli_defaults_to_read_only_and_cannot_delete_legacy_bytes():
    parser = relocation_cli.build_parser()
    args = parser.parse_args([])
    assert args.apply is False
    assert args.batch_size == relocation.DEFAULT_BATCH_SIZE
    with pytest.raises(relocation_cli._SafeArgumentError):
        parser.parse_args(["--delete-legacy"])


def test_backup_contract_includes_private_file_volume():
    repository = Path(__file__).resolve().parents[1]
    compose = (repository / "docker-compose.yml").read_text(encoding="utf-8")
    backup_script = (repository / "scripts" / "backup.sh").read_text(
        encoding="utf-8"
    )
    assert "vitals_private_files:/private_files:ro" in compose
    assert 'private_files="$BACKUP_DIR/private_files_${ts}.tar.gz"' in backup_script
    assert 'tar -czf "$private_files.tmp" -C "$PRIVATE_FILE_DIR" .' in backup_script
    assert 'PRIVATE_FILE_DIR="${VITALS_PRIVATE_FILE_DIR:-/private_files}"' in backup_script
