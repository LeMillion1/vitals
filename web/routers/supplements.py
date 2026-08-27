"""Endpoints for the supplements catalog (reference, no daily logging)."""
from __future__ import annotations

from vitals.services.alerts import lifecycle as alerts_service_lifecycle

from vitals.services.alerts import contracts as alerts_service_contracts

from vitals.services.supplements import queries as supplement_queries
from vitals.services.supplements import writes as supplement_writes

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Evidence
from vitals.services.conflicts import engine
from vitals.services.conflicts.engine import ConflictBlocked
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from web.deps import get_session, require_auth
from web.templating import templates

router = APIRouter(prefix="/supplements", tags=["supplements"])


def _redirect(request: Request) -> RedirectResponse:
    response = RedirectResponse(url="/supplements", status_code=status.HTTP_303_SEE_OTHER)
    if "hx-request" in request.headers:
        response.headers["HX-Redirect"] = "/supplements"
    return response


@router.get("", response_class=HTMLResponse)
async def supplements_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    supplements = await supplement_queries.list_supplements(
        db,
        subject_id=ownership.subject_id,
    )
    alerts = await alerts_service_lifecycle.list_active_scoped(
        db,
        context=alerts_service_contracts.HealthAlertContext(ownership.owner_action()),
        domain=Domain.SUPPLEMENTS,
        legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
    )
    return templates.TemplateResponse(
        request,
        "supplements/index.html",
        {
            "username": username,
            "supplements": supplements,
            "alerts": alerts,
            "evidence_tiers": [e.value for e in Evidence],
        },
    )


@router.post("/save")
async def save_supplement(
    request: Request,
    id: Optional[int] = Form(None),
    name: str = Form(...),
    dose: Optional[str] = Form(None),
    timing: Optional[str] = Form(None),
    evidence: Optional[str] = Form(None),
    active: bool = Form(False),
    contraindications: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    override: bool = Form(False),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    try:
        conflict_context = (
            await engine.resolve_legacy_conflict_write_context(
                db,
                actor_username=username,
            )
        )
        prepared = await engine.prepare_scoped_write(
            db,
            context=conflict_context,
        )
        if id is not None:
            await supplement_writes.update_supplement(
                db,
                id,
                name=name,
                dose=dose,
                timing=timing,
                evidence=evidence or None,
                active=active,
                contraindications=contraindications,
                note=note,
                override=override,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        else:
            await supplement_writes.add_supplement(
                db,
                name=name,
                dose=dose,
                timing=timing,
                evidence=evidence or None,
                active=active,
                contraindications=contraindications,
                note=note,
                override=override,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        await db.commit()
    except ConflictBlocked as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"violations": [v.to_dict() for v in e.violations]},
        )
    return _redirect(request)


@router.post("/{id}/toggle")
async def toggle_supplement(
    request: Request,
    id: int,
    active: bool = Form(...),
    override: bool = Form(False),
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
    try:
        await supplement_writes.set_active(
            db,
            id,
            active,
            override=override,
            identity=conflict_context.identity,
            prepared_conflict_write=prepared,
        )
        await db.commit()
    except ConflictBlocked as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"violations": [v.to_dict() for v in e.violations]},
        )
    return _redirect(request)


@router.post("/{id}/delete")
async def delete_supplement(
    request: Request,
    id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    await supplement_writes.delete_supplement(
        db,
        id,
        identity=ownership.owner_action(),
    )
    await db.commit()
    return _redirect(request)
