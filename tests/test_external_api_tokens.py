"""A credential that names one record, and an endpoint that believes it.

The external API was authorized by one installation-wide string, and resolved
its subject from whoever ``.env`` named as the owner. That is a per-subject
token by accident on a single-user machine and a credential with no boundary the
moment a second person exists — the last ``.env``-owner read left on a data
path. These are the rules that replace it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from vitals.enums import ExternalApiTokenStatus, UserStatus
from vitals.models.identity import ExternalApiToken, HealthSubject, User
from vitals.services.external_api import tokens

ENVIRONMENT_TOKEN = "environment-token"


@pytest.fixture
def _environment_token(monkeypatch):
    monkeypatch.setenv("VITALS_EXTERNAL_API_TOKEN", ENVIRONMENT_TOKEN)


@pytest.fixture
def _no_environment_token(monkeypatch):
    monkeypatch.delenv("VITALS_EXTERNAL_API_TOKEN", raising=False)


async def _user(session, slug: str) -> User:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    return user


async def _second_person(session, slug: str = "second-person"):
    owner = await _user(session, slug)
    subject = HealthSubject(
        owner_user_id=owner.id, display_name="Second", timezone="UTC"
    )
    session.add(subject)
    await session.flush()
    return owner, subject


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


# ── The secret exists once ───────────────────────────────────────────────────


async def test_the_secret_is_returned_and_never_stored(db_session, legacy_owner_roots):
    """No screen and no query can show it again, deliberately.

    A credential an operator can read back out of the database is a credential
    an operator can use.
    """

    issued = await tokens.issue(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        subject_id=legacy_owner_roots.subject_id,
        label="Kitchen dashboard",
    )
    await db_session.commit()

    assert issued.secret
    stored = await db_session.get(ExternalApiToken, issued.record.id)
    assert issued.secret not in repr(stored.__dict__), "the secret was stored"
    assert stored.token_hash != issued.secret
    assert len(stored.token_hash) == 64


async def test_a_credential_needs_a_label(db_session, legacy_owner_roots):
    """A list of indistinguishable secrets is one nobody revokes from."""

    with pytest.raises(tokens.ExternalApiTokenError):
        await tokens.issue(
            db_session,
            owner_user_id=legacy_owner_roots.user_id,
            subject_id=legacy_owner_roots.subject_id,
            label="   ",
        )


async def test_a_credential_longer_than_a_year_is_refused(
    db_session, legacy_owner_roots
):
    with pytest.raises(tokens.ExternalApiTokenError):
        await tokens.issue(
            db_session,
            owner_user_id=legacy_owner_roots.user_id,
            subject_id=legacy_owner_roots.subject_id,
            label="Forever",
            lifetime=timedelta(days=400),
        )


# ── Only the record's owner may mint one ─────────────────────────────────────


async def test_only_the_owner_may_issue_a_credential_for_a_record(
    db_session, legacy_owner_roots
):
    """Not a professional in care, not a platform administrator.

    Handing out a long-lived key to somebody's health data is not something a
    support grant should be able to do quietly, and a doctor's consent is to
    read within the app rather than to issue credentials against it.
    """

    stranger = await _user(db_session, "not-the-owner")
    await db_session.commit()

    with pytest.raises(tokens.NotTheSubjectOwner):
        await tokens.issue(
            db_session,
            owner_user_id=stranger.id,
            subject_id=legacy_owner_roots.subject_id,
            label="Not mine to issue",
        )


async def test_a_stranger_revoking_is_told_it_does_not_exist(
    db_session, legacy_owner_roots
):
    """Rather than that they may not touch it: probing ids learns nothing."""

    issued = await tokens.issue(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        subject_id=legacy_owner_roots.subject_id,
        label="Kitchen dashboard",
    )
    stranger = await _user(db_session, "not-the-owner-either")
    await db_session.commit()

    with pytest.raises(tokens.TokenNotFound):
        await tokens.revoke(
            db_session, owner_user_id=stranger.id, token_id=issued.record.id
        )


async def test_a_record_may_not_hold_unbounded_credentials(
    db_session, legacy_owner_roots
):
    for index in range(tokens.MAX_LIVE_TOKENS):
        await tokens.issue(
            db_session,
            owner_user_id=legacy_owner_roots.user_id,
            subject_id=legacy_owner_roots.subject_id,
            label=f"Dashboard {index}",
        )
    await db_session.commit()

    with pytest.raises(tokens.TooManyTokens):
        await tokens.issue(
            db_session,
            owner_user_id=legacy_owner_roots.user_id,
            subject_id=legacy_owner_roots.subject_id,
            label="One too many",
        )


# ── Authentication is a fresh question every time ────────────────────────────


async def test_a_revoked_credential_stops_working_immediately(
    db_session, legacy_owner_roots
):
    issued = await tokens.issue(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        subject_id=legacy_owner_roots.subject_id,
        label="Kitchen dashboard",
    )
    await db_session.commit()
    assert await tokens.authenticate(db_session, presented=issued.secret) is not None

    await tokens.revoke(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        token_id=issued.record.id,
    )
    await db_session.commit()
    assert await tokens.authenticate(db_session, presented=issued.secret) is None


async def test_a_lapsed_credential_stops_working_without_anybody_acting(
    db_session, legacy_owner_roots
):
    """The clock is the enforcement, not a job that has to have run."""

    issued = await tokens.issue(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        subject_id=legacy_owner_roots.subject_id,
        label="Kitchen dashboard",
    )
    await db_session.commit()

    # Aged by moving both ends back: the schema requires the expiry to stay
    # strictly after creation.
    lapsed = issued.record.created_at - timedelta(days=200)
    issued.record.created_at = lapsed
    issued.record.expires_at = lapsed + timedelta(days=90)
    await db_session.commit()

    assert await tokens.authenticate(db_session, presented=issued.secret) is None


async def test_suspending_the_owner_stops_their_credentials(
    db_session, legacy_owner_roots
):
    """A credential outliving its owner's access is the bug this replaces."""

    issued = await tokens.issue(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        subject_id=legacy_owner_roots.subject_id,
        label="Kitchen dashboard",
    )
    await db_session.commit()

    owner = await db_session.get(User, legacy_owner_roots.user_id)
    owner.status = UserStatus.SUSPENDED.value
    await db_session.commit()

    assert await tokens.authenticate(db_session, presented=issued.secret) is None


