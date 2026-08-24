"""The professional's side: whose records they hold, and one of them at a time.

Every route below `/care/{subject_id}` names its patient in the path. That is
not a URL style choice — see ``web.care_context`` for why a server-side
"currently selected patient" cannot be made safe.
"""
from __future__ import annotations

import uuid
from datetime import date as date_type

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import PolicyAction, PolicyResourceType
from vitals.enums import CareRelationshipStatus, ConsentStatus, Domain
from vitals.models.identity import HealthSubject
from vitals.models.professional import CareRelationship, ConsentGrant
from vitals.services import digest_service, modules_service
from vitals.services.care import invitations, records, relationships
from vitals.services.care import threads as care_threads
from web.care_context import CareContext, principal_user_id, require_care_context
from web.deps import get_session, require_auth
from web.templating import templates

router = APIRouter(prefix="/care", tags=["care"])


@router.get("/accept/{token}", response_class=HTMLResponse)
async def show_invitation(
    request: Request,
    token: str,
    username: str = Depends(require_auth),
):
    """Confirm before spending a one-time link.

    A GET must not consume it. Browsers, link previews and mail scanners fetch
    URLs without anybody having decided anything, and a one-time invitation
    spent by a preview is one the intended person can never use.
    """

    return templates.TemplateResponse(
        request, "care/accept.html", {"token": token, "username": username}
    )


@router.post("/accept/{token}")
async def accept_invitation(
    request: Request,
    token: str,
    db: AsyncSession = Depends(get_session),
    _username: str = Depends(require_auth),
):
    """Take up an offer, which establishes care and nothing more.

    No consent is created here. Being in care and having agreed to show
    something are the patient's two separate decisions, and accepting on their
    behalf would be making the second one for them.
    """

    user_id = await principal_user_id(request, db)
    verified_email = await _verified_email(request, db, user_id=user_id)
    try:
        invitation = await invitations.accept(
            db,
            token=token,
            accepting_user_id=user_id,
            verified_email=verified_email,
        )
        await relationships.establish_from_invitation(
            db, invitation=invitation
        )
    except invitations.InvitationError:
        # Spent, expired, revoked, wrong address, never existed. One answer.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except relationships.KindMismatch as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except relationships.CareError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return RedirectResponse(
        url="/care?accepted=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def _verified_email(
    request: Request, db: AsyncSession, *, user_id: uuid.UUID
) -> str | None:
    """The address this session has actually proved, or nothing.

    After the federated cutover the provider states it at sign-in; before it,
    the account's own column counts only once somebody has verified it. Neither
    is inferred from the other, and an unverified address is not an address —
    it is somebody asserting they own a mailbox, which is what the invitation's
    binding exists to stop.
    """

    from vitals.models.identity import User as UserModel

    row = (
        await db.execute(
            select(UserModel.normalized_email, UserModel.email_verified_at).where(
                UserModel.id == user_id
            )
        )
    ).one_or_none()
    if row is None or row.email_verified_at is None:
        return None
    return row.normalized_email


@router.get("", response_class=HTMLResponse)
async def roster(
    request: Request,
    accepted: bool = False,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
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
            "expires_at": row.expires_at,
        }
        for row in rows
    ]
    return templates.TemplateResponse(
        request,
        "care/roster.html",
        {"patients": patients, "username": username, "accepted": accepted},
    )


#: The record, section by section, and what each one needs to be shown.
#:
#: Three different keys, deliberately not collapsed into one. ``section`` is
#: where the assembled context keeps it, ``domain`` is what the consent grants,
#: and ``module`` is what the patient switched on. They coincide for most rows
#: and disagree for ``hevy``/``workouts``, and pretending otherwise would hide
#: exactly one row's worth of a real distinction: what a patient consented to
#: share is not the same question as what they use.
RECORD_SECTIONS: tuple[tuple[str, Domain, str], ...] = (
    ("weight", Domain.WEIGHT, "weight"),
    ("labs", Domain.LABS, "labs"),
    ("body_comp", Domain.BODY_COMPOSITION, "body_comp"),
    ("nutrition", Domain.NUTRITION, "nutrition"),
    ("hrt", Domain.HRT, "hrt"),
    ("glp1", Domain.GLP1, "glp1"),
    ("supplements", Domain.SUPPLEMENTS, "supplements"),
    ("skincare", Domain.SKINCARE, "skincare"),
    ("genetics", Domain.GENETICS, "genetics"),
    ("garmin", Domain.GARMIN, "garmin"),
    ("hevy", Domain.WORKOUTS, "hevy"),
)


