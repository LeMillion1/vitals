"""Static boundaries for Daily Brief and scoped proactive preferences."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
PROACTIVE = ROOT / "vitals" / "services" / "proactive"
BRIEF = PROACTIVE / "brief"
PREFERENCES = PROACTIVE / "preferences"

BRIEF_RANK = {
    "contracts": 0,
    "context": 1,
    "prompt": 1,
    "preparation": 2,
    "rendering": 3,
    "persistence": 3,
    "jobs": 4,
}
PREFERENCE_RANK = {
    "contracts": 0,
    "codec": 1,
    "queries": 2,
    "legacy": 2,
    "writes": 3,
}


def _package_dependencies(path: Path, package: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    prefix = f"vitals.services.proactive.{package}."
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 1 and node.module:
            dependencies.add(node.module.split(".", 1)[0])
        elif node.module and node.module.startswith(prefix):
            dependencies.add(node.module.removeprefix(prefix).split(".", 1)[0])
    return dependencies


def _assert_package(package: Path, ranks: dict[str, int], *, max_lines: int) -> None:
    assert {path.stem for path in package.glob("*.py")} == {"__init__", *ranks}
    for leaf, rank in ranks.items():
        path = package / f"{leaf}.py"
        assert len(path.read_text(encoding="utf-8").splitlines()) <= max_lines
        dependencies = _package_dependencies(path, package.name)
        assert leaf not in dependencies
        assert all(ranks[dependency] < rank for dependency in dependencies)


def test_brief_is_a_bounded_package_with_a_one_way_dag():
    assert not (PROACTIVE / "brief.py").exists()
    _assert_package(BRIEF, BRIEF_RANK, max_lines=550)


def test_preferences_are_bounded_and_keep_writes_downstream_of_reads():
    assert not (PROACTIVE / "prefs.py").exists()
    _assert_package(PREFERENCES, PREFERENCE_RANK, max_lines=600)


def test_callers_do_not_import_removed_flat_modules():
    offenders: list[str] = []
    needles = (
        "from vitals.services.proactive import brief",
        "from vitals.services.proactive import prefs",
        "vitals.services.proactive.brief import brief_job",
        "vitals.services.proactive.prefs",
    )
    for root_name in ("vitals", "web", "tests"):
        for path in (ROOT / root_name).rglob("*.py"):
            if path == Path(__file__):
                continue
            source = path.read_text(encoding="utf-8")
            if any(needle in source for needle in needles):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_domain_packages_do_not_depend_on_delivery_frameworks():
    for package in (BRIEF, PREFERENCES):
        for path in package.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            imported = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            assert not any(
                name == "web"
                or name.startswith("web.")
                or name == "fastapi"
                or name.startswith("fastapi.")
                for name in imported
            )
