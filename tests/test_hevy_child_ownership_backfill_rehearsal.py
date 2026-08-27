"""Production-shaped Stage-3E rehearsal from a synthetic revision-0034 lake."""

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
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services.hevy import queries as hevy_queries
from vitals.services.hevy import raw_payloads as hevy_raw_payloads
from vitals.operations.ownership.hevy_child import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.hrt_child import (
    HRT_CHILD_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.identity.bootstrap import bootstrap_legacy_owner
from vitals.operations.ownership.normalized import (
    NORMALIZED_MANUAL_BACKFILL_PHASE,
)
from vitals.operations.ownership.provider_raw import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.raw import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.tenancy.bootstrap import bootstrap_legacy_resource_roots
from vitals.ownership import PRE_OWNERSHIP_CONTRACT_REVISION


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
TABLES = ("hevy_workouts", "hevy_exercises", "hevy_sets")
WORKOUT_ID = 42_001
RAW_ID = 41_001
EXERCISE_IDS = (42_101, 42_102)
SET_IDS = (42_201, 42_202)
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
    tables = _reflect(connection, ("raw_payloads", *TABLES))
    created_at = datetime(2026, 8, 20, 8, 30, 15)
    updated_at = datetime(2026, 8, 20, 9, 45, 30)
    common = {"created_at": created_at, "updated_at": updated_at}
    payload = {
        "id": "synthetic-hevy-workout",
        "title": "Synthetic workout",
        "description": "Synthetic provider fixture",
        "start_time": "2026-08-18T08:00:00Z",
        "end_time": "2026-08-18T09:00:00Z",
        "updated_at": "2026-08-18T10:00:00Z",
        "exercises": [
            {
                "index": 0,
                "title": "Synthetic press",
                "exercise_template_id": "synthetic-press",
                "sets": [
                    {
                        "index": 0,
                        "type": "normal",
                        "weight_kg": 80.0,
                        "reps": 8,
                        "rpe": 8.0,
                    }
                ],
            },
            {
                "index": 1,
                "title": "Synthetic row",
                "exercise_template_id": "synthetic-row",
                "sets": [
                    {
                        "index": 0,
                        "type": "normal",
                        "weight_kg": 70.0,
                        "reps": 10,
                        "rpe": 7.5,
                    }
                ],
            },
        ],
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
            description=payload["description"],
            start_time=datetime(2026, 8, 18, 8, 0, 0),
            end_time=datetime(2026, 8, 18, 9, 0, 0),
            duration_seconds=3_600,
            hevy_updated_at=datetime(2026, 8, 18, 10, 0, 0),
            **common,
        )
    )
    connection.execute(
        tables["hevy_exercises"].insert(),
        [
            {
                "id": EXERCISE_IDS[0],
                "workout_id": WORKOUT_ID,
                "exercise_index": 0,
                "title": "Synthetic press",
                "exercise_template_id": "synthetic-press",
                **common,
            },
            {
                "id": EXERCISE_IDS[1],
                "workout_id": WORKOUT_ID,
                "exercise_index": 1,
                "title": "Synthetic row",
                "exercise_template_id": "synthetic-row",
                **common,
            },
        ],
    )
    connection.execute(
        tables["hevy_sets"].insert(),
        [
            {
                "id": SET_IDS[0],
                "exercise_id": EXERCISE_IDS[0],
                "set_index": 0,
                "set_type": "normal",
                "weight_kg": 80.0,
                "reps": 8,
                "rpe": 8.0,
                **common,
            },
            {
                "id": SET_IDS[1],
                "exercise_id": EXERCISE_IDS[1],
                "set_index": 0,
                "set_type": "normal",
                "weight_kg": 70.0,
                "reps": 10,
                "rpe": 7.5,
                **common,
            },
        ],
    )


