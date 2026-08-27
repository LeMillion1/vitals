"""Static gate keeping the singleton conflict writers from coming back.

``enforce`` and ``enforce_day_end`` no longer exist: every write path hands the
engine a subject and the conflict decision together.  This inventory outlived
the migration it was written for and now guards the result — a reintroduced
spelling, an alias, or a direct import fails here rather than quietly restoring
an unscoped write.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_ROOTS = (REPO_ROOT / "vitals", REPO_ROOT / "web")
_LEGACY_WRITER_APIS = frozenset({"enforce", "enforce_day_end"})

_EXPECTED_LEGACY_CALLS = Counter(
    {
    }
)

# The activation service is the only intended replacement.  The router used to
# write ``row.active`` directly; keeping only service functions here makes that
# cutover irreversible. ``toggle_rule_activation`` is reserved so either public
# service spelling remains confined to this module.
_ALLOWED_ACTIVE_WRITER_SCOPES = frozenset(
    {
        (
            "vitals/services/conflicts/activation.py",
            "set_rule_activation",
        ),
        (
            "vitals/services/conflicts/activation.py",
            "toggle_rule_activation",
        ),
    }
)
_REQUIRED_CURRENT_ACTIVE_WRITER_SCOPES = frozenset(
    {
        (
            "vitals/services/conflicts/activation.py",
            "set_rule_activation",
        ),
    }
)
_EXPECTED_DYNAMIC_RULE_WRITER_SCOPES = frozenset(
    {("vitals/services/conflicts/catalog.py", "sync_catalog")}
)


def _production_files() -> Iterable[Path]:
    for root in _PRODUCTION_ROOTS:
        yield from sorted(root.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _string_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "active"
        and isinstance(node.value, ast.Name)
        and node.value.id == "ConflictRule"
    ):
        return "active"
    return None


def _assignment_targets(node: ast.AST) -> Iterable[ast.AST]:
    if isinstance(node, (ast.Tuple, ast.List)):
        for item in node.elts:
            yield from _assignment_targets(item)
        return
    yield node


def _target_sets_active(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "active"
    if isinstance(node, ast.Subscript):
        return _string_key(node.slice) == "active"
    return False


def _imports_conflict_rule(tree: ast.AST) -> tuple[bool, list[str]]:
    imported = False
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name == "vitals.models.conflict_rule":
                    imported = True
                    violations.append(
                        "ConflictRule must use the canonical direct model import"
                    )
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module not in {"vitals.models", "vitals.models.conflict_rule"}:
            continue
        for name in node.names:
            if name.name != "ConflictRule":
                continue
            imported = True
            if name.asname is not None:
                violations.append(
                    f"ConflictRule must not be imported as alias {name.asname!r}"
                )
    return imported, violations


@dataclass(frozen=True)
class _Audit:
    legacy_calls: tuple[tuple[str, str, str], ...]
    legacy_bypasses: tuple[str, ...]
    active_writer_scopes: frozenset[tuple[str, str]]
    active_bypasses: tuple[str, ...]
    dynamic_rule_writer_scopes: frozenset[tuple[str, str]]


class _Visitor(ast.NodeVisitor):
    def __init__(self, *, relative_path: str, imports_conflict_rule: bool) -> None:
        self.relative_path = relative_path
        self.imports_conflict_rule = imports_conflict_rule
        self.scope: list[str] = []
        self.has_canonical_engine_import = False
        self.legacy_calls: list[tuple[str, str, str]] = []
        self.legacy_bypasses: list[str] = []
        self.active_writer_scopes: set[tuple[str, str]] = set()
        self.active_bypasses: list[str] = []
        self.dynamic_rule_writer_scopes: set[tuple[str, str]] = set()
        self._direct_writer_call_nodes: set[int] = set()

    @property
    def function(self) -> str:
        return ".".join(self.scope) if self.scope else "<module>"

    def _where(self, node: ast.AST) -> str:
        return f"{self.relative_path}:{getattr(node, 'lineno', '?')} ({self.function})"

    def _legacy_bypass(self, node: ast.AST, message: str) -> None:
        self.legacy_bypasses.append(f"{self._where(node)}: {message}")

    def _active_write(self) -> None:
        self.active_writer_scopes.add((self.relative_path, self.function))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.scope.append("<lambda>")
        self.generic_visit(node)
        self.scope.pop()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "vitals.services.conflicts":
            for name in node.names:
                if name.name != "engine":
                    continue
                if name.asname is None:
                    self.has_canonical_engine_import = True
                else:
                    self._legacy_bypass(
                        node,
                        f"engine module alias {name.asname!r} is forbidden",
                    )
        elif node.module == "vitals.services.conflicts.engine":
            for name in node.names:
                if name.name in _LEGACY_WRITER_APIS or name.name == "*":
                    self._legacy_bypass(
                        node,
                        f"direct legacy writer import {name.name!r} is forbidden",
                    )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for name in node.names:
            if name.name == "vitals.services.conflicts.engine":
                self._legacy_bypass(
                    node,
                    "import conflict engine through `import` is forbidden; use the "
                    "canonical service import",
                )
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        if self.has_canonical_engine_import and node.arg == "engine":
            self._legacy_bypass(node, "local engine binding is forbidden")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if (
            self.has_canonical_engine_import
            and node.id == "engine"
            and isinstance(node.ctx, ast.Store)
        ):
            self._legacy_bypass(node, "rebinding engine is forbidden")
        if node.id in _LEGACY_WRITER_APIS and isinstance(node.ctx, ast.Load):
            self._legacy_bypass(
                node,
                f"bare legacy writer reference {node.id!r} is forbidden",
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            node.attr in _LEGACY_WRITER_APIS
            and isinstance(node.value, ast.Name)
            and node.value.id == "engine"
            and id(node) not in self._direct_writer_call_nodes
        ):
            self._legacy_bypass(
                node,
                f"legacy writer {node.attr!r} may only be used as a direct call",
            )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _string_key(node.slice) in _LEGACY_WRITER_APIS:
            value = node.value
            engine_dict = (
                isinstance(value, ast.Attribute)
                and value.attr == "__dict__"
                and isinstance(value.value, ast.Name)
                and value.value.id == "engine"
            )
            engine_vars = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "vars"
                and value.args
                and isinstance(value.args[0], ast.Name)
                and value.args[0].id == "engine"
            )
            if engine_dict or engine_vars:
                self._legacy_bypass(
                    node,
                    "dynamic legacy writer dictionary lookup is forbidden",
                )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if (
            self.has_canonical_engine_import
            and isinstance(node.value, ast.Name)
            and node.value.id == "engine"
        ):
            self._legacy_bypass(node, "aliasing the engine module is forbidden")
        if self.imports_conflict_rule:
            for target in node.targets:
                if any(_target_sets_active(item) for item in _assignment_targets(target)):
                    self._active_write()
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self.imports_conflict_rule and any(
            _target_sets_active(item) for item in _assignment_targets(node.target)
        ):
            self._active_write()
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if self.imports_conflict_rule and any(
            _target_sets_active(item) for item in _assignment_targets(node.target)
        ):
            self._active_write()
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        if self.imports_conflict_rule:
            for target in node.targets:
                if any(_target_sets_active(item) for item in _assignment_targets(target)):
                    self.active_bypasses.append(
                        f"{self._where(node)}: deleting ConflictRule.active is forbidden"
                    )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _LEGACY_WRITER_APIS:
            self._direct_writer_call_nodes.add(id(func))
            if (
                isinstance(func.value, ast.Name)
                and func.value.id == "engine"
            ):
                self.legacy_calls.append(
                    (self.relative_path, self.function, func.attr)
                )
            else:
                self._legacy_bypass(
                    node,
                    f"legacy writer {func.attr!r} must be called on canonical "
                    "engine",
                )
        elif isinstance(func, ast.Name) and func.id in _LEGACY_WRITER_APIS:
            self._legacy_bypass(
                node,
                f"bare legacy writer call {func.id!r} is forbidden",
            )

        if (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "engine"
            and _string_key(node.args[1]) in _LEGACY_WRITER_APIS
        ):
            self._legacy_bypass(node, "dynamic legacy writer lookup is forbidden")

        dynamic_import = (
            isinstance(func, ast.Name)
            and func.id in {"__import__", "import_module"}
        ) or (
            isinstance(func, ast.Attribute)
            and func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id == "importlib"
        )
        if (
            dynamic_import
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "vitals.services.conflicts.engine"
        ):
            self._legacy_bypass(node, "dynamic engine import is forbidden")

        if self.imports_conflict_rule:
            if (
                isinstance(func, ast.Name)
                and func.id == "ConflictRule"
                and any(keyword.arg == "active" for keyword in node.keywords)
            ):
                self._active_write()
            if isinstance(func, ast.Name) and func.id == "setattr" and len(node.args) >= 2:
                key = _string_key(node.args[1])
                if key == "active":
                    self._active_write()
                elif key is None:
                    self.dynamic_rule_writer_scopes.add(
                        (self.relative_path, self.function)
                    )
            if isinstance(func, ast.Attribute) and func.attr in {"update", "values"}:
                if any(keyword.arg == "active" for keyword in node.keywords):
                    self._active_write()
                for argument in node.args:
                    if isinstance(argument, ast.Dict) and any(
                        _string_key(key) == "active"
                        for key in argument.keys
                        if key is not None
                    ):
                        self._active_write()

        self.generic_visit(node)


@lru_cache(maxsize=1)
def _audit() -> _Audit:
    calls: list[tuple[str, str, str]] = []
    legacy_bypasses: list[str] = []
    active_scopes: set[tuple[str, str]] = set()
    active_bypasses: list[str] = []
    dynamic_scopes: set[tuple[str, str]] = set()

    for path in _production_files():
        relative = _relative(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports_rule, import_violations = _imports_conflict_rule(tree)
        visitor = _Visitor(
            relative_path=relative,
            imports_conflict_rule=imports_rule,
        )
        visitor.visit(tree)
        if visitor.legacy_calls and not visitor.has_canonical_engine_import:
            visitor.legacy_bypasses.append(
                f"{relative}: canonical engine import is missing"
            )
        calls.extend(visitor.legacy_calls)
        legacy_bypasses.extend(visitor.legacy_bypasses)
        active_scopes.update(visitor.active_writer_scopes)
        active_bypasses.extend(f"{relative}: {item}" for item in import_violations)
        active_bypasses.extend(visitor.active_bypasses)
        dynamic_scopes.update(visitor.dynamic_rule_writer_scopes)

    return _Audit(
        legacy_calls=tuple(sorted(calls)),
        legacy_bypasses=tuple(sorted(set(legacy_bypasses))),
        active_writer_scopes=frozenset(active_scopes),
        active_bypasses=tuple(sorted(set(active_bypasses))),
        dynamic_rule_writer_scopes=frozenset(dynamic_scopes),
    )


def _catalog_fields() -> tuple[str, ...]:
    path = REPO_ROOT / "vitals/services/conflicts/catalog.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values = [
        node.value
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "_CATALOG_FIELDS"
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
        )
    ]
    assert len(values) == 1, "conflict catalog field contract must have one literal"
    literal = ast.literal_eval(values[0])
    assert isinstance(literal, tuple)
    return literal


def test_legacy_conflict_writer_inventory_is_exact() -> None:
    actual = Counter(_audit().legacy_calls)

    assert actual == _EXPECTED_LEGACY_CALLS
    # No legacy enforce site remains: every domain that had one now demands a
    # subject and the conflict decision that authorised the write.
    assert sum(count for (*_, api), count in actual.items() if api == "enforce") == 0
    assert (
        sum(
            count
            for (*_, api), count in actual.items()
            if api == "enforce_day_end"
        )
        == 0
    )


def test_the_unscoped_engine_entry_points_are_gone() -> None:
    """The inventory counts call sites; this counts the functions themselves.

    A zero-call-site inventory would still pass if somebody re-added ``enforce``
    and left it unused, and the next caller would find it waiting.
    """

    from vitals.services.conflicts import engine

    for name in sorted(_LEGACY_WRITER_APIS | {"evaluate"}):
        assert not hasattr(engine, name), (
            f"engine.{name} is back. Every evaluation is scoped: use "
            "evaluate_scoped/enforce_scoped, or enforce_prepared when the write "
            "path already holds a prepared capability."
        )


def test_legacy_conflict_writers_have_no_alias_or_direct_import_bypass() -> None:
    # Scoped readers such as evaluate_scoped/load_scoped_rules are deliberately
    # absent from `_LEGACY_WRITER_APIS` and therefore do not count as writer debt.
    assert _audit().legacy_bypasses == ()


def test_conflict_rule_active_writers_are_confined_to_activation_seams() -> None:
    audit = _audit()

    assert audit.active_bypasses == ()
    assert _REQUIRED_CURRENT_ACTIVE_WRITER_SCOPES <= audit.active_writer_scopes
    assert audit.active_writer_scopes <= _ALLOWED_ACTIVE_WRITER_SCOPES
    assert (
        audit.dynamic_rule_writer_scopes == _EXPECTED_DYNAMIC_RULE_WRITER_SCOPES
    )
    assert "active" not in _catalog_fields(), (
        "catalog.sync_catalog uses dynamic setattr; adding active to "
        "_CATALOG_FIELDS would overwrite the user's activation choice"
    )
