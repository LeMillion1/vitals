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

from vitals.enums import Domain, RuleType, Severity, UserStatus
from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject, User
from vitals.ownership import WriteIdentity
from vitals.services import alerts_service
from vitals.utils.timeutils import today_local

logger = logging.getLogger(__name__)

# Domain resolvers may attach this internal key to an item so a scoped update
# can replace exactly one current entity rather than evaluating old+new state.
CONFLICT_ENTITY_KEY = "__conflict_entity_key__"


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


@dataclass(frozen=True, slots=True)
class ConflictWriteContext:
    """Identity and date frozen at one subject-aware write boundary."""

    identity: WriteIdentity
    evaluation_date: date_type
    legacy_bridge: LegacyConflictBridge = LegacyConflictBridge.REJECT

    def __post_init__(self) -> None:
        if not isinstance(self.identity, WriteIdentity):
            raise TypeError("identity must be a WriteIdentity")
        if type(self.evaluation_date) is not date_type:
            raise TypeError("evaluation_date must be a date")
        if not isinstance(self.legacy_bridge, LegacyConflictBridge):
            raise TypeError("legacy_bridge must be a LegacyConflictBridge")
        # Reuse the read contract's UUID and bridge validation rather than
        # allowing write and read scopes to drift subtly apart.
        ConflictScope(
            subject_id=self.identity.subject_id,
            evaluation_date=self.evaluation_date,
            legacy_bridge=self.legacy_bridge,
        )

    @property
    def scope(self) -> ConflictScope:
        return ConflictScope(
            subject_id=self.identity.subject_id,
            evaluation_date=self.evaluation_date,
            legacy_bridge=self.legacy_bridge,
        )


