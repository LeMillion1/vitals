"""Weight service tests — manual-over-Garmin priority, Navy/LBM derivation,
noise alerts, and chart-series assembly."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from freezegun import freeze_time
from sqlalchemy import func, select

from vitals.enums import Source
from vitals.models.weight import WeightLog
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.services import alerts_service, weight_service
from vitals.services.conflicts import engine
from vitals.utils.timeutils import today_local



async def _garmin_weight(db_session, owner_write, value: float, *, on_date):
    """One Garmin-sourced weigh-in with the provenance the domain requires.

    A Garmin fact is only valid alongside the account connection it arrived
    through and the payload it arrived in, so the test builds both rather than
    asserting a shape the service would refuse.
    """
    from vitals.enums import (
        Domain,
        IntegrationConnectionStatus,
        IntegrationConnectionType,
        IntegrationProvider,
    )
    from vitals.models.tenancy import IntegrationConnection
    from vitals.services import raw_payload_service

    connection = await db_session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == owner_write.subject_id,
            IntegrationConnection.provider == IntegrationProvider.GARMIN.value,
        )
    )
    if connection is None:
        connection = IntegrationConnection(
            subject_id=owner_write.subject_id,
            provider=IntegrationProvider.GARMIN.value,
            connection_type=IntegrationConnectionType.ACCOUNT.value,
            external_account_discriminator="synthetic-garmin",
            status=IntegrationConnectionStatus.ACTIVE.value,
        )
        db_session.add(connection)
        await db_session.flush()
    raw = await raw_payload_service.upsert_owned_raw_payload(
        db_session,
        identity=owner_write.identity,
        integration_connection_id=connection.id,
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        external_id=f"garmin:weight:{on_date.isoformat()}",
        payload={"date": on_date.isoformat(), "weight_kg": value},
    )
    return await weight_service.log_weight(
        db_session,
        on_date=on_date,
        weight_kg=value,
        source=Source.GARMIN_API.value,
        raw_payload_id=raw.id,
        identity=owner_write.identity,
        integration_connection_id=connection.id,
        prepared_weight_write=await owner_write.weight_write(on_date),
    )


async def test_log_weight_creates_active_row(db_session, owner_write):
    w = await weight_service.log_weight(
        db_session,
        on_date=date(2026, 6, 1),
        weight_kg=88.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(date(2026, 6, 1)),
    )
    await db_session.commit()
    assert w.superseded is False
    active = await weight_service.get_active_weight(
        db_session,
        date(2026, 6, 1),
        subject_id=owner_write.subject_id,
    )
    assert active is not None and active.weight_kg == 88.0


async def test_historical_rawless_garmin_weight_uses_reviewed_checkpoint(
    db_session,
    legacy_owner_roots,
):
    historical = WeightLog(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=None,
        integration_connection_id=None,
        date=date(2020, 1, 2),
        domain="weight",
        source=Source.GARMIN_API.value,
        weight_kg=88.0,
        raw_payload_id=None,
        superseded=False,
    )
    db_session.add(historical)
    await db_session.flush()
    stamp = datetime(2026, 8, 20, tzinfo=UTC)
    db_session.add(
        OwnershipBackfillCheckpoint(
            phase_key="stage3.channel_optional.weight_logs.v1.weight_logs",
            subject_id=legacy_owner_roots.subject_id,
            status="completed",
            scan_high_watermark_id=historical.id,
            snapshot_rows=1,
            last_scanned_id=historical.id,
            scanned_rows=1,
            updated_rows=1,
            unchanged_rows=0,
            data_checksum_before="a" * 64,
            data_checksum_after="b" * 64,
            ownership_checksum_after="b" * 64,
            started_at=stamp,
            updated_at=stamp,
            completed_at=stamp,
        )
    )
    await db_session.commit()

    rows = await weight_service.list_active_weights(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
    )

    assert [row.id for row in rows] == [historical.id]


async def test_manual_supersedes_garmin_same_date(db_session, owner_write):
    d = date(2026, 6, 2)
    await _garmin_weight(
        db_session,
        owner_write,
        89.5,
        on_date=d,
    )
    await weight_service.log_weight(
        db_session,
        on_date=d,
        weight_kg=88.0,
        source=Source.MANUAL.value,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(d),
    )
    await db_session.commit()

    active = await weight_service.get_active_weight(
        db_session,
        d,
        subject_id=owner_write.subject_id,
    )
    assert active.source == Source.MANUAL.value
    assert active.weight_kg == 88.0
    # Both rows are kept (data lake); exactly one is active.
    all_rows = (await weight_service.list_active_weights(
        db_session,
        subject_id=owner_write.subject_id,
    ))
    assert len([r for r in all_rows if r.date == d]) == 1


async def test_garmin_does_not_override_existing_manual(db_session, owner_write):
    d = date(2026, 6, 3)
    await weight_service.log_weight(
        db_session,
        on_date=d,
        weight_kg=88.0,
        source=Source.MANUAL.value,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(d),
    )
    await _garmin_weight(
        db_session,
        owner_write,
        90.0,
        on_date=d,
    )
    await db_session.commit()

    active = await weight_service.get_active_weight(
        db_session,
        d,
        subject_id=owner_write.subject_id,
    )
    assert active.source == Source.MANUAL.value
    assert active.weight_kg == 88.0


async def test_repeated_garmin_import_under_manual_weight_is_deduplicated(db_session, owner_write):
    d = date(2026, 6, 3)
    await weight_service.log_weight(
        db_session,
        on_date=d,
        weight_kg=84.0,
        source=Source.MANUAL.value,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(d),
    )
    for _ in range(2):
        await _garmin_weight(
            db_session,
            owner_write,
            85.0,
            on_date=d,
        )

    rows = (
        await db_session.execute(select(WeightLog).where(WeightLog.date == d))
    ).scalars().all()
    assert len(rows) == 2
    assert len([row for row in rows if row.source == Source.GARMIN_API.value]) == 1


async def test_inbound_dedupe_does_not_swallow_a_manual_reentry(db_session, owner_write):
    d = date(2026, 6, 3)
    await weight_service.log_weight(
        db_session,
        on_date=d,
        weight_kg=85.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(d),
    )
    await weight_service.log_weight(
        db_session,
        on_date=d,
        weight_kg=84.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(d),
    )
    newest = await weight_service.log_weight(
        db_session,
        on_date=d,
        weight_kg=85.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(d),
    )

    active = await weight_service.get_active_weight(
        db_session,
        d,
        subject_id=owner_write.subject_id,
    )
    assert active is newest
    assert active.weight_kg == 85.0
    assert (
        await db_session.execute(
            select(func.count()).select_from(WeightLog).where(WeightLog.date == d)
        )
    ).scalar_one() == 3


async def test_same_source_reentry_supersedes_not_overwrites(db_session, owner_write):
    """A second manual entry for the same date must NOT silently overwrite the
    first: the old row is kept (flagged superseded), a new active row carries the
    new value. Data-lake principle — never destroy a prior reading (a correction
    or a re-entry stays recoverable)."""
    from sqlalchemy import select

    from vitals.models.weight import WeightLog

    d = date(2026, 6, 4)
    a = await weight_service.log_weight(
        db_session,
        on_date=d,
        weight_kg=88.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(d),
    )
    b = await weight_service.log_weight(
        db_session,
        on_date=d,
        weight_kg=87.5,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(d),
    )
    await db_session.commit()

    # A new row was created — the old one was not mutated in place.
    assert a.id != b.id
    assert b.superseded is False and b.weight_kg == 87.5

    # Exactly one active row, and it holds the new value.
    active = await weight_service.get_active_weight(
        db_session,
        d,
        subject_id=owner_write.subject_id,
    )
    assert active.id == b.id and active.weight_kg == 87.5

    # The previous reading survives in the data lake (superseded, value intact).
    rows = (
        await db_session.execute(select(WeightLog).where(WeightLog.date == d))
    ).scalars().all()
    assert len(rows) == 2
    old = next(r for r in rows if r.id == a.id)
    assert old.superseded is True and old.weight_kg == 88.0


async def test_repeated_identical_import_does_not_pile_up_rows(db_session, owner_write):
    """Re-importing the same weigh-in must not append a clone. Garmin's daily
    bundle repeats today's weight on every poll; each poll used to add a row and
    supersede the last, so a day grew a stack of identical rows and deleting the
    visible one just promoted the next."""
    from sqlalchemy import select

    from vitals.models.weight import WeightLog

    d = date(2026, 6, 6)
    for _ in range(3):
        await _garmin_weight(
            db_session,
            owner_write,
            105.0,
            on_date=d,
        )
    await db_session.commit()

    rows = (
        await db_session.execute(select(WeightLog).where(WeightLog.date == d))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].superseded is False

    # A genuinely different reading from the same source still lands as its own row.
    await _garmin_weight(
        db_session,
        owner_write,
        104.6,
        on_date=d,
    )
    await db_session.commit()
    rows = (
        await db_session.execute(select(WeightLog).where(WeightLog.date == d))
    ).scalars().all()
    assert len(rows) == 2


async def test_body_measurement_computes_navy_and_lbm(db_session, owner_write):
    d = date(2026, 6, 5)
    await weight_service.log_weight(
        db_session,
        on_date=d,
        weight_kg=88.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(d),
    )
    m = await weight_service.upsert_body_measurement(
        db_session,
        on_date=d,
        neck_cm=38,
        waist_cm=85,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await db_session.commit()
    assert m.body_fat_pct == pytest.approx(14.52, abs=0.05)
    assert m.lbm_kg == pytest.approx(88.0 * (1 - 14.52 / 100), abs=0.05)


async def test_lbm_recomputed_when_weight_changes(db_session, owner_write):
    d = date(2026, 6, 6)
    await weight_service.log_weight(
        db_session,
        on_date=d,
        weight_kg=90.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(d),
    )
    m = await weight_service.upsert_body_measurement(
        db_session,
        on_date=d,
        neck_cm=38,
        waist_cm=85,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    lbm_before = m.lbm_kg
    # New weight for the same date → LBM should follow.
    await weight_service.log_weight(
        db_session,
        on_date=d,
        weight_kg=85.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(d),
    )
    await db_session.commit()
    await db_session.refresh(m)
    assert m.lbm_kg is not None and m.lbm_kg < lbm_before


async def test_measurement_without_weight_has_null_lbm(db_session, owner_write):
    d = date(2026, 6, 7)
    m = await weight_service.upsert_body_measurement(
        db_session,
        on_date=d,
        neck_cm=38,
        waist_cm=85,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await db_session.commit()
    assert m.body_fat_pct is not None
    assert m.lbm_kg is None


async def test_partial_measurement_update_preserves_other_fields(db_session, owner_write):
    d = date(2026, 6, 8)
    await weight_service.log_weight(
        db_session,
        on_date=d,
        weight_kg=88.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(d),
    )
    first = await weight_service.upsert_body_measurement(
        db_session,
        on_date=d,
        neck_cm=38,
        waist_cm=85,
        hips_cm=100,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await db_session.commit()
    assert first.body_fat_pct is not None

    # A partial call (e.g. MCP log_measurement given just one field) must merge
    # onto the existing row, not blank the fields it didn't mention.
    second = await weight_service.upsert_body_measurement(
        db_session,
        on_date=d,
        waist_cm=86,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await db_session.commit()
    assert second.id == first.id
    assert second.neck_cm == 38
    assert second.hips_cm == 100
    assert second.waist_cm == 86
    assert second.body_fat_pct is not None


async def test_noise_alert_raise_and_resolve(db_session, owner_write):
    # Active marker covering "today" → info alert raised.
    await weight_service.add_noise_marker(
        db_session,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 30),
        reason="creatine loading",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 1)),
    )
    await weight_service.refresh_noise_alert(
        db_session,
        on_date=date(2026, 6, 10),
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 10)),
    )
    await db_session.commit()
    active = await alerts_service.list_active(db_session, domain="weight", subject_id=owner_write.subject_id)
    assert any(a.alert_key == weight_service.NOISE_ALERT_KEY for a in active)
    assert active[0].severity == "info"

    # A day outside the range → the alert resolves.
    await weight_service.refresh_noise_alert(
        db_session,
        on_date=date(2026, 7, 10),
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 7, 10)),
    )
    await db_session.commit()
    active2 = await alerts_service.list_active(db_session, domain="weight", subject_id=owner_write.subject_id)
    assert not any(a.alert_key == weight_service.NOISE_ALERT_KEY for a in active2)


async def test_dismissed_noise_alert_returns_after_local_midnight(db_session, owner_write):
    """The daily-nag contract crosses the *local* midnight, not UTC's.

    Both frozen instants are the same UTC day; only Chisinau's calendar flips
    between them. Dismissing at 23:30 local must hide the alert for the rest of
    that local day and let it back at 00:30 the next one — if the comparison ever
    slipped to UTC, the second half of every evening would silently un-dismiss.
    """
    await weight_service.add_noise_marker(
        db_session,
        start_date=date(2026, 6, 1),
        reason="creatine loading",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 1)),
    )

    with freeze_time("2026-06-10 20:30:00"):  # 23:30 local (UTC+3 in June)
        assert today_local() == date(2026, 6, 10)
        alert = await weight_service.refresh_noise_alert(
            db_session,
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(today_local()),
        )
        assert alert is not None
        await alerts_service.resolve_alert(db_session, alert.id, subject_id=owner_write.subject_id)
        await db_session.commit()
        # Same local day → stays dismissed.
        assert await weight_service.refresh_noise_alert(
            db_session,
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(today_local()),
        ) is None

    with freeze_time("2026-06-10 21:30:00"):  # 00:30 local — next local day
        assert today_local() == date(2026, 6, 11)
        again = await weight_service.refresh_noise_alert(
            db_session,
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(today_local()),
        )
        await db_session.commit()
        assert again is not None and again.id != alert.id


async def test_chart_series_excludes_noise_from_trend(db_session, owner_write):
    # Clean downtrend (100→90 over 06-01..06-11) with a water-weight spike on a
    # day we mark as noise. The noise range must fully drop out of the MA, the
    # regression trend, and the projection — but stay visible in raw + overlay.
    base = date(2026, 6, 1)
    from datetime import timedelta

    for i in range(11):
        await weight_service.log_weight(
            db_session,
            on_date=base + timedelta(days=i),
            weight_kg=100.0 - i,
            identity=owner_write.identity,
            prepared_weight_write=await owner_write.weight_write(base + timedelta(days=i)),
        )
    # Spike day (water weight) we mark as noise — overwrites 06-06 in place.
    await weight_service.log_weight(
        db_session,
        on_date=base + timedelta(days=5),
        weight_kg=120.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(base + timedelta(days=5)),
    )
    await weight_service.add_noise_marker(
        db_session,
        start_date=base + timedelta(days=5),
        end_date=base + timedelta(days=5),
        reason="sodium",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(base + timedelta(days=5)),
    )
    await db_session.commit()

    series = await weight_service.chart_series(
        db_session,
        goal_kg=85.0,
        subject_id=owner_write.subject_id,
    )

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


async def test_chart_series_weekly_delta_is_last_7_days_not_lifetime_slope(db_session, owner_write):
    """The dashboard's "change over the week" card must show the LAST week, not the
    average weekly rate over the whole log. Steep loss early then a two-week
    plateau: the regression slope stays strongly negative while the real weekly
    movement is zero — the exact case where the old (slope-based) card lied."""
    from datetime import timedelta

    base = date(2026, 6, 1)
    for i in range(10):                       # 06-01..06-10: 100 → 91
        await weight_service.log_weight(
            db_session,
            on_date=base + timedelta(days=i),
            weight_kg=100.0 - i,
            identity=owner_write.identity,
            prepared_weight_write=await owner_write.weight_write(base + timedelta(days=i)),
        )
    for i in range(10, 24):                   # 06-11..06-24: flat at 90
        await weight_service.log_weight(
            db_session,
            on_date=base + timedelta(days=i),
            weight_kg=90.0,
            identity=owner_write.identity,
            prepared_weight_write=await owner_write.weight_write(base + timedelta(days=i)),
        )
    await db_session.commit()

    series = await weight_service.chart_series(
        db_session,
        subject_id=owner_write.subject_id,
    )

    # MA7 on 06-24 and on 06-17 both sit fully inside the plateau → no movement.
    assert series["weekly_delta"] == pytest.approx(0.0, abs=0.01)
    # …while the lifetime slope still reads as a big weekly loss.
    assert series["trend"]["slope_per_week"] < -1.0


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


async def test_delete_weight_log_reactivates_superseded(db_session, owner_write):
    d = date(2026, 6, 2)
    # 1. Garmin weight log
    w_garmin = await _garmin_weight(
        db_session,
        owner_write,
        89.5,
        on_date=d,
    )
    # 2. Manual weight log (supersedes Garmin)
    w_manual = await weight_service.log_weight(
        db_session,
        on_date=d,
        weight_kg=88.0,
        source=Source.MANUAL.value,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(d),
    )
    await db_session.commit()

    active = await weight_service.get_active_weight(
        db_session,
        d,
        subject_id=owner_write.subject_id,
    )
    assert active.id == w_manual.id
    assert w_garmin.superseded is True

    # 3. Delete manual log -> Garmin log becomes active again
    deleted = await weight_service.delete_weight_log(
        db_session,
        w_manual.id,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(),
    )
    await db_session.commit()
    assert deleted is True

    active2 = await weight_service.get_active_weight(
        db_session,
        d,
        subject_id=owner_write.subject_id,
    )
    assert active2.id == w_garmin.id
    assert active2.superseded is False


async def test_delete_body_measurement(db_session, owner_write):
    d = date(2026, 6, 5)
    m = await weight_service.upsert_body_measurement(
        db_session,
        on_date=d,
        neck_cm=38,
        waist_cm=85,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await db_session.commit()

    measurements = await weight_service.list_body_measurements(
        db_session,
        subject_id=owner_write.subject_id,
    )
    assert len(measurements) == 1

    deleted = await weight_service.delete_body_measurement(
        db_session,
        m.id,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await db_session.commit()
    assert deleted is True

    measurements2 = await weight_service.list_body_measurements(
        db_session,
        subject_id=owner_write.subject_id,
    )
    assert len(measurements2) == 0


async def test_delete_noise_marker(db_session, owner_write):
    m = await weight_service.add_noise_marker(
        db_session,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 10),
        reason="sodium",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 1)),
    )
    await db_session.commit()

    markers = await weight_service.list_noise_markers(
        db_session,
        subject_id=owner_write.subject_id,
    )
    assert len(markers) == 1

    deleted = await weight_service.delete_noise_marker(
        db_session,
        m.id,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await db_session.commit()
    assert deleted is True

    markers2 = await weight_service.list_noise_markers(
        db_session,
        subject_id=owner_write.subject_id,
    )
    assert len(markers2) == 0


async def test_delete_progress_photo(db_session, owner_write):
    from vitals.enums import FileAssetPurpose
    from vitals.services import file_asset_service

    asset = await file_asset_service.register_legacy_local(
        db_session,
        subject_id=owner_write.subject_id,
        uploaded_by_user_id=owner_write.identity.actor_user_id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO,
        storage_ref="uploads/test_photo.jpg",
        media_type="image/jpeg",
        size_bytes=11,
        content_sha256="7" * 64,
    )
    p = await weight_service.add_progress_photo(
        db_session,
        on_date=date(2026, 6, 1),
        file_key="uploads/test_photo.jpg",
        note="Test photo",
        identity=owner_write.identity,
        file_asset_id=asset.id,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 1)),
    )
    await db_session.commit()

    photos = await weight_service.list_progress_photos(
        db_session,
        subject_id=owner_write.subject_id,
    )
    assert len(photos) == 1

    deletion = await weight_service.delete_progress_photo(
        db_session,
        p.id,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await db_session.commit()
    assert deletion == weight_service.ProgressPhotoDeletion(
        file_key="uploads/test_photo.jpg",
        file_asset_id=asset.id,
    )

    photos2 = await weight_service.list_progress_photos(
        db_session,
        subject_id=owner_write.subject_id,
    )
    assert len(photos2) == 0



# ── Write-path validation ─────────────────────────────────────────────────────
# The MCP tools reach these services directly, with no HTML form to bound the
# numbers, so the service itself has to reject nonsense.
@pytest.mark.parametrize("bad_kg", [0, -5, 900, float("nan")])
async def test_log_weight_rejects_implausible_weight(db_session, bad_kg, owner_write):
    with pytest.raises(ValueError):
        await weight_service.log_weight(
            db_session,
            on_date=date(2026, 6, 20),
            weight_kg=bad_kg,
            identity=owner_write.identity,
            prepared_weight_write=await owner_write.weight_write(date(2026, 6, 20)),
        )


async def test_update_weight_log_rejects_implausible_weight(db_session, owner_write):
    row = await weight_service.log_weight(
        db_session,
        on_date=date(2026, 6, 21),
        weight_kg=88.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(date(2026, 6, 21)),
    )
    await db_session.commit()
    with pytest.raises(ValueError):
        await weight_service.update_weight_log(
            db_session,
            row.id,
            on_date=date(2026, 6, 21),
            weight_kg=900.0,
            identity=owner_write.identity,
            prepared_weight_write=await owner_write.weight_write(date(2026, 6, 21)),
        )


async def test_editing_superseded_weight_does_not_change_lbm(db_session, owner_write):
    """A retained Garmin fact is not the weight used for body composition."""
    on_date = date(2026, 6, 24)
    inactive = await _garmin_weight(
        db_session,
        owner_write,
        90.0,
        on_date=on_date,
    )
    await weight_service.log_weight(
        db_session,
        on_date=on_date,
        weight_kg=80.0,
        source=Source.MANUAL.value,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(on_date),
    )
    measurement = await weight_service.upsert_body_measurement(
        db_session,
        on_date=on_date,
        neck_cm=39.0,
        waist_cm=86.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(on_date),
    )
    await db_session.commit()
    lbm_before = measurement.lbm_kg

    edited = await weight_service.update_weight_log(
        db_session,
        inactive.id,
        on_date=on_date,
        weight_kg=100.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(on_date),
    )
    await db_session.flush()

    active = await weight_service.get_active_weight(
        db_session,
        on_date,
        subject_id=owner_write.subject_id,
    )
    await db_session.refresh(measurement)
    assert edited is not None and edited.superseded is True
    assert active is not None and active.weight_kg == 80.0
    assert measurement.lbm_kg == lbm_before


async def test_blocked_weight_date_move_keeps_original_row(db_session, monkeypatch, owner_write):
    """A router may commit after rendering 409; the move must still be atomic."""
    original_date = date(2026, 6, 25)
    target_date = date(2026, 6, 26)
    original = await weight_service.log_weight(
        db_session,
        on_date=original_date,
        weight_kg=81.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(original_date),
    )
    original_id = original.id
    await db_session.commit()

    async def block(*args, **kwargs):
        raise engine.ConflictBlocked([])

    # The scoped writer is the only one left, so the block has to come from the
    # prepared enforcement rather than the legacy singleton one.
    monkeypatch.setattr(
        weight_service.engine, "enforce_prepared", block
    )
    with pytest.raises(engine.ConflictBlocked):
        await weight_service.update_weight_log(
            db_session,
            original_id,
            on_date=target_date,
            weight_kg=80.5,
            identity=owner_write.identity,
            prepared_weight_write=await owner_write.weight_write(target_date),
        )

    # Match the web route's caught-409 lifecycle: committing the outer
    # transaction must not make the savepoint's DELETE permanent.
    await db_session.commit()
    preserved = await db_session.get(WeightLog, original_id)
    assert preserved is not None
    assert preserved.date == original_date
    assert preserved.weight_kg == 81.0
    assert await weight_service.get_active_weight(
        db_session,
        target_date,
        subject_id=owner_write.subject_id,
    ) is None


@pytest.mark.parametrize(
    "field, value",
    [("neck_cm", 0), ("waist_cm", 500), ("hips_cm", -30)],
)
async def test_upsert_measurement_rejects_implausible_circumference(
    db_session, field, value, owner_write
):
    with pytest.raises(ValueError):
        await weight_service.upsert_body_measurement(
            db_session,
            on_date=date(2026, 6, 22),
            **{field: value},
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(date(2026, 6, 22)),
        )


async def test_plausible_measurement_still_saves(db_session, owner_write):
    """The bounds must not get in the way of a normal entry."""
    row = await weight_service.upsert_body_measurement(
        db_session,
        on_date=date(2026, 6, 23),
        neck_cm=39.0,
        waist_cm=86.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 23)),
    )
    await db_session.commit()
    assert row.neck_cm == 39.0 and row.waist_cm == 86.0


# ── Editing a measurement's date keeps the fields not passed ─────────────────
async def test_update_measurement_date_change_keeps_untouched_fields(db_session, owner_write):
    """Moving a measurement to another date used to blank every field the caller
    didn't repeat: the row was deleted first, so the partial merge on the new date
    had nothing left to merge with. The MCP edit tool passes one field at a time,
    so this quietly destroyed neck/hips and the derived body-fat % / LBM."""
    await weight_service.log_weight(
        db_session,
        on_date=date(2026, 7, 2),
        weight_kg=88.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(date(2026, 7, 2)),
    )
    row = await weight_service.upsert_body_measurement(
        db_session,
        on_date=date(2026, 7, 1),
        neck_cm=39.0,
        waist_cm=86.0,
        hips_cm=99.0,
        note="morning",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 7, 1)),
    )
    await db_session.commit()
    assert row.body_fat_pct is not None

    # Only the date and the waist change — neck/hips/note must survive.
    moved = await weight_service.update_body_measurement(
        db_session,
        row.id,
        on_date=date(2026, 7, 2),
        waist_cm=85.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 7, 2)),
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

    rows = await weight_service.list_body_measurements(
        db_session,
        subject_id=owner_write.subject_id,
    )
    assert len(rows) == 1


# ── partial=False: a blank field on the edit form means "delete this" ─────────
async def test_update_measurement_non_partial_clears_blanked_fields(db_session, owner_write):
    """The HTML edit form posts every field it renders, so an empty one is the
    owner deleting a value. Under the partial merge it silently came back."""
    await weight_service.log_weight(
        db_session,
        on_date=date(2026, 7, 5),
        weight_kg=88.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(date(2026, 7, 5)),
    )
    row = await weight_service.upsert_body_measurement(
        db_session,
        on_date=date(2026, 7, 5),
        neck_cm=39.0,
        waist_cm=86.0,
        note="morning",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 7, 5)),
    )
    await db_session.commit()
    assert row.body_fat_pct is not None and row.lbm_kg is not None

    edited = await weight_service.update_body_measurement(
        db_session,
        row.id,
        on_date=date(2026, 7, 5),
        waist_cm=85.0,
        partial=False,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 7, 5)),
    )
    await db_session.commit()

    assert edited is not None
    assert edited.waist_cm == 85.0
    assert edited.neck_cm is None
    assert edited.note is None
    # Navy needs both circumferences, so the derived pair goes with the neck.
    assert edited.body_fat_pct is None
    assert edited.lbm_kg is None


async def test_update_measurement_non_partial_clears_across_a_date_change(db_session, owner_write):
    """Same rule when the edit also moves the row: nothing is carried over."""
    row = await weight_service.upsert_body_measurement(
        db_session,
        on_date=date(2026, 7, 6),
        neck_cm=39.0,
        waist_cm=86.0,
        note="morning",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 7, 6)),
    )
    await db_session.commit()

    moved = await weight_service.update_body_measurement(
        db_session,
        row.id,
        on_date=date(2026, 7, 7),
        waist_cm=85.0,
        partial=False,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 7, 7)),
    )
    await db_session.commit()

    assert moved is not None
    assert moved.date == date(2026, 7, 7)
    assert moved.waist_cm == 85.0
    assert moved.neck_cm is None
    assert moved.note is None
    rows = await weight_service.list_body_measurements(
        db_session,
        subject_id=owner_write.subject_id,
    )
    assert len(rows) == 1
