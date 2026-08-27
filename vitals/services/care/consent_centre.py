"""Patient-facing care consent policy and read projections.

The HTTP layer supplies the authenticated owner context and renders the result.
This module owns the policy vocabulary and the database projection so consent
rules cannot drift between forms, APIs, and future delivery surfaces.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import AccessScope, PolicyResourceType
from vitals.enums import (
    CareRelationshipStatus,
    ConsentStatus,
    Domain,
    ProfessionalInvitationStatus,
)
from vitals.models.identity import User
from vitals.models.professional import (
    CareRelationship,
    ConsentGrant,
    ConsentScope,
    ProfessionalInvitation,
    ProfessionalProfile,
)
from vitals.services.modules import preferences as modules_service
from vitals.services.authorization.subject_access import AccessContext
from vitals.services.care import record_projection, records, relationships


@dataclass(frozen=True, slots=True)
class ProfessionalConsentProjection:
    relationship_id: uuid.UUID
    kind: str
    name: str
    verified: bool
    relationship_status: str
    consent_status: str | None
    version: int | None
    expires_at: datetime | None
    domains: list[str]
    guidance: bool
    messages: bool


@dataclass(frozen=True, slots=True)
class ConsentCentreProjection:
    subject_id: uuid.UUID
    professionals: tuple[ProfessionalConsentProjection, ...]
    guidance: Any
    guidance_author_names: dict[uuid.UUID, str]
    pending_invitations: tuple[ProfessionalInvitation, ...]
    shareable_domains: tuple[Domain, ...]


def shared_domains(
    scope_rows: set[tuple[str, str, str]],
    *,
    visible_domains: tuple[Domain, ...] = record_projection.CARE_DOMAINS,
) -> list[str]:
    """Collapse action-level grants into ordered patient-visible domains."""

    granted = {
        key
        for resource_type, key, _action in scope_rows
        if resource_type == PolicyResourceType.DOMAIN.value
    }
    return list(
        domain.value for domain in visible_domains if domain.value in granted
    )


def selected_scopes(
    domains: list[str],
    *,
    allowed_domains: frozenset[Domain],
    allow_guidance: bool,
    allow_messages: bool,
) -> frozenset[AccessScope]:
    """Translate a patient's form selection into exact policy scopes."""

    try:
        selected_domains = {Domain(value) for value in domains}
    except ValueError as exc:
        raise relationships.CareValidationError("unknown record section") from exc
    if not selected_domains.issubset(allowed_domains):
        raise relationships.CareValidationError("unknown record section")

    scopes = {
        AccessScope(
            resource_type=PolicyResourceType.DOMAIN,
            resource_key=domain.value,
            action=action,
        )
        for domain in selected_domains
        for action in relationships.READ_ONLY_ACTIONS
    }
    if allow_guidance:
        scopes.update(
            AccessScope(
                resource_type=PolicyResourceType.ARTIFACT,
                resource_key=artifact,
                action=action,
            )
            for artifact in relationships.AUTHORED_ARTIFACTS
            for action in relationships.AUTHORED_ACTIONS
        )
    if allow_messages:
        scopes.update(
            AccessScope(
                resource_type=PolicyResourceType.OPERATION,
                resource_key=relationships.MESSAGE_OPERATION,
                action=action,
            )
            for action in relationships.MESSAGE_ACTIONS
        )
    if not scopes:
        raise relationships.CareValidationError(
            "choose at least one record section or collaboration feature"
        )
    return frozenset(scopes)


async def selected_scopes_for_subject(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    domains: list[str],
    custom: bool,
    allow_guidance: bool,
    allow_messages: bool,
) -> frozenset[AccessScope]:
    """Resolve module gates and create the exact consent scope selection."""

    enabled_modules = await modules_service.get_enabled_modules(
        session,
        subject_id=subject_id,
    )
    allowed_domains = frozenset(
        record_projection.enabled_care_domains(enabled_modules)
    )
    if not custom:
        domains = [domain.value for domain in allowed_domains]
        allow_guidance = True
        allow_messages = True
    return selected_scopes(
        domains,
        allowed_domains=allowed_domains,
        allow_guidance=allow_guidance,
        allow_messages=allow_messages,
    )


