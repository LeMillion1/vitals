#!/usr/bin/env python3
"""Rotate the PostgreSQL migration-owner password without printing it.

Run this through the one-shot ``vitals_migrate`` service with the host operator
env mounted read/write.  The web container must already use ``.env.runtime``.
The command stages and fsyncs the new operator file before changing PostgreSQL,
then atomically publishes it while the authenticated connection is still open.
If publication fails, it restores the old database password before returning.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine


_ASSIGNMENT = re.compile(
    r"^(?P<prefix>[ \t]*(?:export[ \t]+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<separator>[ \t]*=).*$"
)
_UPDATED_KEYS = frozenset({"VITALS_DB_PASSWORD", "VITALS_MIGRATION_DATABASE_URL"})


class MigrationPasswordRotationError(RuntimeError):
    """The rotation preflight or bounded recovery path failed."""


def _migration_url(values: dict[str, Any]) -> URL:
    raw = values.get("VITALS_MIGRATION_DATABASE_URL")
    password = values.get("VITALS_DB_PASSWORD")
    if not isinstance(raw, str) or not raw:
        raise MigrationPasswordRotationError("migration_url_missing")
    if not isinstance(password, str) or not password:
        raise MigrationPasswordRotationError("database_password_missing")
    try:
        url = make_url(raw)
    except sa.exc.ArgumentError as exc:
        raise MigrationPasswordRotationError("migration_url_invalid") from exc
    if url.drivername != "postgresql+asyncpg":
        raise MigrationPasswordRotationError("migration_url_driver_invalid")
    if not url.username or not url.password or not url.database:
        raise MigrationPasswordRotationError("migration_url_incomplete")
    if url.password != password:
        raise MigrationPasswordRotationError("database_password_mismatch")
    configured_user = values.get("VITALS_DB_USER")
    if configured_user and configured_user != url.username:
        raise MigrationPasswordRotationError("database_user_mismatch")
    configured_database = values.get("VITALS_DB_NAME")
    if configured_database and configured_database != url.database:
        raise MigrationPasswordRotationError("database_name_mismatch")
    return url


def _updated_operator_content(content: str, updates: dict[str, str]) -> str:
    if set(updates) != _UPDATED_KEYS or any(
        "\n" in value or "\r" in value for value in updates.values()
    ):
        raise MigrationPasswordRotationError("unsafe_update")
    counts = {key: 0 for key in updates}
    output: list[str] = []
    for line in content.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        match = _ASSIGNMENT.match(stripped)
        key = match.group("key") if match else None
        if key not in updates:
            output.append(line)
            continue
        counts[key] += 1
        newline = "\r\n" if line.endswith("\r\n") else "\n"
        output.append(
            f"{match.group('prefix')}{key}{match.group('separator')}"
            f"{updates[key]}{newline}"
        )
    if any(count != 1 for count in counts.values()):
        raise MigrationPasswordRotationError("operator_key_cardinality_invalid")
    return "".join(output)


def _stage_operator_file(path: Path, content: str) -> Path:
    source_stat = path.stat()
    mode = stat.S_IMODE(source_stat.st_mode)
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{path.name}.rotation-",
        dir=path.parent,
        text=True,
    )
    staged = Path(staged_name)
    try:
        os.fchmod(descriptor, mode)
        if (source_stat.st_uid, source_stat.st_gid) != (os.geteuid(), os.getegid()):
            os.fchown(descriptor, source_stat.st_uid, source_stat.st_gid)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


async def _quoted_password(connection: Any, password: str) -> str:
    value = await connection.scalar(
        sa.text("SELECT quote_literal(:password)"),
        {"password": password},
    )
    if not isinstance(value, str) or not value:
        raise MigrationPasswordRotationError("password_quote_failed")
    await connection.rollback()
    return value


async def rotate_migration_password(
    env_path: Path,
    *,
    new_password: str | None = None,
) -> dict[str, Any]:
    if not env_path.is_file():
        raise MigrationPasswordRotationError("operator_env_missing")
    original_content = env_path.read_text(encoding="utf-8")
    values = dict(dotenv_values(stream=None, dotenv_path=env_path))
    current_url = _migration_url(values)
    replacement = new_password or secrets.token_urlsafe(36)
    if not replacement or replacement == current_url.password:
        raise MigrationPasswordRotationError("replacement_password_invalid")
    replacement_url = current_url.set(password=replacement).render_as_string(
        hide_password=False
    )
    new_content = _updated_operator_content(
        original_content,
        {
            "VITALS_DB_PASSWORD": replacement,
            "VITALS_MIGRATION_DATABASE_URL": replacement_url,
        },
    )
    staged = _stage_operator_file(env_path, new_content)

    engine = create_async_engine(current_url)
    published = False
    try:
        async with engine.connect() as connection:
            identity = (
                await connection.execute(
                    sa.text("SELECT current_user, current_database()")
                )
            ).one()
            await connection.rollback()
            if identity[0] != current_url.username or identity[1] != current_url.database:
                raise MigrationPasswordRotationError("connection_identity_mismatch")
            role_ident = connection.dialect.identifier_preparer.quote(
                current_url.username
            )
            replacement_literal = await _quoted_password(connection, replacement)
            current_literal = await _quoted_password(connection, current_url.password)
            async with connection.begin():
                await connection.exec_driver_sql(
                    f"ALTER ROLE {role_ident} PASSWORD {replacement_literal}"
                )
            try:
                os.replace(staged, env_path)
            except BaseException as publish_error:
                async with connection.begin():
                    await connection.exec_driver_sql(
                        f"ALTER ROLE {role_ident} PASSWORD {current_literal}"
                    )
                raise MigrationPasswordRotationError(
                    "operator_env_publish_failed_database_restored"
                ) from publish_error
            published = True
            directory_fd = os.open(env_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        await engine.dispose()
        if not published:
            staged.unlink(missing_ok=True)

    return {
        "database": current_url.database,
        "operation": "rotate_migration_db_password",
        "result": "ok",
        "role": current_url.username,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    try:
        result = asyncio.run(rotate_migration_password(args.env_file))
    except (MigrationPasswordRotationError, OSError, sa.exc.SQLAlchemyError):
        print(
            json.dumps(
                {
                    "operation": "rotate_migration_db_password",
                    "result": "error",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
