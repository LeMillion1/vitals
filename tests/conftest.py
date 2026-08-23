"""Shared test fixtures (shaped like Boxly's conftest).

Defaults to throwaway in-memory SQLite; point ``VITALS_TEST_DATABASE_URL`` at a
real Postgres (``scripts/test_postgres.sh`` does this) to exercise what SQLite
fakes — JSONB / GIN / partial-unique indexes, ``func.date`` semantics — which is
where this schema actually lives. ``@pytest.mark.integration`` tests are skipped
on SQLite.
"""

from contextlib import asynccontextmanager, nullcontext
import os
import uuid
from pathlib import Path

# Set before importing app modules so config/security read test values.
os.environ.setdefault("VITALS_TESTING", "1")
os.environ.setdefault("VITALS_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("VITALS_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("VITALS_TIMEZONE", "Europe/Chisinau")
os.environ.setdefault("VITALS_HEIGHT_CM", "190")
os.environ.setdefault("VITALS_SEX", "male")
os.environ.setdefault("VITALS_SESSION_SECRET", "test-session-secret")
os.environ.setdefault("VITALS_AUTH_USERNAME", "tester")
# bcrypt hash of "password" (4 rounds — fast test cost).
os.environ.setdefault(
    "VITALS_AUTH_PASSWORD_HASH",
    "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha",
)
os.environ.setdefault("VITALS_COOKIE_SECURE", "false")
os.environ.setdefault("VITALS_MCP_CLIENT_SECRET", "test-mcp-secret")
os.environ.setdefault("VITALS_MCP_REDIRECT_HOSTS", "claude.ai,oauth-redirect.googleusercontent.com")

# Explicitly clear external API credentials to isolate test runs from developer's .env
os.environ["VITALS_GARMIN_EMAIL"] = ""
os.environ["VITALS_GARMIN_PASSWORD"] = ""
os.environ["VITALS_HEVY_API_KEY"] = ""
os.environ["VITALS_OPENROUTER_API_KEY"] = ""

import pytest
import pytest_asyncio
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

import vitals.models  # noqa: F401 — register all tables on Base.metadata
from vitals.models.base import Base
from tests.schema_modes import pre_ownership_contract_metadata

TEST_USERNAME = "tester"
TEST_PASSWORD = "password"

TEST_DATABASE_URL = os.getenv(
    "VITALS_TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:"
)

if "sqlite" in TEST_DATABASE_URL:
    TEST_ENGINE = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
else:
    # NullPool on the Postgres path: pytest-asyncio gives each test its own event
    # loop, and a pooled asyncpg connection opened in a previous loop blows up on
    # reuse ("another operation is in progress") — which silently capped this
    # project at one working integration test per file. Not pooling means every
    # test opens its own connection, which is what we want for a test run anyway.
    TEST_ENGINE = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)


def pytest_sessionfinish(session, exitstatus):
    """Dispose the shared engine so the process actually exits.

    aiosqlite runs every connection in its own **non-daemon** worker thread, and
    ``StaticPool`` deliberately keeps one connection open for the whole session
    (that is how an ``:memory:`` database survives between tests). Nothing ever
    closed it, so after the last test two live threads kept the interpreter from
    shutting down: the suite printed its summary and then sat there until
    something killed it — minutes of wall time per run, and the reason every
    invocation had to be wrapped in ``timeout``.

    Two engines can be holding such a thread: this module's ``TEST_ENGINE`` and
    the app's own lazily-built one in ``web.deps`` (any code path that skips the
    dependency override builds it against the same in-memory URL).
    """
    import asyncio
    import threading

    async def _dispose() -> None:
        await TEST_ENGINE.dispose()
        try:
            from web import deps
        except Exception:  # web/ not importable in a pure-core run — nothing to do
            return
        factory = deps._session_factory
        if factory is not None:
            await factory.kw["bind"].dispose()

    asyncio.run(_dispose())

    # Anything still non-daemon here will hang the interpreter the same way, so
    # name it now instead of leaving the next person with a silent 15-minute run.
    stragglers = [
        t.name
        for t in threading.enumerate()
        if t is not threading.main_thread() and not t.daemon and t.is_alive()
    ]
    if stragglers:
        print(
            "\nWARNING: non-daemon threads still alive after teardown — this run "
            f"will not exit on its own: {', '.join(stragglers)}"
        )


