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
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services import health_profile_service
from vitals.services.access_resolution import AccessDeniedError
from vitals.services.alerts_service import AlertLegacyBridgeError
from vitals.services.legacy_ownership import (
    LegacyOwnershipError,
    NoPersonalRecordError,
)
from vitals.services.conflict_activation_service import (
    ConflictActivationLegacyBridgeError,
)
from vitals.services.conflict_engine import ConflictLegacyBridgeError
from vitals.services.digest_service import DigestOwnershipError
from vitals.services.garmin_weight_service import (
    GarminWeightExportLegacyBridgeError,
)
from vitals.services.proactive.prefs import (
    LegacyProactivePreferencesBridgeClosedError,
)
from vitals.services.scoped_settings_service import (
    LegacyScopedSettingBridgeClosedError,
)
from vitals.services.share_service import ShareOwnershipError
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
    load_subject_timezone,
    require_module,
)
from web.templating import STATIC_DIR, templates

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
                # The one caller allowed to say it. This is the record
                # ``VITALS_AUTH_USERNAME`` names, so the Garmin and Hevy values
                # in ``.env`` are theirs; for anybody else those roots start
                # with no credential at all, because the file describes the
                # operator and not them.
                adopt_environment_credentials=True,
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
            # Age, sex, height, programme and goals move out of ``.env`` into
            # this owner's own row. Adopted here rather than on first read for
            # the reason every other step in this block exists: while the
            # installation is one person, the unattributed value is
            # unambiguously theirs, and afterwards nothing can say whose it was.
            await health_profile_service.adopt_installation_profile(
                session,
                subject_id=identity.subject_id,
            )
            await session.commit()
            return preference_bundle
        except _LEGACY_BOOTSTRAP_CLOSED:
            # Not a misconfiguration — the destination of this whole migration.
            #
            # Every step above is compatibility scaffolding for an installation
            # that is one person: reconcile the .env credential with the durable
            # identity, materialize that person's module map, seed their
            # notification preferences. All three resolve "the subject" through
            # the sole-owner bridge, which fail-closes the moment a second
            # health subject exists, because it genuinely cannot tell whose
            # record was meant.
            #
            # Refusing to boot was the right answer while that state was
            # impossible. It stopped being right when PR-07 made a second
            # subject the point: the process would not start at all, so the
            # professional features could not be deployed by the installations
            # they were built for.
            #
            # There is nothing to reconcile here and nothing to lose by
            # skipping. Scheduled jobs fall back to their defaults, which is
            # what a shared installation needs anyway — per-subject schedules
            # are PR-09's work, not something to fake from one person's row.
            await session.rollback()
            logger.warning(
                "legacy identity bootstrap skipped: this installation holds "
                "more than one health subject, so there is no sole owner to "
                "reconcile. Scheduled jobs use their defaults."
            )
            return None
        except Exception:
            await session.rollback()
            raise


#: Every "this needs exactly one health subject" refusal, from the several
#: compatibility bridges that each grew their own. They share no base class,
#: which is why this is a list rather than a catch — and the list is useful as
#: itself: it is the porting backlog. A module leaves it by resolving through
#: ``resolve_access_context`` instead of a sole-subject bridge.
#:
#: Kept narrow on purpose. Every *other* error still fails closed, at startup
#: and in a request, because a half-reconciled identity must not go on serving.
_LEGACY_BOOTSTRAP_CLOSED = (
    LegacyOwnershipError,
    LegacyScopedSettingBridgeClosedError,
    ConflictLegacyBridgeError,
    ShareOwnershipError,
    DigestOwnershipError,
    AlertLegacyBridgeError,
    ConflictActivationLegacyBridgeError,
    LegacyProactivePreferencesBridgeClosedError,
    GarminWeightExportLegacyBridgeError,
)


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
        register_all_jobs(
            preference_bundle.as_flat_dict()
            if preference_bundle is not None
            else None
        )
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
        try:
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
        except _LEGACY_BOOTSTRAP_CLOSED:
            # Seeding one person's hormone panel from the curated catalog, in an
            # installation that has more than one person. There is no "the
            # person" to seed for, and picking one would be inventing a fact
            # about somebody's treatment. Skipped, like the identity bootstrap
            # above and for the same reason.
            await session.rollback()
            logger.warning(
                "hormone panel seed skipped: more than one health subject, so "
                "there is no sole owner to seed for"
            )

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
        # Before the module map and the nav: both of them, and every page under
        # them, ask what day it is.
        Depends(load_subject_timezone),
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
# and the mount below hands anything in ``static`` to whoever asks.
#
# They are served by ``/files/{opaque_key}`` instead, which addresses an asset
# rather than a path. Nothing reachable here is servable any more, so this route
# exists only to make sure the mount can never see the subtree: it claims the
# prefix ahead of it and answers nothing, for everybody, always.
#
# It MUST stay above ``app.mount`` — routes match in registration order, and the
# mount would otherwise swallow the prefix and hand out medical records to
# whoever guessed a filename. That ordering is pinned by a test.
UPLOADS_DIR = os.path.realpath(os.path.join(STATIC_DIR, "uploads"))


