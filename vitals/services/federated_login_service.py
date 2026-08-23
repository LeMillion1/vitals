"""Turning a validated provider login into a local user, or refusing to.

:mod:`vitals.services.oidc` decides whether a token is genuine. This decides
whether the person it describes may have a session here, which is a different
question with a different answer: a perfectly valid login by somebody with no
account is a refusal, not a new account.

Provisioning is closed. An identity the provider vouches for is not an
invitation, and treating it as one would mean anybody who can register with the
provider can register here — which is exactly what a self-hosted health record
must not do.

The one exception is the bootstrap. An installation that predates federated
login already has an owner and no way to prove which provider identity is
theirs, so an operator names it: they read the opaque subject from the
provider's own console and configure it. On the first login matching that
subject, the existing owner is bound to it. Email is deliberately not used for
this — a provider may let somebody claim an address later, and a link made on
that basis would hand over the whole record.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserStatus
from vitals.models.identity import User, UserFederatedIdentity
from vitals.services.identity_service import acquire_identity_governance_lock


class FederatedLoginError(RuntimeError):
    """This login may not become a session here."""


class UnknownFederatedIdentity(FederatedLoginError):
    """A valid provider login with no account on this installation."""


class InactiveAccount(FederatedLoginError):
    """The account exists and may not be used."""


class BootstrapRefused(UnknownFederatedIdentity):
    """The one-time owner binding cannot apply to this installation's state.

    A subclass of :class:`UnknownFederatedIdentity` on purpose. Whether the
    binding was never configured, has already run, or cannot decide which user
    it means, the answer to whoever is knocking is the same one — otherwise a
    stranger probing with a guessed subject learns whether this installation
    has an owner, and how far along its setup is. The distinction survives in
    the message, which goes to the log and not to the browser.
    """


async def _link(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    issuer: str,
    subject: str,
    authenticated_at: datetime | None,
) -> UserFederatedIdentity:
    link = UserFederatedIdentity(
        user_id=user_id,
        issuer=issuer,
        subject=subject,
        last_authenticated_at=authenticated_at,
    )
    session.add(link)
    await session.flush()
    return link


async def _bootstrap_owner(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    authenticated_at: datetime | None,
) -> User:
    """Bind the installation's existing owner to the subject an operator named.

    Every precondition is checked inside the governance lock, because the whole
    value of this path is that it can happen exactly once. Two simultaneous
    first logins must not both find "no links yet" and both bind.
    """

    await acquire_identity_governance_lock(session)

    linked = await session.scalar(select(func.count()).select_from(UserFederatedIdentity))
    if linked:
        raise BootstrapRefused(
            "this installation already has a federated identity; the one-time "
            "owner binding cannot run again"
        )

    users = list(await session.scalars(select(User).order_by(User.created_at).limit(2)))
    if len(users) != 1:
        raise BootstrapRefused(
            "the one-time owner binding needs exactly one existing user; found "
            f"{len(users)}"
        )
    owner = users[0]
    if owner.status != UserStatus.ACTIVE.value:
        raise InactiveAccount("the existing owner's account is not active")

    await _link(
        session,
        user_id=owner.id,
        issuer=issuer,
        subject=subject,
        authenticated_at=authenticated_at,
    )
    return owner


async def resolve_federated_user(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    authenticated_at: datetime | None = None,
    bootstrap_subject: str = "",
) -> User:
    """The local user this provider identity is, or a refusal.

    Never creates an account for an unrecognised identity. The only way a link
    appears is the operator-configured bootstrap, and that runs once.
    """

    if not issuer.strip() or not subject.strip():
        raise FederatedLoginError("a federated login needs both an issuer and a subject")

    link = await session.scalar(
        select(UserFederatedIdentity).where(
            UserFederatedIdentity.issuer == issuer,
            UserFederatedIdentity.subject == subject,
        )
    )

    if link is None:
        if bootstrap_subject and subject == bootstrap_subject:
            owner = await _bootstrap_owner(
                session,
                issuer=issuer,
                subject=subject,
                authenticated_at=authenticated_at,
            )
            owner.last_login_at = datetime.now(timezone.utc)
            await session.flush()
            return owner
        # Deliberately the same refusal whether the subject is unknown or the
        # bootstrap has already run: a stranger learns nothing about whether
        # this installation has an owner, or who.
        raise UnknownFederatedIdentity(
            "this provider identity has no account on this installation"
        )

    user = await session.get(User, link.user_id)
    if user is None:
        raise UnknownFederatedIdentity(
            "the account behind this provider identity no longer exists"
        )
    if user.status != UserStatus.ACTIVE.value:
        raise InactiveAccount("the account behind this provider identity is not active")

    link.last_authenticated_at = authenticated_at
    user.last_login_at = datetime.now(timezone.utc)
    await session.flush()
    return user


__all__ = [
    "BootstrapRefused",
    "FederatedLoginError",
    "InactiveAccount",
    "UnknownFederatedIdentity",
    "resolve_federated_user",
]
