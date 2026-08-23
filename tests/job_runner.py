"""Run a scheduled job the way the scheduler runs it.

A subject-scoped job takes a mandatory ``subject_id``: it always says whose
record it is working on, because a job that could leave that out is how the
whole background half of the product stopped once a second person existed.

Tests used to call these jobs with nothing but a session factory, which worked
only because the resolver would fall back to "the sole subject". That fallback
is gone from the job path. This helper puts the test back on the same footing as
production — enumerate the subjects, run once for each — rather than inventing a
second way in that only tests use.

In a one-subject fixture it runs exactly once, which is what almost every caller
here wants. In a two-subject one it runs twice, which is usually what the test
should have been asserting anyway.
"""

from __future__ import annotations

from typing import Any

from vitals.scheduler.fanout import list_subject_ids


async def run_job_for_every_subject(job, session_factory, redis=None, **kwargs: Any):
    """Call ``job`` once per health subject, forwarding any extra arguments.

    Returns the list of whatever the job returned, in subject order, so a caller
    that cares about one subject's result can still read it.
    """

    results = []
    for subject_id in await list_subject_ids(session_factory):
        results.append(
            await job(session_factory, redis, subject_id=subject_id, **kwargs)
        )
    return results
