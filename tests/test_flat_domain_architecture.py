"""Static ratchets for formerly flat health-domain service packages."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "vitals" / "services"
PACKAGES = {
    "glp1": {"jobs.py", "plateau.py", "queries.py", "writes.py"},
    "nutrition": {
        "analytics.py",
        "conflicts.py",
        "governance.py",
        "jobs.py",
        "queries.py",
        "writes.py",
    },
    "skincare": {"conflicts.py", "governance.py", "queries.py", "writes.py"},
    "timeline": {"annotations.py", "events.py"},
    "supplements": {
        "conflicts.py",
        "governance.py",
        "parsing.py",
        "queries.py",
        "writes.py",
    },
    "milestones": {"goals.py", "governance.py", "progress.py", "queries.py"},
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(path: Path) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _definitions(path: Path) -> set[str]:
    return {
        node.name
        for node in _tree(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def test_flat_service_modules_are_replaced_by_bounded_context_packages() -> None:
    for domain, leaves in PACKAGES.items():
        assert not (SERVICES / f"{domain}_service.py").exists()
        assert {
            path.name for path in (SERVICES / domain).glob("*.py") if path.name != "__init__.py"
        } == leaves


def test_new_domain_leaves_do_not_depend_on_delivery_or_legacy_facades() -> None:
    forbidden_legacy = {
        "vitals.services.glp1_service",
        "vitals.services.nutrition_service",
        "vitals.services.skincare_service",
    }
    for domain in PACKAGES:
        for path in (SERVICES / domain).glob("*.py"):
            if path.name == "__init__.py":
                continue
            imports = _imports(path)
            assert not imports.intersection(forbidden_legacy), path
            assert not {
                name
                for name in imports
                if name == "web"
                or name.startswith("web.")
                or name == "fastapi"
                or name.startswith("fastapi.")
            }, path


def test_new_domain_reusable_leaves_do_not_own_transactions() -> None:
    offenders: list[str] = []
    for domain in PACKAGES:
        for path in (SERVICES / domain).glob("*.py"):
            if path.name in {"__init__.py", "jobs.py"}:
                continue
            for node in ast.walk(_tree(path)):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"commit", "rollback"}
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


def test_new_domain_surface_owners_are_explicit() -> None:
    expected = {
        ("glp1", "queries.py"): {
            "list_injections",
            "list_dose_phases",
            "list_side_effects",
            "resolve_active_scoped",
        },
        ("glp1", "writes.py"): {
            "log_injection",
            "add_dose_phase",
            "log_side_effect",
            "delete_injection",
        },
        ("glp1", "plateau.py"): {"evaluate_plateau", "refresh_plateau_alert"},
        ("glp1", "jobs.py"): {"plateau_job"},
        ("nutrition", "queries.py"): {"list_meals", "list_meals_for_date"},
        ("nutrition", "writes.py"): {"log_meal", "update_meal", "delete_meal"},
        ("nutrition", "analytics.py"): {
            "daily_summary",
            "nutrition_summary",
            "macro_energy_shares",
        },
        ("nutrition", "conflicts.py"): {"resolve_today_scoped"},
        ("nutrition", "jobs.py"): {"day_end_job"},
        ("skincare", "queries.py"): {
            "get_log",
            "list_logs",
            "list_observations",
            "list_products",
        },
        ("skincare", "writes.py"): {
            "upsert_log",
            "add_observation",
            "add_product",
            "delete_product",
        },
        ("skincare", "conflicts.py"): {"resolve_today_scoped"},
        ("timeline", "annotations.py"): {
            "create_annotation",
            "update_annotation",
            "delete_annotation",
            "list_annotations",
            "overlays_for",
        },
        ("timeline", "events.py"): {"TimelineEvent", "_derived_events", "list_events"},
        ("supplements", "parsing.py"): {"_parse_slot", "timing_bucket"},
        ("supplements", "queries.py"): {"list_supplements", "get_supplement"},
        ("supplements", "writes.py"): {
            "add_supplement",
            "update_supplement",
            "set_active",
            "delete_supplement",
        },
        ("supplements", "conflicts.py"): {"resolve_active_scoped"},
        ("milestones", "queries.py"): {"list_milestones"},
        ("milestones", "goals.py"): {
            "create_milestone",
            "update_milestone",
            "set_status",
            "delete_milestone",
        },
        ("milestones", "progress.py"): {"progress", "dashboard_cards"},
    }
    for (domain, leaf), names in expected.items():
        assert names <= _definitions(SERVICES / domain / leaf)


def test_python_callers_do_not_reference_removed_service_modules() -> None:
    removed = {
        "glp1_service",
        "nutrition_service",
        "skincare_service",
        "timeline_service",
        "supplements_service",
        "milestones_service",
    }
    offenders: list[str] = []
    for base in (ROOT / "vitals", ROOT / "web", ROOT / "tests", ROOT / "scripts"):
        for path in base.rglob("*.py"):
            if path == Path(__file__):
                continue
            source = path.read_text(encoding="utf-8")
            if any(name in source for name in removed):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_timeline_annotation_records_are_separate_from_cross_domain_projection() -> None:
    annotations = SERVICES / "timeline" / "annotations.py"
    events = SERVICES / "timeline" / "events.py"

    assert "_derived_events" not in _definitions(annotations)
    assert {
        "create_annotation",
        "update_annotation",
        "delete_annotation",
    }.isdisjoint(_definitions(events))
    assert not {
        name
        for name in _imports(annotations)
        if name.startswith("vitals.models.") and name != "vitals.models.timeline"
    }
