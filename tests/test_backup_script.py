"""Executable contracts for the local disaster-recovery backup sidecar."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backup.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_backup(
    tmp_path: Path,
    *,
    pg_dump_status: int = 0,
    gzip_status: int = 0,
    tar_fail_for: str = "",
    create_garmin: bool = True,
    create_private: bool = True,
    create_legacy: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "pg_dump",
        f"printf 'synthetic database dump\\n'\nexit {pg_dump_status}",
    )
    _write_executable(fake_bin / "gzip", f"cat\nexit {gzip_status}")
    _write_executable(
        fake_bin / "tar",
        """
output="$2"
source_dir="$4"
case "${source_dir##*/}" in
    "$FAKE_TAR_FAIL_FOR")
        if [ -n "$FAKE_TAR_FAIL_FOR" ]; then exit 9; fi
        ;;
esac
printf 'archive of %s\\n' "$source_dir" > "$output"
""",
    )

    backup_dir = tmp_path / "backups"
    garmin_dir = tmp_path / "garmin"
    private_dir = tmp_path / "private"
    legacy_dir = tmp_path / "legacy_uploads"
    if create_garmin:
        garmin_dir.mkdir()
        (garmin_dir / "token.json").write_text("synthetic", encoding="utf-8")
    if create_private:
        private_dir.mkdir()
        (private_dir / "medical.bin").write_bytes(b"synthetic")
    if create_legacy:
        legacy_dir.mkdir()
        (legacy_dir / "legacy-medical.bin").write_bytes(b"synthetic")

    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "VITALS_BACKUP_DIR": str(backup_dir),
        "VITALS_GARMIN_SESSION_DIR": str(garmin_dir),
        "VITALS_PRIVATE_FILE_DIR": str(private_dir),
        "VITALS_LEGACY_UPLOAD_DIR": str(legacy_dir),
        "VITALS_BACKUP_RETENTION_DAYS": "7",
        "VITALS_BACKUP_INTERVAL_SECONDS": "60",
        "VITALS_BACKUP_RUN_ONCE": "true",
        "FAKE_TAR_FAIL_FOR": tar_fail_for,
    }
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, backup_dir


def _manifests(backup_dir: Path) -> list[Path]:
    return sorted(backup_dir.glob("vitals_bundle_*.sha256"))


def test_success_creates_one_exact_owner_only_bundle(tmp_path):
    result, backup_dir = _run_backup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    manifest, = _manifests(backup_dir)
    lines = manifest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4

    names = [line.split("  ", 1)[1] for line in lines]
    stamp = manifest.name.removeprefix("vitals_bundle_").removesuffix(".sha256")
    assert names == [
        f"vitals_{stamp}.sql.gz",
        f"garmin_session_{stamp}.tar.gz",
        f"private_files_{stamp}.tar.gz",
        f"legacy_uploads_{stamp}.tar.gz",
    ]
    for line, name in zip(lines, names, strict=True):
        digest, listed_name = line.split("  ", 1)
        artifact = backup_dir / listed_name
        assert digest == hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"pg_dump_status": 7}, "database dump failed"),
        ({"gzip_status": 8}, "database dump failed"),
        ({"tar_fail_for": "garmin"}, "Garmin session archive failed"),
        ({"tar_fail_for": "private"}, "private-file archive failed"),
        ({"tar_fail_for": "legacy_uploads"}, "legacy-upload archive failed"),
        ({"create_garmin": False}, "no Garmin session dir"),
        ({"create_private": False}, "no private file dir"),
        ({"create_legacy": False}, "no legacy upload dir"),
    ],
)
def test_incomplete_cycle_publishes_nothing(tmp_path, kwargs, error):
    result, backup_dir = _run_backup(tmp_path, **kwargs)

    assert result.returncode != 0
    assert error in result.stderr
    assert list(backup_dir.iterdir()) == []


def test_failed_cycle_does_not_rotate_an_old_complete_bundle(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    old_stamp = "20200101T000000Z"
    old_files = [
        backup_dir / f"vitals_{old_stamp}.sql.gz",
        backup_dir / f"garmin_session_{old_stamp}.tar.gz",
        backup_dir / f"private_files_{old_stamp}.tar.gz",
        backup_dir / f"legacy_uploads_{old_stamp}.tar.gz",
        backup_dir / f"vitals_bundle_{old_stamp}.sha256",
    ]
    for old_file in old_files:
        old_file.write_bytes(b"old good recovery point")
        os.utime(old_file, (1, 1))

    result, actual_dir = _run_backup(tmp_path, pg_dump_status=9)

    assert actual_dir == backup_dir
    assert result.returncode != 0
    assert all(path.exists() for path in old_files)


def test_manifest_detects_changed_artifact(tmp_path):
    result, backup_dir = _run_backup(tmp_path)
    assert result.returncode == 0
    manifest, = _manifests(backup_dir)
    artifact = next(backup_dir.glob("vitals_*.sql.gz"))
    artifact.write_bytes(artifact.read_bytes() + b"changed")

    verify = subprocess.run(
        ["sha256sum", "-c", manifest.name],
        cwd=backup_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    assert verify.returncode != 0
