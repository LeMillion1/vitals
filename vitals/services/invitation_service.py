"""Offering one professional a way into one patient's record.

The offer is a token in a link, and the design of it is mostly a list of things
a link must not be.

It must not be a bearer capability that outlives its purpose, so it expires and
it is one-time. It must not be usable by whoever it was forwarded to, so it is
bound to an address and the address has to be a *verified* claim — an unverified
one is somebody asserting they own a mailbox, which is exactly the thing the
binding stops. It must not be reconstructible from a database copy, so only the
token's hash is stored. And a refusal must not be informative: a spent
invitation, an expired one, one for a different address and one that never
existed all answer the same way, because those answers together would be a map
of who is being treated by whom.

Accepting does not grant access either. It creates the relationship half of the
pair; the patient's consent is the other half, and both are required before any
record is reachable.
"""

from __future__ import annotations

import hashlib
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    ProfessionalInvitationStatus,
    ProfessionalKind,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.professional import ProfessionalInvitation
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.rls_session import enter_platform_scope

#: How long an offer stands. Long enough to survive a weekend and a spam folder,
#: short enough that a link found in an old mailbox is no longer a way in.
DEFAULT_TTL = timedelta(days=14)

#: 32 bytes of urandom, URL-safe. The link is the only place this ever exists.
_TOKEN_BYTES = 32


class InvitationError(RuntimeError):
    """Base class for invitation failures."""


class InvitationValidationError(ValueError):
    """A submitted value is not usable."""


class InvitationRefused(InvitationError):
    """This token does not open anything.

    Deliberately one exception for every reason. Unknown, spent, revoked,
    expired, and addressed-to-somebody-else are five different facts, and a
    caller able to tell them apart could enumerate who is inviting whom.
    """


class NotTheSubjectOwner(InvitationError):
    """Only the person whose record it is may offer or withdraw access to it."""


@dataclass(frozen=True, slots=True)
class IssuedInvitation:
    """The stored row, plus the token — which exists here and in the link only."""

    invitation: ProfessionalInvitation
    token: str


def normalize_email(raw: object) -> str:
    """Fold an address to the form the binding compares.

    Deliberately shallow: NFKC, strip, lower-case. No provider-specific
    cleverness — dropping dots or ``+`` tags is right for exactly one mail
    provider and wrong for the rest, and being wrong here means an invitation
    that the intended person cannot accept.
    """

    if not isinstance(raw, str):
        raise InvitationValidationError("email must be a string")
    folded = unicodedata.normalize("NFKC", raw).strip().lower()
    if not folded or "@" not in folded or len(folded) > 320:
        raise InvitationValidationError("email is not a usable address")
    return folded


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    """Read a naive timestamp as UTC.

    SQLite hands back naive datetimes and PostgreSQL aware ones, and the two
    meet in the expiry comparison. Reading naive as UTC is right because that is
    what every column here stores; guessing local time would silently move every
    deadline by the host's offset.
    """

    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _now(session: AsyncSession) -> datetime:
    """The database's clock, so an expiry is not decided by the app server's."""

    stamp = await session.scalar(select(func.now()))
    return _as_utc(stamp) if stamp is not None else datetime.now(timezone.utc)


async def _require_subject_owner(
    session: AsyncSession, *, subject_id: uuid.UUID, actor_user_id: uuid.UUID
) -> None:
    owner_user_id = await session.scalar(
        select(HealthSubject.owner_user_id).where(HealthSubject.id == subject_id)
    )
    if owner_user_id is None or owner_user_id != actor_user_id:
        raise NotTheSubjectOwner("only the owner of this record may offer access to it")


async def invite(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    kind: ProfessionalKind | str,
    email: str,
    ttl: timedelta = DEFAULT_TTL,
) -> IssuedInvitation:
    """Offer one professional a way into this record, once, for a while.

    Returns the token alongside the row. It is not stored and cannot be
    recovered: if the caller loses it, the invitation is withdrawn and a new one
    issued, which is the correct outcome — a link that can be re-read out of the
    database is a link an operator can use.
    """

    if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
        raise InvitationValidationError("ttl must be a positive interval")
    resolved_kind = (
        kind if isinstance(kind, ProfessionalKind) else ProfessionalKind(str(kind))
    )
    invited_email = normalize_email(email)
    await _require_subject_owner(
        session, subject_id=subject_id, actor_user_id=actor_user_id
    )

    now = await _now(session)
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    invitation = ProfessionalInvitation(
        subject_id=subject_id,
        invited_by_user_id=actor_user_id,
        kind=resolved_kind.value,
        invited_email=invited_email,
        token_hash=_hash(token),
        status=ProfessionalInvitationStatus.PENDING.value,
        expires_at=now + ttl,
    )
    session.add(invitation)
    try:
        await session.flush()
    except IntegrityError as exc:  # pragma: no cover - 256 bits do not collide
        raise InvitationError("could not store the invitation") from exc
    return IssuedInvitation(invitation=invitation, token=token)


