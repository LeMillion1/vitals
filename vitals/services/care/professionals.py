"""Registering a professional claim, and an operator deciding about it.

Nothing here grants access to anything. That is the whole design: a profile is a
statement — this is my name, this is my licence number, I am a doctor — and the
operator workflow decides only whether the statement checks out. Whose record
the person may then reach is a separate question, answered by a relationship the
patient accepted and a consent they gave.

The reason to keep those apart is concrete. If verification implied access, then
one operator approving one licence would admit that person to every record in
the installation, and the patient would never have been asked. Verification is
about the world outside; consent is about one patient. They are different
questions and they get different tables.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AuditOutcome,
    ProfessionalKind,
    ProfessionalVerificationStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import AuditEvent, User, UserRole
from vitals.models.professional import ProfessionalProfile, ProfessionalReviewDecision
from vitals.services.identity.governance import acquire_identity_governance_lock

#: Statuses a professional may put their own profile into. Everything else is an
#: operator's verdict, and a profile that could set its own verdict would make
#: the review a formality it can skip.
_SELF_SERVE_STATUSES = frozenset(
    {
        ProfessionalVerificationStatus.UNVERIFIED,
        ProfessionalVerificationStatus.PENDING,
    }
)

#: Which role a verified profile of each kind corresponds to. Holding the role
#: is necessary and never sufficient — see the module docstring.
ROLE_FOR_KIND = {
    ProfessionalKind.DOCTOR: UserRoleName.DOCTOR,
    ProfessionalKind.TRAINER: UserRoleName.TRAINER,
}

_AUDIT_SURFACE = "care.professionals"

_REVIEW_TRANSITIONS = frozenset(
    {
        (
            ProfessionalVerificationStatus.PENDING,
            ProfessionalVerificationStatus.VERIFIED,
        ),
        (
            ProfessionalVerificationStatus.PENDING,
            ProfessionalVerificationStatus.REJECTED,
        ),
        (
            ProfessionalVerificationStatus.VERIFIED,
            ProfessionalVerificationStatus.SUSPENDED,
        ),
        (
            ProfessionalVerificationStatus.SUSPENDED,
            ProfessionalVerificationStatus.VERIFIED,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class ProfessionalReviewHistoryEntry:
    from_status: str
    to_status: str
    reviewer_username: str
    note: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProfessionalReviewEntry:
    """The bounded account claim an operator may review; never a health record."""

    profile_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    kind: str
    verification_status: str
    display_name: str
    credential_reference: str | None
    review_note: str | None
    created_at: datetime
    updated_at: datetime
    history: tuple[ProfessionalReviewHistoryEntry, ...]


class ProfessionalError(RuntimeError):
    """Base class for professional-profile failures."""


class ProfessionalValidationError(ValueError):
    """A submitted value is not usable."""


class ProfessionalNotFoundError(ProfessionalError):
    """No such profile."""


class ProfessionalConflictError(ProfessionalError):
    """The profile already exists, or is not in a state this transition allows."""


class NotAReviewerError(ProfessionalError):
    """This principal may not decide about somebody else's claim."""


def _clean(value: object, field: str, *, limit: int, required: bool) -> str | None:
    if value is None:
        if required:
            raise ProfessionalValidationError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ProfessionalValidationError(f"{field} must be a string")
    stripped = value.strip()
    if not stripped:
        if required:
            raise ProfessionalValidationError(f"{field} must not be blank")
        return None
    if len(stripped) > limit:
        raise ProfessionalValidationError(
            f"{field} must be at most {limit} characters"
        )
    return stripped


