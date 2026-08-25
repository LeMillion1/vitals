"""Deployment contracts that keep parallel Compose projects independent."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_compose_resources_remain_project_scoped():
    compose = _compose()

    assert "name" not in compose
    assert all("container_name" not in service for service in compose["services"].values())
    assert all(
        not isinstance(volume, dict) or not {"name", "external"}.intersection(volume)
        for volume in compose["volumes"].values()
    )


def test_app_has_one_configurable_loopback_mapping():
    compose = _compose()
    ports = compose["services"]["vitals_app"]["ports"]

    assert ports == ["127.0.0.1:${VITALS_APP_PORT:-8000}:8000"]
    assert "127.0.0.1:8000:8000" not in ports


def test_configurable_app_port_is_documented_in_both_operator_languages():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "# VITALS_APP_PORT=8000" in example
    assert readme.count("VITALS_APP_PORT=8001") == 2


def test_operator_commands_address_the_compose_service():
    dependency_snapshot = (ROOT / "docs" / "known-good-deps.txt").read_text(
        encoding="utf-8"
    )
    backfill = (ROOT / "scripts" / "backfill_garmin_reparse.py").read_text(
        encoding="utf-8"
    )

    assert "docker exec vitals_app" not in dependency_snapshot + backfill
    assert dependency_snapshot.count("docker compose exec vitals_app") == 1
    assert backfill.count("docker compose exec vitals_app") == 3
