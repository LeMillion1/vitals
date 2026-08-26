from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts import oidc_cutover
from scripts.provision_runtime_db_role import provision_runtime_role
from vitals.models.base import Base
from vitals.models.identity import HealthSubject, McpAccessToken, User
from vitals.runtime_env import read_env_key, write_env_keys
from vitals.services.authentication.oidc import OidcSettings
from vitals.utils.passwords import hash_password


def _runtime(tmp_path: Path, *, oidc: bool = False, bootstrap: bool = True) -> Path:
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    path = parent / "vitals.env"
    values = {
        "VITALS_AUTH_PASSWORD_HASH": hash_password("legacy-password"),
        "VITALS_AUTH_USERNAME": "owner",
        "VITALS_DATABASE_URL": "sqlite+aiosqlite:///synthetic.db",
        "VITALS_OIDC_BOOTSTRAP_SUBJECT": "owner-sub" if oidc and bootstrap else "",
        "VITALS_OIDC_CLIENT_ID": "client" if oidc else "",
        "VITALS_OIDC_CLIENT_SECRET": "provider-secret" if oidc else "",
        "VITALS_OIDC_ISSUER": "https://auth.example.test" if oidc else "",
        "VITALS_OIDC_REDIRECT_URL": ("https://vitals.example.test/auth/callback" if oidc else ""),
        "VITALS_PUBLIC_URL": "https://vitals.example.test",
        "VITALS_REGISTRATION_UNLOCKED": "0",
        "VITALS_SESSION_SECRET": "old-session-secret",
    }
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _secret(tmp_path: Path, value: str = "provider-secret") -> Path:
    parent = tmp_path / "oidc-secret"
    parent.mkdir(mode=0o700)
    path = parent / "client-secret"
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def _enable_args(runtime: Path, secret: Path, *, confirm: str | None = None):
    legacy_password = secret.parent / "legacy-password"
    legacy_password.write_text("legacy-password", encoding="utf-8")
    legacy_password.chmod(0o600)
    return argparse.Namespace(
        bootstrap_subject="owner-sub",
        client_id="client",
        client_secret_file=secret,
        confirm=confirm,
        issuer="https://auth.example.test",
        legacy_password_file=legacy_password,
        operation="enable",
        redirect_url="https://vitals.example.test/auth/callback",
        runtime_env=runtime,
    )


def _value(path: Path, key: str) -> str:
    return read_env_key(
        path,
        key,
        require_existing=True,
        require_owner_only=True,
    )


async def _seed_connector_database(database_url: str) -> tuple[list, list, list]:
    """Create two subjects with live, expired, and already-revoked connectors."""

    engine = create_async_engine(database_url)
    now = datetime.now(timezone.utc)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            users = [
                User(
                    username=f"owner-{index}",
                    normalized_username=f"owner-{index}",
                    password_hash="$synthetic",
                    status="active",
                )
                for index in range(2)
            ]
            session.add_all(users)
            await session.flush()
            subjects = [
                HealthSubject(
                    owner_user_id=user.id,
                    display_name=f"Subject {index}",
                    timezone="Asia/Almaty",
                )
                for index, user in enumerate(users)
            ]
            session.add_all(subjects)
            await session.flush()
            live = [
                McpAccessToken(
                    user_id=user.id,
                    subject_id=subject.id,
                    client_id="connector",
                    audience="https://vitals.example.test/mcp",
                    issued_at=now - timedelta(days=1),
                    expires_at=now + timedelta(days=30),
                )
                for user, subject in zip(users, subjects, strict=True)
            ]
            expired = [
                McpAccessToken(
                    user_id=users[0].id,
                    subject_id=subjects[0].id,
                    client_id="expired",
                    audience="https://vitals.example.test/mcp",
                    issued_at=now - timedelta(days=30),
                    expires_at=now - timedelta(days=1),
                )
            ]
            revoked = [
                McpAccessToken(
                    user_id=users[1].id,
                    subject_id=subjects[1].id,
                    client_id="already-revoked",
                    audience="https://vitals.example.test/mcp",
                    issued_at=now - timedelta(days=1),
                    expires_at=now + timedelta(days=30),
                    revoked_at=now - timedelta(hours=1),
                )
            ]
            session.add_all([*live, *expired, *revoked])
            await session.flush()
            return (
                [row.id for row in live],
                [row.id for row in expired],
                [row.id for row in revoked],
            )
    finally:
        await engine.dispose()