def pytest_collection_modifyitems(config, items):
    """Skip ``@pytest.mark.integration`` unless pointed at a real Postgres."""
    if "postgresql" in TEST_DATABASE_URL:
        return
    skip_pg = pytest.mark.skip(
        reason="integration test requires Postgres: point VITALS_TEST_DATABASE_URL "
        "at one (scripts/test_postgres.sh does it via Docker; see CONTRIBUTING.md "
        "for the no-Docker route)"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_pg)


@pytest.fixture(autouse=True)
def _reset_engine_registries():
    """Keep module-level registries (conflict resolvers, scheduler jobs) isolated
    between tests."""
    from vitals.services import conflict_engine
    from vitals.scheduler import scheduler as scheduler_mod

    conflict_engine.clear_domain_resolvers()
    scheduler_mod.clear_jobs()
    yield
    conflict_engine.clear_domain_resolvers()
    scheduler_mod.clear_jobs()


_SQLITE_SCHEMA_READY = False
# Which of the two schemas the shared SQLite database currently holds.
_SQLITE_SCHEMA_MODE: str | None = None


def _empty_every_table(conn) -> None:
    """Delete all rows, children first — the cheap way back to a blank database.

    Dropping and recreating 42 tables costs ~160 ms per test, which was most of
    this suite's wall time. Emptying them is ~15 ms and isolates tests just as
    well: no model uses ``AUTOINCREMENT``, so SQLite hands out ids from 1 again
    once a table is empty.
    """
    for table in reversed(Base.metadata.sorted_tables):
        conn.execute(table.delete())


@pytest_asyncio.fixture
async def db_session(request):
    global _SQLITE_SCHEMA_READY, _SQLITE_SCHEMA_MODE
    mode = (
        "pre_ownership_contract"
        if request.node.get_closest_marker("pre_ownership_contract")
        else "ownership_contract"
    )
    relax = (
        pre_ownership_contract_metadata
        if mode == "pre_ownership_contract"
        else nullcontext
    )
    async with TEST_ENGINE.begin() as conn:
        if "sqlite" not in TEST_DATABASE_URL:
            # Postgres runs one connection per test (NullPool), so the schema does
            # not survive between them — keep recreating it there.
            #
            # ``drop_all`` only knows the tables the models still declare, so a
            # table a revision dropped stays behind in a database created before
            # it — and a leftover with a foreign key into a live table blocks
            # that table from being dropped at all. Clearing the schema outright
            # is what a *test* database wants: it is rebuilt on the next line,
            # and the alternative is every developer hitting a stale-database
            # failure that says nothing about their change.
            await conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            await conn.exec_driver_sql("CREATE SCHEMA public")
            with relax():
                await conn.run_sync(Base.metadata.create_all)
        elif _SQLITE_SCHEMA_READY and _SQLITE_SCHEMA_MODE == mode:
            await conn.run_sync(_empty_every_table)
        else:
            # Switching between the two schemas costs a rebuild, so the marked
            # modules are worth keeping few and keeping together.
            if _SQLITE_SCHEMA_READY:
                await conn.run_sync(Base.metadata.drop_all)
            with relax():
                await conn.run_sync(Base.metadata.create_all)
            _SQLITE_SCHEMA_READY = True
        _SQLITE_SCHEMA_MODE = mode
    if "sqlite" in TEST_DATABASE_URL:
        # ``PRAGMA foreign_keys`` is per-connection and this in-memory engine
        # keeps one, so a test that switches enforcement on would otherwise
        # silently harden every test that runs after it.  Reset to the engine's
        # own default and let each test opt in explicitly.
        async with TEST_ENGINE.connect() as reset:
            await reset.exec_driver_sql("PRAGMA foreign_keys=OFF")
    factory = async_sessionmaker(TEST_ENGINE, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session


@asynccontextmanager
async def legacy_unenforced_write(session):
    """Insert a shape that predates the Stage-4 subject-equality constraints.

    Revision 0046 adds those references ``NOT VALID`` on PostgreSQL: rows that
    already existed are never checked, but every new write is.  A test that has
    to reproduce such historical data therefore has to write it the way history
    did — without the constraint — so the service under test still gets the
    chance to fail closed on it.
    """

    bind = session.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        # ``PRAGMA foreign_keys`` is a no-op inside a transaction; deferring is
        # the supported way to write a shape the constraints would reject.
        await session.execute(sa_text("PRAGMA defer_foreign_keys=ON"))
    else:
        await session.execute(sa_text("SET session_replication_role = replica"))
    try:
        yield session
        await session.flush()
    finally:
        if dialect != "sqlite":
            await session.execute(sa_text("SET session_replication_role = origin"))


@pytest_asyncio.fixture
async def unenforced_legacy_write(db_session):
    """Expose :func:`legacy_unenforced_write` bound to the test session."""

    return legacy_unenforced_write


@pytest_asyncio.fixture
async def session_factory(db_session):
    """Fake session factory delegating to the same db_session used in tests."""

    class _CM:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_):
            pass

    class _Factory:
        def __call__(self):
            return _CM()

    return _Factory()


