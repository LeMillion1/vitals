"""The phone's "More" screen — everything the five-slot bottom bar can't hold.

A real page with a real URL, not a bottom sheet: the system Back button works,
it can be linked to, and it never floats over the content it came from. The
section list itself is derived (``modules_service.more_rubrics``) so nothing can
fall out of both the bar and this screen.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services import conflict_engine, labs_service
from vitals.services.modules_service import MODULE_REGISTRY
from vitals.utils.timeutils import today_local
from web.deps import get_session, require_auth
from web.templating import templates

router = APIRouter(prefix="/more", tags=["more"])


@router.get("", response_class=HTMLResponse)
async def more_screen(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    enabled = getattr(request.state, "enabled_modules", None) or {}

    # Labs is the one section whose row carries a warning value ("3 out of
    # range"): the count is what makes it worth opening. Computed here, on this
    # page only — it reads every marker's latest result, far too heavy for the
    # rail that renders on every request.
    out_of_range = 0
    if enabled.get("labs", True):
        scope = await conflict_engine.resolve_legacy_conflict_scope(
            db,
            actor_username=username,
            evaluation_date=today_local(),
        )
        latest = await labs_service.latest_per_marker(
            db,
            subject_id=scope.subject_id,
            include_legacy_unowned=scope.include_legacy_unowned,
        )
        out_of_range = sum(1 for r in latest if labs_service.is_out_of_range(r.flag))

    return templates.TemplateResponse(
        request,
        "more.html",
        {
            "username": username,
            "labs_out_of_range": out_of_range,
            "modules_on": sum(1 for k, v in enabled.items() if v),
            "modules_total": len(MODULE_REGISTRY),
        },
    )