@app.get("/static/uploads/{key:path}")
async def serve_upload(key: str):
    """Seal the private tree off from the static mount. Always a miss.

    Deliberately without a session dependency: there is no authenticated way
    through either, and requiring one would answer 401 to a stranger and 404 to
    the owner — which is a shape that tells the stranger the path was real.
    """

    del key
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


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
    from web.deps import get_request_chrome_scope

    lang = "en"
    enabled = dict(modules_service.DEFAULT_STATE)
    try:
        redis = get_redis_client()
        async with get_session_factory()() as db:
            scope = None
            scope_failed = False
            try:
                scope = await get_request_chrome_scope(request, db)
            except Exception:
                scope_failed = True
                logger.exception(
                    "404 page: chrome scope resolution failed; using safe defaults"
                )
            if not scope_failed:
                try:
                    lang = await language_service.get_language(
                        db,
                        redis,
                        user_id=(scope.user_id if scope is not None else None),
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
                            scope.subject_id
                            if scope is not None
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


@app.exception_handler(NoPersonalRecordError)
async def no_personal_record_handler(request: Request, exc: NoPersonalRecordError):
    """A doctor or a trainer reaching a page about their own health data.

    Registered ahead of the bridge handler and matched more narrowly, because it
    is a different sentence. These accounts are not blocked by an unfinished
    migration and there is no setting that will let them in: the page answers
    "your weight", "your labs", "your day", and they keep no record of their
    own. Somebody who does hold patients is sent where their work actually is;
    anybody else is told plainly, rather than being handed a limit that does not
    apply to them and sent looking for it.
    """

    del exc
    holds_patients = bool(getattr(request.state, "holds_patients", False))
    accept = request.headers.get("accept", "")
    wants_html = request.method == "GET" and "text/html" in accept
    if holds_patients and wants_html:
        return RedirectResponse(url="/care", status_code=status.HTTP_303_SEE_OTHER)
    detail = (
        "У этого аккаунта нет собственной медицинской записи. "
        "Эта страница — о ваших данных, а работа с подопечными живёт в разделе «Подопечные»."
    )
    if wants_html:
        return HTMLResponse(content=detail, status_code=status.HTTP_409_CONFLICT)
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT, content={"detail": detail}
    )


async def legacy_ownership_handler(request: Request, exc: Exception):
    """A route still on the sole-owner adapter, in an installation with two people.

    Most write routers resolve their subject through
    ``resolve_legacy_ownership_context``, which is deliberately fail-closed:
    it refuses the moment the database holds more than one health subject,
    because it has no way to tell whose record the request meant.

    That refusal is correct — nothing is written, and no other person's row is
    reached — but until now it arrived as an unhandled exception and therefore a
    500. Those routes are the remaining compatibility surface of this migration,
    and "not available in a shared installation" is a thing to say plainly
    rather than a crash to read out of a stack trace.

    Deliberately not silent, and deliberately logged at warning: a route ending
    up here is one that still needs porting to ``resolve_access_context``. The
    log names the refusing type as well as the route, because "which bridge" is
    the question the porting work starts from and the route alone rarely answers
    it — several of these pages refuse two layers below the handler they look
    like they belong to.
    """

    logger.warning(
        "legacy sole-owner route reached in a multi-subject installation: "
        "%s %s refused by %s",
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    detail = "Эта страница ещё не поддерживает несколько записей."
    accept = request.headers.get("accept", "")
    if request.method == "GET" and "text/html" in accept:
        return HTMLResponse(
            content=detail, status_code=status.HTTP_409_CONFLICT
        )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT, content={"detail": detail}
    )


# Registered from the tuple rather than by a stack of decorators, so the backlog
# is stated once. It was two lists before, and they drifted: four pages served a
# 500 with a stack trace because ``AlertLegacyBridgeError`` had been added to one
# of them and not the other. A refusal that reads as a crash is worse than the
# refusal — it sends whoever meets it looking for a bug that is not there.
for _bridge_refusal in _LEGACY_BOOTSTRAP_CLOSED:
    app.add_exception_handler(_bridge_refusal, legacy_ownership_handler)


@app.exception_handler(AccessDeniedError)
async def access_denied_handler(request: Request, exc: AccessDeniedError):
    """A refused policy decision is 403, not a crash.

    The response deliberately says nothing about whose record was reached for or
    whether it exists: a denial and a miss have to look the same from outside,
    or the refusal itself becomes a way to probe.
    """

    del exc
    logger.warning("access denied: %s %s", request.method, request.url.path)
    detail = "Недостаточно прав для этой операции."
    accept = request.headers.get("accept", "")
    if request.method == "GET" and "text/html" in accept:
        return HTMLResponse(content=detail, status_code=status.HTTP_403_FORBIDDEN)
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN, content={"detail": detail}
    )


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
from web.routers.files import router as files_router  # noqa: E402
from web.routers.care import router as care_router  # noqa: E402
from web.routers.consents import router as consents_router  # noqa: E402
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
from web.routers.external_api import router as external_api_router  # noqa: E402
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
# Private medical files, addressed by a rotatable key rather than a path.
# Not gated on a module: a lab sheet stays downloadable when labs is off.
app.include_router(files_router)
# The professional's side. Every route below /care/{subject_id} names its
# patient in the path — see web/care_context.py for why that is the design
# rather than a URL style, and not gated on a module: which modules the
# patient has on is their setting, not a reason to hide their doctor.
app.include_router(care_router)
# The patient's side of the same pair. Registered before the settings router
# so /settings/care is matched by its own routes rather than swallowed.
app.include_router(consents_router)
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
