"""Architecture ratchets for the shared medical-document AI coordinator."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _function_source(path: Path, function_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == function_name:
                return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{function_name} not found in {path}")


def test_upload_routes_delegate_paid_ai_transaction_workflow() -> None:
    cases = (
        (ROOT / "web/routers/labs.py", "upload_document"),
        (
            ROOT / "web/routers/weight_routes/body_composition.py",
            "body_scan_upload",
        ),
    )
    for path, function_name in cases:
        source = _function_source(path, function_name)
        assert "run_medical_ai_upload(" in source
        assert ".commit(" not in source
        assert ".rollback(" not in source
        assert "AIInvocationStatus" not in source
        assert "AIGatewayConfigurationError" not in source
        assert "AIQuotaExceededError" not in source


def test_shared_coordinator_is_domain_and_http_response_agnostic() -> None:
    source = (ROOT / "web/medical_ai_upload.py").read_text(encoding="utf-8")
    assert "vitals.services.labs" not in source
    assert "vitals.services.body_scan" not in source
    assert "JSONResponse" not in source
    assert "HTTPException" not in source
