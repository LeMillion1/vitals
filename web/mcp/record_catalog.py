"""Frozen MCP record-hub catalogs.

The model map is compatibility metadata for the established surface contract;
record operations themselves dispatch only to explicit scoped service commands.
"""
from __future__ import annotations

from typing import Optional

from vitals.models import (
    BodyMeasurement,
    BodyScan,
    Injection,
    LabResult,
    MealLog,
    SkincareLog,
    WeightLog,
)


NOTE_MODELS = {
    "weight": WeightLog,
    "nutrition": MealLog,
    "glp1": Injection,
    "skincare": SkincareLog,
    "measurement": BodyMeasurement,
    "body_comp": BodyScan,
    "labs": LabResult,
}


# domain -> (optional module gate, service module, scoped delete command)
DELETE_TARGETS: dict[str, tuple[Optional[str], str, str]] = {
    "weight": (None, "weight.writes", "delete_weight_log"),
    "measurement": (None, "weight.measurements", "delete_body_measurement"),
    "noise_marker": (None, "weight.noise", "delete_noise_marker"),
    "labs": (None, "labs.results", "delete_result"),
    "milestones": (None, "milestones.goals", "delete_milestone"),
    "nutrition": ("nutrition", "nutrition.writes", "delete_meal"),
    "glp1": ("glp1", "glp1.writes", "delete_injection"),
    "glp1_side_effect": ("glp1", "glp1.writes", "delete_side_effect"),
    "glp1_dose_phase": ("glp1", "glp1.writes", "delete_dose_phase"),
    "hrt_dose": ("hrt", "hrt.records", "delete_dose"),
    "hrt_side_effect": ("hrt", "hrt.records", "delete_side_effect"),
    "hrt_cycle": ("hrt", "hrt.cycles", "delete_cycle"),
    "hrt_cycle_item": ("hrt", "hrt.cycles", "delete_cycle_item"),
    "body_comp": ("body_comp", "body_scan.scans.writes", "delete_scan"),
    "timeline": ("timeline", "timeline.annotations", "delete_annotation"),
    "skincare_observation": (
        "skincare",
        "skincare.writes",
        "delete_observation",
    ),
    "supplements": ("supplements", "supplements.writes", "delete_supplement"),
    "genetics": ("genetics", "genetics.writes", "delete_variant"),
}


__all__ = ["DELETE_TARGETS", "NOTE_MODELS"]
