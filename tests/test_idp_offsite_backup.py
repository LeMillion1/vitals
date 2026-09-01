"""Fail-closed contracts for the isolated ZITADEL offsite stream."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "idp_offsite_backup.sh"
STAMP = "20260826T070000Z"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    backup_dir = tmp_path / "idp"
    state_dir = tmp_path / "state"
    secret_dir = tmp_path / "secrets"
    fake_bin = tmp_path / "bin"
    for directory in (backup_dir, state_dir, secret_dir, fake_bin):
        directory.mkdir()

    dump = backup_dir / f"zitadel_{STAMP}.sql.gz"
    dump.write_bytes(b"synthetic identity database")
    bootstrap = backup_dir / f"zitadel_login_client_{STAMP}.pat"
    bootstrap.write_bytes(b"synthetic login-client credential")
    manifest = backup_dir / f"zitadel_bundle_{STAMP}.sha256"
    manifest.write_text(
        f"{hashlib.sha256(dump.read_bytes()).hexdigest()}  {dump.name}\n"
        f"{hashlib.sha256(bootstrap.read_bytes()).hexdigest()}  {bootstrap.name}\n",
        encoding="utf-8",
    )

    repository = secret_dir / "repository"
    password = secret_dir / "password"
    access_key = secret_dir / "s3-access-key"
    secret_key = secret_dir / "s3-secret-key"
    repository.write_text("s3:https://idp.invalid/repository\n", encoding="utf-8")
    password.write_text("synthetic-idp-restic-password\n", encoding="utf-8")
    access_key.write_text("synthetic-idp-access-key\n", encoding="utf-8")
    secret_key.write_text("synthetic-idp-secret-key\n", encoding="utf-8")

    _write_executable(
        fake_bin / "restic",
        """
printf '%s\n' "$*" >> "$FAKE_RESTIC_LOG"
case "$1" in
    snapshots)
        printf '%s\n' "${FAKE_RESTIC_SNAPSHOTS_JSON:-[]}"
        exit "${FAKE_RESTIC_PREFLIGHT_STATUS:-0}"
        ;;
    backup) exit "${FAKE_RESTIC_BACKUP_STATUS:-0}" ;;
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
        "VITALS_IDP_RESTIC_S3_ACCESS_KEY_FILE": str(access_key),
        "VITALS_IDP_RESTIC_S3_SECRET_KEY_FILE": str(secret_key),
        "VITALS_IDP_BACKUP_DIR": str(backup_dir),
        "VITALS_IDP_OFFSITE_STATE_DIR": str(state_dir),
        "VITALS_IDP_OFFSITE_HOSTNAME": "synthetic-identity",
        "VITALS_IDP_OFFSITE_INTERVAL_SECONDS": "60",
        "VITALS_IDP_OFFSITE_RUN_ONCE": "true",
        "FAKE_RESTIC_LOG": str(log),
    }
    return env, state_dir, log, manifest


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_complete_identity_set_is_the_only_replication_payload(tmp_path):
    env, state_dir, log, _ = _fixture(tmp_path)
    excluded_env = tmp_path / "vitals.env"
    excluded_env.write_text("VITALS_SESSION_SECRET=must-not-leak\n", encoding="utf-8")
    excluded_health = tmp_path / "vitals.sql.gz"
    excluded_health.write_bytes(b"health")

    first = _run(env)
    assert first.returncode == 0, first.stderr
    commands = log.read_text(encoding="utf-8").splitlines()
    assert commands[0] == f"snapshots --json --tag vitals-idp-bundle:{STAMP}"
    assert commands[1].startswith(
        "backup --skip-if-unchanged --host synthetic-identity --tag vitals-idp "
    )
    assert f"--tag vitals-idp-bundle:{STAMP}" in commands[1]
    assert f"zitadel_bundle_{STAMP}.sha256" in commands[1]
    assert f"zitadel_{STAMP}.sql.gz" in commands[1]
    assert f"zitadel_login_client_{STAMP}.pat" in commands[1]
    assert str(excluded_env) not in commands[1]
    assert str(excluded_health) not in commands[1]

    marker = state_dir / "last-successful-manifest"
    assert marker.read_text(encoding="utf-8").strip() == (
        f"zitadel_bundle_{STAMP}.sha256"
    )
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600

    env["FAKE_RESTIC_SNAPSHOTS_JSON"] = '[{"id":"existing"}]'
    second = _run(env)
    assert second.returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == [*commands, commands[0]]
    assert "already replicated" in second.stdout


def test_missing_identity_manifest_is_not_a_healthy_wait_state(tmp_path):
    env, state_dir, log, manifest = _fixture(tmp_path)
    manifest.unlink()

    result = _run(env)

    assert result.returncode != 0
    assert "no complete identity recovery set exists" in result.stderr
    assert not log.exists()
    assert not (state_dir / "last-successful-manifest").exists()


@pytest.mark.parametrize(
    "damage",
    ["checksum", "traversal", "absolute", "extra", "digest"],
)
def test_invalid_identity_set_never_reaches_restic(tmp_path, damage):
    env, state_dir, log, manifest = _fixture(tmp_path)
    backup_dir = Path(env["VITALS_IDP_BACKUP_DIR"])
    if damage == "checksum":
        (backup_dir / f"zitadel_{STAMP}.sql.gz").write_bytes(b"changed")
    elif damage == "traversal":
        manifest.write_text(f"{'0' * 64}  ../outside\n", encoding="utf-8")
    elif damage == "absolute":
        manifest.write_text(f"{'0' * 64}  /etc/passwd\n", encoding="utf-8")
    elif damage == "extra":
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f"{'0' * 64}  extra\n",
            encoding="utf-8",
        )
    else:
        manifest.write_text(f"not-a-digest  zitadel_{STAMP}.sql.gz\n", encoding="utf-8")

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
def test_restic_failure_does_not_advance_marker_or_print_secrets(
    tmp_path, failure_env, expected_calls
):
    env, state_dir, log, _ = _fixture(tmp_path)
    env.update(failure_env)

    result = _run(env)

    assert result.returncode != 0
    assert len(log.read_text(encoding="utf-8").splitlines()) == expected_calls
    assert not (state_dir / "last-successful-manifest").exists()
    combined = result.stdout + result.stderr
    for secret in (
        "synthetic-idp-restic-password",
        "synthetic-idp-access-key",
        "synthetic-idp-secret-key",
    ):
        assert secret not in combined


@pytest.mark.parametrize(
    "secret_env",
    [
        "RESTIC_REPOSITORY_FILE",
        "RESTIC_PASSWORD_FILE",
        "VITALS_IDP_RESTIC_S3_ACCESS_KEY_FILE",
        "VITALS_IDP_RESTIC_S3_SECRET_KEY_FILE",
    ],
)
def test_missing_secret_fails_before_repository_access(tmp_path, secret_env):
    env, state_dir, log, _ = _fixture(tmp_path)
    Path(env[secret_env]).unlink()

    result = _run(env)

    assert result.returncode == 2
    assert not log.exists()
    assert not (state_dir / "last-successful-manifest").exists()
