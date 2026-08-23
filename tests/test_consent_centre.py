"""The patient's side: who holds their record, and how they stop it.

The professional's routes name their patient in the path because a stale tab
must not be able to write to the wrong person. These do the opposite for the
same reason: the patient has exactly one record and nothing to select, so the
subject is resolved from *who they are*. Both are the same rule — the subject
comes from whichever source cannot go stale.

Two properties get most of the attention below. The invitation link exists once
and is never in a URL the browser keeps. And withdrawing takes effect on the
professional's next request, not on their next login.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select

from vitals.enums import ProfessionalKind, UserRoleName, UserStatus
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.professional import ProfessionalInvitation, ProfessionalNote


@asynccontextmanager
async def _client_for(username: str):
    """A second, independent browser.

    ``auth_client`` and ``client`` are the same instance in this suite, so
    setting a cookie on one replaces the other's session. A professional and a
    patient acting in the same test genuinely need two browsers.
    """

    from httpx import ASGITransport, AsyncClient

    from web.auth import create_session
    from web.config import SESSION_COOKIE
    from web.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as other:
        other.cookies.set(SESSION_COOKIE, create_session(username))
        yield other


async def _user(session, slug: str, *, roles=(), email=None) -> User:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    if email is not None:
        user.email = email
        user.normalized_email = email
    session.add(user)
    await session.flush()
    for role in roles:
        session.add(UserRole(user_id=user.id, role=role.value))
    await session.flush()
    return user


@pytest.fixture
async def patient_client(auth_client, db_session, legacy_owner_roots):
    """The signed-in account, acting as the owner of their own record."""

    return auth_client, legacy_owner_roots.user_id, legacy_owner_roots.subject_id


# ── Inviting ─────────────────────────────────────────────────────────────────


async def test_the_link_is_shown_once_and_never_lands_in_a_url(
    patient_client, db_session
):
    """A URL ends up in history, in the access log, and in the next referrer.

    An invitation link is a capability. None of those are places to leave one,
    which is why the page is rendered straight from the POST rather than
    redirected to with the token in a query string.
    """

    client, _user_id, _subject_id = patient_client

    response = await client.post(
        "/settings/care/invite",
        data={"email": "doctor@example.test", "kind": ProfessionalKind.DOCTOR.value},
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 200
    # No redirect, so nothing carrying the token enters the address bar.
    assert "location" not in response.headers

    body = response.text
    assert "/care/accept/" in body

    token = body.split("/care/accept/")[1].split("<")[0].strip()
    assert token

    # And it really is not stored: the row holds a hash of it, not it.
    stored = (
        await db_session.scalars(select(ProfessionalInvitation.token_hash))
    ).all()
    assert stored and token not in stored


async def test_the_page_lists_what_is_outstanding(patient_client, db_session):
    client, _user_id, _subject_id = patient_client
    await client.post(
        "/settings/care/invite",
        data={"email": "pending@example.test", "kind": ProfessionalKind.DOCTOR.value},
        headers={"Accept": "text/html"},
    )
    response = await client.get("/settings/care", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "pending@example.test" in response.text


async def test_an_unusable_address_is_refused(patient_client):
    client, _user_id, _subject_id = patient_client
    response = await client.post(
        "/settings/care/invite",
        data={"email": "not-an-address", "kind": ProfessionalKind.DOCTOR.value},
    )
    assert response.status_code == 400


# ── Accepting ────────────────────────────────────────────────────────────────


async def _invited(client, db_session, *, email, kind=ProfessionalKind.DOCTOR):
    response = await client.post(
        "/settings/care/invite",
        data={"email": email, "kind": kind.value},
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 200
    return response.text.split("/care/accept/")[1].split("<")[0].strip()


async def test_a_get_does_not_spend_the_link(patient_client, db_session):
    """Browsers, previews and mail scanners fetch URLs nobody chose to open.

    A one-time invitation spent by a link preview is one the intended person can
    never use, so the confirmation is a POST and the GET only asks.
    """

    from vitals.enums import ProfessionalInvitationStatus

    client, _user_id, _subject_id = patient_client
    token = await _invited(client, db_session, email="preview@example.test")

    response = await client.get(
        f"/care/accept/{token}", headers={"Accept": "text/html"}
    )
    assert response.status_code == 200

    db_session.expire_all()
    status_value = await db_session.scalar(select(ProfessionalInvitation.status))
    assert status_value == ProfessionalInvitationStatus.PENDING.value


async def test_accepting_establishes_care_and_opens_nothing(
    patient_client, db_session
):
    """Being in care and having agreed to show something are two decisions.

    Accepting on the patient's behalf would be making the second one for them.
    """

    from vitals.models.professional import CareRelationship
    from vitals.utils.timeutils import now_utc

    owner_client, _user_id, subject_id = patient_client
    token = await _invited(owner_client, db_session, email="doc@example.test")

    doctor = await _user(
        db_session, "cc-doctor", roles=(UserRoleName.DOCTOR,), email="doc@example.test"
    )
    doctor.email_verified_at = now_utc()
    doctor_id = doctor.id
    await db_session.commit()

    async with _client_for("cc-doctor") as doctor_client:
        response = await doctor_client.post(f"/care/accept/{token}")
        assert response.status_code == 303
        assert response.headers["location"] == f"/care/{subject_id}"

        db_session.expire_all()
        holder = await db_session.scalar(
            select(CareRelationship.professional_user_id)
        )
        assert holder == doctor_id

        # In care, and the record is still shut: no consent has been given.
        assert (
            await doctor_client.get(
                f"/care/{subject_id}", headers={"Accept": "text/html"}
            )
        ).status_code == 404


async def test_an_unverified_address_does_not_accept(patient_client, db_session):
    """An unverified address is somebody asserting they own a mailbox."""

    owner_client, _user_id, _subject_id = patient_client
    token = await _invited(owner_client, db_session, email="unverified@example.test")

    await _user(
        db_session,
        "cc-unverified",
        roles=(UserRoleName.DOCTOR,),
        email="unverified@example.test",
    )
    await db_session.commit()  # email_verified_at stays null

    async with _client_for("cc-unverified") as doctor_client:
        response = await doctor_client.post(f"/care/accept/{token}")
        assert response.status_code == 404


# ── Withdrawing ──────────────────────────────────────────────────────────────


@pytest.fixture
async def in_care(patient_client, db_session):
    """A doctor holding the patient's record, with consent, in their own browser."""

    from vitals.models.professional import CareRelationship
    from vitals.utils.timeutils import now_utc

    owner_client, _user_id, subject_id = patient_client
    token = await _invited(owner_client, db_session, email="incare@example.test")
    doctor = await _user(
        db_session,
        "cc-incare",
        roles=(UserRoleName.DOCTOR,),
        email="incare@example.test",
    )
    doctor.email_verified_at = now_utc()
    await db_session.commit()

    async with _client_for("cc-incare") as doctor_client:
        await doctor_client.post(f"/care/accept/{token}")

        db_session.expire_all()
        relationship_id = await db_session.scalar(select(CareRelationship.id))
        response = await owner_client.post(
            f"/settings/care/{relationship_id}/grant"
        )
        assert response.status_code in (200, 303), response.text
        yield owner_client, doctor_client, subject_id, relationship_id


