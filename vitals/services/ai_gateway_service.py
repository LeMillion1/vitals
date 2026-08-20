"""Platform-funded AI control plane, hard quotas, and at-most-once dispatch.

Database rows contain only authorization/provenance/accounting metadata. Prompt,
response, provider secrets, and exception text exist only in short-lived in-memory
objects. Every mutating database function flushes; its caller owns commit.

Lock order is identity governance -> S -> A -> platform root -> platform quota ->
subject quota -> invocation. No provider await is permitted until the issuing
start-dispatch transaction has committed.
"""
from __future__ import annotations

import asyncio
import uuid
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Generic, TypeVar

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.ai import (
    AIInvocation,
    AIPlatformQuotaPeriod,
    AISubjectQuotaPeriod,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import PlatformIntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.platform_admin_service import (
    PreparedPlatformAdmin,
    require_prepared_platform_admin,
)
from vitals.utils.timeutils import now_utc

T = TypeVar("T")
MAX_SIGNED_BIGINT = (1 << 63) - 1
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
        "_idempotency_key",
        "_invocation_id",
        "_model",
        "_platform_connection_id",
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
        credential: str,
    ) -> "AIDispatchRequest":
        request = object.__new__(cls)
        object.__setattr__(request, "_invocation_id", invocation_id)
        object.__setattr__(request, "_platform_connection_id", platform_connection_id)
        object.__setattr__(request, "_config_version", config_version)
        object.__setattr__(request, "_model", model)
        object.__setattr__(request, "_idempotency_key", idempotency_key)
        object.__setattr__(request, "_credential", credential)
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
    def credential(self) -> str:
        return self._credential


_LEASE_SEAL = object()


