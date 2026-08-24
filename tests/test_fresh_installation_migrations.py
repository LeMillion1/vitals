"""An empty database has to reach head, because that is how one is created.

The container's start command is ``alembic upgrade head && uvicorn …``. On an
empty PostgreSQL that failed: revision ``0005`` seeded five skincare products,
revision ``0049`` made ``skincare_products.subject_id`` NOT NULL and refuses
while any row has no owner, and on a fresh database there is no owner to give
them — identity bootstrap is an application step that runs after migrations. So
the process could not start, and a new installation could not be created at all.

Nothing saw it. Every other migration test starts from a synthetic revision-0034
lake with an owner already bootstrapped, which is the *upgrade* path; the fast
suite builds its schema with ``create_all`` and never runs a migration. The one
path nobody exercised was the one every new deployment takes.

This is a PostgreSQL test because the chain is: revision ``0024`` and others use
JSONB, which SQLite cannot compile, so ``alembic upgrade head`` has never been
runnable there and asserting it on the fast path is not an option.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from vitals.models.base import Base

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


async def _empty_the_database(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_empty_database_migrates_all_the_way_to_head(
    db_session, monkeypatch
):
    """What a brand-new deployment does on its first boot, and nothing else."""

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    alembic_config = AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))

    await db_session.close()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        await _empty_the_database(engine)
        await asyncio.to_thread(command.upgrade, alembic_config, "head")

        async with engine.begin() as connection:
            stamped = await connection.scalar(
                sa.text("SELECT version_num FROM alembic_version")
            )
            # The guard that used to fail, asked directly: no table this
            # migration chain creates may arrive at head holding a row nobody
            # owns, because on a fresh database nobody ever will.
            orphans = await connection.scalar(
                sa.text("SELECT count(*) FROM skincare_products")
            )
        assert stamped is not None
        assert orphans == 0
    finally:
        # Leave the database as the rest of the run expects to find it: the
        # shared fixture rebuilds the schema per test from the models, and an
        # ``alembic_version`` row left behind would make a later migration test
        # believe it had already run.
        await _empty_the_database(engine)
        await engine.dispose()
