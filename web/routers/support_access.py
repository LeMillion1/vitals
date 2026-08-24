"""Two sides of the same grant: an administrator asks, a patient answers.

Deliberately one module and two routers, because the pair only makes sense
together. Every rule about who may do what lives in
:mod:`vitals.services.support_access_service`; these routes carry a form to it
and turn its refusals into pages.

The administrator's console reads across records and is on the named list in
``tests/test_row_level_security.py`` for it. The patient's screens resolve their
subject from *who they are* rather than from the path — the same rule
``web/routers/consents.py`` follows, for the same reason: the subject has to
come from whichever source cannot be stale.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain
from vitals.services import support_access_service as support
from vitals.services.access_resolution import (
    AccessResolutionError,
    resolve_access_context,
)
from web.care_context import principal_user_id
from web.deps import get_session, require_auth
from web.templating import templates

admin_router = APIRouter(prefix="/settings/platform/support", tags=["support"])
patient_router = APIRouter(prefix="/settings/access", tags=["support"])


def _back(url: str, marker: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"{url}?{marker}", status_code=status.HTTP_303_SEE_OTHER
    )


async def _admin_id(request: Request, db: AsyncSession) -> uuid.UUID:
    """The signed-in account, proven to be an active superadmin by the service.

    The check is not repeated here on purpose. Every entry point in the service
    makes it, and a second copy in the router is a second thing to keep in
    step — the one that gets forgotten is always the copy.
    """

    return await principal_user_id(request, db)


async def _own_subject(request: Request, db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = await principal_user_id(request, db)
    try:
        access = await resolve_access_context(db, user_id=user_id, subject_id=None)
    except AccessResolutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This account has no health record of its own.",
        ) from exc
    return user_id, access.subject_id


# ── The administrator's console ──────────────────────────────────────────────


@admin_router.get("", response_class=HTMLResponse)
async def console(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    asked: str | None = None,
    error: str | None = None,
):
    admin_user_id = await _admin_id(request, db)
    try:
        state = await support.console_for_admin(db, admin_user_id=admin_user_id)
        subjects = await support.reachable_subjects(db, admin_user_id=admin_user_id)
    except support.NotAPlatformAdmin as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator access required",
        ) from exc
    return templates.TemplateResponse(
        request,
        "settings/support_console.html",
        {
            "username": username,
            "grants": state.grants,
            "requests": state.requests,
            "subjects": subjects,
            "domains": [domain.value for domain in Domain],
            "default_hours": int(
                support.DEFAULT_GRANT_TTL.total_seconds() // 3600
            ),
            "max_hours": int(support.MAX_GRANT_TTL.total_seconds() // 3600),
            "asked": asked,
            "error": error,
        },
    )


@admin_router.post("/request")
async def ask_for_access(
    request: Request,
    subject_id: uuid.UUID = Form(...),
    reason: str = Form(...),
    hours: int = Form(...),
    domains: list[str] = Form(default=[]),
    ticket_reference: str = Form(default=""),
    _username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    admin_user_id = await _admin_id(request, db)
    try:
        chosen = [Domain(value) for value in domains]
    except ValueError:
        return _back("/settings/platform/support", "error=domain")
    try:
        await support.open_request(
            db,
            admin_user_id=admin_user_id,
            subject_id=subject_id,
            reason=reason,
            scopes=support.read_scopes_for(chosen),
            ttl=timedelta(hours=hours),
            ticket_reference=ticket_reference,
        )
    except support.NotAPlatformAdmin as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator access required",
        ) from exc
    except support.SupportAccessError:
        await db.rollback()
        return _back("/settings/platform/support", "error=refused")
    await db.commit()
    return _back("/settings/platform/support", "asked=1")


@admin_router.post("/{request_id}/withdraw")
async def withdraw(
    request: Request,
    request_id: uuid.UUID,
    _username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    admin_user_id = await _admin_id(request, db)
    try:
        await support.withdraw_request(
            db, admin_user_id=admin_user_id, request_id=request_id
        )
    except support.SupportAccessError:
        await db.rollback()
        return _back("/settings/platform/support", "error=refused")
    await db.commit()
    return _back("/settings/platform/support", "asked=withdrawn")


@admin_router.post("/grant/{grant_id}/revoke")
async def hand_it_back(
    request: Request,
    grant_id: uuid.UUID,
    _username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    """The admin putting the access down rather than waiting for it to lapse."""

    admin_user_id = await _admin_id(request, db)
    try:
        await support.revoke_grant(
            db,
            actor_user_id=admin_user_id,
            grant_id=grant_id,
            reason="Handed back by the administrator who held it.",
        )
    except support.SupportAccessError:
        await db.rollback()
        return _back("/settings/platform/support", "error=refused")
    await db.commit()
    return _back("/settings/platform/support", "asked=handed-back")


# ── The patient's side ───────────────────────────────────────────────────────


@patient_router.get("", response_class=HTMLResponse)
async def access_history(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    decided: str | None = None,
    error: str | None = None,
):
    """Every ask ever made about this record, and whatever is live right now.

    Reachable whether or not anything has ever been asked. A page that only
    exists once support has been in is a page nobody knows to look for, and
    "has anybody been reading my record" is a question a patient is entitled to
    ask on a quiet day and get *no* for.
    """

    _user_id, subject_id = await _own_subject(request, db)
    history = await support.list_for_subject(db, subject_id=subject_id)
    context = await resolve_access_context(db, user_id=_user_id, subject_id=None)
    live = await support.live_grant_for(db, context=context)
    return templates.TemplateResponse(
        request,
        "settings/access_history.html",
        {
            "username": username,
            "history": history,
            "live": live,
            "decided": decided,
            "error": error,
        },
    )


@patient_router.post("/{request_id}/approve")
async def approve(
    request: Request,
    request_id: uuid.UUID,
    _username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    user_id, _subject_id = await _own_subject(request, db)
    try:
        await support.approve_request(
            db, owner_user_id=user_id, request_id=request_id
        )
    except support.SupportAccessError:
        await db.rollback()
        return _back("/settings/access", "error=refused")
    await db.commit()
    return _back("/settings/access", "decided=approved")


@patient_router.post("/{request_id}/decline")
async def decline(
    request: Request,
    request_id: uuid.UUID,
    _username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    user_id, _subject_id = await _own_subject(request, db)
    try:
        await support.decline_request(
            db, owner_user_id=user_id, request_id=request_id
        )
    except support.SupportAccessError:
        await db.rollback()
        return _back("/settings/access", "error=refused")
    await db.commit()
    return _back("/settings/access", "decided=declined")


@patient_router.post("/grant/{grant_id}/revoke")
async def take_it_back(
    request: Request,
    grant_id: uuid.UUID,
    _username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
):
    """Changing your mind, without having to find anybody to ask."""

    user_id, _subject_id = await _own_subject(request, db)
    try:
        await support.revoke_grant(
            db,
            actor_user_id=user_id,
            grant_id=grant_id,
            reason="Withdrawn by the person whose record it is.",
        )
    except support.SupportAccessError:
        await db.rollback()
        return _back("/settings/access", "error=refused")
    await db.commit()
    return _back("/settings/access", "decided=revoked")
