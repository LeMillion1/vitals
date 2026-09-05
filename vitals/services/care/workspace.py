"""Read models for professional and owner care workspaces."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    CareRelationshipStatus,
    ProfessionalKind,
    ProfessionalVerificationStatus,
    UserRoleName,
)
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.professional import ProfessionalProfile
from vitals.models.professional import CareRelationship
from vitals.services.modules import preferences as modules_service
from vitals.services.authorization.subject_access import AccessContext
from vitals.services.care import professionals, record_projection, relationships


@dataclass(frozen=True, slots=True)
class ProfessionalWorkspace:
    assigned_roles: frozenset[str]
    professional_roles: frozenset[str]
    profile: ProfessionalProfile | None
    available_kinds: tuple[ProfessionalKind, ...]
    onboarding_kind: ProfessionalKind | None
    profile_verified: bool
    patients: tuple[relationships.CareRosterEntry, ...]

    @property
    def destination_without_professional_role(self) -> str:
        if UserRoleName.PLATFORM_SUPERADMIN.value in self.assigned_roles:
            return "/settings/platform"
        return "/settings/care"


@dataclass(frozen=True, slots=True)
class SubjectWorkspaceIdentity:
    display_name: str
    timezone: str


async def verified_email_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> str | None:
    """Return an email only when the account has actually verified it."""

    row = (
        await session.execute(
            select(User.normalized_email, User.email_verified_at).where(User.id == user_id)
        )
    ).one_or_none()
    if row is None or row.email_verified_at is None:
        return None
    return row.normalized_email


async def subject_identity(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> SubjectWorkspaceIdentity | None:
    """Load the non-medical subject fields needed by care delivery."""

    row = (
        await session.execute(
            select(HealthSubject.display_name, HealthSubject.timezone).where(
                HealthSubject.id == subject_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return SubjectWorkspaceIdentity(
        display_name=row.display_name or "",
        timezone=row.timezone,
    )


async def active_relationship_kind(
    session: AsyncSession,
    *,
    relationship_id: uuid.UUID,
) -> ProfessionalKind | None:
    """Return a live care relationship kind, never a stale relationship row."""

    value = await session.scalar(
        select(CareRelationship.kind).where(
            CareRelationship.id == relationship_id,
            CareRelationship.status == CareRelationshipStatus.ACTIVE.value,
        )
    )
    return ProfessionalKind(value) if value is not None else None


async def has_live_professional_relationship(
    session: AsyncSession,
    *,
    professional_user_id: uuid.UUID,
) -> bool:
    """Whether a professional currently holds at least one patient."""

    return bool(
        await relationships.list_professional_roster(
            session,
            professional_user_id=professional_user_id,
        )
    )


async def professional_display_names(
    session: AsyncSession,
    *,
    user_ids: set[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """Map immutable professional IDs to patient-safe display names."""

    if not user_ids:
        return {}
    return dict(
        (
            await session.execute(
                select(
                    ProfessionalProfile.user_id,
                    ProfessionalProfile.display_name,
                ).where(ProfessionalProfile.user_id.in_(user_ids))
            )
        ).all()
    )


async def load_professional_workspace(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> ProfessionalWorkspace:
    """Load role, onboarding, and roster state for the professional shell."""

    assigned_roles = frozenset(
        await session.scalars(select(UserRole.role).where(UserRole.user_id == user_id))
    )
    professional_roles = frozenset(
        assigned_roles.intersection(
            (UserRoleName.DOCTOR.value, UserRoleName.TRAINER.value)
        )
    )
    profile = await session.scalar(
        select(ProfessionalProfile).where(ProfessionalProfile.user_id == user_id)
    )
    available_kinds = tuple(
        kind
        for kind in ProfessionalKind
        if professionals.ROLE_FOR_KIND[kind].value in professional_roles
    )
    onboarding_kind = (
        ProfessionalKind(profile.kind)
        if profile is not None
        else available_kinds[0]
        if len(available_kinds) == 1
        else None
    )
    profile_verified = (
        profile is not None
        and profile.verification_status
        == ProfessionalVerificationStatus.VERIFIED.value
    )
    patients = tuple(
        await relationships.list_professional_roster(
            session,
            professional_user_id=user_id,
        )
    )
    return ProfessionalWorkspace(
        assigned_roles=assigned_roles,
        professional_roles=professional_roles,
        profile=profile,
        available_kinds=available_kinds,
        onboarding_kind=onboarding_kind,
        profile_verified=profile_verified,
        patients=patients,
    )


async def visible_record(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    subject_timezone: str,
    context: AccessContext,
) -> record_projection.RecordProjection:
    """Build the module-aware care record visible to one access context."""

    enabled = await modules_service.get_enabled_modules(
        session,
        subject_id=subject_id,
    )
    return await record_projection.assemble_record_projection(
        session,
        context=context,
        enabled_modules=enabled,
        subject_timezone_name=subject_timezone,
    )
