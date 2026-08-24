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


def test_the_platform_setting_is_named_the_same_in_both_halves():
    """Same contract as the subject: the policy reads what the session writes."""

    from vitals.services.rls_session import PLATFORM_SETTING

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
        # The professional accepting an invitation is not bound to this subject
        # yet — that is what accepting is for — and the token is what authorizes
        # reading the row at all.
        ("vitals/services/invitation_service.py", "accept"),
        # Housekeeping across every subject, with no person to act as.
        ("vitals/services/share_service.py", "purge_job"),
        ("vitals/services/ai_gateway_service.py", "reconciliation_job"),
        ("vitals/services/proactive/delivery.py", "delivery_reconciliation_job"),
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

    from vitals.services.rls_session import PLATFORM_SETTING

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

    from vitals.services.rls_session import (
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
