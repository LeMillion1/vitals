"""Production-shaped Stage-3K rehearsal from a synthetic revision-0034 lake."""

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
from vitals.services import (
    conflict_catalog,
    data_portability_service,
    hrt_catalog,
    share_service,
)
from vitals.services.conflict_rule_ownership_backfill_service import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.day_context_ownership_backfill_service import (
    DAY_CONTEXT_OWNERSHIP_BACKFILL_PHASE,
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
from vitals.services.progress_photo_ownership_backfill_service import (
    PROGRESS_PHOTO_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.provider_raw_ownership_backfill_service import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.raw_ownership_backfill_service import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.shared_report_ownership_backfill_service import (
    SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE,
    shared_report_historical_bridge_state,
)
from vitals.services.signal_ownership_backfill_service import (
    SIGNAL_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
REPORT_IDS = (51_201, 51_202, 51_203)
TOKENS = (
    "synthetic-private-stage3k-live-token",
    "synthetic-private-stage3k-revoked-token",
    "synthetic-private-stage3k-expired-token",
)
PRIVATE_SENTINEL = "synthetic-private-stage3k-report-snapshot-title-note"
OWNER_USERNAME = "synthetic-stage3k-owner"
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
    reports = _reflect(connection, ("shared_reports",))["shared_reports"]
    created_at = datetime(2026, 8, 19, 8, 30, 15)
    updated_at = datetime(2026, 8, 19, 9, 45, 30)
    common = {
        "password_hash": PASSWORD_HASH,
        "preset": "full",
        "domains": ["labs"],
        "period_start": date(2026, 7, 1),
        "period_end": date(2026, 8, 18),
        "labs_flagged_only": False,
        "note": PRIVATE_SENTINEL,
        "opened_count": 0,
        "last_opened_at": None,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    connection.execute(
        reports.insert(),
        [
            {
                **common,
                "id": REPORT_IDS[0],
                "token": TOKENS[0],
                "title": f"{PRIVATE_SENTINEL}-live",
                "snapshot": {"private": PRIVATE_SENTINEL, "state": "live"},
                "expires_at": datetime(2030, 8, 30, 12, 0),
                "revoked_at": None,
            },
            {
                **common,
                "id": REPORT_IDS[1],
                "token": TOKENS[1],
                "title": f"{PRIVATE_SENTINEL}-revoked",
                "snapshot": None,
                "expires_at": datetime(2030, 8, 30, 12, 0),
                "revoked_at": datetime(2026, 8, 20, 12, 0),
            },
            {
                **common,
                "id": REPORT_IDS[2],
                "token": TOKENS[2],
                "title": f"{PRIVATE_SENTINEL}-expired",
                "snapshot": {"private": PRIVATE_SENTINEL, "state": "expired"},
                "expires_at": datetime(2026, 8, 20, 12, 0),
                "revoked_at": None,
            },
        ],
    )
    connection.execute(
        sa.text(
            "SELECT setval(pg_get_serial_sequence('shared_reports', 'id'), "
            ":value, true)"
        ),
        {"value": max(REPORT_IDS)},
    )


def _report_projection(connection: sa.Connection) -> list[dict[str, Any]]:
    table = _reflect(connection, ("shared_reports",))["shared_reports"]
    return [
        dict(row)
        for row in connection.execute(
            sa.select(*table.c).order_by(table.c.id)
        ).mappings()
    ]


def _historical_business_hash(connection: sa.Connection) -> str:
    rows = _report_projection(connection)
    return _digest(
        [
            {
                key: value
                for key, value in row.items()
                if key
                not in {
                    "subject_id",
                    "created_by_user_id",
                    "revoked_by_user_id",
                }
            }
            for row in rows
            if row["id"] in REPORT_IDS
        ]
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
            username=OWNER_USERNAME,
            password_hash=PASSWORD_HASH,
            timezone="Asia/Almaty",
        )
        await bootstrap_legacy_resource_roots(session, subject_id=identity.subject_id)
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
    rendered = process.stdout + process.stderr
    assert database_url not in rendered
    assert PRIVATE_SENTINEL not in rendered
    assert not any(token in rendered for token in TOKENS)
    assert not any(str(report_id) in rendered for report_id in REPORT_IDS)
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
                "phase": SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE,
                "prefix": f"{SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE}.%",
            },
        )
        return [dict(row) for row in rows.mappings()]


async def _report_graph(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text("SELECT * FROM shared_reports ORDER BY id")
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
        assert "shared_reports" not in snapshot
        await data_portability_service.import_full(session, snapshot)
        await session.commit()


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_0034_shared_report_stop_resume_volatility_and_restore(
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
        business_hash_before = await _run_sync(engine, _historical_business_hash)

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
            "backfill_shared_report_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert status["phase"] == SHARED_REPORT_OWNERSHIP_BACKFILL_PHASE
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["snapshot_rows"] == 3
        assert status["remaining_rows"] == 3
        assert await _checkpoint_states(engine) == []

        started = await _run_cli(
            "backfill_shared_report_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert started["status"] == "running"
        assert started["batch_table"] == "shared_reports"
        assert started["batch_scanned_rows"] == 1
        assert started["batch_updated_rows"] == 1
        assert await _run_sync(engine, _historical_business_hash) == business_hash_before

        stopped_checkpoint = await _checkpoint_states(engine)
        assert len(stopped_checkpoint) == 1
        assert stopped_checkpoint[0]["status"] == "running"
        graph = await _report_graph(engine)
        assert graph[0]["subject_id"] == identity.subject_id
        assert all(graph[index]["subject_id"] is None for index in (1, 2))
        assert all(row["created_by_user_id"] is None for row in graph)
        assert all(row["revoked_by_user_id"] is None for row in graph)

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            bridge = await shared_report_historical_bridge_state(
                session, subject_id=identity.subject_id
            )
            assert bridge.processed_high_watermark_id == REPORT_IDS[0]
            assert bridge.snapshot_high_watermark_id == REPORT_IDS[-1]
            assert bridge.completed is False
            prepared = await share_service.prepare_legacy_owner(
                session, actor_username=OWNER_USERNAME
            )
            visible = await share_service.list_reports(
                session, prepared_owner=prepared
            )
            assert {row.id for row in visible} == set(REPORT_IDS)
            await session.rollback()

        stopped = await _run_cli(
            "backfill_shared_report_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert stopped["status"] == "running"
        assert stopped["scanned_rows"] == 1
        assert await _checkpoint_states(engine) == stopped_checkpoint

        resumed = await _run_cli(
            "backfill_shared_report_subject_ownership.py",
            ["--apply", "--batch-size", "1000", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert resumed["status"] == "completed"
        assert resumed["snapshot_rows"] == 3
        assert resumed["scanned_rows"] == 3
        assert resumed["updated_rows"] == 3
        assert resumed["remaining_rows"] == 0
        assert await _run_sync(engine, _historical_business_hash) == business_hash_before

        graph = await _report_graph(engine)
        assert {row["id"] for row in graph} == set(REPORT_IDS)
        assert all(row["subject_id"] == identity.subject_id for row in graph)
        assert all(row["created_by_user_id"] is None for row in graph)
        assert all(row["revoked_by_user_id"] is None for row in graph)
        async with factory() as session:
            prepared = await share_service.prepare_legacy_owner(
                session, actor_username=OWNER_USERNAME
            )
            strict = await share_service.list_reports(
                session, prepared_owner=prepared
            )
            assert {row.id for row in strict} == set(REPORT_IDS)
            public = await share_service.resolve_public(session, TOKENS[0])
            assert public is not None and public.id == REPORT_IDS[0]
            await session.rollback()

        completed_checkpoint = await _checkpoint_states(engine)
        idempotent = await _run_cli(
            "backfill_shared_report_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "3"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert idempotent["status"] == "completed"
        assert idempotent["batch_scanned_rows"] == 0
        assert idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoint

        # Supported post-completion lifecycle is volatile: anonymous open,
        # owner revoke/delete, scheduled purge, and a strict new report.
        async with factory() as session:
            opened = await share_service.register_open(session, TOKENS[0])
            assert opened is not None and opened.opened_count == 1
            await session.commit()
        async with factory() as session:
            prepared = await share_service.prepare_legacy_owner(
                session, actor_username=OWNER_USERNAME
            )
            assert await share_service.revoke(
                session, REPORT_IDS[0], prepared_owner=prepared
            )
            await session.commit()
        async with factory() as session:
            assert await share_service.purge_expired(
                session, now=datetime(2026, 8, 21, 12, 0)
            ) == 1
            await session.commit()
        async with factory() as session:
            prepared = await share_service.prepare_legacy_owner(
                session, actor_username=OWNER_USERNAME
            )
            assert await share_service.delete_report(
                session, REPORT_IDS[1], prepared_owner=prepared
            )
            await session.commit()
        async with factory() as session:
            prepared = await share_service.prepare_legacy_owner(
                session, actor_username=OWNER_USERNAME
            )
            live, _password = await share_service.create_report(
                session,
                title=f"{PRIVATE_SENTINEL}-strict",
                domains=[],
                period_start=date(2026, 8, 19),
                period_end=date(2026, 8, 20),
                expires_days=30,
                note=PRIVATE_SENTINEL,
                prepared_owner=prepared,
            )
            assert live.id > REPORT_IDS[-1]
            assert live.subject_id == identity.subject_id
            assert live.created_by_user_id == identity.user_id
            assert live.revoked_by_user_id is None
            live_id = live.id
            await session.commit()

        volatile = await _run_cli(
            "backfill_shared_report_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert volatile["status"] == "completed"
        assert volatile["rows_above_high_watermark"] == 1
        assert await _checkpoint_states(engine) == completed_checkpoint
        graph = await _report_graph(engine)
        assert {row["id"] for row in graph} == {
            REPORT_IDS[0],
            REPORT_IDS[2],
            live_id,
        }
        revoked = next(row for row in graph if row["id"] == REPORT_IDS[0])
        assert revoked["created_by_user_id"] is None
        assert revoked["revoked_by_user_id"] == identity.user_id
        assert revoked["snapshot"] is None
        assert revoked["opened_count"] == 1
        expired = next(row for row in graph if row["id"] == REPORT_IDS[2])
        assert expired["snapshot"] is None

        retained_hash_before = _digest(graph)
        await _round_trip_portability_v1(engine)
        assert _digest(await _report_graph(engine)) == retained_hash_before
        assert await _checkpoint_states(engine) == completed_checkpoint
        assert await _phase_statuses(engine, RAW_OWNERSHIP_BACKFILL_PHASE) == (
            "completed",
        )
        expected_restore_states = {
            NORMALIZED_MANUAL_BACKFILL_PHASE: {"completed", "running"},
            HRT_CHILD_OWNERSHIP_BACKFILL_PHASE: {"completed"},
            PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE: {"completed"},
            HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE: {"completed"},
            HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE: {"running"},
            CONFLICT_RULE_OWNERSHIP_BACKFILL_PHASE: {"running"},
            PROGRESS_PHOTO_OWNERSHIP_BACKFILL_PHASE: {"completed"},
            DAY_CONTEXT_OWNERSHIP_BACKFILL_PHASE: {"completed"},
            SIGNAL_OWNERSHIP_BACKFILL_PHASE: {"completed"},
        }
        for phase, expected in expected_restore_states.items():
            statuses = await _phase_statuses(engine, phase)
            assert statuses
            assert set(statuses) == expected

        restore_status = await _run_cli(
            "backfill_shared_report_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restore_status["status"] == "completed"
        assert restore_status["snapshot_rows"] == 3
        restored = await _run_cli(
            "backfill_shared_report_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "2"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restored["status"] == "completed"
        assert restored["batch_scanned_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoint

        final_checkpoint = await _checkpoint_states(engine)
        with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_REFUSAL)):
            await asyncio.to_thread(command.downgrade, alembic_config, "0044")
        # The refusal rolls the whole downgrade back, so head is still 0046 and
        # the Stage-4 subject-equality references stay installed.
        assert await _alembic_version(engine) == "0046"
        assert await _checkpoint_states(engine) == final_checkpoint
    finally:
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, "head")
        await engine.dispose()
