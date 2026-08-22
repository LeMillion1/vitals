"""Production-shaped Stage-3O rehearsal from a synthetic revision-0034 lake."""

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

from tests.conftest import alembic_head_revision

import vitals.models  # noqa: F401 -- register the complete schema for teardown
from vitals.models.base import Base
from vitals.services import (
    body_scan_service,
    conflict_catalog,
    data_portability_service,
    hrt_catalog,
)
from vitals.services.conflict_rule_ownership_backfill_service import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.hevy_child_ownership_backfill_service import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.hrt_child_ownership_backfill_service import (
    HRT_CHILD_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.hrt_compound_ownership_backfill_service import (
    HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.identity_bootstrap import bootstrap_legacy_owner
from vitals.services.normalized_ownership_backfill_service import (
    NORMALIZED_MANUAL_BACKFILL_PHASE,
)
from vitals.services.body_scan_ownership_backfill_service import (
    BODY_SCAN_OWNERSHIP_BACKFILL_PHASE,
    body_scan_historical_processed_bound,
)
from vitals.services.day_context_ownership_backfill_service import (
    DAY_CONTEXT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.genetic_variant_ownership_backfill_service import (
    GENETIC_VARIANT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.lab_result_ownership_backfill_service import (
    LAB_RESULT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.progress_photo_ownership_backfill_service import (
    PROGRESS_PHOTO_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.shared_report_ownership_backfill_service import (
    SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.signal_ownership_backfill_service import (
    SIGNAL_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.weight_log_ownership_backfill_service import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.provider_raw_ownership_backfill_service import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.raw_ownership_backfill_service import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
RAW_IDS = (56_050, 56_051)
SCAN_IDS = (56_101, 56_102)
SCAN_KEYS = (
    "body/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
    "body/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.pdf",
)
SCAN_NOTE = "synthetic-private-stage3o-sheet"
LIVE_SCAN_ID = 56_201
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
    tables = _reflect(connection, ("raw_payloads", "body_scans"))
    created_at = datetime(2026, 8, 20, 8, 30, 15)
    updated_at = datetime(2026, 8, 20, 9, 45, 30)
    connection.execute(
        tables["raw_payloads"].insert(),
        [
            {
                "id": raw_id,
                "domain": "body_comp",
                "source": "body_scan",
                "external_id": file_key,
                "fetched_at": created_at,
                "payload": {"metrics": [], "fixture": "linked"},
                "processed_at": updated_at,
            }
            for raw_id, file_key in zip(RAW_IDS, SCAN_KEYS, strict=True)
        ],
    )
    connection.execute(
        tables["body_scans"].insert(),
        [
            {
                "id": SCAN_IDS[0],
                "date": date(2026, 8, 20),
                "domain": "body_comp",
                "source": "body_scan",
                "device": "InBody 770",
                "file_key": SCAN_KEYS[0],
                "raw_payload_id": RAW_IDS[0],
                "note": f"{SCAN_NOTE}-0",
                "created_at": created_at,
                "updated_at": updated_at,
            },
            {
                "id": SCAN_IDS[1],
                "date": date(2026, 8, 21),
                "domain": "body_comp",
                "source": "body_scan",
                "device": "MedAss",
                "file_key": SCAN_KEYS[1],
                "raw_payload_id": RAW_IDS[1],
                "note": f"{SCAN_NOTE}-1",
                "created_at": created_at,
                "updated_at": updated_at,
            },
        ],
    )
    for table_name, value in (
        ("raw_payloads", max(RAW_IDS)),
        ("body_scans", max(SCAN_IDS)),
    ):
        connection.execute(
            sa.text(
                f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                ":value, true)"
            ),
            {"value": value},
        )


def _scan_nonownership_hash(connection: sa.Connection) -> str:
    scans = _reflect(connection, ("body_scans",))["body_scans"]
    columns = [
        scans.c.id,
        scans.c.date,
        scans.c.domain,
        scans.c.source,
        scans.c.device,
        scans.c.file_key,
        scans.c.raw_payload_id,
        scans.c.note,
        scans.c.created_at,
        scans.c.updated_at,
    ]
    rows = [
        dict(row)
        for row in connection.execute(
            sa.select(*columns)
            .where(scans.c.id.in_(SCAN_IDS))
            .order_by(scans.c.id)
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
            username="synthetic-stage3o-owner",
            password_hash=PASSWORD_HASH,
            timezone="Asia/Almaty",
        )
        await bootstrap_legacy_resource_roots(
            session,
            subject_id=identity.subject_id,
        )
        await session.commit()
        return identity


async def _sync_catalogs(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await hrt_catalog.sync_catalog(session)
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
    assert database_url not in process.stdout
    assert SCAN_NOTE not in process.stdout
    assert not any(file_key in process.stdout for file_key in SCAN_KEYS)
    return payload


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
    assert SCAN_NOTE not in process.stdout
    assert not any(file_key in process.stdout for file_key in SCAN_KEYS)
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
                "phase": BODY_SCAN_OWNERSHIP_BACKFILL_PHASE,
                "prefix": f"{BODY_SCAN_OWNERSHIP_BACKFILL_PHASE}.%",
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


async def _scan_graph_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT p.id, p.subject_id, p.actor_user_id, p.file_asset_id, "
                "p.file_key, f.subject_id AS asset_subject_id, "
                "f.uploaded_by_user_id, f.purpose, f.storage_backend, "
                "f.storage_ref, f.status "
                "FROM body_scans AS p "
                "LEFT JOIN file_assets AS f ON f.id=p.file_asset_id "
                "ORDER BY p.id"
            )
        )
        return [dict(row) for row in rows.mappings()]


async def _live_scan_asset_count(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM file_assets "
                    "WHERE purpose='body_scan_document' "
                    "AND status IN ('legacy_placeholder', 'pending', 'active')"
                )
            )
        )


async def _add_live_scan(engine: AsyncEngine, identity) -> None:
    """Insert the strict live shape a manual scan takes above the watermark."""

    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO body_scans "
                "(id, subject_id, actor_user_id, file_asset_id, date, domain, "
                "source, device, file_key, raw_payload_id, note) "
                "VALUES (:id, :subject, :actor, NULL, :date, 'body_comp', "
                "'manual', 'Manual entry', NULL, NULL, :note)"
            ),
            {
                "id": LIVE_SCAN_ID,
                "subject": identity.subject_id,
                "actor": identity.user_id,
                "date": date(2026, 8, 23),
                "note": f"{SCAN_NOTE}-live",
            },
        )


async def _delete_live_scan(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            sa.text("DELETE FROM body_scans WHERE id=:id"),
            {"id": LIVE_SCAN_ID},
        )


async def _round_trip_portability_v1(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        snapshot = await data_portability_service.export_full(session)
        await data_portability_service.import_full(session, snapshot)
        await session.commit()


async def _replace_with_empty_portability_v1(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await data_portability_service.import_full(
            session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
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
async def test_real_postgres_0034_body_scan_stop_resume_and_restore(
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
        business_hash_before = await _run_sync(engine, _scan_nonownership_hash)

        await asyncio.to_thread(command.upgrade, alembic_config, "head")
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
                "backfill_day_context_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                DAY_CONTEXT_OWNERSHIP_BACKFILL_PHASE,
                AGGREGATE_CLI_KEYS,
            ),
            (
                "backfill_signal_subject_ownership.py",
                ["--apply", "--batch-size", "1000", "--max-batches", "10"],
                SIGNAL_OWNERSHIP_BACKFILL_PHASE,
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
            "backfill_body_scan_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert status["phase"] == BODY_SCAN_OWNERSHIP_BACKFILL_PHASE
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["snapshot_rows"] == 2
        assert status["remaining_rows"] == 2
        assert await _checkpoint_states(engine) == []

        started = await _run_cli(
            "backfill_body_scan_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert started["status"] == "running"
        assert started["batch_table"] == "body_scans"
        assert started["batch_scanned_rows"] == 1
        assert started["batch_updated_rows"] == 1
        assert await _run_sync(engine, _scan_nonownership_hash) == business_hash_before

        stopped_checkpoint = await _checkpoint_states(engine)
        assert len(stopped_checkpoint) == 1
        assert stopped_checkpoint[0]["status"] == "running"
        graph = await _scan_graph_rows(engine)
        first, second = graph
        assert first["id"] == SCAN_IDS[0]
        assert first["subject_id"] == identity.subject_id
        assert first["actor_user_id"] is None
        assert first["file_asset_id"] is not None
        assert first["asset_subject_id"] == identity.subject_id
        assert first["uploaded_by_user_id"] is None
        assert first["purpose"] == "body_scan_document"
        assert first["storage_backend"] == "legacy_local"
        assert first["storage_ref"] == first["file_key"] == SCAN_KEYS[0]
        assert first["status"] == "legacy_placeholder"
        assert (
            second["subject_id"],
            second["actor_user_id"],
            second["file_asset_id"],
        ) == (None, None, None)

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            assert await body_scan_historical_processed_bound(
                session,
                subject_id=identity.subject_id,
            ) == SCAN_IDS[0]
            # The backfill is halfway: body_comp is closed, so the reader
            # shows exactly the rows that already carry the subject and none of
            # the ones still waiting for the next batch.
            visible = await body_scan_service.list_scans(
                session,
                subject_id=identity.subject_id,
            )
            assert {row.id for row in visible} == {SCAN_IDS[0]}
            processed = next(row for row in visible if row.id == SCAN_IDS[0])
            assert processed.actor_user_id is None

        stopped = await _run_cli(
            "backfill_body_scan_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert stopped["status"] == "running"
        assert stopped["scanned_rows"] == 1
        assert await _checkpoint_states(engine) == stopped_checkpoint

        resumed = await _run_cli(
            "backfill_body_scan_subject_ownership.py",
            ["--apply", "--batch-size", "1000", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert resumed["status"] == "completed"
        assert resumed["completed"] is True
        assert resumed["snapshot_rows"] == 2
        assert resumed["scanned_rows"] == 2
        assert resumed["updated_rows"] == 2
        assert resumed["remaining_rows"] == 0
        assert await _run_sync(engine, _scan_nonownership_hash) == business_hash_before

        graph = await _scan_graph_rows(engine)
        assert {row["id"] for row in graph} == set(SCAN_IDS)
        assert all(row["subject_id"] == identity.subject_id for row in graph)
        assert all(row["actor_user_id"] is None for row in graph)
        assert all(row["file_asset_id"] is not None for row in graph)
        assert all(row["asset_subject_id"] == identity.subject_id for row in graph)
        assert all(row["uploaded_by_user_id"] is None for row in graph)
        assert all(row["storage_ref"] == row["file_key"] for row in graph)
        assert len({row["file_asset_id"] for row in graph}) == 2

        completed_checkpoint = await _checkpoint_states(engine)
        idempotent = await _run_cli(
            "backfill_body_scan_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "3"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert idempotent["status"] == "completed"
        assert idempotent["batch_scanned_rows"] == 0
        assert idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoint

        await _add_live_scan(engine, identity)
        volatile_add = await _run_cli(
            "backfill_body_scan_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert volatile_add["status"] == "completed"
        assert volatile_add["rows_above_high_watermark"] == 1
        assert await _checkpoint_states(engine) == completed_checkpoint
        await _delete_live_scan(engine)
        volatile_delete = await _run_cli(
            "backfill_body_scan_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert volatile_delete["status"] == "completed"
        assert volatile_delete["rows_above_high_watermark"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoint

        await _round_trip_portability_v1(engine)
        restored_graph = await _scan_graph_rows(engine)
        assert len(restored_graph) == 2
        assert all(row["subject_id"] == identity.subject_id for row in restored_graph)
        assert all(row["actor_user_id"] is None for row in restored_graph)
        assert all(row["file_asset_id"] is None for row in restored_graph)
        assert await _live_scan_asset_count(engine) == 0
        restore_status = await _run_cli(
            "backfill_body_scan_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restore_status["status"] == "restore_blocked"
        assert restore_status["completed"] is False
        assert restore_status["remaining_rows"] == 2
        assert await _phase_statuses(
            engine,
            BODY_SCAN_OWNERSHIP_BACKFILL_PHASE,
        ) == ("restore_blocked",)
        apply_error = await _run_cli_error(
            "backfill_body_scan_subject_ownership.py",
            ["--apply", "--batch-size", "1"],
            database_url=database_url,
            expected_code="state_error",
        )
        assert apply_error["mode"] == "apply"
        assert await _live_scan_asset_count(engine) == 0

        await _replace_with_empty_portability_v1(engine)
        empty_status = await _run_cli(
            "backfill_body_scan_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert empty_status["status"] == "completed"
        assert empty_status["snapshot_rows"] == 0
        assert empty_status["remaining_rows"] == 0
        assert await _scan_graph_rows(engine) == []
        assert await _live_scan_asset_count(engine) == 0

        final_checkpoint = await _checkpoint_states(engine)
        with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_REFUSAL)):
            await asyncio.to_thread(command.downgrade, alembic_config, "0044")
        # The refusal rolls the whole downgrade back, so the schema is left at
        # head with every later revision's objects still installed.
        assert await _alembic_version(engine) == alembic_head_revision()
        assert await _checkpoint_states(engine) == final_checkpoint
    finally:
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, "head")
        await engine.dispose()
