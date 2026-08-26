"""Provision and retire recordless installation-control identities.

The public account provisioner must never mint platform authority.  This module
is the deliberately narrower host-operator boundary used only after the first
OIDC owner binding exists.  It creates one active locked-password account with
exactly the platform role, no health subject, and an exact provider identity in
one transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserRoleName, UserStatus
from vitals.models.identity import (
    HealthSubject,
    User,
    UserFederatedIdentity,
    UserRole,
)
from vitals.services import identity_service, platform_admin_service
from vitals.services.authentication import federation, provisioning


class PlatformOperatorError(RuntimeError):
    """A platform-operator lifecycle transition was refused."""


class PlatformOperatorAlreadyExists(PlatformOperatorError):
    """The requested local or provider identity is already represented."""


class PlatformOperatorBootstrapIncomplete(PlatformOperatorError):
    """The installation owner has not completed the first OIDC binding."""


class PlatformOperatorTargetNotFound(PlatformOperatorError):
    """The requested local target does not exist."""


class PlatformOperatorLoginUnproven(PlatformOperatorError):
    """The replacement operator has not proved its provider login yet."""


class PlatformOperatorShapeError(PlatformOperatorError):
    """The actor or target is not the narrow authority-transfer shape."""


@dataclass(frozen=True, slots=True)
class ProvisionedPlatformOperator:
    user_id: uuid.UUID
    identity_id: uuid.UUID


def _exact_provider_value(*, name: str, value: str) -> str:
    if not isinstance(value, str):
        raise PlatformOperatorError(f"{name} must be a string")
    if not value.strip():
        raise PlatformOperatorError(f"{name} must not be blank")
    if value != value.strip():
        raise PlatformOperatorError(
            f"{name} must match the provider value exactly"
        )
    return value


def _provider_key(*, issuer: str, subject: str) -> tuple[str, str]:
    return (
        _exact_provider_value(name="issuer", value=issuer),
        _exact_provider_value(name="subject", value=subject),
    )


async def _user_id_by_username(
    session: AsyncSession,
    *,
    username: str,
) -> uuid.UUID:
    try:
        lookup_key = identity_service.normalize_username(username).lookup_key
    except identity_service.IdentityValidationError as exc:
        raise PlatformOperatorTargetNotFound(
            "the target account does not exist"
        ) from exc
    user_id = await session.scalar(
        select(User.id).where(User.normalized_username == lookup_key)
    )
    if user_id is None:
        raise PlatformOperatorTargetNotFound("the target account does not exist")
    return user_id


async def provision_platform_operator(
    session: AsyncSession,
    *,
    actor_username: str,
    username: str,
    issuer: str,
    subject: str,
) -> ProvisionedPlatformOperator:
    """Create one exact recordless operator and provider binding; never commit."""

    issuer, subject = _provider_key(issuer=issuer, subject=subject)
    prepared = await platform_admin_service.prepare_platform_admin(
        session,
        actor_username=actor_username,
    )
    actor_link = await session.scalar(
        select(UserFederatedIdentity.id).where(
            UserFederatedIdentity.user_id == prepared.user_id,
            UserFederatedIdentity.issuer == issuer,
        )
    )
    if actor_link is None:
        raise PlatformOperatorBootstrapIncomplete(
            "the acting administrator needs an existing binding for this issuer"
        )

    try:
        normalized = identity_service.normalize_username(username)
    except identity_service.IdentityValidationError as exc:
        raise PlatformOperatorError(str(exc)) from exc
    if await session.scalar(
        select(User.id).where(User.normalized_username == normalized.lookup_key)
    ) is not None:
        raise PlatformOperatorAlreadyExists("the target account already exists")
    if await session.scalar(
        select(UserFederatedIdentity.id).where(
            UserFederatedIdentity.issuer == issuer,
            UserFederatedIdentity.subject == subject,
        )
    ) is not None:
        raise PlatformOperatorAlreadyExists(
            "the provider identity is already linked"
        )

    user = User(
        username=normalized.display,
        normalized_username=normalized.lookup_key,
        password_hash=provisioning.LOCKED_PASSWORD_HASH,
        status=UserStatus.ACTIVE.value,
        session_version=1,
    )
    session.add(user)
    await session.flush()
    await identity_service.assign_role(
        session,
        user_id=user.id,
        role=UserRoleName.PLATFORM_SUPERADMIN,
        assigned_by_user_id=prepared.user_id,
    )
    try:
        link = await federation.link_identity(
            session,
            username=user.username,
            issuer=issuer,
            subject=subject,
        )
    except federation.FederatedLoginError as exc:
        raise PlatformOperatorError(str(exc)) from exc
    return ProvisionedPlatformOperator(user_id=user.id, identity_id=link.id)


async def revoke_health_owner_platform_admin(
    session: AsyncSession,
    *,
    actor_username: str,
    target_username: str,
    issuer: str,
) -> tuple[uuid.UUID, bool]:
    """Transfer control from a health owner to a proven operator; never commit."""

    issuer = _exact_provider_value(name="issuer", value=issuer)
    prepared = await platform_admin_service.prepare_platform_admin(
        session,
        actor_username=actor_username,
    )

    actor = await session.get(User, prepared.user_id)
    actor_roles = set(
        await session.scalars(
            select(UserRole.role).where(UserRole.user_id == prepared.user_id)
        )
    )
    actor_subject_id = await session.scalar(
        select(HealthSubject.id).where(
            HealthSubject.owner_user_id == prepared.user_id
        )
    )
    actor_links = list(
        await session.scalars(
            select(UserFederatedIdentity)
            .where(UserFederatedIdentity.user_id == prepared.user_id)
            .limit(2)
        )
    )
    if (
        actor is None
        or actor.password_hash != provisioning.LOCKED_PASSWORD_HASH
        or actor_subject_id is not None
        or actor_roles != {UserRoleName.PLATFORM_SUPERADMIN.value}
        or len(actor_links) != 1
        or actor_links[0].issuer != issuer
    ):
        raise PlatformOperatorShapeError(
            "the acting account must be the exact recordless OIDC platform operator"
        )
    login_proven = await session.scalar(
        select(User.id)
        .join(
            UserFederatedIdentity,
            UserFederatedIdentity.user_id == User.id,
        )
        .where(
            User.id == prepared.user_id,
            UserFederatedIdentity.id == actor_links[0].id,
            User.last_login_at.is_not(None),
            User.last_login_at >= UserFederatedIdentity.linked_at,
        )
    )
    if login_proven is None:
        raise PlatformOperatorLoginUnproven(
            "the acting operator needs a successful provider login after provisioning"
        )

    target_user_id = await _user_id_by_username(
        session,
        username=target_username,
    )
    target = await session.get(User, target_user_id)
    target_subject_id = await session.scalar(
        select(HealthSubject.id).where(HealthSubject.owner_user_id == target_user_id)
    )
    target_member_role = await session.scalar(
        select(UserRole.id).where(
            UserRole.user_id == target_user_id,
            UserRole.role == UserRoleName.MEMBER.value,
        )
    )
    if (
        target is None
        or target.status != UserStatus.ACTIVE.value
        or target_subject_id is None
        or target_member_role is None
    ):
        raise PlatformOperatorShapeError(
            "the target must be an active member who owns a health record"
        )
    changed = await identity_service.revoke_role(
        session,
        user_id=target_user_id,
        role=UserRoleName.PLATFORM_SUPERADMIN,
        actor_user_id=prepared.user_id,
    )
    return target_user_id, changed


__all__ = [
    "PlatformOperatorAlreadyExists",
    "PlatformOperatorBootstrapIncomplete",
    "PlatformOperatorError",
    "PlatformOperatorLoginUnproven",
    "PlatformOperatorShapeError",
    "PlatformOperatorTargetNotFound",
    "ProvisionedPlatformOperator",
    "provision_platform_operator",
    "revoke_health_owner_platform_admin",
]