async def test_a_revoked_credential_is_kept_rather_than_deleted(
    db_session, legacy_owner_roots
):
    """"This dashboard could read my weight until March" is part of the record."""

    issued = await tokens.issue(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        subject_id=legacy_owner_roots.subject_id,
        label="Kitchen dashboard",
    )
    await tokens.revoke(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        token_id=issued.record.id,
    )
    await db_session.commit()

    listed = await tokens.list_for_subject(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert [row.status for row in listed] == [ExternalApiTokenStatus.REVOKED.value]
    assert listed[0].revoked_at is not None
    assert listed[0].label == "Kitchen dashboard"


# ── The endpoint believes the credential, not the environment ────────────────


async def test_the_endpoint_answers_for_the_record_its_token_names(
    client, db_session, legacy_owner_roots, _no_environment_token
):
    """The point of the whole change, in one assertion.

    Two records exist. A credential issued for the second opens the second, and
    the ``.env`` owner's data is not what comes back.
    """

    _owner, second = await _second_person(db_session)
    issued = await tokens.issue(
        db_session,
        owner_user_id=second.owner_user_id,
        subject_id=second.id,
        label="Second person's dashboard",
    )
    await db_session.commit()

    response = await client.get("/external/summary", headers=_bearer(issued.secret))
    assert response.status_code == 200
    # Scoped reads, so the second person's empty record is what an empty
    # payload means — the owner's seeded record would not be empty.
    assert response.json()["nutrition_today"]["meal_count"] == 0


async def test_an_unknown_bearer_is_refused_when_credentials_exist(
    client, db_session, legacy_owner_roots, _no_environment_token
):
    await tokens.issue(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        subject_id=legacy_owner_roots.subject_id,
        label="Kitchen dashboard",
    )
    await db_session.commit()

    response = await client.get("/external/summary", headers=_bearer("not-a-token"))
    assert response.status_code == 401


async def test_the_endpoint_is_off_when_nothing_is_configured_or_issued(
    client, db_session, legacy_owner_roots, _no_environment_token
):
    """503, so the caller can tell "switched off here" from "my token is wrong"."""

    response = await client.get("/external/summary", headers=_bearer("anything"))
    assert response.status_code == 503


async def test_the_environment_token_still_works_for_one_record(
    client, db_session, legacy_owner_roots, _environment_token
):
    """Compatibility, and only where the answer is unambiguous."""

    response = await client.get(
        "/external/summary", headers=_bearer(ENVIRONMENT_TOKEN)
    )
    assert response.status_code == 200


async def test_the_environment_token_is_refused_once_it_would_have_to_guess(
    client, db_session, legacy_owner_roots, _environment_token
):
    """It names no record, so a second record is a question it cannot answer.

    Refused rather than resolved to the ``.env`` owner, which is what it used to
    do — silently, with the holder none the wiser about whose data they had.
    """

    await _second_person(db_session, "second-for-env")
    await db_session.commit()

    response = await client.get(
        "/external/summary", headers=_bearer(ENVIRONMENT_TOKEN)
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "external_api_token_cannot_name_a_record"


async def test_an_issued_credential_still_works_on_a_shared_installation(
    client, db_session, legacy_owner_roots, _environment_token
):
    """Which is the way out of the refusal above, and the reason it is safe."""

    await _second_person(db_session, "second-alongside")
    issued = await tokens.issue(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        subject_id=legacy_owner_roots.subject_id,
        label="Kitchen dashboard",
    )
    await db_session.commit()

    response = await client.get("/external/summary", headers=_bearer(issued.secret))
    assert response.status_code == 200


# ── The screen that issues one ───────────────────────────────────────────────


def _sign_in(client, username: str):
    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client.cookies.set(SESSION_COOKIE, create_session(username))


async def test_the_settings_page_issues_a_key_and_shows_it_once(
    client, db_session, legacy_owner_roots, _no_environment_token
):
    """Rendered from the POST, never redirected with the secret in the URL.

    A URL ends up in browser history, in the access log and in the next page's
    referrer. A bearer token is a capability, and none of those are places to
    leave one — the rule ``consents.issue_invitation`` already follows.
    """

    _sign_in(client, "tester")
    response = await client.post(
        "/settings/external-api",
        data={"label": "Kitchen dashboard", "days": "30"},
        follow_redirects=False,
    )
    assert response.status_code == 200, "the secret was redirected rather than rendered"
    assert "Kitchen dashboard" in response.text

    listed = await tokens.list_for_subject(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert len(listed) == 1
    # The rendered page carries the one copy, and it is not the stored hash.
    assert listed[0].token_hash not in response.text


async def test_the_issued_key_actually_opens_the_endpoint(
    client, db_session, legacy_owner_roots, _no_environment_token
):
    """The screen and the endpoint agree, which is the only thing that matters.

    A credential a settings page mints and an API refuses is the shape of defect
    this branch keeps finding: each half correct, the product broken.
    """

    _sign_in(client, "tester")
    page = await client.post(
        "/settings/external-api",
        data={"label": "Kitchen dashboard", "days": "30"},
        follow_redirects=False,
    )
    # The secret is the one long urlsafe string on the page that is not a hash.
    import re

    candidates = re.findall(r"[A-Za-z0-9_-]{40,}", page.text)
    assert candidates, "no credential was rendered"

    opened = None
    for candidate in candidates:
        response = await client.get(
            "/external/summary", headers=_bearer(candidate)
        )
        if response.status_code == 200:
            opened = candidate
            break
    assert opened, "nothing on the page opened the endpoint it was issued for"


async def test_the_screen_can_stop_a_key(
    client, db_session, legacy_owner_roots, _no_environment_token
):
    issued = await tokens.issue(
        db_session,
        owner_user_id=legacy_owner_roots.user_id,
        subject_id=legacy_owner_roots.subject_id,
        label="Kitchen dashboard",
    )
    await db_session.commit()

    _sign_in(client, "tester")
    stopped = await client.post(
        f"/settings/external-api/{issued.record.id}/revoke", follow_redirects=False
    )
    assert stopped.status_code == 303

    refused = await client.get("/external/summary", headers=_bearer(issued.secret))
    assert refused.status_code in (401, 503)
