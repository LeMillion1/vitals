"""A scheduled job runs once per record, and one bad record does not silence the rest.

Every job used to arrive at the ownership resolver with neither an actor nor a
subject, which means "the sole subject, or refuse". On a two-person installation
that refused, so the digest, the reminders and the nightly sweeps all stopped —
the background half of the product, fail-closed and therefore invisible on
screen. It was found by reading the log of a running installation with ten
patients in it.

The fix is not a loosened count. The caller says whose record it is acting on,
and a scheduler is in a position to say it once per record.

Five jobs moved first. The other eight could not, for two reasons that have both
since gone: the proactive pair sent to one Telegram bot token and chat id, and
the four provider jobs signed in to one Garmin or Hevy account from the
environment — fanning those out would have turned a visible outage into a
disclosure. The transport was removed and ``integration_credentials`` gave each
account its own credential, so all eight are fanned out now, the provider four
per *connection* rather than per subject: somebody who has not connected a watch
has nothing for them to do.
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

    await for_each_subject(job, job_id="weekly_digest")(session_factory, None)

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
        await for_each_subject(job, job_id="weekly_digest")(session_factory, None)

    assert len(seen) == 2
    assert broken_id in seen


async def test_an_empty_installation_is_not_a_failure(session_factory):
    """No subjects is nothing to do, not something to alert about."""

    ran = []

    async def job(session_factory, redis=None, *, subject_id):
        ran.append(subject_id)

    await for_each_subject(job, job_id="weekly_digest")(session_factory, None)
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


def test_every_job_about_a_record_runs_once_per_record():
    """The inverse of what this test used to assert, and the reason it changed.

    Eight jobs used to be pinned here as deliberately *not* fanned out, in two
    groups. The provider syncs signed in to one Garmin or Hevy account from the
    environment; the proactive pair sent to one Telegram bot token and chat id.
    Either way the credential was one set for the whole process, so running them
    once per subject would have delivered ten people's data to one person's
    watch or one person's chat — strictly worse than the fail-closed outage they
    were instead.

    Both reasons are gone. The transport was removed outright, and
    ``integration_credentials`` gave each account its own credential, token
    store, session cache and login breaker. So the list this test keeps is the
    other one: every job that is *about a record* has to run once per record,
    and a job that quietly stops being fanned out is a job that silently serves
    one person on an installation holding ten.

    The six platform jobs are the exception and are named, not defaulted: they
    are about the installation's own state — sweeping unprocessed payloads,
    purging expired links, reconciling provider invocations and delivery state,
    and dispatching generic care wakeups — and have no subject to run for.
    """

    from vitals.scheduler.scheduler import _registry, clear_jobs
    from vitals.scheduler import jobs as job_module

    clear_jobs()
    try:
        job_module.register_all_jobs(None)
        registered = set(_registry)
        fanned = {
            job_id
            for job_id, spec in _registry.items()
            if getattr(spec.func, "__wrapped__", None) is not None
        }
    finally:
        clear_jobs()

    about_the_installation = {
        "share_purge",
        "ai_invocation_reconcile",
        "notification_delivery_reconcile",
        "care_push_dispatch",
        "registration_admission_retention",
    }
    assert about_the_installation <= registered
    assert not (fanned & about_the_installation)

    # Everything else. Named rather than derived, so adding a job forces the
    # decision instead of inheriting whichever answer the expression gives.
    assert {
        "weekly_digest", "hrt_reminders", "glp1_plateau", "nutrition_day_end",
        "raw_payload_sweep", "daily_brief", "nudges",
        "garmin_sync", "garmin_pulse", "garmin_weight_export", "hevy_sync",
    } <= fanned
    assert registered - about_the_installation == fanned


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

    await for_each_subject(job, job_id="weekly_digest")(session_factory, None)

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

    await for_each_subject(job, job_id="weekly_digest")(session_factory, None)
    assert broken_id in seen


# ── Per connection, not per subject ──────────────────────────────────────────


async def test_provider_discovery_explicitly_enters_platform_scope(
    session_factory, monkeypatch
):
    """An unbound runtime session sees zero FORCE-RLS connection rows."""

    from vitals.enums import IntegrationProvider
    from vitals.persistence.rls import in_platform_scope
    from vitals.scheduler import fanout
    from vitals.services import provider_credentials_service

    observed = False

    async def _list(session, *, provider):
        nonlocal observed
        assert provider is IntegrationProvider.GARMIN
        observed = in_platform_scope(session)
        return []

    monkeypatch.setattr(provider_credentials_service, "list_live_account_refs", _list)

    async def job(_factory, _redis, *, subject_id, integration_connection_id):
        raise AssertionError((subject_id, integration_connection_id))

    await fanout.for_each_connection(
        job,
        job_id="garmin_sync",
        provider=IntegrationProvider.GARMIN,
    )(session_factory)

    assert observed


async def _connected(session, subject_id, *, provider):
    from vitals.enums import IntegrationProvider
    from vitals.services import provider_credentials_service
    from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots

    await bootstrap_legacy_resource_roots(session, subject_id=subject_id)
    if provider is IntegrationProvider.GARMIN:
        await provider_credentials_service.set_garmin_credentials(
            session, subject_id=subject_id, email="a@example.test", password="x"
        )
    else:
        await provider_credentials_service.set_hevy_credentials(
            session, subject_id=subject_id, api_key="k"
        )


async def test_a_provider_job_runs_once_per_connected_account(
    session_factory, db_session, legacy_owner_roots, garmin_connected
):
    """The four jobs that could not be fanned out until the credentials moved."""

    from vitals.enums import IntegrationProvider
    from vitals.scheduler.fanout import for_each_connection

    other = await _subject(db_session, "second-athlete")
    await _connected(db_session, other.id, provider=IntegrationProvider.GARMIN)
    await db_session.commit()

    seen: list[uuid.UUID] = []

    async def job(_factory, _redis, *, subject_id, integration_connection_id):
        del integration_connection_id
        seen.append(subject_id)

    await for_each_connection(
        job, job_id="garmin_sync", provider=IntegrationProvider.GARMIN
    )(session_factory)

    assert sorted(map(str, seen)) == sorted(
        map(str, [legacy_owner_roots.subject_id, other.id])
    )


async def test_a_subject_who_connected_nothing_is_absent_rather_than_failing(
    session_factory, db_session, legacy_owner_roots, garmin_connected
):
    """Not an outage to report: they have not connected a watch.

    Enumerating them would mean four scheduled no-ops a day per person, and a
    failure alert for each would be an alert about nothing.
    """

    from vitals.enums import IntegrationProvider
    from vitals.scheduler.fanout import for_each_connection

    bare = await _subject(db_session, "no-watch")
    from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots

    await bootstrap_legacy_resource_roots(db_session, subject_id=bare.id)
    await db_session.commit()

    seen: list[uuid.UUID] = []

    async def job(_factory, _redis, *, subject_id, integration_connection_id):
        del integration_connection_id
        seen.append(subject_id)

    await for_each_connection(
        job, job_id="garmin_sync", provider=IntegrationProvider.GARMIN
    )(session_factory)

    assert seen == [legacy_owner_roots.subject_id]


async def test_one_throttled_account_does_not_stop_the_next(
    session_factory, db_session, legacy_owner_roots, garmin_connected
):
    """The failure the shared login breaker used to cause, from the other end.

    Three failed logins used to pause every account for six hours because the
    counters were one flat Redis key. They are per connection now, and so is
    this: one account's refusal must not end the tick before the next account
    has been tried.
    """

    from vitals.enums import IntegrationProvider
    from vitals.scheduler.fanout import for_each_connection

    other = await _subject(db_session, "second-athlete")
    await _connected(db_session, other.id, provider=IntegrationProvider.GARMIN)
    await db_session.commit()

    seen: list[uuid.UUID] = []

    async def job(_factory, _redis, *, subject_id, integration_connection_id):
        del integration_connection_id
        seen.append(subject_id)
        if subject_id == legacy_owner_roots.subject_id:
            raise RuntimeError("login throttled")

    with pytest.raises(RuntimeError):
        await for_each_connection(
            job, job_id="garmin_sync", provider=IntegrationProvider.GARMIN
        )(session_factory)

    assert len(seen) == 2, "the second account must still have been tried"


@pytest.fixture
def unbound_session_factory(session_factory, db_session):
    """A factory whose sessions arrive unbound, as production's do.

    The suite's factory hands out one shared session, and row security binds it
    to the first subject that uses it and then refuses to move — deliberately,
    because one transaction serves one person. Production gives every job run
    and every outcome record its own session, so the fan-out crosses subjects
    without ever rebinding one. Clearing the binding on entry is what a new
    session does for free.
    """

    from vitals.persistence import rls as rls_session

    class _CM:
        async def __aenter__(self):
            db_session.info.pop(rls_session._SUBJECT_KEY, None)
            return db_session

        async def __aexit__(self, *_):
            return None

    class _Factory:
        def __call__(self):
            return _CM()

    del session_factory
    return _Factory()


async def test_a_failure_is_filed_against_the_record_it_happened_to(
    unbound_session_factory, db_session, legacy_owner_roots, garmin_connected
):
    """It used to be filed against whoever the sole-owner resolver returned.

    Which on a one-person installation was right by accident, and on a
    two-person one was a refusal the handler swallowed — so a failing sync
    raised no alert at all.
    """

    from sqlalchemy import select

    from vitals.enums import IntegrationProvider
    from vitals.models.system_alert import SystemAlert
    from vitals.scheduler.fanout import for_each_connection

    other = await _subject(db_session, "second-athlete")
    await _connected(db_session, other.id, provider=IntegrationProvider.GARMIN)
    await db_session.commit()

    async def job(_factory, _redis, *, subject_id, integration_connection_id):
        del integration_connection_id
        if subject_id == other.id:
            raise RuntimeError("only this record is broken")

    with pytest.raises(RuntimeError):
        await for_each_connection(
            job, job_id="garmin_sync", provider=IntegrationProvider.GARMIN
        )(unbound_session_factory)

    from vitals.persistence import rls as rls_session

    db_session.info.pop(rls_session._SUBJECT_KEY, None)
    rows = list(
        await db_session.scalars(
            select(SystemAlert).where(
                SystemAlert.alert_key == "scheduler.job_failed:garmin_sync",
                SystemAlert.resolved_at.is_(None),
            )
        )
    )
    assert [row.subject_id for row in rows] == [other.id]
