"""Public assembly boundary for digest context projection."""

from __future__ import annotations

import uuid
from datetime import date as date_type, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.weight import BodyMeasurement
from vitals.services.digest.projection.clinical import collect_clinical
from vitals.services.digest.projection.contracts import _DOMAIN_MODULE
from vitals.services.digest.projection.formatting import (
    _garmin_activity_row,
    _garmin_daily_row,
    _nutrition_day_totals,
    _skincare_log_row,
    _subject_profile,
)
from vitals.services.digest.projection.lifestyle import collect_lifestyle
from vitals.services.digest.projection.providers import collect_providers
from vitals.services.digest.projection.stats import _mean, _window_stats
from vitals.services.digest.window import (
    CONTEXT_SCHEMA_VERSION,
    MAX_PERIOD_DAYS,
    REPORT_MODE_BRIEF,
    REPORT_MODE_CLOSED,
    report_window,
)


async def assemble_context(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    on_date: Optional[date_type] = None,
    period_days: int = 7,
    mode: str = REPORT_MODE_CLOSED,
    enabled_modules: Optional[dict[str, bool]] = None,
    max_period_days: int = MAX_PERIOD_DAYS,
    include_days: bool = True,
) -> dict:
    """Build the versioned, date-bounded context shared by report consumers.

    Every read below is scoped to ``subject_id``.  This context is what the
    weekly digest, the daily brief, the doctor's report, and the MCP composition
    tool all reason over, so a single unscoped query here would put one person's
    numbers into another person's document — which is why the subject is
    mandatory rather than inferred.

    Optional domains are gated before their queries run. Empty, disabled, and
    truncated sources remain distinguishable through the ``coverage`` block.
    Aggregate-only consumers may omit the dense calendar-day projection while
    keeping the same exact query window and period statistics.
    """

    if not isinstance(subject_id, uuid.UUID):
        raise TypeError("assemble_context requires the subject it composes for")
    window = report_window(
        on_date=on_date,
        period_days=period_days,
        mode=mode,
        max_period_days=max_period_days,
    )
    today = window.report_date
    period_start = window.period_start
    period_end = window.period_end
    prev_start = window.previous_start
    prev_end = window.previous_end

    from vitals.services.modules import preferences as module_preferences
    from vitals.services.modules.registry import MODULE_REGISTRY

    if enabled_modules is None:
        enabled = await module_preferences.get_enabled_modules(
            session, subject_id=subject_id
        )
    else:
        enabled = {
            key: (True if spec.category == "core" else bool(enabled_modules.get(key, False)))
            for key, spec in MODULE_REGISTRY.items()
        }

    def module_on(key: str) -> bool:
        spec = MODULE_REGISTRY[key]
        return spec.category == "core" or bool(enabled.get(key))

    def domain_visible(domain: str) -> bool:
        """Apply the owning module's gate to secondary cross-domain surfaces."""
        module_key = _DOMAIN_MODULE.get(domain)
        return bool(module_key and module_on(module_key))

    ctx: dict[str, Any] = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "date": today.isoformat(),  # Keep for backward compatibility
        "report_meta": {
            "report_date": today.isoformat(),
            "period_days": period_days,
            "mode": mode,
            # What the window actually covers, so the narrative dates the period
            # rather than the moment it was generated in.
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "previous_start": prev_start.isoformat(),
            "previous_end": prev_end.isoformat(),
        },
        "coverage": {},
        "user_profile": await _subject_profile(session, subject_id=subject_id),
    }

    providers = await collect_providers(
        session, ctx=ctx, subject_id=subject_id, window=window, module_on=module_on
    )
    clinical = await collect_clinical(
        session,
        ctx=ctx,
        subject_id=subject_id,
        window=window,
        module_on=module_on,
        domain_visible=domain_visible,
    )
    await collect_lifestyle(
        session,
        ctx=ctx,
        subject_id=subject_id,
        window=window,
        module_on=module_on,
        domain_visible=domain_visible,
        providers=providers,
        clinical=clinical,
    )

    weights = providers.weights
    garmin_rows = providers.garmin_rows
    garmin_activities = providers.garmin_activities
    sessions = providers.sessions
    glp1_injections = providers.glp1_injections
    glp1_effects = providers.glp1_effects
    all_meals = clinical.all_meals
    all_meals_by_date = clinical.all_meals_by_date
    skin_logs = clinical.skin_logs
    skin_obs = clinical.skin_observations
    hrt_all_doses = clinical.hrt_doses
    hrt_effects = clinical.hrt_effects
    # ``day_context`` stood here — remote or office, gym, how heavy the day was.
    # It was the difference between "HRV fell" and "HRV fell across three heavy
    # office days in a row", and it went with the questions that asked it: the
    # evening block was the only thing that ever put an answer in, and the
    # evening block went with the chat. The prompt no longer names the key
    # either — describing a field the context cannot carry is how a model comes
    # back with a paragraph about data nobody has.
    # ── The join ──────────────────────────────────────────────────────────────
    # One row per day with every domain on it. The report kept reading as a stack
    # of separate domains because that is exactly what it was handed: recovery in
    # one shape, meals as an average, training as dated sessions, the day itself
    # somewhere else. Finding "the night after a heavy session" in that meant
    # joining five differently-shaped blocks by date in its head, and it simply
    # didn't. The join is arithmetic, so it belongs here, not in the prompt — what
    # arrives is the table a person would draw before looking for a pattern.
    if mode != REPORT_MODE_BRIEF and include_days:
        by_date_workouts: dict[str, list[dict[str, Any]]] = {}
        for workout in sessions:
            if workout["in_period"]:
                by_date_workouts.setdefault(workout["date"], []).append(workout)
        by_date_activities: dict[date_type, list[Any]] = {}
        for activity in garmin_activities:
            if period_start <= activity.date <= period_end:
                by_date_activities.setdefault(activity.date, []).append(activity)
        by_date_garmin = {r.date: r for r in garmin_rows}  # one row per date
        by_date_weight = {x.date: x for x in weights}
        period_measurements = list(
            (
                await session.execute(
                    select(BodyMeasurement)
                    .where(
                        BodyMeasurement.subject_id == subject_id,
                        BodyMeasurement.date >= period_start,
                        BodyMeasurement.date <= period_end,
                    )
                    .order_by(BodyMeasurement.date)
                )
            )
            .scalars()
            .all()
        )
        by_date_measurement = {row.date: row for row in period_measurements}
        meals_by_date = all_meals_by_date
        skin_logs_by_date = {row.date: row for row in skin_logs}
        skin_obs_by_date: dict[date_type, list[Any]] = {}
        for row in skin_obs:
            skin_obs_by_date.setdefault(row.date, []).append(row)
        glp1_injections_by_date: dict[date_type, list[Any]] = {}
        for row in glp1_injections:
            glp1_injections_by_date.setdefault(row.date, []).append(row)
        glp1_effects_by_date: dict[date_type, list[Any]] = {}
        for row in glp1_effects:
            glp1_effects_by_date.setdefault(row.date, []).append(row)
        hrt_doses_by_date: dict[date_type, list[Any]] = {}
        for row in hrt_all_doses:
            hrt_doses_by_date.setdefault(row.date, []).append(row)
        hrt_effects_by_date: dict[date_type, list[Any]] = {}
        for row in hrt_effects:
            hrt_effects_by_date.setdefault(row.date, []).append(row)

        ctx["days"] = []
        for i in range(period_days):
            d = period_start + timedelta(days=i)
            g_row = by_date_garmin.get(d)
            meals = meals_by_date.get(d) or []
            workouts = by_date_workouts.get(d.isoformat(), [])
            activities = by_date_activities.get(d, [])
            measurement = by_date_measurement.get(d)
            nutrition_day = _nutrition_day_totals(meals)
            day: dict[str, Any] = {
                "date": d.isoformat(),
                "weekday": d.strftime("%a"),
                "weight_kg": (by_date_weight[d].weight_kg if d in by_date_weight else None),
                "calories": nutrition_day["calories"],
                "protein_g": nutrition_day["protein_g"],
                "fat_g": nutrition_day["fat_g"],
                "carbs_g": nutrition_day["carbs_g"],
                "meal_count": len(meals) or None,
                "last_meal_time": max(
                    (m.eaten_at for m in meals if m.eaten_at), default=None
                ).strftime("%H:%M")
                if any(m.eaten_at for m in meals)
                else None,
                "workout": (
                    {
                        "title": workouts[-1]["title"],
                        "volume_kg": workouts[-1]["volume_kg"],
                        "working_sets": workouts[-1]["working_sets"],
                        "duration_min": workouts[-1]["duration_min"],
                    }
                    if workouts
                    else None
                ),
                "hevy_workouts": [
                    {
                        "title": row["title"],
                        "program": row["program"],
                        "start_time": row["start_time"],
                        "volume_kg": row["volume_kg"],
                        "working_sets": row["working_sets"],
                        "duration_min": row["duration_min"],
                    }
                    for row in workouts
                ]
                or None,
                "garmin_activities": [_garmin_activity_row(row) for row in activities] or None,
                "body_measurement": (
                    {
                        "neck_cm": measurement.neck_cm,
                        "waist_cm": measurement.waist_cm,
                        "hips_cm": measurement.hips_cm,
                        "body_fat_pct": measurement.body_fat_pct,
                        "lbm_kg": measurement.lbm_kg,
                    }
                    if measurement
                    else None
                ),
                "glp1_injections": [
                    {"drug": row.drug, "dose_mg": row.dose_mg, "site": row.site}
                    for row in glp1_injections_by_date.get(d, [])
                ]
                or None,
                "glp1_side_effects": [
                    {"type": row.effect_type, "severity": row.severity}
                    for row in glp1_effects_by_date.get(d, [])
                ]
                or None,
                "hrt_doses": [
                    {
                        "compound_key": row.compound_key,
                        "dose": row.dose,
                        "unit": row.unit,
                    }
                    for row in hrt_doses_by_date.get(d, [])
                ]
                or None,
                "hrt_side_effects": [
                    {"type": row.effect_type, "severity": row.severity}
                    for row in hrt_effects_by_date.get(d, [])
                ]
                or None,
                "skincare": (
                    _skincare_log_row(skin_logs_by_date[d], window)["applied"]
                    if d in skin_logs_by_date
                    else None
                ),
                "skin_observations": [
                    {
                        "inflammation": row.inflammation,
                        "pih": row.pih,
                        "zone": row.zone,
                    }
                    for row in skin_obs_by_date.get(d, [])
                ]
                or None,
            }
            if g_row is not None:
                garmin_day = _garmin_daily_row(g_row)
                day.update(
                    {
                        key: value
                        for key, value in garmin_day.items()
                        if key not in {"date", "source"} and value is not None
                    }
                )
            ctx["days"].append({key: value for key, value in day.items() if value is not None})

    def training_source_stats(start: date_type, end: date_type) -> dict[str, Any]:
        period_activities = [row for row in garmin_activities if start <= row.date <= end]
        period_hevy = [
            row for row in sessions if start.isoformat() <= row["date"] <= end.isoformat()
        ]
        return {
            "garmin": {
                "activities": len(period_activities),
                "duration_min": round(
                    sum(row.duration_seconds or 0 for row in period_activities) / 60,
                    1,
                )
                or None,
                "distance_km": round(
                    sum(row.distance_m or 0 for row in period_activities) / 1000,
                    2,
                )
                or None,
            },
            "hevy": {
                "sessions": len(period_hevy),
                "duration_min": sum(row["duration_min"] or 0 for row in period_hevy) or None,
                "volume_per_session_kg": _mean(row["volume_kg"] for row in period_hevy),
                "volume_samples": sum(row["volume_kg"] is not None for row in period_hevy),
            },
        }

    ctx["training"] = {
        "deduplication": (
            "Garmin activities and Hevy sessions are source-separated; do not "
            "sum them as unique workouts without matching timestamps/types."
        ),
        "current": training_source_stats(period_start, period_end),
        "previous": training_source_stats(prev_start, prev_end),
    }

    # ── The comparison ────────────────────────────────────────────────────────
    # The reason the report was worth reading and wasn't: handed only current
    # values, a narrative can do nothing but read them back, and the dashboard
    # already did that better. Change is the part that isn't on any screen —
    # so the period and the period before it are reduced to the same shape and
    # handed over together. Weight was the one domain that already carried its
    # own history (MA + slope), and the one domain the digest ever said anything
    # about; this gives every other domain the same footing.
    if mode != REPORT_MODE_BRIEF:
        ctx["period_stats"] = {
            "current": _window_stats(
                period_start,
                period_end,
                garmin_rows,
                weights,
                all_meals,
                sessions,
                garmin_activities,
            ),
            "previous": _window_stats(
                prev_start,
                prev_end,
                garmin_rows,
                weights,
                all_meals,
                sessions,
                garmin_activities,
            ),
        }
    return ctx
