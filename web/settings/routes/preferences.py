"""Optional modules, proactive scheduling, and language routes."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.integrations.garmin_client import login_breaker_state
from vitals.process_mode import ProcessMode, load_process_mode
from vitals.services.credentials import providers as credential_providers
from vitals.services.modules import preferences as modules_service
from vitals.services.preferences import language as language_service
from vitals.services.profile import health as health_profile_service
from vitals.services.tenancy.ownership import resolve_legacy_ownership_context
from vitals.services.modules.preferences import ModuleToggleError
from vitals.services.proactive.preferences import contracts as preference_contracts
from vitals.services.proactive.preferences import queries as preference_queries
from vitals.services.proactive.preferences import writes as preference_writes
from web.deps import get_redis, get_session, require_auth
from web.ratelimit import rate_limit
from web.templating import templates

from .common import (
    SETTINGS_BRIEF_PATH,
    SETTINGS_PROFILE_PATH,
    compatibility_override,
    redirect as _redirect,
)

logger = logging.getLogger(__name__)
router = APIRouter()


async def render_modules_settings(
    request: Request,
    username: str,
    *,
    db: AsyncSession,
) -> HTMLResponse:
    """Render the subject-scoped module switches and no other settings."""

    # The global chrome map is already loaded, but direct navigation still has
    # to prove that this account owns a record before offering write controls.
    await resolve_legacy_ownership_context(db, actor_username=username)
    return templates.TemplateResponse(
        request,
        "settings/modules.html",
        {
            "username": username,
            "enabled_modules": (
                getattr(request.state, "enabled_modules", {}) or {}
            ),
        },
    )


@router.get("/modules", response_class=HTMLResponse)
async def modules_settings_page(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    return await render_modules_settings(
        request,
        username,
        db=db,
    )


async def render_brief_settings(
    request: Request,
    username: str,
    *,
    db: AsyncSession,
    redis: Optional[Redis] = None,
    saved: Optional[str] = None,
    error: Optional[str] = None,
    adjusted: Optional[str] = None,
    deferred: Optional[str] = None,
) -> HTMLResponse:
    """Render the existing Brief and scheduler preference bundle."""

    scope = await preference_queries.resolve_legacy_preferences_scope(
        db,
        actor_username=username,
    )
    garmin_account = await credential_providers.resolve_garmin_account(
        db,
        subject_id=scope.subject_id,
    )
    breaker = await compatibility_override(
        "login_breaker_state",
        login_breaker_state,
    )(redis, garmin_account.namespace if garmin_account else "")
    proactive = (
        await preference_queries.get_preferences_bundle(
            db,
            scope=scope,
            actor_username=username,
        )
    ).as_flat_dict()
    subject_timezone = await health_profile_service.get_subject_timezone(
        db,
        subject_id=scope.subject_id,
    )
    return templates.TemplateResponse(
        request,
        "settings/brief.html",
        {
            "username": username,
            "saved": saved,
            "error": error,
            "adjusted": adjusted,
            "deferred": deferred,
            "timezone": subject_timezone or load_config().timezone,
            "proactive": proactive,
            "breaker": breaker,
            "nudge_categories": preference_contracts.NUDGE_CATEGORIES,
            "budget_range": preference_contracts.BUDGET_RANGE,
            "sync_hours_range": preference_contracts.SYNC_HOURS_RANGE,
            "pulse_range": preference_contracts.PULSE_SECONDS_RANGE,
            "weight_export_minutes_range": (
                preference_contracts.WEIGHT_EXPORT_MINUTES_RANGE
            ),
            "weight_max_age_days_range": (
                preference_contracts.WEIGHT_MAX_AGE_DAYS_RANGE
            ),
        },
    )


@router.get("/brief", response_class=HTMLResponse)
async def brief_settings_page(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    saved: Optional[str] = None,
    error: Optional[str] = None,
    adjusted: Optional[str] = None,
    deferred: Optional[str] = None,
) -> HTMLResponse:
    return await render_brief_settings(
        request,
        username,
        db=db,
        redis=redis,
        saved=saved,
        error=error,
        adjusted=adjusted,
        deferred=deferred,
    )

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
    # Brief time is read per subject by its minutely dispatcher. Provider
    # cadences still rebuild one process-wide trigger, so only a sole subject's
    # save may govern those shared jobs.
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
        # The owner's Brief time applies directly from its durable row. This
        # notice is about the process-wide provider cadence only.
        query += "&deferred=reload" if reload_failed else "&deferred=1"
    return _redirect(
        query,
        destination=SETTINGS_BRIEF_PATH,
    )


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
    del request
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
    return _redirect(
        "?saved=language",
        destination=SETTINGS_PROFILE_PATH,
    )