async def build_projection(
    session: AsyncSession,
    *,
    owner_context: AccessContext,
) -> ConsentCentreProjection:
    """Load the complete consent-centre view in bounded queries."""

    subject_id = owner_context.subject_id
    enabled_modules = await modules_service.get_enabled_modules(
        session, subject_id=subject_id
    )
    shareable_domains = record_projection.enabled_care_domains(enabled_modules)
    rows = (
        await session.execute(
            select(
                CareRelationship.id,
                CareRelationship.kind,
                CareRelationship.status,
                CareRelationship.established_at,
                User.username,
                ProfessionalProfile.display_name,
                ProfessionalProfile.verification_status,
                ConsentGrant.id.label("consent_id"),
                ConsentGrant.status.label("consent_status"),
                ConsentGrant.version,
                ConsentGrant.expires_at,
            )
            .join(User, User.id == CareRelationship.professional_user_id)
            .outerjoin(
                ProfessionalProfile,
                ProfessionalProfile.user_id == CareRelationship.professional_user_id,
            )
            .outerjoin(
                ConsentGrant,
                (ConsentGrant.relationship_id == CareRelationship.id)
                & ConsentGrant.status.in_(
                    (ConsentStatus.ACTIVE.value, ConsentStatus.PAUSED.value)
                ),
            )
            .where(
                CareRelationship.subject_id == subject_id,
                CareRelationship.status != CareRelationshipStatus.ENDED.value,
            )
            .order_by(CareRelationship.established_at.desc())
        )
    ).all()

    consent_ids = [row.consent_id for row in rows if row.consent_id is not None]
    scopes: dict[uuid.UUID, set[tuple[str, str, str]]] = {}
    if consent_ids:
        for scope in await session.execute(
            select(
                ConsentScope.consent_grant_id,
                ConsentScope.resource_type,
                ConsentScope.resource_key,
                ConsentScope.action,
            ).where(ConsentScope.consent_grant_id.in_(consent_ids))
        ):
            scopes.setdefault(scope.consent_grant_id, set()).add(
                (scope.resource_type, scope.resource_key, scope.action)
            )

    professionals = []
    for row in rows:
        row_scopes = scopes.get(row.consent_id, set())
        professionals.append(
            ProfessionalConsentProjection(
                relationship_id=row.id,
                kind=row.kind,
                name=row.display_name or row.username,
                verified=row.verification_status == "verified",
                relationship_status=row.status,
                consent_status=row.consent_status,
                version=row.version,
                expires_at=row.expires_at,
                domains=shared_domains(
                    row_scopes,
                    visible_domains=shareable_domains,
                ),
                guidance=any(
                    resource_type == PolicyResourceType.ARTIFACT.value
                    for resource_type, _key, _action in row_scopes
                ),
                messages=any(
                    resource_type == PolicyResourceType.OPERATION.value
                    and key == relationships.MESSAGE_OPERATION
                    for resource_type, key, _action in row_scopes
                ),
            )
        )

    guidance = await records.care_guidance_summary(
        session,
        context=owner_context,
    )
    author_ids = {
        item.actor_user_id
        for item in (*guidance.active_plans, *guidance.recent_notes)
    }
    author_names = (
        dict(
            (
                await session.execute(
                    select(
                        ProfessionalProfile.user_id,
                        ProfessionalProfile.display_name,
                    ).where(ProfessionalProfile.user_id.in_(author_ids))
                )
            ).all()
        )
        if author_ids
        else {}
    )
    pending = tuple(
        await session.scalars(
            select(ProfessionalInvitation)
            .where(
                ProfessionalInvitation.subject_id == subject_id,
                ProfessionalInvitation.status
                == ProfessionalInvitationStatus.PENDING.value,
            )
            .order_by(ProfessionalInvitation.created_at.desc())
        )
    )
    return ConsentCentreProjection(
        subject_id=subject_id,
        professionals=tuple(professionals),
        guidance=guidance,
        guidance_author_names=author_names,
        pending_invitations=pending,
        shareable_domains=shareable_domains,
    )
