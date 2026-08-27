"""Curated, subject-scoped, PHI-conscious projection for owner-directed LLM use."""

from __future__ import annotations

import os
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vitals.models.body_scan import BodyScan
from vitals.models.garmin import GarminActivity, GarminDaily
from vitals.models.genetics import GeneticVariant
from vitals.models.glp1 import DosePhase, Injection, SideEffect
from vitals.models.hevy import HevyExercise, HevySet, HevyWorkout
from vitals.models.hrt import HrtCycle, HrtCycleTemplate, HrtDose, HrtSideEffect
from vitals.models.labs import LabResult
from vitals.models.milestones import Milestone, WeeklyDigest
from vitals.models.nutrition import MealLog
from vitals.models.skincare import SkincareLog, SkincareObservation
from vitals.models.supplements import Supplement
from vitals.models.timeline import Annotation
from vitals.models.weight import BodyMeasurement, NoiseMarker, WeightLog
from vitals.services.portability.v1_contract import (
    GENERIC_OUTPUT_SUPPRESSED_COLUMNS,
    PortabilityError,
    _serialize_value,
)
from vitals.utils.timeutils import now_local

def _llm_profile() -> dict[str, Any]:
    """Owner context (from .env) so the LLM reads the data with the right frame."""
    return {
        "height_cm": os.getenv("VITALS_HEIGHT_CM") or "190",
        "sex": os.getenv("VITALS_SEX") or "male",
        "age": os.getenv("VITALS_USER_AGE") or "18",
        "program": os.getenv("VITALS_USER_PROGRAM") or "",
        "goals": os.getenv("VITALS_USER_GOALS") or "",
        "timezone": os.getenv("VITALS_TIMEZONE", "Europe/Chisinau"),
        "exported_at": now_local().isoformat(timespec="seconds"),
        "units": {"weight": "kg", "distance": "m", "energy": "kcal"},
        "note": (
            "Экспорт данных здоровья одного пользователя (Vitals) для анализа LLM. "
            "Даты в ISO 8601, вес в кг. Это навигатор для поддержки решений, не врач."
        ),
    }


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    """Drop empty (None / "") fields so the digest stays terse for a chat window."""
    return {k: v for k, v in row.items() if v is not None and v != ""}


# Plumbing an LLM has no use for: ids, FK links and row bookkeeping.
_LLM_SKIP_COLUMNS = frozenset(
    {
        "id",
        "raw_payload_id",
        "raw_id",
        "weight_log_id",
        "domain",
        "source",
        "external_id",
        "created_at",
        "updated_at",
    }
) | GENERIC_OUTPUT_SUPPRESSED_COLUMNS


def _row_dump(obj: Any) -> dict[str, Any]:
    """Every mapped column of a row except the plumbing above.

    Used for the wide Garmin tables, where hand-listing fields meant two thirds of
    the captured metrics never reached the export — and a new column would have
    silently missed it too.
    """
    return _compact(
        {
            name: _serialize_value(getattr(obj, name))
            for name in obj.__table__.columns.keys()
            if name not in _LLM_SKIP_COLUMNS
        }
    )


def _row_within(row: dict[str, Any], since_iso: str) -> bool:
    """Keep a row that is not entirely older than ``since``.

    Three shapes go through here. A point in time (``date``) is compared directly.
    A period (``start_date``) survives on its ``end_date``, and an *open* period —
    a dose phase or cycle that started years ago and is running today — has no
    ``end_date`` at all, so it stays whatever its start says. Catalog rows
    (supplements, genetics, cycle templates) carry no date and always stay: a stack
    list is current state, not history.
    """
    end = row.get("end_date")
    if "start_date" in row:
        return end is None or end >= since_iso
    day = row.get("date")
    if day is None:
        return True
    return (end or day) >= since_iso


