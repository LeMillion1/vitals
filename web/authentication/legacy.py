"""Legacy password and second-factor browser routes."""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.i18n import t
from vitals.services.authentication import registration
from vitals.services.authentication import legacy_two_factor as twofa_service
from vitals.utils.passwords import verify_password, verify_password_dummy
from web.authentication.tokens import (
    clear_pending_2fa_cookie,
    create_pending_2fa,
    create_session,
    read_pending_2fa,
    read_session,
    safe_next,
    set_pending_2fa_cookie,
    set_session_cookie,
)
from web.config import PENDING_2FA_COOKIE, SESSION_COOKIE, get_web_config
from web.deps import get_redis, get_session
from web.ratelimit import login_rate_limit
from web.templating import templates

router = APIRouter()


def authenticate(username: str, password: str) -> bool:
    """Verify single-user credentials with a constant-time fallback."""

    if get_web_config().oidc_enabled:
        return False
    cfg = get_web_config()
    if username != cfg.auth_username:
        verify_password_dummy(password)
        return False
    return verify_password(password, cfg.auth_password_hash)


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
):
    if read_session(request.cookies.get(SESSION_COOKIE)) is not None:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    if get_web_config().oidc_enabled:
        next_url = safe_next(next)
        target = f"/auth/start?next={quote(next_url, safe='')}"
        mode = await registration.effective_mode(db)
        return templates.TemplateResponse(
            request,
            "oidc_entry.html",
            {
                "sign_in_url": target,
                "registration_open": mode is registration.RegistrationMode.OPEN,
            },
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Robots-Tag": "noindex, nofollow, noarchive",
            },
        )
    return templates.TemplateResponse(request, "login.html", {"next": safe_next(next)})


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: Optional[str] = Form(None),
    _rl: None = Depends(login_rate_limit(limit=10, window=300)),
    db: AsyncSession = Depends(get_session),
):
    if get_web_config().oidc_enabled:
        raise HTTPException(status_code=404)
    next_url = safe_next(next)
    if authenticate(username, password):
        if (await twofa_service.get_state(db)).enabled:
            response = RedirectResponse(
                url=f"/login/2fa?next={quote(next_url, safe='')}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
            set_pending_2fa_cookie(response, create_pending_2fa(username))
            return response

        response = RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)
        set_session_cookie(response, create_session(username))
        return response

    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": t("login.error.bad_credentials"), "next": next_url},
    )


@router.get("/login/2fa", response_class=HTMLResponse)
async def login_2fa_page(request: Request, next: Optional[str] = None):
    if get_web_config().oidc_enabled:
        raise HTTPException(status_code=404)
    if read_pending_2fa(request.cookies.get(PENDING_2FA_COOKIE)) is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request, "login_2fa.html", {"next": safe_next(next)})


@router.post("/login/2fa")
async def login_2fa(
    request: Request,
    code: str = Form(...),
    next: Optional[str] = Form(None),
    _rl: None = Depends(
        login_rate_limit(limit=10, window=300, bucket="login_2fa", per_ip=False)
    ),
    db: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
):
    if get_web_config().oidc_enabled:
        raise HTTPException(status_code=404)
    next_url = safe_next(next)
    username = read_pending_2fa(request.cookies.get(PENDING_2FA_COOKIE))
    if username is None:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    state = await twofa_service.get_state(db)
    step = twofa_service.verify_code(state.secret, code) if state.enabled else None
    if step is None or not await twofa_service.consume_step(redis, step):
        return templates.TemplateResponse(
            request,
            "login_2fa.html",
            {"error": t("login.error.bad_code"), "next": next_url},
        )

    response = RedirectResponse(url=next_url, status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, create_session(username))
    clear_pending_2fa_cookie(response)
    return response


__all__ = ["authenticate", "router"]
