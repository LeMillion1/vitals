"""Platform-funded AI control plane, hard quotas, and at-most-once dispatch.

Database rows contain only authorization/provenance/accounting metadata. Prompt,
response, provider secrets, and exception text exist only in short-lived in-memory
objects. Every mutating database function flushes; its caller owns commit.

Lock order is identity governance -> S -> A -> raw provenance -> platform root ->
platform quota -> subject quota -> invocation. No provider await is permitted
until the issuing start-dispatch transaction has committed.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationStatus,
)
from vitals.models.ai import (
    AIInvocation,
)
from vitals.models.identity import HealthSubject
from vitals.ownership import WriteIdentity
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.utils.timeutils import now_utc

from vitals.services.ai_gateway.contracts import (
    AICapabilityError,
    AICompletion,
    AIDispatchLease,
    AIDispatchRequest,
    AIGatewayAuthorizationError,
    AIGatewayConfigurationError,
    AIInvocationStateError,
    SanitizedAIUsage,
    T,
    _COMPLETION_SEAL,
    _LEASE_SEAL,
)

from vitals.services.ai_gateway.config import (
    _lock_exact_root,
    _lock_quota_rows,
    _lock_raw_payload_scope,
    _lock_subject_authority,
)

from vitals.services.ai_gateway.invocations import (
    _invocation_key,
)


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
    await _lock_raw_payload_scope(
        session,
        raw_payload_id=snapshot.raw_payload_id,
        subject_id=snapshot.subject_id,
    )
    root = await _lock_exact_root(session, snapshot, require_active=True)
    billing_date = now_utc().date()
    if not (snapshot.quota_period_start <= billing_date < snapshot.quota_period_end):
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
            quota.reserved_cost_microunits < invocation.reserved_cost_microunits
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
            lease._raw_payload_id,
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
        raw_payload_id=lease._raw_payload_id,
        credential=credential,
        fingerprint=lease._fingerprint,
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
        snapshot.raw_payload_id,
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
        select(HealthSubject).where(HealthSubject.id == snapshot.subject_id).with_for_update()
    )
    await _lock_raw_payload_scope(
        session,
        raw_payload_id=snapshot.raw_payload_id,
        subject_id=snapshot.subject_id,
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
    await _lock_raw_payload_scope(
        session,
        raw_payload_id=snapshot.raw_payload_id,
        subject_id=snapshot.subject_id,
    )
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
            quota.reserved_cost_microunits < invocation.reserved_cost_microunits
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
