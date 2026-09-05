"""APScheduler framework for Vitals.

The foundation ships the *framework* + a 1-minute ``keepalive`` heartbeat so
``/health`` can detect a stalled scheduler from day one. Per-module jobs (Hevy
every 6h, Garmin poll, weekly digest, plateau/lab checks) attach by calling
:func:`register_job` at import/startup — no edits here.

Every job runs under the Redis lock (single-runner across workers) and stamps a
heartbeat each tick. Job functions have the signature
``async def job(session_factory, redis) -> None``.
"""
from __future__ import annotations

from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.alerts import lifecycle as alerts_service_lifecycle

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import partial
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.scheduler.scheduler_lock import (
    SchedulerSlotClaimError,
    claim_subject_schedule_slot,
    record_scheduler_heartbeat,
    with_scheduler_lock,
)
from vitals.utils.timeutils import now_utc, subject_timezone

logger = logging.getLogger(__name__)

JobFunc = Callable[[async_sessionmaker[AsyncSession], Optional[Redis]], Awaitable[None]]
SubjectJobFunc = Callable[..., Awaitable[Any]]
SubjectScheduleResolver = Callable[
    [async_sessionmaker[AsyncSession], uuid.UUID],
    Awaitable[dict[str, Any] | None],
]

KEEPALIVE_JOB_ID = "keepalive"
SUBJECT_CRON_TRIGGER = "subject_cron"
SUBJECT_DISPATCH_GRACE_SECONDS = 45
SUBJECT_DISPATCH_LOCK_TTL_SECONDS = 55
SUBJECT_OCCURRENCE_JOB_PREFIX = "subject_occurrence:"

# One active alert per failing job — the id is part of the key (never a timestamp,
# which would defeat the dedupe index and pile up a row per failed tick).
JOB_FAILED_KEY_PREFIX = "scheduler.job_failed"


class JobFailureFamily(StrEnum):
    """Durable ownership boundary for one scheduler-failure alert."""

    PLATFORM = "platform"
    SUBJECT = "subject"
    GARMIN_ACCOUNT = "garmin_account"
    HEVY_ACCOUNT = "hevy_account"


# Job ids are part of the persisted alert key, so classification is an exact,
# reviewed registry rather than a prefix/default heuristic. Adding a job without
# deciding who owns its failure must fail before APScheduler can attach it.
JOB_FAILURE_FAMILY_BY_ID = MappingProxyType(
    {
        "raw_payload_sweep": JobFailureFamily.PLATFORM,
        "share_purge": JobFailureFamily.PLATFORM,
        "ai_invocation_reconcile": JobFailureFamily.PLATFORM,
        "notification_delivery_reconcile": JobFailureFamily.PLATFORM,
        "care_push_dispatch": JobFailureFamily.PLATFORM,
        "registration_admission_retention": JobFailureFamily.PLATFORM,
        "glp1_plateau": JobFailureFamily.SUBJECT,
        "hrt_reminders": JobFailureFamily.SUBJECT,
        "nutrition_day_end": JobFailureFamily.SUBJECT,
        "daily_brief": JobFailureFamily.SUBJECT,
        "nudges": JobFailureFamily.SUBJECT,
        "weekly_digest": JobFailureFamily.SUBJECT,
        "garmin_sync": JobFailureFamily.GARMIN_ACCOUNT,
        "garmin_weight_export": JobFailureFamily.GARMIN_ACCOUNT,
        "garmin_pulse": JobFailureFamily.GARMIN_ACCOUNT,
        "hevy_sync": JobFailureFamily.HEVY_ACCOUNT,
    }
)

