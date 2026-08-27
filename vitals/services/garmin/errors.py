"""Fail-closed errors for owned Garmin workflows."""


class GarminOwnershipError(Exception):
    """Base class for fail-closed owned Garmin ingestion failures."""


class GarminOwnershipValidationError(GarminOwnershipError):
    """The caller did not provide the strict ownership contract."""


class GarminConnectionInactiveError(GarminOwnershipValidationError):
    """A non-active provenance root cannot authorize fresh provider work."""


class GarminOwnershipConflictError(GarminOwnershipError):
    """A legacy-global normalized key belongs to another ownership scope."""


class GarminOwnershipAmbiguityError(GarminOwnershipConflictError):
    """More than one normalized row can represent the requested owned fact."""


class GarminRawPayloadInvariantError(GarminOwnershipError):
    """A reparse root does not carry internally consistent Garmin provenance."""


__all__ = [
    "GarminConnectionInactiveError",
    "GarminOwnershipAmbiguityError",
    "GarminOwnershipConflictError",
    "GarminOwnershipError",
    "GarminOwnershipValidationError",
    "GarminRawPayloadInvariantError",
]
