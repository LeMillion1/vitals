"""Production-shaped Stage-3D rehearsal from a synthetic revision-0034 lake."""

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
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.operations.ownership.hrt_child import (
    HRT_CHILD_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.identity_bootstrap import bootstrap_legacy_owner
from vitals.operations.ownership.normalized import (
    NORMALIZED_MANUAL_BACKFILL_PHASE,
)
from vitals.operations.ownership.provider_raw import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE,
    ProviderRawOwnershipBackfillDependencyError,
    preflight_provider_raw_ownership_backfill,
)
from vitals.operations.ownership.raw import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
    block_raw_ownership_backfill_for_portability_v1_restore,
)
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots
from vitals.ownership import PRE_OWNERSHIP_CONTRACT_REVISION


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
TABLES = (
    "garmin_daily",
    "garmin_activities",
    "garmin_intraday",
    "hevy_workouts",
)
HEVY_CHILD_TABLES = ("hevy_exercises", "hevy_sets")
ROW_IDS = {
    "garmin_daily": (32_001, 32_002),
    "garmin_activities": (32_101, 32_102),
    "garmin_intraday": (32_201, 32_202),
    "hevy_workouts": (32_301, 32_302),
    "hevy_exercises": (32_401,),
    "hevy_sets": (32_501,),
}
RAW_IDS = {
    "garmin_api_daily": 31_001,
    "garmin_hae_daily": 31_002,
    "garmin_activity_a": 31_101,
    "garmin_activity_b": 31_102,
    "hevy_a": 31_201,
    "hevy_b": 31_202,
}
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
ERROR_CLI_KEYS = {
    "error_code",
    "format_version",
    "mode",
    "operation",
    "phase",
    "result",
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
    tables = _reflect(connection, ("raw_payloads", *TABLES, *HEVY_CHILD_TABLES))
    created_at = datetime(2026, 8, 20, 8, 30, 15)
    updated_at = datetime(2026, 8, 20, 9, 45, 30)
    common = {"created_at": created_at, "updated_at": updated_at}
    fetched_at = datetime(2026, 8, 20, 7, 0, 0)
    raw_rows = [
        {
            "id": RAW_IDS["garmin_api_daily"],
            "domain": "garmin",
            "source": "garmin_api",
            "external_id": "daily:2026-08-18",
            "fetched_at": fetched_at,
            "payload": {"calendarDate": "2026-08-18", "synthetic": "daily-api"},
        },
        {
            "id": RAW_IDS["garmin_hae_daily"],
            "domain": "garmin",
            "source": "health_auto_export",
            "external_id": "hae:2026-08-19",
            "fetched_at": fetched_at,
            "payload": {"date": "2026-08-19", "synthetic": "daily-hae"},
        },
        {
            "id": RAW_IDS["garmin_activity_a"],
            "domain": "garmin",
            "source": "garmin_api",
            "external_id": "activity:activity-a",
            "fetched_at": fetched_at,
            "payload": {"activityId": "activity-a", "synthetic": True},
        },
        {
            "id": RAW_IDS["garmin_activity_b"],
            "domain": "garmin",
            "source": "garmin_api",
            "external_id": "activity:activity-b",
            "fetched_at": fetched_at,
            "payload": {"activityid": "activity-b", "synthetic": True},
        },
        {
            "id": RAW_IDS["hevy_a"],
            "domain": "workouts",
            "source": "hevy_api",
            "external_id": "hevy-a",
            "fetched_at": fetched_at,
            "payload": {"id": "hevy-a", "synthetic": True},
        },
        {
            "id": RAW_IDS["hevy_b"],
            "domain": "workouts",
            "source": "hevy_api",
            "external_id": "hevy-b",
            "fetched_at": fetched_at,
            "payload": {"id": "hevy-b", "synthetic": True},
        },
    ]
    connection.execute(tables["raw_payloads"].insert(), raw_rows)
    connection.execute(
        tables["garmin_daily"].insert(),
        [
            {
                "id": ROW_IDS["garmin_daily"][0],
                "date": date(2026, 8, 18),
                "domain": "garmin",
                "source": "garmin_api",
                "raw_payload_id": RAW_IDS["garmin_api_daily"],
                "sleep_score": 82,
                "steps": 8_500,
                **common,
            },
            {
                "id": ROW_IDS["garmin_daily"][1],
                "date": date(2026, 8, 19),
                "domain": "garmin",
                "source": "health_auto_export",
                "raw_payload_id": RAW_IDS["garmin_hae_daily"],
                "sleep_score": 79,
                "steps": 9_250,
                **common,
            },
        ],
    )
    connection.execute(
        tables["garmin_activities"].insert(),
        [
            {
                "id": ROW_IDS["garmin_activities"][0],
                "date": date(2026, 8, 18),
                "domain": "garmin",
                "source": "garmin_api",
                "external_id": "activity-a",
                "raw_payload_id": RAW_IDS["garmin_activity_a"],
                "activity_type": "strength_training",
                "name": "Synthetic activity A",
                **common,
            },
            {
                "id": ROW_IDS["garmin_activities"][1],
                "date": date(2026, 8, 19),
                "domain": "garmin",
                "source": "garmin_api",
                "external_id": "activity-b",
                "raw_payload_id": RAW_IDS["garmin_activity_b"],
                "activity_type": "running",
                "name": "Synthetic activity B",
                **common,
            },
        ],
    )
    connection.execute(
        tables["garmin_intraday"].insert(),
        [
            {
                "id": ROW_IDS["garmin_intraday"][0],
                "date": date(2026, 8, 18),
                "domain": "garmin",
                "source": "garmin_api",
                "raw_payload_id": RAW_IDS["garmin_api_daily"],
                "series_type": "stress",
                "ts": datetime(2026, 8, 18, 10, 0),
                "value": 21.0,
                **common,
            },
            {
                "id": ROW_IDS["garmin_intraday"][1],
                "date": date(2026, 8, 18),
                "domain": "garmin",
                "source": "garmin_api",
                "raw_payload_id": RAW_IDS["garmin_api_daily"],
                "series_type": "body_battery",
                "ts": datetime(2026, 8, 18, 10, 3),
                "value": 68.0,
                **common,
            },
        ],
    )
    connection.execute(
        tables["hevy_workouts"].insert(),
        [
            {
                "id": ROW_IDS["hevy_workouts"][0],
                "date": date(2026, 8, 18),
                "domain": "workouts",
                "source": "hevy_api",
                "external_id": "hevy-a",
                "raw_payload_id": RAW_IDS["hevy_a"],
                "title": "Synthetic workout A",
                **common,
            },
            {
                "id": ROW_IDS["hevy_workouts"][1],
                "date": date(2026, 8, 20),
                "domain": "workouts",
                "source": "hevy_api",
                "external_id": "hevy-b",
                "raw_payload_id": RAW_IDS["hevy_b"],
                "title": "Synthetic workout B",
                **common,
            },
        ],
    )
    connection.execute(
        tables["hevy_exercises"].insert().values(
            id=ROW_IDS["hevy_exercises"][0],
            workout_id=ROW_IDS["hevy_workouts"][0],
            exercise_index=0,
            title="Synthetic exercise",
            exercise_template_id="synthetic-template",
            **common,
        )
    )
    connection.execute(
        tables["hevy_sets"].insert().values(
            id=ROW_IDS["hevy_sets"][0],
            exercise_id=ROW_IDS["hevy_exercises"][0],
            set_index=0,
            set_type="normal",
            weight_kg=80.0,
            reps=8,
            **common,
        )
    )


def _non_ownership_hashes(connection: sa.Connection) -> dict[str, str]:
    names = (*TABLES, *HEVY_CHILD_TABLES)
    tables = _reflect(connection, names)
    result: dict[str, str] = {}
    for name in names:
        table = tables[name]
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
            username="synthetic-stage3d-owner",
            password_hash=PASSWORD_HASH,
            timezone="Asia/Almaty",
        )
        await bootstrap_legacy_resource_roots(
            session,
            subject_id=identity.subject_id,
        )
        await session.commit()
        return identity


