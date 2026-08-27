"""Production-shaped Stage-3G rehearsal from a synthetic revision-0034 lake."""

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
from vitals.operations.ownership import portability_v1
from vitals.services import conflict_catalog, data_portability_service
from vitals.services.hrt import catalog
from vitals.operations.ownership.conflict_rule import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.hevy_child import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.hrt_child import (
    HRT_CHILD_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.hrt_compound import (
    HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.identity_bootstrap import bootstrap_legacy_owner
from vitals.operations.ownership.normalized import (
    NORMALIZED_MANUAL_BACKFILL_PHASE,
)
from vitals.operations.ownership.provider_raw import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.raw import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots
from vitals.ownership import PRE_OWNERSHIP_CONTRACT_REVISION


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
RAW_ID = 43_501
WORKOUT_ID = 43_601
CUSTOM_RULE_ID = 43_701
CUSTOM_MESSAGE = "Synthetic custom Stage-3G rule"
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
    tables = _reflect(
        connection,
        ("conflict_rules", "hevy_workouts", "raw_payloads"),
    )
    created_at = datetime(2026, 8, 20, 8, 30, 15)
    updated_at = datetime(2026, 8, 20, 9, 45, 30)
    payload = {
        "id": "synthetic-stage3g-workout",
        "title": "Synthetic Stage-3G dependency",
        "description": None,
        "start_time": "2026-08-18T08:00:00Z",
        "end_time": "2026-08-18T09:00:00Z",
        "updated_at": "2026-08-18T10:00:00Z",
        "exercises": [],
    }
    connection.execute(
        tables["raw_payloads"].insert().values(
            id=RAW_ID,
            domain="workouts",
            source="hevy_api",
            external_id=payload["id"],
            fetched_at=datetime(2026, 8, 20, 7, 0, 0),
            payload=payload,
        )
    )
    connection.execute(
        tables["hevy_workouts"].insert().values(
            id=WORKOUT_ID,
            date=date(2026, 8, 18),
            domain="workouts",
            source="hevy_api",
            external_id=payload["id"],
            raw_payload_id=RAW_ID,
            title=payload["title"],
            description=None,
            start_time=datetime(2026, 8, 18, 8, 0, 0),
            end_time=datetime(2026, 8, 18, 9, 0, 0),
            duration_seconds=3_600,
            hevy_updated_at=datetime(2026, 8, 18, 10, 0, 0),
            created_at=created_at,
            updated_at=updated_at,
        )
    )
    connection.execute(
        tables["conflict_rules"].insert().values(
            id=CUSTOM_RULE_ID,
            code=None,
            rule_type="soft_warn",
            domain_a="nutrition",
            condition_a={"synthetic_a": True},
            domain_b="labs",
            condition_b={"synthetic_b": {"$gte": 1}},
            severity="warn",
            message=CUSTOM_MESSAGE,
            params={"hours": 2},
            category=None,
            source=None,
            evidence=None,
            active=False,
            created_at=created_at,
            updated_at=updated_at,
        )
    )


def _custom_non_ownership_hash(connection: sa.Connection) -> str:
    rules = _reflect(connection, ("conflict_rules",))["conflict_rules"]
    columns = [column for column in rules.c if column.name != "subject_id"]
    rows = [
        dict(row)
        for row in connection.execute(
            sa.select(*columns).where(rules.c.id == CUSTOM_RULE_ID)
        ).mappings()
    ]
    return _digest(rows)


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
            username="synthetic-stage3g-owner",
            password_hash=PASSWORD_HASH,
            timezone="Asia/Almaty",
        )
        await bootstrap_legacy_resource_roots(
            session,
            subject_id=identity.subject_id,
        )
        await session.commit()
        return identity


async def _sync_catalog(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await catalog.sync_catalog(session)
        await conflict_catalog.sync_catalog(session)
        await session.commit()


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
    assert process.stdout.count("\n") == 1
    payload = json.loads(process.stdout)
    assert set(payload) == expected_keys
    assert database_url not in process.stdout
    assert CUSTOM_MESSAGE not in process.stdout
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
                "phase": CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE,
                "prefix": f"{CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE}.%",
            },
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


async def _rule_counts(engine: AsyncEngine) -> tuple[int, int]:
    async with engine.connect() as connection:
        result = await connection.execute(
            sa.text(
                "SELECT COUNT(*) AS total, "
                "COUNT(*) FILTER (WHERE code IS NULL) AS custom "
                "FROM conflict_rules"
            )
        )
        row = result.one()
        return int(row.total), int(row.custom)


async def _ownership_graph(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT id, code, subject_id, active FROM conflict_rules "
                "ORDER BY id"
            )
        )
        return [dict(row) for row in rows.mappings()]


async def _delete_one_curated_rule(engine: AsyncEngine) -> tuple[str, int]:
    async with engine.begin() as connection:
        row = (
            await connection.execute(
                sa.text(
                    "SELECT code, id FROM conflict_rules WHERE code IS NOT NULL "
                    "ORDER BY code LIMIT 1"
                )
            )
        ).one()
        await connection.execute(
            sa.text("DELETE FROM conflict_rules WHERE id=:id"),
            {"id": row.id},
        )
        return str(row.code), int(row.id)


async def _rule_id_for_code(engine: AsyncEngine, code: str) -> int:
    async with engine.connect() as connection:
        value = await connection.scalar(
            sa.text("SELECT id FROM conflict_rules WHERE code=:code"),
            {"code": code},
        )
        assert value is not None
        return int(value)


