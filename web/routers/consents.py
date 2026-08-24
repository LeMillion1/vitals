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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import AccessScope, PolicyResourceType
from vitals.services.legacy_ownership import NoPersonalRecordError
from vitals.enums import (
    CareRelationshipStatus,
    ConsentStatus,
    Domain,
    ProfessionalInvitationStatus,
    ProfessionalKind,
    RECORD_SECTIONS,
)
from vitals.models.identity import User
from vitals.models.professional import (
    CareRelationship,
    ConsentGrant,
    ConsentScope,
    ProfessionalInvitation,
    ProfessionalProfile,
)
from vitals.services.care import invitations, relationships
from vitals.services.access_resolution import (
    AccessResolutionError,
    resolve_access_context,
)
from web.care_context import principal_user_id
from web.deps import get_session, require_auth
from web.templating import templates

router = APIRouter(prefix="/settings/care", tags=["consents"])


def _shared_domains(scope_rows: set[tuple[str, str, str]]) -> list[str]:
    """Collapse policy actions into the record sections a person chose.

    A consent stores read, list and search as separate authorization facts.
    Those are implementation details, not three copies of the same section on
    the patient's screen. Following ``RECORD_SECTIONS`` also gives the summary
    the same stable order as the form instead of an alphabetical accident.
    """

    granted = {
        key
        for resource_type, key, _action in scope_rows
        if resource_type == PolicyResourceType.DOMAIN.value
    }
    return [domain.value for domain in RECORD_SECTIONS if domain.value in granted]


def _selected_scopes(
    domains: list[str], *, allow_guidance: bool, allow_messages: bool
) -> frozenset[AccessScope]:
    """Translate the patient's form into exact policy vocabulary."""

    try:
        selected_domains = {Domain(value) for value in domains}
    except ValueError as exc:
        raise relationships.CareValidationError("unknown record section") from exc
    if not selected_domains.issubset(RECORD_SECTIONS):
        raise relationships.CareValidationError("unknown record section")

    scopes = {
        AccessScope(
            resource_type=PolicyResourceType.DOMAIN,
            resource_key=domain.value,
            action=action,
        )
        for domain in selected_domains
        for action in relationships.READ_ONLY_ACTIONS
    }
    if allow_guidance:
        scopes.update(
            AccessScope(
                resource_type=PolicyResourceType.ARTIFACT,
                resource_key=artifact,
                action=action,
            )
            for artifact in relationships.AUTHORED_ARTIFACTS
            for action in relationships.AUTHORED_ACTIONS
        )
    if allow_messages:
        scopes.update(
            AccessScope(
                resource_type=PolicyResourceType.OPERATION,
                resource_key=relationships.MESSAGE_OPERATION,
                action=action,
            )
            for action in relationships.MESSAGE_ACTIONS
        )
    if not scopes:
        raise relationships.CareValidationError(
            "choose at least one record section or collaboration feature"
        )
    return frozenset(scopes)


async def _own_subject(request: Request, db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
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
    return user_id, access.subject_id


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
    _user_id, subject_id = await _own_subject(request, db)

    rows = (
        await db.execute(
            select(
                CareRelationship.id,
                CareRelationship.kind,
                CareRelationship.status,
                CareRelationship.established_at,
                User.username,
                ProfessionalProfile.display_name,
                ProfessionalProfile.verification_status,
                ConsentGrant.id.label("consent_id"),
                ConsentGrant.status.label("consent_status"),
                ConsentGrant.version,
                ConsentGrant.expires_at,
            )
            .join(User, User.id == CareRelationship.professional_user_id)
            .outerjoin(
                ProfessionalProfile,
                ProfessionalProfile.user_id == CareRelationship.professional_user_id,
            )
            .outerjoin(
                ConsentGrant,
                (ConsentGrant.relationship_id == CareRelationship.id)
                & ConsentGrant.status.in_(
                    (ConsentStatus.ACTIVE.value, ConsentStatus.PAUSED.value)
                ),
            )
            .where(
                CareRelationship.subject_id == subject_id,
                CareRelationship.status != CareRelationshipStatus.ENDED.value,
            )
            .order_by(CareRelationship.established_at.desc())
        )
    ).all()

    # One query for every live consent's domains rather than one per row.
    consent_ids = [row.consent_id for row in rows if row.consent_id is not None]
    scopes: dict[uuid.UUID, set[tuple[str, str, str]]] = {}
    if consent_ids:
        for scope in await db.execute(
            select(
                ConsentScope.consent_grant_id,
                ConsentScope.resource_type,
                ConsentScope.resource_key,
                ConsentScope.action,
            ).where(ConsentScope.consent_grant_id.in_(consent_ids))
        ):
            scopes.setdefault(scope.consent_grant_id, set()).add(
                (scope.resource_type, scope.resource_key, scope.action)
            )

    professionals = [
        {
            "relationship_id": row.id,
            "kind": row.kind,
            "name": row.display_name or row.username,
            "verified": row.verification_status == "verified",
            "relationship_status": row.status,
            "consent_status": row.consent_status,
            "version": row.version,
            "expires_at": row.expires_at,
            "domains": _shared_domains(scopes.get(row.consent_id, set())),
            "guidance": any(
                resource_type == PolicyResourceType.ARTIFACT.value
                for resource_type, _key, _action in scopes.get(row.consent_id, ())
            ),
            "messages": any(
                resource_type == PolicyResourceType.OPERATION.value
                and key == relationships.MESSAGE_OPERATION
                for resource_type, key, _action in scopes.get(row.consent_id, ())
            ),
        }
        for row in rows
    ]

    pending = list(
        await db.scalars(
            select(ProfessionalInvitation)
            .where(
                ProfessionalInvitation.subject_id == subject_id,
                ProfessionalInvitation.status
                == ProfessionalInvitationStatus.PENDING.value,
            )
            .order_by(ProfessionalInvitation.created_at.desc())
        )
    )

    return templates.TemplateResponse(
        request,
        "settings/care.html",
        {
            # See the note in web/routers/care.py: base.html hides the entire
            # chrome without it.
            "username": username,
            "professionals": professionals,
            "pending": pending,
            "kinds": [kind.value for kind in ProfessionalKind],
            "shareable_domains": [domain.value for domain in RECORD_SECTIONS],
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

    user_id, subject_id = await _own_subject(request, db)
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
    user_id, _subject_id = await _own_subject(request, db)
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

    user_id, _subject_id = await _own_subject(request, db)
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

    user_id, _subject_id = await _own_subject(request, db)
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

    user_id, _subject_id = await _own_subject(request, db)
    try:
        scopes = (
            _selected_scopes(
                domains,
                allow_guidance=bool(allow_guidance),
                allow_messages=bool(allow_messages),
            )
            if custom
            else None
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

    user_id, _subject_id = await _own_subject(request, db)
    try:
        await relationships.end_relationship(
            db, relationship_id=relationship_id, actor_user_id=user_id
        )
    except relationships.CareError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return _redirect()


__all__ = ["router"]
