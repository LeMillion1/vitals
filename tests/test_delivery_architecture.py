"""Static boundaries for durable proactive delivery."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "vitals" / "services" / "proactive" / "delivery"
LEAVES = {
    "contracts",
    "policy",
    "queries",
    "preparation",
    "dispatch",
    "reconciliation",
    "legacy",
}
RANK = {
    "contracts": 0,
    "policy": 1,
    "queries": 2,
    "preparation": 3,
    "dispatch": 4,
    "reconciliation": 5,
    "legacy": 5,
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


def test_delivery_is_a_bounded_package_without_a_flat_shim():
    assert not (PACKAGE.parent / "delivery.py").exists()
    assert {path.stem for path in PACKAGE.glob("*.py")} == {"__init__", *LEAVES}
    for leaf in LEAVES:
        assert len((PACKAGE / f"{leaf}.py").read_text().splitlines()) <= 1200


def test_delivery_leaf_dependencies_are_one_way():
    prefix = "vitals.services.proactive.delivery."
    for leaf in LEAVES:
        dependencies = {
            name.removeprefix(prefix).split(".", 1)[0]
            for name in _imports(PACKAGE / f"{leaf}.py")
            if name.startswith(prefix)
        }
        assert leaf not in dependencies
        assert all(RANK[dependency] < RANK[leaf] for dependency in dependencies)


def test_delivery_core_depends_on_transport_contracts_not_vendor_clients():
    forbidden = ("fastapi", "web", "httpx", "aiohttp", "telegram")
    for leaf in LEAVES - {"legacy"}:
        imports = _imports(PACKAGE / f"{leaf}.py")
        assert not any(
            name == prefix or name.startswith(f"{prefix}.")
            for name in imports
            for prefix in forbidden
        )
        assert not any(name.startswith("vitals.integrations.") for name in imports)


def test_python_callers_do_not_import_removed_delivery_module():
    offenders = []
    needles = (
        "from vitals.services.proactive import delivery",
        "from vitals.services.proactive.delivery import delivery_reconciliation_job",
    )
    for root_name in ("vitals", "web", "tests"):
        for path in (ROOT / root_name).rglob("*.py"):
            if path == Path(__file__):
                continue
            source = path.read_text(encoding="utf-8")
            if any(needle in source for needle in needles):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
