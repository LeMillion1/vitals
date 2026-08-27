"""Static dependency contracts for the Labs bounded-context package."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "vitals" / "services"
LABS_PACKAGE = SERVICES / "labs"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(path: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                imported.add(module)
                imported.update(
                    f"{module}.{alias.name}" for alias in node.names
                )
    return imported


def _leaf_paths() -> list[Path]:
    return sorted(
        path
        for path in LABS_PACKAGE.glob("*.py")
        if path.name != "__init__.py"
    )


def test_legacy_lab_document_ai_service_has_been_removed() -> None:
    assert not (SERVICES / "lab_document_ai_service.py").exists()


def test_labs_flags_is_a_pure_leaf() -> None:
    imports = _imported_modules(LABS_PACKAGE / "flags.py")
    forbidden_roots = ("sqlalchemy", "vitals.models", "vitals.services")
    forbidden = {
        imported
        for imported in imports
        if any(
            imported == root or imported.startswith(f"{root}.")
            for root in forbidden_roots
        )
    }

    assert forbidden == set()


def test_labs_leaves_do_not_import_the_package_aggregate() -> None:
    offenders = {
        path.name
        for path in _leaf_paths()
        if "vitals.services.labs" in _imported_modules(path)
    }

    assert offenders == set()


def test_labs_legacy_flat_service_is_absent_from_the_source_tree() -> None:
    assert not (SERVICES / "labs_service.py").exists()
    offenders = {
        path.name
        for path in _leaf_paths()
        if "vitals.services.labs_service" in _imported_modules(path)
    }

    assert offenders == set()


def test_labs_services_do_not_own_transaction_boundaries() -> None:
    paths = _leaf_paths()

    offenders: list[str] = []
    for path in paths:
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"commit", "rollback"}
            ):
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
                )

    assert offenders == []


def test_clinical_audience_projections_do_not_import_lab_orm_models() -> None:
    audience_paths = (
        ROOT / "vitals" / "services" / "care" / "record_projection.py",
        ROOT / "vitals" / "services" / "emergency" / "projection.py",
    )

    offenders = {
        path.relative_to(ROOT).as_posix()
        for path in audience_paths
        if "vitals.models.labs" in _imported_modules(path)
    }

    assert offenders == set()
