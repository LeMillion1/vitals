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

import pytest

from vitals.enums import UserStatus
from vitals.models.identity import HealthSubject, User


#: What still refuses in a shared installation. One entry left, and it is not a
#: gate: backup format v1 describes an installation holding one person, so with
#: several it has nothing honest to write and says so, naming the per-subject
#: export that does work.
STILL_SOLE_SUBJECT = {
    "/settings/export",
}

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
    db_session.add(
        HealthSubject(
            owner_user_id=owner.id,
            display_name="Second person",
            timezone="Europe/Chisinau",
        )
    )
    await db_session.commit()


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


async def test_the_env_profile_is_not_attributed_to_another_patient(
    db_session, legacy_owner_roots, second_person
):
    """Age, sex, height, programme and goals live in .env, and .env names nobody.

    One set of them for the whole process, and in a one-person installation that
    is unambiguous. With two people it describes at most one and cannot say
    which — and it was being written into every patient's weekly digest,
    doctor's report and share link as though it were theirs. A medical document
    about a body that is not the patient's is worse than one missing five
    fields.
    """

    from sqlalchemy import select

    from vitals.services import digest_service

    other_subject_id = await db_session.scalar(
        select(HealthSubject.id).where(
            HealthSubject.owner_user_id != legacy_owner_roots.user_id
        )
    )
    assert other_subject_id is not None

    for subject_id in (other_subject_id, legacy_owner_roots.subject_id):
        context = await digest_service.assemble_context(
            db_session, subject_id=subject_id
        )
        profile = context["user_profile"]
        assert profile["age"] is None
        assert profile["height_cm"] is None
        assert profile["program"] is None


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

    page = await auth_client.get("/care", headers={"Accept": "text/html"})
    assert page.status_code == 200
    assert "/today" in page.text
    assert "/share" in page.text
