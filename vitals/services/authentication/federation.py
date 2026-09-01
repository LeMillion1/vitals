"""Turning a validated provider login into a local user, or refusing to.

:mod:`vitals.services.authentication.oidc` decides whether a token is genuine. This decides
whether the person it describes may have a session here, which is a different
question with a different answer: a perfectly valid login by somebody with no
account is a refusal, not a new account.

Provisioning is closed, and closed by a decision rather than by absence.
``authentication.registration`` is what says so, and it answers ``disabled`` unless the
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
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AuditOutcome,
    RegistrationAccountKind,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import AuditEvent, User, UserFederatedIdentity
from vitals.services.identity.contracts import IdentityValidationError
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.services.identity.normalization import normalize_email, normalize_username
from vitals.utils.timeutils import now_utc

if TYPE_CHECKING:
    from vitals.services.authentication.oidc import FederatedIdentity


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


@dataclass(frozen=True, slots=True)
class FederatedSessionDecision:
    """The local identity needed to issue a browser session."""

    username: str
    user_id: uuid.UUID
    session_version: int
    authenticated_at: datetime | None
    subject_id: uuid.UUID | None


class FederatedRegistrationState(StrEnum):
    """Non-enumerating state exposed to a registration applicant."""

    PENDING = "pending"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class FederatedRegistrationDecision:
    """An admission state that must be shown without issuing a session."""

    reference: uuid.UUID
    state: FederatedRegistrationState


async def _synchronize_verified_email_claim(
    session: AsyncSession,
    *,
    user: User,
    email: str | None,
    email_verified: bool,
) -> None:
    """Project the provider's current verified mailbox claim onto the account.

    The immutable identity remains ``(issuer, subject)``.  This projection is
    only the proof used by address-bound workflows such as care invitations.
    A later token that no longer vouches for an address revokes that proof, so
    a stale database value can never stand in for the current login.
    """

    await acquire_identity_governance_lock(session)
    if not email_verified:
        user.email_verified_at = None
        await session.flush()
        return

    try:
        normalized = normalize_email(email)
    except IdentityValidationError as exc:
        raise FederatedLoginError(
            "the provider marked an unusable email claim as verified"
        ) from exc

    collision = await session.scalar(
        select(User.id).where(
            User.normalized_email == normalized.lookup_key,
            User.id != user.id,
        )
    )
    if collision is not None:
        # Email is not used to find or merge accounts.  A provider claim that
        # collides with another local account is therefore a refusal, not a
        # reason to move either identity or silently keep an old proof.
        raise FederatedLoginError(
            "the provider's verified email belongs to another local account"
        )

    user.email = normalized.display
    user.normalized_email = normalized.lookup_key
    user.email_verified_at = now_utc()
    await session.flush()


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

    from vitals.services.authentication.startup import (
        OidcOwnerBootstrapGraphError,
        require_oidc_owner_bootstrap_graph,
    )

    try:
        owner = await require_oidc_owner_bootstrap_graph(session)
    except OidcOwnerBootstrapGraphError as exc:
        raise BootstrapRefused(str(exc)) from exc

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
    email_verified: bool,
    preferred_username: str | None,
    authenticated_at: datetime | None,
    registration_intent_id: uuid.UUID | None = None,
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

    from vitals.services.authentication import admission, provisioning, registration

    # Registration policy and identity creation are one governance decision.
    # The lock is deliberately acquired before re-reading both the immutable
    # provider link and the stored mode: a concurrent callback may have created
    # the link while this transaction waited, and an operator may have closed
    # the door after this callback first looked at it.
    await acquire_identity_governance_lock(session)
    existing = await session.scalar(
        select(UserFederatedIdentity).where(
            UserFederatedIdentity.issuer == issuer,
            UserFederatedIdentity.subject == subject,
        )
    )
    if existing is not None:
        return await _finish_linked_login(
            session,
            link=existing,
            authenticated_at=authenticated_at,
            email=email,
            email_verified=email_verified,
        )

    try:
        await registration.require_open_registration(session)
    except registration.RegistrationClosed:
        if registration_intent_id is not None:
            raise admission.AdmissionRefused(
                "this admission proof does not open an account"
            )
        return None

    account_kind = RegistrationAccountKind.MEMBER
    if registration_intent_id is not None:
        intent = await admission.consume_intent(
            session,
            intent_id=registration_intent_id,
        )
        try:
            account_kind = RegistrationAccountKind(intent.account_kind)
        except ValueError as exc:  # Defensive against manually corrupted rows.
            raise admission.AdmissionRefused(
                "this admission proof does not open an account"
            ) from exc

    candidate = (preferred_username or "").strip()
    if not candidate and email and "@" in email:
        candidate = email.split("@", 1)[0].strip()
    if not candidate:
        candidate = f"user-{subject[:24]}"

    try:
        if account_kind is RegistrationAccountKind.MEMBER:
            provisioned = await provisioning.provision_bound_member_account(
                session,
                username=candidate,
                # The provider's display claim may name the account, but only a
                # verified claim may become an address-bound local fact. Project
                # it below after the account exists and collision handling can
                # remain a uniform login refusal.
                email=None,
            )
        else:
            role = (
                UserRoleName.DOCTOR
                if account_kind is RegistrationAccountKind.DOCTOR
                else UserRoleName.TRAINER
            )
            provisioned = await provisioning.provision_account(
                session,
                username=candidate,
                email=None,
                roles=(role.value,),
                with_health_record=False,
            )
    except provisioning.AccountProvisioningError as exc:
        # Invalid or colliding provider naming data is one uniform refusal.
        # Picking a suffix would imply a relationship to an existing account;
        # matching an existing email would turn a mutable claim into identity.
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
    await _synchronize_verified_email_claim(
        session,
        user=user,
        email=email,
        email_verified=email_verified,
    )
    user.last_login_at = datetime.now(timezone.utc)
    changed_fields = ["federated_identity", "roles"]
    if provisioned.subject_id is not None:
        changed_fields.append("subject")
    session.add(
        AuditEvent(
            actor_user_id=None,
            subject_id=provisioned.subject_id,
            event_type="registration.account.provisioned",
            outcome=AuditOutcome.SUCCESS.value,
            resource_type="user",
            resource_id=str(user.id),
            metadata_json={
                "source_surface": "authentication.federation",
                "result_code": f"{account_kind.value}_open_registration_admitted",
                "resource_type": "user",
                "resource_id": str(user.id),
                "changed_fields": changed_fields,
            },
        )
    )
    await session.flush()
    return user


async def _finish_linked_login(
    session: AsyncSession,
    *,
    link: UserFederatedIdentity,
    authenticated_at: datetime | None,
    email: str | None,
    email_verified: bool,
) -> User:
    """Finish a known identity login after any required serialization."""

    user = await session.get(User, link.user_id)
    if user is None:
        raise UnknownFederatedIdentity(
            "the account behind this provider identity no longer exists"
        )
    if user.status != UserStatus.ACTIVE.value:
        raise InactiveAccount("the account behind this provider identity is not active")

    await _synchronize_verified_email_claim(
        session,
        user=user,
        email=email,
        email_verified=email_verified,
    )
    link.last_authenticated_at = authenticated_at
    user.last_login_at = datetime.now(timezone.utc)
    await session.flush()
    return user


async def resolve_existing_federated_user(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    authenticated_at: datetime | None = None,
    email: str | None = None,
    email_verified: bool = False,
) -> User | None:
    """Resolve only a link that already exists, never bootstrap or register.

    Invitation orchestration uses this before creating an account: a person who
    already has one may sign in normally, but an unknown identity carrying an
    invitation must use that exact proof and may never fall through to open
    registration if the installation mode changes during the OIDC round trip.
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
        return None
    return await _finish_linked_login(
        session,
        link=link,
        authenticated_at=authenticated_at,
        email=email,
        email_verified=email_verified,
    )


