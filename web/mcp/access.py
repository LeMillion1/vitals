"""Declarative authorization catalog for the MCP delivery surface.

This module contains no request state and no database access.  It translates a
registered tool (and, for dynamic tools, its arguments) into the capabilities
that the request boundary must require.
"""

from __future__ import annotations

from vitals.access import AccessScope, PolicyAction, PolicyResourceType
from vitals.enums import Domain


def scope(
    resource_type: PolicyResourceType,
    resource_key: str,
    action: PolicyAction,
) -> AccessScope:
    return AccessScope(resource_type, resource_key, action)


def domain_scope(resource_key: str, action: PolicyAction) -> AccessScope:
    return scope(PolicyResourceType.DOMAIN, resource_key, action)


RECORD_DOMAIN_KEYS = tuple(
    domain.value for domain in Domain if domain is not Domain.SYSTEM
)
ALL_DOMAIN_LIST = tuple(
    domain_scope(domain, PolicyAction.LIST) for domain in RECORD_DOMAIN_KEYS
)
NOTE_DOMAIN_KEYS = (
    "weight",
    "nutrition",
    "glp1",
    "skincare",
    "weight",  # body measurements live in the weight domain
    "body_comp",
    "labs",
)


def _fixed_domain(
    domain: str,
    *,
    reads: tuple[str, ...] = (),
    creates: tuple[str, ...] = (),
    updates: tuple[str, ...] = (),
) -> dict[str, tuple[AccessScope, ...]]:
    return {
        **{name: (domain_scope(domain, PolicyAction.LIST),) for name in reads},
        **{name: (domain_scope(domain, PolicyAction.CREATE),) for name in creates},
        **{name: (domain_scope(domain, PolicyAction.UPDATE),) for name in updates},
    }


# Every tool has an authorization classification.  Empty tuples mark tools
# whose capability depends on an argument; they are resolved below and never
# mean that the tool is unrestricted.
TOOL_ACCESS: dict[str, tuple[AccessScope, ...]] = {
    **_fixed_domain(
        "weight",
        reads=("get_weight_logs", "get_measurements"),
        creates=("log_weight", "log_measurement", "add_noise_marker"),
        updates=("update_measurement",),
    ),
    **_fixed_domain(
        "body_comp",
        reads=("get_body_scans", "get_body_scan", "get_body_metric_history"),
        creates=("log_body_scan",),
    ),
    **_fixed_domain(
        "glp1",
        reads=("get_glp1_logs",),
        creates=("log_glp1", "log_side_effect", "add_dose_phase"),
        updates=("update_glp1",),
    ),
    **_fixed_domain(
        "supplements",
        reads=("get_supplements_catalog",),
        creates=("add_supplement",),
        updates=("update_supplement", "set_supplement_active"),
    ),
    **_fixed_domain(
        "genetics",
        reads=("get_genetics_snps",),
        creates=("upsert_genetic_variant",),
    ),
    **_fixed_domain(
        "skincare",
        reads=("get_skincare_logs",),
        creates=("log_skincare", "log_skincare_observation"),
    ),
    **_fixed_domain("workouts", reads=("get_hevy_workouts",)),
    **_fixed_domain("garmin", reads=("get_garmin_metrics",)),
    **_fixed_domain(
        "labs",
        reads=("get_lab_results",),
        creates=("log_lab_result", "log_lab_results"),
        updates=("update_lab_result",),
    ),
    **_fixed_domain(
        "nutrition",
        reads=("get_nutrition_summary", "search_meals"),
        creates=("log_meal",),
        updates=("update_meal",),
    ),
    **_fixed_domain(
        "hrt",
        reads=("get_hrt_logs", "get_hrt_cycles"),
        creates=(
            "log_hrt_dose",
            "log_hrt_side_effect",
            "add_hrt_cycle",
            "add_hrt_cycle_item",
        ),
        updates=("update_hrt_dose", "close_hrt_cycle"),
    ),
    **_fixed_domain(
        "milestones",
        reads=("get_milestones",),
        creates=("create_milestone",),
        updates=("update_milestone",),
    ),
    **_fixed_domain(
        "timeline",
        reads=("get_timeline",),
        creates=("log_event",),
        updates=("update_event",),
    ),
    "get_user_profile": (
        scope(PolicyResourceType.ARTIFACT, "health_profile", PolicyAction.READ),
    ),
    "get_active_alerts": (
        scope(PolicyResourceType.ARTIFACT, "safety_alert", PolicyAction.READ),
    ),
    "resolve_alert": (
        scope(PolicyResourceType.ARTIFACT, "safety_alert", PolicyAction.UPDATE),
    ),
    "override_alert": (
        scope(PolicyResourceType.ARTIFACT, "safety_alert", PolicyAction.UPDATE),
    ),
    "get_weekly_digests": (
        scope(PolicyResourceType.ARTIFACT, "weekly_digest", PolicyAction.LIST),
    ),
    "check_supplement_conflicts": (
        scope(PolicyResourceType.OPERATION, "conflict.check", PolicyAction.READ),
    ),
    "list_conflict_rules": (
        scope(PolicyResourceType.OPERATION, "conflict.check", PolicyAction.READ),
    ),
    "get_full_snapshot": ALL_DOMAIN_LIST,
    "get_data_overview": ALL_DOMAIN_LIST,
    "export_everything": (
        scope(PolicyResourceType.OPERATION, "record.export", PolicyAction.EXPORT),
    ),
    "get_modules": (
        scope(PolicyResourceType.OPERATION, "modules", PolicyAction.READ),
    ),
    "set_module": (
        scope(PolicyResourceType.OPERATION, "modules", PolicyAction.UPDATE),
    ),
    "generate_digest_now": (
        scope(PolicyResourceType.ARTIFACT, "weekly_digest", PolicyAction.CREATE),
        *ALL_DOMAIN_LIST,
    ),
    "get_proactive_state": (
        scope(PolicyResourceType.OPERATION, "proactive", PolicyAction.READ),
    ),
    "sync_garmin": (
        scope(PolicyResourceType.OPERATION, "garmin.sync", PolicyAction.SYNC),
    ),
    "sync_hevy": (
        scope(PolicyResourceType.OPERATION, "hevy.sync", PolicyAction.SYNC),
    ),
    "check_conflicts": (),
    "delete_record": (),
    "get_notes": (),
    "get_trend": (),
    "log_note": (),
}


