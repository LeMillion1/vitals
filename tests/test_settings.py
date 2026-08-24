"""Tests for the settings router and env_writer utility."""
from __future__ import annotations

import os
import re

import pytest

# No module-level ``pytest.mark.asyncio``: pytest.ini runs asyncio_mode=auto, and
# the mark on this file's *sync* env_writer tests only produced warnings.


def test_env_writer_read_missing_file(tmp_path, monkeypatch):
    """read_key returns empty string when .env file does not exist."""
    monkeypatch.setenv("VITALS_ENV_FILE", str(tmp_path / "nonexistent.env"))
    from web.services.env_writer import read_key
    assert read_key("SOME_KEY") == ""


def test_env_writer_read_existing_key(tmp_path, monkeypatch):
    """read_key returns the value for an existing key."""
    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_HEIGHT_CM=185\nVITALS_SEX=male\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    from web.services import env_writer
    import importlib

    importlib.reload(env_writer)
    from web.services.env_writer import read_key
    assert read_key("VITALS_HEIGHT_CM") == "185"
    assert read_key("VITALS_SEX") == "male"
    assert read_key("MISSING_KEY") == ""


def test_env_writer_write_updates_existing_key(tmp_path, monkeypatch):
    """write_keys updates an existing key in-place."""
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "# Comment\nVITALS_HEIGHT_CM=190\nVITALS_SEX=male\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    from web.services.env_writer import write_keys
    write_keys({"VITALS_HEIGHT_CM": "180"})
    content = env_file.read_text(encoding="utf-8")
    assert "VITALS_HEIGHT_CM=180" in content
    assert "VITALS_SEX=male" in content
    assert "# Comment" in content  # comments preserved


def test_env_writer_write_appends_new_key(tmp_path, monkeypatch):
    """write_keys appends a key that doesn't already exist."""
    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_HEIGHT_CM=190\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    from web.services.env_writer import write_keys
    write_keys({"VITALS_NEW_KEY": "hello"})
    content = env_file.read_text(encoding="utf-8")
    assert "VITALS_NEW_KEY=hello" in content
    assert "VITALS_HEIGHT_CM=190" in content


def test_env_writer_write_rejects_newline_in_value(tmp_path, monkeypatch):
    """write_keys refuses a value containing \\n or \\r — unescaped, it would
    break out of its KEY=value line and inject/overwrite another env var."""
    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_A=old_a\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    from web.services.env_writer import write_keys

    with pytest.raises(ValueError):
        write_keys({"VITALS_A": "evil\nVITALS_SESSION_SECRET=hijacked"})
    with pytest.raises(ValueError):
        write_keys({"VITALS_A": "evil\rcarriage"})

    # Rejected write must not have touched the file.
    content = env_file.read_text(encoding="utf-8")
    assert content == "VITALS_A=old_a\n"


def test_env_writer_write_multiple_keys(tmp_path, monkeypatch):
    """write_keys handles multiple updates in a single call."""
    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_A=old_a\nVITALS_B=old_b\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    from web.services.env_writer import write_keys
    write_keys({"VITALS_A": "new_a", "VITALS_B": "new_b", "VITALS_C": "new_c"})
    content = env_file.read_text(encoding="utf-8")
    assert "VITALS_A=new_a" in content
    assert "VITALS_B=new_b" in content
    assert "VITALS_C=new_c" in content


# ── settings page integration tests ──────────────────────────────────────────


async def test_settings_page_requires_auth(client):
    """GET /settings redirects to login when unauthenticated."""
    r = await client.get("/settings", headers={"Accept": "text/html"})
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


