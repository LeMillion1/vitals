"""On-demand Garmin/Hevy sync from the conversation — and the cap that keeps it
from becoming a poll. Three calls a day each; the fourth is refused before the
external API is touched, and a disabled module is refused before that.

Both sync jobs are stubbed: what's under test is the tool wrapper (quota, clamp,
gate, not-configured), not the ingest, which has its own suites.
"""
from __future__ import annotations

import pytest

mcp_router = pytest.importorskip("web.routers.mcp")


@pytest.fixture(autouse=True)
def _use_test_backends(session_factory, redis, monkeypatch, legacy_owner_roots):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    monkeypatch.setattr(mcp_router, "get_redis_client", lambda: redis)


async def test_sync_garmin_caps_at_three_a_day_and_clamps_the_window(monkeypatch):
    from vitals.services import garmin_service

    calls = []

    async def fake_sync_job(
        session_factory, redis=None, *, days=2, actor_username=None
    ):
        calls.append((days, actor_username))
        return {"days": days, "activities": 0, "error": None}

    monkeypatch.setattr(garmin_service, "sync_job", fake_sync_job)

    assert (await mcp_router.sync_garmin())["days"] == 2
    # A gap of a few days is a legitimate ask; a year is not — the window clamps.
    assert (await mcp_router.sync_garmin(days=999))["days"] == 30
    await mcp_router.sync_garmin()

    fourth = await mcp_router.sync_garmin()
    assert "3 times today" in fourth["error"]
    assert calls == [
        (2, "tester"),
        (30, "tester"),
        (2, "tester"),
    ], "the refused call must never reach Garmin"


async def test_sync_garmin_says_so_when_garmin_is_not_configured(monkeypatch):
    from vitals.services import garmin_service

    async def unconfigured(
        session_factory, redis=None, *, days=2, actor_username=None
    ):
        return None

    monkeypatch.setattr(garmin_service, "sync_job", unconfigured)
    assert "not configured" in (await mcp_router.sync_garmin())["error"]


async def test_sync_hevy_refuses_a_disabled_module_without_spending_quota(
    legacy_owner_roots,
    monkeypatch,
):
    from vitals.services import hevy_service, modules_service

    called = []

    async def fake_sync_job(session_factory, redis=None, *, actor_username=None):
        called.append(actor_username)
        return {"fetched": 1, "created": 1, "updated": 0, "skipped": 0}

    monkeypatch.setattr(hevy_service, "sync_job", fake_sync_job)

    # Optional modules default to off.
    assert await mcp_router.sync_hevy() == {"error": "module 'hevy' is disabled"}
    assert called == []

    session_factory = mcp_router.get_session_factory()
    async with session_factory() as session:
        await modules_service.set_module_enabled(
            session,
            key="hevy",
            enabled=True,
            subject_id=legacy_owner_roots.subject_id,
        )
        await session.commit()

    assert (await mcp_router.sync_hevy())["created"] == 1
    assert called == ["tester"]
