"""Turning a validated provider login into a local user, or refusing to.

:mod:`vitals.services.oidc` decides whether a token is genuine. This decides
whether the person it describes may have a session here, which is a different
question with a different answer: a perfectly valid login by somebody with no
account is a refusal, not a new account.

Provisioning is closed, and closed by a decision rather than by absence.
``registration_service`` is what says so, and it answers ``disabled`` unless the
deployment has been cleared to open registration *and* an administrator has
configured a mode — two switches, neither of which is on. An identity the
provider vouches for is not an invitation, and treating it as one would mean
anybody who can register with the provider can register here, which is exactly
what a self-hosted health record must not do.

Until this, "closed" was a property of there being nowhere for a new account to
come from. That was true and it was fragile: the day something could create one,
nothing would have stopped it.

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
from vitals.services.identity_service import (
    IdentityValidationError,
    acquire_identity_governance_lock,
    normalize_username,
)


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


class IdentityAlreadyLinked(FederatedLoginError):
    """That provider identity already belongs to an account here."""


class NoSuchAccount(FederatedLoginError):
    """There is no local account by that name to link."""


async def link_identity(
    session: AsyncSession,
    *,
    username: str,
    issuer: str,
    subject: str,
) -> UserFederatedIdentity:
    """Bind an existing account to a provider identity. Never commits.

    The operator step after ``provision_account`` had always been described and
    never implemented. An account created by the CLI can use no password — the password
    login authenticates exactly one username from ``.env`` — so until its
    provider identity is linked it is an account nobody can reach, and the
    only binding that existed was the one-time bootstrap, which refuses the
    moment an installation has more than one user or more than no links.

    Deliberately by ``(issuer, subject)`` and never by email: a provider may
    let somebody claim an address later, and a link made on that basis hands
    over a whole health record.

    Not exposed through the web layer, and the reason is what a link is. It
    says which human being reaches this record; anybody who could add one from
    a browser could point somebody else's record at themselves. That decision
    stays with whoever has a shell on the machine.
    """

    if not issuer.strip() or not subject.strip():
        raise FederatedLoginError("a link needs both an issuer and a subject")
    if issuer != issuer.strip() or subject != subject.strip():
        raise FederatedLoginError(
            "issuer and subject must match the provider values exactly, "
            "without surrounding whitespace"
        )

    await acquire_identity_governance_lock(session)

    try:
        lookup = normalize_username(username).lookup_key
    except IdentityValidationError as exc:
        raise NoSuchAccount(f"no account named {username!r}") from exc
    user = await session.scalar(
        select(User).where(User.normalized_username == lookup)
    )
    if user is None:
        raise NoSuchAccount(f"no account named {username!r}")
    if user.status != UserStatus.ACTIVE.value:
        raise InactiveAccount("that account is not active")

    existing = await session.scalar(
        select(UserFederatedIdentity).where(
            UserFederatedIdentity.issuer == issuer,
            UserFederatedIdentity.subject == subject,
        )
    )
    if existing is not None:
        # Named rather than silently re-pointed. Moving a link is how one
        # person's identity comes to open another person's record, and if it is
        # ever wanted it should be its own operation with its own name.
        raise IdentityAlreadyLinked(
            "that provider identity is already linked to an account"
        )

    return await _link(
        session,
        user_id=user.id,
        issuer=issuer,
        subject=subject,
        authenticated_at=None,
    )


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


async def _provision_if_registration_is_open(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    email: str | None,
    preferred_username: str | None,
    authenticated_at: datetime | None,
) -> User | None:
    """An account for a stranger, if this installation has said it wants one.

    ``None`` rather than an exception when registration is closed, so the caller
    can fall through to the one uniform refusal above. A closed door and an
    unknown identity have to be indistinguishable from outside.

    The username comes from the provider's ``preferred_username``, or from the
    local part of the email, or from the opaque subject — in that order, and the
    subject is the one that always works. It is a display and lookup name, not
    an identity: the ``(issuer, sub)`` link created below is what proves who
    this is, and it is what every later login matches on. So a collision on the
    name is a collision to resolve, not a way to become somebody else.
    """

    from vitals.services import account_provisioning_service, registration_service

    try:
        await registration_service.require_open_registration(session)
    except registration_service.RegistrationClosed:
        return None

    candidate = (preferred_username or "").strip()
    if not candidate and email and "@" in email:
        candidate = email.split("@", 1)[0].strip()
    if not candidate:
        candidate = f"user-{subject[:24]}"

    try:
        provisioned = await account_provisioning_service.provision_account(
            session,
            username=candidate,
            email=email,
        )
    except account_provisioning_service.AccountAlreadyExists as exc:
        # Somebody already holds this name and it is not this identity — the
        # link lookup above would have found them otherwise. Refusing is right:
        # picking ``candidate-2`` would hand a stranger an account whose name
        # implies a relationship to an existing one.
        raise UnknownFederatedIdentity(
            "this provider identity has no account on this installation"
        ) from exc

    user = await session.get(User, provisioned.user_id)
    await _link(
        session,
        user_id=user.id,
        issuer=issuer,
        subject=subject,
        authenticated_at=authenticated_at,
    )
    user.last_login_at = datetime.now(timezone.utc)
    await session.flush()
    return user


async def resolve_federated_user(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    authenticated_at: datetime | None = None,
    bootstrap_subject: str = "",
    email: str | None = None,
    preferred_username: str | None = None,
) -> User:
    """The local user this provider identity is, or a refusal.

    Creates an account for an unrecognised identity only where
    ``registration_service`` says the installation is accepting them, which by
    default and by deployment gate it is not. Otherwise the only way a link
    appears is the operator-configured bootstrap, and that runs once.

    ``email`` and ``preferred_username`` are claims, used for nothing but naming
    a newly provisioned account. Neither is an identity key and neither is
    matched against an existing user: a provider that lets somebody claim an
    address later would otherwise be a way to take over a record.
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
        provisioned = await _provision_if_registration_is_open(
            session,
            issuer=issuer,
            subject=subject,
            email=email,
            preferred_username=preferred_username,
            authenticated_at=authenticated_at,
        )
        if provisioned is not None:
            return provisioned
        # Deliberately the same refusal whether the subject is unknown, the
        # bootstrap has already run, or registration is closed: a stranger
        # learns nothing about whether this installation has an owner, who they
        # are, or whether it is accepting new people.
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
