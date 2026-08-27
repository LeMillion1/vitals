"""Chart data service tests — generic aggregation for simple metrics, dispatch
for parametrized metrics (labs/hevy/body_comp), and catalog gating."""
from __future__ import annotations

from datetime import date

import pytest

from vitals.models.garmin import GarminDaily
from vitals.models.glp1 import Injection, SideEffect
from vitals.models.nutrition import MealLog
from vitals.models.weight import WeightLog
from vitals.services import chart_data_service, hevy_service, labs_service
from vitals.services.body_scan import scans
from vitals.services.modules_service import MODULE_REGISTRY


DAY1 = date(2026, 6, 1)
DAY2 = date(2026, 6, 2)


# ── simple (generic aggregation) metrics ───────────────────────────────────────
async def test_series_for_weight_filters_superseded(db_session, owned_by_legacy_subject, *, legacy_owner_roots):
    db_session.add_all([
        WeightLog(subject_id=legacy_owner_roots.subject_id, date=DAY1, domain="weight", source="garmin_api", weight_kg=90.0, superseded=True),
        WeightLog(subject_id=legacy_owner_roots.subject_id, date=DAY1, domain="weight", source="manual", weight_kg=88.5, superseded=False),
        WeightLog(subject_id=legacy_owner_roots.subject_id, date=DAY2, domain="weight", source="manual", weight_kg=88.0, superseded=False),
    ])
    await db_session.commit()

    points = await chart_data_service.series_for(db_session, subject_id=owned_by_legacy_subject.subject_id, metric_key="weight.weight_kg")
    assert points == [
        {"date": DAY1.isoformat(), "value": 88.5},
        {"date": DAY2.isoformat(), "value": 88.0},
    ]


async def test_series_for_nutrition_calories_sums_multiple_meals_per_day(db_session, owned_by_legacy_subject, *, legacy_owner_roots):
    db_session.add_all([
        MealLog(subject_id=legacy_owner_roots.subject_id, date=DAY1, domain="nutrition", source="manual", name="Breakfast", calories=400),
        MealLog(subject_id=legacy_owner_roots.subject_id, date=DAY1, domain="nutrition", source="manual", name="Lunch", calories=600),
        MealLog(subject_id=legacy_owner_roots.subject_id, date=DAY2, domain="nutrition", source="manual", name="Dinner", calories=500),
    ])
    await db_session.commit()

    points = await chart_data_service.series_for(db_session, subject_id=owned_by_legacy_subject.subject_id, metric_key="nutrition.calories")
    assert points == [
        {"date": DAY1.isoformat(), "value": 1000.0},
        {"date": DAY2.isoformat(), "value": 500.0},
    ]


async def test_series_for_side_effect_severity_takes_max_per_day(db_session, owned_by_legacy_subject, *, legacy_owner_roots):
    db_session.add_all([
        SideEffect(subject_id=legacy_owner_roots.subject_id, date=DAY1, domain="glp1", source="manual", effect_type="nausea", severity=2),
        SideEffect(subject_id=legacy_owner_roots.subject_id, date=DAY1, domain="glp1", source="manual", effect_type="fatigue", severity=4),
    ])
    await db_session.commit()

    points = await chart_data_service.series_for(db_session, subject_id=owned_by_legacy_subject.subject_id, metric_key="glp1.side_effect_severity")
    assert points == [{"date": DAY1.isoformat(), "value": 4.0}]


async def test_series_for_glp1_dose_sums_per_day(db_session, owned_by_legacy_subject, *, legacy_owner_roots):
    db_session.add_all([
        Injection(subject_id=legacy_owner_roots.subject_id, date=DAY1, domain="glp1", source="manual", drug="semaglutide", dose_mg=0.25),
        Injection(subject_id=legacy_owner_roots.subject_id, date=DAY1, domain="glp1", source="manual", drug="semaglutide", dose_mg=0.25),
    ])
    await db_session.commit()

    points = await chart_data_service.series_for(db_session, subject_id=owned_by_legacy_subject.subject_id, metric_key="glp1.dose_mg")
    assert points == [{"date": DAY1.isoformat(), "value": 0.5}]


async def test_series_for_garmin_sleep_hours_transform(db_session, owned_by_legacy_subject, *, garmin_connection_id, legacy_owner_roots):
    db_session.add(GarminDaily(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id, date=DAY1, domain="garmin", source="garmin_api", sleep_seconds=7 * 3600))
    await db_session.commit()

    points = await chart_data_service.series_for(db_session, subject_id=owned_by_legacy_subject.subject_id, metric_key="garmin.sleep_hours")
    assert points == [{"date": DAY1.isoformat(), "value": 7.0}]


