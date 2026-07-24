"""A failing scheduled job has to be visible.

The heartbeat is stamped *before* a job runs, so a job that throws every tick
keeps ``/health`` green while the data lake quietly stops filling — exactly the
silent-loss class this suite guards against. Two mechanisms cover it:

  * the shared runner raises a ``warn`` alert on failure and clears it on the
    next success (:func:`vitals.scheduler.scheduler._make_runner`);
  * ``/health`` measures every heartbeating job against a budget derived from its
    own schedule, so a job that stops firing turns the endpoint red.
"""
from __future__ import annotations

import time


from vitals.enums import Domain
from vitals.scheduler import scheduler as scheduler_mod
from vitals.services import alerts_service



def _runner(session_factory, job_id: str, func, redis=None):
    spec = scheduler_mod.JobSpec(id=job_id, func=func, trigger="interval", trigger_kwargs={"hours": 6})
    return scheduler_mod._make_runner(spec, session_factory, redis)


async def test_failing_job_raises_alert_and_does_not_propagate(session_factory, db_session):
    async def boom(_factory, _redis):
        raise RuntimeError("Garmin said no")

    await _runner(session_factory, "garmin_sync", boom)()  # must not raise

    alerts = await alerts_service.list_active(db_session, domain=Domain.SYSTEM.value)
    failed = [a for a in alerts if a.alert_key == "scheduler.job_failed:garmin_sync"]
    assert len(failed) == 1
    assert failed[0].severity == "warn"
    assert "Garmin said no" in failed[0].message


async def test_repeated_failures_do_not_pile_up(session_factory, db_session):
    async def boom(_factory, _redis):
        raise RuntimeError("still down")

    run = _runner(session_factory, "hevy_sync", boom)
    await run()
    await run()
    await run()

    alerts = await alerts_service.list_active(db_session, domain=Domain.SYSTEM.value)
    failed = [a for a in alerts if a.alert_key == "scheduler.job_failed:hevy_sync"]
    assert len(failed) == 1, "the alert key must dedupe, not add a row per failed tick"


async def test_successful_run_clears_the_alert(session_factory, db_session):
    state = {"fail": True}

    async def flaky(_factory, _redis):
        if state["fail"]:
            raise RuntimeError("transient")

    run = _runner(session_factory, "hevy_sync", flaky)
    await run()
    assert [
        a for a in await alerts_service.list_active(db_session, domain=Domain.SYSTEM.value)
        if a.alert_key == "scheduler.job_failed:hevy_sync"
    ]

    state["fail"] = False
    await run()
    assert not [
        a for a in await alerts_service.list_active(db_session, domain=Domain.SYSTEM.value)
        if a.alert_key == "scheduler.job_failed:hevy_sync"
    ]


# ── Per-job heartbeat budgets ────────────────────────────────────────────────
async def test_budget_matches_each_job_schedule():
    async def noop(_factory, _redis):
        return None

    scheduler_mod.register_job("hevy_sync", noop, trigger="interval", hours=6)
    scheduler_mod.register_job("garmin_sync", noop, trigger="cron", hour="3,11,16,22", minute=0)
    scheduler_mod.register_job("weekly_digest", noop, trigger="cron", day_of_week="mon", hour=8)

    budgets = scheduler_mod.heartbeat_budgets("Europe/Chisinau")
    slack = scheduler_mod._BUDGET_SLACK_SECONDS

    assert budgets[scheduler_mod.KEEPALIVE_JOB_ID] == 120.0
    assert budgets["hevy_sync"] == 6 * 3600 + slack
    # The widest real gap of 03/11/16/22 is 8h, not 24h/4 — a job may legitimately
    # go that long between runs without being stale.
    assert budgets["garmin_sync"] == 8 * 3600 + slack
    assert budgets["weekly_digest"] == 7 * 86400 + slack


async def test_health_red_when_any_job_heartbeat_is_overdue(client, redis):
    async def noop(_factory, _redis):
        return None

    now = int(time.time())
    await redis.set("scheduler:last_run:keepalive", str(now))

    # A job registered but never heard from: /health used to watch only the
    # keepalive and stayed green while every module job was dead.
    scheduler_mod.register_job("hevy_sync", noop, trigger="interval", hours=6)

    body = (await client.get("/health")).json()
    assert body["status"] == "error"
    assert body["stale_jobs"] == ["hevy_sync"]

    # A fresh stamp for that job brings it back.
    await redis.set("scheduler:last_run:hevy_sync", str(now))
    body = (await client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["stale_jobs"] == []


async def test_health_red_when_job_heartbeat_is_older_than_its_budget(client, redis):
    async def noop(_factory, _redis):
        return None

    now = int(time.time())
    await redis.set("scheduler:last_run:keepalive", str(now))
    scheduler_mod.register_job("hevy_sync", noop, trigger="interval", hours=6)
    # Last seen 9 hours ago — past the 6h schedule plus slack.
    await redis.set("scheduler:last_run:hevy_sync", str(now - 9 * 3600))

    body = (await client.get("/health")).json()
    assert body["status"] == "error"
    assert "hevy_sync" in body["stale_jobs"]
