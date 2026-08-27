"""Per-module scheduled-job registrations.

The scheduler framework (``scheduler.py``) only ships the keepalive heartbeat;
each module attaches its jobs by calling :func:`register_job`. This module
gathers those calls behind :func:`register_all_jobs`. The standalone worker
uses it before :func:`setup_scheduler` reads the registry; the compatibility
combined process uses the same lifecycle. A web-only process prepares the same
registry for health/manifest expectations but never starts it, and settings
changes signal the worker to reload.

Keeping registration here (not at model/service import time) means importing a
service for a unit test never schedules anything — the test ``clear_jobs``
fixture stays effective and jobs only exist when the app actually boots.
"""
from __future__ import annotations


from typing import Any, Optional

from vitals.enums import IntegrationProvider
from vitals.scheduler.fanout import for_each_connection, for_each_subject
from vitals.scheduler.scheduler import JobFailureFamily, clear_jobs, register_job
from vitals.services.proactive.preferences import codec as preference_codec


def register_all_jobs(settings: Optional[dict[str, Any]] = None) -> None:
    """Register every module's scheduled jobs from ``settings``.

    ``None`` → the defaults, which is what the app used before the settings card
    existed. Called at startup *and* on every settings save, so it clears the
    registry first: re-registering has to be able to *remove* a job (the Garmin
    pulse switched off), not only replace one.
    """
    settings = preference_codec.sanitize(settings)
    clear_jobs()

    # Every job below runs once per record. That took two removals and a
    # migration to become true, and the comment that used to be here — naming
    # eight jobs that could not be fanned out — is worth remembering rather than
    # deleting:
    #
    #   daily_brief, nudges  were blocked by one Telegram bot token and one chat
    #       id in the environment. The transport is gone; what they compose now
    #       goes to a delivery journal that is per subject by construction.
    #   hevy_sync, garmin_sync, garmin_pulse, garmin_weight_export  were blocked
    #       by VITALS_GARMIN_EMAIL and VITALS_HEVY_API_KEY — one watch and one
    #       workout account for the whole process. Running them per subject
    #       would have filed the operator's own data as everybody else's, which
    #       is a disclosure rather than the visible outage they were instead.
    #       ``integration_credentials`` gave each account its own credential,
    #       token store, session cache and login breaker.
    #
    # The provider four use ``for_each_connection`` rather than
    # ``for_each_subject``: a subject who has not connected a watch has nothing
    # for them to do, and enumerating them would mean four scheduled no-ops a
    # day per person.
    from vitals.services.glp1.jobs import plateau_job
    from vitals.services.hevy.jobs import sync_job as hevy_sync_job
    from vitals.services.garmin.jobs import sync_job as garmin_sync_job
    from vitals.services.garmin_weight.contracts import OPERATION_LOCK_TTL_SECONDS
    from vitals.services.garmin_weight.jobs import (
        export_job as garmin_weight_export_job,
    )
    from vitals.services.digest.jobs import digest_job
    from vitals.services.nutrition.jobs import day_end_job as nutrition_day_end_job
    from vitals.services.hrt.reminders import reminders_job as hrt_reminders_job
    from vitals.services.garmin.jobs import pulse_job as garmin_pulse_job
    from vitals.services.proactive.brief.jobs import brief_job, last_attempt_hour
    from vitals.services.proactive.nudges import nudges_job
    from vitals.services.proactive.delivery.reconciliation import (
        delivery_reconciliation_job,
    )
    from vitals.services.notifications.care_push_dispatcher import dispatch_job
    from vitals.services.authentication.admission.retention import (
        maintenance_job as registration_admission_retention_job,
    )
    from vitals.services.data_lake.sweep import sweep_pending_job as raw_payload_sweep_job
    from vitals.services.share.jobs import purge_job as share_purge_job
    from vitals.services.ai_gateway.jobs import (
        reconciliation_job as ai_invocation_reconciliation_job,
    )

    # GLP-1 plateau check — once a day at 06:00 local. Cheap read; raises/clears a
    # passive warn alert so it's fresh even on days the dashboard isn't opened.
    register_job(
        "glp1_plateau",
        for_each_subject(plateau_job, job_id="glp1_plateau"),
        trigger="cron",
        failure_family=JobFailureFamily.SUBJECT,
        hour=6,
        minute=0,
    )

    # HRT reminders — once a day at 07:00 local. Nags for overdue bloodwork
    # (while on cycle) and for missed scheduled injections; both idempotent.
    register_job(
        "hrt_reminders",
        for_each_subject(hrt_reminders_job, job_id="hrt_reminders"),
        trigger="cron",
        failure_family=JobFailureFamily.SUBJECT,
        hour=7,
        minute=0,
    )

    # Nutrition day-end check — once a day at 23:00 local, once today's logged
    # totals are effectively final. Raises the very-low-calorie/protein GLP-1
    # warnings, which must never fire off a partial mid-day total.
    register_job(
        "nutrition_day_end",
        for_each_subject(nutrition_day_end_job, job_id="nutrition_day_end"),
        trigger="cron",
        failure_family=JobFailureFamily.SUBJECT,
        hour=23,
        minute=0,
    )

    # Raw-payload sweep (garmin/hevy/labs/body_comp) — nightly at 03:30 local, a
    # quiet hour clear of every other registered job. Re-derives normalized rows
    # for anything upsert_owned_raw_payload left at processed_at IS NULL: a refreshed
    # Garmin/Hevy row, or a labs/body-comp upload the owner extracted but never
    # confirmed. See raw_sweep.sweep_pending_job for why this is one
    # shared job rather than four.
    register_job(
        "raw_payload_sweep",
        for_each_subject(raw_payload_sweep_job, job_id="raw_payload_sweep"),
        trigger="cron",
        failure_family=JobFailureFamily.PLATFORM,
        hour=3,
        minute=30,
    )

    # Doctor-report cleanup — nightly at 04:00 local, right after the raw-payload
    # sweep. Empties the frozen snapshot of every link whose lifetime has run out;
    # the row stays so /share can still show what was shared and when.
    register_job(
        "share_purge",
        share_purge_job,
        trigger="cron",
        failure_family=JobFailureFamily.PLATFORM,
        hour=4,
        minute=0,
    )

    # Platform-funded AI accounting recovery — no provider I/O.  It releases
    # reservations abandoned between prepare and dispatch, and conservatively
    # closes paid dispatches whose process died before finalization.
    register_job(
        "ai_invocation_reconcile",
        ai_invocation_reconciliation_job,
        trigger="interval",
        failure_family=JobFailureFamily.PLATFORM,
        minutes=15,
    )

    # Durable notification-intent recovery is provider-free. It marks abandoned
    # pending/dispatching state conservatively; it never retries Telegram I/O.
    register_job(
        "notification_delivery_reconcile",
        delivery_reconciliation_job,
        trigger="interval",
        failure_family=JobFailureFamily.PLATFORM,
        minutes=15,
    )

    # Consent-rechecked generic care wakeups. The common scheduler runner owns
    # the Redis lock; the service owns only bounded database claims and performs
    # provider I/O after their transaction has committed.
    register_job(
        "care_push_dispatch",
        dispatch_job,
        trigger="interval",
        failure_family=JobFailureFamily.PLATFORM,
        seconds=15,
        lock_ttl=180,
    )

    # Account-admission proofs are short-lived and applicant PII is retained
    # only for the bounded audit window. The primitives process finite batches;
    # an hourly tick catches up safely after downtime without one long lock.
    register_job(
        "registration_admission_retention",
        registration_admission_retention_job,
        trigger="interval",
        failure_family=JobFailureFamily.PLATFORM,
        hours=1,
    )

    # Hevy sync — every 6h. No-ops when Hevy isn't configured.
    register_job(
        "hevy_sync",
        for_each_connection(
            hevy_sync_job, job_id="hevy_sync", provider=IntegrationProvider.HEVY
        ),
        trigger="interval",
        failure_family=JobFailureFamily.HEVY_ACCOUNT,
        hours=6,
    )

    # Garmin poll — every N hours on the clock (default 6 → 00/06/12/18, the same
    # four polls a day the old fixed 3/11/16/22 cron did). A cron rather than an
    # interval so the times survive a deploy instead of re-phasing off boot.
    # No-ops when Garmin isn't configured.
    garmin_sync_hours = settings["garmin_sync_hours"]
    register_job(
        "garmin_sync",
        for_each_connection(
            garmin_sync_job,
            job_id="garmin_sync",
            provider=IntegrationProvider.GARMIN,
        ),
        trigger="cron",
        failure_family=JobFailureFamily.GARMIN_ACCOUNT,
        # APScheduler's hour field is 0..23, so ``*/24`` is invalid even
        # though a 24-hour cadence is a supported preference. Midnight is the
        # stable once-daily phase for that boundary value.
        hour=0 if garmin_sync_hours == 24 else f"*/{garmin_sync_hours}",
        minute=0,
    )

    # Outbound weight sync is a separate opt-in outbox. The job always exists so
    # the DB-backed switch applies without rebuilding the schedule; while off it
    # returns before constructing a Garmin client or touching the network.
    register_job(
        "garmin_weight_export",
        for_each_connection(
            garmin_weight_export_job,
            job_id="garmin_weight_export",
            provider=IntegrationProvider.GARMIN,
        ),
        trigger="interval",
        failure_family=JobFailureFamily.GARMIN_ACCOUNT,
        minutes=settings["garmin_weight_export_minutes"],
        lock_ttl=OPERATION_LOCK_TTL_SECONDS,
    )

    # Morning brief — 11:00 local by default. Syncs Garmin itself first (last
    # night's sleep is the point of the message), stays silent on an empty day, and
    # sends nothing at all until a Telegram channel is configured. Its own lock
    # TTL: the Garmin pull in front of it makes this the slowest job in the registry.
    #
    # An hourly *range*, not one fire: at the scheduled minute last night may not
    # be scored yet (he is still asleep, the watch has not closed the night), and
    # the brief refuses to read recovery off a running night. Each later hour
    # looks again; the delivery journal keeps it to one message a day, and the job
    # returns before the Garmin pull once that message has gone.
    brief_hour, brief_minute = preference_codec.hhmm(settings["brief_time"])
    register_job(
        "daily_brief",
        for_each_subject(brief_job, job_id="daily_brief"),
        trigger="cron",
        failure_family=JobFailureFamily.SUBJECT,
        hour=f"{brief_hour}-{last_attempt_hour(brief_hour)}",
        minute=brief_minute,
        lock_ttl=900,
    )


    # Garmin light pulse (N3) — today's step count between the full syncs, so an
    # evening nudge isn't reasoning off a number from hours ago. One request, no
    # login (the token session is resumed), and skipped outside the active hours
    # from the settings card. 0 seconds switches the job off entirely.
    if settings["pulse_seconds"]:
        register_job(
            "garmin_pulse",
            for_each_connection(
                garmin_pulse_job,
                job_id="garmin_pulse",
                provider=IntegrationProvider.GARMIN,
            ),
            trigger="interval",
            failure_family=JobFailureFamily.GARMIN_ACCOUNT,
            seconds=settings["pulse_seconds"],
            lock_ttl=120,
            heartbeat=False,
        )

    # Nudges — hourly, at :05 so it never lands on top of the polls. Nothing is
    # sent unless a condition actually holds; quiet hours and the daily budget are
    # enforced downstream by delivery_legacy.send.
    register_job(
        "nudges",
        for_each_subject(nudges_job, job_id="nudges"),
        trigger="cron",
        failure_family=JobFailureFamily.SUBJECT,
        minute=5,
    )

    # Weekly AI digest — Mondays at 08:00 local. The database gateway/quota state
    # is authoritative; an unavailable platform gateway causes a clean no-op.
    register_job(
        "weekly_digest",
        for_each_subject(digest_job, job_id="weekly_digest"),
        trigger="cron",
        failure_family=JobFailureFamily.SUBJECT,
        day_of_week="mon",
        hour=8,
        minute=0,
    )
