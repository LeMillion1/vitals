"""Signed browser credentials and their cookie transport."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

from itsdangerous import BadData, BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.responses import Response

from web.config import (
    OIDC_HANDOFF_COOKIE,
    OIDC_HANDOFF_TTL,
    PENDING_2FA_COOKIE,
    PENDING_2FA_TTL,
    SESSION_COOKIE,
    get_web_config,
)

_SESSION_TOKEN_VERSION = 1
_SESSION_TOKEN_TYPE = "web_session"
_SESSION_AUTH_SOURCE = "legacy_env"
_SESSION_V1_KEYS = frozenset({"v", "type", "auth_source", "username"})

_OIDC_TOKEN_VERSION = 2
OIDC_AUTH_SOURCE = "oidc"
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

    Version 1 carries the environment-backed owner's username. Version 2
    carries the stable local user id, revocable session version, provider
    authentication time, and the health subject this session acts for.
    """

    version: int
    token_type: str
    auth_source: str
    username: str
    user_id: uuid.UUID | None = None
    session_version: int | None = None
    authenticated_at: int | None = None
    subject_id: uuid.UUID | None = None


def _get_serializer() -> URLSafeTimedSerializer:
    cfg = get_web_config()
    return URLSafeTimedSerializer(cfg.session_secret, salt="vitals-session")


def _get_mcp_serializer() -> URLSafeTimedSerializer:
    """Return the serializer isolated from browser-session signatures."""

    cfg = get_web_config()
    return URLSafeTimedSerializer(cfg.session_secret, salt="vitals-mcp")


def _get_oidc_handoff_serializer() -> URLSafeTimedSerializer:
    """Return the serializer isolated for state, nonce, and PKCE handoff."""

    cfg = get_web_config()
    return URLSafeTimedSerializer(cfg.session_secret, salt="vitals-oidc-handoff")


def _get_pending_2fa_serializer() -> URLSafeTimedSerializer:
    """Return the serializer isolated for the password-to-TOTP handoff."""

    cfg = get_web_config()
    return URLSafeTimedSerializer(cfg.session_secret, salt="vitals-2fa")


def create_pending_2fa(username: str) -> str:
    """Sign a handle saying this username passed the password check."""

    return _get_pending_2fa_serializer().dumps(username)


def read_pending_2fa(token: str | None) -> Optional[str]:
    """Return the username behind a live pending handle."""

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


def create_oidc_handoff(
    *,
    state: str,
    nonce: str,
    code_verifier: str,
    next_url: str,
    max_age_seconds: int | None = None,
    invitation_id: uuid.UUID | None = None,
) -> str:
    """Seal what the callback needs for the browser to carry there."""

    if invitation_id is not None and (
        not isinstance(invitation_id, uuid.UUID) or invitation_id.int == 0
    ):
        raise ValueError("invitation_id must be a non-zero UUID")
    return _get_oidc_handoff_serializer().dumps(
        {
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "next": next_url,
            "max_age_seconds": max_age_seconds,
            "admission_type": (
                "registration_invitation" if invitation_id is not None else None
            ),
            "invitation_id": (
                str(invitation_id) if invitation_id is not None else None
            ),
        }
    )


def read_oidc_handoff(token: str | None) -> dict | None:
    """Open a strictly shaped OIDC handoff, or fail closed."""

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
        "max_age_seconds",
        "admission_type",
        "invitation_id",
    }:
        return None
    for key in ("state", "nonce", "code_verifier"):
        value = payload[key]
        if not isinstance(value, str) or not value.strip():
            return None
    if not isinstance(payload["next"], str):
        return None
    max_age_seconds = payload["max_age_seconds"]
    if max_age_seconds is not None and (
        type(max_age_seconds) is not int or max_age_seconds <= 0
    ):
        return None
    invitation_id = payload["invitation_id"]
    admission_type = payload["admission_type"]
    if (invitation_id is None) != (admission_type is None):
        return None
    if admission_type is not None and admission_type != "registration_invitation":
        return None
    if invitation_id is not None:
        try:
            invitation_id = uuid.UUID(invitation_id)
        except (AttributeError, TypeError, ValueError):
            return None
        if invitation_id.int == 0:
            return None
    payload["invitation_id"] = invitation_id
    return payload


def set_oidc_handoff_cookie(response: Response, token: str) -> None:
    cfg = get_web_config()
    response.set_cookie(
        key=OIDC_HANDOFF_COOKIE,
        value=token,
        max_age=OIDC_HANDOFF_TTL,
        httponly=True,
        secure=cfg.cookie_secure,
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
    """Confine post-login redirects to a local path."""

    if not next or not next.startswith("/") or next.startswith("//") or "\\" in next:
        return "/"
    parsed = urlsplit(next)
    if parsed.scheme or parsed.netloc:
        return "/"
    return next


def decode_session(token: str | None) -> SessionClaims | None:
    """Verify and strictly decode a browser session token."""

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


def session_issued_at(token: str | None) -> datetime | None:
    """Return the signed cookie issuance time after envelope validation."""

    if decode_session(token) is None:
        return None
    try:
        _payload, issued_at = _get_serializer().loads(
            token,
            max_age=get_web_config().session_ttl,
            return_timestamp=True,
        )
    except BadData:
        return None
    return issued_at


def _decode_v2(payload: dict) -> SessionClaims | None:
    if type(payload["v"]) is not int or payload["v"] != _OIDC_TOKEN_VERSION:
        return None
    if payload["type"] != _SESSION_TOKEN_TYPE:
        return None
    if payload["auth_source"] != OIDC_AUTH_SOURCE:
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
        auth_source=OIDC_AUTH_SOURCE,
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
    """Issue a version 2 session for a provider-authenticated user."""

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
            "auth_source": OIDC_AUTH_SOURCE,
            "username": username,
            "user_id": str(user_id),
            "session_version": session_version,
            "authenticated_at": authenticated_at,
            "subject_id": str(subject_id) if subject_id is not None else None,
        }
    )


def session_allowed_in_current_auth_mode(claims: SessionClaims | None) -> bool:
    """Return whether claims came from the configured authentication authority."""

    if (
        claims is not None
        and get_web_config().oidc_enabled
        and claims.auth_source != OIDC_AUTH_SOURCE
    ):
        return False
    return claims is not None


def read_session(token: str | None) -> Optional[str]:
    """Return a username only from a session valid in the current auth mode."""

    claims = decode_session(token)
    return claims.username if session_allowed_in_current_auth_mode(claims) else None


def create_session(username: str) -> str:
    """Generate a version 1 legacy-owner browser session token."""

    if not isinstance(username, str) or not username.strip():
        raise ValueError("session username must be a non-blank string")
    return _get_serializer().dumps(
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

    response.delete_cookie(key=SESSION_COOKIE, path="/")


__all__ = [
    "OIDC_AUTH_SOURCE",
    "SessionClaims",
    "_get_mcp_serializer",
    "clear_oidc_handoff_cookie",
    "clear_pending_2fa_cookie",
    "clear_session_cookie",
    "create_federated_session",
    "create_oidc_handoff",
    "create_pending_2fa",
    "create_session",
    "decode_session",
    "read_oidc_handoff",
    "read_pending_2fa",
    "read_session",
    "safe_next",
    "session_allowed_in_current_auth_mode",
    "session_issued_at",
    "set_oidc_handoff_cookie",
    "set_pending_2fa_cookie",
    "set_session_cookie",
]
