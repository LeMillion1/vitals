"""Production-shaped Stage-4 rehearsal from a synthetic revision-0034 lake.

The rehearsal runs the complete Stage-3 CLI chain against real PostgreSQL and
then proves the whole-lake gate: that revision 0046 installs the subject
equality references ``NOT VALID``, that recording the reviewed evidence makes
them valid, that the evidence is idempotent, and that a subject boundary broken
behind the constraints is refused without recording anything.
"""

from __future__ import annotations

import asyncio
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
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import vitals.models  # noqa: F401 -- register the complete schema for teardown
from vitals.models.base import Base
from vitals.services import conflict_catalog, hrt_catalog
from vitals.services.identity_bootstrap import bootstrap_legacy_owner
from vitals.services.ownership_validation_service import (
    OWNERSHIP_VALIDATION_PHASE,
    STAGE3_PHASES,
    SUBJECT_EQUALITY_CONSTRAINTS,
)
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

RAW_ID = 84_101
WORKOUT_ID = 84_201
EXERCISE_IDS = (84_301, 84_302)
SET_IDS = (84_401, 84_402)
PRIVATE_SENTINEL = "synthetic-private-stage4-workout-title"
DOWNGRADE_REFUSAL = (
    "0045 downgrade refused: ownership backfill checkpoints contain durable state"
)
PASSWORD_HASH = (
    "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
)

VALIDATION_CLI_KEYS = {
    "checks_total",
    "completed",
    "format_version",
    "graph_checksum",
    "mode",
    "operation",
    "phase",
    "result",
    "rows_inspected",
    "status",
    "tables_total",
    "validated_constraints",
    "violations_total",
}
ERROR_CLI_KEYS = {
    "error_code",
    "format_version",
    "mode",
    "operation",
    "phase",
    "result",
}
BACKFILL_CLI_KEYS = {
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
RAW_CLI_KEYS = BACKFILL_CLI_KEYS - {
    "batch_table",
    "completed_tables",
    "snapshot_rows",
    "tables_total",
}

# The full reviewed Stage-3 chain, in the only order its dependencies allow.
STAGE3_COMMANDS: tuple[tuple[str, list[str], set[str]], ...] = (
    ("backfill_subject_ownership.py", ["--batch-size", "1000"], RAW_CLI_KEYS),
    *(
        (script, ["--batch-size", "1000", "--max-batches", "100"], BACKFILL_CLI_KEYS)
        for script in (
            "backfill_normalized_subject_ownership.py",
            "backfill_hrt_child_subject_ownership.py",
            "backfill_provider_raw_subject_ownership.py",
            "backfill_hevy_child_subject_ownership.py",
            "backfill_hrt_compound_subject_ownership.py",
            "backfill_conflict_rule_subject_ownership.py",
            "backfill_progress_photo_subject_ownership.py",
            "backfill_day_context_subject_ownership.py",
            "backfill_signal_subject_ownership.py",
            "backfill_shared_report_subject_ownership.py",
            "backfill_weight_log_subject_ownership.py",
            "backfill_lab_result_subject_ownership.py",
            "backfill_genetic_variant_subject_ownership.py",
            "backfill_body_scan_subject_ownership.py",
            "backfill_body_scan_metric_subject_ownership.py",
            "backfill_garmin_weight_export_subject_ownership.py",
            "backfill_weekly_digest_subject_ownership.py",
            "backfill_notification_subject_ownership.py",
            "backfill_system_alert_subject_ownership.py",
        )
    ),
)


def _reflect(connection: sa.Connection, names: tuple[str, ...]) -> dict[str, sa.Table]:
    metadata = sa.MetaData()
    return {
        name: sa.Table(name, metadata, autoload_with=connection) for name in names
    }


def _seed_revision_0034(connection: sa.Connection) -> None:
    tables = _reflect(
        connection,
        ("raw_payloads", "hevy_workouts", "hevy_exercises", "hevy_sets"),
    )
    common = {
        "created_at": datetime(2026, 8, 20, 8, 30, 15),
        "updated_at": datetime(2026, 8, 20, 9, 45, 30),
    }
    payload = {
        "id": "synthetic-stage4-workout",
        "title": PRIVATE_SENTINEL,
        "exercises": [
            {"index": 0, "title": "Synthetic press", "sets": []},
            {"index": 1, "title": "Synthetic row", "sets": []},
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
            title=PRIVATE_SENTINEL,
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
                "id": exercise_id,
                "workout_id": WORKOUT_ID,
                "exercise_index": index,
                "title": f"Synthetic exercise {index}",
                "exercise_template_id": f"synthetic-{index}",
                **common,
            }
            for index, exercise_id in enumerate(EXERCISE_IDS)
        ],
    )
    connection.execute(
        tables["hevy_sets"].insert(),
        [
            {
                "id": set_id,
                "exercise_id": EXERCISE_IDS[index],
                "set_index": 0,
                "set_type": "normal",
                "weight_kg": 80.0,
                "reps": 8,
                **common,
            }
            for index, set_id in enumerate(SET_IDS)
        ],
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
            username="synthetic-stage4-owner",
            password_hash=PASSWORD_HASH,
            timezone="Asia/Almaty",
        )
        await bootstrap_legacy_resource_roots(session, subject_id=identity.subject_id)
        await hrt_catalog.sync_catalog(session)
        await conflict_catalog.sync_catalog(session)
        await session.commit()
        return identity


