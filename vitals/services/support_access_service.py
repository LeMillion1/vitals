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

**Read only, for now.** ``repair`` and ``export`` exist in the vocabulary and
are refused here by name. The roadmap sequences them that way on purpose — a
repair needs a bounded diff and a second review, an export needs its own
approval — and a mode that is accepted but unimplemented is worse than one that
says so: it would look approved to the patient and do nothing, or worse, do
something nobody designed.

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


class RequestNotPending(SupportAccessError):
    """The request has already been answered, withdrawn, or has lapsed."""


class UnsupportedMode(SupportAccessError):
    """``repair`` and ``export`` are not implemented and are not pretended to be."""


class ScopesRequired(SupportAccessError):
    """A grant with no scopes authorizes nothing, so an ask with none is refused."""


class NotASupportSession(SupportAccessError):
    """A caller tried to record support use without a matching support grant."""


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

    if mode is not SupportAccessMode.READ:
        raise UnsupportedMode(
            f"support mode {mode.value!r} is not implemented: a repair needs a "
            "bounded diff and a second review, an export needs its own approval, "
            "and accepting the word without the work would look approved to the "
            "patient and do something nobody designed"
        )
    if not scopes:
        raise ScopesRequired(
            "a grant with no scopes authorizes nothing, so an ask with none is "
            "a question with no answer"
        )
    if any(scope.action is not SupportAccessMode.READ for scope in scopes):
        raise UnsupportedMode("a read request may only ask for read scopes")
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
) -> SupportGrant | None:
    """Assemble the snapshot the policy decides one support request from.

    Returns ``None`` whenever anything is missing, which is not a refusal: the
    policy is deny-by-default and a missing grant leaves it that way. Everything
    returned here is re-checked in :func:`vitals.access.is_allowed` — the
    grantee, the status, the expiry, the mode ceiling and the exact scope — so
    this being permissive by mistake still cannot authorize anything on its own.
    """

    grant = await session.scalar(
        select(SupportAccessGrant)
        .options(selectinload(SupportAccessGrant.scopes))
        .where(
            SupportAccessGrant.subject_id == subject_id,
            SupportAccessGrant.granted_to_user_id == admin_user_id,
            SupportAccessGrant.status == SupportAccessStatus.ACTIVE.value,
            SupportAccessGrant.expires_at > evaluated_at,
        )
        .order_by(SupportAccessGrant.expires_at.desc())
        .limit(1)
    )
    if grant is None:
        return None
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
                    if scope.action == SupportAccessMode.READ.value
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
                        if scope.action == SupportAccessMode.READ.value
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
                        if scope.action == SupportAccessMode.READ.value
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
                        if scope.action == SupportAccessMode.READ.value
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
                    sorted(scope.resource_key for scope in request.scopes)
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
    "DEFAULT_GRANT_TTL",
    "GrantNotFound",
    "MAX_GRANT_TTL",
    "NotAPlatformAdmin",
    "NotTheSubjectOwner",
    "REQUEST_WINDOW",
    "RequestNotFound",
    "RequestNotPending",
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
    "console_for_admin",
    "decline_request",
    "expire_stale",
    "list_for_subject",
    "live_grants_for",
    "load_support_grant",
    "open_request",
    "read_scopes_for",
    "reachable_subjects",
    "revoke_grant",
    "withdraw_request",
]
