"""Provision the least-privileged PostgreSQL role used by the web runtime.

The migration connection owns the schema and may bypass row-level security.
The runtime connection must be a different login: it receives ordinary DML
grants, owns no database object, and is explicitly stripped of other authority.
"""

from __future__ import annotations

import asyncio
import json
import os

import sqlalchemy as sa
from dotenv import load_dotenv
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine


# The web login needs one bootstrap capability before it can bind an invitation
# subject.  Exact regprocedure signatures are intentional: granting by name or
# granting every future function would turn one reviewed bridge into an open-
# ended privilege surface.
RUNTIME_EXECUTE_ROUTINES: tuple[str, ...] = (
    "public.authorize_and_lock_professional_invitation(text,uuid,text)",
    "public.attest_shared_report_token(text)",
)
_REQUIRED_ROUTINE_CONFIG = frozenset(
    {"search_path=pg_catalog, pg_temp", "row_security=off"}
)


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


async def _set_runtime_role_password(
    connection,
    *,
    runtime_role: str,
    password: str,
) -> None:
    try:
        await connection.execute(
            sa.text(
                "SELECT set_config('vitals.provision_runtime_role', :role, true), "
                "set_config('vitals.provision_runtime_password', :password, true)"
            ),
            {"password": password, "role": runtime_role},
        )
        await connection.exec_driver_sql(
            "DO $vitals$ BEGIN EXECUTE format("
            "'ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS', "
            "current_setting('vitals.provision_runtime_role'), "
            "current_setting('vitals.provision_runtime_password')); END $vitals$;"
        )
        await connection.exec_driver_sql(
            "SELECT set_config('vitals.provision_runtime_role', '', true), "
            "set_config('vitals.provision_runtime_password', '', true)"
        )
    except sa.exc.SQLAlchemyError:
        raise RuntimeError("failed to configure runtime database login") from None


async def _reset_role_authority(
    connection,
    *,
    migration_role: str,
    runtime_role: str,
    database: str,
) -> None:
    runtime_ident = _quoted_identifier(connection, runtime_role)
    migration_ident = _quoted_identifier(connection, migration_role)
    database_ident = _quoted_identifier(connection, database)

    memberships = (
        await connection.execute(
            sa.text(
                "SELECT parent.rolname FROM pg_auth_members membership "
                "JOIN pg_roles parent ON parent.oid=membership.roleid "
                "JOIN pg_roles member ON member.oid=membership.member "
                "WHERE member.rolname=:role ORDER BY parent.rolname"
            ),
            {"role": runtime_role},
        )
    ).scalars()
    for parent_role in memberships:
        parent_ident = _quoted_identifier(connection, parent_role)
        await connection.exec_driver_sql(
            f"REVOKE {parent_ident} FROM {runtime_ident}"
        )

    await connection.exec_driver_sql(f"ALTER ROLE {runtime_ident} RESET ALL")
    database_settings = (
        await connection.execute(
            sa.text(
                "SELECT database.datname FROM pg_db_role_setting setting "
                "JOIN pg_roles role ON role.oid=setting.setrole "
                "JOIN pg_database database ON database.oid=setting.setdatabase "
                "WHERE role.rolname=:role ORDER BY database.datname"
            ),
            {"role": runtime_role},
        )
    ).scalars()
    for configured_database in database_settings:
        configured_database_ident = _quoted_identifier(
            connection, configured_database
        )
        await connection.exec_driver_sql(
            f"ALTER ROLE {runtime_ident} IN DATABASE "
            f"{configured_database_ident} RESET ALL"
        )

    owns_database = bool(
        await connection.scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_database database "
                "JOIN pg_roles owner ON owner.oid=database.datdba "
                "WHERE database.datname=:database AND owner.rolname=:role)"
            ),
            {"database": database, "role": runtime_role},
        )
    )
    if owns_database:
        await connection.exec_driver_sql(
            f"ALTER DATABASE {database_ident} OWNER TO {migration_ident}"
        )
    await connection.exec_driver_sql(
        f"REASSIGN OWNED BY {runtime_ident} TO {migration_ident}"
    )
    await connection.exec_driver_sql(f"DROP OWNED BY {runtime_ident}")
    remaining_acl_dependencies = int(
        await connection.scalar(
            sa.text(
                "SELECT count(*) FROM pg_shdepend dependency "
                "JOIN pg_roles role ON role.oid=dependency.refobjid "
                "WHERE dependency.refclassid='pg_authid'::regclass "
                "AND dependency.deptype='a' AND role.rolname=:role"
            ),
            {"role": runtime_role},
        )
        or 0
    )
    if remaining_acl_dependencies:
        raise RuntimeError(
            "runtime database role retains ACL dependencies outside this database"
        )


