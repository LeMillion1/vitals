"""Conflict enforcement, override alerts, and day-end reconciliation."""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Severity
from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.alerts import lifecycle as alerts_service_lifecycle
from vitals.services.alerts import validation as alerts_service_validation
from vitals.services.conflicts.engine.contracts import (
    ConflictBlocked,
    ConflictOverrideActorRequired,
    ConflictWriteContext,
    ConflictWriteRuleError,
    PreparedConflictWrite,
    Violation,
)
from vitals.services.conflicts.engine.evaluation import evaluate_scoped
from vitals.services.conflicts.engine.rules import _load_scoped_rules_unchecked
from vitals.services.conflicts.engine.scope import (
    _alert_bridge,
    _health_alert_context,
    _require_live_prepared_write,
    _require_typed_domain,
    prepare_scoped_write,
)


def _stable_violations(violations: Sequence[Violation]) -> list[Violation]:
    """Return deterministic rule-id order before any alert-key lock is taken."""

    return sorted(
        violations,
        key=lambda violation: (
            violation.rule_id is None,
            violation.rule_id if violation.rule_id is not None else 0,
        ),
    )


def _conflict_alert_plan(
    violations: Sequence[Violation],
    *,
    entity_ref: str,
    override: bool,
) -> list[tuple[Violation, str, Severity, bool]]:
    """Validate every derived alert before the first one can be mutated."""

    plan: list[tuple[Violation, str, Severity, bool]] = []
    try:
        alerts_service_validation._require_entity_ref(entity_ref)
        for violation in violations:
            rule_id = violation.rule_id
            if isinstance(rule_id, bool) or not isinstance(rule_id, int) or rule_id < 1:
                raise ConflictWriteRuleError("a firing conflict rule has no persisted positive id")
            alert_key = f"conflict:{rule_id}"
            alerts_service_validation._require_key(alert_key)
            alerts_service_validation._require_message(violation.message)
            try:
                severity = Severity(violation.severity)
            except (TypeError, ValueError) as exc:
                raise ConflictWriteRuleError(
                    "a firing conflict rule has an unknown severity"
                ) from exc
            plan.append(
                (
                    violation,
                    alert_key,
                    severity,
                    violation.is_blocking and override,
                )
            )
    except alerts_service_contracts.AlertValidationError as exc:
        raise ConflictWriteRuleError(str(exc)) from exc
    return plan


async def enforce_prepared(
    session: AsyncSession,
    *,
    prepared: PreparedConflictWrite,
    domain: Domain,
    proposed_state: Any = None,
    override: bool = False,
    entity_ref: str = "",
    include_day_end: bool = False,
    replace_entity_key: str | None = None,
) -> list[Violation]:
    """Evaluate and persist conflicts using a live prepared identity proof."""

    context = _require_live_prepared_write(session, prepared)
    _require_typed_domain(domain)
    if not isinstance(override, bool):
        raise TypeError("override must be a boolean")
    if not isinstance(include_day_end, bool):
        raise TypeError("include_day_end must be a boolean")
    if override and context.identity.actor_user_id is None:
        raise ConflictOverrideActorRequired("conflict override requires an active human actor")

    violations = _stable_violations(
        await evaluate_scoped(
            session,
            scope=context.scope,
            domain=domain,
            proposed_state=proposed_state,
            include_day_end=include_day_end,
            replace_entity_key=replace_entity_key,
        )
    )
    blocking = [violation for violation in violations if violation.is_blocking]
    if blocking and not override:
        # The whole evaluation completes before this branch and no alert function
        # has run, so passive siblings cannot leak through a blocked save.
        raise ConflictBlocked(violations)

    plan = _conflict_alert_plan(
        violations,
        entity_ref=entity_ref,
        override=override,
    )
    alert_context = _health_alert_context(context)
    alert_bridge = _alert_bridge(context)
    for violation, alert_key, severity, overridden in plan:
        await alerts_service_lifecycle.raise_scoped_alert(
            session,
            context=alert_context,
            domain=domain,
            severity=severity,
            message=violation.message,
            alert_key=alert_key,
            entity_ref=entity_ref,
            legacy_bridge=alert_bridge,
            overridden=overridden,
        )
    return violations


async def enforce_scoped(
    session: AsyncSession,
    *,
    context: ConflictWriteContext,
    domain: Domain,
    proposed_state: Any = None,
    override: bool = False,
    entity_ref: str = "",
    include_day_end: bool = False,
    replace_entity_key: str | None = None,
) -> list[Violation]:
    """Prepare identity roots, then run the typed scoped enforcement flow."""

    prepared = await prepare_scoped_write(session, context=context)
    return await enforce_prepared(
        session,
        prepared=prepared,
        domain=domain,
        proposed_state=proposed_state,
        override=override,
        entity_ref=entity_ref,
        include_day_end=include_day_end,
        replace_entity_key=replace_entity_key,
    )


async def reconcile_day_end_scoped(
    session: AsyncSession,
    *,
    context: ConflictWriteContext,
    domain: Domain,
    entity_ref: str = "",
) -> list[Violation]:
    """Raise and clear day-end conflicts inside one exact health scope."""

    _require_typed_domain(domain)
    prepared = await prepare_scoped_write(session, context=context)
    live_context = _require_live_prepared_write(session, prepared)
    violations = _stable_violations(
        await evaluate_scoped(
            session,
            scope=live_context.scope,
            domain=domain,
            include_day_end=True,
        )
    )
    fired_violations = [
        violation for violation in violations if (violation.params or {}).get("day_end_only")
    ]
    fired = {violation.rule_id: violation for violation in fired_violations}
    plan = {
        violation.rule_id: (alert_key, severity)
        for violation, alert_key, severity, _overridden in _conflict_alert_plan(
            fired_violations,
            entity_ref=entity_ref,
            override=False,
        )
    }

    rules = await _load_scoped_rules_unchecked(
        session,
        scope=live_context.scope,
        domain=domain,
    )
    day_end_rules = [rule for rule in rules if (rule.params or {}).get("day_end_only")]
    for rule in day_end_rules:
        if isinstance(rule.id, bool) or not isinstance(rule.id, int) or rule.id < 1:
            raise ConflictWriteRuleError(
                "an active day-end conflict rule has no persisted positive id"
            )

    alert_context = _health_alert_context(live_context)
    alert_bridge = _alert_bridge(live_context)
    for rule in day_end_rules:
        assert rule.id is not None
        alert_key = f"conflict:{rule.id}"
        violation = fired.get(rule.id)
        if violation is None:
            await alerts_service_lifecycle.resolve_scoped_superseded(
                session,
                context=alert_context,
                alert_key=alert_key,
                keep_entity=None,
                legacy_bridge=alert_bridge,
            )
            continue
        planned_key, severity = plan[rule.id]
        await alerts_service_lifecycle.resolve_scoped_superseded(
            session,
            context=alert_context,
            alert_key=planned_key,
            keep_entity=entity_ref,
            legacy_bridge=alert_bridge,
        )
        await alerts_service_lifecycle.raise_scoped_alert(
            session,
            context=alert_context,
            domain=domain,
            severity=severity,
            message=violation.message,
            alert_key=planned_key,
            entity_ref=entity_ref,
            legacy_bridge=alert_bridge,
        )
    return violations
