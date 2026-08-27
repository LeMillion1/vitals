"""Typed contracts and stable constants for proactive preferences."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import time as time_type
from typing import Any

from vitals.enums import IntegrationConnectionStatus

LEGACY_SETTINGS_KEY = "proactive"
SUBJECT_POLICY_KEY = "proactive_subject_policy"
TELEGRAM_DELIVERY_POLICY_KEY = "proactive_delivery_policy"
GARMIN_POLICY_KEY = "garmin_proactive_policy"

# Compatibility label only. Scoped runtime APIs never use this global key.
SETTINGS_KEY = LEGACY_SETTINGS_KEY

# No module gates this layer any more. The switch was the ``signals`` module,
# which was also the free-text capture domain, and both went with the chat.
# Reading it after the module left MODULE_REGISTRY would have returned False for
# everybody, for ever — a layer that is off and says nothing about being off.
# The schedule and the nudge categories below are the whole of the answer now.

CATEGORY_ACTIVITY = "activity"
CATEGORY_NUTRITION = "nutrition"
CATEGORY_DATA = "data"
NUDGE_CATEGORIES: tuple[str, ...] = (
    CATEGORY_ACTIVITY,
    CATEGORY_NUTRITION,
    CATEGORY_DATA,
)

BUDGET_RANGE = (1, 12)
SYNC_HOURS_RANGE = (1, 24)
WEIGHT_EXPORT_MINUTES_RANGE = (5, 1440)
WEIGHT_MAX_AGE_DAYS_RANGE = (1, 30)
PULSE_SECONDS_RANGE = (60, 3600)

DEFAULTS: dict[str, Any] = {
    "brief_time": "11:00",
    "quiet_start": "02:00",
    "quiet_end": "10:00",
    "daily_budget": 4,
    "garmin_sync_hours": 6,
    "garmin_weight_export_minutes": 15,
    "garmin_weight_max_age_days": 30,
    "pulse_seconds": 900,
    "pulse_start_hour": 8,
    "pulse_end_hour": 24,
    "nudges": {category: True for category in NUDGE_CATEGORIES},
}

_SUBJECT_FIELDS = frozenset({"brief_time", "nudges"})
_DELIVERY_FIELDS = frozenset({"quiet_start", "quiet_end", "daily_budget"})
_GARMIN_FIELDS = frozenset(
    {
        "garmin_sync_hours",
        "garmin_weight_export_minutes",
        "garmin_weight_max_age_days",
        "pulse_seconds",
        "pulse_start_hour",
        "pulse_end_hour",
    }
)
_LIVE_CONNECTION_STATUSES = frozenset(
    {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }
)
_NON_RETIRED_CONNECTION_STATUSES = frozenset(
    status.value
    for status in IntegrationConnectionStatus
    if status is not IntegrationConnectionStatus.RETIRED
)


class ProactivePreferencesError(RuntimeError):
    """Base class for scoped proactive preference failures."""


class ProactivePreferencesValidationError(ValueError):
    """Caller input or a persisted preference value is invalid."""


class ProactivePreferencesUnavailableError(ProactivePreferencesError):
    """A strict runtime policy cannot be proved from complete scoped rows."""


class ProactivePreferencesNotConfiguredError(ProactivePreferencesUnavailableError):
    """The subject has never configured proactive delivery.

    This is an ordinary state for a newly provisioned account, unlike a stored
    row with an invalid shape or a partially written three-row bundle. Scheduled
    work may safely skip it while continuing to fail closed on corrupted state.
    """


class ProactivePreferencesScopeError(ProactivePreferencesError):
    """The supplied subject, recipient, or connection graph is not exact."""


class ProactivePreferencesDriftError(ProactivePreferencesError):
    """Legacy and already-split values disagree during exact-one bootstrap."""


class LegacyProactivePreferencesBridgeClosedError(ProactivePreferencesError):
    """A requested legacy mirror is no longer safe for the identity graph."""


@dataclass(frozen=True, slots=True)
class ProactivePreferencesScope:
    """Exact roots needed to read or replace the three policy partitions."""

    subject_id: uuid.UUID
    recipient_user_id: uuid.UUID
    telegram_connection_id: uuid.UUID
    garmin_connection_id: uuid.UUID
    include_legacy: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "subject_id",
            "recipient_user_id",
            "telegram_connection_id",
            "garmin_connection_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, uuid.UUID) or value.int == 0:
                raise ProactivePreferencesValidationError(
                    f"{field_name} must be a non-zero UUID"
                )
        if not isinstance(self.include_legacy, bool):
            raise ProactivePreferencesValidationError(
                "include_legacy must be a bool"
            )
        if self.telegram_connection_id == self.garmin_connection_id:
            raise ProactivePreferencesValidationError(
                "Telegram and Garmin connections must be distinct"
            )


@dataclass(frozen=True, slots=True)
class SubjectProactivePolicy:
    brief_time: time_type
    enabled_nudge_categories: frozenset[str]


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    quiet_start: time_type
    quiet_end: time_type
    daily_budget: int


@dataclass(frozen=True, slots=True)
class GarminProactivePolicy:
    sync_hours: int
    weight_export_minutes: int
    weight_max_age_days: int
    pulse_seconds: int
    pulse_start_hour: int
    pulse_end_hour: int


@dataclass(frozen=True, slots=True)
class ProactivePreferencesBundle:
    subject: SubjectProactivePolicy
    delivery: DeliveryPolicy
    garmin: GarminProactivePolicy

    def as_flat_dict(self) -> dict[str, Any]:
        """Return the stable legacy/UI projection without shared mutable state."""

        enabled = self.subject.enabled_nudge_categories
        return {
            "brief_time": self.subject.brief_time.strftime("%H:%M"),
            "quiet_start": self.delivery.quiet_start.strftime("%H:%M"),
            "quiet_end": self.delivery.quiet_end.strftime("%H:%M"),
            "daily_budget": self.delivery.daily_budget,
            "garmin_sync_hours": self.garmin.sync_hours,
            "garmin_weight_export_minutes": self.garmin.weight_export_minutes,
            "garmin_weight_max_age_days": self.garmin.weight_max_age_days,
            "pulse_seconds": self.garmin.pulse_seconds,
            "pulse_start_hour": self.garmin.pulse_start_hour,
            "pulse_end_hour": self.garmin.pulse_end_hour,
            "nudges": {
                category: category in enabled for category in NUDGE_CATEGORIES
            },
        }
