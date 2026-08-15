"""Outbound Garmin weight sync: opt-in, idempotency and ownership safety."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from vitals.config import Config
from vitals.enums import Source
from vitals.integrations.garmin_client import GarminClient
from vitals.models.garmin import (
    WEIGHT_EXPORT_FAILED,
    WEIGHT_EXPORT_PENDING,
    WEIGHT_EXPORT_SENT,
    GarminWeightExport,
)
from vitals.models.weight import WeightLog
from vitals.services import alerts_service, garmin_weight_service, weight_service

DAY = date(2026, 8, 15)
NOW = datetime(2026, 8, 15, 9, 30)


class FakeWeightClient:
    def __init__(self, rows=None, *, fail_fetch: Exception | None = None):
        self.rows: dict[date, list[dict]] = {DAY: list(rows or [])}
        self.fail_fetch = fail_fetch
        self.fetch_calls: list[date] = []
        self.add_calls: list[tuple[float, datetime]] = []
        self.delete_calls: list[tuple[str, date]] = []
        self._next_id = 100
        self.raise_after_add: Exception | None = None

    async def fetch_daily_weigh_ins(self, on_date: date) -> dict:
        self.fetch_calls.append(on_date)
        if self.fail_fetch is not None:
            raise self.fail_fetch
        return {"dateWeightList": [dict(row) for row in self.rows.get(on_date, [])]}

    async def add_weigh_in(self, weight_kg: float, measured_at: datetime):
        self.add_calls.append((weight_kg, measured_at))
        sample_pk = str(self._next_id)
        self._next_id += 1
        self.rows.setdefault(measured_at.date(), []).append(
            {"samplePk": sample_pk, "weight": weight_kg * 1000}
        )
        if self.raise_after_add is not None:
            raise self.raise_after_add
        # Pinned garminconnect commonly returns None for this 204 endpoint.
        return None

    async def delete_weigh_in(self, sample_pk: str, on_date: date):
        self.delete_calls.append((sample_pk, on_date))
        self.rows[on_date] = [
            row
            for row in self.rows.get(on_date, [])
            if str(row.get("samplePk")) != str(sample_pk)
        ]


async def _manual_weight(db_session, value: float, *, on_date: date = DAY):
    return await weight_service.log_weight(
        db_session,
        on_date=on_date,
        weight_kg=value,
        source=Source.MANUAL.value,
    )


async def _outbox(db_session) -> GarminWeightExport:
    return (await db_session.execute(select(GarminWeightExport))).scalar_one()


async def test_export_is_opt_in_and_enabling_only_queues_locally(db_session):
    await _manual_weight(db_session, 84.5)

    assert await garmin_weight_service.is_enabled(db_session) is False
    assert (await db_session.execute(select(GarminWeightExport))).scalar_one_or_none() is None

    await garmin_weight_service.set_enabled(db_session, True)
    row = await _outbox(db_session)
    assert await garmin_weight_service.is_enabled(db_session) is True
    assert row.status == WEIGHT_EXPORT_PENDING
    assert row.weight_kg == 84.5
    assert row.weight_log_id is not None


async def test_latest_weight_posts_once_then_stays_idempotent(db_session):
    await _manual_weight(db_session, 85.0, on_date=DAY - timedelta(days=1))
    latest = await _manual_weight(db_session, 84.5)
    client = FakeWeightClient()

    first = await garmin_weight_service.export_latest(db_session, client, now=NOW)
    second = await garmin_weight_service.export_latest(db_session, client, now=NOW)

    assert first == {"status": WEIGHT_EXPORT_SENT, "sent": True, "date": DAY}
    assert second == {"status": WEIGHT_EXPORT_SENT, "sent": False, "date": DAY}
    assert len(client.add_calls) == 1
    assert client.add_calls[0] == (84.5, NOW)
    row = await _outbox(db_session)
    assert row.weight_log_id == latest.id
    assert row.remote_owned is True
    assert row.remote_sample_pk == "100"
    assert (
        await db_session.execute(select(func.count()).select_from(GarminWeightExport))
    ).scalar_one() == 1


async def test_equal_preexisting_garmin_weight_skips_post_and_is_not_owned(db_session):
    await _manual_weight(db_session, 84.5)
    client = FakeWeightClient([{"samplePk": 77, "weight": 84500}])

    result = await garmin_weight_service.export_latest(db_session, client, now=NOW)

    assert result["sent"] is False
    assert client.add_calls == []
    row = await _outbox(db_session)
    assert row.status == WEIGHT_EXPORT_SENT
    assert row.remote_sample_pk == "77"
    assert row.remote_owned is False


async def test_same_day_correction_replaces_only_vitals_owned_record(db_session):
    await _manual_weight(db_session, 85.0)
    client = FakeWeightClient()
    await garmin_weight_service.export_latest(db_session, client, now=NOW)

    await _manual_weight(db_session, 84.0)
    result = await garmin_weight_service.export_latest(
        db_session, client, now=NOW + timedelta(minutes=1)
    )

    assert result["sent"] is True
    assert client.delete_calls == [("100", DAY)]
    assert [row["weight"] for row in client.rows[DAY]] == [84000.0]
    row = await _outbox(db_session)
    assert row.remote_owned is True
    assert row.remote_sample_pk == "101"
    assert row.weight_kg == 84.0


async def test_correction_never_deletes_matching_record_vitals_did_not_create(db_session):
    await _manual_weight(db_session, 85.0)
    client = FakeWeightClient([{"samplePk": "external", "weight": 85000}])
    await garmin_weight_service.export_latest(db_session, client, now=NOW)

    await _manual_weight(db_session, 84.0)
    await garmin_weight_service.export_latest(
        db_session, client, now=NOW + timedelta(minutes=1)
    )

    assert client.delete_calls == []
    assert sorted(row["weight"] for row in client.rows[DAY]) == [84000.0, 85000]


async def test_accepted_post_followed_by_timeout_is_reconciled_without_duplicate(db_session):
    await _manual_weight(db_session, 84.5)
    client = FakeWeightClient()
    client.raise_after_add = TimeoutError("response lost")

    failed = await garmin_weight_service.export_latest(db_session, client, now=NOW)
    assert failed["status"] == WEIGHT_EXPORT_FAILED
    assert len(client.add_calls) == 1

    client.raise_after_add = None
    recovered = await garmin_weight_service.export_latest(
        db_session, client, now=NOW + timedelta(minutes=15)
    )
    assert recovered["status"] == WEIGHT_EXPORT_SENT
    assert recovered["sent"] is False
    assert len(client.add_calls) == 1
    assert len(client.rows[DAY]) == 1


async def test_failure_sets_backoff_and_warn_alert(db_session):
    await _manual_weight(db_session, 84.5)
    client = FakeWeightClient(fail_fetch=RuntimeError("Garmin unavailable"))

    result = await garmin_weight_service.export_latest(db_session, client, now=NOW)
    row = await _outbox(db_session)

    assert result["status"] == WEIGHT_EXPORT_FAILED
    assert row.attempts == 1
    assert row.next_attempt_at == NOW + timedelta(minutes=15)
    assert "Garmin unavailable" in row.last_error
    alerts = await alerts_service.list_active(db_session, domain="garmin")
    assert [a.alert_key for a in alerts] == [garmin_weight_service.ALERT_KEY]

    # A scheduler tick inside the backoff window makes no upstream request.
    client.fail_fetch = None
    await garmin_weight_service.export_latest(
        db_session, client, now=NOW + timedelta(minutes=14)
    )
    assert len(client.fetch_calls) == 1


async def test_latest_garmin_import_is_not_echoed_and_older_manual_is_not_backfilled(
    db_session,
):
    await _manual_weight(db_session, 85.0, on_date=DAY - timedelta(days=1))
    await weight_service.log_weight(
        db_session,
        on_date=DAY,
        weight_kg=84.5,
        source=Source.GARMIN_API.value,
    )
    client = FakeWeightClient()

    result = await garmin_weight_service.export_latest(db_session, client, now=NOW)

    assert result == {"status": "empty", "sent": False}
    assert client.fetch_calls == []
    assert (await db_session.execute(select(GarminWeightExport))).scalar_one_or_none() is None


async def test_stale_weight_is_not_exported(db_session):
    await _manual_weight(
        db_session,
        85.0,
        on_date=DAY - timedelta(days=garmin_weight_service.MAX_AGE_DAYS + 1),
    )
    client = FakeWeightClient()

    assert await garmin_weight_service.export_latest(db_session, client, now=NOW) == {
        "status": "empty",
        "sent": False,
    }
    assert client.fetch_calls == []


async def test_disabled_job_never_constructs_a_garmin_client(
    db_session, session_factory, monkeypatch
):
    def explode(*_args, **_kwargs):
        raise AssertionError("disabled export must not create a Garmin client")

    monkeypatch.setattr(GarminClient, "from_config", explode)
    await garmin_weight_service.export_job(session_factory, redis=None)


async def test_repeated_garmin_import_under_manual_weight_is_deduplicated(db_session):
    await _manual_weight(db_session, 84.0)
    for _ in range(2):
        await weight_service.log_weight(
            db_session,
            on_date=DAY,
            weight_kg=85.0,
            source=Source.GARMIN_API.value,
        )

    rows = (
        await db_session.execute(select(WeightLog).where(WeightLog.date == DAY))
    ).scalars().all()
    assert len(rows) == 2
    assert len([row for row in rows if row.source == Source.GARMIN_API.value]) == 1


async def test_inbound_dedupe_does_not_swallow_a_manual_reentry(db_session):
    await _manual_weight(db_session, 85.0)
    await _manual_weight(db_session, 84.0)
    newest = await _manual_weight(db_session, 85.0)

    active = await weight_service.get_active_weight(db_session, DAY)
    assert active is newest
    assert active.weight_kg == 85.0
    assert (
        await db_session.execute(
            select(func.count()).select_from(WeightLog).where(WeightLog.date == DAY)
        )
    ).scalar_one() == 3


def test_daily_weigh_in_parser_uses_individual_grams_not_average():
    rows = garmin_weight_service.parse_daily_weigh_ins(
        {
            "dateWeightList": [
                {"samplePk": 1, "weight": 84500},
                {"samplePk": 2, "weight": 84.0},
            ],
            "totalAverage": {"weight": 99999},
        }
    )
    assert [(row.sample_pk, row.weight_kg) for row in rows] == [
        ("1", 84.5),
        ("2", 84.0),
    ]


class _RawWeightGarmin:
    def __init__(self):
        self.calls = []

    def get_daily_weigh_ins(self, day):
        self.calls.append(("get", day))
        return {"dateWeightList": []}

    def add_weigh_in(self, weight, *, unitKey, timestamp):
        self.calls.append(("add", weight, unitKey, timestamp))
        return {"samplePk": 9}

    def delete_weigh_in(self, sample_pk, day):
        self.calls.append(("delete", sample_pk, day))


async def test_garmin_client_weight_methods_preserve_local_date_and_contract():
    raw = _RawWeightGarmin()
    client = GarminClient(
        Config(database_url="", redis_url="", timezone="Asia/Almaty")
    )
    client._garmin = raw

    assert await client.fetch_daily_weigh_ins(DAY) == {"dateWeightList": []}
    assert await client.add_weigh_in(84.5, datetime(2026, 8, 15, 23, 45)) == {
        "samplePk": 9
    }
    await client.delete_weigh_in("9", DAY)

    assert raw.calls == [
        ("get", "2026-08-15"),
        ("add", 84.5, "kg", "2026-08-15T23:45:00+05:00"),
        ("delete", "9", "2026-08-15"),
    ]
