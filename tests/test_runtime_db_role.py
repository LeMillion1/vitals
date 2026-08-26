"""Contracts for the split migration and application PostgreSQL roles."""

from __future__ import annotations

import os
import secrets
from importlib import import_module

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.provision_runtime_db_role import (
    RUNTIME_EXECUTE_ROUTINES,
    WORKER_EXECUTE_ROUTINES,
    provision_runtime_role,
    provision_runtime_roles,
)


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
    allowed_routines_created: list[str] = []

    try:
        async with admin.begin() as connection:
            synthetic_sql = {
                RUNTIME_EXECUTE_ROUTINES[0]: (
                    "CREATE FUNCTION public."
                    "authorize_and_lock_professional_invitation(text, uuid, text) "
                    "RETURNS TABLE(invitation_id uuid, subject_id uuid) "
                    "LANGUAGE plpgsql VOLATILE SECURITY DEFINER "
                    "SET search_path = pg_catalog, pg_temp "
                    "SET row_security = off AS 'BEGIN RETURN; END'"
                ),
                RUNTIME_EXECUTE_ROUTINES[1]: (
                    "CREATE FUNCTION public.attest_shared_report_token(text) "
                    "RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER "
                    "SET search_path = pg_catalog, pg_temp "
                    "SET row_security = off AS 'BEGIN RETURN 1; END'"
                ),
            }
            for allowed_routine in RUNTIME_EXECUTE_ROUTINES:
                if not await connection.scalar(
                    sa.text("SELECT to_regprocedure(:signature) IS NULL"),
                    {"signature": allowed_routine},
                ):
                    continue
                await connection.exec_driver_sql(synthetic_sql[allowed_routine])
                await connection.exec_driver_sql(
                    f"REVOKE ALL ON FUNCTION {allowed_routine} FROM PUBLIC"
                )
                allowed_routines_created.append(allowed_routine)

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
                for allowed_routine in RUNTIME_EXECUTE_ROUTINES:
                    assert await connection.scalar(
                        sa.text(
                            "SELECT has_function_privilege("
                            "current_user, :signature, 'EXECUTE')"
                        ),
                        {"signature": allowed_routine},
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
            for allowed_routine in allowed_routines_created:
                await connection.exec_driver_sql(
                    f"DROP FUNCTION IF EXISTS {allowed_routine}"
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
async def test_distinct_web_and_worker_logins_have_exact_routine_sets():
    raw_admin_url = os.getenv("VITALS_TEST_DATABASE_URL")
    if not raw_admin_url or not raw_admin_url.startswith("postgresql+asyncpg://"):
        pytest.skip("integration test requires VITALS_TEST_DATABASE_URL")

    admin_url = make_url(raw_admin_url)
    suffix = secrets.token_hex(6)
    web_role = f"vitals_web_test_{suffix}"
    worker_role = f"vitals_worker_test_{suffix}"
    web_url = admin_url.set(username=web_role, password=secrets.token_urlsafe(24))
    worker_url = admin_url.set(
        username=worker_role,
        password=secrets.token_urlsafe(24),
    )
    admin = create_async_engine(admin_url)
    engines = []
    try:
        result = await provision_runtime_roles(
            migration_url=admin_url,
            web_url=web_url,
            worker_url=worker_url,
        )
        assert result["web"]["runtime_role"] == web_role
        assert result["worker"]["runtime_role"] == worker_role
        assert WORKER_EXECUTE_ROUTINES == ()

        for url, expected_routines in (
            (web_url, RUNTIME_EXECUTE_ROUTINES),
            (worker_url, WORKER_EXECUTE_ROUTINES),
        ):
            engine = create_async_engine(url)
            engines.append(engine)
            async with engine.connect() as connection:
                for routine in RUNTIME_EXECUTE_ROUTINES:
                    assert bool(
                        await connection.scalar(
                            sa.text(
                                "SELECT has_function_privilege("
                                "current_user, :routine, 'EXECUTE')"
                            ),
                            {"routine": routine},
                        )
                    ) is (routine in expected_routines)
                attributes = (
                    await connection.execute(
                        sa.text(
                            "SELECT rolsuper, rolbypassrls, rolinherit "
                            "FROM pg_roles WHERE rolname=current_user"
                        )
                    )
                ).one()
                assert tuple(attributes) == (False, False, False)
    finally:
        for engine in engines:
            await engine.dispose()
        async with admin.begin() as connection:
            for role in (web_role, worker_role):
                if await connection.scalar(
                    sa.text(
                        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"
                    ),
                    {"role": role},
                ):
                    role_ident = connection.dialect.identifier_preparer.quote(role)
                    await connection.exec_driver_sql(f"DROP OWNED BY {role_ident}")
                    await connection.exec_driver_sql(f"DROP ROLE {role_ident}")
        await admin.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper_sql",
    (
        "ALTER FUNCTION {signature} SECURITY INVOKER",
        "ALTER FUNCTION {signature} STABLE",
        "ALTER FUNCTION {signature} RESET ALL",
        "ALTER FUNCTION {signature} SET search_path = public",
        "ALTER FUNCTION {signature} OWNER TO {other_owner}",
    ),
    ids=(
        "security-invoker",
        "not-volatile",
        "missing-config",
        "unsafe-search-path",
        "wrong-owner",
    ),
)
@pytest.mark.parametrize(
    "migration_module",
    (
        "migrations.versions.0081_authorize_professional_invitation",
        "migrations.versions.0082_authorize_shared_report_token",
    ),
    ids=("invitation", "shared-report"),
)
async def test_runtime_role_refuses_an_untrusted_required_routine(
    tamper_sql,
    migration_module,
):
    raw_admin_url = os.getenv("VITALS_TEST_DATABASE_URL")
    if not raw_admin_url or not raw_admin_url.startswith("postgresql+asyncpg://"):
        pytest.skip("integration test requires VITALS_TEST_DATABASE_URL")

    admin_url = make_url(raw_admin_url)
    suffix = secrets.token_hex(6)
    runtime_role = f"vitals_untrusted_routine_{suffix}"
    other_owner = f"vitals_untrusted_owner_{suffix}"
    runtime_url = admin_url.set(
        username=runtime_role,
        password=secrets.token_urlsafe(24),
    )
    migrations = tuple(
        import_module(module_name)
        for module_name in (
            "migrations.versions.0081_authorize_professional_invitation",
            "migrations.versions.0082_authorize_shared_report_token",
        )
    )
    migration = import_module(migration_module)
    admin = create_async_engine(admin_url)

    async def install_trusted_routines(connection) -> None:
        for trusted_migration in migrations:
            await connection.exec_driver_sql(
                f"DROP FUNCTION IF EXISTS {trusted_migration.ROUTINE_SIGNATURE}"
            )
            await connection.exec_driver_sql(trusted_migration.CREATE_ROUTINE_SQL)
            await connection.exec_driver_sql(
                "REVOKE ALL ON FUNCTION "
                f"{trusted_migration.ROUTINE_SIGNATURE} FROM PUBLIC"
            )

    try:
        async with admin.begin() as connection:
            await install_trusted_routines(connection)
            other_owner_ident = connection.dialect.identifier_preparer.quote(
                other_owner
            )
            await connection.exec_driver_sql(
                f"CREATE ROLE {other_owner_ident} NOLOGIN"
            )
            await connection.exec_driver_sql(
                tamper_sql.format(
                    signature=migration.ROUTINE_SIGNATURE,
                    other_owner=other_owner_ident,
                )
            )

        with pytest.raises(RuntimeError, match="routine is not trusted"):
            await provision_runtime_role(
                migration_url=admin_url,
                runtime_url=runtime_url,
            )

        async with admin.connect() as connection:
            assert not await connection.scalar(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"
                ),
                {"role": runtime_role},
            )
    finally:
        async with admin.begin() as connection:
            await install_trusted_routines(connection)
            other_owner_ident = connection.dialect.identifier_preparer.quote(
                other_owner
            )
            if await connection.scalar(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"
                ),
                {"role": other_owner},
            ):
                await connection.exec_driver_sql(f"DROP ROLE {other_owner_ident}")
        await admin.dispose()


