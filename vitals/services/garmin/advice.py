"""Pure Garmin recovery-advice projection."""

from __future__ import annotations

from typing import Optional

from vitals.i18n import t
from vitals.models.garmin import GarminDaily

SLEEP_SCORE_FLOOR = 60
BODY_BATTERY_FLOOR = 40
SPO2_FLOOR = 90


def recovery_advice(daily: Optional[GarminDaily]) -> Optional[str]:
    """Return the passive recovery hint, or ``None`` when recovery is fine."""

    if daily is None:
        return None
    notes: list[str] = []
    if daily.sleep_score is not None and daily.sleep_score < SLEEP_SCORE_FLOOR:
        notes.append(t("alert.recovery_sleep", score=daily.sleep_score))
    if (
        daily.body_battery_high is not None
        and daily.body_battery_high < BODY_BATTERY_FLOOR
    ):
        notes.append(t("alert.recovery_battery", value=daily.body_battery_high))
    if daily.spo2_lowest is not None and daily.spo2_lowest < SPO2_FLOOR:
        notes.append(t("alert.recovery_spo2", value=daily.spo2_lowest))
    if daily.breathing_disruption and daily.breathing_disruption != "NONE":
        notes.append(t("alert.recovery_breathing"))
    if not notes:
        return None
    return t("alert.recovery_prefix") + ", ".join(notes) + t(
        "alert.recovery_suffix"
    )
