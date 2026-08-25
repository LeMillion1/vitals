"""Choose the one health record an OAuth connector authorization is about.

The browser session authenticates an account; it does not select a patient.
This service lists only records the account can authorize now: its own record,
or one active care relationship with one active, unexpired consent. The choice
is copied into the single-use authorization code and revalidated when the token
is minted, so neither a stale page nor a changed form becomes authority.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    CareRelationshipStatus,
    ConsentStatus,
    ProfessionalVerificationStatus,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.professional import (
    CareRelationship,
    ConsentGrant,
    ProfessionalProfile,
)
from vitals.services.identity_service import normalize_username
from vitals.utils.timeutils import now_utc


class ConnectorAuthorizationError(RuntimeError):
    """The account cannot authorize the requested record."""


@dataclass(frozen=True, slots=True)
class ConnectorSubject:
    """One safe choice rendered on the connector consent screen."""

    subject_id: uuid.UUID
    display_name: str
    basis: str


async def list_subjects(
    session: AsyncSession, *, username: str
) -> tuple[ConnectorSubject, ...]:
    """Return current connector targets for one active account."""

    lookup = normalize_username(username).lookup_key
    user = await session.scalar(
        select(User).where(
            User.normalized_username == lookup,
            User.status == UserStatus.ACTIVE.value,
        )
    )
    if user is None:
        return ()

    own = await session.scalar(
        select(HealthSubject).where(HealthSubject.owner_user_id == user.id)
    )
    choices: list[ConnectorSubject] = []
    if own is not None:
        choices.append(
            ConnectorSubject(
                subject_id=own.id,
                display_name=own.display_name,
                basis="self",
            )
        )

    rows = (
        await session.execute(
            select(
                CareRelationship.subject_id,
                HealthSubject.display_name,
            )
            .join(HealthSubject, HealthSubject.id == CareRelationship.subject_id)
            .join(
                UserRole,
                (UserRole.user_id == user.id)
                & (UserRole.role == CareRelationship.kind),
            )
            .join(
                ProfessionalProfile,
                (ProfessionalProfile.user_id == user.id)
                & (ProfessionalProfile.kind == CareRelationship.kind)
                & (
                    ProfessionalProfile.verification_status
                    == ProfessionalVerificationStatus.VERIFIED.value
                ),
            )
            .join(
                ConsentGrant,
                ConsentGrant.relationship_id == CareRelationship.id,
            )
            .where(
                CareRelationship.professional_user_id == user.id,
                CareRelationship.status == CareRelationshipStatus.ACTIVE.value,
                ConsentGrant.status == ConsentStatus.ACTIVE.value,
                ConsentGrant.expires_at > now_utc(),
            )
            .order_by(HealthSubject.display_name, CareRelationship.id)
        )
    ).all()
    choices.extend(
        ConnectorSubject(
            subject_id=subject_id,
            display_name=display_name,
            basis="care",
        )
        for subject_id, display_name in rows
        if own is None or subject_id != own.id
    )
    return tuple(choices)


async def resolve_subject(
    session: AsyncSession,
    *,
    username: str,
    requested_subject_id: uuid.UUID | None,
) -> ConnectorSubject:
    """Resolve a submitted choice, with a single-choice compatibility path."""

    choices = await list_subjects(session, username=username)
    if requested_subject_id is None:
        if len(choices) == 1:
            return choices[0]
        raise ConnectorAuthorizationError(
            "connector authorization must select exactly one health subject"
        )
    for choice in choices:
        if choice.subject_id == requested_subject_id:
            return choice
    raise ConnectorAuthorizationError(
        "connector authorization is unavailable for this health subject"
    )


__all__ = [
    "ConnectorAuthorizationError",
    "ConnectorSubject",
    "list_subjects",
    "resolve_subject",
]
