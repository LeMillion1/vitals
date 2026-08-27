"""Nutrition domain: meal logging with macro tracking."""
from __future__ import annotations

from vitals.services.alerts import lifecycle as alerts_service_lifecycle

from vitals.services.alerts import contracts as alerts_service_contracts

from vitals.services.nutrition import analytics as nutrition_analytics
from vitals.services.nutrition import queries as nutrition_queries
from vitals.services.nutrition import writes as nutrition_writes

from datetime import date as date_type, time as time_type, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain
from vitals.services.conflicts import engine
from vitals.services.conflicts.engine import ConflictBlocked
from vitals.utils.timeutils import today_local
from web.deps import get_session, require_auth
from web.templating import templates

router = APIRouter(prefix="/nutrition", tags=["nutrition"])


def _redirect(request: Request) -> RedirectResponse:
    response = RedirectResponse(url="/nutrition", status_code=status.HTTP_303_SEE_OTHER)
    if "hx-request" in request.headers:
        response.headers["HX-Redirect"] = "/nutrition"
    return response


def _pct(value: float, target: float) -> float:
    """Clamped 0-100 progress toward a target, for the ring/bar widths."""
    if not target:
        return 0.0
    return max(0.0, min(100.0, round(value / target * 100)))


@router.get("", response_class=HTMLResponse)
async def nutrition_dashboard(
    request: Request,
    date: Optional[date_type] = None,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    today = today_local()
    selected_date = date or today
    conflict_context = await engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=selected_date,
    )
    # This resolver proves there is exactly one subject, which is the only
    # state where pre-backfill NULL-subject rows may be shown safely.
    day_meals = await nutrition_queries.list_meals_for_date(
        db,
        selected_date,
        subject_id=conflict_context.identity.subject_id,
    )
    summary = await nutrition_analytics.daily_summary(
        db,
        selected_date,
        subject_id=conflict_context.identity.subject_id,
    )
    history = await nutrition_queries.list_meals(
        db,
        start=None,
        end=None,
        subject_id=conflict_context.identity.subject_id,
    )
    alerts = await alerts_service_lifecycle.list_active_scoped(
        db,
        context=alerts_service_contracts.HealthAlertContext(conflict_context.identity),
        domain=Domain.NUTRITION,
        legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
    )
    goals = await nutrition_analytics.get_goals(
        db, subject_id=conflict_context.identity.subject_id
    )

    return templates.TemplateResponse(
        request,
        "nutrition/index.html",
        {
            "username": username,
            "meals_today": day_meals,
            "summary": summary,
            "history": history,
            "alerts": alerts,
            "goals": goals,
            "today": today.isoformat(),
            "today_date": today,
            "selected_date": selected_date.isoformat(),
            "is_today": selected_date == today,
            "prev_date": (selected_date - timedelta(days=1)).isoformat(),
            "next_date": (selected_date + timedelta(days=1)).isoformat(),
            "calories_pct": _pct(summary["totals"]["calories"], goals["calories_max"]),
            "protein_pct": _pct(summary["totals"]["protein_g"], goals["protein_target_g"]),
            "macro_split": nutrition_analytics.macro_energy_shares(summary["totals"]),
        },
    )


@router.post("/meal")
async def add_meal(
    request: Request,
    id: Optional[int] = Form(None),
    date: str = Form(...),
    name: str = Form(...),
    eaten_at: Optional[str] = Form(None),
    calories: Optional[float] = Form(None),
    protein_g: Optional[float] = Form(None),
    fat_g: Optional[float] = Form(None),
    carbs_g: Optional[float] = Form(None),
    note: Optional[str] = Form(None),
    override: bool = Form(False),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    on_date = date_type.fromisoformat(date)
    parsed_time = time_type.fromisoformat(eaten_at) if eaten_at and eaten_at.strip() else None
    try:
        conflict_context = (
            await engine.resolve_legacy_conflict_write_context(
                db,
                actor_username=username,
                evaluation_date=on_date,
            )
        )
        prepared = await engine.prepare_scoped_write(
            db,
            context=conflict_context,
        )
        if id is not None:
            await nutrition_writes.update_meal(
                db, id,
                on_date=on_date,
                name=name,
                eaten_at=parsed_time,
                calories=calories,
                protein_g=protein_g,
                fat_g=fat_g,
                carbs_g=carbs_g,
                note=note,
                override=override,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        else:
            await nutrition_writes.log_meal(
                db,
                on_date=on_date,
                name=name,
                eaten_at=parsed_time,
                calories=calories,
                protein_g=protein_g,
                fat_g=fat_g,
                carbs_g=carbs_g,
                note=note,
                override=override,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        await db.commit()
    except ConflictBlocked as e:
        await db.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"violations": [v.to_dict() for v in e.violations]},
        )
    return _redirect(request)


@router.post("/meal/{id}/delete")
async def delete_meal(
    request: Request,
    id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    conflict_context = await engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
    )
    prepared = await engine.prepare_scoped_write(
        db,
        context=conflict_context,
    )
    await nutrition_writes.delete_meal(
        db,
        id,
        identity=conflict_context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)
