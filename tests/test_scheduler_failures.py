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

import asyncio
import time

import pytest
from sqlalchemy import select

from vitals.enums import Domain, IntegrationProvider
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.scheduler import scheduler as scheduler_mod
from vitals.scheduler.fanout import for_each_connection, for_each_subject
from vitals.scheduler.jobs import register_all_jobs
from vitals.scheduler.scheduler_lock import scheduler_heartbeat_age
from vitals.services import alerts_service



def _runner(session_factory, job_id: str, func, redis=None):
    """A runner around the job *as it is registered*, fan-out included.

    Wrapping a bare function used to be the same thing. It is not any more: a
    job about a record reports its outcome per record, from inside the fan-out,
    because the runner's own version asked for "the sole subject" and had
    nothing honest to say once there were two. A test that skips the wrapper
    tests a shape production does not have.
    """

    family = scheduler_mod.JOB_FAILURE_FAMILY_BY_ID[job_id]
    if family is scheduler_mod.JobFailureFamily.GARMIN_ACCOUNT:
        wrapped = for_each_connection(
            _subject_arity(func), job_id=job_id, provider=IntegrationProvider.GARMIN
        )
    elif family is scheduler_mod.JobFailureFamily.HEVY_ACCOUNT:
        wrapped = for_each_connection(
            _subject_arity(func), job_id=job_id, provider=IntegrationProvider.HEVY
        )
    elif family is scheduler_mod.JobFailureFamily.SUBJECT:
        wrapped = for_each_subject(_subject_arity(func), job_id=job_id)
    else:
        wrapped = func

    spec = scheduler_mod.JobSpec(
        id=job_id,
        func=wrapped,
        trigger="interval",
        failure_family=family,
        trigger_kwargs={"hours": 6},
    )
    return scheduler_mod._make_runner(spec, session_factory, redis)


def _subject_arity(func):
    """The two-argument test doubles here, in the shape a fan-out calls."""

    async def _call(session_factory, redis=None, **kwargs):
        del kwargs
        return await func(session_factory, redis)

    _call.__name__ = getattr(func, "__name__", "job")
    _call.__module__ = getattr(func, "__module__", __name__)
    return _call


async def test_failing_job_raises_owned_provider_alert_and_does_not_propagate(
    session_factory,
    db_session,
    legacy_owner_roots,
    garmin_connected,
):
    """``garmin_connected`` because the fan-out runs per *account*.

    A subject with a connection root and no credential has not connected a
    watch, so there is nothing for this job to do for them and nothing that can
    fail — which is why they are absent from the fan-out rather than present and
    reporting an outage.
    """
    async def boom(_factory, _redis):
        raise RuntimeError("Garmin said no")

    await _runner(session_factory, "garmin_sync", boom)()  # must not raise

    alerts = await alerts_service.list_active(db_session, domain=Domain.SYSTEM.value, subject_id=legacy_owner_roots.subject_id)
    failed = [a for a in alerts if a.alert_key == "scheduler.job_failed:garmin_sync"]
    assert len(failed) == 1
    assert failed[0].severity == "warn"
    assert "Garmin said no" in failed[0].message
    garmin_connection_id = await db_session.scalar(
        select(IntegrationConnection.id).where(
            IntegrationConnection.subject_id == legacy_owner_roots.subject_id,
            IntegrationConnection.provider == IntegrationProvider.GARMIN.value,
        )
    )
    assert (failed[0].subject_id, failed[0].integration_connection_id) == (
        legacy_owner_roots.subject_id,
        garmin_connection_id,
    )


async def test_repeated_failures_do_not_pile_up(
    session_factory,
    db_session,
    legacy_owner_roots,
    hevy_connected,
):
    async def boom(_factory, _redis):
        raise RuntimeError("still down")

    run = _runner(session_factory, "hevy_sync", boom)
    await run()
    await run()
    await run()

    alerts = await alerts_service.list_active(db_session, domain=Domain.SYSTEM.value, subject_id=legacy_owner_roots.subject_id)
    failed = [a for a in alerts if a.alert_key == "scheduler.job_failed:hevy_sync"]
    assert len(failed) == 1, "the alert key must dedupe, not add a row per failed tick"


