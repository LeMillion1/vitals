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
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import vitals.models  # noqa: F401 -- register the complete metadata graph
from vitals.ownership import OWNERSHIP_REGISTRY, TargetColumn
from vitals.persistence.rls import SUBJECT_SETTING

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


#: Every revision that installs a subject policy, newest last. A table added
#: later belongs to a new revision listed here — which is the point: the
#: contract below asks the migrations what is covered rather than being told.
_POLICY_REVISIONS = (
    "0050_force_subject_row_level_security",
    "0051_row_security_for_catalogs_and_children",
    "0055_professional_invitations",
    "0056_care_relationships_and_consent",
    "0057_professional_notes_and_care_plans",
    "0060_per_subject_provider_credentials",
    "0061_care_team_threads_and_messages",
    "0062_support_access_requests",
    "0063_external_api_tokens",
    "0065_subject_scoped_mcp_grants",
    "0067_care_message_attachments",
    "0069_subject_isolated_care_push_outbox",
    "0076_support_repair_actions",
    "0078_break_glass_sessions",
    "0079_portability_import_receipts",
)


def _policy_revision(stem: str):
    spec = importlib.util.spec_from_file_location(
        f"_rev{stem[:4]}", REPOSITORY_ROOT / "migrations" / "versions" / f"{stem}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _all_covered_tables() -> set[str]:
    """Every table any revision put a subject policy on."""

    covered: set[str] = set()
    for stem in _POLICY_REVISIONS:
        module = _policy_revision(stem)
        for attribute in (
            "SUBJECT_ISOLATED_TABLES",
            "INHERITED_CHILDREN",
            "SHARED_WITH_INSTALLATION",
        ):
            covered.update(getattr(module, attribute, ()))
    return covered - _DROPPED_SINCE


#: Tables a policy revision correctly covered and a later one dropped. A policy
#: migration is history and does not change; the metadata is the present. 0058
#: dropped both with the Telegram chat that filled them.
_DROPPED_SINCE = {"signals", "day_context"}


# ── The list is derived, so it has to stay derivable ─────────────────────────

def test_the_policy_covers_exactly_the_tables_with_a_mandatory_subject():
    """A table whose subject became mandatory must not be left unprotected.

    The policy's comparison is only total where the column is: on a nullable
    subject it silently omits the NULL rows instead of deciding about them,
    which is a different predicate and needs its own review.
    """

    from vitals.models.base import Base

    listed = _all_covered_tables()
    expected = {
        table_name
        for table_name, spec in OWNERSHIP_REGISTRY.items()
        if spec.subject is TargetColumn.REQUIRED
        and "subject_id" in Base.metadata.tables[table_name].columns
        and not Base.metadata.tables[table_name].columns["subject_id"].nullable
    }
    # The catalog revision also covers tables whose subject is optional or
    # inherited; those are checked by their own contract further down.
    assert expected <= listed


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
        # Standalone routines are not part of SQLAlchemy metadata.  Without an
        # explicit drop this helper's second migrate-from-zero run would leave
        # revision 0081's function behind while removing its tables.
        await connection.execute(
            sa.text(
                "DROP FUNCTION IF EXISTS public."
                "authorize_and_lock_professional_invitation(text, uuid, text)"
            )
        )
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
        # Only the tables that exist at the pre-contract revision. The registry
        # describes the schema at head, and a table introduced by a later
        # revision has nothing to stamp yet.
        present = set(
            await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
        )
        for table_name, column_name in required_ownership_columns():
            if column_name != "subject_id" or table_name not in present:
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


async def restricted_engine(database_url: str):
    """A role with no special attributes, granted plain DML on every table.

    No ``BYPASSRLS``, not the table owner, not superuser — the shape the
    application connects with in a deployment where the policies are the point.
    """

    admin = create_async_engine(database_url, poolclass=NullPool)
    async with admin.begin() as connection:
        database_ident = connection.dialect.identifier_preparer.quote(
            sa.engine.make_url(database_url).database
        )
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
                f"GRANT CONNECT ON DATABASE {database_ident} TO {RESTRICTED_ROLE}"
            )
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
    restricted = await restricted_engine(database_url)
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
async def test_patient_guidance_requires_and_honours_subject_binding(
    db_session,
    monkeypatch,
):
    """The patient hub's service is empty unbound and exact once owner-bound."""

    from alembic.config import Config as AlembicConfig
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from vitals.services.care import records
    from vitals.services.access_resolution import (
        enter_subject_scope,
        resolve_access_context,
    )

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    await db_session.close()

    admin = await _migrated_engine(
        database_url, AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    )
    restricted = await restricted_engine(database_url)
    try:
        mine, theirs = await _seed_two_subjects(admin)
        async with admin.begin() as connection:
            mine_owner = await connection.scalar(
                sa.text("SELECT owner_user_id FROM health_subjects WHERE id = :id"),
                {"id": mine},
            )
            theirs_owner = await connection.scalar(
                sa.text("SELECT owner_user_id FROM health_subjects WHERE id = :id"),
                {"id": theirs},
            )
            doctor = await connection.scalar(
                sa.text(
                    "INSERT INTO users (id, username, normalized_username, "
                    "password_hash, status, created_at, updated_at) VALUES "
                    "(gen_random_uuid(), 'rls-guidance-doctor', "
                    "'rls-guidance-doctor', '$synthetic', 'active', now(), "
                    "now()) RETURNING id"
                )
            )
            for subject, owner, label in (
                (mine, mine_owner, "mine"),
                (theirs, theirs_owner, "theirs"),
            ):
                relationship = await connection.scalar(
                    sa.text(
                        "INSERT INTO care_relationships "
                        "(id, subject_id, subject_owner_user_id, "
                        "professional_user_id, kind, status, established_at, "
                        "created_at, updated_at) VALUES "
                        "(gen_random_uuid(), :subject, :owner, :doctor, "
                        "'doctor', 'active', now(), now(), now()) RETURNING id"
                    ),
                    {"subject": subject, "owner": owner, "doctor": doctor},
                )
                await connection.execute(
                    sa.text(
                        "INSERT INTO professional_notes "
                        "(id, subject_id, relationship_id, actor_user_id, body, "
                        "created_at, updated_at) VALUES "
                        "(gen_random_uuid(), :subject, :relationship, :doctor, "
                        ":body, now(), now())"
                    ),
                    {
                        "subject": subject,
                        "relationship": relationship,
                        "doctor": doctor,
                        "body": f"Guidance note {label}",
                    },
                )
                await connection.execute(
                    sa.text(
                        "INSERT INTO care_plans "
                        "(id, subject_id, relationship_id, actor_user_id, title, "
                        "body, status, effective_from, created_at, updated_at) "
                        "VALUES (gen_random_uuid(), :subject, :relationship, "
                        ":doctor, :title, :body, 'active', DATE '2026-08-26', "
                        "now(), now())"
                    ),
                    {
                        "subject": subject,
                        "relationship": relationship,
                        "doctor": doctor,
                        "title": f"Guidance plan {label}",
                        "body": f"Plan details {label}",
                    },
                )

        factory = async_sessionmaker(
            restricted, expire_on_commit=False, class_=AsyncSession
        )
        async with factory() as session:
            owner_context = await resolve_access_context(
                session,
                user_id=mine_owner,
                subject_id=None,
            )
            unbound = await records.care_guidance_summary(
                session,
                context=owner_context,
            )
            assert unbound.active_plans == ()
            assert unbound.recent_notes == ()

            await enter_subject_scope(session, owner_context)
            bound = await records.care_guidance_summary(
                session,
                context=owner_context,
            )
            assert [plan.title for plan in bound.active_plans] == [
                "Guidance plan mine"
            ]
            assert [note.body for note in bound.recent_notes] == [
                "Guidance note mine"
            ]
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

    # Every covered table, not only the first revision's: a policy that was
    # created but never forced is a policy the table owner walks straight past,
    # and the application connects as the owner.
    listed = sorted(_all_covered_tables())
    admin = await _migrated_engine(
        database_url, AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    )
    restricted = await restricted_engine(database_url)
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

    from vitals.persistence.rls import (
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
    restricted = await restricted_engine(database_url)
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


# ── The tables revision 0050 deliberately left out ───────────────────────────

def _extension_module():
    spec = importlib.util.spec_from_file_location(
        "_rev0051",
        REPOSITORY_ROOT
        / "migrations"
        / "versions"
        / "0051_row_security_for_catalogs_and_children.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_table_with_a_subject_is_now_covered_by_one_policy_or_the_other():
    """No table with a subject column may sit outside both revisions.

    A table that has the column and no policy is the failure this pair of
    migrations exists to prevent, and it would be invisible: the queries work,
    the tests pass, and the boundary simply is not there.
    """

    from vitals.models.base import Base

    with_subject = {
        name
        for name, table in Base.metadata.tables.items()
        if "subject_id" in table.columns
    }
    covered = _all_covered_tables()
    assert with_subject - covered == set()
    assert covered - with_subject == set()


#: Each inherited child and the table it takes its subject from. A child can
#: only legitimately have none when its parent can — which is why
#: ``hrt_compound_components`` shares rather than hides: a component of a
#: curated compound belongs to the installation, exactly like the compound.
_CHILD_PARENT = {
    "body_scan_metrics": "body_scans",
    "hevy_exercises": "hevy_workouts",
    "hevy_sets": "hevy_workouts",
    "hrt_cycle_items": "hrt_cycles",
    "hrt_cycle_template_items": "hrt_cycle_templates",
    "hrt_compound_components": "hrt_compounds",
}


def test_the_two_predicates_match_what_a_null_subject_means():
    """Which group a table joins follows from the registry, not from taste.

    The question each predicate answers is the same one: is a NULL subject a
    real state, or a row the backfill has not reached? Sharing a row that is
    merely unmigrated would show one person's half-copied data to the next;
    hiding a row that genuinely belongs to the installation would make the
    safety catalog invisible, and a conflict rule nobody can see stops firing.
    """

    extension = _extension_module()

    for table_name in extension.SHARED_WITH_INSTALLATION:
        spec = OWNERSHIP_REGISTRY[table_name]
        if spec.subject is TargetColumn.INHERITED:
            parent = _CHILD_PARENT[table_name]
            assert OWNERSHIP_REGISTRY[parent].subject is TargetColumn.MIXED, (
                f"{table_name} shares its NULL rows, which is only right while "
                f"its parent {parent} may legitimately have none"
            )
        else:
            assert spec.subject in (TargetColumn.MIXED, TargetColumn.OPTIONAL), (
                f"{table_name} shares rows with the installation, so a NULL "
                "subject has to be a real state, not an unfinished backfill"
            )

    for table_name in extension.INHERITED_CHILDREN:
        assert OWNERSHIP_REGISTRY[table_name].subject is TargetColumn.INHERITED
        parent = _CHILD_PARENT[table_name]
        assert OWNERSHIP_REGISTRY[parent].subject is TargetColumn.REQUIRED, (
            f"{table_name} hides its NULL rows, which is only right while its "
            f"parent {parent} must always name a subject"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_catalogs_are_shared_and_children_are_not(
    db_session,
    monkeypatch,
):
    """A curated rule is everybody's; an unstamped child is nobody's.

    Both are NULL-subject rows, and the difference between them is the whole
    reason these tables needed their own revision. Getting it backwards would
    either hide the safety catalog — a conflict rule nobody can see stops firing
    — or show one person's half-migrated workout to the next.
    """

    from alembic.config import Config as AlembicConfig

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    await db_session.close()

    admin = await _migrated_engine(
        database_url, AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    )
    restricted = await restricted_engine(database_url)
    try:
        mine, theirs = await _seed_two_subjects(admin)

        async with admin.begin() as connection:
            # A curated rule belongs to the installation; a custom one to a person.
            for subject_id, message in (
                (None, "curated"),
                (mine, "mine"),
                (theirs, "theirs"),
            ):
                await connection.execute(
                    sa.text(
                        "INSERT INTO conflict_rules (subject_id, rule_type, "
                        "domain_a, condition_a, domain_b, condition_b, severity, "
                        "message, active, created_at, updated_at) VALUES "
                        "(:subject, 'hard_block', 'genetics', '{}'::jsonb, "
                        "'supplements', '{}'::jsonb, 'block', :message, true, "
                        "now(), now())"
                    ),
                    {"subject": subject_id, "message": message},
                )
            # A workout whose children the backfill has not reached.
            workout_id = await connection.scalar(
                sa.text(
                    "INSERT INTO hevy_workouts (subject_id, "
                    "integration_connection_id, external_id, domain, source, "
                    "date, created_at, updated_at) SELECT :subject, id, "
                    "'w-rls', 'workouts', 'hevy_api', current_date, now(), now() "
                    "FROM integration_connections WHERE subject_id = :subject "
                    "LIMIT 1 RETURNING id"
                ),
                {"subject": mine},
            )
            if workout_id is not None:
                await connection.execute(
                    sa.text(
                        "INSERT INTO hevy_exercises (subject_id, workout_id, "
                        "exercise_index, title, created_at, updated_at) VALUES "
                        "(NULL, :workout, 0, 'Bench', now(), now())"
                    ),
                    {"workout": workout_id},
                )

        async with restricted.connect() as connection:
            await connection.execute(
                sa.text("SELECT set_config(:name, :value, false)"),
                {"name": SUBJECT_SETTING, "value": str(mine)},
            )
            visible = {
                row.message
                for row in (
                    await connection.execute(
                        sa.text("SELECT message FROM conflict_rules")
                    )
                ).all()
            }
            # The checked-in catalog is seeded by the migrations, so the table
            # holds more than these three; what matters is which of them landed.
            assert {"curated", "mine"} <= visible
            assert "theirs" not in visible, (
                "the installation's catalog is shared; another person's rule is not"
            )
            # And the real curated catalog came through with it — a safety rule
            # nobody can see is a safety rule that stops firing.
            assert len(visible) > 2

            if workout_id is not None:
                unstamped = await connection.scalar(
                    sa.text("SELECT count(*) FROM hevy_exercises")
                )
                assert unstamped == 0, (
                    "a child the backfill has not reached belongs to nobody yet"
                )
    finally:
        await restricted.dispose()
        await admin.dispose()


# ── The other thing a transaction can be acting for ──────────────────────────
# Revision 0053 adds a second way past the subject comparison, which deserves
# more suspicion than the first. What follows pins that it is narrow: named
# explicitly, transaction-local like the subject, and reachable only from a
# list a reviewer can read.


def _platform_module():
    spec = importlib.util.spec_from_file_location(
        "_rev0053",
        REPOSITORY_ROOT
        / "migrations"
        / "versions"
        / "0053_platform_scope_in_row_security.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _invitation_authorization_module():
    spec = importlib.util.spec_from_file_location(
        "_rev0081",
        REPOSITORY_ROOT
        / "migrations"
        / "versions"
        / "0081_authorize_professional_invitation.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_invitation_authorization_names_one_exact_runtime_routine():
    from scripts.provision_runtime_db_role import RUNTIME_EXECUTE_ROUTINES
    from vitals.services.care.invitations import POSTGRES_AUTHORIZATION_ROUTINE

    migration = _invitation_authorization_module()
    expected = migration.ROUTINE_SIGNATURE.replace(" ", "")
    assert POSTGRES_AUTHORIZATION_ROUTINE.replace(" ", "") == expected
    assert RUNTIME_EXECUTE_ROUTINES == (expected,)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_invitation_authorization_binds_only_the_proven_subject(
    db_session,
    monkeypatch,
):
    """The definer bridge returns one proven root and never opens the platform."""

    import asyncio
    from datetime import timedelta

    from alembic.config import Config as AlembicConfig
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from vitals.enums import (
        CareRelationshipStatus,
        ProfessionalInvitationStatus,
        ProfessionalKind,
        ProfessionalVerificationStatus,
        UserRoleName,
        UserStatus,
    )
    from vitals.models.identity import HealthSubject, User, UserRole
    from vitals.models.professional import CareRelationship, ProfessionalProfile
    from vitals.persistence.rls import bound_subject, in_platform_scope
    from vitals.services.care import invitations, relationships
    from vitals.utils.timeutils import now_utc

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    await db_session.close()

    admin = await _migrated_engine(
        database_url, AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    )
    restricted = await restricted_engine(database_url)
    migration = _invitation_authorization_module()
    signature = migration.ROUTINE_SIGNATURE.replace(" ", "")
    try:
        admin_factory = async_sessionmaker(
            admin, expire_on_commit=False, class_=AsyncSession
        )
        async with admin_factory() as seed:
            owner = User(
                username="rls-invitation-owner",
                normalized_username="rls-invitation-owner",
                email="rls-owner@example.test",
                normalized_email="rls-owner@example.test",
                email_verified_at=now_utc(),
                password_hash="$synthetic",
                status=UserStatus.ACTIVE.value,
            )
            other_owner = User(
                username="rls-invitation-other-owner",
                normalized_username="rls-invitation-other-owner",
                password_hash="$synthetic",
                status=UserStatus.ACTIVE.value,
            )
            doctor = User(
                username="rls-invitation-doctor",
                normalized_username="rls-invitation-doctor",
                email="rls-doctor@example.test",
                normalized_email="rls-doctor@example.test",
                email_verified_at=now_utc(),
                password_hash="$synthetic",
                status=UserStatus.ACTIVE.value,
            )
            suspended = User(
                username="rls-invitation-suspended",
                normalized_username="rls-invitation-suspended",
                email="suspended-doctor@example.test",
                normalized_email="suspended-doctor@example.test",
                email_verified_at=now_utc(),
                password_hash="$synthetic",
                status=UserStatus.SUSPENDED.value,
            )
            concurrent_doctor = User(
                username="rls-invitation-concurrent-doctor",
                normalized_username="rls-invitation-concurrent-doctor",
                email="concurrent-doctor@example.test",
                normalized_email="concurrent-doctor@example.test",
                email_verified_at=now_utc(),
                password_hash="$synthetic",
                status=UserStatus.ACTIVE.value,
            )
            borrower = User(
                username="rls-invitation-borrower",
                normalized_username="rls-invitation-borrower",
                email="borrower@example.test",
                normalized_email="borrower@example.test",
                email_verified_at=now_utc(),
                password_hash="$synthetic",
                status=UserStatus.ACTIVE.value,
            )
            unverified_email_user = User(
                username="rls-invitation-unverified-email",
                normalized_username="rls-invitation-unverified-email",
                email="unverified-doctor@example.test",
                normalized_email="unverified-doctor@example.test",
                email_verified_at=None,
                password_hash="$synthetic",
                status=UserStatus.ACTIVE.value,
            )
            seed.add_all(
                (
                    owner,
                    other_owner,
                    doctor,
                    suspended,
                    concurrent_doctor,
                    borrower,
                    unverified_email_user,
                )
            )
            await seed.flush()
            subject = HealthSubject(
                owner_user_id=owner.id,
                display_name="Invitation subject",
                timezone="Asia/Almaty",
            )
            other_subject = HealthSubject(
                owner_user_id=other_owner.id,
                display_name="Other invitation subject",
                timezone="Asia/Almaty",
            )
            seed.add_all((subject, other_subject))
            await seed.flush()
            for qualified_user, display_name in (
                (owner, "Dr Owner RLS"),
                (doctor, "Dr RLS"),
                (suspended, "Dr Suspended Account RLS"),
                (concurrent_doctor, "Dr Concurrent RLS"),
                (borrower, "Dr Borrower RLS"),
                (unverified_email_user, "Dr Unverified Email RLS"),
            ):
                seed.add(
                    UserRole(
                        user_id=qualified_user.id,
                        role=UserRoleName.DOCTOR.value,
                    )
                )
                seed.add(
                    ProfessionalProfile(
                        user_id=qualified_user.id,
                        kind=ProfessionalKind.DOCTOR.value,
                        verification_status=(
                            ProfessionalVerificationStatus.VERIFIED.value
                        ),
                        display_name=display_name,
                        verified_at=now_utc(),
                        verified_by_user_id=owner.id,
                    )
                )
            issued = await invitations.invite(
                seed,
                subject_id=subject.id,
                actor_user_id=owner.id,
                kind=ProfessionalKind.DOCTOR,
                email="rls-doctor@example.test",
            )
            other_issued = await invitations.invite(
                seed,
                subject_id=other_subject.id,
                actor_user_id=other_owner.id,
                kind=ProfessionalKind.DOCTOR,
                email="rls-doctor@example.test",
            )
            expired_issued = await invitations.invite(
                seed,
                subject_id=subject.id,
                actor_user_id=owner.id,
                kind=ProfessionalKind.DOCTOR,
                email="expired-doctor@example.test",
            )
            lapsed = now_utc() - timedelta(days=30)
            expired_issued.invitation.created_at = lapsed
            expired_issued.invitation.expires_at = lapsed + timedelta(days=7)
            concurrent_issued = await invitations.invite(
                seed,
                subject_id=other_subject.id,
                actor_user_id=other_owner.id,
                kind=ProfessionalKind.DOCTOR,
                email="concurrent-doctor@example.test",
            )
            self_issued = await invitations.invite(
                seed,
                subject_id=subject.id,
                actor_user_id=owner.id,
                kind=ProfessionalKind.DOCTOR,
                email="rls-owner@example.test",
            )
            suspended_issued = await invitations.invite(
                seed,
                subject_id=subject.id,
                actor_user_id=owner.id,
                kind=ProfessionalKind.DOCTOR,
                email="suspended-doctor@example.test",
            )
            unverified_email_issued = await invitations.invite(
                seed,
                subject_id=subject.id,
                actor_user_id=owner.id,
                kind=ProfessionalKind.DOCTOR,
                email="unverified-doctor@example.test",
            )
            # A valid identity proof reaches relationship creation and then
            # fails deterministically on the existing live pair.  That proves
            # the invitation mutation still rolls back after the definer gate.
            seed.add(
                CareRelationship(
                    subject_id=other_subject.id,
                    subject_owner_user_id=other_owner.id,
                    professional_user_id=doctor.id,
                    kind=ProfessionalKind.DOCTOR.value,
                    status=CareRelationshipStatus.ACTIVE.value,
                )
            )
            await seed.commit()
            subject_id = subject.id
            other_subject_id = other_subject.id
            doctor_id = doctor.id
            suspended_id = suspended.id
            owner_id = owner.id
            concurrent_doctor_id = concurrent_doctor.id
            borrower_id = borrower.id
            unverified_email_user_id = unverified_email_user.id
            invitation_id = issued.invitation.id
            other_invitation_id = other_issued.invitation.id
            expired_invitation_id = expired_issued.invitation.id
            concurrent_invitation_id = concurrent_issued.invitation.id
            self_invitation_id = self_issued.invitation.id
            suspended_invitation_id = suspended_issued.invitation.id
            unverified_email_invitation_id = unverified_email_issued.invitation.id
            token = issued.token
            other_token = other_issued.token
            expired_token = expired_issued.token
            concurrent_token = concurrent_issued.token
            self_token = self_issued.token
            suspended_token = suspended_issued.token
            unverified_email_token = unverified_email_issued.token

        async with admin.begin() as connection:
            catalog = (
                await connection.execute(
                    sa.text(
                        "SELECT owner.rolname, routine.prosecdef, "
                        "routine.provolatile, routine.proconfig, "
                        "NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE("
                        "routine.proacl, acldefault('f', routine.proowner))) acl "
                        "WHERE acl.grantee=0 "
                        "AND upper(acl.privilege_type)='EXECUTE') AS no_public "
                        "FROM pg_proc routine "
                        "JOIN pg_roles owner ON owner.oid=routine.proowner "
                        "WHERE routine.oid=to_regprocedure(:signature)"
                    ),
                    {"signature": signature},
                )
            ).one()
            assert catalog.rolname == sa.engine.make_url(database_url).username
            assert catalog.prosecdef
            assert catalog.provolatile in ("v", b"v")
            assert "search_path=pg_catalog, pg_temp" in catalog.proconfig
            assert "row_security=off" in catalog.proconfig
            assert catalog.no_public
            assert not await connection.scalar(
                sa.text(
                    "SELECT has_function_privilege("
                    ":role, :signature, 'EXECUTE')"
                ),
                {"role": RESTRICTED_ROLE, "signature": signature},
            )
            await connection.exec_driver_sql(
                "GRANT EXECUTE ON FUNCTION "
                f"{migration.ROUTINE_SIGNATURE} TO {RESTRICTED_ROLE}"
            )

        restricted_factory = async_sessionmaker(
            restricted, expire_on_commit=False, class_=AsyncSession
        )

        async def assert_refused_without_binding(
            *,
            attempted_token: str,
            user_id: uuid.UUID,
            email: str,
            pending_invitation_id: uuid.UUID | None = None,
        ) -> None:
            async with restricted_factory() as refused:
                assert await refused.scalar(
                    sa.text(
                        "SELECT count(*) FROM public.professional_invitations"
                    )
                ) == 0
                with pytest.raises(invitations.InvitationRefused):
                    await invitations.accept(
                        refused,
                        token=attempted_token,
                        accepting_user_id=user_id,
                        verified_email=email,
                    )
                assert bound_subject(refused) is None
                assert not in_platform_scope(refused)
                assert await refused.scalar(
                    sa.text(
                        "SELECT count(*) FROM public.professional_invitations"
                    )
                ) == 0
                await refused.rollback()
            if pending_invitation_id is not None:
                async with admin.connect() as verification:
                    assert await verification.scalar(
                        sa.text(
                            "SELECT status FROM public.professional_invitations "
                            "WHERE id=:invitation_id"
                        ),
                        {"invitation_id": pending_invitation_id},
                    ) == ProfessionalInvitationStatus.PENDING.value

        await assert_refused_without_binding(
            attempted_token="never-issued-token",
            user_id=doctor_id,
            email="rls-doctor@example.test",
        )
        await assert_refused_without_binding(
            attempted_token=token,
            user_id=borrower_id,
            email="rls-doctor@example.test",
            pending_invitation_id=invitation_id,
        )
        await assert_refused_without_binding(
            attempted_token=unverified_email_token,
            user_id=unverified_email_user_id,
            email="unverified-doctor@example.test",
            pending_invitation_id=unverified_email_invitation_id,
        )
        await assert_refused_without_binding(
            attempted_token=suspended_token,
            user_id=suspended_id,
            email="suspended-doctor@example.test",
            pending_invitation_id=suspended_invitation_id,
        )
        await assert_refused_without_binding(
            attempted_token=self_token,
            user_id=owner_id,
            email="rls-owner@example.test",
            pending_invitation_id=self_invitation_id,
        )
        await assert_refused_without_binding(
            attempted_token=expired_token,
            user_id=doctor_id,
            email="expired-doctor@example.test",
            pending_invitation_id=expired_invitation_id,
        )

        async with admin.begin() as connection:
            await connection.execute(
                sa.text(
                    "DELETE FROM public.user_roles "
                    "WHERE user_id=:user_id AND role='doctor'"
                ),
                {"user_id": doctor_id},
            )
        await assert_refused_without_binding(
            attempted_token=token,
            user_id=doctor_id,
            email="rls-doctor@example.test",
            pending_invitation_id=invitation_id,
        )
        async with admin.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO public.user_roles (id, user_id, role, "
                    "assigned_at) VALUES (gen_random_uuid(), :user_id, "
                    "'doctor', now())"
                ),
                {"user_id": doctor_id},
            )

        async with admin.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE public.user_roles SET role='trainer' "
                    "WHERE user_id=:user_id AND role='doctor'"
                ),
                {"user_id": doctor_id},
            )
        await assert_refused_without_binding(
            attempted_token=token,
            user_id=doctor_id,
            email="rls-doctor@example.test",
            pending_invitation_id=invitation_id,
        )
        async with admin.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE public.user_roles SET role='doctor' "
                    "WHERE user_id=:user_id AND role='trainer'"
                ),
                {"user_id": doctor_id},
            )

        async def set_doctor_profile(*, kind: str, status: str) -> None:
            async with admin.begin() as connection:
                await connection.execute(
                    sa.text(
                        "UPDATE public.professional_profiles SET kind=:kind, "
                        "verification_status=CAST(:status AS varchar), "
                        "verified_at=CASE WHEN CAST(:status AS varchar)="
                        "'verified' "
                        "THEN now() ELSE NULL END, "
                        "verified_by_user_id=CASE WHEN CAST(:status AS varchar)="
                        "'verified' "
                        "THEN CAST(:reviewer_id AS uuid) ELSE NULL::uuid END, "
                        "review_note=CASE WHEN CAST(:status AS varchar)="
                        "'suspended' "
                        "THEN 'Synthetic suspension' ELSE NULL END "
                        "WHERE user_id=:user_id"
                    ),
                    {
                        "kind": kind,
                        "reviewer_id": owner_id,
                        "status": status,
                        "user_id": doctor_id,
                    },
                )

        for profile_kind, profile_status in (
            (
                ProfessionalKind.DOCTOR.value,
                ProfessionalVerificationStatus.PENDING.value,
            ),
            (
                ProfessionalKind.DOCTOR.value,
                ProfessionalVerificationStatus.SUSPENDED.value,
            ),
            (
                ProfessionalKind.TRAINER.value,
                ProfessionalVerificationStatus.VERIFIED.value,
            ),
        ):
            await set_doctor_profile(kind=profile_kind, status=profile_status)
            await assert_refused_without_binding(
                attempted_token=token,
                user_id=doctor_id,
                email="rls-doctor@example.test",
                pending_invitation_id=invitation_id,
            )
            await set_doctor_profile(
                kind=ProfessionalKind.DOCTOR.value,
                status=ProfessionalVerificationStatus.VERIFIED.value,
            )

        async with restricted_factory() as rolled_back:
            accepted_before_relationship_failure = await invitations.accept(
                rolled_back,
                token=other_token,
                accepting_user_id=doctor_id,
                verified_email="rls-doctor@example.test",
            )
            with pytest.raises(relationships.CareError):
                await relationships.establish_from_invitation(
                    rolled_back,
                    invitation=accepted_before_relationship_failure,
                )
            await rolled_back.rollback()

        async with restricted_factory() as accepting:
            accepted = await invitations.accept(
                accepting,
                token=token,
                accepting_user_id=doctor_id,
                verified_email="rls-doctor@example.test",
            )
            assert accepted.id == invitation_id
            assert bound_subject(accepting) == subject_id
            assert not in_platform_scope(accepting)
            relationship = await relationships.establish_from_invitation(
                accepting, invitation=accepted
            )
            assert relationship.subject_id == subject_id
            await accepting.commit()

        await assert_refused_without_binding(
            attempted_token=token,
            user_id=doctor_id,
            email="rls-doctor@example.test",
        )

        async def race_acceptance() -> str:
            async with restricted_factory() as racing:
                try:
                    accepted = await invitations.accept(
                        racing,
                        token=concurrent_token,
                        accepting_user_id=concurrent_doctor_id,
                        verified_email="concurrent-doctor@example.test",
                    )
                    await relationships.establish_from_invitation(
                        racing, invitation=accepted
                    )
                    await racing.commit()
                    return "accepted"
                except invitations.InvitationRefused:
                    await racing.rollback()
                    assert bound_subject(racing) is None
                    assert not in_platform_scope(racing)
                    return "refused"

        race_results = await asyncio.gather(race_acceptance(), race_acceptance())
        assert sorted(race_results) == ["accepted", "refused"]

        async with admin.connect() as verification:
            statuses = dict(
                (
                    await verification.execute(
                        sa.text(
                            "SELECT id, status FROM "
                            "public.professional_invitations "
                            "WHERE id IN (:accepted_id, :other_id, "
                            ":expired_id, :concurrent_id)"
                        ),
                        {
                            "accepted_id": invitation_id,
                            "other_id": other_invitation_id,
                            "expired_id": expired_invitation_id,
                            "concurrent_id": concurrent_invitation_id,
                        },
                    )
                ).all()
            )
            assert statuses[invitation_id] == (
                ProfessionalInvitationStatus.ACCEPTED.value
            )
            assert statuses[other_invitation_id] == (
                ProfessionalInvitationStatus.PENDING.value
            )
            assert statuses[expired_invitation_id] == (
                ProfessionalInvitationStatus.PENDING.value
            )
            assert statuses[concurrent_invitation_id] == (
                ProfessionalInvitationStatus.ACCEPTED.value
            )
            relationship_rows = (
                await verification.execute(
                    sa.text(
                        "SELECT subject_id, invitation_id FROM "
                        "public.care_relationships "
                        "WHERE invitation_id IS NOT NULL"
                    )
                )
            ).all()
            assert set(relationship_rows) == {
                (subject_id, invitation_id),
                (other_subject_id, concurrent_invitation_id),
            }
    finally:
        await restricted.dispose()
        await admin.dispose()


