"""Production-shaped Stage-3A rehearsal from a synthetic revision-0034 lake."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


import vitals.models  # noqa: F401 -- register the complete schema for teardown
from vitals.enums import IntegrationProvider
from vitals.models.base import Base
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.services.identity_bootstrap import bootstrap_legacy_owner
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots
from vitals.ownership import PRE_OWNERSHIP_CONTRACT_REVISION


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
PHASE_KEY = "stage3.raw_payloads.v1"
DOWNGRADE_REFUSAL = (
    "0045 downgrade refused: ownership backfill checkpoints contain durable state"
)
PASSWORD_HASH = (
    "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
)
RAW_IDS = (101, 102, 103, 104, 105)
CLI_OUTPUT_KEYS = {
    "batch_scanned_rows",
    "batch_size",
    "batch_unchanged_rows",
    "batch_updated_rows",
    "batches_processed",
    "completed",
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
    "status",
    "unchanged_rows",
    "updated_rows",
}


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
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


def _reflect_tables(connection: sa.Connection) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    return {
        name: sa.Table(name, metadata, autoload_with=connection)
        for name in (
            "raw_payloads",
            "signals",
            "shared_reports",
            "weekly_digests",
        )
    }


def _seed_revision_0034(connection: sa.Connection) -> None:
    tables = _reflect_tables(connection)
    fetched_at = datetime(2026, 8, 20, 8)
    connection.execute(
        tables["raw_payloads"].insert(),
        [
            {
                "id": 101,
                "domain": "garmin",
                "source": "garmin_api",
                "external_id": "synthetic-raw-101",
                "fetched_at": fetched_at,
                "payload": {"fixture": "garmin", "sequence": 1},
                "processed_at": fetched_at,
            },
            {
                "id": 102,
                "domain": "workouts",
                "source": "hevy_api",
                "external_id": "synthetic-raw-102",
                "fetched_at": fetched_at,
                "payload": {"fixture": "hevy", "sequence": 2},
                "processed_at": fetched_at,
            },
            {
                "id": 103,
                "domain": "signals",
                "source": "telegram",
                "external_id": "synthetic-raw-103",
                "fetched_at": fetched_at,
                "payload": {"fixture": "telegram", "sequence": 3},
                "processed_at": fetched_at,
            },
            {
                "id": 104,
                "domain": "genetics",
                "source": "vcf_import",
                "external_id": "synthetic-raw-104",
                "fetched_at": fetched_at,
                "payload": {"fixture": "vcf-import", "sequence": 4},
                "processed_at": None,
            },
            {
                "id": 105,
                "domain": "labs",
                "source": "lab_parser",
                "external_id": "synthetic-raw-105",
                "fetched_at": fetched_at,
                "payload": {"fixture": "lab-parser", "sequence": 5},
                "processed_at": fetched_at,
            },
        ],
    )
    connection.execute(
        tables["signals"].insert().values(
            id=201,
            date=date(2026, 8, 20),
            domain="signals",
            source="telegram",
            kind="number",
            key="synthetic_link",
            value_num=1.0,
            unit="count",
            note="synthetic link fixture",
            raw_id=103,
            batch_id="synthetic-batch",
            misparse=False,
        )
    )
    connection.execute(
        tables["shared_reports"].insert().values(
            id=301,
            token="a" * 64,
            password_hash=PASSWORD_HASH,
            title="Synthetic frozen report",
            preset="summary",
            domains=["signals"],
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 20),
            labs_flagged_only=False,
            note="synthetic frozen note",
            snapshot={"fixture": "frozen-report", "version": 1},
            expires_at=datetime(2026, 9, 20, 12),
            opened_count=0,
        )
    )
    connection.execute(
        tables["weekly_digests"].insert().values(
            id=401,
            date=date(2026, 8, 17),
            domain="milestones",
            source="scheduler",
            content="Synthetic frozen narrative",
            context_json={"fixture": "frozen-digest", "version": 1},
            model="synthetic/model-v1",
            kind="weekly",
        )
    )


def _legacy_hashes(connection: sa.Connection) -> dict[str, str]:
    tables = _reflect_tables(connection)
    raw_rows = [
        dict(row)
        for row in connection.execute(
            sa.select(
                tables["raw_payloads"].c.id,
                tables["raw_payloads"].c.domain,
                tables["raw_payloads"].c.source,
                tables["raw_payloads"].c.external_id,
                tables["raw_payloads"].c.fetched_at,
                tables["raw_payloads"].c.payload,
                tables["raw_payloads"].c.processed_at,
            )
            .where(tables["raw_payloads"].c.id.in_(RAW_IDS))
            .order_by(tables["raw_payloads"].c.id)
        ).mappings()
    ]
    link_rows = [
        dict(row)
        for row in connection.execute(
            sa.select(
                tables["signals"].c.id,
                tables["signals"].c.raw_id,
                tables["signals"].c.source,
                tables["signals"].c.batch_id,
            )
            .where(tables["signals"].c.id == 201)
            .order_by(tables["signals"].c.id)
        ).mappings()
    ]
    report_rows = [
        dict(row)
        for row in connection.execute(
            sa.select(
                tables["shared_reports"].c.id,
                tables["shared_reports"].c.token,
                tables["shared_reports"].c.title,
                tables["shared_reports"].c.domains,
                tables["shared_reports"].c.period_start,
                tables["shared_reports"].c.period_end,
                tables["shared_reports"].c.note,
                tables["shared_reports"].c.snapshot,
            )
            .where(tables["shared_reports"].c.id == 301)
        ).mappings()
    ]
    digest_rows = [
        dict(row)
        for row in connection.execute(
            sa.select(
                tables["weekly_digests"].c.id,
                tables["weekly_digests"].c.date,
                tables["weekly_digests"].c.domain,
                tables["weekly_digests"].c.source,
                tables["weekly_digests"].c.content,
                tables["weekly_digests"].c.context_json,
                tables["weekly_digests"].c.model,
                tables["weekly_digests"].c.kind,
            )
            .where(tables["weekly_digests"].c.id == 401)
        ).mappings()
    ]
    return {
        "raw_data": _digest(raw_rows),
        "raw_links": _digest(link_rows),
        "frozen_outputs": _digest([*report_rows, *digest_rows]),
    }


async def _run_sync(engine: AsyncEngine, function):
    async with engine.begin() as connection:
        return await connection.run_sync(function)


async def _reset_to_migration_base(engine: AsyncEngine) -> None:
    """Remove the fixture's create-all schema before the real migration chain."""

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))


