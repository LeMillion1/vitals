"""Idempotent stage-0 roots for the legacy single-user integrations.

This bootstrap records ownership boundaries only.  It deliberately does not
read environment configuration, resolve credentials, inspect files, construct a
provider client, or perform network I/O.  The caller owns commit or rollback.

This legacy bootstrap remains required while startup adopts pre-tenancy Garmin,
Hevy, OpenRouter, and Telegram roots. It may be removed only when migrations or
operator workflows create every supported connection root and startup no longer
calls :func:`bootstrap_legacy_resource_roots`.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AuditOutcome,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
)
from vitals.models.identity import AuditEvent, HealthSubject
from vitals.models.tenancy import IntegrationConnection
from vitals.services.identity.governance import acquire_identity_governance_lock

LEGACY_ACCOUNT_DISCRIMINATOR = "legacy_singleton_v1"

LEGACY_CONNECTION_TYPES: Mapping[
    IntegrationProvider, IntegrationConnectionType
] = MappingProxyType(
    {
        IntegrationProvider.GARMIN: IntegrationConnectionType.ACCOUNT,
        IntegrationProvider.HEVY: IntegrationConnectionType.ACCOUNT,
        IntegrationProvider.OPENROUTER: IntegrationConnectionType.AI_GATEWAY,
        IntegrationProvider.TELEGRAM: IntegrationConnectionType.RECIPIENT,
    }
)


class LegacyResourceRootsBootstrapError(RuntimeError):
    """Base class for a fail-closed legacy resource-roots bootstrap error."""


class LegacySubjectNotFoundError(LegacyResourceRootsBootstrapError):
    """The requested health-subject ownership root does not exist."""


@dataclass(frozen=True, slots=True)
class LegacyResourceRootsBootstrapResult:
    """Summary of one flush-only bootstrap attempt."""

    subject_id: uuid.UUID
    created_connection_ids: tuple[uuid.UUID, ...]
    created_providers: frozenset[IntegrationProvider]
    skipped_providers: frozenset[IntegrationProvider]
    audit_event_id: uuid.UUID | None

    @property
    def changed(self) -> bool:
        return bool(self.created_connection_ids)


#: Providers whose ``.env`` credential describes a person rather than the
#: installation. A ``legacy_env:`` ref on one of these means "this account's
#: secret is in the environment file", and only the record that file was written
#: for may say it — see ``adopt_environment_credentials`` below.
_SUBJECT_OWNED_PROVIDERS = frozenset(
    {IntegrationProvider.GARMIN, IntegrationProvider.HEVY}
)


async def bootstrap_legacy_resource_roots(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    adopt_environment_credentials: bool = False,
) -> LegacyResourceRootsBootstrapResult:
    """Create harmless legacy connection roots for one existing subject.

    The shared governance lock serializes this query-then-insert sequence across
    PostgreSQL workers.  Any existing row for the same subject/provider/type is
    authoritative regardless of discriminator or lifecycle state: bootstrap
    skips it verbatim and never repairs, activates, disables, or downgrades it.

    ``adopt_environment_credentials`` decides whether the Garmin and Hevy roots
    are allowed to claim the environment's credentials, and defaults to *no*.
    It used to be unconditional, which was harmless while the only caller was
    the startup bootstrap of the installation's own owner and became a
    disclosure the moment a second subject was created: every new patient's
    roots said "my Garmin password is in ``.env``", and ``.env`` holds the
    operator's. Only the boot path that is reconciling ``VITALS_AUTH_USERNAME``
    passes ``True``. OpenRouter and Telegram are unaffected either way — those
    are installation-wide accounts, and their refs mean what they say for
    everybody.

    The function mutates and flushes only.  It never commits.
    """

    if not isinstance(subject_id, uuid.UUID):
        raise TypeError("subject_id must be a UUID")

    await acquire_identity_governance_lock(session)
    persisted_subject_id = await session.scalar(
        select(HealthSubject.id)
        .where(HealthSubject.id == subject_id)
        .with_for_update()
    )
    if persisted_subject_id is None:
        raise LegacySubjectNotFoundError(f"health subject {subject_id} does not exist")

    existing_pairs = set(
        await session.execute(
            select(
                IntegrationConnection.provider,
                IntegrationConnection.connection_type,
            )
            .where(IntegrationConnection.subject_id == subject_id)
            .with_for_update()
        )
    )

    created_connections: list[IntegrationConnection] = []
    created_providers: set[IntegrationProvider] = set()
    skipped_providers: set[IntegrationProvider] = set()
    for provider, connection_type in LEGACY_CONNECTION_TYPES.items():
        pair = (provider.value, connection_type.value)
        if pair in existing_pairs:
            skipped_providers.add(provider)
            continue

        connection = IntegrationConnection(
            subject_id=subject_id,
            provider=provider.value,
            connection_type=connection_type.value,
            external_account_discriminator=LEGACY_ACCOUNT_DISCRIMINATOR,
            credential_ref=(
                None
                if provider in _SUBJECT_OWNED_PROVIDERS
                and not adopt_environment_credentials
                else f"legacy_env:{provider.value}"
            ),
            status=IntegrationConnectionStatus.LEGACY.value,
        )
        session.add(connection)
        created_connections.append(connection)
        created_providers.add(provider)

    await session.flush()

    audit_event_id: uuid.UUID | None = None
    if created_connections:
        event = AuditEvent(
            actor_user_id=None,
            subject_id=subject_id,
            event_type="tenancy.legacy_resource_roots.bootstrap",
            outcome=AuditOutcome.SUCCESS.value,
            resource_type="health_subject",
            resource_id=str(subject_id),
            metadata_json={
                "source_surface": "startup",
                "result_code": "legacy_connection_roots_created",
                "changed_fields": [
                    f"integration_connections.{provider.value}"
                    for provider, _connection_type in LEGACY_CONNECTION_TYPES.items()
                    if provider in created_providers
                ],
                "record_count": len(created_connections),
            },
        )
        session.add(event)
        await session.flush()
        audit_event_id = event.id

    return LegacyResourceRootsBootstrapResult(
        subject_id=subject_id,
        created_connection_ids=tuple(row.id for row in created_connections),
        created_providers=frozenset(created_providers),
        skipped_providers=frozenset(skipped_providers),
        audit_event_id=audit_event_id,
    )


__all__ = [
    "LEGACY_ACCOUNT_DISCRIMINATOR",
    "LEGACY_CONNECTION_TYPES",
    "LegacyResourceRootsBootstrapError",
    "LegacyResourceRootsBootstrapResult",
    "LegacySubjectNotFoundError",
    "bootstrap_legacy_resource_roots",
]
