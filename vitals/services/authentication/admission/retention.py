"""Bounded expiry and privacy-retention maintenance for admission state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import RegistrationInvitationStatus, RegistrationRequestStatus
from vitals.models.registration import RegistrationInvitation, RegistrationRequest
from vitals.services.authentication.admission._shared import (
    DEFAULT_RETENTION,
    MINIMUM_RETENTION,
    AdmissionValidationError,
    RetentionResult,
    audit,
    bounded_limit,
    coerce_timestamp,
    database_now,
    expire_invitation,
    expire_request,
)
from vitals.services.identity_service import acquire_identity_governance_lock


async def expire_due(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 500,
) -> RetentionResult:
    """Expire a bounded batch without throwing, suitable for maintenance jobs."""

    limit = bounded_limit(limit)
    await acquire_identity_governance_lock(session)
    stamp = await database_now(session, supplied=now)
    invitations = list(
        await session.scalars(
            select(RegistrationInvitation)
            .where(
                RegistrationInvitation.status
                == RegistrationInvitationStatus.PENDING.value,
                RegistrationInvitation.expires_at <= stamp,
            )
            .order_by(RegistrationInvitation.expires_at, RegistrationInvitation.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for row in invitations:
        expire_invitation(row, now=stamp)
        audit(
            session,
            event_type="registration.invitation.expired",
            resource_type="registration_invitation",
            resource_id=row.id,
            result_code="maintenance_expired",
            changed_fields=("status",),
        )
    remaining = max(0, limit - len(invitations))
    requests = []
    if remaining:
        requests = list(
            await session.scalars(
                select(RegistrationRequest)
                .where(
                    RegistrationRequest.status
                    == RegistrationRequestStatus.PENDING.value,
                    RegistrationRequest.expires_at <= stamp,
                )
                .order_by(RegistrationRequest.expires_at, RegistrationRequest.id)
                .limit(remaining)
                .with_for_update(skip_locked=True)
            )
        )
        for row in requests:
            expire_request(row, now=stamp)
            audit(
                session,
                event_type="registration.request.expired",
                resource_type="registration_request",
                resource_id=row.id,
                result_code="maintenance_expired",
                changed_fields=("status",),
            )
    await session.flush()
    return RetentionResult(invitations=len(invitations), requests=len(requests))


async def purge_terminal(
    session: AsyncSession,
    *,
    before: datetime | None = None,
    now: datetime | None = None,
    limit: int = 500,
) -> RetentionResult:
    """Scrub a bounded batch of terminal applicant PII and user references.

    Rows and opaque outcomes remain. The default retains terminal data for 90
    days, and an explicit cutoff may not reduce that safety floor below 30 days.
    This is a scheduled retention primitive, not an operator action, so its
    audit actor is intentionally null.
    """

    limit = bounded_limit(limit)
    await acquire_identity_governance_lock(session)
    stamp = await database_now(session, supplied=now)
    cutoff = (
        stamp - DEFAULT_RETENTION
        if before is None
        else coerce_timestamp(before)
    )
    if cutoff > stamp - MINIMUM_RETENTION:
        raise AdmissionValidationError(
            f"terminal admission data must be retained for at least "
            f"{MINIMUM_RETENTION.days} days"
        )
    invitation_terminal = or_(
        RegistrationInvitation.consumed_at <= cutoff,
        RegistrationInvitation.revoked_at <= cutoff,
        RegistrationInvitation.expired_at <= cutoff,
    )
    invitations = list(
        await session.scalars(
            select(RegistrationInvitation)
            .where(
                RegistrationInvitation.status
                != RegistrationInvitationStatus.PENDING.value,
                RegistrationInvitation.purged_at.is_(None),
                invitation_terminal,
            )
            .order_by(RegistrationInvitation.created_at, RegistrationInvitation.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for row in invitations:
        row.token_digest = None
        row.normalized_email = None
        row.invited_by_user_id = None
        row.consumed_by_user_id = None
        row.revoked_by_user_id = None
        row.purged_at = stamp

    remaining = max(0, limit - len(invitations))
    requests = []
    if remaining:
        request_terminal = or_(
            RegistrationRequest.reviewed_at <= cutoff,
            RegistrationRequest.expired_at <= cutoff,
        )
        requests = list(
            await session.scalars(
                select(RegistrationRequest)
                .where(
                    RegistrationRequest.status
                    != RegistrationRequestStatus.PENDING.value,
                    RegistrationRequest.purged_at.is_(None),
                    request_terminal,
                )
                .order_by(RegistrationRequest.created_at, RegistrationRequest.id)
                .limit(remaining)
                .with_for_update(skip_locked=True)
            )
        )
        for row in requests:
            row.issuer = None
            row.subject = None
            row.verified_email = None
            row.normalized_verified_email = None
            row.preferred_username = None
            row.reviewer_user_id = None
            row.provisioned_user_id = None
            row.review_note = None
            row.purged_at = stamp

    total = len(invitations) + len(requests)
    if total:
        audit(
            session,
            event_type="registration.retention.purged",
            resource_type="registration_admission",
            resource_id="terminal_data",
            result_code="retention_scrubbed",
            changed_fields=("applicant_pii", "user_references"),
            record_count=total,
        )
    await session.flush()
    return RetentionResult(invitations=len(invitations), requests=len(requests))


__all__ = ["expire_due", "purge_terminal"]
