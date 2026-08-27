"""Static and route-contract ratchets for the settings delivery package."""

from __future__ import annotations

import ast
from pathlib import Path

from web.routers import settings


ROOT = Path(__file__).parents[1]
AGGREGATE = ROOT / "web" / "routers" / "settings.py"
ROUTES = ROOT / "web" / "settings" / "routes"

EXPECTED_MANIFEST = {
    ("GET", "/settings", "settings_page"),
    ("GET", "/settings/export", "export_backup"),
    ("GET", "/settings/export-llm", "export_llm"),
    ("GET", "/settings/export-subject", "export_subject_backup"),
    ("GET", "/settings/platform", "platform_settings_page"),
    ("GET", "/settings/platform/ai", "platform_ai_page"),
    ("POST", "/settings/2fa/disable", "disable_twofa"),
    ("POST", "/settings/2fa/enable", "confirm_twofa"),
    ("POST", "/settings/2fa/start", "start_twofa"),
    ("POST", "/settings/ai", "save_ai"),
    ("POST", "/settings/connectors/{connector_id}/revoke", "revoke_connector"),
    ("POST", "/settings/external-api", "issue_external_api_token"),
    (
        "POST",
        "/settings/external-api/{token_id}/revoke",
        "revoke_external_api_token",
    ),
    ("POST", "/settings/garmin", "save_garmin"),
    ("POST", "/settings/garmin/weight-toggle", "toggle_garmin_weight_export"),
    ("POST", "/settings/garmin/weight/send-now", "send_garmin_weight_now"),
    ("POST", "/settings/hevy", "save_hevy"),
    ("POST", "/settings/import", "import_backup"),
    ("POST", "/settings/import-subject", "import_subject_record"),
    ("POST", "/settings/language", "save_language"),
    ("POST", "/settings/mcp", "save_mcp"),
    ("POST", "/settings/modules", "toggle_module"),
    ("POST", "/settings/password", "change_password"),
    ("POST", "/settings/platform/ai/configuration", "save_ai"),
    ("POST", "/settings/platform/ai/disable", "disable_platform_ai"),
    ("POST", "/settings/platform/ai/enable", "enable_platform_ai"),
    ("POST", "/settings/platform/ai/quota", "configure_platform_ai_quota"),
    ("POST", "/settings/platform/mcp", "save_mcp"),
    ("POST", "/settings/proactive", "save_proactive"),
    ("POST", "/settings/profile", "save_profile"),
    ("POST", "/settings/restart", "restart_container"),
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


def test_settings_route_manifest_is_exact():
    actual = {
        (method, route.path, route.name)
        for included in settings.router.routes
        for route in included.effective_candidates()
        for method in route.methods or ()
    }
    assert actual == EXPECTED_MANIFEST


def test_settings_aggregate_has_no_orm_or_workflow():
    source = AGGREGATE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(AGGREGATE))
    assert len(source.splitlines()) <= 100
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in ast.walk(tree)
    )
    imports = _imports(AGGREGATE)
    assert not {
        name
        for name in imports
        if name.startswith(("sqlalchemy", "vitals.models", "vitals.services"))
    }


def test_settings_route_leaves_stay_bounded_and_reuse_shared_forms():
    expected = {
        "__init__.py",
        "common.py",
        "portability.py",
        "preferences.py",
        "profile.py",
        "providers.py",
        "security.py",
    }
    assert {path.name for path in ROUTES.glob("*.py")} == expected
    line_counts = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in ROUTES.glob("*.py")
    }
    assert max(line_counts.values()) < 400, line_counts
    provider_imports = _imports(ROUTES / "providers.py")
    assert "web.settings.forms" in provider_imports
    assert (ROOT / "web" / "settings" / "platform.py").exists()


def test_settings_route_leaves_delegate_database_queries_to_services():
    for path in ROUTES.glob("*.py"):
        imports = _imports(path)
        assert not any(name.startswith("vitals.models") for name in imports)
        assert {
            name for name in imports if name.startswith("sqlalchemy")
        } <= {"sqlalchemy.ext.asyncio"}
        assert "select(" not in path.read_text(encoding="utf-8")
