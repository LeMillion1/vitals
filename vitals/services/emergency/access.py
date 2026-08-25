"""Three-account, read-only emergency access to one exact health record.

This is deliberately not an alternative constructor for ``SupportAccessGrant``.
It has its own rows, lifecycle and selectors, and its authorization is consumed
only by the dedicated break-glass record projection.  No role, care consent,
support grant or connector scope is unioned into this decision.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vitals.enums import (
    AuditOutcome,
    BreakGlassStatus,
    Domain,
    UserRoleName,
    UserStatus,
)
from vitals.models.break_glass import (
    BreakGlassApproval,
    BreakGlassScope,
    BreakGlassSession,
)
from vitals.models.identity import AuditEvent, HealthSubject, User, UserRole
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.utils.timeutils import now_utc

APPROVAL_WINDOW = timedelta(minutes=15)
ALLOWED_TTL_MINUTES = frozenset({15, 30, 60})
# Deliberately reviewed and closed.  Do not derive this from a broader record
# projection: adding a new care surface must not silently add it to an emergency
# policy somebody approved before that surface existed.
ALLOWED_DOMAINS = frozenset(
    {
        Domain.WEIGHT,
        Domain.LABS,
        Domain.BODY_COMPOSITION,
        Domain.NUTRITION,
        Domain.HRT,
        Domain.GLP1,
        Domain.SUPPLEMENTS,
        Domain.SKINCARE,
        Domain.GENETICS,
        Domain.GARMIN,
        Domain.WORKOUTS,
    }
)
AUDIT_SURFACE = "emergency_access"

EVENT_INITIATED = "break_glass.initiated"
EVENT_APPROVED = "break_glass.approved"
EVENT_ACTIVATED = "break_glass.activated"
EVENT_OPENED = "break_glass.record.opened"
EVENT_REVOKED = "break_glass.revoked"


class BreakGlassError(RuntimeError):
    """Base for every fail-closed emergency-access refusal."""


class NotAPlatformAdmin(BreakGlassError):
    """The actor is not an active platform superadmin now."""


class InvalidEmergencyRequest(BreakGlassError):
    """The requested emergency capability is not in the reviewed shape."""


class EmergencySessionNotFound(BreakGlassError):
    """No session exists for all exact selectors presented by the caller."""


class EmergencySessionClosed(BreakGlassError):
    """The request or access window is not live."""


class ApprovalNotAllowed(BreakGlassError):
    """This account cannot add an approval to this session."""


class NotTheSubjectOwner(BreakGlassError):
    """Only the record owner may use the patient-side operation."""


@dataclass(frozen=True, slots=True)
class EmergencyAuthorization:
    """Exact capability passed from authorization to the isolated projection."""

    session_id: uuid.UUID
    subject_id: uuid.UUID
    holder_user_id: uuid.UUID
    domain_keys: tuple[str, ...]
    subject_timezone: str
    subject_display_name: str | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class EmergencySessionView:
    session_id: uuid.UUID
    subject_id: uuid.UUID
    holder_user_id: uuid.UUID
    holder_username: str
    viewer_is_holder: bool
    status: str
    reason: str
    incident_reference: str | None
    domain_keys: tuple[str, ...]
    requested_ttl_minutes: int
    initiated_at: datetime
    approval_deadline: datetime
    approval_count: int
    activated_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class PatientEmergencyView:
    session_id: uuid.UUID
    holder_username: str
    status: str
    reason: str
    incident_reference: str | None
    domain_keys: tuple[str, ...]
    initiated_at: datetime
    approval_deadline: datetime
    approval_count: int
    activated_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class EmergencyBannerState:
    pending_count: int
    active_count: int

    @property
    def total_count(self) -> int:
        return self.pending_count + self.active_count


async def _now(session: AsyncSession) -> datetime:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stamp = await session.scalar(select(func.clock_timestamp()))
        if stamp is not None:
            return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    return now_utc()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _effective_status(row: BreakGlassSession, *, now: datetime) -> str:
    if row.status == BreakGlassStatus.REVOKED.value:
        return BreakGlassStatus.REVOKED.value
    if row.status == BreakGlassStatus.EXPIRED.value:
        return BreakGlassStatus.EXPIRED.value
    if row.status == BreakGlassStatus.PENDING.value:
        return (
            BreakGlassStatus.PENDING.value
            if _as_utc(row.approval_deadline) > now
            else BreakGlassStatus.EXPIRED.value
        )
    if row.status == BreakGlassStatus.ACTIVE.value:
        return (
            BreakGlassStatus.ACTIVE.value
            if _as_utc(row.expires_at) > now
            else BreakGlassStatus.EXPIRED.value
        )
    return BreakGlassStatus.EXPIRED.value


async def _active_admin_ids(
    session: AsyncSession, user_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    ids = frozenset(user_ids)
    if not ids:
        return set()
    rows = await session.scalars(
        select(User.id)
        .join(UserRole, UserRole.user_id == User.id)
        .where(
            User.id.in_(ids),
            User.status == UserStatus.ACTIVE.value,
            UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
        )
    )
    return set(rows)


async def _require_active_admin(
    session: AsyncSession, *, user_id: uuid.UUID
) -> None:
    if await _active_admin_ids(session, (user_id,)) != {user_id}:
        raise NotAPlatformAdmin("an active platform superadmin is required")


async def require_platform_admin(
    session: AsyncSession, *, user_id: uuid.UUID
) -> None:
    """Public, read-only role check used to guard the empty selector console."""

    await _require_active_admin(session, user_id=user_id)


def _audit(
    session: AsyncSession,
    *,
    event_type: str,
    actor_user_id: uuid.UUID,
    subject_id: uuid.UUID,
    session_id: uuid.UUID,
    reason_code: str,
    domain_keys: Iterable[str] = (),
    record_count: int | None = None,
) -> None:
    """Append an operational envelope without reason text or medical values."""

    metadata: dict[str, object] = {
        "source_surface": AUDIT_SURFACE,
        "reason_code": reason_code,
        "resource_type": "break_glass_session",
        "resource_id": str(session_id),
    }
    keys = sorted(set(domain_keys))
    if keys:
        metadata["scope_keys"] = [f"domain:{key}" for key in keys]
    if record_count is not None:
        metadata["record_count"] = record_count
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            subject_id=subject_id,
            event_type=event_type,
            outcome=AuditOutcome.SUCCESS.value,
            resource_type="break_glass_session",
            resource_id=str(session_id),
            metadata_json=metadata,
        )
    )


def _normalized_domains(domains: Iterable[Domain]) -> tuple[Domain, ...]:
    supplied = tuple(domains)
    if not supplied:
        raise InvalidEmergencyRequest("at least one record domain is required")
    if len(set(supplied)) != len(supplied):
        raise InvalidEmergencyRequest("record domains must be unique")
    if any(not isinstance(domain, Domain) or domain not in ALLOWED_DOMAINS for domain in supplied):
        raise InvalidEmergencyRequest("only reviewed read-only record domains are allowed")
    return tuple(sorted(supplied, key=lambda item: item.value))


async def _load_exact(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    session_id: uuid.UUID,
    for_update: bool = False,
) -> BreakGlassSession:
    statement = (
        select(BreakGlassSession)
        .options(
            selectinload(BreakGlassSession.scopes),
            selectinload(BreakGlassSession.approvals).selectinload(
                BreakGlassApproval.approved_by
            ),
            selectinload(BreakGlassSession.initiated_by),
            selectinload(BreakGlassSession.subject),
        )
        .where(
            BreakGlassSession.id == session_id,
            BreakGlassSession.subject_id == subject_id,
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        statement = statement.with_for_update()
    row = await session.scalar(statement)
    if row is None:
        raise EmergencySessionNotFound("no emergency session for those selectors")
    return row


def _exact_domain_keys(row: BreakGlassSession) -> tuple[str, ...]:
    keys = tuple(sorted(scope.resource_key for scope in row.scopes))
    if (
        not keys
        or len(keys) != len(set(keys))
        or any(
            scope.resource_type != "domain"
            or scope.action != "read"
            or "*" in scope.resource_key
            for scope in row.scopes
        )
        or any(key not in {domain.value for domain in ALLOWED_DOMAINS} for key in keys)
    ):
        raise InvalidEmergencyRequest("stored emergency scopes are not exact read domains")
    return keys


async def initiate(
    session: AsyncSession,
    *,
    holder_user_id: uuid.UUID,
    subject_id: uuid.UUID,
    reason: str,
    domains: Iterable[Domain],
    ttl_minutes: int,
    incident_reference: str | None = None,
) -> BreakGlassSession:
    """Open a 15-minute approval request; this grants no access by itself."""

    chosen = _normalized_domains(domains)
    if ttl_minutes not in ALLOWED_TTL_MINUTES:
        raise InvalidEmergencyRequest("TTL must be 15, 30, or 60 minutes")
    clean_reason = reason.strip()
    if not clean_reason or len(clean_reason) > 2000:
        raise InvalidEmergencyRequest("a reason of at most 2000 characters is required")
    clean_reference = (incident_reference or "").strip() or None
    if clean_reference is not None and len(clean_reference) > 120:
        raise InvalidEmergencyRequest("incident reference must be at most 120 characters")

    await acquire_identity_governance_lock(session)
    await _require_active_admin(session, user_id=holder_user_id)
    subject = await session.scalar(
        select(HealthSubject).where(HealthSubject.id == subject_id).with_for_update()
    )
    if subject is None or subject.owner_user_id == holder_user_id:
        raise InvalidEmergencyRequest("an emergency session must name another account's record")

    now = await _now(session)
    row = BreakGlassSession(
        subject_id=subject_id,
        initiated_by_user_id=holder_user_id,
        status=BreakGlassStatus.PENDING.value,
        reason=clean_reason,
        incident_reference=clean_reference,
        requested_ttl_minutes=ttl_minutes,
        initiated_at=now,
        approval_deadline=now + APPROVAL_WINDOW,
    )
    session.add(row)
    await session.flush()
    for domain in chosen:
        session.add(
            BreakGlassScope(
                session_id=row.id,
                subject_id=subject_id,
                resource_type="domain",
                resource_key=domain.value,
                action="read",
                created_at=now,
            )
        )
    _audit(
        session,
        event_type=EVENT_INITIATED,
        actor_user_id=holder_user_id,
        subject_id=subject_id,
        session_id=row.id,
        reason_code="emergency_access_initiated",
        domain_keys=(domain.value for domain in chosen),
    )
    await session.flush()
    await session.refresh(row, attribute_names=["scopes"])
    return row


async def inspect_exact(
    session: AsyncSession,
    *,
    viewer_user_id: uuid.UUID,
    subject_id: uuid.UUID,
    session_id: uuid.UUID,
) -> EmergencySessionView:
    """Reveal a request only after both opaque selectors and admin role match."""

    await _require_active_admin(session, user_id=viewer_user_id)
    row = await _load_exact(session, subject_id=subject_id, session_id=session_id)
    now = await _now(session)
    keys = _exact_domain_keys(row)
    return EmergencySessionView(
        session_id=row.id,
        subject_id=row.subject_id,
        holder_user_id=row.initiated_by_user_id,
        holder_username=row.initiated_by.username,
        viewer_is_holder=viewer_user_id == row.initiated_by_user_id,
        status=_effective_status(row, now=now),
        reason=row.reason,
        incident_reference=row.incident_reference,
        domain_keys=keys,
        requested_ttl_minutes=row.requested_ttl_minutes,
        initiated_at=_as_utc(row.initiated_at),
        approval_deadline=_as_utc(row.approval_deadline),
        approval_count=len(row.approvals),
        activated_at=_as_utc(row.activated_at),
        expires_at=_as_utc(row.expires_at),
        revoked_at=_as_utc(row.revoked_at),
    )


async def approve(
    session: AsyncSession,
    *,
    approver_user_id: uuid.UUID,
    subject_id: uuid.UUID,
    session_id: uuid.UUID,
) -> BreakGlassSession:
    """Add one distinct approval and activate exactly on the second."""

    await acquire_identity_governance_lock(session)
    row = await _load_exact(
        session, subject_id=subject_id, session_id=session_id, for_update=True
    )
    now = await _now(session)
    if _effective_status(row, now=now) != BreakGlassStatus.PENDING.value:
        raise EmergencySessionClosed("the fifteen-minute approval window is closed")
    if approver_user_id == row.initiated_by_user_id:
        raise ApprovalNotAllowed("the holder cannot approve their own session")
    await _require_active_admin(session, user_id=approver_user_id)
    await _require_active_admin(session, user_id=row.initiated_by_user_id)
    existing_approval = await session.scalar(
        select(BreakGlassApproval.id).where(
            BreakGlassApproval.session_id == row.id,
            BreakGlassApproval.approved_by_user_id == approver_user_id,
        )
    )
    if existing_approval is not None:
        raise ApprovalNotAllowed("this account already approved the session")
    approval_count_before = int(
        await session.scalar(
            select(func.count(BreakGlassApproval.id)).where(
                BreakGlassApproval.session_id == row.id
            )
        )
        or 0
    )
    if approval_count_before >= 2:
        raise EmergencySessionClosed("the session already has its two approvals")

    approval = BreakGlassApproval(
        session_id=row.id,
        subject_id=row.subject_id,
        holder_user_id=row.initiated_by_user_id,
        approved_by_user_id=approver_user_id,
        approved_at=now,
    )
    session.add(approval)
    await session.flush()
    approval_count = approval_count_before + 1
    _audit(
        session,
        event_type=EVENT_APPROVED,
        actor_user_id=approver_user_id,
        subject_id=row.subject_id,
        session_id=row.id,
        reason_code="emergency_access_approval_recorded",
        record_count=approval_count,
    )

    if approval_count == 2:
        reviewer_ids = set(
            await session.scalars(
                select(BreakGlassApproval.approved_by_user_id).where(
                    BreakGlassApproval.session_id == row.id
                )
            )
        )
        required_ids = reviewer_ids | {row.initiated_by_user_id}
        if len(required_ids) != 3 or await _active_admin_ids(session, required_ids) != required_ids:
            raise ApprovalNotAllowed("three distinct active superadmins are required")
        row.status = BreakGlassStatus.ACTIVE.value
        row.activated_at = now
        row.expires_at = now + timedelta(minutes=row.requested_ttl_minutes)
        _audit(
            session,
            event_type=EVENT_ACTIVATED,
            actor_user_id=approver_user_id,
            subject_id=row.subject_id,
            session_id=row.id,
            reason_code="emergency_access_activated",
            domain_keys=_exact_domain_keys(row),
            record_count=2,
        )
    await session.flush()
    return row


async def authorize_read(
    session: AsyncSession,
    *,
    holder_user_id: uuid.UUID,
    subject_id: uuid.UUID,
    session_id: uuid.UUID,
) -> EmergencyAuthorization:
    """Resolve only this emergency session; no other authority is consulted."""

    await acquire_identity_governance_lock(session)
    row = await _load_exact(
        session, subject_id=subject_id, session_id=session_id, for_update=True
    )
    now = await _now(session)
    if row.initiated_by_user_id != holder_user_id:
        raise EmergencySessionNotFound("the exact holder does not match")
    if _effective_status(row, now=now) != BreakGlassStatus.ACTIVE.value:
        raise EmergencySessionClosed("emergency access is not active")
    reviewer_ids = {item.approved_by_user_id for item in row.approvals}
    required_ids = reviewer_ids | {holder_user_id}
    if len(row.approvals) != 2 or len(required_ids) != 3:
        raise EmergencySessionClosed("two distinct non-holder approvals are required")
    if await _active_admin_ids(session, required_ids) != required_ids:
        raise EmergencySessionClosed("all three approving administrators must remain active")
    keys = _exact_domain_keys(row)
    return EmergencyAuthorization(
        session_id=row.id,
        subject_id=row.subject_id,
        holder_user_id=holder_user_id,
        domain_keys=keys,
        subject_timezone=row.subject.timezone,
        subject_display_name=row.subject.display_name,
        expires_at=_as_utc(row.expires_at),
    )


async def record_opened(
    session: AsyncSession,
    *,
    authorization: EmergencyAuthorization,
    loaded_domain_keys: Iterable[str],
) -> None:
    """Recheck the live session, then audit exactly what left the boundary."""

    current = await authorize_read(
        session,
        holder_user_id=authorization.holder_user_id,
        subject_id=authorization.subject_id,
        session_id=authorization.session_id,
    )
    loaded = tuple(sorted(set(loaded_domain_keys)))
    if any(key not in current.domain_keys for key in loaded):
        raise InvalidEmergencyRequest("the rendered record exceeded its emergency scope")
    _audit(
        session,
        event_type=EVENT_OPENED,
        actor_user_id=current.holder_user_id,
        subject_id=current.subject_id,
        session_id=current.session_id,
        reason_code="emergency_record_opened",
        domain_keys=loaded,
        record_count=len(loaded),
    )
    await session.flush()


async def _require_owner(
    session: AsyncSession, *, owner_user_id: uuid.UUID, subject_id: uuid.UUID
) -> None:
    owner = await session.scalar(
        select(HealthSubject.owner_user_id).where(HealthSubject.id == subject_id)
    )
    if owner != owner_user_id:
        raise NotTheSubjectOwner("only the record owner may manage emergency access")


async def revoke_by_owner(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    subject_id: uuid.UUID,
    session_id: uuid.UUID,
) -> None:
    await _require_owner(session, owner_user_id=owner_user_id, subject_id=subject_id)
    row = await _load_exact(
        session, subject_id=subject_id, session_id=session_id, for_update=True
    )
    now = await _now(session)
    if _effective_status(row, now=now) not in {
        BreakGlassStatus.PENDING.value,
        BreakGlassStatus.ACTIVE.value,
    }:
        raise EmergencySessionClosed("emergency access is already closed")
    row.status = BreakGlassStatus.REVOKED.value
    row.revoked_at = now
    row.revoked_by_user_id = owner_user_id
    _audit(
        session,
        event_type=EVENT_REVOKED,
        actor_user_id=owner_user_id,
        subject_id=subject_id,
        session_id=row.id,
        reason_code="emergency_access_revoked_by_patient",
    )
    await session.flush()


async def revoke_by_holder(
    session: AsyncSession,
    *,
    holder_user_id: uuid.UUID,
    subject_id: uuid.UUID,
    session_id: uuid.UUID,
) -> None:
    await _require_active_admin(session, user_id=holder_user_id)
    row = await _load_exact(
        session, subject_id=subject_id, session_id=session_id, for_update=True
    )
    now = await _now(session)
    if row.initiated_by_user_id != holder_user_id:
        raise EmergencySessionNotFound("the exact holder does not match")
    if _effective_status(row, now=now) not in {
        BreakGlassStatus.PENDING.value,
        BreakGlassStatus.ACTIVE.value,
    }:
        raise EmergencySessionClosed("emergency access is already closed")
    row.status = BreakGlassStatus.REVOKED.value
    row.revoked_at = now
    row.revoked_by_user_id = holder_user_id
    _audit(
        session,
        event_type=EVENT_REVOKED,
        actor_user_id=holder_user_id,
        subject_id=subject_id,
        session_id=row.id,
        reason_code="emergency_access_released_by_holder",
    )
    await session.flush()


async def list_for_subject(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    subject_id: uuid.UUID,
    limit: int = 50,
) -> tuple[PatientEmergencyView, ...]:
    """Patient-visible emergency history for their exact record."""

    await _require_owner(session, owner_user_id=owner_user_id, subject_id=subject_id)
    now = await _now(session)
    rows = (
        await session.scalars(
            select(BreakGlassSession)
            .options(
                selectinload(BreakGlassSession.scopes),
                selectinload(BreakGlassSession.approvals),
                selectinload(BreakGlassSession.initiated_by),
            )
            .where(BreakGlassSession.subject_id == subject_id)
            .order_by(BreakGlassSession.initiated_at.desc(), BreakGlassSession.id)
            .limit(limit)
        )
    ).all()
    return tuple(
        PatientEmergencyView(
            session_id=row.id,
            holder_username=row.initiated_by.username,
            status=_effective_status(row, now=now),
            reason=row.reason,
            incident_reference=row.incident_reference,
            domain_keys=_exact_domain_keys(row),
            initiated_at=_as_utc(row.initiated_at),
            approval_deadline=_as_utc(row.approval_deadline),
            approval_count=len(row.approvals),
            activated_at=_as_utc(row.activated_at),
            expires_at=_as_utc(row.expires_at),
            revoked_at=_as_utc(row.revoked_at),
        )
        for row in rows
    )


async def open_counts_for_subject(
    session: AsyncSession, *, subject_id: uuid.UUID
) -> EmergencyBannerState:
    """Pending requests and active reads that the patient can still revoke."""

    now = await _now(session)
    pending = int(
        await session.scalar(
            select(func.count(BreakGlassSession.id)).where(
                BreakGlassSession.subject_id == subject_id,
                BreakGlassSession.status == BreakGlassStatus.PENDING.value,
                BreakGlassSession.approval_deadline > now,
            )
        )
        or 0
    )
    active = int(
        await session.scalar(
            select(func.count(BreakGlassSession.id)).where(
                BreakGlassSession.subject_id == subject_id,
                BreakGlassSession.status == BreakGlassStatus.ACTIVE.value,
                BreakGlassSession.expires_at > now,
            )
        )
        or 0
    )
    return EmergencyBannerState(pending_count=pending, active_count=active)


__all__ = [
    "ALLOWED_DOMAINS",
    "ALLOWED_TTL_MINUTES",
    "APPROVAL_WINDOW",
    "ApprovalNotAllowed",
    "BreakGlassError",
    "EmergencyAuthorization",
    "EmergencyBannerState",
    "EmergencySessionClosed",
    "EmergencySessionNotFound",
    "EmergencySessionView",
    "InvalidEmergencyRequest",
    "NotAPlatformAdmin",
    "NotTheSubjectOwner",
    "PatientEmergencyView",
    "approve",
    "authorize_read",
    "initiate",
    "inspect_exact",
    "list_for_subject",
    "open_counts_for_subject",
    "record_opened",
    "require_platform_admin",
    "revoke_by_holder",
    "revoke_by_owner",
]
