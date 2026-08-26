"""The optional identity profile has no runnable unapproved image."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.yml"
SENTINEL = (
    "ghcr.io/zitadel/zitadel:v0.0.0@sha256:"
    + "0" * 64
)


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _run_preflight() -> subprocess.CompletedProcess[str]:
    command = _compose()["services"]["vitals_idp_config_check"]["command"][0]
    # Compose turns ``$$`` into one container-side dollar. Execute the same
    # script locally without letting the host substitute provider secrets.
    command = command.replace("$$", "$")
    environment = {
        "VITALS_IDP_MASTERKEY": "0123456789abcdef0123456789abcdef",
        "VITALS_IDP_DB_PASSWORD": "synthetic-db-password",
        "VITALS_IDP_ADMIN_PASSWORD": "Aa1!synthetic-admin-password",
    }
    return subprocess.run(
        ["/bin/sh", "-ec", command],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_identity_profile_has_only_a_nonexistent_versioned_sentinel():
    source = COMPOSE_PATH.read_text(encoding="utf-8")
    compose = _compose()
    provider = compose["services"]["vitals_idp"]
    preflight = compose["services"]["vitals_idp_config_check"]

    assert provider["image"] == SENTINEL
    assert "VITALS_IDP_IMAGE" not in preflight["environment"]
    assert "v2.66.0" not in source
    assert "${VITALS_IDP_IMAGE" not in source


def test_identity_preflight_refuses_even_with_all_secrets_configured():
    result = _run_preflight()

    assert result.returncode != 0
    assert "no provider image/configuration is approved" in result.stderr
