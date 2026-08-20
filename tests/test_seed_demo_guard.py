"""Fail-closed contract for destructive legacy demo seed utilities."""

from __future__ import annotations

import asyncio
import ast
from pathlib import Path

import pytest
from sqlalchemy import event, func, inspect, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from scripts import seed_demo as seed_module
from vitals.enums import Domain, Source, UserStatus
from vitals.models.base import Base
from vitals.models.identity import HealthSubject, User
from vitals.models.supplements import Supplement


DESTRUCTIVE_WRITERS = (
    "seed_supplements",
    "seed_genetics",
    "seed_lab_markers",
    "seed_conflict_rules",
    "seed_dose_phases",
    "seed_weight",
    "seed_measurements",
    "seed_garmin",
    "seed_meals",
    "seed_skincare",
    "seed_injections",
    "seed_labs",
    "seed_workouts",
    "seed_milestones",
    "seed_digests",
    "seed_app_settings",
)


def _sentinel() -> Supplement:
    return Supplement(
        name="Synthetic existing supplement",
        key="synthetic-existing",
        domain=Domain.SUPPLEMENTS.value,
        source=Source.MANUAL.value,
    )


def test_static_inventory_discovers_every_guarded_delete_writer():
    tree = ast.parse(Path(seed_module.__file__).read_text(encoding="utf-8"))
    discovered: dict[str, tuple[ast.AsyncFunctionDef, list[ast.Call]]] = {}
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        deletes = [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "delete"
        ]
        if deletes:
            discovered[node.name] = (node, deletes)

    assert set(discovered) == set(DESTRUCTIVE_WRITERS)
    assert sum(len(deletes) for _node, deletes in discovered.values()) == 18
    for node, _deletes in discovered.values():
        first = node.body[0]
        assert isinstance(first, ast.Expr)
        assert isinstance(first.value, ast.Await)
        call = first.value.value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Name)
        assert call.func.id == "_authorize_destructive_demo_seed"


async def test_every_destructive_writer_rejects_identity_without_mutation(
    db_session,
    legacy_owner_roots,
):
    sentinel = _sentinel()
    sentinel_key = sentinel.key
    db_session.add(sentinel)
    await db_session.commit()

    for writer_name in DESTRUCTIVE_WRITERS:
        writer = getattr(seed_module, writer_name)
        with pytest.raises(RuntimeError, match="cannot run after commercial identity"):
            await writer(db_session)
        await db_session.rollback()

    rows = list(
        await db_session.scalars(
            select(Supplement).where(Supplement.key == sentinel_key)
        )
    )
    assert len(rows) == 1


async def test_full_demo_seed_rejects_identity_without_mutation(
    db_session,
    legacy_owner_roots,
):
    sentinel = _sentinel()
    sentinel_key = sentinel.key
    db_session.add(sentinel)
    await db_session.commit()

    with pytest.raises(RuntimeError, match="cannot run after commercial identity"):
        await seed_module.seed_demo(db_session)
    await db_session.rollback()

    rows = list(
        await db_session.scalars(
            select(Supplement).where(Supplement.key == sentinel_key)
        )
    )
    assert len(rows) == 1


async def test_open_transaction_rejects_before_pending_state_autoflush(db_session):
    pending = _sentinel()
    db_session.add(pending)

    with pytest.raises(RuntimeError, match="fresh transaction"):
        await seed_module.seed_genetics(db_session)

    state = inspect(pending)
    assert state.pending
    assert pending.id is None


async def test_pending_subject_delete_cannot_hide_identity_from_guard(
    db_session,
    legacy_owner_roots,
):
    sentinel = _sentinel()
    sentinel_key = sentinel.key
    db_session.add(sentinel)
    await db_session.commit()
    subject = await db_session.get(HealthSubject, legacy_owner_roots.subject_id)
    assert subject is not None
    await db_session.delete(subject)

    with pytest.raises(RuntimeError, match="fresh transaction"):
        await seed_module.seed_supplements(db_session)

    assert subject in db_session.deleted
    await db_session.rollback()
    assert await db_session.get(HealthSubject, legacy_owner_roots.subject_id) is not None
    assert await db_session.scalar(
        select(func.count())
        .select_from(Supplement)
        .where(Supplement.key == sentinel_key)
    ) == 1


async def test_governance_lock_precedes_subject_check_and_global_delete(
    db_session,
    monkeypatch,
):
    assert db_session.bind is not None
    events: list[str] = []
    original_lock = seed_module.acquire_identity_governance_lock

    async def observed_lock(session: AsyncSession) -> None:
        events.append("governance_lock")
        await original_lock(session)

    def observe_statement(*args) -> None:
        events.append(str(args[2]))

    monkeypatch.setattr(
        seed_module,
        "acquire_identity_governance_lock",
        observed_lock,
    )
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", observe_statement)
    try:
        await seed_module.seed_supplements(db_session)
    finally:
        event.remove(
            db_session.bind.sync_engine,
            "before_cursor_execute",
            observe_statement,
        )

    if db_session.bind.dialect.name == "postgresql":
        assert events[0] == "governance_lock"
    else:
        assert events[0] == "BEGIN IMMEDIATE"
    subject_check_index = next(
        index for index, statement in enumerate(events) if "FROM health_subjects" in statement
    )
    delete_index = next(
        index
        for index, statement in enumerate(events)
        if statement.startswith("DELETE FROM supplements")
    )
    assert 0 < subject_check_index < delete_index
    if db_session.bind.dialect.name == "postgresql":
        assert "pg_advisory_xact_lock" in events[1]
        assert subject_check_index == 2


