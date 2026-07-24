"""Weight service tests — manual-over-Garmin priority, Navy/LBM derivation,
noise alerts, and chart-series assembly."""
from __future__ import annotations

from datetime import date

import pytest
from freezegun import freeze_time

from vitals.enums import Source
from vitals.services import alerts_service, weight_service
from vitals.utils.timeutils import today_local



async def test_log_weight_creates_active_row(db_session):
    w = await weight_service.log_weight(
        db_session, on_date=date(2026, 6, 1), weight_kg=88.0
    )
    await db_session.commit()
    assert w.superseded is False
    active = await weight_service.get_active_weight(db_session, date(2026, 6, 1))
    assert active is not None and active.weight_kg == 88.0


async def test_manual_supersedes_garmin_same_date(db_session):
    d = date(2026, 6, 2)
    await weight_service.log_weight(
        db_session, on_date=d, weight_kg=89.5, source=Source.GARMIN_API.value
    )
    await weight_service.log_weight(
        db_session, on_date=d, weight_kg=88.0, source=Source.MANUAL.value
    )
    await db_session.commit()

    active = await weight_service.get_active_weight(db_session, d)
    assert active.source == Source.MANUAL.value
    assert active.weight_kg == 88.0
    # Both rows are kept (data lake); exactly one is active.
    all_rows = (await weight_service.list_active_weights(db_session))
    assert len([r for r in all_rows if r.date == d]) == 1


async def test_garmin_does_not_override_existing_manual(db_session):
    d = date(2026, 6, 3)
    await weight_service.log_weight(
        db_session, on_date=d, weight_kg=88.0, source=Source.MANUAL.value
    )
    await weight_service.log_weight(
        db_session, on_date=d, weight_kg=90.0, source=Source.GARMIN_API.value
    )
    await db_session.commit()

    active = await weight_service.get_active_weight(db_session, d)
    assert active.source == Source.MANUAL.value
    assert active.weight_kg == 88.0


async def test_same_source_reentry_supersedes_not_overwrites(db_session):
    """A second manual entry for the same date must NOT silently overwrite the
    first: the old row is kept (flagged superseded), a new active row carries the
    new value. Data-lake principle — never destroy a prior reading (a correction
    or a re-entry stays recoverable)."""
    from sqlalchemy import select

    from vitals.models.weight import WeightLog

    d = date(2026, 6, 4)
    a = await weight_service.log_weight(db_session, on_date=d, weight_kg=88.0)
    b = await weight_service.log_weight(db_session, on_date=d, weight_kg=87.5)
    await db_session.commit()

    # A new row was created — the old one was not mutated in place.
    assert a.id != b.id
    assert b.superseded is False and b.weight_kg == 87.5

    # Exactly one active row, and it holds the new value.
    active = await weight_service.get_active_weight(db_session, d)
    assert active.id == b.id and active.weight_kg == 87.5

    # The previous reading survives in the data lake (superseded, value intact).
    rows = (
        await db_session.execute(select(WeightLog).where(WeightLog.date == d))
    ).scalars().all()
    assert len(rows) == 2
    old = next(r for r in rows if r.id == a.id)
    assert old.superseded is True and old.weight_kg == 88.0


async def test_body_measurement_computes_navy_and_lbm(db_session):
    d = date(2026, 6, 5)
    await weight_service.log_weight(db_session, on_date=d, weight_kg=88.0)
    m = await weight_service.upsert_body_measurement(
        db_session, on_date=d, neck_cm=38, waist_cm=85
    )
    await db_session.commit()
    assert m.body_fat_pct == pytest.approx(14.52, abs=0.05)
    assert m.lbm_kg == pytest.approx(88.0 * (1 - 14.52 / 100), abs=0.05)


