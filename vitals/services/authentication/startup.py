"""Fail-closed database preconditions for an OIDC-authenticated process.

Password mode may create the original installation owner from its legacy
environment credential.  OIDC mode must never do that: the provider proves a
principal, while the durable Vitals database decides which local account that
principal may enter.  This service checks that the configured issuer can reach
an active account, or that the one-time owner binding is still safe to perform.

The function is read-only apart from taking the shared identity-governance
lock.  The startup boundary owns its transaction.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserRoleName, UserStatus
from vitals.models.identity import (
    HealthSubject,
    User,
    UserFederatedIdentity,
    UserRole,
)
from vitals.services.identity_service import acquire_identity_governance_lock


class OidcStartupStateError(RuntimeError):
    """The configured OIDC authority cannot safely reach a local account."""


class OidcOwnerBootstrapGraphError(OidcStartupStateError):
    """The one-time owner binding cannot identify one safe privileged owner."""


async def require_oidc_owner_bootstrap_graph(session: AsyncSession) -> User:
    """Return the only safe owner for a first OIDC link, or refuse.

    The governance lock is taken here so both process startup and the callback
    enforce the same graph while it is stable. Calling this after already taking
    the transaction-scoped advisory lock is harmless and keeps the helper safe
    for future callers.
    """

    await acquire_identity_governance_lock(session)

    users = list(await session.scalars(select(User).limit(2)))
    if len(users) != 1:
        raise OidcOwnerBootstrapGraphError(
            "the one-time OIDC owner binding requires exactly one existing user; "
            f"found {len(users)}"
        )
    owner = users[0]
    if owner.status != UserStatus.ACTIVE.value:
        raise OidcOwnerBootstrapGraphError(
            "the one-time OIDC owner binding requires an active existing user"
        )

    roles = set(
        await session.scalars(select(UserRole.role).where(UserRole.user_id == owner.id))
    )
    required_roles = {
        UserRoleName.MEMBER.value,
        UserRoleName.PLATFORM_SUPERADMIN.value,
    }
    if not required_roles.issubset(roles):
        raise OidcOwnerBootstrapGraphError(
            "the one-time OIDC owner binding requires the existing user to hold "
            "member and platform_superadmin roles"
        )

    subjects = list(await session.scalars(select(HealthSubject).limit(2)))
    if len(subjects) != 1 or subjects[0].owner_user_id != owner.id:
        raise OidcOwnerBootstrapGraphError(
            "the one-time OIDC owner binding requires exactly the existing "
            "user's one health subject"
        )
    return owner


async def validate_oidc_startup_state(
    session: AsyncSession,
    *,
    issuer: str,
    bootstrap_subject: str,
) -> None:
    """Prove that OIDC startup has a safe local-account destination.

    An already-used installation needs an active link for this exact issuer.
    A never-linked installation instead needs the explicit bootstrap subject,
    exactly one active user, and exactly that user's one health subject.  Those
    are the same structural assumptions the one-time callback binding makes,
    checked before the HTTP process starts accepting traffic.
    """

    if not isinstance(issuer, str) or not issuer.strip():
        raise OidcStartupStateError("OIDC startup requires a non-blank issuer")
    if not isinstance(bootstrap_subject, str):
        raise OidcStartupStateError("OIDC bootstrap subject must be a string")

    await acquire_identity_governance_lock(session)

    current_link_count = int(
        await session.scalar(
            select(func.count())
            .select_from(UserFederatedIdentity)
            .where(UserFederatedIdentity.issuer == issuer)
        )
        or 0
    )
    reachable_admin_link_count = int(
        await session.scalar(
            select(func.count(func.distinct(UserFederatedIdentity.id)))
            .select_from(UserFederatedIdentity)
            .join(User, User.id == UserFederatedIdentity.user_id)
            .join(UserRole, UserRole.user_id == User.id)
            .where(
                UserFederatedIdentity.issuer == issuer,
                User.status == UserStatus.ACTIVE.value,
                UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
            )
        )
        or 0
    )
    if reachable_admin_link_count:
        if bootstrap_subject.strip():
            matching_bootstrap_count = int(
                await session.scalar(
                    select(func.count(func.distinct(UserFederatedIdentity.id)))
                    .select_from(UserFederatedIdentity)
                    .join(User, User.id == UserFederatedIdentity.user_id)
                    .join(HealthSubject, HealthSubject.owner_user_id == User.id)
                    .join(UserRole, UserRole.user_id == User.id)
                    .where(
                        UserFederatedIdentity.issuer == issuer,
                        UserFederatedIdentity.subject == bootstrap_subject.strip(),
                        User.status == UserStatus.ACTIVE.value,
                        UserRole.role == UserRoleName.PLATFORM_SUPERADMIN.value,
                    )
                )
                or 0
            )
            if not matching_bootstrap_count:
                raise OidcStartupStateError(
                    "VITALS_OIDC_BOOTSTRAP_SUBJECT does not match the existing "
                    "owner binding; remove the exhausted value or restore the "
                    "correct one"
                )
        return
    if current_link_count:
        raise OidcStartupStateError(
            "the configured OIDC issuer has no active platform administrator "
            "with a local identity binding"
        )

    any_link_count = int(
        await session.scalar(
            select(func.count()).select_from(UserFederatedIdentity)
        )
        or 0
    )
    if any_link_count:
        raise OidcStartupStateError(
            "the configured OIDC issuer does not match any existing identity binding"
        )

    if not bootstrap_subject.strip():
        raise OidcStartupStateError(
            "an unlinked installation requires VITALS_OIDC_BOOTSTRAP_SUBJECT"
        )

    await require_oidc_owner_bootstrap_graph(session)


__all__ = [
    "OidcOwnerBootstrapGraphError",
    "OidcStartupStateError",
    "require_oidc_owner_bootstrap_graph",
    "validate_oidc_startup_state",
]