async def _visible_record(
    db: AsyncSession, care: CareContext
) -> tuple[dict[str, dict], list[str]]:
    """The patient's record as this professional may see it, and what is missing.

    A doctor and a trainer are granted the same domains — the kind decides who
    is writing, not what may be read — so by default this is the whole record.
    The patient can narrow it, and when they have, the withheld sections are
    named rather than quietly absent: a clinician reasoning from a partial
    record needs to know it is partial, and a gap they cannot see is worse than
    one they can.

    Modules the patient has switched off are not withheld and are not named.
    Those are sections of the product they do not use, which is not about this
    professional and not theirs to be told about.
    """

    permitted = {
        module
        for _section, domain, module in RECORD_SECTIONS
        if care.may(resource_key=domain.value)
    }
    enabled = await modules_service.get_enabled_modules(
        db, subject_id=care.subject_id
    )
    context = await digest_service.assemble_context(
        db,
        subject_id=care.subject_id,
        enabled_modules={
            key: bool(value) and key in permitted
            for key, value in enabled.items()
        },
    )

    # The module gate above is a narrowing, not the boundary. ``assemble_context``
    # forces every *core* module on whatever it is handed — weight, labs and
    # garmin among them — because a report of an installation that switched off
    # its own core sections is not a thing. That is right for the report and
    # wrong as an authorization, so consent is applied here instead, by building
    # the view out of the permitted sections rather than by removing the others
    # from a whole context. A whitelist cannot be defeated by a section this
    # screen has not thought about yet.
    record = {
        section: context.get(section)
        for section, _domain, module in RECORD_SECTIONS
        if module in permitted
    }
    coverage = {
        section: (context.get("coverage") or {}).get(section)
        for section, _domain, module in RECORD_SECTIONS
        if module in permitted and (context.get("coverage") or {}).get(section)
    }
    withheld = [
        domain.value
        for _section, domain, module in RECORD_SECTIONS
        if module not in permitted and enabled.get(module, False)
    ]
    return (
        {
            "record": record,
            "coverage": coverage,
            "period": (context.get("report_meta") or {}),
        },
        withheld,
    )


