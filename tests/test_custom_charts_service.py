"""Unit tests for the custom-chart-builder storage service — defaults,
persistence, validation, caps, and Redis caching. Mirrors
tests/test_modules_service.py in structure."""
from __future__ import annotations

import json
import logging

import pytest

from vitals.models.scoped_settings import SubjectSetting
from vitals.services import custom_charts_service as svc
from vitals.services.custom_charts_service import ChartConfigError


WEIGHT_SERIES = [{"domain": "weight", "metric_key": "weight.weight_kg"}]
STRESS_SERIES = [{"domain": "garmin", "metric_key": "garmin.avg_stress"}]


async def test_defaults_on_empty_db(db_session, legacy_owner_roots):
    assert await svc.list_charts(db_session, subject_id=legacy_owner_roots.subject_id) == []


async def test_create_and_persist(db_session, legacy_owner_roots):
    created = await svc.create_chart(db_session, name="Вес и стресс", series=WEIGHT_SERIES + STRESS_SERIES, subject_id=legacy_owner_roots.subject_id)
    await db_session.commit()

    assert created["name"] == "Вес и стресс"
    assert len(created["series"]) == 2
    assert created["series"][0]["color_slot"] == 0
    assert created["series"][1]["color_slot"] == 1
    assert created["normalize"] is False

    fresh = await svc.list_charts(db_session, redis=None, subject_id=legacy_owner_roots.subject_id)
    assert len(fresh) == 1
    assert fresh[0]["id"] == created["id"]


async def test_create_multiple_charts_appends(db_session, legacy_owner_roots):
    await svc.create_chart(db_session, name="Chart A", series=WEIGHT_SERIES, subject_id=legacy_owner_roots.subject_id)
    await svc.create_chart(db_session, name="Chart B", series=STRESS_SERIES, subject_id=legacy_owner_roots.subject_id)
    await db_session.commit()

    charts = await svc.list_charts(db_session, redis=None, subject_id=legacy_owner_roots.subject_id)
    assert [c["name"] for c in charts] == ["Chart A", "Chart B"]


async def test_create_requires_name(db_session, legacy_owner_roots):
    with pytest.raises(ChartConfigError):
        await svc.create_chart(db_session, name="  ", series=WEIGHT_SERIES, subject_id=legacy_owner_roots.subject_id)


async def test_create_requires_at_least_one_series(db_session, legacy_owner_roots):
    with pytest.raises(ChartConfigError):
        await svc.create_chart(db_session, name="Empty", series=[], subject_id=legacy_owner_roots.subject_id)


async def test_create_rejects_too_many_series(db_session, legacy_owner_roots):
    series = [{"domain": "weight", "metric_key": "weight.weight_kg"} for _ in range(svc.MAX_SERIES_PER_CHART + 1)]
    with pytest.raises(ChartConfigError):
        await svc.create_chart(db_session, name="Too many", series=series, subject_id=legacy_owner_roots.subject_id)


async def test_create_rejects_unknown_metric_key(db_session, legacy_owner_roots):
    with pytest.raises(ChartConfigError):
        await svc.create_chart(
            db_session, name="Bad", series=[{"domain": "weight", "metric_key": "no.such.metric"}],
            subject_id=legacy_owner_roots.subject_id,
        )


async def test_create_requires_param_when_metric_needs_one(db_session, legacy_owner_roots):
    with pytest.raises(ChartConfigError):
        await svc.create_chart(
            db_session, name="Missing param", series=[{"domain": "labs", "metric_key": "labs.marker"}],
            subject_id=legacy_owner_roots.subject_id,
        )


async def test_create_rejects_param_when_metric_takes_none(db_session, legacy_owner_roots):
    with pytest.raises(ChartConfigError):
        await svc.create_chart(
            db_session,
            name="Unexpected param",
            series=[{"domain": "weight", "metric_key": "weight.weight_kg", "param": "x"}],
            subject_id=legacy_owner_roots.subject_id,
        )


async def test_create_accepts_valid_param(db_session, legacy_owner_roots):
    created = await svc.create_chart(
        db_session,
        name="TSH",
        series=[{"domain": "labs", "metric_key": "labs.marker", "param": "TSH"}],
        subject_id=legacy_owner_roots.subject_id,
    )
    await db_session.commit()
    assert created["series"][0]["param"] == "TSH"


