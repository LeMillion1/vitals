"""Simple public account choice and invitation entry without bearer leakage."""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import RegistrationAccountKind
from vitals.services.authentication import admission, registration
from web.admission_handoff import (
    clear_registration_intent_cookie,
    clear_invitation_claim_cookie,
    create_registration_intent_claim,
    create_invitation_claim,
    set_registration_intent_cookie,
    set_invitation_claim_cookie,
)
from web.authentication.tokens import read_session
from web.config import SESSION_COOKIE, get_web_config
from web.deps import get_session
from web.ratelimit import login_rate_limit
from web.request_bodies import read_bounded_json_object
from web.templating import templates

MAX_EXCHANGE_BODY_BYTES = 1024
INVITATION_CSP = (
    "default-src 'none'; "
    "script-src 'nonce-{nonce}'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)

router = APIRouter(prefix="/register", tags=["registration"])


async def _public_page(
    request: Request,
    db: AsyncSession,
    *,
    error: bool = False,
    selected_kind: str = "",
) -> HTMLResponse:
    cfg = get_web_config()
    mode = await registration.effective_mode(db)
    return templates.TemplateResponse(
        request,
        "register.html",
        {
            "registration_open": (
                cfg.oidc_enabled and mode is registration.RegistrationMode.OPEN
            ),
            "account_kinds": tuple(RegistrationAccountKind),
            "selected_kind": selected_kind,
            "error": error,
        },
        headers={
            "Cache-Control": "no-store",
            # Keep same-origin form submissions attributable to this page.
            # ``no-referrer`` makes browsers send ``Origin: null`` for the POST,
            # which the global CSRF boundary correctly refuses.
            "Referrer-Policy": "same-origin",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
        },
    )


@router.get("", response_class=HTMLResponse)
async def public_registration(
    request: Request,
    db: AsyncSession = Depends(get_session),
):
    """Ask one plain product question before handing identity to ZITADEL."""

    if read_session(request.cookies.get(SESSION_COOKIE)) is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return await _public_page(request, db)


@router.post("", response_class=HTMLResponse)
async def begin_public_registration(
    request: Request,
    account_kind: str = Form(""),
    db: AsyncSession = Depends(get_session),
    _limit: None = Depends(
        login_rate_limit(
            limit=10,
            window=300,
            bucket="public_registration",
        )
    ),
):
    """Persist a one-time server intent and begin the provider login."""

    if read_session(request.cookies.get(SESSION_COOKIE)) is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    if not get_web_config().oidc_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        intent = await admission.issue_intent(db, account_kind=account_kind)
    except (admission.AdmissionError, admission.AdmissionValidationError):
        await db.rollback()
        return await _public_page(
            request,
            db,
            error=True,
            selected_kind=account_kind,
        )
    await db.commit()

    next_url = (
        "/today"
        if intent.account_kind == RegistrationAccountKind.MEMBER.value
        else "/care"
    )
    response = RedirectResponse(
        url=f"/auth/start?next={next_url}",
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store"},
    )
    clear_invitation_claim_cookie(response)
    clear_registration_intent_cookie(response)
    set_registration_intent_cookie(
        response,
        create_registration_intent_claim(intent.id),
    )
    return response


async def _json_object(request: Request) -> dict[str, Any]:
    return await read_bounded_json_object(
        request,
        max_bytes=MAX_EXCHANGE_BODY_BYTES,
    )


def _response(payload: dict[str, bool], *, status_code: int) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/invite")
async def invitation_entry(request: Request):
    if not get_web_config().oidc_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    csp_nonce = secrets.token_urlsafe(24)
    return templates.TemplateResponse(
        request,
        "register_invite.html",
        {"csp_nonce": csp_nonce},
        headers={
            "Content-Security-Policy": INVITATION_CSP.format(nonce=csp_nonce),
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
        },
    )


@router.post("/invite/exchange")
async def exchange_invitation(
    request: Request,
    db: AsyncSession = Depends(get_session),
    _limit: None = Depends(
        login_rate_limit(
            limit=10,
            window=300,
            bucket="registration_invitation",
        )
    ),
) -> JSONResponse:
    if not get_web_config().oidc_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if request.headers.get("sec-fetch-site") != "same-origin":
        return _response({"ok": False}, status_code=403)
    origin = request.headers.get("origin")
    parsed_origin = urlsplit(origin) if origin else None
    if (
        parsed_origin is None
        or parsed_origin.scheme not in {"http", "https"}
        or parsed_origin.netloc != request.headers.get("host", "")
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        return _response({"ok": False}, status_code=403)
    body = await _json_object(request)
    if set(body) != {"token"}:
        response = _response({"ok": False}, status_code=401)
        clear_invitation_claim_cookie(response)
        return response
    try:
        invitation_id = await admission.claim_invitation(
            db,
            token=body["token"],
        )
    except admission.AdmissionRefused:
        response = _response({"ok": False}, status_code=401)
        clear_invitation_claim_cookie(response)
        return response

    response = _response({"ok": True}, status_code=200)
    # A shared health-device may still hold somebody else's local session. The
    # invitation starts a fresh identity ceremony, so possession of a valid
    # bearer ends every previous login/pending-login handle immediately — even
    # if the person later cancels at the provider.
    from web.authentication.tokens import (
        clear_oidc_handoff_cookie,
        clear_pending_2fa_cookie,
        clear_session_cookie,
    )

    clear_session_cookie(response)
    clear_pending_2fa_cookie(response)
    clear_oidc_handoff_cookie(response)
    clear_registration_intent_cookie(response)
    set_invitation_claim_cookie(
        response,
        create_invitation_claim(invitation_id),
    )
    return response


__all__ = ["router"]
