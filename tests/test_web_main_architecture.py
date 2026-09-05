"""Static contracts for the FastAPI composition boundary."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
WEB = ROOT / "web"
MODULES = {
    "app_lifecycle": WEB / "app_lifecycle.py",
    "error_handlers": WEB / "error_handlers.py",
    "system_routes": WEB / "system_routes.py",
    "main": WEB / "main.py",
}
RANK = {
    "app_lifecycle": 0,
    "system_routes": 0,
    "error_handlers": 1,
    "main": 2,
}
OWNERS = {
    "app_lifecycle": {
        "_bootstrap_legacy_identity",
        "_load_oidc_identity_state",
        "lifespan",
    },
    "error_handlers": {
        "access_denied_handler",
        "auth_exception_handler",
        "http_exception_handler",
        "legacy_ownership_handler",
        "module_disabled_handler",
        "no_personal_record_handler",
        "recent_authentication_handler",
        "register_error_handlers",
    },
    "system_routes": {
        "health",
        "register_system_routes",
        "root",
        "serve_upload",
        "service_worker",
    },
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _definitions(path: Path) -> set[str]:
    return {
        node.name
        for node in _tree(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _web_boundary_dependencies(path: Path) -> set[str]:
    dependencies: set[str] = set()
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.module in {f"web.{name}" for name in MODULES}:
            dependencies.add(node.module.removeprefix("web."))
    return dependencies


def test_main_is_a_small_composition_root_with_explicit_leaf_owners() -> None:
    assert len(MODULES["main"].read_text().splitlines()) <= 250
    assert all(
        len(path.read_text().splitlines()) <= 450
        for name, path in MODULES.items()
        if name != "main"
    )
    for owner, names in OWNERS.items():
        assert names <= _definitions(MODULES[owner])
        assert names.isdisjoint(_definitions(MODULES["main"]))


def test_main_boundary_dependencies_are_one_way() -> None:
    for module, path in MODULES.items():
        dependencies = _web_boundary_dependencies(path)
        assert module not in dependencies
        assert all(RANK[dependency] < RANK[module] for dependency in dependencies)
        if module != "main":
            source = path.read_text(encoding="utf-8")
            assert "from web.routers" not in source
            assert "import web.main" not in source
            assert "from web import main" not in source


def test_system_route_registration_preserves_private_static_order() -> None:
    source = MODULES["system_routes"].read_text(encoding="utf-8")
    route_markers = (
        'app.add_route("/sw.js"',
        '"/static/uploads/{key:path}"',
        'app.mount("/static"',
        'app.add_api_route("/health"',
        'app.add_api_route("/"',
    )
    positions = [source.index(marker) for marker in route_markers]
    assert positions == sorted(positions)


def test_delivery_leaves_register_through_explicit_installers() -> None:
    for name in ("error_handlers", "system_routes"):
        source = MODULES[name].read_text(encoding="utf-8")
        assert "@app." not in source
    main_source = MODULES["main"].read_text(encoding="utf-8")
    assert "register_system_routes(app)" in main_source
    assert "register_error_handlers(app)" in main_source


def test_module_chrome_runs_before_request_session_consumers() -> None:
    """A cold module cache must not ask an already checked-out request for a peer."""

    tree = _tree(MODULES["main"])
    app_assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "app"
            for target in node.targets
        )
    )
    assert isinstance(app_assignment.value, ast.Call)
    dependencies = next(
        keyword.value
        for keyword in app_assignment.value.keywords
        if keyword.arg == "dependencies"
    )
    assert isinstance(dependencies, ast.List)
    names = [
        element.args[0].id
        for element in dependencies.elts
        if isinstance(element, ast.Call)
        and element.args
        and isinstance(element.args[0], ast.Name)
    ]
    assert names[0] == "load_enabled_modules"


def test_required_mcp_and_oauth_surfaces_fail_startup_when_imports_break() -> None:
    """A healthy process must never silently omit required protocol surfaces."""

    tree = _tree(MODULES["main"])
    guarded_imports: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        imported_modules = {
            child.module
            for child in ast.walk(node)
            if isinstance(child, ast.ImportFrom) and child.module
        }
        catches_import_error = any(
            isinstance(handler.type, ast.Name) and handler.type.id == "ImportError"
            for handler in node.handlers
        )
        if catches_import_error and imported_modules.intersection(
            {"web.routers.mcp", "web.routers.oauth"}
        ):
            guarded_imports.append(node.lineno)

    assert guarded_imports == []
