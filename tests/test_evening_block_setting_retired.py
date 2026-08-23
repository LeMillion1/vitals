"""Revision 0059 takes ``evening_time`` out of the rows that already hold it.

The evening block was the 23:45 message that asked what the day had been made
of, and it wrote its answers to ``day_context``. Revision 0058 dropped that
table; nothing schedules an evening block any more, and the settings card kept
offering a time field for it.

Removing the field from the code is not enough on its own, and that is what this
test is really about. ``prefs._strict_object`` compares a stored policy's key set
against ``_SUBJECT_FIELDS`` with ``!=`` — deliberately, because a preference row
that has drifted from the code is worth failing on rather than silently ignoring.
So an installation that had ever saved its proactive settings would come back up
with a row carrying a field the code no longer knows, and every read of it would
raise instead of returning a policy. The rewrite has to land in the same revision
that removes the field.

Built from ``create_all`` and stamped at 0058 rather than replayed from the first
revision: the two tables this touches are plain key/JSON rows, and replaying
fifty-eight revisions to reach them would test the chain rather than the rewrite.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import sqlalchemy as sa

import vitals.models  # noqa: F401 -- register every table for create_all
from vitals.models.base import Base

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

SUBJECT_POLICY = {
    "brief_time": "08:15",
    "evening_time": "22:30",
    "nudges": {"activity": True, "nutrition": False, "data": True},
}
FLAT_POLICY = {
    "brief_time": "11:00",
    "evening_time": "23:45",
    "quiet_start": "02:00",
    "quiet_end": "10:00",
    "daily_budget": 4,
    "garmin_sync_hours": 6,
    "nudges": {"activity": True, "nutrition": True, "data": True},
}


async def _read(connection, table, where):
    row = await connection.execute(
        sa.text(f"SELECT value FROM {table} WHERE {where}")
    )
    return row.scalar_one()


@pytest.mark.integration
async def test_the_stored_evening_time_is_removed_and_restored(
    db_session, monkeypatch
):
    """Upgrade drops the key from both shapes; downgrade puts the default back.

    Both rows are asserted, because the policy lives in two: the live per-subject
    ``subject_settings`` row and the flat pre-identity ``app_settings`` one that
    an installation which has not been through identity bootstrap still reads.
    Only one of them was obvious, and a rewrite that missed the other would leave
    exactly the installations least able to notice it broken.
    """

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
    subject_id = "11111111-2222-3333-4444-555555555555"
    owner_id = "66666666-7777-8888-9999-000000000000"

    try:
        async with engine.begin() as connection:
            # Not ``drop_all``: it only knows the tables the models still
            # declare, so one a revision dropped stays behind and its foreign
            # keys block the live tables from going.
            await connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            await connection.exec_driver_sql("CREATE SCHEMA public")
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                sa.text(
                    "INSERT INTO users (id, username, normalized_username, "
                    "password_hash, status) VALUES (:uid, 'rehearsal', "
                    "'rehearsal', 'synthetic-test-hash', 'active')"
                ),
                {"uid": owner_id},
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO health_subjects "
                    "(id, owner_user_id, display_name, timezone) "
                    "VALUES (:id, :uid, 'Rehearsal', 'Asia/Almaty')"
                ),
                {"id": subject_id, "uid": owner_id},
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO subject_settings (subject_id, key, value) "
                    "VALUES (:id, 'proactive_subject_policy', :v)"
                ),
                {"id": subject_id, "v": json.dumps(SUBJECT_POLICY)},
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO app_settings (key, value) "
                    "VALUES ('proactive', :v)"
                ),
                {"v": json.dumps(FLAT_POLICY)},
            )
        await asyncio.to_thread(command.stamp, alembic_config, "0058")

        await asyncio.to_thread(command.upgrade, alembic_config, "0059")
        async with engine.begin() as connection:
            subject = await _read(
                connection,
                "subject_settings",
                "key = 'proactive_subject_policy'",
            )
            flat = await _read(connection, "app_settings", "key = 'proactive'")
        assert set(subject) == {"brief_time", "nudges"}
        assert subject["brief_time"] == "08:15", "the other fields are untouched"
        assert "evening_time" not in flat
        assert flat["daily_budget"] == 4, "the other fields are untouched"

        await asyncio.to_thread(command.downgrade, alembic_config, "0058")
        async with engine.begin() as connection:
            subject = await _read(
                connection,
                "subject_settings",
                "key = 'proactive_subject_policy'",
            )
            flat = await _read(connection, "app_settings", "key = 'proactive'")
        # The default, not 22:30: the value is not recoverable from here, and a
        # row *missing* the key fails the same strict decoder on an older binary.
        assert subject["evening_time"] == "23:45"
        assert flat["evening_time"] == "23:45"
        assert subject["brief_time"] == "08:15"
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP SCHEMA public CASCADE")
            await connection.exec_driver_sql("CREATE SCHEMA public")
        await engine.dispose()


def test_a_row_that_is_not_an_object_is_left_alone():
    """A migration is the wrong place to guess what a broken preference meant."""

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_m0059",
        REPOSITORY_ROOT
        / "migrations"
        / "versions"
        / "0059_retire_the_evening_block_setting.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._edit(None, drop=True) is None
    assert module._edit("11:00", drop=True) is None
    assert module._edit({"brief_time": "08:15"}, drop=True) is None
    assert module._edit({"evening_time": "22:30"}, drop=True) == {}
    assert module._edit({"brief_time": "08:15"}, drop=False) == {
        "brief_time": "08:15",
        "evening_time": "23:45",
    }
