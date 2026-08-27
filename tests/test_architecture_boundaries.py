"""Static dependency guards for the core application architecture."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VITALS = ROOT / "vitals"
SERVICES = VITALS / "services"


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _tracked_python_paths(root: Path) -> list[Path]:
    relative_root = root.relative_to(ROOT).as_posix()
    tracked = subprocess.run(
        ["git", "ls-files", "--", relative_root],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return [Path(path) for path in tracked if path.endswith(".py")]


def _platform_scope_callers() -> set[tuple[str, str | None]]:
    callers: set[tuple[str, str | None]] = set()
    for path in _python_files(VITALS) + _python_files(ROOT / "web"):
        source = path.read_text(encoding="utf-8")
        if "enter_platform_scope" not in source or path.name == "rls_session.py":
            continue

        tree = ast.parse(source, filename=str(path))
        stack: list[tuple[ast.AST, str | None]] = [(tree, None)]
        while stack:
            node, owner = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = node.name
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "enter_platform_scope"
            ):
                callers.add((path.relative_to(ROOT).as_posix(), owner))
            stack.extend((child, owner) for child in ast.iter_child_nodes(node))
    return callers


def _rls_policy_tables(migration_files: list[Path], current_tables: set[str]) -> set[str]:
    """Derive the live RLS union from every migration that names policy tables."""

    attributes = (
        "SUBJECT_ISOLATED_TABLES",
        "INHERITED_CHILDREN",
        "SHARED_WITH_INSTALLATION",
    )
    covered: set[str] = set()
    for path in migration_files:
        source = path.read_text(encoding="utf-8")
        if not any(attribute in source for attribute in attributes):
            continue
        spec = importlib.util.spec_from_file_location(f"_architecture_counter_{path.stem}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for attribute in attributes:
            covered.update(getattr(module, attribute, ()))
    return covered & current_tables


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
            name
            for name in _imports(path)
            if name == "web"
            or name.startswith("web.")
            or name == "fastapi"
            or name.startswith("fastapi.")
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

    assert not violations, "Application services depend on operational code:\n" + "\n".join(
        violations
    )


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
                or name.startswith(tuple(f"{prefix}." for prefix in forbidden_prefixes))
            )
            if forbidden:
                violations.append(f"{path.relative_to(ROOT)}: {', '.join(forbidden)}")

    assert not violations, "Pure analytics depend on I/O layers:\n" + "\n".join(violations)


def test_no_new_flat_service_modules() -> None:
    """Turn the current flat directory into an explicit, shrinking debt ledger."""

    legacy = {
        path.name
        for path in _tracked_python_paths(SERVICES)
        if path.parent == Path("vitals/services") and path.name != "__init__.py"
    }
    baseline = 39

    assert len(legacy) <= baseline, (
        f"Flat service module count grew from the guarded ceiling {baseline} to {len(legacy)}. "
        "Put new behavior in a bounded-context package instead."
    )


def test_architecture_reference_counters_match_the_source_tree() -> None:
    """The visual reference must not silently keep yesterday's topology."""

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    import vitals.models  # noqa: F401 - populate SQLAlchemy metadata
    from vitals.enums import Domain, RECORD_SECTIONS
    from vitals.models.base import Base
    from vitals.ownership import OWNERSHIP_REGISTRY
    from vitals.ownership_deploy import OWNERSHIP_BACKFILL_SEQUENCE
    from vitals.scheduler.scheduler import JOB_FAILURE_FAMILY_BY_ID

    migration_files = sorted((ROOT / "migrations" / "versions").glob("*.py"))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1
    head = heads[0]

    tracked_services = _tracked_python_paths(SERVICES)
    tracked_integrations = _tracked_python_paths(VITALS / "integrations")
    tracked_routers = _tracked_python_paths(ROOT / "web" / "routers")
    tables = len(Base.metadata.tables)
    current_table_names = set(Base.metadata.tables)
    subject_tables = sum("subject_id" in table.c for table in Base.metadata.tables.values())
    rls_policy_tables = _rls_policy_tables(migration_files, current_table_names)
    rls_tables = len(rls_policy_tables)
    assert rls_tables == subject_tables
    mandatory_subject_tables = sum(
        "subject_id" in table.c and not table.c.subject_id.nullable
        for table in Base.metadata.tables.values()
    )
    phases = len(OWNERSHIP_BACKFILL_SEQUENCE)
    sequenced_scripts = [step.script for step in OWNERSHIP_BACKFILL_SEQUENCE]
    ownership_scripts = {
        path.name
        for path in (ROOT / "scripts").glob("backfill_*.py")
        if path.name != "backfill_garmin_reparse.py"
    }
    assert set(sequenced_scripts) == ownership_scripts
    assert len(sequenced_scripts) == len(set(sequenced_scripts))
    assert len({step.phase for step in OWNERSHIP_BACKFILL_SEQUENCE}) == phases
    services = sum(path.name != "__init__.py" for path in tracked_services)
    flat_services = sum(
        path.parent == Path("vitals/services") and path.name != "__init__.py"
        for path in tracked_services
    )
    integrations = sum(path.name != "__init__.py" for path in tracked_integrations)
    routers = sum(path.name != "__init__.py" for path in tracked_routers)
    jobs = len(JOB_FAILURE_FAMILY_BY_ID)
    jobs_tree = ast.parse((VITALS / "scheduler" / "jobs.py").read_text(encoding="utf-8"))
    fanout_jobs = sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"for_each_subject", "for_each_connection"}
        for node in ast.walk(jobs_tree)
    )
    conflict_tree = ast.parse(
        (SERVICES / "conflicts" / "registrations.py").read_text(encoding="utf-8")
    )
    conflict_domains = sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "register_domain_resolver"
        for node in ast.walk(conflict_tree)
    )
    platform_scope_functions = len(_platform_scope_callers())
    domain_count = len(Domain)
    record_section_count = len(RECORD_SECTIONS)
    ownership_counts = Counter(
        specification.ownership.value for specification in OWNERSHIP_REGISTRY.values()
    )
    assert len(OWNERSHIP_REGISTRY) == tables
    subject_data_tables = ownership_counts["subject_data"]

    markdown = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    html = (ROOT / "docs" / "ARCHITECTURE.html").read_text(encoding="utf-8")
    audit = (ROOT / "docs" / "MULTI_USER_IMPLEMENTATION_AUDIT.md").read_text(encoding="utf-8")

    expected_markdown_rows = {
        f"| table count, ownership classes | `vitals/ownership.py` | {tables} tables, {subject_data_tables} of them `subject_data` |",
        f"| mandatory-subject table count | `subject_id` NOT NULL in `Base.metadata` | {mandatory_subject_tables} |",
        f"| the backfill phases | `OWNERSHIP_BACKFILL_SEQUENCE` in `vitals/ownership_deploy.py` | {phases} |",
        f"| the domains | `vitals.enums.Domain` | {domain_count} |",
        f"| external integration modules | tracked non-`__init__` modules in `vitals/integrations/` | {integrations} |",
        f"| the scheduled jobs | `vitals/scheduler/jobs.py` | {jobs}, of which {fanout_jobs} fan out per record |",
        f"| migration count | `migrations/versions/` | {len(migration_files)}, head `{head}` |",
        f"| RLS table count | table coverage from revisions `0050` through `0079`, plus the `0083` worker-capability policy rewrite, asserted in `tests/test_row_level_security.py` | {rls_tables} |",
        f"| platform-scope functions | the permitted list in `tests/test_row_level_security.py` | {platform_scope_functions} |",
        f"| routers, tracked application-service modules | tracked non-`__init__` files in `web/routers/`, `vitals/services/` | {routers} and {services} |",
    }
    assert expected_markdown_rows <= set(markdown.splitlines())

    expected_audit_rows = {
        f"| Alembic head | `.venv/bin/python -m alembic heads` → `{head} (head)`; {len(migration_files)} files in `migrations/versions` | Verified |",
        f"| SQLAlchemy tables | `len(Base.metadata.tables)` → {tables} | Verified |",
        f"| Ownership registry | `len(OWNERSHIP_REGISTRY)` → {len(OWNERSHIP_REGISTRY)} | Verified and exhaustive in code |",
        f"| Subject-scoped tables | {subject_tables} metadata tables carry `subject_id`; the RLS revision union covers the same set | Verified |",
        f"| Required subject columns | {mandatory_subject_tables} of the {subject_tables} `subject_id` columns are non-null | Verified |",
        f"| Ownership cutover | {phases} ordered backfill phases and {phases} matching scripts | Verified |",
        f"| Domain enum | {domain_count} values: {record_section_count} record sections plus internal `system` | Verified |",
        f"| External integration modules | {integrations} tracked modules under `vitals/integrations` | Verified |",
        f"| Web routers | {routers} tracked non-`__init__` modules under `web/routers` | Verified |",
        f"| Application services | {services} tracked non-`__init__` modules under `vitals/services`, recursively | Verified |",
        f"| Flat service debt | {flat_services} tracked root modules under `vitals/services`; guarded against growth by `test_architecture_boundaries.py` | Verified, reduced by {74 - flat_services} |",
        f"| Scheduled jobs | {jobs} registered jobs; {fanout_jobs} fan out per subject or provider connection | Verified |",
        f"| Platform-scope callers | the AST contract in `tests/test_row_level_security.py` enumerates {platform_scope_functions} exact permitted functions; invitation acceptance is no longer one of them | Verified and shrinking |",
    }
    assert expected_audit_rows <= set(audit.splitlines())

    assert f"<span><b>{tables}</b> tables</span>" in html
    assert f"<span><b>{len(migration_files)}</b> migrations · head {head}</span>" in html
    assert f"<span><b>{domain_count}</b> domains</span>" in html
    assert f"<span><b>{services}</b> application services</span>" in html
    assert f"<span><b>{rls_tables}</b> RLS tables</span>" in html
    assert f"{integrations} external integration modules" in html
    assert f"'>{routers} routers · HTMX UI" in html
    assert f"'>{jobs} APScheduler jobs · {fanout_jobs} fan-out" in html
    assert f"'>{services} tracked service modules" in html
    assert f"'>{tables} tables · JSONB lake" in html
    assert f"mandatory on {mandatory_subject_tables} tables" in html
    assert f"FORCE RLS on {rls_tables} tables" in html
    assert f"{phases} resumable backfill phases" in html
    assert f"bootstrap, then {phases} phases" in html
    assert f"{platform_scope_functions} permitted caller functions" in html
    assert f"the {conflict_domains} registered conflict domains" in html
    assert f"upgrade through {head}</b>" in html
    for ownership_class, count in ownership_counts.items():
        assert f"<tr><td><code>{ownership_class}</code></td><td>{count}</td>" in html


def test_core_has_no_import_time_cycles() -> None:
    cycles = [
        component for component in _strong_components(_import_time_graph()) if len(component) > 1
    ]

    assert not cycles, "Import-time dependency cycles:\n" + "\n".join(
        " -> ".join(sorted(component)) for component in cycles
    )
