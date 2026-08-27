"""The NOT NULL ownership contract: revision 0049 and what it promises.

Three separate things have to agree, and each has failed independently before:
the ownership registry, which says a reference is required; the models, which
the fast suite's ``create_all`` schema comes from; and the migration, which is
what a real installation actually runs. This module pins the migration's column
list to the registry, and the integration case pins the migration's behaviour on
PostgreSQL — including its refusal to run over a half-finished backfill, which
is the failure mode that would otherwise be discovered mid-deploy.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import vitals.models  # noqa: F401 -- register the complete metadata graph
from vitals.models.base import Base
from vitals.services.identity.bootstrap import bootstrap_legacy_owner
from vitals.ownership import (
    OWNERSHIP_REGISTRY,
    PRE_OWNERSHIP_CONTRACT_REVISION,
    OwnershipBackfillIncompleteError,
    TargetColumn,
)

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
# A real bcrypt hash: the bootstrap accepts an existing owner only by exact
# hash, so a placeholder would make the helper refuse on a second run.
PASSWORD_HASH = (
    "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
)
_MIGRATION = (
    REPOSITORY_ROOT
    / "migrations"
    / "versions"
    / "0049_required_ownership_contract.py"
)


def _revision_module():
    spec = importlib.util.spec_from_file_location("_rev0049", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_OWNERSHIP_COLUMNS = (
    ("subject", "subject_id"),
    ("connection", "integration_connection_id"),
    ("file_asset", "file_asset_id"),
)


#: Tables 0049 correctly listed and a later revision dropped. The migration is
#: history and does not change; the registry is the present and no longer has
#: them, so the comparison has to subtract what has since gone. Revision 0058
#: dropped both when the Telegram chat that filled them was removed.
_DROPPED_SINCE = {
    ("signals", "subject_id"),
    ("day_context", "subject_id"),
}


def test_the_migration_alters_exactly_what_the_registry_requires():
    """The list in the migration is derived, so it must stay derivable.

    A table that changes classification — or a new one that arrives already
    required — has to fail here rather than be quietly left nullable, which is
    the one outcome nothing downstream would notice: scoped keys and RLS both
    ignore a null instead of rejecting it.
    """

    listed = set(_revision_module().REQUIRED_OWNERSHIP_COLUMNS) - _DROPPED_SINCE
    expected = {
        (table_name, column_name)
        for table_name, spec in OWNERSHIP_REGISTRY.items()
        for field, column_name in _OWNERSHIP_COLUMNS
        if getattr(spec, field) is TargetColumn.REQUIRED
        and column_name in Base.metadata.tables[table_name].columns
    }
    # The eight tables created NOT NULL from the start are already covered by
    # their own revisions and are not re-altered here.
    already_not_null = {
        (table_name, column_name)
        for table_name, column_name in expected
        if not Base.metadata.tables[table_name].columns[column_name].nullable
        and (table_name, column_name) not in listed
    }
    assert listed | already_not_null == expected
    assert not listed & already_not_null


def test_every_listed_column_exists_and_is_mandatory_in_the_model():
    for table_name, column_name in _revision_module().REQUIRED_OWNERSHIP_COLUMNS:
        if (table_name, column_name) in _DROPPED_SINCE:
            continue
        column = Base.metadata.tables[table_name].columns[column_name]
        assert not column.nullable, f"{table_name}.{column_name}"


async def _alembic_version(engine) -> str:
    async with engine.connect() as connection:
        return str(
            await connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            )
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_contract_refuses_an_unfinished_backfill(
    db_session,
    monkeypatch,
):
    """A lake with one unstamped row must stop the deploy, by name.

    ``SET NOT NULL`` would fail on its own, but with PostgreSQL's message — one
    column, no count, and no indication that a backfill is what is missing. The
    guard runs first so the operator learns which tables are behind and how far.
    """

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    alembic_config = AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    await db_session.close()
    engine = create_async_engine(database_url, poolclass=NullPool)
    _revision_module()

    try:
        async with engine.begin() as connection:
            # Not ``drop_all``: it only knows the tables the models still
            # declare, so one a revision dropped stays behind and its foreign
            # keys block the live tables from going. A rehearsal database is
            # rebuilt from migrations on the next line anyway.
            await connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            await connection.exec_driver_sql("CREATE SCHEMA public")
            await connection.execute(
                sa.text("DROP TABLE IF EXISTS alembic_version")
            )
        await asyncio.to_thread(
            command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION
        )

        # Two unowned rows in two tables, so the message covers both rather
        # than one. Revision 0005 used to supply the second set for free — it
        # seeded five global skincare products — until that turned out to make
        # a fresh installation unmigratable, because on an empty database no
        # owner ever appears for them and this guard is what refuses. The seed
        # is gone; the rows are inserted here, which is what this test was
        # always about.
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO supplements (key, name, active, created_at) "
                    "VALUES ('iron', 'Iron', true, now())"
                )
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO skincare_products "
                    "(name, type, created_at, updated_at) "
                    "VALUES ('Retinoid', 'retinoid', now(), now())"
                )
            )

        with pytest.raises(OwnershipBackfillIncompleteError) as caught:
            await asyncio.to_thread(command.upgrade, alembic_config, "head")
        message = str(caught.value)
        assert "supplements.subject_id: 1" in message
        assert "skincare_products.subject_id: 1" in message
        assert "backfill" in message

        # The refusal is a refusal, not a partial application: nothing was
        # altered, so the schema is still exactly the pre-contract one.
        assert await _alembic_version(engine) == PRE_OWNERSHIP_CONTRACT_REVISION
        async with engine.connect() as connection:
            nullable = await connection.scalar(
                sa.text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'weight_logs' AND column_name = 'subject_id'"
                )
            )
        assert nullable == "YES"
    finally:
        async with engine.begin() as connection:
            # Not ``drop_all``: it only knows the tables the models still
            # declare, so one a revision dropped stays behind and its foreign
            # keys block the live tables from going. A rehearsal database is
            # rebuilt from migrations on the next line anyway.
            await connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            await connection.exec_driver_sql("CREATE SCHEMA public")
            await connection.execute(
                sa.text("DROP TABLE IF EXISTS alembic_version")
            )
        await asyncio.to_thread(
            command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION
        )
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_contract_lands_on_an_empty_lake_and_reverses(
    db_session,
    monkeypatch,
):
    """With nothing left to stamp the contract applies, and downgrade undoes it."""

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    alembic_config = AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    await db_session.close()
    engine = create_async_engine(database_url, poolclass=NullPool)
    # Minus what a later revision dropped: this rehearsal runs the whole chain
    # to head, so by the time it inspects nullability those tables are gone.
    listed = [
        pair
        for pair in _revision_module().REQUIRED_OWNERSHIP_COLUMNS
        if pair not in _DROPPED_SINCE
    ]

    async def _nullability() -> dict[tuple[str, str], str]:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa.text(
                        "SELECT table_name, column_name, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = current_schema()"
                    )
                )
            ).all()
        return {(table, column): nullable for table, column, nullable in rows}

    try:
        async with engine.begin() as connection:
            # Not ``drop_all``: it only knows the tables the models still
            # declare, so one a revision dropped stays behind and its foreign
            # keys block the live tables from going. A rehearsal database is
            # rebuilt from migrations on the next line anyway.
            await connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            await connection.exec_driver_sql("CREATE SCHEMA public")
            await connection.execute(
                sa.text("DROP TABLE IF EXISTS alembic_version")
            )
        await asyncio.to_thread(
            command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION
        )
        before = await _nullability()
        assert all(before[key] == "YES" for key in listed)

        # Stand in for the backfill phases: give every row still without an
        # owner to the installation's sole subject, which is what they do. The
        # phases themselves are rehearsed one by one in their own modules; what
        # matters here is that the contract lands on their result.
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            identity = await bootstrap_legacy_owner(
                session,
                username="synthetic-contract-owner",
                password_hash=PASSWORD_HASH,
                timezone="Asia/Almaty",
            )
            await session.commit()
        async with engine.begin() as connection:
            for table_name, column_name in listed:
                if column_name != "subject_id":
                    continue
                await connection.execute(
                    sa.text(
                        f'UPDATE "{table_name}" SET "{column_name}" = :value '
                        f'WHERE "{column_name}" IS NULL'
                    ),
                    {"value": identity.subject_id},
                )

        await asyncio.to_thread(command.upgrade, alembic_config, "head")
        after = await _nullability()
        assert all(after[key] == "NO" for key in listed), [
            key for key in listed if after[key] != "NO"
        ]

        # No leftover scaffolding: the proving CHECK constraints are dropped once
        # SET NOT NULL has taken over from them.
        async with engine.connect() as connection:
            leftovers = (
                await connection.scalars(
                    sa.text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conname LIKE '%\\_present' AND contype = 'c'"
                    )
                )
            ).all()
        assert list(leftovers) == []

        await asyncio.to_thread(
            command.downgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION
        )
        reversed_nullability = await _nullability()
        assert all(reversed_nullability[key] == "YES" for key in listed)
    finally:
        async with engine.begin() as connection:
            # Not ``drop_all``: it only knows the tables the models still
            # declare, so one a revision dropped stays behind and its foreign
            # keys block the live tables from going. A rehearsal database is
            # rebuilt from migrations on the next line anyway.
            await connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            await connection.exec_driver_sql("CREATE SCHEMA public")
            await connection.execute(
                sa.text("DROP TABLE IF EXISTS alembic_version")
            )
        await asyncio.to_thread(
            command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION
        )
        await engine.dispose()
