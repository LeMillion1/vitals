"""Platform support reaching one patient's record, with the patient deciding.

The policy engine has understood support grants since PR-02: `_support_allows`
in :mod:`vitals.access` checks the grantee, the lifecycle, the expiry, the mode
ceiling and the exact scope. Nothing has ever created one, so the whole path was
unreachable — an admin's role authorized nothing and there was no way to make it
authorize anything. This module is what makes the machinery usable, and it is
deliberately the *only* way a grant comes into existence.

**An admin asks; the patient answers.** That order is the product decision and
it is enforced in three places rather than one, because it is the thing this
module exists to guarantee. ``open_request`` refuses an actor who is not an
active platform superadmin. ``approve_request`` refuses an actor who does not
own the subject. And the schema refuses a grant whose approver is its grantee,
so even a caller that got past both cannot write one.

**Read, exceptional export, and exact repair are different doors.** Read
requests enumerate record sections. Export requests carry one fixed operation
scope and release one transient subject-portability file exactly once. Repair
grants carry one fixed operation plus its explicit read dependency; every
schema-fixed diff still needs separate patient review before execution.

**Nothing is deleted.** A declined or withdrawn request stays, because "support
asked to read my record in March and I said no" is a thing a patient is
entitled to find later, and a table that remembers only the approvals cannot
answer it. Revoking a grant likewise marks it and keeps it.

Times come from the database rather than from the process, and from
``clock_timestamp()`` rather than ``now()`` on PostgreSQL: the expiry of a grant
is compared against ``approved_at`` by a check constraint, and two values that
disagree about when "now" was would make a legitimate one-hour grant look
either impossible or already over.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vitals.access import (
    AccessContext,
    AccessRequest,
    AccessScope,
    PolicyAction,
    PolicyResourceType,
    SupportGrant,
    is_allowed,
)
from vitals.enums import (
    AuditOutcome,
    Domain,
    SupportAccessMode,
    SupportAccessRequestStatus,
    SupportAccessStatus,
    SupportRepairStatus,
    SupportScopeResourceType,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import (
    AuditEvent,
    HealthSubject,
    SupportAccessGrant,
    SupportAccessRequest,
    SupportAccessRequestScope,
    SupportAccessScope,
    User,
    UserRole,
)
from vitals.models.support_repair import (
    CLEAR_DERIVED_ESTIMATES_OPERATION,
    SupportRepairAction,
)
from vitals.models.weight import BodyMeasurement
from vitals.ownership import WriteIdentity
from vitals.services import data_portability_service
from vitals.services import conflict_engine
from vitals.services.identity_service import acquire_identity_governance_lock

#: How long an unanswered ask stays answerable. A request nobody replied to is
#: not a pending obligation forever, and a patient returning after a holiday
#: should find it closed rather than live.
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


async def open_request(
    session: AsyncSession,
    *,
    admin_user_id: uuid.UUID,
    subject_id: uuid.UUID,
    reason: str,
    scopes: Sequence[RequestedScope],
    ttl: timedelta = DEFAULT_GRANT_TTL,
    ticket_reference: str | None = None,
    mode: SupportAccessMode = SupportAccessMode.READ,
) -> SupportAccessRequest:
    """Ask one patient for time-limited, scoped access to their record.

    Never commits. Authorizes nothing by itself: what this writes is a question,
    and until the patient answers it the policy engine has no grant to find.
    """

    if not scopes:
        raise ScopesRequired(
            "a grant with no scopes authorizes nothing, so an ask with none is "
            "a question with no answer"
        )
    if mode is SupportAccessMode.READ:
        if any(scope.action is not SupportAccessMode.READ for scope in scopes):
            raise UnsupportedMode("a read request may only ask for read scopes")
    elif mode is SupportAccessMode.REPAIR:
        supplied = {
            (scope.resource_type.value, scope.resource_key, scope.action.value)
            for scope in scopes
        }
        if supplied != _exact_repair_scope_rows() or len(scopes) != 2:
            raise UnsupportedMode(
                "a repair request must name only Weight read and the exact "
                "derived-estimate clearing operation"
            )
    elif mode is SupportAccessMode.EXPORT:
        if tuple(scopes) != export_scope():
            raise UnsupportedMode(
                "an export request must name only the versioned subject export operation"
            )
        if ttl != DEFAULT_GRANT_TTL:
            raise SupportAccessError(
                "an exceptional support export uses the fixed two-hour approval window"
            )
    else:  # Defensive against callers bypassing the enum type contract.
        raise UnsupportedMode(f"support mode {mode!r} is not implemented")
    cleaned_reason = (reason or "").strip()
    if not cleaned_reason:
        raise SupportAccessError(
            "a support request needs a reason: it is shown to the patient "
            "verbatim, and an approval asked for without one is not informed"
        )
    if ttl <= timedelta(0) or ttl > MAX_GRANT_TTL:
        raise SupportAccessError(
            f"a support grant lasts between one second and {MAX_GRANT_TTL}"
        )

    await _require_platform_admin(session, user_id=admin_user_id)
    with session.no_autoflush:
        owner = await session.scalar(
            select(HealthSubject.owner_user_id).where(HealthSubject.id == subject_id)
        )
    if owner is None:
        raise SupportAccessError("no such health subject")
    if owner == admin_user_id:
        # The schema refuses it too, one layer down. Said here as well because
        # "you cannot approve your own request" is a sentence, and a check
        # constraint violation is a stack trace.
        raise NotTheSubjectOwner(
            "an admin cannot ask themselves for access to their own record"
        )

    stamp = await _now(session)
    request = SupportAccessRequest(
        subject_id=subject_id,
        requested_by_user_id=admin_user_id,
        mode=mode.value,
        status=SupportAccessRequestStatus.PENDING.value,
        reason=cleaned_reason,
        ticket_reference=(ticket_reference or "").strip() or None,
        requested_ttl_seconds=int(ttl.total_seconds()),
        created_at=stamp,
        expires_at=stamp + REQUEST_WINDOW,
    )
    session.add(request)
    await session.flush()

    for scope in scopes:
        session.add(
            SupportAccessRequestScope(
                request_id=request.id,
                subject_id=subject_id,
                resource_type=scope.resource_type.value,
                resource_key=scope.resource_key,
                action=scope.action.value,
            )
        )
    _audit(
        session,
        event_type=EVENT_REQUESTED,
        actor_user_id=admin_user_id,
        subject_id=subject_id,
        grant_id=None,
        resource_id=request.id,
        reason_code="support_access_requested",
    )
    await session.flush()
    return request


async def _pending(
    session: AsyncSession, *, request_id: uuid.UUID, now: datetime
) -> SupportAccessRequest:
    request = await session.scalar(
        select(SupportAccessRequest)
        .options(selectinload(SupportAccessRequest.scopes))
        .where(SupportAccessRequest.id == request_id)
        .with_for_update()
    )
    if request is None:
        raise RequestNotFound("no such support request")
    if request.status != _LIVE_REQUEST:
        raise RequestNotPending(
            f"this request is already {request.status} and cannot be answered again"
        )
    if now >= _as_utc(request.expires_at):
        raise RequestNotPending("this request has lapsed and cannot be answered")
    return request


async def approve_request(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    request_id: uuid.UUID,
) -> SupportAccessGrant:
    """Say yes, which is the only thing that ever writes a grant. Never commits."""

    now = await _now(session)
    request = await _pending(session, request_id=request_id, now=now)
    if request.mode == SupportAccessMode.EXPORT.value:
        stored_scopes = {
            (scope.resource_type, scope.resource_key, scope.action)
            for scope in request.scopes
        }
        exact_export_scope = {
            (
                SupportScopeResourceType.OPERATION.value,
                EXPORT_OPERATION_KEY,
                SupportAccessMode.EXPORT.value,
            )
        }
        if (
            stored_scopes != exact_export_scope
            or request.requested_ttl_seconds
            != int(DEFAULT_GRANT_TTL.total_seconds())
        ):
            raise UnsupportedMode(
                "the stored export request is not the exact approved operation"
            )
    elif request.mode == SupportAccessMode.REPAIR.value:
        stored_scopes = {
            (scope.resource_type, scope.resource_key, scope.action)
            for scope in request.scopes
        }
        if stored_scopes != _exact_repair_scope_rows() or len(request.scopes) != 2:
            raise UnsupportedMode(
                "the stored repair request is not the exact reviewed operation"
            )
    await _require_subject_owner(
        session, user_id=owner_user_id, subject_id=request.subject_id
    )
    # Re-checked at the moment of approval rather than trusted from the ask: a
    # superadmin whose role was removed while the request sat unanswered must
    # not be handed access by an approval that arrives afterwards.
    await _require_platform_admin(session, user_id=request.requested_by_user_id)

    grant = SupportAccessGrant(
        subject_id=request.subject_id,
        granted_to_user_id=request.requested_by_user_id,
        approved_by_user_id=owner_user_id,
        mode=request.mode,
        status=SupportAccessStatus.ACTIVE.value,
        reason=request.reason,
        approved_at=now,
        expires_at=now + timedelta(seconds=request.requested_ttl_seconds),
    )
    session.add(grant)
    await session.flush()

    for scope in request.scopes:
        session.add(
            SupportAccessScope(
                grant_id=grant.id,
                resource_type=scope.resource_type,
                resource_key=scope.resource_key,
                action=scope.action,
            )
        )

    request.status = SupportAccessRequestStatus.APPROVED.value
    request.decided_at = now
    request.decided_by_user_id = owner_user_id
    request.granted_id = grant.id

    _audit(
        session,
        event_type=EVENT_APPROVED,
        actor_user_id=owner_user_id,
        subject_id=request.subject_id,
        grant_id=grant.id,
        resource_id=request.id,
        reason_code="support_access_approved",
    )
    await session.flush()
    return grant


async def decline_request(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    request_id: uuid.UUID,
) -> SupportAccessRequest:
    """Say no, and keep having said it. Never commits."""

    now = await _now(session)
    request = await _pending(session, request_id=request_id, now=now)
    await _require_subject_owner(
        session, user_id=owner_user_id, subject_id=request.subject_id
    )
    request.status = SupportAccessRequestStatus.DECLINED.value
    request.decided_at = now
    request.decided_by_user_id = owner_user_id
    _audit(
        session,
        event_type=EVENT_DECLINED,
        actor_user_id=owner_user_id,
        subject_id=request.subject_id,
        grant_id=None,
        resource_id=request.id,
        reason_code="support_access_declined",
    )
    await session.flush()
    return request


async def withdraw_request(
    session: AsyncSession,
    *,
    admin_user_id: uuid.UUID,
    request_id: uuid.UUID,
) -> SupportAccessRequest:
    """Take the ask back, which only the person who asked may do. Never commits.

    Recorded as ``withdrawn`` rather than deleted, and marked as decided by the
    admin: the patient's history should show that they were asked and that the
    ask was taken back, not a gap where a question used to be.
    """

    now = await _now(session)
    request = await _pending(session, request_id=request_id, now=now)
    if request.requested_by_user_id != admin_user_id:
        raise RequestNotFound("no such support request")
    request.status = SupportAccessRequestStatus.WITHDRAWN.value
    request.decided_at = now
    request.decided_by_user_id = admin_user_id
    _audit(
        session,
        event_type=EVENT_WITHDRAWN,
        actor_user_id=admin_user_id,
        subject_id=request.subject_id,
        grant_id=None,
        resource_id=request.id,
        reason_code="support_access_withdrawn",
    )
    await session.flush()
    return request


async def revoke_grant(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    grant_id: uuid.UUID,
    reason: str,
) -> SupportAccessGrant:
    """End a live grant now. Never commits.

    Either side may: the patient who approved it, and the admin who holds it.
    A patient who changes their mind must not have to find somebody, and an
    admin who has finished should be able to put the access down rather than
    wait for it to lapse.
    """

    cleaned = (reason or "").strip()
    if not cleaned:
        raise SupportAccessError("revoking a support grant needs a reason")

    grant = await session.scalar(
        select(SupportAccessGrant)
        .where(SupportAccessGrant.id == grant_id)
        .with_for_update()
    )
    if grant is None:
        raise GrantNotFound("no such support grant")

    with session.no_autoflush:
        owner = await session.scalar(
            select(HealthSubject.owner_user_id).where(
                HealthSubject.id == grant.subject_id
            )
        )
    if actor_user_id not in (owner, grant.granted_to_user_id):
        raise GrantNotFound("no such support grant")
    if grant.status != SupportAccessStatus.ACTIVE.value:
        raise SupportAccessError(f"this grant is already {grant.status}")

    now = await _now(session)
    grant.status = SupportAccessStatus.REVOKED.value
    grant.revoked_at = now
    grant.revoked_by_user_id = actor_user_id
    grant.revocation_reason = cleaned
    _audit(
        session,
        event_type=EVENT_REVOKED,
        actor_user_id=actor_user_id,
        subject_id=grant.subject_id,
        grant_id=grant.id,
        resource_id=grant.id,
        reason_code=(
            "support_access_revoked_by_patient"
            if actor_user_id == owner
            else "support_access_revoked_by_admin"
        ),
    )
    await session.flush()
    return grant


async def load_support_grant(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    admin_user_id: uuid.UUID,
    evaluated_at: datetime,
    support_grant_id: uuid.UUID | None = None,
) -> SupportGrant | None:
    """Assemble one exact, live support grant snapshot for policy evaluation.

    A supplied id must match this subject and grantee and be live. Without one,
    the historical direct URL remains compatible only when exactly one grant is
    live. Two grants are never merged and never guessed between: ambiguity is a
    refusal that the delivery layer renders as the same 404 as every other miss.
    """

    await _require_platform_admin(session, user_id=admin_user_id)
    statement = (
        select(SupportAccessGrant)
        .options(selectinload(SupportAccessGrant.scopes))
        .where(
            SupportAccessGrant.subject_id == subject_id,
            SupportAccessGrant.granted_to_user_id == admin_user_id,
            SupportAccessGrant.status == SupportAccessStatus.ACTIVE.value,
            SupportAccessGrant.expires_at > evaluated_at,
        )
    )
    if support_grant_id is not None:
        statement = statement.where(SupportAccessGrant.id == support_grant_id)
    grants = list(
        (
            await session.execute(
                statement.order_by(
                    SupportAccessGrant.expires_at.desc(), SupportAccessGrant.id
                ).limit(2)
            )
        )
        .scalars()
        .all()
    )
    if not grants:
        return None
    if support_grant_id is None and len(grants) > 1:
        raise AmbiguousSupportGrant(
            "multiple live support grants require an exact grant selector"
        )
    grant = grants[0]
    return SupportGrant(
        grant_id=grant.id,
        granted_to_user_id=grant.granted_to_user_id,
        subject_id=grant.subject_id,
        mode=SupportAccessMode(grant.mode),
        status=SupportAccessStatus(grant.status),
        expires_at=_as_utc(grant.expires_at),
        revoked_at=_as_utc(grant.revoked_at) if grant.revoked_at else None,
        scopes=frozenset(
            AccessScope(
                resource_type=PolicyResourceType(scope.resource_type),
                resource_key=scope.resource_key,
                action=PolicyAction(scope.action),
            )
            for scope in grant.scopes
        ),
    )


@dataclass(frozen=True, slots=True)
class PatientLiveGrant:
    """One live support door as the record owner is entitled to see it."""

    grant_id: uuid.UUID
    grantee_username: str
    expires_at: datetime
    scope_keys: tuple[str, ...]


async def live_grants_for(
    session: AsyncSession, *, context: AccessContext
) -> tuple[PatientLiveGrant, ...]:
    """Every currently effective support grant for the owner's record.

    This is deliberately a patient-only projection. A professional or support
    holder may know the grant that authorizes *their* access, but must not use
    this function to enumerate which other operators can reach the record.
    """

    if context.subject_owner_user_id != context.principal.user_id:
        raise NotTheSubjectOwner(
            "only the person whose record it is may list every live support grant"
        )

    rows = (
        await session.execute(
            select(SupportAccessGrant, User.username)
            .options(selectinload(SupportAccessGrant.scopes))
            .join(User, User.id == SupportAccessGrant.granted_to_user_id)
            .where(
                SupportAccessGrant.subject_id == context.subject_id,
                SupportAccessGrant.status == SupportAccessStatus.ACTIVE.value,
                SupportAccessGrant.expires_at > context.evaluated_at,
            )
            .order_by(
                SupportAccessGrant.expires_at,
                SupportAccessGrant.approved_at,
                SupportAccessGrant.id,
            )
        )
    ).all()
    return tuple(
        PatientLiveGrant(
            grant_id=grant.id,
            grantee_username=username,
            expires_at=_as_utc(grant.expires_at),
            scope_keys=tuple(
                sorted(
                    f"{scope.resource_type}:{scope.resource_key}"
                    for scope in grant.scopes
                    if scope.action == grant.mode
                )
            ),
        )
        for grant, username in rows
    )


@dataclass(frozen=True, slots=True)
class PatientAccessRequest:
    """One request as the record owner may safely render it."""

    request_id: uuid.UUID
    requested_by_username: str
    effective_status: str
    reason: str
    ticket_reference: str | None
    requested_ttl_seconds: int
    created_at: datetime
    expires_at: datetime
    scope_keys: tuple[str, ...]
    grant_lifecycle: str | None
    grant_ends_at: datetime | None
    grant_end_actor_username: str | None


@dataclass(frozen=True, slots=True)
class PatientAccessRequestHistory:
    """A bounded patient history, with actionable requests kept first."""

    pending: tuple[PatientAccessRequest, ...]
    past: tuple[PatientAccessRequest, ...]
    has_more: bool


def _grant_lifecycle(
    request: SupportAccessRequest, *, now: datetime
) -> tuple[str | None, datetime | None, str | None]:
    """Derive grant truth without relying on the expiry maintenance job."""

    grant = request.granted
    if request.status != SupportAccessRequestStatus.APPROVED.value or grant is None:
        return None, None, None
    if grant.status == SupportAccessStatus.REVOKED.value or grant.revoked_at is not None:
        if grant.revoked_by_user_id == grant.approved_by_user_id:
            lifecycle = "revoked_by_owner"
        elif grant.revoked_by_user_id == grant.granted_to_user_id:
            lifecycle = "handed_back_by_holder"
        else:
            lifecycle = "revoked"
        return (
            lifecycle,
            _as_utc(grant.revoked_at) if grant.revoked_at is not None else None,
            grant.revoked_by.username if grant.revoked_by is not None else None,
        )
    if grant.status == SupportAccessStatus.CONSUMED.value:
        return (
            "consumed",
            _as_utc(grant.consumed_at) if grant.consumed_at is not None else None,
            None,
        )
    if grant.status == SupportAccessStatus.EXPIRED.value or now >= _as_utc(
        grant.expires_at
    ):
        return "expired", _as_utc(grant.expires_at), None
    if grant.status == SupportAccessStatus.ACTIVE.value:
        return "live", _as_utc(grant.expires_at), None
    return None, None, None


async def list_for_subject(
    session: AsyncSession,
    *,
    context: AccessContext,
    limit: int = 50,
) -> PatientAccessRequestHistory:
    """Return at most ``limit`` requests for the owner-protected access page.

    Effective pending/expired state comes from the database clock on every
    read; this projection never depends on ``expire_stale`` and never performs
    maintenance writes. Actionable requests are selected before recent past
    requests so a busy history cannot hide a decision the owner still needs to
    make. Declined, withdrawn, approved, and lapsed requests remain visible.
    """

    if limit < 1 or limit > 100:
        raise ValueError("support request history limit must be between 1 and 100")
    if context.subject_owner_user_id != context.principal.user_id:
        raise NotTheSubjectOwner(
            "only the person whose record it is may read its support request history"
        )

    now = await _now(session)
    effectively_pending = (
        (SupportAccessRequest.status == _LIVE_REQUEST)
        & (SupportAccessRequest.expires_at > now)
    )
    rows = list(
        (
            await session.execute(
                select(SupportAccessRequest)
                .options(
                    selectinload(SupportAccessRequest.scopes),
                    selectinload(SupportAccessRequest.requested_by),
                    selectinload(SupportAccessRequest.granted).selectinload(
                        SupportAccessGrant.revoked_by
                    ),
                )
                .where(SupportAccessRequest.subject_id == context.subject_id)
                .order_by(
                    case((effectively_pending, 0), else_=1),
                    SupportAccessRequest.created_at.desc(),
                    SupportAccessRequest.id.desc(),
                )
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )

    projected: list[PatientAccessRequest] = []
    for request in rows[:limit]:
        effective_status = request.status
        if (
            effective_status == _LIVE_REQUEST
            and now >= _as_utc(request.expires_at)
        ):
            effective_status = SupportAccessRequestStatus.EXPIRED.value
        lifecycle, grant_ends_at, actor_username = _grant_lifecycle(
            request, now=now
        )
        projected.append(
            PatientAccessRequest(
                request_id=request.id,
                requested_by_username=request.requested_by.username,
                effective_status=effective_status,
                reason=request.reason,
                ticket_reference=request.ticket_reference,
                requested_ttl_seconds=request.requested_ttl_seconds,
                created_at=_as_utc(request.created_at),
                expires_at=_as_utc(request.expires_at),
                scope_keys=tuple(
                    sorted(
                        f"{scope.resource_type}:{scope.resource_key}"
                        for scope in request.scopes
                        if scope.action == request.mode
                    )
                ),
                grant_lifecycle=lifecycle,
                grant_ends_at=grant_ends_at,
                grant_end_actor_username=actor_username,
            )
        )

    pending = tuple(
        request
        for request in projected
        if request.effective_status == SupportAccessRequestStatus.PENDING.value
    )
    past = tuple(
        request
        for request in projected
        if request.effective_status != SupportAccessRequestStatus.PENDING.value
    )
    return PatientAccessRequestHistory(
        pending=pending,
        past=past,
        has_more=len(rows) > limit,
    )


async def record_record_opened(
    session: AsyncSession,
    *,
    context: AccessContext,
    domain_keys: Iterable[str],
    artifact_keys: Iterable[str] = (),
) -> AuditEvent:
    """Durably describe one support-granted record response, without PHI.

    The exact grant row is locked and rechecked after the record was assembled.
    This turns a revoke/read race into an order: either revocation wins and the
    response is refused, or this event commits before the response is returned
    and revocation follows it. A caller must commit this event before handing
    the rendered medical response to the browser.
    """

    snapshot = context.support_grant
    if snapshot is None:
        raise NotASupportSession("record access is not based on a support grant")
    if snapshot.subject_id != context.subject_id:
        raise NotASupportSession("support grant and selected record do not match")
    if snapshot.granted_to_user_id != context.principal.user_id:
        raise NotASupportSession("support grant and signed-in account do not match")

    # Role assignment/removal takes this same transaction lock. Holding it
    # through the caller's disclosure commit makes the live-role check an
    # ordered fact rather than a snapshot that can race role revocation.
    await acquire_identity_governance_lock(session)
    await _require_platform_admin(session, user_id=context.principal.user_id)
    grant = (
        await session.execute(
            select(
                SupportAccessGrant.id,
                SupportAccessGrant.status,
                SupportAccessGrant.revoked_at,
                SupportAccessGrant.expires_at,
                SupportAccessGrant.mode,
            )
            .where(
                SupportAccessGrant.id == snapshot.grant_id,
                SupportAccessGrant.subject_id == context.subject_id,
                SupportAccessGrant.granted_to_user_id == context.principal.user_id,
            )
            .with_for_update()
        )
    ).one_or_none()
    if grant is None:
        raise NotASupportSession("the support grant no longer exists")

    now = await _now(session)
    if (
        grant.status != SupportAccessStatus.ACTIVE.value
        or grant.revoked_at is not None
        or now >= _as_utc(grant.expires_at)
    ):
        raise NotASupportSession("the support grant is no longer active")

    live_scopes = set(
        (
            await session.execute(
                select(
                    SupportAccessScope.resource_type,
                    SupportAccessScope.resource_key,
                    SupportAccessScope.action,
                ).where(SupportAccessScope.grant_id == grant.id)
            )
        ).all()
    )

    domains = tuple(
        sorted({str(key).strip() for key in domain_keys if str(key).strip()})
    )
    artifacts = tuple(
        sorted({str(key).strip() for key in artifact_keys if str(key).strip()})
    )
    requested = tuple((PolicyResourceType.DOMAIN, key) for key in domains) + tuple(
        (PolicyResourceType.ARTIFACT, key) for key in artifacts
    )
    for resource_type, resource_key in requested:
        request = AccessRequest(
            subject_id=context.subject_id,
            resource_type=resource_type,
            resource_key=resource_key,
            action=PolicyAction.READ,
        )
        if not is_allowed(context, request):
            raise NotASupportSession(
                "the rendered record exceeds the approved support scope"
            )
        if (
            resource_type.value,
            resource_key,
            SupportAccessMode.READ.value,
        ) not in live_scopes:
            raise NotASupportSession(
                "the live support grant does not contain the rendered scope"
            )

    event = AuditEvent(
        actor_user_id=context.principal.user_id,
        subject_id=context.subject_id,
        support_access_grant_id=grant.id,
        event_type=EVENT_RECORD_OPENED,
        outcome=AuditOutcome.SUCCESS.value,
        resource_type="health_record",
        resource_id=str(context.subject_id),
        metadata_json={
            "correlation_id": str(uuid.uuid4()),
            "source_surface": "web.care.record",
            "reason_code": "approved_support_read",
            "resource_type": "health_record",
            "resource_id": str(context.subject_id),
            "grant_mode": grant.mode,
        },
    )
    session.add(event)
    await session.flush()
    return event


async def consume_subject_export(
    session: AsyncSession,
    *,
    context: AccessContext,
) -> dict[str, object]:
    """Build and consume one exact exceptional export grant. Never commits.

    The grant and identity-governance locks remain held while the portability
    snapshot is assembled. The caller must serialize the returned value and
    commit this transaction before returning any bytes. A generation or
    serialization failure can then roll back without spending the approval;
    once the commit lands, the grant is terminal even if the connection drops.
    """

    snapshot = context.support_grant
    if snapshot is None:
        raise NotASupportSession("export is not based on a support grant")
    if (
        snapshot.subject_id != context.subject_id
        or snapshot.granted_to_user_id != context.principal.user_id
        or snapshot.mode is not SupportAccessMode.EXPORT
    ):
        raise NotASupportSession("support export grant does not match this request")

    exact_request = AccessRequest(
        subject_id=context.subject_id,
        resource_type=PolicyResourceType.OPERATION,
        resource_key=EXPORT_OPERATION_KEY,
        action=PolicyAction.EXPORT,
    )
    if not is_allowed(context, exact_request):
        raise NotASupportSession("support export is outside the approved scope")

    await acquire_identity_governance_lock(session)
    await _require_platform_admin(session, user_id=context.principal.user_id)
    grant = await session.scalar(
        select(SupportAccessGrant)
        .options(selectinload(SupportAccessGrant.scopes))
        .where(
            SupportAccessGrant.id == snapshot.grant_id,
            SupportAccessGrant.subject_id == context.subject_id,
            SupportAccessGrant.granted_to_user_id == context.principal.user_id,
        )
        .with_for_update()
    )
    if grant is None:
        raise NotASupportSession("the support export grant no longer exists")

    now = await _now(session)
    if (
        grant.mode != SupportAccessMode.EXPORT.value
        or grant.status != SupportAccessStatus.ACTIVE.value
        or grant.revoked_at is not None
        or grant.consumed_at is not None
        or now >= _as_utc(grant.expires_at)
    ):
        raise NotASupportSession("the support export grant is no longer usable")

    live_scopes = {
        (scope.resource_type, scope.resource_key, scope.action)
        for scope in grant.scopes
    }
    required_scope = {
        (
            SupportScopeResourceType.OPERATION.value,
            EXPORT_OPERATION_KEY,
            SupportAccessMode.EXPORT.value,
        )
    }
    if live_scopes != required_scope:
        raise NotASupportSession("the support export grant is not exact")

    payload = await data_portability_service.export_subject(
        session, subject_id=context.subject_id
    )
    grant.status = SupportAccessStatus.CONSUMED.value
    grant.consumed_at = now
    event = AuditEvent(
        actor_user_id=context.principal.user_id,
        subject_id=context.subject_id,
        support_access_grant_id=grant.id,
        event_type=EVENT_RECORD_EXPORTED,
        outcome=AuditOutcome.SUCCESS.value,
        resource_type="subject_export",
        resource_id=str(context.subject_id),
        metadata_json={
            "correlation_id": str(uuid.uuid4()),
            "source_surface": "web.settings.support_export",
            "reason_code": "approved_support_export",
            "resource_type": "subject_export",
            "resource_id": str(context.subject_id),
            "grant_mode": SupportAccessMode.EXPORT.value,
        },
    )
    session.add(event)
    await session.flush()
    return payload


def _repair_audit(
    session: AsyncSession,
    *,
    action: SupportRepairAction,
    actor_user_id: uuid.UUID,
    event_type: str,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    result_code: str,
) -> None:
    """Append a grant-correlated repair event without copying medical values."""

    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            subject_id=action.subject_id,
            support_access_grant_id=action.support_access_grant_id,
            event_type=event_type,
            outcome=outcome.value,
            resource_type="body_measurement",
            resource_id=str(action.target_body_measurement_id),
            metadata_json={
                "request_id": str(action.id),
                "source_surface": AUDIT_SURFACE,
                "result_code": result_code,
                "reason_code": "approved_support_repair",
                "resource_type": "body_measurement",
                "resource_id": str(action.target_body_measurement_id),
                "changed_fields": ["body_fat_pct", "lbm_kg"],
                "grant_mode": SupportAccessMode.REPAIR.value,
            },
        )
    )


def _grant_has_exact_repair_scope(grant: SupportAccessGrant) -> bool:
    return {
        (scope.resource_type, scope.resource_key, scope.action)
        for scope in grant.scopes
    } == _exact_repair_scope_rows() and len(grant.scopes) == 2


def _context_has_exact_repair_scope(context: AccessContext) -> bool:
    required = (
        AccessRequest(
            subject_id=context.subject_id,
            resource_type=PolicyResourceType.DOMAIN,
            resource_key=Domain.WEIGHT.value,
            action=PolicyAction.READ,
        ),
        AccessRequest(
            subject_id=context.subject_id,
            resource_type=PolicyResourceType.OPERATION,
            resource_key=REPAIR_OPERATION_KEY,
            action=PolicyAction.REPAIR,
        ),
    )
    return all(is_allowed(context, request) for request in required)


async def _lock_live_repair_grant(
    session: AsyncSession, *, context: AccessContext
) -> tuple[SupportAccessGrant, datetime]:
    snapshot = context.support_grant
    if (
        snapshot is None
        or snapshot.subject_id != context.subject_id
        or snapshot.granted_to_user_id != context.principal.user_id
        or snapshot.mode is not SupportAccessMode.REPAIR
        or not _context_has_exact_repair_scope(context)
    ):
        raise NotASupportSession("repair is not based on the exact approved grant")

    await acquire_identity_governance_lock(session)
    await _require_platform_admin(session, user_id=context.principal.user_id)
    grant = await session.scalar(
        select(SupportAccessGrant)
        .options(selectinload(SupportAccessGrant.scopes))
        .where(
            SupportAccessGrant.id == snapshot.grant_id,
            SupportAccessGrant.subject_id == context.subject_id,
            SupportAccessGrant.granted_to_user_id == context.principal.user_id,
        )
        .with_for_update()
    )
    if grant is None:
        raise NotASupportSession("the repair grant no longer exists")
    now = await _now(session)
    if (
        grant.mode != SupportAccessMode.REPAIR.value
        or grant.status != SupportAccessStatus.ACTIVE.value
        or grant.revoked_at is not None
        or now >= _as_utc(grant.expires_at)
        or not _grant_has_exact_repair_scope(grant)
    ):
        raise NotASupportSession("the repair grant is no longer exact and active")
    return grant, now


@dataclass(frozen=True, slots=True)
class RepairMeasurement:
    measurement_id: int
    date: object
    body_fat_pct: float | None
    lbm_kg: float | None


@dataclass(frozen=True, slots=True)
class RepairActionView:
    action_id: uuid.UUID
    grant_id: uuid.UUID
    operator_username: str
    measurement_id: int
    measurement_date: object
    before_body_fat_pct: float | None
    before_lbm_kg: float | None
    status: str
    proposed_at: datetime
    execute_before: datetime


def _effective_repair_status(
    action: SupportRepairAction, *, now: datetime
) -> str:
    if action.status not in {
        SupportRepairStatus.PROPOSED.value,
        SupportRepairStatus.APPROVED.value,
    }:
        return action.status
    grant = action.grant
    if (
        grant.status != SupportAccessStatus.ACTIVE.value
        or grant.revoked_at is not None
        or now >= _as_utc(grant.expires_at)
        or now >= _as_utc(action.execute_before)
    ):
        return "expired"
    return action.status


def _repair_view(
    action: SupportRepairAction, *, now: datetime
) -> RepairActionView:
    return RepairActionView(
        action_id=action.id,
        grant_id=action.support_access_grant_id,
        operator_username=action.proposed_by.username,
        measurement_id=action.target_body_measurement_id,
        measurement_date=action.target.date,
        before_body_fat_pct=action.before_body_fat_pct,
        before_lbm_kg=action.before_lbm_kg,
        status=_effective_repair_status(action, now=now),
        proposed_at=_as_utc(action.proposed_at),
        execute_before=_as_utc(action.execute_before),
    )


async def repair_workspace(
    session: AsyncSession, *, context: AccessContext
) -> tuple[tuple[RepairMeasurement, ...], tuple[RepairActionView, ...]]:
    """Subject-bound proposal workspace for one exact repair grant."""

    grant, now = await _lock_live_repair_grant(session, context=context)
    measurements = tuple(
        RepairMeasurement(
            measurement_id=row.id,
            date=row.date,
            body_fat_pct=row.body_fat_pct,
            lbm_kg=row.lbm_kg,
        )
        for row in await session.scalars(
            select(BodyMeasurement)
            .where(
                BodyMeasurement.subject_id == context.subject_id,
                (BodyMeasurement.body_fat_pct.is_not(None))
                | (BodyMeasurement.lbm_kg.is_not(None)),
            )
            .order_by(BodyMeasurement.date.desc(), BodyMeasurement.id.desc())
            .limit(100)
        )
    )
    actions = list(
        await session.scalars(
            select(SupportRepairAction)
            .options(
                selectinload(SupportRepairAction.grant),
                selectinload(SupportRepairAction.proposed_by),
                selectinload(SupportRepairAction.target),
            )
            .where(SupportRepairAction.support_access_grant_id == grant.id)
            .order_by(SupportRepairAction.proposed_at.desc())
            .limit(50)
        )
    )
    return measurements, tuple(_repair_view(row, now=now) for row in actions)


async def propose_clear_derived_estimates(
    session: AsyncSession,
    *,
    context: AccessContext,
    measurement_id: int,
    idempotency_key: uuid.UUID,
) -> SupportRepairAction:
    """Propose the fixed NULL/NULL diff. It does not mutate the measurement."""

    if isinstance(measurement_id, bool) or not isinstance(measurement_id, int):
        raise RepairNotFound("measurement id must be an integer")
    if not isinstance(idempotency_key, uuid.UUID) or idempotency_key.int == 0:
        raise SupportAccessError("idempotency key must be a non-zero UUID")
    grant, now = await _lock_live_repair_grant(session, context=context)
    existing = await session.scalar(
        select(SupportRepairAction).where(
            SupportRepairAction.support_access_grant_id == grant.id,
            SupportRepairAction.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing

    target = await session.scalar(
        select(BodyMeasurement)
        .where(
            BodyMeasurement.id == measurement_id,
            BodyMeasurement.subject_id == context.subject_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if target is None:
        raise RepairNotFound("no such body measurement in this record")
    if target.body_fat_pct is None and target.lbm_kg is None:
        raise RepairStateError("the derived estimates are already absent")
    open_action = await session.scalar(
        select(SupportRepairAction.id).where(
            SupportRepairAction.subject_id == context.subject_id,
            SupportRepairAction.target_body_measurement_id == target.id,
            SupportRepairAction.operation_key == REPAIR_OPERATION_KEY,
            SupportRepairAction.status.in_(
                (
                    SupportRepairStatus.PROPOSED.value,
                    SupportRepairStatus.APPROVED.value,
                )
            ),
        )
    )
    if open_action is not None:
        raise RepairStateError("this grant already has an open action for the target")

    action = SupportRepairAction(
        subject_id=context.subject_id,
        support_access_grant_id=grant.id,
        proposed_by_user_id=context.principal.user_id,
        operation_key=REPAIR_OPERATION_KEY,
        target_body_measurement_id=target.id,
        status=SupportRepairStatus.PROPOSED.value,
        idempotency_key=idempotency_key,
        proposed_at=now,
        execute_before=_as_utc(grant.expires_at),
        before_body_fat_pct=target.body_fat_pct,
        before_lbm_kg=target.lbm_kg,
        target_updated_at_at_proposal=target.updated_at,
    )
    session.add(action)
    await session.flush()
    _repair_audit(
        session,
        action=action,
        actor_user_id=context.principal.user_id,
        event_type=EVENT_REPAIR_PROPOSED,
        result_code="proposed",
    )
    await session.flush()
    return action


async def review_repair(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    action_id: uuid.UUID,
    approve: bool,
) -> SupportRepairAction:
    """The record owner separately approves or declines one exact diff."""

    action = await session.scalar(
        select(SupportRepairAction)
        .options(selectinload(SupportRepairAction.grant).selectinload(SupportAccessGrant.scopes))
        .where(SupportRepairAction.id == action_id)
        .with_for_update()
    )
    if action is None:
        raise RepairNotFound("no such support repair")
    await _require_subject_owner(
        session, user_id=owner_user_id, subject_id=action.subject_id
    )
    if action.status != SupportRepairStatus.PROPOSED.value:
        raise RepairStateError("this repair proposal was already reviewed")
    now = await _now(session)
    if approve:
        grant = action.grant
        await _require_platform_admin(session, user_id=action.proposed_by_user_id)
        if (
            grant.mode != SupportAccessMode.REPAIR.value
            or grant.status != SupportAccessStatus.ACTIVE.value
            or grant.revoked_at is not None
            or now >= _as_utc(grant.expires_at)
            or now >= _as_utc(action.execute_before)
            or not _grant_has_exact_repair_scope(grant)
        ):
            raise RepairStateError("the repair grant is no longer active")
    action.status = (
        SupportRepairStatus.APPROVED.value
        if approve
        else SupportRepairStatus.DECLINED.value
    )
    action.reviewed_by_user_id = owner_user_id
    action.reviewed_at = now
    _repair_audit(
        session,
        action=action,
        actor_user_id=owner_user_id,
        event_type=EVENT_REPAIR_APPROVED if approve else EVENT_REPAIR_DECLINED,
        result_code="approved" if approve else "declined",
    )
    await session.flush()
    return action


async def execute_repair(
    session: AsyncSession,
    *,
    context: AccessContext,
    action_id: uuid.UUID,
) -> SupportRepairAction:
    """Execute once, or durably close the approved proposal as stale."""

    grant, now = await _lock_live_repair_grant(session, context=context)
    action = await session.scalar(
        select(SupportRepairAction)
        .where(
            SupportRepairAction.id == action_id,
            SupportRepairAction.subject_id == context.subject_id,
            SupportRepairAction.support_access_grant_id == grant.id,
            SupportRepairAction.proposed_by_user_id == context.principal.user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if action is None:
        raise RepairNotFound("no such repair under this grant")
    if action.status == SupportRepairStatus.EXECUTED.value:
        return action
    if action.status != SupportRepairStatus.APPROVED.value:
        raise RepairStateError("only an approved repair can execute")
    if now >= _as_utc(action.execute_before):
        raise RepairStateError("the repair approval has expired")

    target_date = await session.scalar(
        select(BodyMeasurement.date).where(
            BodyMeasurement.id == action.target_body_measurement_id,
            BodyMeasurement.subject_id == action.subject_id,
        )
    )
    if target_date is None:
        raise RepairNotFound("the repair target no longer exists")
    prepared = await conflict_engine.prepare_scoped_write(
        session,
        context=conflict_engine.ConflictWriteContext(
            identity=WriteIdentity(
                subject_id=action.subject_id,
                actor_user_id=context.principal.user_id,
            ),
            evaluation_date=target_date,
        ),
    )
    target = await session.scalar(
        select(BodyMeasurement)
        .where(
            BodyMeasurement.id == action.target_body_measurement_id,
            BodyMeasurement.subject_id == action.subject_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if target is None:
        raise RepairNotFound("the repair target no longer exists")
    if (
        target.updated_at != action.target_updated_at_at_proposal
        or target.body_fat_pct != action.before_body_fat_pct
        or target.lbm_kg != action.before_lbm_kg
    ):
        action.status = SupportRepairStatus.STALE.value
        _repair_audit(
            session,
            action=action,
            actor_user_id=context.principal.user_id,
            event_type=EVENT_REPAIR_STALE,
            outcome=AuditOutcome.FAILED,
            result_code="target_changed",
        )
        await session.flush()
        return action

    await conflict_engine.enforce_prepared(
        session,
        prepared=prepared,
        domain=Domain.WEIGHT,
        proposed_state={"measurement": True},
        override=False,
        entity_ref=f"body_measurement:{target.date.isoformat()}",
    )
    target.body_fat_pct = None
    target.lbm_kg = None
    await session.flush()
    await session.refresh(target, attribute_names=["updated_at"])
    action.status = SupportRepairStatus.EXECUTED.value
    action.executed_by_user_id = context.principal.user_id
    action.executed_at = now
    action.target_updated_at_after_execute = target.updated_at
    _repair_audit(
        session,
        action=action,
        actor_user_id=context.principal.user_id,
        event_type=EVENT_REPAIR_EXECUTED,
        result_code="executed",
    )
    await session.flush()
    return action


async def revert_repair(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    action_id: uuid.UUID,
) -> SupportRepairAction:
    """Owner-safe inverse, allowed after the support grant itself has closed."""

    await acquire_identity_governance_lock(session)
    action = await session.scalar(
        select(SupportRepairAction)
        .where(SupportRepairAction.id == action_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if action is None:
        raise RepairNotFound("no such support repair")
    await _require_subject_owner(
        session, user_id=owner_user_id, subject_id=action.subject_id
    )
    if action.status != SupportRepairStatus.EXECUTED.value:
        raise RepairStateError("only an executed repair can be reverted")
    target_date = await session.scalar(
        select(BodyMeasurement.date).where(
            BodyMeasurement.id == action.target_body_measurement_id,
            BodyMeasurement.subject_id == action.subject_id,
        )
    )
    if target_date is None:
        raise RepairNotFound("the repair target no longer exists")
    prepared = await conflict_engine.prepare_scoped_write(
        session,
        context=conflict_engine.ConflictWriteContext(
            identity=WriteIdentity(
                subject_id=action.subject_id, actor_user_id=owner_user_id
            ),
            evaluation_date=target_date,
        ),
    )
    target = await session.scalar(
        select(BodyMeasurement)
        .where(
            BodyMeasurement.id == action.target_body_measurement_id,
            BodyMeasurement.subject_id == action.subject_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if target is None:
        raise RepairNotFound("the repair target no longer exists")
    if (
        target.body_fat_pct is not None
        or target.lbm_kg is not None
        or target.updated_at != action.target_updated_at_after_execute
    ):
        raise RepairStateError("the measurement changed after repair; revert refused")
    await conflict_engine.enforce_prepared(
        session,
        prepared=prepared,
        domain=Domain.WEIGHT,
        proposed_state={"measurement": True},
        override=False,
        entity_ref=f"body_measurement:{target.date.isoformat()}",
    )
    target.body_fat_pct = action.before_body_fat_pct
    target.lbm_kg = action.before_lbm_kg
    now = await _now(session)
    action.status = SupportRepairStatus.REVERTED.value
    action.reverted_by_user_id = owner_user_id
    action.reverted_at = now
    _repair_audit(
        session,
        action=action,
        actor_user_id=owner_user_id,
        event_type=EVENT_REPAIR_REVERTED,
        result_code="reverted",
    )
    await session.flush()
    return action


async def repair_actions_for_subject(
    session: AsyncSession, *, context: AccessContext, limit: int = 50
) -> tuple[RepairActionView, ...]:
    """Bounded protected history for the record owner."""

    if context.subject_owner_user_id != context.principal.user_id:
        raise NotTheSubjectOwner("only the record owner may read repair history")
    now = await _now(session)
    actions = list(
        await session.scalars(
            select(SupportRepairAction)
            .options(
                selectinload(SupportRepairAction.grant),
                selectinload(SupportRepairAction.proposed_by),
                selectinload(SupportRepairAction.target),
            )
            .where(SupportRepairAction.subject_id == context.subject_id)
            .order_by(SupportRepairAction.proposed_at.desc())
            .limit(limit)
        )
    )
    return tuple(_repair_view(action, now=now) for action in actions)


@dataclass(frozen=True, slots=True)
class RecordOpenedEvent:
    """Patient-facing projection of one PHI-free audit envelope and its grant."""

    event_id: uuid.UUID
    grant_id: uuid.UUID
    actor_username: str
    occurred_at: datetime
    scope_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecordOpenedHistory:
    events: tuple[RecordOpenedEvent, ...]
    has_more: bool


async def record_opened_history(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    limit: int = 50,
) -> RecordOpenedHistory:
    """Recent actual support openings for one patient's access centre."""

    if limit < 1 or limit > 100:
        raise ValueError("support read history limit must be between 1 and 100")
    rows = (
        await session.execute(
            select(AuditEvent, User.username)
            .options(
                selectinload(AuditEvent.support_access_grant).selectinload(
                    SupportAccessGrant.scopes
                )
            )
            .join(User, User.id == AuditEvent.actor_user_id)
            .where(
                AuditEvent.subject_id == subject_id,
                AuditEvent.event_type == EVENT_RECORD_OPENED,
                AuditEvent.outcome == AuditOutcome.SUCCESS.value,
                AuditEvent.support_access_grant_id.is_not(None),
            )
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
            .limit(limit + 1)
        )
    ).all()
    events: list[RecordOpenedEvent] = []
    for event, username in rows[:limit]:
        grant = event.support_access_grant
        if event.support_access_grant_id is None or grant is None:
            continue
        events.append(
            RecordOpenedEvent(
                event_id=event.id,
                grant_id=event.support_access_grant_id,
                actor_username=username,
                occurred_at=_as_utc(event.occurred_at),
                scope_keys=tuple(
                    sorted(
                        f"{scope.resource_type}:{scope.resource_key}"
                        for scope in grant.scopes
                        if scope.action == grant.mode
                    )
                ),
            )
        )
    return RecordOpenedHistory(events=tuple(events), has_more=len(rows) > limit)


