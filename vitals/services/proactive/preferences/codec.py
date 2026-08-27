"""Validation, encoding, and decoding for proactive preference values."""
from __future__ import annotations

from datetime import time as time_type
from typing import Any

from vitals.services.proactive.preferences.contracts import (
    BUDGET_RANGE,
    DEFAULTS,
    NUDGE_CATEGORIES,
    PULSE_SECONDS_RANGE,
    SYNC_HOURS_RANGE,
    WEIGHT_EXPORT_MINUTES_RANGE,
    WEIGHT_MAX_AGE_DAYS_RANGE,
    DeliveryPolicy,
    GarminProactivePolicy,
    ProactivePreferencesBundle,
    ProactivePreferencesUnavailableError,
    SubjectProactivePolicy,
    _DELIVERY_FIELDS,
    _GARMIN_FIELDS,
    _SUBJECT_FIELDS,
)

def _time(raw: Any, default: str) -> str:
    try:
        return time_type.fromisoformat(str(raw)).strftime("%H:%M")
    except (TypeError, ValueError):
        return default


def _int(raw: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(raw)))
    except (TypeError, ValueError):
        return default


def sanitize(raw: Any) -> dict[str, Any]:
    """Normalize untrusted legacy/form input into one complete flat value."""

    src = raw if isinstance(raw, dict) else {}
    stored_nudges = src.get("nudges")
    if not isinstance(stored_nudges, dict):
        stored_nudges = {}

    pulse_raw = src.get("pulse_seconds", DEFAULTS["pulse_seconds"])
    try:
        pulse = int(pulse_raw)
    except (TypeError, ValueError):
        pulse = DEFAULTS["pulse_seconds"]
    pulse = (
        0
        if pulse <= 0
        else max(PULSE_SECONDS_RANGE[0], min(PULSE_SECONDS_RANGE[1], pulse))
    )

    start_hour = _int(
        src.get("pulse_start_hour"), DEFAULTS["pulse_start_hour"], 0, 23
    )
    end_hour = _int(
        src.get("pulse_end_hour"), DEFAULTS["pulse_end_hour"], 1, 24
    )
    end_hour = max(end_hour, start_hour + 1)

    return {
        "brief_time": _time(src.get("brief_time"), DEFAULTS["brief_time"]),
        "quiet_start": _time(src.get("quiet_start"), DEFAULTS["quiet_start"]),
        "quiet_end": _time(src.get("quiet_end"), DEFAULTS["quiet_end"]),
        "daily_budget": _int(
            src.get("daily_budget"), DEFAULTS["daily_budget"], *BUDGET_RANGE
        ),
        "garmin_sync_hours": _int(
            src.get("garmin_sync_hours"),
            DEFAULTS["garmin_sync_hours"],
            *SYNC_HOURS_RANGE,
        ),
        "garmin_weight_export_minutes": _int(
            src.get("garmin_weight_export_minutes"),
            DEFAULTS["garmin_weight_export_minutes"],
            *WEIGHT_EXPORT_MINUTES_RANGE,
        ),
        "garmin_weight_max_age_days": _int(
            src.get("garmin_weight_max_age_days"),
            DEFAULTS["garmin_weight_max_age_days"],
            *WEIGHT_MAX_AGE_DAYS_RANGE,
        ),
        "pulse_seconds": pulse,
        "pulse_start_hour": start_hour,
        "pulse_end_hour": end_hour,
        "nudges": {
            category: bool(stored_nudges.get(category, True))
            for category in NUDGE_CATEGORIES
        },
    }


def hhmm(value: str) -> tuple[int, int]:
    parsed = time_type.fromisoformat(value)
    return parsed.hour, parsed.minute


def as_time(value: str) -> time_type:
    return time_type.fromisoformat(value)


def _subject_value(clean: dict[str, Any]) -> dict[str, Any]:
    return {key: clean[key] for key in _SUBJECT_FIELDS}


def _delivery_value(clean: dict[str, Any]) -> dict[str, Any]:
    return {key: clean[key] for key in _DELIVERY_FIELDS}


def _garmin_value(clean: dict[str, Any]) -> dict[str, Any]:
    return {key: clean[key] for key in _GARMIN_FIELDS}


