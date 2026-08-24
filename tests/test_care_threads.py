"""The patient is in the room, and cannot be taken out of it.

This is the safest useful communication model and the reason it was chosen over
a private clinical channel: a conversation about somebody that they can read is
one they can correct, and one nobody has to be trusted to summarise. The tests
here are mostly about the ways that could stop being true.

Four rules, one section each. The subject participates in every thread about
them and cannot be removed. Being in the room is a row and is not permission.
Reading and sending are separately revocable. Nothing is deleted.
"""

from __future__ import annotations

import uuid

import pytest

from vitals.access import AccessScope, PolicyAction, PolicyResourceType
from vitals.enums import (
    CareThreadStatus,
    ProfessionalKind,
    UserRoleName,
    UserStatus,
)
from vitals.models.care_thread import CareThreadParticipant
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.services import care_service, invitation_service
from vitals.services import care_thread_service as threads
from vitals.services.access_resolution import resolve_access_context


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


async def _take_into_care(
    session, *, subject, owner, slug: str, kind=ProfessionalKind.DOCTOR, scopes=None
):
    professional = await _user(
        session,
        slug,
        roles=(
            UserRoleName.DOCTOR
            if kind is ProfessionalKind.DOCTOR
            else UserRoleName.TRAINER,
        ),
    )
    issued = await invitation_service.invite(
        session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=kind,
        email=f"{slug}@example.test",
    )
    await invitation_service.accept(
        session,
        token=issued.token,
        accepting_user_id=professional.id,
        verified_email=f"{slug}@example.test",
    )
    relationship = await care_service.establish_from_invitation(
        session, invitation=issued.invitation
    )
    grant = await care_service.grant_consent(
        session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
        scopes=scopes,
    )
    return professional, relationship, grant


async def _patient(session, slug: str):
    owner = await _user(session, slug)
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name=f"Synthetic {slug}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return owner, subject


async def _context(session, user, subject):
    return await resolve_access_context(
        session, user_id=user.id, subject_id=subject.id
    )


# ── The patient is in the room ───────────────────────────────────────────────


async def test_a_thread_a_professional_opens_has_the_patient_in_it(db_session):
    """The difference between this feature and a hidden clinical channel."""

    owner, subject = await _patient(db_session, "thread-open")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-open-doc"
    )
    context = await _context(db_session, doctor, subject)

    thread = await threads.open_thread(db_session, context=context, title="Bloods")
    await db_session.commit()

    _t, _messages, participants = await threads.read_thread(
        db_session, context=context, thread_id=thread.id
    )
    assert {p.user_id for p in participants} == {owner.id, doctor.id}
    # The patient is in it as its subject, under no relationship; the doctor is
    # in it because of one.
    by_user = {p.user_id: p for p in participants}
    assert by_user[owner.id].relationship_id is None
    assert by_user[doctor.id].relationship_id is not None


async def test_the_patient_reads_it_without_any_consent_at_all(db_session):
    """Self-ownership is its own basis, which is what patient-visible means."""

    owner, subject = await _patient(db_session, "thread-self")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-self-doc"
    )
    doctor_context = await _context(db_session, doctor, subject)
    thread = await threads.open_thread(
        db_session, context=doctor_context, title="Bloods"
    )
    await threads.send_message(
        db_session, context=doctor_context, thread_id=thread.id, body="Please fast."
    )
    await db_session.commit()

    owner_context = await _context(db_session, owner, subject)
    _t, messages, _participants = await threads.read_thread(
        db_session, context=owner_context, thread_id=thread.id
    )
    assert [m.body for m in messages] == ["Please fast."]


async def test_the_patient_cannot_be_removed_by_anybody(db_session):
    """Including themselves. A thread about somebody they cannot read is the
    thing this feature exists not to be."""

    owner, subject = await _patient(db_session, "thread-keep")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-keep-doc"
    )
    doctor_context = await _context(db_session, doctor, subject)
    thread = await threads.open_thread(
        db_session, context=doctor_context, title="Bloods"
    )
    await db_session.commit()

    for actor in (doctor, owner):
        context = await _context(db_session, actor, subject)
        with pytest.raises(threads.CareThreadValidationError):
            await threads.remove_participant(
                db_session,
                context=context,
                thread_id=thread.id,
                user_id=owner.id,
            )