async def _run_cli_process(
    script: str, arguments: list[str], *, database_url: str
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
        timeout=180,
        check=False,
    )


async def _run_cli(
    script: str,
    arguments: list[str],
    *,
    database_url: str,
    expected_keys: set[str],
    expected_returncode: int = 0,
) -> dict[str, Any]:
    process = await _run_cli_process(script, arguments, database_url=database_url)
    assert process.returncode == expected_returncode, (process.stdout, process.stderr)
    assert process.stderr == ""
    assert process.stdout.count("\n") == 1
    payload = json.loads(process.stdout)
    assert set(payload) == expected_keys
    rendered = process.stdout + process.stderr
    assert database_url not in rendered
    assert PRIVATE_SENTINEL not in rendered
    assert not any(str(value) in rendered for value in (*EXERCISE_IDS, *SET_IDS))
    return payload


async def _constraint_states(engine: AsyncEngine) -> dict[str, bool]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT conname, convalidated FROM pg_constraint "
                "WHERE contype='f' AND conname = ANY(:names)"
            ),
            {"names": list(SUBJECT_EQUALITY_CONSTRAINTS.values())},
        )
        return {row.conname: bool(row.convalidated) for row in rows}


async def _validation_checkpoints(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT * FROM ownership_backfill_checkpoints "
                "WHERE phase_key=:phase"
            ),
            {"phase": OWNERSHIP_VALIDATION_PHASE},
        )
        return [dict(row) for row in rows.mappings()]


async def _stage3_statuses(engine: AsyncEngine) -> dict[str, str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT phase_key, status FROM ownership_backfill_checkpoints "
                "WHERE phase_key = ANY(:phases)"
            ),
            {"phases": list(STAGE3_PHASES)},
        )
        return {row.phase_key: row.status for row in rows}


