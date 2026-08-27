"""Architecture ratchets for the decomposed portability service."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PORTABILITY = ROOT / "vitals" / "services" / "portability"
LEGACY_MODULE = "vitals.services.data_portability_service"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_flat_data_portability_service_is_retired() -> None:
    assert not (ROOT / "vitals" / "services" / "data_portability_service.py").exists()

    offenders: list[str] = []
    for source_root in (ROOT / "vitals", ROOT / "web", ROOT / "tests"):
        for path in source_root.rglob("*.py"):
            if LEGACY_MODULE in _imports(path):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_portability_leaves_follow_the_one_way_dependency_graph() -> None:
    contract_imports = _imports(PORTABILITY / "v1_contract.py")
    export_imports = _imports(PORTABILITY / "v1_export.py")
    import_imports = _imports(PORTABILITY / "v1_import.py")
    llm_imports = _imports(PORTABILITY / "llm_projection.py")

    assert not {
        "vitals.services.portability.v1_export",
        "vitals.services.portability.v1_import",
        "vitals.services.portability.llm_projection",
    } & contract_imports
    assert "vitals.services.portability.v1_contract" in export_imports
    assert "vitals.services.portability.v1_contract" in import_imports
    assert "vitals.services.portability.v1_export" in import_imports
    assert "vitals.services.portability.llm_projection" not in import_imports
    assert "vitals.services.portability.v1_contract" in llm_imports
    assert "vitals.services.portability.v1_export" not in llm_imports
    assert "vitals.services.portability.v1_import" not in llm_imports


def test_portability_concerns_cannot_collapse_back_into_one_monolith() -> None:
    leaves = (
        "v1_contract.py",
        "v1_export.py",
        "v1_import.py",
        "llm_projection.py",
    )
    line_counts = {
        name: len((PORTABILITY / name).read_text(encoding="utf-8").splitlines())
        for name in leaves
    }

    assert max(line_counts.values()) < 1_500, line_counts
