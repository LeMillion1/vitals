"""Period AI digest service (module 10) — the product core.

For each report we assemble a versioned, module-aware **structured cross-domain
snapshot** with one authoritative date window and ask the LLM for an *analytical
narrative* — the interpretation of how the domains relate, not a restatement of
the numbers. The structured context is stored alongside the text so it can be
re-inspected or re-run later.

Production generation reserves one subject-owned platform AI invocation, closes
the database transaction, performs exactly one provider call, then atomically
finalizes accounting and the digest artifact.  The legacy injected-client seam
is quarantined to databases with no commercial identity roots.
"""
from __future__ import annotations

from vitals.services.ai_gateway import contracts as ai_gateway_service_contracts
from vitals.services.ai_gateway import dispatch as ai_gateway_service_dispatch

import json
from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationStatus,
    DigestKind,
)
from vitals.integrations.llm_client import (
    LLMCallResult,
    LLMClient,
)
from vitals.models.ai import AIInvocation
from vitals.models.milestones import DOMAIN, WeeklyDigest
from vitals.ownership import WriteIdentity

from vitals.services.digest.ownership import (
    DigestInvocationStateError,
    DigestOwnershipError,
    PreparedDigest,
    PreparedDigestOwner,
    _DIGEST_MAX_TOKENS,
    _require_prepared_digest,
    _require_prepared_digest_owner,
)
from vitals.services.digest.prompt import DIGEST_SYSTEM, DIGEST_SYSTEM_EN

def _resolve_openrouter_credential(credential_ref: str) -> str | None:
    if credential_ref not in ai_gateway_service_contracts.ALLOWED_CREDENTIAL_REFS:
        return None
    credential = load_config().openrouter_api_key.strip()
    return credential or None


async def start_digest_dispatch(
    session: AsyncSession,
    prepared: PreparedDigest,
    *,
    credential_resolver=None,
) -> ai_gateway_service_contracts.AIDispatchLease:
    """Freshly authorize and charge one prepared digest; caller commits."""
    snapshot = _require_prepared_digest(prepared)
    if not snapshot._dispatchable:
        raise DigestInvocationStateError(
            f"digest invocation is {snapshot._reservation_status.value}"
        )
    resolver = credential_resolver or _resolve_openrouter_credential
    return await ai_gateway_service_dispatch.start_ai_dispatch(
        session,
        identity=WriteIdentity(
            subject_id=snapshot._subject_id,
            actor_user_id=snapshot._actor_user_id,
        ),
        invocation_id=snapshot._invocation_id,
        credential_resolver=resolver,
    )


async def cancel_prepared_digest(
    session: AsyncSession,
    prepared: PreparedDigest,
) -> AIInvocation:
    """Release a still-prepared reservation after a zero-network boundary error."""
    snapshot = _require_prepared_digest(prepared)
    return await ai_gateway_service_dispatch.cancel_reserved_ai_invocation(
        session,
        identity=WriteIdentity(
            subject_id=snapshot._subject_id,
            actor_user_id=snapshot._actor_user_id,
        ),
        invocation_id=snapshot._invocation_id,
    )


async def release_prepared_digest(
    session: AsyncSession,
    prepared: PreparedDigest,
) -> bool:
    """Best-effort zero-network release after a failed start authorization.

    The caller must begin a fresh transaction and owns commit/rollback.  A
    concurrent dispatcher, suspended actor, or stale capability returns ``False``
    without disguising the original boundary error; the platform reconciliation
    job remains the crash/revocation backstop.
    """

    try:
        await cancel_prepared_digest(session, prepared)
    except (
        ai_gateway_service_contracts.AIGatewayAuthorizationError,
        ai_gateway_service_contracts.AIGatewayConfigurationError,
        ai_gateway_service_contracts.AIInvocationStateError,
    ):
        return False
    return True


