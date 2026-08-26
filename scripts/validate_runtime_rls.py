#!/usr/bin/env python3
"""Prove the restored PostgreSQL subject boundary with aggregate-only output.

The migration role is used only for cross-subject aggregate reads.  Every
subject-data read through the runtime role must fail closed until the
transaction is bound to one subject, and every declared strict subject table
must have forced row-level security.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from vitals.persistence.rls import (  # noqa: E402
    PLATFORM_CAPABILITY_PREDICATE,
    PLATFORM_CAPABILITY_ROLE_PREFIX,
)

OPERATION = "validate_runtime_rls"
SUBJECT_SETTING = "vitals.subject_id"
PLATFORM_SETTING = "vitals.platform_scope"
_POLICY_REVISIONS = (
    "0050_force_subject_row_level_security",
    "0051_row_security_for_catalogs_and_children",
    "0055_professional_invitations",
    "0056_care_relationships_and_consent",
    "0057_professional_notes_and_care_plans",
    "0060_per_subject_provider_credentials",
    "0061_care_team_threads_and_messages",
    "0062_support_access_requests",
    "0063_external_api_tokens",
    "0065_subject_scoped_mcp_grants",
    "0067_care_message_attachments",
    "0069_subject_isolated_care_push_outbox",
    "0076_support_repair_actions",
    "0078_break_glass_sessions",
    "0079_portability_import_receipts",
)
_DROPPED_POLICY_TABLES = frozenset({"signals", "day_context"})
_SUBJECT_PREDICATE = (
    "(subject_id = (NULLIF(current_setting('vitals.subject_id'::text, true), "
    "''::text))::uuid)"
)
_NORMALIZED_CAPABILITY_PREDICATE = (
    "(EXISTS ( SELECT 1\n"
    "   FROM pg_roles capability\n"
    "  WHERE ((capability.rolname = ('vitals_platform_scope_db_'::text || "
    "( SELECT (database.oid)::text AS oid\n"
    "           FROM pg_database database\n"
    "          WHERE (database.datname = current_database())))) AND "
    "(capability.rolcanlogin = false) AND (capability.rolsuper = false) AND "
    "(capability.rolcreatedb = false) AND (capability.rolcreaterole = false) "
    "AND (capability.rolinherit = false) AND "
    "(capability.rolreplication = false) AND "
    "(capability.rolbypassrls = false) AND "
    "pg_has_role(SESSION_USER, capability.oid, 'MEMBER'::text))))"
)
_PLATFORM_PREDICATE = (
    "((subject_id = (NULLIF(current_setting('vitals.subject_id'::text, true), "
    "''::text))::uuid) OR ((current_setting('vitals.platform_scope'::text, "
    "true) = 'on'::text) AND "
    "__PLATFORM_CAPABILITY_PREDICATE__))"
)
_SHARED_PREDICATE = (
    "((subject_id IS NULL) OR "
    "(subject_id = (NULLIF(current_setting('vitals.subject_id'::text, true), "
    "''::text))::uuid) OR ((current_setting('vitals.platform_scope'::text, "
    "true) = 'on'::text) AND "
    "__PLATFORM_CAPABILITY_PREDICATE__))"
)
_PLATFORM_PREDICATE = _PLATFORM_PREDICATE.replace(
    "__PLATFORM_CAPABILITY_PREDICATE__", _NORMALIZED_CAPABILITY_PREDICATE
)
_SHARED_PREDICATE = _SHARED_PREDICATE.replace(
    "__PLATFORM_CAPABILITY_PREDICATE__", _NORMALIZED_CAPABILITY_PREDICATE
)
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
        "platform_capability_invalid",
        "platform_scope_rows_mismatch",
        "required_subject_tables_missing",
        "required_table_policy_invalid",
        "required_table_not_forced",
        "restored_subject_rows_missing",
        "row_security_missing",
        "row_security_not_forced",
        "runtime_identity_mismatch",
        "subject_data_missing",
        "unbound_subject_rows_visible",
        "worker_identity_mismatch",
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


def _database_urls(environ: Mapping[str, str]) -> tuple[URL, URL, URL]:
    migration = _postgres_url(environ, "VITALS_MIGRATION_DATABASE_URL")
    runtime = _postgres_url(environ, "VITALS_DATABASE_URL")
    worker = _postgres_url(environ, "VITALS_WORKER_DATABASE_URL")
    if len({migration.username, runtime.username, worker.username}) != 3:
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
    worker_endpoint = (
        worker.host.lower(),
        worker.port or 5432,
        worker.database,
    )
    if migration_endpoint != runtime_endpoint or migration_endpoint != worker_endpoint:
        _fail("database_endpoint_mismatch")
    return migration, runtime, worker


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


def _required_subject_tables(
    relation_state: Mapping[str, tuple[bool, bool]],
    policy_kinds: Mapping[str, str] | None = None,
) -> list[str]:
    """Require every current table that a migration placed behind subject RLS."""

    required = sorted(policy_kinds if policy_kinds is not None else _policy_kinds())
    if not required or any(table_name not in relation_state for table_name in required):
        _fail("required_subject_tables_missing")
    return required


def _policy_kinds() -> dict[str, str]:
    """Derive today's exact policy inventory from the immutable migrations."""

    kinds: dict[str, str] = {}
    for stem in _POLICY_REVISIONS:
        path = _REPOSITORY_ROOT / "migrations" / "versions" / f"{stem}.py"
        spec = importlib.util.spec_from_file_location(f"_vitals_rls_{stem[:4]}", path)
        if spec is None or spec.loader is None:
            _fail("required_subject_tables_missing")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        shared = set(getattr(module, "SHARED_WITH_INSTALLATION", ()))
        tables = set(getattr(module, "SUBJECT_ISOLATED_TABLES", ()))
        tables.update(getattr(module, "INHERITED_CHILDREN", ()))
        tables.update(shared)
        for table_name in tables:
            if table_name in _DROPPED_POLICY_TABLES:
                continue
            if table_name in shared:
                kind = "shared"
            elif stem.startswith(("0050_", "0051_")) or hasattr(
                module, "PLATFORM_SETTING"
            ):
                kind = "platform"
            else:
                kind = "subject"
            previous = kinds.setdefault(table_name, kind)
            if previous != kind:
                _fail("required_table_policy_invalid")
    return kinds


