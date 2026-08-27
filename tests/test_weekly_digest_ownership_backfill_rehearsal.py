"""Production-shaped Stage-3R rehearsal from a synthetic revision-0034 lake."""

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
from vitals.operations.ownership.progress_photo import (
    PROGRESS_PHOTO_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.provider_raw import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.raw import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.shared_report import (
    SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots
from vitals.operations.ownership.weight_log import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.body_scan_metric import (
    BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.garmin_weight_export import (
    GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.weekly_digest import (
    WEEKLY_DIGEST_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.body_scan import (
    BODY_SCAN_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.genetic_variant import (
    GENETIC_VARIANT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.operations.ownership.lab_result import (
    LAB_RESULT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.ownership import PRE_OWNERSHIP_CONTRACT_REVISION


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

DIGEST_IDS = (59_201, 59_202)
DIGEST_DATES = (date(2026, 8, 19), date(2026, 8, 20))
LIVE_DATE = date(2026, 8, 21)
LIVE_DIGEST_ID = 59_301
PRIVATE_SENTINEL = "synthetic-private-stage3r-digest-narrative"
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
    tables = _reflect(connection, ("weekly_digests",))
    created_at = datetime(2026, 8, 19, 8, 30, 15)
    updated_at = datetime(2026, 8, 19, 9, 45, 30)
    connection.execute(
        tables["weekly_digests"].insert(),
        [
            {
                "id": digest_id,
                "date": on_date,
                "domain": "milestones",
                "source": "scheduler",
                "content": f"{PRIVATE_SENTINEL}-{index}",
                "context_json": {"weight": []},
                "model": "synthetic/digest",
                "created_at": created_at,
                "updated_at": updated_at,
            }
            for index, (digest_id, on_date) in enumerate(
                zip(DIGEST_IDS, DIGEST_DATES, strict=True)
            )
        ],
    )
    connection.execute(
        sa.text(
            "SELECT setval(pg_get_serial_sequence('weekly_digests', 'id'), "
            ":value, true)"
        ),
        {"value": max(DIGEST_IDS)},
    )


def _digest_nonownership_hash(connection: sa.Connection) -> str:
    table = _reflect(connection, ("weekly_digests",))["weekly_digests"]
    rows = [
        dict(row)
        for row in connection.execute(
            sa.select(
                table.c.id,
                table.c.date,
                table.c.domain,
                table.c.source,
                table.c.content,
                table.c.model,
                table.c.created_at,
                table.c.updated_at,
            )
            .where(table.c.id.in_(DIGEST_IDS))
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
            username="synthetic-stage3r-owner",
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
    assert not any(day.isoformat() in rendered for day in DIGEST_DATES)
    assert not any(str(value) in rendered for value in DIGEST_IDS)
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
                "phase": WEEKLY_DIGEST_OWNERSHIP_BACKFILL_PHASE,
                "prefix": f"{WEEKLY_DIGEST_OWNERSHIP_BACKFILL_PHASE}.%",
            },
        )
        return [dict(row) for row in rows.mappings()]


async def _digest_graph(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT id, subject_id, actor_user_id, integration_connection_id, "
                "ai_invocation_id, date, domain, source, kind, content, model, "
                "created_at, updated_at FROM weekly_digests ORDER BY id"
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
                "weekly_digests": [],
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
async def test_real_postgres_0034_weekly_digest_stop_resume_volatility_and_restore(
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
        business_hash_before = await _run_sync(engine, _digest_nonownership_hash)
        identity = await _bootstrap_roots(engine)
        await _sync_catalogs(engine)

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
            (
                "backfill_conflict_rule_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),
            (
                "backfill_progress_photo_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                PROGRESS_PHOTO_OWNERSHIP_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),

            (
                "backfill_shared_report_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),
            (
                "backfill_weight_log_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                WEIGHT_LOG_OWNERSHIP_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),
            (
                "backfill_lab_result_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                LAB_RESULT_OWNERSHIP_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),
            (
                "backfill_genetic_variant_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                GENETIC_VARIANT_OWNERSHIP_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),
            (
                "backfill_body_scan_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                BODY_SCAN_OWNERSHIP_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),
            (
                "backfill_body_scan_metric_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),
            (
                "backfill_garmin_weight_export_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_PHASE,
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

        status = await _run_cli(
            "backfill_weekly_digest_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert status["phase"] == WEEKLY_DIGEST_OWNERSHIP_BACKFILL_PHASE
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["snapshot_rows"] == 2
        assert status["remaining_rows"] == 2
        assert await _checkpoint_states(engine) == []

        started = await _run_cli(
            "backfill_weekly_digest_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert started["status"] == "running"
        assert started["batch_table"] == "weekly_digests"
        assert started["batch_scanned_rows"] == 1
        assert started["batch_updated_rows"] == 1
        assert await _run_sync(engine, _digest_nonownership_hash) == business_hash_before

        stopped_checkpoint = await _checkpoint_states(engine)
        assert len(stopped_checkpoint) == 1
        assert stopped_checkpoint[0]["status"] == "running"
        graph = await _digest_graph(engine)
        assert graph[0]["subject_id"] == identity.subject_id
        assert graph[1]["subject_id"] is None
        # A child copies only the reviewed parent subject.

        stopped = await _run_cli(
            "backfill_weekly_digest_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert stopped["status"] == "running"
        assert stopped["scanned_rows"] == 1
        assert await _checkpoint_states(engine) == stopped_checkpoint

        resumed = await _run_cli(
            "backfill_weekly_digest_subject_ownership.py",
            ["--apply", "--batch-size", "1000", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert resumed["status"] == "completed"
        assert resumed["snapshot_rows"] == 2
        assert resumed["scanned_rows"] == 2
        assert resumed["updated_rows"] == 2
        assert resumed["remaining_rows"] == 0
        assert await _run_sync(engine, _digest_nonownership_hash) == business_hash_before

        graph = await _digest_graph(engine)
        assert {row["id"] for row in graph} == set(DIGEST_IDS)
        assert all(row["subject_id"] == identity.subject_id for row in graph)

        assert all(
            row["integration_connection_id"] is None
            and row["actor_user_id"] is None
            and row["ai_invocation_id"] is None
            for row in await _digest_graph(engine)
        )

        completed_checkpoint = await _checkpoint_states(engine)
        idempotent = await _run_cli(
            "backfill_weekly_digest_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "3"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert idempotent["status"] == "completed"
        assert idempotent["batch_scanned_rows"] == 0
        assert idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoint

        # Supported post-completion volatility: a strict live artifact funded
        # through the retained subject gateway, above the frozen watermark.
        async with engine.connect() as connection:
            gateway_id = await connection.scalar(
                sa.text(
                    "SELECT id FROM integration_connections "
                    "WHERE subject_id=:subject AND provider='openrouter' "
                    "AND connection_type='ai_gateway'"
                ),
                {"subject": identity.subject_id},
            )
        assert gateway_id is not None
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO weekly_digests "
                    "(id, subject_id, integration_connection_id, date, domain, "
                    "source, kind, content, model) "
                    "VALUES (:id, :subject, :gateway, :date, 'milestones', "
                    "'scheduler', 'weekly', :content, 'synthetic/digest')"
                ),
                {
                    "id": LIVE_DIGEST_ID,
                    "subject": identity.subject_id,
                    "gateway": gateway_id,
                    "date": LIVE_DATE,
                    "content": f"{PRIVATE_SENTINEL}-live",
                },
            )

        volatile = await _run_cli(
            "backfill_weekly_digest_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert volatile["status"] == "completed"
        assert volatile["rows_above_high_watermark"] == 1
        assert await _checkpoint_states(engine) == completed_checkpoint

        # Backup v1 neither exports nor replaces digests: the retained
        # artifacts and their completed checkpoint survive a round trip intact.
        await _round_trip_portability_v1(engine)
        restored_graph = await _digest_graph(engine)
        assert len(restored_graph) == 3
        assert all(row["subject_id"] == identity.subject_id for row in restored_graph)
        assert await _run_sync(engine, _digest_nonownership_hash) == (
            business_hash_before
        )
        assert await _checkpoint_states(engine) == completed_checkpoint
        restore_status = await _run_cli(
            "backfill_weekly_digest_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restore_status["status"] == "completed"
        assert restore_status["snapshot_rows"] == 2
        assert await _phase_statuses(
            engine, WEEKLY_DIGEST_OWNERSHIP_BACKFILL_PHASE
        ) == ("completed",)

        await _replace_with_empty_portability_v1(engine)
        empty_status = await _run_cli(
            "backfill_weekly_digest_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert empty_status["status"] == "completed"
        assert empty_status["snapshot_rows"] == 2
        assert len(await _digest_graph(engine)) == 3

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
