"""Static contracts for the Garmin Weight bounded context and HTTP delivery."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = ROOT / "vitals" / "services" / "garmin_weight"

SERVICE_FILES = {
    "__init__.py",
    "contracts.py",
    "dispatch.py",
    "jobs.py",
    "outbox.py",
    "reconciliation.py",
    "settings.py",
}

TOP_LEVEL_DAG = {
    "contracts": set(),
    "outbox": {"contracts"},
    "reconciliation": {"contracts", "outbox"},
    "settings": {"contracts", "outbox"},
    "dispatch": {"contracts", "outbox", "reconciliation", "settings"},
    "jobs": {"contracts", "dispatch", "outbox", "settings"},
}

ROUTE_MANIFEST = [
    ("/weight", frozenset({"GET"}), "weight_dashboard"),
    ("/weight/measures", frozenset({"GET"}), "weight_measures"),
    ("/weight/log", frozenset({"POST"}), "log_weight_entry"),
    ("/weight/measurement", frozenset({"POST"}), "log_measurement_entry"),
    ("/weight/noise", frozenset({"POST"}), "add_noise_entry"),
    ("/weight/photo", frozenset({"POST"}), "add_photo_entry"),
    ("/weight/body-scan/upload", frozenset({"POST"}), "body_scan_upload"),
    ("/weight/body-scan/confirm", frozenset({"POST"}), "body_scan_confirm"),
    (
        "/weight/body-scan/{scan_id}/delete",
        frozenset({"POST"}),
        "delete_body_scan_entry",
    ),
    ("/weight/log/{id}/delete", frozenset({"POST"}), "delete_weight_entry"),
    (
        "/weight/measurement/{id}/delete",
        frozenset({"POST"}),
        "delete_measurement_entry",
    ),
    ("/weight/noise/{id}/delete", frozenset({"POST"}), "delete_noise_marker_entry"),
    ("/weight/photo/delete", frozenset({"POST"}), "delete_photo_entry"),
]


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _top_level_internal_dependencies(path: Path) -> set[str]:
    dependencies: set[str] = set()
    for node in _tree(path).body:
        if not isinstance(node, ast.ImportFrom):
            continue
        prefix = "vitals.services.garmin_weight."
        if node.module and node.module.startswith(prefix):
            dependencies.add(node.module.removeprefix(prefix).split(".", 1)[0])
    return dependencies


def test_garmin_weight_package_has_exact_leaf_manifest_and_no_flat_service() -> None:
    assert {path.name for path in SERVICE_ROOT.glob("*.py")} == SERVICE_FILES
    assert not (ROOT / "vitals" / "services" / "garmin_weight_service.py").exists()


def test_garmin_weight_top_level_imports_follow_exact_dag() -> None:
    assert {
        leaf: _top_level_internal_dependencies(SERVICE_ROOT / f"{leaf}.py")
        for leaf in TOP_LEVEL_DAG
    } == TOP_LEVEL_DAG


def test_production_has_no_legacy_garmin_weight_import() -> None:
    forbidden = "vitals.services.garmin_weight_service"
    for root_name in ("vitals", "web", "scripts"):
        for path in (ROOT / root_name).rglob("*.py"):
            tree = _tree(path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    assert node.module != forbidden, path
                    if node.module == "vitals.services":
                        assert all(alias.name != "garmin_weight_service" for alias in node.names), (
                            path
                        )
                elif isinstance(node, ast.Import):
                    assert all(alias.name != forbidden for alias in node.names), path


def test_weight_delivery_has_explicit_common_and_route_leaves() -> None:
    route_root = ROOT / "web" / "routers" / "weight_routes"
    assert {path.name for path in route_root.glob("*.py")} == {
        "__init__.py",
        "body_composition.py",
        "common.py",
        "records.py",
    }
    for path in route_root.glob("*.py"):
        source = path.read_text()
        assert "vitals.models" not in source
        assert "session.execute(" not in source
        assert "session.scalar(" not in source
        assert "from sqlalchemy import" not in source


def test_weight_delivery_leaves_do_not_depend_back_on_composition_root() -> None:
    route_root = ROOT / "web" / "routers" / "weight_routes"
    for path in route_root.glob("*.py"):
        source = path.read_text()
        assert "from web.routers import weight" not in source
        assert "from web.routers.weight import" not in source
        assert "_weight_facade" not in source


def test_weight_public_route_manifest_is_exact() -> None:
    from web.routers.weight import router

    assert [
        (route.path, frozenset(route.methods or ()), route.name) for route in router.routes
    ] == ROUTE_MANIFEST