async def test_create_enforces_max_charts(db_session, legacy_owner_roots, monkeypatch):
    monkeypatch.setattr(svc, "MAX_CHARTS", 1)
    await svc.create_chart(db_session, name="First", series=WEIGHT_SERIES, subject_id=legacy_owner_roots.subject_id)
    await db_session.commit()
    with pytest.raises(ChartConfigError):
        await svc.create_chart(db_session, name="Second", series=WEIGHT_SERIES, subject_id=legacy_owner_roots.subject_id)


async def test_delete_removes_only_target(db_session, legacy_owner_roots):
    a = await svc.create_chart(db_session, name="A", series=WEIGHT_SERIES, subject_id=legacy_owner_roots.subject_id)
    b = await svc.create_chart(db_session, name="B", series=STRESS_SERIES, subject_id=legacy_owner_roots.subject_id)
    await db_session.commit()

    removed = await svc.delete_chart(db_session, a["id"], subject_id=legacy_owner_roots.subject_id)
    await db_session.commit()
    assert removed is True

    remaining = await svc.list_charts(db_session, redis=None, subject_id=legacy_owner_roots.subject_id)
    assert [c["id"] for c in remaining] == [b["id"]]


async def test_delete_unknown_id_returns_false(db_session, legacy_owner_roots):
    await svc.create_chart(db_session, name="A", series=WEIGHT_SERIES, subject_id=legacy_owner_roots.subject_id)
    await db_session.commit()
    assert await svc.delete_chart(db_session, "does-not-exist", subject_id=legacy_owner_roots.subject_id) is False


async def test_get_chart_by_id(db_session, legacy_owner_roots):
    created = await svc.create_chart(db_session, name="A", series=WEIGHT_SERIES, subject_id=legacy_owner_roots.subject_id)
    await db_session.commit()
    fetched = await svc.get_chart(db_session, created["id"], subject_id=legacy_owner_roots.subject_id)
    assert fetched == created


async def test_malformed_value_falls_back(db_session, legacy_owner_roots, caplog):
    db_session.add(
        SubjectSetting(
            subject_id=legacy_owner_roots.subject_id,
            key=svc.SETTINGS_KEY,
            value={"not": "a list"},
        )
    )
    await db_session.commit()

    with caplog.at_level(logging.WARNING):
        charts = await svc.list_charts(db_session, redis=None, subject_id=legacy_owner_roots.subject_id)

    assert charts == []
    assert any("not an array" in r.getMessage() for r in caplog.records)


async def test_malformed_entries_dropped(db_session, legacy_owner_roots):
    db_session.add(
        SubjectSetting(
            subject_id=legacy_owner_roots.subject_id,
            key=svc.SETTINGS_KEY,
            value=[
                {
                    "id": "ok1",
                    "name": "Good",
                    "series": [{"metric_key": "weight.weight_kg"}],
                },
                {"id": "bad1"},  # missing name/series
                "not-even-a-dict",
                {  # empty series → dropped
                    "id": "ok2",
                    "name": "Also good",
                    "series": [],
                },
            ],
        )
    )
    await db_session.commit()

    charts = await svc.list_charts(db_session, redis=None, subject_id=legacy_owner_roots.subject_id)
    assert [c["id"] for c in charts] == ["ok1"]


async def test_redis_cache_is_read_through(db_session, legacy_owner_roots, redis):
    await svc.prime_cache(redis, [{"id": "x1", "name": "Cached", "normalize": False,
                                    "series": [{"metric_key": "weight.weight_kg", "color_slot": 0}]}], subject_id=legacy_owner_roots.subject_id)
    assert json.loads(await redis.get(svc.cache_key(legacy_owner_roots.subject_id)))[0]["id"] == "x1"

    charts = await svc.list_charts(db_session, redis, subject_id=legacy_owner_roots.subject_id)
    assert charts[0]["id"] == "x1"  # served from cache; DB has no row


async def test_create_primes_cache(db_session, legacy_owner_roots, redis):
    created = await svc.create_chart(db_session, name="A", series=WEIGHT_SERIES, redis=redis, subject_id=legacy_owner_roots.subject_id)
    await db_session.commit()

    cached = json.loads(await redis.get(svc.cache_key(legacy_owner_roots.subject_id)))
    assert cached[0]["id"] == created["id"]
