"""Pure module manifest and safe default state.

This leaf has no persistence, cache, web, or localization dependencies.  It is
the single source of truth for module identity, category, route, and navigation
group membership.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleSpec:
    key: str
    category: str
    route: str
    rubric: str = ""
    eyebrow: str = ""


NAV_RUBRICS: tuple[str, ...] = ("health", "markers", "lifestyle")

MODULE_REGISTRY: dict[str, ModuleSpec] = {
    module.key: module
    for module in (
        ModuleSpec("weight", "core", "/weight", "health"),
        ModuleSpec("garmin", "core", "/garmin", "health"),
        ModuleSpec("hevy", "optional", "/hevy", "health"),
        ModuleSpec("nutrition", "optional", "/nutrition", "health"),
        ModuleSpec("timeline", "optional", "/timeline", "health"),
        ModuleSpec("reports", "core", "/reports", "health", eyebrow="digest"),
        ModuleSpec("charts", "core", "/charts", "health"),
        ModuleSpec("glp1", "optional", "/glp1", "markers"),
        ModuleSpec("hrt", "optional", "/hrt", "markers"),
        ModuleSpec("labs", "core", "/labs", "markers"),
        ModuleSpec("genetics", "optional", "/genetics", "markers"),
        ModuleSpec("supplements", "optional", "/supplements", "lifestyle"),
        ModuleSpec("skincare", "optional", "/skincare", "lifestyle"),
        ModuleSpec("interactions", "optional", "/interactions", "lifestyle"),
        ModuleSpec("body_comp", "optional", "/weight"),
    )
}

CORE_KEYS: frozenset[str] = frozenset(
    key for key, spec in MODULE_REGISTRY.items() if spec.category == "core"
)
OPTIONAL_KEYS: frozenset[str] = frozenset(
    key for key, spec in MODULE_REGISTRY.items() if spec.category == "optional"
)
DEFAULT_STATE: dict[str, bool] = {
    **{key: True for key in CORE_KEYS},
    **{key: False for key in OPTIONAL_KEYS},
}

__all__ = [
    "CORE_KEYS",
    "DEFAULT_STATE",
    "MODULE_REGISTRY",
    "ModuleSpec",
    "NAV_RUBRICS",
    "OPTIONAL_KEYS",
]
