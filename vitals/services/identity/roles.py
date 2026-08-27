"""Role assignment and user lifecycle governance."""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import AuditOutcome, UserRoleName, UserStatus
from vitals.models.identity import AuditEvent, User, UserRole
from vitals.services.identity.contracts import (
    IdentityValidationError,
    LastActivePlatformSuperadminError,
    UserNotFoundError,
)
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.services.identity.queries import has_active_platform_superadmin


async def _user_for_update(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise UserNotFoundError(f"user {user_id} does not exist")
    return user


async def _role_for_update(
    session: AsyncSession, *, user_id: uuid.UUID, role: UserRoleName
) -> Optional[UserRole]:
    return await session.scalar(
        select(UserRole)
        .where(UserRole.user_id == user_id, UserRole.role == role.value)
        .with_for_update()
    )


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


def _as_role(role: UserRoleName | str) -> UserRoleName:
    try:
        return UserRoleName(role)
    except (TypeError, ValueError) as exc:
        raise IdentityValidationError(f"unknown user role: {role!r}") from exc


def _as_status(status: UserStatus | str) -> UserStatus:
    try:
        return UserStatus(status)
    except (TypeError, ValueError) as exc:
        raise IdentityValidationError(f"unknown user status: {status!r}") from exc


async def assign_role(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    role: UserRoleName | str,
    assigned_by_user_id: uuid.UUID | None,
) -> UserRole:
    """Idempotently assign a capability role without granting subject access."""

    role_name = _as_role(role)
    await acquire_identity_governance_lock(session)
    await _user_for_update(session, user_id)
    existing = await _role_for_update(session, user_id=user_id, role=role_name)
    if existing is not None:
        return existing
    assignment = UserRole(
        user_id=user_id,
        role=role_name.value,
        assigned_by_user_id=assigned_by_user_id,
    )
    session.add(assignment)
    _add_audit_event(
        session,
        actor_user_id=assigned_by_user_id,
        user_id=user_id,
        event_type="identity.role.assigned",
        result_code=f"{role_name.value}_assigned",
        changed_fields=["roles"],
    )
    await session.flush()
    return assignment


async def revoke_role(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    role: UserRoleName | str,
    actor_user_id: uuid.UUID | None,
) -> bool:
    """Revoke a role, rejecting removal of the last active superadmin."""

    role_name = _as_role(role)
    await acquire_identity_governance_lock(session)
    user = await _user_for_update(session, user_id)
    assignment = await _role_for_update(session, user_id=user_id, role=role_name)
    if assignment is None:
        return False
    if (
        role_name is UserRoleName.PLATFORM_SUPERADMIN
        and user.status == UserStatus.ACTIVE.value
        and not await has_active_platform_superadmin(session, exclude_user_id=user.id)
    ):
        raise LastActivePlatformSuperadminError(
            "cannot revoke the last active platform_superadmin role"
        )
    await session.delete(assignment)
    _add_audit_event(
        session,
        actor_user_id=actor_user_id,
        user_id=user_id,
        event_type="identity.role.revoked",
        result_code=f"{role_name.value}_revoked",
        changed_fields=["roles"],
    )
    await session.flush()
    return True


async def change_user_status(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    new_status: UserStatus | str,
    actor_user_id: uuid.UUID | None,
) -> User:
    """Change lifecycle status while preserving one active platform superadmin."""

    status = _as_status(new_status)
    await acquire_identity_governance_lock(session)
    user = await _user_for_update(session, user_id)
    if user.status == status.value:
        return user
    superadmin_role = await _role_for_update(
        session, user_id=user.id, role=UserRoleName.PLATFORM_SUPERADMIN
    )
    if (
        user.status == UserStatus.ACTIVE.value
        and status is not UserStatus.ACTIVE
        and superadmin_role is not None
        and not await has_active_platform_superadmin(session, exclude_user_id=user.id)
    ):
        raise LastActivePlatformSuperadminError(
            "cannot deactivate the last active platform_superadmin"
        )
    previous_status = user.status
    user.status = status.value
    user.session_version += 1
    if status is not UserStatus.ACTIVE:
        from vitals.services.notifications import web_push_subscriptions

        await web_push_subscriptions.revoke_all(session, user_id=user.id)
    _add_audit_event(
        session,
        actor_user_id=actor_user_id,
        user_id=user.id,
        event_type="identity.user.status_changed",
        result_code=f"{previous_status}_to_{status.value}",
        changed_fields=["status", "session_version"],
    )
    await session.flush()
    return user


__all__ = ["assign_role", "change_user_status", "revoke_role"]
