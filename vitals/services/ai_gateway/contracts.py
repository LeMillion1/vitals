"""Platform-funded AI control plane, hard quotas, and at-most-once dispatch.

Database rows contain only authorization/provenance/accounting metadata. Prompt,
response, provider secrets, and exception text exist only in short-lived in-memory
objects. Every mutating database function flushes; its caller owns commit.

Lock order is identity governance -> S -> A -> raw provenance -> platform root ->
platform quota -> subject quota -> invocation. No provider await is permitted
until the issuing start-dispatch transaction has committed.
"""

from __future__ import annotations

import uuid
import weakref
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Generic, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
)
from vitals.models.ai import (
    AIInvocation,
)
from vitals.persistence.transactions import (
    TransactionOutcomeError,
    register_root_transaction_outcome,
)

T = TypeVar("T")
MAX_SIGNED_BIGINT = (1 << 63) - 1
MAX_SIGNED_INTEGER = (1 << 31) - 1
RAW_BACKED_PURPOSES = frozenset(
    {
        AIInvocationPurpose.SIGNAL_PARSE,
        AIInvocationPurpose.QUESTION_REPLY,
        AIInvocationPurpose.LAB_DOCUMENT_PARSE,
        AIInvocationPurpose.BODY_SCAN_PARSE,
    }
)
ALLOWED_CREDENTIAL_REFS = frozenset(
    {
        "env:VITALS_OPENROUTER_API_KEY",
        "legacy_env:openrouter",
    }
)
PREPARED_STALE_AFTER = timedelta(minutes=15)
DISPATCHING_STALE_AFTER = timedelta(hours=1)


class AIGatewayError(RuntimeError):
    """Base class for fail-closed gateway operations."""


class AIGatewayAuthorizationError(AIGatewayError):
    """The actor cannot invoke AI for the exact requested subject."""


class AIGatewayConfigurationError(AIGatewayError):
    """The platform root or an exact shared billing period is unavailable."""


class AIQuotaExceededError(AIGatewayError):
    """A hard platform or subject budget cannot cover the reservation."""


class AIQuotaImmutableError(AIGatewayError):
    """A used or overlapping quota period cannot be rewritten."""


class AIInvocationStateError(AIGatewayError):
    """An invocation cannot perform the requested lifecycle transition."""


class AIIdempotencyConflictError(AIGatewayError):
    """An idempotency key was reused with a different immutable call shape."""


class AICapabilityError(AIGatewayError):
    """An opaque dispatch/completion capability is invalid or replayed."""


class AIProviderDispatchError(AIGatewayError):
    """Sanitized provider failure suitable for rethrow after finalization."""

    def __init__(self, error_code: AIInvocationErrorCode):
        self.error_code = error_code
        super().__init__(f"AI provider dispatch failed with {error_code.value}")


def _register_transaction_outcome(
    session: AsyncSession,
    *,
    on_commit,
    on_rollback,
) -> None:
    try:
        register_root_transaction_outcome(
            session,
            on_commit=on_commit,
            on_rollback=on_rollback,
        )
    except TransactionOutcomeError:
        raise AICapabilityError("AI capability requires one active outer transaction") from None


def _validate_period(period_start: date, period_end: date) -> None:
    if (
        not isinstance(period_start, date)
        or isinstance(period_start, datetime)
        or not isinstance(period_end, date)
        or isinstance(period_end, datetime)
        or period_end <= period_start
    ):
        raise ValueError("quota period must be a positive half-open date interval")


def _validate_nonnegative_integer(value: int, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_SIGNED_BIGINT
    ):
        raise ValueError(f"{name} must fit a nonnegative signed bigint")


def _validate_reservation(cost_microunits: int, units: int) -> None:
    _validate_nonnegative_integer(cost_microunits, "reserved_cost_microunits")
    _validate_nonnegative_integer(units, "reserved_units")
    if cost_microunits == 0 and units == 0:
        raise ValueError("an AI reservation must reserve cost or units")


