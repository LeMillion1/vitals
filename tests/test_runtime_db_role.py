"""Contracts for the split migration and application PostgreSQL roles."""

from __future__ import annotations

import os
import secrets

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.provision_runtime_db_role import provision_runtime_role


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_runtime_role_is_restricted_and_receives_future_table_grants():
    raw_admin_url = os.getenv("VITALS_TEST_DATABASE_URL")
    if not raw_admin_url or not raw_admin_url.startswith("postgresql+asyncpg://"):
        pytest.skip("integration test requires VITALS_TEST_DATABASE_URL")

    admin_url = make_url(raw_admin_url)
    suffix = secrets.token_hex(6)
    role = f"vitals_runtime_test_{suffix}"
    password = secrets.token_urlsafe(24)
    table = f"runtime_grant_test_{suffix}"
    runtime_url = admin_url.set(username=role, password=password)
    admin = create_async_engine(admin_url)

    try:
        result = await provision_runtime_role(
            migration_url=admin_url,
            runtime_url=runtime_url,
        )
        assert result["runtime_role"] == role
        assert result["owned_relations"] == 0

        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'CREATE TABLE "{table}" (id integer)')

        runtime = create_async_engine(runtime_url)
        try:
            async with runtime.begin() as connection:
                attributes = (
                    await connection.execute(
                        sa.text(
                            "SELECT rolsuper, rolcreatedb, rolcreaterole, "
                            "rolinherit, rolreplication, rolbypassrls "
                            "FROM pg_roles WHERE rolname=current_user"
                        )
                    )
                ).one()
                assert tuple(attributes) == (False, False, False, False, False, False)
                await connection.exec_driver_sql(f'INSERT INTO "{table}" VALUES (1)')
                assert (
                    await connection.scalar(
                        sa.text(f'SELECT count(*) FROM "{table}"')
                    )
                    == 1
                )
        finally:
            await runtime.dispose()
    finally:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}"')
            exists = await connection.scalar(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"),
                {"role": role},
            )
            if exists:
                await connection.exec_driver_sql(f'DROP OWNED BY "{role}"')
                await connection.exec_driver_sql(f'DROP ROLE "{role}"')
        await admin.dispose()


@pytest.mark.asyncio
async def test_runtime_role_cannot_equal_migration_role():
    url = make_url("postgresql+asyncpg://same:secret@localhost/vitals")

    with pytest.raises(RuntimeError, match="must be different"):
        await provision_runtime_role(migration_url=url, runtime_url=url)


@pytest.mark.asyncio
async def test_oidc_startup_uses_subject_binding_under_restricted_runtime_role(
    db_session,
    session_factory,
    monkeypatch,
):
    """OIDC readiness needs no BYPASSRLS/platform-wide startup scope."""

    raw_admin_url = os.getenv("VITALS_TEST_DATABASE_URL")
    if not raw_admin_url or not raw_admin_url.startswith("postgresql+asyncpg://"):
        pytest.skip("integration test requires VITALS_TEST_DATABASE_URL")

    from vitals.enums import UserStatus
    from vitals.models.identity import HealthSubject, User, UserFederatedIdentity
    from web.main import _bootstrap_legacy_identity, _load_oidc_identity_state

    expected = await _bootstrap_legacy_identity(
        session_factory,
        timezone="Asia/Almaty",
    )
    await db_session.rollback()

    for name, value in (
        ("VITALS_OIDC_ISSUER", "https://idp.example.test"),
        ("VITALS_OIDC_CLIENT_ID", "vitals"),
        ("VITALS_OIDC_CLIENT_SECRET", "synthetic-secret"),
        (
            "VITALS_OIDC_REDIRECT_URL",
            "https://vitals.example.test/auth/callback",
        ),
        ("VITALS_OIDC_BOOTSTRAP_SUBJECT", "provider-owner-subject"),
        ("VITALS_PUBLIC_URL", "https://vitals.example.test"),
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("VITALS_AUTH_USERNAME", raising=False)
    monkeypatch.delenv("VITALS_AUTH_PASSWORD_HASH", raising=False)

    admin_url = make_url(raw_admin_url)
    suffix = secrets.token_hex(6)
    role = f"vitals_oidc_runtime_test_{suffix}"
    runtime_url = admin_url.set(username=role, password=secrets.token_urlsafe(24))
    admin = create_async_engine(admin_url)
    runtime = None
    try:
        await provision_runtime_role(
            migration_url=admin_url,
            runtime_url=runtime_url,
        )
        runtime = create_async_engine(runtime_url)
        runtime_factory = async_sessionmaker(runtime, expire_on_commit=False)

        assert await _load_oidc_identity_state(runtime_factory) == expected

        owner = await db_session.scalar(sa.select(User))
        db_session.add(
            UserFederatedIdentity(
                user_id=owner.id,
                issuer="https://idp.example.test",
                subject="provider-owner-subject",
            )
        )
        second = User(
            username="second-person",
            normalized_username="second-person",
            password_hash=None,
            status=UserStatus.ACTIVE.value,
        )
        db_session.add(second)
        await db_session.flush()
        db_session.add(
            HealthSubject(
                owner_user_id=second.id,
                display_name="Second person",
                timezone="Asia/Almaty",
            )
        )
        await db_session.commit()

        assert await _load_oidc_identity_state(runtime_factory) is None
    finally:
        if runtime is not None:
            await runtime.dispose()
        async with admin.begin() as connection:
            exists = await connection.scalar(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"),
                {"role": role},
            )
            if exists:
                await connection.exec_driver_sql(f'DROP OWNED BY "{role}"')
                await connection.exec_driver_sql(f'DROP ROLE "{role}"')
        await admin.dispose()
