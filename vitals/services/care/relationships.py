"""A professional in care for a patient, and what that patient agreed to show.

Two records, and access needs both live at the moment of the request. Neither is
sufficient and the asymmetry between them is deliberate: a relationship with no
live consent is an ordinary, correct state — somebody the patient agreed to work
with and has not yet, or no longer, agreed to show anything to.

Consent is versioned rather than edited. Narrowing what somebody may read is a
new version superseding the old, so "what was this professional allowed to see
on the day they read it" stays answerable. An updated row cannot answer that,
and it is the question any later dispute is actually about.

What this module does *not* do is decide anything. It loads a
:class:`~vitals.access.RelationshipGrant` and hands it to the pure policy in
``vitals.access``, which checks the actor, the lifecycle, the expiry and the
exact requested scope on every single decision. Nothing here is a shortcut past
that, and a grant this module returns is still refused by the policy if any of
those fail.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from vitals.access import AccessScope, PolicyAction, PolicyResourceType, RelationshipGrant
from vitals.enums import (
    CareRelationshipStatus,
    ConsentStatus,
    Domain,
    ProfessionalInvitationStatus,
    ProfessionalKind,
)
from vitals.models.identity import HealthSubject
from vitals.models.care_thread import CareMessage, CareThreadParticipant
from vitals.models.professional import (
    CareRelationship,
    ConsentGrant,
    ConsentScope,
    ProfessionalInvitation,
    ProfessionalProfile,
)
from vitals.services.identity_service import acquire_identity_governance_lock

#: How long a consent stands before the patient has to say so again. Consent
#: that never lapses is consent nobody revisits, and a professional who stopped
#: being involved two years ago should not still be reading.
DEFAULT_CONSENT_TTL = timedelta(days=365)

#: What a professional may do to a patient's own facts by default: look at them.
#: Never create, update or delete — a patient's record is theirs, and a
#: professional's contribution belongs in their own note rather than inside
#: somebody else's measurement.
READ_ONLY_ACTIONS: tuple[PolicyAction, ...] = (
    PolicyAction.READ,
    PolicyAction.LIST,
    PolicyAction.SEARCH,
)

#: The artifacts a professional writes themselves, and what they may do to them.
#: Writing here is not an exception to the read-only rule — it is where the rule
#: sends them. A doctor in care has to be able to record what they think, and
#: the whole point of a separate record is that recording it does not mean
#: editing the patient's measurements.
#:
#: Deleting is absent on purpose. A clinical note somebody can make disappear is
#: a worse record than one that stays and is superseded, and the patient cannot
#: consent to a history they will not be able to see.
AUTHORED_ARTIFACTS: tuple[str, ...] = ("professional_note", "care_plan")
AUTHORED_ACTIONS: tuple[PolicyAction, ...] = (
    PolicyAction.READ,
    PolicyAction.LIST,
    PolicyAction.CREATE,
    PolicyAction.UPDATE,
)

#: Talking to the patient, as an operation rather than an artifact. The two
#: actions are separately revocable on purpose: a patient who wants a doctor to
#: be able to look back at what was said without being able to add to it can
#: withdraw ``message`` and keep ``read``, and that is a narrowing worth being
#: able to express. Withdrawing both closes the conversation to them without
#: deleting it.
#:
#: It is in the default set because a care team that cannot talk to the patient
#: is not what anybody invites one for. The patient can pass their own scopes
#: and leave it out.
MESSAGE_OPERATION: str = "care_team.message"
MESSAGE_ACTIONS: tuple[PolicyAction, ...] = (
    PolicyAction.READ,
    PolicyAction.MESSAGE,
)

#: Every domain that describes the patient, and the same set whichever kind of
#: professional it is. The separation between a doctor and a trainer is not what
#: each may look at — it is that they are two different people with two
#: relationships and two sets of their own notes.
#:
#: Deriving the list rather than writing it out is deliberate. A domain added
#: later is one the patient has, so it belongs in what a new consent offers; a
#: hand-written list would leave it invisible until somebody remembered. Consents
#: already granted are untouched either way, because each one stores concrete
#: scope rows rather than a reference to this.
#:
#: ``SYSTEM`` is the exception, and not because it is sensitive. It is the
#: installation's own operational state — scheduler alerts, ingestion failures —
#: which is not something about the patient at all, and the application already
#: keeps it off patient-facing surfaces through
#: ``alerts_service.is_platform_alert_key``.
DEFAULT_DOMAINS: tuple[Domain, ...] = tuple(
    domain for domain in Domain if domain is not Domain.SYSTEM
)


class CareError(RuntimeError):
    """Base class for relationship and consent failures."""


class CareValidationError(ValueError):
    """A submitted value is not usable."""


class NotTheSubjectOwner(CareError):
    """Only the patient decides who is in care for them and what they may see."""


class KindMismatch(CareError):
    """This professional is not the kind of professional they were invited as."""


class RelationshipNotFound(CareError):
    """No such relationship, or none this actor may act on."""


class ConsentNotFound(CareError):
    """No live consent to act on."""


@dataclass(frozen=True, slots=True)
class CareRosterEntry:
    """One professional-to-patient relationship as it should appear today."""

    relationship_id: uuid.UUID
    subject_id: uuid.UUID
    display_name: str
    kind: str
    relationship_status: str
    consent_status: str | None
    consent_expires_at: datetime | None
    open: bool
    consent_expired: bool
    unread_threads: int
    last_message_at: datetime | None


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _now(session: AsyncSession) -> datetime:
    stamp = await session.scalar(select(func.now()))
    return _as_utc(stamp) if stamp is not None else datetime.now(timezone.utc)


def default_scopes(kind: ProfessionalKind | str) -> frozenset[AccessScope]:
    """What a consent offers before the patient narrows it.

    A starting point, never a floor: the patient can pass their own set, and
    everything here is written into ``consent_scopes`` as concrete rows, so a
    consent granted today keeps meaning what it meant even if this function
    changes tomorrow.

    ``kind`` does not change the answer, and it is still a parameter rather than
    being dropped. The kind is a real distinction — it decides which
    professional this is, and a doctor and a trainer are two different people
    with two relationships and two sets of their own notes — it just is not a
    distinction about what may be *looked at*. Splitting the domains by kind
    would make the narrower choice the one a patient has to know to ask for,
    and the patient chose whom to invite.
    """

    if not isinstance(kind, ProfessionalKind):
        # Validated rather than ignored: an unknown kind reaching here means a
        # relationship was written with one, and that is worth failing on.
        ProfessionalKind(str(kind))
    facts = {
        AccessScope(
            resource_type=PolicyResourceType.DOMAIN,
            resource_key=domain.value,
            action=action,
        )
        for domain in DEFAULT_DOMAINS
        for action in READ_ONLY_ACTIONS
    }
    authored = {
        AccessScope(
            resource_type=PolicyResourceType.ARTIFACT,
            resource_key=artifact,
            action=action,
        )
        for artifact in AUTHORED_ARTIFACTS
        for action in AUTHORED_ACTIONS
    }
    conversation = {
        AccessScope(
            resource_type=PolicyResourceType.OPERATION,
            resource_key=MESSAGE_OPERATION,
            action=action,
        )
        for action in MESSAGE_ACTIONS
    }
    return frozenset(facts | authored | conversation)


async def list_professional_roster(
    session: AsyncSession, *, professional_user_id: uuid.UUID
) -> list[CareRosterEntry]:
    """List every non-ended relationship, including why a record is closed.

    The roster is a cross-subject index, not authorization. Opening a record
    still resolves its relationship and exact consent scopes afresh. This view
    mirrors the same lifecycle ceiling, including expiry, so it never offers a
    link that the next request must reject.
    """

    evaluated_at = await _now(session)
    participation = aliased(CareThreadParticipant)
    unread_message = aliased(CareMessage)
    latest_participation = aliased(CareThreadParticipant)
    latest_message = aliased(CareMessage)
    has_unread = (
        select(unread_message.id)
        .where(
            unread_message.thread_id == participation.thread_id,
            unread_message.actor_user_id != professional_user_id,
            unread_message.created_at > participation.last_read_at,
        )
        .correlate(participation)
        .exists()
    )
    unread_threads = (
        select(func.count(participation.id))
        .where(
            participation.relationship_id == CareRelationship.id,
            participation.user_id == professional_user_id,
            participation.removed_at.is_(None),
            has_unread,
        )
        .correlate(CareRelationship)
        .scalar_subquery()
    )
    last_message_at = (
        select(func.max(latest_message.created_at))
        .select_from(latest_participation)
        .join(
            latest_message,
            latest_message.thread_id == latest_participation.thread_id,
        )
        .where(
            latest_participation.relationship_id == CareRelationship.id,
            latest_participation.user_id == professional_user_id,
            latest_participation.removed_at.is_(None),
        )
        .correlate(CareRelationship)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(
                CareRelationship.id,
                CareRelationship.subject_id,
                CareRelationship.kind,
                CareRelationship.status,
                HealthSubject.display_name,
                ConsentGrant.status.label("consent_status"),
                ConsentGrant.expires_at,
                unread_threads.label("unread_threads"),
                last_message_at.label("last_message_at"),
            )
            .join(HealthSubject, HealthSubject.id == CareRelationship.subject_id)
            .outerjoin(
                ConsentGrant,
                (ConsentGrant.relationship_id == CareRelationship.id)
                & ConsentGrant.status.in_(
                    (ConsentStatus.ACTIVE.value, ConsentStatus.PAUSED.value)
                ),
            )
            .where(
                CareRelationship.professional_user_id == professional_user_id,
                CareRelationship.status != CareRelationshipStatus.ENDED.value,
            )
            .order_by(HealthSubject.display_name, CareRelationship.id)
        )
    ).all()

    roster: list[CareRosterEntry] = []
    for row in rows:
        expires_at = _as_utc(row.expires_at) if row.expires_at else None
        message_at = _as_utc(row.last_message_at) if row.last_message_at else None
        expired = expires_at is not None and expires_at <= evaluated_at
        is_open = (
            row.status == CareRelationshipStatus.ACTIVE.value
            and row.consent_status == ConsentStatus.ACTIVE.value
            and not expired
        )
        roster.append(
            CareRosterEntry(
                relationship_id=row.id,
                subject_id=row.subject_id,
                display_name=row.display_name,
                kind=row.kind,
                relationship_status=row.status,
                consent_status=row.consent_status,
                consent_expires_at=expires_at,
                open=is_open,
                consent_expired=expired,
                # A closed record may retain conversation history, but it is
                # not actionable work while the professional cannot enter it.
                unread_threads=int(row.unread_threads or 0) if is_open else 0,
                last_message_at=message_at if is_open else None,
            )
        )
    # One deterministic work queue: actionable records first, then unread work,
    # then recent contact. A display name is only the final stable tiebreak.
    roster.sort(key=lambda item: (item.display_name.casefold(), str(item.subject_id)))
    roster.sort(
        key=lambda item: item.last_message_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    roster.sort(key=lambda item: item.unread_threads, reverse=True)
    roster.sort(key=lambda item: item.open, reverse=True)
    return roster


async def _relationship_of_patient(
    session: AsyncSession, *, relationship_id: uuid.UUID, actor_user_id: uuid.UUID
) -> CareRelationship:
    """Find one relationship *inside the patient's scope*, locked.

    The ownership condition is in the query rather than checked after the read.
    Fetching by id and then asking whose it is means the row has already been
    loaded — into the identity map, into whatever the caller does next — before
    anything decided the caller was allowed to have it. Naming the owner in the
    ``WHERE`` makes somebody else's relationship simply not exist here.
    """

    relationship = await session.scalar(
        select(CareRelationship)
        .join(
            HealthSubject,
            HealthSubject.id == CareRelationship.subject_id,
        )
        .where(
            CareRelationship.id == relationship_id,
            HealthSubject.owner_user_id == actor_user_id,
        )
        .with_for_update(of=CareRelationship)
    )
    if relationship is None:
        # Missing and not-yours are the same answer: a caller able to tell them
        # apart could enumerate which relationships exist.
        raise RelationshipNotFound("no such relationship")
    return relationship


async def establish_from_invitation(
    session: AsyncSession, *, invitation: ProfessionalInvitation
) -> CareRelationship:
    """Turn an accepted offer into a relationship — and nothing more.

    No consent is created here. Being in care and having agreed to show
    something are different decisions, and folding them together would make the
    act of accepting an invitation also the act of granting access, which is a
    decision the patient has not been asked for at that point.
    """

    if invitation.status != ProfessionalInvitationStatus.ACCEPTED.value:
        raise CareValidationError("only an accepted invitation establishes care")
    if invitation.accepted_by_user_id is None:  # pragma: no cover - constraint
        raise CareValidationError("an accepted invitation names who accepted it")

    await acquire_identity_governance_lock(session)
    owner_user_id = await session.scalar(
        select(HealthSubject.owner_user_id).where(
            HealthSubject.id == invitation.subject_id
        )
    )
    if owner_user_id is None:
        raise CareValidationError("the invited record no longer exists")

    # A doctor and a trainer are two different professionals, not two labels on
    # one. Without this the kind on the relationship is only what the patient
    # happened to type into the invitation, and "my trainer" and "my doctor"
    # stop being facts about who these people are.
    #
    # Only checked where there is something to check against. A professional who
    # has never filled in a profile has claimed no kind, so there is nothing for
    # the invitation to contradict. Requiring a profile — or a *verified* one —
    # before care can start is the natural next step, and it is a decision about
    # onboarding order rather than a technical one: it would hold every new
    # professional at the door until an operator reached them.
    claimed_kind = await session.scalar(
        select(ProfessionalProfile.kind).where(
            ProfessionalProfile.user_id == invitation.accepted_by_user_id
        )
    )
    if claimed_kind is not None and claimed_kind != invitation.kind:
        raise KindMismatch(
            "this professional is not the kind of professional they were invited as"
        )

    relationship = CareRelationship(
        subject_id=invitation.subject_id,
        subject_owner_user_id=owner_user_id,
        professional_user_id=invitation.accepted_by_user_id,
        kind=invitation.kind,
        status=CareRelationshipStatus.ACTIVE.value,
        invitation_id=invitation.id,
    )
    session.add(relationship)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise CareError(
            "this professional is already in care for this record"
        ) from exc
    return relationship


async def grant_consent(
    session: AsyncSession,
    *,
    relationship_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    scopes: frozenset[AccessScope] | None = None,
    ttl: timedelta = DEFAULT_CONSENT_TTL,
) -> ConsentGrant:
    """Record what the patient agrees this professional may see, as a new version.

    Supersedes whatever version was live. Narrowing is therefore not an edit:
    the old version keeps its rows and its dates, and the record of what applied
    last month survives the change.

    ``scopes`` defaults to the kind's read-only domain set. Passing a set is how
    a patient narrows it; there is no way to pass one that is not made of exact
    resource/action pairs, because the scope type has no wildcard.
    """

    if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
        raise CareValidationError("ttl must be a positive interval")

    await acquire_identity_governance_lock(session)
    relationship = await _relationship_of_patient(
        session, relationship_id=relationship_id, actor_user_id=actor_user_id
    )
    if relationship.status == CareRelationshipStatus.ENDED.value:
        raise CareValidationError("this relationship has ended")

    resolved_scopes = (
        default_scopes(relationship.kind) if scopes is None else frozenset(scopes)
    )
    if not resolved_scopes:
        raise CareValidationError(
            "a consent that permits nothing is a revocation; revoke instead"
        )
    for scope in resolved_scopes:
        if not isinstance(scope, AccessScope):
            raise CareValidationError("every scope must be an AccessScope")

    now = await _now(session)
    live = await session.scalar(
        select(ConsentGrant)
        .where(
            ConsentGrant.relationship_id == relationship.id,
            ConsentGrant.status.in_(
                (ConsentStatus.ACTIVE.value, ConsentStatus.PAUSED.value)
            ),
        )
        .with_for_update()
    )
    highest = await session.scalar(
        select(func.max(ConsentGrant.version)).where(
            ConsentGrant.relationship_id == relationship.id
        )
    )
    if live is not None:
        live.status = ConsentStatus.SUPERSEDED.value
        live.paused_at = None
        await session.flush()

    grant = ConsentGrant(
        relationship_id=relationship.id,
        subject_id=relationship.subject_id,
        version=int(highest or 0) + 1,
        status=ConsentStatus.ACTIVE.value,
        expires_at=now + ttl,
    )
    session.add(grant)
    await session.flush()
    for scope in sorted(
        resolved_scopes,
        key=lambda s: (s.resource_type.value, s.resource_key, s.action.value),
    ):
        session.add(
            ConsentScope(
                consent_grant_id=grant.id,
                subject_id=relationship.subject_id,
                resource_type=scope.resource_type.value,
                resource_key=scope.resource_key,
                action=scope.action.value,
            )
        )
    await session.flush()
    return grant


async def _live_consent(
    session: AsyncSession, *, relationship_id: uuid.UUID
) -> ConsentGrant:
    grant = await session.scalar(
        select(ConsentGrant)
        .where(
            ConsentGrant.relationship_id == relationship_id,
            ConsentGrant.status.in_(
                (ConsentStatus.ACTIVE.value, ConsentStatus.PAUSED.value)
            ),
        )
        .with_for_update()
    )
    if grant is None:
        raise ConsentNotFound("there is no live consent on this relationship")
    return grant


async def set_consent_paused(
    session: AsyncSession,
    *,
    relationship_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    paused: bool,
) -> ConsentGrant:
    """Step back without tearing anything down, and step forward again.

    A pause is the patient taking a break — a second opinion, a holiday, a
    disagreement — and resuming must not cost them a new invitation and a new
    consent. Revocation is the other thing, and it does not come back.
    """

    await acquire_identity_governance_lock(session)
    await _relationship_of_patient(
        session, relationship_id=relationship_id, actor_user_id=actor_user_id
    )
    grant = await _live_consent(session, relationship_id=relationship_id)

    if paused:
        if grant.status == ConsentStatus.PAUSED.value:
            return grant
        grant.status = ConsentStatus.PAUSED.value
        grant.paused_at = await _now(session)
    else:
        if grant.status == ConsentStatus.ACTIVE.value:
            return grant
        grant.status = ConsentStatus.ACTIVE.value
        grant.paused_at = None
    await session.flush()
    return grant


async def revoke_consent(
    session: AsyncSession,
    *,
    relationship_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> ConsentGrant:
    """Withdraw permission now. Not a pause, and not reversible.

    The row stays: what was permitted, and until when, is history the patient
    may need later. Nothing reads a revoked version as permission.
    """

    await acquire_identity_governance_lock(session)
    await _relationship_of_patient(
        session, relationship_id=relationship_id, actor_user_id=actor_user_id
    )
    grant = await _live_consent(session, relationship_id=relationship_id)
    grant.status = ConsentStatus.REVOKED.value
    grant.paused_at = None
    grant.revoked_at = await _now(session)
    await session.flush()
    return grant


async def end_relationship(
    session: AsyncSession,
    *,
    relationship_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> CareRelationship:
    """End the care, and every consent under it with it.

    Leaving a live consent behind an ended relationship would be a permission
    with nobody to exercise it — until somebody re-established the pair and it
    silently applied again.
    """

    await acquire_identity_governance_lock(session)
    # Either party may end it — care is not something one side holds the other
    # in — so the condition names both rather than only the patient, and it is
    # still in the query rather than a check after the row is already loaded.
    relationship = await session.scalar(
        select(CareRelationship)
        .where(
            CareRelationship.id == relationship_id,
            or_(
                CareRelationship.subject_owner_user_id == actor_user_id,
                CareRelationship.professional_user_id == actor_user_id,
            ),
        )
        .with_for_update()
    )
    if relationship is None:
        raise NotTheSubjectOwner("only the two parties may end their own relationship")
    if relationship.status == CareRelationshipStatus.ENDED.value:
        return relationship

    now = await _now(session)
    live = await session.scalars(
        select(ConsentGrant)
        .where(
            ConsentGrant.relationship_id == relationship.id,
            ConsentGrant.status.in_(
                (ConsentStatus.ACTIVE.value, ConsentStatus.PAUSED.value)
            ),
        )
        .with_for_update()
    )
    for grant in live:
        grant.status = ConsentStatus.REVOKED.value
        grant.paused_at = None
        grant.revoked_at = now

    relationship.status = CareRelationshipStatus.ENDED.value
    relationship.ended_at = now
    relationship.ended_by_user_id = actor_user_id
    await session.flush()
    return relationship


async def load_relationship_grant(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    professional_user_id: uuid.UUID,
    evaluated_at: datetime,
) -> RelationshipGrant | None:
    """Assemble the snapshot the policy decides one request from.

    Returns ``None`` whenever anything is missing — no relationship, no live
    consent, the wrong pair. That is not a refusal: the policy is
    deny-by-default and a missing grant simply leaves it that way. The refusal
    still happens in ``vitals.access``, which re-checks the actor, the
    lifecycle, the expiry and the exact scope even on a grant this returned.

    A paused relationship or a paused consent produces a grant marked inactive
    rather than no grant at all. The distinction is for the caller building a
    screen: "paused" and "never granted" look identical from a ``None`` and are
    different things to say to a patient.
    """

    relationship = await session.scalar(
        select(CareRelationship).where(
            CareRelationship.subject_id == subject_id,
            CareRelationship.professional_user_id == professional_user_id,
            CareRelationship.status != CareRelationshipStatus.ENDED.value,
        )
    )
    if relationship is None:
        return None

    grant = await session.scalar(
        select(ConsentGrant)
        .options(selectinload(ConsentGrant.scopes))
        .where(
            ConsentGrant.relationship_id == relationship.id,
            ConsentGrant.status.in_(
                (ConsentStatus.ACTIVE.value, ConsentStatus.PAUSED.value)
            ),
        )
    )
    if grant is None:
        return None

    active = (
        relationship.status == CareRelationshipStatus.ACTIVE.value
        and grant.status == ConsentStatus.ACTIVE.value
    )
    return RelationshipGrant(
        relationship_id=relationship.id,
        consent_grant_id=grant.id,
        professional_user_id=professional_user_id,
        subject_id=subject_id,
        consent_version=grant.version,
        expires_at=_as_utc(grant.expires_at),
        scopes=frozenset(
            AccessScope(
                resource_type=PolicyResourceType(scope.resource_type),
                resource_key=scope.resource_key,
                action=PolicyAction(scope.action),
            )
            for scope in grant.scopes
        ),
        active=active,
        revoked_at=_as_utc(grant.revoked_at) if grant.revoked_at else None,
    )


__all__ = [
    "DEFAULT_CONSENT_TTL",
    "DEFAULT_DOMAINS",
    "AUTHORED_ACTIONS",
    "AUTHORED_ARTIFACTS",
    "READ_ONLY_ACTIONS",
    "CareError",
    "CareRosterEntry",
    "CareValidationError",
    "ConsentNotFound",
    "KindMismatch",
    "NotTheSubjectOwner",
    "RelationshipNotFound",
    "default_scopes",
    "end_relationship",
    "establish_from_invitation",
    "grant_consent",
    "load_relationship_grant",
    "list_professional_roster",
    "revoke_consent",
    "set_consent_paused",
]
