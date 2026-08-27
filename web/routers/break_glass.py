"""Exact-selector web boundary for separately governed emergency access."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain
from vitals.persistence.rls import bind_session_subject
from vitals.services.modules import preferences as modules_service
from vitals.services.authorization.subject_access import (
    AccessResolutionError,
    enter_subject_scope,
    resolve_access_context,
)
from vitals.services.emergency import access as emergency
from vitals.services.emergency import projection as record_projection
from vitals.services.tenancy.contracts import NoPersonalRecordError
from web.care_context import principal_user_id
from web.deps import get_session, require_recent_auth
from web.templating import templates

admin_router = APIRouter(
    prefix="/settings/platform/break-glass", tags=["break-glass"]
)
patient_router = APIRouter(
    prefix="/settings/access/break-glass", tags=["break-glass"]
)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


def _selector(value: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(value)
        if parsed.int == 0 or str(parsed) != value.lower():
            raise ValueError
        return parsed
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


async def _own_subject(
    request: Request, db: AsyncSession
) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = await principal_user_id(request, db)
    try:
        access = await resolve_access_context(db, user_id=user_id, subject_id=None)
    except AccessResolutionError as exc:
        raise NoPersonalRecordError(
            "this account keeps no health record of its own"
        ) from exc
    await enter_subject_scope(db, access)
    return user_id, access.subject_id


@admin_router.get("", response_class=HTMLResponse)
async def console(
    request: Request,
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    error: str | None = None,
):
    actor_id = await principal_user_id(request, db)
    try:
        # A visibility check, not a broad session query. The screen deliberately
        # lists no people and no sessions: both exact UUID selectors must arrive
        # from the incident channel.
        await emergency.require_platform_admin(db, user_id=actor_id)
    except emergency.NotAPlatformAdmin as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc
    return templates.TemplateResponse(
        request,
        "settings/break_glass_console.html",
        {
            "username": username,
            "domains": sorted(domain.value for domain in emergency.ALLOWED_DOMAINS),
            "ttl_choices": sorted(emergency.ALLOWED_TTL_MINUTES),
            "error": error,
        },
    )


@admin_router.post("/initiate")
async def initiate(
    request: Request,
    subject_selector: str = Form(...),
    reason: str = Form(...),
    domains: list[str] = Form(default=[]),
    ttl_minutes: int = Form(...),
    incident_reference: str = Form(default=""),
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    try:
        subject_id = _selector(subject_selector)
        chosen = [Domain(value) for value in domains]
    except (HTTPException, ValueError):
        return _redirect("/settings/platform/break-glass?error=refused")
    holder_id = await principal_user_id(request, db)
    await bind_session_subject(db, subject_id)
    try:
        row = await emergency.initiate(
            db,
            holder_user_id=holder_id,
            subject_id=subject_id,
            reason=reason,
            domains=chosen,
            ttl_minutes=ttl_minutes,
            incident_reference=incident_reference,
        )
        await db.commit()
    except emergency.BreakGlassError:
        await db.rollback()
        return _redirect("/settings/platform/break-glass?error=refused")
    return _redirect(
        f"/settings/platform/break-glass/{subject_id}/session/{row.id}"
    )


@admin_router.post("/inspect")
async def inspect_redirect(
    subject_selector: str = Form(...),
    session_selector: str = Form(...),
    _username: str = Depends(require_recent_auth),
):
    try:
        subject_id = _selector(subject_selector)
        session_id = _selector(session_selector)
    except HTTPException:
        return _redirect("/settings/platform/break-glass?error=refused")
    return _redirect(
        f"/settings/platform/break-glass/{subject_id}/session/{session_id}"
    )


async def _admin_session(
    request: Request,
    db: AsyncSession,
    *,
    subject_selector: str,
    session_selector: str,
):
    subject_id = _selector(subject_selector)
    session_id = _selector(session_selector)
    actor_id = await principal_user_id(request, db)
    await bind_session_subject(db, subject_id)
    view = await emergency.inspect_exact(
        db,
        viewer_user_id=actor_id,
        subject_id=subject_id,
        session_id=session_id,
    )
    return actor_id, subject_id, session_id, view


@admin_router.get(
    "/{subject_selector}/session/{session_selector}",
    response_class=HTMLResponse,
)
async def session_detail(
    request: Request,
    subject_selector: str,
    session_selector: str,
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    saved: str | None = None,
    error: str | None = None,
):
    try:
        _actor, _subject, _session, view = await _admin_session(
            request,
            db,
            subject_selector=subject_selector,
            session_selector=session_selector,
        )
    except (emergency.BreakGlassError, HTTPException):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    response = templates.TemplateResponse(
        request,
        "settings/break_glass_session.html",
        {"username": username, "session": view, "saved": saved, "error": error},
    )
    response.headers["Cache-Control"] = "no-store, private"
    return response


@admin_router.post("/{subject_selector}/session/{session_selector}/approve")
async def approve(
    request: Request,
    subject_selector: str,
    session_selector: str,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    try:
        subject_id = _selector(subject_selector)
        session_id = _selector(session_selector)
        actor_id = await principal_user_id(request, db)
        await bind_session_subject(db, subject_id)
        await emergency.approve(
            db,
            approver_user_id=actor_id,
            subject_id=subject_id,
            session_id=session_id,
        )
        await db.commit()
    except (emergency.BreakGlassError, HTTPException):
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    return _redirect(
        f"/settings/platform/break-glass/{subject_id}/session/{session_id}?saved=approved"
    )


@admin_router.post("/{subject_selector}/session/{session_selector}/revoke")
async def holder_revoke(
    request: Request,
    subject_selector: str,
    session_selector: str,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    try:
        subject_id = _selector(subject_selector)
        session_id = _selector(session_selector)
        actor_id = await principal_user_id(request, db)
        await bind_session_subject(db, subject_id)
        await emergency.revoke_by_holder(
            db,
            holder_user_id=actor_id,
            subject_id=subject_id,
            session_id=session_id,
        )
        await db.commit()
    except (emergency.BreakGlassError, HTTPException):
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    return _redirect(
        f"/settings/platform/break-glass/{subject_id}/session/{session_id}?saved=released"
    )


@admin_router.get(
    "/{subject_selector}/session/{session_selector}/record",
    response_class=HTMLResponse,
)
async def record(
    request: Request,
    subject_selector: str,
    session_selector: str,
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    try:
        subject_id = _selector(subject_selector)
        session_id = _selector(session_selector)
        holder_id = await principal_user_id(request, db)
        await bind_session_subject(db, subject_id)
        authorization = await emergency.authorize_read(
            db,
            holder_user_id=holder_id,
            subject_id=subject_id,
            session_id=session_id,
        )
        enabled = await modules_service.get_enabled_modules(db, subject_id=subject_id)
        visible = await record_projection.assemble_record_projection(
            db,
            subject_id=subject_id,
            allowed_domain_keys=authorization.domain_keys,
            enabled_modules=enabled,
            subject_timezone_name=authorization.subject_timezone,
        )
        response = templates.TemplateResponse(
            request,
            "settings/break_glass_record.html",
            {
                "username": username,
                "authorization": authorization,
                "record": visible.record,
                "coverage": visible.coverage,
                "period": visible.period,
                "withheld_domains": (),
                "record_restricted": True,
                "break_glass": True,
                "care": {"is_support": False},
            },
        )
        await emergency.record_opened(
            db,
            authorization=authorization,
            loaded_domain_keys=visible.loaded_domains,
        )
        await db.commit()
    except (emergency.BreakGlassError, HTTPException, ValueError):
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    response.headers["Cache-Control"] = "no-store, private"
    return response


@patient_router.post("/{session_id}/revoke")
async def patient_revoke(
    request: Request,
    session_id: uuid.UUID,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    owner_id, subject_id = await _own_subject(request, db)
    try:
        await emergency.revoke_by_owner(
            db,
            owner_user_id=owner_id,
            subject_id=subject_id,
            session_id=session_id,
        )
        await db.commit()
    except emergency.BreakGlassError:
        await db.rollback()
        return _redirect("/settings/access?error=refused")
    return _redirect("/settings/access?decided=break-glass-revoked")


__all__ = ["admin_router", "patient_router"]
