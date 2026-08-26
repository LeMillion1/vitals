"""Which Garmin account is *this* patient's, and everything that follows from it.

``credential_ref`` on an ``IntegrationConnection`` has been a handle naming
where a secret lives since the tenancy roots were introduced, and for Garmin and
Hevy nothing ever resolved it: both clients read ``.env`` directly, which is one
watch and one workout account for the whole process. This is the resolver those
two providers never had.

**A credential is not the only thing that has to be per account.** Everything
the Garmin client keeps beside it is process-wide too — the cached token session
in Redis, the login breaker's counters, the token store on disk. Two subjects
sharing those would mean one person's session resuming as another's, and one
person's failed logins pausing everybody. So this returns an account rather than
a password: a ``Config`` specialised to one connection, carrying its
credentials, its own token directory and the namespace its Redis keys hang off.

**Every account is namespaced, including the installation owner's.** An
alternative was to leave the owner on the unsuffixed paths so their cached
session survived the upgrade, and it was rejected: it makes "which subject is
the owner" a question every one of these lookups has to answer, and the answers
available — creation order, a discriminator the demo seeder also writes — are
either ambiguous or wrong. The cost of not doing it is one credential login for
the owner on the first sync after the upgrade, which is what happens whenever a
token expires anyway. What a burst of logins risks is a block; one does not.

**``legacy_env:`` names the installation's own account, and only it.** The
tenancy bootstrap used to write that ref on every subject's roots, without
knowing who they were for, so honouring it by its text would hand the operator's
Garmin to every patient. It is written only when the bootstrap is adopting the
environment's owner now, and revision 0060 cleared it from everybody else's — so
the ref means what it says, and a subject without one reads as "not configured",
which is the truth: nobody has entered their credentials yet.
"""
from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import Config, load_config
from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
)
from vitals.models.tenancy import IntegrationConnection
from vitals.services import credential_vault_service

#: The prefix the tenancy bootstrap writes on a root it has no secret for.
LEGACY_ENV_REF_PREFIX = "legacy_env:"

#: The provider/type pairs this module knows how to resolve. OpenRouter is a
#: platform gateway with its own control plane, and Telegram is gone; neither
#: belongs to a subject the way these two do.
_RESOLVABLE: dict[IntegrationProvider, IntegrationConnectionType] = {
    IntegrationProvider.GARMIN: IntegrationConnectionType.ACCOUNT,
    IntegrationProvider.HEVY: IntegrationConnectionType.ACCOUNT,
}

#: A connection that may still be used. ``RETIRED`` is provenance only.
_LIVE_STATUSES = tuple(
    status.value
    for status in IntegrationConnectionStatus
    if status is not IntegrationConnectionStatus.RETIRED
)


class ProviderCredentialsError(Exception):
    """Base class for a fail-closed provider credential error."""


class ProviderCredentialsValidationError(ProviderCredentialsError):
    """A caller passed something that is not a subject, or not a provider."""


class ProviderRootMissing(ProviderCredentialsError):
    """This subject has no connection root for this provider.

    Raised only by the writers. A reader answers ``None`` instead: for a read,
    "no root" and "no credential" are the same fact — nothing to sign in with —
    and every caller already handles it as not configured.
    """


@dataclass(frozen=True, slots=True)
class ProviderAccount:
    """One subject's account with one provider, and how to act as it."""

    subject_id: uuid.UUID
    integration_connection_id: uuid.UUID
    provider: IntegrationProvider
    #: A ``Config`` carrying this account's credentials, token directory and key
    #: namespace, and the installation's everything else. Handed straight to
    #: ``GarminClient``/``HevyClient``, whose constructors already take one.
    config: Config
    #: Whether there is anything to sign in with. ``False`` is ordinary: a
    #: subject who has not connected their watch yet.
    configured: bool

    @property
    def namespace(self) -> str:
        return self.config.provider_key_namespace


@dataclass(frozen=True, slots=True)
class ProviderAccountRef:
    """A scheduled-work target with no credential material attached."""

    subject_id: uuid.UUID
    integration_connection_id: uuid.UUID


