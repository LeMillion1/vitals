"""Short-lived browser proofs for account-admission handoffs.

The invitation bearer arrives in the URL fragment, which browsers do not send
to the server. A same-origin exchange validates it and replaces it with this
signed handle containing only an opaque database UUID. Public registration uses
the same safe shape with a separate salt: account kind remains in its one-time
database intent while the browser carries only the opaque row id. Cookies are
signed, not encrypted, so no token, address, provider claim, account kind, or
decision detail belongs here. Administrator-approved requests use a third salt
and cookie carrying only an opaque UUID and public state so OAuth query
parameters do not remain in browser history.
"""

from __future__ import annotations

import uuid

from fastapi import Response
from itsdangerous import BadData, URLSafeTimedSerializer

from web.config import (
    REGISTRATION_ADMISSION_COOKIE,
    REGISTRATION_ADMISSION_TTL,
    REGISTRATION_INTENT_COOKIE,
    REGISTRATION_INTENT_TTL,
    REGISTRATION_REQUEST_COOKIE,
    REGISTRATION_REQUEST_TTL,
    get_web_config,
)

_VERSION = 1
_TYPE = "registration_invitation"
_KEYS = frozenset({"v", "type", "invitation_id"})
_REQUEST_TYPE = "registration_request_status"
_REQUEST_KEYS = frozenset({"v", "type", "request_id", "state"})
_REQUEST_STATES = frozenset({"pending", "closed"})
_INTENT_TYPE = "registration_intent"
_INTENT_KEYS = frozenset({"v", "type", "intent_id"})


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        get_web_config().session_secret,
        salt="vitals-registration-admission",
    )


def create_invitation_claim(invitation_id: uuid.UUID) -> str:
    if not isinstance(invitation_id, uuid.UUID) or invitation_id.int == 0:
        raise ValueError("invitation_id must be a non-zero UUID")
    return _serializer().dumps(
        {
            "v": _VERSION,
            "type": _TYPE,
            "invitation_id": str(invitation_id),
        }
    )


def read_invitation_claim(token: str | None) -> uuid.UUID | None:
    if not isinstance(token, str) or not token:
        return None
    try:
        payload = _serializer().loads(
            token,
            max_age=REGISTRATION_ADMISSION_TTL,
        )
    except BadData:
        return None
    if not isinstance(payload, dict) or set(payload) != _KEYS:
        return None
    if type(payload["v"]) is not int or payload["v"] != _VERSION:
        return None
    if payload["type"] != _TYPE:
        return None
    try:
        invitation_id = uuid.UUID(payload["invitation_id"])
    except (AttributeError, TypeError, ValueError):
        return None
    return invitation_id if invitation_id.int != 0 else None


def set_invitation_claim_cookie(response: Response, token: str) -> None:
    cfg = get_web_config()
    response.set_cookie(
        key=REGISTRATION_ADMISSION_COOKIE,
        value=token,
        max_age=REGISTRATION_ADMISSION_TTL,
        path="/auth",
        secure=cfg.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_invitation_claim_cookie(response: Response) -> None:
    cfg = get_web_config()
    response.delete_cookie(
        key=REGISTRATION_ADMISSION_COOKIE,
        path="/auth",
        secure=cfg.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _intent_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        get_web_config().session_secret,
        salt="vitals-registration-intent",
    )


def create_registration_intent_claim(intent_id: uuid.UUID) -> str:
    """Seal only the opaque database id selected before provider login."""

    if not isinstance(intent_id, uuid.UUID) or intent_id.int == 0:
        raise ValueError("intent_id must be a non-zero UUID")
    return _intent_serializer().dumps(
        {
            "v": _VERSION,
            "type": _INTENT_TYPE,
            "intent_id": str(intent_id),
        }
    )


def read_registration_intent_claim(token: str | None) -> uuid.UUID | None:
    if not isinstance(token, str) or not token:
        return None
    try:
        payload = _intent_serializer().loads(
            token,
            max_age=REGISTRATION_INTENT_TTL,
        )
    except BadData:
        return None
    if not isinstance(payload, dict) or set(payload) != _INTENT_KEYS:
        return None
    if type(payload["v"]) is not int or payload["v"] != _VERSION:
        return None
    if payload["type"] != _INTENT_TYPE:
        return None
    try:
        intent_id = uuid.UUID(payload["intent_id"])
    except (AttributeError, TypeError, ValueError):
        return None
    return intent_id if intent_id.int != 0 else None


def set_registration_intent_cookie(response: Response, token: str) -> None:
    cfg = get_web_config()
    response.set_cookie(
        key=REGISTRATION_INTENT_COOKIE,
        value=token,
        max_age=REGISTRATION_INTENT_TTL,
        path="/auth",
        secure=cfg.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_registration_intent_cookie(response: Response) -> None:
    cfg = get_web_config()
    response.delete_cookie(
        key=REGISTRATION_INTENT_COOKIE,
        path="/auth",
        secure=cfg.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _request_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        get_web_config().session_secret,
        salt="vitals-registration-request-status",
    )


def create_request_status_claim(request_id: uuid.UUID, *, state: str) -> str:
    """Seal the non-secret state carried across the callback redirect."""

    if not isinstance(request_id, uuid.UUID) or request_id.int == 0:
        raise ValueError("request_id must be a non-zero UUID")
    if state not in _REQUEST_STATES:
        raise ValueError("request state is invalid")
    return _request_serializer().dumps(
        {
            "v": _VERSION,
            "type": _REQUEST_TYPE,
            "request_id": str(request_id),
            "state": state,
        }
    )


def read_request_status_claim(token: str | None) -> tuple[uuid.UUID, str] | None:
    """Read one unexpired status handoff without consulting applicant input."""

    if not isinstance(token, str) or not token:
        return None
    try:
        payload = _request_serializer().loads(
            token,
            max_age=REGISTRATION_REQUEST_TTL,
        )
    except BadData:
        return None
    if not isinstance(payload, dict) or set(payload) != _REQUEST_KEYS:
        return None
    if type(payload["v"]) is not int or payload["v"] != _VERSION:
        return None
    if payload["type"] != _REQUEST_TYPE or payload["state"] not in _REQUEST_STATES:
        return None
    try:
        request_id = uuid.UUID(payload["request_id"])
    except (AttributeError, TypeError, ValueError):
        return None
    if request_id.int == 0:
        return None
    return request_id, payload["state"]


def set_request_status_cookie(response: Response, token: str) -> None:
    cfg = get_web_config()
    response.set_cookie(
        key=REGISTRATION_REQUEST_COOKIE,
        value=token,
        max_age=REGISTRATION_REQUEST_TTL,
        path="/auth/registration-request",
        secure=cfg.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_request_status_cookie(response: Response) -> None:
    cfg = get_web_config()
    response.delete_cookie(
        key=REGISTRATION_REQUEST_COOKIE,
        path="/auth/registration-request",
        secure=cfg.cookie_secure,
        httponly=True,
        samesite="lax",
    )


__all__ = [
    "clear_invitation_claim_cookie",
    "clear_request_status_cookie",
    "clear_registration_intent_cookie",
    "create_invitation_claim",
    "create_request_status_claim",
    "create_registration_intent_claim",
    "read_invitation_claim",
    "read_request_status_claim",
    "read_registration_intent_claim",
    "set_invitation_claim_cookie",
    "set_request_status_cookie",
    "set_registration_intent_cookie",
]