def test_container_runtime_file_is_the_safe_default():
    assert oidc_cutover.DEFAULT_RUNTIME_ENV == Path("/run/vitals-runtime/vitals.env")


def test_runtime_state_refuses_a_partial_oidc_group(tmp_path):
    runtime = _runtime(tmp_path)
    write_env_keys(
        runtime,
        {"VITALS_OIDC_ISSUER": "https://auth.example.test"},
        require_existing=True,
        require_owner_only=True,
    )

    with pytest.raises(oidc_cutover.OidcCutoverError, match="partial OIDC group"):
        oidc_cutover._runtime_state(runtime)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400])
def test_client_secret_requires_owner_only_writable_mode(tmp_path, mode):
    secret = _secret(tmp_path)
    secret.chmod(mode)

    with pytest.raises(oidc_cutover.OidcCutoverError, match="mode 0600"):
        oidc_cutover._validate_private_secret_file(secret)


def test_client_secret_refuses_symlink_and_multiline(tmp_path):
    secret = _secret(tmp_path)
    link = secret.parent / "link"
    link.symlink_to(secret)
    with pytest.raises(oidc_cutover.OidcCutoverError):
        oidc_cutover._validate_private_secret_file(link)

    secret.write_text("first\nsecond\n", encoding="utf-8")
    with pytest.raises(oidc_cutover.OidcCutoverError, match="one non-empty line"):
        oidc_cutover._validate_private_secret_file(secret)

    secret.write_text(" padded-secret ", encoding="utf-8")
    with pytest.raises(oidc_cutover.OidcCutoverError, match="one non-empty line"):
        oidc_cutover._validate_private_secret_file(secret)


def test_callback_must_equal_the_public_origin(tmp_path):
    runtime = _runtime(tmp_path)

    with pytest.raises(oidc_cutover.OidcCutoverError, match="exactly equal"):
        oidc_cutover._validate_public_callback(
            runtime_env=runtime,
            issuer="https://auth.example.test",
            redirect_url="https://other.example.test/auth/callback",
        )


@pytest.mark.asyncio
async def test_preflight_requires_the_actual_legacy_password(tmp_path):
    runtime = _runtime(tmp_path)
    secret = _secret(tmp_path)
    args = _enable_args(runtime, secret)
    args.legacy_password_file.write_text("wrong-password", encoding="utf-8")

    with pytest.raises(oidc_cutover.OidcCutoverError, match="password proof"):
        await oidc_cutover._enable_preflight(args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata,reason",
    [
        (
            {
                "code_challenge_methods_supported": ["S256"],
                "end_session_endpoint": "https://auth.example.test/logout",
            },
            "client_secret_post",
        ),
        (
            {
                "token_endpoint_auth_methods_supported": ["client_secret_post"],
                "end_session_endpoint": "https://auth.example.test/logout",
            },
            "S256",
        ),
    ],
)
async def test_provider_contract_requires_explicit_post_and_s256(monkeypatch, metadata, reason):
    class Provider:
        def __init__(self, _settings):
            pass

        async def metadata(self):
            return metadata

    monkeypatch.setattr(oidc_cutover, "OidcProvider", Provider)
    settings = OidcSettings(
        issuer="https://auth.example.test",
        client_id="client",
        client_secret="secret",
        redirect_url="https://vitals.example.test/auth/callback",
    )

    with pytest.raises(oidc_cutover.OidcCutoverError, match=reason):
        await oidc_cutover._validate_provider(settings)


