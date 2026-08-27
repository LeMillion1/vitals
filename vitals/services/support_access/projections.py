"""Patient history and platform-admin console projections."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vitals.enums import AuditOutcome, SupportAccessStatus
from vitals.models.identity import (
    AuditEvent,
    HealthSubject,
    SupportAccessGrant,
    SupportAccessRequest,
    User,
)
from vitals.services.support_access.contracts import (
    EVENT_RECORD_OPENED,
    _LIVE_REQUEST,
    _as_utc,
    _now,
    _require_platform_admin,
)


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
    mode: str
    reason: str
    created_at: datetime
    expires_at: datetime
    scope_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Console:
    """What one admin has open and outstanding for one selected record."""

    grants: tuple[ConsoleGrant, ...]
    requests: tuple[ConsoleRequest, ...]


async def console_for_admin(
    session: AsyncSession,
    *,
    admin_user_id: uuid.UUID,
    subject_id: uuid.UUID | None,
) -> Console:
    """This admin's live grants and unanswered asks for one exact record.

    The root console passes ``None`` only to prove the live platform role before
    a record code is entered. Once a code is present the router binds that exact
    subject before calling. A pending request deliberately carries no patient
    name; approval is what permits the active-grant projection to reveal it.
    """

    await _require_platform_admin(session, user_id=admin_user_id)
    if subject_id is None:
        return Console(grants=(), requests=())
    now = await _now(session)

    grant_rows = (
        await session.execute(
            select(SupportAccessGrant, HealthSubject.display_name)
            .options(selectinload(SupportAccessGrant.scopes))
            .join(HealthSubject, HealthSubject.id == SupportAccessGrant.subject_id)
            .where(
                SupportAccessGrant.granted_to_user_id == admin_user_id,
                SupportAccessGrant.subject_id == subject_id,
                SupportAccessGrant.status == SupportAccessStatus.ACTIVE.value,
                SupportAccessGrant.expires_at > now,
            )
            .order_by(SupportAccessGrant.expires_at)
        )
    ).all()

    request_rows = (
        await session.execute(
            select(SupportAccessRequest)
            .options(selectinload(SupportAccessRequest.scopes))
            .where(
                SupportAccessRequest.requested_by_user_id == admin_user_id,
                SupportAccessRequest.subject_id == subject_id,
                SupportAccessRequest.status == _LIVE_REQUEST,
                SupportAccessRequest.expires_at > now,
            )
            .order_by(SupportAccessRequest.created_at.desc())
        )
    ).scalars().all()

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
            for request in request_rows
        ),
    )
