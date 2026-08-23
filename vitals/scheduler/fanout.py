"""Run one scheduled job once per health subject.

Every job used to arrive at ``resolve_legacy_ownership_context`` with no actor
and no subject, which meant "the sole subject, or refuse". That was the honest
answer while it was true — nothing named whose record was meant — and it stopped
being an answer the moment a second person existed: the digest, the reminders,
the nudges and the sweeps all failed closed on a two-person installation, which
is the whole background half of the product.

The fix is not to loosen the count. It is for the caller to say which record it
is acting on, and a scheduled job is in a position to say it once per subject.

**One subject's failure is not the others'.** A job that raises for one record is
logged and the fan-out continues; the last error is re-raised at the end so the
scheduler still sees the tick as failed and raises its alert. Stopping at the
first would let one broken record silence everybody else's digest, which is the
failure mode that is hardest to notice — nothing is wrong on screen, a report
simply never arrives.
"""

from __future__ import annotations

import functools
import logging
import uuid
from typing import Any, Awaitable, Callable, Optional, Sequence

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

#: A job that knows whose record it is working on.
SubjectJobFunc = Callable[..., Awaitable[Any]]


async def list_subject_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> Sequence[uuid.UUID]:
    """Every health subject, oldest first.

    Ordered so a fan-out is reproducible across ticks: an unordered scan makes
    "which record did this run for first" unanswerable from the logs, and that
    is the question asked when one of them misbehaves.
    """

    from vitals.models.identity import HealthSubject

    async with session_factory() as session:
        return list(
            await session.scalars(
                select(HealthSubject.id).order_by(HealthSubject.id)
            )
        )


def for_each_subject(job: SubjectJobFunc, *, job_id: str) -> Callable[..., Awaitable[None]]:
    """Adapt a subject-aware job into the scheduler's two-argument shape.

    The returned callable is what ``register_job`` stores. It enumerates the
    subjects itself rather than being handed them, because the set changes
    between ticks and a list captured at registration would go stale the first
    time somebody joined.
    """

    # ``functools.wraps`` on purpose, not for tidiness: the scheduler's scope
    # inventory (tests/test_scheduled_job_scope.py) reads ``__module__`` and
    # ``__name__`` off the registered callable to find the job's source and check
    # it reaches a resolver. Wrapping without this would point it at this file,
    # where it would find nothing, and the inventory would go quietly blind — the
    # exact failure that inventory exists to catch. ``__wrapped__`` keeps the
    # fan-out itself discoverable.
    @functools.wraps(job)
    async def _run(
        session_factory: async_sessionmaker[AsyncSession],
        redis: Optional[Redis] = None,
    ) -> None:
        subject_ids = await list_subject_ids(session_factory)
        if not subject_ids:
            logger.info("%s: no health subjects, nothing to run", job_id)
            return

        failures: list[tuple[uuid.UUID, BaseException]] = []
        for subject_id in subject_ids:
            try:
                await job(session_factory, redis, subject_id=subject_id)
            except Exception as exc:  # noqa: BLE001 — see the module docstring
                logger.exception(
                    "%s failed for subject %s; continuing with the rest",
                    job_id,
                    subject_id,
                )
                failures.append((subject_id, exc))

        if failures:
            logger.error(
                "%s failed for %d of %d subjects",
                job_id,
                len(failures),
                len(subject_ids),
            )
            # The scheduler's own alerting keys off the exception, so the tick
            # has to end as a failure. The last one carries the traceback that
            # is already in the log above it.
            raise failures[-1][1]

    return _run
