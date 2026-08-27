"""Contracts shared by digest projection collectors and assembly."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeAlias


_BODY_MEASUREMENT_LIMIT = 6
_BODY_SCAN_LIMIT = 3
_GARMIN_ACTIVITY_LIMIT = 500
_HEVY_SESSION_LIMIT = 300
_TREATMENT_EVENT_LIMIT = 500
_SKINCARE_EVENT_LIMIT = 500
_LAB_HISTORY_PER_MARKER = 3
_GENETICS_LIMIT = 200
_TIMELINE_LIMIT = 200

_REPORT_BODY_METRIC_KEYS = frozenset(
    {
        "weight",
        "skeletal_muscle_mass",
        "body_fat_mass",
        "body_fat_pct",
        "lean_body_mass",
        "fat_free_mass",
        "protein",
        "minerals",
        "total_body_water",
        "intracellular_water",
        "extracellular_water",
        "ecw_tbw_ratio",
        "visceral_fat_area",
        "visceral_fat_level",
        "phase_angle",
        "inbody_score",
        "bmr",
        "waist_hip_ratio",
        "segmental_lean",
        "segmental_fat",
    }
)

_DOMAIN_MODULE = {
    "weight": "weight",
    "body_comp": "body_comp",
    "glp1": "glp1",
    "supplements": "supplements",
    "genetics": "genetics",
    "skincare": "skincare",
    "workouts": "hevy",
    "garmin": "garmin",
    "labs": "labs",
    "nutrition": "nutrition",
    "hrt": "hrt",
    "timeline": "timeline",
    "milestones": "reports",
    "system": "reports",
}

ModuleGate: TypeAlias = Callable[[str], bool]
DomainVisibility: TypeAlias = Callable[[str], bool]


@dataclass(frozen=True)
class ProviderProjection:
    all_weights: list[Any]
    weights: list[Any]
    measurement_history: list[dict[str, Any]]
    latest_weight: Any | None
    scan: Any | None
    garmin_rows: list[Any]
    garmin_activities: list[Any]
    sessions: list[dict[str, Any]]
    glp1_injections: list[Any]
    glp1_effects: list[Any]


@dataclass(frozen=True)
class ClinicalProjection:
    all_meals: list[Any]
    all_meals_by_date: dict[Any, list[Any]]
    all_supplements: list[Any]
    skin_logs: list[Any]
    skin_observations: list[Any]
    all_products: list[Any]
    variants: list[Any]
    hrt_doses: list[Any]
    hrt_effects: list[Any]
