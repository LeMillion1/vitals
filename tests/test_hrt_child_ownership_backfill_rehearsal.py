"""Production-shaped Stage-3C rehearsal from a synthetic revision-0034 lake."""

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
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import vitals.models  # noqa: F401 -- register the complete schema for teardown
from vitals.models.base import Base
from vitals.services import hrt_cycle_service, hrt_template_service
from vitals.services.hrt_child_ownership_backfill_service import (
    HRT_CHILD_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.identity_bootstrap import bootstrap_legacy_owner
from vitals.services.normalized_ownership_backfill_service import (
    NORMALIZED_MANUAL_BACKFILL_PHASE,
)
from vitals.services.raw_ownership_backfill_service import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
TABLES = (
    "hrt_cycles",
    "hrt_cycle_items",
    "hrt_cycle_templates",
    "hrt_cycle_template_items",
)
ROW_IDS = {
    "hrt_cycles": (21_001,),
    "hrt_cycle_items": (21_011, 21_012),
    "hrt_cycle_templates": (22_001,),
    "hrt_cycle_template_items": (22_011, 22_012),
}
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
        tables["hrt_cycles"].insert().values(
            id=ROW_IDS["hrt_cycles"][0],
            domain="hrt",
            source="manual",
            name="Synthetic course",
            kind="course",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 10, 1),
            note="Synthetic cycle parent",
            **common,
        )
    )
    connection.execute(
        tables["hrt_cycle_items"].insert(),
        [
            {
                "id": ROW_IDS["hrt_cycle_items"][0],
                "cycle_id": ROW_IDS["hrt_cycles"][0],
                "compound_id": None,
                "compound_key": "synthetic-compound-a",
                "unit": "mg",
                "start_offset_days": 0,
                "schedule": [{"dose": 10.0, "interval_days": 2, "duration_days": 14}],
                "note": "Synthetic cycle child A",
                **common,
            },
            {
                "id": ROW_IDS["hrt_cycle_items"][1],
                "cycle_id": ROW_IDS["hrt_cycles"][0],
                "compound_id": None,
                "compound_key": "synthetic-compound-b",
                "unit": "mg",
                "start_offset_days": 3,
                "schedule": [{"dose": 5.0, "interval_days": 1, "duration_days": 7}],
                "note": "Synthetic cycle child B",
                **common,
            },
        ],
    )
    connection.execute(
        tables["hrt_cycle_templates"].insert().values(
            id=ROW_IDS["hrt_cycle_templates"][0],
            domain="hrt",
            source="manual",
            name="Synthetic template",
            kind="course",
            note="Synthetic template parent",
            **common,
        )
    )
    connection.execute(
        tables["hrt_cycle_template_items"].insert(),
        [
            {
                "id": ROW_IDS["hrt_cycle_template_items"][0],
                "template_id": ROW_IDS["hrt_cycle_templates"][0],
                "compound_key": "synthetic-compound-a",
                "unit": "mg",
                "start_offset_days": 0,
                "schedule": [{"dose": 10.0, "interval_days": 2, "duration_days": 14}],
                "note": "Synthetic template child A",
                **common,
            },
            {
                "id": ROW_IDS["hrt_cycle_template_items"][1],
                "template_id": ROW_IDS["hrt_cycle_templates"][0],
                "compound_key": "synthetic-compound-b",
                "unit": "mg",
                "start_offset_days": 3,
                "schedule": [{"dose": 5.0, "interval_days": 1, "duration_days": 7}],
                "note": "Synthetic template child B",
                **common,
            },
        ],
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
                .where(table.c.id.in_(ROW_IDS[name]))
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
            username="synthetic-stage3c-owner",
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
        timeout=90,
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
            {"prefix": f"{HRT_CHILD_OWNERSHIP_BACKFILL_PHASE}.%"},
        )
        return [dict(row) for row in rows.mappings()]


async def _ownership_state(engine: AsyncEngine) -> dict[str, list[Any]]:
    state: dict[str, list[Any]] = {}
    async with engine.connect() as connection:
        for table_name in TABLES:
            columns = [sa.column("id"), sa.column("subject_id")]
            if "items" not in table_name:
                columns.append(sa.column("actor_user_id"))
            table = sa.table(table_name, *columns)
            columns = [table.c.id, table.c.subject_id]
            if "items" not in table_name:
                columns.append(table.c.actor_user_id)
            rows = await connection.execute(
                sa.select(*columns)
                .where(table.c.id.in_(ROW_IDS[table_name]))
                .order_by(table.c.id)
            )
            state[table_name] = [dict(row) for row in rows.mappings()]
    return state