async def test_successful_run_clears_the_alert_without_a_human_actor(
    session_factory,
    db_session,
    legacy_owner_roots,
    hevy_connected,
):
    state = {"fail": True}

    async def flaky(_factory, _redis):
        if state["fail"]:
            raise RuntimeError("transient")

    run = _runner(session_factory, "hevy_sync", flaky)
    await run()
    assert [
        a for a in await alerts_service.list_active(db_session, domain=Domain.SYSTEM.value, subject_id=legacy_owner_roots.subject_id)
        if a.alert_key == "scheduler.job_failed:hevy_sync"
    ]

    state["fail"] = False
    await run()
    assert not [
        a for a in await alerts_service.list_active(db_session, domain=Domain.SYSTEM.value, subject_id=legacy_owner_roots.subject_id)
        if a.alert_key == "scheduler.job_failed:hevy_sync"
    ]
    row = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.alert_key == "scheduler.job_failed:hevy_sync"
        )
    )
    assert row is not None
    assert row.subject_id == legacy_owner_roots.subject_id
    assert row.resolved_by_user_id is None


async def test_lock_busy_tick_does_not_clear_previous_failure(
    session_factory,
    db_session,
    legacy_owner_roots,
    hevy_connected,
    redis,
):
    calls = 0

    async def boom(_factory, _redis):
        raise RuntimeError("still failing")

    async def would_succeed(_factory, _redis):
        nonlocal calls
        calls += 1

    await _runner(session_factory, "hevy_sync", boom, redis)()
    row = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.alert_key == "scheduler.job_failed:hevy_sync"
        )
    )
    assert row is not None
    assert row.resolved_at is None

    await redis.set("scheduler:lock:hevy_sync", "another-worker", ex=300)
    await _runner(session_factory, "hevy_sync", would_succeed, redis)()

    await db_session.refresh(row)
    assert calls == 0
    assert row.resolved_at is None


async def test_platform_job_failure_stays_outside_subject_alerts(
    session_factory,
    db_session,
    legacy_owner_roots,
):
    async def boom(_factory, _redis):
        raise RuntimeError("platform sweep failed")

    await _runner(session_factory, "raw_payload_sweep", boom)()

    row = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.alert_key == "scheduler.job_failed:raw_payload_sweep"
        )
    )
    assert row is not None
    assert (row.subject_id, row.integration_connection_id) == (None, None)
    subject_alerts = await alerts_service.list_active_scoped(
        db_session,
        context=alerts_service.HealthAlertContext(
            WriteIdentity(legacy_owner_roots.subject_id, None)
        ),
        legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
    )
    assert row.id not in {item.id for item in subject_alerts}


async def test_subject_job_failure_has_subject_without_provider_connection(
    session_factory,
    db_session,
    legacy_owner_roots,
):
    async def boom(_factory, _redis):
        raise RuntimeError("subject digest failed")

    await _runner(session_factory, "weekly_digest", boom)()

    row = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.alert_key == "scheduler.job_failed:weekly_digest"
        )
    )
    assert row is not None
    assert (row.subject_id, row.integration_connection_id) == (
        legacy_owner_roots.subject_id,
        None,
    )


def test_unknown_job_failure_family_fails_closed_before_runner_creation():
    async def noop(_factory, _redis):
        return None

    spec = scheduler_mod.JobSpec(
        id="unclassified_future_job",
        func=noop,
        trigger="interval",
        failure_family=scheduler_mod.JobFailureFamily.SUBJECT,
        trigger_kwargs={"hours": 1},
    )
    with pytest.raises(
        scheduler_mod.JobFailureClassificationError,
        match="no failure-alert classification",
    ):
        scheduler_mod._make_runner(spec, lambda: None, None)


def test_job_failure_family_registry_is_complete_and_matches_alert_keys():
    register_all_jobs()

    assert {
        job_id: spec.failure_family for job_id, spec in scheduler_mod._registry.items()
    } == dict(scheduler_mod.JOB_FAILURE_FAMILY_BY_ID)

    scheduler_alert_ids = {
        key.removeprefix(f"{scheduler_mod.JOB_FAILED_KEY_PREFIX}:")
        for key in set().union(
            alerts_service.HEALTH_ALERT_KEYS,
            *alerts_service.PROVIDER_ALERT_KEYS.values(),
            *alerts_service.PLATFORM_ALERT_KEYS.values(),
        )
        if key.startswith(f"{scheduler_mod.JOB_FAILED_KEY_PREFIX}:")
    }
    assert scheduler_alert_ids == set(scheduler_mod.JOB_FAILURE_FAMILY_BY_ID)
    assert {
        job_id
        for job_id, family in scheduler_mod.JOB_FAILURE_FAMILY_BY_ID.items()
        if family is scheduler_mod.JobFailureFamily.PLATFORM
    } == {
        "raw_payload_sweep",
        "share_purge",
        "ai_invocation_reconcile",
        "notification_delivery_reconcile",
    }
    for job_id in (
        "ai_invocation_reconcile",
        "notification_delivery_reconcile",
    ):
        reconciliation = scheduler_mod._registry[job_id]
        assert reconciliation.trigger == "interval"
        assert reconciliation.trigger_kwargs == {"minutes": 15}
        assert (
            reconciliation.failure_family
            is scheduler_mod.JobFailureFamily.PLATFORM
        )


