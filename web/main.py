"""FastAPI application entrypoint for the Vitals panel.

Integrates the single-user auth exception handler, database session pooling,
Redis cache connection, and background APScheduler thread.
"""
from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request, status
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from web.auth import router as auth_router
from web.csrf import add_csrf_origin_check, add_security_headers
from web.deps import (
    ModuleDisabled,
    NotAuthenticated,
    get_redis_client,
    get_session_factory,
    get_session,
    get_redis,
    load_enabled_modules,
    load_language,
    load_nav_status,
    require_auth,
    require_module,
)
from web.templating import STATIC_DIR, templates
from web.uploads import storage_refs_for_route_key

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from vitals.services.proactive.prefs import ProactivePreferencesBundle


async def _bootstrap_legacy_identity(
    session_factory,
    *,
    timezone: str,
) -> ProactivePreferencesBundle:
    """Materialize the environment-backed owner and safe resource roots.

    The compatibility login remains environment-backed in this rollout phase,
    but every deployment must have one durable owner/subject boundary before a
    scheduler, connector, or catalog job can start.  Keep this transaction short
    so the PostgreSQL governance lock is never held while unrelated catalogs are
    synchronized.
    """

    from vitals.services.identity_bootstrap import bootstrap_legacy_owner
    from vitals.services.proactive import prefs
    from vitals.services import modules_service
    from vitals.services.scoped_settings_service import (
        ScopedSettingKey,
        SettingScope,
        set_scoped_setting,
    )
    from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots
    from web.config import get_web_config

    web_config = get_web_config()
    async with session_factory() as session:
        try:
            identity = await bootstrap_legacy_owner(
                session,
                username=web_config.auth_username,
                password_hash=web_config.auth_password_hash,
                timezone=timezone,
            )
            await bootstrap_legacy_resource_roots(
                session,
                subject_id=identity.subject_id,
            )
            # Durable delivery deliberately refuses the legacy/default module
            # fallback. Materialize the normalized exact-one value before any
            # scheduler or sender can run, while the bootstrap transaction still
            # holds identity governance and the sole subject root.
            enabled_modules = await modules_service.get_enabled_modules(
                session,
                subject_id=identity.subject_id,
            )
            await set_scoped_setting(
                session,
                scope=SettingScope.SUBJECT,
                key=ScopedSettingKey.ENABLED_MODULES,
                scope_id=identity.subject_id,
                value=enabled_modules,
            )
            preference_scope = await prefs.resolve_legacy_preferences_scope(
                session,
                actor_username=None,
            )
            await prefs.initialize_legacy_preferences(
                session,
                scope=preference_scope,
            )
            preference_bundle = await prefs.get_exact_one_preferences_bundle(
                session,
                scope=preference_scope,
            )
            await session.commit()
            return preference_bundle
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    session_factory = get_session_factory()
    redis = None
    try:
        redis = get_redis_client()
    except Exception as e:
        logger.warning("Redis client could not be loaded at startup: %s", e)

    # Scheduler setup
    from vitals.config import load_config
    from vitals.scheduler.jobs import register_all_jobs
    from vitals.scheduler.scheduler import seed_heartbeats, setup_scheduler
    from vitals.services import conflict_catalog, conflict_engine, hrt_catalog
    from vitals.services.conflict_registrations import register_all_resolvers

    config = load_config()

    # Fail startup closed if the legacy credential cannot be reconciled with the
    # durable identity.  This runs before catalogs, scheduler registration, or
    # connector work so a partially initialized process never serves requests.
    preference_bundle = await _bootstrap_legacy_identity(
        session_factory,
        timezone=config.timezone,
    )

    # Register cross-domain conflict resolvers (supplements/genetics/skincare/...).
    register_all_resolvers()
    # Upsert the curated rule catalog (vitals/data/conflict_rules.yaml) — cheap,
    # idempotent, and keeps the DB in sync with the checked-in YAML on every
    # deploy without a data migration per rule change.
    async with session_factory() as session:
        # Job schedules come from the DB (Settings → proactive), so the registry is
        # attached here rather than before the session opens.
        register_all_jobs(preference_bundle.as_flat_dict())
        await conflict_catalog.sync_catalog(session)
        # Upsert the curated HRT compound catalog (vitals/data/hrt_compounds.yaml).
        await hrt_catalog.sync_catalog(session)
        await session.commit()

    # The panel seed adopts the pre-tenancy catalog only under the shared
    # governance lock. Keep it in a fresh transaction so catalog row locks are
    # never acquired before governance/subject locks.
    from vitals.services import hrt_reminders
    from vitals.utils.timeutils import today_local

    async with session_factory() as session:
        conflict_context = (
            await conflict_engine.resolve_legacy_conflict_write_context(
                session,
                actor_username=None,
                evaluation_date=today_local(),
            )
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        await hrt_reminders.seed_hormone_panel(
            session,
            identity=conflict_context.identity,
            prepared_conflict_write=prepared,
        )
        await session.commit()

    if redis is not None:
        await seed_heartbeats(redis)

    scheduler = setup_scheduler(session_factory, redis, timezone=config.timezone)
    scheduler.start()
    app.state.scheduler = scheduler

    async with AsyncExitStack() as stack:
        # The mounted MCP app builds its streamable-HTTP session manager in its own
        # lifespan, which app.mount() never runs — without this every /mcp/ request
        # fails with "manager not initialized".
        mcp_lifespan = getattr(app.state, "mcp_lifespan", None)
        if mcp_lifespan is not None:
            await stack.enter_async_context(mcp_lifespan(app))
        yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    scheduler.shutdown()


app = FastAPI(
    title="Vitals Health OS",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    # The third door, and the one that stayed open while the other two were shut:
    # an anonymous GET /openapi.json listed every path in the app, which tells a
    # stranger exactly which health modules this install runs. Nothing here is a
    # public API — the schema has no audience.
    openapi_url=None,
    # Resolve the enabled-module map once per request → request.state (read by
    # base.html nav and the require_module guards below).
    dependencies=[
        Depends(load_language),
        Depends(load_enabled_modules),
        # After load_enabled_modules — it reads the resolved module map.
        Depends(load_nav_status),
    ],
)

# Install security barriers
add_csrf_origin_check(app)
add_security_headers(app)

# ── Uploaded files ───────────────────────────────────────────────────────────
# Lab sheets, InBody printouts and progress photos are written under
# ``static/uploads`` so they survive a rebuild on the same bind mount as the rest
# of the assets — but they are the owner's medical records, not site furniture,
# and the mount below hands anything in ``static`` to whoever asks. A random file
# name is not an access control: the URL never expires, logging out does not
# revoke it, and it outlives the session in history, caches and proxy logs.
#
# So this route claims the subtree ahead of the mount and puts the same session
# guard on it as every page. It MUST stay above ``app.mount`` — routes match in
# registration order, and the mount would swallow the prefix first.
UPLOADS_DIR = os.path.realpath(os.path.join(STATIC_DIR, "uploads"))


@app.get("/static/uploads/{key:path}")
async def serve_upload(
    key: str,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    path = os.path.realpath(os.path.join(UPLOADS_DIR, key))
    # ``..`` (and any symlink out) resolves to somewhere else: a miss, not a read.
    # Authorize the persisted graph before consulting file existence so a
    # guessed progress-photo path cannot become a metadata oracle.
    if not path.startswith(UPLOADS_DIR + os.sep):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    # A session proves who the browser user is, not which subject owns this
    # particular medical file. Resolve the compatibility subject independently
    # and honor persisted lifecycle before touching the bytes. Progress photos
    # additionally require a reachable validated fact; arbitrary legacy paths
    # are not an authorization capability.
    from vitals.enums import FileAssetStatus, FileStorageBackend
    from vitals.models.tenancy import FileAsset
    from vitals.services.legacy_ownership import (
        LegacyOwnershipError,
        resolve_legacy_ownership_context,
    )

    try:
        ownership = await resolve_legacy_ownership_context(
            db,
            actor_username=username,
        )
    except LegacyOwnershipError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None

    from vitals.services import weight_service

    try:
        photo = await weight_service.get_progress_photo_by_file_key(
            db,
            file_key=f"uploads/{key}",
            subject_id=ownership.subject_id,
        )
    except weight_service.ProgressPhotoOwnershipError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None

    document_asset = None
    if key.startswith(("labs/", "body/")):
        document_asset = await db.scalar(
            select(FileAsset).where(
                FileAsset.subject_id == ownership.subject_id,
                FileAsset.storage_backend == FileStorageBackend.LEGACY_LOCAL.value,
                FileAsset.storage_ref.in_(storage_refs_for_route_key(key)),
            )
        )
    if photo is not None and document_asset is not None:
        # ``uploads/labs/x`` and ``labs/x`` resolve to the same legacy-local
        # bytes. Two metadata authorities for that path are ambiguous even when
        # both are individually live, so refuse the alias rather than letting
        # one lifecycle silently override the other.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if photo is None and document_asset is not None:
        if document_asset.status in {
            FileAssetStatus.DELETED.value,
            FileAssetStatus.PURGED.value,
        }:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    elif photo is None and not key.startswith(("labs/", "body/")):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    if not os.path.isfile(path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # Never written to disk cache: the file is readable again on the next request,
    # and a logged-out browser should keep nothing. Matches the service worker,
    # which already refuses to cache this prefix.
    return FileResponse(path, headers={"Cache-Control": "private, no-store"})


# Mount static files — everything else under /static is public site furniture
# (CSS, JS, fonts, icons), reachable before login because the login page needs it.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Exception Handlers ────────────────────────────────────────────────────────


@app.exception_handler(NotAuthenticated)
async def auth_exception_handler(request: Request, exc: NotAuthenticated):
    """Redirect unauthorized browser navigation to the login form,

    but return JSON 401 responses for background API/HTMX calls.
    """
    # Check if this request accepts HTML (standard browser GET)
    accept = request.headers.get("accept", "")
    is_html = "text/html" in accept

    if request.method == "GET" and is_html:
        # Preserve next parameter if redirecting
        next_param = str(request.url.path)
        if request.url.query:
            next_param += f"?{request.url.query}"
        login_url = "/login"
        if next_param not in ("", "/"):
            # Percent-encode next_param as a single query value — it can itself
            # contain '&'/'?' (e.g. redirecting back into an OAuth authorize
            # URL), which would otherwise be parsed as separate top-level params
            # on /login and silently truncate `next`.
            login_url += f"?{urlencode({'next': next_param})}"
        return RedirectResponse(url=login_url, status_code=status.HTTP_302_FOUND)

    return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "Not authenticated"})



async def _populate_state_for_error_page(request: Request) -> None:
    """Fill ``request.state`` with lang / enabled_modules for an error
    page rendered through ``base.html``.

    An unmatched route (404) never runs the global ``load_language`` /
    ``load_enabled_modules`` dependencies, because those are
    attached to the API router and only fire once a route matches. base.html reads
    both off ``request.state`` (e.g. ``get_js_strings(request.state.lang)``),
    so without this the 404 template itself raises → 500. Resolve them here with a
    fresh session/redis, mirroring each dependency's fail-safe default so the page
    renders no matter what.
    """
    from vitals.i18n import current_lang
    from vitals.services import language_service, modules_service
    from web.deps import get_request_legacy_ownership

    lang = "en"
    enabled = dict(modules_service.DEFAULT_STATE)
    try:
        redis = get_redis_client()
        async with get_session_factory()() as db:
            ownership = None
            ownership_failed = False
            try:
                ownership = await get_request_legacy_ownership(request, db)
            except Exception:
                ownership_failed = True
                logger.exception(
                    "404 page: ownership resolution failed; using safe defaults"
                )
            if not ownership_failed:
                try:
                    lang = await language_service.get_language(
                        db,
                        redis,
                        user_id=(
                            ownership.owner_user_id
                            if ownership is not None
                            else None
                        ),
                    )
                except Exception:
                    logger.exception(
                        "404 page: language load failed; defaulting to 'en'"
                    )
                try:
                    enabled = await modules_service.get_enabled_modules(
                        db,
                        redis,
                        subject_id=(
                            ownership.subject_id
                            if ownership is not None
                            else None
                        ),
                    )
                except Exception:
                    logger.exception(
                        "404 page: module-state load failed; using defaults"
                    )
    except Exception:
        logger.exception("404 page: could not open db/redis; using all defaults")

    current_lang.set(lang)
    request.state.lang = lang
    request.state.enabled_modules = enabled
    # The rail's sync card is chrome, not information the error page owes anyone —
    # an empty list just hides it.
    request.state.nav_status = []


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Render a branded 404 page for browser navigations and keep JSON 404s for API/HTMX."""
    if exc.status_code != status.HTTP_404_NOT_FOUND:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    accept = request.headers.get("accept", "")
    is_html = "text/html" in accept
    is_htmx = request.headers.get("hx-request", "").lower() == "true"
    if request.method == "GET" and is_html and not is_htmx:
        username = None
        try:
            from web.auth import read_session
            from web.config import SESSION_COOKIE

            username = read_session(request.cookies.get(SESSION_COOKIE))
        except Exception:
            logger.exception("Could not resolve user for 404 page")
        # Unmatched routes skip the global load_* dependencies, so base.html's
        # request.state.{lang,enabled_modules} are unset — populate them or the
        # template render 500s instead of showing the branded 404.
        await _populate_state_for_error_page(request)
        return templates.TemplateResponse(
            request,
            "404.html",
            {
                "username": username,
                "alerts": [],
                "requested_path": request.url.path,
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": exc.detail})


@app.exception_handler(ModuleDisabled)
async def module_disabled_handler(request: Request, exc: ModuleDisabled):
    """A disabled Optional module behaves as if absent: redirect browser GETs to
    the dashboard, return JSON 404 for API/HTMX calls."""
    accept = request.headers.get("accept", "")
    if request.method == "GET" and "text/html" in accept:
        return RedirectResponse(url="/weight", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": exc.detail})


# ── Health check ─────────────────────────────────────────────────────────────


@app.get("/health")
async def health(
    request: Request,
    db_session: AsyncSession = Depends(get_session),
    redis_client = Depends(get_redis)
):
    db_ok = False
    try:
        await db_session.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        logger.error("Healthcheck DB check failed: %s", e)

    redis_ok = False
    heartbeat_age = None
    stale_jobs = None
    try:
        await redis_client.ping()
        redis_ok = True

        from vitals.config import load_config
        from vitals.scheduler.scheduler import KEEPALIVE_JOB_ID, heartbeat_budgets
        from vitals.scheduler.scheduler_lock import scheduler_heartbeat_age

        # Every heartbeating job is checked against a budget derived from its own
        # schedule — watching the keepalive alone left a module job free to stop
        # firing while /health stayed green.
        stale_jobs = []
        for job_id, budget in heartbeat_budgets(load_config().timezone).items():
            age = await scheduler_heartbeat_age(redis_client, job_id)
            if job_id == KEEPALIVE_JOB_ID:
                heartbeat_age = age
            if age is None or age > budget:
                stale_jobs.append(job_id)
    except Exception as e:
        logger.error("Healthcheck Redis check failed: %s", e)

    scheduler_ok = stale_jobs is not None and not stale_jobs
    status_str = "ok" if (db_ok and redis_ok and scheduler_ok) else "error"

    body = {
        "status": status_str,
        "database": "ok" if db_ok else "down",
        "redis": "ok" if redis_ok else "down",
        "scheduler": "ok" if scheduler_ok else "stale",
    }

    # Job ids name the modules this install runs (``hrt_reminders``,
    # ``glp1_plateau``, ...), so a stranger must not read them. The endpoint still
    # answers anonymously — hiding it behind require_auth would make external
    # monitoring go quietly red — but the diagnosis is for the owner only. Read the
    # cookie by hand rather than via Depends: absence must not raise.
    from web.auth import read_session
    from web.config import SESSION_COOKIE

    if read_session(request.cookies.get(SESSION_COOKIE)) is not None:
        body["scheduler_heartbeat_age_seconds"] = heartbeat_age
        body["stale_jobs"] = stale_jobs or []

    return body


# ── Base redirection ──────────────────────────────────────────────────────────


@app.get("/")
async def root():
    return RedirectResponse(url="/today", status_code=status.HTTP_303_SEE_OTHER)


# ── Include Routers ───────────────────────────────────────────────────────────

app.include_router(auth_router)

# Routers under web/routers/ will be included dynamically to avoid import cycles.
# These routers will be imported and registered below.
from web.routers.alerts import router as alerts_router  # noqa: E402
from web.routers.today import router as today_router  # noqa: E402
from web.routers.more import router as more_router  # noqa: E402
from web.routers.weight import router as weight_router  # noqa: E402
from web.routers.glp1 import router as glp1_router  # noqa: E402
from web.routers.supplements import router as supplements_router  # noqa: E402
from web.routers.hrt import router as hrt_router  # noqa: E402
from web.routers.genetics import router as genetics_router  # noqa: E402
from web.routers.skincare import router as skincare_router  # noqa: E402
from web.routers.hevy import router as hevy_router  # noqa: E402
from web.routers.garmin import router as garmin_router  # noqa: E402
from web.routers.labs import router as labs_router  # noqa: E402
from web.routers.reports import router as reports_router  # noqa: E402
from web.routers.nutrition import router as nutrition_router  # noqa: E402
from web.routers.interactions import router as interactions_router  # noqa: E402
from web.routers.settings import router as settings_router  # noqa: E402
from web.routers.charts import router as charts_router  # noqa: E402
from web.routers.timeline import router as timeline_router  # noqa: E402
from web.routers.signals import router as signals_router  # noqa: E402
from web.routers.external_api import router as external_api_router  # noqa: E402
from web.routers.telegram import router as telegram_router  # noqa: E402
from web.routers.public_report import router as public_report_router  # noqa: E402
from web.routers.share import router as share_router  # noqa: E402

# Core modules — always reachable. /today is the landing page and composes every
# enabled domain, so it can never be gated behind one of them.
app.include_router(today_router)
# The phone's "More" screen — a plain page, never gated: it is how a phone
# reaches Settings and the sections the bottom bar has no column for.
app.include_router(more_router)
app.include_router(alerts_router)
app.include_router(weight_router)
app.include_router(garmin_router)
app.include_router(labs_router)
app.include_router(reports_router)
# Doctor reports — the owner's side. Not gated on a module: it publishes whatever
# modules happen to be on, and gating it would hide the revoke button with them.
app.include_router(share_router)
app.include_router(settings_router)
app.include_router(charts_router)
# Read-only JSON API for an external personal dashboard (Bearer-token guarded, not session auth).
app.include_router(external_api_router)
# Telegram webhook — its own secret path + header, no session auth.
app.include_router(telegram_router)
# The published doctor document. The ONE anonymous route in the app: no
# require_auth (the visitor has no account) and no require_module gate (the
# module set is already baked into the frozen snapshot). Its own, stricter CSP
# is set per response — see web/routers/public_report.py.
app.include_router(public_report_router)

# Optional modules — guarded: a disabled module's routes 404 → redirect to /weight.
app.include_router(glp1_router, dependencies=[Depends(require_module("glp1"))])
app.include_router(hevy_router, dependencies=[Depends(require_module("hevy"))])
app.include_router(supplements_router, dependencies=[Depends(require_module("supplements"))])
app.include_router(hrt_router, dependencies=[Depends(require_module("hrt"))])
app.include_router(genetics_router, dependencies=[Depends(require_module("genetics"))])
app.include_router(skincare_router, dependencies=[Depends(require_module("skincare"))])
app.include_router(nutrition_router, dependencies=[Depends(require_module("nutrition"))])
app.include_router(interactions_router, dependencies=[Depends(require_module("interactions"))])
app.include_router(timeline_router, dependencies=[Depends(require_module("timeline"))])
app.include_router(signals_router, dependencies=[Depends(require_module("signals"))])

# ── OAuth & MCP Integration ──────────────────────────────────────────────────
try:
    from web.routers.oauth import router as oauth_router  # noqa: E402
    from web.routers.mcp import get_mcp_app  # noqa: E402

    app.include_router(oauth_router)
    mcp_app, mcp_lifespan = get_mcp_app()
    app.mount("/mcp", mcp_app)
    app.state.mcp_lifespan = mcp_lifespan
except ImportError:
    import logging
    logging.getLogger(__name__).warning("MCP/OAuth disabled (fastmcp not available)")
