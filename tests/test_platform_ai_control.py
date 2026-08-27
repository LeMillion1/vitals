"""No-PHI web and service boundaries for the platform-funded AI gateway."""
from __future__ import annotations

import json
from dataclasses import fields
from datetime import date
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from vitals.enums import IntegrationConnectionStatus, IntegrationProvider, UserRoleName
from vitals.models.ai import AIPlatformQuotaPeriod, AISubjectQuotaPeriod
from vitals.models.identity import AuditEvent, HealthSubject, UserRole
from vitals.models.tenancy import IntegrationConnection, PlatformIntegrationConnection
from vitals.services.platform import ai_control as platform_ai_control
from vitals.services.platform import authorization as platform_authorization
from web.config import get_web_config


@pytest.fixture(autouse=True)
def _restore_ai_runtime_after_each_test(monkeypatch):
    """Route saves are intentionally live; synthetic values must not escape tests."""

    monkeypatch.setenv("VITALS_OPENROUTER_API_KEY", "")
    monkeypatch.setenv("VITALS_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("VITALS_LLM_MODEL_DIGEST", "anthropic/claude-sonnet-4.6")
    monkeypatch.setenv("VITALS_LLM_MODEL_PARSER", "google/gemini-2.5-flash")
    monkeypatch.setenv("VITALS_LLM_MODEL_BRIEF", "")


def _write_ai_env(path, *, key: str = "") -> None:
    path.write_text(
        "\n".join(
            (
                f"VITALS_OPENROUTER_API_KEY={key}",
                "VITALS_OPENROUTER_BASE_URL=https://openrouter.ai/api/v1",
                "VITALS_LLM_MODEL_DIGEST=anthropic/claude-sonnet-4.6",
                "VITALS_LLM_MODEL_PARSER=google/gemini-2.5-flash",
                "VITALS_LLM_MODEL_BRIEF=",
                "",
            )
        ),
        encoding="utf-8",
    )


def _configuration_form(*, key: str = "", digest: str = "anthropic/claude-sonnet-4.6"):
    return {
        "openrouter_api_key": key,
        "openrouter_base_url": "https://openrouter.ai/api/v1",
        "llm_model_digest": digest,
        "llm_model_parser": "google/gemini-2.5-flash",
        "llm_model_brief": "",
    }


async def _prepared_admin(session):
    return await platform_authorization.prepare_platform_admin(
        session,
        actor_username=get_web_config().auth_username,
    )


async def test_control_service_snapshot_is_redacted_and_quota_is_opaque_s_only(
    db_session,
    legacy_owner_roots,
):
    subject = await db_session.get(HealthSubject, legacy_owner_roots.subject_id)
    assert subject is not None
    subject.display_name = "Synthetic Sensitive Profile Name"
    subject.timezone = "Pacific/Honolulu"
    await db_session.commit()

    prepared = await _prepared_admin(db_session)
    transition = await platform_ai_control.apply_gateway_configuration(
        db_session,
        prepared=prepared,
        configuration_changed=False,
        credential_available=True,
    )
    assert transition.action is platform_ai_control.GatewayTransitionAction.CREATED
    quota = await platform_ai_control.configure_aligned_quota_period(
        db_session,
        prepared=prepared,
        subject_id=legacy_owner_roots.subject_id,
        period_start=date(2026, 8, 1),
        period_end=date(2026, 9, 1),
        platform_cost_limit_microunits=10_000,
        platform_unit_limit=20_000,
        subject_cost_limit_microunits=5_000,
        subject_unit_limit=10_000,
    )
    assert quota.changed is True
    await db_session.commit()

    prepared = await _prepared_admin(db_session)
    snapshot = await platform_ai_control.get_platform_ai_control_snapshot(
        db_session,
        prepared=prepared,
    )
    assert snapshot.eligible_subject_ids == (legacy_owner_roots.subject_id,)
    assert snapshot.subject_periods[0].subject_id == legacy_owner_roots.subject_id
    assert {field.name for field in fields(snapshot)} == {
        "gateway",
        "eligible_subject_ids",
        "platform_periods",
        "subject_periods",
    }
    serialized = repr(snapshot)
    assert "Synthetic Sensitive Profile Name" not in serialized
    assert "Pacific/Honolulu" not in serialized
    assert "env:VITALS_OPENROUTER_API_KEY" not in serialized


async def test_configuration_create_noop_disabled_rotation_and_enable(
    auth_client,
    db_session,
    legacy_owner_roots,
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / "platform-ai.env"
    _write_ai_env(env_file)
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_OPENROUTER_API_KEY", "")

    legacy_connection_count = await db_session.scalar(
        select(func.count())
        .select_from(IntegrationConnection)
        .where(IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value)
    )

    created = await auth_client.post(
        "/settings/platform/ai/configuration",
        data=_configuration_form(key="synthetic-control-secret"),
    )
    assert created.status_code == 303
    root = await db_session.scalar(
        select(PlatformIntegrationConnection).where(
            PlatformIntegrationConnection.status
            != IntegrationConnectionStatus.RETIRED.value
        )
    )
    assert root is not None
    assert root.status == IntegrationConnectionStatus.ACTIVE.value
    assert root.config_version == 1
    assert root.credential_ref == platform_ai_control.OPENROUTER_CREDENTIAL_REF
    assert "synthetic-control-secret" not in repr(root.__dict__)

    audit_count = await db_session.scalar(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == "platform.openrouter.configuration.updated"
        )
    )
    no_op = await auth_client.post(
        "/settings/platform/ai/configuration",
        data=_configuration_form(),
    )
    assert no_op.status_code == 303
    assert await db_session.scalar(
        select(func.count()).select_from(PlatformIntegrationConnection)
    ) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == "platform.openrouter.configuration.updated"
        )
    ) == audit_count

    disabled = await auth_client.post("/settings/platform/ai/disable")
    assert disabled.status_code == 303
    changed_while_disabled = await auth_client.post(
        "/settings/platform/ai/configuration",
        data=_configuration_form(digest="synthetic/digest-v2"),
    )
    assert changed_while_disabled.status_code == 303
    roots = list(
        await db_session.scalars(
            select(PlatformIntegrationConnection).order_by(
                PlatformIntegrationConnection.config_version
            )
        )
    )
    assert [(item.config_version, item.status) for item in roots] == [
        (1, IntegrationConnectionStatus.RETIRED.value),
        (2, IntegrationConnectionStatus.DISABLED.value),
    ]

    enabled = await auth_client.post("/settings/platform/ai/enable")
    assert enabled.status_code == 303
    current = await db_session.scalar(
        select(PlatformIntegrationConnection).where(
            PlatformIntegrationConnection.status
            != IntegrationConnectionStatus.RETIRED.value
        )
    )
    assert current is not None
    assert (current.config_version, current.status) == (
        3,
        IntegrationConnectionStatus.ACTIVE.value,
    )
    assert await db_session.scalar(
        select(func.count())
        .select_from(IntegrationConnection)
        .where(IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value)
    ) == legacy_connection_count


