"""Subject isolation enforced by PostgreSQL, not only by the application.

Revision 0050 gives every table whose ``subject_id`` is mandatory a policy that
compares it to the ``vitals.subject_id`` session setting. Proving it takes two
things the rest of the suite deliberately does not have.

The schema has to come from the migrations. Everywhere else the suite builds it
with ``create_all``, which knows about columns and constraints but nothing about
policies — so no other test is affected by row security, and none of them can
demonstrate it either.

And the connection has to come from a role the policies apply to. The suite runs
as a superuser, which bypasses row security by definition; that is convenient
and correct for the backfill and the migration runner, and it is exactly why the
boundary needs a proof that does not use it.

What is pinned here is the three properties that make the policy worth having:
an unbound session sees nothing rather than everything, a bound one sees its own
subject and no other, and ``FORCE`` holds so the table owner is not exempt.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import vitals.models  # noqa: F401 -- register the complete metadata graph
from vitals.ownership import OWNERSHIP_REGISTRY, TargetColumn
from vitals.services.rls_session import SUBJECT_SETTING

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = (
    REPOSITORY_ROOT
    / "migrations"
    / "versions"
    / "0050_force_subject_row_level_security.py"
)
RESTRICTED_ROLE = "vitals_rls_probe"
RESTRICTED_PASSWORD = "synthetic-rls-probe"


def _revision_module():
    spec = importlib.util.spec_from_file_location("_rev0050", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── The list is derived, so it has to stay derivable ─────────────────────────

def test_the_policy_covers_exactly_the_tables_with_a_mandatory_subject():
    """A table whose subject became mandatory must not be left unprotected.

    The policy's comparison is only total where the column is: on a nullable
    subject it silently omits the NULL rows instead of deciding about them,
    which is a different predicate and needs its own review.
    """

    from vitals.models.base import Base

    listed = set(_revision_module().SUBJECT_ISOLATED_TABLES)
    expected = {
        table_name
        for table_name, spec in OWNERSHIP_REGISTRY.items()
        if spec.subject is TargetColumn.REQUIRED
        and "subject_id" in Base.metadata.tables[table_name].columns
        and not Base.metadata.tables[table_name].columns["subject_id"].nullable
    }
    assert listed == expected


def test_the_migration_and_the_application_name_the_same_setting():
    """Two halves of one contract: the policy reads what the session writes."""

    assert _revision_module().SUBJECT_SETTING == SUBJECT_SETTING


# ── The boundary itself, against a migrated schema and a restricted role ─────

async def _migrated_engine(database_url: str, alembic_config):
    """A database built the way a real installation is: by the migrations."""

    import asyncio

    from alembic import command

    from vitals.models.base import Base

    from vitals.ownership import (
        PRE_OWNERSHIP_CONTRACT_REVISION,
        required_ownership_columns,
    )

    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
    await asyncio.to_thread(
        command.upgrade, alembic_config, PRE_OWNERSHIP_CONTRACT_REVISION
    )

    # Revision 0005 seeds five global skincare products, so even an otherwise
    # empty lake is behind and the contract migration refuses. Give them an
    # owner the way the backfill phases do, then finish migrating.
    async with engine.begin() as connection:
        owner_id = await connection.scalar(
            sa.text(
                "INSERT INTO users (id, username, normalized_username, "
                "password_hash, status, created_at, updated_at) VALUES "
                "(gen_random_uuid(), 'rls-seed-owner', 'rls-seed-owner', "
                "'$synthetic', 'active', now(), now()) RETURNING id"
            )
        )
        seed_subject = await connection.scalar(
            sa.text(
                "INSERT INTO health_subjects (id, owner_user_id, timezone, "
                "created_at, updated_at) VALUES (gen_random_uuid(), :owner, "
                "'Asia/Almaty', now(), now()) RETURNING id"
            ),
            {"owner": owner_id},
        )
        for table_name, column_name in required_ownership_columns():
            if column_name != "subject_id":
                continue
            await connection.execute(
                sa.text(
                    f'UPDATE "{table_name}" SET "{column_name}" = :value '
                    f'WHERE "{column_name}" IS NULL'
                ),
                {"value": seed_subject},
            )
    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    return engine


async def _restricted_engine(database_url: str):
    """A role with no special attributes, granted plain DML on every table.

    No ``BYPASSRLS``, not the table owner, not superuser — the shape the
    application connects with in a deployment where the policies are the point.
    """

    admin = create_async_engine(database_url, poolclass=NullPool)
    async with admin.begin() as connection:
        await connection.execute(
            sa.text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE "
                f"rolname = '{RESTRICTED_ROLE}') THEN CREATE ROLE "
                f"{RESTRICTED_ROLE} LOGIN PASSWORD '{RESTRICTED_PASSWORD}'; "
                "END IF; END $$"
            )
        )
        await connection.execute(
            sa.text(f"GRANT USAGE ON SCHEMA public TO {RESTRICTED_ROLE}")
        )
        await connection.execute(
            sa.text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
                f"public TO {RESTRICTED_ROLE}"
            )
        )
        await connection.execute(
            sa.text(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "
                f"{RESTRICTED_ROLE}"
            )
        )
    await admin.dispose()

    _, _, tail = database_url.partition("://")
    _, _, host_and_db = tail.partition("@")
    return create_async_engine(
        f"postgresql+asyncpg://{RESTRICTED_ROLE}:{RESTRICTED_PASSWORD}@{host_and_db}",
        poolclass=NullPool,
    )


async def _seed_two_subjects(engine) -> tuple[uuid.UUID, uuid.UUID]:
    """One owner and one supplement each, written as the exempt superuser."""

    ids: list[uuid.UUID] = []
    async with engine.begin() as connection:
        for label in ("rls-a", "rls-b"):
            owner_id = await connection.scalar(
                sa.text(
                    "INSERT INTO users (id, username, normalized_username, "
                    "password_hash, status, created_at, updated_at) VALUES "
                    "(gen_random_uuid(), :name, :name, '$synthetic', 'active', "
                    "now(), now()) RETURNING id"
                ),
                {"name": label},
            )
            subject_id = await connection.scalar(
                sa.text(
                    "INSERT INTO health_subjects (id, owner_user_id, timezone, "
                    "created_at, updated_at) VALUES (gen_random_uuid(), "
                    ":owner, 'Asia/Almaty', now(), now()) RETURNING id"
                ),
                {"owner": owner_id},
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO supplements (subject_id, domain, source, name, "
                    "\"key\", active, created_at, updated_at) VALUES "
                    "(:subject, 'supplements', 'manual', :name, :key, true, "
                    "now(), now())"
                ),
                {
                    "subject": subject_id,
                    "name": f"Supplement {label}",
                    "key": f"key-{label}",
                },
            )
            ids.append(subject_id)
    return ids[0], ids[1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_policies_isolate_subjects_and_fail_closed(
    db_session,
    monkeypatch,
):
    """Unbound sees nothing; bound sees exactly one subject; a stranger's id opens nothing."""

    from alembic.config import Config as AlembicConfig

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    await db_session.close()

    admin = await _migrated_engine(
        database_url, AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    )
    restricted = await _restricted_engine(database_url)
    try:
        first, second = await _seed_two_subjects(admin)

        async with restricted.connect() as connection:
            unbound = await connection.scalar(
                sa.text("SELECT count(*) FROM supplements")
            )
            assert unbound == 0, "an unbound session must see nothing, not everything"

            for subject_id, key in ((first, "key-rls-a"), (second, "key-rls-b")):
                await connection.execute(
                    sa.text("SELECT set_config(:name, :value, false)"),
                    {"name": SUBJECT_SETTING, "value": str(subject_id)},
                )
                visible = (
                    await connection.execute(
                        sa.text("SELECT subject_id, key FROM supplements")
                    )
                ).all()
                assert [row.subject_id for row in visible] == [subject_id]
                assert visible[0].key == key

            # An id nobody owns is not a skeleton key.
            await connection.execute(
                sa.text("SELECT set_config(:name, :value, false)"),
                {"name": SUBJECT_SETTING, "value": str(uuid.uuid4())},
            )
            assert await connection.scalar(
                sa.text("SELECT count(*) FROM supplements")
            ) == 0
    finally:
        await restricted.dispose()
        await admin.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_write_check_and_force_hold(db_session, monkeypatch):
    """``WITH CHECK`` refuses a write addressed elsewhere, and ``FORCE`` is on.

    Reading is the obvious half. Without the write side a bound session could
    still insert a row addressed to somebody else and then never see what it
    wrote. And without ``FORCE`` none of it would apply to the table owner,
    which is the role the application connects as.
    """

    from alembic.config import Config as AlembicConfig

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    await db_session.close()

    listed = _revision_module().SUBJECT_ISOLATED_TABLES
    admin = await _migrated_engine(
        database_url, AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    )
    restricted = await _restricted_engine(database_url)
    try:
        mine, theirs = await _seed_two_subjects(admin)

        async with admin.connect() as connection:
            rows = (
                await connection.execute(
                    sa.text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE relname = ANY(:names)"
                    ),
                    {"names": list(listed)},
                )
            ).all()
        state = {
            row.relname: (row.relrowsecurity, row.relforcerowsecurity) for row in rows
        }
        assert set(state) == set(listed)
        unforced = sorted(name for name, flags in state.items() if flags != (True, True))
        assert not unforced, f"row security is not forced on: {unforced}"

        insert = sa.text(
            "INSERT INTO supplements "
            "(subject_id, domain, source, name, \"key\", active, created_at, "
            "updated_at) VALUES (:subject_id, 'supplements', 'manual', :name, "
            ":key, true, now(), now())"
        )
        async with restricted.connect() as connection:
            await connection.execute(
                sa.text("SELECT set_config(:name, :value, false)"),
                {"name": SUBJECT_SETTING, "value": str(mine)},
            )
            await connection.execute(
                insert, {"subject_id": mine, "name": "Mine", "key": "rls-mine"}
            )
            with pytest.raises(Exception) as caught:
                await connection.execute(
                    insert,
                    {"subject_id": theirs, "name": "Theirs", "key": "rls-theirs"},
                )
            assert "row-level security" in str(caught.value).lower()
            await connection.rollback()
    finally:
        await restricted.dispose()
        await admin.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_binding_survives_a_commit_and_refuses_a_switch(
    db_session,
    monkeypatch,
):
    """The application's own binding, proven against a role the policy applies to.

    ``set_config(..., is_local => true)`` is discarded at commit, which is what
    makes it safe on a pooled connection and what would otherwise leave a
    service that commits mid-work reading against a policy that matches nothing.
    The rebind-on-new-transaction listener is the difference, and this is where
    it is visible.
    """

    from alembic.config import Config as AlembicConfig
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from vitals.services.rls_session import (
        RlsSessionError,
        bind_session_subject,
        bound_subject,
    )

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    await db_session.close()

    admin = await _migrated_engine(
        database_url, AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    )
    restricted = await _restricted_engine(database_url)
    try:
        mine, theirs = await _seed_two_subjects(admin)
        factory = async_sessionmaker(
            restricted, expire_on_commit=False, class_=AsyncSession
        )
        async with factory() as session:
            await bind_session_subject(session, mine)
            assert bound_subject(session) == mine
            assert await session.scalar(
                sa.text("SELECT count(*) FROM supplements")
            ) == 1

            # The binding is transaction-scoped, so this commit discards it in
            # the database; the session re-applies it when the next one opens.
            await session.commit()
            assert await session.scalar(
                sa.text("SELECT count(*) FROM supplements")
            ) == 1

            # One transaction serves one person. Switching would leave rows
            # already loaded in the identity map under the wrong policy.
            with pytest.raises(RlsSessionError, match="different health subject"):
                await bind_session_subject(session, theirs)
    finally:
        await restricted.dispose()
        await admin.dispose()
