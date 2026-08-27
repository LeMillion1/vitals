"""Scoped rule evaluation and legacy read adapter."""

from __future__ import annotations

from datetime import date as date_type
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, RuleType
from vitals.services.conflicts.engine.contracts import ConflictScope, Violation
from vitals.services.conflicts.engine.matching import (
    _domain_items,
    _matching_items,
    _normalize_proposed,
    _slots,
)
from vitals.services.conflicts.engine.rules import _load_scoped_rules_unchecked
from vitals.services.conflicts.engine.scope import (
    _domain_value,
    _validate_scope,
    resolve_legacy_conflict_scope,
)


async def _evaluate(
    session: AsyncSession,
    domain: str,
    proposed_state: Any = None,
    *,
    include_day_end: bool = False,
    scope: ConflictScope,
    replace_entity_key: str | None = None,
) -> list[Violation]:
    """Evaluate active rules touching ``domain`` against ``proposed_state`` and the
    current state of the other domains. Pure read — returns the firing violations,
    writes nothing.

    Rules whose ``params`` carry ``day_end_only: true`` are skipped unless
    ``include_day_end`` is set. Those rules compare a same-day running total
    against a lower-bound threshold (e.g. "today's calories < 800") which is
    trivially true early in the day — they're only meaningful once the day is
    essentially over, so a once-daily scheduled job (not the live save path)
    passes ``include_day_end=True`` to evaluate them. Every other caller is
    unaffected by default.
    """
    proposed_items = _normalize_proposed(proposed_state)

    rules = await _load_scoped_rules_unchecked(
        session,
        scope=scope,
        domain=domain,
    )

    violations: list[Violation] = []
    item_cache: dict[str, list[dict]] = {}
    for rule in rules:
        if not include_day_end and (rule.params or {}).get("day_end_only"):
            continue
        if rule.domain_a not in item_cache:
            item_cache[rule.domain_a] = await _domain_items(
                session,
                rule.domain_a,
                domain,
                proposed_items,
                scope=scope,
                replace_entity_key=replace_entity_key,
            )
        if rule.domain_b not in item_cache:
            item_cache[rule.domain_b] = await _domain_items(
                session,
                rule.domain_b,
                domain,
                proposed_items,
                scope=scope,
                replace_entity_key=replace_entity_key,
            )
        items_a = item_cache[rule.domain_a]
        items_b = item_cache[rule.domain_b]

        matches_a = _matching_items(rule.condition_a or {}, items_a)
        matches_b = _matching_items(rule.condition_b or {}, items_b)
        if not matches_a or not matches_b:
            continue

        if _is_timing_rule(rule.rule_type):
            # A timing_separation rule is about two items taken *together* — it
            # only fires when some matching item on each side shares the same
            # declared AM/PM/MEAL/DAY slot. Different (or unknown) slots mean
            # they're already separated in practice, so no warning is raised.
            if not (_slots(matches_a) & _slots(matches_b)):
                continue

        violations.append(
            Violation(
                rule_id=rule.id,
                rule_type=rule.rule_type,
                severity=rule.severity,
                message=rule.message,
                domain_a=rule.domain_a,
                domain_b=rule.domain_b,
                params=dict(rule.params or {}),
                category=rule.category,
                source=rule.source,
                evidence=rule.evidence,
            )
        )
    return violations


async def evaluate_scoped(
    session: AsyncSession,
    *,
    scope: ConflictScope,
    domain: Domain | str,
    proposed_state: Any = None,
    include_day_end: bool = False,
    replace_entity_key: str | None = None,
) -> list[Violation]:
    """Evaluate rules and facts belonging to exactly one health subject."""

    await _validate_scope(session, scope)
    if replace_entity_key is not None and (
        not isinstance(replace_entity_key, str) or not replace_entity_key.strip()
    ):
        raise TypeError("replace_entity_key must be a non-blank string or None")
    domain_value = _domain_value(domain)
    return await _evaluate(
        session,
        domain_value,
        proposed_state,
        include_day_end=include_day_end,
        scope=scope,
        replace_entity_key=replace_entity_key,
    )


async def evaluate_legacy_single_subject(
    session: AsyncSession,
    domain: Domain | str,
    proposed_state: Any = None,
    *,
    evaluation_date: date_type | None = None,
    include_day_end: bool = False,
) -> list[Violation]:
    """Explicit read adapter for the installation-token/single-owner stage."""

    scope = await resolve_legacy_conflict_scope(
        session,
        actor_username=None,
        evaluation_date=evaluation_date,
    )
    return await evaluate_scoped(
        session,
        scope=scope,
        domain=domain,
        proposed_state=proposed_state,
        include_day_end=include_day_end,
    )


def _is_timing_rule(rule_type: str) -> bool:
    return rule_type == RuleType.TIMING_SEPARATION.value