@pytest_asyncio.fixture
async def signals_module_on(db_session, legacy_owner_roots):
    """Kept as a no-op so the tests that name it keep running.

    It used to switch on the ``signals`` module, which was also the proactive
    layer's master switch. Both are gone — the module with the signals domain,
    and the switch with it, because nothing replaced it: the layer is on and its
    own preferences decide what it sends. The fixture stays rather than being
    edited out of a few dozen signatures, and it will go with them.
    """

    del db_session, legacy_owner_roots
    return None


@pytest_asyncio.fixture
async def all_modules_on(db_session, legacy_owner_roots):
    """Mirror the migration seed for service tests that exercise the full lake.

    Bare ``create_all`` databases intentionally use the fail-safe optional-off
    default. Cross-domain context tests opt into the production-like all-on
    state explicitly so module-gating behavior remains testable elsewhere.
    """
    from vitals.models.app_settings import AppSetting
    from vitals.models.identity import HealthSubject
    from vitals.models.scoped_settings import SubjectSetting
    from vitals.services.modules_service import MODULE_REGISTRY, SETTINGS_KEY
    from sqlalchemy import select

    enabled = {key: True for key in MODULE_REGISTRY}
    await db_session.merge(
        AppSetting(key=SETTINGS_KEY, value=enabled)
    )
    for subject_id in await db_session.scalars(select(HealthSubject.id)):
        await db_session.merge(
            SubjectSetting(
                subject_id=subject_id,
                key=SETTINGS_KEY,
                value=enabled,
            )
        )
    await db_session.commit()


