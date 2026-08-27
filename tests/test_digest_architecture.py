"""Static boundaries for the period-digest domain package."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
DIGEST = ROOT / "vitals" / "services" / "digest"
PROJECTION = DIGEST / "projection"
PROJECTION_LEAVES = {
    "contracts",
    "formatting",
    "stats",
    "providers",
    "clinical",
    "lifestyle",
    "assembly",
}
PROJECTION_RANK = {
    "contracts": 0,
    "formatting": 1,
    "stats": 2,
    "providers": 2,
    "clinical": 3,
    "lifestyle": 4,
    "assembly": 5,
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_digest_is_a_package_without_a_flat_compatibility_module():
    assert not (ROOT / "vitals" / "services" / "digest_service.py").exists()
    assert {path.name for path in DIGEST.glob("*.py")} == {
        "__init__.py",
        "generation.py",
        "jobs.py",
        "ownership.py",
        "prompt.py",
        "queries.py",
        "window.py",
    }


def test_digest_leaves_follow_one_way_dependencies():
    imports = {path.stem: _imports(path) for path in DIGEST.glob("*.py")}

    assert not any(name.startswith(("fastapi", "web")) for names in imports.values() for name in names)
    assert not any(name.startswith("vitals.services.digest") for name in imports["window"])
    assert not any(name.startswith("vitals.services.digest") for name in imports["prompt"])
    assert "vitals.services.digest.generation" not in imports["ownership"]
    assert "vitals.services.digest.jobs" not in imports["generation"]
    assert "vitals.services.digest.jobs" not in imports["queries"]


def test_projection_is_a_bounded_package_with_a_one_way_dag():
    assert not (DIGEST / "projection.py").exists()
    assert {path.stem for path in PROJECTION.glob("*.py")} == {
        "__init__",
        *PROJECTION_LEAVES,
    }
    prefix = "vitals.services.digest.projection."
    for leaf in PROJECTION_LEAVES:
        path = PROJECTION / f"{leaf}.py"
        assert len(path.read_text(encoding="utf-8").splitlines()) < 1300
        dependencies = {
            name.removeprefix(prefix).split(".", 1)[0]
            for name in _imports(path)
            if name.startswith(prefix)
        }
        assert leaf not in dependencies
        assert all(
            PROJECTION_RANK[dependency] < PROJECTION_RANK[leaf]
            for dependency in dependencies
        )


def test_projection_callers_use_explicit_leaves():
    offenders = []
    needles = (
        "from vitals.services.digest import projection",
        "from vitals.services.digest.projection import assemble_context",
    )
    for root_name in ("vitals", "web", "tests"):
        for path in (ROOT / root_name).rglob("*.py"):
            if path == Path(__file__):
                continue
            source = path.read_text(encoding="utf-8")
            if any(needle in source for needle in needles):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_python_callers_do_not_reference_removed_digest_service():
    offenders = []
    for root_name in ("vitals", "web", "tests"):
        for path in (ROOT / root_name).rglob("*.py"):
            if path == Path(__file__):
                continue
            if "digest_service" in path.read_text(encoding="utf-8"):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
