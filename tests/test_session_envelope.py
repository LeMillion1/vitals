"""What a session cookie may and may not say about itself.

The cookie is signed, so it cannot be forged. It can still be an envelope from
an older release, a truncated one, or one carrying a field this version does not
understand — and accepting a half-understood envelope is how a session outlives
the rules that were meant to bound it. Everything here is about failing closed
on shapes rather than on signatures.
"""

from __future__ import annotations

import uuid

import pytest

from web.auth import (
    create_federated_session,
    create_session,
    decode_session,
    read_session,
)


def test_a_federated_session_round_trips_every_field():
    user_id, subject_id = uuid.uuid4(), uuid.uuid4()
    token = create_federated_session(
        username="owner",
        user_id=user_id,
        session_version=7,
        authenticated_at=1_756_000_000,
        subject_id=subject_id,
    )
    claims = decode_session(token)

    assert claims is not None
    assert claims.version == 2
    assert claims.auth_source == "oidc"
    assert claims.user_id == user_id
    assert claims.session_version == 7
    assert claims.authenticated_at == 1_756_000_000
    assert claims.subject_id == subject_id


def test_a_federated_session_may_carry_no_subject_yet():
    """Between logging in and choosing whose record to open, there is none."""

    token = create_federated_session(
        username="owner",
        user_id=uuid.uuid4(),
        session_version=1,
        authenticated_at=None,
        subject_id=None,
    )
    claims = decode_session(token)
    assert claims is not None and claims.subject_id is None
    assert claims.authenticated_at is None


def test_the_older_envelopes_still_decode():
    """A release must not log everybody out to deploy.

    Version 1 cookies stay valid until their normal TTL expires; the fields
    version 2 adds simply are not there.
    """

    claims = decode_session(create_session("owner"))
    assert claims is not None
    assert claims.version == 1
    assert claims.username == "owner"
    assert claims.user_id is None
    assert claims.session_version is None


def test_read_session_keeps_returning_a_username_for_either_version():
    """Twenty-five routers depend on this signature; both envelopes feed it."""

    assert read_session(create_session("owner")) == "owner"
    assert (
        read_session(
            create_federated_session(
                username="owner",
                user_id=uuid.uuid4(),
                session_version=1,
                authenticated_at=None,
                subject_id=None,
            )
        )
        == "owner"
    )


# ── Failing closed ───────────────────────────────────────────────────────────

def test_a_tampered_cookie_is_rejected():
    token = create_federated_session(
        username="owner",
        user_id=uuid.uuid4(),
        session_version=1,
        authenticated_at=None,
        subject_id=None,
    )
    assert decode_session(token[:-4] + "AAAA") is None
    assert decode_session("") is None
    assert decode_session(None) is None


def test_an_envelope_with_an_extra_field_is_rejected():
    """A field this version does not understand is a version it does not know."""

    from itsdangerous import URLSafeTimedSerializer

    from web.config import get_web_config

    serializer = URLSafeTimedSerializer(
        get_web_config().session_secret, salt="vitals-session"
    )
    token = serializer.dumps(
        {
            "v": 2,
            "type": "web_session",
            "auth_source": "oidc",
            "username": "owner",
            "user_id": str(uuid.uuid4()),
            "session_version": 1,
            "authenticated_at": None,
            "subject_id": None,
            "is_admin": True,  # not a field this version has ever had
        }
    )
    assert decode_session(token) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", "not-a-uuid"),
        ("user_id", None),
        ("subject_id", "not-a-uuid"),
        ("session_version", 0),
        ("session_version", -1),
        ("session_version", "1"),
        ("session_version", True),
        ("authenticated_at", "recently"),
        ("username", ""),
        ("username", "   "),
        ("auth_source", "legacy_env"),
        ("type", "mcp_token"),
        ("v", 1),
        ("v", 3),
    ],
)
def test_a_malformed_field_fails_closed(field, value):
    """Each of these would otherwise be trusted by something downstream."""

    from itsdangerous import URLSafeTimedSerializer

    from web.config import get_web_config

    payload = {
        "v": 2,
        "type": "web_session",
        "auth_source": "oidc",
        "username": "owner",
        "user_id": str(uuid.uuid4()),
        "session_version": 1,
        "authenticated_at": None,
        "subject_id": None,
    }
    payload[field] = value
    serializer = URLSafeTimedSerializer(
        get_web_config().session_secret, salt="vitals-session"
    )
    assert decode_session(serializer.dumps(payload)) is None


def test_issuing_a_session_for_nobody_is_refused():
    for kwargs in (
        {"username": "", "user_id": uuid.uuid4(), "session_version": 1},
        {"username": "owner", "user_id": "not-a-uuid", "session_version": 1},
        {"username": "owner", "user_id": uuid.uuid4(), "session_version": 0},
        {"username": "owner", "user_id": uuid.uuid4(), "session_version": True},
    ):
        with pytest.raises(ValueError):
            create_federated_session(
                authenticated_at=None, subject_id=None, **kwargs
            )


def test_the_cookie_carries_no_authorization_state():
    """It is signed, not secret: anybody holding it can read every field.

    Roles and subject access therefore stay out of it and are resolved from the
    database, where revoking one takes effect immediately rather than when a
    cookie expires.
    """

    import json
    import zlib

    from itsdangerous.encoding import base64_decode

    token = create_federated_session(
        username="owner",
        user_id=uuid.uuid4(),
        session_version=1,
        authenticated_at=None,
        subject_id=uuid.uuid4(),
    )
    # No secret involved: this is what anybody holding the cookie can read.
    # itsdangerous marks a zlib-compressed payload with a leading dot, before
    # the usual payload.timestamp.signature.
    compressed = token.startswith(".")
    body = (token[1:] if compressed else token).split(".")[0]
    raw = base64_decode(body)
    decoded = json.loads(zlib.decompress(raw) if compressed else raw)
    assert set(decoded) == {
        "v",
        "type",
        "auth_source",
        "username",
        "user_id",
        "session_version",
        "authenticated_at",
        "subject_id",
    }
    for forbidden in ("roles", "is_admin", "scopes", "permissions", "email"):
        assert forbidden not in decoded
