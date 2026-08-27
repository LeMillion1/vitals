"""Typed failures and ownership input validation for raw data-lake writes."""
from __future__ import annotations

import uuid

from vitals.ownership import WriteIdentity


class RawPayloadServiceError(Exception):
    """Base class for fail-closed owned raw-payload failures."""


class RawPayloadValidationError(RawPayloadServiceError):
    """An ownership input does not use the strict typed contract."""


class RawPayloadReferenceError(RawPayloadServiceError):
    """A connection or file root cannot be used in this subject scope."""

    def __init__(self, field_name: str, reference_id: uuid.UUID, detail: str) -> None:
        self.field_name = field_name
        self.reference_id = reference_id
        super().__init__(f"{field_name} {detail}")


class RawPayloadReferenceNotFoundError(RawPayloadReferenceError):
    """A requested connection or file root does not exist."""


class RawPayloadReferenceOwnershipError(RawPayloadReferenceError):
    """A requested connection or file root belongs to another subject."""


class RawPayloadReferenceLifecycleError(RawPayloadReferenceError):
    """A requested connection or file root cannot authorize ingestion."""


class RawPayloadConflictError(RawPayloadServiceError):
    """A historical ownership reference conflicts with the requested write."""


class RawPayloadAmbiguityError(RawPayloadConflictError):
    """More than one row matches a scoped lookup or legacy adoption path."""


def validate_owned_inputs(
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID | None,
    file_asset_id: uuid.UUID | None,
) -> None:
    if not isinstance(identity, WriteIdentity):
        raise RawPayloadValidationError("identity must be a WriteIdentity")
    for field_name, value in (
        ("integration_connection_id", integration_connection_id),
        ("file_asset_id", file_asset_id),
    ):
        if value is not None and not isinstance(value, uuid.UUID):
            raise RawPayloadValidationError(f"{field_name} must be a UUID or None")


__all__ = [
    "RawPayloadAmbiguityError",
    "RawPayloadConflictError",
    "RawPayloadReferenceError",
    "RawPayloadReferenceLifecycleError",
    "RawPayloadReferenceNotFoundError",
    "RawPayloadReferenceOwnershipError",
    "RawPayloadServiceError",
    "RawPayloadValidationError",
    "validate_owned_inputs",
]
