"""Weight, treatment, wearable, and training collectors."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vitals.services.digest.window import ReportWindow, _coverage, _period_name
from vitals.services.digest.projection.contracts import (
    ModuleGate,
    ProviderProjection,
    _BODY_MEASUREMENT_LIMIT,
    _BODY_SCAN_LIMIT,
    _GARMIN_ACTIVITY_LIMIT,
    _HEVY_SESSION_LIMIT,
    _REPORT_BODY_METRIC_KEYS,
    _TREATMENT_EVENT_LIMIT,
)
from vitals.services.digest.projection.formatting import (
    _GARMIN_DAILY_FIELDS,
    _bounded_scalars,
    _garmin_activity_row,
    _garmin_daily_row,
)
from vitals.services.glp1 import plateau as glp1_plateau
from vitals.services.glp1 import queries as glp1_queries


async def collect_providers(
    session: AsyncSession,
    *,
    ctx: dict[str, Any],
    subject_id: uuid.UUID,
    window: ReportWindow,
    module_on: ModuleGate,
) -> ProviderProjection:
    period_start = window.period_start
    period_end = window.period_end
    prev_start = window.previous_start
    prev_end = window.previous_end
    since = period_start
    from vitals.services.weight import analytics as weight_analytics
    from vitals.services.weight import logs as weight_logs
    from vitals.services.weight import noise as weight_noise

    # Protocol phases have their own bounded block below; the chart helper is
    # used only for weight trend math here, so do not perform its overlay query.
    series = await weight_analytics.chart_series(
        session, end=period_end, include_glp1=False, subject_id=subject_id
    )
    all_weights = list(
        await weight_logs.list_active_weights(session, end=period_end, subject_id=subject_id)
    )
    weights = [w for w in all_weights if prev_start <= w.date <= period_end]

    markers = await weight_noise.list_noise_markers(session, subject_id=subject_id)
    matching_markers = []
    for m in markers:
        if m.start_date <= period_end and (m.end_date is None or m.end_date >= prev_start):
            marker_periods = []
            if m.start_date <= period_end and (m.end_date is None or m.end_date >= period_start):
                marker_periods.append("current")
            if m.start_date <= prev_end and (m.end_date is None or m.end_date >= prev_start):
                marker_periods.append("previous")
            matching_markers.append(
                {
                    "start": m.start_date.isoformat(),
                    "end": m.end_date.isoformat() if m.end_date else None,
                    "periods": marker_periods,
                    "reason": m.reason,
                    # direction: which way the scale is biased vs real fat trend.
                    # up   = scale inflated (creatine/sodium) → real loss is better
                    # down = scale deflated (dehydration)     → real situation worse
                    # null = unknown / treat as neutral
                    "direction": m.direction,
                }
            )

    last_ma = series["trend_ma"][-1] if series["trend_ma"] else None
    latest_weight = all_weights[-1] if all_weights else None
    ctx["weight"] = {
        "latest_kg": latest_weight.weight_kg if latest_weight else None,
        # When that measurement was taken. ``latest_kg`` is the newest weight as
        # of this report's period_end; without the date an old value reads as if
        # it were measured today.
        "latest_date": latest_weight.date.isoformat() if latest_weight else None,
        "ma7_kg": last_ma["weight_kg"] if last_ma else None,
        # Date the MA7 was last calculated. During a noise period ALL measurements
        # inside it are excluded from the MA, so ma7_date will be the last clean
        # day BEFORE the noise started — potentially weeks ago. Do NOT compare
        # latest_kg directly to ma7_kg as if they describe the same moment.
        "ma7_date": last_ma["date"] if last_ma else None,
        "trend_kg_per_week": series["trend"]["slope_per_week"] if series.get("trend") else None,
        "noise_markers": matching_markers,
    }

    from vitals.models.weight import BodyMeasurement

    measurement_rows = list(
        (
            await session.execute(
                select(BodyMeasurement)
                .where(
                    BodyMeasurement.subject_id == subject_id,
                    BodyMeasurement.date <= period_end,
                )
                .order_by(BodyMeasurement.date.desc(), BodyMeasurement.id.desc())
                .limit(_BODY_MEASUREMENT_LIMIT + 1)
            )
        )
        .scalars()
        .all()
    )
    measurements_truncated = len(measurement_rows) > _BODY_MEASUREMENT_LIMIT
    measurement_rows = measurement_rows[:_BODY_MEASUREMENT_LIMIT]
    measurement_history = [
        {
            "date": row.date.isoformat(),
            "neck_cm": row.neck_cm,
            "waist_cm": row.waist_cm,
            "hips_cm": row.hips_cm,
            "body_fat_pct": row.body_fat_pct,
            "lbm_kg": row.lbm_kg,
            "note": row.note,
            "source": row.source,
        }
        for row in reversed(measurement_rows)
    ]
    measurement_delta = None
    if len(measurement_history) >= 2:
        previous_measurement, latest_measurement = measurement_history[-2:]
        measurement_delta = {
            key: (
                round(latest_measurement[key] - previous_measurement[key], 2)
                if latest_measurement[key] is not None and previous_measurement[key] is not None
                else None
            )
            for key in ("neck_cm", "waist_cm", "hips_cm", "body_fat_pct", "lbm_kg")
        }
        measurement_delta["from_date"] = previous_measurement["date"]
        measurement_delta["to_date"] = latest_measurement["date"]
    ctx["weight"]["measurements"] = measurement_history or None
    ctx["weight"]["measurement_delta"] = measurement_delta
    ctx["coverage"]["weight"] = _coverage(
        module="weight",
        enabled=True,
        dates=[row.date for row in all_weights],
        window=window,
        truncated=measurements_truncated,
        extra={
            "measurement_rows": len(measurement_rows),
            "measurement_limit": _BODY_MEASUREMENT_LIMIT,
            "measurements_truncated": measurements_truncated,
        },
    )

    glp1_enabled = module_on("glp1")
    glp1_injections: list[Any] = []
    glp1_effects: list[Any] = []
    glp1_phases: list[Any] = []
    glp1_truncated = False
    injections_truncated = False
    effects_truncated = False
    phases_truncated = False
    if glp1_enabled:
        from vitals.models.glp1 import DosePhase, Injection, SideEffect

        glp1_injections, injections_truncated = await _bounded_scalars(
            session,
            select(Injection)
            .where(
                Injection.subject_id == subject_id,
                Injection.date >= prev_start,
                Injection.date <= period_end,
            )
            .order_by(Injection.date, Injection.id),
            _TREATMENT_EVENT_LIMIT,
        )
        glp1_effects, effects_truncated = await _bounded_scalars(
            session,
            select(SideEffect)
            .where(
                SideEffect.subject_id == subject_id,
                SideEffect.date >= prev_start,
                SideEffect.date <= period_end,
            )
            .order_by(SideEffect.date, SideEffect.id),
            _TREATMENT_EVENT_LIMIT,
        )
        glp1_phases, phases_truncated = await _bounded_scalars(
            session,
            select(DosePhase)
            .where(
                DosePhase.subject_id == subject_id,
                DosePhase.start_date <= period_end,
                or_(DosePhase.end_date.is_(None), DosePhase.end_date >= prev_start),
            )
            .order_by(DosePhase.start_date, DosePhase.id),
            _TREATMENT_EVENT_LIMIT,
        )
        glp1_truncated = any((injections_truncated, effects_truncated, phases_truncated))
        phase = await glp1_queries.active_dose_phase(
            session, on_date=period_end, subject_id=subject_id
        )
        ctx["glp1"] = {
            # Legacy headline fields.
            "drug": phase.drug if phase else None,
            "dose_mg": phase.dose_mg if phase else None,
            "plateau": await glp1_plateau.evaluate_plateau(
                session, on_date=period_end, subject_id=subject_id
            ),
            "active_phase": (
                {
                    "start_date": phase.start_date.isoformat(),
                    "end_date": phase.end_date.isoformat() if phase.end_date else None,
                    "drug": phase.drug,
                    "dose_mg": phase.dose_mg,
                    "note": phase.note,
                    "source": phase.source,
                }
                if phase
                else None
            ),
            "phases": [
                {
                    "start_date": row.start_date.isoformat(),
                    "end_date": row.end_date.isoformat() if row.end_date else None,
                    "drug": row.drug,
                    "dose_mg": row.dose_mg,
                    "note": row.note,
                    "source": row.source,
                }
                for row in glp1_phases
            ]
            or None,
            "injections": [
                {
                    "date": row.date.isoformat(),
                    "period": _period_name(row.date, window),
                    "drug": row.drug,
                    "dose_mg": row.dose_mg,
                    "site": row.site,
                    "note": row.note,
                    "source": row.source,
                }
                for row in glp1_injections
            ]
            or None,
            "side_effects": [
                {
                    "date": row.date.isoformat(),
                    "period": _period_name(row.date, window),
                    "effect_type": row.effect_type,
                    "severity": row.severity,
                    "note": row.note,
                    "source": row.source,
                }
                for row in glp1_effects
            ]
            or None,
        }
    else:
        ctx["glp1"] = None
    ctx["coverage"]["glp1"] = _coverage(
        module="glp1",
        enabled=glp1_enabled,
        dates=[
            *(row.date for row in (*glp1_injections, *glp1_effects)),
            *(row.start_date for row in glp1_phases),
        ],
        window=window,
        rows=len(glp1_injections) + len(glp1_effects) + len(glp1_phases),
        truncated=glp1_truncated,
        extra={
            "event_limit_per_collection": _TREATMENT_EVENT_LIMIT,
            "phase_rows": len(glp1_phases),
            "injections_truncated": injections_truncated,
            "side_effects_truncated": effects_truncated,
            "phases_truncated": phases_truncated,
        },
    )

    from vitals.analytics.body_metrics import (
        HEADLINE_KEYS,
        METRIC_REGISTRY,
        lbm_from_scan,
    )

    body_comp_enabled = module_on("body_comp")
    scans: list[Any] = []
    scans_truncated = False
    if body_comp_enabled:
        from vitals.models.body_scan import BodyScan

        scans, scans_truncated = await _bounded_scalars(
            session,
            select(BodyScan)
            .where(BodyScan.subject_id == subject_id, BodyScan.date <= period_end)
            .options(selectinload(BodyScan.metrics))
            .order_by(BodyScan.date.desc(), BodyScan.id.desc()),
            _BODY_SCAN_LIMIT,
        )
    scan = scans[0] if scans else None
    if scan is not None:
        by_key = {m.metric_key: m for m in scan.metrics}
        comp_metrics: dict[str, Any] = {}
        for k in HEADLINE_KEYS:
            m = by_key.get(k)
            if m is not None:
                spec = METRIC_REGISTRY.get(k)
                comp_metrics[k] = {
                    "value": m.value,
                    "unit": m.unit or (spec.unit if spec else None),
                }
        lbm = lbm_from_scan(scan.metrics)
        if lbm is not None:
            comp_metrics["lean_body_mass"] = {"value": lbm, "unit": "кг"}
        scan_history = []
        for history_scan in reversed(scans):
            metrics = []
            for metric in history_scan.metrics:
                if metric.metric_key not in _REPORT_BODY_METRIC_KEYS:
                    continue
                spec = METRIC_REGISTRY.get(metric.metric_key)
                metrics.append(
                    {
                        "key": metric.metric_key,
                        "value": metric.value,
                        "unit": metric.unit or (spec.unit if spec else None),
                        "segment": metric.segment,
                        "ref_low": metric.ref_low,
                        "ref_high": metric.ref_high,
                    }
                )
            derived_lbm = lbm_from_scan(history_scan.metrics)
            if derived_lbm is not None and not any(
                item["key"] == "lean_body_mass" and item["segment"] is None for item in metrics
            ):
                metrics.append(
                    {
                        "key": "lean_body_mass",
                        "value": derived_lbm,
                        "unit": "кг",
                        "segment": None,
                        "ref_low": None,
                        "ref_high": None,
                    }
                )
            scan_history.append(
                {
                    "date": history_scan.date.isoformat(),
                    "device": history_scan.device,
                    "metrics": metrics,
                    "source": history_scan.source,
                }
            )

        deltas = []
        if len(scan_history) >= 2:
            previous_metrics = {
                (item["key"], item["segment"]): item["value"]
                for item in scan_history[-2]["metrics"]
            }
            for item in scan_history[-1]["metrics"]:
                previous_value = previous_metrics.get((item["key"], item["segment"]))
                if previous_value is None:
                    continue
                deltas.append(
                    {
                        "key": item["key"],
                        "segment": item["segment"],
                        "value": round(item["value"] - previous_value, 3),
                        "unit": item["unit"],
                    }
                )
        ctx["body_comp"] = {
            "date": scan.date.isoformat(),
            "device": scan.device,
            "metrics": comp_metrics,
            "scans": scan_history,
            "deltas_from_previous_scan": deltas or None,
        }
    else:
        ctx["body_comp"] = None
    ctx["coverage"]["body_comp"] = _coverage(
        module="body_comp",
        enabled=body_comp_enabled,
        dates=[row.date for row in scans],
        window=window,
        truncated=scans_truncated,
        extra={"scan_limit": _BODY_SCAN_LIMIT},
    )

    from vitals.services.garmin import advice as garmin_advice
    from vitals.services.garmin import queries as garmin_queries
    from vitals.models.garmin import GarminActivity, GarminDaily

    g = await garmin_queries.latest_daily(session, before_or_on=period_end, subject_id=subject_id)
    garmin_rows = list(
        await garmin_queries.list_daily_between(
            session, prev_start, period_end, subject_id=subject_id
        )
    )
    garmin_activities, garmin_activities_truncated = await _bounded_scalars(
        session,
        select(GarminActivity)
        .where(
            GarminActivity.subject_id == subject_id,
            GarminActivity.date >= prev_start,
            GarminActivity.date <= period_end,
        )
        .order_by(GarminActivity.date, GarminActivity.start_time, GarminActivity.id),
        _GARMIN_ACTIVITY_LIMIT,
    )
    total_days_logged = int(
        (
            await session.execute(
                select(func.count())
                .select_from(GarminDaily)
                .where(
                    GarminDaily.subject_id == subject_id,
                    GarminDaily.date <= period_end,
                    or_(
                        GarminDaily.sleep_score.is_not(None),
                        GarminDaily.sleep_seconds.is_not(None),
                        GarminDaily.resting_hr.is_not(None),
                        GarminDaily.hrv_avg.is_not(None),
                        GarminDaily.body_battery_high.is_not(None),
                        GarminDaily.avg_stress.is_not(None),
                        GarminDaily.steps.is_not(None),
                        GarminDaily.active_calories.is_not(None),
                    ),
                )
            )
        ).scalar()
        or 0
    )
    if g or garmin_activities:
        garmin_headline = _garmin_daily_row(g) if g else {"date": None}
        garmin_headline.update(
            {
                "advice": garmin_advice.recovery_advice(g),
                "total_days_logged": total_days_logged,
                "activities": [
                    {
                        **_garmin_activity_row(row),
                        "period": _period_name(row.date, window),
                    }
                    for row in garmin_activities
                ]
                or None,
            }
        )
        ctx["garmin"] = garmin_headline
    else:
        ctx["garmin"] = None

    def garmin_metric_counts(start: date_type, end: date_type) -> dict[str, int]:
        rows = [row for row in garmin_rows if start <= row.date <= end]
        return {
            key: sum(getattr(row, key) is not None for row in rows) for key in _GARMIN_DAILY_FIELDS
        }

    garmin_headline_outside_windows = bool(
        g is not None and all(row.id != g.id for row in garmin_rows)
    )
    ctx["coverage"]["garmin"] = _coverage(
        module="garmin",
        enabled=True,
        dates=[
            *(row.date for row in garmin_rows),
            *(row.date for row in garmin_activities),
            *([g.date] if garmin_headline_outside_windows else []),
        ],
        window=window,
        rows=(len(garmin_rows) + len(garmin_activities) + int(garmin_headline_outside_windows)),
        truncated=garmin_activities_truncated,
        extra={
            "daily_rows": len(garmin_rows),
            "activity_rows": len(garmin_activities),
            "headline_outside_windows": garmin_headline_outside_windows,
            "activity_limit": _GARMIN_ACTIVITY_LIMIT,
            "activities_truncated": garmin_activities_truncated,
            "metric_samples": {
                "current": garmin_metric_counts(period_start, period_end),
                "previous": garmin_metric_counts(prev_start, prev_end),
            },
        },
    )

    import vitals.services.hevy.queries as hevy_queries

    since = period_start
    hevy_enabled = module_on("hevy")
    hevy_rows: list[Any] = []
    hevy_truncated = False
    if hevy_enabled:
        from vitals.models.hevy import HevyExercise, HevyWorkout

        hevy_rows, hevy_truncated = await _bounded_scalars(
            session,
            select(HevyWorkout)
            .where(
                HevyWorkout.subject_id == subject_id,
                HevyWorkout.date >= prev_start,
                HevyWorkout.date <= period_end,
            )
            .options(selectinload(HevyWorkout.exercises).selectinload(HevyExercise.sets))
            .order_by(HevyWorkout.date, HevyWorkout.start_time, HevyWorkout.id),
            _HEVY_SESSION_LIMIT,
        )
        last_workout = (
            await session.execute(
                select(func.max(HevyWorkout.date)).where(
                    HevyWorkout.subject_id == subject_id, HevyWorkout.date <= period_end
                )
            )
        ).scalar()
    else:
        last_workout = None
    sessions = [
        {
            **hevy_queries.workout_summary(row),
            "in_period": row.date >= since,
            "period": _period_name(row.date, window),
            "source": row.source,
        }
        for row in hevy_rows
    ]
    # The gap between sessions is the one training number a window edge cannot
    # move. Handed only a count, the narrative had to explain the boundary to say
    # anything true — and nobody wants a paragraph about window boundaries.
    gaps = [
        (date_type.fromisoformat(b["date"]) - date_type.fromisoformat(a["date"])).days
        for a, b in zip(sessions, sessions[1:])
    ]
    ctx["hevy"] = (
        {
            "total_workouts": sum(1 for row in sessions if row["in_period"]),
            "last_workout": last_workout.isoformat() if last_workout else None,
            "mean_gap_days": round(sum(gaps) / len(gaps), 1) if gaps else None,
            "gap_samples": len(gaps),
            "sessions": sessions or None,
        }
        if hevy_enabled
        else None
    )
    hevy_latest_outside_windows = bool(
        last_workout is not None and all(row.date != last_workout for row in hevy_rows)
    )
    ctx["coverage"]["hevy"] = _coverage(
        module="hevy",
        enabled=hevy_enabled,
        dates=[
            *(row.date for row in hevy_rows),
            *([last_workout] if hevy_latest_outside_windows else []),
        ],
        window=window,
        rows=len(hevy_rows) + int(hevy_latest_outside_windows),
        truncated=hevy_truncated,
        extra={
            "session_limit": _HEVY_SESSION_LIMIT,
            "session_rows_in_windows": len(hevy_rows),
            "latest_outside_windows": hevy_latest_outside_windows,
        },
    )

    return ProviderProjection(
        all_weights=all_weights,
        weights=weights,
        measurement_history=measurement_history,
        latest_weight=latest_weight,
        scan=scan,
        garmin_rows=garmin_rows,
        garmin_activities=garmin_activities,
        sessions=sessions,
        glp1_injections=glp1_injections,
        glp1_effects=glp1_effects,
    )
