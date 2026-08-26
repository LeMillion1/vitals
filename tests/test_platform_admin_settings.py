"""Superadmin-only OpenRouter configuration boundaries."""
from __future__ import annotations

import inspect
import json

import pytest
from sqlalchemy import select

from vitals.enums import UserRoleName
from vitals.models.identity import AuditEvent, UserRole
from vitals.services import platform_admin_service
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


def test_platform_mutations_require_recent_authentication():
    from web.deps import require_recent_auth
    from web.routers import settings

    for endpoint in (
        settings.save_ai,
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
    prepared = await platform_admin_service.prepare_platform_admin(
        db_session,
        actor_username=get_web_config().auth_username,
    )
    await db_session.commit()

    with pytest.raises(platform_admin_service.PlatformAdminCapabilityError):
        await platform_admin_service.record_openrouter_configuration_change(
            db_session,
            prepared=prepared,
            changed_fields=("digest_model",),
        )
