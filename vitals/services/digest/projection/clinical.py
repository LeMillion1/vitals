"""Labs, nutrition, catalog, genetics, alert, and HRT collectors."""

from __future__ import annotations

import uuid
from datetime import date as date_type, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.digest.window import ReportWindow, _coverage, _period_name
from vitals.services.alerts import legacy as alerts_service_legacy
from vitals.services.alerts import validation as alerts_service_validation
from vitals.services.nutrition import analytics as nutrition_analytics
from vitals.services.nutrition import queries as nutrition_queries
from vitals.services.skincare import queries as skincare_queries
from vitals.services.supplements import queries as supplement_queries
from vitals.services.digest.projection.contracts import (
    ClinicalProjection,
    DomainVisibility,
    ModuleGate,
    _GENETICS_LIMIT,
    _LAB_HISTORY_PER_MARKER,
    _SKINCARE_EVENT_LIMIT,
    _TREATMENT_EVENT_LIMIT,
)
from vitals.services.digest.projection.formatting import (
    _NUTRITION_FIELDS,
    _bounded_scalars,
    _nutrition_day_totals,
    _skincare_log_row,
)
from vitals.services.digest.projection.stats import _mean


async def collect_clinical(
    session: AsyncSession,
    *,
    ctx: dict[str, Any],
    subject_id: uuid.UUID,
    window: ReportWindow,
    module_on: ModuleGate,
    domain_visible: DomainVisibility,
) -> ClinicalProjection:
    period_start = window.period_start
    period_end = window.period_end
    prev_start = window.previous_start
    prev_end = window.previous_end
    since = period_start
    from vitals.models.labs import LabMarker, LabResult
    from vitals.services.labs.flags import is_out_of_range

    # Every result in the two comparison windows is retained. Older history is
    # bounded to the latest points per marker, which is all the trend block can
    # emit; this avoids loading an unbounded lifetime table just to slice it in
    # Python afterwards.
    lab_window_rows = list(
        (
            await session.execute(
                select(LabResult)
                .where(
                    LabResult.subject_id == subject_id,
                    LabResult.date >= prev_start,
                    LabResult.date <= period_end,
                )
                .order_by(LabResult.date.desc(), LabResult.id.desc())
            )
        )
        .scalars()
        .all()
    )
    ranked_lab_ids = (
        select(
            LabResult.id.label("id"),
            func.row_number()
            .over(
                partition_by=LabResult.marker_key,
                order_by=(LabResult.date.desc(), LabResult.id.desc()),
            )
            .label("history_rank"),
        )
        .where(LabResult.subject_id == subject_id, LabResult.date <= period_end)
        .subquery()
    )
    recent_lab_rows = list(
        (
            await session.execute(
                # Scoped through the ranked subquery above, which is already
                # restricted to this subject.
                select(LabResult)
                .join(ranked_lab_ids, LabResult.id == ranked_lab_ids.c.id)
                .where(ranked_lab_ids.c.history_rank <= _LAB_HISTORY_PER_MARKER)
                .order_by(LabResult.date.desc(), LabResult.id.desc())
            )
        )
        .scalars()
        .all()
    )
    lab_rows_by_id = {row.id: row for row in (*lab_window_rows, *recent_lab_rows)}
    lab_rows = sorted(
        lab_rows_by_id.values(),
        key=lambda row: (row.date, row.id),
        reverse=True,
    )
    total_lab_rows_as_of = int(
        (
            await session.execute(
                select(func.count())
                .select_from(LabResult)
                .where(
                    LabResult.subject_id == subject_id,
                    LabResult.date <= period_end,
                )
            )
        ).scalar()
        or 0
    )
    labs_truncated = total_lab_rows_as_of > len(lab_rows)
    marker_rows: dict[str, list[Any]] = {}
    for row in lab_rows:
        marker_rows.setdefault(row.marker_key, []).append(row)
    marker_catalog = {
        row.normalized_name: row
        for row in (
            await session.execute(
                select(LabMarker)
                .where(
                    LabMarker.subject_id == subject_id,
                    LabMarker.is_canonical.is_(True),
                )
                .order_by(LabMarker.name)
            )
        )
        .scalars()
        .all()
    }
    latest_labs = [rows[0] for rows in marker_rows.values()]
    results_in_period = [row for row in lab_rows if period_start <= row.date <= period_end]
    retest_rows = []
    for marker_key, rows in marker_rows.items():
        latest = rows[0]
        catalog = marker_catalog.get(marker_key)
        marker = catalog.name if catalog is not None else latest.marker
        interval = catalog.retest_interval_days if catalog else None
        next_retest = latest.date + timedelta(days=interval) if interval else None
        deferred = bool(
            catalog and catalog.defer_until is not None and catalog.defer_until > period_end
        )
        retest_rows.append(
            {
                "marker": marker,
                "latest_date": latest.date.isoformat(),
                "tier": catalog.tier if catalog else None,
                "retest_interval_days": interval,
                "next_retest_date": next_retest.isoformat() if next_retest else None,
                "defer_until": (
                    catalog.defer_until.isoformat() if catalog and catalog.defer_until else None
                ),
                "due": bool(next_retest and next_retest <= period_end and not deferred),
                "note": catalog.note if catalog else None,
            }
        )

    ctx["labs"] = {
        "out_of_range": [
            {
                "marker": row.marker,
                "value": row.value,
                "unit": row.unit,
                "flag": row.flag,
                "date": row.date.isoformat(),
                "ref_low": row.ref_low,
                "ref_high": row.ref_high,
                "lab_name": row.lab_name,
                "note": row.note,
                "source": row.source,
            }
            for row in latest_labs
            if is_out_of_range(row.flag) and 0 <= (period_end - row.date).days <= 14
        ],
        "results_in_period": [
            {
                "marker": row.marker,
                "value": row.value,
                "unit": row.unit,
                "flag": row.flag,
                "date": row.date.isoformat(),
                "ref_low": row.ref_low,
                "ref_high": row.ref_high,
                "lab_name": row.lab_name,
                "note": row.note,
                "source": row.source,
            }
            for row in reversed(results_in_period)
        ]
        or None,
        "trends": [
            {
                "marker": (
                    marker_catalog[marker_key].name
                    if marker_key in marker_catalog
                    else rows[0].marker
                ),
                "unit": rows[0].unit,
                "ref_low": rows[0].ref_low,
                "ref_high": rows[0].ref_high,
                "points": [
                    {
                        "date": row.date.isoformat(),
                        "value": row.value,
                        "flag": row.flag,
                    }
                    for row in reversed(rows[:_LAB_HISTORY_PER_MARKER])
                ],
            }
            for marker_key, rows in marker_rows.items()
            if len(rows) >= 2
        ]
        or None,
        "retest": retest_rows or None,
    }
    ctx["coverage"]["labs"] = _coverage(
        module="labs",
        enabled=True,
        dates=[row.date for row in lab_rows],
        window=window,
        truncated=labs_truncated,
        extra={
            "markers": len(marker_rows),
            "history_limit_per_marker": _LAB_HISTORY_PER_MARKER,
            "total_rows_as_of_period_end": total_lab_rows_as_of,
        },
    )

    # Two periods of meals: the block below is about this one, the comparison at
    # the end needs the one before it, and one read covers both.
    nutrition_enabled = module_on("nutrition")
    all_meals = (
        list(
            await nutrition_queries.list_meals(
                session, start=prev_start, end=period_end, subject_id=subject_id
            )
        )
        if nutrition_enabled
        else []
    )
    all_meals_by_date: dict[date_type, list[Any]] = {}
    for meal in all_meals:
        all_meals_by_date.setdefault(meal.date, []).append(meal)
    nutrition_totals_by_date = {
        on_date: _nutrition_day_totals(day_meals)
        for on_date, day_meals in all_meals_by_date.items()
    }
    nutrition_meals = [m for m in all_meals if m.date >= since]
    if nutrition_meals:
        current_totals = [
            totals
            for on_date, totals in nutrition_totals_by_date.items()
            if period_start <= on_date <= period_end
        ]
        days_with_logs = len(current_totals)
        meals_after_21 = sum(bool(m.eaten_at and m.eaten_at.hour >= 21) for m in nutrition_meals)
        goals = await nutrition_analytics.get_goals(session, subject_id=subject_id)
        ctx["nutrition"] = {
            "avg_calories_per_day": _mean(totals["calories"] for totals in current_totals),
            "avg_protein_per_day_g": _mean(totals["protein_g"] for totals in current_totals),
            "avg_fat_per_day_g": _mean(totals["fat_g"] for totals in current_totals),
            "avg_carbs_per_day_g": _mean(totals["carbs_g"] for totals in current_totals),
            "days_with_logs": days_with_logs,
            "total_meals": len(nutrition_meals),
            "meals_after_21": meals_after_21,
            "metric_samples": {
                key: sum(totals[key] is not None for totals in current_totals)
                for key, _attr in _NUTRITION_FIELDS
            },
            "goals": goals,
        }
    else:
        ctx["nutrition"] = None
    ctx["coverage"]["nutrition"] = _coverage(
        module="nutrition",
        enabled=nutrition_enabled,
        dates=[row.date for row in all_meals],
        window=window,
        extra={
            "metric_samples": {
                period: {
                    key: sum(
                        totals[key] is not None
                        for on_date, totals in nutrition_totals_by_date.items()
                        if start <= on_date <= end
                    )
                    for key, _attr in _NUTRITION_FIELDS
                }
                for period, start, end in (
                    ("current", period_start, period_end),
                    ("previous", prev_start, prev_end),
                )
            }
        },
    )

    # Supplements / skincare / genetics / active alerts — enabled domains the
    # digest used to ignore, so cross-domain reasoning it promises (e.g. "started
    # ashwagandha → sleep/HRV shifted", "introduced a retinoid → skin reacted")
    # had no data to work with. Each domain is read through its own service (lazy
    # import); empty → null.

    supplements_enabled = module_on("supplements")
    all_supps = (
        list(
            await supplement_queries.list_supplements(
                session, subject_id=subject_id, active_only=False
            )
        )
        if supplements_enabled
        else []
    )
    active_supps = [row for row in all_supps if row.active]
    ctx["supplements"] = (
        [
            {
                "key": s.key,
                "name": s.name,
                "dose": s.dose,
                "timing": s.timing,
                "evidence": s.evidence,
                "contraindications": s.contraindications,
                "note": s.note,
                "source": s.source,
                # The table is a current catalog, not a dated adherence log.
                "state_is_current_catalog": True,
            }
            for s in active_supps
        ]
        if active_supps
        else None
    )
    ctx["coverage"]["supplements"] = _coverage(
        module="supplements",
        enabled=supplements_enabled,
        window=window,
        rows=len(all_supps),
        extra={
            "active_rows": len(active_supps),
            "historical_state_reliable": False,
        },
    )

    from vitals.models.skincare import SkincareLog, SkincareObservation

    skincare_enabled = module_on("skincare")
    if skincare_enabled:
        skin_logs, skin_logs_truncated = await _bounded_scalars(
            session,
            select(SkincareLog)
            .where(
                SkincareLog.subject_id == subject_id,
                SkincareLog.date >= prev_start,
                SkincareLog.date <= period_end,
            )
            .order_by(SkincareLog.date.desc(), SkincareLog.id.desc()),
            _SKINCARE_EVENT_LIMIT,
        )
        skin_obs, skin_obs_truncated = await _bounded_scalars(
            session,
            select(SkincareObservation)
            .where(
                SkincareObservation.subject_id == subject_id,
                SkincareObservation.date >= prev_start,
                SkincareObservation.date <= period_end,
            )
            .order_by(
                SkincareObservation.date.desc(),
                SkincareObservation.id.desc(),
            ),
            _SKINCARE_EVENT_LIMIT,
        )
        skin_logs.reverse()
        skin_obs.reverse()
        all_products = list(
            await skincare_queries.list_products(session, subject_id=subject_id, active_only=False)
        )
        active_products = [row for row in all_products if row.active]
    else:
        skin_logs = []
        skin_obs = []
        all_products = []
        active_products = []
        skin_logs_truncated = False
        skin_obs_truncated = False
    current_skin_obs = [row for row in skin_obs if row.date >= period_start]
    current_skin_logs = [row for row in skin_logs if row.date >= period_start]
    if current_skin_obs or current_skin_logs or active_products:
        ctx["skincare"] = {
            "recent_observations": [
                {
                    "date": o.date.isoformat(),
                    "inflammation": o.inflammation,
                    "pih": o.pih,
                    "zone": o.zone,
                    "note": o.note,
                    "source": o.source,
                }
                for o in current_skin_obs
            ],
            "active_products": len(active_products),
            "products": [
                {
                    "name": product.name,
                    "type": product.type,
                    "active_ingredient": product.active_ingredient,
                    "default_time": product.default_time,
                    "schedule_days": product.schedule_days,
                    "usage_instructions": product.usage_instructions,
                    "state_is_current_catalog": True,
                }
                for product in active_products
            ]
            or None,
            "logs": [_skincare_log_row(row, window) for row in current_skin_logs] or None,
            "comparison_logs": [
                _skincare_log_row(row, window) for row in skin_logs if row.date <= prev_end
            ]
            or None,
            "comparison_observations": [
                {
                    "date": row.date.isoformat(),
                    "inflammation": row.inflammation,
                    "pih": row.pih,
                    "zone": row.zone,
                    "note": row.note,
                    "source": row.source,
                }
                for row in skin_obs
                if row.date <= prev_end
            ]
            or None,
        }
    else:
        ctx["skincare"] = None
    ctx["coverage"]["skincare"] = _coverage(
        module="skincare",
        enabled=skincare_enabled,
        dates=[row.date for row in (*skin_logs, *skin_obs)],
        window=window,
        rows=len(skin_logs) + len(skin_obs) + len(all_products),
        truncated=skin_logs_truncated or skin_obs_truncated,
        extra={
            "product_rows": len(active_products),
            "event_limit_per_collection": _SKINCARE_EVENT_LIMIT,
            "logs_truncated": skin_logs_truncated,
            "observations_truncated": skin_obs_truncated,
            "historical_product_state_reliable": False,
        },
    )

    genetics_enabled = module_on("genetics")
    variants: list[Any] = []
    genetics_truncated = False
    if genetics_enabled:
        from vitals.models.genetics import GeneticVariant

        variants, genetics_truncated = await _bounded_scalars(
            session,
            select(GeneticVariant)
            .where(
                GeneticVariant.subject_id == subject_id,
                or_(
                    GeneticVariant.marker.is_not(None),
                    GeneticVariant.impact.is_not(None),
                    GeneticVariant.interpretation.is_not(None),
                    GeneticVariant.action_notes.is_not(None),
                ),
            )
            .order_by(GeneticVariant.gene, GeneticVariant.rsid),
            _GENETICS_LIMIT,
        )
    ctx["genetics"] = [
        {
            "marker": row.marker,
            "gene": row.gene,
            "rsid": row.rsid,
            "genotype": row.genotype,
            "impact": row.impact,
            "impact_domain": row.impact_domain,
            "interpretation": row.interpretation,
            "action_notes": row.action_notes,
            "source": row.source,
        }
        for row in variants
    ] or None
    ctx["coverage"]["genetics"] = _coverage(
        module="genetics",
        enabled=genetics_enabled,
        window=window,
        rows=len(variants),
        truncated=genetics_truncated,
    )

    active_alerts = [
        row
        for row in await alerts_service_legacy.list_active(session, subject_id=subject_id)
        if row.created_at.date() <= period_end
        and domain_visible(row.domain)
        and not alerts_service_validation.is_platform_alert_key(row.alert_key)
    ]
    ctx["alerts"] = (
        [
            {
                "severity": a.severity,
                "domain": a.domain,
                "message": a.message,
                "alert_key": a.alert_key,
            }
            for a in active_alerts
        ]
        if active_alerts
        else None
    )
    ctx["coverage"]["alerts"] = _coverage(
        module="reports",
        enabled=True,
        window=window,
        rows=len(active_alerts),
    )

    # HRT — the strongest intervention in the lake and, until now, invisible to the
    # digest: a compound change and the sleep/labs/skin shift that follows it could
    # never be connected. Active protocol + doses inside the period + side effects.
    from vitals.services.hrt import cycles, records
    from vitals.models.hrt import HrtDose, HrtSideEffect

    hrt_enabled = module_on("hrt")
    cycle = None
    hrt_all_doses: list[Any] = []
    hrt_effects: list[Any] = []
    planned_hrt: list[dict[str, Any]] = []
    hrt_truncated = False
    doses_truncated = False
    hrt_effects_truncated = False
    planned_truncated = False
    compound_names: dict[str, dict[str, Any]] = {}
    if hrt_enabled:
        cycle = await cycles.active_cycle(session, on_date=period_end, subject_id=subject_id)
        hrt_all_doses, doses_truncated = await _bounded_scalars(
            session,
            select(HrtDose)
            .where(
                HrtDose.subject_id == subject_id,
                HrtDose.date >= prev_start,
                HrtDose.date <= period_end,
            )
            .order_by(HrtDose.date, HrtDose.id),
            _TREATMENT_EVENT_LIMIT,
        )
        hrt_effects, hrt_effects_truncated = await _bounded_scalars(
            session,
            select(HrtSideEffect)
            .where(
                HrtSideEffect.subject_id == subject_id,
                HrtSideEffect.date >= prev_start,
                HrtSideEffect.date <= period_end,
            )
            .order_by(HrtSideEffect.date, HrtSideEffect.id),
            _TREATMENT_EVENT_LIMIT,
        )
        compounds = await records.list_compounds(session, subject_id=subject_id, active_only=False)
        compound_names = {
            row.key: {
                "name": row.name,
                "name_ru": row.name_ru,
                "class": row.compound_class,
                "route": row.route,
                "half_life_hours": row.half_life_hours,
            }
            for row in compounds
        }
        if cycle is not None:
            all_planned = await cycles.planned_administrations(
                session,
                start=prev_start,
                end=period_end,
                cycle=cycle,
                subject_id=subject_id,
            )
            planned_truncated = len(all_planned) > _TREATMENT_EVENT_LIMIT
            planned_hrt = all_planned[:_TREATMENT_EVENT_LIMIT]
        hrt_truncated = any((doses_truncated, hrt_effects_truncated, planned_truncated))

    def hrt_dose_row(row) -> dict[str, Any]:
        meta = compound_names.get(row.compound_key) or {}
        return {
            "date": row.date.isoformat(),
            "period": _period_name(row.date, window),
            "compound_key": row.compound_key,
            "compound": meta.get("name"),
            "compound_ru": meta.get("name_ru"),
            "dose": row.dose,
            "unit": row.unit,
            "site": row.site,
            "note": row.note,
            "source": row.source,
        }

    hrt_doses = [row for row in hrt_all_doses if row.date >= period_start]
    previous_hrt_doses = [row for row in hrt_all_doses if row.date <= prev_end]
    current_hrt_effects = [row for row in hrt_effects if row.date >= period_start]
    previous_hrt_effects = [row for row in hrt_effects if row.date <= prev_end]
    ctx["hrt"] = (
        {
            "cycle": (
                {
                    "kind": cycle.kind,
                    "name": cycle.name,
                    "start_date": cycle.start_date.isoformat(),
                    "end_date": cycle.end_date.isoformat() if cycle.end_date else None,
                    "note": cycle.note,
                    "compounds": [item.compound_key for item in cycle.items],
                    "items": [
                        {
                            "compound_key": item.compound_key,
                            **(compound_names.get(item.compound_key) or {}),
                            "unit": item.unit,
                            "start_offset_days": item.start_offset_days,
                            "schedule": item.schedule,
                            "note": item.note,
                        }
                        for item in cycle.items
                    ],
                }
                if cycle is not None
                else None
            ),
            # Legacy current-period fields.
            "doses": [hrt_dose_row(row) for row in hrt_doses],
            "side_effects": [
                {
                    "date": row.date.isoformat(),
                    "period": "current",
                    "effect_type": row.effect_type,
                    "severity": row.severity,
                    "note": row.note,
                    "source": row.source,
                }
                for row in current_hrt_effects
            ],
            "comparison_doses": [hrt_dose_row(row) for row in previous_hrt_doses] or None,
            "comparison_side_effects": [
                {
                    "date": row.date.isoformat(),
                    "period": "previous",
                    "effect_type": row.effect_type,
                    "severity": row.severity,
                    "note": row.note,
                    "source": row.source,
                }
                for row in previous_hrt_effects
            ]
            or None,
            "planned_administrations": [
                {
                    "date": item["date"].isoformat(),
                    "period": _period_name(item["date"], window),
                    "compound_key": item["compound_key"],
                    "compound": (compound_names.get(item["compound_key"]) or {}).get("name"),
                    "dose": item["dose"],
                    "unit": item["unit"],
                }
                for item in planned_hrt
            ]
            or None,
        }
        if (cycle is not None or hrt_all_doses or hrt_effects)
        else None
    )
    ctx["coverage"]["hrt"] = _coverage(
        module="hrt",
        enabled=hrt_enabled,
        dates=[
            *(row.date for row in (*hrt_all_doses, *hrt_effects)),
            *([cycle.start_date] if cycle is not None else []),
        ],
        window=window,
        rows=(len(hrt_all_doses) + len(hrt_effects) + len(planned_hrt) + int(cycle is not None)),
        truncated=hrt_truncated,
        extra={
            "event_limit_per_collection": _TREATMENT_EVENT_LIMIT,
            "cycle_rows": int(cycle is not None),
            "planned_rows": len(planned_hrt),
            "doses_truncated": doses_truncated,
            "side_effects_truncated": hrt_effects_truncated,
            "planned_truncated": planned_truncated,
        },
    )

    return ClinicalProjection(
        all_meals=all_meals,
        all_meals_by_date=all_meals_by_date,
        all_supplements=all_supps,
        skin_logs=skin_logs,
        skin_observations=skin_obs,
        all_products=all_products,
        variants=variants,
        hrt_doses=hrt_all_doses,
        hrt_effects=hrt_effects,
    )
