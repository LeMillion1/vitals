"""Two records, and access needs both of them live at once.

A ``care_relationship`` says a professional is in care for a patient. A
``consent_grant`` says what that patient agreed they may see. Neither is
sufficient, and the tests below are mostly about the ways that could quietly
stop being true: a role that starts meaning something, an invitation that grants
on acceptance, a paused consent that keeps working, a revocation that takes
effect on the next request but not this one.

The asymmetry between the two is deliberate. A relationship with no live consent
is an ordinary, correct state — somebody the patient agreed to work with and has
not yet, or no longer, agreed to show anything to.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from vitals.access import (
    AccessRequest,
    AccessScope,
    PolicyAction,
    PolicyResourceType,
    is_allowed,
)
from vitals.enums import (
    CareRelationshipStatus,
    ConsentStatus,
    Domain,
    ProfessionalKind,
    ProfessionalVerificationStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.services.care import invitations, relationships
from vitals.services.access_resolution import resolve_access_context


async def _user(session, slug: str, *, roles=(), status=UserStatus.ACTIVE) -> User:
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


async def _patient(session, slug: str) -> tuple[User, HealthSubject]:
    owner = await _user(session, slug)
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name=f"Synthetic {slug}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return owner, subject


async def _in_care(session, slug: str, *, kind=ProfessionalKind.DOCTOR):
    """A patient, a professional, and an established relationship — no consent."""

    owner, subject = await _patient(session, slug)
    role = (
        UserRoleName.DOCTOR
        if kind is ProfessionalKind.DOCTOR
        else UserRoleName.TRAINER
    )
    professional = await _user(session, f"{slug}-pro", roles=(role,))
    operator = await _user(
        session,
        f"{slug}-operator",
        roles=(UserRoleName.PLATFORM_SUPERADMIN,),
    )
    from vitals.services.care import professionals

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
        email=f"{slug}-pro@example.test",
    )
    await invitations.accept(
        session,
        token=issued.token,
        accepting_user_id=professional.id,
        verified_email=f"{slug}-pro@example.test",
    )
    relationship = await relationships.establish_from_invitation(
        session, invitation=issued.invitation
    )
    return owner, subject, professional, relationship


def _reads(scope_key: str = "weight") -> AccessRequest:
    return AccessRequest(
        subject_id=uuid.uuid4(),
        resource_type=PolicyResourceType.DOMAIN,
        resource_key=scope_key,
        action=PolicyAction.READ,
    )


async def _may(session, professional, subject, *, key="weight", action=None):
    context = await resolve_access_context(
        session, user_id=professional.id, subject_id=subject.id
    )
    return is_allowed(
        context,
        AccessRequest(
            subject_id=subject.id,
            resource_type=PolicyResourceType.DOMAIN,
            resource_key=key,
            action=action or PolicyAction.READ,
        ),
    )


async def test_the_first_consent_task_never_reappears_after_revocation(db_session):
    owner, subject, _professional, relationship = await _in_care(
        db_session, "first-consent-task"
    )
    assert await relationships.has_relationship_awaiting_first_consent(
        db_session,
        subject_id=subject.id,
        owner_user_id=owner.id,
    )

    await relationships.grant_consent(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
    )
    assert not await relationships.has_relationship_awaiting_first_consent(
        db_session,
        subject_id=subject.id,
        owner_user_id=owner.id,
    )

    await relationships.revoke_consent(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
    )
    assert not await relationships.has_relationship_awaiting_first_consent(
        db_session,
        subject_id=subject.id,
        owner_user_id=owner.id,
    )


# ── Neither half is sufficient ───────────────────────────────────────────────


async def test_a_relationship_without_consent_reaches_nothing(db_session):
    """The correct and ordinary state right after an invitation is accepted."""

    _owner, subject, professional, _rel = await _in_care(db_session, "care-nocons")
    assert not await _may(db_session, professional, subject)


async def test_consent_without_a_relationship_cannot_exist(db_session):
    """There is nothing to hang it on, which is the point of the shape."""

    owner, _subject = await _patient(db_session, "care-noreal")
    with pytest.raises(relationships.RelationshipNotFound):
        await relationships.grant_consent(
            db_session, relationship_id=uuid.uuid4(), actor_user_id=owner.id
        )


async def test_both_together_open_exactly_what_was_agreed(db_session):
    owner, subject, professional, relationship = await _in_care(
        db_session, "care-both"
    )
    await relationships.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )

    assert await _may(db_session, professional, subject, key="weight")
    assert await _may(db_session, professional, subject, key="labs")
    assert await _may(db_session, professional, subject, key="skincare")
    # Reading is what was agreed to. Writing the patient's own facts is not, and
    # the installation's operational alerts are not the patient's record at all.
    assert not await _may(
        db_session, professional, subject, key="weight", action=PolicyAction.UPDATE
    )
    assert not await _may(db_session, professional, subject, key="system")


async def test_professional_path_is_not_shadowed_by_ambiguous_support_grants(
    db_session,
):
    from vitals.services import support_access_service as support

    owner, subject, professional, relationship = await _in_care(
        db_session, "care-support-dual-role"
    )
    await relationships.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    db_session.add(
        UserRole(
            user_id=professional.id,
            role=UserRoleName.PLATFORM_SUPERADMIN.value,
        )
    )
    await db_session.flush()

    support_grants = {}
    for domain in (Domain.LABS, Domain.NUTRITION):
        request = await support.open_request(
            db_session,
            admin_user_id=professional.id,
            subject_id=subject.id,
            reason=f"Synthetic dual-role {domain.value} check.",
            scopes=support.read_scopes_for((domain,)),
        )
        support_grants[domain] = await support.approve_request(
            db_session, owner_user_id=owner.id, request_id=request.id
        )
    await db_session.commit()

    professional_context = await resolve_access_context(
        db_session, user_id=professional.id, subject_id=subject.id
    )
    assert professional_context.relationship_grant is not None
    assert professional_context.support_grant is None

    selected = await resolve_access_context(
        db_session,
        user_id=professional.id,
        subject_id=subject.id,
        support_grant_id=support_grants[Domain.LABS].id,
    )
    assert selected.relationship_grant is None
    assert selected.support_grant is not None
    assert selected.support_grant.grant_id == support_grants[Domain.LABS].id
    assert is_allowed(
        selected,
        AccessRequest(
            subject_id=subject.id,
            resource_type=PolicyResourceType.DOMAIN,
            resource_key=Domain.LABS.value,
            action=PolicyAction.READ,
        ),
    )
    assert not is_allowed(
        selected,
        AccessRequest(
            subject_id=subject.id,
            resource_type=PolicyResourceType.DOMAIN,
            resource_key=Domain.NUTRITION.value,
            action=PolicyAction.READ,
        ),
    )


async def test_the_role_alone_is_still_nothing(db_session):
    """Holding the doctor role has never been access and must not become it."""

    _owner, subject = await _patient(db_session, "care-role")
    stranger = await _user(
        db_session, "care-role-doctor", roles=(UserRoleName.DOCTOR,)
    )
    assert not await _may(db_session, stranger, subject)


# ── What each kind gets by default ───────────────────────────────────────────


async def test_a_doctor_and_a_trainer_are_offered_the_same_record(db_session):
    """The separation between them is not what each may look at.

    It is that they are two different people, with two relationships and two
    sets of their own notes. Splitting the domains by kind would make the
    narrower choice the one a patient has to know to ask for — and the patient
    already chose whom to invite.
    """

    doctor_scopes = relationships.default_scopes(ProfessionalKind.DOCTOR)
    trainer_scopes = relationships.default_scopes(ProfessionalKind.TRAINER)
    assert doctor_scopes == trainer_scopes

    offered = {scope.resource_key for scope in doctor_scopes}
    for domain in Domain:
        if domain is Domain.SYSTEM:
            # The installation's own operational state, not the patient's.
            assert domain.value not in offered
        else:
            assert domain.value in offered, domain


def test_a_domain_added_later_is_offered_without_anybody_remembering():
    """Derived from the enum, so a new module is not silently invisible.

    Consents already granted are untouched: each stores concrete scope rows
    rather than a reference to this list.
    """

    assert set(relationships.DEFAULT_DOMAINS) == set(Domain) - {Domain.SYSTEM}


async def test_no_default_lets_a_professional_write_a_patients_facts(db_session):
    """A patient's record is theirs. A professional's contribution is their own note.

    The distinction is between the two kinds of resource, not between reading
    and writing as such: a doctor in care has to be able to record what they
    think, and the point of a separate record is that recording it is not
    editing somebody's measurement.
    """

    writing = {
        PolicyAction.CREATE,
        PolicyAction.UPDATE,
        PolicyAction.DELETE,
        PolicyAction.SHARE,
        PolicyAction.EXPORT,
    }
    for kind in ProfessionalKind:
        scopes = relationships.default_scopes(kind)
        fact_actions = {
            scope.action
            for scope in scopes
            if scope.resource_type is PolicyResourceType.DOMAIN
        }
        assert not fact_actions & writing, kind

        # And they can write the thing that is theirs to write.
        authored = {
            (scope.resource_key, scope.action)
            for scope in scopes
            if scope.resource_type is PolicyResourceType.ARTIFACT
        }
        assert ("professional_note", PolicyAction.CREATE) in authored, kind
        # Never delete: a clinical note somebody can make disappear is a worse
        # record than one that stays and is superseded.
        assert not any(action is PolicyAction.DELETE for _key, action in authored)


async def test_a_trainer_sees_the_whole_record_too(db_session):
    owner, subject, professional, relationship = await _in_care(
        db_session, "care-kind", kind=ProfessionalKind.TRAINER
    )
    await relationships.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )

    for key in ("workouts", "labs", "genetics", "hrt"):
        assert await _may(db_session, professional, subject, key=key), key
    # Still read-only, and still not the installation's own alerts.
    assert not await _may(
        db_session, professional, subject, key="labs", action=PolicyAction.UPDATE
    )
    assert not await _may(db_session, professional, subject, key="system")


async def test_a_doctor_cannot_be_taken_on_as_a_trainer(db_session):
    """The physical separation, as a fact rather than a label.

    Without this the kind on a relationship is only what the patient happened to
    type into the invitation, and "my trainer" and "my doctor" stop being
    statements about who these people are.
    """

    from vitals.services.care import professionals

    owner, subject = await _patient(db_session, "care-mismatch")
    doctor = await _user(
        db_session, "care-mismatch-doc", roles=(UserRoleName.DOCTOR,)
    )
    await professionals.submit_profile(
        db_session,
        user_id=doctor.id,
        kind=ProfessionalKind.DOCTOR,
        display_name="Dr Synthetic",
    )

    issued = await invitations.invite(
        db_session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=ProfessionalKind.TRAINER,
        email="care-mismatch-doc@example.test",
    )
    await invitations.accept(
        db_session,
        token=issued.token,
        accepting_user_id=doctor.id,
        verified_email="care-mismatch-doc@example.test",
    )

    with pytest.raises(relationships.KindMismatch):
        await relationships.establish_from_invitation(
            db_session, invitation=issued.invitation
        )


async def test_a_care_invitation_never_promotes_an_ordinary_member(db_session):
    """The token chooses a person; it is not a professional-role grant."""

    owner, subject = await _patient(db_session, "care-member-invite")
    member = await _user(
        db_session,
        "care-member-invite-acceptor",
        roles=(UserRoleName.MEMBER,),
    )
    issued = await invitations.invite(
        db_session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=ProfessionalKind.DOCTOR,
        email="care-member-invite@example.test",
    )
    await invitations.accept(
        db_session,
        token=issued.token,
        accepting_user_id=member.id,
        verified_email="care-member-invite@example.test",
    )

    with pytest.raises(relationships.KindMismatch):
        await relationships.establish_from_invitation(
            db_session, invitation=issued.invitation
        )


async def test_a_professional_with_no_profile_cannot_establish_care(db_session):
    """The cross-subject relationship starts only after operator verification."""

    owner, subject = await _patient(db_session, "care-noprofile")
    professional = await _user(
        db_session,
        "care-noprofile-pro",
        roles=(UserRoleName.TRAINER,),
    )
    issued = await invitations.invite(
        db_session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=ProfessionalKind.TRAINER,
        email="care-noprofile@example.test",
    )
    await invitations.accept(
        db_session,
        token=issued.token,
        accepting_user_id=professional.id,
        verified_email="care-noprofile@example.test",
    )
    with pytest.raises(relationships.ProfessionalNotVerified):
        await relationships.establish_from_invitation(
            db_session, invitation=issued.invitation
        )


@pytest.mark.parametrize(
    "status",
    [
        ProfessionalVerificationStatus.PENDING,
        ProfessionalVerificationStatus.REJECTED,
        ProfessionalVerificationStatus.SUSPENDED,
    ],
)
async def test_only_a_verified_profile_can_establish_care(db_session, status):
    from vitals.services.care import professionals

    slug = f"care-unverified-{status.value}"
    owner, subject = await _patient(db_session, slug)
    professional = await _user(
        db_session, f"{slug}-pro", roles=(UserRoleName.DOCTOR,)
    )
    operator = await _user(
        db_session,
        f"{slug}-operator",
        roles=(UserRoleName.PLATFORM_SUPERADMIN,),
    )
    profile = await professionals.submit_profile(
        db_session,
        user_id=professional.id,
        kind=ProfessionalKind.DOCTOR,
        display_name=f"Dr {slug}",
    )
    if status is ProfessionalVerificationStatus.REJECTED:
        await professionals.decide(
            db_session,
            profile_id=profile.id,
            reviewer_user_id=operator.id,
            expected_status=ProfessionalVerificationStatus.PENDING,
            status=status,
            note="synthetic review refusal",
        )
    elif status is ProfessionalVerificationStatus.SUSPENDED:
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
            note="synthetic review refusal",
        )
    issued = await invitations.invite(
        db_session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=ProfessionalKind.DOCTOR,
        email=f"{slug}@example.test",
    )
    await invitations.accept(
        db_session,
        token=issued.token,
        accepting_user_id=professional.id,
        verified_email=f"{slug}@example.test",
    )

    with pytest.raises(relationships.ProfessionalNotVerified):
        await relationships.establish_from_invitation(
            db_session, invitation=issued.invitation
        )


async def test_losing_the_exact_professional_role_closes_the_next_read(db_session):
    """A different professional role is not a substitute for the invited one."""

    owner, subject, professional, relationship = await _in_care(
        db_session,
        "care-role-revoked",
        kind=ProfessionalKind.TRAINER,
    )
    await relationships.grant_consent(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
    )
    assert await _may(db_session, professional, subject)

    trainer_role = await db_session.scalar(
        select(UserRole).where(
            UserRole.user_id == professional.id,
            UserRole.role == UserRoleName.TRAINER.value,
        )
    )
    await db_session.delete(trainer_role)
    db_session.add(
        UserRole(user_id=professional.id, role=UserRoleName.DOCTOR.value)
    )
    await db_session.flush()

    assert not await _may(db_session, professional, subject)
    assert await relationships.list_professional_roster(
        db_session, professional_user_id=professional.id
    ) == []


async def test_suspending_a_profile_closes_the_next_read(db_session):
    from vitals.models.professional import ProfessionalProfile
    from vitals.services.care import professionals

    owner, subject, professional, relationship = await _in_care(
        db_session, "care-profile-suspended"
    )
    await relationships.grant_consent(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
    )
    assert await _may(db_session, professional, subject)

    profile = await db_session.scalar(
        select(ProfessionalProfile).where(
            ProfessionalProfile.user_id == professional.id
        )
    )
    operator = await _user(
        db_session,
        "care-profile-suspended-operator-2",
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

    assert not await _may(db_session, professional, subject)


async def test_a_suspended_profile_disappears_from_the_cross_subject_roster(
    db_session,
):
    from vitals.models.professional import ProfessionalProfile
    from vitals.services.care import professionals

    owner, subject, professional, relationship = await _in_care(
        db_session, "care-profile-roster-suspended"
    )
    await relationships.grant_consent(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
    )
    assert [row.subject_id for row in await relationships.list_professional_roster(
        db_session, professional_user_id=professional.id
    )] == [subject.id]

    profile = await db_session.scalar(
        select(ProfessionalProfile).where(
            ProfessionalProfile.user_id == professional.id
        )
    )
    operator = await _user(
        db_session,
        "care-profile-roster-suspended-operator-2",
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

    assert await relationships.list_professional_roster(
        db_session, professional_user_id=professional.id
    ) == []


async def test_the_patient_can_narrow_what_was_offered(db_session):
    owner, subject, professional, relationship = await _in_care(
        db_session, "care-narrow"
    )
    await relationships.grant_consent(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
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

    assert await _may(db_session, professional, subject, key="weight")
    assert not await _may(db_session, professional, subject, key="labs")
    assert not await _may(
        db_session, professional, subject, key="weight", action=PolicyAction.LIST
    )


async def test_a_consent_that_permits_nothing_is_a_revocation(db_session):
    owner, _subject, _pro, relationship = await _in_care(db_session, "care-empty")
    with pytest.raises(relationships.CareValidationError, match="revoke"):
        await relationships.grant_consent(
            db_session,
            relationship_id=relationship.id,
            actor_user_id=owner.id,
            scopes=frozenset(),
        )


# ── Versioning ───────────────────────────────────────────────────────────────


async def test_narrowing_is_a_new_version_rather_than_an_edit(db_session):
    """So that "what applied last month" survives this month's change.

    An updated row cannot answer that, and it is the question any later dispute
    is actually about.
    """

    from sqlalchemy import func, select

    from vitals.models.professional import ConsentGrant, ConsentScope

    async def _scope_count(grant_id):
        # Counted rather than read off the relationship: a lazy load inside an
        # async session has no greenlet to load in.
        return int(
            await db_session.scalar(
                select(func.count())
                .select_from(ConsentScope)
                .where(ConsentScope.consent_grant_id == grant_id)
            )
            or 0
        )

    owner, _subject, _pro, relationship = await _in_care(db_session, "care-version")
    first = await relationships.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    first_id, first_scopes = first.id, await _scope_count(first.id)

    second = await relationships.grant_consent(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
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
    assert second.version == first.version + 1
    assert second.id != first_id

    superseded = await db_session.get(ConsentGrant, first_id)
    assert superseded.status == ConsentStatus.SUPERSEDED.value
    # Its rows are still there: what was permitted, and until when.
    assert await _scope_count(first_id) == first_scopes
    assert first_scopes > 0

    live = (
        await db_session.scalars(
            select(ConsentGrant).where(
                ConsentGrant.relationship_id == relationship.id,
                ConsentGrant.status.in_(("active", "paused")),
            )
        )
    ).all()
    assert [grant.id for grant in live] == [second.id]


# ── Pausing, revoking, ending ────────────────────────────────────────────────


async def test_a_pause_closes_access_and_resuming_reopens_it(db_session):
    """A break must not cost a new invitation and a new consent."""

    owner, subject, professional, relationship = await _in_care(
        db_session, "care-pause"
    )
    await relationships.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    assert await _may(db_session, professional, subject)

    await relationships.set_consent_paused(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
        paused=True,
    )
    assert not await _may(db_session, professional, subject)

    await relationships.set_consent_paused(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
        paused=False,
    )
    assert await _may(db_session, professional, subject)


async def test_revocation_takes_effect_on_the_very_next_decision(db_session):
    """Not on the next login, not on the next cache expiry — the next request."""

    owner, subject, professional, relationship = await _in_care(
        db_session, "care-revoke"
    )
    await relationships.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    assert await _may(db_session, professional, subject)

    await relationships.revoke_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    assert not await _may(db_session, professional, subject)


async def test_a_revocation_does_not_come_back(db_session):
    owner, _subject, _pro, relationship = await _in_care(db_session, "care-norevive")
    await relationships.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    await relationships.revoke_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    with pytest.raises(relationships.ConsentNotFound):
        await relationships.set_consent_paused(
            db_session,
            relationship_id=relationship.id,
            actor_user_id=owner.id,
            paused=False,
        )


async def test_ending_the_relationship_revokes_what_was_under_it(db_session):
    """A live consent behind an ended relationship is a permission in waiting."""

    from vitals.models.professional import ConsentGrant

    owner, subject, professional, relationship = await _in_care(
        db_session, "care-end"
    )
    grant = await relationships.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    await relationships.end_relationship(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )

    assert relationship.status == CareRelationshipStatus.ENDED.value
    reloaded = await db_session.get(ConsentGrant, grant.id)
    assert reloaded.status == ConsentStatus.REVOKED.value
    assert not await _may(db_session, professional, subject)


async def test_either_party_may_end_it(db_session):
    """Care is not something one side holds the other in."""

    _owner, _subject, professional, relationship = await _in_care(
        db_session, "care-end-pro"
    )
    ended = await relationships.end_relationship(
        db_session, relationship_id=relationship.id, actor_user_id=professional.id
    )
    assert ended.status == CareRelationshipStatus.ENDED.value
    assert ended.ended_by_user_id == professional.id


async def test_a_stranger_ends_nothing(db_session):
    _owner, _subject, _pro, relationship = await _in_care(db_session, "care-end-str")
    stranger = await _user(db_session, "care-end-stranger")
    with pytest.raises(relationships.NotTheSubjectOwner):
        await relationships.end_relationship(
            db_session, relationship_id=relationship.id, actor_user_id=stranger.id
        )


async def test_only_the_patient_decides_what_is_shown(db_session):
    """And somebody else's relationship does not exist rather than being refused.

    The ownership condition is in the query, not a check after the read. Told
    apart, "not yours" and "no such thing" would let a caller enumerate which
    relationships exist by trying ids.
    """

    _owner, _subject, professional, relationship = await _in_care(
        db_session, "care-consent-owner"
    )
    for actor in (professional.id, uuid.uuid4()):
        with pytest.raises(relationships.RelationshipNotFound):
            await relationships.grant_consent(
                db_session, relationship_id=relationship.id, actor_user_id=actor
            )

    # A relationship that genuinely is not there answers identically.
    with pytest.raises(relationships.RelationshipNotFound):
        await relationships.grant_consent(
            db_session, relationship_id=uuid.uuid4(), actor_user_id=professional.id
        )


# ── Expiry ───────────────────────────────────────────────────────────────────


async def test_consent_lapses_rather_than_standing_forever(db_session):
    """Consent nobody revisits is consent nobody withdrew either.

    The grant is aged rather than given a zero term. ``ck_consent_grants_
    positive_ttl`` forbids ``expires_at <= granted_at``, so the shortcut this
    used to take — setting the expiry equal to the grant — built a row
    PostgreSQL rejects outright, and the test errored in its own setup on the
    only database that runs in production. Moving both timestamps into the past
    keeps the term positive and is what an expired grant actually looks like.
    """

    from datetime import datetime, timezone

    owner, subject, professional, relationship = await _in_care(
        db_session, "care-expiry"
    )
    grant = await relationships.grant_consent(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
        ttl=timedelta(days=1),
    )
    assert await _may(db_session, professional, subject)

    now = datetime.now(timezone.utc)
    grant.granted_at = now - timedelta(days=30)
    grant.expires_at = now - timedelta(days=29)
    await db_session.flush()
    assert not await _may(db_session, professional, subject)


async def test_a_zero_or_negative_term_is_refused(db_session):
    owner, _subject, _pro, relationship = await _in_care(db_session, "care-ttl")
    for ttl in (timedelta(0), timedelta(days=-1), "a fortnight", None):
        with pytest.raises(relationships.CareValidationError):
            await relationships.grant_consent(
                db_session,
                relationship_id=relationship.id,
                actor_user_id=owner.id,
                ttl=ttl,
            )


# ── The owner's own access is untouched ──────────────────────────────────────


async def test_the_patient_still_reaches_their_own_record(db_session):
    """Self-ownership is its own basis and must not start depending on a grant."""

    owner, subject, _pro, _rel = await _in_care(db_session, "care-self")
    context = await resolve_access_context(
        db_session, user_id=owner.id, subject_id=subject.id
    )
    assert context.relationship_grant is None
    for action in PolicyAction:
        assert is_allowed(
            context,
            AccessRequest(
                subject_id=subject.id,
                resource_type=PolicyResourceType.DOMAIN,
                resource_key="weight",
                action=action,
            ),
        ), action


async def test_one_live_relationship_per_pair(db_session):
    """Two would mean two sets of consent and no rule about which applies."""

    from sqlalchemy.exc import IntegrityError

    from vitals.models.professional import CareRelationship

    owner, subject, professional, _rel = await _in_care(db_session, "care-dup")
    db_session.add(
        CareRelationship(
            subject_id=subject.id,
            subject_owner_user_id=owner.id,
            professional_user_id=professional.id,
            kind=ProfessionalKind.DOCTOR.value,
            status=CareRelationshipStatus.ACTIVE.value,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