class AIDispatchLease:
    """Opaque one-shot proof of a committed dispatching transition."""

    __slots__ = (
        "__weakref__",
        "_armed",
        "_actor_user_id",
        "_commit_listener",
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
        "_reserved_cost_microunits",
        "_reserved_units",
        "_rollback_listener",
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

        # Start-dispatch rejects nested transactions, so these callbacks identify
        # the single issuing outer boundary unambiguously.
        sync_session = session.sync_session
        lease_ref = weakref.ref(lease)

        def after_commit(_session) -> None:
            target = lease_ref()
            if target is None:
                return
            object.__setattr__(target, "_armed", True)
            rollback_listener = target._rollback_listener
            if rollback_listener is not None:
                event.remove(sync_session, "after_rollback", rollback_listener)
            object.__setattr__(target, "_commit_listener", None)
            object.__setattr__(target, "_rollback_listener", None)

        def after_rollback(_session) -> None:
            target = lease_ref()
            if target is None:
                return
            object.__setattr__(target, "_consumed", True)
            object.__setattr__(target, "_credential", None)
            object.__setattr__(target, "_session", None)
            commit_listener = target._commit_listener
            if commit_listener is not None:
                event.remove(sync_session, "after_commit", commit_listener)
            object.__setattr__(target, "_commit_listener", None)
            object.__setattr__(target, "_rollback_listener", None)

        object.__setattr__(lease, "_commit_listener", after_commit)
        object.__setattr__(lease, "_rollback_listener", after_rollback)
        event.listen(
            sync_session,
            "after_commit",
            after_commit,
            once=True,
        )
        event.listen(
            sync_session,
            "after_rollback",
            after_rollback,
            once=True,
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
        "_commit_listener",
        "_consumed",
        "_error_code",
        "_fingerprint",
        "_finalizing",
        "_invocation_id",
        "_payload",
        "_rollback_listener",
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
        object.__setattr__(completion, "_commit_listener", None)
        object.__setattr__(completion, "_rollback_listener", None)
        object.__setattr__(completion, "_seal", _COMPLETION_SEAL)
        return completion

    def _bind_finalization(self, session: AsyncSession) -> None:
        sync_session = session.sync_session
        object.__setattr__(self, "_finalizing", True)
        object.__setattr__(self, "_session", session)
        completion_ref = weakref.ref(self)

        def after_commit(_session) -> None:
            target = completion_ref()
            if target is None:
                return
            object.__setattr__(target, "_consumed", True)
            object.__setattr__(target, "_finalizing", False)
            object.__setattr__(target, "_payload", None)
            object.__setattr__(target, "_session", None)
            rollback_listener = target._rollback_listener
            if rollback_listener is not None:
                event.remove(sync_session, "after_rollback", rollback_listener)
            object.__setattr__(target, "_commit_listener", None)
            object.__setattr__(target, "_rollback_listener", None)

        def after_rollback(_session) -> None:
            target = completion_ref()
            if target is None:
                return
            object.__setattr__(target, "_finalizing", False)
            object.__setattr__(target, "_session", None)
            commit_listener = target._commit_listener
            if commit_listener is not None:
                event.remove(sync_session, "after_commit", commit_listener)
            object.__setattr__(target, "_commit_listener", None)
            object.__setattr__(target, "_rollback_listener", None)

        object.__setattr__(self, "_commit_listener", after_commit)
        object.__setattr__(self, "_rollback_listener", after_rollback)
        event.listen(sync_session, "after_commit", after_commit, once=True)
        event.listen(sync_session, "after_rollback", after_rollback, once=True)

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


async def _lock_subject_authority(
    session: AsyncSession,
    identity: WriteIdentity,
) -> HealthSubject:
    if not isinstance(identity, WriteIdentity):
        raise TypeError("identity must be a WriteIdentity")
    await acquire_identity_governance_lock(session)
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == identity.subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if subject is None:
        raise AIGatewayAuthorizationError("AI subject authorization failed")
    if identity.actor_user_id is None:
        return subject
    actor = await session.scalar(
        select(User)
        .where(User.id == identity.actor_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        actor is None
        or actor.status != UserStatus.ACTIVE.value
        or subject.owner_user_id != actor.id
    ):
        raise AIGatewayAuthorizationError("AI subject authorization failed")
    return subject


async def _lock_current_root(session: AsyncSession) -> PlatformIntegrationConnection:
    root = await session.scalar(
        select(PlatformIntegrationConnection)
        .where(
            PlatformIntegrationConnection.provider
            == IntegrationProvider.OPENROUTER.value,
            PlatformIntegrationConnection.connection_type
            == IntegrationConnectionType.AI_GATEWAY.value,
            PlatformIntegrationConnection.status
            == IntegrationConnectionStatus.ACTIVE.value,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if root is None:
        raise AIGatewayConfigurationError("active platform AI gateway is required")
    return root


async def _lock_exact_root(
    session: AsyncSession,
    invocation: AIInvocation | _InvocationKey,
    *,
    require_active: bool,
) -> PlatformIntegrationConnection:
    root = await session.scalar(
        select(PlatformIntegrationConnection)
        .where(
            PlatformIntegrationConnection.id
            == invocation.platform_integration_connection_id
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        root is None
        or root.config_version != invocation.config_version
        or (require_active and root.status != IntegrationConnectionStatus.ACTIVE.value)
    ):
        raise AIGatewayConfigurationError(
            "exact active platform AI gateway provenance is required"
        )
    return root


async def _lock_quota_rows(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    period_start: date,
    period_end: date,
) -> tuple[AIPlatformQuotaPeriod, AISubjectQuotaPeriod]:
    platform = await session.scalar(
        select(AIPlatformQuotaPeriod)
        .where(
            AIPlatformQuotaPeriod.period_start == period_start,
            AIPlatformQuotaPeriod.period_end == period_end,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if platform is None:
        raise AIGatewayConfigurationError(
            "exact platform AI quota period is required"
        )
    subject = await session.scalar(
        select(AISubjectQuotaPeriod)
        .where(
            AISubjectQuotaPeriod.subject_id == subject_id,
            AISubjectQuotaPeriod.period_start == period_start,
            AISubjectQuotaPeriod.period_end == period_end,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if subject is None:
        raise AIGatewayConfigurationError(
            "exact subject AI quota period is required"
        )
    return platform, subject


async def _lock_current_quota_rows(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    billing_date: date,
) -> tuple[AIPlatformQuotaPeriod, AISubjectQuotaPeriod]:
    platform_rows = list(
        await session.scalars(
            select(AIPlatformQuotaPeriod)
            .where(
                AIPlatformQuotaPeriod.period_start <= billing_date,
                AIPlatformQuotaPeriod.period_end > billing_date,
            )
            .order_by(
                AIPlatformQuotaPeriod.period_start,
                AIPlatformQuotaPeriod.period_end,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(platform_rows) != 1:
        raise AIGatewayConfigurationError(
            "current UTC date requires exactly one platform AI quota period"
        )
    platform = platform_rows[0]
    subject_rows = list(
        await session.scalars(
            select(AISubjectQuotaPeriod)
            .where(
                AISubjectQuotaPeriod.subject_id == subject_id,
                AISubjectQuotaPeriod.period_start <= billing_date,
                AISubjectQuotaPeriod.period_end > billing_date,
            )
            .order_by(
                AISubjectQuotaPeriod.period_start,
                AISubjectQuotaPeriod.period_end,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(subject_rows) != 1:
        raise AIGatewayConfigurationError(
            "current UTC date requires exactly one subject AI quota period"
        )
    subject = subject_rows[0]
    if (
        subject.period_start != platform.period_start
        or subject.period_end != platform.period_end
    ):
        raise AIGatewayConfigurationError(
            "subject AI quota period must align to the platform period"
        )
    return platform, subject


def _has_capacity(row, *, cost_microunits: int, units: int) -> bool:
    used_cost = row.reserved_cost_microunits + row.charged_cost_microunits
    used_units = row.reserved_units + row.charged_units
    if (
        used_cost > MAX_SIGNED_BIGINT - cost_microunits
        or used_units > MAX_SIGNED_BIGINT - units
    ):
        return False
    return (
        used_cost + cost_microunits <= row.cost_limit_microunits
        and used_units + units <= row.unit_limit
    )


async def create_gateway(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
    external_account_discriminator: str,
    credential_ref: str,
) -> PlatformIntegrationConnection:
    """Create the first active root under platform-superadmin control only."""

    actor_id = require_prepared_platform_admin(session, prepared)
    discriminator = _clean_string(
        external_account_discriminator, "external_account_discriminator", 128
    )
    resolver_ref = _credential_ref(credential_ref)
    current = await session.scalar(
        select(PlatformIntegrationConnection)
        .where(PlatformIntegrationConnection.status != "retired")
        .with_for_update()
    )
    if current is not None:
        raise AIGatewayConfigurationError("a current platform AI gateway exists")
    root = PlatformIntegrationConnection(
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=discriminator,
        credential_ref=resolver_ref,
        status=IntegrationConnectionStatus.ACTIVE.value,
        config_version=1,
        configured_by_user_id=actor_id,
    )
    session.add(root)
    await session.flush()
    return root


async def rotate_gateway(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
    external_account_discriminator: str,
    credential_ref: str,
) -> PlatformIntegrationConnection:
    """Atomically retire the immutable current root and insert its replacement."""

    actor_id = require_prepared_platform_admin(session, prepared)
    discriminator = _clean_string(
        external_account_discriminator, "external_account_discriminator", 128
    )
    resolver_ref = _credential_ref(credential_ref)
    current = await session.scalar(
        select(PlatformIntegrationConnection)
        .where(PlatformIntegrationConnection.status != "retired")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current is None:
        raise AIGatewayConfigurationError("a current platform AI gateway is required")
    current.status = IntegrationConnectionStatus.RETIRED.value
    current.retired_at = now_utc()
    await session.flush()
    replacement = PlatformIntegrationConnection(
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=discriminator,
        credential_ref=resolver_ref,
        status=IntegrationConnectionStatus.ACTIVE.value,
        config_version=current.config_version + 1,
        configured_by_user_id=actor_id,
    )
    session.add(replacement)
    await session.flush()
    return replacement


async def disable_gateway(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
) -> PlatformIntegrationConnection:
    """Disable fresh dispatch without changing immutable root identity."""

    require_prepared_platform_admin(session, prepared)
    current = await session.scalar(
        select(PlatformIntegrationConnection)
        .where(PlatformIntegrationConnection.status != "retired")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current is None:
        raise AIGatewayConfigurationError("a current platform AI gateway is required")
    current.status = IntegrationConnectionStatus.DISABLED.value
    await session.flush()
    return current


async def _ensure_nonoverlapping_period(
    session: AsyncSession,
    model,
    *,
    period_start: date,
    period_end: date,
    subject_id: uuid.UUID | None = None,
):
    query = select(model).where(
        model.period_start < period_end,
        model.period_end > period_start,
    )
    if subject_id is not None:
        query = query.where(model.subject_id == subject_id)
    rows = list(await session.scalars(query.with_for_update()))
    for row in rows:
        if row.period_start != period_start or row.period_end != period_end:
            raise AIQuotaImmutableError("AI quota periods must not overlap")
    return rows[0] if rows else None


async def _quota_period_is_used(
    session: AsyncSession,
    row: AIPlatformQuotaPeriod | AISubjectQuotaPeriod,
) -> bool:
    if any(
        (
            row.reserved_cost_microunits,
            row.charged_cost_microunits,
            row.reserved_units,
            row.charged_units,
        )
    ):
        return True
    query = select(AIInvocation.id).where(
        AIInvocation.quota_period_start == row.period_start,
        AIInvocation.quota_period_end == row.period_end,
    )
    if isinstance(row, AISubjectQuotaPeriod):
        query = query.where(AIInvocation.subject_id == row.subject_id)
    return await session.scalar(query.limit(1)) is not None


async def configure_platform_quota_period(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
    period_start: date,
    period_end: date,
    cost_limit_microunits: int,
    unit_limit: int,
) -> AIPlatformQuotaPeriod:
    """Configure numeric platform capacity without granting subject access."""

    actor_id = require_prepared_platform_admin(session, prepared)
    _validate_period(period_start, period_end)
    _validate_nonnegative_integer(cost_limit_microunits, "cost_limit_microunits")
    _validate_nonnegative_integer(unit_limit, "unit_limit")
    row = await _ensure_nonoverlapping_period(
        session,
        AIPlatformQuotaPeriod,
        period_start=period_start,
        period_end=period_end,
    )
    if row is None:
        row = AIPlatformQuotaPeriod(
            period_start=period_start,
            period_end=period_end,
            cost_limit_microunits=cost_limit_microunits,
            unit_limit=unit_limit,
            configured_by_user_id=actor_id,
        )
        session.add(row)
    elif (
        row.cost_limit_microunits != cost_limit_microunits
        or row.unit_limit != unit_limit
    ):
        if await _quota_period_is_used(session, row):
            raise AIQuotaImmutableError("a used AI quota period is immutable")
        row.cost_limit_microunits = cost_limit_microunits
        row.unit_limit = unit_limit
        row.configured_by_user_id = actor_id
    await session.flush()
    return row


async def configure_subject_quota_period(
    session: AsyncSession,
    *,
    prepared: PreparedPlatformAdmin,
    subject_id: uuid.UUID,
    period_start: date,
    period_end: date,
    cost_limit_microunits: int,
    unit_limit: int,
) -> AISubjectQuotaPeriod:
    """Configure capacity by opaque S only; no subject profile is returned."""

    actor_id = require_prepared_platform_admin(session, prepared)
    if not isinstance(subject_id, uuid.UUID):
        raise TypeError("subject_id must be a UUID")
    _validate_period(period_start, period_end)
    _validate_nonnegative_integer(cost_limit_microunits, "cost_limit_microunits")
    _validate_nonnegative_integer(unit_limit, "unit_limit")
    subject_exists = await session.scalar(
        select(HealthSubject.id)
        .where(HealthSubject.id == subject_id)
        .with_for_update()
    )
    if subject_exists is None:
        raise AIGatewayConfigurationError("quota subject does not exist")
    platform_period = await session.scalar(
        select(AIPlatformQuotaPeriod)
        .where(
            AIPlatformQuotaPeriod.period_start == period_start,
            AIPlatformQuotaPeriod.period_end == period_end,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if platform_period is None:
        raise AIGatewayConfigurationError(
            "subject AI quota period must align to an existing platform period"
        )
    row = await _ensure_nonoverlapping_period(
        session,
        AISubjectQuotaPeriod,
        subject_id=subject_id,
        period_start=period_start,
        period_end=period_end,
    )
    if row is None:
        row = AISubjectQuotaPeriod(
            subject_id=subject_id,
            period_start=period_start,
            period_end=period_end,
            cost_limit_microunits=cost_limit_microunits,
            unit_limit=unit_limit,
            configured_by_user_id=actor_id,
        )
        session.add(row)
    elif (
        row.cost_limit_microunits != cost_limit_microunits
        or row.unit_limit != unit_limit
    ):
        if await _quota_period_is_used(session, row):
            raise AIQuotaImmutableError("a used AI quota period is immutable")
        row.cost_limit_microunits = cost_limit_microunits
        row.unit_limit = unit_limit
        row.configured_by_user_id = actor_id
    await session.flush()
    return row


async def reserve_ai_invocation(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    purpose: AIInvocationPurpose | str,
    source: AIInvocationSource | str,
    model: str,
    idempotency_key: str,
    reserved_cost_microunits: int,
    reserved_units: int,
) -> AIReservationResult:
    """Authorize exact S and reserve both hard ledgers in one short transaction."""

    _validate_reservation(reserved_cost_microunits, reserved_units)
    purpose_value = _as_purpose(purpose)
    source_value = _as_source(source)
    model_value = _clean_string(model, "model", 128)
    key_value = _clean_string(idempotency_key, "idempotency_key", 128)
    if not isinstance(identity, WriteIdentity):
        raise TypeError("identity must be a WriteIdentity")
    if (
        source_value is AIInvocationSource.SCHEDULER
        and identity.actor_user_id is not None
    ) or (
        source_value is not AIInvocationSource.SCHEDULER
        and identity.actor_user_id is None
    ):
        raise AIGatewayAuthorizationError(
            "AI invocation source does not match actor provenance"
        )
    await _lock_subject_authority(session, identity)
    root = await _lock_current_root(session)
    billing_date = now_utc().date()
    platform_quota, subject_quota = await _lock_current_quota_rows(
        session,
        subject_id=identity.subject_id,
        billing_date=billing_date,
    )
    existing = await session.scalar(
        select(AIInvocation)
        .where(
            AIInvocation.subject_id == identity.subject_id,
            AIInvocation.purpose == purpose_value.value,
            AIInvocation.idempotency_key == key_value,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        expected_fingerprint = (
            identity.subject_id,
            identity.actor_user_id,
            purpose_value.value,
            source_value.value,
            model_value,
            key_value,
            reserved_cost_microunits,
            reserved_units,
            root.id,
            root.config_version,
            platform_quota.period_start,
            platform_quota.period_end,
        )
        actual_fingerprint = (
            existing.subject_id,
            existing.actor_user_id,
            existing.purpose,
            existing.source,
            existing.model,
            existing.idempotency_key,
            existing.reserved_cost_microunits,
            existing.reserved_units,
            existing.platform_integration_connection_id,
            existing.config_version,
            existing.quota_period_start,
            existing.quota_period_end,
        )
        if actual_fingerprint != expected_fingerprint:
            raise AIIdempotencyConflictError(
                "AI idempotency key is bound to a different call fingerprint"
            )
        status = AIInvocationStatus(existing.status)
        return AIReservationResult(
            invocation_id=existing.id,
            status=status,
            created=False,
            dispatchable=status is AIInvocationStatus.PREPARED,
        )
    for quota in (platform_quota, subject_quota):
        if not _has_capacity(
            quota,
            cost_microunits=reserved_cost_microunits,
            units=reserved_units,
        ):
            raise AIQuotaExceededError("AI quota cannot cover the reservation")
    invocation = AIInvocation(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        platform_integration_connection_id=root.id,
        purpose=purpose_value.value,
        source=source_value.value,
        model=model_value,
        config_version=root.config_version,
        idempotency_key=key_value,
        quota_period_start=platform_quota.period_start,
        quota_period_end=platform_quota.period_end,
        reserved_cost_microunits=reserved_cost_microunits,
        reserved_units=reserved_units,
        charged_cost_microunits=0,
        charged_units=0,
        status=AIInvocationStatus.PREPARED.value,
    )
    for quota in (platform_quota, subject_quota):
        quota.reserved_cost_microunits += reserved_cost_microunits
        quota.reserved_units += reserved_units
    session.add(invocation)
    await session.flush()
    return AIReservationResult(
        invocation_id=invocation.id,
        status=AIInvocationStatus.PREPARED,
        created=True,
        dispatchable=True,
    )


async def _invocation_key(
    session: AsyncSession, invocation_id: uuid.UUID
) -> _InvocationKey:
    if not isinstance(invocation_id, uuid.UUID):
        raise TypeError("invocation_id must be a UUID")
    with session.no_autoflush:
        row = (
            await session.execute(
                select(
                    AIInvocation.id,
                    AIInvocation.subject_id,
                    AIInvocation.actor_user_id,
                    AIInvocation.platform_integration_connection_id,
                    AIInvocation.config_version,
                    AIInvocation.purpose,
                    AIInvocation.source,
                    AIInvocation.model,
                    AIInvocation.idempotency_key,
                    AIInvocation.quota_period_start,
                    AIInvocation.quota_period_end,
                    AIInvocation.reserved_cost_microunits,
                    AIInvocation.reserved_units,
                ).where(AIInvocation.id == invocation_id)
            )
        ).one_or_none()
    if row is None:
        raise AIInvocationStateError("AI invocation does not exist")
    return _InvocationKey(*row)


async def start_ai_dispatch(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    invocation_id: uuid.UUID,
    credential_resolver: Callable[[str], str | None],
) -> AIDispatchLease:
    """Freshly authorize and charge once, resolving a local secret in memory."""

    if session.in_nested_transaction():
        raise AICapabilityError("start dispatch requires an outer transaction")
    if not callable(credential_resolver):
        raise TypeError("credential_resolver must be synchronous and callable")
    snapshot = await _invocation_key(session, invocation_id)
    if (
        snapshot.subject_id != identity.subject_id
        or snapshot.actor_user_id != identity.actor_user_id
    ):
        raise AIGatewayAuthorizationError("AI subject authorization failed")
    await _lock_subject_authority(session, identity)
    root = await _lock_exact_root(session, snapshot, require_active=True)
    billing_date = now_utc().date()
    if not (
        snapshot.quota_period_start
        <= billing_date
        < snapshot.quota_period_end
    ):
        raise AIGatewayConfigurationError(
            "reserved AI quota period does not contain the current UTC date"
        )
    platform_quota, subject_quota = await _lock_quota_rows(
        session,
        subject_id=snapshot.subject_id,
        period_start=snapshot.quota_period_start,
        period_end=snapshot.quota_period_end,
    )
    invocation = await session.scalar(
        select(AIInvocation)
        .where(AIInvocation.id == invocation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invocation is None or invocation.status != AIInvocationStatus.PREPARED.value:
        raise AIInvocationStateError("AI invocation cannot obtain another lease")
    credential = credential_resolver(root.credential_ref)
    if credential is None or not isinstance(credential, str) or not credential.strip():
        raise AIGatewayConfigurationError("platform AI credential is unavailable")
    for quota in (platform_quota, subject_quota):
        if (
            quota.reserved_cost_microunits
            < invocation.reserved_cost_microunits
            or quota.reserved_units < invocation.reserved_units
        ):
            raise AIInvocationStateError("AI reservation accounting is inconsistent")
        quota.reserved_cost_microunits -= invocation.reserved_cost_microunits
        quota.reserved_units -= invocation.reserved_units
        quota.charged_cost_microunits += invocation.reserved_cost_microunits
        quota.charged_units += invocation.reserved_units
    invocation.charged_cost_microunits = invocation.reserved_cost_microunits
    invocation.charged_units = invocation.reserved_units
    invocation.status = AIInvocationStatus.DISPATCHING.value
    invocation.started_at = now_utc()
    await session.flush()
    return AIDispatchLease._issue(
        session=session,
        invocation=invocation,
        credential=credential.strip(),
    )


async def dispatch_ai(
    lease: AIDispatchLease,
    *,
    provider_call: Callable[[AIDispatchRequest], Awaitable[T]],
    usage_extractor: Callable[[T], SanitizedAIUsage],
) -> AICompletion[T]:
    """Consume one committed lease with no active issuing DB transaction."""

    if (
        not isinstance(lease, AIDispatchLease)
        or lease._seal is not _LEASE_SEAL
        or not lease._armed
        or lease._consumed
        or lease._session is None
        or lease._credential is None
        or lease._fingerprint
        != (
            lease._invocation_id,
            lease._subject_id,
            lease._actor_user_id,
            lease._purpose,
            lease._source,
            lease._model,
            lease._idempotency_key,
            lease._reserved_cost_microunits,
            lease._reserved_units,
            lease._platform_connection_id,
            lease._config_version,
            lease._period_start,
            lease._period_end,
        )
    ):
        raise AICapabilityError("dispatch lease is stale, uncommitted, or consumed")
    issuing_session = lease._session
    if issuing_session.in_transaction():
        raise AICapabilityError("provider call cannot span a database transaction")
    if not callable(provider_call) or not callable(usage_extractor):
        raise TypeError("provider_call and usage_extractor must be callable")
    credential = lease._credential
    object.__setattr__(lease, "_consumed", True)
    object.__setattr__(lease, "_credential", None)
    object.__setattr__(lease, "_session", None)
    request = AIDispatchRequest._issue(
        invocation_id=lease._invocation_id,
        platform_connection_id=lease._platform_connection_id,
        config_version=lease._config_version,
        model=lease._model,
        idempotency_key=lease._idempotency_key,
        credential=credential,
    )
    try:
        try:
            result = await provider_call(request)
        finally:
            object.__setattr__(request, "_credential", None)
    except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
        return AICompletion._issue(
            invocation_id=lease._invocation_id,
            status=AIInvocationStatus.AMBIGUOUS,
            error_code=AIInvocationErrorCode.TIMEOUT,
            usage=SanitizedAIUsage(),
            payload=None,
            fingerprint=lease._fingerprint,
        )
    except Exception:
        return AICompletion._issue(
            invocation_id=lease._invocation_id,
            status=AIInvocationStatus.AMBIGUOUS,
            error_code=AIInvocationErrorCode.PROVIDER_UNAVAILABLE,
            usage=SanitizedAIUsage(),
            payload=None,
            fingerprint=lease._fingerprint,
        )
    try:
        usage = usage_extractor(result)
        if not isinstance(usage, SanitizedAIUsage):
            raise TypeError
        if (
            usage.cost_microunits > lease._reserved_cost_microunits
            or usage.input_tokens + usage.output_tokens > lease._reserved_units
        ):
            raise ValueError
    except Exception:
        return AICompletion._issue(
            invocation_id=lease._invocation_id,
            status=AIInvocationStatus.FAILED,
            error_code=AIInvocationErrorCode.INVALID_RESPONSE,
            usage=SanitizedAIUsage(),
            payload=None,
            fingerprint=lease._fingerprint,
        )
    return AICompletion._issue(
        invocation_id=lease._invocation_id,
        status=AIInvocationStatus.SUCCEEDED,
        error_code=None,
        usage=usage,
        payload=result,
        fingerprint=lease._fingerprint,
    )


async def finalize_ai_invocation(
    session: AsyncSession,
    *,
    completion: AICompletion[T],
) -> AIInvocation:
    """Persist one sanitized terminal result in a fresh accounting transaction."""

    if (
        not isinstance(completion, AICompletion)
        or completion._seal is not _COMPLETION_SEAL
        or completion._consumed
        or completion._finalizing
    ):
        raise AICapabilityError("AI completion is invalid or already consumed")
    if session.in_nested_transaction():
        raise AICapabilityError("AI finalization requires an outer transaction")
    await acquire_identity_governance_lock(session)
    snapshot = await _invocation_key(session, completion._invocation_id)
    if completion._fingerprint != (
        snapshot.invocation_id,
        snapshot.subject_id,
        snapshot.actor_user_id,
        snapshot.purpose,
        snapshot.source,
        snapshot.model,
        snapshot.idempotency_key,
        snapshot.reserved_cost_microunits,
        snapshot.reserved_units,
        snapshot.platform_integration_connection_id,
        snapshot.config_version,
        snapshot.quota_period_start,
        snapshot.quota_period_end,
    ):
        raise AICapabilityError("AI completion provenance does not match invocation")
    await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == snapshot.subject_id)
        .with_for_update()
    )
    await _lock_exact_root(session, snapshot, require_active=False)
    await _lock_quota_rows(
        session,
        subject_id=snapshot.subject_id,
        period_start=snapshot.quota_period_start,
        period_end=snapshot.quota_period_end,
    )
    invocation = await session.scalar(
        select(AIInvocation)
        .where(AIInvocation.id == completion._invocation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invocation is None or invocation.status != AIInvocationStatus.DISPATCHING.value:
        raise AIInvocationStateError("AI invocation cannot be finalized again")
    usage = completion._usage
    invocation.status = completion._status.value
    invocation.upstream_request_id = usage.upstream_request_id
    invocation.input_tokens = usage.input_tokens
    invocation.output_tokens = usage.output_tokens
    invocation.cost_microunits = usage.cost_microunits
    invocation.error_code = (
        completion._error_code.value if completion._error_code is not None else None
    )
    invocation.finished_at = now_utc()
    await session.flush()
    completion._bind_finalization(session)
    return invocation


async def cancel_reserved_ai_invocation(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    invocation_id: uuid.UUID,
    error_code: AIInvocationErrorCode = AIInvocationErrorCode.CANCELLED_BY_POLICY,
) -> AIInvocation:
    """Release a reservation only before paid dispatch has started."""

    if not isinstance(error_code, AIInvocationErrorCode):
        raise TypeError("error_code must be an AIInvocationErrorCode")
    snapshot = await _invocation_key(session, invocation_id)
    if (
        snapshot.subject_id != identity.subject_id
        or snapshot.actor_user_id != identity.actor_user_id
    ):
        raise AIGatewayAuthorizationError("AI subject authorization failed")
    await _lock_subject_authority(session, identity)
    await _lock_exact_root(session, snapshot, require_active=False)
    platform_quota, subject_quota = await _lock_quota_rows(
        session,
        subject_id=snapshot.subject_id,
        period_start=snapshot.quota_period_start,
        period_end=snapshot.quota_period_end,
    )
    invocation = await session.scalar(
        select(AIInvocation)
        .where(AIInvocation.id == invocation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invocation is None or invocation.status != AIInvocationStatus.PREPARED.value:
        raise AIInvocationStateError("only a prepared invocation can be cancelled")
    for quota in (platform_quota, subject_quota):
        if (
            quota.reserved_cost_microunits
            < invocation.reserved_cost_microunits
            or quota.reserved_units < invocation.reserved_units
        ):
            raise AIInvocationStateError("AI reservation accounting is inconsistent")
        quota.reserved_cost_microunits -= invocation.reserved_cost_microunits
        quota.reserved_units -= invocation.reserved_units
    invocation.status = AIInvocationStatus.CANCELLED.value
    invocation.error_code = error_code.value
    invocation.finished_at = now_utc()
    await session.flush()
    return invocation


async def reconcile_stale_dispatches(
    session: AsyncSession,
    *,
    stale_before: datetime,
    limit: int = 100,
) -> int:
    """Mark stale paid dispatches ambiguous without any provider activity."""

    _validate_aware_utc(stale_before, "stale_before")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    await acquire_identity_governance_lock(session)
    candidate_ids = list(
        await session.scalars(
            select(AIInvocation.id)
            .where(
                AIInvocation.status == AIInvocationStatus.DISPATCHING.value,
                AIInvocation.started_at < stale_before,
            )
            .order_by(
                AIInvocation.quota_period_start,
                AIInvocation.quota_period_end,
                AIInvocation.subject_id,
                AIInvocation.id,
            )
            .limit(limit)
        )
    )
    changed = 0
    for invocation_id in candidate_ids:
        snapshot = await _invocation_key(session, invocation_id)
        await session.scalar(
            select(HealthSubject)
            .where(HealthSubject.id == snapshot.subject_id)
            .with_for_update()
        )
        await _lock_exact_root(session, snapshot, require_active=False)
        await _lock_quota_rows(
            session,
            subject_id=snapshot.subject_id,
            period_start=snapshot.quota_period_start,
            period_end=snapshot.quota_period_end,
        )
        invocation = await session.scalar(
            select(AIInvocation)
            .where(AIInvocation.id == invocation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            invocation is None
            or invocation.status != AIInvocationStatus.DISPATCHING.value
            or invocation.started_at is None
        ):
            continue
        invocation.status = AIInvocationStatus.AMBIGUOUS.value
        invocation.error_code = AIInvocationErrorCode.TIMEOUT.value
        invocation.finished_at = now_utc()
        changed += 1
    if changed:
        await session.flush()
    return changed


async def reconcile_stale_reservations(
    session: AsyncSession,
    *,
    stale_before: datetime,
    limit: int = 100,
    error_code: AIInvocationErrorCode = AIInvocationErrorCode.CANCELLED_BY_POLICY,
) -> int:
    """Release abandoned prepared reservations without provider activity."""

    _validate_aware_utc(stale_before, "stale_before")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    if not isinstance(error_code, AIInvocationErrorCode):
        raise TypeError("error_code must be an AIInvocationErrorCode")
    await acquire_identity_governance_lock(session)
    candidate_ids = list(
        await session.scalars(
            select(AIInvocation.id)
            .where(
                AIInvocation.status == AIInvocationStatus.PREPARED.value,
                AIInvocation.created_at < stale_before,
            )
            .order_by(
                AIInvocation.quota_period_start,
                AIInvocation.quota_period_end,
                AIInvocation.subject_id,
                AIInvocation.id,
            )
            .limit(limit)
        )
    )
    changed = 0
    for invocation_id in candidate_ids:
        key = await _invocation_key(session, invocation_id)
        await session.scalar(
            select(HealthSubject)
            .where(HealthSubject.id == key.subject_id)
            .with_for_update()
        )
        await _lock_exact_root(session, key, require_active=False)
        platform_quota, subject_quota = await _lock_quota_rows(
            session,
            subject_id=key.subject_id,
            period_start=key.quota_period_start,
            period_end=key.quota_period_end,
        )
        invocation = await session.scalar(
            select(AIInvocation)
            .where(AIInvocation.id == invocation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if invocation is None or invocation.status != AIInvocationStatus.PREPARED.value:
            continue
        for quota in (platform_quota, subject_quota):
            if (
                quota.reserved_cost_microunits
                < invocation.reserved_cost_microunits
                or quota.reserved_units < invocation.reserved_units
            ):
                raise AIInvocationStateError(
                    "AI reservation accounting is inconsistent"
                )
            quota.reserved_cost_microunits -= invocation.reserved_cost_microunits
            quota.reserved_units -= invocation.reserved_units
        invocation.status = AIInvocationStatus.CANCELLED.value
        invocation.error_code = error_code.value
        invocation.finished_at = now_utc()
        changed += 1
    if changed:
        await session.flush()
    return changed


async def reconciliation_job(session_factory, redis=None) -> None:
    """Release abandoned reservations and close paid ambiguous dispatches.

    Each phase owns a short transaction so its governance/subject/root locks are
    released before the next population is scanned.  The job performs no provider
    I/O and stores no prompt, response, credential, or exception text.
    """

    del redis
    current = now_utc()
    async with session_factory() as session:
        await reconcile_stale_reservations(
            session,
            stale_before=current - PREPARED_STALE_AFTER,
        )
        await session.commit()
    async with session_factory() as session:
        await reconcile_stale_dispatches(
            session,
            stale_before=current - DISPATCHING_STALE_AFTER,
        )
        await session.commit()


__all__ = [
    "ALLOWED_CREDENTIAL_REFS",
    "DISPATCHING_STALE_AFTER",
    "MAX_SIGNED_BIGINT",
    "PREPARED_STALE_AFTER",
    "AICapabilityError",
    "AICompletion",
    "AIDispatchLease",
    "AIDispatchRequest",
    "AIGatewayAuthorizationError",
    "AIGatewayConfigurationError",
    "AIGatewayError",
    "AIInvocationStateError",
    "AIIdempotencyConflictError",
    "AIProviderDispatchError",
    "AIQuotaExceededError",
    "AIQuotaImmutableError",
    "AIReservationResult",
    "SanitizedAIUsage",
    "cancel_reserved_ai_invocation",
    "configure_platform_quota_period",
    "configure_subject_quota_period",
    "create_gateway",
    "disable_gateway",
    "dispatch_ai",
    "finalize_ai_invocation",
    "reconcile_stale_dispatches",
    "reconcile_stale_reservations",
    "reconciliation_job",
    "reserve_ai_invocation",
    "rotate_gateway",
    "start_ai_dispatch",
]