async def _converge_privileges(
    connection,
    *,
    migration_role: str,
    runtime_role: str,
    database: str,
) -> None:
    runtime_ident = _quoted_identifier(connection, runtime_role)
    migration_ident = _quoted_identifier(connection, migration_role)
    database_ident = _quoted_identifier(connection, database)

    # A signature is only an address, not an authority proof.  Refuse before
    # changing any grants if the migration-owned bridge at that address was
    # replaced, made invoker-rights, given an unsafe search path, or exposed to
    # PUBLIC.  The whole provisioning transaction rolls back on this refusal.
    for routine_signature in RUNTIME_EXECUTE_ROUTINES:
        routine = (
            await connection.execute(
                sa.text(
                    "SELECT owner.rolname AS owner, language.lanname AS language, "
                    "routine.prosecdef, routine.provolatile, routine.prokind, "
                    "routine.proleakproof, routine.proconfig, "
                    "NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE("
                    "routine.proacl, acldefault('f', routine.proowner))) acl "
                    "WHERE acl.grantee=0 "
                    "AND upper(acl.privilege_type)='EXECUTE') AS no_public "
                    "FROM pg_proc routine "
                    "JOIN pg_roles owner ON owner.oid=routine.proowner "
                    "JOIN pg_language language ON language.oid=routine.prolang "
                    "WHERE routine.oid=to_regprocedure(:signature)"
                ),
                {"signature": routine_signature},
            )
        ).mappings().one_or_none()
        config = (
            frozenset(routine["proconfig"] or ()) if routine is not None else None
        )
        failures = []
        if routine is None:
            failures.append("missing")
        else:
            if routine["owner"] != migration_role:
                failures.append("wrong-owner")
            if routine["language"] != "plpgsql":
                failures.append("wrong-language")
            if not routine["prosecdef"]:
                failures.append("security-invoker")
            if routine["provolatile"] not in ("v", b"v"):
                failures.append("not-volatile")
            if routine["prokind"] not in ("f", b"f"):
                failures.append("not-function")
            if routine["proleakproof"]:
                failures.append("leakproof")
            if config != _REQUIRED_ROUTINE_CONFIG:
                failures.append("unsafe-config")
            if not routine["no_public"]:
                failures.append("public-execute")
        if failures:
            reasons = ", ".join(failures)
            raise RuntimeError(
                "required runtime routine is not trusted: "
                f"{routine_signature} ({reasons})"
            )

    schemas = list(
        (
            await connection.execute(
                sa.text(
                    "SELECT nspname FROM pg_namespace "
                    "WHERE nspname <> 'information_schema' "
                    "AND nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
                    "ORDER BY nspname"
                )
            )
        ).scalars()
    )
    user_types = (
        await connection.execute(
            sa.text(
                "SELECT namespace.nspname, object.typname FROM pg_type object "
                "JOIN pg_namespace namespace ON namespace.oid=object.typnamespace "
                "WHERE namespace.nspname <> 'information_schema' "
                "AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
                "AND object.typrelid=0 AND object.typelem=0 "
                "ORDER BY namespace.nspname, object.typname"
            )
        )
    ).all()
    default_acl_entries = (
        await connection.execute(
            sa.text(
                "SELECT DISTINCT grantor.rolname, namespace.nspname, "
                "defaults.defaclobjtype FROM pg_default_acl defaults "
                "CROSS JOIN LATERAL aclexplode(defaults.defaclacl) acl "
                "JOIN pg_roles grantee ON grantee.oid=acl.grantee "
                "JOIN pg_roles grantor ON grantor.oid=defaults.defaclrole "
                "LEFT JOIN pg_namespace namespace "
                "ON namespace.oid=defaults.defaclnamespace "
                "WHERE grantee.rolname=:role "
                "ORDER BY grantor.rolname, namespace.nspname NULLS FIRST, "
                "defaults.defaclobjtype"
            ),
            {"role": runtime_role},
        )
    ).all()
    large_object_oids = list(
        (
            await connection.execute(
                sa.text("SELECT oid FROM pg_largeobject_metadata ORDER BY oid")
            )
        ).scalars()
    )

    await connection.exec_driver_sql(
        f"REVOKE ALL PRIVILEGES ON DATABASE {database_ident} FROM {runtime_ident}"
    )
    await connection.exec_driver_sql(
        f"REVOKE ALL PRIVILEGES ON DATABASE {database_ident} FROM PUBLIC"
    )
    await connection.exec_driver_sql(
        f"GRANT CONNECT ON DATABASE {database_ident} TO {runtime_ident}"
    )
    for schema in schemas:
        schema_ident = _quoted_identifier(connection, schema)
        await connection.exec_driver_sql(
            f"REVOKE ALL PRIVILEGES ON SCHEMA {schema_ident} FROM {runtime_ident}"
        )
        await connection.exec_driver_sql(
            f"REVOKE ALL PRIVILEGES ON SCHEMA {schema_ident} FROM PUBLIC"
        )
        await connection.exec_driver_sql(
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA "
            f"{schema_ident} FROM {runtime_ident}"
        )
        await connection.exec_driver_sql(
            "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA "
            f"{schema_ident} FROM PUBLIC"
        )
        await connection.exec_driver_sql(
            "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA "
            f"{schema_ident} FROM {runtime_ident}"
        )
        await connection.exec_driver_sql(
            "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA "
            f"{schema_ident} FROM PUBLIC"
        )
        await connection.exec_driver_sql(
            "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA "
            f"{schema_ident} FROM {runtime_ident}"
        )
        await connection.exec_driver_sql(
            "REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA "
            f"{schema_ident} FROM PUBLIC"
        )
    for schema, type_name in user_types:
        schema_ident = _quoted_identifier(connection, schema)
        type_ident = _quoted_identifier(connection, type_name)
        await connection.exec_driver_sql(
            f"REVOKE ALL PRIVILEGES ON TYPE {schema_ident}.{type_ident} "
            f"FROM {runtime_ident}"
        )
        await connection.exec_driver_sql(
            f"REVOKE ALL PRIVILEGES ON TYPE {schema_ident}.{type_ident} FROM PUBLIC"
        )
        if schema == "public":
            await connection.exec_driver_sql(
                f"GRANT USAGE ON TYPE {schema_ident}.{type_ident} TO {runtime_ident}"
            )
    for large_object_oid in large_object_oids:
        await connection.exec_driver_sql(
            f"REVOKE ALL PRIVILEGES ON LARGE OBJECT {large_object_oid} "
            f"FROM {runtime_ident}, PUBLIC"
        )

    default_object_types = {
        "r": "TABLES",
        "S": "SEQUENCES",
        "f": "FUNCTIONS",
        "T": "TYPES",
        "n": "SCHEMAS",
    }
    for grantor, schema, object_code in default_acl_entries:
        if isinstance(object_code, bytes):
            object_code = object_code.decode("ascii")
        object_type = default_object_types.get(object_code)
        if object_type is None:
            raise RuntimeError(
                f"unsupported default privilege object type: {object_code!r}"
            )
        grantor_ident = _quoted_identifier(connection, grantor)
        schema_clause = ""
        if schema is not None:
            schema_ident = _quoted_identifier(connection, schema)
            schema_clause = f"IN SCHEMA {schema_ident} "
        await connection.exec_driver_sql(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {grantor_ident} "
            f"{schema_clause}REVOKE ALL PRIVILEGES ON {object_type} "
            f"FROM {runtime_ident}"
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
    for routine_signature in RUNTIME_EXECUTE_ROUTINES:
        await connection.exec_driver_sql(
            f"GRANT EXECUTE ON FUNCTION {routine_signature} TO {runtime_ident}"
        )
    for object_type in ("TABLES", "SEQUENCES", "FUNCTIONS", "TYPES", "SCHEMAS"):
        await connection.exec_driver_sql(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration_ident} "
            f"REVOKE ALL PRIVILEGES ON {object_type} FROM {runtime_ident}"
        )
    await connection.exec_driver_sql(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration_ident} "
        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    )
    for object_type in ("TABLES", "SEQUENCES", "FUNCTIONS", "TYPES"):
        await connection.exec_driver_sql(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration_ident} "
            f"IN SCHEMA public REVOKE ALL PRIVILEGES ON {object_type} "
            f"FROM {runtime_ident}"
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
    await connection.exec_driver_sql(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration_ident} "
        "IN SCHEMA public GRANT USAGE ON TYPES "
        f"TO {runtime_ident}"
    )