class PreparedConflictWrite:
    """Opaque validated capability tied to one exact session transaction.

    Domain services may prepare before taking their own row locks, then call
    :func:`enforce_prepared` later in the same transaction. Binding the token to
    the session, root transaction, optional savepoint, and immutable context
    prevents a proof obtained before a commit/rollback from being reused after
    its locks have been released. Construction is factory-only so callers cannot
    use ``dataclasses.replace`` to substitute a subject or actor after proof.
    """

    __slots__ = (
        "_context",
        "_context_fingerprint",
        "_nested_transaction",
        "_seal",
        "_session",
        "_transaction",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise ConflictPreparedWriteError(
            "prepared conflict writes are issued only by prepare_scoped_write"
        )

    @classmethod
    def _issue(
        cls,
        *,
        context: ConflictWriteContext,
        session: AsyncSession,
        transaction: object,
        nested_transaction: object | None,
    ) -> PreparedConflictWrite:
        prepared = object.__new__(cls)
        object.__setattr__(prepared, "_context", context)
        object.__setattr__(
            prepared,
            "_context_fingerprint",
            _write_context_fingerprint(context),
        )
        object.__setattr__(prepared, "_session", session)
        object.__setattr__(prepared, "_transaction", transaction)
        object.__setattr__(prepared, "_nested_transaction", nested_transaction)
        object.__setattr__(prepared, "_seal", _PREPARED_WRITE_SEAL)
        return prepared

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedConflictWrite is immutable")

    @property
    def context(self) -> ConflictWriteContext:
        return self._context

    @property
    def scope(self) -> ConflictScope:
        return self.context.scope


_PREPARED_WRITE_SEAL = object()


def _write_context_fingerprint(
    context: ConflictWriteContext,
) -> tuple[uuid.UUID, uuid.UUID | None, date_type, LegacyConflictBridge]:
    return (
        context.identity.subject_id,
        context.identity.actor_user_id,
        context.evaluation_date,
        context.legacy_bridge,
    )


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
    def normalized(item: dict) -> dict:
        return {
            key: value
            for key, value in item.items()
            if key != CONFLICT_ENTITY_KEY
        }

    if proposed_state is None:
        return []
    if isinstance(proposed_state, dict):
        return [normalized(proposed_state)]
    return [
        normalized(item)
        for item in proposed_state
        if isinstance(item, dict)
    ]


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
    replace_entity_key: str | None = None,
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
    if domain == changed_domain and replace_entity_key is not None:
        items = [
            item
            for item in items
            if item.get(CONFLICT_ENTITY_KEY) != replace_entity_key
        ]
    # Entity markers are resolver bookkeeping, never part of the custom-rule
    # predicate grammar or an externally supplied proposed-state shape.
    items = [
        {
            key: value
            for key, value in item.items()
            if key != CONFLICT_ENTITY_KEY
        }
        for item in items
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


class ConflictActorNotFound(ConflictScopeError):
    """The write actor does not exist."""


class ConflictActorInactive(ConflictScopeError):
    """The write actor is not active."""


class ConflictActorOwnershipError(ConflictScopeError):
    """A legacy-bridge actor is not the sole subject's current owner."""


class ConflictPreparedWriteError(ConflictScopeError):
    """A prepared write is missing, foreign to the session, or no longer live."""


class ConflictOverrideActorRequired(ConflictScopeError):
    """A conflict override was requested without an active human actor."""


class ConflictWriteRuleError(ConflictScopeError):
    """A firing rule cannot be represented by the typed alert contract."""


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


def _require_write_context(context: ConflictWriteContext) -> None:
    if not isinstance(context, ConflictWriteContext):
        raise TypeError("context must be a ConflictWriteContext")


def _require_typed_domain(domain: Domain) -> None:
    if not isinstance(domain, Domain):
        raise TypeError("domain must be a Domain")


def _alert_bridge(context: ConflictWriteContext) -> alerts_service.LegacyAlertBridge:
    if context.legacy_bridge is LegacyConflictBridge.FULLY_UNOWNED:
        return alerts_service.LegacyAlertBridge.FULLY_UNOWNED
    return alerts_service.LegacyAlertBridge.REJECT


def _health_alert_context(
    context: ConflictWriteContext,
) -> alerts_service.HealthAlertContext:
    return alerts_service.HealthAlertContext(context.identity)


async def prepare_scoped_write(
    session: AsyncSession,
    *,
    context: ConflictWriteContext,
) -> PreparedConflictWrite:
    """Lock and validate the identity roots for one scoped conflict write.

    Identity governance is always taken before subject/user row locks. Besides
    freezing the compatibility bridge's exact-one proof, this prevents a strict
    write from deadlocking against an identity mutation that takes governance
    and user locks before reaching the subject row.
    """

    _require_write_context(context)
    await _acquire_legacy_governance_lock(session)
    with session.no_autoflush:
        if context.legacy_bridge is LegacyConflictBridge.FULLY_UNOWNED:
            subject_ids = list(
                await session.scalars(
                    select(HealthSubject.id)
                    .order_by(HealthSubject.id)
                    .limit(2)
                )
            )
            if subject_ids != [context.identity.subject_id]:
                raise ConflictLegacyBridgeError(
                    "fully-unowned conflict writes require exactly one matching "
                    "health subject"
                )

        subject = await session.scalar(
            select(HealthSubject)
            .where(HealthSubject.id == context.identity.subject_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if subject is None:
            raise ConflictSubjectNotFound("health subject does not exist")

        required_user_ids: set[uuid.UUID] = set()
        if context.identity.actor_user_id is not None:
            required_user_ids.add(context.identity.actor_user_id)
        if context.legacy_bridge is LegacyConflictBridge.FULLY_UNOWNED:
            required_user_ids.add(subject.owner_user_id)

        users = (
            {
                user.id: user
                for user in await session.scalars(
                    select(User)
                    .where(User.id.in_(tuple(required_user_ids)))
                    .order_by(User.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            }
            if required_user_ids
            else {}
        )

        if context.legacy_bridge is LegacyConflictBridge.FULLY_UNOWNED:
            owner = users.get(subject.owner_user_id)
            if owner is None or owner.status != UserStatus.ACTIVE.value:
                raise ConflictLegacyBridgeError(
                    "fully-unowned conflict writes require an active sole-subject owner"
                )

        actor_user_id = context.identity.actor_user_id
        if actor_user_id is not None:
            actor = users.get(actor_user_id)
            if actor is None:
                raise ConflictActorNotFound("actor user does not exist")
            if actor.status != UserStatus.ACTIVE.value:
                raise ConflictActorInactive("actor user is not active")
            if (
                context.legacy_bridge is LegacyConflictBridge.FULLY_UNOWNED
                and actor_user_id != subject.owner_user_id
            ):
                raise ConflictActorOwnershipError(
                    "fully-unowned conflict writes require the owner or system actor"
                )

    transaction = session.sync_session.get_transaction()
    if transaction is None:  # pragma: no cover - every SQLAlchemy query autobegins
        raise ConflictPreparedWriteError("conflict write has no active transaction")
    return PreparedConflictWrite._issue(
        context=context,
        session=session,
        transaction=transaction,
        nested_transaction=session.sync_session.get_nested_transaction(),
    )


def _require_live_prepared_write(
    session: AsyncSession,
    prepared: PreparedConflictWrite,
) -> ConflictWriteContext:
    if not isinstance(prepared, PreparedConflictWrite):
        raise ConflictPreparedWriteError(
            "prepared must be a PreparedConflictWrite"
        )
    try:
        valid_seal = prepared._seal is _PREPARED_WRITE_SEAL
        context = prepared._context
        valid_fingerprint = (
            prepared._context_fingerprint
            == _write_context_fingerprint(context)
        )
        prepared_session = prepared._session
        transaction = prepared._transaction
        nested_transaction = prepared._nested_transaction
    except (AttributeError, TypeError) as exc:
        raise ConflictPreparedWriteError(
            "prepared conflict write is not a valid issued capability"
        ) from exc
    if not valid_seal or not valid_fingerprint:
        raise ConflictPreparedWriteError(
            "prepared conflict write context was not issued by the validator"
        )
    if prepared_session is not session:
        raise ConflictPreparedWriteError(
            "prepared conflict write belongs to another session"
        )
    if session.sync_session.get_transaction() is not transaction:
        raise ConflictPreparedWriteError(
            "prepared conflict write transaction is no longer active"
        )
    if session.sync_session.get_nested_transaction() is not nested_transaction:
        raise ConflictPreparedWriteError(
            "prepared conflict write savepoint is no longer active"
        )
    return context


def require_prepared_identity(
    session: AsyncSession,
    *,
    prepared: PreparedConflictWrite,
    identity: WriteIdentity,
) -> ConflictWriteContext:
    """Validate a capability before a domain service reads its target row.

    Stateful updates often need the locked row to build ``proposed_state``.
    This public guard lets them prove the exact session/transaction/identity
    first, so an invalid token cannot be used to materialize or lock a row from
    another scope before :func:`enforce_prepared` runs.
    """

    if not isinstance(identity, WriteIdentity):
        raise ConflictPreparedWriteError(
            "a prepared conflict write requires an explicit WriteIdentity"
        )
    context = _require_live_prepared_write(session, prepared)
    if context.identity != identity:
        raise ConflictPreparedWriteError(
            "write identity does not match prepared conflict write"
        )
    return context


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
    result = await session.execute(
        select(ConflictRule).where(ownership_scope).order_by(ConflictRule.id)
    )
    rows = result.scalars().all()
    # Authenticate every candidate before trusting mutable DB domain columns.
    # Otherwise a forged catalog row can move itself out of the requested
    # domain and silently disable a checked-in safety definition before the
    # integrity check ever sees it.
    _require_catalog_rule_integrity(rows, catalog)
    # Curated definitions are global, but their activation belongs to the
    # selected health subject.  Import lazily because the activation service
    # reuses ``LegacyConflictBridge`` as part of its public typed contract.
    from vitals.services import conflict_activation_service

    activation_state = await conflict_activation_service.read_activation_state(
        session,
        subject_id=scope.subject_id,
        legacy_bridge=scope.legacy_bridge,
    )
    activation = conflict_activation_service.effective_rule_activation(
        rows,
        activation_state,
    )
    if active_only:
        rows = [row for row in rows if activation[row.id]]
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


async def resolve_legacy_conflict_write_context(
    session: AsyncSession,
    *,
    actor_username: str | None,
    evaluation_date: date_type | None = None,
) -> ConflictWriteContext:
    """Resolve the registration-disabled owner into an explicit write context.

    The governance lock precedes every exact-one, owner-lifecycle, and username
    proof and remains held for the surrounding transaction. A username denotes
    an authenticated owner action; ``None`` denotes a trusted system/job action.
    """

    from vitals.services.legacy_ownership import resolve_legacy_ownership_context

    await _acquire_legacy_governance_lock(session)
    ownership = await resolve_legacy_ownership_context(
        session,
        actor_username=actor_username,
    )
    identity = (
        ownership.system_action()
        if actor_username is None
        else ownership.owner_action()
    )
    return ConflictWriteContext(
        identity=identity,
        evaluation_date=evaluation_date or today_local(),
        legacy_bridge=LegacyConflictBridge.FULLY_UNOWNED,
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
        replace_entity_key=None,
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
        alerts_service._require_entity_ref(entity_ref)
        for violation in violations:
            rule_id = violation.rule_id
            if isinstance(rule_id, bool) or not isinstance(rule_id, int) or rule_id < 1:
                raise ConflictWriteRuleError(
                    "a firing conflict rule has no persisted positive id"
                )
            alert_key = f"conflict:{rule_id}"
            alerts_service._require_key(alert_key)
            alerts_service._require_message(violation.message)
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
    except alerts_service.AlertValidationError as exc:
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
        raise ConflictOverrideActorRequired(
            "conflict override requires an active human actor"
        )

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
        await alerts_service.raise_scoped_alert(
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
        violation
        for violation in violations
        if (violation.params or {}).get("day_end_only")
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
    day_end_rules = [
        rule for rule in rules if (rule.params or {}).get("day_end_only")
    ]
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
            await alerts_service.resolve_scoped_superseded(
                session,
                context=alert_context,
                alert_key=alert_key,
                keep_entity=None,
                legacy_bridge=alert_bridge,
            )
            continue
        planned_key, severity = plan[rule.id]
        await alerts_service.resolve_scoped_superseded(
            session,
            context=alert_context,
            alert_key=planned_key,
            keep_entity=entity_ref,
            legacy_bridge=alert_bridge,
        )
        await alerts_service.raise_scoped_alert(
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