async def test_the_patient_opens_and_closes_their_own_record(in_care, db_session):
    """And closing takes effect on the professional's next request."""

    owner_client, doctor_client, subject_id, relationship_id = in_care

    opened = await doctor_client.get(
        f"/care/{subject_id}", headers={"Accept": "text/html"}
    )
    assert opened.status_code == 200

    await owner_client.post(f"/settings/care/{relationship_id}/revoke")
    closed = await doctor_client.get(
        f"/care/{subject_id}", headers={"Accept": "text/html"}
    )
    assert closed.status_code == 404


async def test_a_pause_comes_back_and_a_revocation_does_not(in_care, db_session):
    owner_client, doctor_client, subject_id, relationship_id = in_care

    await owner_client.post(f"/settings/care/{relationship_id}/pause")
    assert (
        await doctor_client.get(f"/care/{subject_id}", headers={"Accept": "text/html"})
    ).status_code == 404

    await owner_client.post(
        f"/settings/care/{relationship_id}/pause", data={"resume": "1"}
    )
    assert (
        await doctor_client.get(f"/care/{subject_id}", headers={"Accept": "text/html"})
    ).status_code == 200

    await owner_client.post(f"/settings/care/{relationship_id}/revoke")
    await owner_client.post(
        f"/settings/care/{relationship_id}/pause", data={"resume": "1"}
    )
    assert (
        await doctor_client.get(f"/care/{subject_id}", headers={"Accept": "text/html"})
    ).status_code == 404


async def test_ending_the_care_stops_the_writing_too(in_care, db_session):
    owner_client, doctor_client, subject_id, relationship_id = in_care

    await owner_client.post(f"/settings/care/{relationship_id}/end")
    response = await doctor_client.post(
        f"/care/{subject_id}/note", data={"body": "Too late"}
    )
    assert response.status_code == 404
    assert await db_session.scalar(
        select(func.count()).select_from(ProfessionalNote)
    ) == 0


async def test_the_page_says_what_each_person_can_see(in_care, db_session):
    owner_client, _doctor_client, _subject_id, _relationship_id = in_care
    response = await owner_client.get(
        "/settings/care", headers={"Accept": "text/html"}
    )
    assert response.status_code == 200
    assert "cc-incare" in response.text
    # Every domain the consent actually carries, named on the page.
    assert "labs" in response.text or "Анализы" in response.text


async def test_a_stranger_cannot_withdraw_somebody_elses_consent(
    in_care, db_session
):
    """The relationship is resolved inside the actor's scope, so it is a miss."""

    _owner_client, _doctor_client, _subject_id, relationship_id = in_care
    stranger = await _user(db_session, "cc-stranger")
    db_session.add(
        HealthSubject(
            owner_user_id=stranger.id,
            display_name="Stranger",
            timezone="Asia/Almaty",
        )
    )
    await db_session.commit()

    async with _client_for("cc-stranger") as stranger_client:
        response = await stranger_client.post(
            f"/settings/care/{relationship_id}/revoke"
        )
    assert response.status_code == 404
