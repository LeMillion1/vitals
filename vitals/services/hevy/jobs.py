"""Transactional Hevy sync entry points for schedulers and MCP."""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from vitals.enums import IntegrationProvider
from vitals.ownership import WriteIdentity
from vitals.services.hevy.ownership import HevyOwnershipInactiveConnectionError
from vitals.services.hevy.sync import sync_owned

logger = logging.getLogger(__name__)


async def sync_now_for_actor(
    session_factory,
    redis=None,
    *,
    actor_username: str,
) -> Optional[dict]:
    """Sync Hevy for the health record owned by the named actor."""

    from vitals.services.legacy_ownership import resolve_legacy_ownership_context

    async with session_factory() as session:
        ownership = await resolve_legacy_ownership_context(
            session,
            actor_username=actor_username,
            required_connections=(IntegrationProvider.HEVY,),
        )
        subject_id = ownership.subject_id
        actor_user_id = ownership.actor_user_id
    return await sync_job(
        session_factory,
        redis,
        subject_id=subject_id,
        actor_user_id=actor_user_id,
    )


async def sync_job(
    session_factory,
    redis=None,
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Optional[dict]:
    """Run one transactional Hevy sync, or no-op when it is unavailable."""

    from vitals.integrations.hevy_client import HevyClient

    del integration_connection_id  # named by the fan-out; resolved below

    async with session_factory() as session:
        from vitals.services.credentials import providers
        from vitals.services.legacy_ownership import (
            resolve_subject_ownership_context,
        )

        ownership = await resolve_subject_ownership_context(
            session,
            subject_id=subject_id,
            required_connections=(IntegrationProvider.HEVY,),
        )
        account = await providers.resolve_hevy_account(
            session, subject_id=ownership.subject_id
        )
        if account is None or not account.configured:
            await session.rollback()
            return None
        client = HevyClient.from_config(account.config)
        try:
            summary = await sync_owned(
                session,
                client,
                identity=(
                    WriteIdentity(ownership.subject_id, actor_user_id)
                    if actor_user_id is not None
                    else ownership.write_identity
                ),
                integration_connection_id=ownership.connection_id(
                    IntegrationProvider.HEVY
                ),
            )
        except HevyOwnershipInactiveConnectionError:
            logger.info("Hevy sync skipped: connection is not active")
            await session.rollback()
            return None
        await session.commit()
        if redis is not None:
            import time

            await redis.set(
                providers.sync_marker_key(
                    IntegrationProvider.HEVY, account.namespace
                ),
                str(int(time.time())),
            )
        return summary


__all__ = ["sync_job", "sync_now_for_actor"]