async def test_series_for_none_metric_requires_no_param(db_session, owned_by_legacy_subject):
    # Passing a param to a simple metric is simply ignored (dispatch short-circuits).
    points = await chart_data_service.series_for(db_session, subject_id=owned_by_legacy_subject.subject_id, metric_key="weight.weight_kg", param="ignored")
    assert points == []


async def test_series_for_unknown_metric_raises_keyerror(db_session, owned_by_legacy_subject):
    with pytest.raises(KeyError):
        await chart_data_service.series_for(db_session, subject_id=owned_by_legacy_subject.subject_id, metric_key="does.not_exist")


# ── parametrized metrics ────────────────────────────────────────────────────────
async def test_series_for_labs_marker_requires_param(db_session, owned_by_legacy_subject):
    with pytest.raises(ValueError):
        await chart_data_service.series_for(db_session, subject_id=owned_by_legacy_subject.subject_id, metric_key="labs.marker")


async def test_series_for_labs_marker_matches_marker_history(db_session, owned_by_legacy_subject, owner_write):
    await labs_service.add_result(
        db_session,
        on_date=DAY1,
        marker="TSH",
        value=2.1,
        unit="mIU/L",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY1),
    )
    await labs_service.add_result(
        db_session,
        on_date=DAY2,
        marker="TSH",
        value=2.4,
        unit="mIU/L",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY2),
    )
    await db_session.commit()

    points = await chart_data_service.series_for(db_session, subject_id=owned_by_legacy_subject.subject_id, metric_key="labs.marker", param="TSH")
    expected = await labs_service.marker_history(
        db_session, "TSH", subject_id=owned_by_legacy_subject.subject_id
    )
    assert points == [{"date": r["date"], "value": r["value"]} for r in expected]


async def test_series_for_hevy_exercise_matches_working_weight_series(db_session, owned_by_legacy_subject, *, hevy_owned_scope):
    class FakeHevyClient:
        is_configured = True

        async def fetch_workouts(self, *, max_pages=50):
            return [{
                "id": "w1",
                "title": "Day A — Push",
                "start_time": "2026-06-10T10:00:00Z",
                "end_time": "2026-06-10T11:00:00Z",
                "updated_at": "2026-06-10T11:00:00Z",
                "exercises": [{
                    "index": 0,
                    "title": "Bench Press",
                    "exercise_template_id": "BENCH",
                    "sets": [{"index": 0, "type": "normal", "weight_kg": 80.0, "reps": 8}],
                }],
            }]

    await hevy_service.sync_owned(db_session, FakeHevyClient(), identity=hevy_owned_scope.identity, integration_connection_id=hevy_owned_scope.connection_id)
    await db_session.commit()

    points = await chart_data_service.series_for(db_session, subject_id=owned_by_legacy_subject.subject_id, metric_key="hevy.working_weight", param="BENCH")
    expected = await hevy_service.working_weight_series(
        db_session, "BENCH", subject_id=owned_by_legacy_subject.subject_id
    )
    assert points == [{"date": r["date"], "value": r["weight_kg"]} for r in expected]


async def test_series_for_body_scan_metric_whole_body(
    db_session, owned_by_legacy_subject, owner_write
):
    await scans.save_scan(
        db_session, on_date=DAY1, device="InBody",
        metrics=[{"label": "Процент жира", "value": 18.5, "unit": "%"}],
        source="manual",
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(DAY1),
    )
    await db_session.commit()

    points = await chart_data_service.series_for(db_session, subject_id=owned_by_legacy_subject.subject_id, metric_key="body_comp.metric", param="body_fat_pct")
    assert points == [{"date": DAY1.isoformat(), "value": 18.5}]


async def test_series_for_body_scan_metric_segmental(
    db_session, owned_by_legacy_subject, owner_write
):
    await scans.save_scan(
        db_session, on_date=DAY1, device="InBody",
        metrics=[{"label": "Мышцы", "value": 3.2, "unit": "кг", "segment": "trunk"}],
        source="manual",
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(DAY1),
    )
    await db_session.commit()

    points = await chart_data_service.series_for(
        db_session,
        subject_id=owned_by_legacy_subject.subject_id, metric_key="body_comp.metric", param="segmental_lean:trunk"
    )
    assert points == [{"date": DAY1.isoformat(), "value": 3.2}]


