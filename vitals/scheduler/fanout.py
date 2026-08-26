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

from vitals.utils.timeutils import subject_timezone

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

    return [subject_id for subject_id, _zone in await _list_subjects(session_factory)]


async def _list_subjects(
    session_factory: async_sessionmaker[AsyncSession],
) -> Sequence[tuple[uuid.UUID, str | None]]:
    """Each subject with the zone their days are measured in."""

    from vitals.models.identity import HealthSubject

    async with session_factory() as session:
        rows = await session.execute(
            select(HealthSubject.id, HealthSubject.timezone).order_by(
                HealthSubject.id
            )
        )
        return [(row[0], row[1]) for row in rows]


async def _record_outcome_for(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: str,
    subject_id: uuid.UUID,
    error: BaseException | None,
) -> None:
    """Raise or clear this record's failure alert for this job.

    The shared runner records one outcome per *tick*, and until every job about
    a record was fanned out that was the same thing. It is not any more: a job
    that failed for one person and succeeded for nine would raise one alert
    against whichever subject the sole-owner resolver happened to return — and
    on an installation with two people that resolver refuses, so the alert never
    appeared at all and the failure was a log line.

    Best-effort by construction, exactly as the runner's version is: alert
    bookkeeping must never turn a job that reported a failure into a job that
    crashed reporting it.
    """

    from vitals.scheduler.scheduler import record_subject_job_outcome

    try:
        await record_subject_job_outcome(
            session_factory,
            job_id,
            str(error) if error is not None else None,
            subject_id=subject_id,
        )
    except Exception:  # noqa: BLE001
        # Including an unclassified job id, which ``_make_runner`` refuses at
        # registration — so reaching it here means something built a fan-out
        # without one, and that is worth a log rather than a dead tick.
        logger.exception(
            "Could not record the outcome of %s for subject %s", job_id, subject_id
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
        subjects = await _list_subjects(session_factory)
        if not subjects:
            logger.info("%s: no health subjects, nothing to run", job_id)
            return
        subject_ids = [subject_id for subject_id, _zone in subjects]

        failures: list[tuple[uuid.UUID, BaseException]] = []
        for subject_id, zone in subjects:
            try:
                # Their clock, not the installation's. A job that closes a day
                # or reads "today" is answering a question about this person,
                # and the answer moves with where they are — see
                # vitals/utils/timeutils.subject_timezone.
                with subject_timezone(zone):
                    await job(session_factory, redis, subject_id=subject_id)
            except Exception as exc:  # noqa: BLE001 — see the module docstring
                logger.exception(
                    "%s failed for subject %s; continuing with the rest",
                    job_id,
                    subject_id,
                )
                failures.append((subject_id, exc))
                await _record_outcome_for(
                    session_factory,
                    job_id=job_id,
                    subject_id=subject_id,
                    error=exc,
                )
            else:
                await _record_outcome_for(
                    session_factory,
                    job_id=job_id,
                    subject_id=subject_id,
                    error=None,
                )

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


async def _list_provider_accounts(
    session_factory: async_sessionmaker[AsyncSession],
    provider,
) -> Sequence[Any]:
    """Every account with this provider that has something to sign in with.

    A subject with a connection root and no credential is absent rather than
    present and failing: they have not connected a watch, which is an ordinary
    state and not an outage to report.
    """

    from vitals.persistence.rls import enter_platform_scope
    from vitals.services import provider_credentials_service

    async with session_factory() as session:
        # ``integration_connections`` is FORCE-RLS protected. A scheduler tick
        # has no human subject to bind while it discovers which accounts need
        # work, so an unbound runtime session correctly sees no rows. Open the
        # installation scope only for this short enumeration transaction; each
        # account is processed later by a fresh subject-bound job session.
        await enter_platform_scope(session)
        return await provider_credentials_service.list_live_account_refs(
            session, provider=provider
        )


async def _subject_zone(
    session_factory: async_sessionmaker[AsyncSession],
    subject_id: uuid.UUID,
) -> str | None:
    """Their clock, for a job that closes a day or asks what "today" is."""

    from vitals.models.identity import HealthSubject

    async with session_factory() as session:
        return await session.scalar(
            select(HealthSubject.timezone).where(HealthSubject.id == subject_id)
        )


def for_each_connection(
    job: SubjectJobFunc, *, job_id: str, provider
) -> Callable[..., Awaitable[None]]:
    """Run one provider job once per account, not once per installation.

    The four provider jobs were the last that could not be fanned out, and the
    reason was never the scheduler: their credentials were one Garmin login and
    one Hevy key for the whole process, so running them per subject would have
    filed the operator's own watch data as everybody else's. That is fixed —
    ``provider_credentials_service`` resolves a connection into the account it
    belongs to — and this is the other half.

    **Per account rather than per subject**, unlike :func:`for_each_subject`.
    They are the same set today, because a subject has at most one live
    connection per provider, and they are not the same question: a subject with
    no watch has nothing for these jobs to do, and enumerating them would mean
    four scheduled no-ops a day per person who has not connected one. It also
    leaves room for the second account a person is eventually allowed to have.

    **One subject's failure is not the others'**, exactly as in the subject
    fan-out: a Garmin login that has been throttled for one account must not
    stop the next account's sync, which is precisely the failure the shared
    login breaker used to cause and that the per-connection keys fixed.
    """

    @functools.wraps(job)
    async def _run(
        session_factory: async_sessionmaker[AsyncSession],
        redis: Optional[Redis] = None,
    ) -> None:
        accounts = await _list_provider_accounts(session_factory, provider)
        if not accounts:
            logger.info(
                "%s: no configured %s account, nothing to run",
                job_id,
                getattr(provider, "value", provider),
            )
            return

        failures: list[tuple[uuid.UUID, BaseException]] = []
        for account in accounts:
            zone = await _subject_zone(session_factory, account.subject_id)
            try:
                with subject_timezone(zone):
                    await job(
                        session_factory,
                        redis,
                        subject_id=account.subject_id,
                        integration_connection_id=account.integration_connection_id,
                    )
            except Exception as exc:  # noqa: BLE001 — see the module docstring
                logger.exception(
                    "%s failed for subject %s; continuing with the rest",
                    job_id,
                    account.subject_id,
                )
                failures.append((account.subject_id, exc))
                await _record_outcome_for(
                    session_factory,
                    job_id=job_id,
                    subject_id=account.subject_id,
                    error=exc,
                )
            else:
                await _record_outcome_for(
                    session_factory,
                    job_id=job_id,
                    subject_id=account.subject_id,
                    error=None,
                )

        if failures:
            logger.error(
                "%s failed for %d of %d %s accounts",
                job_id,
                len(failures),
                len(accounts),
                getattr(provider, "value", provider),
            )
            raise failures[-1][1]

    return _run