@dataclass(frozen=True, slots=True)
class ConsoleGrant:
    """One live grant, as the admin's console shows it."""

    grant_id: uuid.UUID
    subject_id: uuid.UUID
    subject_display_name: str
    mode: str
    approved_at: datetime
    expires_at: datetime
    scope_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConsoleRequest:
    """One ask this admin has made and nobody has answered yet."""

    request_id: uuid.UUID
    subject_id: uuid.UUID
    subject_display_name: str
    mode: str
    reason: str
    created_at: datetime
    expires_at: datetime
    scope_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Console:
    """What one admin has open and outstanding, across every record."""

    grants: tuple[ConsoleGrant, ...]
    requests: tuple[ConsoleRequest, ...]


async def console_for_admin(
    session: AsyncSession, *, admin_user_id: uuid.UUID
) -> Console:
    """This admin's live grants and unanswered asks, across every subject.

    Enters the platform scope, and is on the named list in
    ``tests/test_row_level_security.py`` for it. The reason is the same shape as
    the entries already there: an admin's own list spans every record that
    approved one, so there is no single subject to bind — and binding one would
    answer a different question. Both queries name this admin, so what the open
    scope can reach and what it returns are the same rows.

    Returns frozen values rather than ORM objects on purpose. A template holding
    a live row could lazy-load its way to a subject's data from inside a request
    that has the boundary open, which is the one thing this scope must not be
    used to do.
    """

    await _require_platform_admin(session, user_id=admin_user_id)
    from vitals.persistence.rls import enter_platform_scope

    await enter_platform_scope(session)
    now = await _now(session)

    grant_rows = (
        await session.execute(
            select(SupportAccessGrant, HealthSubject.display_name)
            .options(selectinload(SupportAccessGrant.scopes))
            .join(HealthSubject, HealthSubject.id == SupportAccessGrant.subject_id)
            .where(
                SupportAccessGrant.granted_to_user_id == admin_user_id,
                SupportAccessGrant.status == SupportAccessStatus.ACTIVE.value,
                SupportAccessGrant.expires_at > now,
            )
            .order_by(SupportAccessGrant.expires_at)
        )
    ).all()

    request_rows = (
        await session.execute(
            select(SupportAccessRequest, HealthSubject.display_name)
            .options(selectinload(SupportAccessRequest.scopes))
            .join(HealthSubject, HealthSubject.id == SupportAccessRequest.subject_id)
            .where(
                SupportAccessRequest.requested_by_user_id == admin_user_id,
                SupportAccessRequest.status == _LIVE_REQUEST,
                SupportAccessRequest.expires_at > now,
            )
            .order_by(SupportAccessRequest.created_at.desc())
        )
    ).all()

    return Console(
        grants=tuple(
            ConsoleGrant(
                grant_id=grant.id,
                subject_id=grant.subject_id,
                subject_display_name=display_name,
                mode=grant.mode,
                approved_at=_as_utc(grant.approved_at),
                expires_at=_as_utc(grant.expires_at),
                scope_keys=tuple(
                    sorted(
                        f"{scope.resource_type}:{scope.resource_key}"
                        for scope in grant.scopes
                        if scope.action == grant.mode
                    )
                ),
            )
            for grant, display_name in grant_rows
        ),
        requests=tuple(
            ConsoleRequest(
                request_id=request.id,
                subject_id=request.subject_id,
                subject_display_name=display_name,
                mode=request.mode,
                reason=request.reason,
                created_at=_as_utc(request.created_at),
                expires_at=_as_utc(request.expires_at),
                scope_keys=tuple(
                    sorted(
                        f"{scope.resource_type}:{scope.resource_key}"
                        for scope in request.scopes
                        if scope.action == request.mode
                    )
                ),
            )
            for request, display_name in request_rows
        ),
    )