async def render_digest(
    prepared: PreparedDigest,
    lease: ai_gateway_service_contracts.AIDispatchLease,
) -> ai_gateway_service_contracts.AICompletion[LLMCallResult[str]]:
    """Perform exactly one gateway-funded OpenRouter call with no DB I/O."""
    snapshot = _require_prepared_digest(prepared)
    system = DIGEST_SYSTEM_EN if snapshot._lang == "en" else DIGEST_SYSTEM

    async def provider_call(
        request: ai_gateway_service_contracts.AIDispatchRequest,
    ) -> LLMCallResult[str]:
        if (
            request.invocation_id != snapshot._invocation_id
            or request.model != snapshot._model
        ):
            raise DigestInvocationStateError("digest dispatch provenance changed")
        config = replace(
            load_config(),
            openrouter_api_key=request.credential,
        )
        return await LLMClient(config).complete_text_with_usage(
            snapshot._prompt,
            model=request.model,
            system=system,
            max_tokens=_DIGEST_MAX_TOKENS,
        )

    def usage_extractor(
        result: LLMCallResult[str],
    ) -> ai_gateway_service_contracts.SanitizedAIUsage:
        if (
            not isinstance(result, LLMCallResult)
            or not isinstance(result.value, str)
            or not result.value.strip()
            or result.input_tokens is None
            or result.output_tokens is None
            or result.cost_microunits is None
        ):
            raise ValueError("digest provider usage is incomplete")
        return ai_gateway_service_contracts.SanitizedAIUsage(
            upstream_request_id=result.upstream_request_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_microunits=result.cost_microunits,
        )

    return await ai_gateway_service_dispatch.dispatch_ai(
        lease,
        provider_call=provider_call,
        usage_extractor=usage_extractor,
    )


async def persist_digest(
    session: AsyncSession,
    prepared: PreparedDigest,
    completion: ai_gateway_service_contracts.AICompletion[LLMCallResult[str]],
) -> WeeklyDigest | None:
    """Atomically finalize paid metadata and insert one successful artifact."""
    snapshot = _require_prepared_digest(prepared)
    if completion.invocation_id != snapshot._invocation_id:
        raise DigestInvocationStateError("digest completion belongs to another call")
    invocation = await ai_gateway_service_dispatch.finalize_ai_invocation(
        session,
        completion=completion,
    )
    if (
        invocation.subject_id != snapshot._subject_id
        or invocation.actor_user_id != snapshot._actor_user_id
        or invocation.purpose != AIInvocationPurpose.WEEKLY_DIGEST.value
        or invocation.source != snapshot._invocation_source.value
        or invocation.model != snapshot._model
    ):
        raise DigestInvocationStateError("digest invocation provenance changed")
    if invocation.status != AIInvocationStatus.SUCCEEDED.value:
        return None
    result = completion.payload
    if (
        not isinstance(result, LLMCallResult)
        or not isinstance(result.value, str)
        or not result.value.strip()
    ):
        raise DigestInvocationStateError("successful digest payload is missing")
    try:
        context = json.loads(snapshot._context_json_text)
    except (TypeError, ValueError) as exc:  # pragma: no cover - frozen factory output
        raise DigestOwnershipError("prepared digest context is invalid") from exc
    row = WeeklyDigest(
        subject_id=snapshot._subject_id,
        actor_user_id=snapshot._actor_user_id,
        integration_connection_id=None,
        ai_invocation_id=invocation.id,
        date=snapshot._on_date,
        domain=DOMAIN,
        source=snapshot._artifact_source,
        kind=DigestKind.WEEKLY.value,
        content=result.value.strip(),
        context_json=context,
        model=snapshot._model,
    )
    session.add(row)
    await session.flush()
    return row


async def existing_digest_for_prepared(
    session: AsyncSession,
    prepared: PreparedDigest,
    *,
    prepared_owner: PreparedDigestOwner,
) -> WeeklyDigest | None:
    """Reload one idempotent artifact under a fresh exact-owner read proof."""
    snapshot = _require_prepared_digest(prepared)
    owner = _require_prepared_digest_owner(session, prepared_owner)
    if (
        owner._subject_id != snapshot._subject_id
        or owner._actor_user_id != snapshot._actor_user_id
        or snapshot._existing_artifact_id is None
    ):
        return None
    return await session.scalar(
        select(WeeklyDigest)
        .where(
            WeeklyDigest.id == snapshot._existing_artifact_id,
            WeeklyDigest.subject_id == snapshot._subject_id,
            WeeklyDigest.ai_invocation_id == snapshot._invocation_id,
        )
        .execution_options(populate_existing=True)
    )
