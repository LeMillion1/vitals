"""Safe operator-file contracts for migration-owner password rotation."""

from __future__ import annotations

import os
import secrets
import sys

import pytest
import sqlalchemy as sa
from dotenv import dotenv_values
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

import scripts.rotate_migration_db_password as rotation_module
from scripts.rotate_migration_db_password import (
    MigrationPasswordRotationError,
    _migration_url,
    _updated_operator_content,
    rotate_migration_password,
)


def _content() -> str:
    return (
        "# synthetic operator file\n"
        "VITALS_DB_USER=vitals_admin\n"
        "VITALS_DB_PASSWORD=old-password\n"
        "VITALS_DB_NAME=vitals_db\n"
        "VITALS_MIGRATION_DATABASE_URL="
        "postgresql+asyncpg://vitals_admin:old-password@vitals_db:5432/vitals_db\n"
        "VITALS_DATABASE_URL="
        "postgresql+asyncpg://vitals_runtime:runtime-password@vitals_db:5432/vitals_db\n"
    )


def test_operator_update_changes_only_the_two_owner_password_fields(tmp_path):
    updated = _updated_operator_content(
        _content(),
        {
            "VITALS_DB_PASSWORD": "new-password",
            "VITALS_MIGRATION_DATABASE_URL": (
                "postgresql+asyncpg://vitals_admin:new-password@vitals_db:5432/"
                "vitals_db"
            ),
        },
    )
    path = tmp_path / ".env"
    path.write_text(updated, encoding="utf-8")
    values = dotenv_values(path)

    assert values["VITALS_DB_PASSWORD"] == "new-password"
    assert "new-password" in values["VITALS_MIGRATION_DATABASE_URL"]
    assert values["VITALS_DATABASE_URL"].endswith(
        "vitals_runtime:runtime-password@vitals_db:5432/vitals_db"
    )
    assert "old-password" not in updated
    assert updated.count("VITALS_DB_PASSWORD=") == 1
    assert updated.count("VITALS_MIGRATION_DATABASE_URL=") == 1


@pytest.mark.parametrize(
    "content",
    [
        _content().replace("VITALS_DB_PASSWORD=old-password\n", ""),
        _content() + "VITALS_DB_PASSWORD=duplicate\n",
    ],
)
def test_operator_update_refuses_missing_or_duplicate_keys(content):
    with pytest.raises(
        MigrationPasswordRotationError,
        match="operator_key_cardinality_invalid",
    ):
        _updated_operator_content(
            content,
            {
                "VITALS_DB_PASSWORD": "new-password",
                "VITALS_MIGRATION_DATABASE_URL": (
                    "postgresql+asyncpg://vitals_admin:new-password@db/vitals_db"
                ),
            },
        )


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("VITALS_DB_PASSWORD", "different", "database_password_mismatch"),
        ("VITALS_DB_USER", "other", "database_user_mismatch"),
        ("VITALS_DB_NAME", "other", "database_name_mismatch"),
    ],
)
def test_rotation_preflight_refuses_inconsistent_operator_identity(
    tmp_path,
    key,
    value,
    reason,
):
    path = tmp_path / ".env"
    path.write_text(_content(), encoding="utf-8")
    values = dict(dotenv_values(path))
    values[key] = value

    with pytest.raises(MigrationPasswordRotationError, match=reason):
        _migration_url(values)


def test_operator_update_never_accepts_newlines():
    with pytest.raises(MigrationPasswordRotationError, match="unsafe_update"):
        _updated_operator_content(
            _content(),
            {
                "VITALS_DB_PASSWORD": "new\nVITALS_DATABASE_URL=stolen",
                "VITALS_MIGRATION_DATABASE_URL": "postgresql+asyncpg://safe",
            },
        )


def test_cli_reports_only_a_bounded_reason_code(monkeypatch, capsys, tmp_path):
    def fail(_coroutine):
        _coroutine.close()
        raise MigrationPasswordRotationError("operator_env_publish_failed")

    monkeypatch.setattr(rotation_module.asyncio, "run", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        ["rotate", "--env-file", str(tmp_path / ".env")],
    )

    assert rotation_module.main() == 1
    assert capsys.readouterr().err.strip() == (
        '{"operation": "rotate_migration_db_password", '
        '"reason": "operator_env_publish_failed", "result": "error"}'
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_role_and_operator_file_rotate_together(tmp_path):
    raw_admin_url = os.getenv("VITALS_TEST_DATABASE_URL")
    if not raw_admin_url or not raw_admin_url.startswith("postgresql+asyncpg://"):
        pytest.skip("integration test requires VITALS_TEST_DATABASE_URL")

    admin_url = make_url(raw_admin_url)
    suffix = secrets.token_hex(6)
    role = f"vitals_rotation_test_{suffix}"
    old_password = secrets.token_urlsafe(24)
    new_password = secrets.token_urlsafe(24)
    role_url = admin_url.set(username=role, password=old_password)
    admin = create_async_engine(admin_url)
    role_ident = admin.dialect.identifier_preparer.quote(role)
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"VITALS_DB_USER={role}\n"
        f"VITALS_DB_PASSWORD={old_password}\n"
        f"VITALS_DB_NAME={admin_url.database}\n"
        "VITALS_MIGRATION_DATABASE_URL="
        f"{role_url.render_as_string(hide_password=False)}\n",
        encoding="utf-8",
    )

    try:
        async with admin.begin() as connection:
            old_literal = await connection.scalar(
                sa.text("SELECT quote_literal(:password)"),
                {"password": old_password},
            )
            await connection.exec_driver_sql(
                f"CREATE ROLE {role_ident} LOGIN PASSWORD {old_literal}"
            )
            database_ident = admin.dialect.identifier_preparer.quote(
                admin_url.database
            )
            await connection.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {database_ident} TO {role_ident}"
            )

        result = await rotate_migration_password(
            env_path,
            new_password=new_password,
        )

        assert result == {
            "database": admin_url.database,
            "operation": "rotate_migration_db_password",
            "result": "ok",
            "role": role,
        }
        values = dotenv_values(env_path)
        assert values["VITALS_DB_PASSWORD"] == new_password
        rotated_url = make_url(values["VITALS_MIGRATION_DATABASE_URL"])
        assert rotated_url.password == new_password

        rotated = create_async_engine(rotated_url)
        try:
            async with rotated.connect() as connection:
                assert await connection.scalar(sa.text("SELECT current_user")) == role
        finally:
            await rotated.dispose()
    finally:
        async with admin.begin() as connection:
            exists = await connection.scalar(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"),
                {"role": role},
            )
            if exists:
                database_ident = admin.dialect.identifier_preparer.quote(
                    admin_url.database
                )
                await connection.exec_driver_sql(
                    f"REVOKE CONNECT ON DATABASE {database_ident} FROM {role_ident}"
                )
                await connection.exec_driver_sql(f"DROP ROLE {role_ident}")
        await admin.dispose()