@pytest.mark.asyncio
async def test_runtime_role_repairs_public_execute_lost_by_plain_dump_restore():
    raw_admin_url = os.getenv("VITALS_TEST_DATABASE_URL")
    if not raw_admin_url or not raw_admin_url.startswith("postgresql+asyncpg://"):
        pytest.skip("integration test requires VITALS_TEST_DATABASE_URL")

    admin_url = make_url(raw_admin_url)
    suffix = secrets.token_hex(6)
    role = f"vitals_restore_acl_test_{suffix}"
    runtime_url = admin_url.set(username=role, password=secrets.token_urlsafe(24))
    signature = RUNTIME_EXECUTE_ROUTINES[0]
    admin = create_async_engine(admin_url)
    try:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(
                f"GRANT EXECUTE ON FUNCTION {signature} TO PUBLIC"
            )
            assert await connection.scalar(
                sa.text(
                    "SELECT has_function_privilege('public', :signature, 'EXECUTE')"
                ),
                {"signature": signature},
            )

        result = await provision_runtime_role(
            migration_url=admin_url,
            runtime_url=runtime_url,
        )
        assert result["extra_privileges"] == 0

        async with admin.connect() as connection:
            assert not await connection.scalar(
                sa.text(
                    "SELECT has_function_privilege('public', :signature, 'EXECUTE')"
                ),
                {"signature": signature},
            )
            assert await connection.scalar(
                sa.text(
                    "SELECT has_function_privilege(:role, :signature, 'EXECUTE')"
                ),
                {"role": role, "signature": signature},
            )
    finally:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(
                f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC"
            )
            if await connection.scalar(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"),
                {"role": role},
            ):
                role_ident = connection.dialect.identifier_preparer.quote(role)
                await connection.exec_driver_sql(f"DROP OWNED BY {role_ident}")
                await connection.exec_driver_sql(f"DROP ROLE {role_ident}")
        await admin.dispose()


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
