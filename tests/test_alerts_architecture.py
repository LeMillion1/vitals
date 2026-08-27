"""Static boundaries for scoped and legacy system alerts."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "vitals" / "services" / "alerts"
LEAVES = {
    "contracts",
    "validation",
    "context",
    "queries",
    "lifecycle",
    "legacy",
}
RANK = {
    "contracts": 0,
    "legacy": 0,
    "validation": 1,
    "context": 2,
    "queries": 3,
    "lifecycle": 4,
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_alerts_is_a_bounded_package_without_a_flat_shim():
    assert not (PACKAGE.parent / "alerts_service.py").exists()
    assert {path.stem for path in PACKAGE.glob("*.py")} == {"__init__", *LEAVES}
    for leaf in LEAVES:
        assert len((PACKAGE / f"{leaf}.py").read_text().splitlines()) <= 600


def test_alert_leaf_dependencies_are_one_way():
    prefix = "vitals.services.alerts."
    for leaf in LEAVES:
        dependencies = {
            name.removeprefix(prefix).split(".", 1)[0]
            for name in _imports(PACKAGE / f"{leaf}.py")
            if name.startswith(prefix)
        }
        assert leaf not in dependencies
        assert all(RANK[dependency] < RANK[leaf] for dependency in dependencies)


def test_alert_core_has_no_web_dependency():
    for leaf in LEAVES:
        imports = _imports(PACKAGE / f"{leaf}.py")
        assert not any(name == "web" or name.startswith("web.") for name in imports)


def test_python_callers_do_not_import_removed_alerts_module():
    offenders = []
    needles = (
        "from vitals.services import alerts_service",
        "from vitals.services.alerts_service import",
    )
    for root_name in ("vitals", "web", "tests"):
        for path in (ROOT / root_name).rglob("*.py"):
            if path == Path(__file__):
                continue
            source = path.read_text(encoding="utf-8")
            if any(needle in source for needle in needles):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
