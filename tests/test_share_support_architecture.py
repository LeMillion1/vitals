"""Architecture and security-boundary ratchets for sharing and support access."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "vitals" / "services"
SHARE = SERVICES / "share"
SUPPORT = SERVICES / "support_access"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(path: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def _calls(path: Path, names: set[str]) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in names
    ]


def test_flat_share_and_support_services_are_retired() -> None:
    assert not (SERVICES / "share_service.py").exists()
    assert not (SERVICES / "support_access_service.py").exists()

    forbidden = {
        "vitals.services.share_service",
        "vitals.services.support_access_service",
    }
    offenders: list[str] = []
    for source_root in (ROOT / "vitals", ROOT / "web", ROOT / "tests"):
        for path in source_root.rglob("*.py"):
            if _imports(path) & forbidden:
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_share_public_bearer_boundary_is_not_owned_by_report_lifecycle() -> None:
    public_imports = _imports(SHARE / "public_access.py")
    reports_imports = _imports(SHARE / "reports.py")
    public_source = (SHARE / "public_access.py").read_text(encoding="utf-8")

    assert "vitals.persistence.rls" in public_imports
    assert "bind_session_subject" in public_source
    assert "public.attest_shared_report_token(text)" in (
        SHARE / "ownership.py"
    ).read_text(encoding="utf-8")
    assert "vitals.services.share.public_access" not in reports_imports


def test_support_export_and_repair_are_separate_authorization_doors() -> None:
    export_imports = _imports(SUPPORT / "export.py")
    repair_imports = _imports(SUPPORT / "repair.py")

    assert "vitals.services.portability" in export_imports
    assert "vitals.services.portability" not in repair_imports
    assert "vitals.services.conflicts" in repair_imports
    assert "vitals.services.support_access.repair" not in export_imports


def test_reusable_share_and_support_leaves_do_not_own_commits() -> None:
    share_leaves = tuple(
        path for path in SHARE.glob("*.py") if path.name not in {"__init__.py", "jobs.py"}
    )
    support_leaves = tuple(
        path for path in SUPPORT.glob("*.py") if path.name != "__init__.py"
    )
    offenders = {
        path.relative_to(ROOT).as_posix(): _calls(path, {"commit", "rollback"})
        for path in share_leaves + support_leaves
        if _calls(path, {"commit", "rollback"})
    }
    assert offenders == {}


def test_share_and_support_concerns_cannot_collapse_into_monoliths() -> None:
    leaves = tuple(
        path
        for package in (SHARE, SUPPORT)
        for path in package.glob("*.py")
        if path.name != "__init__.py"
    )
    line_counts = {
        path.relative_to(ROOT).as_posix(): len(
            path.read_text(encoding="utf-8").splitlines()
        )
        for path in leaves
    }
    assert max(line_counts.values()) < 1_000, line_counts
