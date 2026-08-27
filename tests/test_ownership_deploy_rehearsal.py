"""The whole deploy, once, on one lake.

Twenty rehearsals prove one backfill phase each, and two more prove the contract
migration and the row policies — but on a lake that was empty or stamped by hand.
Nothing put the two halves together: a revision-0034 database with real data,
carried through every phase in order and then through the contract.

That is the sequence a production upgrade performs, and it is the one that
cannot be retried. Revision 0049 is a one-way boundary once a second subject has
written, so a phase-ordering mistake found during the upgrade is found past the
point where downgrade is allowed. It is worth one slow test.

What is checked is deliberately not what the per-phase rehearsals check. They own
stop/resume, volatility, restore and each phase's own semantics. This one owns
the seam between them: that every phase reports completion, that nothing is left
unstamped afterwards, that the contract migration accepts the result rather than
refusing it, that the data is the same data, and that the policies then isolate
it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

import vitals.models  # noqa: F401 -- register the complete schema for teardown
from vitals.ownership import (
    PRE_OWNERSHIP_CONTRACT_REVISION,
    required_ownership_columns,
)
from vitals.ownership_deploy import OWNERSHIP_BACKFILL_SEQUENCE
from vitals.services import conflict_catalog
from vitals.services.hrt import catalog
from vitals.services.identity_bootstrap import bootstrap_legacy_owner
from vitals.persistence.rls import SUBJECT_SETTING
from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots

from tests.test_row_level_security import restricted_engine

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
PASSWORD_HASH = "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
SEED_DATE = date(2026, 8, 19)
SEED_AT = datetime(2026, 8, 19, 8, 30, 15)


def test_the_sequence_covers_every_ownership_phase_on_disk():
    """A new phase cannot ship without taking its place in the order.

    The scripts directory is the operator's menu; the sequence is the order. A
    phase present in one and absent from the other is a phase somebody will run
    at the wrong time, or not at all.
    """

    on_disk = {
        path.name
        for path in (REPOSITORY_ROOT / "scripts").glob("backfill_*.py")
        # A reparse is not an ownership phase: it re-derives normalized rows
        # from payloads whose owner is already settled.
        if path.name != "backfill_garmin_reparse.py"
    }
    sequenced = [step.script for step in OWNERSHIP_BACKFILL_SEQUENCE]
    assert set(sequenced) == on_disk
    assert len(sequenced) == len(set(sequenced)), "a phase is listed twice"


def _seed_revision_0034(connection: sa.Connection) -> None:
    """A small lake with something for most phases to find.

    Breadth matters more than depth here: each phase's own rehearsal covers its
    shapes, and what this needs is for the chain to have work at every link.
    """

    metadata = sa.MetaData()
    metadata.reflect(bind=connection)

    def insert(table_name: str, rows: list[dict[str, Any]]) -> None:
        connection.execute(metadata.tables[table_name].insert(), rows)

    raw_payloads = metadata.tables["raw_payloads"]
    connection.execute(
        raw_payloads.insert(),
        [
            {
                "domain": "garmin",
                "source": "garmin_api",
                "external_id": f"daily:{SEED_DATE.isoformat()}",
                "payload": {"summary": {"totalSteps": 8000}},
                "fetched_at": SEED_AT,
            },
            {
                "domain": "labs",
                "source": "lab_parser",
                "external_id": "panel:deploy-rehearsal",
                "payload": {"markers": [{"name": "ferritin", "value": 45.0}]},
                "fetched_at": SEED_AT,
            },
        ],
    )
    signal_raw_id = connection.execute(
        raw_payloads.insert().values(
            domain="signals",
            source="telegram",
            external_id="telegram:synthetic-deploy-rehearsal",
            payload={"text": "synthetic"},
            fetched_at=SEED_AT,
        )
    ).inserted_primary_key[0]
    insert(
        "signals",
        [
            {
                "date": SEED_DATE,
                "domain": "signals",
                "source": "telegram",
                "kind": "note",
                "key": "synthetic",
                "raw_id": signal_raw_id,
                "batch_id": "synthetic-deploy",
            }
        ],
    )
    insert(
        "day_context",
        [
            {
                "date": SEED_DATE,
                "domain": "signals",
                "source": "template",
                "answers": {"synthetic": True},
            }
        ],
    )
    insert(
        "weight_logs",
        [
            {
                "date": SEED_DATE,
                "weight_kg": 86.4,
                "domain": "weight",
                "source": "manual",
                "is_active": True,
            }
        ],
    )
    insert(
        "supplements",
        [
            {
                "name": "Creatine",
                "key": "creatine",
                "domain": "supplements",
                "source": "manual",
                "active": True,
            }
        ],
    )
    insert(
        "meal_logs",
        [
            {
                "date": SEED_DATE,
                "name": "Ужин",
                "calories": 700,
                "domain": "nutrition",
                "source": "manual",
            }
        ],
    )
    # Revision 0077 adds canonical lab-marker columns. The ownership backfill
    # deliberately runs at 0048, so a real pre-existing marker proves that the
    # operator phase materializes only columns available at that revision.
    insert(
        "lab_markers",
        [
            {
                "domain": "labs",
                "name": "synthetic-deploy-marker",
                "tier": 2,
            }
        ],
    )
    insert(
        "system_alerts",
        [
            {
                "created_at": SEED_AT,
                "domain": "weight",
                "severity": "warn",
                "message": "synthetic-deploy-rehearsal",
                "alert_key": "weight.noisy_period_active",
                "entity_ref": "",
            }
        ],
    )


def _business_fingerprint(connection: sa.Connection) -> str:
    """What the lake holds, ignoring who it now belongs to.

    The backfill adds ownership and must change nothing else. Comparing this
    before and after is what turns "the phases reported success" into "the data
    survived".
    """

    metadata = sa.MetaData()
    metadata.reflect(bind=connection)
    payload: dict[str, list[dict[str, Any]]] = {}
    ownership_columns = {
        "subject_id",
        "actor_user_id",
        "integration_connection_id",
        "file_asset_id",
        "platform_connection_id",
        "platform_integration_connection_id",
    }
    for name in (
        "raw_payloads",
        "weight_logs",
        "supplements",
        "meal_logs",
        "system_alerts",
    ):
        table = metadata.tables[name]
        columns = [
            column
            for column in table.columns
            if column.name not in ownership_columns
        ]
        payload[name] = [
            {key: value for key, value in row.items()}
            for row in connection.execute(
                sa.select(*columns).order_by(table.c.id)
            ).mappings()
        ]
    encoded = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _run_sync(engine: AsyncEngine, function):
    async with engine.begin() as connection:
        return await connection.run_sync(function)


async def _remaining_nulls(engine: AsyncEngine) -> dict[str, int]:
    """What the contract migration's own guard would find."""

    outstanding: dict[str, int] = {}
    async with engine.connect() as connection:
        # ``required_ownership_columns`` describes the schema at head, and this
        # runs against a lake at the pre-contract revision. A table introduced
        # by a later revision is created with its ownership mandatory from the
        # first row, so it has no unowned history for the guard to find — and
        # querying it here would only ask about a relation that is not there.
        present = set(
            await connection.run_sync(
                lambda sync_connection: sa.inspect(sync_connection).get_table_names()
            )
        )
        for table_name, column_name in required_ownership_columns():
            if table_name not in present:
                continue
            remaining = await connection.scalar(
                sa.text(
                    f'SELECT count(*) FROM "{table_name}" '
                    f'WHERE "{column_name}" IS NULL'
                )
            )
            if remaining:
                outstanding[f"{table_name}.{column_name}"] = remaining
    return outstanding


