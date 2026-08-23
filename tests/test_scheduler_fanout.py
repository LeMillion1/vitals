"""A scheduled job runs once per subject, and one bad record does not silence the rest.

Every job used to arrive at the ownership resolver with neither an actor nor a
subject, which means "the sole subject, or refuse". On a two-person installation
that refused, so the digest, the reminders and the nightly sweeps all stopped —
the background half of the product, fail-closed and therefore invisible on
screen. It was found by reading the log of a running installation with ten
patients in it.

The fix is not a loosened count. The caller says whose record it is acting on,
and a scheduler is in a position to say it once per subject.

Five jobs moved. Eight did not, and the last test here is about them: they end
in a send — to one Garmin account, or to one Telegram chat — and the credential
for it is one set for the whole process. Fanning those out would turn an outage
into a disclosure.
"""

from __future__ import annotations

import uuid

import pytest

from vitals.enums import UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.scheduler.fanout import for_each_subject, list_subject_ids


async def _subject(session, slug: str) -> HealthSubject:
    owner = User(
        username=slug,
        normalized_username=slug,
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name=f"Subject {slug}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return subject


async def test_the_job_runs_once_for_every_subject(
    session_factory, db_session, legacy_owner_roots
):
    await _subject(db_session, "fanout-second")
    await _subject(db_session, "fanout-third")
    await db_session.commit()

    seen: list[uuid.UUID] = []

    async def job(session_factory, redis=None, *, subject_id):
        seen.append(subject_id)

    await for_each_subject(job, job_id="probe")(session_factory, None)

    expected = await list_subject_ids(session_factory)
    assert seen == list(expected)
    assert len(seen) == 3


async def test_one_failing_subject_does_not_stop_the_others(
    session_factory, db_session, legacy_owner_roots
):
    """The failure mode this is really about.

    Stopping at the first error would let one broken record silence everybody
    else's digest — and silence is the hardest outcome to notice, because
    nothing is wrong on any screen; a report simply never arrives. So the
    fan-out carries on and still ends the tick as a failure, which is what the
    scheduler's own alerting keys off.
    """

    second = await _subject(db_session, "fanout-broken")
    await db_session.commit()
    broken_id = second.id

    seen: list[uuid.UUID] = []

    async def job(session_factory, redis=None, *, subject_id):
        seen.append(subject_id)
        if subject_id == broken_id:
            raise RuntimeError("this record is broken")

    with pytest.raises(RuntimeError, match="this record is broken"):
        await for_each_subject(job, job_id="probe")(session_factory, None)

    assert len(seen) == 2
    assert broken_id in seen


async def test_an_empty_installation_is_not_a_failure(session_factory):
    """No subjects is nothing to do, not something to alert about."""

    ran = []

    async def job(session_factory, redis=None, *, subject_id):
        ran.append(subject_id)

    await for_each_subject(job, job_id="probe")(session_factory, None)
    assert ran == []


def test_the_wrapper_keeps_the_job_findable():
    """The scope inventory reads the registered callable's own identity.

    ``tests/test_scheduled_job_scope.py`` finds each job's source through
    ``__module__``/``__name__`` and checks it reaches a resolver. A wrapper that
    replaced those would point it at the fan-out module, where it would find
    nothing — and an inventory that silently matches nothing is the failure that
    inventory exists to catch.
    """

    from vitals.services.digest_service import digest_job

    wrapped = for_each_subject(digest_job, job_id="weekly_digest")
    assert wrapped.__module__ == digest_job.__module__
    assert wrapped.__name__ == digest_job.__name__
    assert wrapped.__wrapped__ is digest_job


def test_the_credential_bound_jobs_are_not_fanned_out():
    """Eight jobs deliberately still refuse, and the reason is worth pinning.

    Eight, in two groups. The provider syncs sign in to one Garmin or Hevy
    account; the proactive four send to one Telegram bot token and chat id.
    Either way the credential is one set for the whole process, so running them
    once per subject would deliver ten people's data to one person's watch or
    one person's chat — a cross-subject send, strictly worse than the
    fail-closed outage they are today.

    The proactive four are the ones this test is really for. They read the lake
    and look exactly like the jobs that were ported; the difference is the last
    step, where they send.
    """

    from vitals.scheduler.scheduler import _registry, clear_jobs
    from vitals.scheduler import jobs as job_module

    clear_jobs()
    try:
        job_module.register_all_jobs(None)
        fanned = {
            job_id
            for job_id, spec in _registry.items()
            if getattr(spec.func, "__wrapped__", None) is not None
        }
    finally:
        clear_jobs()

    external = {
        # One watch, one Hevy account.
        "hevy_sync", "garmin_sync", "garmin_weight_export", "garmin_pulse",
        # One Telegram bot token and one chat id. These read the lake and then
        # *send*, and the send is where the single credential is — which is why
        # they look like lake work and are not.
        "daily_brief", "evening_block", "nudges",
    }
    assert not (fanned & external), (
        "these run against one process-wide credential and must not be fanned "
        f"out per subject: {sorted(fanned & external)}"
    )
    # And the ones that were ported stayed ported.
    assert {
        "weekly_digest", "hrt_reminders", "glp1_plateau", "nutrition_day_end",
        "raw_payload_sweep",
    } <= fanned


async def test_each_subject_is_run_on_their_own_clock(
    session_factory, db_session, legacy_owner_roots
):
    """A day is closed where the person is, not where the server is.

    ``health_subjects.timezone`` has always held the real answer and nothing
    read it: "today" came from ``VITALS_TIMEZONE``, which was the installation's
    and, while an installation was one person, also theirs. Fanned out over ten
    people it stopped being theirs — and ``nutrition_day_end`` exists precisely
    to run once a day's totals are final, so on the wrong clock it finalises a
    day still in progress.
    """

    from vitals.utils.timeutils import now_local

    far_east = await _subject(db_session, "fanout-kiritimati")
    far_east.timezone = "Pacific/Kiritimati"  # UTC+14, the earliest there is
    far_west = await _subject(db_session, "fanout-midway")
    far_west.timezone = "Pacific/Midway"  # UTC-11, the latest
    await db_session.commit()
    east_id, west_id = far_east.id, far_west.id

    seen: dict[uuid.UUID, object] = {}

    async def job(session_factory, redis=None, *, subject_id):
        seen[subject_id] = now_local()

    await for_each_subject(job, job_id="probe")(session_factory, None)

    # Twenty-five hours apart, so the two never share a wall-clock reading and
    # very often do not share a date either.
    assert seen[east_id] > seen[west_id]
    assert (seen[east_id] - seen[west_id]).total_seconds() > 24 * 3600


async def test_an_unusable_zone_does_not_take_the_tick_down(
    session_factory, db_session, legacy_owner_roots
):
    """One malformed row is not a reason for nobody to get their digest."""

    from vitals.utils.timeutils import now_local

    broken = await _subject(db_session, "fanout-bad-zone")
    broken.timezone = "Not/AZone"
    await db_session.commit()
    broken_id = broken.id

    seen: dict[uuid.UUID, object] = {}

    async def job(session_factory, redis=None, *, subject_id):
        seen[subject_id] = now_local()

    await for_each_subject(job, job_id="probe")(session_factory, None)
    assert broken_id in seen
