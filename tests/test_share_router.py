"""``/share`` — the owner's screen: create, revoke, delete, download.

The end-to-end path (create here, open at ``/r/<token>``) is what proves the two
routers agree about tokens, passwords and the frozen snapshot; the rest are the
guards on the form.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from freezegun import freeze_time
from sqlalchemy import select

from vitals.enums import Domain
from vitals.models.garmin import GarminDaily
from vitals.models.labs import LabResult
from vitals.models.nutrition import MealLog
from vitals.models.share import SharedReport
from vitals.models.identity import HealthSubject
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
    assert r.text.count(f'max="{TODAY.isoformat()}"') == 2


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


@pytest.mark.parametrize("period", ["90", "all", "custom"])
@pytest.mark.asyncio
async def test_each_period_choice_includes_today_and_excludes_future_facts(
    period,
    legacy_owner_roots,
    all_modules_on,
    auth_client,
    db_session,
    owned_by_legacy_subject,
):
    del all_modules_on, owned_by_legacy_subject
    future = TODAY + timedelta(days=1)
    db_session.add_all(
        [
            WeightLog(
                subject_id=legacy_owner_roots.subject_id,
                date=TODAY,
                domain="weight",
                source="manual",
                weight_kg=86.4,
            ),
            WeightLog(
                subject_id=legacy_owner_roots.subject_id,
                date=future,
                domain="weight",
                source="manual",
                weight_kg=99.9,
            ),
            LabResult(
                subject_id=legacy_owner_roots.subject_id,
                date=TODAY,
                domain="labs",
                source="manual",
                marker="Current marker",
                value=18.0,
                unit="ng/mL",
                flag="low",
            ),
            LabResult(
                subject_id=legacy_owner_roots.subject_id,
                date=future,
                domain="labs",
                source="manual",
                marker="Future marker",
                value=999.0,
                unit="ng/mL",
                flag="high",
            ),
            MealLog(
                subject_id=legacy_owner_roots.subject_id,
                date=TODAY,
                domain="nutrition",
                source="manual",
                name="Current meal",
                calories=500.0,
                protein_g=30.0,
            ),
            MealLog(
                subject_id=legacy_owner_roots.subject_id,
                date=future,
                domain="nutrition",
                source="manual",
                name="Future meal",
                calories=9000.0,
                protein_g=900.0,
            ),
        ]
    )
    await db_session.commit()

    overrides = {
        "period": period,
        "domains": [
            Domain.WEIGHT.value,
            Domain.LABS.value,
            Domain.NUTRITION.value,
        ],
    }
    if period == "custom":
        overrides.update(
            period_start=TODAY.isoformat(),
            period_end=TODAY.isoformat(),
        )
    _, row = await _created(auth_client, db_session, **overrides)

    assert row.period_end == TODAY
    assert row.snapshot["period"]["final_day_incomplete"] is True
    assert row.snapshot["blocks"]["weight"]["points"] == [
        [TODAY.isoformat(), 86.4]
    ]
    assert [
        marker["marker"] for marker in row.snapshot["blocks"]["labs"]["markers"]
    ] == ["Current marker"]
    assert row.snapshot["blocks"]["nutrition"] == {
        "days_logged": 1,
        "calories_per_day": 500.0,
        "protein_per_day_g": 30.0,
    }
    assert future.isoformat() not in str(row.snapshot)
    assert "Future marker" not in str(row.snapshot)
    assert "9000" not in str(row.snapshot)


@pytest.mark.asyncio
async def test_custom_period_end_is_capped_at_today(
    legacy_owner_roots,
    auth_client,
    db_session,
    owned_by_legacy_subject,
):
    db_session.add(
        WeightLog(
            subject_id=legacy_owner_roots.subject_id,
            date=TODAY,
            domain="weight",
            source="manual",
            weight_kg=86.4,
        )
    )
    await db_session.commit()

    _, row = await _created(
        auth_client,
        db_session,
        domains=[Domain.WEIGHT.value],
        period="custom",
        period_start=TODAY.isoformat(),
        period_end=(TODAY + timedelta(days=7)).isoformat(),
    )

    assert row.period_start == TODAY
    assert row.period_end == TODAY


@pytest.mark.asyncio
async def test_today_comes_from_the_record_owners_timezone_near_a_utc_boundary(
    legacy_owner_roots,
    auth_client,
    db_session,
    owned_by_legacy_subject,
):
    subject = await db_session.get(HealthSubject, legacy_owner_roots.subject_id)
    subject.timezone = "Asia/Almaty"
    await db_session.commit()

    # It is still September 5 in the installation's Europe/Chisinau timezone,
    # but already September 6 for this record owner in Almaty.
    with freeze_time("2026-09-05 20:30:00+00:00"):
        owners_today = date(2026, 9, 6)
        db_session.add(
            WeightLog(
                subject_id=legacy_owner_roots.subject_id,
                date=owners_today,
                domain="weight",
                source="manual",
                weight_kg=86.4,
            )
        )
        await db_session.commit()

        _, row = await _created(
            auth_client,
            db_session,
            domains=[Domain.WEIGHT.value],
            period="custom",
            period_start=owners_today.isoformat(),
            period_end=owners_today.isoformat(),
        )

    assert row.period_start == owners_today
    assert row.period_end == owners_today
    assert row.snapshot["blocks"]["weight"]["points"] == [
        [owners_today.isoformat(), 86.4]
    ]


@pytest.mark.asyncio
async def test_all_time_honours_history_older_than_the_preset_limit(
    legacy_owner_roots,
    garmin_connection_id,
    all_modules_on,
    auth_client,
    db_session,
    owned_by_legacy_subject,
):
    del all_modules_on, owned_by_legacy_subject
    oldest = TODAY - timedelta(days=400)
    db_session.add_all(
        [
            WeightLog(
                subject_id=legacy_owner_roots.subject_id,
                date=oldest,
                domain="weight",
                source="manual",
                weight_kg=90.0,
            ),
            WeightLog(
                subject_id=legacy_owner_roots.subject_id,
                date=TODAY,
                domain="weight",
                source="manual",
                weight_kg=86.0,
            ),
            MealLog(
                subject_id=legacy_owner_roots.subject_id,
                date=oldest,
                domain="nutrition",
                source="manual",
                name="Old meal",
                calories=300.0,
                protein_g=20.0,
            ),
            MealLog(
                subject_id=legacy_owner_roots.subject_id,
                date=TODAY,
                domain="nutrition",
                source="manual",
                name="Current meal",
                calories=700.0,
                protein_g=40.0,
            ),
            GarminDaily(
                subject_id=legacy_owner_roots.subject_id,
                integration_connection_id=garmin_connection_id,
                date=oldest,
                domain="garmin",
                source="garmin_api",
                sleep_seconds=6 * 3600,
                sleep_score=60,
            ),
            GarminDaily(
                subject_id=legacy_owner_roots.subject_id,
                integration_connection_id=garmin_connection_id,
                date=TODAY,
                domain="garmin",
                source="garmin_api",
                sleep_seconds=8 * 3600,
                sleep_score=80,
            ),
        ]
    )
    await db_session.commit()

    _, row = await _created(
        auth_client,
        db_session,
        domains=[
            Domain.WEIGHT.value,
            Domain.GARMIN.value,
            Domain.NUTRITION.value,
        ],
        period="all",
    )

    assert row.period_start == oldest
    assert row.period_end == TODAY
    assert row.snapshot["blocks"]["weight"]["points"] == [
        [oldest.isoformat(), 90.0],
        [TODAY.isoformat(), 86.0],
    ]
    assert row.snapshot["blocks"]["nutrition"] == {
        "days_logged": 2,
        "calories_per_day": 500.0,
        "protein_per_day_g": 30.0,
    }
    assert row.snapshot["blocks"]["garmin"]["days"] == 2
    assert row.snapshot["blocks"]["garmin"]["current"]["sleep_hours"] == 7.0
    assert row.snapshot["blocks"]["garmin"]["current"]["sleep_score"] == 70.0


@pytest.mark.parametrize(
    "period, extra",
    [
        ("181", {}),
        (
            "custom",
            {
                "period_start": (TODAY - timedelta(days=180)).isoformat(),
                "period_end": TODAY.isoformat(),
            },
        ),
        (
            "custom",
            {
                "period_start": (TODAY + timedelta(days=1)).isoformat(),
                "period_end": (TODAY + timedelta(days=2)).isoformat(),
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_unsupported_bounded_periods_return_400_without_a_report(
    period,
    extra,
    legacy_owner_roots,
    auth_client,
    db_session,
    owned_by_legacy_subject,
):
    await _seed(db_session, legacy_owner_roots=legacy_owner_roots)

    response = await auth_client.post(
        "/share",
        data=_form(period=period, **extra),
    )

    assert response.status_code == 400
    assert (await db_session.execute(select(SharedReport))).scalars().first() is None


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