async def accept(
    session: AsyncSession,
    *,
    token: str,
    accepting_user_id: uuid.UUID,
    verified_email: str | None,
) -> ProfessionalInvitation:
    """Spend one invitation on behalf of the person it was addressed to.

    ``verified_email`` is what the caller's *authenticated* session established,
    and it is a parameter rather than something read from the account on
    purpose. After the federated cutover an address is a claim the provider
    makes at sign-in, not a column here, and a service that read a stored
    address would be trusting whichever half of the system last wrote it.
    ``None`` means the session established no verified address, which is not a
    reason to skip the check — it is a refusal.

    Runs in the platform scope: the acceptor is not bound to this subject yet,
    and the token is what authorizes reading the row at all.
    """

    if not isinstance(token, str) or not token.strip():
        raise InvitationRefused("this invitation does not open anything")
    if not isinstance(accepting_user_id, uuid.UUID) or accepting_user_id.int == 0:
        raise InvitationValidationError("accepting_user_id must be a non-zero UUID")

    await enter_platform_scope(session)
    await acquire_identity_governance_lock(session)

    invitation = await session.scalar(
        select(ProfessionalInvitation)
        .where(ProfessionalInvitation.token_hash == _hash(token))
        .with_for_update()
    )
    # Everything below raises the same thing. See InvitationRefused.
    if invitation is None:
        raise InvitationRefused("this invitation does not open anything")
    if invitation.status != ProfessionalInvitationStatus.PENDING.value:
        raise InvitationRefused("this invitation does not open anything")

    now = await _now(session)
    if now >= _as_utc(invitation.expires_at):
        # Mark it, so the state stops being a lie, and still refuse.
        invitation.status = ProfessionalInvitationStatus.EXPIRED.value
        await session.flush()
        raise InvitationRefused("this invitation does not open anything")

    if verified_email is None:
        raise InvitationRefused("this invitation does not open anything")
    try:
        presented = normalize_email(verified_email)
    except InvitationValidationError:
        raise InvitationRefused("this invitation does not open anything") from None
    if not secrets.compare_digest(presented, invitation.invited_email):
        raise InvitationRefused("this invitation does not open anything")

    status = await session.scalar(
        select(User.status).where(User.id == accepting_user_id)
    )
    if status != UserStatus.ACTIVE.value:
        raise InvitationRefused("this invitation does not open anything")

    # The patient cannot be their own professional: a relationship that names
    # one person twice has no second party to consent to anything.
    owner_user_id = await session.scalar(
        select(HealthSubject.owner_user_id).where(
            HealthSubject.id == invitation.subject_id
        )
    )
    if owner_user_id == accepting_user_id:
        raise InvitationRefused("this invitation does not open anything")

    invitation.status = ProfessionalInvitationStatus.ACCEPTED.value
    invitation.accepted_at = now
    invitation.accepted_by_user_id = accepting_user_id
    await session.flush()
    return invitation


async def revoke(
    session: AsyncSession,
    *,
    invitation_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> ProfessionalInvitation:
    """Withdraw an offer that has not been taken up.

    An accepted one is not withdrawn here — by then it is a relationship, and
    ending a relationship is a different operation with its own record.
    """

    invitation = await session.scalar(
        select(ProfessionalInvitation)
        .where(ProfessionalInvitation.id == invitation_id)
        .with_for_update()
    )
    if invitation is None:
        raise InvitationRefused("this invitation does not open anything")
    await _require_subject_owner(
        session, subject_id=invitation.subject_id, actor_user_id=actor_user_id
    )
    if invitation.status == ProfessionalInvitationStatus.REVOKED.value:
        return invitation
    if invitation.status != ProfessionalInvitationStatus.PENDING.value:
        raise InvitationError("only an invitation nobody has taken up can be withdrawn")

    now = await _now(session)
    invitation.status = ProfessionalInvitationStatus.REVOKED.value
    invitation.revoked_at = now
    await session.flush()
    return invitation


async def list_for_subject(
    session: AsyncSession, *, subject_id: uuid.UUID
) -> list[ProfessionalInvitation]:
    """What this record currently has outstanding, newest first."""

    return list(
        await session.scalars(
            select(ProfessionalInvitation)
            .where(ProfessionalInvitation.subject_id == subject_id)
            .order_by(
                ProfessionalInvitation.created_at.desc(),
                ProfessionalInvitation.id,
            )
        )
    )


__all__ = [
    "DEFAULT_TTL",
    "InvitationError",
    "InvitationRefused",
    "InvitationValidationError",
    "IssuedInvitation",
    "NotTheSubjectOwner",
    "accept",
    "invite",
    "list_for_subject",
    "normalize_email",
    "revoke",
]
