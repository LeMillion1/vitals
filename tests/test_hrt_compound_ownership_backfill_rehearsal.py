"""Production-shaped Stage-3F rehearsal from a synthetic revision-0034 lake."""

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
from vitals.services import data_portability_service, hrt_catalog
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
from vitals.services.provider_raw_ownership_backfill_service import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.raw_ownership_backfill_service import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
RAW_ID = 43_001
WORKOUT_ID = 43_101
CURATED_ID = 43_201
CUSTOM_ID = 43_202
CURATED_COMPONENT_IDS = (43_301, 43_302, 43_303, 43_304)
CUSTOM_COMPONENT_IDS = (43_305, 43_306)
CURATED_KEY = "sustanon_250"
CUSTOM_KEY = "synthetic_custom_blend"
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
        (
            "raw_payloads",
            "hevy_workouts",
            "hrt_compounds",
            "hrt_compound_components",
        ),
    )
    created_at = datetime(2026, 8, 20, 8, 30, 15)
    updated_at = datetime(2026, 8, 20, 9, 45, 30)
    common = {"created_at": created_at, "updated_at": updated_at}
    payload = {
        "id": "synthetic-stage3f-workout",
        "title": "Synthetic Stage-3F dependency",
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
            **common,
        )
    )

    definition = dict(hrt_catalog.load_compound_catalog())[CURATED_KEY]
    curated_values = hrt_catalog._normalize_values(definition)
    connection.execute(
        tables["hrt_compounds"].insert(),
        [
            {
                "id": CURATED_ID,
                "domain": "hrt",
                "source": "system",
                "key": CURATED_KEY,
                "active": True,
                **curated_values,
                **common,
            },
            {
                "id": CUSTOM_ID,
                "domain": "hrt",
                "source": "manual",
                "key": CUSTOM_KEY,
                "name": "Synthetic custom blend",
                "name_ru": None,
                "compound_class": "testosterone",
                "ester": "blend",
                "route": "intramuscular",
                "dose_unit": "mg",
                "conc_mg_ml": 200.0,
                "tablet_mg": None,
                "half_life_hours": 96.0,
                "active_fraction": 0.7,
                "aromatizes": "true",
                "aliases": ["synthetic custom"],
                "active": True,
                "note": "Synthetic custom evidence",
                **common,
            },
        ],
    )
    curated_components = definition["components"]
    component_rows = [
        {
            "id": component_id,
            "compound_id": CURATED_ID,
            "ester": component["ester"],
            "mg": float(component["mg"]),
            **common,
        }
        for component_id, component in zip(
            CURATED_COMPONENT_IDS, curated_components, strict=True
        )
    ]
    component_rows.extend(
        [
            {
                "id": CUSTOM_COMPONENT_IDS[0],
                "compound_id": CUSTOM_ID,
                "ester": "custom-short",
                "mg": 80.0,
                **common,
            },
            {
                "id": CUSTOM_COMPONENT_IDS[1],
                "compound_id": CUSTOM_ID,
                "ester": "custom-long",
                "mg": 120.0,
                **common,
            },
        ]
    )
    connection.execute(tables["hrt_compound_components"].insert(), component_rows)


def _custom_non_ownership_hash(connection: sa.Connection) -> str:
    tables = _reflect(connection, ("hrt_compounds", "hrt_compound_components"))
    parent_columns = [
        column
        for column in tables["hrt_compounds"].c
        if column.name not in {"subject_id", "actor_user_id"}
    ]
    component_columns = [
        column
        for column in tables["hrt_compound_components"].c
        if column.name != "subject_id"
    ]
    rows = [
        {
            "table": "hrt_compounds",
            **dict(row),
        }
        for row in connection.execute(
            sa.select(*parent_columns).where(
                tables["hrt_compounds"].c.id == CUSTOM_ID
            )
        ).mappings()
    ]
    rows.extend(
        {
            "table": "hrt_compound_components",
            **dict(row),
        }
        for row in connection.execute(
            sa.select(*component_columns)
            .where(tables["hrt_compound_components"].c.compound_id == CUSTOM_ID)
            .order_by(tables["hrt_compound_components"].c.id)
        ).mappings()
    )
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
            username="synthetic-stage3f-owner",
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
            {"prefix": f"{HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE}.%"},
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