def test_the_platform_setting_is_named_the_same_in_both_halves():
    """Same contract as the subject: the policy reads what the session writes."""

    from vitals.persistence.rls import PLATFORM_SETTING

    assert _platform_module().PLATFORM_SETTING == PLATFORM_SETTING


def test_the_rewrite_reaches_every_policy_the_two_revisions_installed():
    """A policy left on the old predicate is a table the jobs cannot sweep.

    The rewrite derives its list from revisions 0050 and 0051 rather than
    repeating it, so this asserts the derivation covers all three groups — and
    that each keeps the predicate that matches what a NULL subject means there.
    """

    rows = _platform_module()._tables()
    covered = {name for name, _, _ in rows}

    isolated = set(_revision_module().SUBJECT_ISOLATED_TABLES)
    children = set(_extension_module().INHERITED_CHILDREN)
    shared = set(_extension_module().SHARED_WITH_INSTALLATION)
    assert covered == isolated | children | shared
    assert len(rows) == len(covered), "a table was rewritten twice"

    for name, before, after in rows:
        # The subject comparison survives; the platform clause is added to it.
        assert before in after or "subject_id IS NULL" in after
        assert "vitals.platform_scope" in after
        # Only the shared group keeps treating a NULL subject as a real state.
        assert ("subject_id IS NULL" in after) == (name in shared)


