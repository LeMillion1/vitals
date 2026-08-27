"""Optional modules, proactive scheduling, and language routes."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.process_mode import ProcessMode, load_process_mode
from vitals.services.modules import preferences as modules_service
from vitals.services.preferences import language as language_service
from vitals.services.tenancy.ownership import resolve_legacy_ownership_context
from vitals.services.modules.preferences import ModuleToggleError
from vitals.services.proactive.preferences import contracts as preference_contracts
from vitals.services.proactive.preferences import queries as preference_queries
from vitals.services.proactive.preferences import writes as preference_writes
from web.deps import get_redis, get_session, require_auth
from web.ratelimit import rate_limit
from web.templating import templates

from .common import compatibility_override, redirect as _redirect

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/modules")
async def toggle_module(
    request: Request,
    module: str = Form(...),
    enabled: bool = Form(...),
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    _rl: None = Depends(rate_limit("settings_modules", limit=30, window=60)),
):
    """Enable/disable an Optional dashboard module, on the fly.

    Persists to ``app_settings`` (source of truth), write-through to Redis, then
    returns an OOB fragment that re-renders the header nav so it updates live —
    no page reload.
    """
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    try:
        state = await modules_service.set_module_enabled(
            db,
            key=module,
            enabled=enabled,
            subject_id=ownership.subject_id,
        )
    except ModuleToggleError as e:
        # Core/unknown module — reject loudly (Zero Silent Errors).
        return JSONResponse({"error": str(e)}, status_code=status.HTTP_400_BAD_REQUEST)

    await db.commit()
    await modules_service.prime_cache(
        redis,
        state,
        subject_id=ownership.subject_id,
    )
    # Reflect the new state for the OOB nav render in *this* response.
    request.state.enabled_modules = state
    return templates.TemplateResponse(
        request,
        "partials/modules_oob.html",
        {"username": username, "enabled_modules": state},
    )


# ── Two-factor auth ───────────────────────────────────────────────────────────



@router.post("/proactive")
async def save_proactive(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    brief_time: str = Form(preference_contracts.DEFAULTS["brief_time"]),
    garmin_sync_hours: int = Form(preference_contracts.DEFAULTS["garmin_sync_hours"]),
    garmin_weight_export_minutes: int = Form(
        preference_contracts.DEFAULTS["garmin_weight_export_minutes"]
    ),
    garmin_weight_max_age_days: int = Form(
        preference_contracts.DEFAULTS["garmin_weight_max_age_days"]
    ),
    pulse_seconds: int = Form(preference_contracts.DEFAULTS["pulse_seconds"]),
    pulse_start_hour: int = Form(preference_contracts.DEFAULTS["pulse_start_hour"]),
    pulse_end_hour: int = Form(preference_contracts.DEFAULTS["pulse_end_hour"]),
):
    """Save proactive settings and rebuild a process-local schedule when present.

    Everything else on this page writes ``.env`` and needs a restart; these are in
    the DB precisely so they don't. ``preference_codec.sanitize`` clamps whatever arrives —
    the HTML min/max are a courtesy, not the guard.

    The card no longer offers quiet hours, the daily budget or the nudge
    switches: every one of them gates a *send*, and with the Telegram transport
    gone there is nothing to send with. The stored policy keeps them, because the
    delivery engine still reads it and a first web push has to be governed by
    something — so this handler reads the current values and writes them back
    unchanged rather than letting the ``Form`` defaults quietly reset whatever the
    owner last chose.
    """
    preference_scope = await preference_queries.resolve_legacy_preferences_scope(
        db,
        actor_username=username,
    )
    current = (
        await preference_queries.get_preferences_bundle(
            db,
            scope=preference_scope,
            actor_username=username,
        )
    ).as_flat_dict()
    raw_prefs = {
        **current,
        "brief_time": brief_time,
        "garmin_sync_hours": garmin_sync_hours,
        "garmin_weight_export_minutes": garmin_weight_export_minutes,
        "garmin_weight_max_age_days": garmin_weight_max_age_days,
        "pulse_seconds": pulse_seconds,
        "pulse_start_hour": pulse_start_hour,
        "pulse_end_hour": pulse_end_hour,
    }
    settings = (
        await preference_writes.set_preferences_bundle(
            db,
            raw_prefs,
            scope=preference_scope,
            actor_username=username,
        )
    ).as_flat_dict()
    # Asked before the commit closes the transaction, and answered about this
    # person: the scheduler registry is one per process, so rebuilding it from
    # a save re-times everybody's jobs. Whose Save that is allowed to be is a
    # question the row itself cannot answer.
    governs_schedule = await preference_queries.governs_the_process_schedule(
        db, subject_id=preference_scope.subject_id
    )
    await db.commit()

    schedule_applied = False
    reload_failed = False
    if governs_schedule:
        process_mode = compatibility_override("load_process_mode", load_process_mode)()
        if process_mode is ProcessMode.COMBINED:
            schedule_applied = compatibility_override(
                "apply_schedule", apply_schedule
            )(request.app, settings)
        elif process_mode is ProcessMode.WEB:
            schedule_applied = await compatibility_override(
                "signal_schedule_reload", signal_schedule_reload
            )()
            reload_failed = not schedule_applied
    # preference_codec.sanitize() (called inside set_preferences_bundle) silently clamps
    # out-of-range
    # input — compare what was submitted to what actually got stored so the
    # user can be told, instead of seeing a plain "saved" while their number
    # was quietly changed underneath them.
    adjusted = raw_prefs != settings
    query = "?saved=proactive"
    if adjusted:
        query += "&adjusted=1"
    deferred = not governs_schedule or reload_failed
    if deferred:
        # Saved, and deliberately not applied to the running scheduler. A plain
        # "saved" here would be true about the row and false about the effect,
        # which is the worse of the two silences.
        query += "&deferred=reload" if reload_failed else "&deferred=1"
    return _redirect(query)


async def signal_schedule_reload() -> bool:
    """Publish an opaque split-worker generation within a bounded wait."""

    from vitals.scheduler.control import (
        WEB_SIGNAL_TIMEOUT_SECONDS,
        request_schedule_reload,
    )
    from web.deps import get_redis_client

    try:
        async with asyncio.timeout(WEB_SIGNAL_TIMEOUT_SECONDS):
            await request_schedule_reload(get_redis_client())
    except Exception:
        # The preference commit already succeeded. PostgreSQL polling will
        # discover it, but the immediate wake/ack path is delayed; report that
        # honestly rather than turning a durable write into an HTTP 500.
        logger.exception("could not signal the split scheduler reload")
        return False
    return True


def apply_schedule(app, settings: dict) -> bool:
    """Re-register the jobs and push them onto the running scheduler.

    Best-effort on purpose: the settings *are* saved by the time this runs, so a
    scheduler that isn't up (tests, a worker that never started one) must not turn
    a successful save into a 500 — the new schedule is picked up at next boot
    either way.
    """
    from vitals.scheduler.jobs import register_all_jobs
    from vitals.scheduler.scheduler import apply_registry
    from web.deps import get_redis_client, get_session_factory

    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is None:
        return False
    try:
        register_all_jobs(settings)
        apply_registry(scheduler, get_session_factory(), get_redis_client())
    except Exception:
        logger.exception("could not apply the new schedule; it takes effect on restart")
        return False
    return True


@router.post("/language")
async def save_language(
    request: Request,
    language: str = Form(...),
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    lang = await language_service.set_language(
        db,
        language,
        redis=None,
        user_id=ownership.owner_user_id,
    )
    await db.commit()
    await language_service.prime_cache(
        redis,
        lang,
        user_id=ownership.owner_user_id,
    )
    return RedirectResponse(
        url="/settings?saved=language",
        status_code=status.HTTP_303_SEE_OTHER,
    )
