"""Pure Garmin payload normalization and timestamp parsing."""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, timezone
from typing import Any, Optional

from vitals.utils.timeutils import to_local_naive


# Domain wire keys, repeated here so the pure parser does not import ORM models.
# A contract test keeps them aligned with ``vitals.models.garmin``.
SERIES_STRESS = "stress"
SERIES_BODY_BATTERY = "body_battery"
SERIES_HEART_RATE = "heart_rate"
SERIES_SLEEP_HR = "sleep_hr"
SERIES_SLEEP_SPO2 = "sleep_spo2"
SERIES_SLEEP_RESPIRATION = "sleep_respiration"
SERIES_SLEEP_STRESS = "sleep_stress"
SERIES_SLEEP_BB = "sleep_bb"
SERIES_SLEEP_HRV = "sleep_hrv"
SERIES_SLEEP_MOVEMENT = "sleep_movement"


def _dig(payload: Any, *path: str) -> Any:
    """Walk nested dict keys, tolerating missing keys and non-dicts."""

    cur = payload
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _num(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _intish(value: Any) -> Optional[int]:
    number = _num(value)
    return int(round(number)) if number is not None else None


def _first(*values: Any) -> Any:
    """Return the first non-None Garmin shape variant."""

    for value in values:
        if value is not None:
            return value
    return None


def _strip_level_suffix(phrase: Optional[str]) -> Optional[str]:
    """Drop Garmin's numeric training-status intensity suffix."""

    if not isinstance(phrase, str):
        return phrase
    base, _, suffix = phrase.rpartition("_")
    return base if base and suffix.isdigit() else phrase


def _parse_sleep_boundary(sleep_dto: dict, prefix: str) -> Optional[datetime]:
    """Parse Garmin's GMT or pre-offset local sleep boundary."""

    gmt_ms = _num(sleep_dto.get(f"{prefix}TimestampGMT"))
    if gmt_ms is not None:
        return to_local_naive(
            datetime.fromtimestamp(gmt_ms / 1000, tz=timezone.utc)
        )
    local_ms = _num(sleep_dto.get(f"{prefix}TimestampLocal"))
    if local_ms is not None:
        return datetime.fromtimestamp(
            local_ms / 1000,
            tz=timezone.utc,
        ).replace(tzinfo=None)
    return None


def _epoch_ms_to_local(value: Any) -> Optional[datetime]:
    """Convert a true UTC epoch-millisecond timestamp to local naive time."""

    milliseconds = _num(value)
    if milliseconds is None:
        return None
    return to_local_naive(
        datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    )


def _descriptor_index(descriptors: Any, wanted_key: str) -> Optional[int]:
    """Resolve a value column in Garmin's positional intraday arrays."""

    if not isinstance(descriptors, list):
        return None
    for item in descriptors:
        if not isinstance(item, dict):
            continue
        key = _first(
            item.get("key"),
            item.get("bodyBatteryValueDescriptorKey"),
        )
        if key == wanted_key:
            return _intish(
                _first(
                    item.get("index"),
                    item.get("bodyBatteryValueDescriptorIndex"),
                )
            )
    return None


def _parse_intraday_points(
    rows: Any,
    *,
    value_index: Optional[int] = None,
) -> list[tuple[datetime, float]]:
    """Normalize positional samples, dropping negative sentinel readings."""

    out: list[tuple[datetime, float]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        timestamp = _epoch_ms_to_local(row[0])
        if timestamp is None:
            continue
        if value_index is not None and 0 <= value_index < len(row):
            value = _num(row[value_index])
        else:
            value = next(
                (
                    candidate
                    for candidate in (_num(column) for column in row[1:])
                    if candidate is not None
                ),
                None,
            )
        if value is None or value < 0:
            continue
        out.append((timestamp, value))
    out.sort(key=lambda point: point[0])
    return out


_SLEEP_SERIES = (
    (SERIES_SLEEP_HR, "sleepHeartRate", "startGMT", "value"),
    (SERIES_SLEEP_STRESS, "sleepStress", "startGMT", "value"),
    (SERIES_SLEEP_BB, "sleepBodyBattery", "startGMT", "value"),
    (SERIES_SLEEP_HRV, "hrvData", "startGMT", "value"),
    (
        SERIES_SLEEP_RESPIRATION,
        "wellnessEpochRespirationDataDTOList",
        "startTimeGMT",
        "respirationValue",
    ),
    (
        SERIES_SLEEP_SPO2,
        "wellnessEpochSPO2DataDTOList",
        "epochTimestamp",
        "spo2Reading",
    ),
    (SERIES_SLEEP_MOVEMENT, "sleepMovement", "startGMT", "activityLevel"),
)

_SLEEP_STAGE_NAMES = {0: "deep", 1: "light", 2: "rem", 3: "awake"}


def _gmt_moment(value: Any) -> Optional[datetime]:
    """Parse either Garmin nightly timestamp shape as local naive time."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _epoch_ms_to_local(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return to_local_naive(parsed)


def _parse_sleep_points(
    rows: Any,
    ts_key: str,
    value_key: str,
) -> list[tuple[datetime, float]]:
    """Normalize Garmin's dictionary-shaped nightly point series."""

    out: list[tuple[datetime, float]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        timestamp = _gmt_moment(row.get(ts_key))
        value = _num(row.get(value_key))
        if timestamp is None or value is None or value < 0:
            continue
        out.append((timestamp, value))
    out.sort(key=lambda point: point[0])
    return out


def _sleep_intraday_series(
    raw: dict,
) -> dict[str, list[tuple[datetime, float]]]:
    """Normalize all nightly point series from one daily bundle."""

    sleep = raw.get("sleep")
    if not isinstance(sleep, dict):
        sleep = {}
    return {
        series_type: _parse_sleep_points(sleep.get(key), ts_key, value_key)
        for series_type, key, ts_key, value_key in _SLEEP_SERIES
    }


def _intraday_series(raw: dict) -> dict[str, list[tuple[datetime, float]]]:
    """Normalize all whole-day and nightly curves in a daily bundle."""

    stress_payload = raw.get("stress") or {}
    stress_rows = stress_payload.get("stressValuesArray")
    stress_index = _descriptor_index(
        stress_payload.get("stressValueDescriptorsDTOList"),
        "stressLevel",
    )

    body_battery_rows = stress_payload.get("bodyBatteryValuesArray")
    body_battery_descriptors = stress_payload.get(
        "bodyBatteryValueDescriptorsDTOList"
    )
    if not body_battery_rows:
        body_battery_payload = raw.get("body_battery")
        body_battery = (
            body_battery_payload[0]
            if isinstance(body_battery_payload, list) and body_battery_payload
            else body_battery_payload
        )
        if isinstance(body_battery, dict):
            body_battery_rows = body_battery.get("bodyBatteryValuesArray")
            body_battery_descriptors = _first(
                body_battery.get("bodyBatteryValueDescriptorDTOList"),
                body_battery.get("bodyBatteryValueDescriptorsDTOList"),
            )
    body_battery_index = _descriptor_index(
        body_battery_descriptors,
        "bodyBatteryLevel",
    )

    heart_rate_payload = raw.get("heart_rate") or {}
    heart_rate_rows = heart_rate_payload.get("heartRateValues")
    heart_rate_index = _descriptor_index(
        heart_rate_payload.get("heartRateValueDescriptors"),
        "heartrate",
    )

    return {
        SERIES_STRESS: _parse_intraday_points(
            stress_rows,
            value_index=stress_index,
        ),
        SERIES_BODY_BATTERY: _parse_intraday_points(
            body_battery_rows,
            value_index=body_battery_index,
        ),
        SERIES_HEART_RATE: _parse_intraday_points(
            heart_rate_rows,
            value_index=heart_rate_index,
        ),
        **_sleep_intraday_series(raw),
    }


def _parse_sleep_intervals(
    rows: Any,
    value_key: str,
    out_key: str,
) -> Optional[list]:
    """Normalize Garmin's nightly interval arrays into JSON-safe spans."""

    if not isinstance(rows, list) or not rows:
        return None
    out: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        start = _gmt_moment(item.get("startGMT"))
        end = _gmt_moment(item.get("endGMT"))
        if start is None or end is None:
            continue
        out.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                out_key: _intish(item.get(value_key)),
            }
        )
    out.sort(key=lambda span: span["start"])
    return out or None


def _normalize_sleep_stages(raw: dict) -> Optional[list]:
    """Normalize the nightly hypnogram and resolve stage codes to names."""

    stages = _parse_sleep_intervals(
        _dig(raw, "sleep", "sleepLevels"),
        "activityLevel",
        "stage",
    )
    if stages is None:
        return None
    for stage in stages:
        stage["stage"] = _SLEEP_STAGE_NAMES.get(stage["stage"], "unknown")
    return stages


def _normalize_breathing_events(raw: dict) -> Optional[list]:
    """Normalize breathing-disruption severity spans, including zero values."""

    return _parse_sleep_intervals(
        _dig(raw, "sleep", "breathingDisruptionData"),
        "value",
        "value",
    )


def _normalize_daily(raw: dict) -> dict:
    """Reduce a raw daily bundle to GarminDaily column values."""

    summary = raw.get("summary") or {}
    sleep_dto = _dig(raw, "sleep", "dailySleepDTO") or {}
    hrv = _dig(raw, "hrv", "hrvSummary") or {}
    training_readiness = raw.get("training_readiness")
    readiness = (
        training_readiness[0]
        if isinstance(training_readiness, list) and training_readiness
        else training_readiness
        if isinstance(training_readiness, dict)
        else {}
    )
    max_metrics = raw.get("max_metrics")
    metric = (
        max_metrics[0]
        if isinstance(max_metrics, list) and max_metrics
        else max_metrics
        if isinstance(max_metrics, dict)
        else {}
    )
    status_map = _dig(
        raw,
        "training_status",
        "mostRecentTrainingStatus",
        "latestTrainingStatusData",
    )
    status = next(iter(status_map.values()), {}) if isinstance(status_map, dict) else {}
    if not isinstance(status, dict):
        status = {}
    acute_load = status.get("acuteTrainingLoadDTO") or {}

    return {
        "sleep_seconds": _intish(sleep_dto.get("sleepTimeSeconds")),
        "sleep_score": _intish(
            _dig(sleep_dto, "sleepScores", "overall", "value")
        ),
        "deep_sleep_seconds": _intish(sleep_dto.get("deepSleepSeconds")),
        "light_sleep_seconds": _intish(sleep_dto.get("lightSleepSeconds")),
        "rem_sleep_seconds": _intish(sleep_dto.get("remSleepSeconds")),
        "awake_seconds": _intish(sleep_dto.get("awakeSleepSeconds")),
        "sleep_start": _parse_sleep_boundary(sleep_dto, "sleepStart"),
        "sleep_end": _parse_sleep_boundary(sleep_dto, "sleepEnd"),
        "awake_count": _intish(sleep_dto.get("awakeCount")),
        "restless_moments": _intish(
            _first(
                sleep_dto.get("restlessMomentsCount"),
                _dig(raw, "sleep", "restlessMomentsCount"),
            )
        ),
        "avg_sleep_stress": _intish(sleep_dto.get("avgSleepStress")),
        "avg_sleep_hr": _intish(sleep_dto.get("avgHeartRate")),
        "spo2_lowest": _intish(sleep_dto.get("lowestSpO2Value")),
        "respiration_lowest": _num(sleep_dto.get("lowestRespirationValue")),
        "respiration_highest": _num(sleep_dto.get("highestRespirationValue")),
        "body_battery_change": _intish(
            _dig(raw, "sleep", "bodyBatteryChange")
        ),
        "breathing_disruption": sleep_dto.get("breathingDisruptionSeverity"),
        "sleep_need_actual": _intish(
            _first(
                _dig(sleep_dto, "nextSleepNeed", "actual"),
                _dig(raw, "sleep", "nextSleepNeed", "actual"),
            )
        ),
        "sleep_stages": _normalize_sleep_stages(raw),
        "breathing_events": _normalize_breathing_events(raw),
        "resting_hr": _intish(
            _first(
                summary.get("restingHeartRate"),
                _dig(raw, "rhr", "restingHeartRate"),
            )
        ),
        "avg_hr": _intish(summary.get("averageHeartRate")),
        "max_hr": _intish(summary.get("maxHeartRate")),
        "min_hr": _intish(summary.get("minHeartRate")),
        "hrv_avg": _num(_first(hrv.get("lastNightAvg"), hrv.get("weeklyAvg"))),
        "hrv_status": hrv.get("status"),
        "avg_respiration": _num(summary.get("avgWakingRespirationValue")),
        "spo2_avg": _num(
            _first(summary.get("averageSpo2"), summary.get("averageSpo2Value"))
        ),
        "avg_stress": _intish(summary.get("averageStressLevel")),
        "max_stress": _intish(summary.get("maxStressLevel")),
        "body_battery_high": _intish(summary.get("bodyBatteryHighestValue")),
        "body_battery_low": _intish(summary.get("bodyBatteryLowestValue")),
        "steps": _intish(summary.get("totalSteps")),
        "floors_climbed": _intish(summary.get("floorsAscended")),
        "active_calories": _intish(summary.get("activeKilocalories")),
        "bmr_calories": _intish(summary.get("bmrKilocalories")),
        "total_calories": _intish(summary.get("totalKilocalories")),
        "intensity_minutes_moderate": _intish(
            summary.get("moderateIntensityMinutes")
        ),
        "intensity_minutes_vigorous": _intish(
            summary.get("vigorousIntensityMinutes")
        ),
        "training_readiness": _intish(
            readiness.get("score") if isinstance(readiness, dict) else None
        ),
        "vo2max": _num(
            _dig(metric, "generic", "vo2MaxValue")
            if isinstance(metric, dict)
            else None
        ),
        "training_status": _strip_level_suffix(
            status.get("trainingStatusFeedbackPhrase")
        ),
        "acute_load": _num(acute_load.get("acuteTrainingLoad")),
        "load_ratio": _num(acute_load.get("acwrPercent")),
    }


def _extract_weight_kg(raw: dict) -> Optional[float]:
    """Normalize a Garmin weigh-in to kilograms."""

    grams = _first(
        _dig(raw, "body_composition", "totalAverage", "weight"),
        _dig(raw, "summary", "weight"),
    )
    kilograms = _num(grams)
    if kilograms is None:
        return None
    return (
        round(kilograms / 1000.0, 2)
        if kilograms > 1000
        else round(kilograms, 2)
    )


def _activity_external_id(raw: dict) -> str:
    return str(raw.get("activityId") or raw.get("activityid") or "").strip()


def _normalize_hr_zones(raw: dict) -> Optional[list]:
    """Normalize activity HR-zone detail or summary fallbacks."""

    detail = _dig(raw, "_details", "hr_zones")
    if isinstance(detail, list) and detail:
        out = [
            {
                "zone": _intish(zone.get("zoneNumber")),
                "secs": _num(zone.get("secsInZone")),
                "low_hr": _intish(zone.get("zoneLowBoundary")),
            }
            for zone in detail
            if isinstance(zone, dict)
        ]
        if out:
            return out
    fallback = [
        {
            "zone": number,
            "secs": _num(raw.get(f"hrTimeInZone_{number}")),
            "low_hr": None,
        }
        for number in range(1, 6)
        if raw.get(f"hrTimeInZone_{number}") is not None
    ]
    return fallback or None


def _normalize_splits(raw: dict) -> Optional[list]:
    """Normalize per-lap activity split detail."""

    laps = _dig(raw, "_details", "splits", "lapDTOs")
    if not isinstance(laps, list) or not laps:
        return None
    out = [
        {
            "index": _intish(lap.get("lapIndex")),
            "distance_m": _num(lap.get("distance")),
            "duration_s": _num(lap.get("duration")),
            "avg_hr": _intish(lap.get("averageHR")),
            "max_hr": _intish(lap.get("maxHR")),
            "avg_speed_mps": _num(lap.get("averageSpeed")),
        }
        for lap in laps
        if isinstance(lap, dict)
    ]
    return out or None


def _parse_activity_start(raw: dict) -> Optional[datetime]:
    for key in ("startTimeGMT", "startTimeLocal"):
        value = raw.get(key)
        if value:
            try:
                text = str(value).replace("Z", "+00:00")
                return to_local_naive(datetime.fromisoformat(text))
            except (ValueError, TypeError):
                continue
    return None


def _parse_hae_date(value: Any) -> Optional[date_type]:
    if not value:
        return None
    try:
        return date_type.fromisoformat(str(value)[:10])
    except ValueError:
        return None
