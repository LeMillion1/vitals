"""Idempotent stage-0 roots for the legacy single-user integrations.

This bootstrap records ownership boundaries only.  It deliberately does not
read environment configuration, resolve credentials, inspect files, construct a
provider client, or perform network I/O.  The caller owns commit or rollback.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

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
from vitals.services.identity_service import acquire_identity_governance_lock

LEGACY_ACCOUNT_DISCRIMINATOR = "legacy_singleton_v1"

_LEGACY_CONNECTION_ROOTS = (
    (IntegrationProvider.GARMIN, IntegrationConnectionType.ACCOUNT),
    (IntegrationProvider.HEVY, IntegrationConnectionType.ACCOUNT),
    (IntegrationProvider.OPENROUTER, IntegrationConnectionType.AI_GATEWAY),
    (IntegrationProvider.TELEGRAM, IntegrationConnectionType.RECIPIENT),
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


async def bootstrap_legacy_resource_roots(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> LegacyResourceRootsBootstrapResult:
    """Create harmless legacy connection roots for one existing subject.

    The shared governance lock serializes this query-then-insert sequence across
    PostgreSQL workers.  Any existing row for the same subject/provider/type is
    authoritative regardless of discriminator or lifecycle state: bootstrap
    skips it verbatim and never repairs, activates, disables, or downgrades it.

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
    for provider, connection_type in _LEGACY_CONNECTION_ROOTS:
        pair = (provider.value, connection_type.value)
        if pair in existing_pairs:
            skipped_providers.add(provider)
            continue

        connection = IntegrationConnection(
            subject_id=subject_id,
            provider=provider.value,
            connection_type=connection_type.value,
            external_account_discriminator=LEGACY_ACCOUNT_DISCRIMINATOR,
            credential_ref=f"legacy_env:{provider.value}",
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
                    for provider, _connection_type in _LEGACY_CONNECTION_ROOTS
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
    "LegacyResourceRootsBootstrapError",
    "LegacyResourceRootsBootstrapResult",
    "LegacySubjectNotFoundError",
    "bootstrap_legacy_resource_roots",
]