# Health budgets crossing the web/worker boundary are deliberately capped by a
# reviewed, preference-independent value per job. Publishing the live budget
# would disclose exact user-selected cadences (for example Garmin sync hours)
# through Redis. The worker publishes only which jobs it owns; web maps those
# ids to these conservative maxima. The one-minute keepalive still detects a
# dead worker promptly, while an individually stalled job is allowed no longer
# than the widest supported schedule for that job plus the standard slack.
HEARTBEAT_BUDGET_CAP_SECONDS_BY_JOB = MappingProxyType(
    {
        KEEPALIVE_JOB_ID: 120.0,
        "raw_payload_sweep": 90_300.0,
        "share_purge": 90_300.0,
        "ai_invocation_reconcile": 1_200.0,
        "notification_delivery_reconcile": 1_200.0,
        "care_push_dispatch": 315.0,
        "registration_admission_retention": 3_900.0,
        "glp1_plateau": 90_300.0,
        "hrt_reminders": 90_300.0,
        "nutrition_day_end": 90_300.0,
        "daily_brief": 90_300.0,
        "nudges": 7_500.0,
        "weekly_digest": 608_700.0,
        "garmin_sync": 90_300.0,
        "garmin_weight_export": 86_700.0,
        "hevy_sync": 21_900.0,
    }
)


class JobFailureClassificationError(ValueError):
    """A scheduled job has no exact, reviewed failure-alert ownership family."""


class SchedulerHealthClassificationError(ValueError):
    """A heartbeating job has no safe preference-independent health cap."""


class SchedulerHeartbeatSeedError(RuntimeError):
    """One or more readiness heartbeats were not durably recorded."""


def _require_failure_family(
    job_id: str,
    failure_family: JobFailureFamily,
) -> None:
    if not isinstance(failure_family, JobFailureFamily):
        raise JobFailureClassificationError(
            "failure_family must be a JobFailureFamily member"
        )
    expected = JOB_FAILURE_FAMILY_BY_ID.get(job_id)
    if expected is None:
        raise JobFailureClassificationError(
            f"scheduled job {job_id!r} has no failure-alert classification"
        )
    if failure_family is not expected:
        raise JobFailureClassificationError(
            f"scheduled job {job_id!r} must use failure family {expected.value!r}"
        )


@dataclass
class JobSpec:
    id: str
    func: JobFunc
    trigger: str  # "interval" | "cron"
    failure_family: JobFailureFamily
    trigger_kwargs: dict = field(default_factory=dict)
    lock_ttl: int = 300
    heartbeat: bool = True
    subject_schedule_resolver: SubjectScheduleResolver | None = None


_registry: dict[str, JobSpec] = {}


def register_job(
    job_id: str,
    func: JobFunc,
    *,
    trigger: str,
    failure_family: JobFailureFamily,
    lock_ttl: int = 300,
    heartbeat: bool = True,
    **trigger_kwargs: Any,
) -> None:
    """Register a scheduled job. Modules call this at import/startup. Re-registering
    the same id replaces the previous spec."""
    _require_failure_family(job_id, failure_family)
    _registry[job_id] = JobSpec(
        id=job_id,
        func=func,
        trigger=trigger,
        failure_family=failure_family,
        trigger_kwargs=trigger_kwargs,
        lock_ttl=lock_ttl,
        heartbeat=heartbeat,
    )


def register_subject_cron_job(
    job_id: str,
    func: SubjectJobFunc,
    *,
    failure_family: JobFailureFamily,
    schedule_resolver: SubjectScheduleResolver | None = None,
    lock_ttl: int = 300,
    heartbeat: bool = True,
    **trigger_kwargs: Any,
) -> None:
    """Register one logical job whose cron is evaluated per subject timezone.

    APScheduler receives a minute-level dispatcher under the same logical id;
    the dispatcher creates short-lived one-shot jobs only for subjects whose
    current local minute matches this cron.  A resolver supplies per-subject
    cron fields for preferences such as Daily Brief's start time.
    """

    _require_failure_family(job_id, failure_family)
    if failure_family is not JobFailureFamily.SUBJECT:
        raise JobFailureClassificationError(
            "subject-local cron jobs must use the subject failure family"
        )
    if schedule_resolver is None and not trigger_kwargs:
        raise ValueError("subject-local cron jobs need cron fields or a resolver")
    if schedule_resolver is not None and trigger_kwargs:
        raise ValueError(
            "subject-local cron jobs use either fixed cron fields or a resolver"
        )
    _registry[job_id] = JobSpec(
        id=job_id,
        func=func,
        trigger=SUBJECT_CRON_TRIGGER,
        failure_family=failure_family,
        trigger_kwargs=trigger_kwargs,
        lock_ttl=lock_ttl,
        heartbeat=heartbeat,
        subject_schedule_resolver=schedule_resolver,
    )


