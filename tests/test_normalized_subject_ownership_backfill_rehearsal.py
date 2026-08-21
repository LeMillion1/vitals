"""Production-shaped Stage-3B rehearsal from a synthetic revision-0034 lake."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tests.conftest import alembic_head_revision

import vitals.models  # noqa: F401 -- register the complete schema for teardown
from vitals.models.base import Base
from vitals.services.identity_bootstrap import bootstrap_legacy_owner
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
RAW_PHASE_KEY = "stage3.raw_payloads.v1"
NORMALIZED_PHASE_KEY = "stage3.normalized_manual.v1"
TABLES = (
    "annotations",
    "body_measurements",
    "meal_logs",
    "milestones",
    "noise_markers",
    "supplements",
)
ROW_IDS = {table: 10_001 + index for index, table in enumerate(TABLES)}
DOWNGRADE_REFUSAL = (
    "0045 downgrade refused: ownership backfill checkpoints contain durable state"
)
PASSWORD_HASH = (
    "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
)
RAW_CLI_KEYS = {
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
NORMALIZED_CLI_KEYS = {
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


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime, time)):
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
        name: sa.Table(name, metadata, autoload_with=connection) for name in TABLES
    }


def _seed_revision_0034(connection: sa.Connection) -> None:
    tables = _reflect_tables(connection)
    created_at = datetime(2026, 8, 20, 8, 30, 15)
    updated_at = datetime(2026, 8, 20, 9, 45, 30)
    common = {"created_at": created_at, "updated_at": updated_at}

    connection.execute(
        tables["annotations"].insert().values(
            id=ROW_IDS["annotations"],
            date=date(2026, 8, 18),
            domain="timeline",
            source="manual",
            kind="note",
            title="Synthetic timeline fixture",
            note="Synthetic annotation details",
            **common,
        )
    )
    connection.execute(
        tables["body_measurements"].insert().values(
            id=ROW_IDS["body_measurements"],
            date=date(2026, 8, 18),
            domain="weight",
            source="manual",
            neck_cm=39.0,
            waist_cm=87.0,
            body_fat_pct=18.5,
            note="Synthetic body fixture",
            **common,
        )
    )
    connection.execute(
        tables["meal_logs"].insert().values(
            id=ROW_IDS["meal_logs"],
            date=date(2026, 8, 19),
            domain="nutrition",
            source="manual",
            name="Synthetic meal fixture",
            eaten_at=time(12, 15),
            calories=540.0,
            protein_g=42.0,
            note="Synthetic nutrition details",
            **common,
        )
    )
    connection.execute(
        tables["milestones"].insert().values(
            id=ROW_IDS["milestones"],
            domain="weight",
            name="Synthetic milestone fixture",
            target_value=80.0,
            target_unit="kg",
            deadline=date(2026, 12, 31),
            status="active",
            note="Synthetic milestone details",
            **common,
        )
    )
    connection.execute(
        tables["noise_markers"].insert().values(
            id=ROW_IDS["noise_markers"],
            domain="weight",
            source="manual",
            start_date=date(2026, 8, 15),
            end_date=date(2026, 8, 17),
            reason="Synthetic noise fixture",
            direction="up",
            **common,
        )
    )
    connection.execute(
        tables["supplements"].insert().values(
            id=ROW_IDS["supplements"],
            domain="supplements",
            source="manual",
            name="Synthetic supplement fixture",
            key="synthetic-supplement",
            dose="1 synthetic unit",
            timing="morning",
            evidence="C",
            active=True,
            note="Synthetic supplement details",
            **common,
        )
    )


def _non_ownership_hashes(connection: sa.Connection) -> dict[str, str]:
    tables = _reflect_tables(connection)
    result: dict[str, str] = {}
    for name, table in tables.items():
        columns = [
            column
            for column in table.c
            if column.name not in {"subject_id", "actor_user_id"}
        ]
        rows = [
            dict(row)
            for row in connection.execute(
                sa.select(*columns)
                .where(table.c.id == ROW_IDS[name])
                .order_by(table.c.id)
            ).mappings()
        ]
        result[name] = _digest(rows)
    return result


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
            username="synthetic-stage3b-owner",
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
    script: str,
    arguments: list[str],
    *,
    database_url: str,
    expected_keys: set[str],
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["VITALS_DATABASE_URL"] = database_url
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(REPOSITORY_ROOT / "scripts" / script), *arguments],
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
    assert set(payload) == expected_keys
    assert database_url not in process.stdout
    return payload


async def _checkpoint_states(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT * FROM ownership_backfill_checkpoints "
                "WHERE phase_key LIKE :prefix ORDER BY phase_key"
            ),
            {"prefix": f"{NORMALIZED_PHASE_KEY}.%"},
        )
        return [dict(row) for row in rows.mappings()]


async def _ownership_state(engine: AsyncEngine) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    async with engine.connect() as connection:
        for table_name in TABLES:
            result = await connection.execute(
                sa.text(
                    f"SELECT subject_id, actor_user_id FROM {table_name} "
                    "WHERE id = :row_id"
                ),
                {"row_id": ROW_IDS[table_name]},
            )
            row = result.mappings().one()
            rows.append({"table": table_name, **dict(row)})
    return rows


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_0034_upgrade_stage3a_then_stage3b_stop_resume(
    db_session,
    monkeypatch,
):
    """Rehearse real DDL, process stop/resume, idempotence, and rollback gate."""

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    alembic_config = AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    await db_session.close()
    engine = create_async_engine(database_url, poolclass=NullPool)
    migration_control_ready = False

    try:
        migration_control_ready = True
        await _reset_to_migration_base(engine)
        await asyncio.to_thread(command.upgrade, alembic_config, "0034")
        await _run_sync(engine, _seed_revision_0034)
        before_hashes = await _run_sync(engine, _non_ownership_hashes)

        await asyncio.to_thread(command.upgrade, alembic_config, "head")
        identity = await _bootstrap_roots(engine)
        assert await _checkpoint_states(engine) == []

        raw_prerequisite = await _run_cli(
            "backfill_subject_ownership.py",
            ["--apply"],
            database_url=database_url,
            expected_keys=RAW_CLI_KEYS,
        )
        assert raw_prerequisite["phase"] == RAW_PHASE_KEY
        assert raw_prerequisite["status"] == "completed"
        assert raw_prerequisite["scanned_rows"] == 0

        status = await _run_cli(
            "backfill_normalized_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=NORMALIZED_CLI_KEYS,
        )
        assert status["phase"] == NORMALIZED_PHASE_KEY
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["tables_total"] == 17
        # The real 0034 chain carries reviewed catalog seeds (for example
        # skincare products) in addition to the six explicit rehearsal rows.
        snapshot_rows = status["snapshot_rows"]
        assert snapshot_rows >= len(TABLES)
        assert status["remaining_rows"] == snapshot_rows
        assert status["batches_processed"] == 0
        assert await _checkpoint_states(engine) == []

        first = await _run_cli(
            "backfill_normalized_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "4"],
            database_url=database_url,
            expected_keys=NORMALIZED_CLI_KEYS,
        )
        assert first["status"] == "running"
        assert first["batch_table"] == "body_measurements"
        assert first["batch_scanned_rows"] == 1
        assert first["batch_updated_rows"] == 1
        assert first["scanned_rows"] == 2
        assert first["updated_rows"] == 2
        assert first["completed_tables"] == 4
        first_checkpoints = await _checkpoint_states(engine)
        assert len(first_checkpoints) == 4
        assert {row["status"] for row in first_checkpoints} == {"completed"}

        stopped_status = await _run_cli(
            "backfill_normalized_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=NORMALIZED_CLI_KEYS,
        )
        assert stopped_status["status"] == "running"
        assert stopped_status["scanned_rows"] == 2
        assert stopped_status["completed_tables"] == 4
        assert await _checkpoint_states(engine) == first_checkpoints

        resumed = await _run_cli(
            "backfill_normalized_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "20"],
            database_url=database_url,
            expected_keys=NORMALIZED_CLI_KEYS,
        )
        assert resumed["status"] == "completed"
        assert resumed["completed"] is True
        assert resumed["batch_table"] == "supplements"
        assert resumed["tables_total"] == 17
        assert resumed["completed_tables"] == 17
        assert resumed["snapshot_rows"] == snapshot_rows
        assert resumed["scanned_rows"] == snapshot_rows
        assert resumed["updated_rows"] == snapshot_rows
        assert resumed["unchanged_rows"] == 0
        assert resumed["remaining_rows"] == 0

        completed_checkpoints = await _checkpoint_states(engine)
        assert len(completed_checkpoints) == 17
        assert {row["status"] for row in completed_checkpoints} == {"completed"}

        idempotent = await _run_cli(
            "backfill_normalized_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "3"],
            database_url=database_url,
            expected_keys=NORMALIZED_CLI_KEYS,
        )
        assert idempotent["status"] == "completed"
        assert idempotent["batch_scanned_rows"] == 0
        assert idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoints

        ownership_rows = await _ownership_state(engine)
        assert len(ownership_rows) == len(TABLES)
        for row in ownership_rows:
            assert row["subject_id"] == identity.subject_id
            assert row["actor_user_id"] is None

        after_hashes = await _run_sync(engine, _non_ownership_hashes)
        assert after_hashes == before_hashes

        with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_REFUSAL)):
            await asyncio.to_thread(command.downgrade, alembic_config, "0044")
        # The refusal rolls the whole downgrade back, so the schema is left at
        # head with every later revision's objects still installed.
        assert await _alembic_version(engine) == alembic_head_revision()
        assert await _checkpoint_states(engine) == completed_checkpoints
        assert await _run_sync(engine, _non_ownership_hashes) == before_hashes
    finally:
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, "head")
        await engine.dispose()
