"""``/r/<token>`` — the app's only anonymous route.

Everything here is a security property, so each one is its own test: nothing
leaks before the password, one cookie doesn't open another report, dead links
are indistinguishable from each other, the page carries no script and no way
back into the app, and guessing is throttled.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from vitals.enums import Domain
from vitals.models.labs import LabResult
from vitals.models.weight import WeightLog
from vitals.services import share_service
from vitals.services.modules_service import MODULE_REGISTRY
from vitals.utils.timeutils import now_local
from web.config import get_web_config

pytestmark = pytest.mark.usefixtures("legacy_owner_roots")

ALL_ON = {k: True for k in MODULE_REGISTRY}
START = date(2026, 3, 1)
END = date(2026, 3, 30)

# A value that must never appear on a page nobody has unlocked.
SECRET_WEIGHT = 86.4


async def _seed(db_session) -> None:
    if (await db_session.execute(select(WeightLog))).scalars().first() is not None:
        return
    db_session.add_all(
        [
            WeightLog(date=START, domain="weight", source="manual", weight_kg=89.0),
            WeightLog(date=END, domain="weight", source="manual", weight_kg=SECRET_WEIGHT),
            LabResult(
                date=date(2026, 3, 10), domain="labs", source="manual",
                marker="Ферритин", value=18.0, unit="нг/мл", ref_low=30, ref_high=400,
                flag="low",
            ),
        ]
    )
    await db_session.flush()


async def _make_report(db_session, **kwargs):
    await _seed(db_session)
    owner = await share_service.prepare_legacy_owner(
        db_session,
        actor_username=get_web_config().auth_username,
    )
    params = dict(
        title="Endocrinologist",
        domains=[Domain.WEIGHT.value, Domain.LABS.value],
        period_start=START,
        period_end=END,
        enabled=ALL_ON,
    )
    params.update(kwargs)
    row, password = await share_service.create_report(
        db_session,
        prepared_owner=owner,
        **params,
    )
    await db_session.commit()
    return row, password


@pytest.mark.asyncio
async def test_counted_days_agree_with_the_number(client, db_session):
    """"Еда записана за 2 дня", not "за 2 дней" — the one place the document
    counts out loud, and the form changes with the number."""
    from vitals.i18n import current_lang
    from vitals.models.nutrition import MealLog

    db_session.add_all(
        [
            MealLog(date=START, domain="nutrition", source="manual",
                    name="Овсянка", calories=400.0, protein_g=20.0),
            MealLog(date=START + timedelta(days=1), domain="nutrition",
                    source="manual", name="Курица", calories=600.0, protein_g=50.0),
        ]
    )
    previous = current_lang.get()
    current_lang.set("ru")
    try:
        row, password = await _make_report(
            db_session, domains=[Domain.WEIGHT.value, Domain.NUTRITION.value]
        )
        await client.post(f"/r/{row.token}", data={"password": password})
        doc = (await client.get(f"/r/{row.token}")).text
    finally:
        current_lang.set(previous)

    assert "за 2 дня" in doc
    assert "за 2 дней" not in doc


@pytest.mark.asyncio
async def test_locked_page_shows_a_form_and_leaks_nothing(client, db_session):
    row, _ = await _make_report(db_session)

    r = await client.get(f"/r/{row.token}")
    assert r.status_code == 200
    body = r.text
    assert 'type="password"' in body
    assert str(SECRET_WEIGHT) not in body
    assert "Ферритин" not in body
    assert row.title not in body


@pytest.mark.asyncio
async def test_correct_password_opens_the_document(client, db_session):
    row, password = await _make_report(db_session)

    unlock = await client.post(f"/r/{row.token}", data={"password": password})
    assert unlock.status_code == 303

    doc = await client.get(f"/r/{row.token}")
    assert doc.status_code == 200
    assert "Ферритин" in doc.text
    assert "<svg" in doc.text


@pytest.mark.asyncio
async def test_password_verification_runs_without_a_database_transaction(
    client,
    db_session,
    monkeypatch,
):
    from web.routers import public_report as public_router

    row, password = await _make_report(db_session)
    original = public_router.verify_password

    def checked(candidate, password_hash):
        assert not db_session.in_transaction()
        return original(candidate, password_hash)

    monkeypatch.setattr(public_router, "verify_password", checked)
    assert (
        await client.post(f"/r/{row.token}", data={"password": password})
    ).status_code == 303


@pytest.mark.asyncio
async def test_unlocked_document_renders_before_governance_is_released(
    client,
    db_session,
    monkeypatch,
):
    from web.routers import public_report as public_router

    row, password = await _make_report(db_session)
    await client.post(f"/r/{row.token}", data={"password": password})
    original = public_router.render_document

    def checked(request, report, *, download=False):
        assert db_session.in_transaction()
        return original(request, report, download=download)

    monkeypatch.setattr(public_router, "render_document", checked)
    response = await client.get(f"/r/{row.token}")
    assert response.status_code == 200
    assert "Ферритин" in response.text


@pytest.mark.asyncio
async def test_access_cookie_does_not_follow_a_recycled_report_id(
    client,
    db_session,
):
    first, password = await _make_report(db_session)
    await client.post(f"/r/{first.token}", data={"password": password})
    old_id = first.id

    owner = await share_service.prepare_legacy_owner(
        db_session,
        actor_username=get_web_config().auth_username,
    )
    assert await share_service.delete_report(
        db_session,
        old_id,
        prepared_owner=owner,
    )
    await db_session.commit()
    replacement, _ = await _make_report(db_session, title="Replacement")
    if replacement.id != old_id:
        # PostgreSQL sequences do not recycle ids naturally; force the database
        # state this regression protects without changing the token.
        replacement.id = old_id
        await db_session.commit()
    assert replacement.id == old_id

    response = await client.get(f"/r/{replacement.token}")
    assert response.status_code == 200
    assert 'type="password"' in response.text
    assert "Ферритин" not in response.text


@pytest.mark.asyncio
async def test_wrong_password_grants_nothing(client, db_session):
    row, _ = await _make_report(db_session)

    bad = await client.post(f"/r/{row.token}", data={"password": "not-the-password"})
    assert bad.status_code == 401
    assert "Ферритин" not in bad.text

    still_locked = await client.get(f"/r/{row.token}")
    assert 'type="password"' in still_locked.text


@pytest.mark.asyncio
async def test_a_cookie_for_one_report_does_not_open_another(client, db_session):
    first, first_password = await _make_report(db_session)
    second, _ = await _make_report(db_session, title="Dermatologist")

    await client.post(f"/r/{first.token}", data={"password": first_password})
    assert "Ферритин" in (await client.get(f"/r/{first.token}")).text

    other = await client.get(f"/r/{second.token}")
    assert 'type="password"' in other.text
    assert "Ферритин" not in other.text


@pytest.mark.asyncio
async def test_missing_revoked_and_expired_are_indistinguishable(client, db_session):
    revoked, _ = await _make_report(db_session)
    expired, _ = await _make_report(db_session)
    owner = await share_service.prepare_legacy_owner(
        db_session,
        actor_username=get_web_config().auth_username,
    )
    await share_service.revoke(
        db_session,
        revoked.id,
        prepared_owner=owner,
    )
    expired.expires_at = now_local() - timedelta(days=1)
    await db_session.commit()

    pages = [
        await client.get("/r/there-is-no-such-token"),
        await client.get(f"/r/{revoked.token}"),
        await client.get(f"/r/{expired.token}"),
    ]
    assert {p.status_code for p in pages} == {404}
    assert len({p.text for p in pages}) == 1


@pytest.mark.asyncio
async def test_opens_are_counted(client, db_session):
    row, password = await _make_report(db_session)
    assert row.opened_count == 0

    await client.post(f"/r/{row.token}", data={"password": password})
    await db_session.refresh(row)

    assert row.opened_count == 1
    assert row.last_opened_at is not None


@pytest.mark.asyncio
async def test_password_attempts_are_throttled(client, db_session):
    row, _ = await _make_report(db_session)

    codes = [
        (await client.post(f"/r/{row.token}", data={"password": "guess"})).status_code
        for _ in range(22)
    ]
    assert 429 in codes


@pytest.mark.asyncio
async def test_public_pages_are_hardened(client, db_session):
    row, password = await _make_report(db_session)

    for response in (
        await client.get(f"/r/{row.token}"),
        await client.get("/r/nope"),
    ):
        csp = response.headers["content-security-policy"]
        assert "unsafe-eval" not in csp and "script-src" not in csp
        assert csp.startswith("default-src 'none'")
        assert response.headers["x-robots-tag"].startswith("noindex")
        assert "no-store" in response.headers["cache-control"]
        # NOT "no-referrer": per Fetch, a document with that policy sends
        # `Origin: null` on a form POST, and the app's origin check then 403s the
        # password form from the page that rendered it. See harden().
        assert response.headers["referrer-policy"] == "strict-origin"


@pytest.mark.asyncio
async def test_the_password_form_survives_the_origin_check(client, db_session):
    """The regression the header bug above actually caused.

    Every other test here posts without an Origin header, which the CSRF
    middleware skips — so the whole feature could 403 in a browser with the suite
    fully green. This one sends the header a browser sends.
    """
    row, password = await _make_report(db_session)

    r = await client.post(
        f"/r/{row.token}",
        data={"password": password},
        headers={"origin": "http://test", "host": "test"},
    )
    assert r.status_code == 303, r.text[:200]


@pytest.mark.asyncio
async def test_the_document_has_no_script_and_no_way_back_into_the_app(client, db_session):
    row, password = await _make_report(db_session)
    await client.post(f"/r/{row.token}", data={"password": password})
    body = (await client.get(f"/r/{row.token}")).text

    assert "<script" not in body.lower()
    assert "/static/" not in body
    assert "x-data" not in body and "hx-" not in body
    # No anchor at all — the doctor cannot click their way into /weight.
    assert "<a " not in body and "href=" not in body


@pytest.mark.asyncio
async def test_the_document_is_self_contained(client, db_session):
    """It has to survive being saved to disk and opened with the network off."""
    row, password = await _make_report(db_session)
    await client.post(f"/r/{row.token}", data={"password": password})
    body = (await client.get(f"/r/{row.token}")).text

    assert body.lstrip().lower().startswith("<!doctype html>")
    assert "<style>" in body
    # The SVG namespace is a bare identifier, not something a browser fetches.
    offline = body.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "http://" not in offline and "https://" not in offline


@pytest.mark.asyncio
async def test_the_document_speaks_the_language_it_was_frozen_in(client, db_session):
    """The owner's current UI setting has no say over a document that was already
    handed to somebody. Frozen in English while the app itself runs in Russian
    (the ``client`` fixture seeds ru), the document must still read English."""
    from vitals.i18n import STRINGS, current_lang

    current_lang.set("en")
    row, password = await _make_report(db_session)
    assert row.snapshot["lang"] == "en"

    await client.post(f"/r/{row.token}", data={"password": password})
    body = (await client.get(f"/r/{row.token}")).text
    assert STRINGS["en"]["doc.heading"] in body
    assert STRINGS["ru"]["doc.heading"] not in body


@pytest.mark.asyncio
async def test_the_owners_routes_are_still_behind_auth(client, db_session):
    """The anonymous router must not have loosened the gate on anything else."""
    for path in ("/weight", "/labs"):
        r = await client.get(path, headers={"accept": "text/html"})
        assert r.status_code in (302, 303), path
        assert "/login" in r.headers.get("location", "")
