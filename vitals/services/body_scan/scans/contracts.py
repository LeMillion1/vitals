"""Body-scan invariants shared by persistence workflows."""

from __future__ import annotations

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.body_scan import BodyScan
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.weight import governance as weight_governance
from vitals.services.weight.contracts import PreparedWeightWrite

VISCERAL_ALERT_KEY = "body_comp.visceral_high"
PHASE_ALERT_KEY = "body_comp.phase_low"


class BodyScanOwnershipError(ValueError):
    """A scan, metric, or provenance root is outside the requested scope."""


class BodyScanRawAlreadyNormalizedError(BodyScanOwnershipError):
    """One immutable raw payload already owns a normalized BodyScan fact."""


def require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity | None,
    prepared: PreparedWeightWrite | None,
) -> engine.ConflictWriteContext | None:
    if identity is None and prepared is None:
        return None
    if identity is None or prepared is None:
        raise engine.ConflictPreparedWriteError(
            "scoped body-scan writes require identity and a prepared Weight write"
        )
    return weight_governance.require_prepared_weight_identity(
        session,
        prepared=prepared,
        identity=identity,
    )


def require_evaluation_date(
    context: engine.ConflictWriteContext,
    on_date: date,
) -> None:
    if context.evaluation_date != on_date:
        raise engine.ConflictPreparedWriteError(
            "body-scan date does not match prepared Weight capability"
        )


def scan_entity_key(scan: BodyScan) -> str:
    return f"body_scan:{scan.id}"
