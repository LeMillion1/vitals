"""Static dependency contracts for the Weight bounded-context package."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEIGHT_PACKAGE = ROOT / "vitals" / "services" / "weight"


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


def _leaf_paths() -> list[Path]:
    return sorted(
        path
        for path in WEIGHT_PACKAGE.glob("*.py")
        if path.name != "__init__.py"
    )


def _top_level_definitions(path: Path) -> set[str]:
    return {
        node.name
        for node in _tree(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def test_weight_contracts_do_not_depend_on_models_or_delivery() -> None:
    imports = _imports(WEIGHT_PACKAGE / "contracts.py")
    forbidden = {
        name
        for name in imports
        if name == "web"
        or name.startswith("web.")
        or name == "fastapi"
        or name.startswith("fastapi.")
        or name == "vitals.models"
        or name.startswith("vitals.models.")
    }
    assert forbidden == set()


def test_weight_leaves_do_not_own_transactions() -> None:
    offenders: list[str] = []
    for path in _leaf_paths():
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"commit", "rollback"}
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


def test_weight_leaves_do_not_import_the_package_aggregate() -> None:
    offenders = {
        path.name
        for path in _leaf_paths()
        if "vitals.services.weight" in _imports(path)
    }
    assert offenders == set()


def test_weight_leaves_do_not_import_the_legacy_facade() -> None:
    offenders: list[str] = []
    for path in _leaf_paths():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Import):
                if any(
                    alias.name == "vitals.services.weight_service"
                    for alias in node.names
                ):
                    offenders.append(path.name)
            elif isinstance(node, ast.ImportFrom) and node.module == "vitals.services":
                if any(alias.name == "weight_service" for alias in node.names):
                    offenders.append(path.name)
    assert offenders == []


def test_weight_log_and_query_surfaces_have_leaf_owners() -> None:
    logs = WEIGHT_PACKAGE / "logs.py"
    queries = WEIGHT_PACKAGE / "queries.py"
    writes = WEIGHT_PACKAGE / "writes.py"
    analytics = WEIGHT_PACKAGE / "analytics.py"

    log_surfaces = {
        "_assert_weight_scope_integrity",
        "_get_weight_log_date_in_scope",
        "_get_weight_log_for_update",
        "_validate_new_weight_provenance",
        "_validate_persisted_weight_provenance",
        "_weight_scope_condition",
        "get_active_weight",
        "list_active_weights",
    }
    query_surfaces = {
        "BoundedWeightProjection",
        "WeightProjectionNoiseRange",
        "WeightProjectionPoint",
        "care_weight_history",
        "emergency_weight_history",
        "list_weight_notes",
        "resolve_active_scoped",
    }
    write_surfaces = {
        "delete_weight_log",
        "log_weight",
        "project_body_scan_weight",
        "update_weight_log",
        "update_weight_note",
    }
    analytics_surfaces = {"chart_series"}

    assert log_surfaces <= _top_level_definitions(logs)
    assert query_surfaces <= _top_level_definitions(queries)
    assert write_surfaces <= _top_level_definitions(writes)
    assert analytics_surfaces <= _top_level_definitions(analytics)
    assert not (ROOT / "vitals" / "services" / "weight_service.py").exists()


def test_clinical_audience_projections_do_not_import_weight_orm_models() -> None:
    audience_paths = (
        ROOT / "vitals" / "services" / "care" / "record_projection.py",
        ROOT / "vitals" / "services" / "emergency" / "projection.py",
    )

    offenders = {
        path.relative_to(ROOT).as_posix()
        for path in audience_paths
        if "vitals.models.weight" in _imports(path)
    }

    assert offenders == set()