# ── catalog ──────────────────────────────────────────────────────────────────
async def test_build_catalog_omits_disabled_optional_domain(db_session, owned_by_legacy_subject):
    enabled = {k: (MODULE_REGISTRY[k].category == "core") for k in MODULE_REGISTRY}
    catalog = await chart_data_service.build_catalog(db_session, enabled, subject_id=owned_by_legacy_subject.subject_id)
    assert "weight" in catalog
    assert "garmin" in catalog
    assert "glp1" not in catalog
    assert "nutrition" not in catalog


async def test_build_catalog_includes_enabled_optional_domain_with_params(db_session, owned_by_legacy_subject, owner_write):
    await labs_service.add_result(
        db_session,
        on_date=DAY1,
        marker="TSH",
        value=2.1,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY1),
    )
    await db_session.commit()

    enabled = {k: True for k in MODULE_REGISTRY}
    catalog = await chart_data_service.build_catalog(db_session, enabled, subject_id=owned_by_legacy_subject.subject_id)
    assert "glp1" in catalog
    labs_metric = next(m for m in catalog["labs"]["metrics"] if m["key"] == "labs.marker")
    assert {p["value"] for p in labs_metric["params"]} == {"TSH"}


async def test_build_catalog_lang_selects_label(db_session, owned_by_legacy_subject):
    catalog_ru = await chart_data_service.build_catalog(db_session, {}, subject_id=owned_by_legacy_subject.subject_id, lang="ru")
    catalog_en = await chart_data_service.build_catalog(db_session, {}, subject_id=owned_by_legacy_subject.subject_id, lang="en")
    weight_ru = next(m for m in catalog_ru["weight"]["metrics"] if m["key"] == "weight.weight_kg")
    weight_en = next(m for m in catalog_en["weight"]["metrics"] if m["key"] == "weight.weight_kg")
    assert weight_ru["label"] == "Вес"
    assert weight_en["label"] == "Weight"


# ── resolve_chart_series ────────────────────────────────────────────────────────
async def test_resolve_chart_series_auto_labels_and_units(db_session, owned_by_legacy_subject, *, garmin_connection_id, legacy_owner_roots):
    db_session.add(WeightLog(subject_id=legacy_owner_roots.subject_id, date=DAY1, domain="weight", source="manual", weight_kg=88.0, superseded=False))
    db_session.add(GarminDaily(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id, date=DAY1, domain="garmin", source="garmin_api", avg_stress=35))
    await db_session.commit()

    config = {
        "series": [
            {"domain": "weight", "metric_key": "weight.weight_kg", "param": None, "color_slot": 0},
            {"domain": "garmin", "metric_key": "garmin.avg_stress", "param": None, "color_slot": 1},
        ]
    }
    resolved = await chart_data_service.resolve_chart_series(
        db_session, config, subject_id=owned_by_legacy_subject.subject_id
    )
    assert resolved[0]["label"] == "Вес"
    assert resolved[0]["unit"] == "кг"
    assert resolved[0]["points"] == [{"date": DAY1.isoformat(), "value": 88.0}]
    assert resolved[1]["label"] == "Уровень стресса"
    assert resolved[1]["unit"] is None


async def test_resolve_chart_series_skips_unknown_metric_key(db_session, owned_by_legacy_subject):
    config = {"series": [{"domain": "weight", "metric_key": "no.such.metric", "param": None, "color_slot": 0}]}
    resolved = await chart_data_service.resolve_chart_series(
        db_session, config, subject_id=owned_by_legacy_subject.subject_id
    )
    assert resolved == []


async def test_resolve_chart_series_labs_marker_unit_from_catalog(db_session, owned_by_legacy_subject, owner_write):
    await labs_service.add_result(
        db_session,
        on_date=DAY1,
        marker="TSH",
        value=2.1,
        unit="mIU/L",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY1),
    )
    await db_session.commit()

    config = {"series": [{"domain": "labs", "metric_key": "labs.marker", "param": "TSH", "color_slot": 0}]}
    resolved = await chart_data_service.resolve_chart_series(
        db_session, config, subject_id=owned_by_legacy_subject.subject_id
    )
    assert resolved[0]["unit"] == "mIU/L"
    assert resolved[0]["label"] == "TSH"
