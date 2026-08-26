#!/usr/bin/env python3
"""Replace one disposable restore's owner login with synthetic credentials.

This command is intentionally usable only when an explicit restore-drill marker
is present. It mutates account credentials in the scratch database, never the
health record, and emits only an aggregate result.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import stat

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


_USERNAME = re.compile(r"drill-[0-9a-f]{12}")
_BCRYPT = re.compile(r"\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}")
_RUN_ID = re.compile(r"[0-9a-f]{12}")
_MARKER_FILE = Path("/run/vitals-restore-drill/marker.json")


def _required(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name.lower()}_missing")
    return value


async def _prepare() -> dict[str, object]:
    if _required("VITALS_RESTORE_DRILL") != "true":
        raise RuntimeError("restore_drill_marker_invalid")
    username = _required("VITALS_DRILL_USERNAME")
    password_hash = _required("VITALS_DRILL_PASSWORD_HASH")
    if _USERNAME.fullmatch(username) is None:
        raise RuntimeError("drill_username_invalid")
    if _BCRYPT.fullmatch(password_hash) is None:
        raise RuntimeError("drill_password_hash_invalid")
    marker_path = Path(_required("VITALS_RESTORE_DRILL_MARKER_FILE"))
    if marker_path != _MARKER_FILE:
        raise RuntimeError("restore_drill_marker_invalid")
    try:
        marker_stat = marker_path.lstat()
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RuntimeError("restore_drill_marker_invalid") from None
    if marker_path.is_symlink() or not stat.S_ISREG(marker_stat.st_mode):
        raise RuntimeError("restore_drill_marker_invalid")
    if not isinstance(marker, dict):
        raise RuntimeError("restore_drill_marker_invalid")
    run_id = str(marker.get("run_id", ""))
    if (
        _RUN_ID.fullmatch(run_id) is None
        or marker != {"project": f"vitals_drill_{run_id}", "run_id": run_id}
        or username != f"drill-{run_id}"
    ):
        raise RuntimeError("restore_drill_marker_invalid")

    raw_url = _required("VITALS_DATABASE_URL")
    try:
        url = make_url(raw_url)
    except sa.exc.ArgumentError:
        raise RuntimeError("database_url_invalid") from None
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "vitals_db"
        or (url.port or 5432) != 5432
        or url.database != f"vitals_drill_{run_id}"
        or url.username != f"vitals_drill_owner_{run_id}"
        or not url.password
        or url.query
    ):
        raise RuntimeError("database_url_invalid")

    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            owner_count = int(
                await connection.scalar(
                    sa.text(
                        "SELECT count(DISTINCT users.id) FROM users "
                        "JOIN user_roles roles ON roles.user_id=users.id "
                        "WHERE roles.role='platform_superadmin' "
                        "AND users.status='active'"
                    )
                )
                or 0
            )
            if owner_count != 1:
                raise RuntimeError("active_owner_count_invalid")
            result = await connection.execute(
                sa.text(
                    "UPDATE users SET username=:username, "
                    "normalized_username=:username, password_hash=:password_hash, "
                    "session_version=session_version + 1 "
                    "WHERE id=(SELECT users.id FROM users "
                    "JOIN user_roles roles ON roles.user_id=users.id "
                    "WHERE roles.role='platform_superadmin' "
                    "AND users.status='active')"
                ),
                {"password_hash": password_hash, "username": username},
            )
            if result.rowcount != 1:
                raise RuntimeError("owner_update_count_invalid")
    finally:
        await engine.dispose()
    return {
        "format_version": 1,
        "operation": "prepare_restore_drill_login",
        "result": "ok",
        "owners_updated": 1,
    }


def main() -> int:
    try:
        payload = asyncio.run(_prepare())
        exit_code = 0
    except Exception as exc:
        code = str(exc)
        if not re.fullmatch(r"[a-z0-9_]+", code):
            code = "internal_error"
        payload = {
            "format_version": 1,
            "operation": "prepare_restore_drill_login",
            "result": "error",
            "error_code": code,
        }
        exit_code = 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
