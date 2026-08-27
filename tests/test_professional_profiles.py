"""A claim about who somebody is, and an operator deciding about it.

Nothing in this file grants access to anything, and that is the design rather
than an omission. A profile says: this is my name, this is my licence, I am a
doctor. Verification says an operator checked that against the register that
issued it. Whose record the person may then reach is a different question with a
different answer, and it is the patient's to give.

The reason to keep them apart is concrete. If verification implied access, one
operator approving one licence would admit that person to every record in the
installation and no patient would ever have been asked.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from vitals.enums import (
    ProfessionalKind,
    ProfessionalVerificationStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import AuditEvent, User, UserRole
from vitals.models.professional import ProfessionalReviewDecision
from vitals.services.care import professionals


async def _user(
    session,
    slug: str,
    *,
    roles=(UserRoleName.DOCTOR,),
    status=UserStatus.ACTIVE,
) -> User:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=status.value,
    )
    session.add(user)
    await session.flush()
    for role in roles:
        session.add(UserRole(user_id=user.id, role=role.value))
    await session.flush()
    return user


async def _operator(session, slug: str) -> User:
    return await _user(
        session,
        slug,
        roles=(UserRoleName.PLATFORM_SUPERADMIN, UserRoleName.DOCTOR),
    )


# ── Submitting a claim ───────────────────────────────────────────────────────


async def test_a_submitted_profile_lands_in_the_queue(db_session):
    """Pending rather than unverified: submitting is the act of asking.

    A profile nobody has been asked about would sit unreviewed forever, which
    is indistinguishable from a queue that is working.
    """

    doctor = await _user(db_session, "prof-submit")
    operator = await _operator(db_session, "prof-submit-op")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Synthetic",
        credential_reference="LIC-00042",
    )

    assert profile.verification_status == ProfessionalVerificationStatus.PENDING.value
    assert profile.verified_at is None and profile.verified_by_user_id is None
    assert [
        entry.profile_id
        for entry in await professionals.review_console(
            db_session, reviewer_user_id=operator.id
        )
    ] == [profile.id]


async def test_one_profile_per_account(db_session):
    """Somebody who is both holds one profile; the relationship names the kind.

    Two profiles would mean two sets of defaults on one account, and a patient
    who accepted a trainer would have no way to be sure which set applied.
    """

    doctor = await _user(db_session, "prof-duplicate")
    await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Synthetic",
    )
    with pytest.raises(professionals.ProfessionalConflictError):
        await professionals.submit_profile(
            db_session,
            user_id=doctor.id,
            kind=ProfessionalKind.DOCTOR,
            display_name="The same doctor again",
        )


@pytest.mark.parametrize("name", ["", "   ", None, 7, "x" * 201])
async def test_a_profile_needs_a_usable_name(db_session, name):
    doctor = await _user(db_session, f"prof-name-{abs(hash(str(name))) % 9999}")
    with pytest.raises(professionals.ProfessionalValidationError):
        await professionals.submit_profile(
            db_session,
            user_id=doctor.id,
            kind=ProfessionalKind.DOCTOR,
            display_name=name,
        )


async def test_a_suspended_account_cannot_submit(db_session):
    doctor = await _user(db_session, "prof-suspended", status=UserStatus.SUSPENDED)
    with pytest.raises(professionals.ProfessionalNotFoundError):
        await professionals.submit_profile(
            db_session,
            user_id=doctor.id,
            kind=ProfessionalKind.DOCTOR,
            display_name="Dr Suspended",
        )


async def test_a_profile_kind_must_match_an_assigned_role(db_session):
    doctor = await _user(
        db_session,
        "prof-wrong-kind",
        roles=(UserRoleName.DOCTOR,),
    )

    with pytest.raises(professionals.ProfessionalValidationError):
        await professionals.submit_profile(
            db_session,
            user_id=doctor.id,
            kind=ProfessionalKind.TRAINER,
            display_name="Not a trainer",
        )


async def test_a_rejected_profile_can_be_corrected_without_changing_kind(db_session):
    doctor = await _user(db_session, "prof-resubmit")
    operator = await _operator(db_session, "prof-resubmit-op")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr First",
        credential_reference="WRONG",
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status=ProfessionalVerificationStatus.PENDING,
        status=ProfessionalVerificationStatus.REJECTED,
        note="credential does not match the register",
    )

    corrected = await professionals.resubmit_profile(
        db_session,
        user_id=doctor.id,
        display_name="Dr Corrected",
        credential_reference="LIC-42",
    )

    assert corrected.kind == ProfessionalKind.DOCTOR.value
    assert corrected.display_name == "Dr Corrected"
    assert corrected.credential_reference == "LIC-42"
    assert corrected.verification_status == ProfessionalVerificationStatus.PENDING.value
    assert corrected.review_note is None


async def test_a_professional_cannot_self_clear_a_suspension(db_session):
    doctor = await _user(db_session, "prof-no-self-unsuspend")
    operator = await _operator(db_session, "prof-no-self-unsuspend-op")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Suspended",
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status=ProfessionalVerificationStatus.PENDING,
        status=ProfessionalVerificationStatus.VERIFIED,
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status=ProfessionalVerificationStatus.VERIFIED,
        status=ProfessionalVerificationStatus.SUSPENDED,
        note="licence suspended",
    )

    with pytest.raises(professionals.ProfessionalConflictError):
        await professionals.resubmit_profile(
            db_session,
            user_id=doctor.id,
            display_name="Dr Self Cleared",
        )


# ── Deciding about it ────────────────────────────────────────────────────────


async def test_verifying_records_who_decided_and_when(db_session):
    """A verification with no reviewer is a claim that verified itself."""

    doctor = await _user(db_session, "prof-verify")
    operator = await _operator(db_session, "prof-verify-op")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Synthetic",
    )

    decided = await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status=ProfessionalVerificationStatus.PENDING,
        status=ProfessionalVerificationStatus.VERIFIED,
    )
    assert decided.verification_status == ProfessionalVerificationStatus.VERIFIED.value
    assert decided.verified_by_user_id == operator.id
    assert decided.verified_at is not None
    assert await professionals.is_verified(db_session, user_id=doctor.id)


async def test_a_review_takes_the_identity_governance_fence(
    db_session, monkeypatch
):
    doctor = await _user(db_session, "prof-governance")
    operator = await _operator(db_session, "prof-governance-op")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Governed",
    )
    observed = False
    original = professionals.acquire_identity_governance_lock

    async def guarded(session):
        nonlocal observed
        observed = True
        return await original(session)

    monkeypatch.setattr(professionals, "acquire_identity_governance_lock", guarded)
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status=ProfessionalVerificationStatus.PENDING,
        status=ProfessionalVerificationStatus.VERIFIED,
    )

    assert observed


async def test_only_an_operator_can_read_the_bounded_review_console(db_session):
    doctor = await _user(db_session, "prof-console")
    operator = await _operator(db_session, "prof-console-op")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Console",
        credential_reference="LIC-CONSOLE",
    )

    with pytest.raises(professionals.NotAReviewerError):
        await professionals.review_console(
            db_session, reviewer_user_id=doctor.id
        )

    entries = await professionals.review_console(
        db_session, reviewer_user_id=operator.id
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry.profile_id == profile.id
    assert entry.username == doctor.username
    assert entry.credential_reference == "LIC-CONSOLE"
    assert not hasattr(entry, "user")


async def test_a_corrected_claim_keeps_the_previous_review_for_the_next_operator(
    db_session,
):
    doctor = await _user(db_session, "prof-console-history")
    operator = await _operator(db_session, "prof-console-history-op")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr First Claim",
        credential_reference="WRONG",
    )
    await professionals.reject_profile(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        note="The register uses a different number.",
    )
    await professionals.resubmit_profile(
        db_session,
        user_id=doctor.id,
        display_name="Dr Corrected Claim",
        credential_reference="LIC-CORRECT",
    )

    (entry,) = await professionals.review_console(
        db_session, reviewer_user_id=operator.id
    )
    assert entry.verification_status == ProfessionalVerificationStatus.PENDING.value
    assert entry.review_note is None
    assert len(entry.history) == 1
    assert entry.history[0].to_status == ProfessionalVerificationStatus.REJECTED.value
    assert entry.history[0].note == "The register uses a different number."


async def test_review_history_is_append_only_and_carries_no_free_text(db_session):
    doctor = await _user(db_session, "prof-audit")
    operator = await _operator(db_session, "prof-audit-op")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Audit",
        credential_reference="SECRET-LICENCE",
    )

    await professionals.verify_profile(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
    )
    await professionals.suspend_profile(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        note="private free-text reason",
    )
    await professionals.reinstate_profile(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
    )

    events = list(
        await db_session.scalars(
            select(AuditEvent).order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )
    assert [event.event_type for event in events].count(
        "care.professional_profile.verified"
    ) == 2
    assert [event.event_type for event in events].count(
        "care.professional_profile.suspended"
    ) == 1
    assert {event.metadata_json["result_code"] for event in events} == {
        "pending_to_verified",
        "verified_to_suspended",
        "suspended_to_verified",
    }
    audit_text = str([event.metadata_json for event in events])
    assert "private free-text reason" not in audit_text
    assert "SECRET-LICENCE" not in audit_text
    assert "Dr Audit" not in audit_text
    decisions = list(
        await db_session.scalars(
            select(ProfessionalReviewDecision).order_by(
                ProfessionalReviewDecision.created_at,
                ProfessionalReviewDecision.id,
            )
        )
    )
    assert {decision.to_status for decision in decisions} == {
        ProfessionalVerificationStatus.VERIFIED.value,
        ProfessionalVerificationStatus.SUSPENDED.value,
    }
    suspension = next(
        decision
        for decision in decisions
        if decision.to_status == ProfessionalVerificationStatus.SUSPENDED.value
    )
    assert suspension.note == "private free-text reason"
    assert suspension.reviewer_user_id == operator.id


async def test_a_stale_review_cannot_overwrite_a_newer_decision(db_session):
    doctor = await _user(db_session, "prof-stale-review")
    operator = await _operator(db_session, "prof-stale-review-op")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Stale",
    )
    await professionals.verify_profile(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
    )

    with pytest.raises(professionals.ProfessionalConflictError):
        await professionals.verify_profile(
            db_session,
            profile_id=profile.id,
            reviewer_user_id=operator.id,
        )

    assert profile.verification_status == ProfessionalVerificationStatus.VERIFIED.value
    assert len(list(await db_session.scalars(select(AuditEvent)))) == 1


@pytest.mark.parametrize("mutation", ["inactive", "role_removed"])
async def test_verification_rechecks_the_target_account_and_exact_role(
    db_session, mutation
):
    doctor = await _user(db_session, f"prof-target-{mutation}")
    operator = await _operator(db_session, f"prof-target-{mutation}-op")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Target",
    )
    if mutation == "inactive":
        doctor.status = UserStatus.SUSPENDED.value
        expected = professionals.ProfessionalNotFoundError
    else:
        role = await db_session.scalar(
            select(UserRole).where(
                UserRole.user_id == doctor.id,
                UserRole.role == UserRoleName.DOCTOR.value,
            )
        )
        await db_session.delete(role)
        await db_session.flush()
        expected = professionals.ProfessionalValidationError

    with pytest.raises(expected):
        await professionals.verify_profile(
            db_session,
            profile_id=profile.id,
            reviewer_user_id=operator.id,
        )

    assert profile.verification_status == ProfessionalVerificationStatus.PENDING.value
    assert list(await db_session.scalars(select(AuditEvent))) == []


@pytest.mark.parametrize(
    "status",
    [
        ProfessionalVerificationStatus.REJECTED,
        ProfessionalVerificationStatus.SUSPENDED,
    ],
)
async def test_a_refusal_has_to_say_why(db_session, status):
    """The professional needs to know what to fix; the next operator, what was found."""

    doctor = await _user(db_session, f"prof-refuse-{status.value}")
    operator = await _operator(db_session, f"prof-refuse-op-{status.value}")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Synthetic",
    )
    expected = ProfessionalVerificationStatus.PENDING
    if status is ProfessionalVerificationStatus.SUSPENDED:
        await professionals.decide(
            db_session,
            profile_id=profile.id,
            reviewer_user_id=operator.id,
            expected_status=ProfessionalVerificationStatus.PENDING,
            status=ProfessionalVerificationStatus.VERIFIED,
        )
        expected = ProfessionalVerificationStatus.VERIFIED

    with pytest.raises(professionals.ProfessionalValidationError):
        await professionals.decide(
            db_session,
            profile_id=profile.id,
            reviewer_user_id=operator.id,
            expected_status=expected,
            status=status,
        )

    decided = await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status=expected,
        status=status,
        note="the register has no entry for that number",
    )
    assert decided.verification_status == status.value
    assert decided.review_note
    assert not await professionals.is_verified(db_session, user_id=doctor.id)


async def test_suspending_a_verified_profile_withdraws_the_stamp(db_session):
    """A licence can lapse after the fact, and the profile must stop reading as checked.

    Deleting it instead would erase the trail of it ever having been approved,
    which is the thing an audit of a withdrawn licence needs most.
    """

    doctor = await _user(db_session, "prof-lapse")
    operator = await _operator(db_session, "prof-lapse-op")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Synthetic",
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status=ProfessionalVerificationStatus.PENDING,
        status=ProfessionalVerificationStatus.VERIFIED,
    )
    assert await professionals.is_verified(db_session, user_id=doctor.id)

    suspended = await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status=ProfessionalVerificationStatus.VERIFIED,
        status=ProfessionalVerificationStatus.SUSPENDED,
        note="licence withdrawn by the issuing register",
    )
    assert suspended.verified_at is None
    assert suspended.verified_by_user_id is None
    assert not await professionals.is_verified(db_session, user_id=doctor.id)
    # The profile is still there, still saying it was once approved.
    assert suspended.review_note


async def test_only_an_operator_decides(db_session):
    """Holding the doctor role is not holding the power to grant it."""

    doctor = await _user(db_session, "prof-notop", roles=(UserRoleName.DOCTOR,))
    other = await _user(db_session, "prof-notop-other", roles=(UserRoleName.DOCTOR,))
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Synthetic",
    )

    with pytest.raises(professionals.NotAReviewerError):
        await professionals.decide(
            db_session,
            profile_id=profile.id,
            reviewer_user_id=other.id,
            expected_status=ProfessionalVerificationStatus.PENDING,
            status=ProfessionalVerificationStatus.VERIFIED,
        )


async def test_nobody_reviews_their_own_claim(db_session):
    """So that "an operator approved this" stays a statement about two people."""

    operator = await _operator(db_session, "prof-self")
    profile = await professionals.submit_profile(
        db_session,
        user_id=operator.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Operator",
    )

    with pytest.raises(professionals.NotAReviewerError):
        await professionals.decide(
            db_session,
            profile_id=profile.id,
            reviewer_user_id=operator.id,
            expected_status=ProfessionalVerificationStatus.PENDING,
            status=ProfessionalVerificationStatus.VERIFIED,
        )


@pytest.mark.parametrize(
    "status",
    [
        ProfessionalVerificationStatus.UNVERIFIED,
        ProfessionalVerificationStatus.PENDING,
    ],
)
async def test_a_review_records_a_verdict_rather_than_a_shrug(db_session, status):
    """Putting a claim back in the queue is not a decision about it."""

    doctor = await _user(db_session, f"prof-shrug-{status.value}")
    operator = await _operator(db_session, f"prof-shrug-op-{status.value}")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Synthetic",
    )
    with pytest.raises(professionals.ProfessionalValidationError):
        await professionals.decide(
            db_session,
            profile_id=profile.id,
            reviewer_user_id=operator.id,
            expected_status=ProfessionalVerificationStatus.PENDING,
            status=status,
        )


async def test_deciding_about_a_profile_that_is_not_there(db_session):
    operator = await _operator(db_session, "prof-missing-op")
    with pytest.raises(professionals.ProfessionalNotFoundError):
        await professionals.decide(
            db_session,
            profile_id=uuid.uuid4(),
            reviewer_user_id=operator.id,
            expected_status=ProfessionalVerificationStatus.PENDING,
            status=ProfessionalVerificationStatus.VERIFIED,
        )


# ── What a verified profile is still not ─────────────────────────────────────


async def test_a_verified_profile_reaches_nobodys_record(db_session):
    """The property the whole separation exists for.

    A verified doctor with no relationship and no consent is a stranger to every
    subject in the installation. If this ever passes for the wrong reason, the
    thing that broke is worth more than the test.
    """

    from vitals.access import (
        AccessRequest,
        PolicyAction,
        PolicyResourceType,
        is_allowed,
    )
    from vitals.models.identity import HealthSubject
    from vitals.services.authorization.subject_access import resolve_access_context

    doctor = await _user(db_session, "prof-stranger", roles=(UserRoleName.DOCTOR,))
    operator = await _operator(db_session, "prof-stranger-op")
    profile = await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Synthetic",
    )
    await professionals.decide(
        db_session,
        profile_id=profile.id,
        reviewer_user_id=operator.id,
        expected_status=ProfessionalVerificationStatus.PENDING,
        status=ProfessionalVerificationStatus.VERIFIED,
    )

    patient = await _user(db_session, "prof-stranger-patient")
    subject = HealthSubject(
        owner_user_id=patient.id,
        display_name="Synthetic patient",
        timezone="Asia/Almaty",
    )
    db_session.add(subject)
    await db_session.flush()

    context = await resolve_access_context(
        db_session, user_id=doctor.id, subject_id=subject.id
    )
    for action in PolicyAction:
        assert not is_allowed(
            context,
            AccessRequest(
                subject_id=subject.id,
                resource_type=PolicyResourceType.DOMAIN,
                resource_key="weight",
                action=action,
            ),
        ), action