def clear_jobs() -> None:
    """Drop all registered jobs (test isolation)."""
    _registry.clear()


def heartbeat_job_ids() -> list[str]:
    """Job ids ``/health`` should watch — the keepalive plus every registered job
    that records a heartbeat."""
    ids = [KEEPALIVE_JOB_ID]
    ids.extend(spec.id for spec in _registry.values() if spec.heartbeat)
    return ids


# How many upcoming fires to sample when sizing a job's staleness budget. Eight
# covers a full cycle of every schedule in use (a 4-times-a-day cron, a weekly
# digest), so the widest real gap is always among the samples.
_FIRE_SAMPLES = 8
# Slack on top of that gap: a tick may start late or run long, and a false
# "scheduler is dead" is worse than noticing a genuinely dead job minutes later.
_BUDGET_SLACK_SECONDS = 300.0


def _build_trigger(spec: JobSpec, scheduler_timezone):
    """Build one trigger without mutating a live scheduler."""

    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    if spec.trigger == SUBJECT_CRON_TRIGGER:
        # The real cron is evaluated against each subject's ZoneInfo inside the
        # dispatcher.  This process-level trigger is only a UTC-invariant minute
        # boundary and deliberately carries no person's schedule.
        return CronTrigger(timezone="UTC", minute="*", second=0)
    factory = CronTrigger if spec.trigger == "cron" else IntervalTrigger
    return factory(timezone=scheduler_timezone, **spec.trigger_kwargs)


def _validate_subject_cron_spec(spec: JobSpec) -> None:
    """Validate every static local cron before mutating a live scheduler."""

    if spec.trigger != SUBJECT_CRON_TRIGGER or spec.subject_schedule_resolver:
        return
    from apscheduler.triggers.cron import CronTrigger

    CronTrigger(timezone="UTC", **spec.trigger_kwargs)


def _subject_cron_matches(
    *,
    utc_minute: datetime,
    zone: ZoneInfo,
    trigger_kwargs: dict[str, Any],
) -> bool:
    """Whether a subject's cron contains exactly this absolute minute."""

    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger(timezone=zone, **trigger_kwargs)
    next_fire = trigger.get_next_fire_time(None, utc_minute)
    return (
        next_fire is not None
        and next_fire.astimezone(timezone.utc) == utc_minute
    )


def _subject_execution_lock_id(job_id: str, subject_id: uuid.UUID) -> str:
    """A scoped mutex name; the retained slot claim owns occurrence dedupe."""

    import hashlib

    opaque_subject = hashlib.sha256(str(subject_id).encode("ascii")).hexdigest()
    return f"{job_id}:subject:{opaque_subject}"


def _make_subject_occurrence_runner(
    spec: JobSpec,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Optional[Redis],
    *,
    subject_id: uuid.UUID,
    zone_name: str,
    deadline: datetime,
) -> Callable[[], Awaitable[None]]:
    """Build one independently executing, already-claimed subject occurrence."""

    async def _run() -> None:
        # A busy worker must omit a stale occurrence, never turn it into a late
        # notification or a day-end evaluation against the wrong date.
        if now_utc() > deadline:
            logger.warning(
                "%s occurrence for subject %s missed its current-slot deadline",
                spec.id,
                subject_id,
            )
            return

        executed = False

        async def _execute_locked() -> None:
            nonlocal executed
            # The outer check avoids needless lock traffic for work already
            # stale when APScheduler starts it.  This second check is the
            # authoritative one: acquiring the distributed mutex can itself
            # take the occurrence past its current-minute boundary.
            if now_utc() > deadline:
                logger.warning(
                    "%s occurrence for subject %s expired while waiting to run",
                    spec.id,
                    subject_id,
                )
                return
            executed = True
            with subject_timezone(zone_name):
                await spec.func(
                    session_factory,
                    redis,
                    subject_id=subject_id,
                )

        try:
            if redis is None:
                await _execute_locked()
            else:
                await with_scheduler_lock(
                    redis,
                    _subject_execution_lock_id(spec.id, subject_id),
                    spec.lock_ttl,
                    _execute_locked,
                )
        except Exception as exc:
            logger.exception(
                "%s failed for subject %s",
                spec.id,
                subject_id,
            )
            detail = str(exc)[:200] or exc.__class__.__name__
            await record_subject_job_outcome(
                session_factory,
                spec.id,
                detail,
                subject_id=subject_id,
            )
            raise
        else:
            # A busy subject mutex is an omitted occurrence, not proof that an
            # earlier failure recovered.
            if executed:
                await record_subject_job_outcome(
                    session_factory,
                    spec.id,
                    None,
                    subject_id=subject_id,
                )

    return _run