async def test_settings_page_renders(auth_client):
    """GET /settings renders all four config sections."""
    r = await auth_client.get("/settings", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "Профиль пользователя" in r.text
    assert "OpenRouter" in r.text
    assert "Hevy" in r.text
    assert "Garmin Connect" in r.text
    # Password and two-factor share one card — "signing in", not two neighbours
    # that both talk about the same door.
    assert "Вход в Vitals" in r.text
    assert "Двухфакторная защита" in r.text
    assert 'name="garmin_weight_export_minutes"' in r.text
    assert 'name="garmin_weight_max_age_days"' in r.text
    # Verify download links have hx-boost="false" to bypass HTMX boosting
    assert 'href="/settings/export" class="v-btn text-xs text-center" download hx-boost="false"' in r.text
    assert 'href="/settings/export-llm" class="v-btn-ghost text-xs text-center" download hx-boost="false"' in r.text


async def test_settings_page_has_gear_icon(auth_client):
    """The gear icon (⚙️ link to /settings) appears in the base layout."""
    r = await auth_client.get("/weight", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert 'href="/settings"' in r.text


async def test_settings_save_profile(
    auth_client, db_session, legacy_owner_roots, tmp_path, monkeypatch
):
    """POST /settings/profile writes the person's record, and leaves .env alone.

    It used to write ``.env``, which describes the installation and not anybody
    in it — one height and one sex for however many patients the installation
    holds. The environment is deliberately untouched now: it is what an
    installation that has not upgraded yet adopts from at startup, and a second
    answer that disagrees with the row is worse than a stale one nothing reads.
    """

    from vitals.services import health_profile_service

    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_HEIGHT_CM=190\nVITALS_SEX=male\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))

    r = await auth_client.post(
        "/settings/profile",
        data={
            "height_cm": "185",
            "sex": "male",
            "user_age": "30",
            "timezone": "Europe/Chisinau",
            "user_program": "тест",
            "user_goals": "цель1, цель2",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=profile"

    profile = await health_profile_service.get_profile(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert profile.height_cm == 185
    assert profile.age == 30
    assert profile.program == "тест"
    assert profile.goals == ("цель1", "цель2")

    assert env_file.read_text(encoding="utf-8") == (
        "VITALS_HEIGHT_CM=190\nVITALS_SEX=male\n"
    )


async def test_settings_save_ai_key(auth_client, tmp_path, monkeypatch):
    """POST /settings/ai writes the OpenRouter API key."""
    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_OPENROUTER_API_KEY=\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_OPENROUTER_API_KEY", "")
    monkeypatch.setenv("VITALS_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("VITALS_LLM_MODEL_DIGEST", "anthropic/claude-sonnet-4.6")
    monkeypatch.setenv("VITALS_LLM_MODEL_PARSER", "google/gemini-2.5-flash")
    monkeypatch.setenv("VITALS_LLM_MODEL_BRIEF", "")

    r = await auth_client.post(
        "/settings/ai",
        data={
            "openrouter_api_key": "sk-or-test-123",
            "llm_model_digest": "anthropic/claude-sonnet-4.6",
            "llm_model_parser": "google/gemini-2.5-flash",
            "openrouter_base_url": "https://openrouter.ai/api/v1",
        },
    )
    assert r.status_code == 303
    assert "saved=ai" in r.headers["location"]

    content = env_file.read_text(encoding="utf-8")
    assert "VITALS_OPENROUTER_API_KEY=sk-or-test-123" in content


async def test_settings_save_ai_sentinel_not_overwritten(auth_client, tmp_path, monkeypatch):
    """When user submits sentinel value for secret field, existing key is NOT overwritten."""
    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_OPENROUTER_API_KEY=sk-or-real-key\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("VITALS_LLM_MODEL_DIGEST", "anthropic/claude-sonnet-4.6")
    monkeypatch.setenv("VITALS_LLM_MODEL_PARSER", "google/gemini-2.5-flash")
    monkeypatch.setenv("VITALS_LLM_MODEL_BRIEF", "")

    # Submitting an empty api_key (like when user leaves placeholder)
    r = await auth_client.post(
        "/settings/ai",
        data={
            "openrouter_api_key": "",  # empty = no change
            "llm_model_digest": "anthropic/claude-sonnet-4.6",
            "llm_model_parser": "google/gemini-2.5-flash",
            "openrouter_base_url": "https://openrouter.ai/api/v1",
        },
    )
    assert r.status_code == 303
    content = env_file.read_text(encoding="utf-8")
    # Original key must survive
    assert "VITALS_OPENROUTER_API_KEY=sk-or-real-key" in content


async def test_settings_save_hevy(
    auth_client, db_session, legacy_owner_roots, tmp_path, monkeypatch
):
    """POST /settings/hevy stores the key against this person's connection.

    It wrote ``VITALS_HEVY_API_KEY`` — one workout account for the whole
    installation. The environment is left alone: it is the adoption source for a
    deployment that has not upgraded yet, and a second answer that disagrees
    with the stored one is worse than a stale one nothing reads.
    """

    from vitals.enums import IntegrationProvider
    from vitals.services import provider_credentials_service

    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_HEVY_API_KEY=\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))

    r = await auth_client.post("/settings/hevy", data={"hevy_api_key": "hevy_abc123"})
    assert r.status_code == 303
    assert "saved=hevy" in r.headers["location"]

    db_session.expire_all()
    account = await provider_credentials_service.resolve_account(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        provider=IntegrationProvider.HEVY,
    )
    assert account.configured
    assert account.config.hevy_api_key == "hevy_abc123"
    assert env_file.read_text(encoding="utf-8") == "VITALS_HEVY_API_KEY=\n"


async def test_settings_save_garmin(
    auth_client, db_session, legacy_owner_roots, tmp_path, monkeypatch
):
    """Credential saves are live, while export remains a separate explicit opt-in.

    "Live" used to mean writing ``.env`` and then ``os.environ`` so the next
    client built from ``load_config()`` would see it — the installation's one
    watch. It means stored against this record now, which the resolver reads on
    every construction, so it is live for the person who typed it and for nobody
    else.
    """
    from vitals.services import provider_credentials_service

    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_GARMIN_EMAIL=\nVITALS_GARMIN_PASSWORD=\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_GARMIN_EMAIL", "")
    monkeypatch.setenv("VITALS_GARMIN_PASSWORD", "")

    r = await auth_client.post(
        "/settings/garmin",
        data={
            "garmin_email": "user@example.com",
            "garmin_password": "hunter2",
        },
    )
    assert r.status_code == 303
    assert "saved=garmin" in r.headers["location"]

    from vitals.services import garmin_weight_service

    db_session.expire_all()
    account = await provider_credentials_service.resolve_garmin_account(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert account.config.garmin_email == "user@example.com"
    assert account.config.garmin_password == "hunter2"
    assert env_file.read_text(encoding="utf-8") == (
        "VITALS_GARMIN_EMAIL=\nVITALS_GARMIN_PASSWORD=\n"
    )
    assert await garmin_weight_service.is_enabled(db_session) is False

    page = await auth_client.get("/settings", headers={"Accept": "text/html"})
    assert 'hx-post="/settings/garmin/weight-toggle"' in page.text
    assert 'hx-post="/settings/garmin/weight/send-now"' not in page.text


async def test_garmin_weight_toggle_refuses_missing_credentials(
    auth_client, db_session, tmp_path, monkeypatch
):
    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_GARMIN_EMAIL=\nVITALS_GARMIN_PASSWORD=\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))

    r = await auth_client.post(
        "/settings/garmin/weight-toggle",
        data={"enabled": "true"},
        headers={"HX-Request": "true"},
    )

    assert r.status_code == 200
    assert "Экспорт остался выключен" in r.text
    from vitals.services import garmin_weight_service

    assert await garmin_weight_service.is_enabled(db_session) is False
    assert not re.search(
        r'id="s-garmin-weight-export"[^>]*\schecked(?:\s|>)', r.text
    )


async def test_garmin_weight_toggle_applies_live_and_can_turn_off(
    auth_client, db_session, garmin_connected, tmp_path, monkeypatch
):
    """The opt-in is gated on *this* record having a Garmin account.

    It used to be gated on ``VITALS_GARMIN_EMAIL`` and to copy those values into
    ``os.environ`` on the way through, which is the installation's single watch:
    a patient with no Garmin of their own passed the check on the strength of
    the operator's, and their weight would have been pushed to somebody else's
    account.
    """

    env_file = tmp_path / "test.env"
    env_file.write_text(
        "VITALS_GARMIN_EMAIL=\nVITALS_GARMIN_PASSWORD=\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_GARMIN_EMAIL", "")
    monkeypatch.setenv("VITALS_GARMIN_PASSWORD", "")

    enabled = await auth_client.post(
        "/settings/garmin/weight-toggle",
        data={"enabled": "true"},
        headers={"HX-Request": "true"},
    )

    assert enabled.status_code == 200
    assert "Экспорт веса включён" in enabled.text
    assert re.search(
        r'id="s-garmin-weight-export"[^>]*\schecked(?:\s|>)', enabled.text
    )

    from vitals.services import garmin_weight_service

    assert await garmin_weight_service.is_enabled(db_session) is True

    disabled = await auth_client.post(
        "/settings/garmin/weight-toggle",
        data={"enabled": "false"},
        headers={"HX-Request": "true"},
    )
    assert disabled.status_code == 200
    assert "Экспорт веса выключен" in disabled.text
    assert await garmin_weight_service.is_enabled(db_session) is False
    assert not re.search(
        r'id="s-garmin-weight-export"[^>]*\schecked(?:\s|>)', disabled.text
    )


async def test_garmin_weight_send_now_calls_safe_service(
    auth_client, monkeypatch
):
    from vitals.services import garmin_weight_service

    called = {}

    async def _send_now(session, *, prepared, redis=None):
        called["session"] = session
        called["prepared"] = prepared
        called["redis"] = redis
        return {"status": "sent", "sent": True}

    monkeypatch.setattr(garmin_weight_service, "send_now_scoped", _send_now)
    r = await auth_client.post(
        "/settings/garmin/weight/send-now",
        headers={"HX-Request": "true"},
    )

    assert r.status_code == 200
    assert "Последний подходящий вес безопасно сверен с Garmin" in r.text
    assert called["session"] is not None
    assert isinstance(
        called["prepared"],
        garmin_weight_service.PreparedGarminWeightExport,
    )
    assert called["redis"] is not None


@pytest.mark.parametrize(
    "export_status",
    [
        "pending",
        "checking",
        "sent",
        "matched",
        "failed",
        "skipped",
        "conflict",
        "unverified",
        "delete_pending",
        "delete_checking",
        "delete_failed",
        "deleted",
    ],
)
@pytest.mark.parametrize("lang", ["en", "ru"])
def test_garmin_weight_partial_renders_every_status_and_escapes_error(
    export_status, lang
):
    from datetime import date, datetime

    from vitals.i18n import current_lang, t
    from web.templating import format_number, templates

    token = current_lang.set(lang)
    try:
        expected = t(
            f"settings.garmin_weight_status.{export_status}",
            date="17-08-2026",
            weight=format_number(84.5),
        )
        expected_next = t(
            "settings.garmin_weight_next_attempt", at="17-08-2026 10:30"
        )
        html = templates.get_template("partials/garmin_weight_export.html").render(
            {
                "garmin_credentials_configured": True,
                "garmin_weight_action": None,
                "garmin_weight_export": {
                    "enabled": True,
                    "status": export_status,
                    "date": date(2026, 8, 17),
                    "weight_kg": 84.5,
                    "last_error": "<script>alert('secret')</script>",
                    "next_attempt_at": datetime(2026, 8, 17, 10, 30),
                },
            }
        )
    finally:
        current_lang.reset(token)

    assert expected in html
    assert expected_next in html
    assert "<script>alert('secret')</script>" not in html
    assert "&lt;script&gt;" in html


async def test_settings_save_mcp(auth_client, tmp_path, monkeypatch):
    """POST /settings/mcp writes client id and secret."""
    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_MCP_CLIENT_ID=\nVITALS_MCP_CLIENT_SECRET=\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))

    r = await auth_client.post(
        "/settings/mcp",
        data={"mcp_client_id": "test-id", "mcp_client_secret": "test-secret"},
    )
    assert r.status_code == 303
    assert "saved=mcp" in r.headers["location"]

    content = env_file.read_text(encoding="utf-8")
    assert "VITALS_MCP_CLIENT_ID=test-id" in content
    assert "VITALS_MCP_CLIENT_SECRET=test-secret" in content



