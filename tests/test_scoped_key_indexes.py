"""Stage-5B contract: the scoped unique keys are installed beside the legacy ones."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

import vitals.models  # noqa: F401 -- register the complete schema
from vitals.enums import Domain, Source
from vitals.models.base import Base
from vitals.models.labs import LabMarker
from vitals.models.weight import BodyMeasurement, WeightLog
from vitals.scoped_keys import SCOPED_KEYS


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
MIGRATION = REPOSITORY_ROOT / "migrations" / "versions" / "0047_scoped_unique_keys.py"


def _migration_module():
    spec = importlib.util.spec_from_file_location("revision_0047", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _index(name: str) -> sa.Index:
    return next(
        index
        for table in Base.metadata.tables.values()
        for index in table.indexes
        if index.name == name
    )


def test_migration_matches_the_reviewed_catalog_exactly():
    """The migration and the registry cannot drift apart silently."""

    module = _migration_module()
    assert module.revision == "0047"
    assert module.down_revision == "0046"
    assert list(module.SCOPED_UNIQUE_INDEXES) == [
        (
            spec.table,
            index.name,
            index.columns,
            index.postgresql_predicate,
            index.sqlite_predicate,
        )
        for spec in SCOPED_KEYS
        for index in spec.replacements
    ]


def test_every_scoped_key_is_installed_beside_its_legacy_key():
    for spec in SCOPED_KEYS:
        table = Base.metadata.tables[spec.table]
        names = {index.name for index in table.indexes}
        names |= {constraint.name for constraint in table.constraints}
        # Stage 5B is purely additive: the legacy global key still stands.
        assert spec.legacy_name in names, spec.legacy_name
        for replacement in spec.replacements:
            assert replacement.name in names, replacement.name
            index = _index(replacement.name)
            assert index.unique is True
            assert tuple(
                column.name for column in index.expressions
            ) == replacement.columns
            for dialect, expected in (
                ("postgresql", replacement.postgresql_predicate),
                ("sqlite", replacement.sqlite_predicate),
            ):
                actual = index.dialect_options.get(dialect, {}).get("where")
                assert (actual is None) == (expected is None), replacement.name
                if expected is not None:
                    assert str(actual) == expected, replacement.name


def test_no_scoped_key_narrows_what_its_legacy_key_already_allows():
    """A scoped key is strictly weaker, so installing it can reject nothing.

    Every replacement either keeps the legacy columns and adds a scope column,
    or keeps them and narrows the row set with a predicate. Both directions can
    only accept more rows than the global key did, which is why Stage 5B is safe
    to install before any write path changes.
    """

    for spec in SCOPED_KEYS:
        for replacement in spec.replacements:
            legacy = set(spec.legacy_columns)
            scoped = set(replacement.columns)
            widened_by_scope = legacy < scoped
            narrowed_by_predicate = (
                legacy == scoped
                and replacement.postgresql_predicate is not None
                and (
                    spec.legacy_postgresql_predicate is None
                    or replacement.postgresql_predicate
                    != spec.legacy_postgresql_predicate
                )
            )
            assert widened_by_scope or narrowed_by_predicate, replacement.name


@pytest.mark.asyncio
async def test_scoped_key_actually_enforces_within_one_subject(
    db_session, legacy_owner_roots
):
    db_session.add(
        LabMarker(
            subject_id=legacy_owner_roots.subject_id, name="ferritin", unit="ng/mL"
        )
    )
    await db_session.flush()
    db_session.add(
        LabMarker(
            subject_id=legacy_owner_roots.subject_id, name="ferritin", unit="ng/mL"
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_scoped_weight_key_only_covers_the_active_row(
    db_session, legacy_owner_roots
):
    """The partial predicate carries over: superseded history may repeat."""

    same_day = date(2026, 7, 8)
    db_session.add_all(
        [
            WeightLog(
                subject_id=legacy_owner_roots.subject_id,
                date=same_day,
                domain=Domain.WEIGHT.value,
                source=Source.GARMIN_API.value,
                weight_kg=80.0,
                superseded=True,
            ),
            WeightLog(
                subject_id=legacy_owner_roots.subject_id,
                date=same_day,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                weight_kg=81.0,
                superseded=True,
            ),
            WeightLog(
                subject_id=legacy_owner_roots.subject_id,
                date=same_day,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                weight_kg=81.5,
                superseded=False,
            ),
        ]
    )
    await db_session.flush()

    db_session.add(
        WeightLog(
            subject_id=legacy_owner_roots.subject_id,
            date=same_day,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            weight_kg=82.0,
            superseded=False,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_measurement_scoped_key_enforces_one_set_per_subject_day(
    db_session, legacy_owner_roots
):
    db_session.add(
        BodyMeasurement(
            subject_id=legacy_owner_roots.subject_id,
            date=date(2026, 7, 9),
            domain=Domain.BODY_COMPOSITION.value,
            source=Source.MANUAL.value,
            waist_cm=84.0,
        )
    )
    await db_session.flush()
    db_session.add(
        BodyMeasurement(
            subject_id=legacy_owner_roots.subject_id,
            date=date(2026, 7, 9),
            domain=Domain.BODY_COMPOSITION.value,
            source=Source.MANUAL.value,
            waist_cm=85.0,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def _index_state(engine, names: list[str]) -> dict[str, bool]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT c.relname AS name, i.indisvalid AS valid "
                "FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                "WHERE c.relname = ANY(:names)"
            ),
            {"names": names},
        )
        return {row.name: bool(row.valid) for row in rows}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_0047_installs_valid_scoped_keys_concurrently(
    db_session,
    monkeypatch,
):
    """The cutover's indexes build without locking a populated health table."""

    import asyncio
    import os

    from alembic import command
    from alembic.config import Config as AlembicConfig
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    await db_session.close()
    alembic_config = AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    engine = create_async_engine(database_url, poolclass=NullPool)
    replacements = [
        index.name for spec in SCOPED_KEYS for index in spec.replacements
    ]
    legacy = [spec.legacy_name for spec in SCOPED_KEYS]
    migration_control_ready = False

    try:
        migration_control_ready = True
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
        await asyncio.to_thread(command.upgrade, alembic_config, "0034")

        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO lab_markers (name, domain, tier, "
                    "created_at, updated_at) VALUES "
                    "('synthetic-marker', 'labs', 1, now(), now())"
                )
            )
        await asyncio.to_thread(command.upgrade, alembic_config, "0046")
        assert await _index_state(engine, replacements) == {}
        assert set(await _index_state(engine, legacy)) == set(legacy)

        await asyncio.to_thread(command.upgrade, alembic_config, "0047")
        installed = await _index_state(engine, replacements)
        assert set(installed) == set(replacements)
        # A CONCURRENTLY build that failed would leave the index INVALID.
        assert all(installed.values())
        # Purely additive: every legacy global key still stands beside it.
        assert set(await _index_state(engine, legacy)) == set(legacy)

        await asyncio.to_thread(command.downgrade, alembic_config, "0046")
        assert await _index_state(engine, replacements) == {}
        assert set(await _index_state(engine, legacy)) == set(legacy)

        await asyncio.to_thread(command.upgrade, alembic_config, "0047")
        assert all((await _index_state(engine, replacements)).values())
    finally:
        if migration_control_ready:
            await asyncio.to_thread(command.upgrade, alembic_config, "head")
        await engine.dispose()
