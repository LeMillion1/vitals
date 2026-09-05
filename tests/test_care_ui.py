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

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from vitals.enums import ProfessionalKind, UserRoleName, UserStatus
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.professional import (
    CareRelationship,
    ProfessionalNote,
    ProfessionalProfile,
)
from vitals.persistence import rls
from vitals.services.care import invitations, professionals, relationships
from vitals.utils.timeutils import now_utc


async def _user(session, slug: str, *, roles=()) -> User:
    email = slug if "@" in slug else f"{slug}@example.test"
    user = User(
        username=slug,
        normalized_username=slug,
        email=email,
        normalized_email=email,
        email_verified_at=now_utc(),
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


async def _take_into_care(
    session,
    *,
    owner,
    subject,
    professional,
    consent=True,
    kind=ProfessionalKind.DOCTOR,
):
    profile = await session.scalar(
        select(ProfessionalProfile).where(
            ProfessionalProfile.user_id == professional.id
        )
    )
    if profile is None:
        operator = await _user(
            session,
            f"{professional.username}-reviewer",
            roles=(UserRoleName.PLATFORM_SUPERADMIN,),
        )
        profile = await professionals.submit_profile(
            session,
            user_id=professional.id,
            kind=kind,
            display_name=(
                f"Coach {professional.username}"
                if kind is ProfessionalKind.TRAINER
                else f"Dr {professional.username}"
            ),
        )
        await professionals.decide(
            session,
            profile_id=profile.id,
            reviewer_user_id=operator.id,
            expected_status="pending",
            status="verified",
        )
    email = f"{professional.username}@example.test"
    issued = await invitations.invite(
        session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=kind,
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


async def _relationship_id(session, *, subject, professional) -> uuid.UUID:
    relationship_id = await session.scalar(
        select(CareRelationship.id).where(
            CareRelationship.subject_id == subject.id,
            CareRelationship.professional_user_id == professional.id,
        )
    )
    assert relationship_id is not None
    return relationship_id


async def _open_professional_conversation(client, session, *, subject, professional):
    relationship_id = await _relationship_id(
        session, subject=subject, professional=professional
    )
    return await client.post(
        f"/care/{subject.id}/messages/relationship/{relationship_id}",
        follow_redirects=False,
    )


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
    doctor_email = (
        doctor.username
        if "@" in doctor.username
        else f"{doctor.username}@example.test"
    )
    doctor.email = doctor_email
    doctor.normalized_email = doctor_email
    doctor.email_verified_at = now_utc()
    db_session.add(UserRole(user_id=doctor.id, role=UserRoleName.DOCTOR.value))
    await db_session.flush()
    operator = await _user(
        db_session,
        "care-ui-operator",
        roles=(UserRoleName.PLATFORM_SUPERADMIN,),
    )
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Human Name",
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status="pending",
        status="verified",
    )

    owner_a, subject_a = await _patient(db_session, "care-ui-a")
    owner_b, subject_b = await _patient(db_session, "care-ui-b")
    await _take_into_care(
        db_session, owner=owner_a, subject=subject_a, professional=doctor
    )
    # These acceptances represent two distinct browser requests. Production
    # gives each one a fresh session, so end and forget the first RLS scope.
    await db_session.commit()
    db_session.info.pop(rls._SUBJECT_KEY, None)
    await _take_into_care(
        db_session, owner=owner_b, subject=subject_b, professional=doctor
    )
    await db_session.commit()
    db_session.info.pop(rls._SUBJECT_KEY, None)

    client.cookies.set(SESSION_COOKIE, create_session(doctor.username))
    del set_session_cookie
    return client, doctor, (owner_a, subject_a), (owner_b, subject_b)


@pytest.fixture
async def new_trainer_client(client, db_session, legacy_owner_roots):
    """A professional-only account at the first useful screen after login."""

    from web.auth import create_session
    from web.config import SESSION_COOKIE

    trainer = await _user(
        db_session,
        "care-onboarding-trainer",
        roles=(UserRoleName.TRAINER,),
    )
    await db_session.commit()
    client.cookies.set(SESSION_COOKIE, create_session(trainer.username))
    return client, trainer


async def test_a_new_professional_lands_on_one_onboarding_action(
    new_trainer_client,
):
    client, _trainer = new_trainer_client

    landing = await client.get("/", follow_redirects=False)
    assert landing.status_code == 303
    assert landing.headers["location"] == "/care"

    page = await client.get("/care", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert 'action="/care/profile"' in page.text
    assert "Trainer" in page.text or "Тренер" in page.text
    assert 'value="care-onboarding-trainer"' in page.text
    assert (
        "Certificate or qualification" in page.text
        or "Сертификат или квалификация" in page.text
    )
    assert (
        "Name shown to clients" in page.text
        or "Имя для клиентов" in page.text
    )
    assert "independent operator" not in page.text
    assert "независимой проверки" not in page.text
    assert 'name="kind"' not in page.text
    # Navigation and sign-out stay reachable before the first patient exists.
    assert 'href="/care"' in page.text
    assert 'action="/logout"' in page.text
    # Device setup is secondary and appears only after professional review.
    assert "/settings/notifications/web-push/subscription" not in page.text


async def test_professional_profile_error_stays_on_the_plain_form(
    new_trainer_client, db_session
):
    client, trainer = new_trainer_client
    trainer_id = trainer.id

    response = await client.post(
        "/care/profile",
        data={
            "display_name": "   ",
            "credential_reference": "CERT-KEEP",
        },
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert 'action="/care/profile"' in response.text
    assert 'role="alert"' in response.text
    assert 'value="CERT-KEEP"' in response.text
    assert await db_session.scalar(
        select(ProfessionalProfile.id).where(
            ProfessionalProfile.user_id == trainer_id
        )
    ) is None


async def test_email_username_is_not_suggested_as_a_public_profile_name(
    client, db_session
):
    """The initial fallback is blank, but a value submitted by the user survives."""

    from web.auth import create_session
    from web.config import SESSION_COOKIE

    trainer = await _user(
        db_session,
        "profile-default@example.test",
        roles=(UserRoleName.TRAINER,),
    )
    await db_session.commit()
    client.cookies.set(SESSION_COOKIE, create_session(trainer.username))

    initial = await client.get("/care", headers={"Accept": "text/html"})
    assert initial.status_code == 200
    assert re.search(
        r'id="professional-display-name"[^>]*value=""', initial.text
    )

    submitted_name = "Visible.Name@example.test"
    invalid = await client.post(
        "/care/profile",
        data={
            "display_name": submitted_name,
            "credential_reference": "x" * 201,
        },
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert invalid.status_code == 422
    assert f'value="{submitted_name}"' in invalid.text


async def test_an_ordinary_member_is_sent_from_professional_care_to_their_hub(
    client, db_session
):
    """The professional home must not masquerade as an empty patient roster."""

    from web.auth import create_session
    from web.config import SESSION_COOKIE

    member, _subject = await _patient(db_session, "care-route-member")
    await db_session.commit()
    client.cookies.set(SESSION_COOKIE, create_session(member.username))
    response = await client.get(
        "/care", headers={"Accept": "text/html"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings/care"


@pytest.mark.parametrize(
    "roles",
    [
        (UserRoleName.DOCTOR,),
        (UserRoleName.TRAINER,),
        (UserRoleName.DOCTOR, UserRoleName.TRAINER),
    ],
)
async def test_each_professional_role_keeps_the_roster_route(
    client, db_session, roles
):
    """Doctor, trainer and dual-role accounts all own the `/care` workspace."""

    from web.auth import create_session
    from web.config import SESSION_COOKIE

    professional = await _user(
        db_session,
        "care-route-" + "-".join(role.value for role in roles),
        roles=roles,
    )
    await db_session.commit()
    client.cookies.set(SESSION_COOKIE, create_session(professional.username))

    response = await client.get(
        "/care", headers={"Accept": "text/html"}, follow_redirects=False
    )

    assert response.status_code == 200
    assert 'href="/care"' in response.text


async def test_trainer_workspace_uses_neutral_care_language(client, db_session):
    from web.auth import create_session
    from web.config import SESSION_COOKIE

    trainer = await _user(
        db_session,
        "care-language-trainer",
        roles=(UserRoleName.TRAINER,),
    )
    owner, subject = await _patient(db_session, "care-language-trainee")
    await _take_into_care(
        db_session,
        owner=owner,
        subject=subject,
        professional=trainer,
        kind=ProfessionalKind.TRAINER,
    )
    await db_session.commit()
    client.cookies.set(SESSION_COOKIE, create_session(trainer.username))

    record = await client.get(
        f"/care/{subject.id}", headers={"Accept": "text/html"}
    )
    assert record.status_code == 200
    assert "People in your care" in record.text or "К подопечным" in record.text
    assert (
        "Your private conversation with this person." in record.text
        or "Ваш личный разговор с этим подопечным." in record.text
    )
    assert (
        "Clear steps they can follow." in record.text
        or "Понятные и выполнимые шаги." in record.text
    )
    assert "All patients" not in record.text
    assert "Все пациенты" not in record.text
    assert "this patient" not in record.text

    opened = await _open_professional_conversation(
        client, db_session, subject=subject, professional=trainer
    )
    conversation = await client.get(
        opened.headers["location"], headers={"Accept": "text/html"}
    )
    assert conversation.status_code == 200
    assert (
        "Your private conversation with this person." in conversation.text
        or "Ваш личный разговор с этим подопечным." in conversation.text
    )
    assert (
        "Write a message" in conversation.text
        or "Напишите сообщение" in conversation.text
    )
    assert "Write to the patient" not in conversation.text
    assert "Напишите пациенту" not in conversation.text


async def test_the_account_role_not_the_form_chooses_profile_kind(
    new_trainer_client, db_session
):
    client, trainer = new_trainer_client
    trainer_id = trainer.id
    response = await client.post(
        "/care/profile",
        data={
            "display_name": "Coach Synthetic",
            "credential_reference": "CERT-42",
            "kind": "doctor",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/care?submitted=1"

    db_session.expire_all()
    from vitals.models.professional import ProfessionalProfile

    profile = await db_session.scalar(
        select(ProfessionalProfile).where(ProfessionalProfile.user_id == trainer_id)
    )
    assert profile.kind == ProfessionalKind.TRAINER.value
    assert profile.verification_status == "pending"

    page = await client.get(response.headers["location"])
    assert "under review" in page.text.lower() or "на проверке" in page.text.lower()
    assert 'id="professional-review-title"' in page.text
    assert (
        "Return here to see the current review status" in page.text
        or "Возвращайтесь сюда, чтобы увидеть актуальный статус" in page.text
    )
    assert "sent for independent review" not in page.text
    assert "отправлен на независимую проверку" not in page.text
    assert "Check status" in page.text or "Проверить статус" in page.text


async def test_verified_empty_roster_explains_the_invitation_flow(
    client, db_session
):
    from web.auth import create_session
    from web.config import SESSION_COOKIE

    trainer = await _user(
        db_session,
        "care-empty-trainer",
        roles=(UserRoleName.TRAINER,),
    )
    operator = await _user(
        db_session,
        "care-empty-operator",
        roles=(UserRoleName.PLATFORM_SUPERADMIN,),
    )
    profile = await professionals.submit_profile(
        db_session,
        user_id=trainer.id,
        kind=ProfessionalKind.TRAINER,
        display_name="Coach Empty",
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status="pending",
        status="verified",
    )
    await db_session.commit()
    client.cookies.set(SESSION_COOKIE, create_session(trainer.username))

    page = await client.get("/care", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "Clients" in page.text or "Клиенты" in page.text
    assert 'id="professional-roster-empty-title"' in page.text
    assert (
        "one-time invitation link" in page.text
        or "одноразовую ссылку" in page.text
    )
    assert (
        "email address that is verified on this account" in page.text
        or "адрес, который подтверждён в этом аккаунте" in page.text
    )
    assert page.text.index('id="professional-roster-empty-title"') < page.text.index(
        "data-web-push-card"
    )


async def test_a_rejected_professional_can_correct_the_same_profile(
    new_trainer_client, db_session
):
    client, trainer = new_trainer_client
    trainer_id = trainer.id
    await client.post(
        "/care/profile",
        data={"display_name": "Coach First", "credential_reference": "WRONG"},
    )
    operator = await _user(
        db_session,
        "care-onboarding-operator",
        roles=(UserRoleName.PLATFORM_SUPERADMIN,),
    )
    from vitals.models.professional import ProfessionalProfile

    profile = await db_session.scalar(
        select(ProfessionalProfile).where(ProfessionalProfile.user_id == trainer_id)
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status="pending",
        status="rejected",
        note="Use the public register number",
    )
    await db_session.commit()

    rejected = await client.get("/care", headers={"Accept": "text/html"})
    assert "Use the public register number" in rejected.text
    corrected = await client.post(
        "/care/profile",
        data={"display_name": "Coach Corrected", "credential_reference": "CERT-7"},
        follow_redirects=False,
    )
    assert corrected.status_code == 303

    db_session.expire_all()
    profile = await db_session.scalar(
        select(ProfessionalProfile).where(ProfessionalProfile.user_id == trainer_id)
    )
    assert profile.id is not None
    assert profile.display_name == "Coach Corrected"
    assert profile.kind == ProfessionalKind.TRAINER.value
    assert profile.verification_status == "pending"
    assert profile.review_note is None


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

    duplicate_activation = await client.post(
        f"/care/{subject_id}/plan/{plan_id}/status",
        data={"plan_status": CarePlanStatus.ACTIVE.value},
        follow_redirects=False,
    )
    assert duplicate_activation.status_code == 303

    hidden_downgrade = await client.post(
        f"/care/{subject_id}/plan/{plan_id}/status",
        data={"plan_status": CarePlanStatus.DRAFT.value},
        follow_redirects=False,
    )
    assert hidden_downgrade.status_code == 400
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

    duplicate_archive = await client.post(
        f"/care/{subject_id}/plan/{plan_id}/status",
        data={"plan_status": CarePlanStatus.ARCHIVED.value},
        follow_redirects=False,
    )
    assert duplicate_archive.status_code == 303

    for invalid_status in (CarePlanStatus.DRAFT, CarePlanStatus.ACTIVE):
        refused = await client.post(
            f"/care/{subject_id}/plan/{plan_id}/status",
            data={"plan_status": invalid_status.value},
            follow_redirects=False,
        )
        assert refused.status_code == 400
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


async def test_the_roster_puts_unread_patient_conversations_first(
    doctor_client, db_session
):
    client, doctor, (_owner_a, subject_a), (owner_b, subject_b) = doctor_client
    opened_a = await _open_professional_conversation(
        client, db_session, subject=subject_a, professional=doctor
    )
    assert opened_a.status_code == 303
    opened_b = await _open_professional_conversation(
        client, db_session, subject=subject_b, professional=doctor
    )
    assert opened_b.status_code == 303
    thread_a = opened_a.headers["location"].rsplit("/", 1)[1]
    thread_b = opened_b.headers["location"].rsplit("/", 1)[1]
    await client.post(
        f"/care/{subject_a.id}/messages/{thread_a}",
        data={"body": "How are you?"},
        follow_redirects=False,
    )
    await client.post(
        f"/care/{subject_b.id}/messages/{thread_b}",
        data={"body": "How is it today?"},
        follow_redirects=False,
    )

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


async def test_roster_message_activity_requires_the_exact_read_scope(
    doctor_client, db_session
):
    from vitals.access import AccessScope, PolicyAction, PolicyResourceType
    from vitals.enums import Domain
    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client, doctor, (_owner_a, subject_a), (owner_b, subject_b) = doctor_client
    opened = await _open_professional_conversation(
        client, db_session, subject=subject_b, professional=doctor
    )
    assert opened.status_code == 303
    thread_id = opened.headers["location"].rsplit("/", 1)[1]

    client.cookies.set(SESSION_COOKIE, create_session(owner_b.username))
    replied = await client.post(
        f"/care/{subject_b.id}/messages/{thread_id}",
        data={"body": "Private activity outside the narrowed consent."},
        follow_redirects=False,
    )
    assert replied.status_code == 303

    relationship_id = await _relationship_id(
        db_session,
        subject=subject_b,
        professional=doctor,
    )
    await relationships.grant_consent(
        db_session,
        relationship_id=relationship_id,
        actor_user_id=owner_b.id,
        scopes=frozenset(
            {
                AccessScope(
                    resource_type=PolicyResourceType.DOMAIN,
                    resource_key=Domain.WEIGHT.value,
                    action=PolicyAction.READ,
                )
            }
        ),
    )
    await db_session.commit()

    rows = await relationships.list_professional_roster(
        db_session,
        professional_user_id=doctor.id,
    )
    narrowed = next(row for row in rows if row.subject_id == subject_b.id)
    assert narrowed.open
    assert narrowed.unread_threads == 0
    assert narrowed.last_message_at is None

    client.cookies.set(SESSION_COOKIE, create_session(doctor.username))
    roster = await client.get("/care", headers={"Accept": "text/html"})
    assert roster.status_code == 200
    assert f'href="/care/{subject_b.id}"' in roster.text
    assert "New:" not in roster.text
    assert "Новых:" not in roster.text
    assert "Last message:" not in roster.text
    assert "Последнее сообщение:" not in roster.text
    assert subject_a.display_name in roster.text
    assert f'action="/care/{subject_b.id}/messages/relationship/' not in roster.text

    # The authorized record is still useful, without promising a conversation
    # that the next POST would correctly refuse under health-only consent.
    record = await client.get(
        f"/care/{subject_b.id}", headers={"Accept": "text/html"}
    )
    assert record.status_code == 200
    assert f'action="/care/{subject_b.id}/messages/relationship/' not in record.text
    refused = await client.post(
        f"/care/{subject_b.id}/messages/relationship/{relationship_id}",
        follow_redirects=False,
    )
    assert refused.status_code == 404


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


async def test_a_suspension_closes_the_roster_record_and_direct_routes(
    doctor_client, db_session
):
    client, doctor, (_owner_a, subject_a), _b = doctor_client
    subject_a_id = subject_a.id
    profile = await db_session.scalar(
        select(ProfessionalProfile).where(ProfessionalProfile.user_id == doctor.id)
    )
    operator = await _user(
        db_session,
        "care-ui-suspension-operator",
        roles=(UserRoleName.PLATFORM_SUPERADMIN,),
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status="verified",
        status="suspended",
        note="Synthetic licence withdrawal",
    )
    await db_session.commit()

    roster = await client.get("/care", headers={"Accept": "text/html"})
    assert roster.status_code == 200
    assert "Synthetic licence withdrawal" in roster.text
    assert f'href="/care/{subject_a_id}"' not in roster.text
    assert (
        await client.get(
            f"/care/{subject_a_id}", headers={"Accept": "text/html"}
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/care/{subject_a_id}/note",
            data={"body": "must not persist after suspension"},
        )
    ).status_code == 404


async def test_role_revocation_hides_patient_names_from_the_roster(
    doctor_client, db_session
):
    client, doctor, (_owner_a, subject_a), (_owner_b, subject_b) = doctor_client
    role = await db_session.scalar(
        select(UserRole).where(
            UserRole.user_id == doctor.id,
            UserRole.role == UserRoleName.DOCTOR.value,
        )
    )
    await db_session.delete(role)
    await db_session.commit()

    roster = await client.get("/care", headers={"Accept": "text/html"})
    assert roster.status_code == 303
    # The synthetic legacy owner also holds the platform role. Once its doctor
    # role is revoked, the platform hub is the only remaining account surface.
    assert roster.headers["location"] == "/settings/platform"
    assert str(subject_a.id) not in roster.text
    assert str(subject_b.id) not in roster.text
    assert subject_a.display_name not in roster.text
    assert subject_b.display_name not in roster.text


# ── The record itself ────────────────────────────────────────────────────────


async def _weight(session, subject_id, *, kg: float, days_ago: int = 0):
    """A weigh-in inside the active record's current subject-local window."""

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

    client, doctor, (_owner_a, subject_a), (_owner_b, subject_b) = doctor_client

    opened = await _open_professional_conversation(
        client, db_session, subject=subject_a, professional=doctor
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


async def test_opening_the_stable_conversation_is_idempotent_and_message_free(
    doctor_client, db_session
):
    from vitals.models.care_thread import CareMessage, CareThread

    client, doctor, (_owner_a, subject_a), _b = doctor_client
    first = await _open_professional_conversation(
        client, db_session, subject=subject_a, professional=doctor
    )
    second = await _open_professional_conversation(
        client, db_session, subject=subject_a, professional=doctor
    )

    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"]
    assert await db_session.scalar(select(func.count()).select_from(CareThread)) == 1
    assert await db_session.scalar(select(func.count()).select_from(CareMessage)) == 0


async def test_the_patient_sees_new_until_the_conversation_is_opened(
    doctor_client, db_session
):
    client, doctor, (owner_a, subject_a), _b = doctor_client
    opened = await _open_professional_conversation(
        client, db_session, subject=subject_a, professional=doctor
    )
    assert opened.status_code == 303
    thread_id = opened.headers["location"].rsplit("/", 1)[1]
    await client.post(
        f"/care/{subject_a.id}/messages/{thread_id}",
        data={"body": "Please fast for twelve hours."},
        follow_redirects=False,
    )

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
    client, doctor, (_owner_a, subject_a), (_owner_b, subject_b) = doctor_client

    opened = await _open_professional_conversation(
        client, db_session, subject=subject_a, professional=doctor
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


async def test_patient_cannot_start_an_owner_only_conversation(
    doctor_client, db_session
):
    """The inbox never promises a care-team recipient it cannot add."""

    from vitals.models.care_thread import CareThread
    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client, _doctor, (owner, subject), _b = doctor_client
    client.cookies.set(SESSION_COOKIE, create_session(owner.username))

    inbox = await client.get(
        f"/care/{subject.id}/messages", headers={"Accept": "text/html"}
    )
    assert inbox.status_code == 200
    assert f'action="/care/{subject.id}/messages"' not in inbox.text
    assert "Start conversation" not in inbox.text
    assert "Начать разговор" not in inbox.text

    direct = await client.post(
        f"/care/{subject.id}/messages",
        data={"title": "Unaddressed", "body": "Can anyone see this?"},
        follow_redirects=False,
    )
    assert direct.status_code == 405
    assert await db_session.scalar(select(func.count()).select_from(CareThread)) == 0


async def test_patient_opens_the_exact_professionals_shared_conversation(
    doctor_client, db_session
):
    from vitals.models.care_thread import CareThreadParticipant
    from vitals.models.professional import CareRelationship
    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client, doctor, (owner, subject), _b = doctor_client
    relationship_id = await db_session.scalar(
        select(CareRelationship.id).where(
            CareRelationship.subject_id == subject.id,
            CareRelationship.professional_user_id == doctor.id,
        )
    )
    client.cookies.set(SESSION_COOKIE, create_session(owner.username))

    team = await client.get("/settings/care", headers={"Accept": "text/html"})
    assert team.status_code == 200
    assert f'action="/messages/relationship/{relationship_id}"' in team.text

    opened = await client.post(
        f"/messages/relationship/{relationship_id}",
        follow_redirects=False,
    )
    assert opened.status_code == 303
    thread_id = uuid.UUID(opened.headers["location"].rsplit("/", 1)[1])
    participants = set(
        await db_session.scalars(
            select(CareThreadParticipant.user_id).where(
                CareThreadParticipant.thread_id == thread_id,
                CareThreadParticipant.removed_at.is_(None),
            )
        )
    )
    assert participants == {owner.id, doctor.id}
    inbox = await client.get(
        f"/care/{subject.id}/messages", headers={"Accept": "text/html"}
    )
    assert (
        "Conversation with Dr Human Name" in inbox.text
        or "Переписка с Dr Human Name" in inbox.text
    )

    client.cookies.set(SESSION_COOKIE, create_session(doctor.username))
    reopened = await client.post(
        f"/care/{subject.id}/messages/relationship/{relationship_id}",
        follow_redirects=False,
    )
    assert reopened.status_code == 303
    assert reopened.headers["location"] == opened.headers["location"]
    shared = await client.get(
        opened.headers["location"], headers={"Accept": "text/html"}
    )
    assert shared.status_code == 200


async def test_owner_record_does_not_offer_professional_write_forms(
    client, legacy_owner_roots
):
    """Self-access may read professional artifacts but cannot author them."""

    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client.cookies.set(SESSION_COOKIE, create_session("tester"))
    response = await client.get(
        f"/care/{legacy_owner_roots.subject_id}",
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 200
    assert f'action="/care/{legacy_owner_roots.subject_id}/note"' not in response.text
    assert f'action="/care/{legacy_owner_roots.subject_id}/plan"' not in response.text
    assert 'href="/settings/care"' in response.text
    assert "Back to care team" in response.text or "К команде помощи" in response.text
    assert 'href="/messages"' in response.text


async def test_narrow_consent_does_not_present_hidden_artifacts_as_empty(
    doctor_client, db_session
):
    """Unreadable notes and plans are withheld, not reported as nonexistent."""

    from vitals.models.professional import CarePlan
    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client, doctor, (owner, subject), _b = doctor_client
    relationship_id = await _relationship_id(
        db_session, subject=subject, professional=doctor
    )

    note_body = "A note that narrower consent must not describe as absent."
    plan_title = "A plan outside the narrower consent"
    assert (
        await client.post(
            f"/care/{subject.id}/note",
            data={"body": note_body},
            follow_redirects=False,
        )
    ).status_code == 303
    assert (
        await client.post(
            f"/care/{subject.id}/plan",
            data={
                "title": plan_title,
                "body": "Existing guidance remains in history.",
                "effective_from": "2026-08-27",
            },
            follow_redirects=False,
        )
    ).status_code == 303
    assert await db_session.scalar(
        select(func.count()).select_from(ProfessionalNote)
    )
    assert await db_session.scalar(select(func.count()).select_from(CarePlan))

    client.cookies.set(SESSION_COOKIE, create_session(owner.username))
    narrowed = await client.post(
        f"/settings/care/{relationship_id}/grant",
        data={"custom": "1", "domains": "weight", "allow_messages": "1"},
        follow_redirects=False,
    )
    assert narrowed.status_code == 303

    client.cookies.set(SESSION_COOKIE, create_session(doctor.username))
    page = await client.get(
        f"/care/{subject.id}", headers={"Accept": "text/html"}
    )

    assert page.status_code == 200
    assert note_body not in page.text
    assert plan_title not in page.text
    assert "Nothing written yet." not in page.text
    assert "Пока ничего не записано." not in page.text
    assert "No plan yet." not in page.text
    assert "Плана пока нет." not in page.text


async def test_shared_care_pages_keep_role_relative_links_and_navigation(
    doctor_client, db_session
):
    """The path is shared; the resolved reader decides the surrounding shell."""

    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client, doctor, (owner, subject), _b = doctor_client

    professional_record = await client.get(
        f"/care/{subject.id}", headers={"Accept": "text/html"}
    )
    assert professional_record.status_code == 200
    assert 'href="/care" class="v-btn-ghost text-xs"' in professional_record.text
    relationship_id = await _relationship_id(
        db_session, subject=subject, professional=doctor
    )
    assert (
        f'action="/care/{subject.id}/messages/relationship/{relationship_id}"'
        in professional_record.text
    )
    assert re.search(
        r'<a href="/care" class="mh-rail-foot-link\s+is-active" aria-current="page">',
        professional_record.text,
    )
    # This fixture is deliberately dual-role, so its personal phone bar must
    # still identify the professional workspace as a destination under More.
    assert re.search(
        r'<a href="/more" class="v-bnav-link\s+is-active" aria-current="page">',
        professional_record.text,
    )

    opened = await _open_professional_conversation(
        client, db_session, subject=subject, professional=doctor
    )
    client.cookies.set(SESSION_COOKIE, create_session(owner.username))
    patient_thread = await client.get(
        "/messages", headers={"Accept": "text/html"}, follow_redirects=True
    )

    assert patient_thread.status_code == 200
    assert str(patient_thread.url).endswith(f"/care/{subject.id}/messages")
    assert 'href="/settings/care" class="v-btn-ghost text-xs"' in patient_thread.text
    assert re.search(
        r'<a href="/messages" class="mh-rail-foot-link\s+is-active" aria-current="page">',
        patient_thread.text,
    )
    assert re.search(
        r'<a href="/more" class="v-bnav-link\s+is-active" aria-current="page">',
        patient_thread.text,
    )
    assert not re.search(
        r'<a href="/care" class="mh-rail-foot-link\s+is-active"',
        patient_thread.text,
    )
    assert opened.status_code == 303
    del doctor


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

    profile = await db_session.scalar(
        select(ProfessionalProfile).where(ProfessionalProfile.user_id == doctor.id)
    )
    profile.display_name = "Dr Conversation Name"
    await db_session.commit()

    opened = await _open_professional_conversation(
        client, db_session, subject=subject_a, professional=doctor
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
    assert "Care conversation" in page.text or "Общение со специалистом" in page.text
    assert "All conversations" in page.text or "Все разговоры" in page.text
    assert f'href="/care/{subject_a.id}" class="v-btn-ghost text-xs"' in page.text
    assert f'href="/care/{subject_a.id}/messages"' in page.text
    assert "Start conversation" not in page.text
    assert "Начать разговор" not in page.text
    assert "data-care-conversation" in page.text
    assert len(
        re.findall(r"<form[^>]+data-care-conversation-action", page.text)
    ) == 3
    fallback = page.text.split(
        "<template data-care-conversation-unavailable", 1
    )[1].split("</template>", 1)[0]
    assert (
        "Conversation unavailable" in fallback
        or "Переписка недоступна" in fallback
    )
    assert 'role="alert"' in fallback
    assert 'tabindex="-1"' in fallback
    assert 'href="/care"' in fallback
    assert str(subject_a.id) not in fallback
    assert thread_id not in fallback
    assert subject_a.display_name not in fallback
    assert "Please fast for twelve hours." not in fallback


async def test_the_patient_conversation_uses_the_owners_perspective(
    doctor_client, db_session
):
    """The shared screen speaks to its reader instead of calling them a patient."""

    client, doctor, (owner_a, subject_a), _b = doctor_client
    opened = await _open_professional_conversation(
        client, db_session, subject=subject_a, professional=doctor
    )
    thread_id = opened.headers["location"].rsplit("/", 1)[1]
    await client.post(
        f"/care/{subject_a.id}/messages/{thread_id}",
        data={"body": "How are you feeling?"},
        follow_redirects=False,
    )

    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client.cookies.set(SESSION_COOKIE, create_session(owner_a.username))
    replied = await client.post(
        f"/care/{subject_a.id}/messages/{thread_id}",
        data={"body": "Much better."},
        follow_redirects=False,
    )
    assert replied.status_code == 303

    page = await client.get(
        f"/care/{subject_a.id}/messages/{thread_id}",
        headers={"Accept": "text/html"},
    )
    assert page.status_code == 200
    assert "Care team" in page.text or "Команда помощи" in page.text
    assert (
        "Write a message" in page.text
        or "Напишите сообщение" in page.text
    )
    assert "Write to the patient" not in page.text
    assert "Напишите пациенту" not in page.text
    assert 'data-message-own="true"' in page.text
    assert 'data-message-own="false"' in page.text
    assert 'href="/settings/care" class="v-btn-ghost text-xs"' in page.text
    assert 'href="/messages" class="v-btn-ghost text-xs"' in page.text
    fallback = page.text.split(
        "<template data-care-conversation-unavailable", 1
    )[1].split("</template>", 1)[0]
    assert 'href="/messages"' in fallback
    assert str(subject_a.id) not in fallback
    assert thread_id not in fallback
    assert "How are you feeling?" not in fallback
    assert "Much better." not in fallback


async def test_message_corrections_and_thread_state_are_shared_actions(
    doctor_client, db_session
):
    """Both sides use one screen, but only the author can correct their row."""

    from vitals.models.care_thread import CareMessage, CareThread
    from web.auth import create_session
    from web.config import SESSION_COOKIE

    client, doctor, (owner, subject), _b = doctor_client
    subject_id = subject.id
    owner_username = owner.username
    doctor_username = doctor.username
    opened = await _open_professional_conversation(
        client, db_session, subject=subject, professional=doctor
    )
    thread_id = uuid.UUID(opened.headers["location"].rsplit("/", 1)[1])
    assert (
        await client.post(
            f"/care/{subject_id}/messages/{thread_id}",
            data={"body": "Take this in the morning."},
            follow_redirects=False,
        )
    ).status_code == 303

    doctor_message = await db_session.scalar(
        select(CareMessage).where(CareMessage.thread_id == thread_id)
    )
    assert doctor_message is not None
    client.cookies.set(SESSION_COOKIE, create_session(owner_username))
    assert (
        await client.post(
            f"/care/{subject_id}/messages/{thread_id}",
            data={"body": "Understood."},
            follow_redirects=False,
        )
    ).status_code == 303
    patient_message = await db_session.scalar(
        select(CareMessage).where(
            CareMessage.thread_id == thread_id,
            CareMessage.actor_user_id == owner.id,
        )
    )
    assert patient_message is not None
    doctor_message_id = doctor_message.id
    patient_message_id = patient_message.id
    patient_page = await client.get(
        f"/care/{subject_id}/messages/{thread_id}",
        headers={"Accept": "text/html"},
    )
    assert patient_page.status_code == 200
    assert patient_page.text.count("data-care-message-editor") == 1
    assert f"/messages/{patient_message_id}/revise" in patient_page.text
    assert f"/messages/{doctor_message_id}/revise" not in patient_page.text

    client.cookies.set(SESSION_COOKIE, create_session(doctor_username))
    page = await client.get(
        f"/care/{subject_id}/messages/{thread_id}",
        headers={"Accept": "text/html"},
    )
    assert page.status_code == 200
    assert page.text.count("data-care-message-editor") == 1
    own_action = (
        f"/care/{subject_id}/messages/{thread_id}/messages/"
        f"{doctor_message_id}/revise"
    )
    other_action = (
        f"/care/{subject_id}/messages/{thread_id}/messages/"
        f"{patient_message_id}/revise"
    )
    assert f'action="{own_action}"' in page.text
    assert f'action="{other_action}"' not in page.text
    assert 'data-care-thread-action="close"' in page.text

    corrected = await client.post(
        own_action,
        data={"body": "Take this with breakfast."},
        follow_redirects=False,
    )
    assert corrected.status_code == 303
    await db_session.refresh(doctor_message)
    assert doctor_message.body == "Take this with breakfast."
    assert doctor_message.edited_at is not None

    not_the_author = await client.post(
        other_action,
        data={"body": "A professional cannot rewrite this."},
        follow_redirects=False,
    )
    assert not_the_author.status_code == 404
    await db_session.refresh(patient_message)
    assert patient_message.body == "Understood."

    closed = await client.post(
        f"/care/{subject_id}/messages/{thread_id}/close",
        follow_redirects=False,
    )
    assert closed.status_code == 303
    conversation = await db_session.get(CareThread, thread_id)
    await db_session.refresh(conversation)
    assert conversation.status == "closed"
    page = await client.get(
        f"/care/{subject_id}/messages/{thread_id}",
        headers={"Accept": "text/html"},
    )
    assert page.status_code == 200
    assert 'data-care-thread-action="reopen"' in page.text
    assert "data-care-message-editor" not in page.text

    stale_edit = await client.post(
        own_action,
        data={"body": "This closed history must not change."},
        follow_redirects=False,
    )
    assert stale_edit.status_code == 303
    assert stale_edit.headers["location"].endswith("?state_changed=1")
    await db_session.refresh(doctor_message)
    assert doctor_message.body == "Take this with breakfast."

    reopened = await client.post(
        f"/care/{subject_id}/messages/{thread_id}/reopen",
        follow_redirects=False,
    )
    assert reopened.status_code == 303
    await db_session.refresh(conversation)
    assert conversation.status == "open"


async def test_message_actions_recheck_csrf_and_live_consent(
    doctor_client, db_session
):
    from vitals.models.care_thread import CareMessage, CareThread

    client, doctor, (owner, subject), _b = doctor_client
    subject_id = subject.id
    doctor_id = doctor.id
    owner_id = owner.id
    opened = await _open_professional_conversation(
        client, db_session, subject=subject, professional=doctor
    )
    thread_id = uuid.UUID(opened.headers["location"].rsplit("/", 1)[1])
    await client.post(
        f"/care/{subject_id}/messages/{thread_id}",
        data={"body": "Original."},
        follow_redirects=False,
    )
    message = await db_session.scalar(
        select(CareMessage).where(CareMessage.thread_id == thread_id)
    )
    thread = await db_session.get(CareThread, thread_id)
    assert message is not None and thread is not None
    message_id = message.id

    forged = await client.post(
        f"/care/{subject_id}/messages/{thread_id}/close",
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert forged.status_code == 403
    await db_session.refresh(thread)
    assert thread.status == "open"

    relationship = await db_session.scalar(
        select(CareRelationship).where(
            CareRelationship.subject_id == subject_id,
            CareRelationship.professional_user_id == doctor_id,
        )
    )
    assert relationship is not None
    await relationships.set_consent_paused(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner_id,
        paused=True,
    )
    await db_session.commit()

    close_after_pause = await client.post(
        f"/care/{subject_id}/messages/{thread_id}/close",
        follow_redirects=False,
    )
    revise_after_pause = await client.post(
        f"/care/{subject_id}/messages/{thread_id}/messages/{message_id}/revise",
        data={"body": "Consent must win."},
        follow_redirects=False,
    )
    assert close_after_pause.status_code == 404
    assert revise_after_pause.status_code == 404
    await db_session.refresh(thread)
    await db_session.refresh(message)
    assert thread.status == "open"
    assert message.body == "Original."


async def test_patient_keeps_history_but_no_thread_actions_after_revocation(
    doctor_client,
    db_session,
    monkeypatch,
    tmp_path,
):
    """Ownership is a read basis, not an imaginary message recipient."""

    from vitals.models.care_thread import (
        CareMessage,
        CareMessageAttachment,
        CareThread,
    )
    from vitals.models.tenancy import FileAsset
    from web.auth import create_session
    from web.config import SESSION_COOKIE

    private_root = tmp_path / "private-care-files"
    monkeypatch.setenv("VITALS_PRIVATE_FILE_ROOT", str(private_root))
    client, doctor, (owner, subject), (_other_owner, other_subject) = doctor_client
    subject_id = subject.id
    other_subject_id = other_subject.id
    opened = await _open_professional_conversation(
        client,
        db_session,
        subject=subject,
        professional=doctor,
    )
    thread_id = uuid.UUID(opened.headers["location"].rsplit("/", 1)[1])
    assert (
        await client.post(
            f"/care/{subject_id}/messages/{thread_id}",
            data={"body": "Professional history."},
            follow_redirects=False,
        )
    ).status_code == 303

    client.cookies.set(SESSION_COOKIE, create_session(owner.username))
    assert (
        await client.post(
            f"/care/{subject_id}/messages/{thread_id}",
            data={"body": "Patient history."},
            follow_redirects=False,
        )
    ).status_code == 303
    patient_message = await db_session.scalar(
        select(CareMessage).where(
            CareMessage.thread_id == thread_id,
            CareMessage.actor_user_id == owner.id,
        )
    )
    relationship = await db_session.scalar(
        select(CareRelationship).where(
            CareRelationship.subject_id == subject_id,
            CareRelationship.professional_user_id == doctor.id,
        )
    )
    assert patient_message is not None and relationship is not None
    patient_message_id = patient_message.id
    await relationships.revoke_consent(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
    )
    await db_session.commit()

    page = await client.get(
        f"/care/{subject_id}/messages/{thread_id}",
        headers={"Accept": "text/html"},
    )
    assert page.status_code == 200
    assert "Professional history." in page.text
    assert "Patient history." in page.text
    assert re.findall(r"<form[^>]+data-care-conversation-action", page.text) == []
    assert re.findall(r"<form[^>]+data-care-thread-action", page.text) == []
    assert "data-care-message-editor" not in page.text
    assert 'id="message-body"' not in page.text

    before = {
        "messages": await db_session.scalar(
            select(func.count()).select_from(CareMessage)
        ),
        "attachments": await db_session.scalar(
            select(func.count()).select_from(CareMessageAttachment)
        ),
        "assets": await db_session.scalar(select(func.count()).select_from(FileAsset)),
    }
    headers = {"HX-Request": "true", "Accept": "text/html"}
    responses = [
        await client.post(
            f"/care/{subject_id}/messages/{thread_id}",
            data={"body": "UNSENT-PATIENT-TEXT"},
            headers=headers,
            follow_redirects=False,
        ),
        await client.post(
            f"/care/{subject_id}/messages/{thread_id}/messages/"
            f"{patient_message_id}/revise",
            data={"body": "UNCHANGED-PATIENT-TEXT"},
            headers=headers,
            follow_redirects=False,
        ),
        await client.post(
            f"/care/{subject_id}/messages/{thread_id}/close",
            headers=headers,
            follow_redirects=False,
        ),
        await client.post(
            f"/care/{subject_id}/messages/{thread_id}",
            data={"body": "UNSENT-PATIENT-ATTACHMENT"},
            files={
                "attachment": (
                    "synthetic.pdf",
                    b"%PDF-1.7\nsynthetic revoked upload\n%%EOF\n",
                    "application/pdf",
                )
            },
            headers=headers,
            follow_redirects=False,
        ),
        await client.post(
            f"/care/{subject_id}/messages/{uuid.uuid4()}",
            data={"body": "UNSENT-MISSING-THREAD"},
            headers=headers,
            follow_redirects=False,
        ),
        await client.post(
            f"/care/{other_subject_id}/messages/{thread_id}",
            data={"body": "UNSENT-FOREIGN-SUBJECT"},
            headers=headers,
            follow_redirects=False,
        ),
    ]
    assert all(response.status_code == 404 for response in responses)
    assert all(response.content == responses[0].content for response in responses)
    assert responses[0].json() == {"detail": "Not Found"}

    conversation = await db_session.get(CareThread, thread_id)
    assert conversation is not None
    await db_session.refresh(conversation)
    conversation.status = "closed"
    await db_session.commit()
    reopen = await client.post(
        f"/care/{subject_id}/messages/{thread_id}/reopen",
        headers=headers,
        follow_redirects=False,
    )
    assert reopen.status_code == 404
    assert reopen.content == responses[0].content

    await db_session.refresh(conversation)
    await db_session.refresh(patient_message)
    assert conversation.status == "closed"
    assert patient_message.body == "Patient history."
    assert patient_message.edited_at is None
    assert {
        "messages": await db_session.scalar(
            select(func.count()).select_from(CareMessage)
        ),
        "attachments": await db_session.scalar(
            select(func.count()).select_from(CareMessageAttachment)
        ),
        "assets": await db_session.scalar(select(func.count()).select_from(FileAsset)),
    } == before
    assert not private_root.exists()


async def test_stale_conversation_send_keeps_one_json_404_and_writes_nothing(
    doctor_client, db_session
):
    """HTMX gets the API boundary; trusted page markup explains it locally."""

    from vitals.models.care_thread import CareMessage

    client, doctor, (owner, subject), _b = doctor_client
    subject_id = subject.id
    doctor_id = doctor.id
    owner_id = owner.id
    opened = await _open_professional_conversation(
        client, db_session, subject=subject, professional=doctor
    )
    thread_id = uuid.UUID(opened.headers["location"].rsplit("/", 1)[1])
    sent = await client.post(
        f"/care/{subject.id}/messages/{thread_id}",
        data={"body": "Original."},
        follow_redirects=False,
    )
    assert sent.status_code == 303

    relationship = await db_session.scalar(
        select(CareRelationship).where(
            CareRelationship.subject_id == subject_id,
            CareRelationship.professional_user_id == doctor_id,
        )
    )
    assert relationship is not None
    relationship_id = relationship.id
    await relationships.set_consent_paused(
        db_session,
        relationship_id=relationship_id,
        actor_user_id=owner_id,
        paused=True,
    )
    await db_session.commit()

    htmx_headers = {"HX-Request": "true", "Accept": "text/html"}
    paused = await client.post(
        f"/care/{subject_id}/messages/{thread_id}",
        data={"body": "UNSENT-PAUSED-CLINICAL-TEXT"},
        headers=htmx_headers,
        follow_redirects=False,
    )
    assert paused.status_code == 404
    assert paused.headers["content-type"].startswith("application/json")
    assert paused.json() == {"detail": "Not Found"}
    assert b"UNSENT-PAUSED-CLINICAL-TEXT" not in paused.content
    assert str(subject_id).encode() not in paused.content

    # Restoring consent proves a missing thread reaches a different service
    # boundary but remains indistinguishable from the paused relationship.
    await relationships.set_consent_paused(
        db_session,
        relationship_id=relationship_id,
        actor_user_id=owner_id,
        paused=False,
    )
    await db_session.commit()
    missing = await client.post(
        f"/care/{subject_id}/messages/{uuid.uuid4()}",
        data={"body": "UNSENT-MISSING-THREAD"},
        headers=htmx_headers,
        follow_redirects=False,
    )
    assert missing.status_code == 404
    assert missing.content == paused.content

    resumed = await client.post(
        f"/care/{subject_id}/messages/{thread_id}",
        data={"body": "After resume."},
        follow_redirects=False,
    )
    assert resumed.status_code == 303

    await relationships.revoke_consent(
        db_session,
        relationship_id=relationship_id,
        actor_user_id=owner_id,
    )
    await db_session.commit()
    revoked = await client.post(
        f"/care/{subject_id}/messages/{thread_id}",
        data={"body": "UNSENT-REVOKED-CLINICAL-TEXT"},
        headers=htmx_headers,
        follow_redirects=False,
    )
    api = await client.post(
        f"/care/{subject_id}/messages/{thread_id}",
        data={"body": "UNSENT-API-TEXT"},
        follow_redirects=False,
    )
    assert revoked.status_code == api.status_code == 404
    assert revoked.content == api.content == paused.content

    messages = (
        await db_session.execute(
            select(CareMessage).where(CareMessage.thread_id == thread_id)
        )
    ).scalars().all()
    assert len(messages) == 2
    assert {message.body for message in messages} == {"Original.", "After resume."}


async def test_the_conversation_list_renders(doctor_client, db_session):
    client, doctor, (_owner_a, subject_a), _b = doctor_client

    await _open_professional_conversation(
        client, db_session, subject=subject_a, professional=doctor
    )
    page = await client.get(
        f"/care/{subject_a.id}/messages", headers={"Accept": "text/html"}
    )
    assert page.status_code == 200
    assert "Care conversation" in page.text or "Общение со специалистом" in page.text
    assert "Start conversation" not in page.text
    assert "Начать разговор" not in page.text


async def test_old_topic_threads_remain_history_without_a_new_topic_form(
    doctor_client, db_session
):
    from vitals.services.authorization.subject_access import resolve_access_context
    from vitals.services.care import threads

    client, doctor, (_owner_a, subject_a), _b = doctor_client
    context = await resolve_access_context(
        db_session, user_id=doctor.id, subject_id=subject_a.id
    )
    legacy = await threads.open_thread(
        db_session, context=context, title="Earlier ferritin follow-up"
    )
    await threads.send_message(
        db_session,
        context=context,
        thread_id=legacy.id,
        body="This historical message must remain readable.",
    )
    await db_session.commit()
    canonical = await _open_professional_conversation(
        client, db_session, subject=subject_a, professional=doctor
    )
    assert canonical.status_code == 303

    page = await client.get(
        f"/care/{subject_a.id}/messages", headers={"Accept": "text/html"}
    )
    assert page.status_code == 200
    assert "Earlier ferritin follow-up" in page.text
    assert (
        "Earlier topic-based conversations remain here as readable history."
        in page.text
        or "Предыдущие тематические разговоры" in page.text
    )
    assert 'name="title"' not in page.text

    old_direct = await client.post(
        f"/care/{subject_a.id}/messages",
        data={"title": "A topic that must not be created"},
        follow_redirects=False,
    )
    assert old_direct.status_code == 405
    historical_page = await client.get(
        f"/care/{subject_a.id}/messages/{legacy.id}",
        headers={"Accept": "text/html"},
    )
    assert historical_page.status_code == 200
    assert "This historical message must remain readable." in historical_page.text


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
    doctor_id = doctor.id
    owner_a_id = owner_a.id
    owner_a_username = owner_a.username
    subject_a_id = subject_a.id
    subject_b_id = subject_b.id
    payload = b"%PDF-1.7\nsynthetic care document\n%%EOF\n"

    opened = await _open_professional_conversation(
        client, db_session, subject=subject_a, professional=doctor
    )
    assert opened.status_code == 303
    thread_id = opened.headers["location"].rsplit("/", 1)[1]
    sent = await client.post(
        f"/care/{subject_a_id}/messages/{thread_id}",
        data={"body": "Please review this result."},
        files={
            # The claimed content type is deliberately hostile. The server
            # derives its response type from validated content and extension.
            "attachment": ("synthetic-result.pdf", payload, "text/html"),
        },
        follow_redirects=False,
    )
    assert sent.status_code == 303

    attachment = await db_session.scalar(select(CareMessageAttachment))
    assert attachment is not None
    attachment_id = attachment.id
    asset = await db_session.get(FileAsset, attachment.file_asset_id)
    assert asset is not None
    assert asset.subject_id == subject_a_id
    assert asset.storage_backend == "private_local"
    assert asset.status == "active"
    assert asset.purpose == "care_message_attachment"
    assert asset.byte_size == len(payload)
    assert len(asset.sha256_hex or "") == 64
    assert asset.storage_ref.startswith("care/")
    assert subject_a_id.hex not in asset.storage_ref
    path = private_file_disk_path(str(private_root), asset.storage_ref)
    assert Path(path).read_bytes() == payload

    page = await client.get(
        f"/care/{subject_a_id}/messages/{thread_id}",
        headers={"Accept": "text/html"},
    )
    assert page.status_code == 200
    assert "synthetic-result.pdf" in page.text

    download_url = (
        f"/care/{subject_a_id}/messages/{thread_id}/attachments/{attachment_id}"
    )
    downloaded = await client.get(download_url)
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    assert downloaded.headers["content-type"].startswith("application/pdf")
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert "attachment" in downloaded.headers["content-disposition"]

    other_thread_id = uuid.uuid4()
    wrong_thread = await client.get(
        f"/care/{subject_a_id}/messages/{other_thread_id}/attachments/{attachment_id}"
    )
    assert wrong_thread.status_code == 404

    # The same opaque attachment under another patient is indistinguishable
    # from one that does not exist.
    wrong_subject = await client.get(
        f"/care/{subject_b_id}/messages/{thread_id}/attachments/{attachment_id}"
    )
    assert wrong_subject.status_code == 404

    relationship = await db_session.scalar(
        select(CareRelationship).where(
            CareRelationship.subject_id == subject_a_id,
            CareRelationship.professional_user_id == doctor_id,
        )
    )
    assert relationship is not None
    await relationships.set_consent_paused(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner_a_id,
        paused=True,
    )
    await db_session.commit()
    assert (await client.get(download_url)).status_code == 404

    # Withdrawing professional access never withdraws the patient's own record.
    client.cookies.set(SESSION_COOKIE, create_session(owner_a_username))
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
    client, doctor, (_owner_a, subject_a), _b = doctor_client
    opened = await _open_professional_conversation(
        client, db_session, subject=subject_a, professional=doctor
    )
    assert opened.status_code == 303
    thread_id = opened.headers["location"].rsplit("/", 1)[1]
    threads_before = await db_session.scalar(
        select(func.count()).select_from(CareThread)
    )
    messages_before = await db_session.scalar(
        select(func.count()).select_from(CareMessage)
    )
    response = await client.post(
        f"/care/{subject_a.id}/messages/{thread_id}",
        data={"body": "This must not persist."},
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
