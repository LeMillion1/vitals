#!/usr/bin/env python3
"""Prove the restored PostgreSQL subject boundary with aggregate-only output.

The migration role is used only for cross-subject aggregate reads.  Every
subject-data read through the runtime role must fail closed until the
transaction is bound to one subject, and every declared strict subject table
must have forced row-level security.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from vitals.ownership import OWNERSHIP_REGISTRY, TargetColumn


OPERATION = "validate_runtime_rls"
SUBJECT_SETTING = "vitals.subject_id"
_ERROR_CODES = frozenset(
    {
        "bound_subject_rows_mismatch",
        "cancelled",
        "database_endpoint_mismatch",
        "database_identity_mismatch",
        "database_roles_not_distinct",
        "database_url_driver_invalid",
        "database_url_incomplete",
        "database_url_invalid",
        "database_url_missing",
        "migration_identity_mismatch",
        "required_subject_tables_missing",
        "required_table_not_forced",
        "restored_subject_rows_missing",
        "row_security_missing",
        "row_security_not_forced",
        "runtime_identity_mismatch",
        "subject_data_missing",
        "unbound_subject_rows_visible",
    }
)


class RuntimeRlsValidationError(RuntimeError):
    """A bounded restore-validation precondition or proof failed."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_CODES else "internal_error"
        super().__init__(self.code)


def _fail(code: str) -> None:
    raise RuntimeRlsValidationError(code)


def _postgres_url(environ: Mapping[str, str], name: str) -> URL:
    raw = (environ.get(name) or "").strip()
    if not raw:
        _fail("database_url_missing")
    try:
        url = make_url(raw)
    except sa.exc.ArgumentError:
        _fail("database_url_invalid")
    if url.drivername != "postgresql+asyncpg":
        _fail("database_url_driver_invalid")
    if not url.username or not url.password or not url.host or not url.database:
        _fail("database_url_incomplete")
    return url


def _database_urls(environ: Mapping[str, str]) -> tuple[URL, URL]:
    migration = _postgres_url(environ, "VITALS_MIGRATION_DATABASE_URL")
    runtime = _postgres_url(environ, "VITALS_DATABASE_URL")
    if migration.username == runtime.username:
        _fail("database_roles_not_distinct")
    migration_endpoint = (
        migration.host.lower(),
        migration.port or 5432,
        migration.database,
    )
    runtime_endpoint = (
        runtime.host.lower(),
        runtime.port or 5432,
        runtime.database,
    )
    if migration_endpoint != runtime_endpoint:
        _fail("database_endpoint_mismatch")
    return migration, runtime


async def _connection_identity(connection: Any) -> tuple[str, str, str | None, int]:
    row = (
        await connection.execute(
            sa.text(
                "SELECT current_user, current_database(), "
                "inet_server_addr()::text, inet_server_port()"
            )
        )
    ).one()
    return row[0], row[1], row[2], row[3]


def _quote_table(connection: Any, table_name: str) -> str:
    preparer = connection.dialect.identifier_preparer
    return f"{preparer.quote('public')}.{preparer.quote(table_name)}"