async def _unenforced(engine: AsyncEngine, statement: str, parameters: dict) -> None:
    """Break or repair a boundary behind the validated constraints."""

    async with engine.begin() as connection:
        await connection.execute(sa.text("SET session_replication_role = replica"))
        await connection.execute(sa.text(statement), parameters)
        await connection.execute(sa.text("SET session_replication_role = origin"))


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_stage4_whole_lake_validation_and_constraint_promotion(
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
        await asyncio.to_thread(command.upgrade, alembic_config, "head")

        # Revision 0046 installs the subject-equality references unvalidated, so
        # the migration never scans a lake whose ownership is not proved yet.
        installed = await _constraint_states(engine)
        assert set(installed) == set(SUBJECT_EQUALITY_CONSTRAINTS.values())
        assert not any(installed.values())

        subject_id = (await _bootstrap_roots(engine)).subject_id

        # Before Stage 3 finishes the gate refuses to look at the lake at all.
        blocked = await _run_cli(
            "validate_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=ERROR_CLI_KEYS,
            expected_returncode=1,
        )
        assert blocked["error_code"] == "dependency_error"
        assert await _validation_checkpoints(engine) == []

        for script, arguments, expected_keys in STAGE3_COMMANDS:
            result = await _run_cli(
                script,
                ["--apply", *arguments],
                database_url=database_url,
                expected_keys=expected_keys,
            )
            assert result["status"] == "completed", script
        statuses = await _stage3_statuses(engine)
        assert set(statuses) == set(STAGE3_PHASES)
        assert set(statuses.values()) == {"completed"}

        # The read-only gate proves the lake without recording or promoting.
        status = await _run_cli(
            "validate_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=VALIDATION_CLI_KEYS,
        )
        assert status["phase"] == OWNERSHIP_VALIDATION_PHASE
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["completed"] is False
        assert status["violations_total"] == 0
        assert status["validated_constraints"] == 0
        assert status["tables_total"] > 0
        assert status["checks_total"] >= status["tables_total"]
        assert await _validation_checkpoints(engine) == []
        assert not any((await _constraint_states(engine)).values())

        applied = await _run_cli(
            "validate_subject_ownership.py",
            ["--apply"],
            database_url=database_url,
            expected_keys=VALIDATION_CLI_KEYS,
        )
        assert applied["mode"] == "apply"
        assert applied["status"] == "completed"
        assert applied["completed"] is True
        assert applied["violations_total"] == 0
        assert applied["validated_constraints"] == len(SUBJECT_EQUALITY_CONSTRAINTS)
        assert applied["tables_total"] == status["tables_total"]
        assert applied["graph_checksum"] == status["graph_checksum"]

        promoted = await _constraint_states(engine)
        assert set(promoted) == set(SUBJECT_EQUALITY_CONSTRAINTS.values())
        assert all(promoted.values())

        recorded = await _validation_checkpoints(engine)
        assert len(recorded) == 1
        assert recorded[0]["status"] == "completed"
        assert recorded[0]["subject_id"] == subject_id
        assert recorded[0]["updated_rows"] == 0
        assert recorded[0]["ownership_checksum_after"] == status["graph_checksum"]

        # Recording the same lake twice is a no-op beyond its own timestamps.
        repeated = await _run_cli(
            "validate_subject_ownership.py",
            ["--apply"],
            database_url=database_url,
            expected_keys=VALIDATION_CLI_KEYS,
        )
        assert repeated["status"] == "completed"
        assert repeated["graph_checksum"] == applied["graph_checksum"]
        assert all((await _constraint_states(engine)).values())
        assert len(await _validation_checkpoints(engine)) == 1

        # A validated constraint now refuses to move a parent out from under a
        # child on the ordinary write path.
        with pytest.raises(Exception, match="fk_hevy_sets_exercise_subject"):
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text(
                        "UPDATE hevy_exercises SET subject_id = NULL "
                        "WHERE id = :id"
                    ),
                    {"id": EXERCISE_IDS[0]},
                )

        # A boundary broken behind the constraints is still refused by the gate,
        # and a refused run records nothing.
        await _unenforced(
            engine,
            "UPDATE hevy_exercises SET subject_id = NULL WHERE id = :id",
            {"id": EXERCISE_IDS[0]},
        )
        violated = await _run_cli(
            "validate_subject_ownership.py",
            ["--apply"],
            database_url=database_url,
            expected_keys=ERROR_CLI_KEYS,
            expected_returncode=2,
        )
        assert violated["error_code"] == "violation"
        assert (await _validation_checkpoints(engine))[0][
            "ownership_checksum_after"
        ] == status["graph_checksum"]

        await _unenforced(
            engine,
            "UPDATE hevy_exercises SET subject_id = :subject WHERE id = :id",
            {"id": EXERCISE_IDS[0], "subject": subject_id},
        )
        repaired = await _run_cli(
            "validate_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=VALIDATION_CLI_KEYS,
        )
        assert repaired["status"] == "completed"
        assert repaired["graph_checksum"] == status["graph_checksum"]

        final_checkpoints = await _validation_checkpoints(engine)
        with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_REFUSAL)):
            await asyncio.to_thread(command.downgrade, alembic_config, "0044")
        # The refusal rolls the whole downgrade back, so the Stage-4 references
        # stay installed and valid alongside the evidence they were proved with.
        assert await _alembic_version(engine) == "0046"
        assert all((await _constraint_states(engine)).values())
        assert await _validation_checkpoints(engine) == final_checkpoints
    finally:
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, "head")
        await engine.dispose()
