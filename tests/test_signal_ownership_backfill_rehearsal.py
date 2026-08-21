"""Production-shaped Stage-3J rehearsal from a synthetic revision-0034 lake."""

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
from vitals.enums import (
    Domain,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
)
from vitals.models.base import Base
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import (
    conflict_catalog,
    data_portability_service,
    hrt_catalog,
    signals_service,
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
from vitals.services.signal_ownership_backfill_service import (
    SIGNAL_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
RAW_ID = 51_100
LATE_REPARSE_RAW_ID = 51_101
SIGNAL_IDS = (51_201, 51_202)
SIGNAL_DATES = (date(2026, 8, 19), date(2026, 8, 20))
PRIVATE_SENTINEL = "synthetic-private-stage3j-signal-text-key-batch"
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
    tables = _reflect(connection, ("raw_payloads", "signals"))
    created_at = datetime(2026, 8, 19, 8, 30, 15)
    updated_at = datetime(2026, 8, 19, 9, 45, 30)
    connection.execute(
        tables["raw_payloads"].insert(),
        [
            {
                "id": RAW_ID,
                "domain": "signals",
                "source": "telegram",
                "external_id": "synthetic-stage3j-linked-raw",
                "fetched_at": created_at,
                "payload": {"text": PRIVATE_SENTINEL, "fixture": "linked"},
                "processed_at": updated_at,
            },
            {
                # Stage3A adopts this pending historical raw as exact S+C with
                # A null. It is deliberately normalized only after Stage3J.
                "id": LATE_REPARSE_RAW_ID,
                "domain": "signals",
                "source": "telegram",
                "external_id": "synthetic-stage3j-late-reparse-raw",
                "fetched_at": created_at,
                "payload": {"text": PRIVATE_SENTINEL, "fixture": "late"},
                "processed_at": None,
            },
        ],
    )
    connection.execute(
        tables["signals"].insert(),
        [
            {
                "id": SIGNAL_IDS[0],
                "date": SIGNAL_DATES[0],
                "domain": "signals",
                "source": "telegram",
                # The historical bridge preserves the pre-registry ``number``
                # kind; only above-HWM live writes require the current enum.
                "kind": "number",
                "key": "synthetic_legacy_linked",
                "value_num": 1.0,
                "unit": "count",
                "note": PRIVATE_SENTINEL,
                "at_time": time(8, 15),
                "raw_id": RAW_ID,
                "batch_id": "synthetic-stage3j-linked",
                "misparse": False,
                "created_at": created_at,
                "updated_at": updated_at,
            },
            {
                "id": SIGNAL_IDS[1],
                "date": SIGNAL_DATES[1],
                "domain": "signals",
                "source": "telegram",
                "kind": "symptom",
                "key": "synthetic_legacy_rawless",
                "value_num": 2.0,
                "unit": None,
                "note": PRIVATE_SENTINEL,
                "at_time": None,
                "raw_id": None,
                "batch_id": "synthetic-stage3j-rawless",
                "misparse": False,
                "created_at": created_at,
                "updated_at": updated_at,
            },
        ],
    )
    for table_name, value in (
        ("raw_payloads", LATE_REPARSE_RAW_ID),
        ("signals", max(SIGNAL_IDS)),
    ):
        connection.execute(
            sa.text(
                f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                ":value, true)"
            ),
            {"value": value},
        )


def _signal_nonownership_hash(connection: sa.Connection) -> str:
    table = _reflect(connection, ("signals",))["signals"]
    rows = [
        dict(row)
        for row in connection.execute(
            sa.select(
                table.c.id,
                table.c.date,
                table.c.domain,
                table.c.source,
                table.c.kind,
                table.c.key,
                table.c.value_num,
                table.c.unit,
                table.c.note,
                table.c.at_time,
                table.c.raw_id,
                table.c.batch_id,
                table.c.misparse,
                table.c.created_at,
                table.c.updated_at,
            )
            .where(table.c.id.in_(SIGNAL_IDS))
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
            username="synthetic-stage3j-owner",
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
    assert not any(day.isoformat() in rendered for day in SIGNAL_DATES)
    assert not any(
        str(value) in rendered
        for value in SIGNAL_IDS + (RAW_ID, LATE_REPARSE_RAW_ID)
    )
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
                "phase": SIGNAL_OWNERSHIP_BACKFILL_PHASE,
                "prefix": f"{SIGNAL_OWNERSHIP_BACKFILL_PHASE}.%",
            },
        )
        return [dict(row) for row in rows.mappings()]


async def _signal_graph(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT id, subject_id, actor_user_id, integration_connection_id, "
                "date, domain, source, kind, key, value_num, unit, note, at_time, "
                "raw_id, batch_id, misparse, created_at, updated_at "
                "FROM signals ORDER BY id"
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


async def _phase_high_watermark(engine: AsyncEngine, phase: str) -> int:
    async with engine.connect() as connection:
        value = await connection.scalar(
            sa.text(
                "SELECT scan_high_watermark_id "
                "FROM ownership_backfill_checkpoints WHERE phase_key=:phase"
            ),
            {"phase": phase},
        )
        assert value is not None
        return int(value)


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
                "signals": [],
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
async def test_real_postgres_0034_signal_stop_resume_volatility_and_restore(
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
        business_hash_before = await _run_sync(engine, _signal_nonownership_hash)

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
            "backfill_signal_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert status["phase"] == SIGNAL_OWNERSHIP_BACKFILL_PHASE
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["snapshot_rows"] == 2
        assert status["remaining_rows"] == 2
        assert await _checkpoint_states(engine) == []

        started = await _run_cli(
            "backfill_signal_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert started["status"] == "running"
        assert started["batch_table"] == "signals"
        assert started["batch_scanned_rows"] == 1
        assert started["batch_updated_rows"] == 1
        assert await _run_sync(engine, _signal_nonownership_hash) == business_hash_before

        stopped_checkpoint = await _checkpoint_states(engine)
        assert len(stopped_checkpoint) == 1
        assert stopped_checkpoint[0]["status"] == "running"
        graph = await _signal_graph(engine)
        assert graph[0]["subject_id"] == identity.subject_id
        assert graph[1]["subject_id"] is None
        assert all(row["actor_user_id"] is None for row in graph)
        assert all(row["integration_connection_id"] is None for row in graph)

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            processed = await signals_service.list_signals(
                session, subject_id=identity.subject_id, include_misparse=True
            )
            assert [row.id for row in processed] == [SIGNAL_IDS[0]]

        stopped = await _run_cli(
            "backfill_signal_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert stopped["status"] == "running"
        assert stopped["scanned_rows"] == 1
        assert await _checkpoint_states(engine) == stopped_checkpoint

        resumed = await _run_cli(
            "backfill_signal_subject_ownership.py",
            ["--apply", "--batch-size", "1000", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert resumed["status"] == "completed"
        assert resumed["snapshot_rows"] == 2
        assert resumed["scanned_rows"] == 2
        assert resumed["updated_rows"] == 2
        assert resumed["remaining_rows"] == 0
        assert await _run_sync(engine, _signal_nonownership_hash) == business_hash_before

        graph = await _signal_graph(engine)
        assert {row["id"] for row in graph} == set(SIGNAL_IDS)
        assert all(row["subject_id"] == identity.subject_id for row in graph)
        assert all(row["actor_user_id"] is None for row in graph)
        assert all(row["integration_connection_id"] is None for row in graph)
        async with factory() as session:
            strict = await signals_service.list_signals(
                session, subject_id=identity.subject_id, include_misparse=True
            )
            assert {row.id for row in strict} == set(SIGNAL_IDS)
            frequencies = await signals_service.key_frequency(
                session, subject_id=identity.subject_id
            )
            assert sum(item.count for item in frequencies) == 2

        completed_checkpoint = await _checkpoint_states(engine)
        idempotent = await _run_cli(
            "backfill_signal_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "3"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert idempotent["status"] == "completed"
        assert idempotent["batch_scanned_rows"] == 0
        assert idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoint

        # Supported lifecycle remains valid after completion: one historical
        # batch is marked, another row deleted, a pre-Stage3A actorless S+C raw
        # and one current raw are reparsed, and an MCP fact is inserted above the
        # frozen Stage3J high-water mark.
        raw_high_watermark = await _phase_high_watermark(
            engine, RAW_OWNERSHIP_BACKFILL_PHASE
        )
        assert LATE_REPARSE_RAW_ID <= raw_high_watermark
        async with factory() as session:
            assert await signals_service.mark_misparse(
                session,
                "synthetic-stage3j-linked",
                subject_id=identity.subject_id,
            ) == 1
            assert await signals_service.delete_signal(
                session,
                SIGNAL_IDS[1],
                subject_id=identity.subject_id,
            )
            connection = await session.scalar(
                sa.select(IntegrationConnection).where(
                    IntegrationConnection.subject_id == identity.subject_id,
                    IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
                    IntegrationConnection.connection_type
                    == IntegrationConnectionType.RECIPIENT.value,
                )
            )
            assert connection is not None
            late_raw = await session.get(RawPayload, LATE_REPARSE_RAW_ID)
            assert late_raw is not None
            assert late_raw.subject_id == identity.subject_id
            assert late_raw.actor_user_id is None
            assert late_raw.integration_connection_id == connection.id
            actorless = await signals_service.reparse_unparsed(
                session,
                parse=lambda _text: [
                    {
                        "kind": "exposure",
                        "key": "synthetic_actorless_reparsed",
                        "note": PRIVATE_SENTINEL,
                    }
                ],
                limit=1,
                since_days=30,
                subject_id=identity.subject_id,
                integration_connection_id=connection.id,
                allow_historical_null_actor_connection=True,
            )
            assert len(actorless) == 1
            assert actorless[0].id > completed_checkpoint[0][
                "scan_high_watermark_id"
            ]
            assert actorless[0].raw_id == LATE_REPARSE_RAW_ID
            assert actorless[0].subject_id == identity.subject_id
            assert actorless[0].actor_user_id is None
            assert actorless[0].integration_connection_id == connection.id
            raw = RawPayload(
                subject_id=identity.subject_id,
                actor_user_id=identity.user_id,
                integration_connection_id=connection.id,
                domain=Domain.SIGNALS.value,
                source=Source.TELEGRAM.value,
                external_id="synthetic-stage3j-reparse",
                payload={"text": PRIVATE_SENTINEL},
            )
            session.add(raw)
            await session.flush()
            reparsed = await signals_service.reparse_unparsed(
                session,
                parse=lambda _text: [
                    {
                        "kind": "symptom",
                        "key": "synthetic_reparsed",
                        "note": PRIVATE_SENTINEL,
                    }
                ],
                subject_id=identity.subject_id,
                integration_connection_id=connection.id,
            )
            assert len(reparsed) == 1 and reparsed[0].raw_id == raw.id
            live = await signals_service.create_signals(
                session,
                items=[{"kind": "state", "key": "synthetic_live", "value_num": 3}],
                on_date=date(2026, 8, 21),
                source=Source.MCP.value,
                identity=WriteIdentity(identity.subject_id, identity.user_id),
            )
            assert len(live) == 1 and live[0].raw_id is None
            await session.commit()

        volatile = await _run_cli(
            "backfill_signal_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert volatile["status"] == "completed"
        assert volatile["rows_above_high_watermark"] == 3
        assert await _checkpoint_states(engine) == completed_checkpoint

        await _round_trip_portability_v1(engine)
        restored_graph = await _signal_graph(engine)
        assert len(restored_graph) == 4
        assert all(row["subject_id"] == identity.subject_id for row in restored_graph)
        assert all(row["actor_user_id"] is None for row in restored_graph)
        assert all(row["integration_connection_id"] is None for row in restored_graph)
        restore_status = await _run_cli(
            "backfill_signal_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restore_status["status"] == "running"
        assert restore_status["snapshot_rows"] == 4
        assert restore_status["scanned_rows"] == 0
        assert await _phase_statuses(engine, SIGNAL_OWNERSHIP_BACKFILL_PHASE) == (
            "running",
        )

        restored = await _run_cli(
            "backfill_signal_subject_ownership.py",
            ["--apply", "--batch-size", "2", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restored["status"] == "completed"
        assert restored["snapshot_rows"] == 4
        assert restored["updated_rows"] == 0
        assert restored["unchanged_rows"] == 4

        await _replace_with_empty_portability_v1(engine)
        empty_status = await _run_cli(
            "backfill_signal_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert empty_status["status"] == "completed"
        assert empty_status["snapshot_rows"] == 0
        assert empty_status["remaining_rows"] == 0
        assert await _signal_graph(engine) == []

        final_checkpoint = await _checkpoint_states(engine)
        with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_REFUSAL)):
            await asyncio.to_thread(command.downgrade, alembic_config, "0044")
        assert await _alembic_version(engine) == "0045"
        assert await _checkpoint_states(engine) == final_checkpoint
    finally:
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, "head")
        await engine.dispose()
