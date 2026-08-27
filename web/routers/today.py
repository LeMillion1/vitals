"""The entry point: what is going on today, before picking a domain."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services import digest_service, today_service
from vitals.services.conflicts import engine
from vitals.utils.timeutils import today_local
from web.deps import get_session, require_auth
from web.templating import templates

router = APIRouter(prefix="/today", tags=["today"])


@router.get("", response_class=HTMLResponse)
async def today_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    scope = await engine.resolve_legacy_conflict_scope(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    digest_owner = await digest_service.prepare_digest_owner(
        db,
        actor_username=username,
    )
    ctx = await today_service.build(
        db,
        enabled_modules=getattr(request.state, "enabled_modules", None),
        subject_id=scope.subject_id,
        prepared_digest_owner=digest_owner,
    )
    return templates.TemplateResponse(
        request,
        "today/index.html",
        {**ctx, "username": username, "today": today_local().isoformat()},
    )
