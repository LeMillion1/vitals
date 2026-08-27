"""Pure predicate matching and scoped domain item resolution."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.conflicts.engine.contracts import CONFLICT_ENTITY_KEY, ConflictScope
from vitals.services.conflicts.engine.registry import ConflictResolverUnavailable, _resolvers

logger = logging.getLogger("vitals.services.conflict_engine")


def _normalize_proposed(proposed_state: Any) -> list[dict]:
    def normalized(item: dict) -> dict:
        return {key: value for key, value in item.items() if key != CONFLICT_ENTITY_KEY}

    if proposed_state is None:
        return []
    if isinstance(proposed_state, dict):
        return [normalized(proposed_state)]
    return [normalized(item) for item in proposed_state if isinstance(item, dict)]


# Recognized comparison/membership/presence operators for a field's expected
# value (e.g. ``condition_a = {"dose_mg": {"$gte": 2.0}}``). Any dict whose keys
# all start with "$" is treated as an operator dict rather than a literal value.
_OPERATOR_KEYS = frozenset({"$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$exists", "$contains"})
# Top-level boolean combinators — these replace the implicit per-key AND with OR
# / explicit AND / negation over a list of *conditions* (not field values).
_LOGIC_KEYS = frozenset({"$any", "$all", "$not"})


def _looks_like_operator_dict(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(isinstance(k, str) and k.startswith("$") for k in value)
    )


def _apply_operators(actual: Any, ops: dict) -> bool:
    """Evaluate an operator dict against one field's actual value. Every operator
    present must hold (implicit AND). A comparison against an incompatible type
    (e.g. ``$gt`` on a string) fails the match rather than raising — a malformed
    rule must never crash evaluation/save."""
    try:
        for op, expected in ops.items():
            if op == "$gt":
                if actual is None or not (actual > expected):
                    return False
            elif op == "$gte":
                if actual is None or not (actual >= expected):
                    return False
            elif op == "$lt":
                if actual is None or not (actual < expected):
                    return False
            elif op == "$lte":
                if actual is None or not (actual <= expected):
                    return False
            elif op == "$in":
                if actual not in expected:
                    return False
            elif op == "$nin":
                if actual in expected:
                    return False
            elif op == "$exists":
                if (actual is not None) != bool(expected):
                    return False
            elif op == "$contains":
                if actual is None or expected not in actual:
                    return False
            else:
                logger.warning("conflict_engine: unknown operator %r ignored", op)
        return True
    except TypeError:
        logger.warning("conflict_engine: type mismatch evaluating %r against %r", ops, actual)
        return False


def _field_matches(actual: Any, expected: Any) -> bool:
    if _looks_like_operator_dict(expected):
        return _apply_operators(actual, expected)
    return actual == expected


def _matches(condition: dict, item: dict) -> bool:
    """Predicate match: every key in ``condition`` must hold against ``item``
    (implicit AND). A key's value is either a literal (equality, the original
    behavior — fully backward compatible) or an operator dict (``$gt``/``$gte``/
    ``$lt``/``$lte``/``$in``/``$nin``/``$exists``/``$contains``). The three
    top-level keys ``$any``/``$all``/``$not`` take a list of *conditions* (or, for
    ``$not``, a single condition) instead of matching a field, giving OR/AND/NOT
    over whole sub-conditions. An empty condition matches any item (a
    domain-presence rule)."""
    if not isinstance(condition, dict):
        return False
    for key, expected in condition.items():
        if key == "$any":
            if not (
                isinstance(expected, (list, tuple)) and any(_matches(c, item) for c in expected)
            ):
                return False
        elif key == "$all":
            if not (
                isinstance(expected, (list, tuple)) and all(_matches(c, item) for c in expected)
            ):
                return False
        elif key == "$not":
            if _matches(expected, item):
                return False
        elif not _field_matches(item.get(key), expected):
            return False
    return True


async def _domain_items(
    session: AsyncSession,
    domain: str,
    changed_domain: str,
    proposed_items: list[dict],
    *,
    scope: ConflictScope,
    replace_entity_key: str | None = None,
) -> list[dict]:
    """Current items of ``domain``, plus the proposed items when ``domain`` is the
    one being changed (so a new item can clash with something already present in
    the same domain, e.g. retinoid + peel the same evening)."""
    items: list[dict] = []
    registration = _resolvers.get(domain)
    if registration is None:
        raise ConflictResolverUnavailable(
            f"no scoped conflict resolver is registered for domain {domain!r}"
        )
    items.extend(await registration.scoped(session, scope=scope))
    if domain == changed_domain and replace_entity_key is not None:
        items = [item for item in items if item.get(CONFLICT_ENTITY_KEY) != replace_entity_key]
    # Entity markers are resolver bookkeeping, never part of the custom-rule
    # predicate grammar or an externally supplied proposed-state shape.
    items = [
        {key: value for key, value in item.items() if key != CONFLICT_ENTITY_KEY} for item in items
    ]
    if domain == changed_domain:
        items.extend(proposed_items)
    return items


def _side_satisfied(condition: dict, items: list[dict]) -> bool:
    return any(_matches(condition, item) for item in items)


def _matching_items(condition: dict, items: list[dict]) -> list[dict]:
    return [item for item in items if _matches(condition, item)]


def _slots(items: list[dict]) -> set:
    """The distinct non-empty ``timing_slot`` values carried by matching items.
    Only the supplements resolver currently sets this key (see
    ``supplement_parsing._parse_slot``); other domains' items simply have no
    slot, which safely excludes them here."""
    return {item.get("timing_slot") for item in items if item.get("timing_slot")}
