"""Subject-local cron dispatch without duplicate or catch-up executions."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.scheduler import scheduler as scheduler_module
from vitals.scheduler.fanout import list_active_subject_schedules
from vitals.scheduler.scheduler_lock import (
    SUBJECT_SLOT_CLAIM_TTL_SECONDS,
    _subject_slot_claim_digest,
    claim_subject_schedule_slot,
)


class SchedulerProbe:
    def __init__(self) -> None:
        self.added: list[SimpleNamespace] = []

    def add_job(self, func, **kwargs):
        row = SimpleNamespace(func=func, **kwargs)
        self.added.append(row)
        return row


def _subject_spec(job, **trigger_kwargs):
    return scheduler_module.JobSpec(
        id="weekly_digest",
        func=job,
        trigger=scheduler_module.SUBJECT_CRON_TRIGGER,
        failure_family=scheduler_module.JobFailureFamily.SUBJECT,
        trigger_kwargs=trigger_kwargs,
    )


def test_subject_cron_matches_fractional_offset_zone() -> None:
    zone = ZoneInfo("Asia/Kathmandu")

    assert scheduler_module._subject_cron_matches(
        utc_minute=datetime(2026, 9, 7, 2, 20, tzinfo=timezone.utc),
        zone=zone,
        trigger_kwargs={"hour": 8, "minute": 5},
    )
    assert not scheduler_module._subject_cron_matches(
        utc_minute=datetime(2026, 9, 7, 2, 5, tzinfo=timezone.utc),
        zone=zone,
        trigger_kwargs={"hour": 8, "minute": 5},
    )


def test_dst_gap_is_skipped_and_fold_has_one_wall_slot_identity() -> None:
    zone = ZoneInfo("America/New_York")
    cron = {"hour": 1, "minute": 30}
    first_fold = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    second_fold = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)

    assert scheduler_module._subject_cron_matches(
        utc_minute=first_fold,
        zone=zone,
        trigger_kwargs=cron,
    )
    assert scheduler_module._subject_cron_matches(
        utc_minute=second_fold,
        zone=zone,
        trigger_kwargs=cron,
    )
    subject_id = uuid.uuid4()
    first_local = first_fold.astimezone(zone).replace(tzinfo=None)
    second_local = second_fold.astimezone(zone).replace(tzinfo=None)
    assert first_local == second_local
    assert _subject_slot_claim_digest(
        "weekly_digest", subject_id, first_local
    ) == _subject_slot_claim_digest("weekly_digest", subject_id, second_local)

    nonexistent = {"hour": 2, "minute": 30}
    spring_window = (
        datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc)
        + timedelta(minutes=offset)
        for offset in range(180)
    )
    assert not any(
        scheduler_module._subject_cron_matches(
            utc_minute=candidate,
            zone=zone,
            trigger_kwargs=nonexistent,
        )
        for candidate in spring_window
    )


async def test_dst_fold_dispatcher_claims_the_repeated_wall_slot_once(
    monkeypatch,
    redis,
) -> None:
    from vitals.scheduler import fanout

    subject_id = uuid.uuid4()

    async def subjects(_factory):
        return [(subject_id, "America/New_York")]

    current = {"now": datetime(2026, 11, 1, 5, 30, 5, tzinfo=timezone.utc)}
    monkeypatch.setattr(fanout, "list_active_subject_schedules", subjects)
    monkeypatch.setattr(scheduler_module, "now_utc", lambda: current["now"])
    probe = SchedulerProbe()

    async def job(*_args, **_kwargs):
        return None

    dispatch = scheduler_module._make_subject_dispatcher(
        _subject_spec(job, hour=1, minute=30),
        probe,
        object(),
        redis,
    )

    await dispatch(object(), redis)
    current["now"] = datetime(2026, 11, 1, 6, 30, 5, tzinfo=timezone.utc)
    await dispatch(object(), redis)

    assert len(probe.added) == 1


async def test_slot_claim_is_opaque_retained_and_atomic(redis) -> None:
    subject_id = uuid.uuid4()
    slot = datetime(2026, 9, 7, 8, 0)

    first, second = await asyncio.gather(
        claim_subject_schedule_slot(
            redis,
            job_id="weekly_digest",
            subject_id=subject_id,
            local_slot=slot,
        ),
        claim_subject_schedule_slot(
            redis,
            job_id="weekly_digest",
            subject_id=subject_id,
            local_slot=slot,
        ),
    )

    assert sorted(item is None for item in (first, second)) == [False, True]
    digest = first or second
    assert digest is not None
    key = f"scheduler:subject_slot:v1:{digest}"
    assert str(subject_id) not in key
    assert slot.isoformat() not in key
    assert 0 < await redis.ttl(key) <= SUBJECT_SLOT_CLAIM_TTL_SECONDS


async def test_dispatcher_runs_only_the_subject_due_at_this_instant(
    monkeypatch,
    redis,
) -> None:
    from vitals.scheduler import fanout

    almaty = uuid.uuid4()
    los_angeles = uuid.uuid4()

    async def subjects(_factory):
        return [(almaty, "Asia/Almaty"), (los_angeles, "America/Los_Angeles")]

    monkeypatch.setattr(fanout, "list_active_subject_schedules", subjects)
    observed = datetime(2026, 9, 7, 3, 0, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler_module, "now_utc", lambda: observed)
    outcomes = []

    async def record(_factory, job_id, error, *, subject_id):
        outcomes.append((job_id, error, subject_id))

    monkeypatch.setattr(scheduler_module, "record_subject_job_outcome", record)
    ran = []

    async def job(_factory, _redis, *, subject_id):
        ran.append(subject_id)

    probe = SchedulerProbe()
    spec = _subject_spec(job, hour=8, minute=0)
    dispatch = scheduler_module._make_subject_dispatcher(
        spec,
        probe,
        object(),
        redis,
    )

    await dispatch(object(), redis)

    assert len(probe.added) == 1
    assert probe.added[0].id.startswith(
        f"{scheduler_module.SUBJECT_OCCURRENCE_JOB_PREFIX}weekly_digest:"
    )
    await probe.added[0].func()
    assert ran == [almaty]
    assert outcomes == [("weekly_digest", None, almaty)]


async def test_schedule_resolver_failure_is_isolated_to_one_subject(
    monkeypatch,
    redis,
) -> None:
    from vitals.scheduler import fanout

    malformed = uuid.uuid4()
    healthy = uuid.uuid4()

    async def subjects(_factory):
        return [(malformed, "UTC"), (healthy, "UTC")]

    async def resolve(_factory, subject_id):
        if subject_id == malformed:
            raise ValueError("malformed subject schedule")
        return {"hour": 8, "minute": 0}

    monkeypatch.setattr(fanout, "list_active_subject_schedules", subjects)
    observed = datetime(2026, 9, 7, 8, 0, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler_module, "now_utc", lambda: observed)
    outcomes = []

    async def record(_factory, job_id, error, *, subject_id):
        outcomes.append((job_id, error, subject_id))

    monkeypatch.setattr(scheduler_module, "record_subject_job_outcome", record)
    ran = []

    async def job(_factory, _redis, *, subject_id):
        ran.append(subject_id)

    spec = scheduler_module.JobSpec(
        id="weekly_digest",
        func=job,
        trigger=scheduler_module.SUBJECT_CRON_TRIGGER,
        failure_family=scheduler_module.JobFailureFamily.SUBJECT,
        subject_schedule_resolver=resolve,
    )
    probe = SchedulerProbe()
    dispatch = scheduler_module._make_subject_dispatcher(
        spec,
        probe,
        object(),
        redis,
    )

    await dispatch(object(), redis)

    assert len(probe.added) == 1
    await probe.added[0].func()
    assert ran == [healthy]
    assert outcomes == [
        ("weekly_digest", "malformed subject schedule", malformed),
        ("weekly_digest", None, healthy),
    ]


async def test_invalid_subject_timezone_does_not_block_a_healthy_subject(
    monkeypatch,
    redis,
) -> None:
    from vitals.scheduler import fanout

    invalid = uuid.uuid4()
    healthy = uuid.uuid4()

    async def subjects(_factory):
        return [(invalid, "Not/A_Timezone"), (healthy, "UTC")]

    monkeypatch.setattr(fanout, "list_active_subject_schedules", subjects)
    observed = datetime(2026, 9, 7, 8, 0, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler_module, "now_utc", lambda: observed)
    outcomes = []

    async def record(_factory, job_id, error, *, subject_id):
        outcomes.append((job_id, error, subject_id))

    monkeypatch.setattr(scheduler_module, "record_subject_job_outcome", record)
    ran = []

    async def job(_factory, _redis, *, subject_id):
        ran.append(subject_id)

    probe = SchedulerProbe()
    dispatch = scheduler_module._make_subject_dispatcher(
        _subject_spec(job, hour=8, minute=0),
        probe,
        object(),
        redis,
    )

    await dispatch(object(), redis)
    assert len(probe.added) == 1
    await probe.added[0].func()

    assert ran == [healthy]
    assert outcomes[0][0::2] == ("weekly_digest", invalid)
    assert "Not/A_Timezone" in outcomes[0][1]
    assert outcomes[1] == ("weekly_digest", None, healthy)


@pytest.mark.parametrize("delayed_stage", ["discovery", "resolver"])
async def test_dispatcher_drops_a_slot_that_expires_during_preparation(
    monkeypatch,
    redis,
    delayed_stage,
) -> None:
    from vitals.scheduler import fanout

    subject_id = uuid.uuid4()
    current = {"now": datetime(2026, 9, 7, 8, 0, 5, tzinfo=timezone.utc)}

    async def subjects(_factory):
        if delayed_stage == "discovery":
            current["now"] = current["now"].replace(second=46)
        return [(subject_id, "UTC")]

    async def resolve(_factory, _subject_id):
        if delayed_stage == "resolver":
            current["now"] = current["now"].replace(second=46)
        return {"hour": 8, "minute": 0}

    monkeypatch.setattr(fanout, "list_active_subject_schedules", subjects)
    monkeypatch.setattr(scheduler_module, "now_utc", lambda: current["now"])
    probe = SchedulerProbe()

    async def job(*_args, **_kwargs):
        raise AssertionError("expired preparation must not dispatch work")

    spec = _subject_spec(job, hour=8, minute=0)
    if delayed_stage == "resolver":
        spec.trigger_kwargs = {}
        spec.subject_schedule_resolver = resolve
    dispatch = scheduler_module._make_subject_dispatcher(
        spec,
        probe,
        object(),
        redis,
    )

    await dispatch(object(), redis)

    assert probe.added == []


async def test_redis_claim_failure_does_not_dispatch_unclaimed_work(
    monkeypatch,
) -> None:
    from vitals.scheduler import fanout

    subject_id = uuid.uuid4()

    async def subjects(_factory):
        return [(subject_id, "UTC")]

    class UnavailableRedis:
        async def set(self, *_args, **_kwargs):
            raise ConnectionError("synthetic Redis outage")

    monkeypatch.setattr(fanout, "list_active_subject_schedules", subjects)
    monkeypatch.setattr(
        scheduler_module,
        "now_utc",
        lambda: datetime(2026, 9, 7, 8, 0, 5, tzinfo=timezone.utc),
    )
    probe = SchedulerProbe()

    async def job(*_args, **_kwargs):
        raise AssertionError("unclaimed work must never run")

    dispatch = scheduler_module._make_subject_dispatcher(
        _subject_spec(job, hour=8, minute=0),
        probe,
        object(),
        UnavailableRedis(),
    )

    with pytest.raises(
        scheduler_module.SchedulerSlotClaimError,
        match="could not claim subject-local slot",
    ):
        await dispatch(object(), None)

    assert probe.added == []


async def test_failed_occurrence_keeps_claim_and_later_skip_cannot_clear_it(
    monkeypatch,
    redis,
) -> None:
    from vitals.scheduler import fanout

    subject_id = uuid.uuid4()

    async def subjects(_factory):
        return [(subject_id, "UTC")]

    monkeypatch.setattr(fanout, "list_active_subject_schedules", subjects)
    observed = datetime(2026, 9, 7, 8, 0, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler_module, "now_utc", lambda: observed)
    outcomes = []

    async def record(_factory, job_id, error, *, subject_id):
        outcomes.append((job_id, error, subject_id))

    monkeypatch.setattr(scheduler_module, "record_subject_job_outcome", record)

    async def boom(_factory, _redis, *, subject_id):
        raise RuntimeError(f"broken {subject_id}")

    probe = SchedulerProbe()
    spec = _subject_spec(boom, hour=8, minute=0)
    dispatch = scheduler_module._make_subject_dispatcher(
        spec,
        probe,
        object(),
        redis,
    )
    await dispatch(object(), redis)
    with pytest.raises(RuntimeError, match="broken"):
        await probe.added[0].func()

    await dispatch(object(), redis)

    assert len(probe.added) == 1, "the retained failed-slot claim must not replay"
    assert len(outcomes) == 1
    assert outcomes[0][1].startswith("broken")

    # A different claimed slot whose subject mutex is busy is also not evidence
    # that the existing failure recovered.
    tomorrow = observed + timedelta(days=1)
    monkeypatch.setattr(scheduler_module, "now_utc", lambda: tomorrow)
    busy_key = (
        "scheduler:lock:"
        + scheduler_module._subject_execution_lock_id(
            "weekly_digest", subject_id
        )
    )
    await redis.set(busy_key, "other-worker", ex=300)
    busy = scheduler_module._make_subject_occurrence_runner(
        spec,
        object(),
        redis,
        subject_id=subject_id,
        zone_name="UTC",
        deadline=tomorrow + timedelta(seconds=30),
    )
    await busy()
    assert len(outcomes) == 1


async def test_occurrence_rechecks_deadline_after_waiting_for_subject_lock(
    monkeypatch,
) -> None:
    subject_id = uuid.uuid4()
    current = {"now": datetime(2026, 9, 7, 8, 0, 5, tzinfo=timezone.utc)}
    deadline = current["now"].replace(second=45)
    monkeypatch.setattr(scheduler_module, "now_utc", lambda: current["now"])
    outcomes = []

    async def record(*_args, **_kwargs):
        outcomes.append((_args, _kwargs))

    async def delayed_lock(_redis, _job_id, _ttl, fn):
        current["now"] = current["now"].replace(second=46)
        return await fn()

    monkeypatch.setattr(scheduler_module, "record_subject_job_outcome", record)
    monkeypatch.setattr(scheduler_module, "with_scheduler_lock", delayed_lock)
    ran = []

    async def job(*_args, **_kwargs):
        ran.append(True)

    runner = scheduler_module._make_subject_occurrence_runner(
        _subject_spec(job, hour=8, minute=0),
        object(),
        object(),
        subject_id=subject_id,
        zone_name="UTC",
        deadline=deadline,
    )

    await runner()

    assert ran == []
    assert outcomes == []


async def test_occurrence_binds_the_subject_timezone_while_job_runs(
    monkeypatch,
) -> None:
    from vitals.utils import timeutils

    subject_id = uuid.uuid4()
    observed = datetime(2026, 9, 7, 8, 0, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler_module, "now_utc", lambda: observed)
    monkeypatch.setattr(
        scheduler_module,
        "record_subject_job_outcome",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    seen = []

    async def job(*_args, **_kwargs):
        seen.append(timeutils._zone().key)

    runner = scheduler_module._make_subject_occurrence_runner(
        _subject_spec(job, hour=8, minute=0),
        object(),
        None,
        subject_id=subject_id,
        zone_name="Pacific/Kiritimati",
        deadline=observed + timedelta(seconds=30),
    )

    await runner()

    assert seen == ["Pacific/Kiritimati"]


async def test_slow_occurrence_does_not_block_next_minute_other_subject(
    monkeypatch,
    redis,
) -> None:
    from vitals.scheduler import fanout

    first_subject = uuid.uuid4()
    second_subject = uuid.uuid4()
    current = {"now": datetime(2026, 9, 7, 8, 0, 5, tzinfo=timezone.utc)}
    visible = {"subjects": [(first_subject, "UTC")]}
    started = asyncio.Event()
    release = asyncio.Event()
    ran = []

    async def subjects(_factory):
        return visible["subjects"]

    async def record(*_args, **_kwargs):
        return None

    async def job(_factory, _redis, *, subject_id):
        ran.append(subject_id)
        if subject_id == first_subject:
            started.set()
            await release.wait()

    monkeypatch.setattr(fanout, "list_active_subject_schedules", subjects)
    monkeypatch.setattr(scheduler_module, "now_utc", lambda: current["now"])
    monkeypatch.setattr(scheduler_module, "record_subject_job_outcome", record)
    probe = SchedulerProbe()
    spec = _subject_spec(job, minute="*")
    dispatch = scheduler_module._make_subject_dispatcher(
        spec,
        probe,
        object(),
        redis,
    )

    await dispatch(object(), redis)
    first_task = asyncio.create_task(probe.added[0].func())
    await started.wait()

    current["now"] += timedelta(minutes=1)
    visible["subjects"] = [(second_subject, "UTC")]
    await dispatch(object(), redis)
    assert len(probe.added) == 2
    await probe.added[1].func()
    assert ran == [first_subject, second_subject]

    release.set()
    await first_task


async def test_active_schedule_discovery_excludes_suspended_owner(
    session_factory,
    db_session,
    legacy_owner_roots,
) -> None:
    active_subject = await db_session.get(
        HealthSubject,
        legacy_owner_roots.subject_id,
    )
    active_subject.timezone = "Asia/Almaty"
    suspended = User(
        username="suspended-schedule",
        normalized_username="suspended-schedule",
        password_hash="synthetic-test-hash",
        status=UserStatus.SUSPENDED.value,
    )
    db_session.add(suspended)
    await db_session.flush()
    suspended_subject = HealthSubject(
        owner_user_id=suspended.id,
        timezone="UTC",
    )
    db_session.add(suspended_subject)
    await db_session.commit()

    rows = await list_active_subject_schedules(session_factory)

    assert (legacy_owner_roots.subject_id, "Asia/Almaty") in rows
    assert suspended_subject.id not in {subject_id for subject_id, _zone in rows}


async def test_daily_brief_schedule_is_read_from_each_subject_row(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    redis,
) -> None:
    from vitals.models.scoped_settings import SubjectSetting
    from vitals.scheduler.jobs import _daily_brief_schedule
    from vitals.services.proactive.preferences.contracts import SUBJECT_POLICY_KEY

    second_owner = User(
        username="second-brief-owner",
        normalized_username="second-brief-owner",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(second_owner)
    await db_session.flush()
    second_subject = HealthSubject(
        owner_user_id=second_owner.id,
        timezone="UTC",
    )
    db_session.add(second_subject)
    await db_session.flush()
    second_subject_id = second_subject.id
    first_subject = await db_session.get(
        HealthSubject,
        legacy_owner_roots.subject_id,
    )
    first_subject.timezone = "UTC"

    rows = {
        legacy_owner_roots.subject_id: SubjectSetting(
            subject_id=legacy_owner_roots.subject_id,
            key=SUBJECT_POLICY_KEY,
            value={
                "brief_time": "09:17",
                "nudges": {
                    "activity": True,
                    "nutrition": True,
                    "data": True,
                },
            },
        ),
        second_subject_id: SubjectSetting(
            subject_id=second_subject_id,
            key=SUBJECT_POLICY_KEY,
            value={
                "brief_time": "13:41",
                "nudges": {
                    "activity": True,
                    "nutrition": True,
                    "data": True,
                },
            },
        ),
    }
    for subject_id, new_row in rows.items():
        existing = await db_session.get(
            SubjectSetting,
            (subject_id, SUBJECT_POLICY_KEY),
        )
        if existing is None:
            db_session.add(new_row)
        else:
            existing.value = new_row.value
    await db_session.commit()
    independent_factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    assert await _daily_brief_schedule(
        independent_factory,
        legacy_owner_roots.subject_id,
    ) == {"hour": "9-14", "minute": 17}
    assert await _daily_brief_schedule(
        independent_factory,
        second_subject_id,
    ) == {"hour": "13-18", "minute": 41}

    current = {"now": datetime(2026, 9, 7, 9, 17, 5, tzinfo=timezone.utc)}
    monkeypatch.setattr(scheduler_module, "now_utc", lambda: current["now"])

    async def record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scheduler_module, "record_subject_job_outcome", record)
    ran = []

    async def brief(_factory, _redis, *, subject_id):
        ran.append(subject_id)

    probe = SchedulerProbe()
    spec = scheduler_module.JobSpec(
        id="daily_brief",
        func=brief,
        trigger=scheduler_module.SUBJECT_CRON_TRIGGER,
        failure_family=scheduler_module.JobFailureFamily.SUBJECT,
        subject_schedule_resolver=_daily_brief_schedule,
    )
    dispatch = scheduler_module._make_subject_dispatcher(
        spec,
        probe,
        independent_factory,
        redis,
    )

    await dispatch(independent_factory, redis)
    assert len(probe.added) == 1
    await probe.added[0].func()
    current["now"] = datetime(2026, 9, 7, 13, 41, 5, tzinfo=timezone.utc)
    await dispatch(independent_factory, redis)
    assert len(probe.added) == 2
    await probe.added[1].func()

    assert ran == [legacy_owner_roots.subject_id, second_subject_id]


def test_subject_dispatcher_is_minutely_and_reload_preserves_occurrence() -> None:
    scheduler_module.register_subject_cron_job(
        "weekly_digest",
        lambda *_args, **_kwargs: None,
        failure_family=scheduler_module.JobFailureFamily.SUBJECT,
        day_of_week="mon",
        hour=8,
        minute=0,
    )
    scheduler = scheduler_module.setup_scheduler(lambda: None, None, timezone="UTC")
    logical = scheduler.get_job("weekly_digest")
    assert "minute='*'" in str(logical.trigger)
    assert str(logical.trigger.timezone) == "UTC"
    assert scheduler_module.heartbeat_budgets("UTC")["weekly_digest"] == 360.0

    async def pending_occurrence():
        return None

    occurrence_id = f"{scheduler_module.SUBJECT_OCCURRENCE_JOB_PREFIX}test"
    scheduler.add_job(
        pending_occurrence,
        trigger="date",
        run_date=datetime.now(timezone.utc) + timedelta(days=1),
        id=occurrence_id,
    )
    scheduler_module.apply_registry(scheduler, lambda: None, None)

    assert scheduler.get_job(occurrence_id) is not None


async def test_dispatcher_never_catches_up_a_previous_minute(
    monkeypatch,
    redis,
) -> None:
    from vitals.scheduler import fanout

    subject_id = uuid.uuid4()

    async def subjects(_factory):
        return [(subject_id, "UTC")]

    monkeypatch.setattr(fanout, "list_active_subject_schedules", subjects)
    monkeypatch.setattr(
        scheduler_module,
        "now_utc",
        lambda: datetime(2026, 9, 7, 8, 1, 1, tzinfo=timezone.utc),
    )
    probe = SchedulerProbe()

    async def job(*_args, **_kwargs):
        raise AssertionError("the 08:00 slot must not be replayed at 08:01")

    await scheduler_module._make_subject_dispatcher(
        _subject_spec(job, hour=8, minute=0),
        probe,
        object(),
        redis,
    )(object(), redis)

    assert probe.added == []
