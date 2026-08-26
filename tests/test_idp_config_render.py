"""Contracts for the private configuration consumed by distroless ZITADEL."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "idp_render_config.sh"


def test_renderer_writes_owner_only_config_without_printing_secrets(tmp_path):
    service_password = "synthetic-service-password"
    admin_password = "Aa1.synthetic-admin-password"
    service_file = tmp_path / "service-password"
    admin_file = tmp_path / "admin-password"
    service_file.write_text(service_password + "\n", encoding="utf-8")
    admin_file.write_text(admin_password + "\n", encoding="utf-8")
    config_dir = tmp_path / "config"
    environment = {
        **os.environ,
        "VITALS_IDP_DB_SERVICE_PASSWORD_FILE": str(service_file),
        "VITALS_IDP_ADMIN_PASSWORD_FILE": str(admin_file),
        "VITALS_IDP_ADMIN_USERNAME": "operator",
        "VITALS_IDP_ADMIN_EMAIL": "operator@example.test",
        "VITALS_IDP_LOGIN_PAT_EXPIRATION": "2027-08-26T00:00:00Z",
        "VITALS_IDP_CONFIG_DIR": str(config_dir),
        "VITALS_IDP_CONFIG_OWNER": f"{os.getuid()}:{os.getgid()}",
    }

    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert service_password not in result.stdout + result.stderr
    assert admin_password not in result.stdout + result.stderr
    runtime = config_dir / "config.yaml"
    steps = config_dir / "steps.yaml"
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o400
    assert stat.S_IMODE(steps.stat().st_mode) == 0o400
    assert yaml.safe_load(runtime.read_text(encoding="utf-8"))["Database"][
        "postgres"
    ]["DSN"].startswith("postgresql://zitadel:")
    first = yaml.safe_load(steps.read_text(encoding="utf-8"))["FirstInstance"]
    assert first["LoginClientPatPath"] == "/zitadel/bootstrap/login-client.pat"
    assert first["Org"]["Human"]["Password"] == admin_password
    assert first["Org"]["LoginClient"]["Machine"]["Username"] == "login-client"


def test_renderer_refuses_a_yaml_metacharacter_in_email(tmp_path):
    service_file = tmp_path / "service-password"
    admin_file = tmp_path / "admin-password"
    service_file.write_text("synthetic-service-password\n", encoding="utf-8")
    admin_file.write_text("Aa1.synthetic-admin-password\n", encoding="utf-8")
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env={
            **os.environ,
            "VITALS_IDP_DB_SERVICE_PASSWORD_FILE": str(service_file),
            "VITALS_IDP_ADMIN_PASSWORD_FILE": str(admin_file),
            "VITALS_IDP_ADMIN_USERNAME": "operator",
            "VITALS_IDP_ADMIN_EMAIL": "operator:bad@example.test",
            "VITALS_IDP_LOGIN_PAT_EXPIRATION": "2027-08-26T00:00:00Z",
            "VITALS_IDP_CONFIG_DIR": str(tmp_path / "config"),
            "VITALS_IDP_CONFIG_OWNER": f"{os.getuid()}:{os.getgid()}",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "not YAML-safe" in result.stderr