async def test_control_page_and_aligned_quota_never_render_subject_profile_or_secret(
    auth_client,
    db_session,
    legacy_owner_roots,
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / "platform-ai.env"
    _write_ai_env(env_file, key="synthetic-never-render")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    subject = await db_session.get(HealthSubject, legacy_owner_roots.subject_id)
    assert subject is not None
    subject.display_name = "Synthetic Private Display Name"
    await db_session.commit()

    response = await auth_client.get("/settings/platform/ai")
    assert response.status_code == 200
    assert str(legacy_owner_roots.subject_id) in response.text
    assert "Synthetic Private Display Name" not in response.text
    assert "synthetic-never-render" not in response.text
    assert 'name="subject_id"' in response.text

    quota = await auth_client.post(
        "/settings/platform/ai/quota",
        data={
            "subject_id": str(legacy_owner_roots.subject_id),
            "period_start": "2026-08-01",
            "period_end": "2026-09-01",
            "platform_cost_limit_microunits": "10000",
            "platform_unit_limit": "20000",
            "subject_cost_limit_microunits": "5000",
            "subject_unit_limit": "10000",
        },
    )
    assert quota.status_code == 303
    platform_period = await db_session.get(
        AIPlatformQuotaPeriod,
        (date(2026, 8, 1), date(2026, 9, 1)),
    )
    subject_period = await db_session.get(
        AISubjectQuotaPeriod,
        (legacy_owner_roots.subject_id, date(2026, 8, 1), date(2026, 9, 1)),
    )
    assert platform_period is not None
    assert subject_period is not None
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.metadata_json["result_code"].as_string()
            == "quota_configured"
        )
    )
    assert audit is not None
    assert audit.subject_id is None
    serialized = json.dumps(audit.metadata_json, sort_keys=True)
    assert str(legacy_owner_roots.subject_id) not in serialized
    assert "Synthetic Private Display Name" not in serialized


