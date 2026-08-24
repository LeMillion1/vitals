"""What a professional writes, and why it is not in the patient's facts.

A doctor's reading of a lab panel is not the lab panel. Keeping them apart is
partly about the record — a year later the two would be indistinguishable — and
partly about permission: if a professional's thinking lived inside the patient's
measurements, a professional would need to be able to write into them, and the
read-only default would have to go.

Three rules, and each has its tests below. Only a professional in live care may
write. Only the author may change what they wrote. Nothing is deleted.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from vitals.access import AccessScope, PolicyAction, PolicyResourceType
from vitals.enums import (
    CarePlanStatus,
    Domain,
    ProfessionalKind,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.services.care import invitations, records, relationships
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


async def _in_care_with_consent(session, slug: str, *, scopes=None):
    """A patient, a doctor, an established relationship and a live consent."""

    owner = await _user(session, slug)
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name=f"Synthetic {slug}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()

    doctor = await _user(session, f"{slug}-doc", roles=(UserRoleName.DOCTOR,))
    issued = await invitations.invite(
        session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=ProfessionalKind.DOCTOR,
        email=f"{slug}-doc@example.test",
    )
    await invitations.accept(
        session,
        token=issued.token,
        accepting_user_id=doctor.id,
        verified_email=f"{slug}-doc@example.test",
    )
    relationship = await relationships.establish_from_invitation(
        session, invitation=issued.invitation
    )
    await relationships.grant_consent(
        session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
        scopes=scopes,
    )
    return owner, subject, doctor, relationship


async def _context(session, user, subject):
    return await resolve_access_context(
        session, user_id=user.id, subject_id=subject.id
    )


# ── The two sides fit together ───────────────────────────────────────────────


def test_the_artifact_keys_the_two_services_use_are_the_same():
    """A mismatch would make every write unauthorized and nothing would say so.

    ``relationships`` writes these keys into a consent; this service asks the
    policy about them. Nothing else connects the two, so the pair is asserted
    rather than assumed.
    """

    granted = {
        scope.resource_key
        for kind in ProfessionalKind
        for scope in relationships.default_scopes(kind)
        if scope.resource_type is PolicyResourceType.ARTIFACT
    }
    assert {records.NOTE_ARTIFACT, records.PLAN_ARTIFACT} <= granted


# ── Only somebody in live care may write ─────────────────────────────────────


async def test_a_professional_in_care_writes_their_own_note(db_session):
    _owner, subject, doctor, relationship = await _in_care_with_consent(
        db_session, "rec-write"
    )
    context = await _context(db_session, doctor, subject)

    note = await records.write_note(
        db_session, context=context, body="Reviewed the panel; TSH is drifting."
    )
    assert note.actor_user_id == doctor.id
    assert note.subject_id == subject.id
    # Stored so it stays reviewable: a note with no care behind it is one
    # nobody can say was authorized.
    assert note.relationship_id == relationship.id


async def test_a_stranger_writes_nothing(db_session):
    _owner, subject, _doctor, _rel = await _in_care_with_consent(
        db_session, "rec-stranger"
    )
    stranger = await _user(
        db_session, "rec-stranger-outsider", roles=(UserRoleName.DOCTOR,)
    )
    context = await _context(db_session, stranger, subject)

    with pytest.raises(records.NotInLiveCare):
        await records.write_note(db_session, context=context, body="Not mine to write")


async def test_revoking_consent_stops_the_writing_too(db_session):
    """Being in care is necessary and not sufficient — the consent is the rest."""

    owner, subject, doctor, relationship = await _in_care_with_consent(
        db_session, "rec-revoked"
    )
    await relationships.revoke_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    context = await _context(db_session, doctor, subject)

    with pytest.raises(records.NotInLiveCare):
        await records.write_note(db_session, context=context, body="After revocation")


async def test_a_narrowed_consent_that_only_reads_does_not_let_them_write(db_session):
    """A patient who agreed to be read has not agreed to be written about."""

    _owner, subject, doctor, _rel = await _in_care_with_consent(
        db_session,
        "rec-narrow",
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
    context = await _context(db_session, doctor, subject)

    with pytest.raises(records.NotInLiveCare):
        await records.write_note(db_session, context=context, body="Not agreed to")


async def test_ending_the_relationship_stops_the_writing(db_session):
    owner, subject, doctor, relationship = await _in_care_with_consent(
        db_session, "rec-ended"
    )
    await relationships.end_relationship(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    context = await _context(db_session, doctor, subject)

    with pytest.raises(records.NotInLiveCare):
        await records.write_note(db_session, context=context, body="After the end")


# ── Only the author may change it ────────────────────────────────────────────


async def test_the_author_revises_their_own_note(db_session):
    _owner, subject, doctor, _rel = await _in_care_with_consent(
        db_session, "rec-revise"
    )
    context = await _context(db_session, doctor, subject)
    note = await records.write_note(db_session, context=context, body="First reading")

    revised = await records.revise_note(
        db_session, context=context, note_id=note.id, body="On reflection, watch it"
    )
    assert revised.id == note.id
    assert revised.body == "On reflection, watch it"


async def test_a_second_professional_cannot_edit_the_first_ones_note(db_session):
    """A note somebody else can edit is not that person's note.

    And the refusal is a not-found rather than a denial: the author condition is
    in the query, so a note that is not yours does not exist here.
    """

    owner, subject, first, _rel = await _in_care_with_consent(
        db_session, "rec-second"
    )
    context_first = await _context(db_session, first, subject)
    note = await records.write_note(
        db_session, context=context_first, body="The first reading"
    )

    second = await _user(db_session, "rec-second-trainer", roles=(UserRoleName.TRAINER,))
    issued = await invitations.invite(
        db_session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=ProfessionalKind.TRAINER,
        email="rec-second-trainer@example.test",
    )
    await invitations.accept(
        db_session,
        token=issued.token,
        accepting_user_id=second.id,
        verified_email="rec-second-trainer@example.test",
    )
    second_relationship = await relationships.establish_from_invitation(
        db_session, invitation=issued.invitation
    )
    await relationships.grant_consent(
        db_session,
        relationship_id=second_relationship.id,
        actor_user_id=owner.id,
    )
    context_second = await _context(db_session, second, subject)

    with pytest.raises(records.NotTheAuthor):
        await records.revise_note(
            db_session, context=context_second, note_id=note.id, body="Overwritten"
        )
    assert note.body == "The first reading"

    # But they can read it: a second professional joining a case needs to see
    # what the first concluded, which is what a shared record is for.
    visible = await records.list_notes(db_session, context=context_second)
    assert [row.id for row in visible] == [note.id]


async def test_the_patient_reads_what_was_written_about_them(db_session):
    """Self-ownership already authorizes it; this pins that nothing narrowed it."""

    owner, subject, doctor, _rel = await _in_care_with_consent(db_session, "rec-read")
    doctor_context = await _context(db_session, doctor, subject)
    note = await records.write_note(
        db_session, context=doctor_context, body="What I think"
    )

    owner_context = await _context(db_session, owner, subject)
    visible = await records.list_notes(db_session, context=owner_context)
    assert [row.id for row in visible] == [note.id]


async def test_the_patient_does_not_write_a_professionals_note(db_session):
    """They own the record; they are not in care for themselves."""

    owner, subject, _doctor, _rel = await _in_care_with_consent(
        db_session, "rec-patient-write"
    )
    context = await _context(db_session, owner, subject)

    with pytest.raises(records.NotInLiveCare):
        await records.write_note(db_session, context=context, body="My own note")


async def test_a_note_in_one_record_is_not_reachable_from_another(db_session):
    """The doctor is in live care — for somebody else."""

    _owner_a, subject_a, doctor_a, _rel_a = await _in_care_with_consent(
        db_session, "rec-cross-a"
    )
    _owner_b, subject_b, doctor_b, _rel_b = await _in_care_with_consent(
        db_session, "rec-cross-b"
    )
    context_b = await _context(db_session, doctor_b, subject_b)
    theirs = await records.write_note(
        db_session, context=context_b, body="B's record"
    )

    # A's doctor, holding B's note id, asking inside A's record.
    context_a = await _context(db_session, doctor_a, subject_a)
    with pytest.raises(records.NotTheAuthor):
        await records.revise_note(
            db_session, context=context_a, note_id=theirs.id, body="Nope"
        )
    assert theirs.body == "B's record"

    # And asking inside B's record, where they are nobody.
    context_a_in_b = await _context(db_session, doctor_a, subject_b)
    with pytest.raises(records.NotInLiveCare):
        await records.revise_note(
            db_session, context=context_a_in_b, note_id=theirs.id, body="Nope"
        )


# ── Plans, and the fact that nothing is deleted ──────────────────────────────


async def test_a_plan_is_drafted_then_followed_then_archived(db_session):
    _owner, subject, doctor, _rel = await _in_care_with_consent(db_session, "rec-plan")
    context = await _context(db_session, doctor, subject)

    plan = await records.write_plan(
        db_session,
        context=context,
        title="Twelve weeks of base",
        body="Three sessions a week, easy pace.",
        effective_from=date(2026, 9, 1),
        effective_to=date(2026, 11, 24),
    )
    assert plan.status == CarePlanStatus.DRAFT.value
    assert await records.list_plans(db_session, context=context) == [plan]

    active = await records.set_plan_status(
        db_session, context=context, plan_id=plan.id, status=CarePlanStatus.ACTIVE
    )
    assert active.status == CarePlanStatus.ACTIVE.value

    archived = await records.set_plan_status(
        db_session, context=context, plan_id=plan.id, status=CarePlanStatus.ARCHIVED
    )
    assert archived.status == CarePlanStatus.ARCHIVED.value
    # Gone from the working list, still in the record.
    assert await records.list_plans(db_session, context=context) == []
    assert await records.list_plans(
        db_session, context=context, include_archived=True
    ) == [plan]


async def test_an_archived_plan_stays_archived(db_session):
    """What somebody was told to do last spring is part of the record of their care."""

    _owner, subject, doctor, _rel = await _in_care_with_consent(
        db_session, "rec-plan-archived"
    )
    context = await _context(db_session, doctor, subject)
    plan = await records.write_plan(
        db_session,
        context=context,
        title="Old plan",
        body="Superseded.",
        effective_from=date(2026, 1, 1),
    )
    await records.set_plan_status(
        db_session, context=context, plan_id=plan.id, status=CarePlanStatus.ARCHIVED
    )

    with pytest.raises(records.ProfessionalRecordValidationError):
        await records.set_plan_status(
            db_session, context=context, plan_id=plan.id, status=CarePlanStatus.ACTIVE
        )


def test_neither_record_has_a_delete_path():
    """The rule, asserted against the module rather than described in a comment.

    Checked structurally: no exported name mentions deleting, and nothing in the
    module calls ``session.delete`` or issues a ``DELETE``. Grepping the source
    text would only find the paragraph explaining why none of that is here.
    """

    import ast
    import inspect

    assert not [name for name in records.__all__ if "delete" in name.lower()]

    tree = ast.parse(inspect.getsource(records))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        attribute = getattr(node.func, "attr", None)
        assert attribute != "delete", ast.dump(node)[:120]


async def test_a_plan_cannot_stop_before_it_starts(db_session):
    _owner, subject, doctor, _rel = await _in_care_with_consent(
        db_session, "rec-plan-range"
    )
    context = await _context(db_session, doctor, subject)
    with pytest.raises(records.ProfessionalRecordValidationError):
        await records.write_plan(
            db_session,
            context=context,
            title="Backwards",
            body="Ends before it begins.",
            effective_from=date(2026, 9, 1),
            effective_to=date(2026, 8, 1),
        )


@pytest.mark.parametrize("body", ["", "   ", None, 7, "x" * 20001])
async def test_a_note_needs_something_in_it(db_session, body):
    _owner, subject, doctor, _rel = await _in_care_with_consent(
        db_session, f"rec-body-{abs(hash(str(body))) % 9999}"
    )
    context = await _context(db_session, doctor, subject)
    with pytest.raises(records.ProfessionalRecordValidationError):
        await records.write_note(db_session, context=context, body=body)


async def test_revising_something_that_is_not_there(db_session):
    _owner, subject, doctor, _rel = await _in_care_with_consent(
        db_session, "rec-missing"
    )
    context = await _context(db_session, doctor, subject)
    with pytest.raises(records.NotTheAuthor):
        await records.revise_note(
            db_session, context=context, note_id=uuid.uuid4(), body="Nothing there"
        )
