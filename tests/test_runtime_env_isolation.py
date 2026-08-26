"""The web process cannot receive PostgreSQL owner authority."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.create_runtime_env import create_runtime_env, migrate_runtime_env
from vitals.runtime_env import (
    PRIVILEGED_ENV_KEYS,
    RuntimeEnvIsolationError,
    validate_runtime_environment,
)
from web.config import get_web_config


def _operator_env(path: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "# synthetic operator file",
                "VITALS_DATABASE_URL=postgresql+asyncpg://runtime:run@db/vitals",
                "VITALS_WORKER_DATABASE_URL=postgresql+asyncpg://worker:run@db/vitals",
                "VITALS_MIGRATION_DATABASE_URL=postgresql+asyncpg://admin:own@db/vitals",
                "VITALS_DB_PASSWORD=owner-password",
                "VITALS_DB_USER=vitals_admin",
                "VITALS_SESSION_SECRET=synthetic-session-secret",
                "VITALS_AUTH_USERNAME=synthetic-owner",
                "VITALS_AUTH_PASSWORD_HASH=synthetic-hash",
                "VITALS_APP_PORT=8000",
                "VITALS_HEVY_API_KEY=synthetic-runtime-key",
                "",
            )
        ),
        encoding="utf-8",
    )


def test_runtime_file_is_allowlisted_private_and_never_overwritten(tmp_path):
    source = tmp_path / ".env"
    destination = tmp_path / ".env.runtime"
    _operator_env(source)

    count = create_runtime_env(source=source, destination=destination)

    content = destination.read_text(encoding="utf-8")
    assert count == 5
    assert "VITALS_DATABASE_URL=" in content
    assert "VITALS_SESSION_SECRET=" in content
    assert "VITALS_HEVY_API_KEY=" in content
    assert "VITALS_MIGRATION_DATABASE_URL" not in content
    assert "VITALS_WORKER_DATABASE_URL" not in content
    assert "VITALS_DB_PASSWORD" not in content
    assert "VITALS_DB_USER" not in content
    assert "VITALS_APP_PORT" not in content
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    validate_runtime_environment(destination, environ={})

    with pytest.raises(RuntimeEnvIsolationError, match="refusing to overwrite"):
        create_runtime_env(source=source, destination=destination)


def test_legacy_runtime_migration_preserves_only_validated_runtime_assignments(
    tmp_path,
):
    legacy = tmp_path / ".env.runtime"
    destination = tmp_path / ".vitals-runtime" / "vitals.env"
    legacy.write_text(
        "# Settings-owned legacy file\n"
        "VITALS_DATABASE_URL=postgresql+asyncpg://runtime:run@db/vitals\n"
        "VITALS_OPENROUTER_API_KEY=synthetic-runtime-key\n",
        encoding="utf-8",
    )

    count = migrate_runtime_env(source=legacy, destination=destination)

    assert count == 2
    assert legacy.is_file()
    assert destination.parent.is_dir()
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert "VITALS_OPENROUTER_API_KEY=synthetic-runtime-key" in (
        destination.read_text(encoding="utf-8")
    )
    with pytest.raises(RuntimeEnvIsolationError, match="refusing to overwrite"):
        migrate_runtime_env(source=legacy, destination=destination)


def test_legacy_runtime_migration_rejects_control_plane_keys(tmp_path):
    legacy = tmp_path / ".env.runtime"
    destination = tmp_path / ".vitals-runtime" / "vitals.env"
    legacy.write_text(
        "VITALS_DATABASE_URL=postgresql+asyncpg://runtime:run@db/vitals\n"
        "VITALS_MIGRATION_DATABASE_URL=postgresql+asyncpg://owner:own@db/vitals\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeEnvIsolationError, match="non-runtime keys"):
        migrate_runtime_env(source=legacy, destination=destination)

    assert not destination.exists()


def test_runtime_creation_rejects_an_existing_non_private_directory(tmp_path):
    source = tmp_path / ".env"
    _operator_env(source)
    runtime_dir = tmp_path / "runtime-config"
    runtime_dir.mkdir(mode=0o755)
    runtime_dir.chmod(0o755)
    destination = runtime_dir / "vitals.env"

    with pytest.raises(RuntimeEnvIsolationError, match="mode 0700"):
        create_runtime_env(source=source, destination=destination)

    assert not destination.exists()


def test_create_runtime_env_cli_defaults_to_private_dedicated_directory(tmp_path):
    source = tmp_path / ".env"
    _operator_env(source)

    result = subprocess.run(
        [sys.executable, str(Path("scripts/create_runtime_env.py").resolve())],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    destination = tmp_path / ".vitals-runtime" / "vitals.env"
    assert destination.is_file()
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_core_dotenv_loader_uses_exact_vitals_env_file(tmp_path):
    runtime = tmp_path / "runtime-config" / "vitals.env"
    runtime.parent.mkdir()
    runtime.write_text(
        "VITALS_DATABASE_URL=sqlite+aiosqlite:///synthetic-runtime.db\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("VITALS_DATABASE_URL", None)
    environment["VITALS_ENV_FILE"] = str(runtime)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from vitals.config import load_config; print(load_config().database_url)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sqlite+aiosqlite:///synthetic-runtime.db"


def test_all_direct_dotenv_loaders_honor_vitals_env_file():
    for relative_path in (
        "vitals/config.py",
        "migrations/env.py",
        "scripts/provision_runtime_db_role.py",
        "scripts/seed_compose_roles.py",
    ):
        source = Path(relative_path).read_text(encoding="utf-8")
        assert "VITALS_ENV_FILE" in source or "runtime_environment_path" in source


@pytest.mark.parametrize(
    "line",
    [
        "VITALS_MIGRATION_DATABASE_URL=postgresql://owner",
        "VITALS_DB_PASSWORD=owner-password",
        "UNKNOWN_CONTROL_PLANE_SECRET=secret",
    ],
)
def test_runtime_file_rejects_every_non_runtime_assignment(tmp_path, line):
    runtime = tmp_path / ".env.runtime"
    runtime.write_text(
        "VITALS_DATABASE_URL=postgresql+asyncpg://runtime:run@db/vitals\n"
        + line
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeEnvIsolationError, match="non-runtime keys"):
        validate_runtime_environment(runtime, environ={})


@pytest.mark.parametrize("key", sorted(PRIVILEGED_ENV_KEYS))
def test_runtime_process_rejects_injected_privileged_keys(tmp_path, key):
    runtime = tmp_path / ".env.runtime"
    runtime.write_text(
        "VITALS_DATABASE_URL=postgresql+asyncpg://runtime:run@db/vitals\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeEnvIsolationError, match=key):
        validate_runtime_environment(runtime, environ={key: "synthetic"})


def test_web_config_enforces_runtime_boundary_when_compose_requires_it(
    tmp_path,
    monkeypatch,
):
    runtime = tmp_path / ".env.runtime"
    runtime.write_text(
        "VITALS_DATABASE_URL=postgresql+asyncpg://runtime:run@db/vitals\n"
        "VITALS_SESSION_SECRET=synthetic-session-secret\n"
        "VITALS_AUTH_USERNAME=synthetic-owner\n"
        "VITALS_AUTH_PASSWORD_HASH=synthetic-hash\n"
        "VITALS_MIGRATION_DATABASE_URL=postgresql://owner\n",
        encoding="utf-8",
    )
    for key in PRIVILEGED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VITALS_RUNTIME_ENV_ISOLATION_REQUIRED", "true")
    monkeypatch.setenv("VITALS_ENV_FILE", str(runtime))
    monkeypatch.setenv("VITALS_SESSION_SECRET", "synthetic-session-secret")
    monkeypatch.setenv("VITALS_AUTH_USERNAME", "synthetic-owner")
    monkeypatch.setenv("VITALS_AUTH_PASSWORD_HASH", "synthetic-hash")

    with pytest.raises(RuntimeError, match="runtime environment isolation failed"):
        get_web_config()


def test_shared_runtime_preflight_rejects_privileged_worker_environment(tmp_path):
    from vitals.runtime_env import require_runtime_environment_isolation

    runtime = tmp_path / ".env.runtime"
    runtime.write_text(
        "VITALS_DATABASE_URL=postgresql+asyncpg://worker:run@db/vitals\n",
        encoding="utf-8",
    )
    environ = {
        "VITALS_RUNTIME_ENV_ISOLATION_REQUIRED": "true",
        "VITALS_ENV_FILE": str(runtime),
        "VITALS_MIGRATION_DATABASE_URL": "postgresql://owner",
    }

    with pytest.raises(RuntimeError, match="VITALS_MIGRATION_DATABASE_URL"):
        require_runtime_environment_isolation(environ)


def test_runtime_allowlist_covers_every_production_app_env_reference():
    """A new runtime setting must make an explicit privilege decision."""

    from vitals.runtime_env import RUNTIME_ENV_KEYS

    referenced: set[str] = set()
    for root in (Path("vitals"), Path("web")):
        for path in root.rglob("*.py"):
            if path == Path("vitals/runtime_env.py"):
                continue
            text = path.read_text(encoding="utf-8")
            referenced.update(re.findall(r"\bVITALS_[A-Z0-9_]+\b", text))
    control_only = {
        "VITALS_ENV_FILE",
        "VITALS_PUSH_COPY__",
        "VITALS_RESTORE_DRILL",
        "VITALS_RUNTIME_ENV_ISOLATION_REQUIRED",
        "VITALS_TESTING",
    }
    assert referenced - RUNTIME_ENV_KEYS - control_only == set()
