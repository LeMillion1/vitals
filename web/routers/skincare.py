"""Endpoints for skincare daily checklist + observations."""
from __future__ import annotations

from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain
from vitals.services import alerts_service, skincare_service
from vitals.services.conflicts import engine
from vitals.services.conflicts.engine import ConflictBlocked
from vitals.utils.timeutils import today_local
from web.deps import get_session, require_auth
from web.templating import templates

router = APIRouter(prefix="/skincare", tags=["skincare"])


def _redirect(request: Request) -> RedirectResponse:
    response = RedirectResponse(url="/skincare", status_code=status.HTTP_303_SEE_OTHER)
    if "hx-request" in request.headers:
        response.headers["HX-Redirect"] = "/skincare"
    return response


async def _prepared_owner_write(
    db: AsyncSession,
    *,
    username: str,
    evaluation_date: date_type,
):
    context = await engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=evaluation_date,
    )
    prepared = await engine.prepare_scoped_write(
        db,
        context=context,
    )
    return context, prepared


@router.get("", response_class=HTMLResponse)
async def skincare_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    today = today_local()
    context = await engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=today,
    )
    identity = context.identity
    logs = await skincare_service.list_logs(
        db,
        subject_id=identity.subject_id,
    )
    observations = await skincare_service.list_observations(
        db,
        subject_id=identity.subject_id,
    )
    alerts = await alerts_service.list_active_scoped(
        db,
        context=alerts_service.HealthAlertContext(identity),
        domain=Domain.SKINCARE,
        legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
    )
    today_log = await skincare_service.get_log(
        db,
        today,
        subject_id=identity.subject_id,
    )

    # Load products dynamically
    products = await skincare_service.list_products(
        db,
        subject_id=identity.subject_id,
    )

    # Load active skincare rules inside the same proved legacy-owner boundary.
    conflict_rules = await engine.load_scoped_rules(
        db,
        scope=context.scope,
        domain=Domain.SKINCARE,
    )

    return templates.TemplateResponse(
        request,
        "skincare/index.html",
        {
            "username": username,
            "logs": logs,
            "observations": observations,
            "alerts": alerts,
            "today_log": today_log,
            "today": today.isoformat(),
            "products": products,
            "conflict_rules": conflict_rules,
        },
    )


@router.post("/log")
async def save_log(
    request: Request,
    date: str = Form(...),
    retinoid: bool = Form(False),
    azelaic: bool = Form(False),
    peel: bool = Form(False),
    niacinamide_spf: bool = Form(False),
    moisturizer: bool = Form(False),
    vitamin_c: bool = Form(False),
    benzoyl_peroxide: bool = Form(False),
    note: Optional[str] = Form(None),
    override: bool = Form(False),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    on_date = date_type.fromisoformat(date)
    context, prepared = await _prepared_owner_write(
        db,
        username=username,
        evaluation_date=on_date,
    )
    try:
        await skincare_service.upsert_log(
            db,
            on_date=on_date,
            retinoid=retinoid,
            azelaic=azelaic,
            peel=peel,
            niacinamide_spf=niacinamide_spf,
            moisturizer=moisturizer,
            vitamin_c=vitamin_c,
            benzoyl_peroxide=benzoyl_peroxide,
            note=note,
            override=override,
            identity=context.identity,
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


@router.post("/observation")
async def save_observation(
    request: Request,
    date: str = Form(...),
    inflammation: Optional[int] = Form(None),
    pih: Optional[int] = Form(None),
    zone: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    on_date = date_type.fromisoformat(date)
    context, prepared = await _prepared_owner_write(
        db,
        username=username,
        evaluation_date=on_date,
    )
    await skincare_service.add_observation(
        db,
        on_date=on_date,
        inflammation=inflammation,
        pih=pih,
        zone=zone,
        note=note,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)


@router.post("/log/{id}/delete")
async def delete_log(
    request: Request,
    id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    context, prepared = await _prepared_owner_write(
        db,
        username=username,
        evaluation_date=today_local(),
    )
    await skincare_service.delete_log(
        db,
        id,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)


@router.post("/observation/{id}/delete")
async def delete_observation(
    request: Request,
    id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    context, prepared = await _prepared_owner_write(
        db,
        username=username,
        evaluation_date=today_local(),
    )
    await skincare_service.delete_observation(
        db,
        id,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)


@router.post("/product/save")
async def save_product(
    request: Request,
    id: Optional[int] = Form(None),
    name: str = Form(...),
    type: str = Form(...),
    active_ingredient: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    usage_instructions: Optional[str] = Form(None),
    default_time: str = Form("evening"),
    schedule_days: list[str] = Form([]),
    active: bool = Form(False),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    days = [int(x) for x in schedule_days if x.isdigit()]
    context, prepared = await _prepared_owner_write(
        db,
        username=username,
        evaluation_date=today_local(),
    )
    if id is not None:
        await skincare_service.update_product(
            db,
            id,
            name=name,
            type=type,
            active_ingredient=active_ingredient or None,
            description=description or None,
            usage_instructions=usage_instructions or None,
            default_time=default_time,
            schedule_days=days,
            active=active,
            identity=context.identity,
            prepared_conflict_write=prepared,
        )
    else:
        await skincare_service.add_product(
            db,
            name=name,
            type=type,
            active_ingredient=active_ingredient or None,
            description=description or None,
            usage_instructions=usage_instructions or None,
            default_time=default_time,
            schedule_days=days,
            active=active,
            identity=context.identity,
            prepared_conflict_write=prepared,
        )
    await db.commit()
    return _redirect(request)


@router.post("/product/{id}/delete")
async def delete_product(
    request: Request,
    id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    context, prepared = await _prepared_owner_write(
        db,
        username=username,
        evaluation_date=today_local(),
    )
    await skincare_service.delete_product(
        db,
        id,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)