async def _ownership_graph(engine: AsyncEngine) -> dict[str, list[dict[str, Any]]]:
    async with engine.connect() as connection:
        parents = await connection.execute(
            sa.text(
                "SELECT id, key, source, subject_id, actor_user_id "
                "FROM hrt_compounds WHERE id IN (:curated_id, :custom_id) "
                "ORDER BY id"
            ),
            {"curated_id": CURATED_ID, "custom_id": CUSTOM_ID},
        )
        components = await connection.execute(
            sa.text(
                "SELECT c.id, c.compound_id, c.ester, c.subject_id, "
                "p.source AS parent_source, p.subject_id AS parent_subject_id "
                "FROM hrt_compound_components c "
                "JOIN hrt_compounds p ON p.id=c.compound_id "
                "WHERE p.id IN (:curated_id, :custom_id) ORDER BY c.id"
            ),
            {"curated_id": CURATED_ID, "custom_id": CUSTOM_ID},
        )
        return {
            "parents": [dict(row) for row in parents.mappings()],
            "components": [dict(row) for row in components.mappings()],
        }


async def _catalog_component_ids(engine: AsyncEngine) -> tuple[int, ...]:
    async with engine.connect() as connection:
        rows = await connection.scalars(
            sa.text(
                "SELECT c.id FROM hrt_compound_components c "
                "JOIN hrt_compounds p ON p.id=c.compound_id "
                "WHERE p.key=:key ORDER BY c.id"
            ),
            {"key": CURATED_KEY},
        )
        return tuple(rows)


