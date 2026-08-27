"""Pure navigation projections derived from the module registry."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .registry import MODULE_REGISTRY, NAV_RUBRICS, ModuleSpec


def nav_modules(
    enabled: Optional[dict[str, bool]] = None,
    *,
    rubric: Optional[str] = None,
) -> list[ModuleSpec]:
    """Return visible navigation entries in registry order."""

    state = enabled or {}
    return [
        spec
        for spec in MODULE_REGISTRY.values()
        if spec.rubric
        and (spec.category == "core" or state.get(spec.key))
        and (rubric is None or spec.rubric == rubric)
    ]


BOTTOM_SLOT_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("health", "rubric"),
    ("nutrition", "module"),
    ("lifestyle", "rubric"),
    ("markers", "rubric"),
)
BOTTOM_SLOT_COUNT = 3


@dataclass(frozen=True)
class NavSlot:
    key: str
    label_key: str
    icon: str
    route: str
    routes: tuple[str, ...]


def bottom_slots(enabled: Optional[dict[str, bool]] = None) -> list[NavSlot]:
    """Return the three dynamic middle slots of the mobile bottom bar."""

    state = enabled or {}
    own_column = {
        MODULE_REGISTRY[key].route
        for key, kind in BOTTOM_SLOT_CANDIDATES
        if kind == "module"
    }
    slots: list[NavSlot] = []
    for key, kind in BOTTOM_SLOT_CANDIDATES:
        if len(slots) == BOTTOM_SLOT_COUNT:
            break
        if kind == "rubric":
            members = nav_modules(state, rubric=key)
            if not members:
                continue
            routes = tuple(member.route for member in members if member.route not in own_column)
            slots.append(
                NavSlot(
                    key=key,
                    label_key=f"nav.tab.{key}",
                    icon=members[0].key,
                    route=members[0].route,
                    routes=routes or tuple(member.route for member in members),
                )
            )
        else:
            spec = MODULE_REGISTRY[key]
            if spec.category != "core" and not state.get(key):
                continue
            slots.append(
                NavSlot(
                    key=key,
                    label_key=f"nav.{key}",
                    icon=key,
                    route=spec.route,
                    routes=(spec.route,),
                )
            )
    return slots


def more_rubrics(enabled: Optional[dict[str, bool]] = None) -> list[str]:
    """Return rubrics not represented by a bottom-bar slot."""

    taken = {slot.key for slot in bottom_slots(enabled)}
    return [
        rubric
        for rubric in NAV_RUBRICS
        if rubric not in taken and nav_modules(enabled, rubric=rubric)
    ]


def more_routes(enabled: Optional[dict[str, bool]] = None) -> tuple[str, ...]:
    """Return prefixes that activate the bottom bar's More slot."""

    return ("/more", "/settings") + tuple(
        spec.route
        for rubric in more_rubrics(enabled)
        for spec in nav_modules(enabled, rubric=rubric)
    )


__all__ = [
    "BOTTOM_SLOT_CANDIDATES",
    "BOTTOM_SLOT_COUNT",
    "NavSlot",
    "bottom_slots",
    "more_routes",
    "more_rubrics",
    "nav_modules",
]
