"""HTTP exception presentation and registration for the Vitals app."""

from __future__ import annotations

import logging
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from vitals.i18n import current_lang, t
from vitals.services.authorization.subject_access import AccessDeniedError
from vitals.services.tenancy.contracts import NoPersonalRecordError
from web.app_lifecycle import LEGACY_BOOTSTRAP_CLOSED
from web.deps import (
    ModuleDisabled,
    NotAuthenticated,
    RecentAuthenticationRequired,
    get_redis_client,
    get_session_factory,
)
from web.templating import templates

logger = logging.getLogger(__name__)

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


async def recent_authentication_handler(
    request: Request, exc: RecentAuthenticationRequired
):
    """Send a sensitive browser action through a real authentication step."""

    del exc
    accept = request.headers.get("accept", "")
    is_htmx = request.headers.get("HX-Request", "").lower() == "true"
    if "text/html" not in accept and not is_htmx:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Recent authentication required"},
        )

    next_path = request.url.path if request.method == "GET" else "/"
    referer = request.headers.get("referer")
    if referer:
        parsed = urlsplit(referer)
        if parsed.netloc == request.url.netloc and parsed.path.startswith("/"):
            next_path = parsed.path
            if parsed.query:
                next_path += f"?{parsed.query}"

    from web.authentication.tokens import clear_session_cookie
    from web.config import get_web_config

    if get_web_config().oidc_enabled:
        target = "/auth/start?" + urlencode(
            {"step_up": "true", "next": next_path}
        )
        if is_htmx:
            return Response(
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"HX-Redirect": target},
            )
        return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)

    target = "/login?" + urlencode({"next": next_path})
    response = (
        Response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"HX-Redirect": target},
        )
        if is_htmx
        else RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    )
    clear_session_cookie(response)
    return response

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
    from vitals.services.modules import preferences as modules_service
    from vitals.services.modules.registry import DEFAULT_STATE
    from vitals.services.preferences import language as language_service
    from web.deps import get_request_chrome_scope

    lang = "en"
    enabled = dict(DEFAULT_STATE)
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


async def _render_refusal(
    request: Request,
    *,
    status_code: int,
    copy_prefix: str,
    primary_href: str,
    primary_key: str | None = None,
):
    """Render a refusal as a page instead of a naked sentence.

    Both callers used to answer a browser with ``HTMLResponse(content=detail)``.
    The sentence was right; the page was a dead end — no masthead, no
    navigation, no link. A superadmin on a shared installation can open exactly
    one address, ``/care``, and on every other one they were left on a white
    page with no way to find it. That is invisible to a status-code assertion
    and obvious the moment you open the thing.

    ``base.html`` reads ``request.state`` for language and module state. A
    matched route has already populated it, but these exceptions can be raised
    from inside a dependency that runs before ``load_language`` — so fill it in
    when it is missing rather than assume, or the refusal page itself 500s.
    """

    if not hasattr(request.state, "lang") or not hasattr(
        request.state, "enabled_modules"
    ):
        await _populate_state_for_error_page(request)
    if not hasattr(request.state, "nav_status"):
        request.state.nav_status = []

    # A matched route has already set both values in ``load_language``. Keeping
    # the ContextVar aligned here also makes the error-page fallback and direct
    # handler tests deterministic instead of depending on a previous request.
    current_lang.set(getattr(request.state, "lang", "en"))
    username = None
    try:
        from web.authentication.tokens import read_session
        from web.config import SESSION_COOKIE

        username = read_session(request.cookies.get(SESSION_COOKIE))
    except Exception:
        logger.exception("Could not resolve user for refusal page")

    return templates.TemplateResponse(
        request,
        "refusal.html",
        {
            "username": username,
            "alerts": [],
            "kicker": t(f"{copy_prefix}.kicker"),
            "headline": t(f"{copy_prefix}.headline"),
            "body": t(f"{copy_prefix}.body"),
            "primary_href": primary_href,
            "primary_label": t(primary_key or f"{copy_prefix}.primary"),
            "back_label": t("refusal.back"),
        },
        status_code=status_code,
    )


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
            from web.authentication.tokens import read_session
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

