"""Generic conflict-read MCP tools without router or ORM dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from vitals.enums import Domain
from vitals.services.conflicts import activation, engine


VALID_CONFLICT_DOMAINS = frozenset(domain.value for domain in Domain)


@dataclass(frozen=True)
class ConflictToolDependencies:
    get_session_factory: Callable[[], Any]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    serialize_row: Callable[[Any], dict]


@dataclass(frozen=True)
class RegisteredConflictTools:
    list_conflict_rules: Callable[..., Awaitable[list[dict]]]
    check_conflicts: Callable[..., Awaitable[list[dict]]]


def register_conflict_tools(
    server: Any,
    deps: ConflictToolDependencies,
) -> RegisteredConflictTools:
    """Register generic conflict catalog/evaluation tools in frozen order."""

    @server.tool()
    async def list_conflict_rules(
        domain: Optional[str] = None,
        category: Optional[str] = None,
    ) -> list[dict]:
        """Lists the curated cross-domain conflict rules (vitals/data/conflict_rules.yaml),
        optionally filtered by ``domain`` (matches either side of the rule) and/or
        ``category`` (absorption, pharmacogenomics, dermatology, lab_safety, glp1,
        contraindication). Only ``active`` rules are meaningful for evaluation, but
        inactive ones are included too so a caller can see the full catalog."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            rows = await engine.load_scoped_rules(
                session,
                scope=scope,
                domain=domain,
                active_only=False,
            )
            activation_state = await activation.read_activation_state(
                session,
                subject_id=scope.subject_id,
                legacy_bridge=scope.legacy_bridge,
            )
            rule_activation = activation.effective_rule_activation(
                rows,
                activation_state,
            )
            if category:
                rows = [row for row in rows if row.category == category]
            payloads = []
            for row in rows:
                payload = deps.serialize_row(row)
                payload["active"] = rule_activation[row.id]
                payloads.append(payload)
            return payloads

    @server.tool()
    async def check_conflicts(domain: str, payload: dict) -> list[dict]:
        """Evaluates an arbitrary proposed state against the active conflict rules
        for ``domain`` (one of: weight, glp1, supplements, genetics, skincare,
        labs, nutrition, workouts, garmin, milestones, system, body_comp). E.g.
        ``check_conflicts("labs", {"marker": "Калий", "value": 5.5})`` or
        ``check_conflicts("supplements", {"key": "iron", "active": True})``.
        Read-only — never writes, never blocks; returns the violations that would
        fire if this state were saved."""
        if domain not in VALID_CONFLICT_DOMAINS:
            choices = ", ".join(sorted(VALID_CONFLICT_DOMAINS))
            return [{"error": f"Unknown domain '{domain}'. Use one of: {choices}"}]

        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            try:
                violations = await engine.evaluate_scoped(
                    session,
                    scope=scope,
                    domain=domain,
                    proposed_state=payload,
                )
            except engine.ConflictResolverUnavailable as exc:
                return [{"error": str(exc)}]
            return [violation.to_dict() for violation in violations]

    return RegisteredConflictTools(
        list_conflict_rules=list_conflict_rules,
        check_conflicts=check_conflicts,
    )


__all__ = [
    "ConflictToolDependencies",
    "RegisteredConflictTools",
    "VALID_CONFLICT_DOMAINS",
    "register_conflict_tools",
]
