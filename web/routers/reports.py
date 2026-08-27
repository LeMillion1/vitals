"""Endpoints for module 10: goal cards (milestones) and the weekly AI digest."""
from __future__ import annotations

from vitals.services.milestones import goals as milestone_goals
from vitals.services.milestones import progress as milestone_progress

import secrets
from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import DigestKind, Domain

from vitals.services.digest import ownership as digest_ownership
from vitals.services.digest import queries as digest_queries
from vitals.services.conflicts import engine
from vitals.services.proactive import report_workflows
from vitals.services.proactive.brief import preparation as brief_preparation
from vitals.utils.timeutils import today_local
from web.deps import get_session, require_auth
from web.ratelimit import rate_limit
from web.templating import templates

router = APIRouter(prefix="/reports", tags=["reports"])

# Domains a goal can relate to (for the create form select).
GOAL_DOMAINS = [
    Domain.WEIGHT.value, Domain.BODY_COMPOSITION.value, Domain.GLP1.value, Domain.WORKOUTS.value,
    Domain.GARMIN.value, Domain.LABS.value, Domain.SKINCARE.value,
]


@router.get("", response_class=HTMLResponse)
async def reports_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Goal cards, the latest weekly digest and its history, and today's brief."""
    milestone_scope = await engine.resolve_legacy_conflict_scope(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    cards = await milestone_progress.dashboard_cards(
        db,
        subject_id=milestone_scope.subject_id,
    )
    digest_owner = await digest_ownership.prepare_digest_owner(
        db,
        actor_username=username,
    )
    latest = await digest_queries.latest_digest(db, prepared_owner=digest_owner)
    history = await digest_queries.list_digests(
        db,
        limit=12,
        prepared_owner=digest_owner,
    )
    latest_brief = await digest_queries.latest_digest(
        db,
        kind=DigestKind.DAILY_BRIEF.value,
        prepared_owner=digest_owner,
    )
    ai_availability = await brief_preparation.project_ai_availability(
        db,
        actor_username=username,
    )

    return templates.TemplateResponse(
        request,
        "reports/index.html",
        {
            "username": username,
            "cards": cards,
            "latest_digest": latest,
            "history": history,
            "latest_brief": latest_brief,
            "goal_domains": GOAL_DOMAINS,
            "llm_configured": ai_availability.available,
            "brief_ai_available": ai_availability.available,
            # No delivery channel exists until web push lands: Telegram was the
            # only one and its single env token/chat pair could not belong to
            # more than one person. The page keeps the flag so the section that
            # offers to send a brief stays honestly switched off rather than
            # disappearing without explanation.
            "channel_configured": False,
            "brief_build_token": secrets.token_urlsafe(24),
            "brief_test_token": secrets.token_urlsafe(24),
            "today": today_local().isoformat(),
            "digest": request.query_params.get("digest"),
            "brief": request.query_params.get("brief"),
        },
    )


@router.post("/milestone")
async def create_milestone(
    request: Request,
    name: str = Form(...),
    domain: str = Form(Domain.WEIGHT.value),
    target_value: Optional[float] = Form(None),
    target_unit: Optional[str] = Form(None),
    deadline: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    conflict_context = await engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    prepared = await engine.prepare_scoped_write(
        db,
        context=conflict_context,
    )
    await milestone_goals.create_milestone(
        db,
        name=name.strip(),
        domain=domain,
        target_value=target_value,
        target_unit=target_unit,
        deadline=date_type.fromisoformat(deadline) if deadline else None,
        note=note,
        identity=conflict_context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)


@router.post("/milestone/{milestone_id}/status")
async def set_milestone_status(
    request: Request,
    milestone_id: int,
    status_value: str = Form(..., alias="status"),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    conflict_context = await engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    prepared = await engine.prepare_scoped_write(
        db,
        context=conflict_context,
    )
    await milestone_goals.set_status(
        db,
        milestone_id,
        status_value,
        identity=conflict_context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)


@router.post("/milestone/{milestone_id}/delete")
async def delete_milestone(
    request: Request,
    milestone_id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    conflict_context = await engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=today_local(),
    )
    prepared = await engine.prepare_scoped_write(
        db,
        context=conflict_context,
    )
    await milestone_goals.delete_milestone(
        db,
        milestone_id,
        identity=conflict_context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)


@router.post("/digest")
async def generate_digest_now(
    request: Request,
    period_days: int = Form(7),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
    _rl: None = Depends(rate_limit("digest_generate", limit=5, window=60)),
):
    """Generate this week's digest on demand."""
    outcome = await report_workflows.generate_digest(
        db,
        actor_username=username,
        period_days=period_days,
    )
    return _redirect(request, f"?digest={outcome.value}")


@router.post("/brief")
async def build_brief_now(
    request: Request,
    request_token: str = Form(
        ...,
        min_length=22,
        max_length=96,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
    _rl: None = Depends(rate_limit("brief_build", limit=10, window=60)),
):
    """Assemble one intentionally requested brief and show it without sending."""
    outcome = await report_workflows.build_brief(
        db,
        actor_username=username,
        request_token=request_token,
        on_date=today_local(),
    )
    return _redirect(request, f"?brief={outcome.value}")


@router.post("/brief/test")
async def send_test_brief(
    request: Request,
    request_token: str = Form(
        ...,
        min_length=22,
        max_length=96,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
    _rl: None = Depends(rate_limit("brief_test", limit=5, window=300)),
):
    """One live send, to catch what only a real Telegram message shows — broken
    formatting, a message too long, a channel that isn't actually wired up."""
    outcome = await report_workflows.send_test_brief(
        db,
        actor_username=username,
        request_token=request_token,
        on_date=today_local(),
    )
    return _redirect(request, f"?brief={outcome.value}")


def _redirect(request: Request, suffix: str = "") -> RedirectResponse:
    url = f"/reports{suffix}"
    response = RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    if "hx-request" in request.headers:
        response.headers["HX-Redirect"] = url
    return response
