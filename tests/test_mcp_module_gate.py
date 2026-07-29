"""A switched-off module must stay off for the tool surface too.

Turning a module off in settings is the owner saying "I don't track this". The web
routes honour it; the tool surface honoured it on three writes out of forty, so a
conversation could refill a domain the owner had just emptied out of the UI.

The classification test below is the part that lasts: a newly added write tool has
to be listed as gated or explicitly excused, so "don't forget the check" stops
being a thing anyone has to remember.
"""
from __future__ import annotations

import inspect

import pytest

mcp_router = pytest.importorskip("web.routers.mcp")


@pytest.fixture(autouse=True)
def _use_test_factory(session_factory, monkeypatch):
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)


# tool name → the optional module key it writes into
GATED_WRITE_TOOLS: dict[str, str] = {
    "log_meal": "nutrition",
    "update_meal": "nutrition",
    "delete_meal": "nutrition",
    "log_glp1": "glp1",
    "update_glp1": "glp1",
    "delete_glp1": "glp1",
    "log_side_effect": "glp1",
    "delete_side_effect": "glp1",
    "add_dose_phase": "glp1",
    "delete_dose_phase": "glp1",
    "log_hrt_dose": "hrt",
    "add_hrt_cycle": "hrt",
    "add_hrt_cycle_item": "hrt",
    "log_skincare": "skincare",
    "log_skincare_observation": "skincare",
    "delete_skincare_observation": "skincare",
    "add_supplement": "supplements",
    "update_supplement": "supplements",
    "set_supplement_active": "supplements",
    "delete_supplement": "supplements",
    "log_body_scan": "body_comp",
    "delete_body_scan": "body_comp",
    "log_event": "timeline",
    "update_event": "timeline",
    "delete_event": "timeline",
    "log_signal": "signals",
    "delete_signal": "signals",
    "mark_signal_misparse": "signals",
    # Day context lives in the signals domain, which is also the master switch for
    # the whole proactive layer — off means the day is not being tracked at all.
    "log_day_context": "signals",
}

# Write tools that deliberately have no module gate, each with the reason.
UNGATED_WRITE_TOOLS: dict[str, str] = {
    "log_weight": "weight is a core module — always on",
    "delete_weight": "weight is core",
    "log_measurement": "weight is core",
    "update_measurement": "weight is core",
    "delete_measurement": "weight is core",
    "add_noise_marker": "weight is core",
    "delete_noise_marker": "weight is core",
    "log_lab_result": "labs is core",
    "log_lab_results": "labs is core",
    "update_lab_result": "labs is core",
    "delete_lab_result": "labs is core",
    "log_note": "writes the note column of an already-existing row in any domain",
    "create_milestone": "goals are core",
    "update_milestone": "goals are core",
    "delete_milestone": "goals are core",
    "resolve_alert": "alerts are raised by every domain, core ones included",
    "override_alert": "same — an alert is not owned by an optional module",
    "set_module": "this is the toggle itself",
    "generate_digest_now": "reports is core",
}

_WRITE_PREFIXES = ("log_", "add_", "create_", "update_", "delete_", "set_", "mark_",
                   "close_", "resolve_", "override_", "generate_")


def _write_tool_names() -> set[str]:
    return {
        name
        for name in dir(mcp_router)
        if name.startswith(_WRITE_PREFIXES)
        and inspect.iscoroutinefunction(getattr(mcp_router, name))
        and getattr(mcp_router, name).__module__ == mcp_router.__name__
    }


def test_every_write_tool_is_classified():
    """A new write tool must be listed as gated or explicitly excused — otherwise
    it ships ungated and nobody notices until a disabled domain fills up again."""
    assert _write_tool_names() == set(GATED_WRITE_TOOLS) | set(UNGATED_WRITE_TOOLS)


@pytest.mark.parametrize("tool_name,module_key", sorted(GATED_WRITE_TOOLS.items()))
async def test_gated_write_tool_refuses_when_module_is_off(tool_name, module_key):
    """Modules default to off for optional domains, so no setup is needed: every
    one of these must refuse before touching the database."""
    tool = getattr(mcp_router, tool_name)
    # Positional-only stand-ins for whatever required args the tool takes — the
    # gate fires before any of them is looked at.
    args = [1 for p in inspect.signature(tool).parameters.values()
            if p.default is inspect.Parameter.empty]
    result = await tool(*args)
    assert result == {"error": f"module '{module_key}' is disabled"}


async def test_gated_write_tool_works_once_the_module_is_on():
    await mcp_router.set_module("nutrition", True)
    row = await mcp_router.log_meal(name="Обед", calories=500, on_date="2026-07-01")
    assert row["calories"] == 500
