"""Transaction-owning Garmin entry points for scheduler and interactive sync."""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from vitals.enums import IntegrationProvider
from vitals.ownership import WriteIdentity
from vitals.services.credentials import providers
from vitals.services.garmin.errors import GarminConnectionInactiveError
from vitals.services.garmin.sync import pulse_owned, sync_owned
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)


async def pulse_job(
    session_factory,
    redis=None,
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None = None,
) -> None:
    """Run the lightweight Garmin pulse for one scheduled subject."""

    from vitals.integrations.garmin_client import GarminClient
    from vitals.services.tenancy.contracts import (
        LegacyOwnershipError,
        LegacySubjectResolutionError,
    )
    from vitals.services.tenancy.ownership import resolve_subject_ownership_context
    from vitals.services.proactive.preferences import contracts as preference_contracts
    from vitals.services.proactive.preferences import queries as preference_queries

    del integration_connection_id  # named by fan-out; resolved under ownership

    async with session_factory() as session:
        try:
            ownership = await resolve_subject_ownership_context(
                session,
                subject_id=subject_id,
                required_connections=(IntegrationProvider.GARMIN,),
            )
        except LegacySubjectResolutionError:
            logger.warning(
                "Garmin pulse skipped: no single health subject to sync for",
                exc_info=True,
            )
            return
        except LegacyOwnershipError:
            logger.warning(
                "Garmin pulse skipped: legacy ownership is unavailable",
                exc_info=True,
            )
            return
        try:
            policy = await preference_queries.get_garmin_policy(
                session,
                subject_id=ownership.subject_id,
                integration_connection_id=ownership.connection_id(
                    IntegrationProvider.GARMIN
                ),
            )
        except preference_contracts.ProactivePreferencesError:
            logger.warning(
                "Garmin pulse skipped: scoped preferences are unavailable",
                exc_info=True,
            )
            return
        if not policy.pulse_seconds:
            return
        if not policy.pulse_start_hour <= now_local().hour < policy.pulse_end_hour:
            return

        account = await providers.resolve_garmin_account(
            session,
            subject_id=ownership.subject_id,
        )
        if account is None or not account.configured:
            return
        client = GarminClient.from_config(account.config, redis)
        try:
            await pulse_owned(
                session,
                client,
                identity=ownership.write_identity,
                integration_connection_id=ownership.connection_id(
                    IntegrationProvider.GARMIN
                ),
            )
        except GarminConnectionInactiveError:
            logger.info("Garmin pulse skipped: connection is not active")
            await session.rollback()
            return
        await session.commit()


async def sync_now_for_actor(
    session_factory,
    redis=None,
    *,
    actor_username: str,
    days: int = 2,
) -> Optional[dict]:
    """Resolve an actor-owned record, then run a Garmin sync."""

    from vitals.services.tenancy.ownership import resolve_legacy_ownership_context

    async with session_factory() as session:
        ownership = await resolve_legacy_ownership_context(
            session,
            actor_username=actor_username,
            required_connections=(IntegrationProvider.GARMIN,),
        )
        subject_id = ownership.subject_id
        actor_user_id = ownership.actor_user_id
    return await sync_job(
        session_factory,
        redis,
        days=days,
        subject_id=subject_id,
        actor_user_id=actor_user_id,
    )


async def sync_job(
    session_factory,
    redis=None,
    *,
    days: int = 2,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Optional[dict]:
    """Poll one scheduler-named Garmin account and commit its unit of work."""

    from vitals.i18n import current_lang
    from vitals.integrations.garmin_client import GarminClient
    from vitals.services.preferences.language import get_language
    from vitals.services.tenancy.ownership import resolve_subject_ownership_context

    del integration_connection_id  # named by fan-out; resolved under ownership

    async with session_factory() as session:
        ownership = await resolve_subject_ownership_context(
            session,
            subject_id=subject_id,
            required_connections=(IntegrationProvider.GARMIN,),
        )
        account = await providers.resolve_garmin_account(
            session,
            subject_id=ownership.subject_id,
        )
        if account is None or not account.configured:
            await session.rollback()
            return None
        client = GarminClient.from_config(account.config, redis)
        lang = await get_language(
            session,
            redis,
            user_id=ownership.owner_user_id,
        )
        current_lang.set(lang)

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
                    IntegrationProvider.GARMIN
                ),
                days=days,
            )
        except GarminConnectionInactiveError:
            logger.info("Garmin sync skipped: connection is not active")
            await session.rollback()
            return None
        await session.commit()
        if redis is not None and summary.get("error") is None:
            import time

            await redis.set(
                providers.sync_marker_key(
                    IntegrationProvider.GARMIN,
                    account.namespace,
                ),
                str(int(time.time())),
            )
        return summary
