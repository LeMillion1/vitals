"""Per-module scheduled-job registrations.

The scheduler framework (``scheduler.py``) only ships the keepalive heartbeat;
each module attaches its jobs by calling :func:`register_job`. This module
gathers those calls behind :func:`register_all_jobs`, invoked once from the web
lifespan before :func:`setup_scheduler` reads the registry.

Keeping registration here (not at model/service import time) means importing a
service for a unit test never schedules anything — the test ``clear_jobs``
fixture stays effective and jobs only exist when the app actually boots.
"""
from __future__ import annotations

from vitals.config import load_config
from vitals.scheduler.scheduler import register_job


def register_all_jobs() -> None:
    """Register every module's scheduled jobs. Idempotent (re-registering an id
    replaces it), so it's safe to call once per startup."""
    from vitals.services.glp1_service import plateau_job
    from vitals.services.hevy_service import sync_job as hevy_sync_job
    from vitals.services.garmin_service import sync_job as garmin_sync_job
    from vitals.services.digest_service import digest_job
    from vitals.services.nutrition_service import day_end_job as nutrition_day_end_job
    from vitals.services.hrt_reminders import reminders_job as hrt_reminders_job
    from vitals.services.garmin_service import pulse_job as garmin_pulse_job
    from vitals.services.proactive.brief import brief_job
    from vitals.services.proactive.day_plan import evening_job
    from vitals.services.proactive.nudges import nudges_job

    # GLP-1 plateau check — once a day at 06:00 local. Cheap read; raises/clears a
    # passive warn alert so it's fresh even on days the dashboard isn't opened.
    register_job(
        "glp1_plateau",
        plateau_job,
        trigger="cron",
        hour=6,
        minute=0,
    )

    # HRT reminders — once a day at 07:00 local. Nags for overdue bloodwork
    # (while on cycle) and for missed scheduled injections; both idempotent.
    register_job(
        "hrt_reminders",
        hrt_reminders_job,
        trigger="cron",
        hour=7,
        minute=0,
    )

    # Nutrition day-end check — once a day at 23:00 local, once today's logged
    # totals are effectively final. Raises the very-low-calorie/protein GLP-1
    # warnings, which must never fire off a partial mid-day total.
    register_job(
        "nutrition_day_end",
        nutrition_day_end_job,
        trigger="cron",
        hour=23,
        minute=0,
    )

    # Hevy sync — every 6h. No-ops when Hevy isn't configured.
    register_job(
        "hevy_sync",
        hevy_sync_job,
        trigger="interval",
        hours=6,
    )

    # Garmin poll — scheduled at 03:00, 11:00, 16:00, and 22:00 local.
    # No-ops when Garmin isn't configured.
    register_job(
        "garmin_sync",
        garmin_sync_job,
        trigger="cron",
        hour="3,11,16,22",
        minute=0,
    )

    # Morning brief — 11:00 local. Syncs Garmin itself first (last night's sleep is
    # the point of the message), stays silent on an empty day, and sends nothing at
    # all until a Telegram channel is configured. Its own lock TTL: the Garmin pull
    # in front of it makes this the slowest job in the registry.
    register_job(
        "daily_brief",
        brief_job,
        trigger="cron",
        hour=11,
        minute=0,
        lock_ttl=900,
    )

    # Evening block — 23:45 local, deliberately not midnight: past 00:00 the
    # message would ask about the wrong "tomorrow". Sums the day up, invites a
    # free-text answer, and offers one-tap corrections to tomorrow's template.
    register_job(
        "evening_block",
        evening_job,
        trigger="cron",
        hour=23,
        minute=45,
    )

    # Garmin light pulse (N3) — today's step count between the four full syncs, so
    # an evening nudge isn't reasoning off a number from 16:00. One request, no
    # login (the token session is resumed), and skipped outside active hours.
    # Interval from VITALS_GARMIN_PULSE_MINUTES; 0 switches the job off entirely.
    pulse_minutes = load_config().garmin_pulse_minutes
    if pulse_minutes:
        register_job(
            "garmin_pulse",
            garmin_pulse_job,
            trigger="interval",
            minutes=pulse_minutes,
            lock_ttl=120,
            heartbeat=False,
        )

    # Nudges — hourly, at :05 so it never lands on top of the polls. Nothing is
    # sent unless a condition actually holds; quiet hours and the daily budget are
    # enforced downstream by delivery.send.
    register_job(
        "nudges",
        nudges_job,
        trigger="cron",
        minute=5,
    )

    # Weekly AI digest — Mondays at 08:00 local. No-ops when no OpenRouter key.
    register_job(
        "weekly_digest",
        digest_job,
        trigger="cron",
        day_of_week="mon",
        hour=8,
        minute=0,
    )
