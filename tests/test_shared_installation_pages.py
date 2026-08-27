"""Every page answers something in a shared installation. None of them crash.

The rest of the web suite runs against a database holding exactly one health
subject, so it cannot see this class of defect at all. Opening a browser on a
two-subject installation found, in about a minute, that the app would not start;
then that every page answered 409; then that four pages answered 500 with a
stack trace; then two more behind those. Every one of them was invisible to
several thousand passing tests.

This is that minute, automated. It does not assert that a page *works* — most of
them still refuse, and refusing is correct while the migration is unfinished.
It asserts the weaker thing that has to be true the whole way through: a refusal
is an answer, not a crash. A 500 here means a sole-subject bridge declined and
nobody caught it, and whoever meets it goes looking for a bug that is not there.

As pages are ported, the expected-refusal list below shrinks. It is the porting
backlog, and it is checked in both directions: a page that starts working while
still listed fails too, so the list cannot quietly go stale.
"""

from __future__ import annotations

from vitals.services.digest.projection import assembly as digest_projection

from datetime import date

import pytest

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserRoleName,
    UserStatus,
)
from vitals.models.hevy import HevyWorkout
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.tenancy import IntegrationConnection
from vitals.models.weight import WeightLog


#: What still reaches a sole-subject compatibility bridge. Full portability-v1
#: export is now authorized as an installation operation before its format
#: check, so ordinary record owners receive the operator-policy answer instead
#: of entering this backlog.
STILL_SOLE_SUBJECT: set[str] = set()

#: Not about this migration: these answer for their own reasons and are checked
#: only for not crashing.
NOT_A_MIGRATION_QUESTION = {
    "/",  # redirects to the dashboard
    "/health",
    "/login",
    "/login/2fa",
    "/auth/start",
    "/auth/callback",
    "/oauth/authorize",
    "/external/summary",  # 503 unless the external API is switched on
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/hrt/release.json",
}


def _page_routes(app) -> list[str]:
    """Every GET route that takes no path parameter."""

    paths: set[str] = set()

    def walk(routes) -> None:
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                walk(included.routes)
                continue
            nested = getattr(route, "routes", None)
            if nested:
                walk(nested)
                continue
            if "GET" not in (getattr(route, "methods", set()) or set()):
                continue
            path = getattr(route, "path", "")
            if "{" in path or path.startswith("/static") or path.startswith("/mcp"):
                continue
            paths.add(path)

    walk(app.routes)
    return sorted(paths)


@pytest.fixture
async def second_person(db_session):
    """One more health subject, which is the whole point of the fixture."""

    owner = User(
        username="second-person",
        normalized_username="second-person",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(owner)
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name="Second person",
        timezone="Europe/Chisinau",
    )
    db_session.add(subject)
    await db_session.commit()
    return subject


async def test_no_page_answers_with_a_stack_trace(
    auth_client, second_person, legacy_owner_roots
):
    from web.main import app

    crashed: list[tuple[str, int]] = []
    for path in _page_routes(app):
        response = await auth_client.get(path, headers={"Accept": "text/html"})
        # 500 exactly: an unhandled exception. 503 is a service stating it is
        # switched off, which is an answer.
        if response.status_code == 500:
            crashed.append((path, response.status_code))

    assert not crashed, (
        "these pages crashed in a shared installation instead of answering: "
        + ", ".join(f"{path} → {code}" for path, code in crashed)
    )


async def test_the_refusing_pages_are_the_ones_on_the_backlog(
    auth_client, second_person, legacy_owner_roots
):
    """The backlog is checked in both directions.

    A page that starts refusing without being listed is a regression. A page
    that stops refusing while still listed means the list is stale, and a stale
    list of "what is left to do" is worse than none.
    """

    from web.main import app

    refused = set()
    for path in _page_routes(app):
        if path in NOT_A_MIGRATION_QUESTION:
            continue
        response = await auth_client.get(path, headers={"Accept": "text/html"})
        if response.status_code == 409:
            refused.add(path)

    newly_refusing = refused - STILL_SOLE_SUBJECT
    assert not newly_refusing, (
        "these pages started refusing in a shared installation: "
        + ", ".join(sorted(newly_refusing))
    )

    now_working = STILL_SOLE_SUBJECT - refused
    assert not now_working, (
        "these pages no longer refuse — remove them from STILL_SOLE_SUBJECT: "
        + ", ".join(sorted(now_working))
    )


