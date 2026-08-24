"""Single-user authentication logic, session cookie management, and auth router.

Uses itsdangerous timed serialization for signed session cookies, matching
the single-user configuration loaded from web/config.py.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadData, BadSignature, SignatureExpired, URLSafeTimedSerializer
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.i18n import t
from vitals.services import twofa_service
from vitals.utils.passwords import verify_password, verify_password_dummy
from web.config import (
    OIDC_HANDOFF_COOKIE,
    OIDC_HANDOFF_TTL,
    PENDING_2FA_COOKIE,
    PENDING_2FA_TTL,
    SESSION_COOKIE,
    get_web_config,
)
from web.deps import get_redis, get_session
from web.ratelimit import login_rate_limit
from web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

_SESSION_TOKEN_VERSION = 1
_SESSION_TOKEN_TYPE = "web_session"
_SESSION_AUTH_SOURCE = "legacy_env"
_SESSION_V1_KEYS = frozenset({"v", "type", "auth_source", "username"})

_OIDC_TOKEN_VERSION = 2
_OIDC_AUTH_SOURCE = "oidc"
_SESSION_V2_KEYS = frozenset(
    {
        "v",
        "type",
        "auth_source",
        "username",
        "user_id",
        "session_version",
        "authenticated_at",
        "subject_id",
    }
)


@dataclass(frozen=True, slots=True)
class SessionClaims:
    """Validated browser-session identity without authorization state.

    The envelope is versioned because its contents changed as authentication
    did. Version 1 carries the environment-backed owner's username. Version 2
    carries what a federated login produces: the local user's id, which is
    stable where a username is not; the session version that makes the session
    revocable; and the moment the provider actually authenticated the person,
    which is what a step-up check measures freshness against.

    A cookie is signed, not secret — anybody holding it can read it. Roles,
    subject access and health data therefore stay out of it, and everything
    here is either an opaque identifier or a timestamp.
    """

    version: int
    token_type: str
    auth_source: str
    username: str
    #: Version 2 only. The local user this session belongs to.
    user_id: uuid.UUID | None = None
    #: Version 2 only. Compared against the user's current value on every
    #: request: bumping that value revokes every session ever issued for them,
    #: which is what makes a session revocable without a server-side store.
    session_version: int | None = None
    #: Version 2 only. When the provider authenticated, as epoch seconds. Not
    #: when the token was issued — a refresh can make that arbitrarily recent
    #: without anybody having proved anything.
    authenticated_at: int | None = None
    #: Version 2 only. The health subject this session is acting for.
    subject_id: uuid.UUID | None = None


def _get_serializer() -> URLSafeTimedSerializer:
    cfg = get_web_config()
    return URLSafeTimedSerializer(cfg.session_secret, salt="vitals-session")


def _get_mcp_serializer() -> URLSafeTimedSerializer:
    """Separate salt from the session serializer so a session cookie and an MCP
    access token can never be mistaken for one another (signature verification
    fails across salts even though both derive from the same session secret).
    """
    cfg = get_web_config()
    return URLSafeTimedSerializer(cfg.session_secret, salt="vitals-mcp")


def _get_oidc_handoff_serializer() -> URLSafeTimedSerializer:
    """A fourth salt, for what the browser carries to the provider and back.

    Same reasoning as the two above: this handle must be worthless anywhere a
    session is expected, and verification across salts fails by construction.
    It holds the state, nonce and PKCE verifier — the three secrets that bind a
    callback to the login request that started it, and which therefore must not
    be guessable, reusable, or readable as a session.
    """

    cfg = get_web_config()
    return URLSafeTimedSerializer(cfg.session_secret, salt="vitals-oidc-handoff")


def _get_pending_2fa_serializer() -> URLSafeTimedSerializer:
    """A third salt, for the handle issued between the password step and the code
    step. Same reasoning as the MCP salt above: this token must be worthless if
    presented anywhere a real session is expected, and signature verification
    across salts fails by construction.
    """
    cfg = get_web_config()
    return URLSafeTimedSerializer(cfg.session_secret, salt="vitals-2fa")


def create_pending_2fa(username: str) -> str:
    """Sign a handle saying "this username just passed the password check"."""
    return _get_pending_2fa_serializer().dumps(username)


def read_pending_2fa(token: str | None) -> Optional[str]:
    """The username behind a live pending handle, or None if absent/expired."""
    if not token:
        return None
    try:
        payload = _get_pending_2fa_serializer().loads(token, max_age=PENDING_2FA_TTL)
    except (SignatureExpired, BadSignature):
        return None
    return payload if isinstance(payload, str) else None


def set_pending_2fa_cookie(response: Response, token: str) -> None:
    cfg = get_web_config()
    response.set_cookie(
        key=PENDING_2FA_COOKIE,
        value=token,
        max_age=PENDING_2FA_TTL,
        path="/",
        secure=cfg.cookie_secure,
        httponly=True,
        samesite=cfg.cookie_samesite,
    )


def clear_pending_2fa_cookie(response: Response) -> None:
    response.delete_cookie(key=PENDING_2FA_COOKIE, path="/")


def create_oidc_handoff(*, state: str, nonce: str, code_verifier: str, next_url: str) -> str:
    """Seal what the callback will need, for the browser to carry there."""

    return _get_oidc_handoff_serializer().dumps(
        {
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "next": next_url,
        }
    )


def read_oidc_handoff(token: str | None) -> dict[str, str] | None:
    """Open the handoff, or fail closed.

    Every field must be a non-empty string of the exact expected set. A handoff
    missing one is a handoff this version did not write, and completing a login
    from it would mean skipping whichever check that field feeds.
    """

    if not isinstance(token, str) or not token:
        return None
    try:
        payload = _get_oidc_handoff_serializer().loads(token, max_age=OIDC_HANDOFF_TTL)
    except BadData:
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "state",
        "nonce",
        "code_verifier",
        "next",
    }:
        return None
    for key in ("state", "nonce", "code_verifier"):
        value = payload[key]
        if not isinstance(value, str) or not value.strip():
            return None
    if not isinstance(payload["next"], str):
        return None
    return payload


def set_oidc_handoff_cookie(response: Response, token: str) -> None:
    cfg = get_web_config()
    response.set_cookie(
        key=OIDC_HANDOFF_COOKIE,
        value=token,
        max_age=OIDC_HANDOFF_TTL,
        httponly=True,
        secure=cfg.cookie_secure,
        # The provider redirects the browser back to us from its own origin, so
        # the cookie has to survive a cross-site navigation. ``lax`` does for a
        # top-level GET, which is what a redirect is; ``strict`` would drop it
        # and every login would fail at the callback.
        samesite="lax",
        path="/",
    )


def clear_oidc_handoff_cookie(response: Response) -> None:
    cfg = get_web_config()
    response.delete_cookie(
        key=OIDC_HANDOFF_COOKIE,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="lax",
        path="/",
    )


def safe_next(next: str | None) -> str:
    """Confine the post-login redirect to a local path (open-redirect guard).

    Accept only a value that resolves to a same-site path: it must begin with a
    single ``/`` and carry no scheme or host. Reject absolute URLs, protocol-
    relative ``//host``, and backslash tricks like ``/\\host`` (browsers normalise
    ``\\`` to ``/``, turning it into a protocol-relative off-site redirect).
    Anything else falls back to ``/``.
    """
    if not next or not next.startswith("/") or next.startswith("//") or "\\" in next:
        return "/"
    parsed = urlsplit(next)
    if parsed.scheme or parsed.netloc:
        return "/"
    return next


def decode_session(token: str | None) -> SessionClaims | None:
    """Verify and strictly decode a browser session token.

    Existing bare-string cookies are accepted as version 0 until their normal
    TTL expires.  New cookies use the exact version 1 envelope; unknown,
    malformed, or extended envelopes fail closed.
    """
    if not isinstance(token, str) or not token:
        return None
    cfg = get_web_config()
    serializer = _get_serializer()
    try:
        payload = serializer.loads(token, max_age=cfg.session_ttl)
    except BadData:
        return None

    if isinstance(payload, str):
        if not payload.strip():
            return None
        return SessionClaims(
            version=0,
            token_type=_SESSION_TOKEN_TYPE,
            auth_source=_SESSION_AUTH_SOURCE,
            username=payload,
        )

    if isinstance(payload, dict) and set(payload) == _SESSION_V2_KEYS:
        return _decode_v2(payload)

    if not isinstance(payload, dict) or set(payload) != _SESSION_V1_KEYS:
        return None
    if type(payload["v"]) is not int or payload["v"] != _SESSION_TOKEN_VERSION:
        return None
    if payload["type"] != _SESSION_TOKEN_TYPE:
        return None
    if payload["auth_source"] != _SESSION_AUTH_SOURCE:
        return None
    username = payload["username"]
    if not isinstance(username, str) or not username.strip():
        return None

    return SessionClaims(
        version=_SESSION_TOKEN_VERSION,
        token_type=_SESSION_TOKEN_TYPE,
        auth_source=_SESSION_AUTH_SOURCE,
        username=username,
    )


def _decode_v2(payload: dict) -> SessionClaims | None:
    """Strictly decode a federated session, or fail closed.

    Every field is checked for shape here rather than trusted downstream. A
    cookie is signed, so this cannot be forged — but it can be an old envelope
    from a previous release, and accepting a half-understood one is how a
    session outlives the rules that were meant to bound it.
    """

    if type(payload["v"]) is not int or payload["v"] != _OIDC_TOKEN_VERSION:
        return None
    if payload["type"] != _SESSION_TOKEN_TYPE:
        return None
    if payload["auth_source"] != _OIDC_AUTH_SOURCE:
        return None

    username = payload["username"]
    if not isinstance(username, str) or not username.strip():
        return None
    session_version = payload["session_version"]
    if type(session_version) is not int or session_version < 1:
        return None
    authenticated_at = payload["authenticated_at"]
    if authenticated_at is not None and type(authenticated_at) is not int:
        return None

    try:
        user_id = uuid.UUID(payload["user_id"])
        subject_id = (
            uuid.UUID(payload["subject_id"])
            if payload["subject_id"] is not None
            else None
        )
    except (AttributeError, TypeError, ValueError):
        return None

    return SessionClaims(
        version=_OIDC_TOKEN_VERSION,
        token_type=_SESSION_TOKEN_TYPE,
        auth_source=_OIDC_AUTH_SOURCE,
        username=username,
        user_id=user_id,
        session_version=session_version,
        authenticated_at=authenticated_at,
        subject_id=subject_id,
    )


def create_federated_session(
    *,
    username: str,
    user_id: uuid.UUID,
    session_version: int,
    authenticated_at: int | None,
    subject_id: uuid.UUID | None,
) -> str:
    """Issue a version 2 session for somebody the provider authenticated.

    The username rides along so page chrome can greet a person without a
    database read; it is display only. Identity is ``user_id``, and authority
    to still be here is ``session_version``, both of which are checked against
    the database on every request that matters.
    """

    if not isinstance(username, str) or not username.strip():
        raise ValueError("session username must be a non-blank string")
    if not isinstance(user_id, uuid.UUID):
        raise ValueError("session user_id must be a UUID")
    if type(session_version) is not int or session_version < 1:
        raise ValueError("session_version must be a positive integer")

    return _get_serializer().dumps(
        {
            "v": _OIDC_TOKEN_VERSION,
            "type": _SESSION_TOKEN_TYPE,
            "auth_source": _OIDC_AUTH_SOURCE,
            "username": username,
            "user_id": str(user_id),
            "session_version": session_version,
            "authenticated_at": authenticated_at,
            "subject_id": str(subject_id) if subject_id is not None else None,
        }
    )


def read_session(token: str | None) -> Optional[str]:
    """Return the username from a valid browser session, preserving the public API."""
    claims = decode_session(token)
    return claims.username if claims is not None else None


def create_session(username: str) -> str:
    """Generate a version 1 legacy-owner browser session token."""
    if not isinstance(username, str) or not username.strip():
        raise ValueError("session username must be a non-blank string")
    serializer = _get_serializer()
    return serializer.dumps(
        {
            "v": _SESSION_TOKEN_VERSION,
            "type": _SESSION_TOKEN_TYPE,
            "auth_source": _SESSION_AUTH_SOURCE,
            "username": username,
        }
    )


def set_session_cookie(response: Response, token: str) -> None:
    """Set the session cookie on an HTTP response."""
    cfg = get_web_config()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=cfg.session_ttl,
        expires=cfg.session_ttl,
        path="/",
        secure=cfg.cookie_secure,
        httponly=True,
        samesite=cfg.cookie_samesite,
    )


def clear_session_cookie(response: Response) -> None:
    """Clear the session cookie from an HTTP response."""
    response.delete_cookie(
        key=SESSION_COOKIE,
        path="/",
    )


def authenticate(username: str, password: str) -> bool:
    """Verify single-user credentials with constant-time check fallback."""
    if get_web_config().oidc_enabled:
        # After the cutover Vitals holds no password material worth checking,
        # and a stored hash from before it must not become a second way in.
        return False

    cfg = get_web_config()
    if username != cfg.auth_username:
        verify_password_dummy(password)
        return False
    return verify_password(password, cfg.auth_password_hash)


# ── Auth Endpoints ────────────────────────────────────────────────────────────


# ── Federated login ──────────────────────────────────────────────────────────
#
# Two routes and a handoff cookie. ``/auth/start`` builds the authorization
# request and seals its secrets; the provider sends the browser to
# ``/auth/callback`` with a code; that route proves the code is one we asked
# for, and only then does a session exist.


_provider_cache: tuple[tuple[str, str, str], object] | None = None


def _provider():
    """The configured provider, kept between requests.

    Discovery and the signing keys are cached on the provider object, so a new
    one per request would mean two extra round trips on every login — and a
    provider that is briefly slow would then be slow for each of them rather
    than once. The cache key is the configuration, so changing it in place
    (which the tests do) builds a new one rather than serving the old.
    """

    global _provider_cache
    from vitals.services.oidc import OidcProvider, OidcSettings

    cfg = get_web_config()
    key = (cfg.oidc_issuer, cfg.oidc_client_id, cfg.oidc_redirect_url)
    if _provider_cache is not None and _provider_cache[0] == key:
        return _provider_cache[1]

    provider = OidcProvider(
        OidcSettings(
            issuer=cfg.oidc_issuer,
            client_id=cfg.oidc_client_id,
            client_secret=cfg.oidc_client_secret,
            redirect_url=cfg.oidc_redirect_url,
        )
    )
    _provider_cache = (key, provider)
    return provider


def _login_failed(request: Request, reason: str):
    """One page for every way a login can fail.

    The reason is logged and never rendered. "No such account", "your account
    is suspended" and "that token was not for us" are three different sentences
    and one fact to whoever is trying: it did not work.
    """

    logger.warning("federated login refused: %s", reason)
    response = templates.TemplateResponse(
        request,
        "login.html",
        {"error": t("login.error.federated"), "next": "/"},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )
    clear_oidc_handoff_cookie(response)
    return response


@router.get("/auth/start")
async def federated_login_start(request: Request, next: Optional[str] = None):
    """Begin a login at the provider."""

    cfg = get_web_config()
    if not cfg.oidc_enabled:
        raise HTTPException(status_code=404)
    if read_session(request.cookies.get(SESSION_COOKIE)) is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)

    from vitals.services.oidc import OidcError

    try:
        login = await _provider().begin_login()
    except OidcError as exc:
        return _login_failed(request, f"could not begin a login: {exc}")

    response = RedirectResponse(
        url=login.authorization_url, status_code=status.HTTP_303_SEE_OTHER
    )
    set_oidc_handoff_cookie(
        response,
        create_oidc_handoff(
            state=login.state,
            nonce=login.nonce,
            code_verifier=login.code_verifier,
            next_url=safe_next(next),
        ),
    )
    return response


@router.get("/auth/callback")
async def federated_login_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    iss: Optional[str] = None,
    error: Optional[str] = None,
    _rl: None = Depends(login_rate_limit(limit=10, window=300)),
    db: AsyncSession = Depends(get_session),
):
    """Complete a login, or refuse it.

    The handoff cookie is cleared on every path out of here, success or not. A
    handoff that survives its callback is one that can be replayed.
    """

    cfg = get_web_config()
    if not cfg.oidc_enabled:
        raise HTTPException(status_code=404)

    handoff = read_oidc_handoff(request.cookies.get(OIDC_HANDOFF_COOKIE))
    if handoff is None:
        return _login_failed(request, "callback arrived with no usable handoff")

    if error:
        # The provider declined. Its error code says why and is for the log.
        return _login_failed(request, f"provider returned an error: {error}")
    if not code or not state:
        return _login_failed(request, "callback arrived without a code and state")

    # Constant-time, because an attacker who can measure how far the comparison
    # got can recover the state a character at a time and forge a callback.
    if not secrets.compare_digest(state, handoff["state"]):
        return _login_failed(request, "callback state does not match this browser's")

    from vitals.services.federated_login_service import (
        FederatedLoginError,
        resolve_federated_user,
    )
    from vitals.services.oidc import OidcError

    provider = _provider()
    try:
        provider.check_response_issuer(iss)
        identity = await provider.complete_login(
            code=code,
            code_verifier=handoff["code_verifier"],
            expected_nonce=handoff["nonce"],
        )
    except OidcError as exc:
        return _login_failed(request, f"token rejected: {exc}")

    try:
        user = await resolve_federated_user(
            db,
            issuer=identity.issuer,
            subject=identity.subject,
            authenticated_at=identity.authenticated_at,
            bootstrap_subject=cfg.oidc_bootstrap_subject,
            # Claims, not keys. They name an account this installation has
            # already decided to create; ``registration_service`` is what makes
            # that decision, and by default it does not.
            email=identity.email,
            preferred_username=identity.preferred_username,
        )
    except FederatedLoginError as exc:
        return _login_failed(request, f"no session for this identity: {exc}")

    # Queried rather than reached through ``user.owned_subject``: that
    # relationship lazy-loads, which outside a greenlet context raises instead
    # of loading — on the one path where every successful login goes.
    from vitals.models.identity import HealthSubject

    subject_id = await db.scalar(
        select(HealthSubject.id).where(HealthSubject.owner_user_id == user.id)
    )

    token = create_federated_session(
        username=user.username,
        user_id=user.id,
        session_version=user.session_version,
        authenticated_at=(
            int(identity.authenticated_at.timestamp())
            if identity.authenticated_at is not None
            else None
        ),
        subject_id=subject_id,
    )
    response = RedirectResponse(
        url=safe_next(handoff["next"]), status_code=status.HTTP_303_SEE_OTHER
    )
    set_session_cookie(response, token)
    clear_oidc_handoff_cookie(response)
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: Optional[str] = None):
    # If already logged in, redirect to dashboard
    token = request.cookies.get(SESSION_COOKIE)
    if read_session(token) is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    if get_web_config().oidc_enabled:
        # A bookmark from before the cutover still works; it lands at the
        # provider instead of at a password field.
        target = "/auth/start"
        if next:
            target += f"?next={quote(safe_next(next), safe='')}"
        return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"next": safe_next(next)})


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: Optional[str] = Form(None),
    # Bound password-guessing by IP. `rate_limit` can't guard this route (it needs
    # an authenticated username for its key), so `/login` gets a dedicated IP-based
    # limiter — this is the only pre-auth entry point and the one that needs it most.
    _rl: None = Depends(login_rate_limit(limit=10, window=300)),
    db: AsyncSession = Depends(get_session),
):
    if get_web_config().oidc_enabled:
        # Hard cutover: this deployment authenticates through its provider, so
        # there is no password to check and no code to consume. 404 rather than
        # 403 — the route is not something this installation has.
        raise HTTPException(status_code=404)
    next_url = safe_next(next)
    if authenticate(username, password):
        # 2FA on → the password alone completes nothing. Hand over only the
        # short-lived pending handle and send the browser to the code step; the
        # real session is minted there and nowhere else.
        if (await twofa_service.get_state(db)).enabled:
            response = RedirectResponse(
                url=f"/login/2fa?next={quote(next_url, safe='')}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
            set_pending_2fa_cookie(response, create_pending_2fa(username))
            return response

        token = create_session(username)
        response = RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)
        set_session_cookie(response, token)
        return response

    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": t("login.error.bad_credentials"), "next": next_url},
    )


# ── Second factor (step 2 of login, only when 2FA is switched on) ─────────────


@router.get("/login/2fa", response_class=HTMLResponse)
async def login_2fa_page(request: Request, next: Optional[str] = None):
    if get_web_config().oidc_enabled:
        raise HTTPException(status_code=404)
    # Reachable only with a live pending handle, so the page can't be used to
    # probe whether 2FA is on for this install.
    if read_pending_2fa(request.cookies.get(PENDING_2FA_COOKIE)) is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request, "login_2fa.html", {"next": safe_next(next)})


@router.post("/login/2fa")
async def login_2fa(
    request: Request,
    code: str = Form(...),
    next: Optional[str] = Form(None),
    # Global, not per-IP: six digits are guessable in bulk and rotating the
    # source address must not buy a fresh allowance. See ``login_rate_limit``.
    _rl: None = Depends(
        login_rate_limit(limit=10, window=300, bucket="login_2fa", per_ip=False)
    ),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    if get_web_config().oidc_enabled:
        raise HTTPException(status_code=404)
    next_url = safe_next(next)
    token = request.cookies.get(PENDING_2FA_COOKIE)
    username = read_pending_2fa(token)
    if username is None:
        # Nothing to complete — five minutes ran out, or there was never a
        # password step. Start over rather than hint at which.
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    state = await twofa_service.get_state(db)
    step = twofa_service.verify_code(state.secret, code) if state.enabled else None
    if step is None or not await twofa_service.consume_step(redis, step):
        return templates.TemplateResponse(
            request,
            "login_2fa.html",
            {"error": t("login.error.bad_code"), "next": next_url},
        )

    response = RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, create_session(username))
    clear_pending_2fa_cookie(response)
    return response


# POST only: a GET logout is a link, and any page (or prefetcher) can point at it.
# Both templates already submit a form — masthead.html and more.html.
@router.post("/logout")
async def logout(request: Request):
    target = "/login"
    cfg = get_web_config()
    claims = decode_session(request.cookies.get(SESSION_COOKIE))
    if (
        cfg.oidc_enabled
        and claims is not None
        and claims.auth_source == _OIDC_AUTH_SOURCE
    ):
        callback = urlsplit(cfg.oidc_redirect_url)
        post_logout_redirect_uri = urlunsplit(
            (callback.scheme, callback.netloc, "/", "", "")
        )
        from vitals.services.oidc import OidcError

        try:
            provider_target = await _provider().end_session_url(
                post_logout_redirect_uri=post_logout_redirect_uri
            )
            if provider_target is not None:
                target = provider_target
        except OidcError as exc:
            # Local logout is the fail-safe result. A provider outage must not
            # keep the Vitals cookie alive merely because federated logout
            # could not be completed.
            logger.warning("provider logout unavailable: %s", exc)

    response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response)
    clear_pending_2fa_cookie(response)
    return response
