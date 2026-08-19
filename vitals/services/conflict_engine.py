"""Cross-domain conflict engine (framework).

``conflict_rules`` are **data** (see models/conflict_rule.py). This module
evaluates the active rules against proposed state and the current state of other
domains, producing :class:`Violation`s, and enforces the override flow:

    1. A mutating service calls :func:`enforce(session, domain, proposed_state,
       override=...)` before it persists.
    2. Any ``block`` violation with ``override=False`` → raises
       :class:`ConflictBlocked` (the router turns this into HTTP 409 + the
       violations payload; the UI offers "Save anyway (Override)").
    3. On override → the write proceeds and each overridden block is recorded as
       an alert with ``override_at`` stamped.
    4. ``soft_warn`` / ``timing_separation`` / ``info`` violations never block —
       they're written as passive alert rows.

How a domain's *current* state is known is module-specific, so modules register a
resolver via :func:`register_domain_resolver`. The foundation ships only the
framework + a subset-equality matcher; Supplements / Genetics / Skincare register
real resolvers and seed real rules.
"""
from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date as date_type
from enum import StrEnum
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, RuleType, Severity
from vitals.models.conflict_rule import ConflictRule
from vitals.services import alerts_service
from vitals.utils.timeutils import today_local

logger = logging.getLogger(__name__)


class LegacyConflictBridge(StrEnum):
    """Whether fully-unowned facts may join one scoped evaluation.

    ``FULLY_UNOWNED`` is an expand/contract bridge, not an authorization mode.
    :func:`evaluate_scoped` independently proves that the requested subject is
    still the installation's sole health subject before any resolver may use it.
    """

    REJECT = "reject"
    FULLY_UNOWNED = "fully_unowned"


@dataclass(frozen=True, slots=True)
class ConflictScope:
    """Complete read boundary for one conflict evaluation.

    ``evaluation_date`` is supplied once by the subject-aware boundary so every
    resolver reasons about the same local calendar day.  Resolvers must never
    fall back to the process-wide clock when a scope is available.
    """

    subject_id: uuid.UUID
    evaluation_date: date_type
    legacy_bridge: LegacyConflictBridge = LegacyConflictBridge.REJECT

    def __post_init__(self) -> None:
        if not isinstance(self.subject_id, uuid.UUID) or self.subject_id.int == 0:
            raise TypeError("subject_id must be a non-zero UUID")
        if type(self.evaluation_date) is not date_type:
            raise TypeError("evaluation_date must be a date")
        if not isinstance(self.legacy_bridge, LegacyConflictBridge):
            raise TypeError("legacy_bridge must be a LegacyConflictBridge")

    @property
    def include_legacy_unowned(self) -> bool:
        return self.legacy_bridge is LegacyConflictBridge.FULLY_UNOWNED


class DomainResolver(Protocol):
    """Read one domain inside the exact conflict scope supplied by the engine."""

    async def __call__(
        self,
        session: AsyncSession,
        *,
        scope: ConflictScope,
    ) -> Sequence[Mapping[str, Any]]: ...


LegacyDomainResolver = Callable[[AsyncSession], Awaitable[Sequence[dict]]]


@dataclass(frozen=True, slots=True)
class _ResolverRegistration:
    scoped: DomainResolver | None
    legacy: LegacyDomainResolver | None = None


_resolvers: dict[str, _ResolverRegistration] = {}


class ConflictResolverUnavailable(RuntimeError):
    """An active scoped rule references a domain without a scoped resolver."""


def register_domain_resolver(
    domain: str,
    resolver: DomainResolver | LegacyDomainResolver,
    *,
    legacy_resolver: LegacyDomainResolver | None = None,
) -> None:
    """Register scoped and transitional legacy readers for one domain.

    The primary resolver is always subject-scoped. ``legacy_resolver`` exists
    only so unchanged write paths can keep their pre-commercial behaviour while
    Stage 1 migrates read boundaries; no new read caller may use it.
    """

    # Pre-Stage-1 tests and write-only extensions registered ``resolver(session)``
    # positionally. Keep those functions in the explicitly legacy arm; a real
    # scoped resolver must expose the keyword-only ``scope`` parameter.
    scope_parameter = inspect.signature(resolver).parameters.get("scope")
    if scope_parameter is not None and (
        scope_parameter.kind is not inspect.Parameter.KEYWORD_ONLY
        or scope_parameter.default is not inspect.Parameter.empty
    ):
        raise TypeError("a scoped conflict resolver requires keyword-only scope")
    has_scope = scope_parameter is not None
    _resolvers[domain] = _ResolverRegistration(
        scoped=resolver if has_scope else None,
        legacy=(
            legacy_resolver
            if legacy_resolver is not None
            else (resolver if not has_scope else None)
        ),
    )


