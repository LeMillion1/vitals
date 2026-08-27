"""Production-shaped Stage-3K rehearsal from a synthetic revision-0034 lake."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker


import vitals.models  # noqa: F401 -- register the complete schema for teardown
from vitals.models.base import Base
from vitals.operations.ownership import portability_v1
from vitals.services.portability import v1_export
from vitals.services.conflicts import catalog as conflict_catalog
from vitals.services.hrt import catalog
from vitals.services.identity.bootstrap import bootstrap_legacy_owner
from vitals.operations.ownership.shared_report import (
    SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.tenancy.bootstrap import bootstrap_legacy_resource_roots


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
REPORT_IDS = (51_201, 51_202, 51_203)
TOKENS = (
    "synthetic-private-stage3k-live-token",
    "synthetic-private-stage3k-revoked-token",
    "synthetic-private-stage3k-expired-token",
)
PRIVATE_SENTINEL = "synthetic-private-stage3k-report-snapshot-title-note"
OWNER_USERNAME = "synthetic-stage3k-owner"
DOWNGRADE_REFUSAL = (
    "0045 downgrade refused: ownership backfill checkpoints contain durable state"
)
PASSWORD_HASH = (
    "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
)
AGGREGATE_CLI_KEYS = {
    "batch_scanned_rows",
    "batch_size",
    "batch_table",
    "batch_unchanged_rows",
    "batch_updated_rows",
    "batches_processed",
    "completed",
    "completed_tables",
    "data_checksum_after",
    "data_checksum_before",
    "format_version",
    "max_batches",
    "mode",
    "operation",
    "ownership_checksum_after",
    "phase",
    "remaining_rows",
    "result",
    "rows_above_high_watermark",
    "scanned_rows",
    "snapshot_rows",
    "status",
    "tables_total",
    "unchanged_rows",
    "updated_rows",
}
RAW_CLI_KEYS = AGGREGATE_CLI_KEYS - {
    "batch_table",
    "completed_tables",
    "snapshot_rows",
    "tables_total",
}


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"unsupported synthetic hash value {type(value).__name__}")


def _digest(rows: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        rows,
        default=_json_default,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _reflect(connection: sa.Connection, names: tuple[str, ...]) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    return {
        name: sa.Table(name, metadata, autoload_with=connection) for name in names
    }


def _seed_revision_0034(connection: sa.Connection) -> None:
    reports = _reflect(connection, ("shared_reports",))["shared_reports"]
    created_at = datetime(2026, 8, 19, 8, 30, 15)
    updated_at = datetime(2026, 8, 19, 9, 45, 30)
    common = {
        "password_hash": PASSWORD_HASH,
        "preset": "full",
        "domains": ["labs"],
        "period_start": date(2026, 7, 1),
        "period_end": date(2026, 8, 18),
        "labs_flagged_only": False,
        "note": PRIVATE_SENTINEL,
        "opened_count": 0,
        "last_opened_at": None,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    connection.execute(
        reports.insert(),
        [
            {
                **common,
                "id": REPORT_IDS[0],
                "token": TOKENS[0],
                "title": f"{PRIVATE_SENTINEL}-live",
                "snapshot": {"private": PRIVATE_SENTINEL, "state": "live"},
                "expires_at": datetime(2030, 8, 30, 12, 0),
                "revoked_at": None,
            },
            {
                **common,
                "id": REPORT_IDS[1],
                "token": TOKENS[1],
                "title": f"{PRIVATE_SENTINEL}-revoked",
                "snapshot": None,
                "expires_at": datetime(2030, 8, 30, 12, 0),
                "revoked_at": datetime(2026, 8, 20, 12, 0),
            },
            {
                **common,
                "id": REPORT_IDS[2],
                "token": TOKENS[2],
                "title": f"{PRIVATE_SENTINEL}-expired",
                "snapshot": {"private": PRIVATE_SENTINEL, "state": "expired"},
                "expires_at": datetime(2026, 8, 20, 12, 0),
                "revoked_at": None,
            },
        ],
    )
    connection.execute(
        sa.text(
            "SELECT setval(pg_get_serial_sequence('shared_reports', 'id'), "
            ":value, true)"
        ),
        {"value": max(REPORT_IDS)},
    )


def _report_projection(connection: sa.Connection) -> list[dict[str, Any]]:
    table = _reflect(connection, ("shared_reports",))["shared_reports"]
    return [
        dict(row)
        for row in connection.execute(
            sa.select(*table.c).order_by(table.c.id)
        ).mappings()
    ]


def _historical_business_hash(connection: sa.Connection) -> str:
    rows = _report_projection(connection)
    return _digest(
        [
            {
                key: value
                for key, value in row.items()
                if key
                not in {
                    "subject_id",
                    "created_by_user_id",
                    "revoked_by_user_id",
                }
            }
            for row in rows
            if row["id"] in REPORT_IDS
        ]
    )


async def _run_sync(engine: AsyncEngine, function):
    async with engine.begin() as connection:
        return await connection.run_sync(function)


async def _reset_to_migration_base(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))


async def _bootstrap_roots(engine: AsyncEngine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        identity = await bootstrap_legacy_owner(
            session,
            username=OWNER_USERNAME,
            password_hash=PASSWORD_HASH,
            timezone="Asia/Almaty",
        )
        await bootstrap_legacy_resource_roots(session, subject_id=identity.subject_id)
        await session.commit()
        return identity


async def _sync_catalogs(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await catalog.sync_catalog(session)
        await conflict_catalog.sync_catalog(session)
        await session.commit()


async def _run_cli_process(
    script: str,
    arguments: list[str],
    *,
    database_url: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["VITALS_DATABASE_URL"] = database_url
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(REPOSITORY_ROOT / "scripts" / script), *arguments],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


async def _run_cli(
    script: str,
    arguments: list[str],
    *,
    database_url: str,
    expected_keys: set[str],
) -> dict[str, Any]:
    process = await _run_cli_process(script, arguments, database_url=database_url)
    assert process.returncode == 0, (process.stdout, process.stderr)
    assert process.stderr == ""
    assert process.stdout.count("\n") == 1
    payload = json.loads(process.stdout)
    assert set(payload) == expected_keys
    rendered = process.stdout + process.stderr
    assert database_url not in rendered
    assert PRIVATE_SENTINEL not in rendered
    assert not any(token in rendered for token in TOKENS)
    assert not any(str(report_id) in rendered for report_id in REPORT_IDS)
    return payload


async def _checkpoint_states(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT * FROM ownership_backfill_checkpoints "
                "WHERE phase_key=:phase OR phase_key LIKE :prefix "
                "ORDER BY phase_key"
            ),
            {
                "phase": SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE,
                "prefix": f"{SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE}.%",
            },
        )
        return [dict(row) for row in rows.mappings()]


async def _report_graph(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text("SELECT * FROM shared_reports ORDER BY id")
        )
        return [dict(row) for row in rows.mappings()]


async def _phase_statuses(engine: AsyncEngine, phase: str) -> tuple[str, ...]:
    async with engine.connect() as connection:
        rows = await connection.scalars(
            sa.text(
                "SELECT status FROM ownership_backfill_checkpoints "
                "WHERE phase_key=:phase OR phase_key LIKE :prefix "
                "ORDER BY phase_key"
            ),
            {"phase": phase, "prefix": f"{phase}.%"},
        )
        return tuple(rows)


async def _round_trip_portability_v1(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        snapshot = await v1_export.export_full(session)
        assert "shared_reports" not in snapshot
        await portability_v1.import_full(session, snapshot)
        await session.commit()


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        )