async def test_a_second_professional_joins_only_through_live_care(db_session):
    owner, subject = await _patient(db_session, "thread-join")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-join-doc"
    )
    trainer, _trel, _tgrant = await _take_into_care(
        db_session,
        subject=subject,
        owner=owner,
        slug="thread-join-coach",
        kind=ProfessionalKind.TRAINER,
    )
    stranger = await _user(db_session, "thread-join-stranger")
    await db_session.commit()

    context = await _context(db_session, doctor, subject)
    thread = await threads.open_thread(db_session, context=context, title="Plan")

    await threads.add_participant(
        db_session, context=context, thread_id=thread.id, user_id=trainer.id
    )
    with pytest.raises(threads.NotInTheConversation):
        await threads.add_participant(
            db_session, context=context, thread_id=thread.id, user_id=stranger.id
        )
    await db_session.commit()

    _t, _m, participants = await threads.read_thread(
        db_session, context=context, thread_id=thread.id
    )
    assert {p.user_id for p in participants} == {owner.id, doctor.id, trainer.id}


# ── Being in the room is not permission ──────────────────────────────────────


async def test_a_paused_consent_stops_the_conversation_without_deleting_it(
    db_session,
):
    """The whole value of the patient holding the switch."""

    owner, subject = await _patient(db_session, "thread-pause")
    doctor, relationship, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-pause-doc"
    )
    context = await _context(db_session, doctor, subject)
    thread = await threads.open_thread(db_session, context=context, title="Bloods")
    await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="Before."
    )
    await db_session.commit()

    await care_service.set_consent_paused(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
        paused=True,
    )
    await db_session.commit()

    paused = await _context(db_session, doctor, subject)
    with pytest.raises(threads.NotInTheConversation):
        await threads.send_message(
            db_session, context=paused, thread_id=thread.id, body="After."
        )
    with pytest.raises(threads.NotInTheConversation):
        await threads.read_thread(db_session, context=paused, thread_id=thread.id)

    # And the patient still has every word of it.
    owner_context = await _context(db_session, owner, subject)
    _t, messages, _p = await threads.read_thread(
        db_session, context=owner_context, thread_id=thread.id
    )
    assert [m.body for m in messages] == ["Before."]


async def test_a_removed_professional_cannot_read_or_send(db_session):
    owner, subject = await _patient(db_session, "thread-removed")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-removed-doc"
    )
    trainer, _trel, _tgrant = await _take_into_care(
        db_session,
        subject=subject,
        owner=owner,
        slug="thread-removed-coach",
        kind=ProfessionalKind.TRAINER,
    )
    context = await _context(db_session, doctor, subject)
    thread = await threads.open_thread(db_session, context=context, title="Plan")
    await threads.add_participant(
        db_session, context=context, thread_id=thread.id, user_id=trainer.id
    )
    await db_session.commit()

    await threads.remove_participant(
        db_session, context=context, thread_id=thread.id, user_id=trainer.id
    )
    await db_session.commit()

    trainer_context = await _context(db_session, trainer, subject)
    with pytest.raises(threads.NotInTheConversation):
        await threads.read_thread(
            db_session, context=trainer_context, thread_id=thread.id
        )
    with pytest.raises(threads.NotInTheConversation):
        await threads.send_message(
            db_session, context=trainer_context, thread_id=thread.id, body="hello"
        )
    # The row stays: who was in the room when a thing was said is part of the
    # record.
    assert (
        await db_session.scalar(
            CareThreadParticipant.__table__.select().where(
                CareThreadParticipant.thread_id == thread.id,
                CareThreadParticipant.user_id == trainer.id,
            )
        )
    ) is not None


async def test_one_professional_does_not_see_another_conversation(db_session):
    """A thread is not "everything about this patient"."""

    owner, subject = await _patient(db_session, "thread-private")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-private-doc"
    )
    trainer, _trel, _tgrant = await _take_into_care(
        db_session,
        subject=subject,
        owner=owner,
        slug="thread-private-coach",
        kind=ProfessionalKind.TRAINER,
    )
    doctor_context = await _context(db_session, doctor, subject)
    private = await threads.open_thread(
        db_session, context=doctor_context, title="Between us two"
    )
    await db_session.commit()

    trainer_context = await _context(db_session, trainer, subject)
    assert await threads.list_threads(db_session, context=trainer_context) == []
    with pytest.raises(threads.NotInTheConversation):
        await threads.read_thread(
            db_session, context=trainer_context, thread_id=private.id
        )

    # The patient sees it, because the patient is in it.
    owner_context = await _context(db_session, owner, subject)
    assert [t.id for t in await threads.list_threads(
        db_session, context=owner_context
    )] == [private.id]


