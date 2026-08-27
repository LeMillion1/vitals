"""Shared support-access scope, error, clock, and audit contract."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AuditOutcome,
    Domain,
    SupportAccessMode,
    SupportAccessRequestStatus,
    SupportScopeResourceType,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import AuditEvent, HealthSubject, User, UserRole
from vitals.models.support_repair import CLEAR_DERIVED_ESTIMATES_OPERATION

REQUEST_WINDOW = timedelta(days=7)

#: The ceiling the schema also enforces. Named here so the screen and the error
#: agree with the constraint instead of discovering it.
MAX_GRANT_TTL = timedelta(hours=24)
DEFAULT_GRANT_TTL = timedelta(hours=2)

#: The surface every audit event from this module names, so a reader can tell
#: support activity from ordinary application writes without joining anything.
AUDIT_SURFACE = "support_access_service"

EVENT_REQUESTED = "support_access.requested"
EVENT_APPROVED = "support_access.approved"
EVENT_DECLINED = "support_access.declined"
EVENT_WITHDRAWN = "support_access.withdrawn"
EVENT_REVOKED = "support_access.revoked"
EVENT_EXPIRED = "support_access.expired"
EVENT_RECORD_OPENED = "support_access.record.opened"
EVENT_RECORD_EXPORTED = "support_access.record.exported"
EVENT_REPAIR_PROPOSED = "support_access.repair.proposed"
EVENT_REPAIR_APPROVED = "support_access.repair.approved"
EVENT_REPAIR_DECLINED = "support_access.repair.declined"
EVENT_REPAIR_EXECUTED = "support_access.repair.executed"
EVENT_REPAIR_STALE = "support_access.repair.stale"
EVENT_REPAIR_REVERTED = "support_access.repair.reverted"

#: The only resource an exceptional support-export grant may contain. Versioned
#: because approving a different export shape must require a fresh decision.
EXPORT_OPERATION_KEY = "data_portability.subject_export.v1"
REPAIR_OPERATION_KEY = CLEAR_DERIVED_ESTIMATES_OPERATION

_LIVE_REQUEST = SupportAccessRequestStatus.PENDING.value


class SupportAccessError(RuntimeError):
    """Base for every refusal this module makes."""


class NotAPlatformAdmin(SupportAccessError):
    """The actor is not an active platform superadmin."""


class NotTheSubjectOwner(SupportAccessError):
    """The actor does not own the record being decided about."""


class RequestNotFound(SupportAccessError):
    """No such request, or not one this actor may see."""


class GrantNotFound(SupportAccessError):
    """No such grant, or not one this actor may act on."""


class AmbiguousSupportGrant(SupportAccessError):
    """More than one live grant exists and the caller did not select one."""


class RequestNotPending(SupportAccessError):
    """The request has already been answered, withdrawn, or has lapsed."""


class UnsupportedMode(SupportAccessError):
    """``repair`` and ``export`` are not implemented and are not pretended to be."""


class ScopesRequired(SupportAccessError):
    """A grant with no scopes authorizes nothing, so an ask with none is refused."""


class NotASupportSession(SupportAccessError):
    """A caller tried to record support use without a matching support grant."""


class RepairNotFound(SupportAccessError):
    """No exact repair action exists in the caller's subject scope."""


class RepairStateError(SupportAccessError):
    """The exact repair action is no longer in the required lifecycle state."""


@dataclass(frozen=True, slots=True)
class RequestedScope:
    """One resource/action pair an ask names, before it is a row."""

    resource_type: SupportScopeResourceType
    resource_key: str
    action: SupportAccessMode


def read_scopes_for(domains: Iterable[Domain]) -> tuple[RequestedScope, ...]:
    """The ordinary shape of a read request: some domains, read on each.

    A helper rather than a default, because "which domains" is the question the
    patient is being asked and something has to have decided it. A caller that
    wants every domain has to say every domain — which is exactly the list the
    approval screen then shows.
    """

    return tuple(
        RequestedScope(
            resource_type=SupportScopeResourceType.DOMAIN,
            resource_key=domain.value,
            action=SupportAccessMode.READ,
        )
        for domain in domains
    )