def _clean_string(value: str, name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise ValueError(f"{name} must be at most {max_length} characters")
    return cleaned


def _credential_ref(value: str) -> str:
    reference = _clean_string(value, "credential_ref", 255)
    if reference not in ALLOWED_CREDENTIAL_REFS:
        raise ValueError("credential_ref is not in the reviewed resolver registry")
    return reference


def _validate_aware_utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _as_purpose(value: AIInvocationPurpose | str) -> AIInvocationPurpose:
    try:
        return AIInvocationPurpose(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("unknown AI invocation purpose") from exc


def _as_source(value: AIInvocationSource | str) -> AIInvocationSource:
    try:
        return AIInvocationSource(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("unknown AI invocation source") from exc


def _validate_raw_binding(
    purpose: AIInvocationPurpose,
    raw_payload_id: int | None,
) -> int | None:
    if raw_payload_id is not None and (
        isinstance(raw_payload_id, bool)
        or not isinstance(raw_payload_id, int)
        or not 1 <= raw_payload_id <= MAX_SIGNED_INTEGER
    ):
        raise ValueError("raw_payload_id must fit a positive signed integer")
    if (purpose in RAW_BACKED_PURPOSES) != (raw_payload_id is not None):
        raise ValueError("AI invocation purpose/raw payload provenance is invalid")
    return raw_payload_id


@dataclass(frozen=True, slots=True)
class AIReservationResult:
    """Non-authorizing idempotency result; terminal duplicates cannot dispatch."""

    invocation_id: uuid.UUID
    status: AIInvocationStatus
    created: bool
    dispatchable: bool


@dataclass(frozen=True, slots=True)
class _InvocationKey:
    """Projected non-PHI provenance used before authorization/row locking."""

    invocation_id: uuid.UUID
    subject_id: uuid.UUID
    actor_user_id: uuid.UUID | None
    raw_payload_id: int | None
    platform_integration_connection_id: uuid.UUID
    config_version: int
    purpose: str
    source: str
    model: str
    idempotency_key: str
    quota_period_start: date
    quota_period_end: date
    reserved_cost_microunits: int
    reserved_units: int


@dataclass(frozen=True, slots=True)
class SanitizedAIUsage:
    """Allowlisted provider metadata extracted from an in-memory result."""

    upstream_request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microunits: int = 0

    def __post_init__(self) -> None:
        if self.upstream_request_id is not None:
            object.__setattr__(
                self,
                "upstream_request_id",
                _clean_string(
                    self.upstream_request_id,
                    "upstream_request_id",
                    255,
                ),
            )
        _validate_nonnegative_integer(self.input_tokens, "input_tokens")
        _validate_nonnegative_integer(self.output_tokens, "output_tokens")
        _validate_nonnegative_integer(self.cost_microunits, "cost_microunits")


class AIDispatchRequest:
    """Ephemeral provider input. Its repr/pickle surface never exposes a secret."""

    __slots__ = (
        "__weakref__",
        "_config_version",
        "_credential",
        "_fingerprint",
        "_idempotency_key",
        "_invocation_id",
        "_model",
        "_platform_connection_id",
        "_raw_payload_id",
    )

    def __init__(self, *args, **kwargs):
        del args, kwargs
        raise AICapabilityError("dispatch requests are service-issued only")

    @classmethod
    def _issue(
        cls,
        *,
        invocation_id: uuid.UUID,
        platform_connection_id: uuid.UUID,
        config_version: int,
        model: str,
        idempotency_key: str,
        raw_payload_id: int | None,
        credential: str,
        fingerprint: tuple,
    ) -> "AIDispatchRequest":
        request = object.__new__(cls)
        object.__setattr__(request, "_invocation_id", invocation_id)
        object.__setattr__(request, "_platform_connection_id", platform_connection_id)
        object.__setattr__(request, "_config_version", config_version)
        object.__setattr__(request, "_model", model)
        object.__setattr__(request, "_idempotency_key", idempotency_key)
        object.__setattr__(request, "_raw_payload_id", raw_payload_id)
        object.__setattr__(request, "_credential", credential)
        object.__setattr__(request, "_fingerprint", fingerprint)
        return request

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("AIDispatchRequest is immutable")

    def __repr__(self) -> str:
        return f"<AIDispatchRequest invocation_id={self._invocation_id} redacted>"

    def __reduce__(self):
        raise TypeError("AIDispatchRequest is not pickleable")

    @property
    def invocation_id(self) -> uuid.UUID:
        return self._invocation_id

    @property
    def platform_connection_id(self) -> uuid.UUID:
        return self._platform_connection_id

    @property
    def config_version(self) -> int:
        return self._config_version

    @property
    def model(self) -> str:
        return self._model

    @property
    def idempotency_key(self) -> str:
        return self._idempotency_key

    @property
    def raw_payload_id(self) -> int | None:
        return self._raw_payload_id

    @property
    def credential(self) -> str:
        return self._credential


_LEASE_SEAL = object()


class AIDispatchLease:
    """Opaque one-shot proof of a committed dispatching transition."""

    __slots__ = (
        "__weakref__",
        "_armed",
        "_actor_user_id",
        "_config_version",
        "_consumed",
        "_credential",
        "_fingerprint",
        "_idempotency_key",
        "_invocation_id",
        "_model",
        "_period_end",
        "_period_start",
        "_platform_connection_id",
        "_purpose",
        "_raw_payload_id",
        "_reserved_cost_microunits",
        "_reserved_units",
        "_seal",
        "_session",
        "_source",
        "_subject_id",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise AICapabilityError("dispatch leases are service-issued only")

    @classmethod
    def _issue(
        cls,
        *,
        session: AsyncSession,
        invocation: AIInvocation,
        credential: str,
    ) -> "AIDispatchLease":
        lease = object.__new__(cls)
        object.__setattr__(lease, "_invocation_id", invocation.id)
        object.__setattr__(lease, "_subject_id", invocation.subject_id)
        object.__setattr__(lease, "_actor_user_id", invocation.actor_user_id)
        object.__setattr__(lease, "_raw_payload_id", invocation.raw_payload_id)
        object.__setattr__(
            lease,
            "_platform_connection_id",
            invocation.platform_integration_connection_id,
        )
        object.__setattr__(lease, "_config_version", invocation.config_version)
        object.__setattr__(lease, "_model", invocation.model)
        object.__setattr__(lease, "_purpose", invocation.purpose)
        object.__setattr__(lease, "_source", invocation.source)
        object.__setattr__(lease, "_period_start", invocation.quota_period_start)
        object.__setattr__(lease, "_period_end", invocation.quota_period_end)
        object.__setattr__(lease, "_idempotency_key", invocation.idempotency_key)
        object.__setattr__(
            lease,
            "_reserved_cost_microunits",
            invocation.reserved_cost_microunits,
        )
        object.__setattr__(lease, "_reserved_units", invocation.reserved_units)
        object.__setattr__(lease, "_credential", credential)
        object.__setattr__(lease, "_session", session)
        object.__setattr__(lease, "_armed", False)
        object.__setattr__(lease, "_consumed", False)
        object.__setattr__(lease, "_seal", _LEASE_SEAL)
        object.__setattr__(
            lease,
            "_fingerprint",
            (
                invocation.id,
                invocation.subject_id,
                invocation.actor_user_id,
                invocation.raw_payload_id,
                invocation.purpose,
                invocation.source,
                invocation.model,
                invocation.idempotency_key,
                invocation.reserved_cost_microunits,
                invocation.reserved_units,
                invocation.platform_integration_connection_id,
                invocation.config_version,
                invocation.quota_period_start,
                invocation.quota_period_end,
            ),
        )

        lease_ref = weakref.ref(lease)

        def after_commit() -> None:
            target = lease_ref()
            if target is None:
                return
            object.__setattr__(target, "_armed", True)

        def after_rollback() -> None:
            target = lease_ref()
            if target is None:
                return
            object.__setattr__(target, "_consumed", True)
            object.__setattr__(target, "_credential", None)
            object.__setattr__(target, "_session", None)

        _register_transaction_outcome(
            session,
            on_commit=after_commit,
            on_rollback=after_rollback,
        )
        return lease

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("AIDispatchLease is immutable")

    def __repr__(self) -> str:
        return f"<AIDispatchLease invocation_id={self._invocation_id} redacted>"

    def __reduce__(self):
        raise TypeError("AIDispatchLease is not pickleable")


_COMPLETION_SEAL = object()


class AICompletion(Generic[T]):
    """Opaque one-shot provider outcome; payload is memory-only and repr-hidden."""

    __slots__ = (
        "__weakref__",
        "_consumed",
        "_error_code",
        "_fingerprint",
        "_finalizing",
        "_invocation_id",
        "_payload",
        "_seal",
        "_session",
        "_status",
        "_usage",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise AICapabilityError("AI completions are service-issued only")

    @classmethod
    def _issue(
        cls,
        *,
        invocation_id: uuid.UUID,
        status: AIInvocationStatus,
        error_code: AIInvocationErrorCode | None,
        usage: SanitizedAIUsage,
        payload: T | None,
        fingerprint: tuple,
    ) -> "AICompletion[T]":
        completion = object.__new__(cls)
        object.__setattr__(completion, "_invocation_id", invocation_id)
        object.__setattr__(completion, "_status", status)
        object.__setattr__(completion, "_error_code", error_code)
        object.__setattr__(completion, "_usage", usage)
        object.__setattr__(completion, "_payload", payload)
        object.__setattr__(completion, "_fingerprint", fingerprint)
        object.__setattr__(completion, "_consumed", False)
        object.__setattr__(completion, "_finalizing", False)
        object.__setattr__(completion, "_session", None)
        object.__setattr__(completion, "_seal", _COMPLETION_SEAL)
        return completion

    def _bind_finalization(self, session: AsyncSession) -> None:
        object.__setattr__(self, "_finalizing", True)
        object.__setattr__(self, "_session", session)
        completion_ref = weakref.ref(self)

        def after_commit() -> None:
            target = completion_ref()
            if target is None:
                return
            object.__setattr__(target, "_consumed", True)
            object.__setattr__(target, "_finalizing", False)
            object.__setattr__(target, "_payload", None)
            object.__setattr__(target, "_session", None)

        def after_rollback() -> None:
            target = completion_ref()
            if target is None:
                return
            object.__setattr__(target, "_finalizing", False)
            object.__setattr__(target, "_session", None)

        _register_transaction_outcome(
            session,
            on_commit=after_commit,
            on_rollback=after_rollback,
        )

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("AICompletion is immutable")

    def __repr__(self) -> str:
        return (
            f"<AICompletion invocation_id={self._invocation_id} "
            f"status={self._status.value} redacted>"
        )

    def __reduce__(self):
        raise TypeError("AICompletion is not pickleable")

    @property
    def invocation_id(self) -> uuid.UUID:
        return self._invocation_id

    @property
    def status(self) -> AIInvocationStatus:
        return self._status

    @property
    def error_code(self) -> AIInvocationErrorCode | None:
        return self._error_code

    @property
    def payload(self) -> T | None:
        return self._payload

    def raise_for_provider_failure(self) -> None:
        if self._error_code is not None:
            raise AIProviderDispatchError(self._error_code)
