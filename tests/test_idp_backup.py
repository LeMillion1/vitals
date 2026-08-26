"""Executable contracts for the separate ZITADEL recovery stream."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "idp_backup.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_backup(
    tmp_path: Path,
    *,
    pg_dump_status: int = 0,
    gzip_status: int = 0,
    table_count: str = "3",
    retention: str = "7",
    interval: str = "60",
    run_once: str = "true",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "pg_dump",
        f"printf 'synthetic identity database dump\\n'\nexit {pg_dump_status}",
    )
    _write_executable(fake_bin / "gzip", f"cat\nexit {gzip_status}")
    _write_executable(fake_bin / "psql", f"printf '%s\\n' '{table_count}'")

    backup_dir = tmp_path / "idp-backups"
    bootstrap_pat = tmp_path / "login-client.pat"
    bootstrap_pat.write_text("synthetic-login-client-pat\n", encoding="utf-8")
    database_password = tmp_path / "database-password"
    database_password.write_text("synthetic-backup-password\n", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "VITALS_IDP_BACKUP_DIR": str(backup_dir),
        "VITALS_IDP_BACKUP_RETENTION_DAYS": retention,
        "VITALS_IDP_BACKUP_INTERVAL_SECONDS": interval,
        "VITALS_IDP_BACKUP_RUN_ONCE": run_once,
        "VITALS_IDP_BOOTSTRAP_PAT_FILE": str(bootstrap_pat),
        "VITALS_IDP_DB_PASSWORD_FILE": str(database_password),
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
    return sorted(backup_dir.glob("zitadel_bundle_*.sha256"))


def test_success_creates_one_exact_owner_only_identity_bundle(tmp_path):
    result, backup_dir = _run_backup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    (manifest,) = _manifests(backup_dir)
    lines = manifest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    stamp = manifest.name.removeprefix("zitadel_bundle_").removesuffix(".sha256")
    expected_names = [
        f"zitadel_{stamp}.sql.gz",
        f"zitadel_login_client_{stamp}.pat",
    ]
    assert [line.split("  ", 1)[1] for line in lines] == expected_names
    for line in lines:
        digest, name = line.split("  ", 1)
        artifact = backup_dir / name
        assert digest == hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"pg_dump_status": 7}, "database dump failed"),
        ({"gzip_status": 8}, "database dump failed"),
    ],
)
def test_incomplete_identity_cycle_publishes_nothing(tmp_path, kwargs, error):
    result, backup_dir = _run_backup(tmp_path, **kwargs)

    assert result.returncode != 0
    assert error in result.stderr
    assert list(backup_dir.iterdir()) == []


def test_failed_identity_cycle_does_not_rotate_an_old_complete_bundle(tmp_path):
    backup_dir = tmp_path / "idp-backups"
    backup_dir.mkdir()
    old_stamp = "20200101T000000Z"
    old_files = [
        backup_dir / f"zitadel_{old_stamp}.sql.gz",
        backup_dir / f"zitadel_bundle_{old_stamp}.sha256",
    ]
    for old_file in old_files:
        old_file.write_bytes(b"old good identity recovery point")
        os.utime(old_file, (1, 1))

    result, actual_dir = _run_backup(tmp_path, pg_dump_status=9)

    assert actual_dir == backup_dir
    assert result.returncode != 0
    assert all(path.exists() for path in old_files)


def test_successful_identity_cycle_rotates_only_expired_exact_bundles(tmp_path):
    backup_dir = tmp_path / "idp-backups"
    backup_dir.mkdir()
    old_stamp = "20200101T000000Z"
    old_dump = backup_dir / f"zitadel_{old_stamp}.sql.gz"
    old_manifest = backup_dir / f"zitadel_bundle_{old_stamp}.sha256"
    preserved = [
        backup_dir / "vitals_bundle_20200101T000000Z.sha256",
        backup_dir / "zitadel_bundle_malformed.sha256",
        backup_dir / "operator-note.txt",
    ]
    for path in (old_dump, old_manifest, *preserved):
        path.write_bytes(b"synthetic old artifact")
        os.utime(path, (1, 1))

    result, actual_dir = _run_backup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert actual_dir == backup_dir
    assert not old_dump.exists()
    assert not old_manifest.exists()
    assert all(path.exists() for path in preserved)
    assert len(_manifests(backup_dir)) == 2  # new exact + preserved malformed


def test_empty_identity_database_is_refused_before_dump(tmp_path):
    result, backup_dir = _run_backup(tmp_path, table_count="0")

    assert result.returncode != 0
    assert "refusing an empty identity database" in result.stderr
    assert list(backup_dir.iterdir()) == []


def test_missing_login_pat_fails_without_publishing_a_bundle(tmp_path):
    bootstrap_pat = tmp_path / "missing-login-client.pat"
    result, backup_dir = _run_backup(tmp_path)
    assert result.returncode == 0
    for path in backup_dir.iterdir():
        path.unlink()

    env = {
        **os.environ,
        "VITALS_IDP_BACKUP_DIR": str(backup_dir),
        "VITALS_IDP_BACKUP_RUN_ONCE": "true",
        "VITALS_IDP_BACKUP_INTERVAL_SECONDS": "60",
        "VITALS_IDP_BOOTSTRAP_PAT_FILE": str(bootstrap_pat),
        "VITALS_IDP_DB_PASSWORD_FILE": str(tmp_path / "database-password"),
    }
    failed = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert failed.returncode != 0
    assert "bootstrap PAT is missing or invalid" in failed.stderr
    assert list(backup_dir.iterdir()) == []


@pytest.mark.parametrize(
    "invalid",
    [
        {"retention": "0"},
        {"retention": "seven"},
        {"interval": "0"},
        {"interval": "daily"},
        {"run_once": "yes"},
    ],
)
def test_invalid_schedule_configuration_fails_before_backup(tmp_path, invalid):
    result, backup_dir = _run_backup(tmp_path, **invalid)

    assert result.returncode == 2
    assert not backup_dir.exists()


def test_identity_manifest_detects_changed_artifact(tmp_path):
    result, backup_dir = _run_backup(tmp_path)
    assert result.returncode == 0
    (manifest,) = _manifests(backup_dir)
    artifact = next(backup_dir.glob("zitadel_*.sql.gz"))
    artifact.write_bytes(artifact.read_bytes() + b"changed")

    verify = subprocess.run(
        ["sha256sum", "-c", manifest.name],
        cwd=backup_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    assert verify.returncode != 0