def _expected_policy(table_name: str, policy_kinds: Mapping[str, str]) -> str:
    predicate = {
        "subject": _SUBJECT_PREDICATE,
        "platform": _PLATFORM_PREDICATE,
        "shared": _SHARED_PREDICATE,
    }.get(policy_kinds.get(table_name, ""))
    if predicate is None:
        _fail("required_table_policy_invalid")
    return predicate


async def validate_runtime_rls(
    *,
    migration_url: URL,
    runtime_url: URL,
    worker_url: URL,
) -> dict[str, int | str]:
    """Return aggregate proof results or raise a bounded validation error."""

    migration_engine = create_async_engine(migration_url, poolclass=NullPool)
    runtime_engine = create_async_engine(runtime_url, poolclass=NullPool)
    worker_engine = create_async_engine(worker_url, poolclass=NullPool)
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
            subject_ids = list(
                (
                    await migration.execute(
                        sa.text(
                            "SELECT id FROM public.health_subjects ORDER BY id LIMIT 2"
                        )
                    )
                ).scalars()
            )
            if subject_count < 1 or not subject_ids:
                _fail("subject_data_missing")
            unknown_subject_id = uuid.uuid4()
            while await migration.scalar(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM public.health_subjects WHERE id=:id)"
                ),
                {"id": unknown_subject_id},
            ):
                unknown_subject_id = uuid.uuid4()

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
                policy_kinds = _policy_kinds()
                required_tables = _required_subject_tables(
                    relation_state, policy_kinds
                )

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

                policy_rows = (
                    await runtime.execute(
                        sa.text(
                            "SELECT object.relname, policy.polname, "
                            "policy.polpermissive, policy.polcmd, policy.polroles, "
                            "pg_get_expr(policy.polqual, policy.polrelid), "
                            "pg_get_expr(policy.polwithcheck, policy.polrelid) "
                            "FROM pg_policy policy "
                            "JOIN pg_class object ON object.oid=policy.polrelid "
                            "JOIN pg_namespace namespace "
                            "ON namespace.oid=object.relnamespace "
                            "WHERE namespace.nspname='public' "
                            "AND object.relname = ANY(:tables) "
                            "ORDER BY object.relname, policy.polname"
                        ),
                        {"tables": required_tables},
                    )
                ).all()
                policies: dict[str, list[tuple[Any, ...]]] = {
                    table_name: [] for table_name in required_tables
                }
                for row in policy_rows:
                    policies[row[0]].append(tuple(row[1:]))
                for table_name in required_tables:
                    rows = policies[table_name]
                    if len(rows) != 1:
                        _fail("required_table_policy_invalid")
                    name, permissive, command, roles, using, check = rows[0]
                    expected_policy = _expected_policy(table_name, policy_kinds)
                    if (
                        name != "rls_subject_isolation"
                        or not permissive
                        or command not in ("*", b"*")
                        or list(roles or ()) != [0]
                        or using != expected_policy
                        or check != expected_policy
                    ):
                        _fail("required_table_policy_invalid")

                expected_counts: dict[uuid.UUID, dict[str, int]] = {}
                for subject_id in subject_ids:
                    expected_counts[subject_id] = {}
                    for table_name in required_tables:
                        table = _quote_table(migration, table_name)
                        expected_counts[subject_id][table_name] = int(
                            await migration.scalar(
                                sa.text(
                                    f"SELECT count(*) FROM {table} "
                                    "WHERE subject_id=:subject_id"
                                ),
                                {"subject_id": subject_id},
                            )
                            or 0
                        )

                shared_expected_counts: dict[str, int] = {}
                for table_name in required_tables:
                    if policy_kinds[table_name] != "shared":
                        shared_expected_counts[table_name] = 0
                        continue
                    table = _quote_table(migration, table_name)
                    shared_expected_counts[table_name] = int(
                        await migration.scalar(
                            sa.text(
                                f"SELECT count(*) FROM {table} "
                                "WHERE subject_id IS NULL"
                            )
                        )
                        or 0
                    )

                platform_expected_counts: dict[str, int] = {}
                for table_name in required_tables:
                    if policy_kinds[table_name] == "subject":
                        platform_expected_counts[table_name] = 0
                        continue
                    table = _quote_table(migration, table_name)
                    platform_expected_counts[table_name] = int(
                        await migration.scalar(
                            sa.text(f"SELECT count(*) FROM {table}")
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
                    await runtime.execute(
                        sa.text("SELECT set_config('vitals.platform_scope', '', true)")
                    )
                    for table_name in required_tables:
                        table = _quote_table(runtime, table_name)
                        visible = int(
                            await runtime.scalar(sa.text(f"SELECT count(*) FROM {table}"))
                            or 0
                        )
                        unbound_rows += visible
                        if visible != shared_expected_counts[table_name]:
                            _fail("unbound_subject_rows_visible")

                    await runtime.execute(
                        sa.text("SELECT set_config(:name, :subject_id, true)"),
                        {
                            "name": SUBJECT_SETTING,
                            "subject_id": str(unknown_subject_id),
                        },
                    )
                    for table_name in required_tables:
                        table = _quote_table(runtime, table_name)
                        visible = int(
                            await runtime.scalar(sa.text(f"SELECT count(*) FROM {table}"))
                            or 0
                        )
                        if visible != shared_expected_counts[table_name]:
                            _fail("bound_subject_rows_mismatch")

                for subject_id in subject_ids:
                    await runtime.rollback()
                    async with runtime.begin():
                        await runtime.execute(
                            sa.text("SELECT set_config('vitals.platform_scope', '', true)")
                        )
                        await runtime.execute(
                            sa.text("SELECT set_config(:name, :subject_id, true)"),
                            {"name": SUBJECT_SETTING, "subject_id": str(subject_id)},
                        )
                        for table_name in required_tables:
                            table = _quote_table(runtime, table_name)
                            visible = int(
                                await runtime.scalar(
                                    sa.text(f"SELECT count(*) FROM {table}")
                                )
                                or 0
                            )
                            bound_rows += visible
                            expected = (
                                expected_counts[subject_id][table_name]
                                + shared_expected_counts[table_name]
                            )
                            if visible != expected:
                                _fail("bound_subject_rows_mismatch")

                inspected_rows = sum(
                    count
                    for subject_counts in expected_counts.values()
                    for count in subject_counts.values()
                )
                if inspected_rows < 1:
                    _fail("restored_subject_rows_missing")

                async with worker_engine.connect() as worker:
                    worker_identity = await _connection_identity(worker)
                    if (
                        worker_identity[0] != worker_url.username
                        or worker_identity[1] != worker_url.database
                    ):
                        _fail("worker_identity_mismatch")
                    if worker_identity[0] in {
                        migration_identity[0],
                        runtime_identity[0],
                    }:
                        _fail("database_roles_not_distinct")
                    if migration_identity[1:] != worker_identity[1:]:
                        _fail("database_identity_mismatch")

                    database_oid = int(
                        await migration.scalar(
                            sa.text(
                                "SELECT oid FROM pg_database "
                                "WHERE datname=current_database()"
                            )
                        )
                    )
                    capability_role = (
                        f"{PLATFORM_CAPABILITY_ROLE_PREFIX}{database_oid}"
                    )
                    capability = (
                        await migration.execute(
                            sa.text(
                                "SELECT role.rolcanlogin, role.rolsuper, "
                                "role.rolcreatedb, role.rolcreaterole, "
                                "role.rolinherit, role.rolreplication, "
                                "role.rolbypassrls, "
                                "COALESCE(cardinality(role.rolconfig), 0), "
                                "(SELECT count(*) FROM pg_auth_members membership "
                                " JOIN pg_roles parent "
                                " ON parent.oid=membership.roleid "
                                " WHERE parent.rolname=:capability), "
                                "(SELECT count(*) FROM pg_auth_members membership "
                                " JOIN pg_roles parent "
                                " ON parent.oid=membership.roleid "
                                " JOIN pg_roles member "
                                " ON member.oid=membership.member "
                                " WHERE parent.rolname=:capability "
                                " AND member.rolname=:worker "
                                " AND NOT membership.admin_option), "
                                "pg_has_role(:worker, role.oid, 'MEMBER'), "
                                "pg_has_role(:worker, role.oid, 'USAGE'), "
                                "pg_has_role(:web, role.oid, 'MEMBER') "
                                "FROM pg_roles role WHERE role.rolname=:capability"
                            ),
                            {
                                "capability": capability_role,
                                "web": runtime_url.username,
                                "worker": worker_url.username,
                            },
                        )
                    ).one_or_none()
                    if capability is None or tuple(capability) != (
                        False,
                        False,
                        False,
                        False,
                        False,
                        False,
                        False,
                        0,
                        1,
                        1,
                        True,
                        False,
                        False,
                    ):
                        _fail("platform_capability_invalid")

                    await runtime.rollback()
                    async with runtime.begin():
                        await runtime.execute(
                            sa.text("SELECT set_config(:name, '', true)"),
                            {"name": SUBJECT_SETTING},
                        )
                        await runtime.execute(
                            sa.text("SELECT set_config(:name, 'on', true)"),
                            {"name": PLATFORM_SETTING},
                        )
                        if bool(
                            await runtime.scalar(
                                sa.text(
                                    f"SELECT {PLATFORM_CAPABILITY_PREDICATE}"
                                )
                            )
                        ):
                            _fail("platform_capability_invalid")
                        for table_name in required_tables:
                            table = _quote_table(runtime, table_name)
                            visible = int(
                                await runtime.scalar(
                                    sa.text(f"SELECT count(*) FROM {table}")
                                )
                                or 0
                            )
                            if visible != shared_expected_counts[table_name]:
                                _fail("platform_scope_rows_mismatch")

                    platform_visible_rows = 0
                    await worker.rollback()
                    async with worker.begin():
                        await worker.execute(
                            sa.text("SELECT set_config(:name, '', true)"),
                            {"name": SUBJECT_SETTING},
                        )
                        await worker.execute(
                            sa.text("SELECT set_config(:name, 'on', true)"),
                            {"name": PLATFORM_SETTING},
                        )
                        if not bool(
                            await worker.scalar(
                                sa.text(
                                    f"SELECT {PLATFORM_CAPABILITY_PREDICATE}"
                                )
                            )
                        ):
                            _fail("platform_capability_invalid")
                        for table_name in required_tables:
                            table = _quote_table(worker, table_name)
                            visible = int(
                                await worker.scalar(
                                    sa.text(f"SELECT count(*) FROM {table}")
                                )
                                or 0
                            )
                            expected = platform_expected_counts[table_name]
                            if visible != expected:
                                _fail("platform_scope_rows_mismatch")
                            platform_visible_rows += visible

                return {
                    "bound_visible_rows": bound_rows,
                    "forced_rls_tables": len(rls_states),
                    "inspected_subject_rows": inspected_rows,
                    "operation": OPERATION,
                    "platform_visible_rows": platform_visible_rows,
                    "required_subject_tables": len(required_tables),
                    "result": "ok",
                    "subjects": subject_count,
                    "unbound_visible_rows": unbound_rows,
                    "validated_subjects": len(subject_ids),
                }
    finally:
        await worker_engine.dispose()
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
        migration_url, runtime_url, worker_url = _database_urls(os.environ)
        payload = asyncio.run(
            validate_runtime_rls(
                migration_url=migration_url,
                runtime_url=runtime_url,
                worker_url=worker_url,
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
