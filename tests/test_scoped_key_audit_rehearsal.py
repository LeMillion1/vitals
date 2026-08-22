"""Production-shaped Stage-5A rehearsal from a synthetic revision-0034 lake.

The rehearsal runs the complete Stage-3 CLI chain and the Stage-4 whole-lake
gate against real PostgreSQL, and then proves the scoped-key cutover audit: that
it refuses to look at a lake Stage 4 has not proved, that a clean lake records
reviewed evidence, that the evidence is idempotent, that a provider row with no
connection is refused because the scoped key would keep no uniqueness for it,
and that the audit creates and drops nothing.
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
from vitals.scoped_keys import SCOPED_KEYS
from vitals.services.ownership_validation_service import (
    OWNERSHIP_VALIDATION_PHASE,
)
from vitals.services.scoped_key_audit_service import SCOPED_KEY_AUDIT_PHASE
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots
from vitals.ownership import PRE_OWNERSHIP_CONTRACT_REVISION


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

RAW_ID = 84_101
WORKOUT_ID = 84_201
EXERCISE_IDS = (84_301, 84_302)
SET_IDS = (84_401, 84_402)
PRIVATE_SENTINEL = "synthetic-private-stage5-workout-title"
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
AUDIT_CLI_KEYS = {
    "audit_checksum",
    "collisions_total",
    "completed",
    "format_version",
    "legacy_keys_total",
    "mode",
    "operation",
    "phase",
    "result",
    "rows_inspected",
    "scoped_indexes_total",
    "status",
    "unscoped_rows_total",
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
            username="synthetic-stage5-owner",
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


async def _checkpoints(engine: AsyncEngine, phase: str) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT * FROM ownership_backfill_checkpoints "
                "WHERE phase_key=:phase"
            ),
            {"phase": phase},
        )
        return [dict(row) for row in rows.mappings()]


async def _index_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        rows = await connection.scalars(
            sa.text("SELECT indexname FROM pg_indexes WHERE schemaname='public'")
        )
        return set(rows)


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
async def test_real_postgres_stage5a_scoped_key_audit_proves_the_cutover(
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
        subject_id = (await _bootstrap_roots(engine)).subject_id

        # By head the scoped keys are installed and the legacy global keys they
        # replaced are gone; the audit changes neither.
        installed = await _index_names(engine)
        replacements = {
            index.name for spec in SCOPED_KEYS for index in spec.replacements
        }
        legacy = {spec.legacy_name for spec in SCOPED_KEYS}
        assert replacements <= installed
        assert not (legacy & installed)

        # Before Stage 4 the audit refuses to look at the lake at all.
        blocked = await _run_cli(
            "audit_scoped_keys.py",
            [],
            database_url=database_url,
            expected_keys=ERROR_CLI_KEYS,
            expected_returncode=1,
        )
        assert blocked["error_code"] == "dependency_error"
        assert blocked["phase"] == SCOPED_KEY_AUDIT_PHASE
        assert await _checkpoints(engine, SCOPED_KEY_AUDIT_PHASE) == []

        for script, arguments, expected_keys in STAGE3_COMMANDS:
            result = await _run_cli(
                script,
                ["--apply", *arguments],
                database_url=database_url,
                expected_keys=expected_keys,
            )
            assert result["status"] == "completed", script

        # Stage 3 alone is not enough either: Stage 4 has to have proved it.
        still_blocked = await _run_cli(
            "audit_scoped_keys.py",
            [],
            database_url=database_url,
            expected_keys=ERROR_CLI_KEYS,
            expected_returncode=1,
        )
        assert still_blocked["error_code"] == "dependency_error"

        validated = await _run_cli(
            "validate_subject_ownership.py",
            ["--apply"],
            database_url=database_url,
            expected_keys=VALIDATION_CLI_KEYS,
        )
        assert validated["status"] == "completed"

        status = await _run_cli(
            "audit_scoped_keys.py",
            [],
            database_url=database_url,
            expected_keys=AUDIT_CLI_KEYS,
        )
        assert status["phase"] == SCOPED_KEY_AUDIT_PHASE
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["collisions_total"] == 0
        assert status["unscoped_rows_total"] == 0
        assert status["legacy_keys_total"] == len(SCOPED_KEYS)
        assert status["scoped_indexes_total"] == sum(
            len(spec.replacements) for spec in SCOPED_KEYS
        )
        assert await _checkpoints(engine, SCOPED_KEY_AUDIT_PHASE) == []
        assert await _index_names(engine) == installed

        applied = await _run_cli(
            "audit_scoped_keys.py",
            ["--apply"],
            database_url=database_url,
            expected_keys=AUDIT_CLI_KEYS,
        )
        assert applied["mode"] == "apply"
        assert applied["status"] == "completed"
        assert applied["audit_checksum"] == status["audit_checksum"]
        # An audit proves; it never installs or drops.
        assert await _index_names(engine) == installed

        recorded = await _checkpoints(engine, SCOPED_KEY_AUDIT_PHASE)
        assert len(recorded) == 1
        assert recorded[0]["status"] == "completed"
        assert recorded[0]["subject_id"] == subject_id
        assert recorded[0]["updated_rows"] == 0
        assert recorded[0]["ownership_checksum_after"] == status["audit_checksum"]

        repeated = await _run_cli(
            "audit_scoped_keys.py",
            ["--apply"],
            database_url=database_url,
            expected_keys=AUDIT_CLI_KEYS,
        )
        assert repeated["audit_checksum"] == applied["audit_checksum"]
        assert len(await _checkpoints(engine, SCOPED_KEY_AUDIT_PHASE)) == 1

        # A provider row with no connection passes Stage 4 — ownership never
        # leaves the reviewed roots — but under (C, external_id) it would keep
        # no uniqueness at all, so the audit has to refuse it.
        await _unenforced(
            engine,
            "UPDATE hevy_workouts SET integration_connection_id = NULL "
            "WHERE id = :id",
            {"id": WORKOUT_ID},
        )
        refused = await _run_cli(
            "audit_scoped_keys.py",
            [],
            database_url=database_url,
            expected_keys=ERROR_CLI_KEYS,
            expected_returncode=2,
        )
        assert refused["error_code"] == "collision"
        assert (await _checkpoints(engine, SCOPED_KEY_AUDIT_PHASE))[0][
            "ownership_checksum_after"
        ] == status["audit_checksum"]

        final_checkpoints = await _checkpoints(engine, SCOPED_KEY_AUDIT_PHASE)
        with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_REFUSAL)):
            await asyncio.to_thread(command.downgrade, alembic_config, "0044")
        assert await _alembic_version(engine) == PRE_OWNERSHIP_CONTRACT_REVISION
        assert await _checkpoints(engine, SCOPED_KEY_AUDIT_PHASE) == final_checkpoints
    finally:
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION)
        await engine.dispose()
