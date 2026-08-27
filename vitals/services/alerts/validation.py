"""Validation boundary for system alerts."""

from __future__ import annotations

import re
import unicodedata
import uuid


from vitals.enums import (
    Domain,
    IntegrationProvider,
    Severity,
)

from vitals.services.alerts.contracts import (
    AlertValidationError,
    AlertContextError,
    AlertPlatformNamespaceError,
    LegacyAlertBridge,
    HealthAlertContext,
    ProviderAlertContext,
    PlatformAlertContext,
    AlertContext,
    HEALTH_ALERT_KEYS,
    PROVIDER_ALERT_KEYS,
    PLATFORM_ALERT_KEYS,
    _MAX_ALERT_KEY_LENGTH,
    _MAX_ENTITY_REF_LENGTH,
)


def _require_context(context: AlertContext) -> None:
    if not isinstance(
        context,
        (HealthAlertContext, ProviderAlertContext, PlatformAlertContext),
    ):
        raise AlertContextError("context must be a typed alert context")


def _require_bridge(value: LegacyAlertBridge) -> None:
    if not isinstance(value, LegacyAlertBridge):
        raise AlertValidationError("legacy_bridge must be a LegacyAlertBridge")


def _has_forbidden_control(value: str) -> bool:
    return any(
        unicodedata.category(char).startswith("C")
        for char in value
        if char not in {"\n", "\r", "\t"}
    )


def _require_key(value: str) -> None:
    if not isinstance(value, str):
        raise AlertValidationError("alert_key must be a string")
    if not value or value != value.strip():
        raise AlertValidationError("alert_key must be non-blank without outer whitespace")
    if len(value) > _MAX_ALERT_KEY_LENGTH:
        raise AlertValidationError("alert_key is too long")
    if _has_forbidden_control(value):
        raise AlertValidationError("alert_key must not contain control characters")


def _require_entity_ref(value: str) -> None:
    if not isinstance(value, str):
        raise AlertValidationError("entity_ref must be a string")
    if value and value != value.strip():
        raise AlertValidationError("entity_ref must not contain outer whitespace when non-empty")
    if len(value) > _MAX_ENTITY_REF_LENGTH:
        raise AlertValidationError("entity_ref is too long")
    if _has_forbidden_control(value):
        raise AlertValidationError("entity_ref must not contain control characters")


def _require_message(value: str) -> None:
    if not isinstance(value, str):
        raise AlertValidationError("message must be a string")
    if not value.strip():
        raise AlertValidationError("message must not be blank")
    if _has_forbidden_control(value):
        raise AlertValidationError("message must not contain control characters")


def _require_domain(value: Domain | None, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, Domain):
        expected = "a Domain or None" if optional else "a Domain"
        raise AlertValidationError(f"domain must be {expected}")


def _require_severity(value: Severity) -> None:
    if not isinstance(value, Severity):
        raise AlertValidationError("severity must be a Severity")


def _require_alert_id(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AlertValidationError("alert_id must be a positive integer")


def _require_optional_entity(value: str | None, field_name: str) -> None:
    if value is None:
        return
    try:
        _require_entity_ref(value)
    except AlertValidationError as exc:
        raise AlertValidationError(f"invalid {field_name}: {exc}") from exc


def _actor_user_id(context: AlertContext) -> uuid.UUID | None:
    if isinstance(context, PlatformAlertContext):
        return context.actor_user_id
    return context.identity.actor_user_id


def _provider_key_matches(provider: IntegrationProvider, alert_key: str) -> bool:
    return alert_key in PROVIDER_ALERT_KEYS[provider]


def _is_platform_key(alert_key: str) -> bool:
    return any(alert_key in keys for keys in PLATFORM_ALERT_KEYS.values())


def is_platform_alert_key(alert_key: str) -> bool:
    """Return whether ``alert_key`` belongs to a platform-only namespace.

    This small public classifier is the transitional composition guard while
    Today/digest are still on the singleton reader.  Platform diagnostics may
    contain operational exception details and must never enter a health report
    or an external-model prompt.  Full subject/provider aggregation replaces
    this guard at the composition cutover.
    """

    if not isinstance(alert_key, str):
        return False
    return _is_platform_key(alert_key)


def _is_provider_key(alert_key: str) -> bool:
    return any(_provider_key_matches(provider, alert_key) for provider in IntegrationProvider)


def _is_health_key(alert_key: str) -> bool:
    if alert_key in HEALTH_ALERT_KEYS:
        return True
    return re.fullmatch(r"conflict:[1-9][0-9]*", alert_key) is not None


def _is_classified_key(alert_key: str) -> bool:
    return _is_health_key(alert_key) or _is_provider_key(alert_key) or _is_platform_key(alert_key)


def _validate_context_key(context: AlertContext, alert_key: str) -> None:
    if isinstance(context, PlatformAlertContext):
        if alert_key not in PLATFORM_ALERT_KEYS[context.namespace]:
            raise AlertPlatformNamespaceError(
                "alert_key does not belong to the selected platform namespace"
            )
        return
    if isinstance(context, ProviderAlertContext):
        if not _provider_key_matches(context.provider, alert_key):
            raise AlertPlatformNamespaceError(
                "alert_key does not belong to the selected provider namespace"
            )
        return
    if not _is_health_key(alert_key):
        raise AlertPlatformNamespaceError("alert_key is not registered as a health-subject alert")
