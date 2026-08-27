"""Ownership validation and lock ordering for Garmin workflows."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
)
from vitals.models.garmin import GarminActivity, GarminDaily
from vitals.models.identity import HealthSubject
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services.garmin.errors import (
    GarminConnectionInactiveError,
    GarminOwnershipAmbiguityError,
    GarminOwnershipConflictError,
    GarminOwnershipValidationError,
)


def _validate_owned_context(
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> None:
    if not isinstance(identity, WriteIdentity):
        raise GarminOwnershipValidationError("identity must be a WriteIdentity")
    if not isinstance(integration_connection_id, uuid.UUID):
        raise GarminOwnershipValidationError(
            "integration_connection_id must be a UUID"
        )


async def _load_owned_garmin_connection(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    allow_retired: bool = False,
    for_update: bool = False,
) -> IntegrationConnection:
    """Validate one subject-owned Garmin account provenance root."""

    _validate_owned_context(identity, integration_connection_id)
    statement = select(IntegrationConnection).where(
        IntegrationConnection.id == integration_connection_id
    )
    if for_update:
        statement = statement.with_for_update()
    with session.no_autoflush:
        connection = await session.scalar(statement)
    if connection is None:
        raise GarminOwnershipValidationError(
            "integration_connection_id does not exist"
        )
    if connection.subject_id != identity.subject_id:
        raise GarminOwnershipConflictError(
            "Garmin connection belongs to another subject"
        )
    if (
        connection.provider != IntegrationProvider.GARMIN.value
        or connection.connection_type != IntegrationConnectionType.ACCOUNT.value
    ):
        raise GarminOwnershipValidationError(
            "integration_connection_id is not a Garmin account connection"
        )
    known_statuses = {status.value for status in IntegrationConnectionStatus}
    if connection.status not in known_statuses:
        raise GarminOwnershipValidationError(
            "Garmin connection has an unknown lifecycle state"
        )
    allowed_statuses = {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }
    if allow_retired:
        allowed_statuses.update(
            {
                IntegrationConnectionStatus.DISABLED.value,
                IntegrationConnectionStatus.RETIRED.value,
            }
        )
    if connection.status not in allowed_statuses:
        raise GarminConnectionInactiveError(
            f"Garmin connection status {connection.status!r} cannot authorize "
            "this operation"
        )
    return connection


async def _lock_owned_garmin_scope(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    allow_retired: bool = False,
) -> IntegrationConnection:
    """Lock Subject then Connection, after the caller's governance lock."""

    _validate_owned_context(identity, integration_connection_id)
    with session.no_autoflush:
        subject_id = await session.scalar(
            select(HealthSubject.id)
            .where(HealthSubject.id == identity.subject_id)
            .with_for_update()
        )
    if subject_id is None:
        raise GarminOwnershipValidationError("identity subject does not exist")
    return await _load_owned_garmin_connection(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_retired=allow_retired,
        for_update=True,
    )


async def _require_legacy_adoption_subject(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> None:
    """Keep fully unscoped adoption behind the single-subject invariant."""

    subject_ids = list(
        await session.scalars(
            select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
        )
    )
    if subject_ids != [subject_id]:
        raise GarminOwnershipConflictError(
            "unscoped legacy Garmin row cannot be adopted after multi-subject "
            "activation"
        )


def _row_scope_is_compatible(
    row: GarminDaily | GarminActivity,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> bool:
    return (
        row.subject_id in {None, identity.subject_id}
        and row.integration_connection_id in {None, integration_connection_id}
    )


async def _owned_single_row_candidate(
    session: AsyncSession,
    *,
    model: type[GarminDaily] | type[GarminActivity],
    natural_clause: Any,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    key_label: str,
) -> GarminDaily | GarminActivity | None:
    """Lock one exact connection-scoped row or compatible legacy candidate."""

    rows = list(
        await session.scalars(
            select(model)
            .where(
                natural_clause,
                or_(
                    model.integration_connection_id == integration_connection_id,
                    model.integration_connection_id.is_(None),
                ),
            )
            .with_for_update()
        )
    )
    if len(rows) > 1:
        raise GarminOwnershipAmbiguityError(
            f"multiple Garmin rows match scoped key {key_label}"
        )
    if not rows:
        return None
    row = rows[0]
    if not _row_scope_is_compatible(
        row,
        identity=identity,
        integration_connection_id=integration_connection_id,
    ):
        raise GarminOwnershipConflictError(
            f"Garmin row for {key_label} belongs to another ownership scope"
        )
    if row.subject_id is None and row.integration_connection_id is None:
        await _require_legacy_adoption_subject(
            session,
            subject_id=identity.subject_id,
        )
    return row


def _adopt_owned_row(
    row: GarminDaily | GarminActivity,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> None:
    """Fill nullable legacy roots without rewriting historical actor identity."""

    if row.subject_id is None:
        row.subject_id = identity.subject_id
    if row.integration_connection_id is None:
        row.integration_connection_id = integration_connection_id