async def export_llm(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    domains: Sequence[str] | None = None,
    since: date | None = None,
) -> dict[str, Any]:
    """Curated, flat, secret-free digest grouped by domain — paste-into-chat ready.

    Both filters default to off, so the web download stays the whole history. The
    MCP tool narrows instead: a chat asking about this month should not be handed
    years of daily Garmin rows. ``domains`` names top-level blocks of the result
    (``weight_history``, ``biomarkers``, …); ``since`` drops rows that ended before
    that date.

    **The subject is mandatory, and every read below is filtered by it.** Twenty-two
    selects here had no subject at all: written when the installation held one
    person, correct then, and a cross-subject export the moment it held two. The
    MCP ``export_everything`` tool resolved a scope before calling this and then
    handed it nothing — so the whole lake came back, everybody's rows in one
    LLM-ready document, which is the worst possible shape for that mistake to
    take.

    No default, deliberately. An omittable scope is exactly what
    ``vitals/legacy_scope.py`` exists to keep out of this codebase, and this
    function is the reason why.
    """

    if not isinstance(subject_id, uuid.UUID) or subject_id.int == 0:
        raise PortabilityError("export_llm requires the subject it is about")
    out: dict[str, Any] = {"profile": _llm_profile()}

    # Weight — active rows only (superseded duplicates are noise for analysis).
    weights = (
        await session.execute(
            select(WeightLog).where(WeightLog.subject_id == subject_id).where(WeightLog.superseded.is_(False)).order_by(WeightLog.date)
        )
    ).scalars().all()
    out["weight_history"] = [
        _compact({"date": w.date.isoformat(), "weight_kg": w.weight_kg, "note": w.note})
        for w in weights
    ]

    measurements = (
        await session.execute(select(BodyMeasurement).where(BodyMeasurement.subject_id == subject_id).order_by(BodyMeasurement.date))
    ).scalars().all()
    out["body_measurements"] = [
        _compact(
            {
                "date": m.date.isoformat(),
                "waist_cm": m.waist_cm,
                "neck_cm": m.neck_cm,
                "hips_cm": m.hips_cm,
                "body_fat_pct": m.body_fat_pct,
                "lbm_kg": m.lbm_kg,
            }
        )
        for m in measurements
    ]

    # Body composition — BIA/InBody scans with every captured metric per scan
    # (the body_comp domain; complements the Navy body_fat_pct/lbm above).
    scans = (
        await session.execute(
            select(BodyScan).where(BodyScan.subject_id == subject_id)
            .options(selectinload(BodyScan.metrics))
            .order_by(BodyScan.date, BodyScan.id)
        )
    ).scalars().all()
    out["body_scans"] = [
        _compact(
            {
                "date": s.date.isoformat(),
                "device": s.device,
                "note": s.note,
                "metrics": [
                    _compact(
                        {
                            "metric": m.metric_key,
                            "value": m.value,
                            "unit": m.unit,
                            "segment": m.segment,
                        }
                    )
                    for m in s.metrics
                ],
            }
        )
        for s in scans
    ]

    noise = (
        await session.execute(select(NoiseMarker).where(NoiseMarker.subject_id == subject_id).order_by(NoiseMarker.start_date))
    ).scalars().all()
    out["noise_periods"] = [
        _compact(
            {
                "start_date": n.start_date.isoformat(),
                "end_date": n.end_date.isoformat() if n.end_date else None,
                "reason": n.reason,
            }
        )
        for n in noise
    ]

    # GLP-1 protocol.
    injections = (
        await session.execute(select(Injection).where(Injection.subject_id == subject_id).order_by(Injection.date))
    ).scalars().all()
    out["glp1_injections"] = [
        _compact({"date": i.date.isoformat(), "drug": i.drug, "dose_mg": i.dose_mg, "site": i.site})
        for i in injections
    ]
    phases = (
        await session.execute(select(DosePhase).where(DosePhase.subject_id == subject_id).order_by(DosePhase.start_date))
    ).scalars().all()
    out["glp1_dose_phases"] = [
        _compact(
            {
                "start_date": p.start_date.isoformat(),
                "end_date": p.end_date.isoformat() if p.end_date else None,
                "drug": p.drug,
                "dose_mg": p.dose_mg,
            }
        )
        for p in phases
    ]
    effects = (
        await session.execute(select(SideEffect).where(SideEffect.subject_id == subject_id).order_by(SideEffect.date))
    ).scalars().all()
    out["glp1_side_effects"] = [
        _compact(
            {"date": e.date.isoformat(), "effect_type": e.effect_type, "severity": e.severity}
        )
        for e in effects
    ]

    # HRT / TRT protocol — doses (with grey-market provenance), cycles with their
    # per-compound plans, side effects, and the user's saved cycle templates.
    hrt_doses = (
        await session.execute(select(HrtDose).where(HrtDose.subject_id == subject_id).order_by(HrtDose.date, HrtDose.id))
    ).scalars().all()
    out["hrt_doses"] = [
        _compact(
            {
                "date": d.date.isoformat(),
                "compound": d.compound_key,
                "dose": d.dose,
                "unit": d.unit,
                "volume_ml": d.volume_ml,
                "brand": d.brand,
                "lab": d.lab,
                "batch": d.batch,
                "site": d.site,
                "note": d.note,
            }
        )
        for d in hrt_doses
    ]
    hrt_cycles = (
        await session.execute(
            select(HrtCycle).where(HrtCycle.subject_id == subject_id)
            .options(selectinload(HrtCycle.items))
            .order_by(HrtCycle.start_date, HrtCycle.id)
        )
    ).scalars().all()
    out["hrt_cycles"] = [
        _compact(
            {
                "start_date": c.start_date.isoformat(),
                "end_date": c.end_date.isoformat() if c.end_date else None,
                "kind": c.kind,
                "name": c.name,
                "note": c.note,
                "items": [
                    _compact(
                        {
                            "compound": it.compound_key,
                            "unit": it.unit,
                            "start_offset_days": it.start_offset_days or None,
                            "schedule": it.schedule,
                        }
                    )
                    for it in c.items
                ],
            }
        )
        for c in hrt_cycles
    ]
    hrt_effects = (
        await session.execute(select(HrtSideEffect).where(HrtSideEffect.subject_id == subject_id).order_by(HrtSideEffect.date))
    ).scalars().all()
    out["hrt_side_effects"] = [
        _compact(
            {"date": e.date.isoformat(), "effect_type": e.effect_type, "severity": e.severity}
        )
        for e in hrt_effects
    ]
    hrt_templates = (
        await session.execute(
            select(HrtCycleTemplate).where(HrtCycleTemplate.subject_id == subject_id)
            .options(selectinload(HrtCycleTemplate.items))
            .order_by(HrtCycleTemplate.name)
        )
    ).scalars().all()
    out["hrt_cycle_templates"] = [
        _compact(
            {
                "name": tp.name,
                "kind": tp.kind,
                "note": tp.note,
                "items": [
                    _compact(
                        {
                            "compound": it.compound_key,
                            "unit": it.unit,
                            "start_offset_days": it.start_offset_days or None,
                            "schedule": it.schedule,
                        }
                    )
                    for it in tp.items
                ],
            }
        )
        for tp in hrt_templates
    ]

    # Labs.
    labs = (
        await session.execute(select(LabResult).where(LabResult.subject_id == subject_id).order_by(LabResult.date, LabResult.marker))
    ).scalars().all()
    out["biomarkers"] = [
        _compact(
            {
                "date": r.date.isoformat(),
                "marker": r.marker,
                "value": r.value,
                "unit": r.unit,
                "ref_low": r.ref_low,
                "ref_high": r.ref_high,
                "flag": r.flag,
            }
        )
        for r in labs
    ]

    # Workouts — rebuild the Hevy tree (workout → exercises → sets) without ids.
    out["workouts"] = await _llm_workouts(session, subject_id=subject_id)

    # Garmin — the whole daily row (sleep phases, HRV, stress, load, …) and the
    # whole activity row (HR zones, splits, training effect, …). The tall
    # ``garmin_intraday`` sample table stays out: ~3k samples a day would bury the
    # rest of the digest, and the daily row already carries its summaries.
    garmin = (
        await session.execute(select(GarminDaily).where(GarminDaily.subject_id == subject_id).order_by(GarminDaily.date))
    ).scalars().all()
    out["garmin_daily"] = [_row_dump(g) for g in garmin]
    activities = (
        await session.execute(
            select(GarminActivity).where(GarminActivity.subject_id == subject_id).order_by(GarminActivity.date, GarminActivity.id)
        )
    ).scalars().all()
    out["garmin_activities"] = [_row_dump(a) for a in activities]

    # Nutrition.
    meals = (
        await session.execute(select(MealLog).where(MealLog.subject_id == subject_id).order_by(MealLog.date))
    ).scalars().all()
    out["nutrition"] = [
        _compact(
            {
                "date": m.date.isoformat(),
                "name": m.name,
                "calories": m.calories,
                "protein_g": m.protein_g,
                "fat_g": m.fat_g,
                "carbs_g": m.carbs_g,
            }
        )
        for m in meals
    ]

    # Reference catalogs.
    supplements = (
        await session.execute(select(Supplement).where(Supplement.subject_id == subject_id).order_by(Supplement.name))
    ).scalars().all()
    out["supplements"] = [
        _compact(
            {
                "name": s.name,
                "dose": s.dose,
                "timing": s.timing,
                "evidence": s.evidence,
                "active": s.active,
                "contraindications": s.contraindications,
            }
        )
        for s in supplements
    ]
    variants = (
        await session.execute(select(GeneticVariant).where(GeneticVariant.subject_id == subject_id).order_by(GeneticVariant.gene))
    ).scalars().all()
    out["genetics"] = [
        _compact(
            {
                "gene": v.gene,
                "rsid": v.rsid,
                "genotype": v.genotype,
                "impact": v.impact,
                "interpretation": v.interpretation,
            }
        )
        for v in variants
    ]

    # Skincare logs + observations.
    sk_logs = (
        await session.execute(select(SkincareLog).where(SkincareLog.subject_id == subject_id).order_by(SkincareLog.date))
    ).scalars().all()
    out["skincare_logs"] = [
        _compact(
            {
                "date": s.date.isoformat(),
                "retinoid": s.retinoid,
                "azelaic": s.azelaic,
                "peel": s.peel,
                "niacinamide_spf": s.niacinamide_spf,
                "moisturizer": s.moisturizer,
                "vitamin_c": s.vitamin_c,
                "benzoyl_peroxide": s.benzoyl_peroxide,
            }
        )
        for s in sk_logs
    ]
    sk_obs = (
        await session.execute(select(SkincareObservation).where(SkincareObservation.subject_id == subject_id).order_by(SkincareObservation.date))
    ).scalars().all()
    out["skincare_observations"] = [
        _compact(
            {
                "date": o.date.isoformat(),
                "inflammation": o.inflammation,
                "pih": o.pih,
                "zone": o.zone,
            }
        )
        for o in sk_obs
    ]

    # Goals + generated narratives.
    milestones = (
        await session.execute(select(Milestone).where(Milestone.subject_id == subject_id).order_by(Milestone.id))
    ).scalars().all()
    out["milestones"] = [
        _compact(
            {
                "name": m.name,
                "domain": m.domain,
                "target_value": m.target_value,
                "target_unit": m.target_unit,
                "deadline": m.deadline.isoformat() if m.deadline else None,
                "status": m.status,
            }
        )
        for m in milestones
    ]
    digests = (
        await session.execute(select(WeeklyDigest).where(WeeklyDigest.subject_id == subject_id).order_by(WeeklyDigest.date))
    ).scalars().all()
    out["weekly_digests"] = [
        _compact({"date": d.date.isoformat(), "content": d.content}) for d in digests
    ]

    # Timeline — manual annotations (derived events already surface through
    # their own domain's block above, so they aren't repeated here).
    annotations = (
        await session.execute(select(Annotation).where(Annotation.subject_id == subject_id).order_by(Annotation.date))
    ).scalars().all()
    out["timeline_annotations"] = [
        _compact(
            {
                "date": a.date.isoformat(),
                "end_date": a.end_date.isoformat() if a.end_date else None,
                "domain": a.domain,
                "kind": a.kind,
                "title": a.title,
                "note": a.note,
            }
        )
        for a in annotations
    ]

    # ``signals`` stood here — the "how it actually felt" layer, parsed out of
    # chat messages. It is gone, and so is the block: a backup written now
    # carries no key for it. An older backup that has one is read and ignored,
    # which is what ``_UNKNOWN_TABLES_ARE_IGNORED`` below is for.

    # Narrowing happens on the assembled digest rather than in each of the twenty
    # queries above: the rows are already flat dicts with their dates, so one pass
    # here filters every block — including ones added later, which a per-query
    # ``where`` would have missed.
    if domains is not None:
        unknown = [d for d in domains if d not in out]
        if unknown:
            blocks = ", ".join(k for k in out if k != "profile")
            raise ValueError(f"unknown domains {unknown}; available: {blocks}")
        out = {k: v for k, v in out.items() if k == "profile" or k in domains}
    if since is not None:
        since_iso = since.isoformat()
        out = {
            k: [r for r in v if _row_within(r, since_iso)] if isinstance(v, list) else v
            for k, v in out.items()
        }

    return out


