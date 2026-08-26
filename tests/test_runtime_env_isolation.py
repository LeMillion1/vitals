"""The web process cannot receive PostgreSQL owner authority."""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

from scripts.create_runtime_env import create_runtime_env
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
    assert "VITALS_DB_PASSWORD" not in content
    assert "VITALS_DB_USER" not in content
    assert "VITALS_APP_PORT" not in content
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    validate_runtime_environment(destination, environ={})

    with pytest.raises(RuntimeEnvIsolationError, match="refusing to overwrite"):
        create_runtime_env(source=source, destination=destination)


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