async def _record_subject_dispatch_error(
    spec: JobSpec,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    subject_id: uuid.UUID,
    error: BaseException,
) -> None:
    logger.exception(
        "%s could not dispatch subject %s",
        spec.id,
        subject_id,
        exc_info=error,
    )
    detail = str(error)[:200] or error.__class__.__name__
    await record_subject_job_outcome(
        session_factory,
        spec.id,
        detail,
        subject_id=subject_id,
    )


def _make_subject_dispatcher(
    spec: JobSpec,
    scheduler: AsyncIOScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Optional[Redis],
) -> JobFunc:
    """Turn one logical subject cron into a short minute-level dispatcher."""

    async def _dispatch(_session_factory, _redis) -> None:
        from vitals.scheduler.fanout import list_active_subject_schedules

        observed_at = now_utc()
        utc_minute = observed_at.replace(second=0, microsecond=0)
        deadline = utc_minute + timedelta(
            seconds=SUBJECT_DISPATCH_GRACE_SECONDS
        )
        if observed_at > deadline:
            # APScheduler's own grace normally drops this tick first.  Keep the
            # same fail-closed boundary when the dispatcher is invoked directly.
            return

        subjects = await list_active_subject_schedules(session_factory)
        if now_utc() > deadline:
            return
        for subject_id, zone_name in subjects:
            if now_utc() > deadline:
                return
            try:
                zone = ZoneInfo(zone_name)
                trigger_kwargs = spec.trigger_kwargs
                if spec.subject_schedule_resolver is not None:
                    resolved = await spec.subject_schedule_resolver(
                        session_factory,
                        subject_id,
                    )
                    if resolved is None:
                        continue
                    if not isinstance(resolved, dict):
                        raise TypeError("subject schedule resolver must return a dict")
                    trigger_kwargs = resolved
                if now_utc() > deadline:
                    return
                if not _subject_cron_matches(
                    utc_minute=utc_minute,
                    zone=zone,
                    trigger_kwargs=trigger_kwargs,
                ):
                    continue

                local_slot = utc_minute.astimezone(zone).replace(tzinfo=None)
                if redis is None:
                    # ``setup_scheduler`` supports a dependency-light local/test
                    # mode.  The production worker always constructs Redis; only
                    # that path promises cross-worker and DST-fold deduplication.
                    from vitals.scheduler.scheduler_lock import (
                        _subject_slot_claim_digest,
                    )

                    claim_digest = _subject_slot_claim_digest(
                        spec.id,
                        subject_id,
                        local_slot,
                    )
                else:
                    claim_digest = await claim_subject_schedule_slot(
                        redis,
                        job_id=spec.id,
                        subject_id=subject_id,
                        local_slot=local_slot,
                    )
                    if claim_digest is None:
                        continue
                if now_utc() > deadline:
                    return

                occurrence_id = (
                    f"{SUBJECT_OCCURRENCE_JOB_PREFIX}{spec.id}:{claim_digest}"
                )
                scheduler.add_job(
                    _make_subject_occurrence_runner(
                        spec,
                        session_factory,
                        redis,
                        subject_id=subject_id,
                        zone_name=zone_name,
                        deadline=deadline,
                    ),
                    trigger="date",
                    run_date=observed_at,
                    id=occurrence_id,
                    replace_existing=False,
                    misfire_grace_time=SUBJECT_DISPATCH_GRACE_SECONDS,
                )
            except SchedulerSlotClaimError:
                # An unavailable Redis is a process-wide safety failure.  Do not
                # keep walking and produce a partial unclaimed fan-out.
                raise
            except Exception as exc:  # noqa: BLE001 — isolate one bad subject
                await _record_subject_dispatch_error(
                    spec,
                    session_factory,
                    subject_id=subject_id,
                    error=exc,
                )

    return _dispatch