async def _llm_workouts(
    session: AsyncSession, *, subject_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Assemble Hevy workouts with their exercises and sets, id-free, in two passes
    (no N+1): load all rows, then group children by parent in Python."""
    workouts = (
        await session.execute(
            select(HevyWorkout)
            .where(HevyWorkout.subject_id == subject_id)
            .order_by(HevyWorkout.date)
        )
    ).scalars().all()
    exercises = (
        await session.execute(
            select(HevyExercise)
            .where(HevyExercise.subject_id == subject_id)
            .order_by(HevyExercise.workout_id, HevyExercise.exercise_index)
        )
    ).scalars().all()
    sets = (
        await session.execute(
            select(HevySet)
            .where(HevySet.subject_id == subject_id)
            .order_by(HevySet.exercise_id, HevySet.set_index)
        )
    ).scalars().all()

    sets_by_exercise: dict[int, list] = defaultdict(list)
    for s in sets:
        sets_by_exercise[s.exercise_id].append(s)
    exercises_by_workout: dict[int, list] = defaultdict(list)
    for e in exercises:
        exercises_by_workout[e.workout_id].append(e)

    result: list[dict[str, Any]] = []
    for w in workouts:
        result.append(
            _compact(
                {
                    "date": w.date.isoformat(),
                    "title": w.title,
                    "program": w.program,
                    "duration_min": round(w.duration_seconds / 60, 1)
                    if w.duration_seconds
                    else None,
                    "exercises": [
                        _compact(
                            {
                                "title": ex.title,
                                "sets": [
                                    _compact(
                                        {
                                            "weight_kg": st.weight_kg,
                                            "reps": st.reps,
                                            "rpe": st.rpe,
                                            "set_type": st.set_type
                                            if st.set_type != "normal"
                                            else None,
                                        }
                                    )
                                    for st in sets_by_exercise.get(ex.id, [])
                                ],
                            }
                        )
                        for ex in exercises_by_workout.get(w.id, [])
                    ],
                }
            )
        )
    return result
