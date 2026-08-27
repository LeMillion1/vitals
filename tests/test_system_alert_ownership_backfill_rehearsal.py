"""Production-shaped Stage-3T rehearsal from a synthetic revision-0034 lake."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
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
from vitals.ownership_deploy import OWNERSHIP_BACKFILL_SEQUENCE
from vitals.operations.ownership import portability_v1
from vitals.services import data_portability_service
from vitals.services.conflicts import catalog as conflict_catalog
from vitals.services.hrt import catalog
from vitals.services.identity_bootstrap import bootstrap_legacy_owner
from vitals.operations.ownership.normalized import (
    NORMALIZED_MANUAL_BACKFILL_PHASE,
)
from vitals.operations.ownership.raw import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots
from vitals.operations.ownership.system_alert import (
    SYSTEM_ALERT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.ownership import PRE_OWNERSHIP_CONTRACT_REVISION


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

ALERT_IDS = (61_201, 61_202, 61_203)
ALERT_DATES = (date(2026, 8, 19), date(2026, 8, 20))
LIVE_DATE = date(2026, 8, 21)
LIVE_ALERT_ID = 61_301
PRIVATE_SENTINEL = "synthetic-private-stage3t-alert-message"
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
    tables = _reflect(connection, ("system_alerts",))
    created_at = datetime(2026, 8, 19, 8, 30, 15)
    connection.execute(
        tables["system_alerts"].insert(),
        [
            {
                "id": alert_id,
                "created_at": created_at,
                "domain": domain,
                "severity": "warn",
                "message": f"{PRIVATE_SENTINEL}-{index}",
                "alert_key": alert_key,
                "entity_ref": f"entity:{index}",
            }
            for index, (alert_id, alert_key, domain) in enumerate(
                zip(
                    ALERT_IDS,
                    (
                        "weight.noisy_period_active",
                        "garmin.auth",
                        "scheduler.job_failed:raw_payload_sweep",
                    ),
                    ("weight", "garmin", "system"),
                    strict=True,
                )
            )
        ],
    )
    connection.execute(
        sa.text(
            "SELECT setval(pg_get_serial_sequence('system_alerts', 'id'), "
            ":value, true)"
        ),
        {"value": max(ALERT_IDS)},
    )


def _alert_nonownership_hash(connection: sa.Connection) -> str:
    table = _reflect(connection, ("system_alerts",))["system_alerts"]
    rows = [
        dict(row)
        for row in connection.execute(
            sa.select(
                table.c.id,
                table.c.created_at,
                table.c.domain,
                table.c.severity,
                table.c.message,
                table.c.alert_key,
                table.c.entity_ref,
                table.c.override_at,
                table.c.resolved_at,
            )
            .where(table.c.id.in_(ALERT_IDS))
            .order_by(table.c.id)
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
            username="synthetic-stage3t-owner",
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
    assert not any(day.isoformat() in rendered for day in ALERT_DATES)
    assert not any(str(value) in rendered for value in ALERT_IDS)
    return payload


ERROR_CLI_KEYS = {
    "error_code",
    "format_version",
    "mode",
    "operation",
    "phase",
    "result",
}


async def _run_cli_error(
    script: str,
    arguments: list[str],
    *,
    database_url: str,
    expected_code: str,
) -> dict[str, Any]:
    process = await _run_cli_process(script, arguments, database_url=database_url)
    assert process.returncode == 1, (process.stdout, process.stderr)
    assert process.stderr == ""
    assert process.stdout.count("\n") == 1
    payload = json.loads(process.stdout)
    assert set(payload) == ERROR_CLI_KEYS
    assert payload["result"] == "error"
    assert payload["error_code"] == expected_code
    assert database_url not in process.stdout
    assert PRIVATE_SENTINEL not in process.stdout
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
                "phase": SYSTEM_ALERT_OWNERSHIP_BACKFILL_PHASE,
                "prefix": f"{SYSTEM_ALERT_OWNERSHIP_BACKFILL_PHASE}.%",
            },
        )
        return [dict(row) for row in rows.mappings()]


async def _alert_graph(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT id, subject_id, integration_connection_id, ai_invocation_id, "
                "domain, severity, alert_key, entity_ref, override_at, resolved_at "
                "FROM system_alerts ORDER BY id"
            )
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
        snapshot = await data_portability_service.export_full(session)
        await portability_v1.import_full(session, snapshot)
        await session.commit()


async def _replace_with_empty_portability_v1(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await portability_v1.import_full(
            session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                "system_alerts": [],
            },
        )
        await session.commit()


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_0034_system_alert_stop_resume_volatility_and_restore(
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
        business_hash_before = await _run_sync(engine, _alert_nonownership_hash)
        identity = await _bootstrap_roots(engine)
        await _sync_catalogs(engine)

        # Every phase before this one, in the order an operator runs them.
        # The order itself lives in ``vitals.ownership_deploy`` so the runbook,
        # the deploy rehearsal and this test cannot drift apart.
        prior_commands = tuple(
            (
                step.script,
                (
                    ["--apply", "--batch-size", "1000"]
                    if step.phase == RAW_OWNERSHIP_BACKFILL_PHASE
                    else ["--apply", "--batch-size", "1000", "--max-batches", "100"]
                    if step.phase == NORMALIZED_MANUAL_BACKFILL_PHASE
                    else ["--apply", "--batch-size", "1000", "--max-batches", "10"]
                ),
                step.phase,
                (
                    RAW_CLI_KEYS
                    if step.phase == RAW_OWNERSHIP_BACKFILL_PHASE
                    else AGGREGATE_CLI_KEYS
                ),
            )
            for step in OWNERSHIP_BACKFILL_SEQUENCE
            if step.phase != SYSTEM_ALERT_OWNERSHIP_BACKFILL_PHASE
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

        status = await _run_cli(
            "backfill_system_alert_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert status["phase"] == SYSTEM_ALERT_OWNERSHIP_BACKFILL_PHASE
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["snapshot_rows"] == 3
        assert status["remaining_rows"] == 3
        assert await _checkpoint_states(engine) == []

        started = await _run_cli(
            "backfill_system_alert_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert started["status"] == "running"
        assert started["batch_table"] == "system_alerts"
        assert started["batch_scanned_rows"] == 1
        assert started["batch_updated_rows"] == 1
        assert await _run_sync(engine, _alert_nonownership_hash) == business_hash_before

        stopped_checkpoint = await _checkpoint_states(engine)
        assert len(stopped_checkpoint) == 1
        assert stopped_checkpoint[0]["status"] == "running"
        graph = await _alert_graph(engine)
        assert graph[0]["subject_id"] == identity.subject_id
        assert graph[1]["subject_id"] is None

        stopped = await _run_cli(
            "backfill_system_alert_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert stopped["status"] == "running"
        assert stopped["scanned_rows"] == 1
        assert await _checkpoint_states(engine) == stopped_checkpoint

        resumed = await _run_cli(
            "backfill_system_alert_subject_ownership.py",
            ["--apply", "--batch-size", "1000", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert resumed["status"] == "completed"
        assert resumed["snapshot_rows"] == 3
        assert resumed["scanned_rows"] == 3
        # The installation-wide alert is scanned but never adopted.
        assert resumed["updated_rows"] == 2
        assert resumed["remaining_rows"] == 0
        assert await _run_sync(engine, _alert_nonownership_hash) == business_hash_before

        graph = await _alert_graph(engine)
        assert {row["id"] for row in graph} == set(ALERT_IDS)

        graph_by_key = {row["alert_key"]: row for row in await _alert_graph(engine)}
        assert (
            graph_by_key["weight.noisy_period_active"]["subject_id"]
            == identity.subject_id
        )
        assert (
            graph_by_key["weight.noisy_period_active"]["integration_connection_id"]
            is None
        )
        assert graph_by_key["garmin.auth"]["subject_id"] == identity.subject_id
        assert (
            graph_by_key["garmin.auth"]["integration_connection_id"] is not None
        )
        # An installation-wide alert owns neither root.
        assert (
            graph_by_key["scheduler.job_failed:raw_payload_sweep"]["subject_id"]
            is None
        )

        completed_checkpoint = await _checkpoint_states(engine)
        idempotent = await _run_cli(
            "backfill_system_alert_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "3"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert idempotent["status"] == "completed"
        assert idempotent["batch_scanned_rows"] == 0
        assert idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoint

        # Supported post-completion volatility: a strict live installation-wide
        # alert above the frozen watermark, which owns neither root.
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO system_alerts "
                    "(id, created_at, domain, severity, message, alert_key, "
                    "entity_ref) VALUES (:id, :created_at, 'system', 'warn', "
                    ":message, 'scheduler.job_failed:share_purge', 'job:live')"
                ),
                {
                    "id": LIVE_ALERT_ID,
                    "created_at": datetime(2026, 8, 21, 9, 0),
                    "message": f"{PRIVATE_SENTINEL}-live",
                },
            )

        volatile = await _run_cli(
            "backfill_system_alert_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert volatile["status"] == "completed"
        assert volatile["rows_above_high_watermark"] == 1
        assert await _checkpoint_states(engine) == completed_checkpoint

        # Backup v1 carries the alerts but strips S/C, so the phase is reset and
        # each reviewed class is adopted again from its own key.
        await _round_trip_portability_v1(engine)
        restored_graph = await _alert_graph(engine)
        assert len(restored_graph) == 4
        restore_status = await _run_cli(
            "backfill_system_alert_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restore_status["status"] == "running"
        assert restore_status["snapshot_rows"] == 4
        assert restore_status["scanned_rows"] == 0
        assert await _phase_statuses(
            engine, SYSTEM_ALERT_OWNERSHIP_BACKFILL_PHASE
        ) == ("running",)

        restored = await _run_cli(
            "backfill_system_alert_subject_ownership.py",
            ["--apply", "--batch-size", "2", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restored["status"] == "completed"
        assert restored["snapshot_rows"] == 4
        restored_by_key = {
            row["alert_key"]: row for row in await _alert_graph(engine)
        }
        assert (
            restored_by_key["garmin.auth"]["integration_connection_id"] is not None
        )
        assert (
            restored_by_key["scheduler.job_failed:raw_payload_sweep"]["subject_id"]
            is None
        )
        assert await _run_sync(engine, _alert_nonownership_hash) == (
            business_hash_before
        )

        await _replace_with_empty_portability_v1(engine)
        empty_status = await _run_cli(
            "backfill_system_alert_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert empty_status["status"] == "completed"
        assert empty_status["snapshot_rows"] == 0
        assert await _alert_graph(engine) == []

        final_checkpoint = await _checkpoint_states(engine)
        with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_REFUSAL)):
            await asyncio.to_thread(command.downgrade, alembic_config, "0044")
        # The refusal rolls the whole downgrade back, so the schema is left at
        # head with every later revision's objects still installed.
        assert await _alembic_version(engine) == PRE_OWNERSHIP_CONTRACT_REVISION
        assert await _checkpoint_states(engine) == final_checkpoint
    finally:
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION)
        await engine.dispose()
