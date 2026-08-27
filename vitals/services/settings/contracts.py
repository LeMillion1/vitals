"""Pure contracts and allowlist for scoped product settings."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from vitals.enums import IntegrationProvider
from vitals.models.scoped_settings import (
    IntegrationConnectionSetting,
    SubjectSetting,
    UserSetting,
)


class SettingScope(StrEnum):
    """Supported ownership boundaries for reviewed legacy settings."""

    USER = "user"
    SUBJECT = "subject"
    INTEGRATION_CONNECTION = "integration_connection"


class ScopedSettingKey(StrEnum):
    """Legacy keys whose destination scope has been explicitly reviewed."""

    UI_LANGUAGE = "ui_language"
    ENABLED_MODULES = "enabled_modules"
    CUSTOM_CHARTS = "custom_charts"
    GARMIN_WEIGHT_EXPORT_ENABLED = "garmin_weight_export_enabled"


@dataclass(frozen=True, slots=True)
class ScopedSettingRoute:
    """One allowlisted legacy-to-scoped persistence mapping."""

    scope: SettingScope
    legacy_key: str
    model: type[UserSetting | SubjectSetting | IntegrationConnectionSetting]
    scope_id_field: str
    required_provider: IntegrationProvider | None = None


SCOPED_SETTING_REGISTRY: Mapping[ScopedSettingKey, ScopedSettingRoute] = (
    MappingProxyType(
        {
            ScopedSettingKey.UI_LANGUAGE: ScopedSettingRoute(
                scope=SettingScope.USER,
                legacy_key="ui_language",
                model=UserSetting,
                scope_id_field="user_id",
            ),
            ScopedSettingKey.ENABLED_MODULES: ScopedSettingRoute(
                scope=SettingScope.SUBJECT,
                legacy_key="enabled_modules",
                model=SubjectSetting,
                scope_id_field="subject_id",
            ),
            ScopedSettingKey.CUSTOM_CHARTS: ScopedSettingRoute(
                scope=SettingScope.SUBJECT,
                legacy_key="custom_charts",
                model=SubjectSetting,
                scope_id_field="subject_id",
            ),
            ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED: ScopedSettingRoute(
                scope=SettingScope.INTEGRATION_CONNECTION,
                legacy_key="garmin_weight_export_enabled",
                model=IntegrationConnectionSetting,
                scope_id_field="integration_connection_id",
                required_provider=IntegrationProvider.GARMIN,
            ),
        }
    )
)

_SECRET_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "credential",
)


class ScopedSettingError(RuntimeError):
    """Base class for persisted scoped-setting state failures."""


class ScopedSettingValidationError(ValueError):
    """The requested key, scope, or identifier is not safe to use."""


class UnknownScopedSettingKeyError(ScopedSettingValidationError):
    """A key is not present in the reviewed migration registry."""


class ForbiddenScopedSettingKeyError(ScopedSettingValidationError):
    """A secret-like key was offered to a generic setting store."""


class ScopedSettingScopeMismatchError(ScopedSettingValidationError):
    """A known key was requested through the wrong ownership boundary."""


class ScopedSettingTargetNotFoundError(ScopedSettingError):
    """The requested user, subject, or connection does not exist."""


class ScopedSettingOwnershipError(ScopedSettingError):
    """A connection is not an allowed resource of the requested subject."""


class ScopedSettingDriftError(ScopedSettingError):
    """Legacy and already-created scoped values disagree during mirroring."""


class LegacyScopedSettingBridgeClosedError(ScopedSettingError):
    """Singleton legacy compatibility is unsafe for the persisted identity graph."""


@dataclass(frozen=True, slots=True)
class _ValidatedRequest:
    key: ScopedSettingKey
    route: ScopedSettingRoute
    scope_id: uuid.UUID
    expected_subject_id: uuid.UUID | None


def _as_scope(scope: SettingScope | str) -> SettingScope:
    try:
        return SettingScope(scope)
    except (TypeError, ValueError) as exc:
        raise ScopedSettingValidationError(
            f"unknown scoped-setting scope: {scope!r}"
        ) from exc


def _as_key(key: ScopedSettingKey | str) -> ScopedSettingKey:
    if not isinstance(key, str):
        raise UnknownScopedSettingKeyError("scoped-setting key must be a string")
    normalized = key.casefold()
    if any(marker in normalized for marker in _SECRET_KEY_MARKERS):
        raise ForbiddenScopedSettingKeyError(
            "secret-like keys are forbidden in generic scoped settings"
        )
    try:
        return ScopedSettingKey(key)
    except ValueError as exc:
        raise UnknownScopedSettingKeyError(
            f"unknown scoped-setting key: {key!r}"
        ) from exc


def _required_uuid(value: object, *, field: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise ScopedSettingValidationError(f"{field} must be a non-zero UUID")
    return value


def _optional_uuid(value: object, *, field: str) -> uuid.UUID | None:
    if value is None:
        return None
    return _required_uuid(value, field=field)


_SCOPE_ID_FIELD = {
    SettingScope.USER: "user id",
    SettingScope.SUBJECT: "health subject id",
    SettingScope.INTEGRATION_CONNECTION: "integration connection id",
}


def _validate_request(
    *,
    scope: SettingScope | str,
    key: ScopedSettingKey | str,
    scope_id: uuid.UUID,
    expected_subject_id: uuid.UUID | None,
) -> _ValidatedRequest:
    """Resolve one settings request against the key's registered route.

    ``scope`` names which kind of thing owns the value and ``scope_id`` is that
    thing's id — one mandatory pair, not three optional ones. The three-id
    spelling this replaced let a caller reach a function with every id left out
    and be told so only at runtime; there was no way to write the call without a
    scope, but also no way to see from the signature that one was required.

    ``expected_subject_id`` is not a scope. It applies to connection-scoped keys
    only and asserts which person the connection belongs to, so a caller holding
    a connection id from elsewhere cannot read settings off somebody else's
    integration.
    """

    parsed_scope = _as_scope(scope)
    parsed_key = _as_key(key)
    route = SCOPED_SETTING_REGISTRY[parsed_key]
    if route.scope is not parsed_scope:
        raise ScopedSettingScopeMismatchError(
            f"{parsed_key.value!r} belongs to {route.scope.value!r}, "
            f"not {parsed_scope.value!r}"
        )

    resolved_id = _required_uuid(scope_id, field=_SCOPE_ID_FIELD[parsed_scope])
    if parsed_scope is SettingScope.INTEGRATION_CONNECTION:
        resolved_subject_id = _optional_uuid(
            expected_subject_id, field="expected_subject_id"
        )
    else:
        if expected_subject_id is not None:
            raise ScopedSettingScopeMismatchError(
                "expected_subject_id applies to integration-connection settings "
                "only; a user or subject setting is already scoped by its own id"
            )
        resolved_subject_id = None

    return _ValidatedRequest(
        key=parsed_key,
        route=route,
        scope_id=resolved_id,
        expected_subject_id=resolved_subject_id,
    )


__all__ = [
    "SCOPED_SETTING_REGISTRY",
    "ForbiddenScopedSettingKeyError",
    "LegacyScopedSettingBridgeClosedError",
    "ScopedSettingDriftError",
    "ScopedSettingError",
    "ScopedSettingKey",
    "ScopedSettingOwnershipError",
    "ScopedSettingRoute",
    "ScopedSettingScopeMismatchError",
    "ScopedSettingTargetNotFoundError",
    "ScopedSettingValidationError",
    "SettingScope",
    "UnknownScopedSettingKeyError",
]
