"""Whose record a connector reaches is decided by whose token asked.

Until now it was not. The OAuth access token has carried the authorizing
account's username since the flow was written, the middleware read only the
signature and the client id, and every tool resolved the ``.env`` owner — so the
answer to "whose record is this" did not depend on the credential at all. On a
single-user machine those are the same person. On a shared one, any signed-in
account could walk the ordinary consent screen, obtain a token, and read and
write somebody else's medical record.

PR-10 names this test: *token A cannot select subject B, even with a known row
ID or direct tool call.*
"""

from __future__ import annotations

from datetime import date

import pytest

from vitals.enums import Domain, Source, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.weight import WeightLog

mcp_router = pytest.importorskip("web.routers.mcp")

pytestmark = pytest.mark.usefixtures("owned_by_legacy_subject")

OWNER_WEIGHT = 72.5
OTHER_WEIGHT = 999.9


@pytest.fixture(autouse=True)
def _use_test_factory(session_factory, db_session, monkeypatch):
    """A factory whose sessions arrive unbound, as production's do.

    The suite hands out one shared session and row security binds it to the
    first subject that uses it, then refuses to move — correctly, because one
    transaction serves one person. Production gives every connector request its
    own session, so two accounts in one test is two sessions there and one here.
    Clearing the binding on entry is what a new session does for free.
    """

    from vitals.services import rls_session

    class _Unbound:
        async def __aenter__(self):
            db_session.info.pop(rls_session._SUBJECT_KEY, None)
            return db_session

        async def __aexit__(self, *_):
            return None

    class _Factory:
        def __call__(self):
            return _Unbound()

    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: _Factory())
    del session_factory


@pytest.fixture(autouse=True)
def _no_actor_left_behind():
    """One request's identity must not outlive it into the next.

    The middleware resets it in a ``finally``; these tests set it by hand, and
    a leaked value would make the next test pass for the wrong reason.
    """

    yield
    mcp_router._MCP_ACTOR.set(None)


def _acting_as(username: str | None):
    return mcp_router._MCP_ACTOR.set(username)


async def _second_person(session, slug: str = "mcp-other") -> HealthSubject:
    owner = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(
        owner_user_id=owner.id, display_name="The Other Person", timezone="UTC"
    )
    session.add(subject)
    await session.flush()
    session.add(
        WeightLog(
            subject_id=subject.id,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            date=date.today(),
            weight_kg=OTHER_WEIGHT,
        )
    )
    await session.flush()
    return subject


async def _both_people(session, legacy_owner_roots) -> HealthSubject:
    session.add(
        WeightLog(
            subject_id=legacy_owner_roots.subject_id,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            date=date.today(),
            weight_kg=OWNER_WEIGHT,
        )
    )
    other = await _second_person(session)
    await session.commit()
    return other


def _weights(answer) -> list[float]:
    return [row["weight_kg"] for row in answer["weights"]]


# ── The token decides ────────────────────────────────────────────────────────


async def test_a_token_reaches_the_record_of_the_account_that_authorized_it(
    db_session, legacy_owner_roots
):
    """The whole point, in one assertion, and it did not hold before.

    Two people, two tokens. Each reads their own weight and neither sees the
    other's — where previously both would have read the ``.env`` owner's.
    """

    await _both_people(db_session, legacy_owner_roots)

    restore = _acting_as("tester")
    try:
        assert _weights(await mcp_router.get_weight_logs(limit=10)) == [OWNER_WEIGHT]
    finally:
        mcp_router._MCP_ACTOR.reset(restore)

    restore = _acting_as("mcp-other")
    try:
        assert _weights(await mcp_router.get_weight_logs(limit=10)) == [OTHER_WEIGHT]
    finally:
        mcp_router._MCP_ACTOR.reset(restore)


