"""Password credential validation, rotation, and retirement."""
from __future__ import annotations

import hmac
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import AuditOutcome
from vitals.models.identity import AuditEvent, User
from vitals.services.identity.contracts import (
    IdentityStateConflictError,
    IdentityValidationError,
    PasswordHashDowngradeError,
    PasswordHashMismatchError,
    UserNotFoundError,
)
from vitals.services.identity.governance import acquire_identity_governance_lock

_BCRYPT_RE = re.compile(r"^\$2[aby]\$(?P<cost>\d{2})\$[./A-Za-z0-9]{53}$")
_MIN_BCRYPT_COST = 4
_MAX_BCRYPT_COST = 31


def bcrypt_cost(password_hash: str) -> int:
    """Validate a bcrypt hash envelope and return its work factor."""

    if not isinstance(password_hash, str):
        raise IdentityValidationError("password hash must be a string")
    match = _BCRYPT_RE.fullmatch(password_hash)
    if match is None:
        raise IdentityValidationError("password hash must be a complete bcrypt hash")
    cost = int(match.group("cost"))
    if not _MIN_BCRYPT_COST <= cost <= _MAX_BCRYPT_COST:
        raise IdentityValidationError("bcrypt cost is outside the supported range")
    return cost


async def _user_for_update(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise UserNotFoundError(f"user {user_id} does not exist")
    return user


def _add_audit_event(
    session: AsyncSession,
    *,
    actor_user_id: uuid.UUID | None,
    user_id: uuid.UUID,
    event_type: str,
    result_code: str,
    changed_fields: list[str],
) -> None:
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            event_type=event_type,
            outcome=AuditOutcome.SUCCESS.value,
            resource_type="user",
            resource_id=str(user_id),
            metadata_json={
                "source_surface": "identity_service",
                "result_code": result_code,
                "changed_fields": changed_fields,
            },
        )
    )


async def rotate_password_hash(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    expected_current_hash: str,
    new_hash: str,
    actor_user_id: uuid.UUID | None,
) -> User:
    """Replace a bcrypt hash with compare-and-swap and revoke old sessions."""

    new_cost = bcrypt_cost(new_hash)
    await acquire_identity_governance_lock(session)
    user = await _user_for_update(session, user_id)
    if not isinstance(expected_current_hash, str) or not hmac.compare_digest(
        user.password_hash, expected_current_hash
    ):
        raise PasswordHashMismatchError("stored password hash changed concurrently")
    try:
        current_cost = bcrypt_cost(user.password_hash)
    except IdentityValidationError as exc:
        raise IdentityStateConflictError(
            "stored password hash is not a valid bcrypt hash"
        ) from exc
    if new_cost < current_cost:
        raise PasswordHashDowngradeError(
            "new bcrypt work factor is lower than the stored work factor"
        )
    if hmac.compare_digest(user.password_hash, new_hash):
        return user
    user.password_hash = new_hash
    user.session_version += 1
    _add_audit_event(
        session,
        actor_user_id=actor_user_id,
        user_id=user.id,
        event_type="identity.password.rotated",
        result_code="password_hash_rotated",
        changed_fields=["password_hash", "session_version"],
    )
    await session.flush()
    return user


async def retire_password_hash(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    expected_current_hash: str,
    actor_user_id: uuid.UUID | None,
    allow_already_retired: bool = False,
) -> User:
    """Remove the last local verifier after federated recovery is proven."""

    await acquire_identity_governance_lock(session)
    user = await _user_for_update(session, user_id)
    if user.password_hash is None:
        if not allow_already_retired:
            raise PasswordHashMismatchError("stored password hash is already absent")
        retirement_events = await session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "identity.password.retired",
                AuditEvent.resource_type == "user",
                AuditEvent.resource_id == str(user.id),
            )
        )
        if not any(
            isinstance(event.metadata_json, dict)
            and event.metadata_json.get("result_code")
            == "password_hash_removed_after_oidc_recovery"
            for event in retirement_events
        ):
            raise IdentityStateConflictError(
                "stored password hash is absent without retirement audit evidence"
            )
        return user
    if not isinstance(expected_current_hash, str) or not hmac.compare_digest(
        user.password_hash, expected_current_hash
    ):
        raise PasswordHashMismatchError("stored password hash changed concurrently")
    bcrypt_cost(user.password_hash)
    user.password_hash = None
    user.session_version += 1
    _add_audit_event(
        session,
        actor_user_id=actor_user_id,
        user_id=user.id,
        event_type="identity.password.retired",
        result_code="password_hash_removed_after_oidc_recovery",
        changed_fields=["password_hash", "session_version"],
    )
    await session.flush()
    return user


__all__ = ["bcrypt_cost", "retire_password_hash", "rotate_password_hash"]