async def test_lbm_recomputed_when_weight_changes(db_session):
    d = date(2026, 6, 6)
    await weight_service.log_weight(db_session, on_date=d, weight_kg=90.0)
    m = await weight_service.upsert_body_measurement(
        db_session, on_date=d, neck_cm=38, waist_cm=85
    )
    lbm_before = m.lbm_kg
    # New weight for the same date → LBM should follow.
    await weight_service.log_weight(db_session, on_date=d, weight_kg=85.0)
    await db_session.commit()
    await db_session.refresh(m)
    assert m.lbm_kg is not None and m.lbm_kg < lbm_before


async def test_measurement_without_weight_has_null_lbm(db_session):
    d = date(2026, 6, 7)
    m = await weight_service.upsert_body_measurement(
        db_session, on_date=d, neck_cm=38, waist_cm=85
    )
    await db_session.commit()
    assert m.body_fat_pct is not None
    assert m.lbm_kg is None


async def test_partial_measurement_update_preserves_other_fields(db_session):
    d = date(2026, 6, 8)
    await weight_service.log_weight(db_session, on_date=d, weight_kg=88.0)
    first = await weight_service.upsert_body_measurement(
        db_session, on_date=d, neck_cm=38, waist_cm=85, hips_cm=100
    )
    await db_session.commit()
    assert first.body_fat_pct is not None

    # A partial call (e.g. MCP log_measurement given just one field) must merge
    # onto the existing row, not blank the fields it didn't mention.
    second = await weight_service.upsert_body_measurement(
        db_session, on_date=d, waist_cm=86
    )
    await db_session.commit()
    assert second.id == first.id
    assert second.neck_cm == 38
    assert second.hips_cm == 100
    assert second.waist_cm == 86
    assert second.body_fat_pct is not None


async def test_noise_alert_raise_and_resolve(db_session):
    # Active marker covering "today" → info alert raised.
    await weight_service.add_noise_marker(
        db_session,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 30),
        reason="creatine loading",
    )
    await weight_service.refresh_noise_alert(db_session, on_date=date(2026, 6, 10))
    await db_session.commit()
    active = await alerts_service.list_active(db_session, domain="weight")
    assert any(a.alert_key == weight_service.NOISE_ALERT_KEY for a in active)
    assert active[0].severity == "info"

    # A day outside the range → the alert resolves.
    await weight_service.refresh_noise_alert(db_session, on_date=date(2026, 7, 10))
    await db_session.commit()
    active2 = await alerts_service.list_active(db_session, domain="weight")
    assert not any(a.alert_key == weight_service.NOISE_ALERT_KEY for a in active2)


async def test_dismissed_noise_alert_returns_after_local_midnight(db_session):
    """The daily-nag contract crosses the *local* midnight, not UTC's.

    Both frozen instants are the same UTC day; only Chisinau's calendar flips
    between them. Dismissing at 23:30 local must hide the alert for the rest of
    that local day and let it back at 00:30 the next one — if the comparison ever
    slipped to UTC, the second half of every evening would silently un-dismiss.
    """
    await weight_service.add_noise_marker(
        db_session, start_date=date(2026, 6, 1), reason="creatine loading"
    )

    with freeze_time("2026-06-10 20:30:00"):  # 23:30 local (UTC+3 in June)
        assert today_local() == date(2026, 6, 10)
        alert = await weight_service.refresh_noise_alert(db_session)
        assert alert is not None
        await alerts_service.resolve_alert(db_session, alert.id)
        await db_session.commit()
        # Same local day → stays dismissed.
        assert await weight_service.refresh_noise_alert(db_session) is None

    with freeze_time("2026-06-10 21:30:00"):  # 00:30 local — next local day
        assert today_local() == date(2026, 6, 11)
        again = await weight_service.refresh_noise_alert(db_session)
        await db_session.commit()
        assert again is not None and again.id != alert.id