async def test_a_write_lands_in_the_record_of_the_token_that_made_it(
    db_session, legacy_owner_roots
):
    """Reads were the visible half; writes are the half that does damage.

    A connector logging a meal into somebody else's record is worse than one
    reading it, and both came from the same resolved subject.
    """

    await _both_people(db_session, legacy_owner_roots)

    restore = _acting_as("mcp-other")
    try:
        await mcp_router.log_weight(weight_kg=101.1)
        # Read back through the same tools rather than around them. What matters
        # is which record a connector can see the write in, and asking the
        # product that question is stronger than asking the table.
        mine = _weights(await mcp_router.get_weight_logs(limit=10))
    finally:
        mcp_router._MCP_ACTOR.reset(restore)
    assert 101.1 in mine, "the write did not reach the acting account's record"

    restore = _acting_as("tester")
    try:
        theirs = _weights(await mcp_router.get_weight_logs(limit=10))
    finally:
        mcp_router._MCP_ACTOR.reset(restore)
    assert 101.1 not in theirs, "a connector wrote into somebody else's record"


async def test_a_known_row_id_does_not_cross_the_boundary(
    db_session, legacy_owner_roots
):
    """PR-10 asks for this by name: not even with the id in hand.

    The tools take integer ids, which are guessable by counting. Authorization
    has to come from the token rather than from whether the id was findable.
    """

    from sqlalchemy import select

    await _both_people(db_session, legacy_owner_roots)
    theirs = await db_session.scalar(
        select(WeightLog.id).where(
            WeightLog.subject_id == legacy_owner_roots.subject_id
        )
    )
    assert theirs is not None

    restore = _acting_as("mcp-other")
    try:
        answer = await mcp_router.delete_record(domain="weight", record_id=theirs)
    finally:
        mcp_router._MCP_ACTOR.reset(restore)

    assert answer.get("deleted") is not True, (
        "one account deleted a row out of another's record by id"
    )
    still_there = await db_session.scalar(
        select(WeightLog.id).where(WeightLog.id == theirs)
    )
    assert still_there is not None, "the row was deleted across the boundary"


# ── A credential that names nobody ───────────────────────────────────────────


async def test_a_token_without_an_identity_is_refused_once_it_would_have_to_guess(
    db_session, legacy_owner_roots
):
    """Old tokens name nobody, and there is no safe way to answer them here.

    Refused rather than resolved to the ``.env`` owner, which is what used to
    happen — silently, with the connector's holder none the wiser about whose
    record they were reading.
    """

    await _both_people(db_session, legacy_owner_roots)

    restore = _acting_as(mcp_router.ANONYMOUS_TOKEN)
    try:
        with pytest.raises(mcp_router.McpActorUnresolved):
            await mcp_router.get_weight_logs(limit=10)
    finally:
        mcp_router._MCP_ACTOR.reset(restore)


async def test_a_token_without_an_identity_still_works_for_one_record(
    db_session, legacy_owner_roots
):
    """Compatibility, and only where the answer is unambiguous.

    An installation with one person has one record for a connector to mean, so
    an old token keeps working there rather than breaking on upgrade.
    """

    db_session.add(
        WeightLog(
            subject_id=legacy_owner_roots.subject_id,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            date=date.today(),
            weight_kg=OWNER_WEIGHT,
        )
    )
    await db_session.commit()

    restore = _acting_as(mcp_router.ANONYMOUS_TOKEN)
    try:
        assert _weights(await mcp_router.get_weight_logs(limit=10)) == [OWNER_WEIGHT]
    finally:
        mcp_router._MCP_ACTOR.reset(restore)


async def test_a_direct_call_is_not_a_token_and_keeps_working(
    db_session, legacy_owner_roots
):
    """No request at all — a scheduled job, an internal call, a test.

    Left exactly as it was, and deliberately not narrowed: the environment names
    an account and the resolver returns *that account's* record, which is a fact
    about one person rather than an assumption about how many exist.
    """

    await _both_people(db_session, legacy_owner_roots)

    assert mcp_router._MCP_ACTOR.get() is None
    assert _weights(await mcp_router.get_weight_logs(limit=10)) == [OWNER_WEIGHT]


# ── The account has to still exist ───────────────────────────────────────────


