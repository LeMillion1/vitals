"""Scheduled period digest generation."""
from __future__ import annotations

from vitals.services.ai_gateway import contracts as ai_gateway_service_contracts

import uuid

from vitals.enums import AIInvocationSource, AIInvocationStatus
from vitals.services.digest.generation import (
    persist_digest,
    release_prepared_digest,
    render_digest,
    start_digest_dispatch,
)
from vitals.services.digest.ownership import (
    DigestInvocationStateError,
    prepare_digest,
    prepare_subject_digest_owner,
)

async def digest_job(
    session_factory, redis=None, *, subject_id: uuid.UUID
) -> None:
    """Generate one idempotent platform-funded weekly digest."""
    del redis
    from vitals.i18n import current_lang
    from vitals.services.preferences.language import get_language

    try:
        async with session_factory() as session:
            owner = await prepare_subject_digest_owner(
                session,
                subject_id=subject_id,
            )
            # DB is authoritative here. Avoid a Redis await while governance and
            # the subject are locked; the weekly job needs no cache acceleration.
            current_lang.set(
                await get_language(
                    session,
                    None,
                    user_id=owner._owner_user_id,
                )
            )
            prepared = await prepare_digest(
                session,
                actor_username=None,
                invocation_source=AIInvocationSource.SCHEDULER,
                prepared_owner=owner,
            )
            await session.commit()
    except (
        ai_gateway_service_contracts.AIGatewayConfigurationError,
        ai_gateway_service_contracts.AIQuotaExceededError,
    ):
        return

    if prepared.existing_artifact_id is not None or not prepared.dispatchable:
        if prepared.reservation_status is AIInvocationStatus.DISPATCHING:
            return
        raise DigestInvocationStateError(
            f"scheduled digest attempt ended as {prepared.reservation_status.value}"
        )

    async with session_factory() as session:
        try:
            lease = await start_digest_dispatch(session, prepared)
            await session.commit()
        except (
            ai_gateway_service_contracts.AIGatewayAuthorizationError,
            ai_gateway_service_contracts.AIGatewayConfigurationError,
        ):
            await session.rollback()
            if await release_prepared_digest(session, prepared):
                await session.commit()
            else:
                await session.rollback()
            return
        except ai_gateway_service_contracts.AIInvocationStateError:
            await session.rollback()
            return

    completion = await render_digest(prepared, lease)
    async with session_factory() as session:
        row = await persist_digest(session, prepared, completion)
        await session.commit()
    if row is None:
        completion.raise_for_provider_failure()