def export_scope() -> tuple[RequestedScope, ...]:
    """The complete, non-composable shape of a support export request."""

    return (
        RequestedScope(
            resource_type=SupportScopeResourceType.OPERATION,
            resource_key=EXPORT_OPERATION_KEY,
            action=SupportAccessMode.EXPORT,
        ),
    )


def repair_scope() -> tuple[RequestedScope, ...]:
    """The complete, non-composable scope for the first support repair."""

    return (
        RequestedScope(
            resource_type=SupportScopeResourceType.DOMAIN,
            resource_key=Domain.WEIGHT.value,
            action=SupportAccessMode.READ,
        ),
        RequestedScope(
            resource_type=SupportScopeResourceType.OPERATION,
            resource_key=REPAIR_OPERATION_KEY,
            action=SupportAccessMode.REPAIR,
        ),
    )


def _exact_repair_scope_rows() -> set[tuple[str, str, str]]:
    return {
        (
            SupportScopeResourceType.DOMAIN.value,
            Domain.WEIGHT.value,
            SupportAccessMode.READ.value,
        ),
        (
            SupportScopeResourceType.OPERATION.value,
            REPAIR_OPERATION_KEY,
            SupportAccessMode.REPAIR.value,
        ),
    }


async def _now(session: AsyncSession) -> datetime:
    """The wall clock, from the database, advancing inside a transaction.

    ``now()`` is the transaction's start time in PostgreSQL, and a grant's
    expiry is compared against its ``approved_at`` by a check constraint. Two
    reads of "now" that disagree would make a legitimate short grant look
    impossible; one that never advances would make two things written together
    look simultaneous when the order matters.
    """

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stamp = await session.scalar(select(func.clock_timestamp()))
        if stamp is not None:
            return (
                stamp
                if stamp.tzinfo is not None
                else stamp.replace(tzinfo=timezone.utc)
            )
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _audit(
    session: AsyncSession,
    *,
    event_type: str,
    actor_user_id: uuid.UUID,
    subject_id: uuid.UUID,
    grant_id: uuid.UUID | None,
    resource_id: uuid.UUID,
    reason_code: str,
) -> None:
    """One immutable event per state change, carrying no PHI and no free text.

    Deliberately not the reason the admin typed. That sentence is shown to the
    patient, who agreed to read it, and is stored on the request where it
    belongs — the audit envelope is an operational record that gets shipped to
    log sinks and read by people with no business seeing why somebody's record
    was investigated. ``reason_code`` is a fixed vocabulary. Approved scope
    categories stay in the subject-protected request/grant tables instead.
    """

    metadata: dict[str, object] = {
        "source_surface": AUDIT_SURFACE,
        "reason_code": reason_code,
        "resource_type": "support_access_request",
        "resource_id": str(resource_id),
    }
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            subject_id=subject_id,
            support_access_grant_id=grant_id,
            event_type=event_type,
            outcome=AuditOutcome.SUCCESS.value,
            resource_type="support_access_request",
            resource_id=str(resource_id),
            metadata_json=metadata,
        )
    )


async def _require_platform_admin(
    session: AsyncSession, *, user_id: uuid.UUID
) -> None:
    """An active superadmin, checked now rather than remembered from a session.

    A role removed five minutes ago must stop authorizing immediately: this is
    the path by which somebody reaches a patient's record, and a suspended
    account holding a live browser session is precisely the case the check is
    for.
    """

    with session.no_autoflush:
        found = await session.scalar(
            select(User.id)
            .join(UserRole, UserRole.user_id == User.id)
            .where(
                User.id == user_id,
                User.status == UserStatus.ACTIVE.value,
                UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
            )
            .limit(1)
        )
    if found is None:
        raise NotAPlatformAdmin(
            "an active platform superadmin is required to ask for support access"
        )


async def _require_subject_owner(
    session: AsyncSession, *, user_id: uuid.UUID, subject_id: uuid.UUID
) -> None:
    with session.no_autoflush:
        owner = await session.scalar(
            select(HealthSubject.owner_user_id).where(HealthSubject.id == subject_id)
        )
    if owner is None or owner != user_id:
        raise NotTheSubjectOwner(
            "only the person whose record it is may answer a support request"
        )