async def _bootstrap_roots(engine: AsyncEngine):
    factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    async with factory() as session:
        identity = await bootstrap_legacy_owner(
            session,
            username="synthetic-stage3-owner",
            password_hash=PASSWORD_HASH,
            timezone="Asia/Almaty",
        )
        await bootstrap_legacy_resource_roots(
            session,
            subject_id=identity.subject_id,
        )
        await session.commit()
        return identity


async def _run_cli(
    arguments: list[str],
    *,
    database_url: str,
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["VITALS_DATABASE_URL"] = database_url
    process = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / "backfill_subject_ownership.py"),
            *arguments,
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert process.returncode == 0, (process.stdout, process.stderr)
    assert process.stderr == ""
    lines = process.stdout.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload) == CLI_OUTPUT_KEYS
    assert database_url not in process.stdout
    return payload


async def _checkpoint_state(engine: AsyncEngine) -> dict[str, Any] | None:
    async with engine.connect() as connection:
        table = OwnershipBackfillCheckpoint.__table__
        result = await connection.execute(
            sa.select(table).where(table.c.phase_key == PHASE_KEY)
        )
        row = result.mappings().one_or_none()
        return dict(row) if row is not None else None


async def _raw_ownership_state(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        metadata = sa.MetaData()
        raw = await connection.run_sync(
            lambda sync_connection: sa.Table(
                "raw_payloads",
                metadata,
                autoload_with=sync_connection,
            )
        )
        rows = await connection.execute(
            sa.select(
                raw.c.id,
                raw.c.subject_id,
                raw.c.actor_user_id,
                raw.c.integration_connection_id,
                raw.c.file_asset_id,
            )
            .where(raw.c.id.in_(RAW_IDS))
            .order_by(raw.c.id)
        )
        return [dict(row) for row in rows.mappings()]


async def _connection_ids(engine: AsyncEngine) -> dict[str, Any]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT provider, id FROM integration_connections "
                "WHERE status = 'legacy' ORDER BY provider"
            )
        )
        return {row.provider: row.id for row in rows}


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_0034_upgrade_stop_resume_and_populated_downgrade(
    db_session,
    monkeypatch,
):
    """Rehearse the real operator path without touching non-synthetic data."""

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    alembic_config = AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    # The fixture created an empty head-shaped schema. Release its connection
    # before the real migration chain takes ACCESS EXCLUSIVE locks.
    await db_session.close()
    engine = create_async_engine(database_url, poolclass=NullPool)
    migration_control_ready = False

    try:
        migration_control_ready = True
        # The fixture creates the ORM schema, whose generated constraint names
        # are not a migration revision. Rebuild this throwaway synthetic DB from
        # Alembic base so both the 0034 baseline and the upgrade are real DDL.
        await _reset_to_migration_base(engine)
        await asyncio.to_thread(command.upgrade, alembic_config, "0034")
        await _run_sync(engine, _seed_revision_0034)
        before_hashes = await _run_sync(engine, _legacy_hashes)

        await asyncio.to_thread(command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION)
        identity = await _bootstrap_roots(engine)
        assert await _checkpoint_state(engine) is None

        status = await _run_cli([], database_url=database_url)
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["remaining_rows"] == 5
        assert status["batches_processed"] == 0
        assert await _checkpoint_state(engine) is None

        first = await _run_cli(
            ["--apply", "--batch-size", "2", "--max-batches", "1"],
            database_url=database_url,
        )
        assert first["status"] == "running"
        assert first["batch_scanned_rows"] == 2
        assert first["batch_updated_rows"] == 2
        assert first["scanned_rows"] == 2
        assert first["remaining_rows"] == 3
        first_checkpoint = await _checkpoint_state(engine)
        assert first_checkpoint is not None

        stopped_status = await _run_cli([], database_url=database_url)
        assert stopped_status["status"] == "running"
        assert stopped_status["scanned_rows"] == 2
        assert stopped_status["remaining_rows"] == 3
        assert await _checkpoint_state(engine) == first_checkpoint

        resumed = await _run_cli(
            ["--apply", "--batch-size", "2", "--max-batches", "2"],
            database_url=database_url,
        )
        assert resumed["status"] == "completed"
        assert resumed["completed"] is True
        assert resumed["batches_processed"] == 2
        assert resumed["scanned_rows"] == 5
        assert resumed["updated_rows"] == 5
        assert resumed["unchanged_rows"] == 0
        assert resumed["remaining_rows"] == 0

        completed_checkpoint = await _checkpoint_state(engine)
        assert completed_checkpoint is not None
        idempotent = await _run_cli(
            ["--apply", "--batch-size", "2", "--max-batches", "3"],
            database_url=database_url,
        )
        assert idempotent["status"] == "completed"
        assert idempotent["batch_scanned_rows"] == 0
        assert idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_state(engine) == completed_checkpoint

        ownership_rows = await _raw_ownership_state(engine)
        connection_ids = await _connection_ids(engine)
        expected_connections = {
            101: connection_ids[IntegrationProvider.GARMIN.value],
            102: connection_ids[IntegrationProvider.HEVY.value],
            103: connection_ids[IntegrationProvider.TELEGRAM.value],
            104: None,
            105: connection_ids[IntegrationProvider.OPENROUTER.value],
        }
        assert len(ownership_rows) == 5
        for row in ownership_rows:
            assert row["subject_id"] == identity.subject_id
            assert row["actor_user_id"] is None
            assert row["file_asset_id"] is None
            assert row["integration_connection_id"] == expected_connections[row["id"]]

        after_hashes = await _run_sync(engine, _legacy_hashes)
        assert after_hashes == before_hashes

        with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_REFUSAL)):
            await asyncio.to_thread(command.downgrade, alembic_config, "0034")
        # The refusal rolls the whole downgrade back, so the schema is left at
        # head with every later revision's objects still installed.
        assert await _alembic_version(engine) == PRE_OWNERSHIP_CONTRACT_REVISION
        assert await _checkpoint_state(engine) == completed_checkpoint
        assert await _run_sync(engine, _legacy_hashes) == before_hashes
    finally:
        # Keep the shared throwaway test DB at head even if an intermediate
        # assertion fails; the next fixture may otherwise see a stale revision.
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION)
        await engine.dispose()