async def test_chart_series_excludes_noise_from_trend(db_session):
    # Clean downtrend (100→90 over 06-01..06-11) with a water-weight spike on a
    # day we mark as noise. The noise range must fully drop out of the MA, the
    # regression trend, and the projection — but stay visible in raw + overlay.
    base = date(2026, 6, 1)
    from datetime import timedelta

    for i in range(11):
        await weight_service.log_weight(
            db_session, on_date=base + timedelta(days=i), weight_kg=100.0 - i
        )
    # Spike day (water weight) we mark as noise — overwrites 06-06 in place.
    await weight_service.log_weight(
        db_session, on_date=base + timedelta(days=5), weight_kg=120.0
    )
    await weight_service.add_noise_marker(
        db_session,
        start_date=base + timedelta(days=5),
        end_date=base + timedelta(days=5),
        reason="sodium",
    )
    await db_session.commit()

    series = await weight_service.chart_series(db_session, goal_kg=85.0)

    # Trend/projection are computed on the noise-excluded series: a clean −1kg/day
    # line reaches 85kg on 2026-06-16 (i=15 from 06-01), slope ≈ −7kg/week.
    assert series["trend"]["slope_per_week"] == pytest.approx(-7.0, abs=0.05)
    assert series["projection"] is not None
    assert series["projection"]["date"] == "2026-06-16"

    # The rolling mean on 06-07 EXCLUDES the 06-06 spike → (100+99+98+97+96+94)/6.
    ma_points = {p["date"]: p["weight_kg"] for p in series["trend_ma"]}
    assert ma_points["2026-06-07"] == pytest.approx(97.333, abs=0.05)
    assert "2026-06-06" not in ma_points  # noise day has no MA point

    # The noise range is still surfaced for the chart overlay…
    assert len(series["noise"]) == 1
    # …and the raw scatter still shows the spike (nothing is hidden from the user).
    assert any(p["date"] == "2026-06-06" and p["weight_kg"] == 120.0 for p in series["raw"])


