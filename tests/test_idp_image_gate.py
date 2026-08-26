"""Executable approval gate for the pinned production-like ZITADEL stack."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.yml"
CHECK = ROOT / "scripts" / "idp_config_check.sh"
API_IMAGE = (
    "ghcr.io/zitadel/zitadel:v4.16.2@sha256:"
    "4b68a2106f60baa2895e5a00a77fcd915d29d0db3f0c011d3eb9f99f557b2b48"
)
LOGIN_IMAGE = (
    "ghcr.io/zitadel/zitadel-login:v4.16.2@sha256:"
    "f35e20cf3edd4a45a44c548b887c58304b350ed56078546735015f5cd17eef75"
)
GATEWAY_IMAGE = (
    "caddy:2.10.2-alpine@sha256:"
    "4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d"
)


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _run_preflight(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    secrets = {
        "masterkey": "0123456789abcdef0123456789abcdef",
        "db_admin": "synthetic-admin-password",
        "db_service": "synthetic-service-password",
        "db_backup": "synthetic-backup-password",
        "admin": "Aa1.synthetic-human-password",
    }
    for name, value in secrets.items():
        suffix = "" if name == "masterkey" else "\n"
        (tmp_path / name).write_text(value + suffix, encoding="utf-8")
    environment = {
        **os.environ,
        "VITALS_IDP_MASTERKEY_FILE": str(tmp_path / "masterkey"),
        "VITALS_IDP_DB_ADMIN_PASSWORD_FILE": str(tmp_path / "db_admin"),
        "VITALS_IDP_DB_SERVICE_PASSWORD_FILE": str(tmp_path / "db_service"),
        "VITALS_IDP_DB_BACKUP_PASSWORD_FILE": str(tmp_path / "db_backup"),
        "VITALS_IDP_ADMIN_PASSWORD_FILE": str(tmp_path / "admin"),
        "VITALS_IDP_DOMAIN": "idp.example.test",
        "VITALS_IDP_PUBLIC_SCHEME": "https",
        "VITALS_IDP_SECURE": "true",
        "VITALS_IDP_EXTERNAL_PORT": "443",
        "VITALS_IDP_PUBLIC_AUTHORITY": "idp.example.test",
        "VITALS_IDP_ORIGIN_PORT": "18080",
        "VITALS_IDP_ADMIN_USERNAME": "operator",
        "VITALS_IDP_ADMIN_EMAIL": "operator@example.test",
        "VITALS_IDP_LOGIN_PAT_EXPIRATION": "2027-08-26T00:00:00Z",
        **overrides,
    }
    return subprocess.run(
        ["/bin/sh", str(CHECK)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_identity_profile_pins_reviewed_api_login_and_gateway_images():
    source = COMPOSE_PATH.read_text(encoding="utf-8")
    compose = _compose()

    assert compose["services"]["vitals_idp"]["image"] == API_IMAGE
    assert compose["services"]["vitals_idp_init"]["image"] == API_IMAGE
    assert compose["services"]["vitals_idp_setup"]["image"] == API_IMAGE
    assert compose["services"]["vitals_idp_login"]["image"] == LOGIN_IMAGE
    assert compose["services"]["vitals_idp_gateway"]["image"] == GATEWAY_IMAGE
    assert compose["services"]["vitals_idp_public_gateway"]["image"] == (
        GATEWAY_IMAGE
    )
    assert "v0.0.0" not in source
    assert "${VITALS_IDP_IMAGE" not in source
    assert "docker.sock" not in str(compose["services"]["vitals_idp_gateway"])
    assert "docker.sock" not in str(
        compose["services"]["vitals_idp_public_gateway"]
    )


def test_identity_preflight_accepts_complete_production_configuration(tmp_path):
    result = _run_preflight(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "configuration accepted" in result.stdout


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"VITALS_IDP_PUBLIC_SCHEME": "http"}, "scheme and external-secure"),
        ({"VITALS_IDP_EXTERNAL_PORT": "8080"}, "public HTTPS on port 443"),
        (
            {"VITALS_IDP_PUBLIC_AUTHORITY": "idp.example.test:443"},
            "canonical public authority idp.example.test",
        ),
        ({"VITALS_IDP_DOMAIN": "bad/domain"}, "plain DNS hostname"),
        ({"VITALS_IDP_LOGIN_PAT_EXPIRATION": "never"}, "explicit UTC timestamp"),
        (
            {"VITALS_IDP_LOGIN_PAT_EXPIRATION": "2020-01-01T00:00:00Z"},
            "expiration must be in the future",
        ),
    ],
)
def test_identity_preflight_rejects_unsafe_public_configuration(
    tmp_path, override, message
):
    result = _run_preflight(tmp_path, **override)

    assert result.returncode == 2
    assert message in result.stderr


def test_identity_preflight_rejects_reused_database_passwords(tmp_path):
    duplicate = tmp_path / "duplicate"
    duplicate.write_text("same-synthetic-password-long\n", encoding="utf-8")
    result = _run_preflight(
        tmp_path,
        VITALS_IDP_DB_ADMIN_PASSWORD_FILE=str(duplicate),
        VITALS_IDP_DB_SERVICE_PASSWORD_FILE=str(duplicate),
    )

    assert result.returncode == 2
    assert "passwords must differ" in result.stderr


def test_identity_service_graph_separates_setup_runtime_and_public_gateway():
    services = _compose()["services"]

    assert services["vitals_idp_init"]["command"] == [
        "init",
        "schema",
        "--config",
        "/zitadel/config/config.yaml",
    ]
    assert services["vitals_idp_setup"]["depends_on"] == {
        "vitals_idp_init": {"condition": "service_completed_successfully"},
        "vitals_idp_bootstrap_prepare": {
            "condition": "service_completed_successfully"
        },
        "vitals_idp_masterkey_prepare": {
            "condition": "service_completed_successfully"
        },
    }
    assert services["vitals_idp"]["depends_on"] == {
        "vitals_idp_db_grants": {"condition": "service_completed_successfully"}
    }
    assert services["vitals_idp_login"]["depends_on"] == {
        "vitals_idp": {"condition": "service_healthy"}
    }
    assert services["vitals_idp_gateway"]["depends_on"] == {
        "vitals_idp": {"condition": "service_healthy"},
        "vitals_idp_login": {"condition": "service_healthy"},
    }
    assert services["vitals_idp_gateway"]["cap_drop"] == ["ALL"]
    assert services["vitals_idp_gateway"]["cap_add"] == ["NET_BIND_SERVICE"]
    publishers = {
        name
        for name, service in services.items()
        if name.startswith("vitals_idp") and service.get("ports")
    }
    assert publishers == {
        "vitals_idp_gateway",
        "vitals_idp_public_gateway",
    }
    assert services["vitals_idp_gateway"]["ports"] == [
        "127.0.0.1:${VITALS_IDP_ORIGIN_PORT:-8080}:8080"
    ]


def test_public_identity_gateway_is_explicit_tls_profile_without_secrets():
    compose = _compose()
    service = compose["services"]["vitals_idp_public_gateway"]
    caddyfile = (ROOT / "deploy" / "zitadel" / "Caddy.public").read_text(
        encoding="utf-8"
    )

    assert service["profiles"] == ["idp-public"]
    assert service["depends_on"] == {
        "vitals_idp": {"condition": "service_healthy"},
        "vitals_idp_login": {"condition": "service_healthy"},
    }
    assert service["ports"] == [
        "0.0.0.0:80:80",
        "0.0.0.0:443:443",
    ]
    assert service["environment"] == {
        "VITALS_IDP_DOMAIN": "${VITALS_IDP_DOMAIN-}",
        "VITALS_IDP_PUBLIC_AUTHORITY": "${VITALS_IDP_PUBLIC_AUTHORITY-}",
    }
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["cap_add"] == ["NET_BIND_SERVICE"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["volumes"] == [
        "./deploy/zitadel/Caddy.public:/etc/caddy/Caddyfile:ro",
        "vitals_idp_caddy_data:/data:rw",
    ]
    assert "secrets" not in service
    assert "docker.sock" not in str(service)
    assert service["healthcheck"]["test"] == [
        "CMD",
        "wget",
        "--spider",
        "-q",
        "http://127.0.0.1:8081/debug/ready",
    ]
    assert "{$VITALS_IDP_DOMAIN}" in caddyfile
    assert "@login path /ui/v2/login /ui/v2/login/*" in caddyfile
    assert "reverse_proxy @login http://vitals_idp_login:3000" in caddyfile
    assert "reverse_proxy h2c://vitals_idp:8080" in caddyfile
    assert "header_up -TE" in caddyfile
    assert "header_up Host {$VITALS_IDP_PUBLIC_AUTHORITY}" in caddyfile
    assert "admin off" in caddyfile
    assert "trusted_proxies static" in caddyfile
    assert "trusted_proxies_strict" in caddyfile
    assert "client_ip_headers CF-Connecting-IP X-Forwarded-For" in caddyfile
    assert "vitals_idp_caddy_data" in compose["volumes"]


def test_identity_surfaces_share_the_canonical_public_authority():
    services = _compose()["services"]
    preflight = services["vitals_idp_config_check"]["environment"]
    setup = services["vitals_idp_setup"]["environment"]
    login = services["vitals_idp_login"]["environment"]
    gateway = services["vitals_idp_gateway"]["environment"]

    assert preflight["VITALS_IDP_PUBLIC_AUTHORITY"] == (
        "${VITALS_IDP_PUBLIC_AUTHORITY:-localhost:8080}"
    )
    assert all(
        "${VITALS_IDP_PUBLIC_AUTHORITY:-localhost:8080}" in setup[name]
        for name in (
            "ZITADEL_DEFAULTINSTANCE_FEATURES_LOGINV2_BASEURI",
            "ZITADEL_OIDC_DEFAULTLOGINURLV2",
            "ZITADEL_OIDC_DEFAULTLOGOUTURLV2",
            "ZITADEL_SAML_DEFAULTLOGINURLV2",
        )
    )
    assert login["CUSTOM_REQUEST_HEADERS"].startswith(
        "Host:${VITALS_IDP_PUBLIC_AUTHORITY:-localhost:8080},"
    )
    assert gateway["VITALS_IDP_PUBLIC_AUTHORITY"] == (
        "${VITALS_IDP_PUBLIC_AUTHORITY:-localhost:8080}"
    )
    local_caddy = (ROOT / "deploy" / "zitadel" / "Caddyfile").read_text(
        encoding="utf-8"
    )
    assert local_caddy.count(
        "header_up Host {$VITALS_IDP_PUBLIC_AUTHORITY}"
    ) == 2


def test_identity_gateways_cannot_reach_identity_database_network():
    compose = _compose()
    services = compose["services"]

    assert services["vitals_idp"]["networks"] == [
        "vitals_idp_internal",
        "vitals_idp_proxy",
    ]
    assert services["vitals_idp_login"]["networks"] == ["vitals_idp_proxy"]
    for gateway in ("vitals_idp_gateway", "vitals_idp_public_gateway"):
        assert services[gateway]["networks"] == [
            "vitals_idp_proxy",
            "vitals_idp_edge",
        ]
    assert compose["networks"]["vitals_idp_internal"]["internal"] is True
    assert compose["networks"]["vitals_idp_proxy"]["internal"] is True


def test_identity_master_key_stays_private_but_readable_to_nonroot_zitadel():
    compose = _compose()
    services = compose["services"]
    prepare = services["vitals_idp_masterkey_prepare"]
    setup = services["vitals_idp_setup"]
    runtime = services["vitals_idp"]

    assert prepare["profiles"] == ["idp"]
    assert prepare["depends_on"] == {
        "vitals_idp_config_check": {
            "condition": "service_completed_successfully"
        }
    }
    assert prepare["secrets"] == ["idp_masterkey"]
    assert prepare["volumes"] == [
        "vitals_idp_masterkey:/zitadel/masterkey:rw"
    ]
    assert prepare["network_mode"] == "none"
    assert prepare["read_only"] is True
    assert prepare["cap_drop"] == ["ALL"]
    assert prepare["cap_add"] == ["CHOWN", "DAC_OVERRIDE", "FOWNER"]
    assert prepare["security_opt"] == ["no-new-privileges:true"]
    assert "install -m 400 -o 1000 -g 1000" in prepare["command"][0]

    for service in (setup, runtime):
        assert "secrets" not in service
        assert "vitals_idp_masterkey:/zitadel/masterkey:ro" in service["volumes"]
        assert "/zitadel/masterkey/masterkey" in service["command"]
        assert "/run/secrets/idp_masterkey" not in str(service)

    assert "vitals_idp_masterkey" in compose["volumes"]
