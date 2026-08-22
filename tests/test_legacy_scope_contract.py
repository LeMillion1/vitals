"""The ratchet that makes PR-04's scoped-service work measurable.

Every core service still accepts an omittable scope: pass ``subject_id`` (or an
``identity``/``context``, and sometimes ``include_legacy_unowned``) and the call
is scoped; omit it and it reads or writes across the whole installation. That
optionality is the last thing standing between this schema and a second person.

This test recomputes the inventory from the source and compares it with the
registry. A module that grows a new bridge fails; a module that loses one fails
too, until the registry is updated to record the progress. The registry can
therefore only move in one direction, and reaching zero is the condition for
making the compatibility columns ``NOT NULL``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from vitals.legacy_scope import (
    LEGACY_BARE_ID_READS,
    LEGACY_SCOPE_BRIDGES,
    bare_id_read_total,
    bridge_total,
)


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPOSITORY_ROOT / "vitals"

# A function is bridged when the scope it needs may be left out.
_OMITTABLE_SCOPE_PARAMETERS = ("subject_id", "identity", "context")
_ESCAPE_HATCH = "include_legacy_unowned"


def _defaults(args: ast.arguments) -> dict[str, ast.expr | None]:
    mapping: dict[str, ast.expr | None] = {}
    for argument, default in zip(args.kwonlyargs, args.kw_defaults):
        mapping[argument.arg] = default
    if args.defaults:
        positional = args.args[len(args.args) - len(args.defaults) :]
        for argument, default in zip(positional, args.defaults):
            mapping[argument.arg] = default
    return mapping


def _is_bridged(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    names = {argument.arg for argument in (*node.args.args, *node.args.kwonlyargs)}
    if _ESCAPE_HATCH in names:
        return True
    defaults = _defaults(node.args)
    return any(
        name in names
        and isinstance(defaults.get(name), ast.Constant)
        and defaults[name].value is None
        for name in _OMITTABLE_SCOPE_PARAMETERS
    )


def _observed() -> dict[str, frozenset[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        module = str(path.relative_to(REPOSITORY_ROOT)).replace("/", ".")[:-3]
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _is_bridged(node):
                found.setdefault(module, set()).add(node.name)
    return {module: frozenset(names) for module, names in found.items()}


def test_no_module_grows_an_unlisted_legacy_bridge():
    observed = _observed()
    for module, names in sorted(observed.items()):
        recorded = LEGACY_SCOPE_BRIDGES.get(module, frozenset())
        unlisted = sorted(names - recorded)
        assert not unlisted, (
            f"{module} added an unscoped bridge: {unlisted}. Give the function a "
            "mandatory scope, or record it in vitals/legacy_scope.py with the "
            "reason it cannot have one yet."
        )


def test_the_registry_records_no_bridge_that_is_already_closed():
    observed = _observed()
    for module, recorded in sorted(LEGACY_SCOPE_BRIDGES.items()):
        stale = sorted(recorded - observed.get(module, frozenset()))
        assert not stale, (
            f"{module} no longer bridges {stale}. Remove them from "
            "vitals/legacy_scope.py so the remaining work stays honest."
        )


def test_the_registry_is_immutable_and_empty():
    import pytest

    with pytest.raises(TypeError):
        LEGACY_SCOPE_BRIDGES["x"] = frozenset()  # type: ignore[index]
    # Zero. Every service now demands its scope, which is the precondition for
    # making the compatibility columns NOT NULL and for turning on FORCE RLS.
    # This is an equality, not a bound: the ratchet held all the way down and
    # nothing may reopen a bridge without deleting this line.
    assert bridge_total() == 0


def _observed_bare_id_reads() -> dict[str, frozenset[str]]:
    """Find ``session.get(Model, id)`` calls against subject-owned models."""

    import vitals.models  # noqa: F401 -- register every mapper

    from vitals.models.base import Base
    from vitals.ownership import OWNERSHIP_REGISTRY, TargetColumn

    tables = {
        mapper.class_.__name__: mapper.class_.__tablename__
        for mapper in Base.registry.mappers
    }
    found: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        module = str(path.relative_to(REPOSITORY_ROOT)).replace("/", ".")[:-3]
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and function.attr == "get"
                and isinstance(function.value, ast.Name)
                and function.value.id == "session"
            ):
                continue
            if not node.args or not isinstance(node.args[0], ast.Name):
                continue
            table = tables.get(node.args[0].id)
            if table is None:
                continue
            spec = OWNERSHIP_REGISTRY.get(table)
            if spec is None or spec.subject is TargetColumn.NONE:
                continue
            found.setdefault(module, set()).add(node.args[0].id)
    return {module: frozenset(models) for module, models in found.items()}


def test_no_module_adds_an_unlisted_bare_key_read():
    observed = _observed_bare_id_reads()
    for module, models in sorted(observed.items()):
        recorded = LEGACY_BARE_ID_READS.get(module, frozenset())
        unlisted = sorted(models - recorded)
        assert not unlisted, (
            f"{module} reads {unlisted} by bare primary key. A key proves "
            "nothing about who the row belongs to — resolve it inside the "
            "caller's scope, or record it in vitals/legacy_scope.py."
        )


def test_the_bare_key_registry_records_nothing_already_closed():
    observed = _observed_bare_id_reads()
    for module, recorded in sorted(LEGACY_BARE_ID_READS.items()):
        stale = sorted(recorded - observed.get(module, frozenset()))
        assert not stale, (
            f"{module} no longer reads {stale} by bare primary key. Remove them "
            "from vitals/legacy_scope.py so the remaining work stays honest."
        )
    assert bare_id_read_total() >= 0
