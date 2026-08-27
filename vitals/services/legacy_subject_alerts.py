"""Aggregate subject-visible alerts for the registration-disabled owner bridge.

This module is a compatibility boundary, not a second alert implementation. It
validates one authoritative :class:`LegacyOwnershipContext`, expands it to the
health scope plus every current and retired provider connection for the same
subject, then delegates all reads and mutations to ``alerts_service``.

Reusable functions flush through the scoped service but never commit. Registration
must remain disabled while the fully-unowned bridge and global alert unique exist.
"""

from __future__ import annotations

from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.alerts import lifecycle as alerts_service_lifecycle

from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationProvider,
)
from vitals.models.identity import HealthSubject
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.services.alerts.contracts import (
    HealthAlertContext,
    LegacyAlertBridge,
    ProviderAlertContext,
)
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.legacy_ownership import LegacyOwnershipContext


class LegacySubjectAlertsError(Exception):
    """Base class for fail-closed legacy alert aggregation failures."""


class LegacySubjectAlertsContextError(LegacySubjectAlertsError):
    """The supplied ownership snapshot is incomplete or not owner/system-bound."""


class LegacySubjectAlertsConnectionError(LegacySubjectAlertsError):
    """The persisted provider roots no longer match the authoritative snapshot."""


_CURRENT_CONNECTION_STATUSES = frozenset(
    {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
        IntegrationConnectionStatus.DISABLED.value,
    }
)
_HISTORICAL_CONNECTION_STATUSES = frozenset(
    {
        *_CURRENT_CONNECTION_STATUSES,
        IntegrationConnectionStatus.RETIRED.value,
    }
)


@dataclass(frozen=True, slots=True)
class _ProviderScope:
    context: ProviderAlertContext
    legacy_bridge: LegacyAlertBridge


@dataclass(frozen=True, slots=True)
class _AggregateScopes:
    health: HealthAlertContext
    providers: tuple[_ProviderScope, ...]


def _validate_ownership(ownership: LegacyOwnershipContext) -> None:
    if not isinstance(ownership, LegacyOwnershipContext):
        raise LegacySubjectAlertsContextError(
            "ownership must be a LegacyOwnershipContext"
        )
    if ownership.actor_user_id not in {None, ownership.owner_user_id}:
        raise LegacySubjectAlertsContextError(
            "legacy alert actor must be the owner or system"
        )
    expected_providers = set(IntegrationProvider)
    if set(ownership.connection_ids) != expected_providers:
        raise LegacySubjectAlertsContextError(
            "legacy alert aggregation requires all current provider roots"
        )
    current_ids = tuple(ownership.connection_ids.values())
    if len(set(current_ids)) != len(current_ids):
        raise LegacySubjectAlertsContextError(
            "current provider roots must use distinct connection IDs"
        )


def _validate_current_connection(
    row: IntegrationConnection | None,
    *,
    ownership: LegacyOwnershipContext,
    provider: IntegrationProvider,
) -> IntegrationConnection:
    if row is None:
        raise LegacySubjectAlertsConnectionError(
            "an authoritative current connection no longer exists"
        )
    if row.subject_id != ownership.subject_id:
        raise LegacySubjectAlertsConnectionError(
            "an authoritative current connection belongs to another subject"
        )
    if row.provider != provider.value:
        raise LegacySubjectAlertsConnectionError(
            "an authoritative current connection has the wrong provider"
        )
    expected_type = alerts_service_contracts.PROVIDER_ALERT_CONNECTION_TYPES[provider]
    if row.connection_type != expected_type.value:
        raise LegacySubjectAlertsConnectionError(
            "an authoritative current connection has the wrong type"
        )
    if row.status not in _CURRENT_CONNECTION_STATUSES:
        raise LegacySubjectAlertsConnectionError(
            "an authoritative current connection is not a usable current root"
        )
    return row


