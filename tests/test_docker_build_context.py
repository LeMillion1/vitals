"""Sensitive local state must never enter a Docker build context."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dockerignore_excludes_every_local_health_and_credential_store():
    ignored = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".env*",
        ".secrets/",
        "*.pem",
        "*.key",
        "oauth_tokens*.json",
        "garmin_session/",
        ".garmin_session/",
        "*garmin*session*",
        "web/static/uploads/",
        "backups/",
        "*.db",
        "*.sqlite",
        "*.sqlite3",
        ".cutover-stamp",
        ".vitals-oidc-cutover-state*",
        ".vitals-deploy*",
        "docker-compose.production.yml",
        "docker-compose.production.yml.before-*",
    } <= ignored
