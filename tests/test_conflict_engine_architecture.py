"""Static package boundaries for conflict evaluation and enforcement."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "vitals" / "services" / "conflicts" / "engine"
LEAVES = {
    "contracts",
    "registry",
    "matching",
    "scope",
    "rules",
    "evaluation",
    "enforcement",
}
RANK = {
    "contracts": 0,
    "registry": 1,
    "matching": 2,
    "scope": 2,
    "rules": 3,
    "evaluation": 4,
    "enforcement": 5,
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


def test_conflict_engine_is_a_bounded_package_without_a_flat_module():
    assert not (PACKAGE.parent / "engine.py").exists()
    assert {path.stem for path in PACKAGE.glob("*.py")} == {"__init__", *LEAVES}
    for leaf in LEAVES:
        assert len((PACKAGE / f"{leaf}.py").read_text().splitlines()) <= 700


def test_conflict_engine_leaf_dependencies_are_one_way():
    prefix = "vitals.services.conflicts.engine."
    for leaf in LEAVES:
        dependencies = {
            name.removeprefix(prefix).split(".", 1)[0]
            for name in _imports(PACKAGE / f"{leaf}.py")
            if name.startswith(prefix)
        }
        assert leaf not in dependencies
        assert all(RANK[dependency] < RANK[leaf] for dependency in dependencies)


def test_conflict_engine_has_no_delivery_dependency():
    for leaf in LEAVES:
        imports = _imports(PACKAGE / f"{leaf}.py")
        assert not any(
            name == prefix or name.startswith(f"{prefix}.")
            for name in imports
            for prefix in ("fastapi", "web")
        )


def test_conflict_engine_public_api_is_explicit():
    init = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(init)
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    ]
    assert len(assignments) == 1
    assert "enforce_prepared" in init
    assert "evaluate_scoped" in init
    assert "register_domain_resolver" in init
