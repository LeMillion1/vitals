"""Static dependency contracts for the Garmin bounded-context package."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GARMIN_PACKAGE = ROOT / "vitals" / "services" / "garmin"


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


def test_garmin_error_contract_is_model_and_delivery_independent() -> None:
    imports: set[str] = set()
    for node in ast.walk(_tree(GARMIN_PACKAGE / "errors.py")):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert not {
        name
        for name in imports
        if name.startswith(("sqlalchemy", "vitals.models", "vitals.services", "web"))
    }


def test_reusable_garmin_leaves_do_not_own_transactions() -> None:
    offenders: list[str] = []
    for path in GARMIN_PACKAGE.glob("*.py"):
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


def test_garmin_query_apis_require_keyword_only_subject_scope() -> None:
    from vitals.services.garmin import queries

    for name in (
        "get_daily",
        "list_daily",
        "list_daily_between",
        "list_nights",
        "list_activities",
        "list_intraday",
        "intraday_series_map",
        "latest_daily",
        "adjacent_night_dates",
        "daily_count",
        "recovery_summary",
    ):
        parameter = inspect.signature(getattr(queries, name)).parameters[
            "subject_id"
        ]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameter.default is inspect.Parameter.empty, name


def test_garmin_mcp_range_query_seams_are_keyword_only_and_optional() -> None:
    from vitals.services.garmin import queries

    for name in ("list_daily", "list_activities"):
        parameters = inspect.signature(getattr(queries, name)).parameters
        for range_name in ("start", "end"):
            parameter = parameters[range_name]
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
            assert parameter.default is None, name
        assert parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY, name


def test_garmin_query_callers_always_pass_subject_scope() -> None:
    query_names = {
        "get_daily",
        "list_daily",
        "list_daily_between",
        "list_nights",
        "list_activities",
        "list_intraday",
        "intraday_series_map",
        "latest_daily",
        "adjacent_night_dates",
        "daily_count",
        "recovery_summary",
    }
    offenders: list[str] = []
    for source_root in ("vitals", "web", "scripts", "tests"):
        for path in (ROOT / source_root).rglob("*.py"):
            for node in ast.walk(_tree(path)):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Attribute):
                    continue
                if not isinstance(node.func.value, ast.Name):
                    continue
                if node.func.value.id != "garmin_queries":
                    continue
                if node.func.attr not in query_names:
                    continue
                if not any(keyword.arg == "subject_id" for keyword in node.keywords):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


def test_garmin_leaves_do_not_reverse_import_facade_or_package_aggregate() -> None:
    for path in GARMIN_PACKAGE.glob("*.py"):
        if path.name == "__init__.py":
            continue
        imports = _imports(path)
        assert "vitals.services.garmin_service" not in imports
        assert "vitals.services.garmin" not in imports


def test_garmin_normalization_is_persistence_and_service_independent() -> None:
    imports = _imports(GARMIN_PACKAGE / "normalization.py")
    assert not {
        name
        for name in imports
        if name.startswith(("sqlalchemy", "vitals.models", "vitals.services", "web"))
    }


def test_garmin_flat_facade_is_removed() -> None:
    assert not (ROOT / "vitals" / "services" / "garmin_service.py").exists()


def test_garmin_production_query_callers_use_owning_leaf() -> None:
    query_names = {
        "get_daily",
        "list_daily",
        "list_daily_between",
        "list_nights",
        "list_activities",
        "list_intraday",
        "intraday_series_map",
        "latest_daily",
        "adjacent_night_dates",
        "daily_count",
        "recovery_summary",
    }
    offenders: list[str] = []
    for source_root in ("vitals", "web", "scripts"):
        for path in (ROOT / source_root).rglob("*.py"):
            for node in ast.walk(_tree(path)):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id != "garmin_queries"
                    and node.func.attr in query_names
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


def test_garmin_normalization_series_keys_match_model_contract() -> None:
    from vitals.models import garmin as model
    from vitals.services.garmin import normalization

    for name in (
        "SERIES_STRESS",
        "SERIES_BODY_BATTERY",
        "SERIES_HEART_RATE",
        "SERIES_SLEEP_HR",
        "SERIES_SLEEP_SPO2",
        "SERIES_SLEEP_RESPIRATION",
        "SERIES_SLEEP_STRESS",
        "SERIES_SLEEP_BB",
        "SERIES_SLEEP_HRV",
        "SERIES_SLEEP_MOVEMENT",
    ):
        assert getattr(normalization, name) == getattr(model, name)


def test_clinical_audience_projections_do_not_import_garmin_orm_models() -> None:
    audience_paths = (
        ROOT / "vitals" / "services" / "care" / "record_projection.py",
        ROOT / "vitals" / "services" / "emergency" / "projection.py",
    )

    assert {
        path.relative_to(ROOT).as_posix()
        for path in audience_paths
        if "vitals.models.garmin" in _imports(path)
    } == set()
