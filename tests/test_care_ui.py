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
from pathlib import Path

import pytest
from sqlalchemy import func, select

from vitals.enums import ProfessionalKind, UserRoleName, UserStatus
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.professional import ProfessionalNote
from vitals.services.care import invitations, relationships


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
    issued = await invitations.invite(
        session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=ProfessionalKind.DOCTOR,
        email=email,
    )
    await invitations.accept(
        session,
        token=issued.token,
        accepting_user_id=professional.id,
        verified_email=email,
    )
    relationship = await relationships.establish_from_invitation(
        session, invitation=issued.invitation
    )
    if consent:
        await relationships.grant_consent(
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
    assert f'action="/care/{subject_a.id}/plan"' in response.text


async def test_notes_and_plans_name_their_author(doctor_client, db_session):
    """A shared record must say who wrote professional guidance."""

    client, doctor, (_owner_a, subject_a), _b = doctor_client
    from vitals.models.professional import ProfessionalProfile

    db_session.add(
        ProfessionalProfile(
            user_id=doctor.id,
            kind=ProfessionalKind.DOCTOR.value,
            display_name="Dr Human Name",
        )
    )
    await db_session.commit()
    await client.post(
        f"/care/{subject_a.id}/note",
        data={"body": "Watch the recovery trend."},
    )
    await client.post(
        f"/care/{subject_a.id}/plan",
        data={
            "title": "Easy week",
            "body": "Keep every session conversational.",
            "effective_from": "2026-09-01",
        },
    )

    page = await client.get(
        f"/care/{subject_a.id}", headers={"Accept": "text/html"}
    )
    assert page.status_code == 200
    assert page.text.count("Dr Human Name") >= 2
    assert "Watch the recovery trend." in page.text
    assert "Easy week" in page.text


async def test_the_plan_lifecycle_is_available_to_its_author(
    doctor_client, db_session
):
    from vitals.enums import CarePlanStatus
    from vitals.models.professional import CarePlan

    client, _doctor, (_owner_a, subject_a), _b = doctor_client
    subject_id = subject_a.id
    await client.post(
        f"/care/{subject_id}/plan",
        data={
            "title": "Easy week",
            "body": "Keep every session conversational.",
            "effective_from": "2026-09-01",
        },
    )
    plan = await db_session.scalar(select(CarePlan))
    assert plan is not None
    plan_id = plan.id

    activated = await client.post(
        f"/care/{subject_id}/plan/{plan_id}/status",
        data={"plan_status": CarePlanStatus.ACTIVE.value},
        follow_redirects=False,
    )
    assert activated.status_code == 303
    db_session.expire_all()
    assert (await db_session.get(CarePlan, plan_id)).status == CarePlanStatus.ACTIVE

    archived = await client.post(
        f"/care/{subject_id}/plan/{plan_id}/status",
        data={"plan_status": CarePlanStatus.ARCHIVED.value},
        follow_redirects=False,
    )
    assert archived.status_code == 303
    db_session.expire_all()
    assert (await db_session.get(CarePlan, plan_id)).status == CarePlanStatus.ARCHIVED


async def test_a_revoked_consent_refuses_the_stale_tab(doctor_client, db_session):
    """The other correct outcome: not the wrong patient, and not a silent write."""

    client, _doctor, (owner_a, subject_a), _b = doctor_client
    from vitals.models.professional import CareRelationship

    relationship_id = await db_session.scalar(
        select(CareRelationship.id).where(CareRelationship.subject_id == subject_a.id)
    )
    await relationships.revoke_consent(
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
    assert "consent v" not in response.text


async def test_the_roster_puts_unread_patient_conversations_first(doctor_client):
    client, doctor, (_owner_a, subject_a), (owner_b, subject_b) = doctor_client
    opened_a = await client.post(
        f"/care/{subject_a.id}/messages",
        data={"title": "Routine", "body": "How are you?"},
        follow_redirects=False,
    )
    assert opened_a.status_code == 303
    opened_b = await client.post(
        f"/care/{subject_b.id}/messages",
        data={"title": "Knee", "body": "How is it today?"},
        follow_redirects=False,
    )
    assert opened_b.status_code == 303
    thread_b = opened_b.headers["location"].rsplit("/", 1)[1]

    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client.cookies.set(SESSION_COOKIE, create_session(owner_b.username))
    replied = await client.post(
        f"/care/{subject_b.id}/messages/{thread_b}",
        data={"body": "It hurts when I run."},
        follow_redirects=False,
    )
    assert replied.status_code == 303

    client.cookies.set(SESSION_COOKIE, create_session(doctor.username))
    roster = await client.get("/care", headers={"Accept": "text/html"})
    assert roster.status_code == 200
    assert f'href="/care/{subject_b.id}/messages"' in roster.text
    assert "New: 1" in roster.text or "Новых: 1" in roster.text
    assert "Last message:" in roster.text or "Последнее сообщение:" in roster.text
    assert roster.text.index(subject_b.display_name) < roster.text.index(
        subject_a.display_name
    )


async def test_the_patient_is_prompted_after_a_professional_accepts(
    client, db_session
):
    owner, subject = await _patient(db_session, "care-consent-task")
    doctor = await _user(
        db_session,
        "care-consent-task-doctor",
        roles=(UserRoleName.DOCTOR,),
    )
    relationship = await _take_into_care(
        db_session,
        owner=owner,
        subject=subject,
        professional=doctor,
        consent=False,
    )
    await db_session.commit()

    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client.cookies.set(SESSION_COOKIE, create_session(owner.username))
    page = await client.get("/today", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert 'href="/settings/care"' in page.text
    assert "Choose access" in page.text or "Выбрать доступ" in page.text

    await relationships.grant_consent(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
    )
    await db_session.commit()
    page = await client.get("/today", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "Choose access" not in page.text
    assert "Выбрать доступ" not in page.text


async def test_a_professional_keeps_phone_navigation_and_logout(client, db_session):
    doctor = await _user(
        db_session, "mobile-doctor", roles=(UserRoleName.DOCTOR,)
    )
    owner, subject = await _patient(db_session, "mobile-patient")
    await _take_into_care(
        db_session, owner=owner, subject=subject, professional=doctor
    )
    await db_session.commit()

    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client.cookies.set(SESSION_COOKIE, create_session(doctor.username))

    response = await client.get("/care", headers={"Accept": "text/html"})

    assert response.status_code == 200
    assert 'class="md:hidden v-bottom-nav mh-bnav mh-role-bnav-2"' in response.text
    assert 'href="/care"' in response.text
    assert 'action="/logout"' in response.text


async def test_a_paused_consent_is_shown_as_paused_rather_than_hidden(
    doctor_client, db_session
):
    """"Gone" and "on hold" are different things to tell a professional."""

    from vitals.models.professional import CareRelationship

    client, _doctor, (owner_a, subject_a), _b = doctor_client
    relationship_id = await db_session.scalar(
        select(CareRelationship.id).where(CareRelationship.subject_id == subject_a.id)
    )
    await relationships.set_consent_paused(
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


async def test_an_expired_consent_is_not_offered_as_an_open_record(
    doctor_client, db_session
):
    """The roster and the record resolver must agree at the expiry boundary."""

    from datetime import timedelta

    from sqlalchemy import select

    from vitals.models.professional import ConsentGrant
    from vitals.utils.timeutils import now_utc

    client, _doctor, (_owner_a, subject_a), _b = doctor_client
    grant = await db_session.scalar(
        select(ConsentGrant).where(ConsentGrant.subject_id == subject_a.id)
    )
    grant.granted_at = now_utc() - timedelta(days=366)
    grant.expires_at = now_utc() - timedelta(days=1)
    await db_session.commit()

    response = await client.get("/care", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "Consent expired" in response.text or "Согласие истекло" in response.text
    assert f'href="/care/{subject_a.id}"' not in response.text
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


# ── The record itself ────────────────────────────────────────────────────────


async def _weight(session, subject_id, *, kg: float, days_ago: int = 2):
    """A weigh-in inside the record's window.

    ``days_ago`` defaults to two rather than zero on purpose: the record shows
    the same closed period every report in this product uses — completed days
    only — so a row written for today is outside it by design.
    """

    from datetime import timedelta

    from vitals.enums import Domain, Source  # noqa: PLC0415
    from vitals.models.weight import WeightLog
    from vitals.utils.timeutils import today_local

    session.add(
        WeightLog(
            subject_id=subject_id,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            date=today_local() - timedelta(days=days_ago),
            weight_kg=kg,
        )
    )
    await session.flush()


async def test_the_professional_sees_the_patients_own_record(
    doctor_client, db_session
):
    """Not only the notes about them — the record the notes are about.

    A doctor and a trainer are granted the same domains: the kind decides who is
    writing, not what may be read. So the default view is the whole record, and
    this is the assertion that it is actually rendered rather than merely
    permitted.
    """

    client, _doctor, (_owner_a, subject_a), (_owner_b, subject_b) = doctor_client
    id_a, id_b = subject_a.id, subject_b.id
    await _weight(db_session, id_a, kg=61.5)
    await _weight(db_session, id_b, kg=93.25)
    await db_session.commit()

    page_a = await client.get(f"/care/{id_a}", headers={"Accept": "text/html"})
    assert page_a.status_code == 200
    # Both separators, because the number is rendered through the locale filter
    # and the test should not pin which locale the suite runs in.
    assert "61,5" in page_a.text or "61.5" in page_a.text
    # And nobody else's. Two patients of the same doctor is the case where a
    # missing subject filter would not show up as an error, only as the wrong
    # number on the right screen.
    assert "93,25" not in page_a.text and "93.25" not in page_a.text


async def test_a_domain_the_patient_withheld_is_named_rather_than_missing(
    doctor_client, db_session
):
    """A clinician reading a partial record has to know it is partial.

    The patient can narrow a consent, and when they have, the sections outside
    it are named rather than quietly absent. A gap a reader cannot see is worse
    than one they can: it reads as "nothing there" and gets reasoned from.
    """

    from vitals.access import AccessScope, PolicyAction, PolicyResourceType
    from vitals.enums import Domain
    from vitals.models.professional import CareRelationship

    client, doctor, (owner_a, subject_a), _b = doctor_client
    id_a, owner_a_id, doctor_id = subject_a.id, owner_a.id, doctor.id
    await _weight(db_session, id_a, kg=61.5)
    await db_session.commit()

    relationship_id = await db_session.scalar(
        select(CareRelationship.id).where(
            CareRelationship.subject_id == id_a,
            CareRelationship.professional_user_id == doctor_id,
        )
    )
    # Everything the default grants, minus the weight domain.
    narrowed = frozenset(
        scope
        for scope in relationships.default_scopes(ProfessionalKind.DOCTOR)
        if not (
            scope.resource_type is PolicyResourceType.DOMAIN
            and scope.resource_key == Domain.WEIGHT.value
        )
    )
    assert narrowed != relationships.default_scopes(ProfessionalKind.DOCTOR)
    del AccessScope, PolicyAction
    await relationships.grant_consent(
        db_session,
        relationship_id=relationship_id,
        actor_user_id=owner_a_id,
        scopes=narrowed,
    )
    await db_session.commit()

    page = await client.get(f"/care/{id_a}", headers={"Accept": "text/html"})
    assert page.status_code == 200
    # The number is gone, and the fact that it was withheld is not.
    assert "61,5" not in page.text and "61.5" not in page.text
    assert "Not shared with you" in page.text or "Не открыто вам" in page.text


# ── The care-team conversation ───────────────────────────────────────────────


async def test_a_stale_tab_talks_to_the_patient_it_was_looking_at(
    doctor_client, db_session
):
    """The same property the note form has, on the surface the patient reads.

    A doctor with two patients open in two tabs is the ordinary case, and a
    message that lands on whoever is "selected" would put one person's words in
    another's conversation — where, unlike a note, the wrong patient would then
    read it.
    """

    from sqlalchemy import select

    from vitals.models.care_thread import CareMessage

    client, _doctor, (_owner_a, subject_a), (_owner_b, subject_b) = doctor_client

    opened = await client.post(
        f"/care/{subject_a.id}/messages",
        data={"title": "Bloods"},
        follow_redirects=False,
    )
    assert opened.status_code == 303
    thread_id = opened.headers["location"].rsplit("/", 1)[1]

    said = await client.post(
        f"/care/{subject_a.id}/messages/{thread_id}",
        data={"body": "Please fast for twelve hours."},
        follow_redirects=False,
    )
    assert said.status_code == 303

    rows = list(await db_session.scalars(select(CareMessage)))
    assert [row.subject_id for row in rows] == [subject_a.id]
    assert subject_b.id not in {row.subject_id for row in rows}


async def test_a_conversation_starts_with_its_first_message(
    doctor_client, db_session
):
    """Starting a shared conversation is one action, not two screens."""

    from vitals.models.care_thread import CareMessage

    client, _doctor, (_owner_a, subject_a), _b = doctor_client
    opened = await client.post(
        f"/care/{subject_a.id}/messages",
        data={"title": "Bloods", "body": "Please fast for twelve hours."},
        follow_redirects=False,
    )

    assert opened.status_code == 303
    message = await db_session.scalar(select(CareMessage))
    assert message is not None
    assert message.subject_id == subject_a.id
    assert message.body == "Please fast for twelve hours."


async def test_the_patient_sees_new_until_the_conversation_is_opened(
    doctor_client,
):
    client, _doctor, (owner_a, subject_a), _b = doctor_client
    opened = await client.post(
        f"/care/{subject_a.id}/messages",
        data={"title": "Bloods", "body": "Please fast for twelve hours."},
        follow_redirects=False,
    )
    assert opened.status_code == 303
    thread_id = opened.headers["location"].rsplit("/", 1)[1]

    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client.cookies.set(SESSION_COOKIE, create_session(owner_a.username))
    inbox = await client.get(
        f"/care/{subject_a.id}/messages", headers={"Accept": "text/html"}
    )
    assert inbox.status_code == 200
    unread_badges = (
        'class="v-chip v-chip-sm">Новое</span>',
        'class="v-chip v-chip-sm">New</span>',
    )
    assert any(badge in inbox.text for badge in unread_badges)
    assert 'data-care-unread-count class="v-chip v-chip-sm">1</span>' in inbox.text

    conversation = await client.get(
        f"/care/{subject_a.id}/messages/{thread_id}",
        headers={"Accept": "text/html"},
    )
    assert conversation.status_code == 200
    inbox = await client.get(
        f"/care/{subject_a.id}/messages", headers={"Accept": "text/html"}
    )
    assert all(badge not in inbox.text for badge in unread_badges)
    assert "data-care-unread-count" not in inbox.text


async def test_another_patients_thread_is_not_reachable_by_its_id(
    doctor_client, db_session
):
    client, _doctor, (_owner_a, subject_a), (_owner_b, subject_b) = doctor_client

    opened = await client.post(
        f"/care/{subject_a.id}/messages",
        data={"title": "Bloods"},
        follow_redirects=False,
    )
    thread_id = opened.headers["location"].rsplit("/", 1)[1]

    # The same thread id, under the other patient in the path.
    wrong = await client.get(f"/care/{subject_b.id}/messages/{thread_id}")
    assert wrong.status_code == 404


async def test_the_patient_reaches_their_own_conversations_without_an_id(
    client, db_session, legacy_owner_roots
):
    """``/messages`` is the patient's door; the subject comes from who they are."""

    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client.cookies.set(SESSION_COOKIE, create_session("tester"))
    response = await client.get("/messages", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/care/{legacy_owner_roots.subject_id}/messages"
    )


async def test_an_account_with_no_record_is_told_so_at_the_patients_door(
    client, db_session, legacy_owner_roots
):
    """A doctor's door into a conversation is the patient it is about."""

    doctor = await _user(db_session, "messages-no-record")
    await db_session.commit()

    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client.cookies.set(SESSION_COOKIE, create_session("messages-no-record"))
    response = await client.get(
        "/messages", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert response.status_code == 409
    # A page, not a sentence: the refusal is rendered through ``refusal.html``
    # so whoever meets it has somewhere to go. An account with no record of its
    # own can open exactly one address on a shared installation, and being told
    # the right thing on a white page with no link is still a dead end.
    assert "нет собственной записи" in response.text
    assert 'href="/care"' in response.text
    del doctor


async def test_the_conversation_page_renders_what_was_said(
    doctor_client, db_session
):
    """Rendered, not just written.

    The service tests read messages back as objects and once passed while this
    page answered 500 on async relationship loading. Rendering also has to use
    human record/profile names rather than expose login handles as chat names.
    """

    client, doctor, (_owner_a, subject_a), _b = doctor_client

    from vitals.models.professional import ProfessionalProfile

    db_session.add(
        ProfessionalProfile(
            user_id=doctor.id,
            kind=ProfessionalKind.DOCTOR.value,
            display_name="Dr Conversation Name",
        )
    )
    await db_session.commit()

    opened = await client.post(
        f"/care/{subject_a.id}/messages",
        data={"title": "Bloods"},
        follow_redirects=False,
    )
    thread_id = opened.headers["location"].rsplit("/", 1)[1]
    await client.post(
        f"/care/{subject_a.id}/messages/{thread_id}",
        data={"body": "Please fast for twelve hours."},
        follow_redirects=False,
    )

    page = await client.get(
        f"/care/{subject_a.id}/messages/{thread_id}",
        headers={"Accept": "text/html"},
    )
    assert page.status_code == 200
    assert "Please fast for twelve hours." in page.text
    # Who said it and who is in the room, in names humans recognize rather than
    # the account handles authorization uses.
    assert "Dr Conversation Name" in page.text
    assert subject_a.display_name in page.text
    assert doctor.username not in page.text
    assert "Bloods" in page.text
    assert "All conversations" in page.text or "Все разговоры" in page.text
    assert "Start conversation" not in page.text
    assert "Начать разговор" not in page.text


async def test_the_conversation_list_renders(doctor_client):
    client, _doctor, (_owner_a, subject_a), _b = doctor_client

    await client.post(
        f"/care/{subject_a.id}/messages",
        data={"title": "Bloods"},
        follow_redirects=False,
    )
    page = await client.get(
        f"/care/{subject_a.id}/messages", headers={"Accept": "text/html"}
    )
    assert page.status_code == 200
    assert "Bloods" in page.text
    assert "Start conversation" in page.text or "Начать разговор" in page.text


async def test_a_care_attachment_stays_private_and_follows_live_consent(
    doctor_client,
    db_session,
    monkeypatch,
    tmp_path,
):
    """The URL is a conversation capability check, not a storage path."""

    from vitals.models.care_thread import CareMessageAttachment
    from vitals.models.professional import CareRelationship
    from vitals.models.tenancy import FileAsset
    from web.auth import create_session
    from web.config import SESSION_COOKIE
    from web.uploads import private_file_disk_path

    private_root = tmp_path / "private-medical-files"
    monkeypatch.setenv("VITALS_PRIVATE_FILE_ROOT", str(private_root))
    client, doctor, (owner_a, subject_a), (_owner_b, subject_b) = doctor_client
    payload = b"%PDF-1.7\nsynthetic care document\n%%EOF\n"

    opened = await client.post(
        f"/care/{subject_a.id}/messages",
        data={"title": "Bloods", "body": "Please review this result."},
        files={
            # The claimed content type is deliberately hostile. The server
            # derives its response type from validated content and extension.
            "attachment": ("synthetic-result.pdf", payload, "text/html"),
        },
        follow_redirects=False,
    )
    assert opened.status_code == 303
    thread_id = opened.headers["location"].rsplit("/", 1)[1]

    attachment = await db_session.scalar(select(CareMessageAttachment))
    assert attachment is not None
    asset = await db_session.get(FileAsset, attachment.file_asset_id)
    assert asset is not None
    assert asset.subject_id == subject_a.id
    assert asset.storage_backend == "private_local"
    assert asset.status == "active"
    assert asset.purpose == "care_message_attachment"
    assert asset.byte_size == len(payload)
    assert len(asset.sha256_hex or "") == 64
    assert asset.storage_ref.startswith("care/")
    assert subject_a.id.hex not in asset.storage_ref
    path = private_file_disk_path(str(private_root), asset.storage_ref)
    assert Path(path).read_bytes() == payload

    page = await client.get(
        f"/care/{subject_a.id}/messages/{thread_id}",
        headers={"Accept": "text/html"},
    )
    assert page.status_code == 200
    assert "synthetic-result.pdf" in page.text

    download_url = (
        f"/care/{subject_a.id}/messages/{thread_id}/attachments/{attachment.id}"
    )
    downloaded = await client.get(download_url)
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    assert downloaded.headers["content-type"].startswith("application/pdf")
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert "attachment" in downloaded.headers["content-disposition"]

    other = await client.post(
        f"/care/{subject_a.id}/messages",
        data={"title": "Another conversation"},
        follow_redirects=False,
    )
    assert other.status_code == 303
    other_thread_id = other.headers["location"].rsplit("/", 1)[1]
    wrong_thread = await client.get(
        f"/care/{subject_a.id}/messages/{other_thread_id}/attachments/{attachment.id}"
    )
    assert wrong_thread.status_code == 404

    # The same opaque attachment under another patient is indistinguishable
    # from one that does not exist.
    wrong_subject = await client.get(
        f"/care/{subject_b.id}/messages/{thread_id}/attachments/{attachment.id}"
    )
    assert wrong_subject.status_code == 404

    relationship = await db_session.scalar(
        select(CareRelationship).where(
            CareRelationship.subject_id == subject_a.id,
            CareRelationship.professional_user_id == doctor.id,
        )
    )
    assert relationship is not None
    await relationships.set_consent_paused(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner_a.id,
        paused=True,
    )
    await db_session.commit()
    assert (await client.get(download_url)).status_code == 404

    # Withdrawing professional access never withdraws the patient's own record.
    client.cookies.set(SESSION_COOKIE, create_session(owner_a.username))
    patient_download = await client.get(download_url)
    assert patient_download.status_code == 200
    assert patient_download.content == payload

    # A same-size replacement is not allowed to borrow the metadata row. The
    # stored digest is checked before FileResponse starts streaming bytes.
    Path(path).write_bytes(b"x" * len(payload))
    assert (await client.get(download_url)).status_code == 404


async def test_a_spoofed_care_attachment_is_rejected_before_writing_a_message(
    doctor_client,
    db_session,
    monkeypatch,
    tmp_path,
):
    from vitals.models.care_thread import CareMessage, CareThread

    monkeypatch.setenv("VITALS_PRIVATE_FILE_ROOT", str(tmp_path / "private"))
    client, _doctor, (_owner_a, subject_a), _b = doctor_client
    threads_before = await db_session.scalar(
        select(func.count()).select_from(CareThread)
    )
    messages_before = await db_session.scalar(
        select(func.count()).select_from(CareMessage)
    )
    response = await client.post(
        f"/care/{subject_a.id}/messages",
        data={"title": "Spoof", "body": "This must not persist."},
        files={
            "attachment": (
                "looks-like-a-report.pdf",
                b"<html><script>bad()</script></html>",
                "application/pdf",
            )
        },
        follow_redirects=False,
    )

    assert response.status_code == 415
    assert (
        await db_session.scalar(select(func.count()).select_from(CareThread))
        == threads_before
    )
    assert (
        await db_session.scalar(select(func.count()).select_from(CareMessage))
        == messages_before
    )
    assert not (tmp_path / "private").exists()
