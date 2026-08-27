"""Static boundaries for the platform-funded AI gateway."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "vitals" / "services" / "ai_gateway"
LEAVES = {
    "contracts",
    "config",
    "quota",
    "invocations",
    "dispatch",
    "reconciliation",
    "jobs",
}
RANK = {
    "contracts": 0,
    "config": 1,
    "quota": 2,
    "invocations": 2,
    "dispatch": 3,
    "reconciliation": 3,
    "jobs": 4,
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


def test_ai_gateway_is_a_bounded_package_without_a_flat_shim():
    assert not (PACKAGE.parent / "ai_gateway_service.py").exists()
    assert {path.stem for path in PACKAGE.glob("*.py")} == {"__init__", *LEAVES}
    for leaf in LEAVES:
        assert len((PACKAGE / f"{leaf}.py").read_text().splitlines()) <= 650


def test_ai_gateway_leaf_dependencies_are_one_way():
    prefix = "vitals.services.ai_gateway."
    for leaf in LEAVES:
        dependencies = {
            name.removeprefix(prefix).split(".", 1)[0]
            for name in _imports(PACKAGE / f"{leaf}.py")
            if name.startswith(prefix)
        }
        assert leaf not in dependencies
        assert all(RANK[dependency] < RANK[leaf] for dependency in dependencies)


def test_ai_gateway_core_has_no_delivery_or_web_dependency():
    forbidden = ("fastapi", "web", "vitals.integrations")
    for leaf in LEAVES:
        imports = _imports(PACKAGE / f"{leaf}.py")
        assert not any(
            name == prefix or name.startswith(f"{prefix}.")
            for name in imports
            for prefix in forbidden
        )


def test_python_callers_do_not_import_removed_ai_gateway_module():
    offenders = []
    needles = (
        "from vitals.services import ai_gateway_service",
        "from vitals.services.ai_gateway_service import",
    )
    for root_name in ("vitals", "web", "tests"):
        for path in (ROOT / root_name).rglob("*.py"):
            if path == Path(__file__):
                continue
            source = path.read_text(encoding="utf-8")
            if any(needle in source for needle in needles):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
