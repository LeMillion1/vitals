"""Aggregate-only restore proof for the split PostgreSQL roles."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import scripts.validate_runtime_rls as validator


ROOT = Path(__file__).resolve().parents[1]


def test_validator_entrypoint_resolves_project_from_foreign_cwd(tmp_path):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "PYTHONPATH",
            "VITALS_DATABASE_URL",
            "VITALS_MIGRATION_DATABASE_URL",
            "VITALS_WORKER_DATABASE_URL",
        }
    }

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_runtime_rls.py")],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr == b""
    assert json.loads(result.stdout) == {
        "error_code": "database_url_missing",
        "operation": "validate_runtime_rls",
        "result": "error",
    }


def _url(role: str, database: str = "vitals") -> str:
    return f"postgresql+asyncpg://{role}:synthetic@db:5432/{database}"


@pytest.mark.parametrize(
    ("environ", "code"),
    [
        ({}, "database_url_missing"),
        (
            {
                "VITALS_MIGRATION_DATABASE_URL": "sqlite+aiosqlite:///vitals.db",
                "VITALS_DATABASE_URL": _url("runtime"),
                "VITALS_WORKER_DATABASE_URL": _url("worker"),
            },
            "database_url_driver_invalid",
        ),
        (
            {
                "VITALS_MIGRATION_DATABASE_URL": _url("same"),
                "VITALS_DATABASE_URL": _url("same"),
                "VITALS_WORKER_DATABASE_URL": _url("worker"),
            },
            "database_roles_not_distinct",
        ),
        (
            {
                "VITALS_MIGRATION_DATABASE_URL": _url("owner"),
                "VITALS_DATABASE_URL": _url("runtime", "other"),
                "VITALS_WORKER_DATABASE_URL": _url("worker"),
            },
            "database_endpoint_mismatch",
        ),
    ],
)
def test_database_configuration_fails_with_bounded_codes(environ, code):
    with pytest.raises(validator.RuntimeRlsValidationError, match=f"^{code}$"):
        validator._database_urls(environ)


def test_cli_rejects_arguments_as_one_strict_json_object(capsys):
    assert validator.main(["--unexpected"]) == 2
    rendered = capsys.readouterr().out.strip()
    assert json.loads(rendered) == {
        "error_code": "invalid_arguments",
        "operation": "validate_runtime_rls",
        "result": "error",
    }
    assert rendered.count("\n") == 0


def test_cli_reports_missing_configuration_without_environment_values(
    monkeypatch, capsys
):
    monkeypatch.delenv("VITALS_MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.delenv("VITALS_DATABASE_URL", raising=False)
    monkeypatch.delenv("VITALS_WORKER_DATABASE_URL", raising=False)

    assert validator.main([]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error_code": "database_url_missing",
        "operation": "validate_runtime_rls",
        "result": "error",
    }


def test_cli_does_not_render_database_exception_details(monkeypatch, capsys):
    monkeypatch.setenv("VITALS_MIGRATION_DATABASE_URL", _url("owner"))
    monkeypatch.setenv("VITALS_DATABASE_URL", _url("runtime"))
    monkeypatch.setenv("VITALS_WORKER_DATABASE_URL", _url("worker"))

    def fail(coroutine):
        coroutine.close()
        raise sa.exc.OperationalError(
            "statement",
            {"password": "must-not-leak"},
            RuntimeError("postgresql+asyncpg://private"),
        )

    monkeypatch.setattr(validator.asyncio, "run", fail)

    assert validator.main([]) == 1
    rendered = capsys.readouterr().out.strip()
    assert json.loads(rendered)["error_code"] == "database_error"
    assert "must-not-leak" not in rendered
    assert "postgresql" not in rendered


def test_required_table_inventory_refuses_a_partial_schema(monkeypatch):
    monkeypatch.setattr(
        validator,
        "_policy_kinds",
        lambda: {"supplements": "subject", "weight_logs": "subject"},
    )

    with pytest.raises(
        validator.RuntimeRlsValidationError,
        match="^required_subject_tables_missing$",
    ):
        validator._required_subject_tables({"supplements": (True, True)})


def test_policy_inventory_covers_every_current_migration_declared_table():
    kinds = validator._policy_kinds()

    assert len(kinds) == 71
    assert list(kinds.values()).count("platform") == 61
    assert list(kinds.values()).count("shared") == 5
    assert list(kinds.values()).count("subject") == 5
    assert "body_scan_metrics" in kinds
    assert "system_alerts" in kinds
    assert "mcp_access_tokens" in kinds
    assert not validator._DROPPED_POLICY_TABLES & kinds.keys()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_proves_forced_rls_with_split_roles(monkeypatch):
    raw_admin_url = os.getenv("VITALS_TEST_DATABASE_URL")
    if not raw_admin_url or not raw_admin_url.startswith("postgresql+asyncpg://"):
        pytest.skip("integration test requires VITALS_TEST_DATABASE_URL")

    admin_url = make_url(raw_admin_url)
    suffix = secrets.token_hex(6)
    database = f"vitals_rls_validator_{suffix}"
    migration_role = f"vitals_rls_owner_{suffix}"
    runtime_role = f"vitals_rls_runtime_{suffix}"
    worker_role = f"vitals_rls_worker_{suffix}"
    migration_password = secrets.token_urlsafe(24)
    runtime_password = secrets.token_urlsafe(24)
    worker_password = secrets.token_urlsafe(24)
    migration_url = admin_url.set(
        username=migration_role,
        password=migration_password,
        database=database,
    )
    runtime_url = admin_url.set(
        username=runtime_role,
        password=runtime_password,
        database=database,
    )
    worker_url = admin_url.set(
        username=worker_role,
        password=worker_password,
        database=database,
    )
    admin = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    database_created = False
    roles_created = False
    capability_role: str | None = None
    try:
        async with admin.connect() as connection:
            preparer = connection.dialect.identifier_preparer
            database_ident = preparer.quote(database)
            migration_ident = preparer.quote(migration_role)
            runtime_ident = preparer.quote(runtime_role)
            worker_ident = preparer.quote(worker_role)
            migration_literal = await connection.scalar(
                sa.text("SELECT quote_literal(:password)"),
                {"password": migration_password},
            )
            runtime_literal = await connection.scalar(
                sa.text("SELECT quote_literal(:password)"),
                {"password": runtime_password},
            )
            worker_literal = await connection.scalar(
                sa.text("SELECT quote_literal(:password)"),
                {"password": worker_password},
            )
            await connection.exec_driver_sql(
                f"CREATE ROLE {migration_ident} LOGIN BYPASSRLS "
                f"PASSWORD {migration_literal}"
            )
            await connection.exec_driver_sql(
                f"CREATE ROLE {runtime_ident} LOGIN NOSUPERUSER NOBYPASSRLS "
                f"PASSWORD {runtime_literal}"
            )
            await connection.exec_driver_sql(
                f"CREATE ROLE {worker_ident} LOGIN NOSUPERUSER NOINHERIT "
                f"NOBYPASSRLS PASSWORD {worker_literal}"
            )
            roles_created = True
            await connection.exec_driver_sql(
                f"CREATE DATABASE {database_ident} OWNER {migration_ident}"
            )
            database_created = True

        migration = create_async_engine(migration_url, poolclass=NullPool)
        try:
            first_subject = uuid.UUID(int=1)
            second_subject = uuid.UUID(int=2)
            third_subject = uuid.UUID(int=3)
            async with migration.begin() as connection:
                runtime_ident = connection.dialect.identifier_preparer.quote(
                    runtime_role
                )
                await connection.exec_driver_sql(
                    "CREATE TABLE health_subjects (id uuid PRIMARY KEY)"
                )
                await connection.execute(
                    sa.text(
                        "INSERT INTO health_subjects (id) "
                        "VALUES (:a), (:b), (:c)"
                    ),
                    {
                        "a": first_subject,
                        "b": second_subject,
                        "c": third_subject,
                    },
                )
                await connection.exec_driver_sql(
                    "CREATE TABLE supplements ("
                    "id integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
                    "subject_id uuid REFERENCES health_subjects(id))"
                )
                await connection.execute(
                    sa.text(
                        "INSERT INTO supplements (subject_id) "
                        "VALUES (:first), (:second), (:third), (NULL)"
                    ),
                    {
                        "first": first_subject,
                        "second": second_subject,
                        "third": third_subject,
                    },
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE supplements ENABLE ROW LEVEL SECURITY"
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE supplements FORCE ROW LEVEL SECURITY"
                )
                subject_predicate = (
                    "subject_id = NULLIF(current_setting("
                    "'vitals.subject_id', true), '')::uuid"
                )
                platform_predicate = (
                    f"(subject_id IS NULL OR {subject_predicate} OR (current_setting("
                    "'vitals.platform_scope', true) = 'on' AND "
                    f"{validator.PLATFORM_CAPABILITY_PREDICATE}))"
                )
                await connection.exec_driver_sql(
                    "CREATE POLICY rls_subject_isolation ON supplements "
                    f"USING ({platform_predicate}) "
                    f"WITH CHECK ({platform_predicate})"
                )
                await connection.exec_driver_sql(
                    f"GRANT USAGE ON SCHEMA public TO {runtime_ident}"
                )
                await connection.exec_driver_sql(
                    f"GRANT SELECT ON supplements TO {runtime_ident}"
                )
                worker_ident = connection.dialect.identifier_preparer.quote(
                    worker_role
                )
                await connection.exec_driver_sql(
                    f"GRANT USAGE ON SCHEMA public TO {worker_ident}"
                )
                await connection.exec_driver_sql(
                    f"GRANT SELECT ON supplements TO {worker_ident}"
                )
                database_oid = int(
                    await connection.scalar(
                        sa.text(
                            "SELECT oid FROM pg_database "
                            "WHERE datname=current_database()"
                        )
                    )
                )
        finally:
            await migration.dispose()

        capability_role = (
            f"{validator.PLATFORM_CAPABILITY_ROLE_PREFIX}{database_oid}"
        )
        async with admin.connect() as connection:
            capability_ident = connection.dialect.identifier_preparer.quote(
                capability_role
            )
            worker_ident = connection.dialect.identifier_preparer.quote(worker_role)
            await connection.exec_driver_sql(
                f"CREATE ROLE {capability_ident} NOLOGIN NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
            )
            await connection.exec_driver_sql(
                f"GRANT {capability_ident} TO {worker_ident}"
            )

        monkeypatch.setattr(
            validator,
            "_policy_kinds",
            lambda: {"supplements": "shared"},
        )

        result = await validator.validate_runtime_rls(
            migration_url=migration_url,
            runtime_url=runtime_url,
            worker_url=worker_url,
        )

        assert result == {
            "bound_visible_rows": 4,
            "forced_rls_tables": 1,
            "inspected_subject_rows": 2,
            "operation": "validate_runtime_rls",
            "platform_visible_rows": 4,
            "required_subject_tables": 1,
            "result": "ok",
            "subjects": 3,
            "unbound_visible_rows": 1,
            "validated_subjects": 2,
        }

        async with admin.begin() as connection:
            capability_ident = connection.dialect.identifier_preparer.quote(
                capability_role
            )
            await connection.exec_driver_sql(
                f"ALTER ROLE {capability_ident} INHERIT"
            )
        with pytest.raises(
            validator.RuntimeRlsValidationError,
            match="^platform_capability_invalid$",
        ):
            await validator.validate_runtime_rls(
                migration_url=migration_url,
                runtime_url=runtime_url,
                worker_url=worker_url,
            )
        async with admin.begin() as connection:
            capability_ident = connection.dialect.identifier_preparer.quote(
                capability_role
            )
            await connection.exec_driver_sql(
                f"ALTER ROLE {capability_ident} NOINHERIT"
            )

        migration = create_async_engine(migration_url, poolclass=NullPool)
        try:
            async with migration.begin() as connection:
                await connection.exec_driver_sql(
                    "DROP POLICY rls_subject_isolation ON supplements"
                )
                permissive = (
                    "((subject_id = subject_id) AND "
                    "current_setting('vitals.subject_id', true) <> '')"
                )
                await connection.exec_driver_sql(
                    "CREATE POLICY rls_subject_isolation ON supplements "
                    f"USING ({permissive}) WITH CHECK ({permissive})"
                )
        finally:
            await migration.dispose()

        with pytest.raises(
            validator.RuntimeRlsValidationError,
            match="^required_table_policy_invalid$",
        ):
            await validator.validate_runtime_rls(
                migration_url=migration_url,
                runtime_url=runtime_url,
                worker_url=worker_url,
            )

        migration = create_async_engine(migration_url, poolclass=NullPool)
        try:
            async with migration.begin() as connection:
                await connection.exec_driver_sql(
                    "DROP POLICY rls_subject_isolation ON supplements"
                )
                subject = (
                    "subject_id = NULLIF(current_setting("
                    "'vitals.subject_id', true), '')::uuid"
                )
                await connection.exec_driver_sql(
                    "CREATE POLICY rls_subject_isolation ON supplements "
                    f"USING ({subject}) WITH CHECK (subject_id = subject_id)"
                )
        finally:
            await migration.dispose()

        with pytest.raises(
            validator.RuntimeRlsValidationError,
            match="^required_table_policy_invalid$",
        ):
            await validator.validate_runtime_rls(
                migration_url=migration_url,
                runtime_url=runtime_url,
                worker_url=worker_url,
            )
    finally:
        if database_created or roles_created:
            async with admin.connect() as connection:
                preparer = connection.dialect.identifier_preparer
                if database_created:
                    await connection.exec_driver_sql(
                        f"DROP DATABASE {preparer.quote(database)} WITH (FORCE)"
                    )
                if roles_created:
                    if capability_role is not None:
                        await connection.exec_driver_sql(
                            f"DROP ROLE IF EXISTS {preparer.quote(capability_role)}"
                        )
                    await connection.exec_driver_sql(
                        f"DROP ROLE IF EXISTS {preparer.quote(worker_role)}"
                    )
                    await connection.exec_driver_sql(
                        f"DROP ROLE IF EXISTS {preparer.quote(runtime_role)}"
                    )
                    await connection.exec_driver_sql(
                        f"DROP ROLE IF EXISTS {preparer.quote(migration_role)}"
                    )
        await admin.dispose()