async def test_sqlite_seed_lock_serializes_concurrent_identity_creation(
    tmp_path,
    monkeypatch,
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'seed-race.db'}",
        poolclass=NullPool,
        connect_args={"timeout": 5},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    seed_authorized = asyncio.Event()
    identity_attempted = asyncio.Event()
    release_seed = asyncio.Event()
    original_authorize = seed_module._authorize_destructive_demo_seed

    async def observed_authorize(session: AsyncSession) -> None:
        await original_authorize(session)
        seed_authorized.set()
        await asyncio.wait_for(release_seed.wait(), timeout=5)

    monkeypatch.setattr(
        seed_module,
        "_authorize_destructive_demo_seed",
        observed_authorize,
    )

    async def run_seed() -> None:
        async with factory() as session:
            await seed_module.seed_supplements(session)
            await session.commit()

    async def create_identity() -> None:
        await asyncio.wait_for(seed_authorized.wait(), timeout=5)
        async with factory() as session:
            user = User(
                username="sqlite-seed-race-owner",
                normalized_username="sqlite-seed-race-owner",
                password_hash="$synthetic-test-hash",
                status=UserStatus.ACTIVE.value,
            )
            session.add(user)
            identity_attempted.set()
            await session.flush()
            session.add(HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty"))
            await session.commit()

    seed_task = asyncio.create_task(run_seed())
    identity_task = asyncio.create_task(create_identity())
    try:
        await asyncio.wait_for(identity_attempted.wait(), timeout=5)
        await asyncio.sleep(0.15)
        assert not identity_task.done(), "identity write must wait for the seed lock"
        release_seed.set()
        await asyncio.wait_for(asyncio.gather(seed_task, identity_task), timeout=10)

        async with factory() as verify:
            assert await verify.scalar(
                select(func.count()).select_from(HealthSubject)
            ) == 1
            assert await verify.scalar(
                select(func.count()).select_from(Supplement)
            ) == 7
    finally:
        release_seed.set()
        for task in (seed_task, identity_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(seed_task, identity_task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.integration
async def test_postgres_seed_check_and_delete_serialize_identity_creation(
    db_session,
    monkeypatch,
):
    from vitals.services.identity_service import (
        acquire_identity_governance_lock as real_governance_lock,
    )

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    sentinel = _sentinel()
    db_session.add(sentinel)
    await db_session.commit()

    seed_authorized = asyncio.Event()
    identity_attempted = asyncio.Event()
    original_authorize = seed_module._authorize_destructive_demo_seed

    async def observed_authorize(session: AsyncSession) -> None:
        await original_authorize(session)
        seed_authorized.set()
        await asyncio.wait_for(identity_attempted.wait(), timeout=5)

    monkeypatch.setattr(
        seed_module,
        "_authorize_destructive_demo_seed",
        observed_authorize,
    )

    async def run_seed() -> None:
        async with factory() as session:
            await seed_module.seed_supplements(session)
            await session.commit()

    async def create_identity() -> None:
        await asyncio.wait_for(seed_authorized.wait(), timeout=5)
        async with factory() as session:
            identity_attempted.set()
            await real_governance_lock(session)
            assert await session.scalar(select(func.count()).select_from(Supplement)) == 7
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Supplement)
                    .where(Supplement.key == sentinel.key)
                )
                == 0
            )
            user = User(
                username="seed-race-after-replace",
                normalized_username="seed-race-after-replace",
                password_hash="$synthetic-test-hash",
                status=UserStatus.ACTIVE.value,
            )
            session.add(user)
            await session.flush()
            session.add(HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty"))
            await session.commit()

    await asyncio.wait_for(
        asyncio.gather(run_seed(), create_identity()),
        timeout=10,
    )

    async with factory() as verify:
        assert await verify.scalar(select(func.count()).select_from(HealthSubject)) == 1
        assert await verify.scalar(select(func.count()).select_from(Supplement)) == 7


@pytest.mark.integration
async def test_postgres_identity_creation_wins_race_and_seed_preserves_data(
    db_session,
    monkeypatch,
):
    from vitals.services.identity_service import (
        acquire_identity_governance_lock as real_governance_lock,
    )

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    sentinel = _sentinel()
    db_session.add(sentinel)
    await db_session.commit()

    identity_locked = asyncio.Event()
    seed_attempted = asyncio.Event()
    original_seed_lock = seed_module.acquire_identity_governance_lock

    async def observed_seed_lock(session: AsyncSession) -> None:
        seed_attempted.set()
        await original_seed_lock(session)

    monkeypatch.setattr(
        seed_module,
        "acquire_identity_governance_lock",
        observed_seed_lock,
    )

    async def create_identity() -> None:
        async with factory() as session:
            await real_governance_lock(session)
            identity_locked.set()
            await asyncio.wait_for(seed_attempted.wait(), timeout=5)
            user = User(
                username="seed-race-owner",
                normalized_username="seed-race-owner",
                password_hash="$synthetic-test-hash",
                status=UserStatus.ACTIVE.value,
            )
            session.add(user)
            await session.flush()
            session.add(HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty"))
            await session.commit()

    async def run_seed() -> None:
        await asyncio.wait_for(identity_locked.wait(), timeout=5)
        async with factory() as session:
            with pytest.raises(
                RuntimeError,
                match="cannot run after commercial identity",
            ):
                await seed_module.seed_supplements(session)

    await asyncio.wait_for(
        asyncio.gather(create_identity(), run_seed()),
        timeout=10,
    )

    async with factory() as verify:
        assert await verify.scalar(select(func.count()).select_from(HealthSubject)) == 1
        rows = list(await verify.scalars(select(Supplement)))
        assert len(rows) == 1
        assert rows[0].key == sentinel.key
