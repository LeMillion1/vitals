"""Browser-session envelope compatibility and strict-decoding tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from starlette.requests import Request

from web.auth import (
    SessionClaims,
    _get_mcp_serializer,
    _get_pending_2fa_serializer,
    _get_serializer,
    create_session,
    decode_session,
    read_session,
)
from web.config import SESSION_COOKIE
from web.deps import require_auth


def test_legacy_v0_bare_username_remains_compatible():
    token = _get_serializer().dumps("tester")

    assert decode_session(token) == SessionClaims(
        version=0,
        token_type="web_session",
        auth_source="legacy_env",
        username="tester",
    )
    assert read_session(token) == "tester"


def test_v1_session_uses_exact_envelope_and_round_trips():
    token = create_session("tester")

    assert _get_serializer().loads(token) == {
        "v": 1,
        "type": "web_session",
        "auth_source": "legacy_env",
        "username": "tester",
    }
    assert decode_session(token) == SessionClaims(
        version=1,
        token_type="web_session",
        auth_source="legacy_env",
        username="tester",
    )
    # Keep the existing public contract used by route dependencies and rate limits.
    assert read_session(token) == "tester"


def test_session_claims_are_immutable():
    claims = decode_session(create_session("tester"))
    assert claims is not None

    with pytest.raises(FrozenInstanceError):
        claims.username = "other"  # type: ignore[misc]


async def test_login_emits_v1_while_require_auth_still_returns_username(client):
    response = await client.post(
        "/login", data={"username": "tester", "password": "password"}
    )
    token = response.cookies[SESSION_COOKIE]

    assert response.status_code == 303
    assert _get_serializer().loads(token) == {
        "v": 1,
        "type": "web_session",
        "auth_source": "legacy_env",
        "username": "tester",
    }

    request = Request(
        {
            "type": "http",
            "headers": [
                (b"cookie", f"{SESSION_COOKIE}={token}".encode("ascii")),
            ],
        }
    )
    username = await require_auth(request)
    assert username == "tester"
    assert isinstance(username, str)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        1,
        ["tester"],
        "",
        "   ",
        {"v": 1, "type": "web_session", "auth_source": "legacy_env"},
        {
            "v": 1,
            "type": "web_session",
            "auth_source": "legacy_env",
            "username": "tester",
            "roles": ["platform_superadmin"],
        },
        {
            "v": 2,
            "type": "web_session",
            "auth_source": "legacy_env",
            "username": "tester",
        },
        {
            "v": True,
            "type": "web_session",
            "auth_source": "legacy_env",
            "username": "tester",
        },
        {
            "v": 1,
            "type": "mcp_access_token",
            "auth_source": "legacy_env",
            "username": "tester",
        },
        {
            "v": 1,
            "type": "web_session",
            "auth_source": "db",
            "username": "tester",
        },
        {
            "v": 1,
            "type": "web_session",
            "auth_source": "legacy_env",
            "username": " ",
        },
        {
            "v": 1,
            "type": "web_session",
            "auth_source": "legacy_env",
            "username": 123,
        },
    ],
)
def test_unknown_malformed_blank_and_extra_claims_are_rejected(payload):
    token = _get_serializer().dumps(payload)

    assert decode_session(token) is None
    assert read_session(token) is None


def test_signed_but_undecodable_payload_is_rejected():
    serializer = _get_serializer()
    token = serializer.make_signer(serializer.salt).sign(b"not-json").decode("ascii")

    assert decode_session(token) is None
    assert read_session(token) is None


@pytest.mark.parametrize("username", ["", "   ", None, 123])
def test_session_issuer_rejects_invalid_usernames(username):
    with pytest.raises(ValueError, match="non-blank"):
        create_session(username)  # type: ignore[arg-type]


def test_mcp_and_pending_2fa_tokens_cannot_cross_into_browser_sessions():
    mcp_token = _get_mcp_serializer().dumps(
        {
            "username": "tester",
            "client_id": "vitals-claude-connector",
            "type": "mcp_access_token",
        }
    )
    pending_2fa_token = _get_pending_2fa_serializer().dumps("tester")

    assert decode_session(mcp_token) is None
    assert decode_session(pending_2fa_token) is None


def test_browser_salt_rejects_non_session_dict_even_with_valid_signature():
    token = _get_serializer().dumps(
        {
            "username": "tester",
            "client_id": "vitals-claude-connector",
            "type": "mcp_access_token",
        }
    )

    assert decode_session(token) is None
