"""Reviewable compatibility ratchets for the public MCP v2 surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

mcp_router = pytest.importorskip("web.routers.mcp")


FIXTURE = Path(__file__).parent / "fixtures" / "mcp_tools_v2.json"


def _canonical_tools() -> list[dict]:
    """Return the client-visible, schema-bearing part of the tool registry."""

    tools = mcp_router.mcp._tool_manager.list_tools()
    return [
        {
            "name": tool.name,
            "description": mcp_router._described_for_a_model(tool).description,
            "input_schema": tool.parameters,
            "output_schema": tool.output_schema,
        }
        for tool in tools
    ]


def _canonical_access(catalog: dict) -> dict[str, list[str]]:
    return {
        name: [
            f"{scope.resource_type.value}:{scope.resource_key}:{scope.action.value}"
            for scope in scopes
        ]
        for name, scopes in catalog.items()
    }


def test_mcp_v2_tool_surface_matches_reviewable_fixture() -> None:
    """Names, order, descriptions, defaults and schemas change deliberately."""

    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert expected["server_version"] == mcp_router.MCP_SERVER_VERSION
    assert expected["tools"] == _canonical_tools()
    assert expected["tool_access"] == _canonical_access(mcp_router.TOOL_ACCESS)
    assert expected["resource_access"] == _canonical_access(
        mcp_router.RESOURCE_ACCESS
    )
    assert expected["prompt_access"] == _canonical_access(mcp_router.PROMPT_ACCESS)


EXPECTED_TOOL_MODULES = {
    "add_dose_phase": "glp1",
    "add_hrt_cycle": "hrt",
    "add_hrt_cycle_item": "hrt",
    "add_supplement": "supplements",
    "close_hrt_cycle": "hrt",
    "get_body_metric_history": "body_comp",
    "get_body_scan": "body_comp",
    "get_body_scans": "body_comp",
    "get_genetics_snps": "genetics",
    "get_glp1_logs": "glp1",
    "get_hevy_workouts": "hevy",
    "get_hrt_cycles": "hrt",
    "get_hrt_logs": "hrt",
    "get_nutrition_summary": "nutrition",
    "get_skincare_logs": "skincare",
    "get_supplements_catalog": "supplements",
    "get_timeline": "timeline",
    "log_body_scan": "body_comp",
    "log_event": "timeline",
    "log_glp1": "glp1",
    "log_hrt_dose": "hrt",
    "log_hrt_side_effect": "hrt",
    "log_meal": "nutrition",
    "log_side_effect": "glp1",
    "log_skincare": "skincare",
    "log_skincare_observation": "skincare",
    "search_meals": "nutrition",
    "set_supplement_active": "supplements",
    "sync_hevy": "hevy",
    "update_event": "timeline",
    "update_glp1": "glp1",
    "update_hrt_dose": "hrt",
    "update_meal": "nutrition",
    "update_supplement": "supplements",
    "upsert_genetic_variant": "genetics",
}

EXPECTED_DELETE_TARGETS = {
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

EXPECTED_NOTE_MODELS = {
    "weight": "WeightLog",
    "nutrition": "MealLog",
    "glp1": "Injection",
    "skincare": "SkincareLog",
    "measurement": "BodyMeasurement",
    "body_comp": "BodyScan",
    "labs": "LabResult",
}


def test_mcp_catalogs_are_exact_and_complete() -> None:
    """Every registered tool is authorized and every dynamic hub stays complete."""

    registered = [tool["name"] for tool in _canonical_tools()]
    assert len(registered) == len(set(registered)) == 69
    assert set(mcp_router.TOOL_ACCESS) == set(registered)
    assert mcp_router.TOOL_MODULES == EXPECTED_TOOL_MODULES
    assert set(mcp_router.TOOL_MODULES).issubset(registered)
    assert mcp_router._DELETE_TARGETS == EXPECTED_DELETE_TARGETS
    assert {
        domain: model.__name__ for domain, model in mcp_router._NOTE_MODELS.items()
    } == EXPECTED_NOTE_MODELS
    assert set(mcp_router.RESOURCE_ACCESS) == {
        "vitals://profile",
        "vitals://digest/latest",
    }
    assert set(mcp_router.PROMPT_ACCESS) == {"weekly_review"}


def _registered_surface() -> tuple[list[str], list[str], list[str]]:
    return (
        [tool.name for tool in mcp_router.mcp._tool_manager.list_tools()],
        [str(resource.uri) for resource in mcp_router.mcp._resource_manager.list_resources()],
        [prompt.name for prompt in mcp_router.mcp._prompt_manager.list_prompts()],
    )


def test_get_mcp_app_does_not_register_the_surface_again() -> None:
    """Transport apps may be built repeatedly around one registered server."""

    before = _registered_surface()
    first_app, first_lifespan = mcp_router.get_mcp_app()
    middle = _registered_surface()
    second_app, second_lifespan = mcp_router.get_mcp_app()
    after = _registered_surface()

    assert before == middle == after
    assert before[1] == ["vitals://profile", "vitals://digest/latest"]
    assert before[2] == ["weekly_review"]
    assert first_app is not second_app
    assert callable(first_lifespan) and callable(second_lifespan)