async def validate_runtime_rls(
    *,
    migration_url: URL,
    runtime_url: URL,
) -> dict[str, int | str]:
    """Return aggregate proof results or raise a bounded validation error."""

    migration_engine = create_async_engine(migration_url, poolclass=NullPool)
    runtime_engine = create_async_engine(runtime_url, poolclass=NullPool)
    try:
        async with migration_engine.connect() as migration:
            migration_identity = await _connection_identity(migration)
            if (
                migration_identity[0] != migration_url.username
                or migration_identity[1] != migration_url.database
            ):
                _fail("migration_identity_mismatch")
            subject_count = int(
                await migration.scalar(
                    sa.text("SELECT count(*) FROM public.health_subjects")
                )
                or 0
            )
            subject_id = await migration.scalar(
                sa.text("SELECT id FROM public.health_subjects ORDER BY id LIMIT 1")
            )
            if subject_count < 1 or subject_id is None:
                _fail("subject_data_missing")

            async with runtime_engine.connect() as runtime:
                runtime_identity = await _connection_identity(runtime)
                if (
                    runtime_identity[0] != runtime_url.username
                    or runtime_identity[1] != runtime_url.database
                ):
                    _fail("runtime_identity_mismatch")
                if migration_identity[0] == runtime_identity[0]:
                    _fail("database_roles_not_distinct")
                if migration_identity[1:] != runtime_identity[1:]:
                    _fail("database_identity_mismatch")

                relation_rows = (
                    await runtime.execute(
                        sa.text(
                            "SELECT object.relname, object.relrowsecurity, "
                            "object.relforcerowsecurity "
                            "FROM pg_class object "
                            "JOIN pg_namespace namespace "
                            "ON namespace.oid=object.relnamespace "
                            "WHERE namespace.nspname='public' "
                            "AND object.relkind IN ('r', 'p') "
                            "ORDER BY object.relname"
                        )
                    )
                ).all()
                relation_state = {
                    name: (bool(enabled), bool(forced))
                    for name, enabled, forced in relation_rows
                }
                required_tables = sorted(
                    table_name
                    for table_name, spec in OWNERSHIP_REGISTRY.items()
                    if spec.subject is TargetColumn.REQUIRED
                    and table_name in relation_state
                )
                if not required_tables:
                    _fail("required_subject_tables_missing")

                rls_states = [
                    state for state in relation_state.values() if state[0]
                ]
                if not rls_states:
                    _fail("row_security_missing")
                if any(not forced for _enabled, forced in rls_states):
                    _fail("row_security_not_forced")
                if any(
                    not relation_state[table_name][0]
                    or not relation_state[table_name][1]
                    for table_name in required_tables
                ):
                    _fail("required_table_not_forced")

                expected_counts: dict[str, int] = {}
                for table_name in required_tables:
                    table = _quote_table(migration, table_name)
                    expected_counts[table_name] = int(
                        await migration.scalar(
                            sa.text(
                                f"SELECT count(*) FROM {table} "
                                "WHERE subject_id=:subject_id"
                            ),
                            {"subject_id": subject_id},
                        )
                        or 0
                    )

                unbound_rows = 0
                bound_rows = 0
                await runtime.rollback()
                async with runtime.begin():
                    await runtime.execute(
                        sa.text("SELECT set_config(:name, '', true)"),
                        {"name": SUBJECT_SETTING},
                    )
                    for table_name in required_tables:
                        table = _quote_table(runtime, table_name)
                        visible = int(
                            await runtime.scalar(sa.text(f"SELECT count(*) FROM {table}"))
                            or 0
                        )
                        unbound_rows += visible
                        if visible:
                            _fail("unbound_subject_rows_visible")

                    await runtime.execute(
                        sa.text("SELECT set_config(:name, :subject_id, true)"),
                        {"name": SUBJECT_SETTING, "subject_id": str(subject_id)},
                    )
                    for table_name in required_tables:
                        table = _quote_table(runtime, table_name)
                        visible = int(
                            await runtime.scalar(sa.text(f"SELECT count(*) FROM {table}"))
                            or 0
                        )
                        bound_rows += visible
                        if visible != expected_counts[table_name]:
                            _fail("bound_subject_rows_mismatch")

                inspected_rows = sum(expected_counts.values())
                if inspected_rows < 1:
                    _fail("restored_subject_rows_missing")

                return {
                    "bound_visible_rows": bound_rows,
                    "forced_rls_tables": len(rls_states),
                    "inspected_subject_rows": inspected_rows,
                    "operation": OPERATION,
                    "required_subject_tables": len(required_tables),
                    "result": "ok",
                    "subjects": subject_count,
                    "unbound_visible_rows": unbound_rows,
                }
    finally:
        await runtime_engine.dispose()
        await migration_engine.dispose()


def _error_payload(code: str) -> dict[str, str]:
    return {"error_code": code, "operation": OPERATION, "result": "error"}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        print(json.dumps(_error_payload("invalid_arguments"), separators=(",", ":")))
        return 2
    try:
        migration_url, runtime_url = _database_urls(os.environ)
        payload = asyncio.run(
            validate_runtime_rls(
                migration_url=migration_url,
                runtime_url=runtime_url,
            )
        )
    except KeyboardInterrupt:
        payload = _error_payload("cancelled")
        exit_code = 130
    except RuntimeRlsValidationError as exc:
        payload = _error_payload(exc.code)
        exit_code = 1
    except sa.exc.SQLAlchemyError:
        payload = _error_payload("database_error")
        exit_code = 1
    except Exception:
        payload = _error_payload("internal_error")
        exit_code = 1
    else:
        exit_code = 0
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
