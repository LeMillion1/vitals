"""Superadmin-only OpenRouter configuration boundaries."""
from __future__ import annotations

import inspect
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from vitals.enums import UserRoleName
from vitals.models.identity import AuditEvent, McpAccessToken, UserRole
from vitals.services.platform import authorization as platform_authorization
from web.config import get_web_config


@pytest.fixture(autouse=True)
def _restore_ai_runtime_after_each_test(monkeypatch):
    """Settings saves are live; synthetic provider values stay test-local."""

    monkeypatch.setenv("VITALS_OPENROUTER_API_KEY", "")
    monkeypatch.setenv("VITALS_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("VITALS_LLM_MODEL_DIGEST", "anthropic/claude-sonnet-4.6")
    monkeypatch.setenv("VITALS_LLM_MODEL_PARSER", "google/gemini-2.5-flash")
    monkeypatch.setenv("VITALS_LLM_MODEL_BRIEF", "")


async def _remove_platform_admin_role(db_session) -> None:
    role = await db_session.scalar(
        select(UserRole).where(
            UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value
        )
    )
    assert role is not None
    await db_session.delete(role)
    await db_session.commit()


async def test_non_admin_cannot_write_or_see_openrouter_configuration(
    auth_client,
    db_session,
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "VITALS_OPENROUTER_API_KEY=synthetic-existing\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    await _remove_platform_admin_role(db_session)

    page = await auth_client.get("/settings")
    assert page.status_code == 200
    assert 'action="/settings/ai"' not in page.text
    assert 'action="/settings/mcp"' not in page.text
    assert 'action="/settings/platform/mcp"' not in page.text
    assert "triggerRestart" not in page.text
    assert "synthetic-existing" not in page.text

    platform = await auth_client.get("/settings/platform")
    assert platform.status_code == 403

    response = await auth_client.post(
        "/settings/ai",
        data={
            "openrouter_api_key": "synthetic-replacement",
            "openrouter_base_url": "https://example.invalid/v1",
            "llm_model_digest": "synthetic/digest",
            "llm_model_parser": "synthetic/parser",
            "llm_model_brief": "synthetic/brief",
        },
    )
    assert response.status_code == 403
    assert env_file.read_text(encoding="utf-8") == (
        "VITALS_OPENROUTER_API_KEY=synthetic-existing\n"
    )
    assert await db_session.scalar(
        select(AuditEvent.id).where(
            AuditEvent.event_type
            == "platform.openrouter.configuration.updated"
        )
    ) is None

    mcp_response = await auth_client.post(
        "/settings/mcp",
        data={"mcp_client_id": "replacement", "mcp_client_secret": "secret"},
    )
    assert mcp_response.status_code == 403
    assert "VITALS_MCP_CLIENT_ID" not in env_file.read_text(encoding="utf-8")


def test_platform_mutations_require_recent_authentication():
    from web.deps import require_recent_auth
    from web.routers import settings

    for endpoint in (
        settings.save_ai,
        settings.save_mcp,
        settings.enable_platform_ai,
        settings.disable_platform_ai,
        settings.configure_platform_ai_quota,
        settings.restart_container,
    ):
        dependencies = {
            parameter.default.dependency
            for parameter in inspect.signature(endpoint).parameters.values()
            if hasattr(parameter.default, "dependency")
        }
        assert require_recent_auth in dependencies, endpoint.__name__


def test_platform_router_keeps_the_exact_public_route_manifest():
    from fastapi.routing import APIRoute

    from web.settings.platform import router

    actual = {
        (route.path, method)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert actual == {
        ("/platform", "GET"),
        ("/platform/ai", "GET"),
        ("/ai", "POST"),
        ("/platform/ai/configuration", "POST"),
        ("/platform/ai/enable", "POST"),
        ("/platform/ai/disable", "POST"),
        ("/platform/ai/quota", "POST"),
        ("/mcp", "POST"),
        ("/platform/mcp", "POST"),
    }


def test_application_includes_each_platform_route_once_under_settings():
    from web.main import app
    from web.routers import settings
    from web.settings import platform

    expected = {
        ("/settings/platform", "GET"),
        ("/settings/platform/ai", "GET"),
        ("/settings/ai", "POST"),
        ("/settings/platform/ai/configuration", "POST"),
        ("/settings/platform/ai/enable", "POST"),
        ("/settings/platform/ai/disable", "POST"),
        ("/settings/platform/ai/quota", "POST"),
        ("/settings/mcp", "POST"),
        ("/settings/platform/mcp", "POST"),
    }
    schema = app.openapi()
    assert sum(
        getattr(route, "original_router", None) is settings.router
        for route in app.routes
    ) == 1
    assert sum(
        getattr(route, "original_router", None) is platform.router
        for route in settings.router.routes
    ) == 1
    actual = {
        (path, method.upper())
        for path, operations in schema["paths"].items()
        for method in operations
        if (path, method.upper()) in expected
    }

    assert actual == expected
    for path, method in expected:
        assert "settings" in schema["paths"][path][method.lower()]["tags"]


async def test_mcp_configuration_lives_on_platform_hub(auth_client):
    personal = await auth_client.get("/settings")
    assert personal.status_code == 200
    assert 'action="/settings/platform/mcp"' not in personal.text

    platform = await auth_client.get("/settings/platform")
    assert platform.status_code == 200
    assert 'action="/settings/platform/mcp"' in platform.text


async def test_changing_mcp_client_id_revokes_live_connectors_and_audits(
    auth_client,
    db_session,
    legacy_owner_roots,
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "VITALS_MCP_CLIENT_ID=old-client\nVITALS_MCP_CLIENT_SECRET=old-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_MCP_CLIENT_ID", "old-client")
    monkeypatch.setenv("VITALS_MCP_CLIENT_SECRET", "old-secret")

    now = datetime.now(timezone.utc)
    connector = McpAccessToken(
        user_id=legacy_owner_roots.user_id,
        subject_id=legacy_owner_roots.subject_id,
        client_id="old-client",
        audience="http://test/mcp",
        issued_at=now,
        expires_at=now + timedelta(days=30),
    )
    db_session.add(connector)
    await db_session.commit()

    response = await auth_client.post(
        "/settings/platform/mcp",
        data={"mcp_client_id": "new-client", "mcp_client_secret": ""},
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings/platform?saved=mcp"
    await db_session.refresh(connector)
    assert connector.revoked_at is not None
    assert "VITALS_MCP_CLIENT_ID=new-client" in env_file.read_text(encoding="utf-8")

    event = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == "platform.mcp.configuration.updated"
        )
    )
    assert event is not None
    assert event.subject_id is None
    assert event.metadata_json == {
        "source_surface": "web.settings",
        "changed_fields": ["client_id"],
        "record_count": 1,
    }
    assert "old-client" not in json.dumps(event.metadata_json)
    assert "new-client" not in json.dumps(event.metadata_json)


async def test_mcp_commit_failure_restores_environment_and_rolls_back(
    auth_client,
    db_session,
    legacy_owner_roots,
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / "mcp-commit-failure.env"
    original = (
        "VITALS_MCP_CLIENT_ID=old-client\n"
        "VITALS_MCP_CLIENT_SECRET=old-secret\n"
    )
    env_file.write_text(original, encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_MCP_CLIENT_ID", "old-client")
    monkeypatch.setenv("VITALS_MCP_CLIENT_SECRET", "old-secret")

    now = datetime.now(timezone.utc)
    connector = McpAccessToken(
        user_id=legacy_owner_roots.user_id,
        subject_id=legacy_owner_roots.subject_id,
        client_id="old-client",
        audience="http://test/mcp",
        issued_at=now,
        expires_at=now + timedelta(days=30),
    )
    db_session.add(connector)
    await db_session.commit()
    monkeypatch.setattr(
        db_session,
        "commit",
        AsyncMock(side_effect=RuntimeError("synthetic MCP commit failure")),
    )

    with pytest.raises(RuntimeError, match="synthetic MCP commit failure"):
        await auth_client.post(
            "/settings/platform/mcp",
            data={"mcp_client_id": "new-client", "mcp_client_secret": "new-secret"},
        )

    assert env_file.read_text(encoding="utf-8") == original
    assert os.environ["VITALS_MCP_CLIENT_ID"] == "old-client"
    assert os.environ["VITALS_MCP_CLIENT_SECRET"] == "old-secret"
    await db_session.refresh(connector)
    assert connector.revoked_at is None
    assert await db_session.scalar(
        select(AuditEvent.id).where(
            AuditEvent.event_type == "platform.mcp.configuration.updated"
        )
    ) is None


async def test_mcp_failed_compensation_disables_runtime_authority(
    auth_client,
    db_session,
    tmp_path,
    monkeypatch,
):
    from web.settings import platform

    env_file = tmp_path / "mcp-compensation-failure.env"
    env_file.write_text(
        "VITALS_MCP_CLIENT_ID=old-client\n"
        "VITALS_MCP_CLIENT_SECRET=old-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_MCP_CLIENT_ID", "old-client")
    monkeypatch.setenv("VITALS_MCP_CLIENT_SECRET", "old-secret")
    real_write_keys = platform.write_keys
    write_count = 0

    def fail_second_write(updates):
        nonlocal write_count
        write_count += 1
        if write_count == 1:
            return real_write_keys(updates)
        raise OSError("synthetic MCP compensation failure")

    monkeypatch.setattr(platform, "write_keys", fail_second_write)
    monkeypatch.setattr(
        db_session,
        "commit",
        AsyncMock(side_effect=RuntimeError("synthetic MCP commit failure")),
    )

    with pytest.raises(
        RuntimeError,
        match="platform MCP configuration could not restore its environment",
    ):
        await auth_client.post(
            "/settings/platform/mcp",
            data={"mcp_client_id": "new-client", "mcp_client_secret": "new-secret"},
        )

    assert write_count == 2
    assert os.environ["VITALS_MCP_CLIENT_ID"] == ""
    assert os.environ["VITALS_MCP_CLIENT_SECRET"] == ""
    assert await db_session.scalar(
        select(AuditEvent.id).where(
            AuditEvent.event_type == "platform.mcp.configuration.updated"
        )
    ) is None


async def test_platform_admin_save_is_value_free_in_audit(
    auth_client,
    db_session,
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "VITALS_OPENROUTER_API_KEY=\n"
        "VITALS_OPENROUTER_BASE_URL=https://openrouter.ai/api/v1/\n"
        "VITALS_LLM_MODEL_DIGEST=synthetic/old-digest\n"
        "VITALS_LLM_MODEL_PARSER=synthetic/old-parser\n"
        "VITALS_LLM_MODEL_BRIEF=synthetic/old-brief\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_OPENROUTER_API_KEY", "")

    response = await auth_client.post(
        "/settings/ai",
        data={
            "openrouter_api_key": "synthetic-secret-value",
            "openrouter_base_url": "https://openrouter.ai/api/v1",
            "llm_model_digest": "synthetic/digest",
            "llm_model_parser": "synthetic/parser",
            "llm_model_brief": "synthetic/brief",
        },
    )
    assert response.status_code == 303

    event = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type
            == "platform.openrouter.configuration.updated"
        )
    )
    assert event is not None
    assert event.subject_id is None
    assert event.actor_user_id is not None
    assert event.resource_type == "platform_integration"
    assert event.resource_id == "openrouter"
    assert set(event.metadata_json["changed_fields"]) == {
        "base_url",
        "brief_model",
        "credential_ref",
        "digest_model",
        "parser_model",
    }
    serialized_audit = json.dumps(event.metadata_json, sort_keys=True)
    for forbidden in (
        "synthetic-secret-value",
        "https://openrouter.ai/api/v1",
        "synthetic/digest",
        "synthetic/parser",
        "synthetic/brief",
    ):
        assert forbidden not in serialized_audit


async def test_platform_admin_capability_expires_with_transaction(
    db_session,
    legacy_owner_roots,
):
    prepared = await platform_authorization.prepare_platform_admin(
        db_session,
        actor_username=get_web_config().auth_username,
    )
    await db_session.commit()

    with pytest.raises(platform_authorization.PlatformAdminCapabilityError):
        await platform_authorization.record_openrouter_configuration_change(
            db_session,
            prepared=prepared,
            changed_fields=("digest_model",),
        )
