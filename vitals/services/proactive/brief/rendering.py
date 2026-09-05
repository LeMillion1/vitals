"""Out-of-transaction Daily Brief provider dispatch and rendering."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import AIInvocationSource
from vitals.integrations.llm_client import LLMCallResult, LLMClient
from vitals.ownership import WriteIdentity
from vitals.services.ai_gateway import contracts as ai_gateway_service_contracts
from vitals.services.ai_gateway import dispatch as ai_gateway_service_dispatch
from vitals.services.digest import ownership as digest_ownership
from vitals.services.proactive import compose

from .contracts import (
    BRIEF_SYSTEM,
    BriefInvocationStateError,
    BriefOwnershipError,
    PreparedBrief,
    _BRIEF_MAX_TOKENS,
    _PreparedBrief,
    _RenderedBrief,
)
from .preparation import _require_prepared_brief, _resolve_openrouter_credential
from .prompt import build_prompt

logger = logging.getLogger(__name__)

async def start_brief_dispatch(
    session: AsyncSession,
    prepared: PreparedBrief,
    *,
    credential_resolver=None,
) -> ai_gateway_service_contracts.AIDispatchLease:
    snapshot = _require_prepared_brief(prepared)
    if not snapshot._dispatchable or snapshot._invocation_id is None:
        raise BriefInvocationStateError("Daily Brief is not dispatchable")
    identity = WriteIdentity(
        subject_id=snapshot._subject_id,
        actor_user_id=snapshot._actor_user_id,
    )
    if snapshot._invocation_source is AIInvocationSource.SCHEDULER:
        # The gateway correctly keeps scheduler provenance actorless. Revalidate
        # the frozen owner separately so an owner suspension/rotation between T1
        # and T2 cannot authorize platform spend. This takes canonical
        # governance -> subject -> owner locks before gateway root/quota locks.
        owner = await digest_ownership.prepare_subject_digest_owner(
            session,
            subject_id=snapshot._subject_id,
        )
        if (
            owner.identity != identity
            or owner.owner_user_id != snapshot._owner_user_id
        ):
            raise BriefOwnershipError("Daily Brief owner changed")
    return await ai_gateway_service_dispatch.start_ai_dispatch(
        session,
        identity=identity,
        invocation_id=snapshot._invocation_id,
        credential_resolver=credential_resolver or _resolve_openrouter_credential,
    )


async def render_brief(
    prepared: PreparedBrief,
    lease: ai_gateway_service_contracts.AIDispatchLease,
) -> ai_gateway_service_contracts.AICompletion[LLMCallResult[str]]:
    """Perform exactly one platform-funded provider call without DB access."""

    snapshot = _require_prepared_brief(prepared)

    async def provider_call(
        request: ai_gateway_service_contracts.AIDispatchRequest,
    ) -> LLMCallResult[str]:
        if (
            request.invocation_id != snapshot._invocation_id
            or request.model != snapshot._model
        ):
            raise BriefInvocationStateError("Daily Brief dispatch provenance changed")
        config = replace(load_config(), openrouter_api_key=request.credential)
        return await LLMClient(config).complete_text_with_usage(
            snapshot._prompt,
            model=request.model,
            system=BRIEF_SYSTEM,
            max_tokens=_BRIEF_MAX_TOKENS,
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
            raise ValueError("Daily Brief provider usage is incomplete")
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



async def narrative(llm: Any, ctx: dict) -> str:
    """The model's one block. Returns "" on any failure — never raises."""
    try:
        return await llm.complete_text(
            build_prompt(ctx),
            model=getattr(llm, "brief_model", None),
            system=BRIEF_SYSTEM,
            max_tokens=_BRIEF_MAX_TOKENS,
        )
    except Exception:
        logger.warning("brief narrative unavailable (code=provider_error)")
        return ""



async def _render_brief(llm: Any, prepared: _PreparedBrief) -> _RenderedBrief:
    """Call the model and render text; this function performs no database I/O."""
    if not isinstance(prepared, _PreparedBrief):
        raise BriefOwnershipError("prepared brief must be a _PreparedBrief")

    blocks = compose.header_blocks(prepared.context)
    tail = await narrative(llm, prepared.context)
    if tail:
        blocks.append(compose.Block(compose.KIND_NARRATIVE, tail, 90))

    return _RenderedBrief(
        prepared=prepared,
        content=compose.render(blocks),
        model=getattr(llm, "brief_model", None) if tail else None,
        used_llm=bool(tail),
    )