def _initial_non_ownership_hashes(connection: sa.Connection) -> dict[str, str]:
    tables = _reflect(connection, TABLES)
    row_ids = {
        "hevy_workouts": (WORKOUT_ID,),
        "hevy_exercises": EXERCISE_IDS,
        "hevy_sets": SET_IDS,
    }
    result: dict[str, str] = {}
    for name, table in tables.items():
        columns = [
            column
            for column in table.c
            if column.name
            not in {"subject_id", "actor_user_id", "integration_connection_id"}
        ]
        rows = [
            dict(row)
            for row in connection.execute(
                sa.select(*columns)
                .where(table.c.id.in_(row_ids[name]))
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
            username="synthetic-stage3e-owner",
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
    assert process.stdout.count("\n") == 1
    payload = json.loads(process.stdout)
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
            {"prefix": f"{HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE}.%"},
        )
        return [dict(row) for row in rows.mappings()]


async def _ownership_graph(engine: AsyncEngine) -> dict[str, list[dict[str, Any]]]:
    async with engine.connect() as connection:
        exercises = await connection.execute(
            sa.text(
                "SELECT e.id, e.subject_id, e.integration_connection_id, "
                "w.subject_id AS workout_subject_id, "
                "w.integration_connection_id AS workout_connection_id "
                "FROM hevy_exercises e JOIN hevy_workouts w ON w.id=e.workout_id "
                "WHERE w.id=:workout_id ORDER BY e.id"
            ),
            {"workout_id": WORKOUT_ID},
        )
        sets = await connection.execute(
            sa.text(
                "SELECT s.id, s.subject_id, s.integration_connection_id, "
                "e.subject_id AS exercise_subject_id, "
                "e.integration_connection_id AS exercise_connection_id "
                "FROM hevy_sets s JOIN hevy_exercises e ON e.id=s.exercise_id "
                "WHERE e.workout_id=:workout_id ORDER BY s.id"
            ),
            {"workout_id": WORKOUT_ID},
        )
        return {
            "hevy_exercises": [dict(row) for row in exercises.mappings()],
            "hevy_sets": [dict(row) for row in sets.mappings()],
        }


async def _strict_rebuild(engine: AsyncEngine, subject_id: Any) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        raw = await session.get(RawPayload, RAW_ID)
        assert raw is not None
        connection_id = raw.integration_connection_id
        assert connection_id is not None
        await hevy_raw_payloads.reparse_owned_from_raw(
            session,
            raw,
            identity=WriteIdentity(subject_id=subject_id, actor_user_id=None),
            integration_connection_id=connection_id,
        )
        await session.commit()

    graph = await _ownership_graph(engine)
    assert len(graph["hevy_exercises"]) == 2
    assert len(graph["hevy_sets"]) == 2
    assert {row["id"] for row in graph["hevy_exercises"]}.isdisjoint(EXERCISE_IDS)
    assert {row["id"] for row in graph["hevy_sets"]}.isdisjoint(SET_IDS)
    for row in graph["hevy_exercises"]:
        assert row["subject_id"] == subject_id == row["workout_subject_id"]
        assert (
            row["integration_connection_id"]
            == row["workout_connection_id"]
        )
    for row in graph["hevy_sets"]:
        assert row["subject_id"] == subject_id == row["exercise_subject_id"]
        assert (
            row["integration_connection_id"]
            == row["exercise_connection_id"]
        )

    async with factory() as session:
        workouts = await hevy_queries.list_workouts(
            session, subject_id=subject_id
        )
        workout = next(row for row in workouts if row.id == WORKOUT_ID)
        summary = hevy_queries.workout_summary(workout)
        assert summary["working_sets"] == 2
        assert summary["exercises"] == ["Synthetic press", "Synthetic row"]


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_0034_hevy_children_stop_resume_and_volatile_rebuild(
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
        before_hashes = await _run_sync(engine, _initial_non_ownership_hashes)

        await asyncio.to_thread(command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION)
        identity = await _bootstrap_roots(engine)

        raw = await _run_cli(
            "backfill_subject_ownership.py",
            ["--apply", "--batch-size", "1000"],
            database_url=database_url,
            expected_keys=RAW_CLI_KEYS,
        )
        assert raw["phase"] == RAW_OWNERSHIP_BACKFILL_PHASE
        assert raw["status"] == "completed"
        assert raw["scanned_rows"] == 1

        normalized = await _run_cli(
            "backfill_normalized_subject_ownership.py",
            ["--apply", "--batch-size", "1000", "--max-batches", "100"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert normalized["phase"] == NORMALIZED_MANUAL_BACKFILL_PHASE
        assert normalized["status"] == "completed"
        assert normalized["completed_tables"] == 17

        hrt_children = await _run_cli(
            "backfill_hrt_child_subject_ownership.py",
            ["--apply", "--batch-size", "1000", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert hrt_children["phase"] == HRT_CHILD_OWNERSHIP_BACKFILL_PHASE
        assert hrt_children["status"] == "completed"

        provider = await _run_cli(
            "backfill_provider_raw_subject_ownership.py",
            ["--apply", "--batch-size", "1000", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert provider["phase"] == PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE
        assert provider["status"] == "completed"
        assert provider["completed_tables"] == 4

        status = await _run_cli(
            "backfill_hevy_child_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert status["phase"] == HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["tables_total"] == 2
        assert status["snapshot_rows"] == 4
        assert status["remaining_rows"] == 4
        assert await _checkpoint_states(engine) == []

        exercise_started = await _run_cli(
            "backfill_hevy_child_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert exercise_started["status"] == "running"
        assert exercise_started["batch_table"] == "hevy_exercises"
        assert exercise_started["batch_scanned_rows"] == 1
        assert exercise_started["batch_updated_rows"] == 1
        exercise_stop = await _checkpoint_states(engine)
        assert len(exercise_stop) == 2
        # The group snapshot is frozen atomically before the first row advances.
        assert [row["status"] for row in exercise_stop] == ["running", "running"]

        stopped = await _run_cli(
            "backfill_hevy_child_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert stopped["scanned_rows"] == 1
        assert await _checkpoint_states(engine) == exercise_stop

        exercise_completed = await _run_cli(
            "backfill_hevy_child_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert exercise_completed["batch_table"] == "hevy_exercises"
        assert exercise_completed["completed_tables"] == 1

        set_started = await _run_cli(
            "backfill_hevy_child_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert set_started["status"] == "running"
        assert set_started["batch_table"] == "hevy_sets"
        assert set_started["batch_scanned_rows"] == 1
        assert set_started["batch_updated_rows"] == 1
        set_stop = await _checkpoint_states(engine)
        assert len(set_stop) == 2
        assert [row["status"] for row in set_stop] == ["completed", "running"]

        resumed = await _run_cli(
            "backfill_hevy_child_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert resumed["status"] == "completed"
        assert resumed["completed"] is True
        assert resumed["batch_table"] == "hevy_sets"
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
        assert await _run_sync(engine, _initial_non_ownership_hashes) == before_hashes

        graph = await _ownership_graph(engine)
        for row in graph["hevy_exercises"]:
            assert row["subject_id"] == identity.subject_id
            assert row["subject_id"] == row["workout_subject_id"]
            assert row["integration_connection_id"] == row["workout_connection_id"]
        for row in graph["hevy_sets"]:
            assert row["subject_id"] == identity.subject_id
            assert row["subject_id"] == row["exercise_subject_id"]
            assert row["integration_connection_id"] == row["exercise_connection_id"]

        idempotent = await _run_cli(
            "backfill_hevy_child_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "3"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert idempotent["status"] == "completed"
        assert idempotent["batch_scanned_rows"] == 0
        assert idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoints

        await _strict_rebuild(engine, identity.subject_id)
        volatile_status = await _run_cli(
            "backfill_hevy_child_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert volatile_status["status"] == "completed"
        assert await _checkpoint_states(engine) == completed_checkpoints

        volatile_idempotent = await _run_cli(
            "backfill_hevy_child_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "2"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert volatile_idempotent["status"] == "completed"
        assert volatile_idempotent["batch_scanned_rows"] == 0
        assert volatile_idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoints

        with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_REFUSAL)):
            await asyncio.to_thread(command.downgrade, alembic_config, "0044")
        # The refusal rolls the whole downgrade back, so the schema is left at
        # head with every later revision's objects still installed.
        assert await _alembic_version(engine) == PRE_OWNERSHIP_CONTRACT_REVISION
        assert await _checkpoint_states(engine) == completed_checkpoints
    finally:
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION)
        await engine.dispose()
