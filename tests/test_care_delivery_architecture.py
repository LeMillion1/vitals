"""Static boundaries for the care and consent delivery adapters."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTERS = (
    ROOT / "web" / "routers" / "care.py",
    ROOT / "web" / "routers" / "consents.py",
)
SERVICES = (
    ROOT / "vitals" / "services" / "care" / "consent_centre.py",
    ROOT / "vitals" / "services" / "care" / "workspace.py",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_care_delivery_has_no_orm_or_model_queries() -> None:
    for path in ROUTERS:
        imports = _imports(path)
        assert {
            name for name in imports if name.startswith("sqlalchemy")
        } <= {"sqlalchemy.ext.asyncio"}
        assert not any(name.startswith("vitals.models") for name in imports)
        source = path.read_text(encoding="utf-8")
        assert "select(" not in source


def test_care_policy_and_projections_live_in_core_services() -> None:
    consent_source = ROUTERS[1].read_text(encoding="utf-8")
    care_source = ROUTERS[0].read_text(encoding="utf-8")
    assert "consent_projection.build_projection(" in consent_source
    assert "consent_projection.selected_scopes_for_subject(" in consent_source
    assert "care_workspace.load_professional_workspace(" in care_source
    assert "care_workspace.visible_record(" in care_source

    for path in SERVICES:
        imports = _imports(path)
        assert "fastapi" not in imports
        assert not any(name.startswith("web") for name in imports)


def test_care_delivery_files_stay_bounded() -> None:
    limits = {"care.py": 900, "consents.py": 350}
    for path in ROUTERS:
        assert len(path.read_text(encoding="utf-8").splitlines()) <= limits[path.name]