def _max_gap_seconds(spec: JobSpec, timezone: str) -> float:
    """Longest wait between two consecutive fires of ``spec``'s schedule.

    Measured with the job's own APScheduler trigger rather than parsed by hand —
    a cron at 03:00/11:00/16:00/22:00 has an 8-hour worst gap that no arithmetic
    over the kwargs would give us.
    """
    trigger = _build_trigger(spec, timezone)
    now = datetime.now(trigger.timezone)

    fires: list[datetime] = []
    previous: Optional[datetime] = None
    for _ in range(_FIRE_SAMPLES):
        nxt = trigger.get_next_fire_time(previous, previous or now)
        if nxt is None:  # a finite schedule that has run out
            break
        fires.append(nxt)
        previous = nxt

    gaps = [(b - a).total_seconds() for a, b in zip(fires, fires[1:])]
    return max(gaps) if gaps else 86400.0


def heartbeat_budgets(timezone: str) -> dict[str, float]:
    """How stale each watched job's heartbeat may get before ``/health`` calls the
    scheduler unhealthy, in seconds.

    A one-minute keepalive and an eight-hour Garmin poll cannot share a single
    threshold: the old fixed 120s only ever fitted the keepalive, so every module
    job could stop firing without turning ``/health`` red. Each job now gets a
    budget derived from its own schedule.
    """
    budgets = {KEEPALIVE_JOB_ID: 120.0}
    for spec in _registry.values():
        if spec.heartbeat:
            budgets[spec.id] = _max_gap_seconds(spec, timezone) + _BUDGET_SLACK_SECONDS
    caps = heartbeat_budget_caps(budgets)
    oversized = {
        job_id: budget
        for job_id, budget in budgets.items()
        if budget > caps[job_id]
    }
    if oversized:
        raise SchedulerHealthClassificationError(
            f"heartbeat budget exceeds reviewed cap: {sorted(oversized)}"
        )
    return budgets


def heartbeat_budget_caps(job_ids: Iterable[str]) -> dict[str, float]:
    """Return reviewed health caps without exposing live schedule preferences."""

    ids = tuple(job_ids)
    unknown = sorted(set(ids) - set(HEARTBEAT_BUDGET_CAP_SECONDS_BY_JOB))
    if unknown:
        raise SchedulerHealthClassificationError(
            f"heartbeating jobs need reviewed health caps: {unknown}"
        )
    return {
        job_id: HEARTBEAT_BUDGET_CAP_SECONDS_BY_JOB[job_id]
        for job_id in ids
    }


async def _record_job_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    spec: JobSpec,
    error: Optional[str],
) -> None:
    """The tick's outcome, for a job about the installation itself.

    Without this a broken sweep is invisible: the heartbeat is stamped before
    the run, so ``/health`` stays green while the data lake quietly stops
    filling.

    **A job about a record does not report here.** It used to — through a
    resolver that asked for "the sole subject" — and that was the same thing
    only while an installation was one person. With two it refused, and the
    refusal was swallowed by the handler below, so the alert never appeared at
    all; had it resolved, one person's failure would have been filed against
    whoever the resolver happened to return. Those jobs are fanned out and
    report per record from :func:`record_subject_job_outcome`.
    """

    from vitals.enums import Domain, Severity
    from vitals.i18n import t

    _require_failure_family(spec.id, spec.failure_family)
    if spec.failure_family is not JobFailureFamily.PLATFORM:
        return

    alert_key = f"{JOB_FAILED_KEY_PREFIX}:{spec.id}"
    try:
        async with session_factory() as session:
            context = alerts_service_contracts.PlatformAlertContext(
                namespace=alerts_service_contracts.PlatformAlertNamespace.SCHEDULER_JOB_FAILURE,
                actor_user_id=None,
            )
            if error is None:
                await alerts_service_lifecycle.resolve_scoped_by_key(
                    session,
                    context=context,
                    alert_key=alert_key,
                    legacy_bridge=alerts_service_contracts.LegacyAlertBridge.REJECT,
                )
            else:
                await alerts_service_lifecycle.raise_scoped_alert(
                    session,
                    context=context,
                    domain=Domain.SYSTEM,
                    severity=Severity.WARN,
                    message=t("alert.job_failed", job=spec.id, error=error),
                    alert_key=alert_key,
                    legacy_bridge=alerts_service_contracts.LegacyAlertBridge.REJECT,
                )
            await session.commit()
    except Exception:
        # Alert bookkeeping must never break the tick that reported the failure.
        logger.exception("Could not record outcome of scheduled job %s", spec.id)


