"""Static dependency guards for the core application architecture."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VITALS = ROOT / "vitals"
SERVICES = VITALS / "services"


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _top_level_import_nodes(nodes: list[ast.stmt]) -> list[ast.Import | ast.ImportFrom]:
    imports: list[ast.Import | ast.ImportFrom] = []
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        elif isinstance(node, (ast.If, ast.Try, ast.TryStar)):
            imports.extend(_top_level_import_nodes(node.body))
            imports.extend(_top_level_import_nodes(node.orelse))
            if isinstance(node, (ast.Try, ast.TryStar)):
                imports.extend(_top_level_import_nodes(node.finalbody))
                for handler in node.handlers:
                    imports.extend(_top_level_import_nodes(handler.body))
    return imports


def _import_time_graph() -> dict[str, set[str]]:
    paths = _python_files(VITALS)
    modules = {_module_name(path): path for path in paths}
    graph = {name: set() for name in modules}
    for name, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _top_level_import_nodes(tree.body):
            candidates: set[str] = set()
            if isinstance(node, ast.Import):
                candidates.update(alias.name for alias in node.names)
            else:
                if node.level:
                    package = name.split(".")[:-1]
                    keep = len(package) - node.level + 1
                    prefix = ".".join(package[:keep])
                    base = ".".join(part for part in (prefix, node.module) if part)
                else:
                    base = node.module or ""
                if base:
                    candidates.add(base)
                    candidates.update(f"{base}.{alias.name}" for alias in node.names)
            graph[name].update(candidate for candidate in candidates if candidate in modules)
    return graph


def _strong_components(graph: dict[str, set[str]]) -> list[set[str]]:
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[set[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph[node]:
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] != indices[node]:
            return
        component: set[str] = set()
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == node:
                break
        components.append(component)

    for node in graph:
        if node not in indices:
            visit(node)
    return components


def test_core_does_not_depend_on_web_or_fastapi() -> None:
    violations: list[str] = []
    for path in _python_files(VITALS):
        forbidden = sorted(
            name for name in _imports(path) if name == "web" or name.startswith("web.") or name == "fastapi" or name.startswith("fastapi.")
        )
        if forbidden:
            violations.append(f"{path.relative_to(ROOT)}: {', '.join(forbidden)}")

    assert not violations, "Core-to-delivery dependencies:\n" + "\n".join(violations)


def test_services_do_not_depend_on_operations() -> None:
    violations: list[str] = []
    for path in _python_files(SERVICES):
        forbidden = sorted(
            name
            for name in _imports(path)
            if name == "vitals.operations" or name.startswith("vitals.operations.")
        )
        if forbidden:
            violations.append(f"{path.relative_to(ROOT)}: {', '.join(forbidden)}")

    assert not violations, "Application services depend on operational code:\n" + "\n".join(violations)


def test_pure_analytics_do_not_depend_on_io_layers() -> None:
    analytics_roots = [
        path for path in (VITALS / "analytics", SERVICES / "analytics") if path.exists()
    ]
    pure_modules = {
        "body_metrics.py",
        "navy.py",
        "progression.py",
        "regression.py",
        "rolling.py",
    }
    forbidden_prefixes = (
        "sqlalchemy",
        "vitals.integrations",
        "vitals.models",
        "web",
    )
    violations: list[str] = []
    for root in analytics_roots:
        for path in _python_files(root):
            if path.name not in pure_modules:
                continue
            forbidden = sorted(
                name
                for name in _imports(path)
                if name in forbidden_prefixes
                or name.startswith(
                    tuple(f"{prefix}." for prefix in forbidden_prefixes)
                )
            )
            if forbidden:
                violations.append(f"{path.relative_to(ROOT)}: {', '.join(forbidden)}")

    assert not violations, "Pure analytics depend on I/O layers:\n" + "\n".join(violations)


def test_no_new_flat_service_modules() -> None:
    """Turn the current flat directory into an explicit, shrinking debt ledger."""

    legacy = {
        path.name
        for path in SERVICES.glob("*.py")
        if path.name != "__init__.py"
    }
    # A concurrent untracked user draft is not part of the repository baseline.
    # If implemented, it belongs in ``services/accounts`` rather than here.
    legacy.discard("account_erasure_service.py")
    baseline = 75

    assert len(legacy) <= baseline, (
        f"Flat service module count grew from the guarded ceiling {baseline} to {len(legacy)}. "
        "Put new behavior in a bounded-context package instead."
    )


def test_core_has_no_import_time_cycles() -> None:
    cycles = [component for component in _strong_components(_import_time_graph()) if len(component) > 1]

    assert not cycles, "Import-time dependency cycles:\n" + "\n".join(
        " -> ".join(sorted(component)) for component in cycles
    )