async def _aggregate_scopes(
    session: AsyncSession,
    ownership: LegacyOwnershipContext,
) -> _AggregateScopes:
    _validate_ownership(ownership)
    with session.no_autoflush:
        # Freeze the authoritative connection roster before reading it. Scoped
        # alert calls later in the same transaction reacquire this xact lock
        # reentrantly, so a rotation cannot redirect legacy adoption to the old
        # retired root or make the new current root disappear mid-operation.
        await acquire_identity_governance_lock(session)
        persisted_owner_id = await session.scalar(
            select(HealthSubject.owner_user_id).where(
                HealthSubject.id == ownership.subject_id
            )
        )
        if persisted_owner_id is None:
            raise LegacySubjectAlertsContextError(
                "the authoritative health subject no longer exists"
            )
        if persisted_owner_id != ownership.owner_user_id:
            raise LegacySubjectAlertsContextError(
                "the authoritative owner no longer matches the health subject"
            )
        rows = list(
            await session.scalars(
                select(IntegrationConnection)
                .where(IntegrationConnection.subject_id == ownership.subject_id)
                .order_by(
                    IntegrationConnection.provider,
                    IntegrationConnection.id,
                )
                .execution_options(populate_existing=True)
            )
        )
    by_id = {row.id: row for row in rows}
    identity = ownership.write_identity
    provider_scopes: list[_ProviderScope] = []

    for provider in IntegrationProvider:
        current_id = ownership.connection_ids[provider]
        current = _validate_current_connection(
            by_id.get(current_id),
            ownership=ownership,
            provider=provider,
        )
        expected_type = alerts_service_contracts.PROVIDER_ALERT_CONNECTION_TYPES[provider]
        compatible = [
            row
            for row in rows
            if row.provider == provider.value
            and row.connection_type == expected_type.value
            and row.status in _HISTORICAL_CONNECTION_STATUSES
        ]
        other_current = [
            row
            for row in compatible
            if row.id != current.id and row.status in _CURRENT_CONNECTION_STATUSES
        ]
        if other_current:
            raise LegacySubjectAlertsConnectionError(
                "provider has more than one non-retired current root"
            )

        provider_scopes.append(
            _ProviderScope(
                context=ProviderAlertContext(
                    identity=identity,
                    provider=provider,
                    integration_connection_id=current.id,
                ),
                legacy_bridge=LegacyAlertBridge.FULLY_UNOWNED,
            )
        )
        retired = sorted(
            (
                row
                for row in compatible
                if row.id != current.id
                and row.status == IntegrationConnectionStatus.RETIRED.value
            ),
            key=lambda row: row.id.hex,
        )
        provider_scopes.extend(
            _ProviderScope(
                context=ProviderAlertContext(
                    identity=identity,
                    provider=provider,
                    integration_connection_id=row.id,
                ),
                legacy_bridge=LegacyAlertBridge.REJECT,
            )
            for row in retired
        )

    return _AggregateScopes(
        health=HealthAlertContext(identity=identity),
        providers=tuple(provider_scopes),
    )


def _dedupe_newest(rows: Sequence[SystemAlert]) -> list[SystemAlert]:
    by_id = {row.id: row for row in rows}
    return sorted(
        by_id.values(),
        key=lambda row: (row.created_at, row.id),
        reverse=True,
    )


async def list_active(
    session: AsyncSession,
    *,
    ownership: LegacyOwnershipContext,
    domain: Domain | None = None,
) -> Sequence[SystemAlert]:
    """List active subject alerts, optionally restricted to one exact domain."""

    scopes = await _aggregate_scopes(session, ownership)
    rows = list(
        await alerts_service_lifecycle.list_active_scoped(
            session,
            context=scopes.health,
            domain=domain,
            legacy_bridge=LegacyAlertBridge.FULLY_UNOWNED,
        )
    )
    for scope in scopes.providers:
        rows.extend(
            await alerts_service_lifecycle.list_active_scoped(
                session,
                context=scope.context,
                domain=domain,
                legacy_bridge=scope.legacy_bridge,
            )
        )
    return _dedupe_newest(rows)


async def resolve(
    session: AsyncSession,
    alert_id: int,
    *,
    ownership: LegacyOwnershipContext,
) -> SystemAlert | None:
    """Resolve one visible alert; missing, foreign, and platform IDs return None."""

    scopes = await _aggregate_scopes(session, ownership)
    row = await alerts_service_lifecycle.resolve_scoped_alert(
        session,
        alert_id,
        context=scopes.health,
        legacy_bridge=LegacyAlertBridge.FULLY_UNOWNED,
    )
    if row is not None:
        return row
    for scope in scopes.providers:
        row = await alerts_service_lifecycle.resolve_scoped_alert(
            session,
            alert_id,
            context=scope.context,
            legacy_bridge=scope.legacy_bridge,
        )
        if row is not None:
            return row
    return None


async def override(
    session: AsyncSession,
    alert_id: int,
    *,
    ownership: LegacyOwnershipContext,
) -> SystemAlert | None:
    """Override one owner-visible alert; system contexts remain human-ineligible."""

    scopes = await _aggregate_scopes(session, ownership)
    row = await alerts_service_lifecycle.override_scoped_alert(
        session,
        alert_id,
        context=scopes.health,
        legacy_bridge=LegacyAlertBridge.FULLY_UNOWNED,
    )
    if row is not None:
        return row
    for scope in scopes.providers:
        row = await alerts_service_lifecycle.override_scoped_alert(
            session,
            alert_id,
            context=scope.context,
            legacy_bridge=scope.legacy_bridge,
        )
        if row is not None:
            return row
    return None


async def resolve_all(
    session: AsyncSession,
    *,
    ownership: LegacyOwnershipContext,
    domain: Domain | None = None,
) -> int:
    """Resolve visible health/provider alerts, optionally in one exact domain."""

    scopes = await _aggregate_scopes(session, ownership)
    changed = await alerts_service_lifecycle.resolve_all_scoped(
        session,
        context=scopes.health,
        domain=domain,
        legacy_bridge=LegacyAlertBridge.FULLY_UNOWNED,
    )
    for scope in scopes.providers:
        changed += await alerts_service_lifecycle.resolve_all_scoped(
            session,
            context=scope.context,
            domain=domain,
            legacy_bridge=scope.legacy_bridge,
        )
    return changed


__all__ = [
    "LegacySubjectAlertsConnectionError",
    "LegacySubjectAlertsContextError",
    "LegacySubjectAlertsError",
    "list_active",
    "override",
    "resolve",
    "resolve_all",
]
