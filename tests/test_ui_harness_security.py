"""The live-browser harness must be synthetic and network isolated."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.ui.conftest import _environment


def test_ui_subprocess_environment_does_not_inherit_application_secrets(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("VITALS_GARMIN_PASSWORD", "real-password-must-not-leak")
    monkeypatch.setenv("VITALS_OIDC_CLIENT_SECRET", "real-oidc-must-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "real-cloud-secret-must-not-leak")
    monkeypatch.setenv("HTTPS_PROXY", "http://real-proxy.example")

    environment = _environment(tmp_path / "ui.db")

    assert "VITALS_GARMIN_PASSWORD" not in environment
    assert "VITALS_OIDC_CLIENT_SECRET" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "HTTPS_PROXY" not in environment
    assert environment["PYTHON_DOTENV_DISABLED"] == "1"
    assert environment["VITALS_WEB_PUSH_ENABLED"] == "false"
    assert environment["VITALS_REGISTRATION_UNLOCKED"] == "0"
    assert environment["VITALS_DATABASE_URL"].endswith("/ui.db")


def test_ui_server_installs_explicit_no_network_and_no_job_guards(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket; import tests.ui._serve; "
            "from vitals.scheduler import jobs, scheduler; "
            "jobs.register_all_jobs(); assert not scheduler._registry; "
            "socket.getaddrinfo('localhost', 80); "
            "\ntry: socket.getaddrinfo('provider.example', 443)\n"
            "except OSError: pass\n"
            "else: raise AssertionError('external DNS was allowed')",
        ],
        cwd=repository,
        env=_environment(tmp_path / "ui.db"),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert check.returncode == 0, check.stdout + check.stderr
