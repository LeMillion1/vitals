"""Endpoints for managing active system alerts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, IntegrationProvider
from vitals.services import legacy_subject_alerts
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from web.deps import get_session, require_auth

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.post("/{alert_id}/resolve")
async def resolve_alert(
    alert_id: int,
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Mark an alert resolved. Returns an empty response for HTMX swaps."""
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
        required_connections=tuple(IntegrationProvider),
    )
    await legacy_subject_alerts.resolve(
        db,
        alert_id,
        ownership=ownership,
    )
    await db.commit()

    if "hx-request" in request.headers:
        return Response(status_code=200)

    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=303)


@router.post("/{alert_id}/override")
async def override_alert(
    alert_id: int,
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Mark a block alert overridden. Returns an empty response for HTMX swaps."""
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
        required_connections=tuple(IntegrationProvider),
    )
    await legacy_subject_alerts.override(
        db,
        alert_id,
        ownership=ownership,
    )
    await db.commit()

    if "hx-request" in request.headers:
        return Response(status_code=200)

    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=303)


@router.post("/resolve-all")
async def resolve_all_alerts(
    request: Request,
    domain: Domain | None = None,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Mark all active alerts (optionally filtered by domain) resolved."""
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
        required_connections=tuple(IntegrationProvider),
    )
    await legacy_subject_alerts.resolve_all(
        db,
        ownership=ownership,
        domain=domain,
    )
    await db.commit()

    if "hx-request" in request.headers:
        return Response(status_code=200)

    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=303)