async def _active_user(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    if not isinstance(user_id, uuid.UUID) or user_id.int == 0:
        raise ProfessionalValidationError("user_id must be a non-zero UUID")
    status = await session.scalar(select(User.status).where(User.id == user_id))
    if status is None or status != UserStatus.ACTIVE.value:
        raise ProfessionalNotFoundError("no active account for that id")
    return user_id


async def _require_kind_role(
    session: AsyncSession, *, user_id: uuid.UUID, kind: ProfessionalKind
) -> None:
    role = ROLE_FOR_KIND[kind]
    holds_role = await session.scalar(
        select(UserRole.id).where(
            UserRole.user_id == user_id,
            UserRole.role == role.value,
        )
    )
    if holds_role is None:
        raise ProfessionalValidationError(
            "a professional profile must match an assigned account role"
        )


async def submit_profile(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    kind: ProfessionalKind | str,
    display_name: str,
    credential_reference: str | None = None,
) -> ProfessionalProfile:
    """Record what somebody claims about themselves, awaiting review.

    Lands as ``pending`` rather than ``unverified``: submitting is the act of
    asking, and a profile nobody has been asked about would sit in the queue
    forever. Flushes; the caller owns the transaction.
    """

    await _active_user(session, user_id)
    resolved_kind = (
        kind if isinstance(kind, ProfessionalKind) else ProfessionalKind(str(kind))
    )
    await _require_kind_role(session, user_id=user_id, kind=resolved_kind)
    profile = ProfessionalProfile(
        user_id=user_id,
        kind=resolved_kind.value,
        verification_status=ProfessionalVerificationStatus.PENDING.value,
        display_name=_clean(display_name, "display_name", limit=200, required=True),
        credential_reference=_clean(
            credential_reference, "credential_reference", limit=200, required=False
        ),
    )
    session.add(profile)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ProfessionalConflictError(
            "this account already has a professional profile"
        ) from exc
    return profile


async def resubmit_profile(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    display_name: str,
    credential_reference: str | None = None,
) -> ProfessionalProfile:
    """Correct a rejected claim and return it to the review queue.

    Kind is deliberately immutable here and comes from the existing profile.
    A professional may correct what they submitted after a rejection, but a
    suspension is an operator action and cannot be self-cleared.
    """

    await _active_user(session, user_id)
    profile = await session.scalar(
        select(ProfessionalProfile)
        .where(ProfessionalProfile.user_id == user_id)
        .with_for_update()
    )
    if profile is None:
        raise ProfessionalNotFoundError("no such professional profile")
    if (
        profile.verification_status
        != ProfessionalVerificationStatus.REJECTED.value
    ):
        raise ProfessionalConflictError(
            "only a rejected profile may be corrected and resubmitted"
        )

    kind = ProfessionalKind(profile.kind)
    await _require_kind_role(session, user_id=user_id, kind=kind)
    profile.display_name = _clean(
        display_name, "display_name", limit=200, required=True
    )
    profile.credential_reference = _clean(
        credential_reference,
        "credential_reference",
        limit=200,
        required=False,
    )
    profile.verification_status = ProfessionalVerificationStatus.PENDING.value
    profile.review_note = None
    profile.verified_at = None
    profile.verified_by_user_id = None
    await session.flush()
    return profile


async def _require_reviewer(session: AsyncSession, reviewer_user_id: uuid.UUID) -> None:
    """An operator decides, and never about their own claim.

    The self-review check is not paranoia about a specific person: it is the
    same rule the support grants use, and it exists so that "an operator
    approved this" stays a statement about two people rather than one.
    """

    with session.no_autoflush:
        await _active_user(session, reviewer_user_id)
        roles = set(
            await session.scalars(
                select(UserRole.role).where(UserRole.user_id == reviewer_user_id)
            )
        )
    if UserRoleName.PLATFORM_SUPERADMIN.value not in roles:
        raise NotAReviewerError("verifying a professional is an operator's decision")


async def decide(
    session: AsyncSession,
    *,
    profile_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    expected_status: ProfessionalVerificationStatus | str,
    status: ProfessionalVerificationStatus | str,
    note: str | None = None,
) -> ProfessionalProfile:
    """Record an operator's verdict on one claim.

    ``verified`` stamps who decided and when, because a verification with no
    reviewer is a claim that verified itself. ``rejected`` and ``suspended``
    require a note: the professional needs to know what to fix, and the next
    operator needs to see what this one concluded.

    Suspending is deliberately reachable from ``verified``. A licence can lapse
    or be withdrawn after the fact, and the alternative — deleting the profile —
    would erase the trail of it ever having been approved.
    """

    resolved = (
        status
        if isinstance(status, ProfessionalVerificationStatus)
        else ProfessionalVerificationStatus(str(status))
    )
    expected = (
        expected_status
        if isinstance(expected_status, ProfessionalVerificationStatus)
        else ProfessionalVerificationStatus(str(expected_status))
    )
    if resolved in _SELF_SERVE_STATUSES:
        raise ProfessionalValidationError(
            "a review records a verdict, not a return to the queue"
        )
    # Verification is a live authorization fact.  Take the same transaction
    # fence as relationship, consent, role and push-claim mutations before any
    # row lock so a claim cannot race a suspension (or an approval).
    await acquire_identity_governance_lock(session)
    await _require_reviewer(session, reviewer_user_id)

    profile = await session.scalar(
        select(ProfessionalProfile)
        .where(ProfessionalProfile.id == profile_id)
        .with_for_update()
    )
    if profile is None:
        raise ProfessionalNotFoundError("no such professional profile")
    if profile.user_id == reviewer_user_id:
        raise NotAReviewerError("a claim cannot be reviewed by the person making it")
    current = ProfessionalVerificationStatus(profile.verification_status)
    if current is not expected or (current, resolved) not in _REVIEW_TRANSITIONS:
        raise ProfessionalConflictError(
            "the professional profile changed or this review transition is not allowed"
        )

    if resolved is ProfessionalVerificationStatus.VERIFIED:
        await _active_user(session, profile.user_id)
        await _require_kind_role(
            session,
            user_id=profile.user_id,
            kind=ProfessionalKind(profile.kind),
        )

    cleaned_note = _clean(note, "note", limit=2000, required=False)
    if (
        resolved
        in {
            ProfessionalVerificationStatus.REJECTED,
            ProfessionalVerificationStatus.SUSPENDED,
        }
        and cleaned_note is None
    ):
        raise ProfessionalValidationError(f"{resolved.value} needs a reason")

    profile.verification_status = resolved.value
    profile.review_note = cleaned_note
    if resolved is ProfessionalVerificationStatus.VERIFIED:
        decided_at = await session.scalar(select(func.now()))
        profile.verified_at = decided_at or datetime.now(timezone.utc)
        profile.verified_by_user_id = reviewer_user_id
    else:
        # A withdrawn verification is withdrawn, not annotated. Leaving the
        # stamp behind would let a suspended profile still read as checked.
        profile.verified_at = None
        profile.verified_by_user_id = None
    session.add(
        ProfessionalReviewDecision(
            profile_id=profile.id,
            reviewer_user_id=reviewer_user_id,
            from_status=current.value,
            to_status=resolved.value,
            note=cleaned_note,
        )
    )
    session.add(
        AuditEvent(
            actor_user_id=reviewer_user_id,
            subject_id=None,
            event_type=f"care.professional_profile.{resolved.value}",
            outcome=AuditOutcome.SUCCESS.value,
            resource_type="professional_profile",
            resource_id=str(profile.id),
            metadata_json={
                "source_surface": _AUDIT_SURFACE,
                "result_code": f"{current.value}_to_{resolved.value}",
                "resource_type": "professional_profile",
                "resource_id": str(profile.id),
            },
        )
    )
    await session.flush()
    return profile


async def is_verified(session: AsyncSession, *, user_id: uuid.UUID) -> bool:
    """Whether this account holds a verified professional profile.

    Read by the relationship layer as one *precondition* among several. On its
    own it authorizes nothing, and any caller treating it as a yes/no about
    access has skipped both the relationship and the consent.
    """

    status = await session.scalar(
        select(ProfessionalProfile.verification_status).where(
            ProfessionalProfile.user_id == user_id
        )
    )
    return status == ProfessionalVerificationStatus.VERIFIED.value


async def review_console(
    session: AsyncSession, *, reviewer_user_id: uuid.UUID
) -> tuple[ProfessionalReviewEntry, ...]:
    """Return professional claims only after a fresh operator authorization.

    The credential reference is private account data.  Keeping the reviewer
    check in this service means a future delivery surface cannot accidentally
    turn the queue into an authenticated-but-global directory.
    """

    await _require_reviewer(session, reviewer_user_id)
    rows = list(
        (
            await session.execute(
                select(
                    ProfessionalProfile.id,
                    ProfessionalProfile.user_id,
                    User.username,
                    ProfessionalProfile.kind,
                    ProfessionalProfile.verification_status,
                    ProfessionalProfile.display_name,
                    ProfessionalProfile.credential_reference,
                    ProfessionalProfile.review_note,
                    ProfessionalProfile.created_at,
                    ProfessionalProfile.updated_at,
                )
                .join(User, User.id == ProfessionalProfile.user_id)
                .order_by(ProfessionalProfile.created_at, ProfessionalProfile.id)
            )
        ).all()
    )
    priority = {
        ProfessionalVerificationStatus.PENDING.value: 0,
        ProfessionalVerificationStatus.VERIFIED.value: 1,
        ProfessionalVerificationStatus.SUSPENDED.value: 2,
        ProfessionalVerificationStatus.REJECTED.value: 3,
        ProfessionalVerificationStatus.UNVERIFIED.value: 4,
    }
    history_rows = (
        await session.execute(
            select(
                ProfessionalReviewDecision.profile_id,
                ProfessionalReviewDecision.from_status,
                ProfessionalReviewDecision.to_status,
                User.username,
                ProfessionalReviewDecision.note,
                ProfessionalReviewDecision.created_at,
            )
            .join(User, User.id == ProfessionalReviewDecision.reviewer_user_id)
            .order_by(
                ProfessionalReviewDecision.created_at,
                ProfessionalReviewDecision.id,
            )
        )
    ).all()
    history_by_profile: dict[
        uuid.UUID, list[ProfessionalReviewHistoryEntry]
    ] = {}
    for row in history_rows:
        history_by_profile.setdefault(row.profile_id, []).append(
            ProfessionalReviewHistoryEntry(
                from_status=row.from_status,
                to_status=row.to_status,
                reviewer_username=row.username,
                note=row.note,
                created_at=row.created_at,
            )
        )
    entries = [
        ProfessionalReviewEntry(
            profile_id=row.id,
            user_id=row.user_id,
            username=row.username,
            kind=row.kind,
            verification_status=row.verification_status,
            display_name=row.display_name,
            credential_reference=row.credential_reference,
            review_note=row.review_note,
            created_at=row.created_at,
            updated_at=row.updated_at,
            history=tuple(history_by_profile.get(row.id, ())),
        )
        for row in rows
    ]
    entries.sort(
        key=lambda entry: (
            priority.get(entry.verification_status, 99),
            entry.created_at,
            str(entry.profile_id),
        )
    )
    return tuple(entries)


async def verify_profile(
    session: AsyncSession,
    *,
    profile_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
) -> ProfessionalProfile:
    return await decide(
        session,
        profile_id=profile_id,
        reviewer_user_id=reviewer_user_id,
        expected_status=ProfessionalVerificationStatus.PENDING,
        status=ProfessionalVerificationStatus.VERIFIED,
    )


async def reject_profile(
    session: AsyncSession,
    *,
    profile_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    note: str,
) -> ProfessionalProfile:
    return await decide(
        session,
        profile_id=profile_id,
        reviewer_user_id=reviewer_user_id,
        expected_status=ProfessionalVerificationStatus.PENDING,
        status=ProfessionalVerificationStatus.REJECTED,
        note=note,
    )


async def suspend_profile(
    session: AsyncSession,
    *,
    profile_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
    note: str,
) -> ProfessionalProfile:
    return await decide(
        session,
        profile_id=profile_id,
        reviewer_user_id=reviewer_user_id,
        expected_status=ProfessionalVerificationStatus.VERIFIED,
        status=ProfessionalVerificationStatus.SUSPENDED,
        note=note,
    )


async def reinstate_profile(
    session: AsyncSession,
    *,
    profile_id: uuid.UUID,
    reviewer_user_id: uuid.UUID,
) -> ProfessionalProfile:
    return await decide(
        session,
        profile_id=profile_id,
        reviewer_user_id=reviewer_user_id,
        expected_status=ProfessionalVerificationStatus.SUSPENDED,
        status=ProfessionalVerificationStatus.VERIFIED,
    )


__all__ = [
    "NotAReviewerError",
    "ProfessionalConflictError",
    "ProfessionalError",
    "ProfessionalNotFoundError",
    "ProfessionalReviewEntry",
    "ProfessionalReviewHistoryEntry",
    "ProfessionalValidationError",
    "ROLE_FOR_KIND",
    "decide",
    "is_verified",
    "reinstate_profile",
    "reject_profile",
    "resubmit_profile",
    "review_console",
    "submit_profile",
    "suspend_profile",
    "verify_profile",
]
