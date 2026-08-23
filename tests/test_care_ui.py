"""The professional's screens, and the one property they are shaped around.

**The selected patient travels in the URL, never in the session.**

A "currently selected patient" held server-side is the obvious design and it has
a failure that cannot be tested away. A professional opens patient A, leaves the
tab, selects patient B in another tab, comes back to the first and submits the
form still on screen. With the selection in a cookie that write lands on B —
silently, with A's data in it. Nothing about the request looks wrong, there is
no error to notice, and the record it corrupts belongs to somebody who was never
involved.

With the selection in the path the stale tab submits to the patient it was
rendered for. That is the first test in this file, and everything else is about
keeping it true.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from vitals.enums import ProfessionalKind, UserRoleName, UserStatus
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.professional import ProfessionalNote
from vitals.services import care_service, invitation_service


async def _user(session, slug: str, *, roles=()) -> User:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    for role in roles:
        session.add(UserRole(user_id=user.id, role=role.value))
    await session.flush()
    return user


async def _patient(session, slug: str) -> tuple[User, HealthSubject]:
    owner = await _user(session, slug)
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name=f"Patient {slug}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return owner, subject


async def _take_into_care(session, *, owner, subject, professional, consent=True):
    email = f"{professional.username}@example.test"
    issued = await invitation_service.invite(
        session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=ProfessionalKind.DOCTOR,
        email=email,
    )
    await invitation_service.accept(
        session,
        token=issued.token,
        accepting_user_id=professional.id,
        verified_email=email,
    )
    relationship = await care_service.establish_from_invitation(
        session, invitation=issued.invitation
    )
    if consent:
        await care_service.grant_consent(
            session, relationship_id=relationship.id, actor_user_id=owner.id
        )
    return relationship


@pytest.fixture
async def doctor_client(client, db_session, legacy_owner_roots):
    """A browser session for a professional who holds two patients.

    The legacy owner is the account the test client signs in as, so the
    professional here *is* that account — which is the shape a single-tenant
    install has while it grows a second person.
    """

    from web.auth import create_session, set_session_cookie
    from web.config import SESSION_COOKIE

    doctor_id = legacy_owner_roots.user_id
    doctor = await db_session.get(User, doctor_id)
    db_session.add(UserRole(user_id=doctor.id, role=UserRoleName.DOCTOR.value))
    await db_session.flush()

    owner_a, subject_a = await _patient(db_session, "care-ui-a")
    owner_b, subject_b = await _patient(db_session, "care-ui-b")
    await _take_into_care(
        db_session, owner=owner_a, subject=subject_a, professional=doctor
    )
    await _take_into_care(
        db_session, owner=owner_b, subject=subject_b, professional=doctor
    )
    await db_session.commit()

    client.cookies.set(SESSION_COOKIE, create_session(doctor.username))
    del set_session_cookie
    return client, doctor, (owner_a, subject_a), (owner_b, subject_b)


# ── The property everything else protects ────────────────────────────────────


async def test_a_stale_tab_writes_to_the_patient_it_was_looking_at(
    doctor_client, db_session
):
    """The tab was rendered for A; it posts to A, whatever happened since.

    This is the whole reason the subject is in the path. Had the selection been
    server-side, the second post would have landed on B — with A's words in it,
    silently, in a record whose owner was never involved.
    """

    client, _doctor, (_owner_a, subject_a), (_owner_b, subject_b) = doctor_client
    # Read the ids before anything expires the instances: after ``expire_all``
    # touching an attribute is a lazy refresh, and an async session has no
    # greenlet to refresh in.
    id_a, id_b = subject_a.id, subject_b.id

    # A tab open on A. Meanwhile the professional works on B in another tab.
    opened_b = await client.get(
        f"/care/{id_b}", headers={"Accept": "text/html"}
    )
    assert opened_b.status_code == 200
    await client.post(f"/care/{id_b}/note", data={"body": "About B"})

    # The stale tab submits. Its action still names A.
    response = await client.post(f"/care/{id_a}/note", data={"body": "About A"})
    assert response.status_code in (200, 303)

    db_session.expire_all()
    rows = (
        await db_session.execute(
            select(ProfessionalNote.subject_id, ProfessionalNote.body).order_by(
                ProfessionalNote.body
            )
        )
    ).all()
    assert {(row.subject_id, row.body) for row in rows} == {
        (id_a, "About A"),
        (id_b, "About B"),
    }


async def test_the_form_action_names_the_patient(doctor_client):
    """Which is what makes the test above possible rather than lucky."""

    client, _doctor, (_owner_a, subject_a), _b = doctor_client
    response = await client.get(
        f"/care/{subject_a.id}", headers={"Accept": "text/html"}
    )
    assert response.status_code == 200
    assert f'action="/care/{subject_a.id}/note"' in response.text


async def test_a_revoked_consent_refuses_the_stale_tab(doctor_client, db_session):
    """The other correct outcome: not the wrong patient, and not a silent write."""

    client, _doctor, (owner_a, subject_a), _b = doctor_client
    from vitals.models.professional import CareRelationship

    relationship_id = await db_session.scalar(
        select(CareRelationship.id).where(CareRelationship.subject_id == subject_a.id)
    )
    await care_service.revoke_consent(
        db_session, relationship_id=relationship_id, actor_user_id=owner_a.id
    )
    await db_session.commit()

    response = await client.post(
        f"/care/{subject_a.id}/note", data={"body": "Too late"}
    )
    assert response.status_code == 404
    assert await db_session.scalar(
        select(func.count()).select_from(ProfessionalNote)
    ) == 0


# ── What a stranger sees ─────────────────────────────────────────────────────


async def test_a_patient_you_do_not_hold_is_a_miss(doctor_client, db_session):
    """Not a 403: a denial and a miss have to look the same from outside."""

    client, _doctor, _a, _b = doctor_client
    _owner, stranger_subject = await _patient(db_session, "care-ui-stranger")
    await db_session.commit()

    for path in (
        f"/care/{stranger_subject.id}",
        f"/care/{uuid.uuid4()}",
    ):
        response = await client.get(path, headers={"Accept": "text/html"})
        assert response.status_code == 404, path
        assert "Patient care-ui-stranger" not in response.text


async def test_a_stranger_gets_the_login_page_rather_than_a_miss(client, db_session):
    """The uniform-refusal rule is about telling authenticated callers apart.

    Answering 404 to somebody with no session at all would only hide the login
    page from them.
    """

    _owner, subject = await _patient(db_session, "care-ui-anon")
    await db_session.commit()

    response = await client.get(
        f"/care/{subject.id}", headers={"Accept": "text/html"}
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith("/login")


# ── The roster ───────────────────────────────────────────────────────────────


async def test_the_roster_lists_both_patients(doctor_client):
    client, _doctor, (_oa, subject_a), (_ob, subject_b) = doctor_client
    response = await client.get("/care", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert f"/care/{subject_a.id}" in response.text
    assert f"/care/{subject_b.id}" in response.text


async def test_a_paused_consent_is_shown_as_paused_rather_than_hidden(
    doctor_client, db_session
):
    """"Gone" and "on hold" are different things to tell a professional."""

    from vitals.models.professional import CareRelationship

    client, _doctor, (owner_a, subject_a), _b = doctor_client
    relationship_id = await db_session.scalar(
        select(CareRelationship.id).where(CareRelationship.subject_id == subject_a.id)
    )
    await care_service.set_consent_paused(
        db_session,
        relationship_id=relationship_id,
        actor_user_id=owner_a.id,
        paused=True,
    )
    await db_session.commit()

    response = await client.get("/care", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "Patient care-ui-a" in response.text
    # Listed, named, and not a link — the record is not open right now.
    assert f'href="/care/{subject_a.id}"' not in response.text

    # And it really is closed, not merely unlinked.
    assert (
        await client.get(f"/care/{subject_a.id}", headers={"Accept": "text/html"})
    ).status_code == 404


# ── The banner ───────────────────────────────────────────────────────────────


async def test_the_page_says_why_it_is_open(doctor_client):
    """A screen that cannot say why it is open is one nobody can audit by looking."""

    client, _doctor, (_owner_a, subject_a), _b = doctor_client
    response = await client.get(
        f"/care/{subject_a.id}", headers={"Accept": "text/html"}
    )
    assert response.status_code == 200
    assert "Patient care-ui-a" in response.text
    assert "Открыта вам" in response.text or "Shared with you" in response.text