def test_only_a_named_list_of_callers_may_enter_the_platform_scope():
    """The list is the review surface. A new caller has to be added here first.

    Row security is worth having only while the ways past it are countable.
    Each of these is subject-less for a reason that is about the work itself:
    a visitor with a token has no account to bind, and a sweep across everybody
    has no one person to act as. A path that merely *forgot* to resolve its
    subject belongs nowhere near this list — for that one, seeing nothing is
    the correct outcome and the fix is to bind.
    """

    import ast

    permitted = {
        # The visitor holding a published link has no account; the token is the
        # authorization, checked by share_service itself before anything is read.
        ("vitals/services/share_service.py", "resolve_public"),
        ("vitals/services/share_service.py", "register_open"),
        # An admitted member has no subject-bound session yet. This helper is
        # reached only after an already locked, single-use invitation/request
        # proof has passed, and uses the scope only to materialize that one new
        # account and subject graph; it is not a cross-subject reader.
        (
            "vitals/services/authentication/admission/_shared.py",
            "provision_and_link",
        ),
        # The compatibility startup transaction must discover the sole durable
        # subject before it can bind one. Its identity, connection, settings,
        # and preference reconciliation is bounded by one commit/rollback.
        ("web/main.py", "_bootstrap_legacy_identity"),
        # The standalone worker has no authenticated user and must discover the
        # exact-one compatibility preference scope before it can bind one. It
        # returns only the flattened schedule and releases the read locks with a
        # rollback before any scheduled job starts.
        ("vitals/scheduler/lifecycle.py", "load_worker_settings"),
        # Housekeeping across every subject, with no person to act as.
        ("vitals/services/share_service.py", "purge_job"),
        ("vitals/services/ai_gateway_service.py", "reconciliation_job"),
        ("vitals/services/proactive/delivery.py", "delivery_reconciliation_job"),
        ("vitals/services/notifications/care_push_dispatcher.py", "dispatch_job"),
        (
            "vitals/services/authentication/admission/retention.py",
            "maintenance_job",
        ),
        # Provider fan-out has no subject until it enumerates live account
        # roots. Every returned account is then processed in a separate
        # subject-bound job transaction.
        ("vitals/scheduler/fanout.py", "_list_provider_accounts"),
        # An administrator's own console: their live grants and unanswered asks
        # span every record that answered one, so there is no single subject to
        # bind and binding one would answer a different question. Both queries
        # name this admin, and both return frozen values rather than rows, so
        # nothing reachable under the open scope leaves the function.
        ("vitals/services/support_access_service.py", "console_for_admin"),
        # The list of records an ask may name. Same list ``/settings/platform/ai``
        # already shows an administrator, and for the same reason: choosing whose
        # record to investigate must be a choice from an auditable list, not a
        # free-text search that finds a patient by name.
        ("vitals/services/support_access_service.py", "reachable_subjects"),
        # Fixed-target operator cutover. It must enumerate every legacy file
        # root across subjects; output is aggregate-only and every changed row
        # is independently committed with an append-only audit event.
        ("vitals/operations/file_storage_relocation.py", "inspect"),
        ("vitals/operations/file_storage_relocation.py", "_commit_one"),
    }

    def _enclosing(tree):
        """Attribute each call to the innermost function containing it."""

        found = set()
        stack: list[tuple] = [(tree, None)]
        while stack:
            node, owner = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = node.name
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "enter_platform_scope"
            ):
                found.add(owner)
            stack.extend((child, owner) for child in ast.iter_child_nodes(node))
        return found

    actual = set()
    for path in sorted(
        list(Path("vitals").rglob("*.py")) + list(Path("web").rglob("*.py"))
    ):
        source = path.read_text()
        if "enter_platform_scope" not in source or path.name == "rls_session.py":
            continue
        for name in _enclosing(ast.parse(source)):
            actual.add((path.as_posix(), name))

    added = sorted(actual - permitted)
    assert not added, (
        f"new callers of enter_platform_scope: {added} — each one is a path that "
        "reads across every subject. Add it here with the reason it cannot bind "
        "one, or resolve a subject and bind instead"
    )
    gone = sorted(permitted - actual)
    assert not gone, (
        f"no longer enters the platform scope: {gone} — if that is deliberate, "
        "drop it from the list; if not, the path now reads nothing under RLS"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_platform_scope_reaches_across_subjects(
    db_session,
    monkeypatch,
):
    """What the published-link path needs, and what it must not become.

    Revision 0050 covered ``shared_reports``, and the visitor who opens a
    published link has no account to bind — so the policy matched nothing and
    every doctor link answered "not found", indistinguishable from revoked. The
    scope is the fix. It has to reach every subject to be any use, and it has to
    be something a session asks for by name rather than a state it can drift
    into, or it is just row security switched off.
    """

    from alembic.config import Config as AlembicConfig

    from vitals.persistence.rls import PLATFORM_SETTING

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    await db_session.close()

    admin = await _migrated_engine(
        database_url, AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    )
    restricted = await restricted_engine(database_url)
    try:
        first, second = await _seed_two_subjects(admin)

        async with restricted.connect() as connection:
            # Not asking for it changes nothing: still nothing, not everything.
            assert await connection.scalar(
                sa.text("SELECT count(*) FROM supplements")
            ) == 0

            await connection.execute(
                sa.text("SELECT set_config(:name, 'on', false)"),
                {"name": PLATFORM_SETTING},
            )
            visible = {
                row.subject_id
                for row in (
                    await connection.execute(
                        sa.text("SELECT subject_id FROM supplements")
                    )
                ).all()
            }
            assert {first, second} <= visible

            # Any other value is not the scope. Only the exact string opens it,
            # so a stray setting or a truncated one closes rather than opens.
            for value in ("", "off", "ON", "true", "1"):
                await connection.execute(
                    sa.text("SELECT set_config(:name, :value, false)"),
                    {"name": PLATFORM_SETTING, "value": value},
                )
                assert await connection.scalar(
                    sa.text("SELECT count(*) FROM supplements")
                ) == 0, f"{value!r} must not read as the platform scope"
    finally:
        await restricted.dispose()
        await admin.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_platform_scope_is_transaction_local(
    db_session,
    monkeypatch,
):
    """It ends with the transaction, and the session re-declares the next one.

    Same property the subject binding has, and it matters more here: this scope
    reads across everybody, so one leaking onto a pooled connection would hand
    the next request the whole installation. The listener re-applying it is what
    lets a job commit mid-sweep without either losing the scope or extending it.
    """

    from alembic.config import Config as AlembicConfig

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from vitals.persistence.rls import (
        PLATFORM_SETTING,
        enter_platform_scope,
        in_platform_scope,
    )

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    await db_session.close()

    admin = await _migrated_engine(
        database_url, AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    )
    restricted = await restricted_engine(database_url)
    try:
        await _seed_two_subjects(admin)
        factory = async_sessionmaker(
            restricted, expire_on_commit=False, class_=AsyncSession
        )

        async with factory() as session:
            await enter_platform_scope(session)
            assert in_platform_scope(session)
            assert await session.scalar(
                sa.text("SELECT count(*) FROM supplements")
            ) == 2

            # A sweep commits as it goes; the listener re-declares the scope on
            # the transaction that opens next.
            await session.commit()
            assert await session.scalar(
                sa.text("SELECT count(*) FROM supplements")
            ) == 2

        # A fresh session on the same pooled connection starts closed. This is
        # the leak the transaction-local setting exists to prevent.
        async with factory() as session:
            assert not in_platform_scope(session)
            assert await session.scalar(
                sa.text(f"SELECT current_setting('{PLATFORM_SETTING}', true)")
            ) in (None, "")
            assert await session.scalar(
                sa.text("SELECT count(*) FROM supplements")
            ) == 0
    finally:
        await restricted.dispose()
        await admin.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_fanout_discovers_every_account_under_forced_rls(
    db_session,
    monkeypatch,
):
    """A restricted worker discovers both accounts, then runs each once.

    ``integration_connections`` is FORCE-RLS protected. Before discovery
    entered the installation scope, this runtime role returned an empty list
    and provider schedules reported success without syncing anybody.
    """

    from alembic.config import Config as AlembicConfig
    from cryptography.fernet import Fernet
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from vitals.enums import IntegrationProvider
    from vitals.persistence.rls import bind_session_subject
    from vitals.scheduler import fanout
    from vitals.services import (
        credential_vault_service,
        provider_credentials_service,
    )

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    monkeypatch.setenv(
        credential_vault_service.CREDENTIAL_KEY_ENV,
        Fernet.generate_key().decode("ascii"),
    )
    await db_session.close()

    admin = await _migrated_engine(
        database_url, AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    )
    restricted = await restricted_engine(database_url)
    try:
        first, second = await _seed_two_subjects(admin)
        corrupt_subject, valid_subject = sorted((first, second))
        valid_ciphertext = credential_vault_service.encrypt_mapping(
            {"email": "synthetic@example.test", "password": "synthetic-password"}
        )
        async with admin.begin() as connection:
            for index, subject_id in enumerate(
                (corrupt_subject, valid_subject), start=1
            ):
                connection_id = await connection.scalar(
                    sa.text(
                        "INSERT INTO integration_connections "
                        "(id, subject_id, provider, connection_type, "
                        "external_account_discriminator, credential_ref, "
                        "status, created_at, updated_at) VALUES "
                        "(gen_random_uuid(), :subject, 'garmin', 'account', "
                        ":discriminator, 'vault:v1', 'active', now(), now()) "
                        "RETURNING id"
                    ),
                    {
                        "subject": subject_id,
                        "discriminator": f"synthetic-rls-{index}",
                    },
                )
                await connection.execute(
                    sa.text(
                        "INSERT INTO integration_credentials "
                        "(integration_connection_id, subject_id, key_version, "
                        "ciphertext, created_at, updated_at) VALUES "
                        "(:connection, :subject, 1, :ciphertext, now(), now())"
                    ),
                    {
                        "connection": connection_id,
                        "subject": subject_id,
                        "ciphertext": (
                            b"synthetic-corrupt-ciphertext"
                            if subject_id == corrupt_subject
                            else valid_ciphertext
                        ),
                    },
                )

        factory = async_sessionmaker(
            restricted, expire_on_commit=False, class_=AsyncSession
        )
        seen: list[uuid.UUID] = []
        configured: list[uuid.UUID] = []

        async def job(
            _factory,
            _redis,
            *,
            subject_id,
            integration_connection_id,
        ):
            assert isinstance(integration_connection_id, uuid.UUID)
            seen.append(subject_id)
            async with _factory() as session:
                await bind_session_subject(session, subject_id)
                account = await provider_credentials_service.resolve_garmin_account(
                    session,
                    subject_id=subject_id,
                )
                assert account is not None
                assert account.integration_connection_id == integration_connection_id
                if account.configured:
                    configured.append(subject_id)

        async def ignore_outcome(*_args, **_kwargs):
            return None

        monkeypatch.setattr(fanout, "_record_outcome_for", ignore_outcome)
        with pytest.raises(credential_vault_service.CredentialVaultCorrupt):
            await fanout.for_each_connection(
                job,
                job_id="garmin_sync",
                provider=IntegrationProvider.GARMIN,
            )(factory)

        assert seen == [corrupt_subject, valid_subject]
        assert configured == [valid_subject]
    finally:
        await restricted.dispose()
        await admin.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_postgres_support_disclosure_and_patient_history_bind_one_subject(
    db_session,
    monkeypatch,
):
    """The support path stays useful under FORCE RLS without platform scope."""

    from alembic.config import Config as AlembicConfig
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from vitals.enums import Domain, UserRoleName, UserStatus
    from vitals.models.identity import HealthSubject, User, UserRole
    from vitals.persistence.rls import bind_session_subject, in_platform_scope
    from vitals.services import support_access_service as support
    from vitals.services.access_resolution import resolve_access_context
    from web.auth import create_federated_session
    from web.care_context import require_care_context
    from web.config import SESSION_COOKIE
    from starlette.requests import Request

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    await db_session.close()

    admin_engine = await _migrated_engine(
        database_url, AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini"))
    )
    restricted = await restricted_engine(database_url)
    admin_factory = async_sessionmaker(
        admin_engine, expire_on_commit=False, class_=AsyncSession
    )
    restricted_factory = async_sessionmaker(
        restricted, expire_on_commit=False, class_=AsyncSession
    )
    try:
        async with admin_factory() as seed:
            owner = User(
                username="rls-support-owner",
                normalized_username="rls-support-owner",
                password_hash="$synthetic",
                status=UserStatus.ACTIVE.value,
            )
            operator = User(
                username="rls-support-operator",
                normalized_username="rls-support-operator",
                password_hash="$synthetic",
                status=UserStatus.ACTIVE.value,
            )
            seed.add_all((owner, operator))
            await seed.flush()
            seed.add(
                UserRole(
                    user_id=operator.id,
                    role=UserRoleName.PLATFORM_SUPERADMIN.value,
                )
            )
            subject = HealthSubject(
                owner_user_id=owner.id,
                display_name="RLS support patient",
                timezone="Asia/Almaty",
            )
            seed.add(subject)
            await seed.flush()
            request = await support.open_request(
                seed,
                admin_user_id=operator.id,
                subject_id=subject.id,
                reason="Synthetic RLS support check.",
                scopes=support.read_scopes_for((Domain.LABS,)),
            )
            grant = await support.approve_request(
                seed, owner_user_id=owner.id, request_id=request.id
            )
            second_request = await support.open_request(
                seed,
                admin_user_id=operator.id,
                subject_id=subject.id,
                reason="Second synthetic RLS support check.",
                scopes=support.read_scopes_for((Domain.NUTRITION,)),
            )
            await support.approve_request(
                seed, owner_user_id=owner.id, request_id=second_request.id
            )
            owner_id = owner.id
            operator_id = operator.id
            subject_id = subject.id
            grant_id = grant.id
            await seed.commit()

        async with restricted_factory() as disclosure:
            unbound = await resolve_access_context(
                disclosure, user_id=operator_id, subject_id=subject_id
            )
            assert unbound.support_grant is None

            cookie = create_federated_session(
                username="rls-support-operator",
                user_id=operator_id,
                session_version=1,
                authenticated_at=None,
                subject_id=None,
            )
            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": f"/care/{subject_id}",
                    "query_string": f"support_grant_id={grant_id}".encode("ascii"),
                    "headers": [
                        (
                            b"cookie",
                            f"{SESSION_COOKIE}={cookie}".encode("ascii"),
                        )
                    ],
                }
            )
            care = await require_care_context(
                subject_id=subject_id,
                request=request,
                db=disclosure,
                _username="rls-support-operator",
            )
            context = care.access
            assert context.support_grant is not None
            assert context.support_grant.grant_id == grant_id
            assert {scope.resource_key for scope in context.support_grant.scopes} == {
                Domain.LABS.value
            }
            assert not in_platform_scope(disclosure)
            await support.record_record_opened(
                disclosure,
                context=context,
                domain_keys=(Domain.LABS.value,),
            )
            await disclosure.commit()

        async with restricted_factory() as patient:
            owner_context = await resolve_access_context(
                patient, user_id=owner_id, subject_id=None
            )
            await bind_session_subject(patient, owner_context.subject_id)
            history = await support.record_opened_history(
                patient, subject_id=subject_id
            )
            assert len(history.events) == 1
            assert history.events[0].actor_username == "rls-support-operator"
            assert history.events[0].scope_keys == ("domain:labs",)
            assert not in_platform_scope(patient)

        async with restricted_factory() as asking:
            await bind_session_subject(asking, subject_id)
            pending = await support.open_request(
                asking,
                admin_user_id=operator_id,
                subject_id=subject_id,
                reason="Synthetic request to withdraw under RLS.",
                scopes=support.read_scopes_for((Domain.LABS,)),
            )
            pending_id = pending.id
            await asking.commit()

        async with restricted_factory() as withdrawing:
            await bind_session_subject(withdrawing, subject_id)
            await support.withdraw_request(
                withdrawing,
                admin_user_id=operator_id,
                request_id=pending_id,
            )
            await withdrawing.commit()

        async with restricted_factory() as handing_back:
            await bind_session_subject(handing_back, subject_id)
            await support.revoke_grant(
                handing_back,
                actor_user_id=operator_id,
                grant_id=grant_id,
                reason="Synthetic operator hand-back under RLS.",
            )
            await handing_back.commit()
    finally:
        await restricted.dispose()
        await admin_engine.dispose()