async def _sync_catalog(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await hrt_catalog.sync_catalog(session)
        await session.commit()


async def _round_trip_portability_v1(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        snapshot = await data_portability_service.export_full(session)
        await data_portability_service.import_full(session, snapshot)
        await session.commit()


async def _alembic_version(engine: AsyncEngine) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_0034_hrt_compounds_stop_resume_and_catalog_churn(
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
        custom_hash_before = await _run_sync(engine, _custom_non_ownership_hash)

        await asyncio.to_thread(command.upgrade, alembic_config, "head")
        identity = await _bootstrap_roots(engine)

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
            "backfill_hrt_compound_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert status["phase"] == HRT_COMPOUND_OWNERSHIP_BACKFILL_PHASE
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["tables_total"] == 2
        assert status["snapshot_rows"] == 8
        assert status["remaining_rows"] == 8
        assert await _checkpoint_states(engine) == []

        parent_started = await _run_cli(
            "backfill_hrt_compound_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert parent_started["status"] == "running"
        assert parent_started["batch_table"] == "hrt_compounds"
        assert parent_started["batch_scanned_rows"] == 1
        assert parent_started["batch_updated_rows"] == 0
        assert parent_started["batch_unchanged_rows"] == 1
        parent_stop = await _checkpoint_states(engine)
        assert len(parent_stop) == 2
        assert [row["status"] for row in parent_stop] == ["running", "running"]

        stopped = await _run_cli(
            "backfill_hrt_compound_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert stopped["scanned_rows"] == 1
        assert await _checkpoint_states(engine) == parent_stop

        parent_completed = await _run_cli(
            "backfill_hrt_compound_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert parent_completed["batch_table"] == "hrt_compounds"
        assert parent_completed["completed_tables"] == 1
        assert parent_completed["batch_updated_rows"] == 1

        component_started = await _run_cli(
            "backfill_hrt_compound_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert component_started["status"] == "running"
        assert component_started["batch_table"] == "hrt_compound_components"
        assert component_started["batch_scanned_rows"] == 1
        component_stop = await _checkpoint_states(engine)
        assert [row["status"] for row in component_stop] == ["running", "completed"]

        resumed = await _run_cli(
            "backfill_hrt_compound_subject_ownership.py",
            ["--apply", "--batch-size", "2", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert resumed["status"] == "completed"
        assert resumed["completed"] is True
        assert resumed["batch_table"] == "hrt_compound_components"
        assert resumed["tables_total"] == 2
        assert resumed["completed_tables"] == 2
        assert resumed["snapshot_rows"] == 8
        assert resumed["scanned_rows"] == 8
        assert resumed["updated_rows"] == 3
        assert resumed["unchanged_rows"] == 5
        assert resumed["remaining_rows"] == 0

        completed_checkpoints = await _checkpoint_states(engine)
        assert len(completed_checkpoints) == 2
        assert {row["status"] for row in completed_checkpoints} == {"completed"}
        assert await _run_sync(engine, _custom_non_ownership_hash) == custom_hash_before

        graph = await _ownership_graph(engine)
        parents = {row["key"]: row for row in graph["parents"]}
        assert parents[CURATED_KEY]["subject_id"] is None
        assert parents[CURATED_KEY]["actor_user_id"] is None
        assert parents[CUSTOM_KEY]["subject_id"] == identity.subject_id
        assert parents[CUSTOM_KEY]["actor_user_id"] is None
        for component in graph["components"]:
            if component["compound_id"] == CURATED_ID:
                assert component["subject_id"] is None
                assert component["parent_subject_id"] is None
            else:
                assert component["compound_id"] == CUSTOM_ID
                assert component["subject_id"] == identity.subject_id
                assert component["parent_subject_id"] == identity.subject_id

        idempotent = await _run_cli(
            "backfill_hrt_compound_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "3"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert idempotent["status"] == "completed"
        assert idempotent["batch_scanned_rows"] == 0
        assert idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoints

        old_catalog_component_ids = await _catalog_component_ids(engine)
        assert old_catalog_component_ids == CURATED_COMPONENT_IDS
        await _sync_catalog(engine)
        new_catalog_component_ids = await _catalog_component_ids(engine)
        assert len(new_catalog_component_ids) == len(CURATED_COMPONENT_IDS)
        assert set(new_catalog_component_ids).isdisjoint(old_catalog_component_ids)
        assert await _run_sync(engine, _custom_non_ownership_hash) == custom_hash_before

        volatile_status = await _run_cli(
            "backfill_hrt_compound_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert volatile_status["status"] == "completed"
        assert await _checkpoint_states(engine) == completed_checkpoints

        volatile_idempotent = await _run_cli(
            "backfill_hrt_compound_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "2"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert volatile_idempotent["status"] == "completed"
        assert volatile_idempotent["batch_scanned_rows"] == 0
        assert volatile_idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoints

        await _round_trip_portability_v1(engine)
        assert await _phase_statuses(engine, RAW_OWNERSHIP_BACKFILL_PHASE) == (
            "restore_blocked",
        )
        assert set(
            await _phase_statuses(engine, PROVIDER_RAW_OWNERSHIP_BACKFILL_PHASE)
        ) == {"completed", "restore_blocked"}
        assert set(
            await _phase_statuses(engine, HEVY_CHILD_OWNERSHIP_BACKFILL_PHASE)
        ) == {"completed"}

        restore_status = await _run_cli(
            "backfill_hrt_compound_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restore_status["status"] == "running"
        assert restore_status["remaining_rows"] == restore_status["snapshot_rows"]
        restore_checkpoints = await _checkpoint_states(engine)
        assert len(restore_checkpoints) == 2
        assert {row["status"] for row in restore_checkpoints} == {"running"}

        restored = await _run_cli(
            "backfill_hrt_compound_subject_ownership.py",
            ["--apply", "--batch-size", "1000", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restored["status"] == "completed"
        assert restored["completed_tables"] == 2
        assert restored["remaining_rows"] == 0
        assert await _run_sync(engine, _custom_non_ownership_hash) == custom_hash_before

        restored_graph = await _ownership_graph(engine)
        restored_parents = {row["key"]: row for row in restored_graph["parents"]}
        assert restored_parents[CURATED_KEY]["subject_id"] is None
        assert restored_parents[CURATED_KEY]["actor_user_id"] is None
        assert restored_parents[CUSTOM_KEY]["subject_id"] == identity.subject_id
        assert restored_parents[CUSTOM_KEY]["actor_user_id"] is None
        for component in restored_graph["components"]:
            assert component["subject_id"] == component["parent_subject_id"]

        restored_checkpoints = await _checkpoint_states(engine)
        assert len(restored_checkpoints) == 2
        assert {row["status"] for row in restored_checkpoints} == {"completed"}

        with pytest.raises(RuntimeError, match=re.escape(DOWNGRADE_REFUSAL)):
            await asyncio.to_thread(command.downgrade, alembic_config, "0044")
        assert await _alembic_version(engine) == "0045"
        assert await _checkpoint_states(engine) == restored_checkpoints
    finally:
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, "head")
        await engine.dispose()
