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
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.services import care_service, invitation_service
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
    issued = await invitation_service.invite(
        session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=kind,
        email=f"{slug}-pro@example.test",
    )
    await invitation_service.accept(
        session,
        token=issued.token,
        accepting_user_id=professional.id,
        verified_email=f"{slug}-pro@example.test",
    )
    relationship = await care_service.establish_from_invitation(
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


# ── Neither half is sufficient ───────────────────────────────────────────────


async def test_a_relationship_without_consent_reaches_nothing(db_session):
    """The correct and ordinary state right after an invitation is accepted."""

    _owner, subject, professional, _rel = await _in_care(db_session, "care-nocons")
    assert not await _may(db_session, professional, subject)


async def test_consent_without_a_relationship_cannot_exist(db_session):
    """There is nothing to hang it on, which is the point of the shape."""

    owner, _subject = await _patient(db_session, "care-noreal")
    with pytest.raises(care_service.RelationshipNotFound):
        await care_service.grant_consent(
            db_session, relationship_id=uuid.uuid4(), actor_user_id=owner.id
        )


async def test_both_together_open_exactly_what_was_agreed(db_session):
    owner, subject, professional, relationship = await _in_care(
        db_session, "care-both"
    )
    await care_service.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )

    assert await _may(db_session, professional, subject, key="weight")
    assert await _may(db_session, professional, subject, key="labs")
    # Not in the doctor default set, so not agreed to.
    assert not await _may(db_session, professional, subject, key="skincare")


async def test_the_role_alone_is_still_nothing(db_session):
    """Holding the doctor role has never been access and must not become it."""

    _owner, subject = await _patient(db_session, "care-role")
    stranger = await _user(
        db_session, "care-role-doctor", roles=(UserRoleName.DOCTOR,)
    )
    assert not await _may(db_session, stranger, subject)


# ── What each kind gets by default ───────────────────────────────────────────


async def test_a_doctor_and_a_trainer_do_not_see_the_same_things(db_session):
    """The split is about what the work needs, not about seniority.

    A trainer planning sessions needs load, bodyweight and recovery. They do not
    need a genome, a hormone schedule or a lab panel to do it, and defaulting
    them in would make the narrower choice the one a patient has to know to ask
    for.
    """

    doctor_scopes = care_service.default_scopes(ProfessionalKind.DOCTOR)
    trainer_scopes = care_service.default_scopes(ProfessionalKind.TRAINER)

    doctor_domains = {scope.resource_key for scope in doctor_scopes}
    trainer_domains = {scope.resource_key for scope in trainer_scopes}

    for clinical in (Domain.LABS, Domain.GENETICS, Domain.HRT, Domain.GLP1):
        assert clinical.value in doctor_domains
        assert clinical.value not in trainer_domains, clinical
    assert Domain.WORKOUTS.value in trainer_domains
    assert trainer_domains < doctor_domains | trainer_domains


async def test_no_default_lets_a_professional_write_a_patients_facts(db_session):
    """A patient's record is theirs. A professional's contribution is their own note."""

    writing = {
        PolicyAction.CREATE,
        PolicyAction.UPDATE,
        PolicyAction.DELETE,
        PolicyAction.SHARE,
        PolicyAction.EXPORT,
    }
    for kind in ProfessionalKind:
        actions = {scope.action for scope in care_service.default_scopes(kind)}
        assert not actions & writing, kind


async def test_a_trainer_relationship_gets_a_trainers_defaults(db_session):
    """Even when the same account could also be a doctor elsewhere.

    The kind is on the relationship, not read from the profile, precisely so
    one account holding both cannot take the wider of the two.
    """

    owner, subject, professional, relationship = await _in_care(
        db_session, "care-kind", kind=ProfessionalKind.TRAINER
    )
    db_session.add(UserRole(user_id=professional.id, role=UserRoleName.DOCTOR.value))
    await db_session.flush()
    await care_service.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )

    assert await _may(db_session, professional, subject, key="workouts")
    assert not await _may(db_session, professional, subject, key="labs")


async def test_the_patient_can_narrow_what_was_offered(db_session):
    owner, subject, professional, relationship = await _in_care(
        db_session, "care-narrow"
    )
    await care_service.grant_consent(
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
    with pytest.raises(care_service.CareValidationError, match="revoke"):
        await care_service.grant_consent(
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
    first = await care_service.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    first_id, first_scopes = first.id, await _scope_count(first.id)

    second = await care_service.grant_consent(
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
    await care_service.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    assert await _may(db_session, professional, subject)

    await care_service.set_consent_paused(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
        paused=True,
    )
    assert not await _may(db_session, professional, subject)

    await care_service.set_consent_paused(
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
    await care_service.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    assert await _may(db_session, professional, subject)

    await care_service.revoke_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    assert not await _may(db_session, professional, subject)


async def test_a_revocation_does_not_come_back(db_session):
    owner, _subject, _pro, relationship = await _in_care(db_session, "care-norevive")
    await care_service.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    await care_service.revoke_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    with pytest.raises(care_service.ConsentNotFound):
        await care_service.set_consent_paused(
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
    grant = await care_service.grant_consent(
        db_session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    await care_service.end_relationship(
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
    ended = await care_service.end_relationship(
        db_session, relationship_id=relationship.id, actor_user_id=professional.id
    )
    assert ended.status == CareRelationshipStatus.ENDED.value
    assert ended.ended_by_user_id == professional.id


async def test_a_stranger_ends_nothing(db_session):
    _owner, _subject, _pro, relationship = await _in_care(db_session, "care-end-str")
    stranger = await _user(db_session, "care-end-stranger")
    with pytest.raises(care_service.NotTheSubjectOwner):
        await care_service.end_relationship(
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
        with pytest.raises(care_service.RelationshipNotFound):
            await care_service.grant_consent(
                db_session, relationship_id=relationship.id, actor_user_id=actor
            )

    # A relationship that genuinely is not there answers identically.
    with pytest.raises(care_service.RelationshipNotFound):
        await care_service.grant_consent(
            db_session, relationship_id=uuid.uuid4(), actor_user_id=professional.id
        )


# ── Expiry ───────────────────────────────────────────────────────────────────


async def test_consent_lapses_rather_than_standing_forever(db_session):
    """Consent nobody revisits is consent nobody withdrew either."""

    owner, subject, professional, relationship = await _in_care(
        db_session, "care-expiry"
    )
    grant = await care_service.grant_consent(
        db_session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
        ttl=timedelta(days=1),
    )
    assert await _may(db_session, professional, subject)

    grant.expires_at = grant.granted_at
    await db_session.flush()
    assert not await _may(db_session, professional, subject)


async def test_a_zero_or_negative_term_is_refused(db_session):
    owner, _subject, _pro, relationship = await _in_care(db_session, "care-ttl")
    for ttl in (timedelta(0), timedelta(days=-1), "a fortnight", None):
        with pytest.raises(care_service.CareValidationError):
            await care_service.grant_consent(
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