def _require_subject_id(value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise ProviderCredentialsValidationError("subject_id must be a non-zero UUID")
    return value


async def _connection(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    provider: IntegrationProvider,
    for_update: bool = False,
) -> IntegrationConnection | None:
    connection_type = _RESOLVABLE.get(provider)
    if connection_type is None:
        raise ProviderCredentialsValidationError(
            f"{provider.value} is not a subject-owned provider account"
        )
    statement = select(IntegrationConnection).where(
        IntegrationConnection.subject_id == subject_id,
        IntegrationConnection.provider == provider.value,
        IntegrationConnection.connection_type == connection_type.value,
        IntegrationConnection.status.in_(_LIVE_STATUSES),
    )
    if for_update:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
        return await session.scalar(statement)
    with session.no_autoflush:
        return await session.scalar(statement)


def _namespace_for(connection_id: uuid.UUID) -> str:
    """The connection's id, for every account without exception.

    Deliberately not "empty for the owner": an exception there would have to be
    decided on every lookup, and the facts available to decide it with —
    creation order, the discriminator the demo seeder writes too — are ambiguous
    on exactly the installations that matter. See the module docstring for the
    one login this costs.
    """

    return str(connection_id)


def sync_marker_key(provider: IntegrationProvider, namespace: str) -> str:
    """Where "this account last synced successfully" is remembered.

    ``sync:last_success:garmin`` was flat, so the second patient's dashboard
    would have shown the timestamp of the *owner's* last sync — a page saying
    "synced 20 minutes ago" about data that has never arrived is worse than one
    saying nothing. Empty namespace keeps the owner's existing key, so their
    dashboard does not forget when it last worked.
    """

    base = f"sync:last_success:{provider.value}"
    return base if not namespace else f"{base}:{namespace}"


def _token_dir(base: str, namespace: str) -> str:
    if not namespace:
        return base
    return f"{base.rstrip('/')}/{namespace}"


async def resolve_account(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    provider: IntegrationProvider,
    base_config: Config | None = None,
) -> ProviderAccount | None:
    """This subject's account with this provider, or ``None`` if they have no root.

    Never raises for an unconfigured account. A subject who has not connected
    their watch is an ordinary state and comes back with ``configured=False``
    and a ``Config`` holding no credentials, which both clients already read as
    not configured.
    """

    subject_id = _require_subject_id(subject_id)
    connection = await _connection(session, subject_id=subject_id, provider=provider)
    if connection is None:
        return None

    cfg = base_config or load_config()
    namespace = _namespace_for(connection.id)

    secret: dict[str, str] | None = None
    ref = (connection.credential_ref or "").strip()
    if ref == credential_vault_service.VAULT_CREDENTIAL_REF:
        secret = await credential_vault_service.load(
            session, integration_connection_id=connection.id
        )
    elif ref.startswith(LEGACY_ENV_REF_PREFIX):
        # The environment's values. Safe to take at the ref's word because only
        # the installation's own account carries this ref — the bootstrap writes
        # it for the environment owner alone, and revision 0060 removed it from
        # the roots that had been given it indiscriminately.
        secret = _environment_secret(cfg, provider)

    if provider is IntegrationProvider.GARMIN:
        specialised = dataclasses.replace(
            cfg,
            garmin_email=(secret or {}).get("email", ""),
            garmin_password=(secret or {}).get("password", ""),
            garmin_token_dir=_token_dir(cfg.garmin_token_dir, namespace),
            provider_key_namespace=namespace,
        )
        configured = bool(
            specialised.garmin_email and specialised.garmin_password
        )
    else:
        specialised = dataclasses.replace(
            cfg,
            hevy_api_key=(secret or {}).get("api_key", ""),
            provider_key_namespace=namespace,
        )
        configured = bool(specialised.hevy_api_key)

    return ProviderAccount(
        subject_id=subject_id,
        integration_connection_id=connection.id,
        provider=provider,
        config=specialised,
        configured=configured,
    )


def _environment_secret(
    cfg: Config, provider: IntegrationProvider
) -> dict[str, str] | None:
    if provider is IntegrationProvider.GARMIN:
        if not (cfg.garmin_email and cfg.garmin_password):
            return None
        return {"email": cfg.garmin_email, "password": cfg.garmin_password}
    if not cfg.hevy_api_key:
        return None
    return {"api_key": cfg.hevy_api_key}


async def resolve_garmin_account(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    base_config: Config | None = None,
) -> ProviderAccount | None:
    return await resolve_account(
        session,
        subject_id=subject_id,
        provider=IntegrationProvider.GARMIN,
        base_config=base_config,
    )


async def resolve_hevy_account(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    base_config: Config | None = None,
) -> ProviderAccount | None:
    return await resolve_account(
        session,
        subject_id=subject_id,
        provider=IntegrationProvider.HEVY,
        base_config=base_config,
    )


async def _store(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    provider: IntegrationProvider,
    secret: dict[str, str],
) -> uuid.UUID:
    subject_id = _require_subject_id(subject_id)
    connection = await _connection(
        session, subject_id=subject_id, provider=provider, for_update=True
    )
    if connection is None:
        raise ProviderRootMissing(
            f"this record has no {provider.value} connection to attach a "
            "credential to"
        )
    await credential_vault_service.store(
        session,
        integration_connection_id=connection.id,
        subject_id=subject_id,
        secret=secret,
    )
    # The ref moves off ``legacy_env:`` in the same transaction as the secret
    # lands. Leaving it behind would mean the legacy owner's resolver preferring
    # the environment over the password they just typed.
    connection.credential_ref = credential_vault_service.VAULT_CREDENTIAL_REF
    if connection.status == IntegrationConnectionStatus.LEGACY.value:
        connection.status = IntegrationConnectionStatus.ACTIVE.value
    await session.flush()
    return connection.id


async def set_garmin_credentials(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    email: str,
    password: str,
) -> uuid.UUID:
    """Store this subject's Garmin sign-in. Never commits."""

    email = (email or "").strip()
    password = password or ""
    if not email or not password:
        raise ProviderCredentialsValidationError(
            "a Garmin credential needs both an email and a password"
        )
    return await _store(
        session,
        subject_id=subject_id,
        provider=IntegrationProvider.GARMIN,
        secret={"email": email, "password": password},
    )


async def set_hevy_credentials(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    api_key: str,
) -> uuid.UUID:
    """Store this subject's Hevy API key. Never commits."""

    api_key = (api_key or "").strip()
    if not api_key:
        raise ProviderCredentialsValidationError("a Hevy credential needs a key")
    return await _store(
        session,
        subject_id=subject_id,
        provider=IntegrationProvider.HEVY,
        secret={"api_key": api_key},
    )


async def forget_credentials(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    provider: IntegrationProvider,
) -> bool:
    """Disconnect an account. Never commits.

    The connection row stays. It is the provenance root every fact this
    provider ever produced points at, and deleting it would orphan a history
    that is still true — the account is gone, the workouts happened.
    """

    subject_id = _require_subject_id(subject_id)
    connection = await _connection(
        session, subject_id=subject_id, provider=provider, for_update=True
    )
    if connection is None:
        return False
    had = await credential_vault_service.clear(
        session, integration_connection_id=connection.id
    )
    connection.credential_ref = None
    await session.flush()
    return had


async def list_live_account_refs(
    session: AsyncSession,
    *,
    provider: IntegrationProvider,
) -> list[ProviderAccountRef]:
    """Provider targets that claim a credential, without resolving any secret.

    Scheduler discovery runs across subjects, so it must never decrypt every
    account into one platform-scoped transaction. The subject-bound job resolves
    its own credential later; a missing, unavailable, or corrupt credential is
    consequently isolated to that account instead of aborting discovery for all
    of them.
    """

    connection_type = _RESOLVABLE.get(provider)
    if connection_type is None:
        raise ProviderCredentialsValidationError(
            f"{provider.value} is not a subject-owned provider account"
        )
    rows = (
        await session.execute(
            select(IntegrationConnection.subject_id, IntegrationConnection.id)
            .where(
                IntegrationConnection.provider == provider.value,
                IntegrationConnection.connection_type == connection_type.value,
                IntegrationConnection.status.in_(_LIVE_STATUSES),
                IntegrationConnection.credential_ref.is_not(None),
            )
            .order_by(IntegrationConnection.subject_id)
        )
    ).all()
    return [
        ProviderAccountRef(
            subject_id=row.subject_id,
            integration_connection_id=row.id,
        )
        for row in rows
    ]


__all__ = [
    "LEGACY_ENV_REF_PREFIX",
    "ProviderAccount",
    "ProviderAccountRef",
    "ProviderCredentialsError",
    "ProviderCredentialsValidationError",
    "ProviderRootMissing",
    "forget_credentials",
    "list_live_account_refs",
    "resolve_account",
    "sync_marker_key",
    "resolve_garmin_account",
    "resolve_hevy_account",
    "set_garmin_credentials",
    "set_hevy_credentials",
]