async def module_disabled_handler(request: Request, exc: ModuleDisabled):
    """A disabled Optional module behaves as if absent: redirect browser GETs to
    the dashboard, return JSON 404 for API/HTMX calls."""
    accept = request.headers.get("accept", "")
    if request.method == "GET" and "text/html" in accept:
        return RedirectResponse(url="/weight", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": exc.detail})


async def no_personal_record_handler(request: Request, exc: NoPersonalRecordError):
    """A recordless account reaching a page about its own health data.

    Registered ahead of the bridge handler and matched more narrowly, because it
    is a different sentence. These accounts are not blocked by an unfinished
    migration and there is no setting that will let them in: the page answers
    "your weight", "your labs", "your day", and they keep no record of their own.
    Professional work takes precedence when an account also operates the
    platform. A platform-only operator gets a plain explanation and a route back
    to the control plane; other account shapes keep the existing generic answer.
    """

    del exc
    is_professional = bool(getattr(request.state, "is_professional", False))
    is_platform_admin = bool(getattr(request.state, "is_platform_admin", False))
    accept = request.headers.get("accept", "")
    wants_html = request.method == "GET" and "text/html" in accept
    if is_professional and wants_html:
        return RedirectResponse(url="/care", status_code=status.HTTP_303_SEE_OTHER)
    if is_platform_admin and wants_html:
        return await _render_refusal(
            request,
            status_code=status.HTTP_409_CONFLICT,
            copy_prefix="refusal.no_personal.platform",
            primary_href="/settings/platform",
        )
    detail = (
        "У этого аккаунта нет собственной медицинской записи. "
        "Эта страница — о ваших данных, а работа с подопечными живёт в разделе «Подопечные»."
    )
    if wants_html:
        return await _render_refusal(
            request,
            status_code=status.HTTP_409_CONFLICT,
            copy_prefix="refusal.no_personal.generic",
            primary_href="/care",
        )
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
        return await _render_refusal(
            request,
            status_code=status.HTTP_409_CONFLICT,
            copy_prefix="refusal.legacy_multi_record",
            primary_href="/today",
        )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT, content={"detail": detail}
    )

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
        # The wording stays as generic as the JSON: no name, no hint whether
        # the record exists. Where the button points is not a leak — it is
        # decided by what *this* account holds, which it already knows.
        holds_patients = bool(getattr(request.state, "holds_patients", False))
        return await _render_refusal(
            request,
            status_code=status.HTTP_403_FORBIDDEN,
            copy_prefix="refusal.access_denied",
            primary_href="/care" if holds_patients else "/today",
            primary_key=(
                "refusal.access_denied.primary_patients"
                if holds_patients
                else "refusal.access_denied.primary_dashboard"
            ),
        )
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN, content={"detail": detail}
    )

def register_error_handlers(app: FastAPI) -> None:
    """Install every application-level refusal and exception presenter."""

    app.add_exception_handler(NotAuthenticated, auth_exception_handler)
    app.add_exception_handler(
        RecentAuthenticationRequired,
        recent_authentication_handler,
    )
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(ModuleDisabled, module_disabled_handler)
    app.add_exception_handler(NoPersonalRecordError, no_personal_record_handler)
    for bridge_refusal in LEGACY_BOOTSTRAP_CLOSED:
        app.add_exception_handler(bridge_refusal, legacy_ownership_handler)
    app.add_exception_handler(AccessDeniedError, access_denied_handler)


__all__ = [
    "access_denied_handler",
    "auth_exception_handler",
    "http_exception_handler",
    "legacy_ownership_handler",
    "module_disabled_handler",
    "no_personal_record_handler",
    "recent_authentication_handler",
    "register_error_handlers",
]