@pytest.mark.asyncio
async def test_session_rotation_revokes_only_live_mcp_rows_across_subjects(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'connectors.db'}"
    live_ids, expired_ids, revoked_ids = await _seed_connector_database(database_url)
    runtime = _runtime(tmp_path)
    write_env_keys(
        runtime,
        {"VITALS_DATABASE_URL": database_url},
        require_existing=True,
        require_owner_only=True,
    )

    assert await oidc_cutover._revoke_live_mcp_tokens(runtime_env=runtime) == 2

    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = {
                row.id: row
                for row in (
                    await session.scalars(sa.select(McpAccessToken))
                ).all()
            }
        assert all(rows[row_id].revoked_at is not None for row_id in live_ids)
        assert all(rows[row_id].revoked_at is None for row_id in expired_ids)
        assert all(rows[row_id].revoked_at is not None for row_id in revoked_ids)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_revocation_rolls_back_every_subject_on_midway_failure(
    tmp_path, monkeypatch
):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'rollback.db'}"
    live_ids, _expired_ids, _revoked_ids = await _seed_connector_database(database_url)
    runtime = _runtime(tmp_path)
    write_env_keys(
        runtime,
        {"VITALS_DATABASE_URL": database_url},
        require_existing=True,
        require_owner_only=True,
    )
    original = oidc_cutover._revoke_subject_mcp_tokens
    calls = 0

    async def fail_second(session, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic second-subject failure")
        return await original(session, **kwargs)

    monkeypatch.setattr(oidc_cutover, "_revoke_subject_mcp_tokens", fail_second)
    with pytest.raises(oidc_cutover.OidcCutoverError, match="could not revoke"):
        await oidc_cutover._revoke_live_mcp_tokens(runtime_env=runtime)

    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = {
                row.id: row
                for row in (
                    await session.scalars(
                        sa.select(McpAccessToken).where(McpAccessToken.id.in_(live_ids))
                    )
                ).all()
            }
        assert all(rows[row_id].revoked_at is None for row_id in live_ids)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_enable_does_not_publish_if_mcp_revocation_fails(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    secret = _secret(tmp_path)
    before = runtime.read_bytes()

    async def accepted(*_args, **_kwargs):
        return None

    async def refused(**_kwargs):
        raise oidc_cutover.OidcCutoverError("could not revoke live MCP connectors")

    monkeypatch.setattr(oidc_cutover, "_validate_provider", accepted)
    monkeypatch.setattr(oidc_cutover, "_validate_database_bootstrap", accepted)
    monkeypatch.setattr(oidc_cutover, "_revoke_live_mcp_tokens", refused)

    with pytest.raises(oidc_cutover.OidcCutoverError, match="could not revoke"):
        await oidc_cutover._enable(
            _enable_args(runtime, secret, confirm=oidc_cutover.ENABLE_CONFIRMATION)
        )

    assert runtime.read_bytes() == before


@pytest.mark.asyncio
async def test_enable_rotates_sessions_and_publishes_the_complete_group(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    secret = _secret(tmp_path)

    async def accepted(*_args, **_kwargs):
        return None

    async def revoked(**_kwargs):
        assert oidc_cutover._runtime_state(runtime)[0] == "password"
        return 2

    monkeypatch.setattr(oidc_cutover, "_validate_provider", accepted)
    monkeypatch.setattr(oidc_cutover, "_validate_database_bootstrap", accepted)
    monkeypatch.setattr(oidc_cutover, "_revoke_live_mcp_tokens", revoked)
    monkeypatch.setattr(oidc_cutover.secrets, "token_urlsafe", lambda _size: "rotated-session")

    result = await oidc_cutover._enable(
        _enable_args(
            runtime,
            secret,
            confirm=oidc_cutover.ENABLE_CONFIRMATION,
        )
    )

    assert result["readback"] == "oidc_bootstrap_pending"
    assert result["mcp_connectors_revoked"] == 2
    assert _value(runtime, "VITALS_SESSION_SECRET") == "rotated-session"
    assert _value(runtime, "VITALS_OIDC_CLIENT_SECRET") == "provider-secret"
    assert _value(runtime, "VITALS_OIDC_BOOTSTRAP_SUBJECT") == "owner-sub"


@pytest.mark.asyncio
async def test_enable_failure_after_publish_restores_every_previous_value(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    secret = _secret(tmp_path)
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'publish-failure.db'}"
    live_ids, _expired_ids, _revoked_ids = await _seed_connector_database(database_url)
    write_env_keys(
        runtime,
        {"VITALS_DATABASE_URL": database_url},
        require_existing=True,
        require_owner_only=True,
    )

    async def accepted(*_args, **_kwargs):
        return None

    monkeypatch.setattr(oidc_cutover, "_validate_provider", accepted)
    monkeypatch.setattr(oidc_cutover, "_validate_database_bootstrap", accepted)
    original_state = oidc_cutover._runtime_state
    calls = 0

    def failed_readback(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise oidc_cutover.OidcCutoverError("synthetic readback failure")
        return original_state(path)

    monkeypatch.setattr(oidc_cutover, "_runtime_state", failed_readback)

    with pytest.raises(oidc_cutover.OidcCutoverError, match="previous runtime"):
        await oidc_cutover._enable(
            _enable_args(
                runtime,
                secret,
                confirm=oidc_cutover.ENABLE_CONFIRMATION,
            )
        )

    assert original_state(runtime)[0] == "password"
    assert _value(runtime, "VITALS_SESSION_SECRET") == "old-session-secret"
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = list(
                (
                    await session.scalars(
                        sa.select(McpAccessToken).where(
                            McpAccessToken.id.in_(live_ids)
                        )
                    )
                ).all()
            )
        assert len(rows) == len(live_ids)
        assert all(row.revoked_at is not None for row in rows)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_wrong_enable_confirmation_does_not_run_preflight(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    secret = _secret(tmp_path)
    called = False

    async def probe(_args):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(oidc_cutover, "_enable_preflight", probe)

    with pytest.raises(oidc_cutover.OidcCutoverError, match="exact --confirm"):
        await oidc_cutover._enable(_enable_args(runtime, secret, confirm="wrong"))
    assert called is False


@pytest.mark.asyncio
async def test_rollback_validates_owner_then_clears_oidc_and_rotates(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, oidc=True)
    validated = False

    async def validate(*, runtime_env, legacy_password):
        nonlocal validated
        assert runtime_env == runtime
        assert legacy_password == "legacy-password"
        validated = True

    async def revoked(**_kwargs):
        assert oidc_cutover._runtime_state(runtime)[0] == "oidc_bootstrap_pending"
        return 1

    monkeypatch.setattr(oidc_cutover, "_validate_password_rollback", validate)
    monkeypatch.setattr(oidc_cutover, "_revoke_live_mcp_tokens", revoked)
    monkeypatch.setattr(oidc_cutover.secrets, "token_urlsafe", lambda _size: "rollback-session")
    args = argparse.Namespace(
        confirm=oidc_cutover.ROLLBACK_CONFIRMATION,
        legacy_password_file=_secret(tmp_path, "legacy-password"),
        operation="rollback",
        runtime_env=runtime,
    )

    result = await oidc_cutover._rollback(args)

    assert validated is True
    assert result["readback"] == "password"
    assert result["mcp_connectors_revoked"] == 1
    assert _value(runtime, "VITALS_OIDC_ISSUER") == ""
    assert _value(runtime, "VITALS_SESSION_SECRET") == "rollback-session"


@pytest.mark.asyncio
async def test_rollback_refusal_leaves_runtime_unchanged(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, oidc=True)

    async def refused(**_kwargs):
        raise oidc_cutover.OidcCutoverError("unsafe graph")

    monkeypatch.setattr(oidc_cutover, "_validate_password_rollback", refused)
    args = argparse.Namespace(
        confirm=oidc_cutover.ROLLBACK_CONFIRMATION,
        legacy_password_file=_secret(tmp_path, "legacy-password"),
        operation="rollback",
        runtime_env=runtime,
    )

    with pytest.raises(oidc_cutover.OidcCutoverError, match="unsafe graph"):
        await oidc_cutover._rollback(args)

    assert oidc_cutover._runtime_state(runtime)[0] == "oidc_bootstrap_pending"
    assert _value(runtime, "VITALS_SESSION_SECRET") == "old-session-secret"


@pytest.mark.asyncio
async def test_password_preflight_proves_the_current_password_without_mutation(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    before = runtime.read_bytes()
    validated = False

    async def validate(*, runtime_env, legacy_password):
        nonlocal validated
        assert runtime_env == runtime
        assert legacy_password == "legacy-password"
        validated = True

    monkeypatch.setattr(oidc_cutover, "_validate_password_rollback", validate)
    args = argparse.Namespace(
        legacy_password_file=_secret(tmp_path, "legacy-password"),
        operation="password-preflight",
        runtime_env=runtime,
    )

    result = await oidc_cutover._password_preflight(args)

    assert validated is True
    assert result["readback"] == "password"
    assert runtime.read_bytes() == before


@pytest.mark.asyncio
async def test_finalize_proves_binding_before_clearing_bootstrap(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, oidc=True)
    validated = False

    async def validate(**kwargs):
        nonlocal validated
        assert kwargs["issuer"] == "https://auth.example.test"
        assert kwargs["bootstrap_subject"] == "owner-sub"
        assert kwargs["not_before"].isoformat() == "2026-08-27T12:00:00+00:00"
        validated = True

    monkeypatch.setattr(oidc_cutover, "_require_bound_owner", validate)
    args = argparse.Namespace(
        confirm=oidc_cutover.FINALIZE_CONFIRMATION,
        not_before="2026-08-27T12:00:00+00:00",
        operation="finalize",
        runtime_env=runtime,
    )

    result = await oidc_cutover._finalize(args)

    assert validated is True
    assert result["readback"] == "oidc_bound"
    assert _value(runtime, "VITALS_OIDC_BOOTSTRAP_SUBJECT") == ""
    assert _value(runtime, "VITALS_SESSION_SECRET") == "old-session-secret"


@pytest.mark.parametrize(
    "value",
    ["", "not-a-time", "2026-08-27T12:00:00"],
)
def test_login_proof_timestamp_must_be_aware(value):
    with pytest.raises(oidc_cutover.OidcCutoverError, match="timestamp"):
        oidc_cutover._parse_not_before(value)


@pytest.mark.asyncio
async def test_retire_legacy_keeps_oidc_and_rotates_every_signed_token(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, oidc=True, bootstrap=False)

    async def accepted(**kwargs):
        assert kwargs["bootstrap_subject"] is None
        assert kwargs["legacy_credentials"][0] == "owner"
        assert kwargs["require_password_retired"] is True
        assert kwargs["allow_already_retired"] is False

    async def revoked(**_kwargs):
        assert _value(runtime, "VITALS_AUTH_USERNAME") == "owner"
        assert oidc_cutover._runtime_state(runtime)[0] == "oidc_bound"
        return 4

    monkeypatch.setattr(oidc_cutover, "_require_bound_owner", accepted)
    monkeypatch.setattr(oidc_cutover, "_revoke_live_mcp_tokens", revoked)
    monkeypatch.setattr(oidc_cutover.secrets, "token_urlsafe", lambda _size: "retired-session")
    args = argparse.Namespace(
        confirm=oidc_cutover.RETIRE_CONFIRMATION,
        allow_already_retired=False,
        not_before="2026-08-27T12:00:00+00:00",
        operation="retire-legacy",
        runtime_env=runtime,
    )

    result = await oidc_cutover._retire_legacy(args)

    assert result["readback"] == "oidc_bound"
    assert result["mcp_connectors_revoked"] == 4
    assert result["session_secret_rotated"] is True
    assert _value(runtime, "VITALS_AUTH_USERNAME") == ""
    assert _value(runtime, "VITALS_AUTH_PASSWORD_HASH") == ""
    assert _value(runtime, "VITALS_OIDC_ISSUER") == "https://auth.example.test"
    assert _value(runtime, "VITALS_SESSION_SECRET") == "retired-session"


@pytest.mark.asyncio
async def test_retire_legacy_recovery_is_idempotent_after_atomic_write(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, oidc=True, bootstrap=False)
    oidc_cutover.write_env_keys(
        runtime,
        {"VITALS_AUTH_USERNAME": "", "VITALS_AUTH_PASSWORD_HASH": ""},
        require_existing=True,
        require_owner_only=True,
    )

    async def accepted(**_kwargs):
        return None

    monkeypatch.setattr(oidc_cutover, "_require_bound_owner", accepted)
    args = argparse.Namespace(
        confirm=oidc_cutover.RETIRE_CONFIRMATION,
        allow_already_retired=True,
        not_before="2026-08-27T12:00:00+00:00",
        operation="retire-legacy",
        runtime_env=runtime,
    )

    result = await oidc_cutover._retire_legacy(args)

    assert result["already_retired"] is True
    assert result["session_secret_rotated"] is False
    assert oidc_cutover._runtime_state(runtime)[0] == "oidc_bound"


@pytest.mark.asyncio
async def test_retire_legacy_refuses_unjournaled_already_absent_runtime(tmp_path):
    runtime = _runtime(tmp_path, oidc=True, bootstrap=False)
    oidc_cutover.write_env_keys(
        runtime,
        {"VITALS_AUTH_USERNAME": "", "VITALS_AUTH_PASSWORD_HASH": ""},
        require_existing=True,
        require_owner_only=True,
    )
    args = argparse.Namespace(
        confirm=oidc_cutover.RETIRE_CONFIRMATION,
        allow_already_retired=False,
        not_before="2026-08-27T12:00:00+00:00",
        operation="retire-legacy",
        runtime_env=runtime,
    )

    with pytest.raises(oidc_cutover.OidcCutoverError, match="journaled recovery"):
        await oidc_cutover._retire_legacy(args)


@pytest.mark.asyncio
async def test_retire_preflight_proves_login_without_mutating_runtime(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, oidc=True, bootstrap=False)
    before = runtime.read_bytes()

    async def accepted(**kwargs):
        assert kwargs["retire_password"] is False
        assert kwargs["legacy_credentials"][0] == "owner"

    monkeypatch.setattr(oidc_cutover, "_require_bound_owner", accepted)
    args = argparse.Namespace(
        not_before="2026-08-27T12:00:00+00:00",
        operation="retire-preflight",
        runtime_env=runtime,
    )

    result = await oidc_cutover._retire_preflight(args)

    assert result["result"] == "ok"
    assert runtime.read_bytes() == before


@pytest.mark.asyncio
async def test_preflight_is_read_only(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    secret = _secret(tmp_path)
    before = runtime.read_bytes()

    async def accepted(_args):
        return {"VITALS_OIDC_CLIENT_SECRET": "provider-secret"}

    monkeypatch.setattr(oidc_cutover, "_enable_preflight", accepted)
    args = _enable_args(runtime, secret)
    args.operation = "preflight"

    result = await oidc_cutover._preflight(args)

    assert result["registration"] == "locked_disabled"
    assert runtime.read_bytes() == before


@pytest.mark.asyncio
async def test_status_json_never_contains_runtime_secrets(tmp_path, capsys):
    runtime = _runtime(tmp_path, oidc=True)
    args = argparse.Namespace(operation="status", runtime_env=runtime)

    assert await oidc_cutover._run(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "operation": "oidc_cutover_status",
        "readback": "oidc_bootstrap_pending",
        "result": "ok",
    }
    serialized = json.dumps(payload)
    assert "provider-secret" not in serialized
    assert "old-session-secret" not in serialized


@pytest.mark.asyncio
async def test_unexpected_error_is_sanitized(tmp_path, capsys, monkeypatch):
    runtime = _runtime(tmp_path)

    def exploded(_path):
        raise RuntimeError("postgresql://owner:database-secret@example.test/db")

    monkeypatch.setattr(oidc_cutover, "_runtime_state", exploded)
    args = argparse.Namespace(operation="status", runtime_env=runtime)

    assert await oidc_cutover._run(args) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "database-secret" not in output.err
    assert json.loads(output.err)["reason"] == "unexpected cutover failure"


def test_secret_parent_must_not_be_group_accessible(tmp_path):
    secret = _secret(tmp_path)
    os.chmod(secret.parent, 0o750)

    with pytest.raises(oidc_cutover.OidcCutoverError, match="mode 0700"):
        oidc_cutover._validate_private_secret_file(secret)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mcp_revocation_crosses_forced_rls_without_runtime_escalation(
    db_session, tmp_path
):
    raw_admin_url = os.getenv("VITALS_TEST_DATABASE_URL")
    if not raw_admin_url or not raw_admin_url.startswith("postgresql+asyncpg://"):
        pytest.skip("integration test requires VITALS_TEST_DATABASE_URL")

    admin_url = make_url(raw_admin_url)
    role = f"vitals_oidc_revoke_{secrets.token_hex(6)}"
    runtime_url = admin_url.set(
        username=role,
        password=secrets.token_urlsafe(24),
    )
    admin = create_async_engine(admin_url)
    now = datetime.now(timezone.utc)
    users = [
        User(
            username=f"rls-owner-{index}",
            normalized_username=f"rls-owner-{index}",
            password_hash="$synthetic",
            status="active",
        )
        for index in range(2)
    ]
    db_session.add_all(users)
    await db_session.flush()
    subjects = [
        HealthSubject(
            owner_user_id=user.id,
            display_name=f"RLS subject {index}",
            timezone="Asia/Almaty",
        )
        for index, user in enumerate(users)
    ]
    db_session.add_all(subjects)
    await db_session.flush()
    live = [
        McpAccessToken(
            user_id=user.id,
            subject_id=subject.id,
            client_id="rls-connector",
            audience="https://vitals.example.test/mcp",
            issued_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )
        for user, subject in zip(users, subjects, strict=True)
    ]
    db_session.add_all(live)
    await db_session.commit()

    try:
        await provision_runtime_role(
            migration_url=admin_url,
            runtime_url=runtime_url,
        )
        async with admin.begin() as connection:
            await connection.exec_driver_sql(
                "ALTER TABLE mcp_access_tokens ENABLE ROW LEVEL SECURITY"
            )
            await connection.exec_driver_sql(
                "ALTER TABLE mcp_access_tokens FORCE ROW LEVEL SECURITY"
            )
            await connection.exec_driver_sql(
                "CREATE POLICY rls_subject_isolation ON mcp_access_tokens "
                "USING (subject_id = NULLIF(current_setting("
                "'vitals.subject_id', true), '')::uuid) "
                "WITH CHECK (subject_id = NULLIF(current_setting("
                "'vitals.subject_id', true), '')::uuid)"
            )
            attributes = (
                await connection.execute(
                    sa.text(
                        "SELECT rolsuper, rolbypassrls FROM pg_roles "
                        "WHERE rolname=:role"
                    ),
                    {"role": role},
                )
            ).one()
            assert tuple(attributes) == (False, False)

        runtime = _runtime(tmp_path)
        write_env_keys(
            runtime,
            {
                "VITALS_DATABASE_URL": runtime_url.render_as_string(
                    hide_password=False
                )
            },
            require_existing=True,
            require_owner_only=True,
        )

        assert await oidc_cutover._revoke_live_mcp_tokens(runtime_env=runtime) == 2
        rows = list(
            (
                await db_session.scalars(
                    sa.select(McpAccessToken).where(
                        McpAccessToken.id.in_([row.id for row in live])
                    )
                )
            ).all()
        )
        assert len(rows) == 2
        assert all(row.revoked_at is not None for row in rows)
    finally:
        async with admin.begin() as connection:
            exists = await connection.scalar(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"
                ),
                {"role": role},
            )
            if exists:
                role_ident = connection.dialect.identifier_preparer.quote(role)
                await connection.exec_driver_sql(f"DROP OWNED BY {role_ident}")
                await connection.exec_driver_sql(f"DROP ROLE {role_ident}")
        await admin.dispose()