async def _set_hevy_child_transition(engine: AsyncEngine, subject_id: Any) -> None:
    """Reproduce a set that is owned while its exercise still is not.

    The Stage-4 subject-equality references forbid this shape going forward, so
    the mid-transition history the dependency gate has to refuse is written with
    the constraints stood down.
    """

    async with engine.begin() as connection:
        await connection.execute(sa.text("SET session_replication_role = replica"))
        await connection.execute(
            sa.text(
                "UPDATE hevy_sets SET subject_id = :subject_id, "
                "integration_connection_id = NULL WHERE id = :row_id"
            ),
            {"subject_id": subject_id, "row_id": ROW_IDS["hevy_sets"][0]},
        )
        await connection.execute(sa.text("SET session_replication_role = origin"))


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
    assert database_url not in process.stdout
    return payload


async def _run_cli_error(
    script: str,
    arguments: list[str],
    *,
    database_url: str,
    error_code: str,
) -> dict[str, Any]:
    process = await _run_cli_process(script, arguments, database_url=database_url)
    assert process.returncode == 1
    assert process.stderr == ""
    assert process.stdout.count("\n") == 1
    payload = json.loads(process.stdout)
    assert set(payload) == ERROR_CLI_KEYS
    assert payload["result"] == "error"
    assert payload["error_code"] == error_code
    assert database_url not in process.stdout
    return payload