async def _runtime_role_state(
    connection,
    *,
    migration_role: str,
    runtime_role: str,
) -> dict:
    attributes = (
        await connection.execute(
            sa.text(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                "rolinherit, rolreplication, rolbypassrls, "
                "COALESCE(cardinality(rolconfig), 0) AS role_settings "
                "FROM pg_roles WHERE rolname=:role"
            ),
            {"role": runtime_role},
        )
    ).mappings().one()
    counts = (
        await connection.execute(
            sa.text(
                "SELECT "
                "(SELECT count(*) FROM pg_auth_members membership "
                " JOIN pg_roles member ON member.oid=membership.member "
                " WHERE member.rolname=:role) AS memberships, "
                "(SELECT count(*) FROM pg_db_role_setting setting "
                " JOIN pg_roles role ON role.oid=setting.setrole "
                " WHERE role.rolname=:role) AS database_settings, "
                "(SELECT count(*) FROM pg_database database "
                " JOIN pg_roles owner ON owner.oid=database.datdba "
                " WHERE owner.rolname=:role) + "
                "(SELECT count(*) FROM pg_namespace namespace "
                " JOIN pg_roles owner ON owner.oid=namespace.nspowner "
                " WHERE owner.rolname=:role) + "
                "(SELECT count(*) FROM pg_class object "
                " JOIN pg_roles owner ON owner.oid=object.relowner "
                " WHERE owner.rolname=:role) + "
                "(SELECT count(*) FROM pg_proc object "
                " JOIN pg_roles owner ON owner.oid=object.proowner "
                " WHERE owner.rolname=:role) + "
                "(SELECT count(*) FROM pg_type object "
                " JOIN pg_roles owner ON owner.oid=object.typowner "
                " WHERE owner.rolname=:role) AS owned_objects, "
                "(SELECT count(*) FROM pg_class object "
                " JOIN pg_namespace namespace ON namespace.oid=object.relnamespace "
                " CROSS JOIN LATERAL aclexplode(object.relacl) acl "
                " JOIN pg_roles grantee ON grantee.oid=acl.grantee "
                " WHERE grantee.rolname=:role "
                " AND namespace.nspname <> 'information_schema' "
                " AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
                " AND ((object.relkind='S' AND upper(acl.privilege_type) "
                " NOT IN ('USAGE','SELECT','UPDATE')) OR "
                " (object.relkind<>'S' AND upper(acl.privilege_type) "
                " NOT IN ('SELECT','INSERT','UPDATE','DELETE')))) AS "
                "extra_relation_privileges, "
                "(SELECT count(*) FROM pg_namespace namespace "
                " CROSS JOIN LATERAL aclexplode(namespace.nspacl) acl "
                " JOIN pg_roles grantee ON grantee.oid=acl.grantee "
                " WHERE grantee.rolname=:role "
                " AND upper(acl.privilege_type)<>'USAGE') AS extra_schema_privileges, "
                "(SELECT count(*) FROM pg_database database "
                " CROSS JOIN LATERAL aclexplode(database.datacl) acl "
                " JOIN pg_roles grantee ON grantee.oid=acl.grantee "
                " WHERE grantee.rolname=:role "
                " AND upper(acl.privilege_type)<>'CONNECT') AS extra_database_privileges, "
                "(SELECT count(*) FROM pg_proc object "
                " JOIN pg_namespace namespace ON namespace.oid=object.pronamespace "
                " CROSS JOIN LATERAL aclexplode(COALESCE("
                "object.proacl, acldefault('f', object.proowner))) acl "
                " JOIN pg_roles grantee ON grantee.oid=acl.grantee "
                " WHERE grantee.rolname=:role "
                " AND object.oid NOT IN (SELECT to_regprocedure(signature) "
                " FROM unnest(CAST(:allowed_routines AS text[])) signature)) "
                "AS routine_privileges, "
                "(SELECT count(*) FROM pg_type object "
                " JOIN pg_namespace namespace ON namespace.oid=object.typnamespace "
                " CROSS JOIN LATERAL aclexplode(object.typacl) acl "
                " JOIN pg_roles grantee ON grantee.oid=acl.grantee "
                " WHERE grantee.rolname=:role AND NOT ("
                " namespace.nspname='public' "
                " AND upper(acl.privilege_type)='USAGE')) AS "
                "extra_type_privileges, "
                "(SELECT count(*) FROM pg_namespace namespace "
                " WHERE namespace.nspname <> 'information_schema' "
                " AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
                " AND has_schema_privilege(:role, namespace.oid, 'CREATE')) AS "
                "effective_schema_create, "
                "(SELECT count(*) FROM pg_proc object "
                " JOIN pg_namespace namespace ON namespace.oid=object.pronamespace "
                " WHERE namespace.nspname <> 'information_schema' "
                " AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
                " AND object.oid NOT IN (SELECT to_regprocedure(signature) "
                " FROM unnest(CAST(:allowed_routines AS text[])) signature) "
                " AND has_function_privilege(:role, object.oid, 'EXECUTE')) AS "
                "effective_routine_execute, "
                "(SELECT count(*) FROM "
                " unnest(CAST(:allowed_routines AS text[])) signature "
                " WHERE to_regprocedure(signature) IS NULL "
                " OR NOT has_function_privilege("
                ":role, to_regprocedure(signature), 'EXECUTE')) "
                "AS missing_required_routine_execute, "
                "(SELECT count(*) FROM pg_proc object "
                " JOIN pg_namespace namespace ON namespace.oid=object.pronamespace "
                " CROSS JOIN LATERAL aclexplode(COALESCE("
                "object.proacl, acldefault('f', object.proowner))) acl "
                " WHERE namespace.nspname <> 'information_schema' "
                " AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
                " AND acl.grantee=0) AS public_routine_privileges, "
                "(SELECT count(*) FROM pg_class object "
                " JOIN pg_namespace namespace ON namespace.oid=object.relnamespace "
                " WHERE namespace.nspname <> 'information_schema' "
                " AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
                " AND object.relkind IN ('r','p','v','m','S','f') AND ("
                " (object.relkind='S' AND namespace.nspname<>'public' AND ("
                "  has_sequence_privilege(:role, object.oid, 'USAGE') OR "
                "  has_sequence_privilege(:role, object.oid, 'SELECT') OR "
                "  has_sequence_privilege(:role, object.oid, 'UPDATE'))) OR "
                " (object.relkind<>'S' AND ("
                "  has_table_privilege(:role, object.oid, 'TRUNCATE') OR "
                "  has_table_privilege(:role, object.oid, 'REFERENCES') OR "
                "  has_table_privilege(:role, object.oid, 'TRIGGER') OR "
                "  (namespace.nspname<>'public' AND ("
                "   has_table_privilege(:role, object.oid, 'SELECT') OR "
                "   has_table_privilege(:role, object.oid, 'INSERT') OR "
                "   has_table_privilege(:role, object.oid, 'UPDATE') OR "
                "   has_table_privilege(:role, object.oid, 'DELETE'))))))) AS "
                "effective_extra_relation_privileges, "
                "(SELECT count(*) FROM pg_largeobject_metadata object "
                " CROSS JOIN LATERAL aclexplode(object.lomacl) acl "
                " WHERE acl.grantee=0 OR acl.grantee=("
                " SELECT oid FROM pg_roles WHERE rolname=:role)) AS "
                "effective_large_object_privileges, "
                "(SELECT CASE WHEN has_database_privilege("
                ":role, current_database(), 'TEMPORARY') THEN 1 ELSE 0 END) AS "
                "effective_database_temp, "
                "(SELECT count(*) FROM pg_default_acl defaults "
                " CROSS JOIN LATERAL aclexplode(defaults.defaclacl) acl "
                " JOIN pg_roles grantee ON grantee.oid=acl.grantee "
                " JOIN pg_roles grantor ON grantor.oid=defaults.defaclrole "
                " LEFT JOIN pg_namespace namespace "
                " ON namespace.oid=defaults.defaclnamespace "
                " WHERE grantee.rolname=:role AND NOT ("
                " grantor.rolname=:migration_role "
                " AND namespace.nspname='public' "
                " AND ((defaults.defaclobjtype='r' "
                " AND upper(acl.privilege_type) "
                " IN ('SELECT','INSERT','UPDATE','DELETE')) "
                " OR (defaults.defaclobjtype='S' "
                " AND upper(acl.privilege_type) "
                " IN ('USAGE','SELECT','UPDATE')) "
                " OR (defaults.defaclobjtype='T' "
                " AND upper(acl.privilege_type)='USAGE')))) AS "
                "extra_default_privileges"
            ),
            {
                "allowed_routines": list(RUNTIME_EXECUTE_ROUTINES),
                "migration_role": migration_role,
                "role": runtime_role,
            },
        )
    ).mappings().one()
    state = {**dict(attributes), **{key: int(value) for key, value in counts.items()}}
    expected_attributes = {
        "rolcanlogin": True,
        "rolsuper": False,
        "rolcreatedb": False,
        "rolcreaterole": False,
        "rolinherit": False,
        "rolreplication": False,
        "rolbypassrls": False,
    }
    if any(state[key] != value for key, value in expected_attributes.items()):
        raise RuntimeError("runtime database role has privileged attributes")
    zero_keys = (
        "role_settings",
        "memberships",
        "database_settings",
        "owned_objects",
        "extra_relation_privileges",
        "extra_schema_privileges",
        "extra_database_privileges",
        "routine_privileges",
        "extra_type_privileges",
        "effective_schema_create",
        "effective_routine_execute",
        "missing_required_routine_execute",
        "public_routine_privileges",
        "effective_extra_relation_privileges",
        "effective_large_object_privileges",
        "effective_database_temp",
        "extra_default_privileges",
    )
    if any(state[key] != 0 for key in zero_keys):
        raise RuntimeError("runtime database role retains unexpected authority")
    return state


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
            await connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            await connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
            connected_role = await connection.scalar(sa.text("SELECT current_user"))
            database = await connection.scalar(sa.text("SELECT current_database()"))
            if connected_role != migration_role or database != migration_url.database:
                raise RuntimeError("migration connection identity does not match its URL")

            runtime_ident = _quoted_identifier(connection, runtime_role)
            exists = bool(
                await connection.scalar(
                    sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"),
                    {"role": runtime_role},
                )
            )
            if not exists:
                await connection.exec_driver_sql(
                    f"CREATE ROLE {runtime_ident} NOLOGIN"
                )
            await _set_runtime_role_password(
                connection,
                runtime_role=runtime_role,
                password=runtime_url.password,
            )
            await _reset_role_authority(
                connection,
                migration_role=migration_role,
                runtime_role=runtime_role,
                database=database,
            )
            await _converge_privileges(
                connection,
                migration_role=migration_role,
                runtime_role=runtime_role,
                database=database,
            )
            state = await _runtime_role_state(
                connection,
                migration_role=migration_role,
                runtime_role=runtime_role,
            )
    finally:
        await engine.dispose()

    return {
        "status": "completed",
        "database": migration_url.database,
        "migration_role": migration_role,
        "runtime_role": runtime_role,
        "owned_objects": state["owned_objects"],
        "role_memberships": state["memberships"],
        "role_settings": state["role_settings"] + state["database_settings"],
        "extra_privileges": (
            state["extra_relation_privileges"]
            + state["extra_schema_privileges"]
            + state["extra_database_privileges"]
            + state["routine_privileges"]
            + state["extra_type_privileges"]
            + state["effective_schema_create"]
            + state["effective_routine_execute"]
            + state["missing_required_routine_execute"]
            + state["public_routine_privileges"]
            + state["effective_extra_relation_privileges"]
            + state["effective_large_object_privileges"]
            + state["effective_database_temp"]
            + state["extra_default_privileges"]
        ),
        "superuser": state["rolsuper"],
        "bypass_rls": state["rolbypassrls"],
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
