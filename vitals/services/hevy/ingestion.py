"""Raw-first ingestion of already-fetched Hevy workout payloads."""
from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.hevy import DOMAIN
from vitals.ownership import WriteIdentity
from vitals.services import raw_payload_service
from vitals.services.hevy.normalization import _parse_dt
from vitals.services.hevy.ownership import (
    _adopt_owned_workout_children,
    _owned_children_need_adoption,
    _preflight_workout_raw_link,
    _resolve_owned_workout,
    _validate_owned_raw_payload,
    _validate_workout_raw_link,
)
from vitals.services.hevy.persistence import _upsert_owned_workout
from vitals.utils.timeutils import now_local


async def ingest_owned_workouts(
    session: AsyncSession,
    raw_workouts: Iterable[Any],
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    force: bool,
    summary: dict[str, int],
) -> dict[str, int]:
    """Persist one fetched batch after the facade acquired the owned locks."""

    for raw in raw_workouts:
        if not isinstance(raw, dict):
            summary["skipped"] += 1
            continue
        external_id = str(raw.get("id") or "").strip()
        if not external_id:
            summary["skipped"] += 1
            continue

        workout, adopt_legacy = await _resolve_owned_workout(
            session,
            identity=identity,
            integration_connection_id=integration_connection_id,
            external_id=external_id,
        )
        await _preflight_workout_raw_link(
            session,
            workout=workout,
            identity=identity,
            integration_connection_id=integration_connection_id,
            external_id=external_id,
        )
        children_need_adoption = (
            await _owned_children_need_adoption(
                session,
                workout=workout,
                identity=identity,
                integration_connection_id=integration_connection_id,
            )
            if workout is not None
            else False
        )
        raw_row = await raw_payload_service.upsert_owned_raw_payload(
            session,
            identity=identity,
            integration_connection_id=integration_connection_id,
            domain=DOMAIN,
            source=Source.HEVY_API.value,
            external_id=external_id,
            payload=raw,
        )
        _validate_owned_raw_payload(
            raw_row,
            identity=identity,
            integration_connection_id=integration_connection_id,
            external_id=external_id,
        )

        hevy_updated = _parse_dt(raw.get("updated_at"))
        if (
            workout is not None
            and not adopt_legacy
            and not force
            and workout.hevy_updated_at == hevy_updated
        ):
            _validate_workout_raw_link(workout, raw_payload_id=raw_row.id)
            if workout.raw_payload_id is None:
                workout.raw_payload_id = raw_row.id
            if children_need_adoption:
                await _adopt_owned_workout_children(
                    session,
                    workout=workout,
                    identity=identity,
                    integration_connection_id=integration_connection_id,
                )
            raw_row.processed_at = now_local()
            summary["skipped"] += 1
            continue

        created = await _upsert_owned_workout(
            session,
            raw_row=raw_row,
            identity=identity,
            integration_connection_id=integration_connection_id,
            workout=workout,
            adopt_legacy=adopt_legacy,
        )
        raw_row.processed_at = now_local()
        summary["created" if created else "updated"] += 1

    await session.flush()
    return summary


__all__ = ["ingest_owned_workouts"]