async def _checkpoint_states(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT * FROM ownership_backfill_checkpoints "
                "WHERE phase_key LIKE :prefix ORDER BY phase_key"
            ),
            {"prefix": f"{PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE}.%"},
        )
        return [dict(row) for row in rows.mappings()]


async def _block_raw_dependency(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        high_watermark = int(
            await session.scalar(sa.text("SELECT max(id) FROM raw_payloads")) or 0
        )
        snapshot_rows = int(
            await session.scalar(sa.text("SELECT count(*) FROM raw_payloads")) or 0
        )
        blocked = await block_raw_ownership_backfill_for_portability_v1_restore(
            session,
            scan_high_watermark_id=high_watermark,
            snapshot_rows=snapshot_rows,
        )
        assert blocked.status.value == "restore_blocked"
        await session.commit()


async def _delete_raw_dependency(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            sa.delete(OwnershipBackfillCheckpoint.__table__)
            .where(
                OwnershipBackfillCheckpoint.phase_key
                == RAW_OWNERSHIP_BACKFILL_PHASE
            )
        )


async def _ownership_graph(engine: AsyncEngine) -> dict[str, list[dict[str, Any]]]:
    state: dict[str, list[dict[str, Any]]] = {}
    async with engine.connect() as connection:
        for name in TABLES:
            actor_projection = (
                "NULL::uuid AS actor_user_id"
                if name == "garmin_intraday"
                else "n.actor_user_id"
            )
            rows = await connection.execute(
                sa.text(
                    f"SELECT n.id, n.subject_id, {actor_projection}, "
                    f"n.integration_connection_id, n.raw_payload_id, "
                    f"r.subject_id AS raw_subject_id, "
                    f"r.integration_connection_id AS raw_connection_id "
                    f"FROM {name} n JOIN raw_payloads r "
                    f"ON r.id = n.raw_payload_id "
                    f"WHERE n.id = ANY(:row_ids) ORDER BY n.id"
                ),
                {"row_ids": list(ROW_IDS[name])},
            )
            state[name] = [dict(row) for row in rows.mappings()]
        child_rows = await connection.execute(
            sa.text(
                "SELECT e.subject_id AS exercise_subject_id, "
                "e.integration_connection_id AS exercise_connection_id, "
                "s.subject_id AS set_subject_id, "
                "s.integration_connection_id AS set_connection_id "
                "FROM hevy_exercises e JOIN hevy_sets s ON s.exercise_id = e.id "
                "WHERE e.id = :exercise_id AND s.id = :set_id"
            ),
            {
                "exercise_id": ROW_IDS["hevy_exercises"][0],
                "set_id": ROW_IDS["hevy_sets"][0],
            },
        )
        state["hevy_children"] = [dict(child_rows.mappings().one())]
    return state


async def _assert_provider_dependency_ready(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        try:
            await preflight_provider_raw_ownership_backfill(session)
        except ProviderRawOwnershipBackfillDependencyError as exc:
            raise AssertionError(f"unexpected Stage-3A dependency gap: {exc}") from exc


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_0034_provider_raw_dependency_stop_resume_and_refusal(
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
        before_hashes = await _run_sync(engine, _non_ownership_hashes)

        await asyncio.to_thread(command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION)
        identity = await _bootstrap_roots(engine)
        await _set_hevy_child_transition(engine, identity.subject_id)

        missing = await _run_cli_error(
            "backfill_provider_raw_subject_ownership.py",
            [],
            database_url=database_url,
            error_code="dependency_error",
        )
        assert missing["phase"] == PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE
        assert await _checkpoint_states(engine) == []

        raw = await _run_cli(
            "backfill_subject_ownership.py",
            ["--apply", "--batch-size", "1000"],
            database_url=database_url,
            expected_keys=RAW_CLI_KEYS,
        )
        assert raw["phase"] == RAW_OWNERSHIP_BACKFILL_PHASE
        assert raw["status"] == "completed"
        assert raw["scanned_rows"] == len(RAW_IDS)

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
        assert hrt_children["completed_tables"] == 2

        await _block_raw_dependency(engine)
        blocked = await _run_cli_error(
            "backfill_provider_raw_subject_ownership.py",
            [],
            database_url=database_url,
            error_code="dependency_error",
        )
        assert blocked["phase"] == PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE
        assert await _checkpoint_states(engine) == []
        await _delete_raw_dependency(engine)
        raw_recovered = await _run_cli(
            "backfill_subject_ownership.py",
            ["--apply", "--batch-size", "1000"],
            database_url=database_url,
            expected_keys=RAW_CLI_KEYS,
        )
        assert raw_recovered["status"] == "completed"
        assert raw_recovered["scanned_rows"] == len(RAW_IDS)
        await _assert_provider_dependency_ready(engine)

        status = await _run_cli(
            "backfill_provider_raw_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert status["phase"] == PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["tables_total"] == 4
        assert status["snapshot_rows"] == 8
        assert status["remaining_rows"] == 8
        assert status["batches_processed"] == 0
        assert await _checkpoint_states(engine) == []

        first = await _run_cli(
            "backfill_provider_raw_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "3"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert first["status"] == "running"
        assert first["batch_table"] == "garmin_activities"
        assert first["batch_scanned_rows"] == 1
        assert first["batch_updated_rows"] == 1
        assert first["scanned_rows"] == 3
        assert first["updated_rows"] == 3
        assert first["completed_tables"] == 1
        first_checkpoints = await _checkpoint_states(engine)
        assert len(first_checkpoints) == 2
        assert [row["status"] for row in first_checkpoints] == [
            "running",
            "completed",
        ]

        stopped = await _run_cli(
            "backfill_provider_raw_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert stopped["status"] == "running"
        assert stopped["scanned_rows"] == 3
        assert stopped["completed_tables"] == 1
        assert await _checkpoint_states(engine) == first_checkpoints

        resumed = await _run_cli(
            "backfill_provider_raw_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "20"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert resumed["status"] == "completed"
        assert resumed["completed"] is True
        assert resumed["batch_table"] == "hevy_workouts"
        assert resumed["tables_total"] == 4
        assert resumed["completed_tables"] == 4
        assert resumed["snapshot_rows"] == 8
        assert resumed["scanned_rows"] == 8
        assert resumed["updated_rows"] == 8
        assert resumed["unchanged_rows"] == 0
        assert resumed["remaining_rows"] == 0

        completed_checkpoints = await _checkpoint_states(engine)
        assert len(completed_checkpoints) == 4
        assert {row["status"] for row in completed_checkpoints} == {"completed"}
        idempotent = await _run_cli(
            "backfill_provider_raw_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "3"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert idempotent["status"] == "completed"
        assert idempotent["batch_scanned_rows"] == 0
        assert idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoints

        graph = await _ownership_graph(engine)
        for name in TABLES:
            for row in graph[name]:
                assert row["subject_id"] == identity.subject_id
                assert row["actor_user_id"] is None
                assert row["integration_connection_id"] is not None
                assert row["subject_id"] == row["raw_subject_id"]
                assert (
                    row["integration_connection_id"]
                    == row["raw_connection_id"]
                )
        child = graph["hevy_children"][0]
        assert child["exercise_subject_id"] is None
        assert child["exercise_connection_id"] is None
        assert child["set_subject_id"] == identity.subject_id
        assert child["set_connection_id"] is None
        assert await _run_sync(engine, _non_ownership_hashes) == before_hashes

        with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_REFUSAL)):
            await asyncio.to_thread(command.downgrade, alembic_config, "0044")
        # The refusal rolls the whole downgrade back, so the schema is left at
        # head with every later revision's objects still installed.
        assert await _alembic_version(engine) == PRE_OWNERSHIP_CONTRACT_REVISION
        assert await _checkpoint_states(engine) == completed_checkpoints
        assert await _run_sync(engine, _non_ownership_hashes) == before_hashes
    finally:
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION)
        await engine.dispose()
