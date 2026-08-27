"""Types, status sets, and pure identity helpers for Garmin Weight export."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import Source
from vitals.models.garmin import (
    WEIGHT_EXPORT_CHECKING,
    WEIGHT_EXPORT_CONFLICT,
    WEIGHT_EXPORT_DELETE_CHECKING,
    WEIGHT_EXPORT_DELETE_FAILED,
    WEIGHT_EXPORT_DELETE_PENDING,
    WEIGHT_EXPORT_FAILED,
    WEIGHT_EXPORT_PENDING,
    WEIGHT_EXPORT_UNVERIFIED,
)
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine

SETTING_KEY = "garmin_weight_export_enabled"
ALERT_KEY = "garmin.weight_export"
ALERT_ENTITY = "weight"
WEIGHT_TOLERANCE_KG = 0.05
LOCAL_WEIGHT_TOLERANCE_KG = 1e-6
MAX_ERROR_LENGTH = 500
OPERATION_LOCK_TTL_SECONDS = 900
ELIGIBLE_SOURCES = (Source.MANUAL.value, Source.MCP.value, Source.BODY_SCAN.value)
EXPORT_INTENT_STATUSES = (
    WEIGHT_EXPORT_PENDING,
    WEIGHT_EXPORT_FAILED,
    WEIGHT_EXPORT_CONFLICT,
)
SUPERSEDEABLE_STATUSES = (*EXPORT_INTENT_STATUSES, WEIGHT_EXPORT_CHECKING)
DUE_STATUSES = (
    *EXPORT_INTENT_STATUSES,
    WEIGHT_EXPORT_CHECKING,
    WEIGHT_EXPORT_UNVERIFIED,
    WEIGHT_EXPORT_DELETE_PENDING,
    WEIGHT_EXPORT_DELETE_FAILED,
)
DELETE_STATUSES = (
    WEIGHT_EXPORT_DELETE_PENDING,
    WEIGHT_EXPORT_DELETE_FAILED,
    WEIGHT_EXPORT_DELETE_CHECKING,
)
ISSUE_STATUSES = (
    WEIGHT_EXPORT_FAILED,
    WEIGHT_EXPORT_CONFLICT,
    WEIGHT_EXPORT_UNVERIFIED,
    WEIGHT_EXPORT_DELETE_FAILED,
)
_PREPARED_EXPORT_SEAL = object()


class GarminWeightConflict(RuntimeError):
    """The remote day is non-empty but unsafe to mutate automatically."""


class GarminWeightExportOwnershipError(RuntimeError):
    """A scoped outbox operation cannot prove its complete ownership graph."""


class GarminWeightExportLegacyBridgeError(GarminWeightExportOwnershipError):
    """The fully-unowned outbox bridge cannot be proved safe.

    Its own type rather than the base, because the base covers a broken
    ownership graph — a real fault, and a 500 is the right answer to it. This
    one is the compatibility bridge declining in an installation with more than
    one person, which is a limit to state rather than a fault to report.
    """


class GarminWeightExportPreparedError(GarminWeightExportOwnershipError):
    """A scoped outbox capability is forged, stale, or used in another scope."""


class GarminWeightExportConnectionInactiveError(GarminWeightExportOwnershipError):
    """The Garmin account lifecycle cannot authorize the requested transition."""


@dataclass(frozen=True, slots=True)
class GarminWeightExportContext:
    """Immutable S+A+C authority for one Garmin account outbox."""

    identity: WriteIdentity
    integration_connection_id: uuid.UUID
    legacy_bridge: engine.LegacyConflictBridge = engine.LegacyConflictBridge.REJECT

    def __post_init__(self) -> None:
        if not isinstance(self.identity, WriteIdentity):
            raise TypeError("identity must be a WriteIdentity")
        if not isinstance(self.integration_connection_id, uuid.UUID):
            raise TypeError("integration_connection_id must be a UUID")
        if not isinstance(self.legacy_bridge, engine.LegacyConflictBridge):
            raise TypeError("legacy_bridge must be a LegacyConflictBridge")


class PreparedGarminWeightExport:
    """Opaque proof of governance -> advisory -> S/A -> Garmin C locking."""

    __slots__ = (
        "_context",
        "_historical",
        "_nested_transaction",
        "_seal",
        "_session",
        "_transaction",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise GarminWeightExportPreparedError(
            "prepared Garmin Weight export capabilities are service-issued only"
        )

    @classmethod
    def _issue(
        cls,
        *,
        session: AsyncSession,
        context: GarminWeightExportContext,
        historical: bool,
    ) -> "PreparedGarminWeightExport":
        token = object.__new__(cls)
        object.__setattr__(token, "_context", context)
        object.__setattr__(token, "_historical", historical)
        object.__setattr__(token, "_session", session)
        object.__setattr__(token, "_transaction", session.sync_session.get_transaction())
        object.__setattr__(
            token,
            "_nested_transaction",
            session.sync_session.get_nested_transaction(),
        )
        object.__setattr__(token, "_seal", _PREPARED_EXPORT_SEAL)
        return token

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedGarminWeightExport is immutable")

    @property
    def context(self) -> GarminWeightExportContext:
        return self._context

    @property
    def historical(self) -> bool:
        return self._historical


def _require_prepared_export(
    session: AsyncSession,
    prepared: PreparedGarminWeightExport,
    *,
    historical_ok: bool = True,
) -> GarminWeightExportContext:
    if (
        not isinstance(prepared, PreparedGarminWeightExport)
        or prepared._seal is not _PREPARED_EXPORT_SEAL
        or prepared._session is not session
    ):
        raise GarminWeightExportPreparedError(
            "prepared Garmin Weight export belongs to another session"
        )
    if session.sync_session.get_transaction() is not prepared._transaction:
        raise GarminWeightExportPreparedError(
            "prepared Garmin Weight export transaction is no longer active"
        )
    if session.sync_session.get_nested_transaction() is not prepared._nested_transaction:
        raise GarminWeightExportPreparedError(
            "prepared Garmin Weight export savepoint is no longer active"
        )
    if prepared.historical and not historical_ok:
        raise GarminWeightExportPreparedError(
            "historical capability cannot authorize fresh provider activity"
        )
    return prepared.context


@dataclass(frozen=True)
class RemoteWeighIn:
    sample_pk: Optional[str]
    weight_kg: Optional[float]
    timestamp_ms: Optional[int] = None
    source_type: Optional[str] = None
    sample_pk_exact: bool = False


@dataclass(frozen=True)
class OperationLease:
    """Identity of one committed network attempt."""

    row_id: int
    status: str
    attempts: int
    last_attempt_at: Optional[datetime]
    next_attempt_at: Optional[datetime]
    weight_log_id: Optional[int]
    weight_kg: float
    measured_at: datetime
    dispatch_timestamp_ms: Optional[int]
    remote_sample_pk: Optional[str]
    remote_weight_kg: Optional[float]
    remote_owned: bool


@dataclass(frozen=True)
class DispatchIdentity:
    """Durable identity of one non-idempotent POST attempt."""

    row_id: int
    measured_at: datetime
    dispatch_timestamp_ms: int
    remote_weight_kg: float


def _same_weight(left: Optional[float], right: Optional[float]) -> bool:
    return (
        left is not None
        and right is not None
        and math.isclose(left, right, rel_tol=0.0, abs_tol=WEIGHT_TOLERANCE_KG)
    )


def _same_local_weight(left: float, right: float) -> bool:
    """DB values are the desired truth; do not hide a small local correction."""
    return math.isclose(left, right, rel_tol=0.0, abs_tol=LOCAL_WEIGHT_TOLERANCE_KG)


def _dispatch_timestamp_ms(measured_at: datetime) -> int:
    zone = ZoneInfo(load_config().timezone)
    aware = (
        measured_at.replace(tzinfo=zone)
        if measured_at.tzinfo is None
        else measured_at.astimezone(zone)
    )
    # measured_at is deliberately millisecond-aligned; round instead of truncate
    # so binary floating-point cannot move a .XYZ marker back by one millisecond.
    return round(aware.timestamp() * 1000)
