"""Short-lived browser proof for an account invitation already presented.

The invitation bearer arrives in the URL fragment, which browsers do not send
to the server. A same-origin exchange validates it and replaces it with this
signed handle containing only an opaque database UUID. Cookies are signed, not
encrypted, so no token, address, provider claim, or account kind belongs here.
"""

from __future__ import annotations

import uuid

from fastapi import Response
from itsdangerous import BadData, URLSafeTimedSerializer

from web.config import (
    REGISTRATION_ADMISSION_COOKIE,
    REGISTRATION_ADMISSION_TTL,
    get_web_config,
)

_VERSION = 1
_TYPE = "registration_invitation"
_KEYS = frozenset({"v", "type", "invitation_id"})


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


__all__ = [
    "clear_invitation_claim_cookie",
    "create_invitation_claim",
    "read_invitation_claim",
    "set_invitation_claim_cookie",
]
