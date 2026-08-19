"""Signals — the free-text capture feed, its key frequencies, and manual cleanup.

Deliberately read-and-delete only: signals are *captured* through the bot, not
typed here (the plan's product boundary — the bot is not a second input form, and
the app is not a second bot). What this page is for is seeing what the parser
actually recorded, and fixing it when it got a row wrong.

The key-frequency table is the raw material for consolidation: it is what a closed key
registry gets built from, which is why it counts misparsed rows too.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import SignalKind
from vitals.services import signals_service
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from web.deps import get_session, require_auth
from web.templating import templates

router = APIRouter(prefix="/signals", tags=["signals"])

# One screen of history. A month of one person's messages is a few hundred rows,
# so this is "everything" in practice — paging can wait until it isn't.
FEED_LIMIT = 200


@router.get("", response_class=HTMLResponse)
async def signals_feed(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    signals = await signals_service.list_signals(
        db,
        include_misparse=True,
        limit=FEED_LIMIT,
        subject_id=ownership.subject_id,
        include_legacy_unowned=True,
    )
    frequency = await signals_service.key_frequency(
        db,
        subject_id=ownership.subject_id,
        include_legacy_unowned=True,
    )

    return templates.TemplateResponse(
        request,
        "signals/index.html",
        {
            "username": username,
            "signals": signals,
            "frequency": frequency,
            "kinds": [k.value for k in SignalKind],
            "misparse_count": sum(1 for s in signals if s.misparse),
            # What the row was stored as vs what it folds to on read —
            # the alias table is invisible everywhere else.
            "normalize_key": signals_service.normalize_key,
        },
    )


@router.post("/{signal_id}/delete")
async def delete_signal_entry(
    request: Request,
    signal_id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    await signals_service.delete_signal(
        db,
        signal_id,
        subject_id=ownership.subject_id,
        include_legacy_unowned=True,
    )
    await db.commit()

    response = RedirectResponse(url="/signals", status_code=status.HTTP_303_SEE_OTHER)
    if "hx-request" in request.headers:
        response.headers["HX-Redirect"] = "/signals"
    return response
