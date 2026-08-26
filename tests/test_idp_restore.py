"""Fail-closed contracts for restoring one complete ZITADEL recovery set."""
from __future__ import annotations

import gzip
import hashlib
import os
from pathlib import Path
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "idp_restore.sh"
STAMP = "20260826T190000Z"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    backup_dir = tmp_path / "backups" / "idp"
    bootstrap_dir = tmp_path / "bootstrap"
    fake_bin = tmp_path / "bin"
    for directory in (backup_dir, bootstrap_dir, fake_bin):
        directory.mkdir(parents=True, exist_ok=True)
    dump = backup_dir / f"zitadel_{STAMP}.sql.gz"
    with gzip.open(dump, "wb") as stream:
        stream.write(b"synthetic identity SQL\n")
    bootstrap = backup_dir / f"zitadel_login_client_{STAMP}.pat"
    bootstrap.write_bytes(b"synthetic-login-client-pat\n")
    manifest = backup_dir / f"zitadel_bundle_{STAMP}.sha256"
    manifest.write_text(
        f"{hashlib.sha256(dump.read_bytes()).hexdigest()}  {dump.name}\n"
        f"{hashlib.sha256(bootstrap.read_bytes()).hexdigest()}  {bootstrap.name}\n",
        encoding="utf-8",
    )
    password = tmp_path / "service-password"
    password.write_text("synthetic-service-password\n", encoding="utf-8")
    psql_log = tmp_path / "psql.log"
    restored = tmp_path / "restored.sql"
    _write_executable(
        fake_bin / "psql",
        """
printf '%s\n' "$*" >> "$FAKE_PSQL_LOG"
case "$*" in
    *"SELECT count"*) printf '%s\n' "${FAKE_TABLE_COUNT:-0}" ;;
    *)
        cat > "$FAKE_RESTORE_OUTPUT"
        if [ -n "${FAKE_MUTATE_PATH:-}" ]; then printf 'changed' >> "$FAKE_MUTATE_PATH"; fi
        exit "${FAKE_RESTORE_STATUS:-0}"
        ;;
esac
""",
    )
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "VITALS_IDP_BACKUP_DIR": str(backup_dir),
        "VITALS_IDP_BOOTSTRAP_DIR": str(bootstrap_dir),
        "VITALS_IDP_RESTORE_MANIFEST": manifest.name,
        "VITALS_IDP_DB_PASSWORD_FILE": str(password),
        "VITALS_IDP_BOOTSTRAP_OWNER": f"{os.getuid()}:{os.getgid()}",
        "FAKE_PSQL_LOG": str(psql_log),
        "FAKE_RESTORE_OUTPUT": str(restored),
    }
    return environment, bootstrap_dir, psql_log, manifest


def _run(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_complete_bundle_restores_atomically_into_empty_targets(tmp_path):
    environment, bootstrap_dir, psql_log, _ = _fixture(tmp_path)

    result = _run(environment)

    assert result.returncode == 0, result.stderr
    commands = psql_log.read_text(encoding="utf-8").splitlines()
    assert len(commands) == 2
    assert "-1" in commands[1]
    restored = Path(environment["FAKE_RESTORE_OUTPUT"])
    assert restored.read_bytes() == b"synthetic identity SQL\n"
    pat = bootstrap_dir / "login-client.pat"
    assert pat.read_bytes() == b"synthetic-login-client-pat\n"
    assert stat.S_IMODE(pat.stat().st_mode) == 0o640
    assert stat.S_IMODE(bootstrap_dir.stat().st_mode) == 0o750


@pytest.mark.parametrize("damage", ["checksum", "missing-pat", "extra"])
def test_invalid_bundle_never_reaches_the_database(tmp_path, damage):
    environment, _, psql_log, manifest = _fixture(tmp_path)
    backup_dir = Path(environment["VITALS_IDP_BACKUP_DIR"])
    if damage == "checksum":
        (backup_dir / f"zitadel_{STAMP}.sql.gz").write_bytes(b"tampered")
    elif damage == "missing-pat":
        (backup_dir / f"zitadel_login_client_{STAMP}.pat").unlink()
    else:
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f"{'0' * 64}  extra\n",
            encoding="utf-8",
        )

    result = _run(environment)

    assert result.returncode != 0
    assert not psql_log.exists()


def test_nonempty_database_or_bootstrap_is_refused(tmp_path):
    environment, bootstrap_dir, psql_log, _ = _fixture(tmp_path)
    environment["FAKE_TABLE_COUNT"] = "1"

    database_result = _run(environment)

    assert database_result.returncode == 2
    assert "database is not empty" in database_result.stderr
    assert len(psql_log.read_text(encoding="utf-8").splitlines()) == 1

    environment["FAKE_TABLE_COUNT"] = "0"
    (bootstrap_dir / "login-client.pat").write_text("existing", encoding="utf-8")
    bootstrap_result = _run(environment)

    assert bootstrap_result.returncode == 2
    assert "bootstrap is not empty" in bootstrap_result.stderr


def test_failed_database_restore_does_not_publish_the_pat(tmp_path):
    environment, bootstrap_dir, _, _ = _fixture(tmp_path)
    environment["FAKE_RESTORE_STATUS"] = "9"

    result = _run(environment)

    assert result.returncode == 1
    assert "atomic identity database restore failed" in result.stderr
    assert not (bootstrap_dir / "login-client.pat").exists()
    assert not (bootstrap_dir / "login-client.pat.tmp").exists()


def test_bundle_changed_during_restore_never_publishes_the_pat(tmp_path):
    environment, bootstrap_dir, _, _ = _fixture(tmp_path)
    environment["FAKE_MUTATE_PATH"] = str(
        Path(environment["VITALS_IDP_BACKUP_DIR"])
        / f"zitadel_login_client_{STAMP}.pat"
    )

    result = _run(environment)

    assert result.returncode == 1
    assert "bundle changed during restore" in result.stderr
    assert not (bootstrap_dir / "login-client.pat").exists()
