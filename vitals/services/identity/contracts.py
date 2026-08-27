"""Shared identity errors used by the bounded identity service leaves."""


class IdentityServiceError(RuntimeError):
    """Base class for identity state and governance failures."""


class IdentityValidationError(ValueError):
    """An identity input cannot be represented safely."""


class PreIdentityCompatibilityError(IdentityServiceError):
    """A zero-subject compatibility operation cannot prove a safe snapshot."""


class UnsupportedIdentityDatabaseError(IdentityServiceError):
    """Identity governance was called on an unsupported database dialect."""


class UserNotFoundError(IdentityServiceError):
    """The requested identity does not exist."""


class IdentityStateConflictError(IdentityServiceError):
    """Persisted identity state is inconsistent with the requested mutation."""


class LastActivePlatformSuperadminError(IdentityServiceError):
    """A mutation would leave the platform without an active superadmin."""


class PasswordHashMismatchError(IdentityServiceError):
    """A compare-and-swap password update used a stale current hash."""


class PasswordHashDowngradeError(IdentityServiceError):
    """A password update attempted to lower the bcrypt work factor."""


__all__ = [
    "IdentityServiceError",
    "IdentityStateConflictError",
    "IdentityValidationError",
    "LastActivePlatformSuperadminError",
    "PasswordHashDowngradeError",
    "PasswordHashMismatchError",
    "PreIdentityCompatibilityError",
    "UnsupportedIdentityDatabaseError",
    "UserNotFoundError",
]
