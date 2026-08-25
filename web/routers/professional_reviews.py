"""Operator review of professional claims, separate from patient consent.

The queue contains account identity data, never health records.  Reading it is
still an operator action and every mutation requires recent authentication.
The service owns live role checks, transition locking and immutable audit; this
router only gives those narrow decisions stable browser endpoints.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import ProfessionalVerificationStatus
from vitals.services.care import professionals
from web.care_context import principal_user_id
from web.deps import get_session, require_auth, require_recent_auth
from web.templating import templates

router = APIRouter(
    prefix="/settings/platform/professionals",
    tags=["professional-review"],
)


def _back(*, decided: str | None = None, error: str | None = None) -> RedirectResponse:
    marker = f"decided={decided}" if decided else f"error={error or 'refused'}"
    return RedirectResponse(
        url=f"/settings/platform/professionals?{marker}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def _operator_id(request: Request, db: AsyncSession) -> uuid.UUID:
    return await principal_user_id(request, db)


@router.get("", response_class=HTMLResponse)
async def review_console(
    request: Request,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_session),
    decided: str | None = None,
    error: str | None = None,
):
    operator_id = await _operator_id(request, db)
    try:
        entries = await professionals.review_console(
            db, reviewer_user_id=operator_id
        )
    except professionals.NotAReviewerError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator access required",
        ) from exc

    grouped = {
        state.value: tuple(
            entry
            for entry in entries
            if entry.verification_status == state.value
        )
        for state in ProfessionalVerificationStatus
    }
    return templates.TemplateResponse(
        request,
        "settings/professional_reviews.html",
        {
            "username": username,
            "operator_user_id": operator_id,
            "profiles": grouped,
            "decided": decided,
            "error": error,
        },
    )


async def _apply_review(
    request: Request,
    db: AsyncSession,
    *,
    profile_id: uuid.UUID,
    action: str,
    note: str = "",
) -> RedirectResponse:
    operator_id = await _operator_id(request, db)
    try:
        if action == "verify":
            await professionals.verify_profile(
                db,
                profile_id=profile_id,
                reviewer_user_id=operator_id,
            )
        elif action == "reject":
            await professionals.reject_profile(
                db,
                profile_id=profile_id,
                reviewer_user_id=operator_id,
                note=note,
            )
        elif action == "suspend":
            await professionals.suspend_profile(
                db,
                profile_id=profile_id,
                reviewer_user_id=operator_id,
                note=note,
            )
        elif action == "reinstate":
            await professionals.reinstate_profile(
                db,
                profile_id=profile_id,
                reviewer_user_id=operator_id,
            )
        else:  # pragma: no cover - endpoints pass a closed vocabulary
            raise RuntimeError("unknown professional review action")
    except professionals.NotAReviewerError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator access required",
        ) from exc
    except (
        professionals.ProfessionalConflictError,
        professionals.ProfessionalNotFoundError,
        professionals.ProfessionalValidationError,
    ):
        await db.rollback()
        return _back(error="refused")
    await db.commit()
    return _back(decided=action)


@router.post("/{profile_id}/verify")
async def verify(
    request: Request,
    profile_id: uuid.UUID,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    return await _apply_review(
        request, db, profile_id=profile_id, action="verify"
    )


@router.post("/{profile_id}/reject")
async def reject(
    request: Request,
    profile_id: uuid.UUID,
    note: str = Form(""),
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    return await _apply_review(
        request,
        db,
        profile_id=profile_id,
        action="reject",
        note=note,
    )


@router.post("/{profile_id}/suspend")
async def suspend(
    request: Request,
    profile_id: uuid.UUID,
    note: str = Form(""),
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    return await _apply_review(
        request,
        db,
        profile_id=profile_id,
        action="suspend",
        note=note,
    )


@router.post("/{profile_id}/reinstate")
async def reinstate(
    request: Request,
    profile_id: uuid.UUID,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    return await _apply_review(
        request, db, profile_id=profile_id, action="reinstate"
    )


__all__ = ["router"]
