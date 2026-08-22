"""Context v2 regression contract for the period AI report.

The report is an external-model boundary. These tests use synthetic rows to
prove both halves of that boundary: normalized facts that matter must cross it,
while future facts, disabled modules, raw detail, and local file references must
not.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

import pytest

from vitals.enums import Domain, Source
from vitals.models.body_scan import BodyScan, BodyScanMetric
from vitals.models.garmin import GarminActivity, GarminDaily
from vitals.models.genetics import GeneticVariant
from vitals.models.glp1 import DosePhase, Injection, SideEffect
from vitals.models.hevy import HevyWorkout
from vitals.models.hrt import HrtCompound, HrtDose, HrtSideEffect
from vitals.models.labs import LabMarker, LabResult
from vitals.models.milestones import Milestone
from vitals.models.nutrition import MealLog
from vitals.models.signals import DayContext, Signal
from vitals.models.skincare import SkincareLog, SkincareObservation, SkincareProduct
from vitals.models.system_alert import SystemAlert
from vitals.models.timeline import Annotation
from vitals.models.weight import BodyMeasurement, WeightLog
from vitals.services import (
    alerts_service,
    digest_service,
    hrt_cycle_service,
    modules_service,
)

pytestmark = pytest.mark.usefixtures("all_modules_on", "owned_by_legacy_subject")

DAY = date(2026, 8, 4)


async def test_report_window_separates_closed_day_and_brief(monkeypatch):
    monkeypatch.setattr(digest_service, "today_local", lambda: DAY)

    closed = digest_service.report_window(period_days=1)
    brief = digest_service.report_window(
        period_days=1, mode=digest_service.REPORT_MODE_BRIEF
    )

    assert closed.period_end == DAY - timedelta(days=1)
    assert closed.mode == "closed_period"
    assert brief.period_end == DAY
    assert brief.mode == "daily_brief"

    for invalid in (0, 91):
        with pytest.raises(ValueError, match="between 1 and 90"):
            digest_service.report_window(period_days=invalid)
    with pytest.raises(ValueError, match="requires period_days=1"):
        digest_service.report_window(
            period_days=7, mode=digest_service.REPORT_MODE_BRIEF
        )
    with pytest.raises(ValueError, match="future"):
        digest_service.report_window(on_date=DAY + timedelta(days=1))


async def test_platform_scheduler_diagnostics_never_reach_report_context(
    db_session,
    legacy_owner_roots,
):
    sentinel = "secret-path:/srv/private/trace.sql"
    stamp = datetime.combine(DAY, time(8, 0))
    db_session.add_all(
        [
            SystemAlert(
                domain=Domain.SYSTEM.value,
                severity="warn",
                message=sentinel,
                alert_key="scheduler.job_failed:raw_payload_sweep",
                entity_ref="raw_payload_sweep",
                created_at=stamp,
            ),
            SystemAlert(
                subject_id=legacy_owner_roots.subject_id,
                domain=Domain.SYSTEM.value,
                severity="info",
                message="subject-visible",
                alert_key="brief_empty_day",
                entity_ref="",
                created_at=stamp,
            ),
            SystemAlert(
                subject_id=legacy_owner_roots.subject_id,
                domain=Domain.SYSTEM.value,
                severity="warn",
                message="subject-job-visible",
                alert_key="scheduler.job_failed:weekly_digest",
                entity_ref="weekly_digest",
                created_at=stamp,
            ),
            SystemAlert(
                subject_id=legacy_owner_roots.subject_id,
                domain=Domain.SYSTEM.value,
                severity="warn",
                message="provider-job-visible",
                alert_key="scheduler.job_failed:garmin_sync",
                entity_ref="garmin_sync",
                created_at=stamp,
            ),
        ]
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        on_date=DAY,
        period_days=1,
        mode=digest_service.REPORT_MODE_BRIEF,
    )

    messages = {row["message"] for row in ctx["alerts"]}
    assert "subject-visible" in messages
    assert "subject-job-visible" in messages
    assert "provider-job-visible" in messages
    assert sentinel not in messages


async def test_garmin_activities_and_same_day_hevy_sessions_survive(db_session, legacy_owner_roots, *, garmin_connection_id, hevy_connection_id):
    db_session.add(
        GarminDaily(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id,
            date=DAY,
            domain=Domain.GARMIN.value,
            source=Source.GARMIN_API.value,
            sleep_score=82,
            deep_sleep_seconds=5400,
            rem_sleep_seconds=7200,
            intensity_minutes_vigorous=35,
            vo2max=48.2,
            acute_load=410.0,
            avg_hr=71,
            respiration_lowest=12.5,
            sleep_need_actual=480,
            bmr_calories=1800,
        )
    )
    db_session.add_all(
        [
            GarminActivity(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id,
                external_id="run-1",
                date=DAY,
                domain=Domain.GARMIN.value,
                source=Source.GARMIN_API.value,
                activity_type="running",
                name="Intervals",
                start_time=datetime(2026, 8, 4, 7, 0),
                duration_seconds=2400,
                distance_m=7200,
                avg_hr=156,
                training_effect_aerobic=3.7,
                hr_zone_seconds=[{"zone": 4, "secs": 900}],
            ),
            GarminActivity(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id,
                external_id="walk-1",
                date=DAY,
                domain=Domain.GARMIN.value,
                source=Source.GARMIN_API.value,
                activity_type="walking",
                name="Evening walk",
                start_time=datetime(2026, 8, 4, 20, 0),
                duration_seconds=1800,
                distance_m=2500,
            ),
            GarminActivity(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id,
                external_id="future-run",
                date=DAY + timedelta(days=1),
                domain=Domain.GARMIN.value,
                source=Source.GARMIN_API.value,
                activity_type="running",
                name="Future",
            ),
            HevyWorkout(subject_id=legacy_owner_roots.subject_id, integration_connection_id=hevy_connection_id,
                external_id="hevy-am",
                date=DAY,
                domain=Domain.WORKOUTS.value,
                source=Source.HEVY_API.value,
                title="Push",
                start_time=datetime(2026, 8, 4, 9, 0),
                duration_seconds=3600,
            ),
            HevyWorkout(subject_id=legacy_owner_roots.subject_id, integration_connection_id=hevy_connection_id,
                external_id="hevy-pm",
                date=DAY,
                domain=Domain.WORKOUTS.value,
                source=Source.HEVY_API.value,
                title="Mobility",
                start_time=datetime(2026, 8, 4, 18, 0),
                duration_seconds=1200,
            ),
        ]
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id, on_date=DAY, period_days=7
    )
    report_day = ctx["days"][-1]

    assert [row["name"] for row in ctx["garmin"]["activities"]] == [
        "Intervals",
        "Evening walk",
    ]
    assert len(report_day["garmin_activities"]) == 2
    assert len(report_day["hevy_workouts"]) == 2
    assert report_day["workout"]["title"] == "Mobility"
    assert report_day["vo2max"] == 48.2
    assert report_day["deep_sleep_seconds"] == 5400
    assert ctx["training"]["current"]["garmin"]["activities"] == 2
    assert ctx["training"]["current"]["hevy"]["sessions"] == 2
    assert ctx["training"]["current"]["hevy"]["volume_samples"] == 0
    assert ctx["hevy"]["gap_samples"] == 1
    assert ctx["period_stats"]["current"]["garmin_activities"] == 2
    assert ctx["period_stats"]["current"]["sample_counts"]["vo2max"] == 1
    assert ctx["period_stats"]["current"]["sleep_need_hours"] == 8
    assert ctx["period_stats"]["current"]["avg_hr"] == 71
    assert ctx["coverage"]["garmin"]["activity_rows"] == 2
    assert ctx["coverage"]["garmin"]["freshness_days"] == 0


async def test_historical_context_excludes_future_rows_from_every_fixed_block(
    db_session,
    legacy_owner_roots,
):
    current_scan = BodyScan(subject_id=legacy_owner_roots.subject_id,
        date=DAY,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
        device="Synthetic",
    )
    current_scan.metrics.append(
        BodyScanMetric(
            metric_key="body_fat_pct",
            label="Body fat",
            value=18.0,
            unit="%",
            category="composition",
        )
    )
    future_scan = BodyScan(subject_id=legacy_owner_roots.subject_id,
        date=DAY + timedelta(days=6),
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
        device="Future",
    )
    future_scan.metrics.append(
        BodyScanMetric(
            metric_key="body_fat_pct",
            label="Body fat",
            value=9.0,
            unit="%",
            category="composition",
        )
    )
    db_session.add_all(
        [
            WeightLog(subject_id=legacy_owner_roots.subject_id,
                date=DAY,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                weight_kg=88.0,
            ),
            WeightLog(subject_id=legacy_owner_roots.subject_id,
                date=DAY + timedelta(days=6),
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                weight_kg=70.0,
            ),
            current_scan,
            future_scan,
            LabResult(subject_id=legacy_owner_roots.subject_id,
                date=DAY,
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="ALT",
                value=30.0,
                unit="U/L",
                flag="normal",
            ),
            LabResult(subject_id=legacy_owner_roots.subject_id,
                date=DAY + timedelta(days=6),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="ALT",
                value=300.0,
                unit="U/L",
                flag="high",
            ),
            SkincareObservation(subject_id=legacy_owner_roots.subject_id,
                date=DAY + timedelta(days=6),
                domain=Domain.SKINCARE.value,
                source=Source.MANUAL.value,
                inflammation=5,
            ),
            HrtSideEffect(subject_id=legacy_owner_roots.subject_id,
                date=DAY + timedelta(days=6),
                domain=Domain.HRT.value,
                source=Source.MANUAL.value,
                effect_type="future-effect",
                severity=5,
            ),
            Injection(subject_id=legacy_owner_roots.subject_id,
                date=DAY + timedelta(days=6),
                domain=Domain.GLP1.value,
                source=Source.MANUAL.value,
                drug="semaglutide",
                dose_mg=2.0,
            ),
        ]
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id, on_date=DAY)

    assert ctx["weight"]["latest_date"] == DAY.isoformat()
    assert ctx["body_comp"]["date"] == DAY.isoformat()
    assert [row["date"] for row in ctx["labs"]["results_in_period"]] == [
        DAY.isoformat()
    ]
    assert ctx["skincare"] is None
    assert ctx["hrt"] is None
    assert ctx["glp1"]["injections"] is None


async def test_disabled_module_is_absent_and_explicit_in_coverage(db_session, legacy_owner_roots):
    stamp = datetime.combine(DAY, time.min)
    db_session.add_all(
        [
            HrtSideEffect(subject_id=legacy_owner_roots.subject_id,
                date=DAY,
                domain=Domain.HRT.value,
                source=Source.MANUAL.value,
                effect_type="private-effect",
                severity=3,
            ),
            Annotation(subject_id=legacy_owner_roots.subject_id,
                date=DAY,
                domain=Domain.HRT.value,
                source=Source.MANUAL.value,
                kind="note",
                title="private-timeline",
            ),
            Annotation(subject_id=legacy_owner_roots.subject_id,
                date=DAY,
                domain=Domain.TIMELINE.value,
                source=Source.MANUAL.value,
                kind="note",
                title="visible-timeline",
            ),
            Milestone(subject_id=legacy_owner_roots.subject_id,
                domain=Domain.HRT.value,
                name="private-milestone",
                status="active",
                created_at=stamp,
                updated_at=stamp,
            ),
            Milestone(subject_id=legacy_owner_roots.subject_id,
                domain=Domain.WEIGHT.value,
                name="visible-milestone",
                status="active",
                created_at=stamp,
                updated_at=stamp,
            ),
        ]
    )
    alert = await alerts_service.raise_alert(
        db_session,
        domain=Domain.HRT.value,
        severity="warn",
        message="private-alert",
        alert_key="private-hrt-alert",
    )
    alert.created_at = stamp
    await modules_service.set_module_enabled(
        db_session,
        key="hrt",
        enabled=False,
        subject_id=legacy_owner_roots.subject_id,
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id, on_date=DAY)

    assert ctx["hrt"] is None
    assert ctx["coverage"]["hrt"] == {
        "module": "hrt",
        "enabled": False,
        "status": "disabled",
        "rows": 0,
        "current_rows": 0,
        "previous_rows": 0,
        "first_date": None,
        "last_date": None,
        "freshness_days": None,
        "truncated": False,
        "event_limit_per_collection": 500,
        "cycle_rows": 0,
        "planned_rows": 0,
        "doses_truncated": False,
        "side_effects_truncated": False,
        "planned_truncated": False,
    }
    assert {row["title"] for row in ctx["timeline"]} == {
        "visible-timeline",
        "visible-milestone",
    }
    assert [row["name"] for row in ctx["milestones"]] == ["visible-milestone"]
    assert "private-" not in json.dumps(ctx)
    assert all("hrt_side_effects" not in row for row in ctx["days"])


async def test_lab_history_is_bounded_per_marker_and_reports_truncation(db_session, legacy_owner_roots):
    db_session.add_all(
        [
            LabResult(subject_id=legacy_owner_roots.subject_id,
                date=DAY - timedelta(days=offset),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="ALT",
                value=float(offset),
                unit="U/L",
                flag="normal",
            )
            for offset in range(40, 35, -1)
        ]
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id, on_date=DAY)

    assert len(ctx["labs"]["trends"][0]["points"]) == 3
    assert ctx["coverage"]["labs"]["truncated"] is True
    assert ctx["coverage"]["labs"]["history_limit_per_marker"] == 3


async def test_glp1_and_hrt_include_plan_fact_and_comparison(db_session, legacy_owner_roots, owner_write):
    db_session.add(
        HrtCompound(
            subject_id=legacy_owner_roots.subject_id,
            domain=Domain.HRT.value,
            source=Source.MANUAL.value,
            key="test_enanthate",
            name="Testosterone enanthate",
            name_ru="Тестостерон энантат",
            compound_class="testosterone",
            route="intramuscular",
            dose_unit="mg",
            half_life_hours=108.0,
        )
    )
    db_session.add(
        DosePhase(subject_id=owner_write.subject_id,
            domain=Domain.GLP1.value,
            source=Source.MANUAL.value,
            start_date=DAY - timedelta(days=30),
            drug="tirzepatide",
            dose_mg=7.5,
        )
    )
    db_session.add_all(
        [
            Injection(subject_id=owner_write.subject_id,
                date=DAY - timedelta(days=8),
                domain=Domain.GLP1.value,
                source=Source.MANUAL.value,
                drug="tirzepatide",
                dose_mg=5.0,
            ),
            Injection(subject_id=owner_write.subject_id,
                date=DAY - timedelta(days=1),
                domain=Domain.GLP1.value,
                source=Source.MANUAL.value,
                drug="tirzepatide",
                dose_mg=7.5,
            ),
            SideEffect(subject_id=owner_write.subject_id,
                date=DAY,
                domain=Domain.GLP1.value,
                source=Source.MANUAL.value,
                effect_type="nausea",
                severity=2,
            ),
        ]
    )
    await db_session.flush()
    cycle = await hrt_cycle_service.add_cycle(
        db_session,
        kind="course",
        start_date=DAY - timedelta(days=10),
        name="TRT",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await hrt_cycle_service.add_cycle_item(
        db_session,
        cycle.id,
        compound_key="test_enanthate",
        schedule=[{"dose": 100, "interval_days": 7, "duration_days": 35}],
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    db_session.add_all(
        [
            HrtDose(subject_id=owner_write.subject_id,
                date=DAY - timedelta(days=8),
                domain=Domain.HRT.value,
                source=Source.MANUAL.value,
                compound_key="test_enanthate",
                dose=90,
                unit="mg",
            ),
            HrtDose(subject_id=owner_write.subject_id,
                date=DAY - timedelta(days=1),
                domain=Domain.HRT.value,
                source=Source.MANUAL.value,
                compound_key="test_enanthate",
                dose=100,
                unit="mg",
            ),
            HrtSideEffect(subject_id=owner_write.subject_id,
                date=DAY,
                domain=Domain.HRT.value,
                source=Source.MANUAL.value,
                effect_type="acne",
                severity=3,
            ),
        ]
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id, on_date=DAY)

    assert ctx["glp1"]["active_phase"]["dose_mg"] == 7.5
    assert {row["period"] for row in ctx["glp1"]["injections"]} == {
        "current",
        "previous",
    }
    item = ctx["hrt"]["cycle"]["items"][0]
    assert item["name"] == "Testosterone enanthate"
    assert item["schedule"][0]["interval_days"] == 7.0
    assert ctx["hrt"]["doses"][0]["dose"] == 100
    assert ctx["hrt"]["comparison_doses"][0]["dose"] == 90
    assert ctx["hrt"]["planned_administrations"]
    assert ctx["hrt"]["side_effects"][0]["effect_type"] == "acne"
    assert ctx["coverage"]["hrt"]["status"] == "available"
    assert ctx["coverage"]["hrt"]["cycle_rows"] == 1


async def test_supporting_domains_are_complete_but_compact(db_session, legacy_owner_roots):
    old_scan = BodyScan(subject_id=legacy_owner_roots.subject_id,
        date=DAY - timedelta(days=20),
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
        device="Synthetic",
        file_key="private/old.jpg",
    )
    old_scan.metrics.extend(
        [
            BodyScanMetric(
                metric_key="body_fat_pct",
                label="Body fat",
                value=20.0,
                unit="%",
                category="composition",
            ),
            BodyScanMetric(
                metric_key="skeletal_muscle_mass",
                label="SMM",
                value=36.0,
                unit="kg",
                category="composition",
            ),
        ]
    )
    new_scan = BodyScan(subject_id=legacy_owner_roots.subject_id,
        date=DAY,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
        device="Synthetic",
        file_key="private/new.jpg",
    )
    new_scan.metrics.extend(
        [
            BodyScanMetric(
                metric_key="body_fat_pct",
                label="Body fat",
                value=18.5,
                unit="%",
                category="composition",
            ),
            BodyScanMetric(
                metric_key="skeletal_muscle_mass",
                label="SMM",
                value=37.0,
                unit="kg",
                category="composition",
            ),
        ]
    )
    db_session.add_all(
        [
            BodyMeasurement(subject_id=legacy_owner_roots.subject_id,
                date=DAY - timedelta(days=20),
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                neck_cm=40,
                waist_cm=94,
                body_fat_pct=21,
            ),
            BodyMeasurement(subject_id=legacy_owner_roots.subject_id,
                date=DAY,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                neck_cm=40,
                waist_cm=90,
                body_fat_pct=18,
            ),
            old_scan,
            new_scan,
            LabMarker(subject_id=legacy_owner_roots.subject_id,
                domain=Domain.LABS.value,
                name="Ferritin",
                tier=1,
                retest_interval_days=7,
                note="Protocol cadence",
            ),
            LabResult(subject_id=legacy_owner_roots.subject_id,
                date=DAY - timedelta(days=10),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="Ferritin",
                value=90,
                unit="ng/mL",
                flag="normal",
            ),
            MealLog(subject_id=legacy_owner_roots.subject_id,
                date=DAY,
                domain=Domain.NUTRITION.value,
                source=Source.MANUAL.value,
                name="Dinner",
                eaten_at=time(21, 30),
                calories=800,
                protein_g=60,
                fat_g=30,
                carbs_g=70,
            ),
            MealLog(subject_id=legacy_owner_roots.subject_id,
                date=DAY - timedelta(days=1),
                domain=Domain.NUTRITION.value,
                source=Source.MANUAL.value,
                name="Calories only",
                calories=500,
            ),
            SkincareLog(subject_id=legacy_owner_roots.subject_id,
                date=DAY,
                domain=Domain.SKINCARE.value,
                source=Source.MANUAL.value,
                retinoid=True,
                moisturizer=True,
            ),
            SkincareProduct(subject_id=legacy_owner_roots.subject_id,
                name="Retinal",
                type="Retinoid",
                active_ingredient="retinaldehyde",
                default_time="evening",
                schedule_days=[1, 3, 5],
                active=True,
            ),
            GeneticVariant(subject_id=legacy_owner_roots.subject_id,
                domain=Domain.GENETICS.value,
                source=Source.VCF_IMPORT.value,
                gene="HFE",
                rsid="rs1800562",
                genotype="AG",
                marker="hemochromatosis_carrier",
                impact="moderate",
                impact_domain=Domain.LABS.value,
                interpretation="Carrier status",
                action_notes="Interpret ferritin with clinician",
            ),
        ]
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id, on_date=DAY, period_days=14
    )

    assert ctx["weight"]["measurement_delta"]["waist_cm"] == -4
    assert {row["key"] for row in ctx["body_comp"]["deltas_from_previous_scan"]} >= {
        "body_fat_pct",
        "skeletal_muscle_mass",
    }
    assert ctx["labs"]["results_in_period"][0]["flag"] == "normal"
    assert ctx["labs"]["retest"][0]["retest_interval_days"] == 7
    assert ctx["labs"]["retest"][0]["due"] is True
    assert ctx["nutrition"]["avg_fat_per_day_g"] == 30
    assert ctx["nutrition"]["avg_carbs_per_day_g"] == 70
    assert ctx["nutrition"]["avg_protein_per_day_g"] == 60
    assert ctx["nutrition"]["metric_samples"] == {
        "calories": 2,
        "protein_g": 1,
        "fat_g": 1,
        "carbs_g": 1,
    }
    assert ctx["period_stats"]["current"]["sample_counts"]["protein_per_day_g"] == 1
    assert ctx["nutrition"]["meals_after_21"] == 1
    assert ctx["skincare"]["logs"][0]["applied"] == [
        "retinoid",
        "moisturizer",
    ]
    assert ctx["skincare"]["products"][0]["active_ingredient"] == "retinaldehyde"
    assert ctx["genetics"][0]["interpretation"] == "Carrier status"
    assert ctx["genetics"][0]["action_notes"] == "Interpret ferritin with clinician"

    payload = json.dumps(ctx)
    for forbidden in (
        "file_key",
        "raw_payload_id",
        "sleep_stages",
        "breathing_events",
        "splits",
    ):
        assert forbidden not in payload


async def test_day_context_aliases_and_truncation_are_explicit(db_session, legacy_owner_roots):
    db_session.add(
        DayContext(subject_id=legacy_owner_roots.subject_id,
            date=DAY,
            domain=Domain.SIGNALS.value,
            source=Source.MANUAL.value,
            planned={"where": "office", "gym": False, "load": "normal"},
            answers={"gym": True, "load": "heavy"},
        )
    )
    db_session.add_all(
        [
            Signal(subject_id=legacy_owner_roots.subject_id,
                date=DAY,
                domain=Domain.SIGNALS.value,
                source=Source.TELEGRAM.value,
                kind="exposure",
                key="coffee_late",
                value_num=1,
                batch_id=f"signal-{index}",
            )
            for index in range(51)
        ]
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id, on_date=DAY, period_days=1
    )

    day_context = ctx["day_context"][0]
    assert day_context["resolved"] == {
        "where": "office",
        "gym": True,
        "load": "heavy",
    }
    assert day_context["answered_keys"] == ["gym", "load"]
    assert day_context["source_by_field"]["where"] == "template"
    assert day_context["source_by_field"]["gym"] == "manual"
    assert ctx["days"][0]["day"] == day_context["resolved"]
    assert len(ctx["signals"]) == 50
    assert {row["key"] for row in ctx["signals"]} == {"caffeine_late"}
    assert ctx["signals"][0]["stored_key"] == "coffee_late"
    assert ctx["coverage"]["signals"]["truncated"] is True


async def test_ru_and_en_prompts_describe_the_same_v2_contract():
    for prompt in (digest_service.DIGEST_SYSTEM, digest_service.DIGEST_SYSTEM_EN):
        for key in (
            "schema_version=2",
            "coverage",
            "results_in_period",
            "planned_administrations",
            "sample_counts",
            "freshness_days",
            "resolved",
        ):
            assert key in prompt
        assert "active_compounds" not in prompt
