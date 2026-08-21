"""The period report must show the period, not a number that depends on its edge.

Two failures shipped together: training arrived as a single count over a rolling
window (Saturday plus the Tuesday before it read as "1 workout in 7 days"), and
recovery arrived as one day's numbers next to the all-time row count — which the
narrative then quoted as the report's sample size ("43 days of history").
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from vitals.enums import Source
from vitals.models.garmin import GarminDaily
from vitals.models.hevy import HevyWorkout
from vitals.services import digest_service

# A Tuesday, like the day the complaint came from.
DAY = date(2026, 8, 4)

pytestmark = pytest.mark.usefixtures("all_modules_on", "owned_by_legacy_subject")


@pytest.fixture(autouse=True)
def _now_is_the_morning_after(monkeypatch):
    """Pin "now" to the day after DAY, so a period asked for on DAY ends on DAY.

    Without it these tests read differently depending on the date they are run:
    the window ends yesterday whenever the report is generated today.
    """
    monkeypatch.setattr(digest_service, "today_local", lambda: DAY + timedelta(days=1))


def _workout(external_id: str, on_date: date, title: str) -> HevyWorkout:
    return HevyWorkout(
        external_id=external_id,
        domain="hevy",
        date=on_date,
        source=Source.HEVY_API.value,
        title=title,
    )


async def test_sessions_before_the_window_are_visible_but_not_counted(db_session, legacy_owner_roots):
    db_session.add_all(
        [
            _workout("w_sat", DAY - timedelta(days=3), "Push"),   # Saturday, in period
            _workout("w_tue", DAY - timedelta(days=7), "Pull"),   # a week ago, outside
            _workout("w_old", DAY - timedelta(days=40), "Legs"),  # far outside, gone
        ]
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id, on_date=DAY, period_days=7)
    hevy = ctx["hevy"]

    # The count keeps window semantics — it is the *cadence* that was missing.
    assert hevy["total_workouts"] == 1
    assert [s["date"] for s in hevy["sessions"]] == [
        (DAY - timedelta(days=7)).isoformat(),
        (DAY - timedelta(days=3)).isoformat(),
    ], "chronological, one period back, and the 40-day-old session stays out"
    assert [s["in_period"] for s in hevy["sessions"]] == [False, True]
    assert hevy["sessions"][1]["title"] == "Push"


async def test_recovery_covers_the_period_not_just_the_latest_day(db_session, legacy_owner_roots):
    """A "weekly" report used to be handed one night of recovery data."""
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

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id, on_date=DAY, period_days=7)

    assert [d["date"] for d in ctx["days"]] == [
        (DAY - timedelta(days=i)).isoformat() for i in range(6, -1, -1)
    ], "seven days, chronological — the two older rows stay outside the period"
    assert ctx["days"][-1]["sleep_score"] == 80
    assert ctx["days"][-1]["sleep_hours"] == 7.0
    # The all-time count is still there, but it is nine, not the period.
    assert ctx["garmin"]["total_days_logged"] == 9


async def test_period_is_handed_the_period_before_it_to_compare_against(db_session, legacy_owner_roots):
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
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        on_date=DAY, period_days=7
    ))["period_stats"]

    assert stats["current"]["start"] == (DAY - timedelta(days=6)).isoformat()
    assert stats["previous"]["end"] == (DAY - timedelta(days=7)).isoformat()
    assert stats["current"]["sleep_score"] == 90.0
    assert stats["previous"]["sleep_score"] == 70.0
    # Coverage travels with the mean: a "better week" off two nights isn't one.
    assert stats["current"]["garmin_days"] == 7
    assert stats["current"]["workouts"] == 1
    assert stats["previous"]["workouts"] == 2


async def test_the_window_holds_only_days_that_are_over(db_session, monkeypatch, legacy_owner_roots):
    """Generated just after midnight, the report treated the current date as a
    seventh day of the window and read its missing log as a day its owner had
    skipped — while he was still awake on it. The window ends yesterday and holds
    seven closed days instead."""
    monkeypatch.setattr(digest_service, "today_local", lambda: DAY)  # report run on DAY

    # Garmin has written today's row, but the watch has scored nothing on it yet.
    db_session.add(
        GarminDaily(domain="garmin", source=Source.GARMIN_API.value, date=DAY)
    )
    for i in range(1, 8):
        db_session.add(
            GarminDaily(
                domain="garmin",
                source=Source.GARMIN_API.value,
                date=DAY - timedelta(days=i),
                sleep_score=85,
                resting_hr=59,
            )
        )
    await db_session.commit()

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id, on_date=DAY, period_days=7)
    meta, current = ctx["report_meta"], ctx["period_stats"]["current"]

    assert meta["period_end"] == (DAY - timedelta(days=1)).isoformat()
    assert meta["period_start"] == (DAY - timedelta(days=7)).isoformat()
    assert meta["report_date"] == DAY.isoformat(), "generated today, about last week"
    assert current["days"] == 7
    assert current["garmin_days"] == 7, "seven closed days, all of them scored"
    assert [d["date"] for d in ctx["days"]][-1] == (DAY - timedelta(days=1)).isoformat()


async def test_a_past_period_ends_on_its_own_date(db_session, monkeypatch, legacy_owner_roots):
    """Regenerate an old digest and nothing shifts: every day in it is long over."""
    monkeypatch.setattr(digest_service, "today_local", lambda: DAY + timedelta(days=30))

    meta = (await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        on_date=DAY, period_days=7
    ))["report_meta"]

    assert meta["period_end"] == DAY.isoformat()
    assert meta["period_start"] == (DAY - timedelta(days=6)).isoformat()


async def test_the_day_table_joins_the_domains_by_date(db_session, monkeypatch, legacy_owner_roots):
    """The links the report kept failing to find need the domains on one row."""
    from vitals.services import nutrition_service, signals_service

    monkeypatch.setattr(digest_service, "today_local", lambda: DAY + timedelta(days=1))
    session_day = DAY - timedelta(days=3)

    db_session.add_all(
        [
            _workout("w_joined", session_day, "Full body"),
            GarminDaily(
                domain="garmin",
                source=Source.GARMIN_API.value,
                date=session_day + timedelta(days=1),
                sleep_score=61,
                hrv_avg=41.0,
            ),
        ]
    )
    await nutrition_service.log_meal(
        db_session, on_date=session_day, name="ужин", calories=800, protein_g=60.0
    )
    await signals_service.set_day_context(
        db_session, session_day, answers={"where": "office", "gym": True, "load": "heavy"}
    )
    await db_session.commit()

    days = {
        d["date"]: d
        for d in (await digest_service.assemble_context(
            db_session,
            subject_id=legacy_owner_roots.subject_id,
            on_date=DAY, period_days=7
        ))["days"]
    }

    trained = days[session_day.isoformat()]
    assert trained["workout"]["title"] == "Full body"
    assert trained["calories"] == 800
    assert trained["protein_g"] == 60.0
    assert trained["day"]["load"] == "heavy"
    # The night after the session, on its own row — the shape a link is read from.
    assert days[(session_day + timedelta(days=1)).isoformat()]["hrv_avg"] == 41.0


async def test_training_cadence_survives_the_window_edge(db_session, legacy_owner_roots):
    """The gap between sessions is the number a window boundary cannot move — and
    the one the narrative can quote without explaining the boundary first."""
    db_session.add_all(
        [
            _workout("w1", DAY - timedelta(days=10), "Full body"),
            _workout("w2", DAY - timedelta(days=7), "Full body"),
            _workout("w3", DAY - timedelta(days=3), "Full body"),
        ]
    )
    await db_session.commit()

    hevy = (await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        on_date=DAY, period_days=7
    ))["hevy"]

    assert hevy["total_workouts"] == 1, "the window edge still cuts the count"
    assert hevy["mean_gap_days"] == 3.5, "the rhythm does not move with it"


async def test_labs_trends_show_drift_that_stays_inside_the_range(db_session, legacy_owner_roots):
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

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id, on_date=DAY, period_days=7)

    assert ctx["labs"]["out_of_range"] == [], "every value is inside its range"
    trends = {t["marker"]: t for t in ctx["labs"]["trends"]}
    assert list(trends) == ["Ферритин"]
    assert [p["value"] for p in trends["Ферритин"]["points"]] == [120.0, 95.0, 80.0]
    assert trends["Ферритин"]["ref_low"] == 30.0


async def test_a_long_window_carries_all_of_its_signals(db_session, legacy_owner_roots):
    """The signals block is handed to the narrative as "the period, chronologically".
    Read with the service's screen-sized default, a long window lost everything past
    the 200th newest row — and the days it lost were the oldest ones, so the period
    silently started later than its own header said."""
    from vitals.models.signals import Signal

    days, per_day = 30, 10
    db_session.add_all(
        [
            Signal(
                date=DAY - timedelta(days=i),
                domain="signals",
                source=Source.TELEGRAM.value,
                kind="symptom",
                key="headache",
                batch_id=f"b{i}_{n}",
                note=f"день {i}",
            )
            for i in range(days)
            for n in range(per_day)
        ]
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        on_date=DAY, period_days=days
    )

    assert len(ctx["signals"]) == days * per_day
    assert ctx["signals"][0]["date"] == (DAY - timedelta(days=days - 1)).isoformat(), (
        "the first day of the window, not the first day that survived the cut"
    )


async def test_brief_context_stays_a_single_day(db_session, legacy_owner_roots):
    """The explicit brief mode stays compact even though a 1-day report does not."""
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

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        on_date=DAY,
        period_days=1,
        mode=digest_service.REPORT_MODE_BRIEF,
    )
    assert "days" not in ctx["garmin"]
    assert "period_stats" not in ctx