async def test_a_suspended_account_stops_authorizing_its_connector(
    db_session, legacy_owner_roots
):
    """A token is valid for a year; a suspension has to bite the same afternoon.

    ``resolve_legacy_ownership_context`` matches the name and never reads
    ``status``, so without an explicit check a suspended person's connector
    would keep reading their record until the signature expired.
    """

    other = await _both_people(db_session, legacy_owner_roots)
    owner = await db_session.get(User, other.owner_user_id)
    owner.status = UserStatus.SUSPENDED.value
    await db_session.commit()

    restore = _acting_as("mcp-other")
    try:
        with pytest.raises(mcp_router.McpActorUnresolved):
            await mcp_router.get_weight_logs(limit=10)
    finally:
        mcp_router._MCP_ACTOR.reset(restore)


async def test_a_token_naming_nobody_at_all_is_refused(
    db_session, legacy_owner_roots
):
    """A signature is not an identity. The named account has to exist."""

    await _both_people(db_session, legacy_owner_roots)

    restore = _acting_as("somebody-who-was-deleted")
    try:
        with pytest.raises(mcp_router.McpActorUnresolved):
            await mcp_router.get_weight_logs(limit=10)
    finally:
        mcp_router._MCP_ACTOR.reset(restore)


# ── The token verifier is what puts it there ─────────────────────────────────


def _token_for(username: str | None, *, client_id: str = "vitals-claude-connector") -> str:
    from web.auth import _get_mcp_serializer

    payload = {"client_id": client_id, "type": "mcp_access_token"}
    if username is not None:
        payload["username"] = username
    return _get_mcp_serializer().dumps(payload)


async def _verify(token: str):
    """What the SDK is told about one credential.

    The tests above set the actor by hand, which proves the tools read the seam
    and proves nothing about the half that fills it. This is that half: a real
    signed token through the real verifier, and the ``AccessToken`` the SDK
    would hand a tool.
    """

    return await mcp_router._ConnectorTokenVerifier().verify_token(token)


async def test_the_verifier_hands_the_sdk_the_tokens_identity():
    """A signed token in, a subject out — which is what ``get_access_token``
    then gives every tool."""

    granted = await _verify(_token_for("patient01"))
    assert granted is not None
    assert granted.subject == "patient01"
    assert granted.claims["username"] == "patient01"


async def test_a_token_that_names_nobody_verifies_with_no_subject():
    """The distinction the whole fix rests on.

    An old token is a valid credential that identifies no one. It must verify —
    breaking every existing connector on upgrade would be its own defect — and
    it must arrive with ``subject`` empty, so ``_current_actor`` can report it
    as anonymous rather than as "no request happened".
    """

    granted = await _verify(_token_for(None))
    assert granted is not None
    assert granted.subject is None
    assert granted.claims == {}


async def test_an_unsigned_or_tampered_token_verifies_as_nothing():
    for token in ("", "not-a-token", _token_for("patient01")[:-4]):
        assert await _verify(token) is None, f"{token!r} verified"


async def test_a_token_for_another_client_is_refused():
    """The client id is part of what a token is for."""

    assert await _verify(_token_for("patient01", client_id="somebody-else")) is None


async def test_a_token_of_the_wrong_type_is_refused():
    """A session cookie replayed as a connector token, and the reverse.

    They are signed with different salts, so this cannot happen by accident —
    the type check is what makes it not happen on purpose either.
    """

    from web.auth import _get_mcp_serializer

    forged = _get_mcp_serializer().dumps(
        {"username": "patient01", "client_id": "vitals-claude-connector",
         "type": "web_session"}
    )
    assert await _verify(forged) is None


async def test_the_verified_subject_is_what_the_tools_resolve_from():
    """The two halves joined, without a running server.

    ``_current_actor`` reads ``get_access_token()``; the verifier is what fills
    it. Asserting they agree on the same token is what keeps the seam from
    being two ideas about who is asking.
    """

    granted = await _verify(_token_for("patient01"))
    assert granted is not None

    import mcp.server.auth.middleware.auth_context as auth_context

    # The SDK stores an ``AuthenticatedUser`` and reads ``.access_token`` off
    # it, which is what its own middleware puts there after verification.
    restore = auth_context.auth_context_var.set(
        auth_context.AuthenticatedUser(granted)
    )
    try:
        assert mcp_router._current_actor() == "patient01"
    finally:
        auth_context.auth_context_var.reset(restore)