@pytest_asyncio.fixture
async def redis():
    """In-memory fakeredis client (async)."""
    import fakeredis.aioredis

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def client(db_session, redis):
    """FastAPI AsyncClient pointing at the root app with dependency overrides."""
    from web.main import app
    from web.deps import get_session, get_redis

    # Seed all dashboard modules ON so Optional pages are reachable in web tests —
    # mirrors the 0012 migration seed (create_all doesn't run migrations, and the
    # fail-safe default is Optional OFF, which would otherwise hide/redirect them).
    from vitals.models.app_settings import AppSetting
    from vitals.models.identity import HealthSubject
    from vitals.models.scoped_settings import SubjectSetting
    from vitals.services.modules_service import MODULE_REGISTRY, SETTINGS_KEY
    from vitals.services.language_service import SETTINGS_KEY as LANG_SETTINGS_KEY
    from sqlalchemy import select

    # merge(), not add(): another fixture may already have written these rows
    # (``signals_module_on`` does), and a blind insert would collide on the PK.
    enabled = {key: True for key in MODULE_REGISTRY}
    await db_session.merge(AppSetting(key=SETTINGS_KEY, value=enabled))
    for subject_id in await db_session.scalars(select(HealthSubject.id)):
        await db_session.merge(
            SubjectSetting(
                subject_id=subject_id,
                key=SETTINGS_KEY,
                value=enabled,
            )
        )
    await db_session.merge(AppSetting(key=LANG_SETTINGS_KEY, value="ru"))
    await db_session.commit()

    async def _get_session():
        yield db_session

    async def _get_redis():
        return redis

    app.dependency_overrides[get_session] = _get_session
    app.dependency_overrides[get_redis] = _get_redis

    from httpx import ASGITransport, AsyncClient
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as c:
        yield c

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def legacy_owner_roots(db_session):
    """Materialize the identity/tenancy roots production creates at startup."""
    # ASGITransport does not run the application's lifespan in these tests.
    # Production startup materializes the legacy owner/subject/resource roots
    # before serving requests or jobs, so focused tests that cross one of those
    # boundaries opt into the same invariant explicitly.
    from vitals.config import load_config
    from vitals.services.identity_bootstrap import bootstrap_legacy_owner
    from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots
    from web.config import get_web_config

    web_config = get_web_config()
    identity = await bootstrap_legacy_owner(
        db_session,
        username=web_config.auth_username,
        password_hash=web_config.auth_password_hash,
        timezone=load_config().timezone,
    )
    await bootstrap_legacy_resource_roots(
        db_session, subject_id=identity.subject_id
    )
    from vitals.services import modules_service
    from vitals.services.scoped_settings_service import (
        ScopedSettingKey,
        SettingScope,
        set_scoped_setting,
    )

    enabled_modules = await modules_service.get_enabled_modules(
        db_session,
        subject_id=identity.subject_id,
    )
    await set_scoped_setting(
        db_session,
        scope=SettingScope.SUBJECT,
        key=ScopedSettingKey.ENABLED_MODULES,
        scope_id=identity.subject_id,
        value=enabled_modules,
    )
    from vitals.services.proactive import prefs

    preference_scope = await prefs.resolve_legacy_preferences_scope(
        db_session,
        actor_username=web_config.auth_username,
    )
    await prefs.initialize_legacy_preferences(
        db_session,
        scope=preference_scope,
    )
    await db_session.commit()
    return identity


@pytest_asyncio.fixture
async def auth_client(client, legacy_owner_roots):
    """An authenticated AsyncClient using credentials from the test env."""

    # TEST_USERNAME/TEST_PASSWORD are module-level globals; reference them directly
    # rather than re-importing `tests.conftest` (which a site-packages `tests`
    # package can shadow, breaking the import).
    r = await client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    assert r.status_code == 303
    return client


@pytest_asyncio.fixture
async def platform_ai_ready(db_session, legacy_owner_roots, monkeypatch):
    """Provision the synthetic installation gateway and aligned owner quota."""

    from datetime import date

    from vitals.enums import (
        IntegrationConnectionStatus,
        IntegrationConnectionType,
        IntegrationProvider,
    )
    from vitals.models.ai import AIPlatformQuotaPeriod, AISubjectQuotaPeriod
    from vitals.models.tenancy import PlatformIntegrationConnection

    monkeypatch.setenv("VITALS_OPENROUTER_API_KEY", "synthetic-platform-ai-key")
    root = PlatformIntegrationConnection(
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator="synthetic-test-platform-ai",
        credential_ref="env:VITALS_OPENROUTER_API_KEY",
        status=IntegrationConnectionStatus.ACTIVE.value,
        config_version=1,
        configured_by_user_id=legacy_owner_roots.user_id,
    )
    period_start = date(2020, 1, 1)
    period_end = date(2100, 1, 1)
    db_session.add_all(
        [
            root,
            AIPlatformQuotaPeriod(
                period_start=period_start,
                period_end=period_end,
                cost_limit_microunits=1_000_000_000,
                unit_limit=1_000_000_000,
                configured_by_user_id=legacy_owner_roots.user_id,
            ),
            AISubjectQuotaPeriod(
                subject_id=legacy_owner_roots.subject_id,
                period_start=period_start,
                period_end=period_end,
                cost_limit_microunits=1_000_000_000,
                unit_limit=1_000_000_000,
                configured_by_user_id=legacy_owner_roots.user_id,
            ),
        ]
    )
    await db_session.commit()
    return root


