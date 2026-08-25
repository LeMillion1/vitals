"""Redacted operator view of account invitations and the registration door."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import RegistrationInvitationStatus
from vitals.models.registration import RegistrationInvitation
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
) -> RegistrationConsole:
    """Return live invites without exposing raw addresses or token digests."""

    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= MAX_CONSOLE_PAGE:
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
    )


__all__ = [
    "InvitationConsoleEntry",
    "RegistrationConsole",
    "CONSOLE_PAGE_SIZE",
    "MAX_CONSOLE_PAGE",
    "registration_console",
]
