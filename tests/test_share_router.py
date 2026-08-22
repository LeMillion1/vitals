"""``/share`` — the owner's screen: create, revoke, delete, download.

The end-to-end path (create here, open at ``/r/<token>``) is what proves the two
routers agree about tokens, passwords and the frozen snapshot; the rest are the
guards on the form.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from vitals.enums import Domain
from vitals.models.labs import LabResult
from vitals.models.share import SharedReport
from vitals.models.weight import WeightLog
from vitals.utils.timeutils import today_local

# Everything is relative to today: the form's own period buttons count backwards
# from it, so fixed dates would fall out of the window.
TODAY = today_local()


async def _seed(db_session, *, legacy_owner_roots) -> None:
    db_session.add_all(
        [
            WeightLog(subject_id=legacy_owner_roots.subject_id, date=TODAY - timedelta(days=40), domain="weight", source="manual", weight_kg=89.0),
            WeightLog(subject_id=legacy_owner_roots.subject_id, date=TODAY - timedelta(days=5), domain="weight", source="manual", weight_kg=86.4),
            LabResult(subject_id=legacy_owner_roots.subject_id,
                date=TODAY - timedelta(days=10), domain="labs", source="manual",
                marker="Ферритин", value=18.0, unit="нг/мл", ref_low=30, ref_high=400,
                flag="low",
            ),
        ]
    )
    await db_session.commit()


def _form(**overrides) -> dict:
    data = {
        "title": "Endocrinologist, August",
        "preset": "endocrinologist",
        "domains": [Domain.WEIGHT.value, Domain.LABS.value],
        "period": "90",
        "expires_days": "30",
    }
    data.update(overrides)
    return data


def _password_from(html: str) -> str:
    """The one-time password, dug out of the Alpine x-data attribute it is
    rendered into (so its quotes arrive HTML-escaped)."""
    match = re.search(r"value: &#34;([a-z2-9]{12})&#34;", html)
    assert match, "no password on the page"
    return match.group(1)


async def _created(auth_client, db_session, **overrides):
    r = await auth_client.post("/share", data=_form(**overrides))
    assert r.status_code == 200, r.text[:400]
    row = (await db_session.execute(select(SharedReport))).scalars().first()
    return r, row


@pytest.mark.asyncio
async def test_page_renders_the_form(legacy_owner_roots, auth_client, db_session, owned_by_legacy_subject):
    await _seed(db_session, legacy_owner_roots=legacy_owner_roots)
    r = await auth_client.get("/share")
    assert r.status_code == 200
    assert 'name="domains"' in r.text
    assert 'name="period"' in r.text
    assert 'name="expires_days"' in r.text


@pytest.mark.asyncio
async def test_create_then_open_the_link_end_to_end(legacy_owner_roots, auth_client, db_session, owned_by_legacy_subject):
    await _seed(db_session, legacy_owner_roots=legacy_owner_roots)
    page, row = await _created(auth_client, db_session)

    # The link and the password are on the page exactly once, and nowhere else
    # ever again.
    assert f"/r/{row.token}" in page.text
    password = _password_from(page.text)

    unlock = await auth_client.post(f"/r/{row.token}", data={"password": password})
    assert unlock.status_code == 303
    doc = await auth_client.get(f"/r/{row.token}")
    assert "Ферритин" in doc.text

    # Re-opening /share must not print the password again.
    assert password not in (await auth_client.get("/share")).text


@pytest.mark.asyncio
async def test_custom_range_is_stored_as_asked(legacy_owner_roots, auth_client, db_session, owned_by_legacy_subject):
    await _seed(db_session, legacy_owner_roots=legacy_owner_roots)
    start, end = TODAY - timedelta(days=20), TODAY - timedelta(days=2)
    _, row = await _created(
        auth_client, db_session,
        period="custom", period_start=start.isoformat(), period_end=end.isoformat(),
    )
    assert row.period_start == start and row.period_end == end


@pytest.mark.asyncio
async def test_all_time_starts_at_the_oldest_record(legacy_owner_roots, auth_client, db_session, owned_by_legacy_subject):
    await _seed(db_session, legacy_owner_roots=legacy_owner_roots)
    _, row = await _created(auth_client, db_session, period="all")
    assert row.period_start == TODAY - timedelta(days=40)


@pytest.mark.asyncio
async def test_expiry_is_not_the_period(legacy_owner_roots, auth_client, db_session, owned_by_legacy_subject):
    await _seed(db_session, legacy_owner_roots=legacy_owner_roots)
    _, row = await _created(auth_client, db_session, period="180", expires_days="7")
    assert (row.period_end - row.period_start).days == 179
    assert 6 <= (row.expires_at - row.created_at).days <= 7


@pytest.mark.asyncio
async def test_no_sections_is_refused(legacy_owner_roots, auth_client, db_session, owned_by_legacy_subject):
    await _seed(db_session, legacy_owner_roots=legacy_owner_roots)
    r = await auth_client.post("/share", data=_form(domains=[]))
    assert r.status_code == 400
    assert (await db_session.execute(select(SharedReport))).scalars().first() is None


@pytest.mark.asyncio
async def test_backwards_range_is_refused(legacy_owner_roots, auth_client, db_session, owned_by_legacy_subject):
    await _seed(db_session, legacy_owner_roots=legacy_owner_roots)
    r = await auth_client.post(
        "/share",
        data=_form(period="custom", period_start=TODAY.isoformat(),
                   period_end=(TODAY - timedelta(days=10)).isoformat()),
    )
    assert r.status_code == 400
    assert (await db_session.execute(select(SharedReport))).scalars().first() is None


@pytest.mark.asyncio
async def test_a_window_with_no_data_is_refused_rather_than_published_empty(
    legacy_owner_roots,
    auth_client, db_session,
):
    """An empty document reads as a patient with no history, which is a different
    claim from "this window happens to be empty"."""
    await _seed(db_session, legacy_owner_roots=legacy_owner_roots)
    r = await auth_client.post(
        "/share",
        data=_form(period="custom",
                   period_start=(TODAY - timedelta(days=400)).isoformat(),
                   period_end=(TODAY - timedelta(days=390)).isoformat()),
    )
    assert r.status_code == 400
    assert (await db_session.execute(select(SharedReport))).scalars().first() is None


@pytest.mark.asyncio
async def test_revoke_kills_the_link(legacy_owner_roots, auth_client, db_session, owned_by_legacy_subject):
    await _seed(db_session, legacy_owner_roots=legacy_owner_roots)
    page, row = await _created(auth_client, db_session)
    password = _password_from(page.text)

    r = await auth_client.post(f"/share/{row.id}/revoke")
    assert r.status_code == 303

    locked = await auth_client.post(f"/r/{row.token}", data={"password": password})
    assert locked.status_code == 404


@pytest.mark.asyncio
async def test_delete_removes_the_row(legacy_owner_roots, auth_client, db_session, owned_by_legacy_subject):
    await _seed(db_session, legacy_owner_roots=legacy_owner_roots)
    _, row = await _created(auth_client, db_session)
    r = await auth_client.post(f"/share/{row.id}/delete")
    assert r.status_code == 303
    assert (await db_session.execute(select(SharedReport))).scalars().first() is None


@pytest.mark.asyncio
async def test_download_is_the_same_document_as_the_link(legacy_owner_roots, auth_client, db_session, owned_by_legacy_subject):
    await _seed(db_session, legacy_owner_roots=legacy_owner_roots)
    page, row = await _created(auth_client, db_session)
    password = _password_from(page.text)
    await auth_client.post(f"/r/{row.token}", data={"password": password})

    served = (await auth_client.get(f"/r/{row.token}")).text
    downloaded = await auth_client.get(f"/share/{row.id}/download")

    assert downloaded.status_code == 200
    assert "attachment" in downloaded.headers["content-disposition"]
    assert downloaded.text == served


@pytest.mark.asyncio
async def test_share_is_behind_auth(client, db_session):
    for method, path in (("get", "/share"), ("get", "/share/1/download")):
        r = await getattr(client, method)(path, headers={"accept": "text/html"})
        assert r.status_code in (302, 303) and "/login" in r.headers["location"]
