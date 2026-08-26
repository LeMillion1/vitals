"""Executable fail-closed contracts for encrypted off-host replication."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "offsite_backup.sh"
STAMP = "20260826T070000Z"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    backup_dir = tmp_path / "backups"
    state_dir = tmp_path / "state"
    secret_dir = tmp_path / "secrets"
    fake_bin = tmp_path / "bin"
    for directory in (backup_dir, state_dir, secret_dir, fake_bin):
        directory.mkdir()

    names = [
        f"vitals_{STAMP}.sql.gz",
        f"garmin_session_{STAMP}.tar.gz",
        f"private_files_{STAMP}.tar.gz",
        f"legacy_uploads_{STAMP}.tar.gz",
    ]
    for index, name in enumerate(names):
        (backup_dir / name).write_bytes(f"synthetic-{index}".encode())
    manifest = backup_dir / f"vitals_bundle_{STAMP}.sha256"
    manifest.write_text(
        "".join(
            f"{hashlib.sha256((backup_dir / name).read_bytes()).hexdigest()}  {name}\n"
            for name in names
        ),
        encoding="utf-8",
    )

    repository = secret_dir / "repository"
    password = secret_dir / "password"
    access_key = secret_dir / "s3-access-key"
    secret_key = secret_dir / "s3-secret-key"
    environment = tmp_path / "vitals.env"
    repository.write_text("s3:https://example.invalid/vitals\n", encoding="utf-8")
    password.write_text("synthetic-restic-password\n", encoding="utf-8")
    access_key.write_text("synthetic-access-key\n", encoding="utf-8")
    secret_key.write_text("synthetic-secret-key\n", encoding="utf-8")
    environment.write_text("VITALS_SYNTHETIC_ONLY=true\n", encoding="utf-8")

    _write_executable(
        fake_bin / "restic",
        """
printf '%s\\n' "$*" >> "$FAKE_RESTIC_LOG"
case "$1" in
    snapshots) exit "${FAKE_RESTIC_PREFLIGHT_STATUS:-0}" ;;
    backup)
        exit "${FAKE_RESTIC_BACKUP_STATUS:-0}"
        ;;
    *) exit 99 ;;