async def test_another_patients_thread_id_finds_nothing(db_session):
    """A known identifier is never enough."""

    owner_a, subject_a = await _patient(db_session, "thread-idor-a")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject_a, owner=owner_a, slug="thread-idor-doc"
    )
    context_a = await _context(db_session, doctor, subject_a)
    thread = await threads.open_thread(db_session, context=context_a, title="A")

    owner_b, subject_b = await _patient(db_session, "thread-idor-b")
    _doctor_b, _relb, _grantb = await _take_into_care(
        db_session, subject=subject_b, owner=owner_b, slug="thread-idor-doc-b"
    )
    await db_session.commit()

    owner_b_context = await _context(db_session, owner_b, subject_b)
    with pytest.raises(threads.ThreadNotFound):
        await threads.read_thread(
            db_session, context=owner_b_context, thread_id=thread.id
        )


# ── Reading and sending are separately revocable ─────────────────────────────


def _scopes_without(action: PolicyAction) -> frozenset[AccessScope]:
    return frozenset(
        scope
        for scope in care_service.default_scopes(ProfessionalKind.DOCTOR)
        if not (
            scope.resource_type is PolicyResourceType.OPERATION
            and scope.resource_key == threads.MESSAGE_OPERATION
            and scope.action is action
        )
    )


async def test_a_consent_that_reads_but_does_not_send(db_session):
    """The narrowing worth being able to express."""

    owner, subject = await _patient(db_session, "thread-readonly")
    doctor, _rel, _grant = await _take_into_care(
        db_session,
        subject=subject,
        owner=owner,
        slug="thread-readonly-doc",
    )
    context = await _context(db_session, doctor, subject)
    thread = await threads.open_thread(db_session, context=context, title="Bloods")
    await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="While allowed."
    )
    await db_session.commit()

    # The patient narrows it: keep looking, stop writing.
    await care_service.revoke_consent(
        db_session, relationship_id=_rel.id, actor_user_id=owner.id
    )
    await care_service.grant_consent(
        db_session,
        relationship_id=_rel.id,
        actor_user_id=owner.id,
        scopes=_scopes_without(PolicyAction.MESSAGE),
    )
    await db_session.commit()

    narrowed = await _context(db_session, doctor, subject)
    _t, messages, _p = await threads.read_thread(
        db_session, context=narrowed, thread_id=thread.id
    )
    assert [m.body for m in messages] == ["While allowed."]
    with pytest.raises(threads.NotInTheConversation):
        await threads.send_message(
            db_session, context=narrowed, thread_id=thread.id, body="Not any more."
        )


async def test_a_consent_without_the_operation_at_all_opens_nothing(db_session):
    owner, subject = await _patient(db_session, "thread-none")
    doctor, _rel, _grant = await _take_into_care(
        db_session,
        subject=subject,
        owner=owner,
        slug="thread-none-doc",
        scopes=_scopes_without(PolicyAction.MESSAGE)
        & _scopes_without(PolicyAction.READ),
    )
    context = await _context(db_session, doctor, subject)
    with pytest.raises(threads.NotInTheConversation):
        await threads.open_thread(db_session, context=context, title="Nope")


async def test_the_default_consent_carries_the_conversation(db_session):
    """A care team that cannot talk to the patient is not what one is invited for."""

    scopes = care_service.default_scopes(ProfessionalKind.DOCTOR)
    conversation = {
        scope.action
        for scope in scopes
        if scope.resource_type is PolicyResourceType.OPERATION
        and scope.resource_key == threads.MESSAGE_OPERATION
    }
    assert conversation == {PolicyAction.READ, PolicyAction.MESSAGE}


# ── Nothing is deleted ───────────────────────────────────────────────────────


async def test_a_correction_keeps_its_author_and_says_it_changed(db_session):
    owner, subject = await _patient(db_session, "thread-edit")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-edit-doc"
    )
    context = await _context(db_session, doctor, subject)
    thread = await threads.open_thread(db_session, context=context, title="Bloods")
    message = await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="Fast for 8 hours."
    )
    await db_session.commit()
    assert message.edited_at is None

    revised = await threads.revise_message(
        db_session,
        context=context,
        message_id=message.id,
        body="Fast for 12 hours.",
    )
    await db_session.commit()
    assert revised.id == message.id
    assert revised.actor_user_id == doctor.id
    assert revised.edited_at is not None


