"""The period report must show the period, not a number that depends on its edge.

Two failures shipped together: training arrived as a single count over a rolling
window (Saturday plus the Tuesday before it read as "1 workout in 7 days"), and
recovery arrived as one day's numbers next to the all-time row count — which the
narrative then quoted as the report's sample size ("43 days of history").
"""
from __future__ import annotations

from datetime import date, timedelta

from vitals.enums import Source
from vitals.models.garmin import GarminDaily
from vitals.models.hevy import HevyWorkout
from vitals.services import digest_service

# A Tuesday, like the day the complaint came from.
DAY = date(2026, 8, 4)


def _workout(external_id: str, on_date: date, title: str) -> HevyWorkout:
    return HevyWorkout(
        external_id=external_id,
        domain="hevy",
        date=on_date,
        source=Source.HEVY_API.value,
        title=title,
    )


async def test_sessions_before_the_window_are_visible_but_not_counted(db_session):
    db_session.add_all(
        [
            _workout("w_sat", DAY - timedelta(days=3), "Push"),   # Saturday, in period
            _workout("w_tue", DAY - timedelta(days=7), "Pull"),   # a week ago, outside
            _workout("w_old", DAY - timedelta(days=40), "Legs"),  # far outside, gone
        ]
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(db_session, on_date=DAY, period_days=7)
    hevy = ctx["hevy"]

    # The count keeps window semantics — it is the *cadence* that was missing.
    assert hevy["total_workouts"] == 1
    assert [s["date"] for s in hevy["sessions"]] == [
        (DAY - timedelta(days=7)).isoformat(),
        (DAY - timedelta(days=3)).isoformat(),
    ], "chronological, one period back, and the 40-day-old session stays out"
    assert [s["in_period"] for s in hevy["sessions"]] == [False, True]
    assert hevy["sessions"][1]["title"] == "Push"


async def test_garmin_days_cover_the_period_not_just_the_latest(db_session):
    for i in range(9):
        db_session.add(
            GarminDaily(
                domain="garmin",
                source=Source.GARMIN_API.value,
                date=DAY - timedelta(days=i),
                sleep_score=80 + i,
                resting_hr=59,
                hrv_avg=53.0,
                sleep_seconds=7 * 3600,
            )
        )
    await db_session.commit()

    ctx = await digest_service.assemble_context(db_session, on_date=DAY, period_days=7)
    days = ctx["garmin"]["days"]

    assert [d["date"] for d in days] == [
        (DAY - timedelta(days=i)).isoformat() for i in range(6, -1, -1)
    ], "seven days, chronological — the two older rows stay outside the period"
    assert days[-1]["sleep_score"] == 80
    assert days[-1]["sleep_hours"] == 7.0
    # The all-time count is still there, but it is nine, not the period.
    assert ctx["garmin"]["total_days_logged"] == 9


async def test_period_is_handed_the_period_before_it_to_compare_against(db_session):
    """Without a yardstick the narrative can only read current values back, which
    is the dashboard's job. Both windows come back in the same shape."""
    for i in range(14):
        db_session.add(
            GarminDaily(
                domain="garmin",
                source=Source.GARMIN_API.value,
                date=DAY - timedelta(days=i),
                # This period sleeps at 90, the one before it at 70.
                sleep_score=90 if i < 7 else 70,
                resting_hr=60,
            )
        )
    db_session.add_all(
        [
            _workout("w_now", DAY - timedelta(days=1), "Push"),
            _workout("w_prev_a", DAY - timedelta(days=8), "Pull"),
            _workout("w_prev_b", DAY - timedelta(days=11), "Legs"),
        ]
    )
    await db_session.commit()

    stats = (await digest_service.assemble_context(
        db_session, on_date=DAY, period_days=7
    ))["period_stats"]

    assert stats["current"]["start"] == (DAY - timedelta(days=6)).isoformat()
    assert stats["previous"]["end"] == (DAY - timedelta(days=7)).isoformat()
    assert stats["current"]["sleep_score"] == 90.0
    assert stats["previous"]["sleep_score"] == 70.0
    # Coverage travels with the mean: a "better week" off two nights isn't one.
    assert stats["current"]["garmin_days"] == 7
    assert stats["current"]["workouts"] == 1
    assert stats["previous"]["workouts"] == 2


async def test_labs_trends_show_drift_that_stays_inside_the_range(db_session):
    """A marker sliding 120 → 95 → 80 never trips a flag, so out_of_range never
    sees it — and a table of current values shows one green number."""
    from vitals.services import labs_service

    for offset, value in ((120, 120.0), (60, 95.0), (10, 80.0)):
        await labs_service.add_result(
            db_session,
            on_date=DAY - timedelta(days=offset),
            marker="Ферритин",
            value=value,
            unit="нг/мл",
            ref_low=30.0,
            ref_high=400.0,
        )
    # One-off marker: no trajectory, nothing to say, stays out.
    await labs_service.add_result(
        db_session, on_date=DAY, marker="Кальций общий", value=2.4,
        unit="ммоль/л", ref_low=2.1, ref_high=2.6,
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(db_session, on_date=DAY, period_days=7)

    assert ctx["labs"]["out_of_range"] == [], "every value is inside its range"
    trends = {t["marker"]: t for t in ctx["labs"]["trends"]}
    assert list(trends) == ["Ферритин"]
    assert [p["value"] for p in trends["Ферритин"]["points"]] == [120.0, 95.0, 80.0]
    assert trends["Ферритин"]["ref_low"] == 30.0


async def test_brief_context_stays_a_single_day(db_session):
    """period_days=1 is the morning brief: no per-day series bolted onto it."""
    db_session.add(
        GarminDaily(
            domain="garmin",
            source=Source.GARMIN_API.value,
            date=DAY,
            sleep_score=87,
            resting_hr=59,
        )
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(db_session, on_date=DAY, period_days=1)
    assert "days" not in ctx["garmin"]
    assert "period_stats" not in ctx
