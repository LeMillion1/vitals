"""Owned Hevy provider sync orchestration."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.ownership import WriteIdentity
from vitals.services.hevy.ingestion import ingest_owned_workouts
from vitals.services.hevy.ownership import (
    _lock_owned_hevy_scope,
    _require_owned_hevy_connection,
)
from vitals.services.identity.governance import acquire_identity_governance_lock


async def _fetch_provider_workouts(client: Any, *, max_pages: int) -> list[Any]:
    """Perform the provider-only step without accepting a database session."""

    return await client.fetch_workouts(max_pages=max_pages)


async def sync_owned(
    session: AsyncSession,
    client: Any,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    max_pages: int = 50,
    force: bool = False,
) -> dict[str, int]:
    """Fetch and normalize Hevy workouts inside one explicit S/A/C scope.

    The connection is validated before network I/O. Every fetched object goes
    through the strict owned raw-payload chokepoint even when its normalized row
    is unchanged, so an old S/C-null raw row is safely adopted instead of being
    bypassed by the idempotency shortcut. The caller owns commit or rollback.

    The provider fetch is isolated in a session-free helper. The fail-closed
    connection preflight remains before it to preserve the observable promise
    that an invalid root never spends provider quota; consequently the caller's
    transaction remains open during this network step.
    """

    await _require_owned_hevy_connection(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_retired=False,
    )
    raw_workouts = await _fetch_provider_workouts(client, max_pages=max_pages)
    summary = {
        "fetched": len(raw_workouts),
        "created": 0,
        "updated": 0,
        "skipped": 0,
    }
    # Keep alert legacy adoption and provider ingestion on one canonical lock
    # order without holding the governance lock across vendor network latency.
    await acquire_identity_governance_lock(session)
    await _lock_owned_hevy_scope(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_retired=False,
    )

    return await ingest_owned_workouts(
        session,
        raw_workouts,
        identity=identity,
        integration_connection_id=integration_connection_id,
        force=force,
        summary=summary,
    )


__all__ = ["sync_owned"]
