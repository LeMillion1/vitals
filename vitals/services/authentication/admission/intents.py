"""PII-free, one-time state for an open-registration account-kind choice.

The browser carries only a separately signed opaque UUID. This module owns the
authoritative account kind and lifecycle, but deliberately does not provision
or link an identity. Callers own the surrounding transaction and therefore the
atomic ordering between intent consumption and later federation work.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import RegistrationAccountKind, RegistrationIntentStatus
from vitals.models.registration import RegistrationIntent
from vitals.services.authentication.admission._shared import (
    AdmissionRefused,
    account_kind as resolve_account_kind,
    as_utc,
    audit,
    bounded_ttl,
    database_now,
    require_mode,
)
from vitals.services.authentication.registration import RegistrationMode
from vitals.services.identity.governance import acquire_identity_governance_lock

INTENT_TTL = timedelta(minutes=15)
MAX_INTENT_TTL = INTENT_TTL
_REFUSAL = "this admission proof does not open an account"


def _presented_intent_id(value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise AdmissionRefused(_REFUSAL)
    return value


def _expire(row: RegistrationIntent, *, now: datetime) -> None:
    row.status = RegistrationIntentStatus.EXPIRED.value
    row.expired_at = max(now, as_utc(row.expires_at))


async def issue_intent(
    session: AsyncSession,
    *,
    account_kind: RegistrationAccountKind | str,
    ttl: timedelta = INTENT_TTL,
) -> RegistrationIntent:
    """Persist one short-lived account choice while open mode is effective."""

    ttl = bounded_ttl(ttl, name="ttl", maximum=MAX_INTENT_TTL)
    kind = resolve_account_kind(account_kind)

    await acquire_identity_governance_lock(session)
    await require_mode(session, RegistrationMode.OPEN)
    now = await database_now(session)
    intent = RegistrationIntent(
        account_kind=kind.value,
        status=RegistrationIntentStatus.PENDING.value,
        expires_at=now + ttl,
        # Keep the whole short-lived window on the same post-lock database
        # statement clock. A server default may be PostgreSQL transaction time
        # and therefore precede a governance-lock wait by an unbounded amount.
        created_at=now,
        updated_at=now,
    )
    session.add(intent)
    await session.flush()
    audit(
        session,
        event_type="registration.intent.issued",
        resource_type="registration_intent",
        resource_id=intent.id,
        result_code="issued",
        changed_fields=("status", "account_kind", "expires_at"),
    )
    await session.flush()
    return intent


async def _lock_intent(
    session: AsyncSession,
    *,
    intent_id: uuid.UUID,
) -> RegistrationIntent:
    opaque_id = _presented_intent_id(intent_id)
    await acquire_identity_governance_lock(session)
    await require_mode(session, RegistrationMode.OPEN)
    row = await session.scalar(
        select(RegistrationIntent)
        .where(RegistrationIntent.id == opaque_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.status != RegistrationIntentStatus.PENDING.value:
        raise AdmissionRefused(_REFUSAL)

    # A malformed persisted kind must fail closed even if a schema constraint
    # was absent in an old or manually modified environment.
    try:
        resolve_account_kind(row.account_kind)
    except ValueError as exc:
        raise AdmissionRefused(_REFUSAL) from exc

    now = await database_now(session)
    if now >= as_utc(row.expires_at):
        _expire(row, now=now)
        audit(
            session,
            event_type="registration.intent.expired",
            resource_type="registration_intent",
            resource_id=row.id,
            result_code="expired_on_lock",
            changed_fields=("status",),
        )
        await session.flush()
        raise AdmissionRefused(_REFUSAL)
    return row


async def lock_intent(
    session: AsyncSession,
    *,
    intent_id: uuid.UUID,
) -> RegistrationIntent:
    """Lock and return one current intent without consuming it.

    The governance and row locks remain owned by the caller's transaction.
    """

    return await _lock_intent(session, intent_id=intent_id)


async def consume_intent(
    session: AsyncSession,
    *,
    intent_id: uuid.UUID,
) -> RegistrationIntent:
    """Consume one current intent exactly once without provisioning an account."""

    row = await _lock_intent(session, intent_id=intent_id)
    now = await database_now(session)
    if now >= as_utc(row.expires_at):
        _expire(row, now=now)
        audit(
            session,
            event_type="registration.intent.expired",
            resource_type="registration_intent",
            resource_id=row.id,
            result_code="expired_on_consume",
            changed_fields=("status",),
        )
        await session.flush()
        raise AdmissionRefused(_REFUSAL)

    row.status = RegistrationIntentStatus.CONSUMED.value
    row.consumed_at = now
    audit(
        session,
        event_type="registration.intent.consumed",
        resource_type="registration_intent",
        resource_id=row.id,
        result_code="consumed",
        changed_fields=("status",),
    )
    await session.flush()
    return row


__all__ = [
    "INTENT_TTL",
    "MAX_INTENT_TTL",
    "consume_intent",
    "issue_intent",
    "lock_intent",
]
