"""Immutable contracts and fail-closed errors for legacy tenancy resolution."""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from vitals.access import AccessContext
from vitals.enums import IntegrationProvider
from vitals.ownership import WriteIdentity


class LegacyOwnershipError(Exception):
    """Base class for fail-closed legacy ownership resolution errors."""


class LegacyOwnershipValidationError(LegacyOwnershipError):
    """Resolver input is invalid or does not use the frozen enum contract."""


class LegacySubjectResolutionError(LegacyOwnershipError):
    """There is not exactly one local health subject."""


class NoPersonalRecordError(LegacySubjectResolutionError):
    """The signed-in account keeps no health record of its own."""


class LegacyOwnerResolutionError(LegacyOwnershipError):
    """The sole subject does not have a resolvable active owner identity."""


class LegacyActorMismatchError(LegacyOwnershipError):
    """A human actor does not match the subject's owner identity."""


class LegacyConnectionResolutionError(LegacyOwnershipError):
    """A requested provider does not have one usable provenance root."""

    def __init__(self, provider: IntegrationProvider, detail: str) -> None:
        self.provider = provider
        super().__init__(f"legacy {provider.value} connection {detail}")


class LegacyConnectionMissingError(LegacyConnectionResolutionError):
    """No connection exists for the provider's frozen legacy type."""


class LegacyConnectionAmbiguousError(LegacyConnectionResolutionError):
    """More than one non-retired connection matches the provider/type pair."""


class LegacyConnectionRetiredError(LegacyConnectionResolutionError):
    """The provider/type pair exists only as retired provenance."""


class LegacyConnectionStateError(LegacyConnectionResolutionError):
    """A matching connection has an unknown persisted lifecycle state."""


class LegacyConnectionNotResolvedError(LegacyConnectionResolutionError):
    """A caller requested an ID that was not part of this resolution."""


@dataclass(frozen=True, slots=True)
class LegacyOwnershipContext:
    """Immutable ownership roots for one legacy operation."""

    subject_id: uuid.UUID
    owner_user_id: uuid.UUID
    actor_user_id: uuid.UUID | None
    connection_ids: Mapping[IntegrationProvider, uuid.UUID]
    access: AccessContext | None = None

    def __post_init__(self) -> None:
        for field_name in ("subject_id", "owner_user_id"):
            if not isinstance(getattr(self, field_name), uuid.UUID):
                raise LegacyOwnershipValidationError(f"{field_name} must be a UUID")
        if self.actor_user_id is not None and not isinstance(
            self.actor_user_id, uuid.UUID
        ):
            raise LegacyOwnershipValidationError("actor_user_id must be a UUID or None")

        copied: dict[IntegrationProvider, uuid.UUID] = {}
        try:
            items = self.connection_ids.items()
        except AttributeError as exc:
            raise LegacyOwnershipValidationError(
                "connection_ids must be a mapping"
            ) from exc
        for provider, connection_id in items:
            if not isinstance(provider, IntegrationProvider):
                raise LegacyOwnershipValidationError(
                    "connection_ids keys must be IntegrationProvider members"
                )
            if not isinstance(connection_id, uuid.UUID):
                raise LegacyOwnershipValidationError(
                    "connection_ids values must be UUIDs"
                )
            copied[provider] = connection_id
        object.__setattr__(self, "connection_ids", MappingProxyType(copied))

    @property
    def write_identity(self) -> WriteIdentity:
        return WriteIdentity(
            subject_id=self.subject_id,
            actor_user_id=self.actor_user_id,
        )

    def owner_action(self) -> WriteIdentity:
        return WriteIdentity(
            subject_id=self.subject_id,
            actor_user_id=self.owner_user_id,
        )

    def system_action(self) -> WriteIdentity:
        return WriteIdentity(subject_id=self.subject_id, actor_user_id=None)

    def connection_id(self, provider: IntegrationProvider) -> uuid.UUID:
        if not isinstance(provider, IntegrationProvider):
            raise LegacyOwnershipValidationError(
                "provider must be an IntegrationProvider member"
            )
        try:
            return self.connection_ids[provider]
        except KeyError:
            raise LegacyConnectionNotResolvedError(
                provider, "was not requested"
            ) from None


__all__ = [
    "LegacyActorMismatchError",
    "LegacyConnectionAmbiguousError",
    "LegacyConnectionMissingError",
    "LegacyConnectionNotResolvedError",
    "LegacyConnectionResolutionError",
    "LegacyConnectionRetiredError",
    "LegacyConnectionStateError",
    "LegacyOwnerResolutionError",
    "LegacyOwnershipContext",
    "LegacyOwnershipError",
    "LegacyOwnershipValidationError",
    "LegacySubjectResolutionError",
    "NoPersonalRecordError",
]