async def test_settings_change_password_wrong_old(auth_client):
    """POST /settings/password with wrong current password shows error."""
    r = await auth_client.post(
        "/settings/password",
        data={
            "old_password": "wrongpassword",
            "new_password": "newpassword123",
            "new_password_confirm": "newpassword123",
        },
        headers={"Accept": "text/html"},
    )
    assert r.status_code == 200
    assert "Неверный текущий пароль" in r.text


async def test_settings_change_password_mismatch(auth_client):
    """POST /settings/password with mismatched new passwords shows error."""
    r = await auth_client.post(
        "/settings/password",
        data={
            "old_password": "password",
            "new_password": "newpass123",
            "new_password_confirm": "different456",
        },
        headers={"Accept": "text/html"},
    )
    assert r.status_code == 200
    assert "не совпадают" in r.text


async def test_settings_change_password_too_short(auth_client):
    """POST /settings/password with short new password shows error."""
    r = await auth_client.post(
        "/settings/password",
        data={
            "old_password": "password",
            "new_password": "short",
            "new_password_confirm": "short",
        },
        headers={"Accept": "text/html"},
    )
    assert r.status_code == 200
    assert "8 символов" in r.text


async def _persist_owner_hash(db_session, password_hash: str) -> None:
    """Align the startup-materialized owner with a per-test env credential."""
    from sqlalchemy import select

    from vitals.models.identity import User

    user = await db_session.scalar(select(User))
    assert user is not None
    user.password_hash = password_hash
    await db_session.commit()


