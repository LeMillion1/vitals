"""Unit tests for the dashboard-modularity service.

Pure-ish logic over a tiny KV table — runs on the fast SQLite path (the JSON
column degrades via with_variant). Covers the fail-safe contract: defaults on
empty/corrupt config, Core always-on, Optional isolation, and Redis caching.
"""
from __future__ import annotations

import json

import pytest

from vitals.models.scoped_settings import SubjectSetting
from vitals.services import modules_service
from vitals.services.modules_service import (
    DEFAULT_STATE,
    SETTINGS_KEY,
    ModuleToggleError,
)



async def test_defaults_on_empty_db(db_session, legacy_owner_roots):
    """No config row → Core True, Optional False, never raises."""
    state = await modules_service.get_enabled_modules(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert state["weight"] is True
    assert state["garmin"] is True
    assert state["labs"] is True
    assert state["reports"] is True
    assert state["hevy"] is False
    assert state["glp1"] is False
    assert state == DEFAULT_STATE


async def test_core_forced_true_even_if_stored_false(db_session, legacy_owner_roots):
    """A stored Core=False must be ignored — Core is locked on."""
    await db_session.merge(
        SubjectSetting(
            subject_id=legacy_owner_roots.subject_id,
            key=SETTINGS_KEY,
            value={"weight": False, "labs": False, "hevy": True},
        )
    )
    await db_session.commit()

    state = await modules_service.get_enabled_modules(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert state["weight"] is True
    assert state["labs"] is True
    assert state["hevy"] is True


async def test_set_optional_persists(db_session, legacy_owner_roots):
    """Enabling an Optional module persists to the DB."""
    returned = await modules_service.set_module_enabled(
        db_session,
        key="hevy",
        enabled=True,
        subject_id=legacy_owner_roots.subject_id,
    )
    await db_session.commit()
    assert returned["hevy"] is True

    fresh = await modules_service.get_enabled_modules(
        db_session,
        redis=None,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert fresh["hevy"] is True


async def test_toggle_isolation(db_session, legacy_owner_roots):
    """Toggling one Optional module leaves the others untouched."""
    await modules_service.set_module_enabled(
        db_session,
        key="hevy",
        enabled=True,
        subject_id=legacy_owner_roots.subject_id,
    )
    await modules_service.set_module_enabled(
        db_session,
        key="supplements",
        enabled=True,
        subject_id=legacy_owner_roots.subject_id,
    )
    await modules_service.set_module_enabled(
        db_session,
        key="hevy",
        enabled=False,
        subject_id=legacy_owner_roots.subject_id,
    )
    await db_session.commit()

    state = await modules_service.get_enabled_modules(
        db_session,
        redis=None,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert state["hevy"] is False
    assert state["supplements"] is True  # unaffected by the hevy toggle
    assert state["glp1"] is False        # still default


async def test_cannot_disable_core(db_session, legacy_owner_roots):
    """Core modules are not toggleable."""
    with pytest.raises(ModuleToggleError):
        await modules_service.set_module_enabled(
            db_session,
            key="weight",
            enabled=False,
            subject_id=legacy_owner_roots.subject_id,
        )


async def test_unknown_module_raises(db_session, legacy_owner_roots):
    """An unknown key is rejected loudly (Zero Silent Errors)."""
    with pytest.raises(ModuleToggleError):
        await modules_service.set_module_enabled(
            db_session,
            key="does_not_exist",
            enabled=True,
            subject_id=legacy_owner_roots.subject_id,
        )


async def test_unknown_keys_dropped(db_session, legacy_owner_roots):
    """Stale/unknown stored keys are projected away by the registry."""
    await db_session.merge(
        SubjectSetting(
            subject_id=legacy_owner_roots.subject_id,
            key=SETTINGS_KEY,
            value={"foobar": True, "hevy": True},
        )
    )
    await db_session.commit()

    state = await modules_service.get_enabled_modules(
        db_session,
        redis=None,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert "foobar" not in state
    assert state["hevy"] is True
    assert set(state) == set(DEFAULT_STATE)


async def test_malformed_value_falls_back(
    db_session, monkeypatch, legacy_owner_roots
):
    """A non-object value → safe defaults, and the fallback is LOGGED."""
    await db_session.merge(
        SubjectSetting(
            subject_id=legacy_owner_roots.subject_id,
            key=SETTINGS_KEY,
            value="garbage-not-a-dict",
        )
    )
    await db_session.commit()

    warnings: list[str] = []
    monkeypatch.setattr(
        modules_service.logger,
        "warning",
        lambda message, *args, **_kwargs: warnings.append(
            message % args if args else message
        ),
    )
    state = await modules_service.get_enabled_modules(
        db_session,
        redis=None,
        subject_id=legacy_owner_roots.subject_id,
    )

    assert state == DEFAULT_STATE
    assert any("not an object" in warning for warning in warnings)


async def test_redis_cache_is_read_through(db_session, redis, legacy_owner_roots):
    """A primed Redis value is served without touching the (empty) DB."""
    await modules_service.prime_cache(
        redis,
        {**DEFAULT_STATE, "hevy": True},
        subject_id=legacy_owner_roots.subject_id,
    )
    # Sanity: the cache holds JSON we can read back.
    assert json.loads(await redis.get(
        modules_service.cache_key(legacy_owner_roots.subject_id)
    ))["hevy"] is True

    state = await modules_service.get_enabled_modules(
        db_session,
        redis,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert state["hevy"] is True  # came from cache; DB has no row


async def test_get_primes_cache_from_db(db_session, redis, legacy_owner_roots):
    """A DB read writes the resolved state through to Redis."""
    await db_session.merge(
        SubjectSetting(
            subject_id=legacy_owner_roots.subject_id,
            key=SETTINGS_KEY,
            value={"glp1": True},
        )
    )
    await db_session.commit()

    await modules_service.get_enabled_modules(
        db_session,
        redis,
        subject_id=legacy_owner_roots.subject_id,
    )

    cached = json.loads(await redis.get(
        modules_service.cache_key(legacy_owner_roots.subject_id)
    ))
    assert cached["glp1"] is True


@pytest.mark.integration
async def test_concurrent_toggles_do_not_lose_updates(db_session, legacy_owner_roots):
    """Two concurrent toggles of *different* modules must both survive.

    ``set_module_enabled`` is a read-modify-write of a single JSON row: read the
    map, flip one key, write it back. Without a row lock, two near-simultaneous
    toggles both read the old map and the last writer silently drops the other's
    change (lost update). The ``SELECT … FOR UPDATE`` fix serializes them on the
    row. Requires real concurrency + row locking → Postgres only (on SQLite the
    write is serialized by the file lock and ``with_for_update`` is a no-op, so the
    race can't be reproduced).
    """
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    await db_session.merge(
        SubjectSetting(
            subject_id=legacy_owner_roots.subject_id,
            key=SETTINGS_KEY,
            value=dict(DEFAULT_STATE),
        )
    )
    await db_session.commit()

    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, class_=AsyncSession)

    # Session A grabs the FOR UPDATE lock by toggling glp1, holding its transaction
    # open (no commit yet).
    session_a = factory()
    await modules_service.set_module_enabled(
        session_a,
        key="glp1",
        enabled=True,
        subject_id=legacy_owner_roots.subject_id,
    )

    # Session B toggles hevy concurrently; with the row lock it must block on the
    # SELECT FOR UPDATE until A commits.
    async def toggle_b():
        async with factory() as session_b:
            await modules_service.set_module_enabled(
                session_b,
                key="hevy",
                enabled=True,
                subject_id=legacy_owner_roots.subject_id,
            )
            await session_b.commit()

    task_b = asyncio.create_task(toggle_b())
    await asyncio.sleep(0.25)  # let B reach (and block on) the lock
    assert not task_b.done(), "session B should block on the row lock held by A"

    await session_a.commit()
    await session_a.close()
    await asyncio.wait_for(task_b, timeout=5)

    async with factory() as verify:
        state = await modules_service.get_enabled_modules(
            verify,
            redis=None,
            subject_id=legacy_owner_roots.subject_id,
        )
    assert state["glp1"] is True, "session A's toggle was lost"
    assert state["hevy"] is True, "session B's toggle was lost"


def test_bottom_bar_is_five_fixed_columns_whatever_is_enabled():
    """The phone bar is Today + three slots + More, always — the grid used to be
    sized from the enabled-module count, so every toggle shifted every icon."""
    from vitals.services.modules_service import (
        BOTTOM_SLOT_COUNT,
        MODULE_REGISTRY,
        OPTIONAL_KEYS,
        bottom_slots,
    )

    enabled = {k: True for k in MODULE_REGISTRY}
    assert [s.key for s in bottom_slots(enabled)] == ["health", "nutrition", "lifestyle"]

    # Turning any single optional module off must not cost the bar a column.
    for key in OPTIONAL_KEYS:
        one_off = {**enabled, key: False}
        assert len(bottom_slots(one_off)) == BOTTOM_SLOT_COUNT, key

    # …nor must turning every optional module off (core-only worst case).
    core_only = {k: k not in OPTIONAL_KEYS for k in MODULE_REGISTRY}
    slots = bottom_slots(core_only)
    assert [s.key for s in slots] == ["health", "markers"]
    assert all(s.route for s in slots)