def alembic_head_revision() -> str:
    """Return the current migration head.

    Its callers moved to :data:`PRE_OWNERSHIP_CONTRACT_REVISION` when the
    ownership contract landed: a rehearsal seeds a revision-0034 lake and cannot
    migrate past the contract until its backfill has finished, so the ceiling it
    asserts against is that revision rather than head. This stays for a test
    that genuinely reaches head and should not have to be edited whenever a
    revision is added.
    """

    from alembic.config import Config as _AlembicConfig
    from alembic.script import ScriptDirectory as _ScriptDirectory

    repository_root = Path(__file__).resolve().parent.parent
    script = _ScriptDirectory.from_config(
        _AlembicConfig(str(repository_root / "alembic.ini"))
    )
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError("the migration chain must have exactly one head")
    return heads[0]


@pytest_asyncio.fixture
async def owned_by_legacy_subject(db_session, legacy_owner_roots):
    """Stamp the sole subject onto rows a test creates without naming one.

    Composition tests build dozens of synthetic domain rows to prove what a
    report contains. Every one of those rows belongs to the person the report is
    about, and spelling that out in each constructor would bury the assertion
    under ownership plumbing. This fixture supplies it the way production does —
    once, from the identity — while leaving any row that names a subject
    explicitly exactly as the test wrote it, so cross-subject cases still work.
    """

    from sqlalchemy import event as sa_event

    subject_id = legacy_owner_roots.subject_id
    sync_session = db_session.sync_session

    from vitals.ownership import OWNERSHIP_REGISTRY, TargetColumn

    _UNSTAMPABLE_TABLES = {"notifications", "notification_delivery_intents"}

    def _stamp(session, _flush_context, _instances):
        for instance in session.new:
            table = getattr(type(instance), "__table__", None)
            if table is None or "subject_id" not in table.columns:
                continue
            spec = OWNERSHIP_REGISTRY.get(table.name)
            # A curated catalog row and a platform-wide alert belong to nobody;
            # stamping them would turn the platform's half of the key into one
            # person's row.
            # An inherited child carries whatever its parent carries, and in
            # these tests the parent is this subject's row.
            if spec is None or spec.subject not in {
                TargetColumn.REQUIRED,
                TargetColumn.INHERITED,
            }:
                continue
            # A delivery artifact's roots travel as a set — subject, recipient,
            # and channel — and a database check enforces that shape. Supplying
            # one of the three would make the row invalid rather than owned.
            if table.name in _UNSTAMPABLE_TABLES:
                continue
            if getattr(instance, "subject_id", None) is None:
                instance.subject_id = subject_id

    sa_event.listen(sync_session, "before_flush", _stamp)
    try:
        yield legacy_owner_roots
    finally:
        sa_event.remove(sync_session, "before_flush", _stamp)