async def record_subject_job_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    job_id: str,
    error: Optional[str],
    *,
    subject_id: uuid.UUID,
) -> None:
    """One record's outcome for one job, called once per record by the fan-out.

    The subject is mandatory and deliberately so: an omittable one is exactly
    the shape ``vitals/legacy_scope.py`` exists to keep out of this codebase,
    and the reason is this function's own history — the version that could be
    called without saying whose record it meant attributed every failure to the
    sole subject, and stopped attributing anything at all once there were two.
    """

    from vitals.enums import Domain, IntegrationProvider, Severity
    from vitals.i18n import t
    from vitals.services.tenancy.ownership import resolve_subject_ownership_context

    # The reviewed registry rather than a live ``JobSpec``: the family is a
    # property of the job, declared once and asserted by its own contract test,
    # and reading it from whatever happens to be registered would make this
    # silently do nothing in any context that builds a spec without registering
    # it — which is how the first version of this quietly recorded nothing.
    family = JOB_FAILURE_FAMILY_BY_ID.get(job_id)
    if family is None:
        raise JobFailureClassificationError(
            f"scheduled job {job_id!r} has no reviewed failure-alert family"
        )
    if family is JobFailureFamily.PLATFORM:
        return

    alert_key = f"{JOB_FAILED_KEY_PREFIX}:{job_id}"
    provider = {
        JobFailureFamily.GARMIN_ACCOUNT: IntegrationProvider.GARMIN,
        JobFailureFamily.HEVY_ACCOUNT: IntegrationProvider.HEVY,
    }.get(family)
    try:
        async with session_factory() as session:
            ownership = await resolve_subject_ownership_context(
                session,
                subject_id=subject_id,
                required_connections=((provider,) if provider is not None else ()),
            )
            if provider is None:
                context: alerts_service_contracts.AlertContext = (
                    alerts_service_contracts.HealthAlertContext(ownership.system_action())
                )
            else:
                context = alerts_service_contracts.ProviderAlertContext(
                    identity=ownership.system_action(),
                    provider=provider,
                    integration_connection_id=ownership.connection_id(provider),
                )
            legacy_bridge = alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED

            if error is None:
                await alerts_service_lifecycle.resolve_scoped_by_key(
                    session,
                    context=context,
                    alert_key=alert_key,
                    legacy_bridge=legacy_bridge,
                )
            else:
                await alerts_service_lifecycle.raise_scoped_alert(
                    session,
                    context=context,
                    domain=Domain.SYSTEM,
                    severity=Severity.WARN,
                    message=t("alert.job_failed", job=job_id, error=error),
                    alert_key=alert_key,
                    legacy_bridge=legacy_bridge,
                )
            await session.commit()
    except Exception:
        # Alert bookkeeping must never break the run that reported the failure.
        logger.exception(
            "Could not record outcome of scheduled job %s for subject %s",
            job_id,
            subject_id,
        )


def _make_runner(
    spec: JobSpec,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Optional[Redis],
) -> Callable[[], Awaitable[None]]:
    _require_failure_family(spec.id, spec.failure_family)

    async def _run() -> None:
        # Liveness stamp first — recorded every tick even when the lock is busy.
        if redis is not None and spec.heartbeat:
            await record_scheduler_heartbeat(redis, spec.id)
        executed = False

        async def _execute_locked() -> None:
            nonlocal executed
            executed = True
            await spec.func(session_factory, redis)

        try:
            if redis is None:
                await _execute_locked()
            else:
                await with_scheduler_lock(
                    redis,
                    spec.id,
                    spec.lock_ttl,
                    _execute_locked,
                )
        except Exception as exc:
            logger.exception("Scheduled job %s failed", spec.id)
            detail = str(exc)[:200] or exc.__class__.__name__
            await _record_job_outcome(session_factory, spec, detail)
        else:
            # A busy distributed lock is a skipped tick, not proof that the
            # previous failure recovered. Only an actually executed job may
            # clear its failure alert.
            if not executed:
                return
            await _record_job_outcome(session_factory, spec, None)

    return _run


