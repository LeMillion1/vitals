"""Generic and scheduled reparse orchestration for the raw data lake."""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.raw_payload import RawPayload
from vitals.utils.timeutils import now_local

logger = logging.getLogger(__name__)

REPARSE_WINDOW_DAYS = 14
REPARSE_BATCH = 20


async def sweep_domain(
    session: AsyncSession,
    *,
    domain: str,
    reparse: Callable[[AsyncSession, RawPayload], Awaitable[Any]],
    has_normalized: Any,
    limit: int = REPARSE_BATCH,
    since_days: int = REPARSE_WINDOW_DAYS,
) -> int:
    """Generic re-parse sweep, modeled on ``signals_service.reparse_unparsed`` —
    extended here so every domain can reuse the same query instead of each
    re-implementing it.

    Picks up to ``limit`` rows for ``domain`` still at ``processed_at IS NULL``
    (what :func:`upsert_owned_raw_payload` leaves behind whenever it refreshes a row)
    within ``since_days`` of ``fetched_at``, excluding rows that already have a
    normalized child. ``has_normalized`` is a caller-built ``EXISTS`` clause
    correlated to ``RawPayload.id`` (e.g. ``select(Model.id).where(Model.
    raw_payload_id == RawPayload.id).exists()``) — passed in rather than
    hard-coded so this function stays domain-agnostic; it never imports a
    domain's own models. ``reparse`` does the actual re-derivation, reusing
    that domain's existing ingest logic.

    Each row runs in a SAVEPOINT. A raising ``reparse`` call is rolled back,
    logged, and skipped so neither a failed PostgreSQL transaction nor a
    partially written child can abort or poison the rest of the batch.
    ``processed_at`` is stamped and flushed inside that SAVEPOINT only after
    ``reparse`` returns. The caller still owns the outer transaction and commit.
    """
    cutoff = now_local() - timedelta(days=since_days)
    stmt = (
        select(RawPayload)
        .where(
            RawPayload.domain == domain,
            RawPayload.processed_at.is_(None),
            RawPayload.fetched_at >= cutoff,
            ~has_normalized,
        )
        .order_by(RawPayload.id)
        .limit(limit)
    )
    done = 0
    for raw in (await session.execute(stmt)).scalars().all():
        try:
            async with session.begin_nested():
                await reparse(session, raw)
                raw.processed_at = now_local()
                await session.flush()
        except Exception:
            logger.warning("re-parse failed for %s raw payload %s", domain, raw.id, exc_info=True)
            continue
        done += 1
    return done


async def sweep_pending_job(session_factory, redis=None, *, subject_id: uuid.UUID) -> None:
    """Nightly sweep for garmin/hevy/labs/body_comp/genetics raw payloads pending a
    normalized row (registered in vitals/scheduler/jobs.py).

    Signals' own reparse instead piggybacks on the morning brief (see
    proactive/inbound.py, called from brief.py) because it has to finish before
    that message goes out. These five domains have no such deadline — they're
    fed by a periodic poll (garmin/hevy) or an owner import/upload
    (labs/body_comp/genetics), not a message that's about to be sent — so one
    shared nightly pass covers all of them instead of separate jobs. Each domain
    commits (and rolls back on failure) on its own so one domain's trouble can't
    lose or block another's completed work.
    """
    from vitals.enums import IntegrationProvider
    from vitals.services.garmin import raw_payloads as garmin_raw_payloads
    from vitals.services.hevy import raw_payloads as hevy_raw_payloads
    from vitals.services.labs import ingestion as lab_ingestion
    from vitals.services.conflicts import engine
    from vitals.services.body_scan.scans import reparse as body_scan_reparse
    from vitals.services.genetics import reparse as genetics_reparse
    from vitals.services.tenancy.ownership import resolve_subject_ownership_context
    from vitals.utils.timeutils import today_local

    async with session_factory() as session:

        async def _sweep_owned_garmin() -> int:
            ownership = await resolve_subject_ownership_context(
                session,
                subject_id=subject_id,
                required_connections=(IntegrationProvider.GARMIN,),
            )
            return await garmin_raw_payloads.reparse_owned_pending(
                session,
                identity=ownership.system_action(),
                integration_connection_id=ownership.connection_id(IntegrationProvider.GARMIN),
            )

        async def _sweep_owned_hevy() -> int:
            ownership = await resolve_subject_ownership_context(
                session,
                subject_id=subject_id,
                required_connections=(IntegrationProvider.HEVY,),
            )
            return await hevy_raw_payloads.reparse_owned_pending(
                session,
                identity=ownership.system_action(),
                integration_connection_id=ownership.connection_id(IntegrationProvider.HEVY),
            )

        async def _sweep_owned_labs() -> int:
            context = await engine.resolve_subject_conflict_write_context(
                session,
                subject_id=subject_id,
                evaluation_date=today_local(),
            )
            prepared = await engine.prepare_scoped_write(
                session,
                context=context,
            )
            return await lab_ingestion.reparse_owned_pending(
                session,
                identity=context.identity,
                prepared_conflict_write=prepared,
            )

        async def _sweep_owned_body_comp() -> int:
            context = await engine.resolve_subject_conflict_write_context(
                session,
                subject_id=subject_id,
                evaluation_date=today_local(),
            )
            return await body_scan_reparse.reparse_owned_pending(
                session,
                identity=context.identity,
            )

        async def _sweep_owned_genetics() -> int:
            context = await engine.resolve_subject_conflict_write_context(
                session,
                subject_id=subject_id,
                evaluation_date=today_local(),
            )
            prepared = await engine.prepare_scoped_write(
                session,
                context=context,
            )
            return await genetics_reparse.reparse_owned_pending(
                session,
                identity=context.identity,
                prepared_conflict_write=prepared,
            )

        for name, sweep in (
            ("garmin", _sweep_owned_garmin),
            ("hevy", _sweep_owned_hevy),
            ("labs", _sweep_owned_labs),
            ("body_comp", _sweep_owned_body_comp),
            ("genetics", _sweep_owned_genetics),
        ):
            try:
                await sweep()
                await session.commit()
            except Exception:
                logger.warning("raw payload sweep failed for domain %s", name, exc_info=True)
                await session.rollback()


__all__ = [
    "REPARSE_BATCH",
    "REPARSE_WINDOW_DAYS",
    "sweep_domain",
    "sweep_pending_job",
]