@pytest.fixture
async def professional_client(client, db_session, legacy_owner_roots):
    """A signed-in account that keeps no health record of its own.

    Which is most doctors and every trainer. They are not an edge case to be
    tolerated — they are half of what this product is now — and until recently
    every personal page told them it did not support several records yet, which
    is both untrue and unactionable.
    """

    from web.auth import create_session
    from web.deps import SESSION_COOKIE

    doctor = User(
        username="dr-no-record",
        normalized_username="dr-no-record",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(doctor)
    await db_session.flush()
    db_session.add(
        UserRole(user_id=doctor.id, role=UserRoleName.DOCTOR.value)
    )
    await db_session.commit()

    client.cookies.set(SESSION_COOKIE, create_session("dr-no-record"))
    return client


async def test_an_account_without_a_record_is_told_that_and_not_something_else(
    professional_client
):
    from web.main import app

    wrong_answer = []
    for path in _page_routes(app):
        if path in NOT_A_MIGRATION_QUESTION:
            continue
        response = await professional_client.get(
            path, headers={"Accept": "text/html"}
        )
        if response.status_code == 500:
            wrong_answer.append(f"{path} crashed")
            continue
        if response.status_code != 409:
            continue
        body = response.text
        # The distinction that matters: "you have no record here" is true and
        # actionable, "this page does not support several records yet" sends
        # them looking for a setting that will never exist.
        if "несколько записей" in body:
            wrong_answer.append(f"{path} blamed the migration")

    assert not wrong_answer, "; ".join(wrong_answer)


async def test_each_patients_report_describes_that_patient(
    db_session, legacy_owner_roots, second_person
):
    """Age, sex, height, programme and goals used to live in .env, which names
    nobody.

    One set of them for the whole process, and in a one-person installation that
    is unambiguous. With two people it describes at most one and cannot say
    which — and it was being written into every patient's weekly digest,
    doctor's report and share link as though it were theirs. For a while they
    were omitted from everybody's, which was a placeholder: it cost the owner
    five fields and answered nothing.

    They are on the subject's own row now. The owner's report has them back; the
    other patient's says nothing about a body nobody has described, which is
    what a blank field in a medical document is supposed to mean.
    """

    from sqlalchemy import select


    other_subject_id = await db_session.scalar(
        select(HealthSubject.id).where(
            HealthSubject.owner_user_id != legacy_owner_roots.user_id
        )
    )
    assert other_subject_id is not None

    owner = await digest_projection.assemble_context(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert owner["user_profile"]["height_cm"] is not None
    assert owner["user_profile"]["age"] is not None

    other = await digest_projection.assemble_context(
        db_session, subject_id=other_subject_id
    )
    assert other["user_profile"]["age"] is None
    assert other["user_profile"]["height_cm"] is None
    assert other["user_profile"]["program"] is None


async def test_hevy_dashboard_reads_only_the_signed_in_subject(
    auth_client,
    db_session,
    legacy_owner_roots,
    second_person,
    hevy_connection_id,
):
    other_connection = IntegrationConnection(
        subject_id=second_person.id,
        provider=IntegrationProvider.HEVY.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator="shared-page-hevy-other",
        credential_ref="test:shared-page-hevy-other",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add(other_connection)
    await db_session.flush()
    db_session.add_all(
        [
            HevyWorkout(
                subject_id=legacy_owner_roots.subject_id,
                integration_connection_id=hevy_connection_id,
                date=date(2026, 8, 1),
                domain=Domain.WORKOUTS.value,
                source=Source.HEVY_API.value,
                external_id="shared-page-hevy-mine",
                title="My scoped Hevy workout",
            ),
            HevyWorkout(
                subject_id=second_person.id,
                integration_connection_id=other_connection.id,
                date=date(2026, 8, 20),
                domain=Domain.WORKOUTS.value,
                source=Source.HEVY_API.value,
                external_id="shared-page-hevy-other",
                title="FOREIGN HEVY WORKOUT MUST NOT LEAK",
            ),
        ]
    )
    await db_session.commit()

    response = await auth_client.get("/hevy", headers={"Accept": "text/html"})

    assert response.status_code == 200
    assert "My scoped Hevy workout" in response.text
    assert "FOREIGN HEVY WORKOUT MUST NOT LEAK" not in response.text


async def test_share_earliest_date_reads_only_the_prepared_owner(
    db_session, legacy_owner_roots, second_person
):
    from vitals.services.share import ownership, queries
    from web.config import get_web_config

    db_session.add_all(
        [
            WeightLog(
                subject_id=legacy_owner_roots.subject_id,
                date=date(2026, 3, 1),
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                weight_kg=80.0,
            ),
            WeightLog(
                subject_id=second_person.id,
                date=date(2020, 1, 1),
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                weight_kg=90.0,
            ),
        ]
    )
    await db_session.flush()
    prepared = await ownership.prepare_legacy_owner(
        db_session, actor_username=get_web_config().auth_username
    )

    earliest = await queries.earliest_data_date(
        db_session, prepared_owner=prepared
    )

    assert earliest == date(2026, 3, 1)


async def test_an_account_without_a_record_is_offered_no_personal_section(
    professional_client
):
    """Every one of them is about a record this account does not have.

    Offered anyway, each link bounces straight back to the roster, and a shelf
    of links that cannot answer is worse than no shelf. The rule is not about
    roles — it is about whether the thing a link leads to exists for this
    reader.

    Deliberately not combined with its sibling below into one comparison:
    ``auth_client`` and ``professional_client`` are the same underlying client,
    and signing one in overwrites the other's cookie.
    """

    page = await professional_client.get("/care", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "/today" not in page.text
    assert "/share" not in page.text
    # The way out is still there. A page with no sign-out is a trap.
    assert "/logout" in page.text


async def test_an_account_with_a_record_is_offered_all_of_them(auth_client):
    """The same rail, for somebody the sections are about."""

    page = await auth_client.get("/settings/care", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "/today" in page.text
    assert "/share" in page.text


async def test_the_page_shows_the_readers_day_not_the_servers(
    auth_client, db_session, legacy_owner_roots
):
    """"Today" is a question about the reader, and the answer moves with them.

    ``health_subjects.timezone`` has always held it and nothing read it: every
    page took the day from ``VITALS_TIMEZONE``, the installation's zone, which
    was also the reader's while an installation was one person. A patient abroad
    saw the server's date on their own dashboard — and logged a weigh-in against
    it.
    """

    from vitals.utils.timeutils import set_timezone, subject_timezone, today_local

    # Twenty-five hours apart, which is the whole point: these two zones never
    # share a date, so the assertion cannot pass by the two happening to agree
    # at the hour the suite runs.
    set_timezone("Pacific/Midway")  # UTC-11
    try:
        subject = await db_session.get(HealthSubject, legacy_owner_roots.subject_id)
        subject.timezone = "Pacific/Kiritimati"  # UTC+14
        await db_session.commit()

        server_day = today_local()
        with subject_timezone("Pacific/Kiritimati"):
            reader_day = today_local()
        assert reader_day != server_day

        response = await auth_client.get("/today", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert reader_day.strftime("%d-%m-%Y") in response.text
        assert server_day.strftime("%d-%m-%Y") not in response.text
    finally:
        set_timezone("Europe/Chisinau")


#: Mutating routes the sweep below deliberately does not call, each for a reason
#: that has nothing to do with this migration.
#:
#: An empty body stops most of these at validation, which is an answer and the
#: whole point. These are the ones it would not stop: they take no body, or the
#: body is optional, and calling them would restart the process, wipe the
#: installation, log the sweep out of its own session, or reach a provider.
NOT_SWEPT = {
    "/settings/restart",  # restarts the process
    "/settings/import",  # whole-installation restore: deletes every portable table
    "/logout",  # would end the session the rest of the sweep runs under
    "/garmin/sync",  # reaches Garmin
    "/garmin/import",  # reaches Garmin
    "/hevy/sync",  # reaches Hevy
    "/settings/garmin/weight/send-now",  # reaches Garmin
    "/reports/brief",  # composes through the AI gateway
    "/reports/brief/test",  # composes through the AI gateway
    "/reports/digest",  # composes through the AI gateway
    "/login",  # about authentication, not about whose record this is
    "/login/2fa",
    "/oauth/token",
    "/oauth/authorize/approve",
    "/alerts/resolve-all",  # takes no body and resolves the sweep's own alerts
}


@pytest.fixture(autouse=True)
def _writes_land_in_a_throwaway_env(tmp_path, monkeypatch):
    """Several settings routes persist into ``.env``, which is the developer's.

    ``env_writer`` resolves the repository's own file unless
    ``VITALS_ENV_FILE`` says otherwise, and most of the suite does not say
    otherwise — so a sweep that posts to ``/settings/garmin`` with an empty body
    rewrites real credentials. Found that way: the first version of this file
    left ``test_garmin_sync_not_configured_redirects`` failing several hundred
    tests later and passing on its own.

    Redirecting the file is the structural fix. Naming the routes in the
    exclusion list above was the first one, and it is the weaker kind — it
    covers the routes somebody remembered.
    """

    monkeypatch.setenv("VITALS_ENV_FILE", str(tmp_path / "sweep.env"))


def _write_routes(app) -> list[tuple[str, str]]:
    """Every mutating route that takes no path parameter."""

    found: set[tuple[str, str]] = set()

    def walk(routes) -> None:
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                walk(included.routes)
                continue
            nested = getattr(route, "routes", None)
            if nested:
                walk(nested)
                continue
            methods = (getattr(route, "methods", set()) or set()) & {
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
            }
            if not methods:
                continue
            path = getattr(route, "path", "")
            if "{" in path or path.startswith("/mcp"):
                continue
            for method in methods:
                found.add((method, path))

    walk(app.routes)
    return sorted(found)


async def test_no_write_route_answers_with_a_stack_trace(
    auth_client, second_person, legacy_owner_roots
):
    """The same property as the page sweep, on the half it could not see.

    Everything above walks ``GET``. A shared installation meets these bridges on
    ``POST`` too, and one of them was live: clicking Save on the notification
    settings answered 409, so nobody on a shared installation could store their
    own brief time. Several thousand tests did not see it, and neither did the
    page sweep, because neither posts.

    Empty bodies on purpose. Most routes stop at validation, which is an answer
    and is all this asserts: a refusal, a redirect, a 422 are all fine, and a
    500 means a bridge declined and nobody caught it.
    """

    from web.main import app

    crashed: list[str] = []
    for method, path in _write_routes(app):
        if path in NOT_SWEPT:
            continue
        response = await auth_client.request(
            method, path, headers={"Accept": "text/html"}
        )
        if response.status_code == 500:
            crashed.append(f"{method} {path}")

    assert not crashed, (
        "these write routes crashed in a shared installation instead of "
        "answering: " + ", ".join(crashed)
    )


async def test_the_owner_can_save_their_own_notification_settings(
    auth_client, second_person, legacy_owner_roots
):
    """The defect this sweep was written for, pinned as itself.

    The refusal was about the shared ``app_settings`` mirror, which does stop
    meaning anything with two people — and it was applied to the whole save,
    including the subject-scoped row that is unambiguous. What a second person
    invalidates is the mirror, not this person's own preferences.
    """

    response = await auth_client.post(
        "/settings/proactive",
        data={"brief_time": "09:30"},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert "saved=proactive" in response.headers["location"]
    # Saved, and honest about the half that did not take effect: the process
    # schedule is one registry and is not rebuilt from one record.
    assert "deferred=1" in response.headers["location"]


#: One minimally valid body per domain write route, so the sweep below reaches
#: past request validation and into the service that used to refuse.
#:
#: The empty-body sweep above is the weaker half and says so: thirty-two of
#: these routes stop at 422, which is an answer but not the one worth having —
#: a bridge two layers down is never asked. These bodies are what asks it.
WRITE_BODIES = {
    "/weight/log": {"weight_kg": "80.5", "date": "2026-08-20"},
    "/weight/measurement": {"date": "2026-08-20", "waist_cm": "84"},
    "/weight/noise": {"start_date": "2026-08-20", "reason": "travel"},
    "/labs/result": {"date": "2026-08-20", "marker": "ferritin", "value": "70"},
    "/reports/milestone": {
        "name": "80 kg",
        "domain": "weight",
        "target_value": "80",
    },
    "/settings/language": {"language": "ru"},
    "/settings/modules": {"module": "skincare", "enabled": "true"},
    "/glp1/injection": {
        "date": "2026-08-20",
        "drug": "semaglutide",
        "dose_mg": "0.5",
    },
    "/glp1/phase": {
        "start_date": "2026-08-20",
        "drug": "semaglutide",
        "dose_mg": "0.5",
    },
    "/glp1/side-effect": {
        "date": "2026-08-20",
        "effect_type": "nausea",
        "severity": "2",
    },
    "/supplements/save": {"name": "Vitamin D"},
    "/hrt/cycle": {"kind": "course", "start_date": "2026-08-20"},
    "/hrt/side-effect": {
        "date": "2026-08-20",
        "effect_type": "acne",
        "severity": "1",
    },
    "/genetics/save": {"gene": "MTHFR"},
    "/skincare/log": {"date": "2026-08-20", "retinoid": "true"},
    "/skincare/observation": {"date": "2026-08-20", "inflammation": "2"},
    "/skincare/product/save": {"name": "Cream", "type": "moisturizer"},
    "/nutrition/meal": {
        "date": "2026-08-20",
        "name": "Breakfast",
        "calories": "500",
    },
    "/timeline": {"title": "Started TRT", "date": "2026-08-20"},
    "/charts": {
        "name": "Weight",
        "domain": ["weight"],
        "metric_key": ["weight_kg"],
    },
    "/settings/proactive": {"brief_time": "09:30"},
}


async def test_the_owner_can_still_write_their_own_record(
    auth_client, second_person, legacy_owner_roots, all_modules_on
):
    """Somebody else existing does not stop this person logging their weight.

    The stronger half of the write sweep. Each of these carries a body the route
    accepts, so the request reaches the service rather than stopping at
    validation, and each is the record's own owner writing their own row.

    A 409 here is the sole-subject refusal and is the thing this asserts is
    gone. Other refusals are left alone: a route may still answer 400 because
    the window it was given holds nothing, and that is about the data, not about
    how many people the installation has.
    """

    refused: list[str] = []
    for path, body in sorted(WRITE_BODIES.items()):
        response = await auth_client.post(
            path,
            data=body,
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        if response.status_code in (409, 500):
            refused.append(f"{path} \u2192 {response.status_code}")

    assert not refused, (
        "the record's own owner could not write to these in a shared "
        "installation: " + ", ".join(refused)
    )


@pytest.fixture
async def patient_client(client, db_session, legacy_owner_roots):
    """A second account that owns its own record — the third case.

    The two above are the record's own owner and an account with no record at
    all. Neither is what a shared installation is mostly made of: somebody who
    is not the ``.env`` owner, keeps their own history, and reaches every page
    about it. Every sole-subject bridge that survived did so because the only
    account exercising it was the one the installation was built around.
    """

    from vitals.models.scoped_settings import SubjectSetting
    from vitals.persistence import rls as rls_session
    from vitals.services.modules import preferences as modules_service
    from vitals.services.tenancy.bootstrap import bootstrap_legacy_resource_roots
    from web.auth import create_session
    from web.deps import SESSION_COOKIE

    user = User(
        username="patient-two",
        normalized_username="patient-two",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name="Patient Two",
        timezone="Europe/Chisinau",
    )
    db_session.add(subject)
    await db_session.flush()
    # The same roots startup gives the ``.env`` owner. Every subject needs them,
    # and today only the bootstrap and the demo seeder create any — which is why
    # a patient born by registration is the next thing to get this wrong.
    await bootstrap_legacy_resource_roots(db_session, subject_id=subject.id)
    db_session.add(
        SubjectSetting(
            subject_id=subject.id,
            key=modules_service.SETTINGS_KEY,
            value={key: True for key in modules_service.MODULE_REGISTRY},
        )
    )
    await db_session.commit()
    # Production hands every request its own session. The suite shares one, and
    # ``legacy_owner_roots`` has already bound it to the owner — a binding that
    # deliberately refuses to move to another person. Clearing it is what a new
    # request does for free.
    db_session.info.pop(rls_session._SUBJECT_KEY, None)

    client.cookies.set(SESSION_COOKIE, create_session("patient-two"))
    return client


async def test_a_second_patient_reaches_every_page(patient_client):
    """Not the owner, and not a professional: the case in between."""

    from web.main import app

    wrong: list[str] = []
    for path in _page_routes(app):
        if path in NOT_A_MIGRATION_QUESTION or path in STILL_SOLE_SUBJECT:
            continue
        response = await patient_client.get(path, headers={"Accept": "text/html"})
        if response.status_code in (409, 500):
            wrong.append(f"{path} \u2192 {response.status_code}")

    assert not wrong, (
        "a patient who is not the installation owner was refused these: "
        + ", ".join(wrong)
    )


async def test_a_second_patient_can_write_their_own_record(patient_client):
    """The same twenty-one write paths, for somebody the app was not built for."""

    refused: list[str] = []
    for path, body in sorted(WRITE_BODIES.items()):
        response = await patient_client.post(
            path,
            data=body,
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        if response.status_code in (409, 500):
            refused.append(f"{path} \u2192 {response.status_code}")

    assert not refused, (
        "a patient who is not the installation owner could not write these: "
        + ", ".join(refused)
    )