async def resolve_federated_user(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    authenticated_at: datetime | None = None,
    bootstrap_subject: str = "",
    email: str | None = None,
    email_verified: bool = False,
    preferred_username: str | None = None,
    registration_intent_id: uuid.UUID | None = None,
) -> User:
    """The local user this provider identity is, or a refusal.

    Creates an account for an unrecognised identity only where
    ``authentication.registration`` says the installation is accepting them, which by
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
            await _synchronize_verified_email_claim(
                session,
                user=owner,
                email=email,
                email_verified=email_verified,
            )
            await session.flush()
            return owner
        provisioned = await _provision_if_registration_is_open(
            session,
            issuer=issuer,
            subject=subject,
            email=email,
            email_verified=email_verified,
            preferred_username=preferred_username,
            authenticated_at=authenticated_at,
            registration_intent_id=registration_intent_id,
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

    return await _finish_linked_login(
        session,
        link=link,
        authenticated_at=authenticated_at,
        email=email,
        email_verified=email_verified,
    )


async def decide_federated_login(
    session: AsyncSession,
    *,
    identity: FederatedIdentity,
    bootstrap_subject: str,
    invitation_id: uuid.UUID | None,
    registration_intent_id: uuid.UUID | None = None,
    step_up: bool,
) -> FederatedSessionDecision | FederatedRegistrationDecision:
    """Resolve one validated provider identity into a local login outcome.

    OIDC validation belongs to the provider adapter and browser redirects belong
    to the web layer. This function owns the application decision between an
    existing account, the one-time owner bootstrap, a registration request, and
    an exact invitation. It never commits; the delivery boundary acknowledges
    a registration request only after committing it.
    """

    from vitals.enums import RegistrationRequestStatus
    from vitals.models.identity import HealthSubject
    from vitals.services.authentication import admission

    if invitation_id is not None and registration_intent_id is not None:
        raise FederatedLoginError("one login may carry only one admission proof")
    if step_up and registration_intent_id is not None:
        raise FederatedLoginError("step-up cannot create an account")

    if invitation_id is None or identity.subject == bootstrap_subject:
        try:
            user = await resolve_federated_user(
                session,
                issuer=identity.issuer,
                subject=identity.subject,
                authenticated_at=identity.authenticated_at,
                bootstrap_subject=bootstrap_subject,
                email=identity.email,
                email_verified=identity.email_verified,
                preferred_username=identity.preferred_username,
                registration_intent_id=registration_intent_id,
            )
        except BootstrapRefused:
            # A failed bootstrap ceremony must not become a public application.
            raise
        except UnknownFederatedIdentity:
            if step_up:
                # Step-up can only re-authenticate an already known account.
                raise
            try:
                row = await admission.submit_request(
                    session,
                    issuer=identity.issuer,
                    subject=identity.subject,
                    verified_email=identity.email,
                    email_verified=identity.email_verified,
                    preferred_username=identity.preferred_username,
                )
            except admission.AdmissionRefused as submission_error:
                row = await admission.get_request(
                    session,
                    issuer=identity.issuer,
                    subject=identity.subject,
                )
                if row is None:
                    raise submission_error

            if row.status == RegistrationRequestStatus.PENDING.value:
                state = FederatedRegistrationState.PENDING
            elif row.status in {
                RegistrationRequestStatus.REJECTED.value,
                RegistrationRequestStatus.EXPIRED.value,
            }:
                state = FederatedRegistrationState.CLOSED
            else:
                raise admission.AdmissionStateError(
                    "approved registration request has no federated identity"
                )
            return FederatedRegistrationDecision(reference=row.id, state=state)
    else:
        user = await resolve_existing_federated_user(
            session,
            issuer=identity.issuer,
            subject=identity.subject,
            authenticated_at=identity.authenticated_at,
            email=identity.email,
            email_verified=identity.email_verified,
        )
        if user is None:
            user = (
                await admission.consume_invitation_claim(
                    session,
                    invitation_id=invitation_id,
                    issuer=identity.issuer,
                    subject=identity.subject,
                    authenticated_at=identity.authenticated_at,
                    verified_email=identity.email,
                    email_verified=identity.email_verified,
                    preferred_username=identity.preferred_username,
                )
            ).user

    subject_id = await session.scalar(
        select(HealthSubject.id).where(HealthSubject.owner_user_id == user.id)
    )
    return FederatedSessionDecision(
        username=user.username,
        user_id=user.id,
        session_version=user.session_version,
        authenticated_at=identity.authenticated_at,
        subject_id=subject_id,
    )


__all__ = [
    "BootstrapRefused",
    "FederatedRegistrationDecision",
    "FederatedRegistrationState",
    "FederatedLoginError",
    "FederatedSessionDecision",
    "InactiveAccount",
    "UnknownFederatedIdentity",
    "decide_federated_login",
    "resolve_existing_federated_user",
    "resolve_federated_user",
]
