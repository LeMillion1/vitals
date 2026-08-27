"""``/share`` — the owner's screen for doctor reports.

Its own section rather than a card on ``/reports``, which holds goals and AI
digests: those are things the app says to its owner, and this is the one place
data leaves the building. That difference is worth a separate URL.

Nothing here is public. The document these pages create lives in
``web/routers/public_report.py``; the two share ``render_document`` so the page
at the link and the downloaded file are the same bytes.
"""
from __future__ import annotations

import logging
from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.share import ownership, queries, reports, snapshot
from vitals.utils.timeutils import now_local, today_local
from web.deps import get_session, require_auth
from web.ratelimit import rate_limit
from web.routers.public_report import render_document
from web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/share", tags=["share"])


async def _page(
    request: Request,
    db: AsyncSession,
    username: str,
    *,
    error: Optional[str] = None,
    created: Optional[dict] = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    enabled = getattr(request.state, "enabled_modules", None) or {}
    owner = await ownership.prepare_legacy_owner(
        db,
        actor_username=username,
    )
    report_rows = await reports.list_reports(db, prepared_owner=owner)
    # The form opens on the last report's selection. That is the personal preset,
    # without a table to store one in: the next appointment is usually the same
    # shape as the last one.
    last = report_rows[0] if report_rows else None
    default_start, default_end = queries.default_period()
    return templates.TemplateResponse(
        request,
        "share/index.html",
        {
            "username": username,
            "reports": report_rows,
            "available": snapshot.available_domains(enabled),
            # Each preset already narrowed to what this install can publish, so
            # picking one can never tick a switched-off module's box.
            "presets": {
                key: {
                    "domains": snapshot.resolve_domains(spec["domains"], enabled),
                    "labs_flagged_only": spec["labs_flagged_only"],
                }
                for key, spec in snapshot.PRESETS.items()
            },
            "period_choices": snapshot.PERIOD_CHOICES,
            "expiry_choices": snapshot.EXPIRY_CHOICES,
            "default_expiry": snapshot.DEFAULT_EXPIRY_DAYS,
            "selected": list(last.domains) if last else snapshot.available_domains(enabled),
            "flagged_only": bool(last.labs_flagged_only) if last else False,
            "default_start": default_start.isoformat(),
            "default_end": default_end.isoformat(),
            "today": today_local(),
            "now": now_local(),
            "error": error,
            "created": created,
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
async def share_page(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    return await _page(request, db, username)


@router.post("", response_class=HTMLResponse)
async def create(
    request: Request,
    title: str = Form(...),
    preset: Optional[str] = Form(None),
    domains: list[str] = Form([]),
    period: str = Form("90"),
    period_start: Optional[str] = Form(None),
    period_end: Optional[str] = Form(None),
    expires_days: int = Form(snapshot.DEFAULT_EXPIRY_DAYS),
    labs_flagged_only: bool = Form(False),
    note: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
    _rl: None = Depends(rate_limit("share_create", limit=20, window=300)),
):
    """Freeze a document and show its link and password — once.

    Deliberately no post/redirect/get: the password exists in exactly one place,
    this response body. Carrying it through a redirect would mean putting a
    credential in a URL or parking it in a store, and neither is worth it for a
    string the owner copies within five seconds.
    """
    enabled = getattr(request.state, "enabled_modules", None) or {}
    chosen = snapshot.resolve_domains(domains, enabled)
    if not chosen:
        return await _page(
            request, db, username, error="share.error.no_sections",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if period == "custom":
        try:
            start = date_type.fromisoformat(period_start or "")
            end = date_type.fromisoformat(period_end or "")
        except ValueError:
            return await _page(
                request, db, username, error="share.error.bad_period",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if end < start:
            return await _page(
                request, db, username, error="share.error.bad_period",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
    elif period == "all":
        owner = await ownership.prepare_legacy_owner(
            db,
            actor_username=username,
        )
        _, end = queries.window_for(1)
        start = (
            await queries.earliest_data_date(db, prepared_owner=owner)
            or end
        )
    else:
        try:
            days = int(period)
        except ValueError:
            days = 90
        start, end = queries.window_for(days)

    if period != "all":
        owner = await ownership.prepare_legacy_owner(
            db,
            actor_username=username,
        )

    row, password = await reports.create_report(
        db,
        title=title,
        domains=chosen,
        period_start=start,
        period_end=end,
        expires_days=expires_days,
        note=note,
        labs_flagged_only=labs_flagged_only,
        preset=preset or None,
        enabled=enabled,
        prepared_owner=owner,
    )
    if not (row.snapshot or {}).get("blocks"):
        # An empty document is worse than no document — it looks like a person
        # with no history rather than a window with nothing in it.
        await db.rollback()
        return await _page(
            request, db, username, error="share.error.empty",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    await db.commit()

    return await _page(
        request,
        db,
        username,
        created={
            "url": str(request.url.replace(path=f"/r/{row.token}", query="")),
            "password": password,
        },
    )


@router.post("/{report_id}/revoke")
async def revoke(
    report_id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    owner = await ownership.prepare_legacy_owner(
        db,
        actor_username=username,
    )
    await reports.revoke(db, report_id, prepared_owner=owner)
    await db.commit()
    return RedirectResponse(url="/share", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{report_id}/delete")
async def delete(
    report_id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    owner = await ownership.prepare_legacy_owner(
        db,
        actor_username=username,
    )
    await reports.delete_report(db, report_id, prepared_owner=owner)
    await db.commit()
    return RedirectResponse(url="/share", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{report_id}/download")
async def download(
    request: Request,
    report_id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """The same document as a file — for handing over on a stick, or printing
    somewhere with no network. Everything is already inline, so there is nothing
    to bundle."""
    owner = await ownership.prepare_legacy_owner(
        db,
        actor_username=username,
    )
    row = await reports.get_report(
        db,
        report_id,
        prepared_owner=owner,
    )
    if row is None or row.snapshot is None:
        return RedirectResponse(url="/share", status_code=status.HTTP_303_SEE_OTHER)
    return Response(
        content=render_document(request, row, download=True),
        media_type="text/html; charset=utf-8",
        headers={
            # ASCII filename on purpose: the title can be Cyrillic, and a raw
            # UTF-8 header value is what turns a download into mojibake.
            "Content-Disposition": f'attachment; filename="vitals-report-{row.id}.html"',
            "X-Robots-Tag": "noindex, nofollow",
        },
    )
