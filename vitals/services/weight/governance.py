"""Prepared Weight capabilities and canonical mutation lock ordering."""
from __future__ import annotations

from datetime import date as date_type

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.identity.governance import acquire_identity_governance_lock

from .contracts import (
    GarminWeightExportContextProtocol,
    PreparedWeightWrite,
    _PREPARED_WEIGHT_WRITE_SEAL,
)


async def prepare_weight_write(
    session: AsyncSession,
    *,
    context: engine.ConflictWriteContext,
    garmin_weight_export_context: GarminWeightExportContextProtocol | None = None,
) -> PreparedWeightWrite:
    """Prepare a scoped Weight mutation in the canonical lock order.

    Identity governance precedes the installation-wide Garmin outbox advisory;
    the generic conflict preparation then locks the subject and actor roots.
    """

    from vitals.services.garmin_weight import contracts as garmin_weight_contracts
    from vitals.services.garmin_weight import outbox as garmin_weight_outbox

    await acquire_identity_governance_lock(session)
    await garmin_weight_outbox.lock_active_weight_change(session)
    prepared = await engine.prepare_scoped_write(
        session,
        context=context,
    )
    prepared_export = None
    if garmin_weight_export_context is not None:
        if garmin_weight_export_context.identity != context.identity:
            raise engine.ConflictPreparedWriteError(
                "Garmin Weight export identity does not match Weight identity"
            )
        if garmin_weight_export_context.legacy_bridge is not context.legacy_bridge:
            raise engine.ConflictPreparedWriteError(
                "Garmin Weight export bridge does not match Weight bridge"
            )
        try:
            prepared_export = await garmin_weight_outbox.prepare_scoped_export(
                session,
                context=garmin_weight_export_context,
            )
        except garmin_weight_contracts.GarminWeightExportConnectionInactiveError:
            # Garmin is an optional destination. A disabled/retired account must
            # stop outbox projection, not block the local health correction.
            prepared_export = None
    return PreparedWeightWrite._issue(
        session=session,
        prepared=prepared,
        garmin_export=prepared_export,
    )


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity | None,
    prepared: PreparedWeightWrite | None,
) -> engine.ConflictWriteContext | None:
    if identity is None or prepared is None:
        raise engine.ConflictPreparedWriteError(
            "scoped weight writes require identity and a prepared weight write"
        )
    if (
        not isinstance(prepared, PreparedWeightWrite)
        or prepared._seal is not _PREPARED_WEIGHT_WRITE_SEAL
        or prepared._session is not session
    ):
        raise engine.ConflictPreparedWriteError(
            "prepared weight write was not issued for this session"
        )
    return engine.require_prepared_identity(
        session,
        prepared=prepared.conflict_write,
        identity=identity,
    )


def require_prepared_weight_identity(
    session: AsyncSession,
    *,
    prepared: PreparedWeightWrite,
    identity: WriteIdentity,
) -> engine.ConflictWriteContext:
    """Validate an issued Weight capability before another service locks roots."""

    return _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared,
    )


def require_aux_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: engine.PreparedConflictWrite,
) -> engine.ConflictWriteContext:
    """Prove an auxiliary Weight write's subject and conflict decision."""

    if identity is None or prepared is None:
        raise engine.ConflictPreparedWriteError(
            "scoped auxiliary weight writes require identity and a prepared "
            "conflict write"
        )
    return engine.require_prepared_identity(
        session,
        prepared=prepared,
        identity=identity,
    )


def require_evaluation_date(
    context: engine.ConflictWriteContext,
    on_date: date_type,
) -> None:
    if context.evaluation_date != on_date:
        raise engine.ConflictPreparedWriteError(
            "weight write date does not match prepared conflict evaluation date"
        )


# Temporary private aliases for the facade and sibling migration.
_require_aux_prepared_write = require_aux_prepared_write
_require_evaluation_date = require_evaluation_date
