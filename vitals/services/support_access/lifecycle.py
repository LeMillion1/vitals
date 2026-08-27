"""Patient-approved support request and grant lifecycle."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vitals.access import AccessContext, AccessScope, PolicyAction, PolicyResourceType, SupportGrant
from vitals.enums import (
    SupportAccessMode,
    SupportAccessRequestStatus,
    SupportAccessStatus,
    SupportScopeResourceType,
)
from vitals.models.identity import (
    HealthSubject,
    SupportAccessGrant,
    SupportAccessRequest,
    SupportAccessRequestScope,
    SupportAccessScope,
    User,
)
from vitals.services.support_access.contracts import (
    DEFAULT_GRANT_TTL,
    EVENT_APPROVED,
    EVENT_DECLINED,
    EVENT_REQUESTED,
    EVENT_REVOKED,
    EVENT_WITHDRAWN,
    EXPORT_OPERATION_KEY,
    MAX_GRANT_TTL,
    REQUEST_WINDOW,
    AmbiguousSupportGrant,
    GrantNotFound,
    NotTheSubjectOwner,
    RequestNotFound,
    RequestNotPending,
    RequestedScope,
    ScopesRequired,
    SupportAccessError,
    UnsupportedMode,
    _LIVE_REQUEST,
    _as_utc,
    _audit,
    _exact_repair_scope_rows,
    _now,
    _require_platform_admin,
    _require_subject_owner,
    export_scope,
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