def _strict_object(
    raw: Any, fields: frozenset[str], *, label: str
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ProactivePreferencesUnavailableError(
            f"{label} preference row has an invalid field set"
        )
    return raw


def _strict_time(raw: Any, *, field: str) -> time_type:
    if not isinstance(raw, str):
        raise ProactivePreferencesUnavailableError(
            f"{field} must be a canonical HH:MM string"
        )
    try:
        parsed = time_type.fromisoformat(raw)
    except ValueError as exc:
        raise ProactivePreferencesUnavailableError(
            f"{field} must be a canonical HH:MM string"
        ) from exc
    if parsed.second or parsed.microsecond or parsed.strftime("%H:%M") != raw:
        raise ProactivePreferencesUnavailableError(
            f"{field} must be a canonical HH:MM string"
        )
    return parsed


def _strict_int(raw: Any, *, field: str, low: int, high: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
        raise ProactivePreferencesUnavailableError(
            f"{field} is outside its supported integer range"
        )
    return raw


def _decode_subject(raw: Any) -> SubjectProactivePolicy:
    value = _strict_object(raw, _SUBJECT_FIELDS, label="subject proactive")
    nudges = value["nudges"]
    if (
        not isinstance(nudges, dict)
        or set(nudges) != set(NUDGE_CATEGORIES)
        or any(not isinstance(nudges[key], bool) for key in NUDGE_CATEGORIES)
    ):
        raise ProactivePreferencesUnavailableError(
            "subject proactive nudge policy is malformed"
        )
    return SubjectProactivePolicy(
        brief_time=_strict_time(value["brief_time"], field="brief_time"),
        enabled_nudge_categories=frozenset(
            key for key in NUDGE_CATEGORIES if nudges[key]
        ),
    )


def _decode_delivery(raw: Any) -> DeliveryPolicy:
    value = _strict_object(raw, _DELIVERY_FIELDS, label="Telegram delivery")
    return DeliveryPolicy(
        quiet_start=_strict_time(value["quiet_start"], field="quiet_start"),
        quiet_end=_strict_time(value["quiet_end"], field="quiet_end"),
        daily_budget=_strict_int(
            value["daily_budget"],
            field="daily_budget",
            low=BUDGET_RANGE[0],
            high=BUDGET_RANGE[1],
        ),
    )


def _decode_garmin(raw: Any) -> GarminProactivePolicy:
    value = _strict_object(raw, _GARMIN_FIELDS, label="Garmin proactive")
    pulse_seconds = value["pulse_seconds"]
    if pulse_seconds != 0:
        pulse_seconds = _strict_int(
            pulse_seconds,
            field="pulse_seconds",
            low=PULSE_SECONDS_RANGE[0],
            high=PULSE_SECONDS_RANGE[1],
        )
    elif isinstance(pulse_seconds, bool) or not isinstance(pulse_seconds, int):
        raise ProactivePreferencesUnavailableError(
            "pulse_seconds is outside its supported integer range"
        )
    start_hour = _strict_int(
        value["pulse_start_hour"], field="pulse_start_hour", low=0, high=23
    )
    end_hour = _strict_int(
        value["pulse_end_hour"], field="pulse_end_hour", low=1, high=24
    )
    if end_hour <= start_hour:
        raise ProactivePreferencesUnavailableError(
            "Garmin pulse window must end after it starts"
        )
    return GarminProactivePolicy(
        sync_hours=_strict_int(
            value["garmin_sync_hours"],
            field="garmin_sync_hours",
            low=SYNC_HOURS_RANGE[0],
            high=SYNC_HOURS_RANGE[1],
        ),
        weight_export_minutes=_strict_int(
            value["garmin_weight_export_minutes"],
            field="garmin_weight_export_minutes",
            low=WEIGHT_EXPORT_MINUTES_RANGE[0],
            high=WEIGHT_EXPORT_MINUTES_RANGE[1],
        ),
        weight_max_age_days=_strict_int(
            value["garmin_weight_max_age_days"],
            field="garmin_weight_max_age_days",
            low=WEIGHT_MAX_AGE_DAYS_RANGE[0],
            high=WEIGHT_MAX_AGE_DAYS_RANGE[1],
        ),
        pulse_seconds=pulse_seconds,
        pulse_start_hour=start_hour,
        pulse_end_hour=end_hour,
    )


def _decode_bundle(
    subject_value: Any,
    delivery_value: Any,
    garmin_value: Any,
) -> ProactivePreferencesBundle:
    return ProactivePreferencesBundle(
        subject=_decode_subject(subject_value),
        delivery=_decode_delivery(delivery_value),
        garmin=_decode_garmin(garmin_value),
    )


def _bundle_from_clean(clean: dict[str, Any]) -> ProactivePreferencesBundle:
    return _decode_bundle(
        _subject_value(clean),
        _delivery_value(clean),
        _garmin_value(clean),
    )


def _stored_or_default(
    subject_value: Any, delivery_value: Any, garmin_value: Any
) -> tuple[Any, Any, Any]:
    """Fill in the partitions nobody has written yet, and only those.

    The decoders are strict on purpose: a stored row with the wrong field set is
    tampered-with or from a schema this build does not understand, and coercing
    it would silently apply a policy the person never chose. Absent is not that.
    A subject who has never opened the notification settings has no row, and the
    honest reading of no row is the defaults — the same ones the form shows.

    Only the human read reaches this. The write paths go through
    ``_require_complete_rows``, where a missing partition means a half-written
    split and must still stop.
    """

    if subject_value is not None and delivery_value is not None and garmin_value is not None:
        return subject_value, delivery_value, garmin_value
    clean = sanitize(None)
    return (
        _subject_value(clean) if subject_value is None else subject_value,
        _delivery_value(clean) if delivery_value is None else delivery_value,
        _garmin_value(clean) if garmin_value is None else garmin_value,
    )