esac
""",
    )
    log = tmp_path / "restic.log"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "RESTIC_REPOSITORY_FILE": str(repository),
        "RESTIC_PASSWORD_FILE": str(password),
        "VITALS_RESTIC_S3_ACCESS_KEY_FILE": str(access_key),
        "VITALS_RESTIC_S3_SECRET_KEY_FILE": str(secret_key),
        "VITALS_BACKUP_DIR": str(backup_dir),
        "VITALS_OFFSITE_STATE_DIR": str(state_dir),
        "VITALS_OFFSITE_ENV_FILE": str(environment),
        "VITALS_OFFSITE_HOSTNAME": "synthetic-vitals",
        "VITALS_OFFSITE_INTERVAL_SECONDS": "60",
        "VITALS_OFFSITE_RUN_ONCE": "true",
        "FAKE_RESTIC_LOG": str(log),
    }
    return env, state_dir, log


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_complete_set_uses_restic_idempotency_and_advances_marker(tmp_path):
    env, state_dir, log = _fixture(tmp_path)

    first = _run(env)
    assert first.returncode == 0, first.stderr
    marker = state_dir / "last-successful-manifest"
    assert marker.read_text(encoding="utf-8").strip() == (
        f"vitals_bundle_{STAMP}.sha256"
    )
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    commands = log.read_text(encoding="utf-8").splitlines()
    assert commands[0] == "snapshots --json"
    assert commands[1].startswith(
        "backup --skip-if-unchanged --host synthetic-vitals --tag vitals"
    )
    assert f"vitals_bundle_{STAMP}.sha256" in commands[1]
    assert f"vitals_{STAMP}.sql.gz" in commands[1]
    assert f"garmin_session_{STAMP}.tar.gz" in commands[1]
    assert f"private_files_{STAMP}.tar.gz" in commands[1]
    assert f"legacy_uploads_{STAMP}.tar.gz" in commands[1]
    assert env["VITALS_OFFSITE_ENV_FILE"] in commands[1]

    second = _run(env)
    assert second.returncode == 0
    repeated_commands = log.read_text(encoding="utf-8").splitlines()
    assert repeated_commands == [*commands, *commands]


@pytest.mark.parametrize("damage", ["checksum", "traversal", "extra"])
def test_malformed_or_changed_set_never_reaches_restic(tmp_path, damage):
    env, state_dir, log = _fixture(tmp_path)
    backup_dir = Path(env["VITALS_BACKUP_DIR"])
    manifest = backup_dir / f"vitals_bundle_{STAMP}.sha256"
    if damage == "checksum":
        (backup_dir / f"vitals_{STAMP}.sql.gz").write_bytes(b"changed")
    elif damage == "traversal":
        lines = manifest.read_text(encoding="utf-8").splitlines()
        lines[0] = f"{'0' * 64}  ../outside"
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f"{'0' * 64}  extra\n",
            encoding="utf-8",
        )

    result = _run(env)
    assert result.returncode != 0
    assert not log.exists()
    assert not (state_dir / "last-successful-manifest").exists()


@pytest.mark.parametrize(
    ("failure_env", "expected_calls"),
    [
        ({"FAKE_RESTIC_PREFLIGHT_STATUS": "7"}, 1),
        ({"FAKE_RESTIC_BACKUP_STATUS": "8"}, 2),
    ],
)
def test_restic_failure_never_advances_marker(
    tmp_path, failure_env, expected_calls
):
    env, state_dir, log = _fixture(tmp_path)
    env.update(failure_env)

    result = _run(env)

    assert result.returncode != 0
    assert len(log.read_text(encoding="utf-8").splitlines()) == expected_calls
    assert not (state_dir / "last-successful-manifest").exists()
    combined = result.stdout + result.stderr
    assert "synthetic-restic-password" not in combined
    assert "synthetic-access-key" not in combined
    assert "synthetic-secret-key" not in combined


def test_failed_backup_is_retried_and_only_success_advances_marker(tmp_path):
    env, state_dir, log = _fixture(tmp_path)
    env["FAKE_RESTIC_BACKUP_STATUS"] = "8"

    failed = _run(env)
    assert failed.returncode != 0
    assert not (state_dir / "last-successful-manifest").exists()

    env.pop("FAKE_RESTIC_BACKUP_STATUS")
    retried = _run(env)

    assert retried.returncode == 0, retried.stderr
    assert (state_dir / "last-successful-manifest").exists()
    commands = log.read_text(encoding="utf-8").splitlines()
    assert commands[0] == commands[2] == "snapshots --json"
    assert commands[1] == commands[3]
    assert commands[1].startswith(
        "backup --skip-if-unchanged --host synthetic-vitals --tag vitals"
    )


@pytest.mark.parametrize(
    "secret_env",
    [
        "RESTIC_REPOSITORY_FILE",
        "RESTIC_PASSWORD_FILE",
        "VITALS_RESTIC_S3_ACCESS_KEY_FILE",
        "VITALS_RESTIC_S3_SECRET_KEY_FILE",
    ],
)
def test_missing_required_secret_fails_before_repository_access(tmp_path, secret_env):
    env, state_dir, log = _fixture(tmp_path)
    Path(env[secret_env]).unlink()

    result = _run(env)

    assert result.returncode == 2
    assert not log.exists()
    assert not (state_dir / "last-successful-manifest").exists()


@pytest.mark.parametrize(
    "secret_env",
    ["VITALS_RESTIC_S3_ACCESS_KEY_FILE", "VITALS_RESTIC_S3_SECRET_KEY_FILE"],
)
def test_multiline_s3_secret_fails_before_repository_access(tmp_path, secret_env):
    env, state_dir, log = _fixture(tmp_path)
    Path(env[secret_env]).write_text("first\nsecond\n", encoding="utf-8")

    result = _run(env)

    assert result.returncode == 2
    assert not log.exists()
    assert not (state_dir / "last-successful-manifest").exists()