async def test_settings_change_password_success(
    auth_client, db_session, tmp_path, monkeypatch
):
    """A password change updates both compatibility config and durable identity."""
    from sqlalchemy import select

    from vitals.models.identity import AuditEvent, HealthSubject, User, UserRole
    from vitals.utils.passwords import verify_password

    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_AUTH_PASSWORD_HASH=old_hash\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    # The handler now updates os.environ live; pin it so monkeypatch restores the
    # original hash on teardown (otherwise the new password leaks to later tests).
    monkeypatch.setenv("VITALS_AUTH_PASSWORD_HASH", os.environ["VITALS_AUTH_PASSWORD_HASH"])

    r = await auth_client.post(
        "/settings/password",
        data={
            "old_password": "password",  # matches TEST_PASSWORD in conftest
            "new_password": "mynewpassword",
            "new_password_confirm": "mynewpassword",
        },
    )
    assert r.status_code == 303
    assert "saved=password" in r.headers["location"]

    content = env_file.read_text(encoding="utf-8")
    # The hash was updated (bcrypt hashes start with $2b$)
    assert "$2b$" in content
    assert "old_hash" not in content

    user = await db_session.scalar(select(User))
    assert user is not None
    assert user.username == "tester"
    assert verify_password("mynewpassword", user.password_hash)
    assert user.session_version == 2
    assert len(list(await db_session.scalars(select(UserRole)))) == 2
    assert await db_session.scalar(select(HealthSubject)) is not None
    events = set(await db_session.scalars(select(AuditEvent.event_type)))
    assert events == {
        "identity.legacy_owner.bootstrap",
        "identity.password.rotated",
        "tenancy.legacy_resource_roots.bootstrap",
    }