# ── The keepalive itself ─────────────────────────────────────────────────────
async def test_keepalive_job_actually_records_its_heartbeat(redis):
    """The one always-on liveness signal has to run — and to *do* something.

    It was registered as ``lambda: _keepalive(redis)``. A lambda that returns a
    coroutine is not a coroutine function, so APScheduler's executor ran it
    synchronously and discarded the coroutine: the stamp was never written, and
    /health called the scheduler dead two minutes after every boot, forever. The
    tests below all set ``scheduler:last_run:keepalive`` by hand, which is exactly
    why none of them noticed — so this one runs the job APScheduler was handed.
    """
    scheduler = scheduler_mod.setup_scheduler(lambda: None, redis)
    job = scheduler.get_job(scheduler_mod.KEEPALIVE_JOB_ID)

    assert asyncio.iscoroutinefunction(job.func), (
        "the keepalive must be a coroutine function — anything else is called "
        "synchronously and its coroutine thrown away"
    )

    await job.func()
    assert await scheduler_heartbeat_age(redis, scheduler_mod.KEEPALIVE_JOB_ID) is not None


# ── Per-job heartbeat budgets ────────────────────────────────────────────────
async def test_budget_matches_each_job_schedule():
    async def noop(_factory, _redis):
        return None

    scheduler_mod.register_job(
        "hevy_sync",
        noop,
        trigger="interval",
        failure_family=scheduler_mod.JobFailureFamily.HEVY_ACCOUNT,
        hours=6,
    )
    scheduler_mod.register_job(
        "garmin_sync",
        noop,
        trigger="cron",
        failure_family=scheduler_mod.JobFailureFamily.GARMIN_ACCOUNT,
        hour="3,11,16,22",
        minute=0,
    )
    scheduler_mod.register_job(
        "weekly_digest",
        noop,
        trigger="cron",
        failure_family=scheduler_mod.JobFailureFamily.SUBJECT,
        day_of_week="mon",
        hour=8,
    )

    budgets = scheduler_mod.heartbeat_budgets("Europe/Chisinau")
    slack = scheduler_mod._BUDGET_SLACK_SECONDS

    assert budgets[scheduler_mod.KEEPALIVE_JOB_ID] == 120.0
    assert budgets["hevy_sync"] == 6 * 3600 + slack
    # The widest real gap of 03/11/16/22 is 8h, not 24h/4 — a job may legitimately
    # go that long between runs without being stale.
    assert budgets["garmin_sync"] == 8 * 3600 + slack
    assert budgets["weekly_digest"] == 7 * 86400 + slack


# These read the job names out of /health, which only the owner is shown — hence
# auth_client. The anonymous shape is checked in tests/test_web.py.
async def test_health_red_when_any_job_heartbeat_is_overdue(auth_client, redis):
    async def noop(_factory, _redis):
        return None

    now = int(time.time())
    await redis.set("scheduler:last_run:keepalive", str(now))

    # A job registered but never heard from: /health used to watch only the
    # keepalive and stayed green while every module job was dead.
    scheduler_mod.register_job(
        "hevy_sync",
        noop,
        trigger="interval",
        failure_family=scheduler_mod.JobFailureFamily.HEVY_ACCOUNT,
        hours=6,
    )

    body = (await auth_client.get("/health")).json()
    assert body["status"] == "error"
    assert body["stale_jobs"] == ["hevy_sync"]

    # A fresh stamp for that job brings it back.
    await redis.set("scheduler:last_run:hevy_sync", str(now))
    body = (await auth_client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["stale_jobs"] == []


async def test_health_red_when_job_heartbeat_is_older_than_its_budget(auth_client, redis):
    async def noop(_factory, _redis):
        return None

    now = int(time.time())
    await redis.set("scheduler:last_run:keepalive", str(now))
    scheduler_mod.register_job(
        "hevy_sync",
        noop,
        trigger="interval",
        failure_family=scheduler_mod.JobFailureFamily.HEVY_ACCOUNT,
        hours=6,
    )
    # Last seen 9 hours ago — past the 6h schedule plus slack.
    await redis.set("scheduler:last_run:hevy_sync", str(now - 9 * 3600))

    body = (await auth_client.get("/health")).json()
    assert body["status"] == "error"
    assert "hevy_sync" in body["stale_jobs"]
