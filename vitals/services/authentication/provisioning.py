"""The one place a health subject is born, other than the legacy bootstrap.

Until now there were two, and only one of them was in the application:
``identity_bootstrap`` makes the installation's own owner out of
``VITALS_AUTH_USERNAME``, and ``scripts/seed_care_demo.py`` makes everybody else.
That is why a second subject has only ever existed on a developer's machine, and
why the shared-installation defects of the last few weeks were all found by
running that script rather than by using the product.

A subject is not one row. It needs an account that owns it, a member role, the
integration roots every provider path resolves through, and a module map — and a
subject missing any of those does not fail loudly, it fails on the fourth page
somebody visits. Collecting them here is the difference between "registration is
a form" and "registration is a form plus four things somebody has to remember".

**Nothing here decides whether an account *may* be created.** That is
``authentication.registration``'s question and it answers ``disabled`` by default. This
is what happens once something has said yes: an operator running the CLI, the
demo seeder, and — when registration opens — the federated login path.

**No environment credentials, ever.** ``bootstrap_legacy_resource_roots`` is
called without ``adopt_environment_credentials``, so a new subject's Garmin and
Hevy roots start with no credential at all. The values in ``.env`` are the
installation owner's; a root that claimed them would hand one person's watch to
everybody provisioned after them.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import UserRoleName, UserStatus
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.scoped_settings import SubjectSetting
from vitals.services.identity_bootstrap import validate_timezone
from vitals.services.identity_service import (
    IdentityValidationError,
    acquire_identity_governance_lock,
    normalize_username,
)
from vitals.utils.timeutils import DEFAULT_TIMEZONE


class AccountProvisioningError(Exception):
    """Base class for a fail-closed provisioning error."""


class AccountAlreadyExists(AccountProvisioningError):
    """That username is taken.

    Deliberately distinct from the refusals in ``authentication.federation``,
    which are uniform on purpose so a stranger learns nothing. This one is only
    ever seen by somebody who is already authorised to create accounts, and for
    them "taken" is the useful answer.
    """


class AccountProvisioningValidationError(AccountProvisioningError):
    """The username, timezone or role set is not one this can act on."""


@dataclass(frozen=True, slots=True)
class ProvisionedAccount:
    user_id: uuid.UUID
    #: ``None`` for an account that keeps no record of its own — a doctor or a
    #: trainer. That is not a degraded state: half of what this product is now
    #: is people who read somebody else's record and have none.
    subject_id: uuid.UUID | None
    roles: tuple[str, ...]


#: What goes in ``password_hash`` when there is no password.
#:
#: The column is nullable and its check only forbids blank, so ``None`` would
#: also pass — but a NULL there reads as "not set yet", which is a state the
#: legacy credential bridge has opinions about. This reads as what it is, and
#: ``verify_password`` returns ``False`` for it rather than raising, because it
#: is not a bcrypt hash and never can be. The leading ``!`` is the Unix locked-
#: account marker, for the same reason.
LOCKED_PASSWORD_HASH = "!no-password-login"


_PROVISIONABLE_ROLES = frozenset(
    {
        UserRoleName.MEMBER.value,
        UserRoleName.DOCTOR.value,
        UserRoleName.TRAINER.value,
    }
)


async def provision_account(
    session: AsyncSession,
    *,
    username: str,
    email: str | None = None,
    password_hash: str | None = None,
    display_name: str | None = None,
    timezone: str = DEFAULT_TIMEZONE,
    roles: tuple[str, ...] = (UserRoleName.MEMBER.value,),
    with_health_record: bool = True,
) -> ProvisionedAccount:
    """Create one account, and the record it owns if it is to own one.

    Never commits: the caller owns the transaction, because provisioning is
    usually one step of something larger — accepting an invitation, running an
    operator command — and a half-committed account is worse than none.

    ``password_hash`` is optional and is expected to be absent under the OIDC
    cutover, where the provider owns credentials and Vitals stores none. An
    account created without one gets :data:`LOCKED_PASSWORD_HASH`, which no
    password can ever verify against — and under the cutover ``authenticate()``
    refuses before it reaches the column at all.
    """

    try:
        normalized = normalize_username(username)
    except (IdentityValidationError, ValueError) as exc:
        raise AccountProvisioningValidationError(str(exc)) from exc

    unknown = set(roles) - _PROVISIONABLE_ROLES
    if unknown:
        # ``platform_superadmin`` is deliberately not provisionable here. It is
        # granted by an operator to an account that already exists, so that
        # creating an account and making it an administrator stay two decisions.
        raise AccountProvisioningValidationError(
            f"these roles cannot be granted at provisioning: {sorted(unknown)}"
        )
    if not roles:
        raise AccountProvisioningValidationError("an account needs at least one role")

    try:
        subject_timezone = validate_timezone(timezone)
    except ValueError as exc:
        raise AccountProvisioningValidationError(str(exc)) from exc

    # Serialized against the legacy bootstrap and against a concurrent
    # provisioning of the same name: the unique index would catch the second
    # one, but as an integrity error at flush rather than as this answer.
    await acquire_identity_governance_lock(session)
    taken = await session.scalar(
        select(User.id).where(User.normalized_username == normalized.lookup_key)
    )
    if taken is not None:
        raise AccountAlreadyExists(f"an account named {normalized.display!r} exists")

    normalized_email = (email or "").strip().casefold() or None
    user = User(
        username=normalized.display,
        normalized_username=normalized.lookup_key,
        email=(email or "").strip() or None,
        normalized_email=normalized_email,
        password_hash=password_hash or LOCKED_PASSWORD_HASH,
        status=UserStatus.ACTIVE.value,
        session_version=1,
    )
    session.add(user)
    await session.flush()

    for role in sorted(set(roles)):
        session.add(UserRole(user_id=user.id, role=role))
    await session.flush()

    subject_id: uuid.UUID | None = None
    if with_health_record:
        subject = HealthSubject(
            owner_user_id=user.id,
            display_name=(display_name or "").strip() or normalized.display,
            timezone=subject_timezone,
        )
        session.add(subject)
        await session.flush()
        subject_id = subject.id
        await _materialize_subject_roots(session, subject_id=subject_id)

    return ProvisionedAccount(
        user_id=user.id,
        subject_id=subject_id,
        roles=tuple(sorted(set(roles))),
    )


async def _materialize_subject_roots(
    session: AsyncSession, *, subject_id: uuid.UUID
) -> None:
    """Everything a subject needs beyond its own row.

    Two things, and both are the kind that fail late rather than loudly. Without
    the integration roots, every provider path refuses and the settings page
    answers 409 — for a reason that has nothing to do with the person reading
    it. Without a module map the subject inherits the installation-wide default,
    which on a shared installation is somebody else's choice of which sections
    exist.

    Notification preferences are deliberately *not* seeded: the write path
    creates all three partitions on the first save, and the human read falls
    back to the defaults it would have written. A row written here would only be
    a copy of that default with an earlier timestamp.
    """

    from vitals.services import modules_service
    from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots

    await bootstrap_legacy_resource_roots(session, subject_id=subject_id)
    session.add(
        SubjectSetting(
            subject_id=subject_id,
            key=modules_service.SETTINGS_KEY,
            value={key: True for key in modules_service.MODULE_REGISTRY},
        )
    )
    await session.flush()


__all__ = [
    "AccountAlreadyExists",
    "AccountProvisioningError",
    "AccountProvisioningValidationError",
    "LOCKED_PASSWORD_HASH",
    "ProvisionedAccount",
    "provision_account",
]
