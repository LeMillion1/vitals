"""Production-shaped Stage-3N rehearsal from a synthetic revision-0034 lake."""

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
from vitals.enums import Source
from vitals.models.base import Base
from vitals.ownership import WriteIdentity
from vitals.services import (
    conflict_catalog,
    conflict_engine,
    conflict_registrations,
    data_portability_service,
    hrt_catalog,
    genetics_service,
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
)
from vitals.services.signal_ownership_backfill_service import (
    SIGNAL_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots
from vitals.services.weight_log_ownership_backfill_service import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.genetic_variant_ownership_backfill_service import (
    GENETIC_VARIANT_OWNERSHIP_BACKFILL_PHASE,
)
from vitals.services.lab_result_ownership_backfill_service import (
    LAB_RESULT_OWNERSHIP_BACKFILL_PHASE,
)


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
RAW_ID = 55_100
VARIANT_IDS = (55_201, 55_202)
VARIANT_DATES = (date(2026, 8, 19), date(2026, 8, 20))
LIVE_DATE = date(2026, 8, 21)
PRIVATE_SENTINEL = "synthetic-private-stage3n-gene-rsid-interpretation"
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
    tables = _reflect(connection, ("raw_payloads", "genetic_variants"))
    created_at = datetime(2026, 8, 19, 8, 30, 15)
    updated_at = datetime(2026, 8, 19, 9, 45, 30)
    connection.execute(
        tables["raw_payloads"].insert(),
        [
            {
                "id": RAW_ID,
                "domain": "genetics",
                "source": "vcf_import",
                # The legacy importer keyed a batch by its filename; the domain
                # reader still accepts that exact pre-versioned revision.
                "external_id": "synthetic-stage3n.vcf",
                "fetched_at": created_at,
                "payload": {
                    "filename": "synthetic-stage3n.vcf",
                    "variants": [["rs1800562", "C", "G", "CG"]],
                },
                "processed_at": updated_at,
            }
        ],
    )
    connection.execute(
        tables["genetic_variants"].insert(),
        [
            {
                "id": VARIANT_IDS[0],
                "domain": "genetics",
                "source": "vcf_import",
                "gene": "HFE",
                "rsid": "rs1800562",
                "genotype": "CG",
                "marker": "hemochromatosis_carrier",
                "impact": "carrier",
                "impact_domain": "supplements",
                "interpretation": PRIVATE_SENTINEL,
                "action_notes": None,
                "created_at": created_at,
                "updated_at": updated_at,
            },
            {
                "id": VARIANT_IDS[1],
                "domain": "genetics",
                "source": "manual",
                "gene": "MTHFR",
                "rsid": "rs1801133",
                "genotype": "CT",
                "marker": "mthfr_heterozygous",
                "impact": "reduced activity",
                "impact_domain": "supplements",
                "interpretation": PRIVATE_SENTINEL,
                "action_notes": None,
                "created_at": created_at,
                "updated_at": updated_at,
            },
        ],
    )
    for table_name, value in (
        ("raw_payloads", RAW_ID),
        ("genetic_variants", max(VARIANT_IDS)),
    ):
        connection.execute(
            sa.text(
                f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                ":value, true)"
            ),
            {"value": value},
        )


def _link_vcf_batch(connection: sa.Connection) -> None:
    """Attach the durable VCF link revision 0037 adds after the 0034 snapshot."""

    table = _reflect(connection, ("genetic_variants",))["genetic_variants"]
    connection.execute(
        table.update()
        .where(table.c.id == VARIANT_IDS[0])
        .values(raw_payload_id=RAW_ID)
    )


def _variant_nonownership_hash(connection: sa.Connection) -> str:
    table = _reflect(connection, ("genetic_variants",))["genetic_variants"]
    rows = [
        dict(row)
        for row in connection.execute(
            sa.select(
                table.c.id,
                table.c.domain,
                table.c.source,
                table.c.gene,
                table.c.rsid,
                table.c.genotype,
                table.c.marker,
                table.c.impact,
                table.c.impact_domain,
                table.c.interpretation,
                table.c.action_notes,
                table.c.raw_payload_id,
                table.c.created_at,
                table.c.updated_at,
            )
            .where(table.c.id.in_(VARIANT_IDS))
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
            username="synthetic-stage3n-owner",
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
    assert not any(day.isoformat() in rendered for day in VARIANT_DATES)
    assert not any(str(value) in rendered for value in VARIANT_IDS + (RAW_ID,))
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
                "phase": GENETIC_VARIANT_OWNERSHIP_BACKFILL_PHASE,
                "prefix": f"{GENETIC_VARIANT_OWNERSHIP_BACKFILL_PHASE}.%",
            },
        )
        return [dict(row) for row in rows.mappings()]


async def _genetic_variant_graph(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT id, subject_id, actor_user_id, domain, source, gene, rsid, "
                "genotype, marker, impact, impact_domain, interpretation, "
                "action_notes, raw_payload_id, created_at, updated_at "
                "FROM genetic_variants ORDER BY id"
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
                "genetic_variants": [],
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
async def test_real_postgres_0034_genetic_variant_stop_resume_volatility_and_restore(
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
        # ``raw_payload_id`` only exists from revision 0037, so the durable VCF
        # link is attached on the upgraded schema rather than in the 0034 seed.
        await _run_sync(engine, _link_vcf_batch)
        business_hash_before = await _run_sync(engine, _variant_nonownership_hash)
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
            "backfill_genetic_variant_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert status["phase"] == GENETIC_VARIANT_OWNERSHIP_BACKFILL_PHASE
        assert status["mode"] == "status"
        assert status["status"] == "not_started"
        assert status["snapshot_rows"] == 2
        assert status["remaining_rows"] == 2
        assert await _checkpoint_states(engine) == []

        started = await _run_cli(
            "backfill_genetic_variant_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "1"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert started["status"] == "running"
        assert started["batch_table"] == "genetic_variants"
        assert started["batch_scanned_rows"] == 1
        assert started["batch_updated_rows"] == 1
        assert await _run_sync(engine, _variant_nonownership_hash) == business_hash_before

        stopped_checkpoint = await _checkpoint_states(engine)
        assert len(stopped_checkpoint) == 1
        assert stopped_checkpoint[0]["status"] == "running"
        graph = await _genetic_variant_graph(engine)
        assert graph[0]["subject_id"] == identity.subject_id
        assert graph[1]["subject_id"] is None
        # The parser roots stay on the raw payload; the fact never gains an
        # actor it did not persist.
        assert all(row["actor_user_id"] is None for row in graph)

        stopped = await _run_cli(
            "backfill_genetic_variant_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert stopped["status"] == "running"
        assert stopped["scanned_rows"] == 1
        assert await _checkpoint_states(engine) == stopped_checkpoint

        resumed = await _run_cli(
            "backfill_genetic_variant_subject_ownership.py",
            ["--apply", "--batch-size", "1000", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert resumed["status"] == "completed"
        assert resumed["snapshot_rows"] == 2
        assert resumed["scanned_rows"] == 2
        assert resumed["updated_rows"] == 2
        assert resumed["remaining_rows"] == 0
        assert await _run_sync(engine, _variant_nonownership_hash) == business_hash_before

        graph = await _genetic_variant_graph(engine)
        assert {row["id"] for row in graph} == set(VARIANT_IDS)
        assert all(row["subject_id"] == identity.subject_id for row in graph)
        assert all(row["actor_user_id"] is None for row in graph)

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            # Migrated history keeps its unknown actor null, which the genetics
            # reader still reaches through the legacy compatibility bridge; its
            # retirement is a separate later gate.
            listed = await genetics_service.list_variants(
                session,
                subject_id=identity.subject_id,
            )
            assert {row.id for row in listed} == set(VARIANT_IDS)

        completed_checkpoint = await _checkpoint_states(engine)
        idempotent = await _run_cli(
            "backfill_genetic_variant_subject_ownership.py",
            ["--apply", "--batch-size", "1", "--max-batches", "3"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert idempotent["status"] == "completed"
        assert idempotent["batch_scanned_rows"] == 0
        assert idempotent["batch_updated_rows"] == 0
        assert await _checkpoint_states(engine) == completed_checkpoint

        # Supported post-completion volatility: a strict live manual variant
        # above the frozen high-water mark.
        write_identity = WriteIdentity(identity.subject_id, identity.user_id)
        conflict_registrations.register_all_resolvers()
        async with factory() as session:
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_engine.ConflictWriteContext(
                    identity=write_identity,
                    evaluation_date=LIVE_DATE,
                    legacy_bridge=conflict_engine.LegacyConflictBridge.REJECT,
                ),
            )
            live = await genetics_service.add_variant(
                session,
                gene="SYNTHETIC",
                rsid="rs-synthetic-live",
                genotype="AA",
                marker="synthetic_live_marker",
                identity=write_identity,
                prepared_conflict_write=prepared,
            )
            assert live.subject_id == identity.subject_id
            assert live.actor_user_id == identity.user_id
            assert live.raw_payload_id is None
            assert live.id > completed_checkpoint[0]["scan_high_watermark_id"]
            await session.commit()

        volatile = await _run_cli(
            "backfill_genetic_variant_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert volatile["status"] == "completed"
        assert volatile["rows_above_high_watermark"] == 1
        assert await _checkpoint_states(engine) == completed_checkpoint

        await _round_trip_portability_v1(engine)
        restored_graph = await _genetic_variant_graph(engine)
        assert len(restored_graph) == 3
        assert all(row["subject_id"] == identity.subject_id for row in restored_graph)
        # Backup v1 carries no actor provenance.
        assert all(row["actor_user_id"] is None for row in restored_graph)
        restore_status = await _run_cli(
            "backfill_genetic_variant_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restore_status["status"] == "running"
        assert restore_status["snapshot_rows"] == 3
        assert restore_status["scanned_rows"] == 0
        assert await _phase_statuses(engine, GENETIC_VARIANT_OWNERSHIP_BACKFILL_PHASE) == (
            "running",
        )

        restored = await _run_cli(
            "backfill_genetic_variant_subject_ownership.py",
            ["--apply", "--batch-size", "2", "--max-batches", "10"],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert restored["status"] == "completed"
        assert restored["snapshot_rows"] == 3
        assert restored["updated_rows"] == 0
        assert restored["unchanged_rows"] == 3

        await _replace_with_empty_portability_v1(engine)
        empty_status = await _run_cli(
            "backfill_genetic_variant_subject_ownership.py",
            [],
            database_url=database_url,
            expected_keys=AGGREGATE_CLI_KEYS,
        )
        assert empty_status["status"] == "completed"
        assert empty_status["snapshot_rows"] == 0
        assert empty_status["remaining_rows"] == 0
        assert await _genetic_variant_graph(engine) == []

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
