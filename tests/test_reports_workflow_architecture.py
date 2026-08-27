"""The Reports router is an HTTP adapter, not an AI/delivery orchestrator."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from vitals.services.proactive import report_workflows


ROOT = Path(__file__).parents[1]
ROUTER = ROOT / "web" / "routers" / "reports.py"
WORKFLOW = ROOT / "vitals" / "services" / "proactive" / "report_workflows.py"
WORKFLOW_ROUTES = {
    "generate_digest_now",
    "build_brief_now",
    "send_test_brief",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_reports_ai_routes_only_delegate_and_map_typed_outcomes():
    functions = {
        node.name: node
        for node in _tree(ROUTER).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert WORKFLOW_ROUTES <= functions.keys()

    for name in WORKFLOW_ROUTES:
        node = functions[name]
        attributes = {
            child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)
        }
        names = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
        assert not {"commit", "rollback"} & attributes
        assert not {
            "ai_gateway_service",
            "channels",
            "delivery",
            "digest_generation",
            "digest_ownership",
        } & names
        assert "report_workflows" in names


def test_reports_router_has_no_ai_gateway_or_delivery_internals():
    source = ROUTER.read_text(encoding="utf-8")
    assert "ai_gateway_service" not in source
    assert "_run_brief_generation" not in source
    assert "start_digest_dispatch" not in source
    assert "start_delivery_dispatch" not in source
    assert "finalize_delivery" not in source


def test_reports_application_workflow_does_not_depend_on_web():
    imports = set()
    for node in ast.walk(_tree(WORKFLOW)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert not any(name == "web" or name.startswith("web.") for name in imports)
    assert not any(name == "fastapi" or name.startswith("fastapi.") for name in imports)


@pytest.mark.parametrize(
    ("route_name", "workflow_name", "outcome", "expected"),
    [
        (
            "generate_digest_now",
            "generate_digest",
            report_workflows.DigestWorkflowOutcome.QUOTA,
            "/reports?digest=quota",
        ),
        (
            "build_brief_now",
            "build_brief",
            report_workflows.BriefWorkflowOutcome.HEADER,
            "/reports?brief=header",
        ),
        (
            "send_test_brief",
            "send_test_brief",
            report_workflows.BriefWorkflowOutcome.NO_CHANNEL,
            "/reports?brief=no_channel",
        ),
    ],
)
async def test_reports_routes_map_typed_workflow_outcomes(
    monkeypatch,
    route_name,
    workflow_name,
    outcome,
    expected,
):
    from web.routers import reports

    async def run_workflow(*args, **kwargs):
        del args, kwargs
        return outcome

    monkeypatch.setattr(report_workflows, workflow_name, run_workflow)
    route = getattr(reports, route_name)
    common = {
        "request": SimpleNamespace(headers={}),
        "db": object(),
        "username": "synthetic-actor",
        "_rl": None,
    }
    if route_name == "generate_digest_now":
        response = await route(period_days=7, **common)
    else:
        response = await route(request_token="synthetic_token_1234567890", **common)

    assert response.status_code == 303
    assert response.headers["location"] == expected
