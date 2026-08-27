"""A link that lets one professional into one patient's record, once.

Most of the design is a list of things a link must not be, and each of those is
a test below.

It must not outlive its purpose, so it expires and it is one-time. It must not
be usable by whoever it was forwarded to, so it is bound to an address, and the
address has to be a *verified* claim — an unverified one is somebody asserting
they own a mailbox, which is the thing the binding exists to stop. It must not
be reconstructible from a database copy, so only its hash is stored. And a
refusal must not be informative: spent, expired, wrong address and never existed
all answer identically, because those answers together are a map of who is being
treated by whom.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from vitals.enums import (
    ProfessionalInvitationStatus,
    ProfessionalKind,
    ProfessionalVerificationStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.professional import ProfessionalProfile
from vitals.services.care import invitations
from vitals.utils.timeutils import now_utc


async def _user(
    session,
    slug: str,
    *,
    status=UserStatus.ACTIVE,
    email: str | None = None,
    qualified: bool = False,
) -> User:
    user = User(
        username=slug,
        normalized_username=slug,
        email=email,
        normalized_email=email,
        email_verified_at=now_utc() if email is not None else None,
        password_hash="$synthetic-test-hash",
        status=status.value,
    )
    session.add(user)
    await session.flush()
    if qualified:
        session.add(UserRole(user_id=user.id, role=UserRoleName.DOCTOR.value))
        session.add(
            ProfessionalProfile(
                user_id=user.id,
                kind=ProfessionalKind.DOCTOR.value,
                verification_status=ProfessionalVerificationStatus.VERIFIED.value,
                display_name=f"Dr {slug}",
                verified_at=now_utc(),
                verified_by_user_id=user.id,
            )
        )
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


async def _offer(session, *, slug: str, email="doctor@example.test", ttl=None):
    owner, subject = await _patient(session, slug)
    issued = await invitations.invite(
        session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=ProfessionalKind.DOCTOR,
        email=email,
        **({"ttl": ttl} if ttl is not None else {}),
    )
    return owner, subject, issued


# ── What is stored, and what is not ──────────────────────────────────────────


async def test_the_token_is_never_written_down(db_session):
    """A copy of this table is not a set of working invitations."""

    import hashlib

    _owner, _subject, issued = await _offer(db_session, slug="inv-hash")
    row = issued.invitation

    assert issued.token
    assert issued.token not in (row.token_hash, row.invited_email)
    assert row.token_hash == hashlib.sha256(issued.token.encode()).hexdigest()
    assert len(row.token_hash) == 64


async def test_two_invitations_never_share_a_token(db_session):
    owner, subject = await _patient(db_session, "inv-unique")
    tokens = set()
    for index in range(5):
        issued = await invitations.invite(
            db_session,
            subject_id=subject.id,
            actor_user_id=owner.id,
            kind=ProfessionalKind.DOCTOR,
            email=f"doctor{index}@example.test",
        )
        tokens.add(issued.token)
    assert len(tokens) == 5


async def test_only_the_owner_of_the_record_may_offer_it(db_session):
    """It is their record; nobody else gets to hand it out."""

    _owner, subject = await _patient(db_session, "inv-owner")
    stranger = await _user(db_session, "inv-stranger")

    with pytest.raises(invitations.NotTheSubjectOwner):
        await invitations.invite(
            db_session,
            subject_id=subject.id,
            actor_user_id=stranger.id,
            kind=ProfessionalKind.DOCTOR,
            email="doctor@example.test",
        )


@pytest.mark.parametrize(
    "email", ["", "   ", "not-an-address", None, 7, "x" * 320 + "@e.test"]
)
async def test_an_offer_needs_a_usable_address(db_session, email):
    owner, subject = await _patient(
        db_session, f"inv-email-{abs(hash(str(email))) % 9999}"
    )
    with pytest.raises(invitations.InvitationValidationError):
        await invitations.invite(
            db_session,
            subject_id=subject.id,
            actor_user_id=owner.id,
            kind=ProfessionalKind.DOCTOR,
            email=email,
        )


def test_folding_an_address_stays_shallow():
    """No provider-specific cleverness.

    Dropping dots or ``+`` tags is right for exactly one mail provider and wrong
    for the rest, and being wrong here means an invitation the intended person
    cannot accept.
    """

    assert invitations.normalize_email("  Doctor@Example.TEST ") == (
        "doctor@example.test"
    )
    assert invitations.normalize_email("first.last+tag@example.test") == (
        "first.last+tag@example.test"
    )


# ── Accepting it ─────────────────────────────────────────────────────────────


async def test_the_addressed_person_can_accept_once(db_session):
    _owner, _subject, issued = await _offer(db_session, slug="inv-accept")
    doctor = await _user(
        db_session,
        "inv-accept-doctor",
        email="doctor@example.test",
        qualified=True,
    )

    accepted = await invitations.accept(
        db_session,
        token=issued.token,
        accepting_user_id=doctor.id,
        verified_email="doctor@example.test",
    )
    assert accepted.status == ProfessionalInvitationStatus.ACCEPTED.value
    assert accepted.accepted_by_user_id == doctor.id
    assert accepted.accepted_at is not None

    # One-time. The second attempt is a refusal like any other.
    with pytest.raises(invitations.InvitationRefused):
        await invitations.accept(
            db_session,
            token=issued.token,
            accepting_user_id=doctor.id,
            verified_email="doctor@example.test",
        )


async def test_the_address_is_matched_however_it_was_typed(db_session):
    _owner, _subject, issued = await _offer(
        db_session, slug="inv-fold", email="Doctor@Example.TEST"
    )
    doctor = await _user(
        db_session,
        "inv-fold-doctor",
        email="doctor@example.test",
        qualified=True,
    )

    accepted = await invitations.accept(
        db_session,
        token=issued.token,
        accepting_user_id=doctor.id,
        verified_email="  doctor@example.test  ",
    )
    assert accepted.status == ProfessionalInvitationStatus.ACCEPTED.value


async def test_a_forwarded_link_does_not_work(db_session):
    """The patient chose a person, not a mailbox."""

    _owner, _subject, issued = await _offer(db_session, slug="inv-forward")
    somebody_else = await _user(db_session, "inv-forward-other")

    with pytest.raises(invitations.InvitationRefused):
        await invitations.accept(
            db_session,
            token=issued.token,
            accepting_user_id=somebody_else.id,
            verified_email="someone.else@example.test",
        )


async def test_an_unverified_address_is_not_an_address(db_session):
    """Otherwise the binding is to whoever claims the mailbox first."""

    _owner, _subject, issued = await _offer(db_session, slug="inv-unverified")
    doctor = await _user(db_session, "inv-unverified-doctor")

    with pytest.raises(invitations.InvitationRefused):
        await invitations.accept(
            db_session,
            token=issued.token,
            accepting_user_id=doctor.id,
            verified_email=None,
        )


def _age_out(invitation) -> None:
    """Make an offer lapsed the way time does, not the way a shortcut does.

    Both timestamps move back together so ``expires_at`` stays strictly after
    ``created_at``. The schema requires that — an offer that expires the instant
    it is issued is not a state the service can produce — and PostgreSQL
    enforces it where SQLite lets it through.
    """

    lapsed = invitation.created_at - timedelta(days=30)
    invitation.created_at = lapsed
    invitation.expires_at = lapsed + timedelta(days=7)


async def test_an_expired_offer_is_refused_and_stops_claiming_to_be_open(db_session):
    """The state is corrected on the way past, so the list stops lying."""

    _owner, _subject, issued = await _offer(
        db_session, slug="inv-expired", ttl=timedelta(seconds=1)
    )
    doctor = await _user(db_session, "inv-expired-doctor")

    # Reach past the clock rather than waiting on it — by moving the whole
    # offer into the past, not by pulling its expiry back onto its issue time.
    # ``ck_professional_invitations_positive_ttl`` forbids
    # ``expires_at <= created_at``, so the shortcut builds a row PostgreSQL
    # rejects outright and the test errors in its own setup on the only
    # database production runs. Aged, the term stays positive and the row is
    # what a lapsed offer actually looks like.
    _age_out(issued.invitation)
    await db_session.flush()

    with pytest.raises(invitations.InvitationRefused):
        await invitations.accept(
            db_session,
            token=issued.token,
            accepting_user_id=doctor.id,
            verified_email="doctor@example.test",
        )
    if db_session.get_bind().dialect.name == "postgresql":
        # The definer routine performs the expiry update without returning the
        # refused row's identifier.  This admin-backed compatibility fixture
        # may refresh the object it already held; the restricted runtime path
        # intentionally learns no subject from an expired token.
        await db_session.refresh(issued.invitation)
    assert issued.invitation.status == ProfessionalInvitationStatus.EXPIRED.value


async def test_a_suspended_account_cannot_accept(db_session):
    _owner, _subject, issued = await _offer(db_session, slug="inv-suspended")
    doctor = await _user(
        db_session, "inv-suspended-doctor", status=UserStatus.SUSPENDED
    )

    with pytest.raises(invitations.InvitationRefused):
        await invitations.accept(
            db_session,
            token=issued.token,
            accepting_user_id=doctor.id,
            verified_email="doctor@example.test",
        )


async def test_the_patient_cannot_be_their_own_professional(db_session):
    """A relationship naming one person twice has no second party to consent."""

    owner, _subject, issued = await _offer(db_session, slug="inv-self")

    with pytest.raises(invitations.InvitationRefused):
        await invitations.accept(
            db_session,
            token=issued.token,
            accepting_user_id=owner.id,
            verified_email="doctor@example.test",
        )


async def test_every_refusal_says_exactly_the_same_thing(db_session):
    """Five different facts, one answer.

    A caller able to tell them apart could ask "is this address being treated
    here?" and get a reliable answer, one address at a time.
    """

    _owner, _subject, issued = await _offer(db_session, slug="inv-uniform")
    doctor = await _user(
        db_session,
        "inv-uniform-doctor",
        email="doctor@example.test",
        qualified=True,
    )
    await invitations.accept(
        db_session,
        token=issued.token,
        accepting_user_id=doctor.id,
        verified_email="doctor@example.test",
    )

    _o2, _s2, expired = await _offer(db_session, slug="inv-uniform-expired")
    _age_out(expired.invitation)
    _o3, _s3, wrong = await _offer(db_session, slug="inv-uniform-wrong")
    await db_session.flush()

    messages = set()
    attempts = (
        (issued.token, "doctor@example.test"),          # already spent
        (expired.token, "doctor@example.test"),         # out of time
        (wrong.token, "somebody@example.test"),         # not the addressee
        ("never-issued-token", "doctor@example.test"),  # no such thing
        ("", "doctor@example.test"),                    # not a token at all
    )
    for token, email in attempts:
        with pytest.raises(invitations.InvitationRefused) as caught:
            await invitations.accept(
                db_session,
                token=token,
                accepting_user_id=doctor.id,
                verified_email=email,
            )
        messages.add(str(caught.value))
    assert len(messages) == 1, messages


# ── Withdrawing it ───────────────────────────────────────────────────────────


async def test_the_owner_can_withdraw_an_offer_nobody_took_up(db_session):
    owner, _subject, issued = await _offer(db_session, slug="inv-revoke")
    doctor = await _user(db_session, "inv-revoke-doctor")

    revoked = await invitations.revoke(
        db_session, invitation_id=issued.invitation.id, actor_user_id=owner.id
    )
    assert revoked.status == ProfessionalInvitationStatus.REVOKED.value
    assert revoked.revoked_at is not None

    with pytest.raises(invitations.InvitationRefused):
        await invitations.accept(
            db_session,
            token=issued.token,
            accepting_user_id=doctor.id,
            verified_email="doctor@example.test",
        )


async def test_withdrawing_twice_is_the_same_as_withdrawing_once(db_session):
    owner, _subject, issued = await _offer(db_session, slug="inv-revoke-twice")
    await invitations.revoke(
        db_session, invitation_id=issued.invitation.id, actor_user_id=owner.id
    )
    again = await invitations.revoke(
        db_session, invitation_id=issued.invitation.id, actor_user_id=owner.id
    )
    assert again.status == ProfessionalInvitationStatus.REVOKED.value


async def test_an_accepted_offer_is_not_withdrawn_here(db_session):
    """By then it is a relationship, and ending one has its own record."""

    owner, _subject, issued = await _offer(db_session, slug="inv-revoke-accepted")
    doctor = await _user(
        db_session,
        "inv-revoke-accepted-doctor",
        email="doctor@example.test",
        qualified=True,
    )
    await invitations.accept(
        db_session,
        token=issued.token,
        accepting_user_id=doctor.id,
        verified_email="doctor@example.test",
    )

    with pytest.raises(invitations.InvitationError):
        await invitations.revoke(
            db_session, invitation_id=issued.invitation.id, actor_user_id=owner.id
        )


async def test_only_the_owner_withdraws(db_session):
    _owner, _subject, issued = await _offer(db_session, slug="inv-revoke-owner")
    stranger = await _user(db_session, "inv-revoke-stranger")

    with pytest.raises(invitations.NotTheSubjectOwner):
        await invitations.revoke(
            db_session,
            invitation_id=issued.invitation.id,
            actor_user_id=stranger.id,
        )


async def test_withdrawing_something_that_is_not_there(db_session):
    owner, _subject = await _patient(db_session, "inv-revoke-missing")
    with pytest.raises(invitations.InvitationRefused):
        await invitations.revoke(
            db_session, invitation_id=uuid.uuid4(), actor_user_id=owner.id
        )


# ── What accepting still is not ──────────────────────────────────────────────


async def test_accepting_reaches_nothing_by_itself(db_session):
    """It creates one half of the pair. Consent is the other, and it is separate.

    A professional who has accepted an invitation and holds no consent is still
    a stranger to the record — which is why acceptance is a state on a row here
    rather than anything the policy engine reads.
    """

    from vitals.access import (
        AccessRequest,
        PolicyAction,
        PolicyResourceType,
        is_allowed,
    )
    from vitals.services.authorization.subject_access import resolve_access_context

    _owner, subject, issued = await _offer(db_session, slug="inv-not-access")
    doctor = await _user(
        db_session,
        "inv-not-access-doctor",
        email="doctor@example.test",
        qualified=db_session.get_bind().dialect.name == "postgresql",
    )
    await invitations.accept(
        db_session,
        token=issued.token,
        accepting_user_id=doctor.id,
        verified_email="doctor@example.test",
    )

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
