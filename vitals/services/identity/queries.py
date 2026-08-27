"""Read-only identity and ownership queries."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserRoleName, UserStatus
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.services.identity.normalization import normalize_username


async def find_user_id_by_username(
    session: AsyncSession, *, username: str
) -> uuid.UUID | None:
    lookup = normalize_username(username).lookup_key
    return await session.scalar(select(User.id).where(User.normalized_username == lookup))


async def is_active_username(session: AsyncSession, *, username: str) -> bool:
    lookup = normalize_username(username).lookup_key
    return (
        await session.scalar(
            select(User.id)
            .where(
                User.normalized_username == lookup,
                User.status == UserStatus.ACTIVE.value,
            )
            .limit(1)
        )
    ) is not None


async def sole_active_subject_owner_username(session: AsyncSession) -> str | None:
    records = tuple(
        (
            await session.execute(
                select(User.username, User.status)
                .select_from(HealthSubject)
                .join(User, User.id == HealthSubject.owner_user_id)
                .limit(2)
            )
        ).all()
    )
    if len(records) != 1 or records[0].status != UserStatus.ACTIVE.value:
        return None
    return records[0].username


async def installation_has_multiple_subjects(session: AsyncSession) -> bool:
    subject_ids = tuple(await session.scalars(select(HealthSubject.id).limit(2)))
    return len(subject_ids) > 1


async def owned_subject_id(
    session: AsyncSession, *, user_id: uuid.UUID
) -> uuid.UUID | None:
    return await session.scalar(
        select(HealthSubject.id).where(HealthSubject.owner_user_id == user_id)
    )


async def user_has_role(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    roles: tuple[UserRoleName | str, ...],
) -> bool:
    values = tuple(role.value if isinstance(role, UserRoleName) else role for role in roles)
    return (
        await session.scalar(
            select(UserRole.id)
            .where(UserRole.user_id == user_id, UserRole.role.in_(values))
            .limit(1)
        )
    ) is not None


async def has_active_platform_superadmin(
    session: AsyncSession, *, exclude_user_id: uuid.UUID | None = None
) -> bool:
    query = (
        select(User.id)
        .join(UserRole, UserRole.user_id == User.id)
        .where(
            User.status == UserStatus.ACTIVE.value,
            UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
        )
        .limit(1)
    )
    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)
    return await session.scalar(query) is not None


__all__ = [
    "find_user_id_by_username",
    "has_active_platform_superadmin",
    "installation_has_multiple_subjects",
    "is_active_username",
    "owned_subject_id",
    "sole_active_subject_owner_username",
    "user_has_role",
]
