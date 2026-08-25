"""Redacted operator view of account invitations and the registration door."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import RegistrationInvitationStatus, RegistrationRequestStatus
from vitals.models.registration import RegistrationInvitation, RegistrationRequest
from vitals.services.authentication.admission._shared import (
    AdmissionValidationError,
    as_utc,
    database_now,
    require_operator,
)
from vitals.services.authentication.registration import (
    RegistrationMode,
    deployment_is_unlocked,
    effective_mode,
    get_stored_mode,
)
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.utils.timeutils import to_local_naive

CONSOLE_PAGE_SIZE = 100
MAX_CONSOLE_PAGE = 10_000


@dataclass(frozen=True, slots=True)
class InvitationConsoleEntry:
    """The non-secret part of a live invitation."""

    invitation_id: uuid.UUID
    reference: str
    masked_email: str
    account_kind: str
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RequestConsoleEntry:
    """The bounded identity summary an operator needs for one decision."""

    request_id: uuid.UUID
    reference: str
    masked_email: str
    account_kind: str
    provider_current: bool
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RegistrationConsole:
    """A bounded view of registration policy and revocable invitations."""

    stored_mode: RegistrationMode
    effective_mode: RegistrationMode
    deployment_unlocked: bool
    invitations: tuple[InvitationConsoleEntry, ...]
    total_live_invitations: int
    page: int
    page_count: int
    has_previous: bool
    has_next: bool
    requests: tuple[RequestConsoleEntry, ...]
    total_live_requests: int
    request_page: int
    request_page_count: int
    request_has_previous: bool
    request_has_next: bool


def _masked_email(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"


async def registration_console(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    page: int = 1,
    request_page: int = 1,
    current_oidc_issuer: str | None = None,
) -> RegistrationConsole:
    """Return live admission work without exposing provider identity keys."""

    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or not 1 <= page <= MAX_CONSOLE_PAGE
        or isinstance(request_page, bool)
        or not isinstance(request_page, int)
        or not 1 <= request_page <= MAX_CONSOLE_PAGE
    ):
        raise AdmissionValidationError("registration console page is out of range")
    await acquire_identity_governance_lock(session)
    await require_operator(session, actor_user_id=actor_user_id)
    stored = await get_stored_mode(session)
    effective = await effective_mode(session)
    now = await database_now(session)
    live_filter = (
        RegistrationInvitation.status
        == RegistrationInvitationStatus.PENDING.value,
        RegistrationInvitation.expires_at > now,
    )
    total = int(
        await session.scalar(
            select(func.count()).select_from(RegistrationInvitation).where(*live_filter)
        )
        or 0
    )
    page_count = max(1, (total + CONSOLE_PAGE_SIZE - 1) // CONSOLE_PAGE_SIZE)
    if page > page_count:
        raise AdmissionValidationError("registration console page is out of range")
    rows = tuple(
        await session.scalars(
            select(RegistrationInvitation)
            .where(*live_filter)
            .order_by(
                RegistrationInvitation.expires_at,
                RegistrationInvitation.created_at,
                RegistrationInvitation.id,
            )
            .offset((page - 1) * CONSOLE_PAGE_SIZE)
            .limit(CONSOLE_PAGE_SIZE)
        )
    )
    request_filter = (
        RegistrationRequest.status == RegistrationRequestStatus.PENDING.value,
        RegistrationRequest.expires_at > now,
    )
    request_total = int(
        await session.scalar(
            select(func.count()).select_from(RegistrationRequest).where(*request_filter)
        )
        or 0
    )
    request_page_count = max(
        1,
        (request_total + CONSOLE_PAGE_SIZE - 1) // CONSOLE_PAGE_SIZE,
    )
    if request_page > request_page_count:
        raise AdmissionValidationError("registration console page is out of range")
    request_rows = tuple(
        await session.scalars(
            select(RegistrationRequest)
            .where(*request_filter)
            .order_by(
                RegistrationRequest.expires_at,
                RegistrationRequest.created_at,
                RegistrationRequest.id,
            )
            .offset((request_page - 1) * CONSOLE_PAGE_SIZE)
            .limit(CONSOLE_PAGE_SIZE)
        )
    )
    return RegistrationConsole(
        stored_mode=stored,
        effective_mode=effective,
        deployment_unlocked=deployment_is_unlocked(),
        invitations=tuple(
            InvitationConsoleEntry(
                invitation_id=row.id,
                reference=str(row.id),
                masked_email=_masked_email(row.normalized_email or ""),
                account_kind=row.account_kind,
                expires_at=to_local_naive(as_utc(row.expires_at)),
                created_at=to_local_naive(as_utc(row.created_at)),
            )
            for row in rows
        ),
        total_live_invitations=total,
        page=page,
        page_count=page_count,
        has_previous=page > 1,
        has_next=page < page_count,
        requests=tuple(
            RequestConsoleEntry(
                request_id=row.id,
                reference=str(row.id),
                masked_email=_masked_email(row.normalized_verified_email or ""),
                account_kind=row.account_kind,
                provider_current=(
                    isinstance(current_oidc_issuer, str)
                    and isinstance(row.issuer, str)
                    and row.issuer == current_oidc_issuer
                ),
                expires_at=to_local_naive(as_utc(row.expires_at)),
                created_at=to_local_naive(as_utc(row.created_at)),
            )
            for row in request_rows
        ),
        total_live_requests=request_total,
        request_page=request_page,
        request_page_count=request_page_count,
        request_has_previous=request_page > 1,
        request_has_next=request_page < request_page_count,
    )


__all__ = [
    "InvitationConsoleEntry",
    "RequestConsoleEntry",
    "RegistrationConsole",
    "CONSOLE_PAGE_SIZE",
    "MAX_CONSOLE_PAGE",
    "registration_console",
]