def clear_domain_resolvers() -> None:
    """Drop all registered resolvers (test isolation)."""
    _resolvers.clear()


@dataclass(frozen=True)
class Violation:
    rule_id: Optional[int]
    rule_type: str
    severity: str
    message: str
    domain_a: str
    domain_b: str
    params: dict = field(default_factory=dict)
    category: Optional[str] = None
    source: Optional[str] = None
    evidence: Optional[str] = None

    @property
    def is_blocking(self) -> bool:
        return self.severity == Severity.BLOCK.value

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_type": self.rule_type,
            "severity": self.severity,
            "message": self.message,
            "domain_a": self.domain_a,
            "domain_b": self.domain_b,
            "params": self.params,
            "category": self.category,
            "source": self.source,
            "evidence": self.evidence,
        }


class ConflictBlocked(Exception):
    """Raised when a ``block`` rule fires and the caller did not override.

    Carries the full violation list so the router can render the warning panel
    and the override button.
    """

    def __init__(self, violations: Sequence[Violation]):
        self.violations = list(violations)
        blocking = [v.message for v in self.violations if v.is_blocking]
        super().__init__("; ".join(blocking) or "Conflict")


def _normalize_proposed(proposed_state: Any) -> list[dict]:
    if proposed_state is None:
        return []
    if isinstance(proposed_state, dict):
        return [proposed_state]
    return [item for item in proposed_state if isinstance(item, dict)]


# Recognized comparison/membership/presence operators for a field's expected
# value (e.g. ``condition_a = {"dose_mg": {"$gte": 2.0}}``). Any dict whose keys
# all start with "$" is treated as an operator dict rather than a literal value.
_OPERATOR_KEYS = frozenset({"$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$exists", "$contains"})
# Top-level boolean combinators — these replace the implicit per-key AND with OR
# / explicit AND / negation over a list of *conditions* (not field values).
_LOGIC_KEYS = frozenset({"$any", "$all", "$not"})