async def test_settings_change_password_takes_effect_live(
    auth_client,
    db_session,
    tmp_path,
    monkeypatch,
):
    """After a password change the new password authenticates and the old one no
    longer does — in the same process, without a container restart."""
    from web.auth import authenticate
    from vitals.utils.passwords import hash_password

    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_AUTH_PASSWORD_HASH=old_hash\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    # Pin a known starting hash so monkeypatch restores it on teardown — the
    # handler mutates os.environ directly, which would otherwise leak to later tests.
    old_hash = hash_password("password")
    monkeypatch.setenv("VITALS_AUTH_PASSWORD_HASH", old_hash)
    await _persist_owner_hash(db_session, old_hash)

    r = await auth_client.post(
        "/settings/password",
        data={
            "old_password": "password",
            "new_password": "brandnewpass",
            "new_password_confirm": "brandnewpass",
        },
    )
    assert r.status_code == 303

    assert authenticate("tester", "password") is False
    assert authenticate("tester", "brandnewpass") is True


async def test_settings_change_password_preserves_stronger_bcrypt_cost(
    auth_client, db_session, tmp_path, monkeypatch
):
    """A pre-existing cost above the runtime default must not make rotation fail."""
    from sqlalchemy import select

    from vitals.models.identity import User
    from vitals.services.identity_service import bcrypt_cost
    from vitals.utils.passwords import hash_password

    stronger_hash = hash_password("password", minimum_rounds=5)
    env_file = tmp_path / "test.env"
    env_file.write_text(
        f"VITALS_AUTH_PASSWORD_HASH={stronger_hash}\n", encoding="utf-8"
    )
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_AUTH_PASSWORD_HASH", stronger_hash)
    await _persist_owner_hash(db_session, stronger_hash)

    response = await auth_client.post(
        "/settings/password",
        data={
            "old_password": "password",
            "new_password": "brandnewpass",
            "new_password_confirm": "brandnewpass",
        },
    )

    assert response.status_code == 303
    user = await db_session.scalar(select(User))
    assert user is not None
    assert bcrypt_cost(user.password_hash) == 5