async def test_only_the_author_may_correct_it(db_session):
    """Not the patient, not another professional. A message somebody else can
    edit is not that person's message."""

    owner, subject = await _patient(db_session, "thread-author")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-author-doc"
    )
    context = await _context(db_session, doctor, subject)
    thread = await threads.open_thread(db_session, context=context, title="Bloods")
    message = await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="Mine."
    )
    await db_session.commit()

    owner_context = await _context(db_session, owner, subject)
    with pytest.raises(threads.NotTheAuthor):
        await threads.revise_message(
            db_session,
            context=owner_context,
            message_id=message.id,
            body="Not yours to change.",
        )


async def test_a_closed_thread_is_read_only_and_still_there(db_session):
    owner, subject = await _patient(db_session, "thread-close")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-close-doc"
    )
    context = await _context(db_session, doctor, subject)
    thread = await threads.open_thread(db_session, context=context, title="Bloods")
    await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="Said."
    )
    await threads.close_thread(db_session, context=context, thread_id=thread.id)
    await db_session.commit()

    assert thread.status == CareThreadStatus.CLOSED.value
    with pytest.raises(threads.NotInTheConversation):
        await threads.send_message(
            db_session, context=context, thread_id=thread.id, body="More."
        )
    _t, messages, _p = await threads.read_thread(
        db_session, context=context, thread_id=thread.id
    )
    assert [m.body for m in messages] == ["Said."]

    await threads.reopen_thread(db_session, context=context, thread_id=thread.id)
    await db_session.commit()
    await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="More."
    )
    await db_session.commit()


async def test_a_blank_or_enormous_message_is_refused(db_session):
    owner, subject = await _patient(db_session, "thread-body")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-body-doc"
    )
    context = await _context(db_session, doctor, subject)
    thread = await threads.open_thread(db_session, context=context, title="Bloods")
    await db_session.commit()

    for body in ("", "   ", "x" * 20001, None, 7):
        with pytest.raises(threads.CareThreadValidationError):
            await threads.send_message(
                db_session, context=context, thread_id=thread.id, body=body
            )


async def test_a_thread_needs_a_title_somebody_can_read(db_session):
    owner, subject = await _patient(db_session, "thread-title")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-title-doc"
    )
    context = await _context(db_session, doctor, subject)
    for title in ("", "  ", "x" * 201, None):
        with pytest.raises(threads.CareThreadValidationError):
            await threads.open_thread(db_session, context=context, title=title)


async def test_a_random_thread_id_is_not_found_rather_than_crashing(db_session):
    owner, subject = await _patient(db_session, "thread-missing")
    context = await _context(db_session, owner, subject)
    with pytest.raises(threads.ThreadNotFound):
        await threads.read_thread(
            db_session, context=context, thread_id=uuid.uuid4()
        )


async def test_a_conversation_reads_in_the_order_it_was_said(db_session):
    """Even when every message is written inside one transaction.

    ``created_at`` used to come from the column default, which is ``now()`` —
    in PostgreSQL the instant the *transaction* began, identical for everything
    written in it. The thread then ordered by its tiebreak, a random UUID, and
    the seeded demo rendered the patient's "I'll book Monday" above the
    doctor's message it was answering. A clinical record that can show a reply
    before what it replies to is worse than one with a coarse clock.

    Written as one transaction on purpose: that is the case that broke, and the
    one an import or a seeder produces.
    """

    owner, subject = await _patient(db_session, "thread-order")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-order-doc"
    )
    doctor_context = await _context(db_session, doctor, subject)
    patient_context = await _context(db_session, owner, subject)

    thread = await threads.open_thread(
        db_session, context=doctor_context, title="Bloods"
    )
    said = ["Ferritin is low.", "Understood.", "Repeat in two weeks.", "Will do."]
    contexts = [doctor_context, patient_context, doctor_context, patient_context]
    for body, context in zip(said, contexts):
        await threads.send_message(
            db_session, context=context, thread_id=thread.id, body=body
        )
    await db_session.commit()

    _thread, messages, _participants = await threads.read_thread(
        db_session, context=doctor_context, thread_id=thread.id
    )
    assert [message.body for message in messages] == said
