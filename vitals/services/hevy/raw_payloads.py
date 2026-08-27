"""Reparse owned Hevy workout facts from the raw payload store."""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Source
from vitals.models.hevy import DOMAIN, HevyWorkout
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services.data_lake import sweep as raw_sweep
from vitals.services.hevy.ownership import (
    HevyRawPayloadInvariantError,
    _lock_owned_hevy_scope,
    _require_owned_hevy_connection,
    _resolve_owned_workout,
    _validate_owned_raw_payload,
    _validate_owned_scope,
)
from vitals.services.hevy.persistence import _upsert_owned_workout
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)


async def reparse_owned_from_raw(
    session: AsyncSession,
    raw_row: RawPayload,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
) -> None:
    """Re-derive one owned workout from an exact subject/connection raw row.

    ``identity`` authorizes the subject boundary but does not replace historical
    attribution: a newly recovered workout inherits ``raw_row.actor_user_id``.
    Retired connections are allowed here only because this API requires the
    caller to supply the exact root and the operation re-derives an already-owned
    historical payload; fresh sync remains forbidden on a retired root.
    """

    _validate_owned_scope(
        identity=identity,
        integration_connection_id=integration_connection_id,
    )
    if (
        not isinstance(raw_row, RawPayload)
        or not isinstance(raw_row.id, int)
        or isinstance(raw_row.id, bool)
    ):
        raise HevyRawPayloadInvariantError(
            "owned Hevy reparse requires a persisted raw payload"
        )
    raw_state = sa_inspect(raw_row)
    if not raw_state.persistent or raw_state.session is not session.sync_session:
        raise HevyRawPayloadInvariantError(
            "owned Hevy reparse rejects detached or forged raw payload state"
        )
    with session.no_autoflush:
        preliminary_raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_row.id)
            .execution_options(populate_existing=True)
        )
    if preliminary_raw is None:
        raise HevyRawPayloadInvariantError(
            "owned Hevy raw payload does not exist"
        )
    if preliminary_raw.subject_id != identity.subject_id:
        raise HevyRawPayloadInvariantError(
            "raw payload belongs to another subject"
        )
    if preliminary_raw.integration_connection_id != integration_connection_id:
        raise HevyRawPayloadInvariantError(
            "raw payload belongs to another integration connection"
        )
    await _lock_owned_hevy_scope(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_retired=True,
    )
    with session.no_autoflush:
        persisted_raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_row.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    if persisted_raw is None:
        raise HevyRawPayloadInvariantError(
            "owned Hevy raw payload does not exist"
        )
    raw_row = persisted_raw
    if raw_row.subject_id != identity.subject_id:
        raise HevyRawPayloadInvariantError(
            "raw payload changed subject ownership during reparse"
        )
    if raw_row.integration_connection_id != integration_connection_id:
        raise HevyRawPayloadInvariantError(
            "raw payload changed integration connection during reparse"
        )
    if not isinstance(raw_row.external_id, str) or not raw_row.external_id:
        raise HevyRawPayloadInvariantError(
            "owned Hevy raw payload requires an external id"
        )
    try:
        raw_identity = WriteIdentity(
            subject_id=raw_row.subject_id,
            actor_user_id=raw_row.actor_user_id,
        )
    except TypeError as exc:
        raise HevyRawPayloadInvariantError(
            "raw payload has invalid subject/actor attribution"
        ) from exc

    raw = _validate_owned_raw_payload(
        raw_row,
        identity=raw_identity,
        integration_connection_id=integration_connection_id,
        external_id=raw_row.external_id,
    )
    external_id = str(raw["id"]).strip()
    workout, adopt_legacy = await _resolve_owned_workout(
        session,
        identity=raw_identity,
        integration_connection_id=integration_connection_id,
        external_id=external_id,
    )
    await _upsert_owned_workout(
        session,
        raw_row=raw_row,
        identity=raw_identity,
        integration_connection_id=integration_connection_id,
        workout=workout,
        adopt_legacy=adopt_legacy,
    )


async def reparse_owned_pending(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    limit: int = raw_sweep.REPARSE_BATCH,
    since_days: int = raw_sweep.REPARSE_WINDOW_DAYS,
) -> int:
    """Sweep pending Hevy rows in one exact S/C scope. Does not commit.

    Each row uses a SAVEPOINT so a malformed payload cannot leave a partially
    rebuilt workout that the next ``has_normalized`` check would skip forever.
    """

    await _require_owned_hevy_connection(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_retired=True,
    )
    has_normalized = (
        select(HevyWorkout.id)
        .where(
            HevyWorkout.raw_payload_id == RawPayload.id,
            HevyWorkout.subject_id == identity.subject_id,
            HevyWorkout.integration_connection_id == integration_connection_id,
        )
        .exists()
    )
    cutoff = now_local() - timedelta(days=since_days)
    stmt = (
        select(RawPayload)
        .where(
            RawPayload.subject_id == identity.subject_id,
            RawPayload.integration_connection_id == integration_connection_id,
            RawPayload.domain == DOMAIN,
            RawPayload.source == Source.HEVY_API.value,
            RawPayload.processed_at.is_(None),
            RawPayload.fetched_at >= cutoff,
            ~has_normalized,
        )
        .order_by(RawPayload.id)
        .limit(limit)
    )
    rows = list(await session.scalars(stmt))
    done = 0
    for raw_row in rows:
        try:
            async with session.begin_nested():
                await reparse_owned_from_raw(
                    session,
                    raw_row,
                    identity=identity,
                    integration_connection_id=integration_connection_id,
                )
                raw_row.processed_at = now_local()
                await session.flush()
        except Exception:
            logger.warning(
                "owned Hevy re-parse failed for raw payload %s",
                raw_row.id,
                exc_info=True,
            )
            continue
        done += 1
    return done


__all__ = ["reparse_owned_from_raw", "reparse_owned_pending"]
