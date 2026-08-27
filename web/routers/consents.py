"""The patient's side: who is in care for them, and what those people may see.

Everything here is the patient acting on their own record, so the subject is
resolved from *who they are* rather than from the path — they have exactly one
record and there is nothing to select. That is the opposite of the professional
routes for the opposite reason, and both are the same rule: the subject comes
from whichever source cannot be stale.

The invitation link appears once. It is not stored — only its hash is — so
there is no page that can show it again, and that is deliberate: a link an
operator could re-read out of the database is a link an operator can use.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.tenancy.contracts import NoPersonalRecordError
from vitals.enums import ProfessionalKind
from vitals.services.care import (
    consent_centre as consent_projection,
    invitations,
    relationships,
)
from vitals.services.authorization.subject_access import (
    AccessContext,
    AccessResolutionError,
    enter_subject_scope,
    resolve_access_context,
)
from web.care_context import principal_user_id
from web.deps import get_session, require_auth
from web.templating import templates

router = APIRouter(prefix="/settings/care", tags=["consents"])


async def _own_subject(
    request: Request, db: AsyncSession
) -> tuple[uuid.UUID, AccessContext]:
    """This account and the record it owns.

    ``subject_id=None`` means "the subject this principal owns" — never "the
    only subject in the database", which is the distinction that lets this page
    keep working once the installation holds more than one person.
    """

    user_id = await principal_user_id(request, db)
    try:
        access = await resolve_access_context(db, user_id=user_id, subject_id=None)
    except AccessResolutionError as exc:
        # A doctor or a trainer keeps no record of their own, and this page is
        # about "who holds *mine*". A bare 404 said nothing to them — it is the
        # same shape PR-08 fixed on every other personal page, arrived at here
        # by a different route because this one resolves its subject itself.
        # The registered handler redirects somebody who holds patients to their
        # roster and tells anybody else plainly.
        raise NoPersonalRecordError(
            "this account keeps no health record of its own"
        ) from exc
    await enter_subject_scope(db, access)
    return user_id, access


def _redirect(fragment: str = "") -> RedirectResponse:
    return RedirectResponse(
        url=f"/settings/care{fragment}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("", response_class=HTMLResponse)
async def consent_centre(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Who holds this record, what they may see, and how to stop it."""

    return await _render(request, db, username=username, issued_link=None)


async def _render(
    request: Request, db: AsyncSession, *, username: str, issued_link: str | None
) -> HTMLResponse:
    _user_id, owner_access = await _own_subject(request, db)
    projection = await consent_projection.build_projection(
        db,
        owner_context=owner_access,
    )

    return templates.TemplateResponse(
        request,
        "settings/care.html",
        {
            # See the note in web/routers/care.py: base.html hides the entire
            # chrome without it.
            "username": username,
            "professionals": projection.professionals,
            "guidance": projection.guidance,
            "guidance_author_names": projection.guidance_author_names,
            "subject_id": projection.subject_id,
            "pending": projection.pending_invitations,
            "kinds": [kind.value for kind in ProfessionalKind],
            "shareable_domains": [
                domain.value for domain in projection.shareable_domains
            ],
            # Shown once, straight from the request that created it. Never
            # read back from the database, because it is not in the database.
            "issued_link": issued_link,
        },
    )


@router.post("/invite")
async def invite(
    request: Request,
    email: str = Form(""),
    kind: str = Form(""),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Offer somebody a way in. The link is shown once and never again."""

    user_id, owner_access = await _own_subject(request, db)
    subject_id = owner_access.subject_id
    try:
        result = await invitations.invite(
            db,
            subject_id=subject_id,
            actor_user_id=user_id,
            kind=kind,
            email=email,
        )
    except (invitations.InvitationValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    await db.commit()
    # Rendered straight from the POST rather than redirected with the token in
    # the query string. A URL ends up in browser history, in the access log and
    # in the next page's referrer; an invitation link is a capability, and none
    # of those are places to leave one. The body is the only copy that leaves
    # here, and there is no page that can show it again because it is not stored.
    return await _render(request, db, username=username, issued_link=result.token)


@router.post("/invitation/{invitation_id}/revoke")
async def withdraw_invitation(
    request: Request,
    invitation_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _username: str = Depends(require_auth),
):
    user_id, _owner_access = await _own_subject(request, db)
    try:
        await invitations.revoke(
            db, invitation_id=invitation_id, actor_user_id=user_id
        )
    except invitations.InvitationError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return _redirect()


@router.post("/{relationship_id}/pause")
async def pause(
    request: Request,
    relationship_id: uuid.UUID,
    resume: str = Form(""),
    db: AsyncSession = Depends(get_session),
    _username: str = Depends(require_auth),
):
    """Step back, or step forward again.

    A pause is a break — a second opinion, a holiday, a disagreement — and
    resuming must not cost a new invitation and a new consent.
    """

    user_id, _owner_access = await _own_subject(request, db)
    try:
        await relationships.set_consent_paused(
            db,
            relationship_id=relationship_id,
            actor_user_id=user_id,
            paused=not resume,
        )
    except relationships.CareError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return _redirect()


@router.post("/{relationship_id}/revoke")
async def revoke(
    request: Request,
    relationship_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _username: str = Depends(require_auth),
):
    """Withdraw permission now. Not a pause, and it does not come back."""

    user_id, _owner_access = await _own_subject(request, db)
    try:
        await relationships.revoke_consent(
            db, relationship_id=relationship_id, actor_user_id=user_id
        )
    except relationships.CareError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return _redirect()


@router.post("/{relationship_id}/grant")
async def grant(
    request: Request,
    relationship_id: uuid.UUID,
    custom: str = Form(""),
    domains: list[str] = Form(default=[]),
    allow_guidance: str = Form(""),
    allow_messages: str = Form(""),
    db: AsyncSession = Depends(get_session),
    _username: str = Depends(require_auth),
):
    """Agree to show this professional the record.

    Separate from accepting them into care, because they are separate
    decisions: somebody may be your doctor for a while before you have decided
    what they should be looking at.
    """

    user_id, owner_access = await _own_subject(request, db)
    subject_id = owner_access.subject_id
    try:
        scopes = await consent_projection.selected_scopes_for_subject(
            db,
            subject_id=subject_id,
            domains=domains,
            custom=bool(custom),
            allow_guidance=bool(allow_guidance),
            allow_messages=bool(allow_messages),
        )
        await relationships.grant_consent(
            db,
            relationship_id=relationship_id,
            actor_user_id=user_id,
            scopes=scopes,
        )
    except relationships.CareValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except relationships.CareError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return _redirect()


@router.post("/{relationship_id}/end")
async def end(
    request: Request,
    relationship_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    _username: str = Depends(require_auth),
):
    """End the care, and every consent under it with it."""

    user_id, _owner_access = await _own_subject(request, db)
    try:
        await relationships.end_relationship(
            db, relationship_id=relationship_id, actor_user_id=user_id
        )
    except relationships.CareError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return _redirect()


__all__ = ["router"]
