"""Daily Brief artifact reconciliation and persistence."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationPurpose,
    AIInvocationStatus,
    DigestKind,
)
from vitals.integrations.llm_client import LLMCallResult
from vitals.models.ai import AIInvocation
from vitals.models.milestones import DOMAIN as DIGEST_DOMAIN, WeeklyDigest
from vitals.ownership import WriteIdentity
from vitals.services.ai_gateway import contracts as ai_gateway_service_contracts
from vitals.services.ai_gateway import dispatch as ai_gateway_service_dispatch
from vitals.services.digest import ownership as digest_ownership

from .context import _require_llm_connection_scope
from .contracts import (
    BriefInvocationStateError,
    BriefOwnershipError,
    PreparedBrief,
    _RenderedBrief,
    _TERMINAL_HEADER_STATUSES,
)
from .preparation import (
    _existing_unfunded_artifact,
    _require_prepared_brief,
)
from .prompt import _context_with_provenance

async def _require_fresh_owner(
    session: AsyncSession,
    prepared: PreparedBrief,
) -> WriteIdentity:
    owner = await digest_ownership.prepare_digest_owner(
        session,
        actor_username=prepared._actor_username,
    )
    identity = owner.identity
    if (
        identity.subject_id != prepared._subject_id
        or identity.actor_user_id != prepared._actor_user_id
        or owner.owner_user_id != prepared._owner_user_id
    ):
        raise BriefOwnershipError("Daily Brief owner changed")
    return identity


async def _existing_for_prepared(
    session: AsyncSession,
    prepared: PreparedBrief,
) -> WeeklyDigest | None:
    if prepared._invocation_id is not None:
        return await session.scalar(
            select(WeeklyDigest).where(
                WeeklyDigest.subject_id == prepared._subject_id,
                WeeklyDigest.ai_invocation_id == prepared._invocation_id,
            )
        )
    return await _existing_unfunded_artifact(
        session,
        subject_id=prepared._subject_id,
        actor_user_id=prepared._actor_user_id,
        artifact_source=prepared._artifact_source,
        on_date=prepared._on_date,
        request_key=prepared._request_key,
    )


async def _insert_brief_artifact(
    session: AsyncSession,
    prepared: PreparedBrief,
    *,
    invocation: AIInvocation | None,
    narrative: str | None,
) -> WeeklyDigest:
    status = AIInvocationStatus(invocation.status) if invocation is not None else None
    if narrative is not None:
        content = f"{prepared._base_content}\n\n{narrative.strip()}"
        model = prepared._model
        mode = "ai"
    else:
        content = prepared._base_content
        model = None
        mode = "header_only"
    row = WeeklyDigest(
        subject_id=prepared._subject_id,
        actor_user_id=prepared._actor_user_id,
        integration_connection_id=None,
        ai_invocation_id=invocation.id if invocation is not None else None,
        date=prepared._on_date,
        domain=DIGEST_DOMAIN,
        source=prepared._artifact_source,
        kind=DigestKind.DAILY_BRIEF.value,
        content=content,
        context_json=_context_with_provenance(prepared, mode=mode, status=status),
        model=model,
    )
    session.add(row)
    await session.flush()
    return row


async def persist_brief(
    session: AsyncSession,
    prepared: PreparedBrief,
    completion: ai_gateway_service_contracts.AICompletion[LLMCallResult[str]] | None,
) -> WeeklyDigest:
    """Finalize accounting and persist one narrative or deterministic header."""

    snapshot = _require_prepared_brief(prepared)
    # A sealed paid completion must always reach terminal accounting, even when
    # the human was suspended or ownership changed during provider I/O. Current
    # authorization still gates T1/T2, non-AI writes, cancellation, and reads.
    if completion is None:
        await _require_fresh_owner(session, snapshot)
    existing = await _existing_for_prepared(session, snapshot)
    if existing is not None:
        return existing
    invocation = None
    narrative = None
    if snapshot._invocation_id is not None:
        if completion is not None:
            if completion.invocation_id != snapshot._invocation_id:
                raise BriefInvocationStateError(
                    "Daily Brief completion belongs to another invocation"
                )
            invocation = await ai_gateway_service_dispatch.finalize_ai_invocation(
                session,
                completion=completion,
            )
        else:
            invocation = await session.scalar(
                select(AIInvocation)
                .where(AIInvocation.id == snapshot._invocation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if invocation is None or (
            invocation.subject_id != snapshot._subject_id
            or invocation.actor_user_id != snapshot._actor_user_id
            or invocation.purpose != AIInvocationPurpose.DAILY_BRIEF.value
            or invocation.source != snapshot._invocation_source.value
            or invocation.model != snapshot._model
        ):
            raise BriefInvocationStateError("Daily Brief invocation provenance changed")
        status = AIInvocationStatus(invocation.status)
        if status is AIInvocationStatus.SUCCEEDED:
            if completion is None:
                raise BriefInvocationStateError(
                    "succeeded Daily Brief payload is unavailable"
                )
            result = completion.payload
            if (
                not isinstance(result, LLMCallResult)
                or not isinstance(result.value, str)
                or not result.value.strip()
            ):
                raise BriefInvocationStateError(
                    "successful Daily Brief payload is missing"
                )
            narrative = result.value.strip()
        elif status not in _TERMINAL_HEADER_STATUSES:
            raise BriefInvocationStateError(
                "live Daily Brief invocation cannot have an artifact"
            )
    elif completion is not None:
        raise BriefInvocationStateError("unfunded Daily Brief cannot finalize AI")
    return await _insert_brief_artifact(
        session,
        snapshot,
        invocation=invocation,
        narrative=narrative,
    )


async def cancel_and_persist_header_brief(
    session: AsyncSession,
    prepared: PreparedBrief,
) -> WeeklyDigest:
    """Release a zero-network reservation and preserve its header provenance."""

    snapshot = _require_prepared_brief(prepared)
    await _require_fresh_owner(session, snapshot)
    existing = await _existing_for_prepared(session, snapshot)
    if existing is not None:
        return existing
    if snapshot._invocation_id is None:
        return await _insert_brief_artifact(
            session,
            snapshot,
            invocation=None,
            narrative=None,
        )
    invocation = await session.scalar(
        select(AIInvocation)
        .where(AIInvocation.id == snapshot._invocation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invocation is None:
        raise BriefInvocationStateError("Daily Brief invocation is missing")
    status = AIInvocationStatus(invocation.status)
    if status is AIInvocationStatus.PREPARED:
        invocation = await ai_gateway_service_dispatch.cancel_reserved_ai_invocation(
            session,
            identity=WriteIdentity(
                subject_id=snapshot._subject_id,
                actor_user_id=snapshot._actor_user_id,
            ),
            invocation_id=snapshot._invocation_id,
            error_code=AIInvocationErrorCode.CANCELLED_BY_POLICY,
        )
    elif status not in _TERMINAL_HEADER_STATUSES:
        raise BriefInvocationStateError(
            "paid or succeeded Daily Brief cannot be cancelled"
        )
    return await _insert_brief_artifact(
        session,
        snapshot,
        invocation=invocation,
        narrative=None,
    )


async def existing_brief_for_prepared(
    session: AsyncSession,
    prepared: PreparedBrief,
) -> WeeklyDigest | None:
    snapshot = _require_prepared_brief(prepared)
    await _require_fresh_owner(session, snapshot)
    return await _existing_for_prepared(session, snapshot)



async def _persist_brief(
    session: AsyncSession,
    rendered: _RenderedBrief,
) -> WeeklyDigest:
    """Persist one rendered payload after revalidating its immutable roots."""
    if not isinstance(rendered, _RenderedBrief):
        raise BriefOwnershipError("rendered brief must be a _RenderedBrief")
    prepared = rendered.prepared
    identity = prepared.identity
    if identity is not None:
        assert prepared.llm_connection_id is not None
        await _require_llm_connection_scope(
            session,
            identity=identity,
            connection_id=prepared.llm_connection_id,
        )

    row = WeeklyDigest(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        integration_connection_id=(
            prepared.llm_connection_id if rendered.used_llm else None
        ),
        date=prepared.on_date,
        domain=DIGEST_DOMAIN,
        source=prepared.source,
        kind=DigestKind.DAILY_BRIEF.value,
        content=rendered.content,
        context_json=prepared.context,
        model=rendered.model,
    )
    session.add(row)
    await session.flush()
    return row
