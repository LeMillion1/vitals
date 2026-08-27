"""Architecture ratchets for the bounded Body Scan context."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
BODY_SCAN = ROOT / "vitals" / "services" / "body_scan"
SCANS = BODY_SCAN / "scans"
AI = BODY_SCAN / "ai"

SCAN_MANIFEST = {
    "contracts": {
        "BodyScanOwnershipError",
        "BodyScanRawAlreadyNormalizedError",
        "require_evaluation_date",
        "require_scoped_prepared_write",
        "scan_entity_key",
    },
    "normalization": {
        "extract_from_file",
        "extract_from_file_with_usage",
        "extract_prepared_file_with_usage",
        "normalize_extracted",
        "prepare_file_for_extraction",
    },
    "ingestion": {"ingest_structured_scan", "save_scan"},
    "queries": {
        "available_metrics",
        "bia_chart_points",
        "get_scan",
        "latest_scan",
        "list_scans",
        "metric_history",
        "resolve_active_scoped",
    },
    "writes": {"delete_scan", "update_scan_note"},
    "alerts": {"refresh_alerts"},
    "reparse": {"reparse_owned_pending"},
}
AI_MANIFEST = {
    "contracts": {
        "BodyScanAIAvailability",
        "BodyScanAIAvailabilityCode",
        "BodyScanAIError",
        "BodyScanAIInvocationStateError",
        "BodyScanAIOwnershipError",
        "BodyScanAIValidationError",
        "BodyScanParseResult",
        "PreparedBodyScanContent",
        "PreparedBodyScanParse",
    },
    "scope": set(),
    "projection": {"project_body_scan_ai_availability"},
    "workflow": {
        "cancel_prepared_body_scan_parse",
        "persist_body_scan_parse",
        "prepare_body_scan_content",
        "prepare_body_scan_parse",
        "render_body_scan",
        "start_body_scan_dispatch",
    },
}
SCAN_RANK = {
    "contracts": 0,
    "normalization": 0,
    "ingestion": 1,
    "queries": 2,
    "writes": 3,
    "alerts": 3,
    "reparse": 4,
}
AI_RANK = {"contracts": 0, "scope": 1, "projection": 2, "workflow": 3}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _public_definitions(path: Path) -> set[str]:
    return {
        node.name
        for node in _tree(path).body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }


def _leaf_dependencies(path: Path, leaves: set[str]) -> set[str]:
    dependencies: set[str] = set()
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.ImportFrom) or node.level != 1:
            continue
        if node.module:
            candidate = node.module.split(".", 1)[0]
            if candidate in leaves:
                dependencies.add(candidate)
        else:
            dependencies.update(alias.name for alias in node.names if alias.name in leaves)
    return dependencies


def test_body_scan_leaf_manifests_are_exact():
    assert {path.stem for path in SCANS.glob("*.py")} == {
        "__init__",
        *SCAN_MANIFEST,
    }
    assert {path.stem for path in AI.glob("*.py")} == {"__init__", *AI_MANIFEST}
    for leaf, expected in SCAN_MANIFEST.items():
        assert _public_definitions(SCANS / f"{leaf}.py") == expected
    for leaf, expected in AI_MANIFEST.items():
        assert _public_definitions(AI / f"{leaf}.py") == expected


def test_body_scan_dependencies_follow_the_bounded_dag():
    for leaf, rank in SCAN_RANK.items():
        dependencies = _leaf_dependencies(
            SCANS / f"{leaf}.py",
            set(SCAN_RANK),
        )
        assert all(SCAN_RANK[dependency] < rank for dependency in dependencies)
    for leaf, rank in AI_RANK.items():
        dependencies = _leaf_dependencies(AI / f"{leaf}.py", set(AI_RANK))
        assert all(AI_RANK[dependency] < rank for dependency in dependencies)


def test_body_scan_has_no_flat_shims_and_leaves_stay_bounded():
    assert not (BODY_SCAN / "scans.py").exists()
    assert not (BODY_SCAN / "ai.py").exists()
    scan_sizes = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in SCANS.glob("*.py")
    }
    ai_sizes = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in AI.glob("*.py")
    }
    assert max(scan_sizes.values()) < 750, scan_sizes
    assert max(ai_sizes.values()) < 550, ai_sizes


def test_python_callers_do_not_import_removed_body_scan_modules():
    forbidden = (
        "from vitals.services.body_scan import scans",
        "from vitals.services.body_scan import ai",
    )
    offenders = []
    for root_name in ("vitals", "web", "tests", "scripts"):
        for path in (ROOT / root_name).rglob("*.py"):
            if path == Path(__file__):
                continue
            source = path.read_text(encoding="utf-8")
            if any(needle in source for needle in forbidden):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