async def _run_phase(step, *, database_url: str) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["VITALS_DATABASE_URL"] = database_url
    process = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts" / step.script),
            "--apply",
            "--batch-size",
            "1000",
            "--max-batches",
            "100",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert process.returncode == 0, (step.script, process.stdout, process.stderr)
    payload = json.loads(process.stdout)
    assert payload["phase"] == step.phase, step.script
    return payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_0034_lake_reaches_head_through_every_phase(
    db_session,
    monkeypatch,
):
    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    alembic_config = AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    await db_session.close()
    engine = create_async_engine(database_url, poolclass=NullPool)

    try:
        async with engine.begin() as connection:
            # Not ``drop_all``: it only knows the tables the models still
            # declare, so one a revision dropped stays behind and its foreign
            # keys block the live tables from going. A rehearsal database is
            # rebuilt from migrations on the next line anyway.
            await connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            await connection.exec_driver_sql("CREATE SCHEMA public")
            await connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
        await asyncio.to_thread(command.upgrade, alembic_config, "0034")
        await _run_sync(engine, _seed_revision_0034)

        await asyncio.to_thread(
            command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION
        )
        fingerprint_before = await _run_sync(engine, _business_fingerprint)

        # A lake this far along is exactly the one the contract must refuse: the
        # columns exist, and nothing has stamped them yet.
        assert await _remaining_nulls(engine)

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        async with async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )() as session:
            identity = await bootstrap_legacy_owner(
                session,
                username="synthetic-deploy-owner",
                password_hash=PASSWORD_HASH,
                timezone="Asia/Almaty",
            )
            await bootstrap_legacy_resource_roots(
                session, subject_id=identity.subject_id
            )
            # The HRT and conflict phases classify rows against the checked-in
            # catalogs, which startup materializes before any job runs.
            await catalog.sync_catalog(session)
            await conflict_catalog.sync_catalog(session)
            await session.commit()

        for step in OWNERSHIP_BACKFILL_SEQUENCE:
            result = await _run_phase(step, database_url=database_url)
            assert result["status"] == "completed", step.script

        # Every phase reporting completion has to mean the lake is stamped;
        # otherwise "completed" is a claim about the loop, not about the data.
        assert await _remaining_nulls(engine) == {}

        await asyncio.to_thread(command.upgrade, alembic_config, "head")

        async with engine.connect() as connection:
            nullable = {
                (row.table_name, row.column_name): row.is_nullable
                for row in (
                    await connection.execute(
                        sa.text(
                            "SELECT table_name, column_name, is_nullable "
                            "FROM information_schema.columns "
                            "WHERE table_schema = current_schema()"
                        )
                    )
                ).all()
            }
        still_nullable = [
            key for key in required_ownership_columns() if nullable.get(key) != "NO"
        ]
        assert not still_nullable

        assert await _run_sync(engine, _business_fingerprint) == fingerprint_before, (
            "the backfill changed data it was only supposed to attribute"
        )

        # The same lake, now behind the policies. Elsewhere they are proven on a
        # database seeded by hand; here they close over rows that arrived at
        # their owner through the nineteen phases.
        restricted = await restricted_engine(database_url)
        try:
            async with restricted.connect() as connection:
                assert await connection.scalar(
                    sa.text("SELECT count(*) FROM weight_logs")
                ) == 0, "an unbound session must see nothing, not everything"
                await connection.execute(
                    sa.text("SELECT set_config(:name, :value, false)"),
                    {"name": SUBJECT_SETTING, "value": str(identity.subject_id)},
                )
                for table_name in ("weight_logs", "supplements", "meal_logs"):
                    assert await connection.scalar(
                        sa.text(f"SELECT count(*) FROM {table_name}")
                    ) == 1, table_name
        finally:
            await restricted.dispose()
    finally:
        async with engine.begin() as connection:
            # Not ``drop_all``: it only knows the tables the models still
            # declare, so one a revision dropped stays behind and its foreign
            # keys block the live tables from going. A rehearsal database is
            # rebuilt from migrations on the next line anyway.
            await connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            await connection.exec_driver_sql("CREATE SCHEMA public")
            await connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
        await asyncio.to_thread(
            command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION
        )
        await engine.dispose()