async def reachable_subjects(
    session: AsyncSession, *, admin_user_id: uuid.UUID
) -> tuple[tuple[uuid.UUID, str], ...]:
    """Every record an ask could name, as ``(id, display name)``.

    Deliberately not a search over people. It is the same list
    ``/settings/platform/ai`` already shows an administrator, for the same
    reason: choosing whose record to investigate has to be a choice from a list
    somebody can audit, not a free-text field that finds a patient by name.
    """

    await _require_platform_admin(session, user_id=admin_user_id)
    from vitals.persistence.rls import enter_platform_scope

    await enter_platform_scope(session)
    rows = (
        await session.execute(
            select(HealthSubject.id, HealthSubject.display_name)
            .where(HealthSubject.owner_user_id != admin_user_id)
            .order_by(HealthSubject.display_name)
        )
    ).all()
    return tuple((row[0], row[1]) for row in rows)


async def expire_stale(session: AsyncSession) -> tuple[int, int]:
    """Mark lapsed asks and lapsed grants for what they are. Never commits.

    Returns ``(requests, grants)`` closed. Expiry is already enforced at every
    read — :func:`load_support_grant` compares the clock and the policy compares
    it again — so this changes no authorization. What it changes is what the
    screens say: a grant that ran out three days ago still reading "active" in a
    patient's access history is the list telling them something untrue about who
    can see their record.
    """

    now = await _now(session)

    stale_requests = (
        await session.execute(
            select(SupportAccessRequest)
            .where(
                SupportAccessRequest.status == _LIVE_REQUEST,
                SupportAccessRequest.expires_at <= now,
            )
            .with_for_update()
        )
    ).scalars().all()
    for request in stale_requests:
        request.status = SupportAccessRequestStatus.EXPIRED.value
        request.decided_at = now
        # Nobody decided it; the clock did. The column is not nullable for a
        # decided row, so the requester stands as the named party — the history
        # reads "the ask this admin made lapsed", which is what happened.
        request.decided_by_user_id = request.requested_by_user_id
        _audit(
            session,
            event_type=EVENT_EXPIRED,
            actor_user_id=request.requested_by_user_id,
            subject_id=request.subject_id,
            grant_id=None,
            resource_id=request.id,
            reason_code="support_request_lapsed",
        )

    stale_grants = (
        await session.execute(
            select(SupportAccessGrant)
            .where(
                SupportAccessGrant.status == SupportAccessStatus.ACTIVE.value,
                SupportAccessGrant.expires_at <= now,
            )
            .with_for_update()
        )
    ).scalars().all()
    for grant in stale_grants:
        # Not ``revoked``: nobody took it away, it ran out. The revocation
        # columns stay null, which the schema's revocation-state constraint
        # requires for any status that is not ``revoked``.
        grant.status = SupportAccessStatus.EXPIRED.value
        _audit(
            session,
            event_type=EVENT_EXPIRED,
            actor_user_id=grant.granted_to_user_id,
            subject_id=grant.subject_id,
            grant_id=grant.id,
            resource_id=grant.id,
            reason_code="support_grant_lapsed",
        )

    await session.flush()
    return len(stale_requests), len(stale_grants)