def _looks_like_operator_dict(value: Any) -> bool:
    return isinstance(value, dict) and bool(value) and all(
        isinstance(k, str) and k.startswith("$") for k in value
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
        logger.warning(
            "conflict_engine: type mismatch evaluating %r against %r", ops, actual
        )
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
            if not (isinstance(expected, (list, tuple)) and any(_matches(c, item) for c in expected)):
                return False
        elif key == "$all":
            if not (isinstance(expected, (list, tuple)) and all(_matches(c, item) for c in expected)):
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
    scope: ConflictScope | None,
) -> list[dict]:
    """Current items of ``domain``, plus the proposed items when ``domain`` is the
    one being changed (so a new item can clash with something already present in
    the same domain, e.g. retinoid + peel the same evening)."""
    items: list[dict] = []
    registration = _resolvers.get(domain)
    if scope is not None:
        if registration is None or registration.scoped is None:
            raise ConflictResolverUnavailable(
                f"no scoped conflict resolver is registered for domain {domain!r}"
            )
        items.extend(await registration.scoped(session, scope=scope))
    elif registration is not None and registration.legacy is not None:
        # Transitional write-path compatibility. Scoped readers never enter this
        # arm; it is retired with the remaining legacy ``enforce`` callers.
        items.extend(await registration.legacy(session))
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
    ``supplements_service._parse_slot``); other domains' items simply have no
    slot, which safely excludes them here."""
    return {item.get("timing_slot") for item in items if item.get("timing_slot")}


class ConflictScopeError(RuntimeError):
    """A requested conflict scope cannot safely authorize a read."""


class ConflictSubjectNotFound(ConflictScopeError):
    """The selected health subject does not exist."""


class ConflictLegacyBridgeError(ConflictScopeError):
    """The fully-unowned bridge is not safe for the current identity graph."""


class ConflictUnsupportedDatabaseError(ConflictScopeError):
    """The database cannot provide the lock required by a legacy bridge."""


class ConflictCatalogIntegrityError(ConflictScopeError):
    """A database row claiming curated provenance differs from the catalog."""


class ConflictRawOwnershipError(ConflictScopeError):
    """A normalized fact links to raw provenance outside its subject scope."""


def _domain_value(domain: Domain | str) -> str:
    try:
        return Domain(domain).value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unknown conflict domain: {domain!r}") from exc


async def _acquire_legacy_governance_lock(session: AsyncSession) -> None:
    from vitals.services.identity_service import (
        UnsupportedIdentityDatabaseError,
        acquire_identity_governance_lock,
    )

    try:
        await acquire_identity_governance_lock(session)
    except UnsupportedIdentityDatabaseError as exc:
        raise ConflictUnsupportedDatabaseError(str(exc)) from exc


async def _validate_scope(session: AsyncSession, scope: ConflictScope) -> None:
    from vitals.models.identity import HealthSubject

    if not isinstance(scope, ConflictScope):
        raise TypeError("scope must be a ConflictScope")
    if scope.legacy_bridge is LegacyConflictBridge.FULLY_UNOWNED:
        # The proof and every resolver read must share this transaction-scoped
        # lock. Otherwise a second subject can commit between the count and a
        # later resolver query, causing fully-unowned facts to be adopted after
        # the installation has already become multi-subject.
        await _acquire_legacy_governance_lock(session)
        subject_ids = list(
            await session.scalars(
                select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
            )
        )
        if subject_ids != [scope.subject_id]:
            raise ConflictLegacyBridgeError(
                "fully-unowned conflict reads require exactly one matching "
                "health subject"
            )
        return
    exists = await session.scalar(
        select(HealthSubject.id).where(HealthSubject.id == scope.subject_id)
    )
    if exists is None:
        raise ConflictSubjectNotFound("health subject does not exist")


def raw_payload_scope_conditions(scope: ConflictScope):
    """Return exact-subject and fully-unowned SQL predicates for RawPayload.

    The exact predicate validates every portable ownership root without loading
    raw payload contents. Historical connections may be disabled or retired,
    but an unresolved/pending connection is not established provenance.
    """

    from vitals.enums import IntegrationConnectionStatus
    from vitals.models.identity import HealthSubject
    from vitals.models.raw_payload import RawPayload
    from vitals.models.tenancy import FileAsset, IntegrationConnection

    historical_statuses = tuple(
        status.value
        for status in IntegrationConnectionStatus
        if status is not IntegrationConnectionStatus.PENDING
    )
    owner_user_id = (
        select(HealthSubject.owner_user_id)
        .where(HealthSubject.id == scope.subject_id)
        .scalar_subquery()
    )
    exact = and_(
        RawPayload.id.is_not(None),
        RawPayload.subject_id == scope.subject_id,
        or_(
            RawPayload.actor_user_id.is_(None),
            RawPayload.actor_user_id == owner_user_id,
        ),
        or_(
            RawPayload.integration_connection_id.is_(None),
            exists(
                select(1).where(
                    IntegrationConnection.id
                    == RawPayload.integration_connection_id,
                    IntegrationConnection.subject_id == scope.subject_id,
                    IntegrationConnection.status.in_(historical_statuses),
                )
            ),
        ),
        or_(
            RawPayload.file_asset_id.is_(None),
            exists(
                select(1).where(
                    FileAsset.id == RawPayload.file_asset_id,
                    FileAsset.subject_id == scope.subject_id,
                )
            ),
        ),
    )
    fully_unowned = and_(
        RawPayload.id.is_not(None),
        RawPayload.subject_id.is_(None),
        RawPayload.actor_user_id.is_(None),
        RawPayload.integration_connection_id.is_(None),
        RawPayload.file_asset_id.is_(None),
    )
    return exact, fully_unowned


_CURATED_RULE_FIELDS = (
    "rule_type",
    "domain_a",
    "condition_a",
    "domain_b",
    "condition_b",
    "severity",
    "message",
    "params",
    "category",
    "source",
    "evidence",
)


def _curated_rule_definitions() -> dict[str, dict[str, Any]]:
    from vitals.services.conflict_catalog import load_rule_catalog

    return {entry["code"]: entry for entry in load_rule_catalog()}


def _require_catalog_rule_integrity(
    rows: Sequence[ConflictRule],
    catalog: Mapping[str, Mapping[str, Any]],
) -> None:
    for row in rows:
        if row.subject_id is not None or row.code is None:
            continue
        definition = catalog.get(row.code)
        if definition is None:
            raise ConflictCatalogIntegrityError(
                "unrecognized global conflict rule provenance"
            )
        if any(
            getattr(row, field_name) != definition.get(field_name)
            for field_name in _CURATED_RULE_FIELDS
        ):
            raise ConflictCatalogIntegrityError(
                "global conflict rule differs from the checked-in catalog"
            )


async def load_scoped_rules(
    session: AsyncSession,
    *,
    scope: ConflictScope,
    domain: Domain | str | None = None,
    active_only: bool = True,
) -> Sequence[ConflictRule]:
    """Load global definitions plus custom rules of exactly one subject."""

    await _validate_scope(session, scope)
    return await _load_scoped_rules_unchecked(
        session,
        scope=scope,
        domain=domain,
        active_only=active_only,
    )


async def _load_scoped_rules_unchecked(
    session: AsyncSession,
    *,
    scope: ConflictScope,
    domain: Domain | str | None,
    active_only: bool = True,
) -> Sequence[ConflictRule]:
    catalog = _curated_rule_definitions()
    curated_codes = tuple(catalog)
    # A portable ``code`` value is not itself provenance: only membership in the
    # checked-in catalog can classify an S=NULL definition as global. An
    # unclassified S=NULL row is legacy custom state and is accepted only by the
    # exact-one bridge.
    ownership_scope = or_(
        ConflictRule.subject_id == scope.subject_id,
        and_(
            ConflictRule.subject_id.is_(None),
            ConflictRule.code.in_(curated_codes),
        ),
    )
    if scope.include_legacy_unowned:
        ownership_scope = or_(
            ownership_scope,
            and_(
                ConflictRule.subject_id.is_(None),
                ConflictRule.code.is_(None),
            ),
        )
    filters = [ownership_scope]
    if active_only:
        filters.append(ConflictRule.active.is_(True))
    result = await session.execute(select(ConflictRule).where(*filters))
    rows = result.scalars().all()
    # Authenticate every candidate before trusting mutable DB domain columns.
    # Otherwise a forged catalog row can move itself out of the requested
    # domain and silently disable a checked-in safety definition before the
    # integrity check ever sees it.
    _require_catalog_rule_integrity(rows, catalog)
    if domain is not None:
        domain_value = _domain_value(domain)
        rows = [
            row
            for row in rows
            if row.domain_a == domain_value or row.domain_b == domain_value
        ]
    return rows


async def _evaluate(
    session: AsyncSession,
    domain: str,
    proposed_state: Any = None,
    *,
    include_day_end: bool = False,
    scope: ConflictScope | None,
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

    if scope is None:
        result = await session.execute(
            select(ConflictRule).where(
                ConflictRule.active.is_(True),
                (ConflictRule.domain_a == domain) | (ConflictRule.domain_b == domain),
            )
        )
        rules = result.scalars().all()
    else:
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
            )
        if rule.domain_b not in item_cache:
            item_cache[rule.domain_b] = await _domain_items(
                session,
                rule.domain_b,
                domain,
                proposed_items,
                scope=scope,
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
) -> list[Violation]:
    """Evaluate rules and facts belonging to exactly one health subject."""

    await _validate_scope(session, scope)
    domain_value = _domain_value(domain)
    return await _evaluate(
        session,
        domain_value,
        proposed_state,
        include_day_end=include_day_end,
        scope=scope,
    )


async def resolve_legacy_conflict_scope(
    session: AsyncSession,
    *,
    actor_username: str | None,
    evaluation_date: date_type | None = None,
) -> ConflictScope:
    """Resolve and authenticate the exact-one owner under one governance lock."""

    from vitals.services.legacy_ownership import resolve_legacy_ownership_context

    # Lock before sampling subject cardinality, owner lifecycle, or actor
    # identity. The transaction retains it through the caller's subsequent rule
    # and resolver reads; identity mutations use the same governance lock.
    await _acquire_legacy_governance_lock(session)
    ownership = await resolve_legacy_ownership_context(
        session,
        actor_username=actor_username,
    )
    return ConflictScope(
        subject_id=ownership.subject_id,
        evaluation_date=evaluation_date or today_local(),
        legacy_bridge=LegacyConflictBridge.FULLY_UNOWNED,
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


async def evaluate(
    session: AsyncSession,
    domain: str,
    proposed_state: Any = None,
    *,
    include_day_end: bool = False,
) -> list[Violation]:
    """Deprecated unscoped compatibility for unchanged Stage-1 write paths.

    New read callers must use :func:`evaluate_scoped` or the explicit exact-one
    adapter. This function remains only because ``enforce`` and its domain
    writers are deliberately outside this bounded read slice.
    """

    return await _evaluate(
        session,
        domain,
        proposed_state,
        include_day_end=include_day_end,
        scope=None,
    )


async def enforce(
    session: AsyncSession,
    domain: str,
    proposed_state: Any = None,
    *,
    override: bool = False,
    entity_ref: str = "",
    include_day_end: bool = False,
) -> list[Violation]:
    """Evaluate + apply the override flow.

    Raises :class:`ConflictBlocked` when a ``block`` violation fires without
    ``override``. Otherwise writes an alert row per violation (stamping
    ``override_at`` on overridden blocks) and returns all violations so the caller
    can surface the non-blocking ones. See :func:`evaluate` for ``include_day_end``.
    """
    violations = await evaluate(session, domain, proposed_state, include_day_end=include_day_end)
    blocking = [v for v in violations if v.is_blocking]

    if blocking and not override:
        raise ConflictBlocked(violations)

    for v in violations:
        overridden = v.is_blocking and override
        await alerts_service.raise_alert(
            session,
            domain=domain,
            severity=v.severity,
            message=v.message,
            alert_key=f"conflict:{v.rule_id}",
            entity_ref=entity_ref,
            overridden=overridden,
        )
    return violations


async def enforce_day_end(
    session: AsyncSession, domain: str, *, entity_ref: str = ""
) -> list[Violation]:
    """Like :func:`enforce`, but for ``day_end_only`` rules specifically —
    call once daily (see ``nutrition_service.day_end_job``), never from a live
    save path.

    Unlike ``enforce()``, this also *resolves* the alert for any day_end_only
    rule touching ``domain`` that is **not** currently violated. Plain
    ``enforce()`` only ever raises — it has no notion of a rule "clearing" —
    which is fine for rules re-evaluated on every save (an unrelated later
    save naturally re-raises or leaves it alone), but wrong here: each day's
    check uses a fresh ``entity_ref`` (today's date), so a rule that stops
    matching would otherwise leave yesterday's alert active forever, needing
    a manual dismiss even after the day's numbers are actually fine.
    """
    violations = await evaluate(session, domain, include_day_end=True)
    fired = {v.rule_id: v for v in violations if (v.params or {}).get("day_end_only")}

    result = await session.execute(
        select(ConflictRule).where(
            ConflictRule.active.is_(True),
            (ConflictRule.domain_a == domain) | (ConflictRule.domain_b == domain),
        )
    )
    day_end_rules = [r for r in result.scalars().all() if (r.params or {}).get("day_end_only")]

    for rule in day_end_rules:
        key = f"conflict:{rule.id}"
        v = fired.get(rule.id)
        if v is None:
            # No longer violated — clear whatever entity_ref it was last
            # raised under (could be an earlier day), not just today's.
            await alerts_service.resolve_superseded(session, alert_key=key, keep_entity=None)
            continue
        # Still/newly violated — supersede a stale earlier-day row, then
        # raise (or refresh) today's.
        await alerts_service.resolve_superseded(session, alert_key=key, keep_entity=entity_ref)
        await alerts_service.raise_alert(
            session,
            domain=domain,
            severity=v.severity,
            message=v.message,
            alert_key=key,
            entity_ref=entity_ref,
        )
    return violations


def _is_timing_rule(rule_type: str) -> bool:
    return rule_type == RuleType.TIMING_SEPARATION.value