@pytest_asyncio.fixture
async def signals_owner(db_session, legacy_owner_roots):
    """The sole owner plus the Telegram channel their messages arrive on.

    A signal is something a person said to the bot, so the closed domain wants
    both the identity it belongs to and the recipient connection it came in
    through. Building the pair once keeps a capture test about capture.
    """

    from types import SimpleNamespace

    from vitals.enums import (
        IntegrationConnectionStatus,
        IntegrationConnectionType,
        IntegrationProvider,
    )
    from vitals.models.tenancy import IntegrationConnection
    from vitals.ownership import WriteIdentity
    from vitals.services.proactive import channels

    connection = IntegrationConnection(
        subject_id=legacy_owner_roots.subject_id,
        provider=IntegrationProvider.TELEGRAM.value,
        connection_type=IntegrationConnectionType.RECIPIENT.value,
        external_account_discriminator="synthetic:signals-owner",
        credential_ref=channels.LEGACY_TELEGRAM_CREDENTIAL_REF,
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add(connection)
    await db_session.flush()
    return SimpleNamespace(
        subject_id=legacy_owner_roots.subject_id,
        identity=WriteIdentity(
            legacy_owner_roots.subject_id, legacy_owner_roots.user_id
        ),
        system_identity=WriteIdentity(legacy_owner_roots.subject_id, None),
        connection=connection,
        connection_id=connection.id,
    )


@pytest_asyncio.fixture
async def legacy_connection_ids(db_session, legacy_owner_roots):
    """The sole owner's bootstrapped connection, looked up by provider.

    ``integration_connection_id`` is the ownership registry's other ``REQUIRED``
    reference on the vendor tables: a Garmin day that arrived through no account
    cannot be attributed to anyone. ``bootstrap_legacy_resource_roots`` already
    created one connection per provider for this subject, so a test seeding
    vendor data asks for the one it means rather than inventing a UUID.
    """

    from sqlalchemy import select

    from vitals.models.tenancy import IntegrationConnection

    rows = await db_session.scalars(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == legacy_owner_roots.subject_id
        )
    )
    by_provider = {row.provider: row.id for row in rows}

    def _lookup(provider) -> uuid.UUID:
        key = getattr(provider, "value", provider)
        try:
            return by_provider[key]
        except KeyError:  # pragma: no cover - a missing root is a bootstrap bug
            raise AssertionError(
                f"the legacy resource roots have no {key!r} connection; "
                f"available: {sorted(by_provider)}"
            ) from None

    return _lookup


@pytest_asyncio.fixture
async def garmin_connection_id(legacy_connection_ids):
    """Shorthand for the owner's Garmin connection — by far the most seeded."""

    from vitals.enums import IntegrationProvider

    return legacy_connection_ids(IntegrationProvider.GARMIN)


@pytest_asyncio.fixture
async def hevy_connection_id(legacy_connection_ids):
    """Shorthand for the owner's Hevy connection."""

    from vitals.enums import IntegrationProvider

    return legacy_connection_ids(IntegrationProvider.HEVY)


@pytest_asyncio.fixture
async def garmin_owned_scope(legacy_owner_roots, garmin_connection_id):
    """Identity plus Garmin account for the owned provider entry points.

    ``sync_owned``, ``pulse_owned`` and the owned ingests all take the pair
    together, because a vendor row is meaningless without both: whose data it is
    and which account it arrived through. The bootstrapped root connection is
    ``legacy`` rather than ``active``, which those paths accept — that is the
    state a single-user installation upgrades into.
    """

    from types import SimpleNamespace

    from vitals.ownership import WriteIdentity

    return SimpleNamespace(
        subject_id=legacy_owner_roots.subject_id,
        connection_id=garmin_connection_id,
        identity=WriteIdentity(
            legacy_owner_roots.subject_id, legacy_owner_roots.user_id
        ),
        # A scheduled pull has no human behind it; the provider paths take this
        # form when they attribute a row to the account rather than to a person.
        system_identity=WriteIdentity(legacy_owner_roots.subject_id, None),
    )


@pytest_asyncio.fixture
async def hevy_owned_scope(legacy_owner_roots, hevy_connection_id):
    """Identity plus Hevy account, the pair the owned Hevy entry points take."""

    from types import SimpleNamespace

    from vitals.ownership import WriteIdentity

    return SimpleNamespace(
        subject_id=legacy_owner_roots.subject_id,
        connection_id=hevy_connection_id,
        identity=WriteIdentity(
            legacy_owner_roots.subject_id, legacy_owner_roots.user_id
        ),
        system_identity=WriteIdentity(legacy_owner_roots.subject_id, None),
    )


@pytest_asyncio.fixture
async def openrouter_connection_id(legacy_connection_ids):
    """The owner's OpenRouter account — a generated narrative's provenance."""

    from vitals.enums import IntegrationProvider

    return legacy_connection_ids(IntegrationProvider.OPENROUTER)


