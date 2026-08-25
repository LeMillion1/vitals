"""Platform-operator account invitations without secret replay surfaces."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import RegistrationAccountKind
from vitals.services.authentication import admission
from vitals.services.authentication.registration import RegistrationMode
from web.care_context import principal_user_id
from web.config import get_web_config
from web.deps import get_session, require_auth, require_recent_auth
from web.templating import templates

router = APIRouter(
    prefix="/settings/platform/registration",
    tags=["registration-operator"],
)

_SECRET_CSP = (
    "default-src 'none'; "
    "script-src 'nonce-{nonce}'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


def _back(*, decided: str | None = None, error: str | None = None) -> RedirectResponse:
    marker = f"decided={decided}" if decided else f"error={error or 'refused'}"
    return RedirectResponse(
        url=f"/settings/platform/registration?{marker}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _forbidden(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Platform administrator access required",
    )


async def _operator_id(request: Request, db: AsyncSession) -> uuid.UUID:
    return await principal_user_id(request, db)


@router.get("", response_class=HTMLResponse)
async def registration_console(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    decided: str | None = None,
    error: str | None = None,
    page: int = 1,
    request_page: int = 1,
):
    operator_id = await _operator_id(request, db)
    cfg = get_web_config()
    try:
        console = await admission.registration_console(
            db,
            actor_user_id=operator_id,
            page=page,
            request_page=request_page,
            current_oidc_issuer=cfg.oidc_issuer if cfg.oidc_enabled else None,
        )
    except admission.AdmissionForbidden as exc:
        raise _forbidden(exc) from exc
    except admission.AdmissionValidationError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
    return templates.TemplateResponse(
        request,
        "settings/registration.html",
        {
            "username": username,
            "console": console,
            "oidc_enabled": cfg.oidc_enabled,
            "can_issue": (
                cfg.oidc_enabled
                and console.effective_mode is RegistrationMode.INVITE_ONLY
            ),
            "can_approve": (
                cfg.oidc_enabled
                and console.effective_mode is RegistrationMode.ADMIN_APPROVED
            ),
            "account_kinds": tuple(RegistrationAccountKind),
            "decided": decided,
            "error": error,
            "issuance_nonce": secrets.token_urlsafe(24),
        },
    )


@router.post("/invitations", response_class=HTMLResponse)
async def issue_invitation(
    request: Request,
    email: str = Form(""),
    account_kind: str = Form(""),
    request_nonce: str = Form(""),
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    operator_id = await _operator_id(request, db)
    try:
        await admission.registration_console(db, actor_user_id=operator_id)
    except admission.AdmissionForbidden as exc:
        raise _forbidden(exc) from exc
    cfg = get_web_config()
    if not cfg.oidc_enabled:
        return _back(error="unavailable")
    if (
        len(request_nonce) < 20
        or len(request_nonce) > 128
        or not request_nonce.isascii()
        or any(not (char.isalnum() or char in "-_") for char in request_nonce)
    ):
        return _back(error="replayed")
    nonce_digest = hashlib.sha256(request_nonce.encode("ascii")).hexdigest()
    try:
        issued = await admission.issue_invitation(
            db,
            actor_user_id=operator_id,
            email=email,
            account_kind=account_kind,
            issuance_request_digest=nonce_digest,
        )
    except admission.AdmissionReplayError:
        await db.rollback()
        return _back(error="replayed")
    except admission.AdmissionForbidden as exc:
        await db.rollback()
        raise _forbidden(exc) from exc
    except (
        admission.AdmissionRefused,
        admission.AdmissionStateError,
        admission.AdmissionValidationError,
    ):
        await db.rollback()
        return _back(error="refused")
    await db.commit()

    csp_nonce = secrets.token_urlsafe(24)
    link = (
        f"{cfg.public_url}/register/invite#token="
        f"{quote(issued.token, safe='')}"
    )
    return templates.TemplateResponse(
        request,
        "settings/registration_invitation_issued.html",
        {
            "invitation_link": link,
            "invitation_reference": str(issued.invitation.id),
            "csp_nonce": csp_nonce,
        },
        headers={
            "Content-Security-Policy": _SECRET_CSP.format(nonce=csp_nonce),
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
            "Cache-Control": "no-store",
        },
    )


@router.post("/invitations/{invitation_id}/revoke")
async def revoke_invitation(
    request: Request,
    invitation_id: uuid.UUID,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    operator_id = await _operator_id(request, db)
    try:
        await admission.revoke_invitation(
            db,
            invitation_id=invitation_id,
            actor_user_id=operator_id,
        )
    except admission.AdmissionForbidden as exc:
        await db.rollback()
        raise _forbidden(exc) from exc
    except admission.AdmissionError:
        await db.rollback()
        return _back(error="refused")
    await db.commit()
    return _back(decided="revoked")


@router.post("/requests/{request_id}/approve")
async def approve_registration_request(
    request: Request,
    request_id: uuid.UUID,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    operator_id = await _operator_id(request, db)
    cfg = get_web_config()
    if not cfg.oidc_enabled:
        return _back(error="request_refused")
    try:
        await admission.approve_request(
            db,
            request_id=request_id,
            reviewer_user_id=operator_id,
            expected_issuer=cfg.oidc_issuer,
        )
    except admission.AdmissionForbidden as exc:
        await db.rollback()
        raise _forbidden(exc) from exc
    except (admission.AdmissionError, admission.AdmissionValidationError):
        await db.rollback()
        return _back(error="request_refused")
    await db.commit()
    return _back(decided="approved")


@router.post("/requests/{request_id}/reject")
async def reject_registration_request(
    request: Request,
    request_id: uuid.UUID,
    reason: str = Form(""),
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    operator_id = await _operator_id(request, db)
    try:
        await admission.reject_request(
            db,
            request_id=request_id,
            reviewer_user_id=operator_id,
            reason=reason,
        )
    except admission.AdmissionForbidden as exc:
        await db.rollback()
        raise _forbidden(exc) from exc
    except (admission.AdmissionError, admission.AdmissionValidationError):
        await db.rollback()
        return _back(error="request_refused")
    await db.commit()
    return _back(decided="rejected")


__all__ = ["router"]