async def _keepalive(redis: Optional[Redis]) -> None:
    if redis is not None:
        await record_scheduler_heartbeat(redis, KEEPALIVE_JOB_ID)


def apply_registry(
    scheduler: AsyncIOScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Optional[Redis] = None,
) -> None:
    """(Re)attach the registry to a scheduler — including a *running* one.

    This is what makes a settings save take effect without a restart:
    ``register_all_jobs`` rebuilds the registry from the new settings and this
    replaces the live jobs with it. Removing what is no longer registered matters
    just as much as adding — switch the Garmin pulse off and the old interval job
    would otherwise keep firing until the next deploy.
    """
    # Construct every trigger before touching the running scheduler. One bad
    # boundary value must not replace the first half of the jobs and then fail,
    # leaving a mixed registry that is retried and rephased on every poll.
    heartbeat_budgets(scheduler.timezone)
    for spec in _registry.values():
        _validate_subject_cron_spec(spec)
    prepared = [
        (spec, _build_trigger(spec, scheduler.timezone))
        for spec in _registry.values()
    ]
    for spec, trigger in prepared:
        runner_spec = spec
        job_options: dict[str, Any] = {}
        trigger_options = spec.trigger_kwargs
        if spec.trigger == SUBJECT_CRON_TRIGGER:
            runner_spec = replace(
                spec,
                func=_make_subject_dispatcher(
                    spec,
                    scheduler,
                    session_factory,
                    redis,
                ),
                # A crash during discovery must not strand a daily slot behind
                # the old service-execution TTL.  Retained occurrence claims
                # make overlap after this short TTL safe.
                lock_ttl=SUBJECT_DISPATCH_LOCK_TTL_SECONDS,
            )
            trigger_options = {}
            job_options = {
                "coalesce": True,
                "misfire_grace_time": SUBJECT_DISPATCH_GRACE_SECONDS,
                "max_instances": 1,
            }
        scheduler.add_job(
            _make_runner(runner_spec, session_factory, redis),
            trigger=trigger,
            id=spec.id,
            replace_existing=True,
            **job_options,
            **trigger_options,
        )

    keep = set(_registry) | {KEEPALIVE_JOB_ID}
    for job in scheduler.get_jobs():
        if (
            job.id not in keep
            and not job.id.startswith(SUBJECT_OCCURRENCE_JOB_PREFIX)
        ):
            scheduler.remove_job(job.id)


def setup_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Optional[Redis] = None,
    *,
    timezone: str = "Europe/Chisinau",
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone)

    apply_registry(scheduler, session_factory, redis)

    # Always-on heartbeat so a dead scheduler is detectable even before any module
    # job is registered.
    #
    # ``partial``, not ``lambda: _keepalive(redis)``: a lambda that *returns* a
    # coroutine is not a coroutine function, so APScheduler's executor called it
    # synchronously and threw the coroutine away — the heartbeat was never
    # recorded, and /health reported the scheduler dead from two minutes after
    # boot forever. ``partial`` keeps ``iscoroutinefunction`` true.
    scheduler.add_job(
        partial(_keepalive, redis),
        trigger="interval",
        minutes=1,
        id=KEEPALIVE_JOB_ID,
        replace_existing=True,
    )
    return scheduler


async def seed_heartbeats(
    redis: Optional[Redis],
    *,
    job_ids: Optional[Iterable[str]] = None,
) -> None:
    """Seed every monitored heartbeat at startup so ``/health`` is green
    immediately (APScheduler's first interval tick is one minute out).

    A live reload may pass only newly enabled ids. Refreshing every existing
    heartbeat during an unrelated settings save would hide a stalled infrequent
    job for its complete schedule budget.
    """
    if redis is None:
        return
    failed_job_ids: list[str] = []
    for job_id in heartbeat_job_ids() if job_ids is None else job_ids:
        if not await record_scheduler_heartbeat(redis, job_id):
            failed_job_ids.append(job_id)
    if failed_job_ids:
        raise SchedulerHeartbeatSeedError(
            f"could not seed scheduler heartbeats: {sorted(failed_job_ids)}"
        )
