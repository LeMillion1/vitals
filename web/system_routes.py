"""Infrastructure and public system routes for the Vitals app."""

from __future__ import annotations

import json
import logging
import os

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.i18n import get_js_strings
from vitals.process_mode import ProcessMode, load_process_mode
from web.deps import get_redis, get_session
from web.templating import STATIC_DIR

logger = logging.getLogger(__name__)

# The private medical subtree is claimed before the public static mount. This
# ordering is a security invariant asserted by the anonymous-surface tests.
UPLOADS_DIR = os.path.realpath(os.path.join(STATIC_DIR, "uploads"))

async def service_worker(request: Request) -> Response:
    """Serve the PWA worker at the origin root so its scope can be ``/``.

    A worker fetched from ``/static/sw.js`` is confined to ``/static/`` unless
    the response explicitly widens it. That left ordinary page navigation
    outside the worker despite the offline handler claiming otherwise. The root
    URL makes the scope structural; the header pins the intent for browsers and
    reverse proxies, and revalidation keeps deploys from pinning an old worker.
    """

    del request
    with open(os.path.join(STATIC_DIR, "sw.js"), encoding="utf-8") as worker:
        source = worker.read()
    copy = {
        lang: {
            "title": get_js_strings(lang)["care.push_notification_title"],
            "body": get_js_strings(lang)["care.push_notification_body"],
        }
        for lang in ("en", "ru")
    }
    prefix = "self.__VITALS_PUSH_COPY__=" + json.dumps(
        copy,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ) + ";\n"
    return Response(
        content=prefix + source,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )

async def serve_upload(key: str):
    """Seal the private tree off from the static mount. Always a miss.

    Deliberately without a session dependency: there is no authenticated way
    through either, and requiring one would answer 401 to a stranger and 404 to
    the owner — which is a shape that tells the stranger the path was real.
    """

    del key
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

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
    process_mode = load_process_mode()
    worker_reload_pending = None if process_mode is ProcessMode.WEB else False
    try:
        await redis_client.ping()
        redis_ok = True

        from vitals.config import load_config
        from vitals.scheduler.scheduler import (
            KEEPALIVE_JOB_ID,
            heartbeat_budget_caps,
            heartbeat_budgets,
        )
        from vitals.scheduler.scheduler_lock import scheduler_heartbeat_age

        if process_mode is ProcessMode.WEB:
            from vitals.scheduler.control import (
                read_worker_health_state,
            )

            desired_generation, manifest = await read_worker_health_state(
                redis_client
            )
            budgets = heartbeat_budget_caps(manifest.heartbeat_job_ids)
            worker_reload_pending = manifest.generation != desired_generation
        else:
            budgets = heartbeat_budgets(load_config().timezone)

        # Every heartbeating job is checked against either its process-local
        # schedule budget or the split worker's reviewed preference-independent
        # cap. Watching keepalive alone left one module job free to stop while
        # /health stayed green.
        stale_jobs = []
        for job_id, budget in budgets.items():
            age = await scheduler_heartbeat_age(redis_client, job_id)
            if job_id == KEEPALIVE_JOB_ID:
                heartbeat_age = age
            if age is None or age > budget:
                stale_jobs.append(job_id)
    except Exception as e:
        logger.error("Healthcheck Redis check failed: %s", e)

    scheduler_ok = (
        stale_jobs is not None and not stale_jobs and not worker_reload_pending
    )
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
    from web.authentication.tokens import read_session
    from web.config import SESSION_COOKIE

    if read_session(request.cookies.get(SESSION_COOKIE)) is not None:
        body["scheduler_heartbeat_age_seconds"] = heartbeat_age
        body["stale_jobs"] = stale_jobs or []
        if process_mode is ProcessMode.WEB:
            body["scheduler_reload_pending"] = worker_reload_pending

    return JSONResponse(
        content=body,
        status_code=(
            status.HTTP_200_OK
            if status_str == "ok"
            else status.HTTP_503_SERVICE_UNAVAILABLE
        ),
    )

async def root(request: Request):
    has_own_record = bool(getattr(request.state, "has_own_record", False))
    is_professional = bool(getattr(request.state, "is_professional", False))
    is_platform_admin = bool(getattr(request.state, "is_platform_admin", False))
    if has_own_record:
        destination = "/today"
    elif is_professional:
        destination = "/care"
    elif is_platform_admin:
        destination = "/settings/platform"
    else:
        destination = "/today"
    return RedirectResponse(url=destination, status_code=status.HTTP_303_SEE_OTHER)

def register_system_routes(app: FastAPI) -> None:
    """Install public infrastructure routes in their security-sensitive order."""

    # A plain Starlette route intentionally bypasses application dependencies:
    # the public worker must remain available while the database is unavailable.
    app.add_route("/sw.js", service_worker, methods=["GET"], name="service_worker")
    app.add_api_route(
        "/static/uploads/{key:path}",
        serve_upload,
        methods=["GET"],
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/", root, methods=["GET"])


__all__ = [
    "UPLOADS_DIR",
    "health",
    "register_system_routes",
    "root",
    "serve_upload",
    "service_worker",
]