ARGUMENT_DOMAIN_ALIASES = {
    "measurement": "weight",
    "measurements": "weight",
    "noise_marker": "weight",
    "body_scans": "body_comp",
    "glp1_injection": "glp1",
    "glp1_side_effect": "glp1",
    "glp1_dose_phase": "glp1",
    "hrt_dose": "hrt",
    "hrt_side_effect": "hrt",
    "hrt_cycle": "hrt",
    "hrt_cycle_item": "hrt",
    "skincare_observation": "skincare",
    "hevy": "workouts",
}


def argument_domain(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    key = value.split(".", 1)[0]
    key = ARGUMENT_DOMAIN_ALIASES.get(key, key)
    return key if key in RECORD_DOMAIN_KEYS else None


def required_tool_scopes(
    name: str, arguments: dict[str, object]
) -> tuple[AccessScope, ...] | None:
    fixed = TOOL_ACCESS.get(name)
    if fixed is None:
        return None
    if fixed:
        return fixed
    if name == "delete_record":
        domain = argument_domain(arguments.get("domain"))
        return (domain_scope(domain, PolicyAction.DELETE),) if domain else None
    if name == "log_note":
        domain = argument_domain(arguments.get("domain"))
        return (domain_scope(domain, PolicyAction.UPDATE),) if domain else None
    if name == "get_notes":
        supplied = arguments.get("domain")
        if supplied is None:
            return tuple(
                domain_scope(domain, PolicyAction.LIST)
                for domain in dict.fromkeys(NOTE_DOMAIN_KEYS)
            )
        domain = argument_domain(supplied)
        return (domain_scope(domain, PolicyAction.LIST),) if domain else None
    if name == "check_conflicts":
        domain = argument_domain(arguments.get("domain"))
        if domain is None:
            return None
        return (
            scope(PolicyResourceType.OPERATION, "conflict.check", PolicyAction.READ),
            domain_scope(domain, PolicyAction.READ),
        )
    if name == "get_trend":
        domain = argument_domain(arguments.get("metric_key"))
        return (domain_scope(domain, PolicyAction.READ),) if domain else None
    return None


def surface_allowed(
    required: tuple[AccessScope, ...] | None,
    granted: frozenset[AccessScope] | None,
) -> bool:
    """Return whether a resolved grant admits one fixed or dynamic surface."""

    if granted is None:
        return True
    return required is not None and set(required).issubset(granted)


def tool_listing_allowed(
    name: str,
    granted: frozenset[AccessScope] | None,
) -> bool:
    """Decide whether a tool can be advertised without knowing its arguments."""

    if granted is None:
        return True
    fixed = TOOL_ACCESS.get(name)
    if fixed is None:
        return False
    if fixed:
        return set(fixed).issubset(granted)
    if name == "delete_record":
        action = PolicyAction.DELETE
    elif name == "log_note":
        action = PolicyAction.UPDATE
    elif name in {"get_notes", "get_trend"}:
        action = PolicyAction.LIST if name == "get_notes" else PolicyAction.READ
    elif name == "check_conflicts":
        operation = scope(
            PolicyResourceType.OPERATION, "conflict.check", PolicyAction.READ
        )
        return operation in granted and any(
            item.resource_type is PolicyResourceType.DOMAIN
            and item.action is PolicyAction.READ
            for item in granted
        )
    else:
        return False
    return any(
        item.resource_type is PolicyResourceType.DOMAIN and item.action is action
        for item in granted
    )


RESOURCE_ACCESS = {
    "vitals://profile": (
        scope(PolicyResourceType.ARTIFACT, "health_profile", PolicyAction.READ),
    ),
    "vitals://digest/latest": (
        scope(PolicyResourceType.ARTIFACT, "weekly_digest", PolicyAction.READ),
    ),
}

PROMPT_ACCESS = {"weekly_review": ALL_DOMAIN_LIST}


__all__ = [
    "ALL_DOMAIN_LIST",
    "ARGUMENT_DOMAIN_ALIASES",
    "NOTE_DOMAIN_KEYS",
    "PROMPT_ACCESS",
    "RECORD_DOMAIN_KEYS",
    "RESOURCE_ACCESS",
    "TOOL_ACCESS",
    "argument_domain",
    "domain_scope",
    "required_tool_scopes",
    "scope",
    "surface_allowed",
    "tool_listing_allowed",
]
