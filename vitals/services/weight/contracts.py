"""Opaque capabilities and immutable handoffs for Weight workflows."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine


class GarminWeightExportContextProtocol(Protocol):
    """Structural Garmin outbox authority exposed to the Weight capability."""

    identity: WriteIdentity
    integration_connection_id: uuid.UUID
    legacy_bridge: engine.LegacyConflictBridge


class PreparedGarminWeightExportProtocol(Protocol):
    """Opaque prepared Garmin outbox capability used by Weight orchestration."""

    @property
    def context(self) -> GarminWeightExportContextProtocol: ...


class WeightOwnershipError(ValueError):
    """A weight-domain row cannot be used inside the requested subject scope."""


class BodyMeasurementDateOccupiedError(WeightOwnershipError):
    """This subject already has a measurement row on the destination date."""


class ProgressPhotoOwnershipError(WeightOwnershipError):
    """A progress-photo fact or its private-file graph is not authoritative."""


@dataclass(frozen=True, slots=True)
class ProgressPhotoDeletion:
    """Immutable handoff for post-commit physical-file cleanup."""

    file_key: str
    file_asset_id: uuid.UUID | None


_PREPARED_WEIGHT_WRITE_SEAL = object()
_ORIGIN_ACTOR_UNSET = object()


class PreparedWeightWrite:
    """Opaque proof that Weight's governance/advisory order was established.

    The generic conflict capability proves identity, transaction, and subject
    locks. Weight additionally has to prove that the Garmin outbox advisory was
    acquired *before* those subject locks. Only :func:`prepare_weight_write`
    issues this wrapper.
    """

    __slots__ = ("_garmin_export", "_prepared", "_seal", "_session")

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise engine.ConflictPreparedWriteError(
            "prepared weight writes are issued only by prepare_weight_write"
        )

    @classmethod
    def _issue(
        cls,
        *,
        session: AsyncSession,
        prepared: engine.PreparedConflictWrite,
        garmin_export: PreparedGarminWeightExportProtocol | None,
    ) -> PreparedWeightWrite:
        token = object.__new__(cls)
        object.__setattr__(token, "_prepared", prepared)
        object.__setattr__(token, "_session", session)
        object.__setattr__(token, "_seal", _PREPARED_WEIGHT_WRITE_SEAL)
        object.__setattr__(token, "_garmin_export", garmin_export)
        return token

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedWeightWrite is immutable")

    @property
    def context(self) -> engine.ConflictWriteContext:
        return self._prepared.context

    @property
    def conflict_write(self) -> engine.PreparedConflictWrite:
        return self._prepared

    @property
    def garmin_weight_export(self) -> PreparedGarminWeightExportProtocol | None:
        """Prepared destination outbox, distinct from the Weight origin roots."""

        return self._garmin_export


__all__ = [
    "BodyMeasurementDateOccupiedError",
    "GarminWeightExportContextProtocol",
    "PreparedWeightWrite",
    "PreparedGarminWeightExportProtocol",
    "ProgressPhotoDeletion",
    "ProgressPhotoOwnershipError",
    "WeightOwnershipError",
]
