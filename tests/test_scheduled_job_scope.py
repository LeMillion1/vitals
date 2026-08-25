"""Every scheduled job says whose data it reads, and the list is the review.

Row security made an old failure mode much quieter. A job that does not bind a
subject used to read across everybody by accident; under the policies it reads
*nothing* — and then finishes, commits, and reports success. The scheduler goes
green, the heartbeat keeps beating, and reservations quietly stop being
released.

That is worse than a crash and it is invisible in a test suite that builds its
schema with ``create_all``. So the guard here is not behavioural, it is an
inventory: each registered job is listed below as one of two kinds, and a new
one fails this test until somebody decides which it is.

The two kinds are not interchangeable. ``BINDS_A_SUBJECT`` is the normal case —
the job resolves whose installation it is and binds, so the policies apply to it
exactly as they do to a request. ``ACTS_FOR_THE_INSTALLATION`` is for the few
whose work genuinely has no subject, and they pay for it by being counted: the
same functions appear in the ``enter_platform_scope`` allowlist in
``test_row_level_security.py``, and both lists have to agree.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

#: Jobs that resolve a subject and bind it, so row security applies to them the
#: same way it applies to a request. The value is the resolver they reach.
#:
#: Every one of them is now on the ``resolve_subject_*`` family, which is the
#: system path: the scheduler fans the job out and each run names the record it
#: is for. The subject is mandatory there, and that is the difference that
#: matters — the ``legacy`` family asks for "the sole subject or refuse", and a
#: job asking that refused outright the moment a second person existed.
#:
#: Four of these were the last holdouts, and their reason is worth keeping: the
#: provider syncs signed in to one Garmin or Hevy account from the environment,
#: and the proactive pair sent to one Telegram bot token and chat id. Fanning
#: them out then would have turned a visible outage into a disclosure. The
#: transport was removed and ``integration_credentials`` gave each account its
#: own credential, so both reasons are gone.
BINDS_A_SUBJECT = {
    # A write into a domain resolves the conflict-write context, which resolves
    # the ownership context, which binds.
    "glp1_plateau": "resolve_subject_conflict_write_context",
    "hrt_reminders": "resolve_subject_conflict_write_context",
    "nutrition_day_end": "resolve_subject_conflict_write_context",
    # Provider syncs resolve the owner before touching the connection, once per
    # connected account.
    "hevy_sync": "resolve_subject_ownership_context",
    "garmin_sync": "resolve_subject_ownership_context",
    "garmin_pulse": "resolve_subject_ownership_context",
    "garmin_weight_export": "resolve_subject_ownership_context",
    # The proactive family resolves the channel owner: a message needs somebody
    # to send it to before it needs anything to say.
    "daily_brief": "resolve_subject_channel_ownership",
    "nudges": "resolve_subject_channel_ownership",
    "weekly_digest": "prepare_subject_digest_owner",
    # Scans unprocessed payloads and then binds per domain before touching
    # anything owned — the outer scan reads a table the sweep itself stamped.
    "raw_payload_sweep": "resolve_subject_ownership_context",
}

#: Jobs whose work belongs to the installation and has no subject to bind. Each
#: sweeps across everybody on a schedule; under the subject comparison alone
#: each would read nothing and report success.
ACTS_FOR_THE_INSTALLATION = {
    "share_purge": "expired shares, across every subject",
    "ai_invocation_reconcile": "stale provider reservations, across every subject",
    "notification_delivery_reconcile": "stalled deliveries, across every subject",
    "care_push_dispatch": "generic care wakeups, across every subject",
}


def _registered_jobs():
    from vitals.scheduler import jobs as job_module
    from vitals.scheduler.scheduler import _registry, clear_jobs

    clear_jobs()
    try:
        job_module.register_all_jobs(None)
        return {
            job_id: (spec.func.__module__, spec.func.__name__)
            for job_id, spec in _registry.items()
        }
    finally:
        clear_jobs()


def test_every_scheduled_job_is_classified():
    """A new job is a decision about scope, so it stops here until somebody makes it."""

    registered = set(_registered_jobs())
    classified = set(BINDS_A_SUBJECT) | set(ACTS_FOR_THE_INSTALLATION)

    unclassified = sorted(registered - classified)
    assert not unclassified, (
        f"scheduled without saying whose data they read: {unclassified} — add each "
        "to BINDS_A_SUBJECT (the normal case: resolve a subject and bind) or to "
        "ACTS_FOR_THE_INSTALLATION (no subject exists, and enter_platform_scope "
        "says so). Under row security an unclassified job most likely reads "
        "nothing and reports success"
    )
    stale = sorted(classified - registered)
    assert not stale, f"listed here but no longer registered: {stale}"

    overlap = sorted(set(BINDS_A_SUBJECT) & set(ACTS_FOR_THE_INSTALLATION))
    assert not overlap, f"a job is one or the other, not both: {overlap}"


def test_the_platform_jobs_are_the_ones_that_enter_the_platform_scope():
    """Two lists, written for different readers, that must describe one reality.

    This one is about jobs; the allowlist in ``test_row_level_security.py`` is
    about the scope itself and also covers the two request paths a visitor takes.
    A job in one and not the other means somebody classified it here and never
    wired it — which under the policies is the silent-success failure again.
    """

    source = (REPOSITORY_ROOT / "tests" / "test_row_level_security.py").read_text()
    tree = ast.parse(source)
    allowlisted: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Tuple)
            and len(node.elts) == 2
            and all(isinstance(e, ast.Constant) for e in node.elts)
            and isinstance(node.elts[0].value, str)
            and node.elts[0].value.endswith(".py")
        ):
            allowlisted.add(node.elts[1].value)

    registered = _registered_jobs()
    for job_id in ACTS_FOR_THE_INSTALLATION:
        _, function_name = registered[job_id]
        assert function_name in allowlisted, (
            f"{job_id} is classified as the installation's work but "
            f"{function_name} is not in the enter_platform_scope allowlist — it "
            "reads nothing under row security"
        )


@pytest.mark.parametrize("job_id", sorted(BINDS_A_SUBJECT))
def test_a_binding_job_reaches_its_resolver(job_id):
    """Cheap structural check: the named resolver is reachable from the module.

    Deliberately shallow — it proves the wiring exists, not that every branch
    takes it. What it catches is a job that was classified as binding and then
    written without ever resolving anybody, which is the case that fails silently.
    """

    import importlib

    module_name, function_name = _registered_jobs()[job_id]
    module = importlib.import_module(module_name)
    source = Path(module.__file__).read_text()
    assert BINDS_A_SUBJECT[job_id] in source, (
        f"{job_id} is classified as binding a subject, but {module_name} never "
        f"mentions {BINDS_A_SUBJECT[job_id]}"
    )
    assert f"def {function_name}(" in source
