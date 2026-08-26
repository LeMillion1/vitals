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


def test_schema_migration_and_runtime_roles_are_separate_startup_steps():
    compose = _compose()
    migrate = compose["services"]["vitals_migrate"]
    roles = compose["services"]["vitals_db_roles"]
    app = compose["services"]["vitals_app"]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert migrate["environment"]["VITALS_DATABASE_URL"].startswith(
        "${VITALS_MIGRATION_DATABASE_URL:"
    )
    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert roles["depends_on"]["vitals_migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert roles["environment"]["VITALS_DATABASE_URL"].startswith(
        "${VITALS_DATABASE_URL:"
    )
    assert roles["environment"]["VITALS_WORKER_DATABASE_URL"].startswith(
        "${VITALS_WORKER_DATABASE_URL:"
    )
    assert roles["command"] == ["python", "scripts/provision_runtime_db_role.py"]
    assert app["depends_on"]["vitals_db_roles"]["condition"] == (
        "service_completed_successfully"
    )
    assert "alembic upgrade" not in dockerfile


def test_app_mounts_only_the_allowlisted_runtime_environment():
    app = _compose()["services"]["vitals_app"]

    runtime_mount = next(
        mount
        for mount in app["volumes"]
        if isinstance(mount, dict) and mount["target"] == "/app/.env"
    )
    assert runtime_mount == {
        "type": "bind",
        "source": "${VITALS_RUNTIME_ENV_FILE:-.env.runtime}",
        "target": "/app/.env",
        "bind": {"create_host_path": False},
    }
    assert app["environment"]["VITALS_RUNTIME_ENV_ISOLATION_REQUIRED"] == "true"
    assert ".env:/app/.env" not in str(app)


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


def test_offsite_backup_is_opt_in_pinned_and_least_privileged():
    compose = _compose()
    service = compose["services"]["vitals_offsite_backup"]

    assert service["profiles"] == ["offsite"]
    assert service["image"] == (
        "ghcr.io/restic/restic:0.19.1@"
        "sha256:2f0373803493361f9304a57150d464677f69a9dad487afec202105aafb2592f2"
    )
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["entrypoint"] == [
        "/bin/sh",
        "/usr/local/bin/offsite_backup.sh",
    ]
    environment = service["environment"]
    assert environment["AWS_EC2_METADATA_DISABLED"] == "true"
    assert "AWS_ACCESS_KEY_ID" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert not {"PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"}.intersection(
        environment
    )
    assert set(service["secrets"]) == {
        "restic_repository",
        "restic_password",
        "restic_s3_access_key",
        "restic_s3_secret_key",
    }
    assert "./backups:/backups:ro" in service["volumes"]
    assert "./.env:/source/vitals.env:ro" in service["volumes"]
    assert {
        "type": "bind",
        "source": "${VITALS_RUNTIME_ENV_FILE:-.env.runtime}",
        "target": "/source/vitals.runtime.env",
        "read_only": True,
        "bind": {"create_host_path": False},
    } in service["volumes"]
    assert service["environment"]["VITALS_OFFSITE_RUNTIME_ENV_FILE"] == (
        "/source/vitals.runtime.env"
    )
    assert not any("docker.sock" in volume for volume in service["volumes"])


def test_idp_backup_waits_for_provider_and_has_only_database_authority():
    compose = _compose()
    service = compose["services"]["vitals_idp_backup"]

    assert service["profiles"] == ["idp"]
    assert service["depends_on"] == {
        "vitals_idp": {"condition": "service_healthy"}
    }
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert set(service["environment"]) == {
        "TZ",
        "PGHOST",
        "PGUSER",
        "PGPASSWORD",
        "PGDATABASE",
        "VITALS_IDP_BACKUP_RETENTION_DAYS",
    }
    source = str(service)
    for forbidden in (
        "VITALS_IDP_MASTERKEY",
        "VITALS_IDP_ADMIN_PASSWORD",
        "VITALS_OIDC_CLIENT_SECRET",
        "VITALS_DB_PASSWORD",
        ".env",
        "docker.sock",
    ):
        assert forbidden not in source


def test_idp_offsite_is_a_separate_secret_and_failure_domain():
    compose = _compose()
    service = compose["services"]["vitals_idp_offsite_backup"]

    assert service["profiles"] == ["idp-offsite"]
    assert "depends_on" not in service
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["entrypoint"] == [
        "/bin/sh",
        "/usr/local/bin/idp_offsite_backup.sh",
    ]
    assert set(service["secrets"]) == {
        "idp_restic_repository",
        "idp_restic_password",
        "idp_restic_s3_access_key",
        "idp_restic_s3_secret_key",
    }
    assert "./backups/idp:/backups/idp:ro" in service["volumes"]
    assert not any(".env" in volume for volume in service["volumes"])
    assert not any("docker.sock" in volume for volume in service["volumes"])
    assert not {"PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"}.intersection(
        service["environment"]
    )

    health_backup = compose["services"]["vitals_backup"]
    health_offsite = compose["services"]["vitals_offsite_backup"]
    assert "vitals_idp" not in str(health_backup)
    assert "vitals_idp" not in str(health_offsite)
