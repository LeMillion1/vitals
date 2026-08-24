"""A connector token that says what it is for, and can be taken back.

Before this it said neither. The payload carried a username, a client id and a
type — no audience, so a token minted for one installation was a token for any
installation that shared a signing secret; and no id of its own, so the only way
to withdraw one was rotating ``VITALS_SESSION_SECRET``, which also invalidates
every web session. "Disconnect the laptop I lost" and "sign the whole household
out and reconnect every client" were one operation, which is a revocation
mechanism in the sense that a fire alarm is a door.

PR-10 asks these to be proved by name: *audience/issuer/client binding,
expiration, replay, consent and token revocation, and immediate user
suspension.*
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from vitals.enums import UserStatus
from vitals.models.identity import McpAccessToken, User
from vitals.services import mcp_token_service as tokens

CLIENT = "vitals-claude-connector"
AUDIENCE = "http://test/mcp"
ISSUER = "http://test"


async def _issue(session, username: str = "tester", **overrides):
    payload, record = await tokens.issue(
        session,
        username=username,
        client_id=overrides.pop("client_id", CLIENT),
        audience=overrides.pop("audience", AUDIENCE),
        issuer=ISSUER,
        **overrides,
    )
    await session.commit()
    return payload, record


async def _verify(session, payload, *, token: str = "signed-value", **overrides):
    return await tokens.verify(
        session,
        payload=payload,
        token=token,
        expected_client_id=overrides.pop("expected_client_id", CLIENT),
        expected_audience=overrides.pop("expected_audience", AUDIENCE),
        expected_issuer=overrides.pop("expected_issuer", ISSUER),
        **overrides,
    )


# ── What the token is for ────────────────────────────────────────────────────


async def test_a_minted_token_names_the_account_the_resource_and_itself(
    db_session, legacy_owner_roots
):
    """``sub``, ``aud``, ``iss`` and ``jti``, which is the whole point.

    ``username`` stays beside ``sub`` deliberately: the subject seam resolves
    by name, and dropping the name in favour of an id would mean two ways to say
    who is asking with no guarantee they agree.
    """

    payload, record = await _issue(db_session)
    assert payload["sub"] == str(legacy_owner_roots.user_id)
    assert payload["username"] == "tester"
    assert payload["aud"] == AUDIENCE
    assert payload["iss"] == ISSUER
    assert payload["jti"] == str(record.id)
    assert record.audience == AUDIENCE


async def test_a_token_minted_for_another_resource_is_refused(
    db_session, legacy_owner_roots
):
    """The audience is what makes a credential specific to this installation.

    Two Vitals installations that shared a signing secret — a restored backup, a
    staging copy — would otherwise accept each other's connector tokens, and
    nothing in the token would say which one it was for.
    """

    payload, _record = await _issue(db_session, audience="https://elsewhere.test/mcp")
    assert await _verify(db_session, payload) is None


async def test_a_token_for_another_client_is_refused(db_session, legacy_owner_roots):
    payload, _record = await _issue(db_session)
    assert (
        await _verify(db_session, payload, expected_client_id="somebody-else") is None
    )


async def test_a_token_from_another_issuer_is_refused(
    db_session, legacy_owner_roots
):
    payload, _record = await _issue(db_session)
    payload["iss"] = "https://restored-copy.example.test"
    assert await _verify(db_session, payload) is None


@pytest.mark.parametrize("claim", ["aud", "iss"])
async def test_a_registry_token_cannot_drop_its_installation_binding(
    db_session, legacy_owner_roots, claim
):
    payload, _record = await _issue(db_session)
    del payload[claim]
    assert await _verify(db_session, payload) is None


async def test_the_audience_is_the_resource_not_the_origin():
    """This origin also serves a website, a JSON API and an OAuth server.

    A token whose audience were the bare origin would be a token for all of
    them.
    """

    assert tokens.audience_for("https://vitals.example") == "https://vitals.example/mcp"
    assert tokens.audience_for("https://vitals.example/") == "https://vitals.example/mcp"


# ── Taking it back ───────────────────────────────────────────────────────────


async def test_a_revoked_connector_stops_working_on_the_next_request(
    db_session, legacy_owner_roots
):
    """Immediately, and without touching anybody else's session.

    That is the difference this table buys: the old answer was to rotate the
    signing secret, which invalidates every MCP token and every web session at
    once.
    """

    payload, record = await _issue(db_session)
    assert await _verify(db_session, payload) is not None

    await tokens.revoke(
        db_session, user_id=legacy_owner_roots.user_id, jti=record.id
    )
    await db_session.commit()
    assert await _verify(db_session, payload) is None


async def test_only_the_account_that_authorized_it_may_disconnect_it(
    db_session, legacy_owner_roots
):
    """And a stranger is told it does not exist rather than that it is not theirs."""

    payload, record = await _issue(db_session)
    stranger = User(
        username="not-the-owner",
        normalized_username="not-the-owner",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(stranger)
    await db_session.commit()

    with pytest.raises(tokens.TokenNotFound):
        await tokens.revoke(db_session, user_id=stranger.id, jti=record.id)
    assert await _verify(db_session, payload) is not None
    del payload


async def test_a_revoked_connector_is_kept_in_the_list(db_session, legacy_owner_roots):
    """"This could read my record until March" is part of the same history the
    support grants and the API keys keep."""

    _payload, record = await _issue(db_session)
    await tokens.revoke(
        db_session, user_id=legacy_owner_roots.user_id, jti=record.id
    )
    await db_session.commit()

    listed = await tokens.list_for_user(
        db_session, user_id=legacy_owner_roots.user_id
    )
    assert [row.id for row in listed] == [record.id]
    assert listed[0].revoked_at is not None


async def test_a_token_whose_row_is_gone_is_not_a_credential(
    db_session, legacy_owner_roots
):
    """A signature this server made for a row that no longer exists.

    A restore from before the token was issued, or a row somebody deleted.
    Neither is an authorization, and the signature alone would happily say
    otherwise.
    """

    payload, record = await _issue(db_session)
    await db_session.delete(await db_session.get(McpAccessToken, record.id))
    await db_session.commit()
    assert await _verify(db_session, payload) is None


# ── The account behind it ────────────────────────────────────────────────────


async def test_suspending_the_account_stops_its_connectors(
    db_session, legacy_owner_roots
):
    """A token is valid for a year; a suspension has to bite the same afternoon."""

    payload, _record = await _issue(db_session)
    owner = await db_session.get(User, legacy_owner_roots.user_id)
    owner.status = UserStatus.SUSPENDED.value
    await db_session.commit()

    assert await _verify(db_session, payload) is None


async def test_a_lapsed_token_stops_working_without_anybody_acting(
    db_session, legacy_owner_roots
):
    """The clock, not a job that has to have run."""

    payload, record = await _issue(db_session)
    lapsed = record.issued_at - timedelta(days=400)
    record.issued_at = lapsed
    record.expires_at = lapsed + timedelta(days=365)
    await db_session.commit()

    assert await _verify(db_session, payload) is None


# ── Tokens minted before any of this ─────────────────────────────────────────


async def test_an_old_token_keeps_working_and_becomes_revocable(
    db_session, legacy_owner_roots
):
    """Adoption, which is what makes this upgrade safe to deploy.

    Breaking every live connector would be its own defect, and leaving old
    tokens outside the table would leave a credential nobody can withdraw —
    exactly what this table exists to stop existing. So the first use of an old
    token records a row for it, and from that moment it behaves like any other.
    """

    from datetime import datetime, timezone

    signed_at = datetime.now(timezone.utc) - timedelta(days=30)
    old_payload = {
        "type": tokens.TOKEN_TYPE,
        "username": "tester",
        "client_id": CLIENT,
    }

    verified = await _verify(
        db_session, old_payload, token="an-old-signed-value", signed_at=signed_at
    )
    await db_session.commit()
    assert verified is not None
    assert verified.username == "tester"

    listed = await tokens.list_for_user(
        db_session, user_id=legacy_owner_roots.user_id
    )
    assert len(listed) == 1
    adopted = listed[0]
    assert adopted.adopted is True, "an adopted token is not marked as one"
    # Dated from the signature, so the list is truthful about when the connector
    # was actually authorized rather than about when it was first noticed.
    assert abs((adopted.issued_at.replace(tzinfo=timezone.utc) - signed_at).days) <= 1

    await tokens.revoke(
        db_session, user_id=legacy_owner_roots.user_id, jti=adopted.id
    )
    await db_session.commit()
    assert (
        await _verify(
            db_session, old_payload, token="an-old-signed-value", signed_at=signed_at
        )
        is None
    ), "an adopted token could not be taken back"


async def test_the_same_old_token_adopts_one_row_not_one_per_request(
    db_session, legacy_owner_roots
):
    """Otherwise a live connector would fill the table one request at a time."""

    old_payload = {"type": tokens.TOKEN_TYPE, "username": "tester", "client_id": CLIENT}
    for _ in range(3):
        await _verify(db_session, old_payload, token="the-same-old-value")
        await db_session.commit()

    listed = await tokens.list_for_user(
        db_session, user_id=legacy_owner_roots.user_id
    )
    assert len(listed) == 1


async def test_the_adopted_row_is_not_a_copy_of_the_secret(
    db_session, legacy_owner_roots
):
    """Its id is derived from the token by hash, not from the token.

    An operator reading this table can revoke what they find and cannot use it,
    which is the same rule the invitations and the API keys follow.
    """

    secret = "a-very-secret-old-token-value"
    old_payload = {"type": tokens.TOKEN_TYPE, "username": "tester", "client_id": CLIENT}
    await _verify(db_session, old_payload, token=secret)
    await db_session.commit()

    listed = await tokens.list_for_user(
        db_session, user_id=legacy_owner_roots.user_id
    )
    assert secret not in repr(listed[0].__dict__)
