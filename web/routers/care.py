"""The professional's side: whose records they hold, and one of them at a time.

Every route below `/care/{subject_id}` names its patient in the path. That is
not a URL style choice — see ``web.care_context`` for why a server-side
"currently selected patient" cannot be made safe.
"""
from __future__ import annotations

from datetime import date as date_type

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import PolicyAction, PolicyResourceType
from vitals.enums import CareRelationshipStatus, ConsentStatus
from vitals.models.identity import HealthSubject
from vitals.models.professional import CareRelationship, ConsentGrant
from vitals.services import professional_record_service as records
from web.care_context import CareContext, principal_user_id, require_care_context
from web.deps import get_session, require_auth
from web.templating import templates

router = APIRouter(prefix="/care", tags=["care"])


@router.get("", response_class=HTMLResponse)
async def roster(
    request: Request,
    db: AsyncSession = Depends(get_session),
    _username: str = Depends(require_auth),
):
    """Everybody this professional is currently in care for.

    Live relationships only, and each one is re-checked against its consent on
    the way in: a row here is a record the professional can actually open, so
    the list never offers a link that answers 404.
    """

    user_id = await principal_user_id(request, db)
    rows = (
        await db.execute(
            select(
                CareRelationship.id,
                CareRelationship.subject_id,
                CareRelationship.kind,
                CareRelationship.status,
                HealthSubject.display_name,
                ConsentGrant.status.label("consent_status"),
                ConsentGrant.version.label("consent_version"),
                ConsentGrant.expires_at,
            )
            .join(HealthSubject, HealthSubject.id == CareRelationship.subject_id)
            .outerjoin(
                ConsentGrant,
                (ConsentGrant.relationship_id == CareRelationship.id)
                & ConsentGrant.status.in_(
                    (ConsentStatus.ACTIVE.value, ConsentStatus.PAUSED.value)
                ),
            )
            .where(
                CareRelationship.professional_user_id == user_id,
                CareRelationship.status != CareRelationshipStatus.ENDED.value,
            )
            .order_by(HealthSubject.display_name, CareRelationship.id)
        )
    ).all()

    patients = [
        {
            "subject_id": row.subject_id,
            "display_name": row.display_name,
            "kind": row.kind,
            # Open means both halves are live. Anything else is shown as its own
            # state rather than hidden: a professional whose consent was paused
            # should see that it was paused, not an empty list.
            "open": (
                row.status == CareRelationshipStatus.ACTIVE.value
                and row.consent_status == ConsentStatus.ACTIVE.value
            ),
            "relationship_status": row.status,
            "consent_status": row.consent_status,
            "consent_version": row.consent_version,
            "expires_at": row.expires_at,
        }
        for row in rows
    ]
    return templates.TemplateResponse(
        request, "care/roster.html", {"patients": patients}
    )


@router.get("/{subject_id}", response_class=HTMLResponse)
async def patient(
    request: Request,
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
):
    """One patient's record, as this professional is allowed to see it."""

    notes = await records.list_notes(db, context=care.access)
    plans = await records.list_plans(db, context=care.access)
    return templates.TemplateResponse(
        request,
        "care/patient.html",
        {
            "care": care,
            "notes": notes,
            "plans": plans,
            "may_write_note": care.may(
                resource_key=records.NOTE_ARTIFACT,
                action=PolicyAction.CREATE,
                resource_type=PolicyResourceType.ARTIFACT,
            ),
        },
    )


@router.post("/{subject_id}/note")
async def add_note(
    request: Request,
    body: str = Form(""),
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
):
    """Write a note about this patient — the one named in the path.

    A stale tab posts here with the subject it was rendered for, which is the
    whole point: the write lands on the patient the professional was looking at
    when they typed it, or it is refused because that care has since ended.
    """

    try:
        await records.write_note(db, context=care.access, body=body)
    except records.ProfessionalRecordValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except records.NotInLiveCare:
        # Consent changed between the page being rendered and this arriving.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return RedirectResponse(
        url=f"/care/{care.subject_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{subject_id}/plan")
async def add_plan(
    request: Request,
    title: str = Form(""),
    body: str = Form(""),
    effective_from: str = Form(""),
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
):
    """Draft a plan for this patient."""

    try:
        starts = date_type.fromisoformat(effective_from)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid date"
        ) from None
    try:
        await records.write_plan(
            db,
            context=care.access,
            title=title,
            body=body,
            effective_from=starts,
        )
    except records.ProfessionalRecordValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except records.NotInLiveCare:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return RedirectResponse(
        url=f"/care/{care.subject_id}", status_code=status.HTTP_303_SEE_OTHER
    )


__all__ = ["router"]
