"""Endpoints for the GLP-1 protocol: injections, dose phases, side effects."""
from __future__ import annotations

from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Drug, InjectionSite, Source
from vitals.services import alerts_service, conflict_engine, glp1_service
from vitals.services.conflict_engine import ConflictBlocked
from vitals.utils.timeutils import today_local
from web.deps import get_session, require_auth
from web.templating import templates

router = APIRouter(prefix="/glp1", tags=["glp1"])


def _redirect(request: Request) -> RedirectResponse:
    response = RedirectResponse(url="/glp1", status_code=status.HTTP_303_SEE_OTHER)
    if "hx-request" in request.headers:
        response.headers["HX-Redirect"] = "/glp1"
    return response


async def _prepared_owner_write(
    db: AsyncSession,
    *,
    username: str,
    evaluation_date: date_type,
):
    context = await conflict_engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=evaluation_date,
    )
    prepared = await conflict_engine.prepare_scoped_write(
        db,
        context=context,
    )
    return context, prepared


@router.get("", response_class=HTMLResponse)
async def glp1_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """GLP-1 dashboard: current dose, body-map rotation, injections, side effects."""
    today = today_local()
    context, prepared = await _prepared_owner_write(
        db,
        username=username,
        evaluation_date=today,
    )
    await glp1_service.refresh_plateau_alert(
        db,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )

    scope_kwargs = {"subject_id": context.identity.subject_id}
    injections = await glp1_service.list_injections(db, **scope_kwargs)
    phases = await glp1_service.list_dose_phases(db, **scope_kwargs)
    side_effects = await glp1_service.list_side_effects(db, **scope_kwargs)
    alerts = await alerts_service.list_active_scoped(
        db,
        context=alerts_service.HealthAlertContext(context.identity),
        domain=Domain.GLP1,
        legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
    )

    active_phase = await glp1_service.active_dose_phase(
        db,
        on_date=today,
        **scope_kwargs,
    )
    last_inj = await glp1_service.last_injection(db, **scope_kwargs)
    await db.commit()

    return templates.TemplateResponse(
        request,
        "glp1/index.html",
        {
            "username": username,
            "injections": injections,
            "phases": sorted(phases, key=lambda p: p.start_date, reverse=True),
            "side_effects": side_effects,
            "alerts": alerts,
            "active_phase": active_phase,
            "last_injection": last_inj,
            "site_counts": glp1_service.site_frequency(injections),
            "drugs": [d.value for d in Drug],
            "sites": [s.value for s in InjectionSite],
            "today": today.isoformat(),
            "today_date": today,
        },
    )


@router.post("/injection")
async def add_injection(
    request: Request,
    id: Optional[int] = Form(None),
    date: str = Form(...),
    drug: str = Form(...),
    dose_mg: float = Form(...),
    site: Optional[str] = Form(None),
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
        if id is not None:
            await glp1_service.update_injection(
                db, id, on_date=on_date, drug=drug, dose_mg=dose_mg,
                site=site, note=note, override=override,
                identity=context.identity,
                prepared_conflict_write=prepared,
            )
        else:
            await glp1_service.log_injection(
                db,
                on_date=on_date,
                drug=drug,
                dose_mg=dose_mg,
                site=site,
                note=note,
                source=Source.MANUAL.value,
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


@router.post("/phase")
async def add_phase(
    request: Request,
    start_date: str = Form(...),
    end_date: Optional[str] = Form(None),
    drug: str = Form(...),
    dose_mg: float = Form(...),
    note: Optional[str] = Form(None),
    override: bool = Form(False),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    start = date_type.fromisoformat(start_date)
    end = date_type.fromisoformat(end_date) if end_date else None
    context, prepared = await _prepared_owner_write(
        db,
        username=username,
        evaluation_date=start,
    )
    try:
        await glp1_service.add_dose_phase(
            db,
            start_date=start,
            end_date=end,
            drug=drug,
            dose_mg=dose_mg,
            note=note,
            source=Source.MANUAL.value,
            override=override,
            identity=context.identity,
            prepared_conflict_write=prepared,
        )
        await db.commit()
    except ConflictBlocked as exc:
        await db.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"violations": [v.to_dict() for v in exc.violations]},
        )
    return _redirect(request)


@router.post("/side-effect")
async def add_side_effect(
    request: Request,
    date: str = Form(...),
    effect_type: str = Form(...),
    severity: int = Form(...),
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
    await glp1_service.log_side_effect(
        db,
        on_date=on_date,
        effect_type=effect_type,
        severity=severity,
        note=note,
        source=Source.MANUAL.value,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)


@router.post("/injection/{id}/delete")
async def delete_injection(
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
    await glp1_service.delete_injection(
        db,
        id,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)


@router.post("/phase/{id}/delete")
async def delete_phase(
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
    await glp1_service.delete_dose_phase(
        db,
        id,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)


@router.post("/side-effect/{id}/delete")
async def delete_side_effect(
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
    await glp1_service.delete_side_effect(
        db,
        id,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)
