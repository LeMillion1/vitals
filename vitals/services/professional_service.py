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
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    ProfessionalKind,
    ProfessionalVerificationStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import User, UserRole
from vitals.models.professional import ProfessionalProfile

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


async def _require_reviewer(session: AsyncSession, reviewer_user_id: uuid.UUID) -> None:
    """An operator decides, and never about their own claim.

    The self-review check is not paranoia about a specific person: it is the
    same rule the support grants use, and it exists so that "an operator
    approved this" stays a statement about two people rather than one.
    """

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
    if resolved in _SELF_SERVE_STATUSES:
        raise ProfessionalValidationError(
            "a review records a verdict, not a return to the queue"
        )
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


async def pending_queue(session: AsyncSession) -> list[ProfessionalProfile]:
    """Claims waiting for somebody to look at them, oldest first."""

    return list(
        await session.scalars(
            select(ProfessionalProfile)
            .where(
                ProfessionalProfile.verification_status
                == ProfessionalVerificationStatus.PENDING.value
            )
            .order_by(ProfessionalProfile.created_at, ProfessionalProfile.id)
        )
    )


__all__ = [
    "NotAReviewerError",
    "ProfessionalConflictError",
    "ProfessionalError",
    "ProfessionalNotFoundError",
    "ProfessionalValidationError",
    "ROLE_FOR_KIND",
    "decide",
    "is_verified",
    "pending_queue",
    "submit_profile",
]
