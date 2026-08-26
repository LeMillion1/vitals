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
    parent_role = f"vitals_parent_test_{suffix}"
    password = secrets.token_urlsafe(24)
    table = f"runtime_grant_test_{suffix}"
    enum_type = f"runtime_type_test_{suffix}"
    owned_schema = f"runtime_schema_test_{suffix}"
    owned_function = f"runtime_function_test_{suffix}"
    runtime_url = admin_url.set(username=role, password=password)
    admin = create_async_engine(admin_url)
    large_object_oid: int | None = None

    try:
        result = await provision_runtime_role(
            migration_url=admin_url,
            runtime_url=runtime_url,
        )
        assert result["runtime_role"] == role
        assert result["owned_objects"] == 0
        assert result["role_memberships"] == 0
        assert result["role_settings"] == 0
        assert result["extra_privileges"] == 0

        async with admin.begin() as connection:
            role_ident = connection.dialect.identifier_preparer.quote(role)
            parent_ident = connection.dialect.identifier_preparer.quote(parent_role)
            database_ident = connection.dialect.identifier_preparer.quote(
                admin_url.database
            )
            await connection.exec_driver_sql(f"CREATE ROLE {parent_ident}")
            await connection.exec_driver_sql(
                f"GRANT {parent_ident} TO {role_ident}"
            )
            await connection.exec_driver_sql(
                f"ALTER ROLE {role_ident} SET vitals.platform_scope TO 'on'"
            )
            await connection.exec_driver_sql(
                f"ALTER ROLE {role_ident} IN DATABASE {database_ident} "
                "SET statement_timeout TO '0'"
            )
            await connection.exec_driver_sql(
                f"ALTER DATABASE {database_ident} OWNER TO {role_ident}"
            )
            await connection.exec_driver_sql(
                f'CREATE SCHEMA "{owned_schema}" AUTHORIZATION {role_ident}'
            )
            await connection.exec_driver_sql(f'CREATE TABLE "{table}" (id integer)')
            await connection.exec_driver_sql(
                f'ALTER TABLE "{table}" OWNER TO {role_ident}'
            )
            await connection.exec_driver_sql(
                f'CREATE FUNCTION "{owned_function}"() RETURNS integer '
                "LANGUAGE sql AS 'SELECT 1'"
            )
            await connection.exec_driver_sql(
                f'ALTER FUNCTION "{owned_function}"() OWNER TO {role_ident}'
            )
            await connection.exec_driver_sql(
                f'GRANT TRUNCATE, REFERENCES, TRIGGER ON "{table}" TO {role_ident}'
            )
            await connection.exec_driver_sql(
                f'GRANT TRUNCATE ON "{table}" TO PUBLIC'
            )
            await connection.exec_driver_sql(
                f"GRANT CREATE ON SCHEMA public TO {role_ident}"
            )
            await connection.exec_driver_sql("GRANT CREATE ON SCHEMA public TO PUBLIC")
            await connection.exec_driver_sql(
                f'CREATE TYPE "{enum_type}" AS ENUM (\'synthetic\')'
            )
            await connection.exec_driver_sql(
                f'GRANT USAGE ON TYPE "{enum_type}" TO {role_ident}'
            )
            await connection.exec_driver_sql(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {parent_ident} "
                "IN SCHEMA public GRANT TRUNCATE ON TABLES "
                f"TO {role_ident}"
            )
            await connection.exec_driver_sql(
                f"GRANT SET ON PARAMETER session_replication_role TO {role_ident}"
            )
            large_object_oid = int(
                await connection.scalar(sa.text("SELECT lo_create(0)"))
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT, UPDATE ON LARGE OBJECT {large_object_oid} "
                f"TO {role_ident}, PUBLIC"
            )

        converged = await provision_runtime_role(
            migration_url=admin_url,
            runtime_url=runtime_url,
        )
        assert converged["owned_objects"] == 0
        assert converged["role_memberships"] == 0
        assert converged["role_settings"] == 0
        assert converged["extra_privileges"] == 0

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
                assert not await connection.scalar(
                    sa.text(
                        "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
                    )
                )
                assert not await connection.scalar(
                    sa.text(
                        "SELECT has_database_privilege("
                        "current_user, current_database(), 'TEMPORARY')"
                    )
                )
                assert not await connection.scalar(
                    sa.text(
                        "SELECT has_parameter_privilege("
                        "current_user, 'session_replication_role', 'SET')"
                    )
                )
                assert not await connection.scalar(
                    sa.text(
                        f"SELECT has_table_privilege(current_user, 'public.\"{table}\"', "
                        "'TRUNCATE')"
                    )
                )
                assert not await connection.scalar(
                    sa.text(
                        "SELECT EXISTS (SELECT 1 FROM pg_default_acl defaults "
                        "CROSS JOIN LATERAL aclexplode(defaults.defaclacl) acl "
                        "JOIN pg_roles grantor ON grantor.oid=defaults.defaclrole "
                        "WHERE acl.grantee=(SELECT oid FROM pg_roles "
                        "WHERE rolname=current_user) "
                        "AND (grantor.rolname=:parent "
                        "OR upper(acl.privilege_type)='TRUNCATE'))"
                    ),
                    {"parent": parent_role},
                )
                assert not await connection.scalar(
                    sa.text(
                        f'SELECT has_function_privilege(current_user, '
                        f"'public.\"{owned_function}\"()', 'EXECUTE')"
                    )
                )
                assert not await connection.scalar(
                    sa.text(
                        "SELECT EXISTS (SELECT 1 FROM pg_largeobject_metadata object "
                        "CROSS JOIN LATERAL aclexplode(object.lomacl) acl "
                        "WHERE object.oid=:oid AND (acl.grantee=0 OR acl.grantee=("
                        "SELECT oid FROM pg_roles WHERE rolname=current_user)))"
                    ),
                    {"oid": large_object_oid},
                )
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
            database_ident = connection.dialect.identifier_preparer.quote(
                admin_url.database
            )
            admin_ident = connection.dialect.identifier_preparer.quote(
                admin_url.username
            )
            await connection.exec_driver_sql(
                f"ALTER DATABASE {database_ident} OWNER TO {admin_ident}"
            )
            await connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}"')
            await connection.exec_driver_sql(
                f'DROP FUNCTION IF EXISTS "{owned_function}"()'
            )
            await connection.exec_driver_sql(f'DROP TYPE IF EXISTS "{enum_type}"')
            await connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{owned_schema}" CASCADE'
            )
            if large_object_oid is not None:
                await connection.execute(
                    sa.text("SELECT lo_unlink(:oid)"), {"oid": large_object_oid}
                )
            exists = await connection.scalar(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"),
                {"role": role},
            )
            if exists:
                await connection.exec_driver_sql(f'DROP OWNED BY "{role}"')
                await connection.exec_driver_sql(f'DROP ROLE "{role}"')
            parent_exists = await connection.scalar(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"),
                {"role": parent_role},
            )
            if parent_exists:
                await connection.exec_driver_sql(f'DROP ROLE "{parent_role}"')
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