__all__ = [
    "AmbiguousSupportGrant",
    "DEFAULT_GRANT_TTL",
    "EVENT_RECORD_EXPORTED",
    "EVENT_REPAIR_APPROVED",
    "EVENT_REPAIR_DECLINED",
    "EVENT_REPAIR_EXECUTED",
    "EVENT_REPAIR_PROPOSED",
    "EVENT_REPAIR_REVERTED",
    "EVENT_REPAIR_STALE",
    "EXPORT_OPERATION_KEY",
    "REPAIR_OPERATION_KEY",
    "GrantNotFound",
    "MAX_GRANT_TTL",
    "NotAPlatformAdmin",
    "NotTheSubjectOwner",
    "REQUEST_WINDOW",
    "RequestNotFound",
    "RequestNotPending",
    "RepairNotFound",
    "RepairStateError",
    "RequestedScope",
    "ScopesRequired",
    "SupportAccessError",
    "UnsupportedMode",
    "Console",
    "ConsoleGrant",
    "ConsoleRequest",
    "PatientAccessRequest",
    "PatientAccessRequestHistory",
    "PatientLiveGrant",
    "approve_request",
    "consume_subject_export",
    "console_for_admin",
    "decline_request",
    "expire_stale",
    "export_scope",
    "execute_repair",
    "list_for_subject",
    "live_grants_for",
    "load_support_grant",
    "open_request",
    "propose_clear_derived_estimates",
    "read_scopes_for",
    "repair_actions_for_subject",
    "repair_scope",
    "repair_workspace",
    "review_repair",
    "revert_repair",
    "reachable_subjects",
    "revoke_grant",
    "withdraw_request",
]