async def test_settings_change_password_restores_env_when_db_commit_fails(
    auth_client, db_session, tmp_path, monkeypatch
):
    """The non-transactional env half is compensated if DB commit fails."""
    from unittest.mock import AsyncMock

    from sqlalchemy import func, select

    from vitals.models.identity import User
    from vitals.utils.passwords import hash_password
    from web.auth import authenticate

    old_hash = hash_password("password")
    env_file = tmp_path / "test.env"
    env_file.write_text(
        f"VITALS_AUTH_PASSWORD_HASH={old_hash}\n", encoding="utf-8"
    )
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_AUTH_PASSWORD_HASH", old_hash)
    await _persist_owner_hash(db_session, old_hash)
    monkeypatch.setattr(
        db_session,
        "commit",
        AsyncMock(side_effect=RuntimeError("synthetic commit failure")),
    )

    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        await auth_client.post(
            "/settings/password",
            data={
                "old_password": "password",
                "new_password": "brandnewpass",
                "new_password_confirm": "brandnewpass",
            },
        )

    assert env_file.read_text(encoding="utf-8") == (
        f"VITALS_AUTH_PASSWORD_HASH={old_hash}\n"
    )
    assert os.environ["VITALS_AUTH_PASSWORD_HASH"] == old_hash
    user_count = await db_session.scalar(select(func.count()).select_from(User))
    assert int(user_count or 0) == 1
    user = await db_session.scalar(select(User))
    assert user is not None and user.password_hash == old_hash
    assert authenticate("tester", "password") is True
    assert authenticate("tester", "brandnewpass") is False


async def test_settings_change_password_compensates_task_cancellation(
    auth_client, db_session, tmp_path, monkeypatch
):
    """Cancellation after the env write must not strand a mismatched hash."""
    import asyncio
    from unittest.mock import AsyncMock

    from sqlalchemy import func, select

    from vitals.models.identity import User
    from vitals.utils.passwords import hash_password
    from web.auth import authenticate

    old_hash = hash_password("password")
    env_file = tmp_path / "test.env"
    env_file.write_text(
        f"VITALS_AUTH_PASSWORD_HASH={old_hash}\n", encoding="utf-8"
    )
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))
    monkeypatch.setenv("VITALS_AUTH_PASSWORD_HASH", old_hash)
    await _persist_owner_hash(db_session, old_hash)
    monkeypatch.setattr(
        db_session,
        "commit",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )

    # Starlette's BaseHTTPMiddleware converts an uncaught BaseException from the
    # endpoint task into this transport-level error. The assertions below pin the
    # behavior that matters: our endpoint saw cancellation and compensated first.
    with pytest.raises(RuntimeError, match="No response returned"):
        await auth_client.post(
            "/settings/password",
            data={
                "old_password": "password",
                "new_password": "brandnewpass",
                "new_password_confirm": "brandnewpass",
            },
        )

    assert env_file.read_text(encoding="utf-8") == (
        f"VITALS_AUTH_PASSWORD_HASH={old_hash}\n"
    )
    assert os.environ["VITALS_AUTH_PASSWORD_HASH"] == old_hash
    user_count = await db_session.scalar(select(func.count()).select_from(User))
    assert int(user_count or 0) == 1
    user = await db_session.scalar(select(User))
    assert user is not None and user.password_hash == old_hash
    assert authenticate("tester", "password") is True
    assert authenticate("tester", "brandnewpass") is False