async def test_weight_check_constraint_rejects_nonpositive(db_session):
    """The DB-level CHECK (weight_kg > 0) rejects junk values — a buggy importer
    or bad input can't persist a non-physical weight.

    Inserted straight through the model: ``log_weight`` now refuses this input
    before the DB sees it (see the range tests below), and this test exists to
    prove the constraint underneath is still the last line of defence."""
    from sqlalchemy.exc import IntegrityError

    from vitals.models.weight import DOMAIN, WeightLog

    db_session.add(
        WeightLog(
            date=date(2026, 6, 1),
            domain=DOMAIN,
            source=Source.MANUAL.value,
            weight_kg=0.0,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_delete_weight_log_reactivates_superseded(db_session):
    d = date(2026, 6, 2)
    # 1. Garmin weight log
    w_garmin = await weight_service.log_weight(
        db_session, on_date=d, weight_kg=89.5, source=Source.GARMIN_API.value
    )
    # 2. Manual weight log (supersedes Garmin)
    w_manual = await weight_service.log_weight(
        db_session, on_date=d, weight_kg=88.0, source=Source.MANUAL.value
    )
    await db_session.commit()

    active = await weight_service.get_active_weight(db_session, d)
    assert active.id == w_manual.id
    assert w_garmin.superseded is True

    # 3. Delete manual log -> Garmin log becomes active again
    deleted = await weight_service.delete_weight_log(db_session, w_manual.id)
    await db_session.commit()
    assert deleted is True

    active2 = await weight_service.get_active_weight(db_session, d)
    assert active2.id == w_garmin.id
    assert active2.superseded is False


async def test_delete_body_measurement(db_session):
    d = date(2026, 6, 5)
    m = await weight_service.upsert_body_measurement(
        db_session, on_date=d, neck_cm=38, waist_cm=85
    )
    await db_session.commit()

    measurements = await weight_service.list_body_measurements(db_session)
    assert len(measurements) == 1

    deleted = await weight_service.delete_body_measurement(db_session, m.id)
    await db_session.commit()
    assert deleted is True

    measurements2 = await weight_service.list_body_measurements(db_session)
    assert len(measurements2) == 0


async def test_delete_noise_marker(db_session):
    m = await weight_service.add_noise_marker(
        db_session,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 10),
        reason="sodium",
    )
    await db_session.commit()

    markers = await weight_service.list_noise_markers(db_session)
    assert len(markers) == 1

    deleted = await weight_service.delete_noise_marker(db_session, m.id)
    await db_session.commit()
    assert deleted is True

    markers2 = await weight_service.list_noise_markers(db_session)
    assert len(markers2) == 0


async def test_delete_progress_photo(db_session):
    p = await weight_service.add_progress_photo(
        db_session,
        on_date=date(2026, 6, 1),
        file_key="uploads/test_photo.jpg",
        note="Test photo",
    )
    await db_session.commit()

    photos = await weight_service.list_progress_photos(db_session)
    assert len(photos) == 1

    file_key = await weight_service.delete_progress_photo(db_session, p.id)
    await db_session.commit()
    assert file_key == "uploads/test_photo.jpg"

    photos2 = await weight_service.list_progress_photos(db_session)
    assert len(photos2) == 0



# ── Write-path validation (S2) ────────────────────────────────────────────────
# The MCP tools reach these services directly, with no HTML form to bound the
# numbers, so the service itself has to reject nonsense.
@pytest.mark.parametrize("bad_kg", [0, -5, 900, float("nan")])
async def test_log_weight_rejects_implausible_weight(db_session, bad_kg):
    with pytest.raises(ValueError):
        await weight_service.log_weight(
            db_session, on_date=date(2026, 6, 20), weight_kg=bad_kg
        )


async def test_update_weight_log_rejects_implausible_weight(db_session):
    row = await weight_service.log_weight(
        db_session, on_date=date(2026, 6, 21), weight_kg=88.0
    )
    await db_session.commit()
    with pytest.raises(ValueError):
        await weight_service.update_weight_log(
            db_session, row.id, on_date=date(2026, 6, 21), weight_kg=900.0
        )


@pytest.mark.parametrize(
    "field, value",
    [("neck_cm", 0), ("waist_cm", 500), ("hips_cm", -30)],
)
async def test_upsert_measurement_rejects_implausible_circumference(
    db_session, field, value
):
    with pytest.raises(ValueError):
        await weight_service.upsert_body_measurement(
            db_session, on_date=date(2026, 6, 22), **{field: value}
        )


async def test_plausible_measurement_still_saves(db_session):
    """The bounds must not get in the way of a normal entry."""
    row = await weight_service.upsert_body_measurement(
        db_session, on_date=date(2026, 6, 23), neck_cm=39.0, waist_cm=86.0
    )
    await db_session.commit()
    assert row.neck_cm == 39.0 and row.waist_cm == 86.0


# ── Editing a measurement's date keeps the fields not passed (D1) ─────────────
async def test_update_measurement_date_change_keeps_untouched_fields(db_session):
    """Moving a measurement to another date used to blank every field the caller
    didn't repeat: the row was deleted first, so the partial merge on the new date
    had nothing left to merge with. The MCP edit tool passes one field at a time,
    so this quietly destroyed neck/hips and the derived body-fat % / LBM."""
    await weight_service.log_weight(
        db_session, on_date=date(2026, 7, 2), weight_kg=88.0
    )
    row = await weight_service.upsert_body_measurement(
        db_session,
        on_date=date(2026, 7, 1),
        neck_cm=39.0,
        waist_cm=86.0,
        hips_cm=99.0,
        note="morning",
    )
    await db_session.commit()
    assert row.body_fat_pct is not None

    # Only the date and the waist change — neck/hips/note must survive.
    moved = await weight_service.update_body_measurement(
        db_session, row.id, on_date=date(2026, 7, 2), waist_cm=85.0
    )
    await db_session.commit()

    assert moved is not None
    assert moved.date == date(2026, 7, 2)
    assert moved.waist_cm == 85.0
    assert moved.neck_cm == 39.0
    assert moved.hips_cm == 99.0
    assert moved.note == "morning"
    # Derived values come back too, now that their inputs survived the move.
    assert moved.body_fat_pct is not None
    assert moved.lbm_kg is not None

    rows = await weight_service.list_body_measurements(db_session)
    assert len(rows) == 1