def test_the_runbook_lists_the_phases_in_the_sequence_order():
    """A runbook that drifts from the code is worse than no runbook.

    An operator follows the document, not the tuple, so the document has to be
    checked against the tuple rather than trusted to have been updated.
    """

    runbook = (REPOSITORY_ROOT / "docs" / "OWNERSHIP_CUTOVER_RUNBOOK.md").read_text()
    listed_lines = [
        line for line in runbook.splitlines() if "scripts/backfill_" in line
    ]
    listed = [line.split("scripts/")[1].split(" ")[0] for line in listed_lines]
    assert listed == [step.script for step in OWNERSHIP_BACKFILL_SEQUENCE]
    assert all("--max-batches 100" in line for line in listed_lines)
    for step in OWNERSHIP_BACKFILL_SEQUENCE:
        assert step.phase in runbook, step.phase

    bootstrap_script = REPOSITORY_ROOT / "scripts" / "bootstrap_ownership_roots.py"
    assert bootstrap_script.is_file()
    bootstrap_source = bootstrap_script.read_text()
    assert "_REPOSITORY_ROOT" in bootstrap_source
    assert "sys.path.insert(0, str(_REPOSITORY_ROOT))" in bootstrap_source
    assert "scripts/bootstrap_ownership_roots.py" in runbook
    assert "docker compose run --rm --no-deps" in runbook
    assert runbook.count('$PWD/.env:/app/.env:ro') == 2


async def test_the_bounded_root_bootstrap_is_idempotent(
    db_session, legacy_owner_roots
):
    from scripts.bootstrap_ownership_roots import bootstrap
    from vitals.models.identity import HealthSubject, User

    user = await db_session.get(User, legacy_owner_roots.user_id)
    subject = await db_session.get(HealthSubject, legacy_owner_roots.subject_id)
    assert user is not None and user.password_hash
    assert subject is not None

    await bootstrap(
        db_session,
        username=user.username,
        password_hash=user.password_hash,
        timezone=subject.timezone,
    )
    await db_session.commit()