async def _strict_consumers(engine: AsyncEngine, subject_id: Any) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        cycles = await hrt_cycle_service.list_cycles(
            session,
            subject_id=subject_id,
            include_legacy_unowned=False,
        )
        templates = await hrt_template_service.list_templates(
            session,
            subject_id=subject_id,
            include_legacy_unowned=False,
        )
        cycle = next(row for row in cycles if row.id == ROW_IDS["hrt_cycles"][0])
        template = next(
            row for row in templates if row.id == ROW_IDS["hrt_cycle_templates"][0]
        )
        assert [row.id for row in cycle.items] == list(ROW_IDS["hrt_cycle_items"])
        assert [row.id for row in template.items] == list(
            ROW_IDS["hrt_cycle_template_items"]
        )
        assert {row.subject_id for row in cycle.items} == {subject_id}
        assert {row.subject_id for row in template.items} == {subject_id}


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_0034_upgrade_dependencies_and_hrt_child_stop_resume(
    db_session,
    monkeypatch,
):
    """Rehearse real DDL, dependency completion, resume, and rollback refusal."""

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

        raw = await _run_cli(
            "backfill_subject_ownership.py",
            ["--apply"],
            database_url=database_url,
            expected_keys=RAW_CLI_KEYS,
        )
        assert raw["phase"] == RAW_OWNERSHIP_BACKFILL_PHASE
        assert raw["status"] == "completed"
        assert raw["scanned_rows"] == 0

        normalized = await _run_cli(
            "backfill_normalized_subject_ownership.py",
            ["--apply", "--batch-size", "1000", "--max-batches", "100"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert normalized["phase"] == NORMALIZED_MANUAL_BACKFILL_PHASE
        assert normalized["status"] == "completed"
        assert normalized["completed_tables"] == 17

        status = await _run_cli(
            "backfill_hrt_child_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert status["phase"] == HRT_CHILD_OWNERSHIP_BACKFILL_PHASE
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["tables_total"] == 2
        assert status["snapshot_rows"] == 4
        assert status["remaining_rows"] == 4
        assert status["batches_processed"] == 0
        assert await _checkpoint_states(engine) == []

        first = await _run_cli(
            "backfill_hrt_child_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert first["status"] == "running"
        assert first["batch_table"] == "hrt_cycle_items"
        assert first["batch_scanned_rows"] == 1
        assert first["batch_updated_rows"] == 1
        assert first["scanned_rows"] == 1
        assert first["completed_tables"] == 0
        first_checkpoints = await _checkpoint_states(engine)
        assert len(first_checkpoints) == 1
        assert first_checkpoints[0]["status"] == "running"

        stopped = await _run_cli(
            "backfill_hrt_child_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert stopped["status"] == "running"
        assert stopped["scanned_rows"] == 1
        assert stopped["completed_tables"] == 0
        assert await _checkpoint_states(engine) == first_checkpoints

        resumed = await _run_cli(
            "backfill_hrt_child_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert resumed["status"] == "completed"
        assert resumed["completed"] is True
        assert resumed["batch_table"] == "hrt_cycle_template_items"
        assert resumed["tables_total"] == 2
        assert resumed["completed_tables"] == 2
        assert resumed["snapshot_rows"] == 4
        assert resumed["scanned_rows"] == 4
        assert resumed["updated_rows"] == 4
        assert resumed["unchanged_rows"] == 0
        assert resumed["remaining_rows"] == 0

        completed_checkpoints = await _checkpoint_states(engine)
        assert len(completed_checkpoints) == 2
        assert {row["status"] for row in completed_checkpoints} == {"completed"}

        idempotent = await _run_cli(
            "backfill_hrt_child_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "3"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert idempotent["status"] == "completed"
        assert idempotent["batch_scanned_rows"] == 0
        assert idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoints

        ownership = await _ownership_state(engine)
        assert ownership["hrt_cycles"][0]["subject_id"] == identity.subject_id
        assert ownership["hrt_cycles"][0]["actor_user_id"] is None
        assert ownership["hrt_cycle_templates"][0]["subject_id"] == identity.subject_id
        assert ownership["hrt_cycle_templates"][0]["actor_user_id"] is None
        for table_name in ("hrt_cycle_items", "hrt_cycle_template_items"):
            assert {row["subject_id"] for row in ownership[table_name]} == {
                identity.subject_id
            }

        await _strict_consumers(engine, identity.subject_id)
        assert await _run_sync(engine, _non_ownership_hashes) == before_hashes

        with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_REFUSAL)):
            await asyncio.to_thread(command.downgrade, alembic_config, "0044")
        # The refusal rolls the whole downgrade back, so head is still 0046 and
        # the Stage-4 subject-equality references stay installed.
        assert await _alembic_version(engine) == "0046"
        assert await _checkpoint_states(engine) == completed_checkpoints
        assert await _run_sync(engine, _non_ownership_hashes) == before_hashes
    finally:
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, "head")
        await engine.dispose()
