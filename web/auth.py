"""Single-user authentication logic, session cookie management, and auth router.

Uses itsdangerous timed serialization for signed session cookies, matching
the single-user configuration loaded from web/config.py.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadData, BadSignature, SignatureExpired, URLSafeTimedSerializer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.i18n import t
from vitals.services import twofa_service
from vitals.utils.passwords import verify_password, verify_password_dummy
from web.config import PENDING_2FA_COOKIE, PENDING_2FA_TTL, SESSION_COOKIE, get_web_config
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
    cfg = get_web_config()
    if username != cfg.auth_username:
        verify_password_dummy(password)
        return False
    return verify_password(password, cfg.auth_password_hash)


# ── Auth Endpoints ────────────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: Optional[str] = None):
    # If already logged in, redirect to dashboard
    token = request.cookies.get(SESSION_COOKIE)
    if read_session(token) is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
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
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response)
    clear_pending_2fa_cookie(response)
    return response
