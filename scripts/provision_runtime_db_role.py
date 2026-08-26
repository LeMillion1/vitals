"""Provision the least-privileged PostgreSQL role used by the web runtime.

The migration connection owns the schema and may bypass row-level security.
The runtime connection must be a different login: it receives ordinary DML
grants, owns no relation, and is explicitly stripped of privileged attributes.
"""

from __future__ import annotations

import asyncio
import json
import os

import sqlalchemy as sa
from dotenv import load_dotenv
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine


def _postgres_url(name: str) -> URL:
    raw = (os.getenv(name) or "").strip()
    try:
        url = make_url(raw)
    except sa.exc.ArgumentError as exc:
        raise SystemExit(f"{name} must be a valid PostgreSQL URL") from exc
    if url.drivername != "postgresql+asyncpg":
        raise SystemExit(f"{name} must use postgresql+asyncpg")
    if not url.username or not url.password or not url.database:
        raise SystemExit(f"{name} must include username, password, and database")
    return url


def _quoted_identifier(connection, value: str) -> str:
    return connection.dialect.identifier_preparer.quote(value)


async def provision_runtime_role(*, migration_url: URL, runtime_url: URL) -> dict:
    migration_role = migration_url.username
    runtime_role = runtime_url.username
    if migration_role == runtime_role:
        raise RuntimeError("migration and runtime database roles must be different")
    migration_target = (
        migration_url.host,
        migration_url.port or 5432,
        migration_url.database,
    )
    runtime_target = (runtime_url.host, runtime_url.port or 5432, runtime_url.database)
    if migration_target != runtime_target:
        raise RuntimeError("migration and runtime URLs must select the same database")

    engine = create_async_engine(migration_url)
    try:
        async with engine.begin() as connection:
            connected_role = await connection.scalar(sa.text("SELECT current_user"))
            database = await connection.scalar(sa.text("SELECT current_database()"))
            if connected_role != migration_role or database != migration_url.database:
                raise RuntimeError("migration connection identity does not match its URL")

            runtime_ident = _quoted_identifier(connection, runtime_role)
            migration_ident = _quoted_identifier(connection, migration_role)
            database_ident = _quoted_identifier(connection, database)
            password_literal = await connection.scalar(
                sa.text("SELECT quote_literal(:password)"),
                {"password": runtime_url.password},
            )
            exists = bool(
                await connection.scalar(
                    sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"),
                    {"role": runtime_role},
                )
            )
            if not exists:
                await connection.exec_driver_sql(
                    f"CREATE ROLE {runtime_ident} LOGIN PASSWORD {password_literal}"
                )
            await connection.exec_driver_sql(
                f"ALTER ROLE {runtime_ident} WITH LOGIN PASSWORD {password_literal} "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION "
                "NOBYPASSRLS"
            )

            owned_relations = int(
                await connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM pg_class c "
                        "JOIN pg_roles r ON r.oid=c.relowner "
                        "WHERE r.rolname=:role AND c.relkind IN "
                        "('r','p','v','m','S','f')"
                    ),
                    {"role": runtime_role},
                )
                or 0
            )
            if owned_relations:
                raise RuntimeError("runtime database role must not own relations")

            await connection.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {database_ident} TO {runtime_ident}"
            )
            await connection.exec_driver_sql(
                f"GRANT USAGE ON SCHEMA public TO {runtime_ident}"
            )
            await connection.exec_driver_sql(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                f"IN SCHEMA public TO {runtime_ident}"
            )
            await connection.exec_driver_sql(
                "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES "
                f"IN SCHEMA public TO {runtime_ident}"
            )
            await connection.exec_driver_sql(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration_ident} "
                "IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE "
                f"ON TABLES TO {runtime_ident}"
            )
            await connection.exec_driver_sql(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration_ident} "
                "IN SCHEMA public GRANT USAGE, SELECT, UPDATE "
                f"ON SEQUENCES TO {runtime_ident}"
            )
    finally:
        await engine.dispose()

    return {
        "status": "completed",
        "database": migration_url.database,
        "migration_role": migration_role,
        "runtime_role": runtime_role,
        "owned_relations": 0,
        "superuser": False,
        "bypass_rls": False,
    }


def main() -> None:
    load_dotenv()
    migration_url = _postgres_url("VITALS_MIGRATION_DATABASE_URL")
    runtime_url = _postgres_url("VITALS_DATABASE_URL")
    result = asyncio.run(
        provision_runtime_role(
            migration_url=migration_url,
            runtime_url=runtime_url,
        )
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