@pytest_asyncio.fixture
async def telegram_connection_id(legacy_connection_ids):
    """The owner's Telegram recipient connection.

    A notification that carries a ``dedupe_key`` has to name its whole root —
    subject, recipient and connection — or none of it; there is no half-owned
    notification. Seeding one therefore needs the channel it was sent on.
    """

    from vitals.enums import IntegrationProvider

    return legacy_connection_ids(IntegrationProvider.TELEGRAM)


@pytest_asyncio.fixture
async def legacy_file_asset_id(db_session, legacy_owner_roots):
    """One private-file root belonging to the owner.

    A progress photo is a pointer and the asset is the file; a test that seeds
    the pointer should say which file it points at rather than leave the link
    empty, which is the shape the ownership registry marks ``REQUIRED``.
    """

    from vitals.enums import FileAssetPurpose, FileStorageBackend
    from vitals.models.tenancy import FileAsset

    asset = FileAsset(
        subject_id=legacy_owner_roots.subject_id,
        uploaded_by_user_id=legacy_owner_roots.user_id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO.value,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        storage_ref="progress/synthetic-fixture.jpg",
    )
    db_session.add(asset)
    await db_session.flush()
    return asset.id


@pytest_asyncio.fixture
async def owner_write(db_session, legacy_owner_roots):
    """One scoped write capability for the sole owner.

    Domain writes now demand the subject and the conflict decision together, so
    a test that exercises a write path needs both. This builds them once from
    the identity, the same way a router does before calling the service.
    """

    from types import SimpleNamespace

    from vitals.services import conflict_engine

    from web.config import get_web_config

    # A human actor: an override is a decision somebody has to be answerable for.
    context = await conflict_engine.resolve_legacy_conflict_write_context(
        db_session,
        actor_username=get_web_config().auth_username,
    )

    def _clock_date(on_date):
        """Rebuild the day under whatever clock is running right now.

        The capability compares its evaluation date by exact type, and a frozen
        clock swaps that type out — so a module-level date built before the
        freeze is the wrong class inside it.
        """

        from datetime import date as clock_date

        return clock_date(on_date.year, on_date.month, on_date.day)

    async def write(on_date=None):
        """A capability is bound to its transaction, so mint a fresh one.

        Domains that evaluate rules for a specific day check the capability's
        evaluation date against the row's, so a test writing a dated fact says
        which day it is writing about.
        """

        scoped = context
        if on_date is not None:
            on_date = _clock_date(on_date)
            scoped = await conflict_engine.resolve_legacy_conflict_write_context(
                db_session,
                actor_username=get_web_config().auth_username,
                evaluation_date=on_date,
            )
        return await conflict_engine.prepare_scoped_write(
            db_session,
            context=scoped,
        )

    async def weight_write(on_date=None):
        """The Weight variant of the same capability.

        Body composition writes a weigh-in alongside the scan, and that path
        takes the Weight lock order rather than the generic one, so it needs the
        capability weight_service issues. Like the router, it carries the Garmin
        destination when one is configured, so a weigh-in that changes reaches
        the export outbox instead of silently skipping it.
        """

        from vitals.services import garmin_weight_service, weight_service

        scoped = context
        if on_date is not None:
            on_date = _clock_date(on_date)
            scoped = await conflict_engine.resolve_legacy_conflict_write_context(
                db_session,
                actor_username=get_web_config().auth_username,
                evaluation_date=on_date,
            )
        export_context = (
            await garmin_weight_service.resolve_optional_legacy_export_context(
                db_session,
                actor_username=get_web_config().auth_username,
            )
        )
        return await weight_service.prepare_weight_write(
            db_session,
            context=scoped,
            garmin_weight_export_context=export_context,
        )

    return SimpleNamespace(
        identity=context.identity,
        write=write,
        weight_write=weight_write,
        context=context,
        subject_id=legacy_owner_roots.subject_id,
    )