@router.get("/{subject_id}", response_class=HTMLResponse)
async def patient(
    request: Request,
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """One patient's record, as this professional is allowed to see it."""

    # Asked before reading, not caught after. Both services refuse a context
    # without the artifact scope, and that refusal is correct — but a viewer
    # whose grant covers domains and not notes is an ordinary, expected reader
    # now that platform support can be one, and an expected reader must not
    # produce an exception. The page already asks ``may()`` this way for its
    # write affordances; this is the same question for its reads.
    may_read_notes = care.may(
        resource_key=records.NOTE_ARTIFACT,
        action=PolicyAction.READ,
        resource_type=PolicyResourceType.ARTIFACT,
    )
    may_read_plans = care.may(
        resource_key=records.PLAN_ARTIFACT,
        action=PolicyAction.READ,
        resource_type=PolicyResourceType.ARTIFACT,
    )
    may_read_messages = care.may(
        resource_key=care_threads.MESSAGE_OPERATION,
        action=PolicyAction.READ,
        resource_type=PolicyResourceType.OPERATION,
    )
    notes = await records.list_notes(db, context=care.access) if may_read_notes else []
    plans = await records.list_plans(db, context=care.access) if may_read_plans else []
    visible, withheld = await _visible_record(db, care)
    return templates.TemplateResponse(
        request,
        "care/patient.html",
        {
            # Without this the whole chrome vanishes — the rail, the bottom bar
            # and the sign-out button are all behind ``{% if username %}`` in
            # base.html. A doctor is redirected here from their own dashboard,
            # so these screens losing it left them with no navigation at all.
            "username": username,
            "care": care,
            "notes": notes,
            "plans": plans,
            "record": visible["record"],
            "coverage": visible["coverage"],
            "period": visible["period"],
            "withheld_domains": withheld,
            "may_read_messages": may_read_messages,
            "may_write_note": care.may(
                resource_key=records.NOTE_ARTIFACT,
                action=PolicyAction.CREATE,
                resource_type=PolicyResourceType.ARTIFACT,
            ),
            "may_write_plan": care.may(
                resource_key=records.PLAN_ARTIFACT,
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


@router.get("/{subject_id}/messages", response_class=HTMLResponse)
async def messages(
    request: Request,
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Every conversation about this patient that this professional is in.

    Not every conversation about the patient. A thread is a room somebody was
    let into, so a doctor sees the ones they were added to and not what a
    trainer was asked separately — the patient is in both.
    """

    try:
        threads = await care_threads.list_threads(db, context=care.access)
    except care_threads.NotInTheConversation:
        threads = []
    return templates.TemplateResponse(
        request,
        "care/messages.html",
        {
            "username": username,
            "care": care,
            "threads": threads,
            "open_thread": None,
            "thread_messages": [],
            "participants": [],
            "may_send": care.may(
                resource_key=care_threads.MESSAGE_OPERATION,
                action=care_threads.SEND_ACTION,
                resource_type=PolicyResourceType.OPERATION,
            ),
        },
    )


@router.get("/{subject_id}/messages/{thread_id}", response_class=HTMLResponse)
async def thread(
    request: Request,
    thread_id: uuid.UUID,
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """One conversation, in full.

    Everything said, including what was said before this reader joined. A thread
    somebody can only see the tail of is one they cannot follow.
    """

    try:
        opened, thread_messages, participants = await care_threads.read_thread(
            db, context=care.access, thread_id=thread_id
        )
        threads = await care_threads.list_threads(db, context=care.access)
    except (care_threads.NotInTheConversation, care_threads.ThreadNotFound):
        # Absent, not yours, and no longer yours are one answer, exactly as the
        # care context itself answers.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None

    return templates.TemplateResponse(
        request,
        "care/messages.html",
        {
            "username": username,
            "care": care,
            "threads": threads,
            "open_thread": opened,
            "thread_messages": thread_messages,
            "participants": participants,
            "may_send": care.may(
                resource_key=care_threads.MESSAGE_OPERATION,
                action=care_threads.SEND_ACTION,
                resource_type=PolicyResourceType.OPERATION,
            ),
        },
    )


@router.post("/{subject_id}/messages")
async def open_conversation(
    request: Request,
    title: str = Form(""),
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
):
    """Start a conversation with this patient — the one named in the path."""

    try:
        opened = await care_threads.open_thread(
            db, context=care.access, title=title
        )
    except care_threads.CareThreadValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except care_threads.CareThreadError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return RedirectResponse(
        url=f"/care/{care.subject_id}/messages/{opened.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{subject_id}/messages/{thread_id}")
async def say(
    request: Request,
    thread_id: uuid.UUID,
    body: str = Form(""),
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
):
    """Say something in one conversation about the patient named in the path."""

    try:
        await care_threads.send_message(
            db, context=care.access, thread_id=thread_id, body=body
        )
    except care_threads.CareThreadValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except care_threads.CareThreadError:
        # Consent changed, care ended, or the thread was closed between the page
        # being rendered and this arriving.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return RedirectResponse(
        url=f"/care/{care.subject_id}/messages/{thread_id}",
        status_code=status.HTTP_303_SEE_OTHER,
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