async def test_ambiguous_configuration_commit_clears_credential_and_rolls_back_if_possible(
    auth_client,
    db_session,
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / "platform-ai.env"
    _write_ai_env(env_file, key="synthetic-previous-secret")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_OPENROUTER_API_KEY", "synthetic-previous-secret")
    monkeypatch.setattr(
        db_session,
        "commit",
        AsyncMock(side_effect=RuntimeError("synthetic commit failure")),
    )

    with pytest.raises(RuntimeError, match="commit outcome is ambiguous"):
        await auth_client.post(
            "/settings/platform/ai/configuration",
            data=_configuration_form(key="synthetic-transient-secret"),
        )

    assert "VITALS_OPENROUTER_API_KEY=\n" in env_file.read_text(encoding="utf-8")
    assert "synthetic-previous-secret" not in env_file.read_text(encoding="utf-8")
    assert "synthetic-transient-secret" not in env_file.read_text(encoding="utf-8")
    assert await db_session.scalar(
        select(func.count()).select_from(PlatformIntegrationConnection)
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == "platform.openrouter.configuration.updated"
        )
    ) == 0


async def test_environment_write_failure_preserves_old_credential_and_rolls_back(
    auth_client,
    db_session,
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / "platform-ai.env"
    _write_ai_env(env_file, key="synthetic-previous-secret")
    original_file = env_file.read_text(encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_OPENROUTER_API_KEY", "synthetic-previous-secret")
    monkeypatch.setattr(
        "web.settings.platform.write_keys",
        lambda _updates: (_ for _ in ()).throw(OSError("synthetic write failure")),
    )

    with pytest.raises(OSError, match="synthetic write failure"):
        await auth_client.post(
            "/settings/platform/ai/configuration",
            data=_configuration_form(key="synthetic-transient-secret"),
        )

    assert env_file.read_text(encoding="utf-8") == original_file
    assert await db_session.scalar(
        select(func.count()).select_from(PlatformIntegrationConnection)
    ) == 0


async def test_service_rejects_unaudited_configuration_rotation_shape(
    db_session,
    legacy_owner_roots,
):
    prepared = await _prepared_admin(db_session)
    await platform_ai_control.apply_gateway_configuration(
        db_session,
        prepared=prepared,
        configuration_changed=False,
        credential_available=True,
    )
    await db_session.commit()

    prepared = await _prepared_admin(db_session)
    with pytest.raises(
        platform_ai_control.PlatformAIControlError,
        match="reviewed changed fields",
    ):
        await platform_ai_control.apply_gateway_configuration(
            db_session,
            prepared=prepared,
            configuration_changed=True,
            credential_available=True,
            changed_fields=frozenset(),
        )
    assert await db_session.scalar(
        select(func.count()).select_from(PlatformIntegrationConnection)
    ) == 1


@pytest.mark.parametrize(
    ("method", "path", "data"),
    (
        ("get", "/settings/platform/ai", None),
        (
            "post",
            "/settings/platform/ai/configuration",
            _configuration_form(key="synthetic-denied"),
        ),
        ("post", "/settings/platform/ai/enable", None),
        ("post", "/settings/platform/ai/disable", None),
        (
            "post",
            "/settings/platform/ai/quota",
            {
                "subject_id": "00000000-0000-0000-0000-000000000001",
                "period_start": "2026-08-01",
                "period_end": "2026-09-01",
                "platform_cost_limit_microunits": "100",
                "platform_unit_limit": "100",
                "subject_cost_limit_microunits": "100",
                "subject_unit_limit": "100",
            },
        ),
    ),
)
async def test_non_admin_cannot_reach_any_platform_ai_control_route(
    auth_client,
    db_session,
    tmp_path,
    monkeypatch,
    method,
    path,
    data,
):
    env_file = tmp_path / "platform-ai.env"
    _write_ai_env(env_file, key="synthetic-existing")
    original_file = env_file.read_text(encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    role = await db_session.scalar(
        select(UserRole).where(UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value)
    )
    assert role is not None
    await db_session.delete(role)
    await db_session.commit()

    if data is None:
        response = await getattr(auth_client, method)(path)
    else:
        response = await getattr(auth_client, method)(path, data=data)
    assert response.status_code == 403
    assert env_file.read_text(encoding="utf-8") == original_file
    assert await db_session.scalar(
        select(func.count()).select_from(PlatformIntegrationConnection)
    ) == 0


async def test_subject_quota_cannot_exceed_platform_limit(
    db_session,
    legacy_owner_roots,
):
    prepared = await _prepared_admin(db_session)
    with pytest.raises(ValueError, match="cannot exceed"):
        await platform_ai_control.configure_aligned_quota_period(
            db_session,
            prepared=prepared,
            subject_id=legacy_owner_roots.subject_id,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 9, 1),
            platform_cost_limit_microunits=99,
            platform_unit_limit=99,
            subject_cost_limit_microunits=100,
            subject_unit_limit=99,
        )
    assert await db_session.scalar(
        select(func.count()).select_from(AIPlatformQuotaPeriod)
    ) == 0


async def test_enable_rejects_unapproved_existing_endpoint_before_creating_root(
    auth_client,
    db_session,
    tmp_path,
    monkeypatch,
):
    env_file = tmp_path / "platform-ai.env"
    _write_ai_env(env_file, key="synthetic-existing")
    content = env_file.read_text(encoding="utf-8").replace(
        "https://openrouter.ai/api/v1",
        "https://example.invalid/api/v1",
    )
    env_file.write_text(content, encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))

    response = await auth_client.post("/settings/platform/ai/enable")

    assert response.status_code == 303
    assert response.headers["location"].endswith("?error=configuration_invalid")
    assert await db_session.scalar(
        select(func.count()).select_from(PlatformIntegrationConnection)
    ) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("openrouter_base_url", "https://example.invalid/api/v1"),
        ("openrouter_base_url", "https://openrouter.ai@evil.invalid/api/v1"),
        ("openrouter_base_url", "https://openrouter.ai/api/v1?key=value"),
        ("llm_model_digest", "model-without-provider"),
        ("llm_model_parser", "provider/model with spaces"),
        ("llm_model_brief", "provider/" + "x" * 256),
        ("openrouter_api_key", "synthetic key with spaces"),
        ("openrouter_api_key", "x" * 2049),
    ),
)
async def test_configuration_rejects_unapproved_provider_inputs_before_mutation(
    auth_client,
    db_session,
    tmp_path,
    monkeypatch,
    field,
    value,
):
    env_file = tmp_path / "platform-ai.env"
    _write_ai_env(env_file, key="synthetic-existing")
    original_file = env_file.read_text(encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    data = _configuration_form()
    data[field] = value

    response = await auth_client.post(
        "/settings/platform/ai/configuration",
        data=data,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("?error=configuration_invalid")
    assert env_file.read_text(encoding="utf-8") == original_file
    assert await db_session.scalar(
        select(func.count()).select_from(PlatformIntegrationConnection)
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == "platform.openrouter.configuration.updated"
        )
    ) == 0