async def test_settings_restart_endpoint(auth_client, monkeypatch):
    """POST /settings/restart triggers a delayed restart without killing the process in tests."""
    killed = []

    def mock_kill(pid, sig):
        killed.append((pid, sig))

    monkeypatch.setattr("os.kill", mock_kill)

    r = await auth_client.post("/settings/restart")
    assert r.status_code == 200
    assert r.json() == {"status": "restarting"}

    # Wait for the background task to execute
    import asyncio
    await asyncio.sleep(0.6)

    import os
    assert len(killed) == 1
    assert killed[0] == (os.getpid(), 15)  # 15 is signal.SIGTERM


# ── proactive settings regressions ────────────────────────────────────────────


async def test_settings_save_proactive_flags_adjusted_values(auth_client):
    """prefs.sanitize() (called inside set_preferences_bundle) silently clamps
    out-of-range input. The redirect must say so instead of a bare "saved",
    or the user has no way to know their number was changed underneath them.

    Clamped on ``garmin_sync_hours`` rather than ``daily_budget``: the budget
    gates a send, and the card stopped offering the controls that do that when
    the transport went, so the handler no longer accepts it from a form.
    """
    r = await auth_client.post(
        "/settings/proactive",
        data={
            "brief_time": "11:00",
            "garmin_sync_hours": "9000",  # SYNC_HOURS_RANGE is (1, 24)
            "pulse_seconds": "900",
            "pulse_start_hour": "8",
            "pulse_end_hour": "24",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=proactive&adjusted=1"


async def test_saving_the_card_does_not_reset_the_hidden_delivery_policy(
    auth_client, db_session
):
    """The risk created by taking those controls off the card.

    ``Form(default)`` fills in a field the browser did not post, so a save from
    the reduced card would have written the *defaults* over quiet hours, the
    budget and the nudge switches — settings the owner had chosen, silently
    reset by a form that no longer mentions them. The handler reads the stored
    bundle and overlays only what the card still asks about.
    """

    from vitals.services.proactive import prefs

    scope = await prefs.resolve_legacy_preferences_scope(
        db_session, actor_username="tester"
    )
    await prefs.set_preferences_bundle(
        db_session,
        {
            **(
                await prefs.get_preferences_bundle(
                    db_session, scope=scope, actor_username="tester"
                )
            ).as_flat_dict(),
            "quiet_start": "01:15",
            "daily_budget": 11,
            "nudges": {"activity": False, "nutrition": True, "data": False},
        },
        scope=scope,
        actor_username="tester",
    )
    await db_session.commit()

    r = await auth_client.post(
        "/settings/proactive",
        data={
            "brief_time": "09:30",
            "garmin_sync_hours": "6",
            "pulse_seconds": "900",
            "pulse_start_hour": "8",
            "pulse_end_hour": "24",
        },
    )
    assert r.status_code == 303

    db_session.expire_all()
    after = (
        await prefs.get_preferences_bundle(
            db_session, scope=scope, actor_username="tester"
        )
    ).as_flat_dict()
    assert after["brief_time"] == "09:30", "the field the card does offer is saved"
    assert after["quiet_start"] == "01:15"
    assert after["daily_budget"] == 11
    assert after["nudges"] == {"activity": False, "nutrition": True, "data": False}


async def test_settings_save_proactive_no_adjusted_flag_in_range(auth_client):
    """The flip side: an in-range save must not claim anything was adjusted."""
    r = await auth_client.post(
        "/settings/proactive",
        data={
            "brief_time": "11:00",
            "garmin_sync_hours": "6",
            "pulse_seconds": "900",
            "pulse_start_hour": "8",
            "pulse_end_hour": "24",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=proactive"


