"""Typed scopes, capabilities, violations, and errors for conflict evaluation."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date as date_type
from enum import StrEnum
from typing import Any, Optional, Protocol, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Severity
from vitals.ownership import WriteIdentity

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


class LegacyUnownedProbe(Protocol):
    """Answer whether one domain still holds a row the bridge would adopt.

    Deliberately not "does this domain have unowned rows". A probe has to mirror
    its resolver's widening exactly, because the two are read together: the
    engine skips the sole-subject proof when every probe says no, and if a probe
    is looser than the widening it guards, a row would be adopted with nobody
    having decided whose it is. Looser the other way is merely a missed
    opportunity, so when in doubt a probe says yes.

    That is why probes live beside the predicates they mirror rather than in one
    table here — a widening that changes has its probe in the same diff.
    """

    async def __call__(self, session: AsyncSession) -> bool: ...


@dataclass(frozen=True, slots=True)
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
