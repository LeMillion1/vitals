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

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.access import AccessScope, PolicyAction, PolicyResourceType
from vitals.enums import (
    CareThreadStatus,
    ProfessionalKind,
    ProfessionalVerificationStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.care_thread import CareThread, CareThreadParticipant
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.persistence import rls
from vitals.services.care import invitations, professionals, relationships, threads
from vitals.services.authorization.subject_access import resolve_access_context


async def _user(session, slug: str, *, roles=()) -> User:
    email = f"{slug}@example.test"
    user = User(
        username=slug,
        normalized_username=slug,
        email=email,
        normalized_email=email,
        email_verified_at=datetime.now(timezone.utc),
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
    operator = await _user(
        session,
        f"{slug}-operator",
        roles=(UserRoleName.PLATFORM_SUPERADMIN,),
    )
    profile = await professionals.submit_profile(
        session,
        user_id=professional.id,
        kind=kind,
        display_name=f"Verified {slug}",
    )
    await professionals.decide(
        session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status="pending",
        status="verified",
    )
    issued = await invitations.invite(
        session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=kind,
        email=f"{slug}@example.test",
    )
    await invitations.accept(
        session,
        token=issued.token,
        accepting_user_id=professional.id,
        verified_email=f"{slug}@example.test",
    )
    relationship = await relationships.establish_from_invitation(
        session, invitation=issued.invitation
    )
    grant = await relationships.grant_consent(
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


async def test_patient_cannot_open_a_conversation_without_a_recipient(db_session):
    owner, subject = await _patient(db_session, "thread-owner-only")
    owner_context = await _context(db_session, owner, subject)

    with pytest.raises(
        threads.CareThreadValidationError,
        match="requires a professional recipient",
    ):
        await threads.open_thread(
            db_session,
            context=owner_context,
            title="Who receives this?",
        )

    assert await db_session.scalar(select(func.count()).select_from(CareThread)) == 0


async def test_relationship_conversation_is_an_idempotent_exact_pair(db_session):
    owner, subject = await _patient(db_session, "thread-pair")
    doctor, relationship, _grant = await _take_into_care(
        db_session,
        subject=subject,
        owner=owner,
        slug="thread-pair-doctor",
    )
    owner_context = await _context(db_session, owner, subject)
    doctor_context = await _context(db_session, doctor, subject)

    # A pre-existing two-person topic thread is history, not something the
    # stable relationship room may silently relabel or take over.
    legacy = await threads.open_thread(
        db_session, context=doctor_context, title="Earlier bloodwork"
    )

    first = await threads.open_relationship_thread(
        db_session,
        context=doctor_context,
        relationship_id=relationship.id,
    )
    second = await threads.open_relationship_thread(
        db_session,
        context=owner_context,
        relationship_id=relationship.id,
    )

    assert second.id == first.id
    assert first.id != legacy.id
    assert first.canonical_relationship_id == relationship.id
    assert legacy.canonical_relationship_id is None
    participants = list(
        await db_session.scalars(
            select(CareThreadParticipant).where(
                CareThreadParticipant.thread_id == first.id,
                CareThreadParticipant.removed_at.is_(None),
            )
        )
    )
    assert {participant.user_id for participant in participants} == {
        owner.id,
        doctor.id,
    }
    professional = next(p for p in participants if p.user_id == doctor.id)
    assert professional.relationship_id == relationship.id


@pytest.mark.integration
async def test_postgres_concurrent_relationship_opens_converge_on_one_room(
    db_session, monkeypatch
):
    """The relationship lock serializes the exact first-open race.

    The first writer is held after inserting the room but before commit. The
    second writer is allowed to issue its own ``FOR UPDATE`` and must wait there
    rather than race the unique constraint. Once the first commits, both calls
    return the same durable room without either transaction failing.
    """

    owner, subject = await _patient(db_session, "thread-pair-race")
    doctor, relationship, _grant = await _take_into_care(
        db_session,
        subject=subject,
        owner=owner,
        slug="thread-pair-race-doctor",
    )
    owner_id = owner.id
    professional_id = doctor.id
    subject_id = subject.id
    relationship_id = relationship.id
    await db_session.commit()

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    original_create = threads._create_pair_thread
    first_room_inserted = asyncio.Event()
    release_first_writer = asyncio.Event()

    async def hold_first_writer(*args, **kwargs):
        room = await original_create(*args, **kwargs)
        first_room_inserted.set()
        await release_first_writer.wait()
        return room

    monkeypatch.setattr(threads, "_create_pair_thread", hold_first_writer)

    lock_queries = 0
    second_lock_issued = asyncio.Event()

    def observe_relationship_lock(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        nonlocal lock_queries
        normalized = statement.lower()
        if "from care_relationships" not in normalized or "for update" not in normalized:
            return
        lock_queries += 1
        if lock_queries == 2:
            second_lock_issued.set()

    event.listen(
        db_session.bind.sync_engine,
        "before_cursor_execute",
        observe_relationship_lock,
    )

    async def open_room() -> uuid.UUID:
        async with factory() as session:
            context = await resolve_access_context(
                session,
                user_id=owner_id,
                subject_id=subject_id,
            )
            room = await threads.open_relationship_thread(
                session,
                context=context,
                relationship_id=relationship_id,
            )
            await session.commit()
            return room.id

    first = asyncio.create_task(open_room())
    second = None
    try:
        await asyncio.wait_for(first_room_inserted.wait(), timeout=5)
        second = asyncio.create_task(open_room())
        await asyncio.wait_for(second_lock_issued.wait(), timeout=5)
        assert not second.done(), (
            "the second writer did not wait on the relationship lock"
        )
        release_first_writer.set()
        first_id, second_id = await asyncio.wait_for(
            asyncio.gather(first, second),
            timeout=5,
        )
    finally:
        release_first_writer.set()
        event.remove(
            db_session.bind.sync_engine,
            "before_cursor_execute",
            observe_relationship_lock,
        )
        pending = [
            task
            for task in (first, second)
            if task is not None and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert first_id == second_id
    async with factory() as verify:
        rooms = list(
            await verify.scalars(
                select(CareThread).where(
                    CareThread.canonical_relationship_id == relationship_id
                )
            )
        )
        participants = list(
            await verify.scalars(
                select(CareThreadParticipant).where(
                    CareThreadParticipant.thread_id == first_id,
                    CareThreadParticipant.removed_at.is_(None),
                )
            )
        )
    assert len(rooms) == 1
    assert {participant.user_id for participant in participants} == {
        owner_id,
        professional_id,
    }


async def test_doctor_and_trainer_get_separate_relationship_conversations(db_session):
    owner, subject = await _patient(db_session, "thread-pair-kinds")
    doctor, doctor_relationship, _grant = await _take_into_care(
        db_session,
        subject=subject,
        owner=owner,
        slug="thread-pair-kinds-doctor",
    )
    trainer, trainer_relationship, _grant = await _take_into_care(
        db_session,
        subject=subject,
        owner=owner,
        slug="thread-pair-kinds-trainer",
        kind=ProfessionalKind.TRAINER,
    )
    owner_context = await _context(db_session, owner, subject)

    doctor_thread = await threads.open_relationship_thread(
        db_session,
        context=owner_context,
        relationship_id=doctor_relationship.id,
    )
    trainer_thread = await threads.open_relationship_thread(
        db_session,
        context=owner_context,
        relationship_id=trainer_relationship.id,
    )

    assert doctor_thread.id != trainer_thread.id
    rows = list(
        await db_session.scalars(
            select(CareThreadParticipant).where(
                CareThreadParticipant.thread_id.in_(
                    (doctor_thread.id, trainer_thread.id)
                ),
                CareThreadParticipant.relationship_id.is_not(None),
            )
        )
    )
    assert {(row.thread_id, row.user_id) for row in rows} == {
        (doctor_thread.id, doctor.id),
        (trainer_thread.id, trainer.id),
    }


async def test_patient_cannot_open_pair_after_message_consent_is_paused(db_session):
    owner, subject = await _patient(db_session, "thread-pair-paused")
    _doctor, relationship, _grant = await _take_into_care(
        db_session,
        subject=subject,
        owner=owner,
        slug="thread-pair-paused-doctor",
    )
    await relationships.set_consent_paused(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
        paused=True,
    )
    owner_context = await _context(db_session, owner, subject)

    with pytest.raises(threads.NotInTheConversation):
        await threads.open_relationship_thread(
            db_session,
            context=owner_context,
            relationship_id=relationship.id,
        )

    assert await db_session.scalar(select(func.count()).select_from(CareThread)) == 0


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


async def test_a_suspended_professional_cannot_be_added_to_a_conversation(
    db_session,
):
    from sqlalchemy import select

    from vitals.models.professional import ProfessionalProfile

    owner, subject = await _patient(db_session, "thread-suspended-join")
    doctor, _rel, _grant = await _take_into_care(
        db_session,
        subject=subject,
        owner=owner,
        slug="thread-suspended-join-doc",
    )
    trainer, _trel, _tgrant = await _take_into_care(
        db_session,
        subject=subject,
        owner=owner,
        slug="thread-suspended-join-coach",
        kind=ProfessionalKind.TRAINER,
    )
    profile = await db_session.scalar(
        select(ProfessionalProfile).where(
            ProfessionalProfile.user_id == trainer.id
        )
    )
    operator = await _user(
        db_session,
        "thread-suspended-join-operator",
        roles=(UserRoleName.PLATFORM_SUPERADMIN,),
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status=ProfessionalVerificationStatus.VERIFIED,
        status=ProfessionalVerificationStatus.SUSPENDED,
        note="synthetic licence withdrawal",
    )

    context = await _context(db_session, doctor, subject)
    thread = await threads.open_thread(db_session, context=context, title="Plan")
    with pytest.raises(threads.NotInTheConversation):
        await threads.add_participant(
            db_session,
            context=context,
            thread_id=thread.id,
            user_id=trainer.id,
        )


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

    await relationships.set_consent_paused(
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
    # The second patient is a separate request; production cannot carry the
    # first request's remembered subject scope into it.
    await db_session.commit()
    db_session.info.pop(rls._SUBJECT_KEY, None)

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
        for scope in relationships.default_scopes(ProfessionalKind.DOCTOR)
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
    message = await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="While allowed."
    )
    await db_session.commit()

    # The patient narrows it: keep looking, stop writing.
    await relationships.revoke_consent(
        db_session, relationship_id=_rel.id, actor_user_id=owner.id
    )
    await relationships.grant_consent(
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
    with pytest.raises(threads.NotInTheConversation):
        await threads.revise_message(
            db_session,
            context=narrowed,
            thread_id=thread.id,
            message_id=message.id,
            body="Still not allowed.",
        )
    with pytest.raises(threads.NotInTheConversation):
        await threads.close_thread(
            db_session, context=narrowed, thread_id=thread.id
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

    scopes = relationships.default_scopes(ProfessionalKind.DOCTOR)
    conversation = {
        scope.action
        for scope in scopes
        if scope.resource_type is PolicyResourceType.OPERATION
        and scope.resource_key == threads.MESSAGE_OPERATION
    }
    assert conversation == {PolicyAction.READ, PolicyAction.MESSAGE}


# ── Unread state belongs to each participant ────────────────────────────────


async def test_a_message_is_unread_only_for_the_other_participant(db_session):
    owner, subject = await _patient(db_session, "thread-unread")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-unread-doc"
    )
    doctor_context = await _context(db_session, doctor, subject)
    patient_context = await _context(db_session, owner, subject)
    thread = await threads.open_thread(
        db_session, context=doctor_context, title="Bloods"
    )
    await threads.send_message(
        db_session,
        context=doctor_context,
        thread_id=thread.id,
        body="Please fast.",
    )
    await db_session.commit()

    assert await threads.unread_marker(
        db_session, context=doctor_context
    ) == 0
    assert await threads.unread_marker(
        db_session, context=patient_context
    ) == 1
    summaries = await threads.list_thread_summaries(
        db_session, context=patient_context
    )
    assert [(item.thread.id, item.unread) for item in summaries] == [
        (thread.id, True)
    ]


async def test_opening_a_thread_advances_only_that_readers_cursor(db_session):
    owner, subject = await _patient(db_session, "thread-read-cursor")
    doctor, _rel, _grant = await _take_into_care(
        db_session,
        subject=subject,
        owner=owner,
        slug="thread-read-cursor-doc",
    )
    doctor_context = await _context(db_session, doctor, subject)
    patient_context = await _context(db_session, owner, subject)
    thread = await threads.open_thread(
        db_session, context=doctor_context, title="Bloods"
    )
    await threads.send_message(
        db_session,
        context=doctor_context,
        thread_id=thread.id,
        body="First.",
    )
    await db_session.commit()

    await threads.mark_thread_read(
        db_session, context=patient_context, thread_id=thread.id
    )
    await db_session.commit()
    assert await threads.unread_marker(
        db_session, context=patient_context
    ) == 0

    # A later professional message is new work; the patient's own reply is not.
    await threads.send_message(
        db_session,
        context=doctor_context,
        thread_id=thread.id,
        body="Second.",
    )
    await db_session.commit()
    assert await threads.unread_marker(
        db_session, context=patient_context
    ) == 1
    await threads.send_message(
        db_session,
        context=patient_context,
        thread_id=thread.id,
        body="Understood.",
    )
    await db_session.commit()
    assert await threads.unread_marker(
        db_session, context=patient_context
    ) == 0


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


async def test_a_correction_can_be_bound_to_the_exact_conversation(db_session):
    owner, subject = await _patient(db_session, "thread-edit-room")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-edit-room-doc"
    )
    context = await _context(db_session, doctor, subject)
    first = await threads.open_thread(db_session, context=context, title="First")
    second = await threads.open_thread(db_session, context=context, title="Second")
    message = await threads.send_message(
        db_session, context=context, thread_id=first.id, body="Belongs in first."
    )
    await db_session.commit()

    with pytest.raises(threads.NotTheAuthor):
        await threads.revise_message(
            db_session,
            context=context,
            thread_id=second.id,
            message_id=message.id,
            body="Must not move through a different URL.",
        )
    await db_session.refresh(message)
    assert message.body == "Belongs in first."


async def test_a_closed_thread_is_read_only_and_still_there(db_session):
    owner, subject = await _patient(db_session, "thread-close")
    doctor, _rel, _grant = await _take_into_care(
        db_session, subject=subject, owner=owner, slug="thread-close-doc"
    )
    context = await _context(db_session, doctor, subject)
    thread = await threads.open_thread(db_session, context=context, title="Bloods")
    message = await threads.send_message(
        db_session, context=context, thread_id=thread.id, body="Said."
    )
    await threads.close_thread(db_session, context=context, thread_id=thread.id)
    await db_session.commit()

    assert thread.status == CareThreadStatus.CLOSED.value
    with pytest.raises(threads.NotInTheConversation):
        await threads.send_message(
            db_session, context=context, thread_id=thread.id, body="More."
        )
    with pytest.raises(threads.ThreadStateChanged):
        await threads.revise_message(
            db_session,
            context=context,
            thread_id=thread.id,
            message_id=message.id,
            body="A closed conversation cannot change.",
        )
    with pytest.raises(threads.ThreadStateChanged):
        await threads.close_thread(
            db_session, context=context, thread_id=thread.id
        )
    _t, messages, _p = await threads.read_thread(
        db_session, context=context, thread_id=thread.id
    )
    assert [m.body for m in messages] == ["Said."]

    await threads.reopen_thread(db_session, context=context, thread_id=thread.id)
    await db_session.commit()
    with pytest.raises(threads.ThreadStateChanged):
        await threads.reopen_thread(
            db_session, context=context, thread_id=thread.id
        )
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
