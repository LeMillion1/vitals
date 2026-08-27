"""Static dependency contracts for the Hevy bounded-context package."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEVY_PACKAGE = ROOT / "vitals" / "services" / "hevy"
LEGACY_FACADE = ROOT / "vitals" / "services" / "hevy_service.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(path: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def _called_attributes(path: Path, names: set[str]) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []
    for node in ast.walk(_tree(path)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in names
        ):
            calls.append((node.func.attr, node.lineno))
    return calls


def test_hevy_normalization_is_a_pure_leaf() -> None:
    imports = _imported_modules(HEVY_PACKAGE / "normalization.py")
    forbidden_roots = (
        "sqlalchemy",
        "vitals.models",
        "vitals.services",
        "web",
    )
    assert not {
        imported
        for imported in imports
        if any(
            imported == root or imported.startswith(f"{root}.")
            for root in forbidden_roots
        )
    }


def test_hevy_ownership_does_not_depend_on_delivery_or_peer_services() -> None:
    imports = _imported_modules(HEVY_PACKAGE / "ownership.py")
    forbidden_roots = ("vitals.services", "web")
    assert not {
        imported
        for imported in imports
        if any(
            imported == root or imported.startswith(f"{root}.")
            for root in forbidden_roots
        )
    }


def test_hevy_ingestion_leaves_do_not_own_network_or_outer_transactions() -> None:
    offenders: list[str] = []
    for filename in ("ingestion.py", "persistence.py", "raw_payloads.py"):
        path = HEVY_PACKAGE / filename
        for attr, lineno in _called_attributes(
            path, {"commit", "rollback", "fetch_workouts"}
        ):
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}:{attr}")
    assert offenders == []


def test_hevy_ingestion_leaves_do_not_depend_on_delivery_or_provider_client() -> None:
    forbidden_roots = (
        "vitals.integrations",
        "vitals.services.identity",
        "web",
    )
    offenders: dict[str, set[str]] = {}
    for filename in ("ingestion.py", "persistence.py", "raw_payloads.py"):
        imports = _imported_modules(HEVY_PACKAGE / filename)
        forbidden = {
            imported
            for imported in imports
            if any(
                imported == root or imported.startswith(f"{root}.")
                for root in forbidden_roots
            )
        }
        if forbidden:
            offenders[filename] = forbidden
    assert offenders == {}


def test_hevy_network_fetch_is_isolated_in_a_session_free_helper() -> None:
    sync_path = HEVY_PACKAGE / "sync.py"
    owners: list[tuple[str, list[str]]] = []
    for node in _tree(sync_path).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "fetch_workouts"
            for child in ast.walk(node)
        ):
            owners.append((node.name, [arg.arg for arg in node.args.args]))
    assert owners == [("_fetch_provider_workouts", ["client"])]


def test_hevy_sync_does_not_own_commit_or_rollback() -> None:
    assert _called_attributes(HEVY_PACKAGE / "sync.py", {"commit", "rollback"}) == []


def test_only_hevy_jobs_owns_outer_transaction_boundaries() -> None:
    owners: dict[str, set[str]] = {}
    for path in sorted(HEVY_PACKAGE.glob("*.py")):
        calls = _called_attributes(path, {"commit", "rollback"})
        if calls:
            owners[path.name] = {name for name, _lineno in calls}
    assert owners == {"jobs.py": {"commit", "rollback"}}


def test_hevy_package_does_not_depend_on_web() -> None:
    offenders = {
        path.name: {
            imported
            for imported in _imported_modules(path)
            if imported == "web" or imported.startswith("web.")
        }
        for path in sorted(HEVY_PACKAGE.glob("*.py"))
    }
    assert {name: imports for name, imports in offenders.items() if imports} == {}


def test_hevy_package_init_is_not_an_aggregate_api() -> None:
    assert _imported_modules(HEVY_PACKAGE / "__init__.py") == set()


def test_hevy_list_workouts_owns_subject_and_date_window() -> None:
    function = next(
        node
        for node in _tree(HEVY_PACKAGE / "queries.py").body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "list_workouts"
    )
    assert [arg.arg for arg in function.args.kwonlyargs] == [
        "subject_id",
        "start",
        "end",
        "limit",
    ]


def test_hevy_workout_window_summary_requires_exact_subject_and_window() -> None:
    function = next(
        node
        for node in _tree(HEVY_PACKAGE / "queries.py").body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "workout_window_summary"
    )
    assert [arg.arg for arg in function.args.kwonlyargs] == [
        "subject_id",
        "start",
        "end",
    ]


def test_clinical_audience_projections_do_not_import_hevy_orm_models() -> None:
    audience_paths = (
        ROOT / "vitals" / "services" / "care" / "record_projection.py",
        ROOT / "vitals" / "services" / "emergency" / "projection.py",
    )

    assert {
        path.relative_to(ROOT).as_posix()
        for path in audience_paths
        if "vitals.models.hevy" in _imported_modules(path)
    } == set()


def test_legacy_hevy_service_facade_is_removed() -> None:
    assert not LEGACY_FACADE.exists()