async def _round_trip_portability_v1(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        snapshot = await data_portability_service.export_full(session)
        await portability_v1.import_full(session, snapshot)
        await session.commit()


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_0034_conflict_rules_stop_resume_catalog_and_restore(
    db_session,
    monkeypatch,
):
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

        await asyncio.to_thread(command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION)
        identity = await _bootstrap_roots(engine)
        await _sync_catalog(engine)
        custom_hash_before = await _run_sync(engine, _custom_non_ownership_hash)

        prior_commands = (
            (
                "backfill_subject_ownership.py",
                ["--apply", "--batch-size", "1000"],
                RAW_OWNERSHIP_BACKFILL_PHASE,
                RAW_CLI_KEYS,
            ),
            (
                "backfill_normalized_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "100"],
                NORMALIZED_MANUAL_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),
            (
                "backfill_hrt_child_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                HRT_CHILD_OWNERSHIP_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),
            (
                "backfill_provider_raw_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),
            (
                "backfill_hevy_child_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),
            (
                "backfill_hrt_compound_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),
        )
        for script, arguments, phase, expected_keys in prior_commands:
            result = await _run_cli(
                script,
                arguments,
                database_url=database_url,
                expected_keys=expected_keys,
            )
            assert result["phase"] == phase
            assert result["status"] == "completed"

        total_rules, custom_rules = await _rule_counts(engine)
        assert total_rules > 3
        assert custom_rules == 1

        status = await _run_cli(
            "backfill_conflict_rule_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert status["phase"] == CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["tables_total"] == 1
        assert status["snapshot_rows"] == total_rules
        assert status["remaining_rows"] == total_rules
        assert await _checkpoint_states(engine) == []

        started = await _run_cli(
            "backfill_conflict_rule_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert started["status"] == "running"
        assert started["batch_table"] == "conflict_rules"
        assert started["batch_scanned_rows"] == 1
        stop_checkpoint = await _checkpoint_states(engine)
        assert len(stop_checkpoint) == 1
        assert stop_checkpoint[0]["status"] == "running"

        stopped = await _run_cli(
            "backfill_conflict_rule_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert stopped["status"] == "running"
        assert stopped["scanned_rows"] == 1
        assert await _checkpoint_states(engine) == stop_checkpoint

        resumed = await _run_cli(
            "backfill_conflict_rule_subject_ownership.py",
            ["--apply", "--batch-size", "1000", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert resumed["status"] == "completed"
        assert resumed["completed"] is True
        assert resumed["completed_tables"] == 1
        assert resumed["snapshot_rows"] == total_rules
        assert resumed["scanned_rows"] == total_rules
        assert resumed["remaining_rows"] == 0
        assert resumed["updated_rows"] == 1
        assert resumed["unchanged_rows"] == total_rules - 1
        assert await _run_sync(engine, _custom_non_ownership_hash) == custom_hash_before

        graph = await _ownership_graph(engine)
        custom = next(row for row in graph if row["id"] == CUSTOM_RULE_ID)
        assert custom["code"] is None
        assert custom["subject_id"] == identity.subject_id
        assert custom["active"] is False
        assert all(row["subject_id"] is None for row in graph if row["code"] is not None)

        completed_checkpoint = await _checkpoint_states(engine)
        idempotent = await _run_cli(
            "backfill_conflict_rule_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "3"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert idempotent["status"] == "completed"
        assert idempotent["batch_scanned_rows"] == 0
        assert idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoint

        deleted_code, deleted_id = await _delete_one_curated_rule(engine)
        await _sync_catalog(engine)
        assert await _rule_id_for_code(engine, deleted_code) != deleted_id
        assert await _run_sync(engine, _custom_non_ownership_hash) == custom_hash_before

        volatile_status = await _run_cli(
            "backfill_conflict_rule_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert volatile_status["status"] == "completed"
        assert await _checkpoint_states(engine) == completed_checkpoint

        await _round_trip_portability_v1(engine)
        assert await _phase_statuses(engine, RAW_OWNERSHIP_BACKFILL_PHASE) == (
            "restore_blocked",
        )
        restore_status = await _run_cli(
            "backfill_conflict_rule_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restore_status["status"] == "running"
        assert restore_status["remaining_rows"] == restore_status["snapshot_rows"]
        restore_checkpoint = await _checkpoint_states(engine)
        assert len(restore_checkpoint) == 1
        assert restore_checkpoint[0]["status"] == "running"

        restored = await _run_cli(
            "backfill_conflict_rule_subject_ownership.py",
            ["--apply", "--batch-size", "1000", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restored["status"] == "completed"
        assert restored["completed_tables"] == 1
        assert restored["remaining_rows"] == 0
        assert await _run_sync(engine, _custom_non_ownership_hash) == custom_hash_before
        restored_graph = await _ownership_graph(engine)
        restored_custom = next(row for row in restored_graph if row["id"] == CUSTOM_RULE_ID)
        assert restored_custom["subject_id"] == identity.subject_id
        assert all(
            row["subject_id"] is None
            for row in restored_graph
            if row["code"] is not None
        )
        assert await _phase_statuses(
            engine, CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE
        ) == ("completed",)

        restored_checkpoint = await _checkpoint_states(engine)
        with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_REFUSAL)):
            await asyncio.to_thread(command.downgrade, alembic_config, "0044")
        # The refusal rolls the whole downgrade back, so the schema is left at
        # head with every later revision's objects still installed.
        assert await _alembic_version(engine) == PRE_OWNERSHIP_CONTRACT_REVISION
        assert await _checkpoint_states(engine) == restored_checkpoint
    finally:
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION)
        await engine.dispose()
