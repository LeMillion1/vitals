"""Platform-funded AI control plane, hard quotas, and at-most-once dispatch.

Database rows contain only authorization/provenance/accounting metadata. Prompt,
response, provider secrets, and exception text exist only in short-lived in-memory
objects. Every mutating database function flushes; its caller owns commit.

Lock order is identity governance -> S -> A -> raw provenance -> platform root ->
platform quota -> subject quota -> invocation. No provider await is permitted
until the issuing start-dispatch transaction has committed.
"""

from __future__ import annotations


from vitals.persistence.rls import enter_platform_scope
from vitals.utils.timeutils import now_utc

from vitals.services.ai_gateway.contracts import (
    DISPATCHING_STALE_AFTER,
    PREPARED_STALE_AFTER,
)

from vitals.services.ai_gateway.reconciliation import (
    reconcile_stale_dispatches,
    reconcile_stale_reservations,
)


async def reconciliation_job(session_factory, redis=None) -> None:
    """Release abandoned reservations and close paid ambiguous dispatches.

    Each phase owns a short transaction so its governance/subject/root locks are
    released before the next population is scanned.  The job performs no provider
    I/O and stores no prompt, response, credential, or exception text.
    """

    del redis
    current = now_utc()
    async with session_factory() as session:
        # Provider invocations across every subject: this job acts for the
        # installation, not for a person.
        await enter_platform_scope(session)
        await reconcile_stale_reservations(
            session,
            stale_before=current - PREPARED_STALE_AFTER,
        )
        await session.commit()
    async with session_factory() as session:
        # Provider invocations across every subject: this job acts for the
        # installation, not for a person.
        await enter_platform_scope(session)
        await reconcile_stale_dispatches(
            session,
            stale_before=current - DISPATCHING_STALE_AFTER,
        )
        await session.commit()


__all__ = ["reconciliation_job"]
