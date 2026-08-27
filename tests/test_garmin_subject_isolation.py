"""Regression coverage for subject-scoped Garmin read surfaces."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.garmin import GarminActivity, GarminDaily, GarminIntraday
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import IntegrationConnection
from vitals.services.garmin import queries as garmin_queries


DAY = date(2026, 8, 24)


async def _other_garmin_scope(session, slug: str):
    owner = User(
        username=slug,
        normalized_username=slug.casefold(),
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(owner_user_id=owner.id, timezone="UTC")
    session.add(subject)
    await session.flush()
    connection = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator=f"synthetic:{slug}",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    session.add(connection)
    await session.flush()
    return subject, connection


def _daily(subject_id, connection_id, on_date, *, score):
    return GarminDaily(
        subject_id=subject_id,
        integration_connection_id=connection_id,
        date=on_date,
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        sleep_seconds=7 * 3600,
        sleep_score=score,
    )


async def test_all_garmin_query_apis_isolate_subject_and_connection_ownership(
    db_session,
    *,
    legacy_owner_roots,
    garmin_connection_id,
):
    mine = legacy_owner_roots.subject_id
    other, other_connection = await _other_garmin_scope(
        db_session,
        "garmin-reader-other",
    )

    mine_days = [
        _daily(mine, garmin_connection_id, DAY - timedelta(days=2), score=68),
        _daily(mine, garmin_connection_id, DAY, score=72),
        _daily(mine, garmin_connection_id, DAY + timedelta(days=2), score=76),
    ]
    other_days = [
        _daily(other.id, other_connection.id, DAY - timedelta(days=1), score=91),
        _daily(other.id, other_connection.id, DAY, score=92),
        _daily(other.id, other_connection.id, DAY + timedelta(days=1), score=93),
    ]
    # The schema currently has independent subject/connection foreign keys. A
    # malformed cross-owned pair must not pass a read boundary even though its
    # row-level subject happens to equal the requested subject.
    mismatched = _daily(
        mine,
        other_connection.id,
        DAY + timedelta(days=3),
        score=100,
    )
    mine_activity = GarminActivity(
        subject_id=mine,
        integration_connection_id=garmin_connection_id,
        external_id="mine-activity",
        date=DAY,
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        name="mine",
        start_time=datetime(2026, 8, 24, 8),
    )
    other_activity = GarminActivity(
        subject_id=other.id,
        integration_connection_id=other_connection.id,
        external_id="other-activity",
        date=DAY,
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        name="other",
        start_time=datetime(2026, 8, 24, 9),
    )
    mine_sample = GarminIntraday(
        subject_id=mine,
        integration_connection_id=garmin_connection_id,
        date=DAY,
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        series_type="stress",
        ts=datetime(2026, 8, 24, 8),
        value=21,
    )
    other_sample = GarminIntraday(
        subject_id=other.id,
        integration_connection_id=other_connection.id,
        date=DAY,
        domain=Domain.GARMIN.value,
        source=Source.GARMIN_API.value,
        series_type="stress",
        ts=datetime(2026, 8, 24, 9),
        value=99,
    )
    db_session.add_all(
        mine_days
        + other_days
        + [
            mismatched,
            mine_activity,
            other_activity,
            mine_sample,
            other_sample,
        ]
    )
    await db_session.flush()

    assert (
        await garmin_queries.get_daily(db_session, DAY, subject_id=mine)
    ).sleep_score == 72
    assert (
        await garmin_queries.get_daily(
            db_session,
            DAY + timedelta(days=3),
            subject_id=mine,
        )
        is None
    )
    assert {
        row.id
        for row in await garmin_queries.list_daily(db_session, subject_id=mine)
    } == {row.id for row in mine_days}
    assert [
        row.date
        for row in await garmin_queries.list_daily(
            db_session,
            subject_id=mine,
            start=DAY - timedelta(days=1),
            end=DAY + timedelta(days=1),
            limit=1,
        )
    ] == [DAY]
    assert {
        row.id
        for row in await garmin_queries.list_nights(db_session, subject_id=mine)
    } == {row.id for row in mine_days}
    assert await garmin_queries.daily_count(db_session, subject_id=mine) == 3
    assert [
        row.external_id
        for row in await garmin_queries.list_activities(
            db_session,
            subject_id=mine,
        )
    ] == ["mine-activity"]
    assert [
        row.external_id
        for row in await garmin_queries.list_activities(
            db_session,
            subject_id=mine,
            start=DAY,
            end=DAY,
            limit=1,
        )
    ] == ["mine-activity"]
    assert [
        row.value
        for row in await garmin_queries.list_intraday(
            db_session,
            subject_id=mine,
            start=DAY,
            end=DAY,
        )
    ] == [21]
    assert await garmin_queries.intraday_series_map(
        db_session,
        DAY,
        subject_id=mine,
    ) == {"stress": [{"ts": "2026-08-24T08:00:00", "value": 21}]}
    assert await garmin_queries.adjacent_night_dates(
        db_session,
        DAY,
        subject_id=mine,
    ) == (DAY - timedelta(days=2), DAY + timedelta(days=2))
    summary = await garmin_queries.recovery_summary(
        db_session,
        subject_id=mine,
        before_or_on=DAY + timedelta(days=3),
    )
    assert summary.total_days_logged == 3
    assert summary.latest is not None
    assert (summary.latest.date, summary.latest.sleep_score) == (
        DAY + timedelta(days=2),
        76,
    )


async def test_garmin_sleep_detail_does_not_open_another_subjects_day(
    auth_client,
    db_session,
):
    other, other_connection = await _other_garmin_scope(
        db_session,
        "garmin-web-other",
    )
    db_session.add(_daily(other.id, other_connection.id, DAY, score=99))
    await db_session.commit()

    response = await auth_client.get(
        f"/garmin/sleep/{DAY.isoformat()}",
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 404
