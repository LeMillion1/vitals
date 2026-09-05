"""The professional's side: whose records they hold, and one of them at a time.

Every route below `/care/{subject_id}` names its patient in the path. That is
not a URL style choice — see ``web.care_context`` for why a server-side
"currently selected patient" cannot be made safe.
"""
from __future__ import annotations

import uuid
from datetime import date as date_type
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import PolicyAction, PolicyResourceType
from vitals.enums import (
    CarePlanStatus,
    FileStorageBackend,
)
from vitals.services.support_access import contracts as support_contracts
from vitals.services.support_access import export as support_export
from vitals.services.care import invitations, professionals, records, relationships
from vitals.services.care import threads as care_threads
from vitals.services.care import workspace as care_workspace
from web.care_context import CareContext, principal_user_id, require_care_context
from web.config import get_web_config
from web.deps import get_session, require_auth
from web.presenters.care import professional_roster_context
from web.templating import STATIC_DIR, templates
from web.uploads import (
    PreparedMedicalDocument,
    care_attachment_storage_ref,
    iter_verified_file,
    open_verified_file,
    prepare_medical_document,
    remove_stored_file,
    safe_medical_media_type,
    write_private_file,
)

router = APIRouter(prefix="/care", tags=["care"])

_INVITATION_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    # Do not send the bearer path; retain the origin required by POST CSRF.
    "Referrer-Policy": "strict-origin",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
}


def _invitation_page(
    request: Request, *, username: str, token: str | None = None,
    error: str | None = None, status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """Render the invitation without reflecting a refused bearer in its body."""
    return templates.TemplateResponse(
        request, "care/accept.html",
        {"token": token, "username": username, "accept_error": error},
        status_code=status_code, headers=_INVITATION_PAGE_HEADERS,
    )


async def _visible_record(db: AsyncSession, care: CareContext):
    """Compatibility seam for tests; domain projection lives in the service."""

    return await care_workspace.visible_record(
        db,
        subject_id=care.subject_id,
        subject_timezone=care.subject_timezone,
        context=care.access,
    )


async def _attach_private_document(
    db: AsyncSession,
    *,
    care: CareContext,
    message_id: uuid.UUID,
    document: PreparedMedicalDocument,
) -> str:
    """Write private bytes, then bind their metadata in this transaction."""

    storage_ref = care_attachment_storage_ref(document.extension)
    private_root = get_web_config().private_file_root
    path = await run_in_threadpool(
        write_private_file, private_root, storage_ref, document.body
    )
    try:
        await care_threads.attach_file(
            db,
            context=care.access,
            message_id=message_id,
            original_filename=document.original_filename,
            storage_ref=storage_ref,
            media_type=document.media_type,
            size_bytes=document.byte_size,
            content_sha256=document.sha256_hex,
        )
    except BaseException:
        await run_in_threadpool(
            remove_stored_file,
            storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
            storage_ref=storage_ref,
            static_dir=STATIC_DIR,
            private_root=private_root,
        )
        raise
    return path


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

    return _invitation_page(request, username=username, token=token)


@router.post("/accept/{token}")
async def accept_invitation(
    request: Request,
    token: str,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Take up an offer, which establishes care and nothing more.

    No consent is created here. Being in care and having agreed to show
    something are the patient's two separate decisions, and accepting on their
    behalf would be making the second one for them.
    """

    user_id = await principal_user_id(request, db)
    verified_email = await care_workspace.verified_email_for_user(
        db,
        user_id=user_id,
    )
    profile_verified = await professionals.is_verified(db, user_id=user_id)
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
    except (invitations.InvitationError, relationships.CareError):
        # ``accept`` flushes before relationship validation. A normal template
        # response would make get_session commit those partial writes, so undo
        # the whole attempt before rendering the one refusal boundary.
        await db.rollback()
        error = "email" if verified_email is None else (
            "profile" if not profile_verified else "generic"
        )
        return _invitation_page(
            request,
            username=username,
            error=error,
            status_code=status.HTTP_404_NOT_FOUND,
        )
    await db.commit()
    return RedirectResponse(
        url="/care?accepted=1",
        status_code=status.HTTP_303_SEE_OTHER,
        headers=_INVITATION_PAGE_HEADERS,
    )


@router.get("", response_class=HTMLResponse)
async def roster(
    request: Request,
    accepted: bool = False,
    submitted: bool = False,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Everybody this professional is currently in care for.

    Live relationships only, and each one is re-checked against its consent on
    the way in: a row here is a record the professional can actually open, so
    the list never offers a link that answers 404.
    """

    user_id = await principal_user_id(request, db)
    workspace = await care_workspace.load_professional_workspace(
        db,
        user_id=user_id,
    )
    if not workspace.professional_roles:
        # ``/care`` is the professional workspace. A record owner reaches
        # their people, consent and guidance through the patient-side hub; an
        # empty professional roster is not a meaningful version of that page.
        # A platform-only operator has a separate control-plane home and must
        # not depend on a fake patient directory merely to reach it.
        return RedirectResponse(
            url=workspace.destination_without_professional_role,
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return templates.TemplateResponse(
        request,
        "care/roster.html",
        professional_roster_context(
            workspace,
            username=username,
            accepted=accepted,
            submitted=submitted,
        ),
    )


@router.post("/profile")
async def submit_professional_profile(
    request: Request,
    display_name: str = Form(""),
    credential_reference: str = Form(""),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Submit or correct this account's professional claim.

    Kind comes only from an assigned role. No form value can turn a member into
    a doctor or let one professional relabel themselves as the other kind.
    """

    user_id = await principal_user_id(request, db)
    workspace = await care_workspace.load_professional_workspace(
        db,
        user_id=user_id,
    )
    try:
        if workspace.profile is None:
            if len(workspace.available_kinds) != 1:
                raise professionals.ProfessionalValidationError(
                    "professional onboarding requires one assigned kind"
                )
            await professionals.submit_profile(
                db,
                user_id=user_id,
                kind=workspace.available_kinds[0],
                display_name=display_name,
                credential_reference=credential_reference,
            )
        else:
            await professionals.resubmit_profile(
                db,
                user_id=user_id,
                display_name=display_name,
                credential_reference=credential_reference,
            )
    except professionals.ProfessionalValidationError:
        await db.rollback()
        workspace = await care_workspace.load_professional_workspace(
            db,
            user_id=user_id,
        )
        return templates.TemplateResponse(
            request,
            "care/roster.html",
            professional_roster_context(
                workspace,
                username=username,
                profile_error="invalid",
                display_name=display_name,
                credential_reference=credential_reference,
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    except professionals.ProfessionalConflictError:
        await db.rollback()
        workspace = await care_workspace.load_professional_workspace(
            db,
            user_id=user_id,
        )
        return templates.TemplateResponse(
            request,
            "care/roster.html",
            professional_roster_context(
                workspace,
                username=username,
                profile_error="conflict",
                display_name=display_name,
                credential_reference=credential_reference,
            ),
            status_code=status.HTTP_409_CONFLICT,
        )
    await db.commit()
    return RedirectResponse(
        url="/care?submitted=1",
        status_code=status.HTTP_303_SEE_OTHER,
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
    plans = (
        await records.list_plans(
            db,
            context=care.access,
            include_archived=care.is_owner,
            include_drafts=not care.is_owner,
        )
        if may_read_plans
        else []
    )
    author_names = await care_workspace.professional_display_names(
        db,
        user_ids={item.actor_user_id for item in (*notes, *plans)},
    )
    visible = await _visible_record(db, care)
    response = templates.TemplateResponse(
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
            "author_names": author_names,
            "record": visible.record,
            "coverage": visible.coverage,
            "period": visible.period,
            "withheld_domains": visible.withheld_domains,
            "record_restricted": visible.restricted,
            # An empty collection is not enough presentation state: it can mean
            # either "none exist" or "this grant cannot read them". Keep that
            # distinction explicit so restricted care and support projections
            # never make a false claim about the patient's record.
            "may_read_notes": may_read_notes,
            "may_read_plans": may_read_plans,
            "may_read_messages": may_read_messages,
            # A shared URL does not determine which account surface the reader
            # is using. The same dual-role account can open its own record or a
            # patient's, so navigation must follow the resolved care context.
            "active_account_nav": (
                "care_team" if care.is_owner else "professional_care"
            ),
            # Self-ownership grants broad record operations, but professional
            # notes and care plans intentionally require a live professional
            # relationship in the record service. Never advertise actions that
            # this exact context must reject after submission.
            "may_write_note": not care.is_owner
            and care.may(
                resource_key=records.NOTE_ARTIFACT,
                action=PolicyAction.CREATE,
                resource_type=PolicyResourceType.ARTIFACT,
            ),
            "may_write_plan": not care.is_owner
            and care.may(
                resource_key=records.PLAN_ARTIFACT,
                action=PolicyAction.CREATE,
                resource_type=PolicyResourceType.ARTIFACT,
            ),
            "may_update_plan": not care.is_owner
            and care.may(
                resource_key=records.PLAN_ARTIFACT,
                action=PolicyAction.UPDATE,
                resource_type=PolicyResourceType.ARTIFACT,
            ),
        },
    )
    if care.is_support:
        artifact_keys = []
        if may_read_notes:
            artifact_keys.append(records.NOTE_ARTIFACT)
        if may_read_plans:
            artifact_keys.append(records.PLAN_ARTIFACT)
        try:
            await support_export.record_record_opened(
                db,
                context=care.access,
                domain_keys=visible.loaded_domains,
                artifact_keys=artifact_keys,
            )
            # Rendering has succeeded, but the response has not left this
            # boundary. No committed audit event means no medical HTML leaves.
            await db.commit()
        except (
            support_contracts.NotASupportSession,
            support_contracts.NotAPlatformAdmin,
        ):
            await db.rollback()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    return response


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
        thread_summaries = await care_threads.list_thread_summaries(
            db, context=care.access
        )
    except care_threads.NotInTheConversation:
        thread_summaries = []
    counterpart_names = await care_workspace.professional_display_names(
        db,
        user_ids={
            item.counterpart_user_id
            for item in thread_summaries
            if item.counterpart_user_id is not None
        },
    )
    return templates.TemplateResponse(
        request,
        "care/messages.html",
        {
            "username": username,
            "care": care,
            "threads": thread_summaries,
            "open_thread": None,
            "thread_messages": [],
            "participants": [],
            "thread_counterpart_names": counterpart_names,
            "has_historical_threads": any(
                item.thread.canonical_relationship_id is None
                for item in thread_summaries
            ),
            "active_account_nav": (
                "messages" if care.is_owner else "professional_care"
            ),
        },
    )


@router.post("/{subject_id}/messages/relationship/{relationship_id}")
async def open_relationship_conversation(
    relationship_id: uuid.UUID,
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
):
    """Open this professional's stable room with the named patient.

    The subject remains in the path, so a stale roster or record tab cannot
    retarget the action. The service also binds the relationship to the exact
    caller and subject before reusing or creating anything.
    """

    try:
        opened = await care_threads.open_relationship_thread(
            db,
            context=care.access,
            relationship_id=relationship_id,
        )
    except care_threads.CareThreadError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return RedirectResponse(
        url=f"/care/{care.subject_id}/messages/{opened.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/{subject_id}/messages/{thread_id}", response_class=HTMLResponse)
async def thread(
    request: Request,
    thread_id: uuid.UUID,
    state_changed: bool = False,
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
    except (care_threads.NotInTheConversation, care_threads.ThreadNotFound):
        # Absent, not yours, and no longer yours are one answer, exactly as the
        # care context itself answers.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None

    participant_users = {
        person.user_id: person.participant.username for person in participants
    }
    message_users = {
        message.actor_user_id: message.author.username for message in thread_messages
    }
    names = await care_workspace.professional_display_names(
        db,
        user_ids=set(participant_users) | set(message_users),
    )
    names.update(
        {
            user_id: fallback
            for user_id, fallback in (participant_users | message_users).items()
            if user_id not in names
        }
    )
    # A patient has a record display name even though their account deliberately
    # has no presentation-name column. It is the right label whenever they are
    # speaking as the subject of this conversation.
    names[care.access.subject_owner_user_id] = care.subject_display_name

    # A GET changes only this reader's cursor. The service advances to the
    # latest message it actually selected, so a concurrent later send remains
    # unread rather than disappearing behind wall-clock now.
    await care_threads.mark_thread_read(
        db, context=care.access, thread_id=opened.id
    )
    await db.commit()

    return templates.TemplateResponse(
        request,
        "care/messages.html",
        {
            "username": username,
            "care": care,
            # An open conversation is one screen. The list and new-thread form
            # belong to its parent route and would otherwise sit above the chat
            # as two unrelated tasks the reader has to scroll past.
            "threads": [],
            "open_thread": opened,
            "thread_messages": thread_messages,
            "participants": participants,
            "viewer_user_id": care.access.principal.user_id,
            "conversation_names": names,
            "active_participant_names": [
                names[person.user_id]
                for person in participants
                if person.removed_at is None
            ],
            "active_account_nav": (
                "messages" if care.is_owner else "professional_care"
            ),
            "thread_state_changed": state_changed,
            "may_send": await care_threads.may_mutate_thread(
                db, context=care.access, thread=opened
            ),
        },
    )


@router.post("/{subject_id}/messages/{thread_id}")
async def say(
    request: Request,
    thread_id: uuid.UUID,
    body: str = Form(""),
    attachment: UploadFile | None = File(None),
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
):
    """Say something in one conversation about the patient named in the path."""

    document = await prepare_medical_document(attachment)
    try:
        message = await care_threads.send_message(
            db, context=care.access, thread_id=thread_id, body=body
        )
        if document is not None:
            await _attach_private_document(
                db,
                care=care,
                message_id=message.id,
                document=document,
            )
    except care_threads.CareThreadValidationError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except care_threads.CareThreadError:
        await db.rollback()
        # Consent changed, care ended, or the thread was closed between the page
        # being rendered and this arriving.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    # See the first-message path above: never risk deleting a committed medical
    # file merely because the client did not observe a clean commit response.
    await db.commit()
    return RedirectResponse(
        url=f"/care/{care.subject_id}/messages/{thread_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    "/{subject_id}/messages/{thread_id}/messages/{message_id}/revise"
)
async def revise_message(
    thread_id: uuid.UUID,
    message_id: uuid.UUID,
    body: str = Form(""),
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
):
    """Correct one message the caller authored in this exact conversation."""

    try:
        await care_threads.revise_message(
            db,
            context=care.access,
            thread_id=thread_id,
            message_id=message_id,
            body=body,
        )
    except care_threads.CareThreadValidationError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except care_threads.ThreadStateChanged:
        await db.rollback()
        return RedirectResponse(
            url=(
                f"/care/{care.subject_id}/messages/{thread_id}"
                "?state_changed=1"
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except care_threads.CareThreadError:
        await db.rollback()
        # A message in another room, another patient's record, or written by
        # somebody else is the same non-enumerating answer.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return RedirectResponse(
        url=f"/care/{care.subject_id}/messages/{thread_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{subject_id}/messages/{thread_id}/close")
async def close_conversation(
    thread_id: uuid.UUID,
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
):
    """Close an open conversation without deleting its record."""

    try:
        await care_threads.close_thread(
            db, context=care.access, thread_id=thread_id
        )
    except care_threads.ThreadStateChanged:
        await db.rollback()
        return RedirectResponse(
            url=(
                f"/care/{care.subject_id}/messages/{thread_id}"
                "?state_changed=1"
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except care_threads.CareThreadError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return RedirectResponse(
        url=f"/care/{care.subject_id}/messages/{thread_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{subject_id}/messages/{thread_id}/reopen")
async def reopen_conversation(
    thread_id: uuid.UUID,
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
):
    """Resume a closed conversation for its current authorized participant."""

    try:
        await care_threads.reopen_thread(
            db, context=care.access, thread_id=thread_id
        )
    except care_threads.ThreadStateChanged:
        await db.rollback()
        return RedirectResponse(
            url=(
                f"/care/{care.subject_id}/messages/{thread_id}"
                "?state_changed=1"
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except care_threads.CareThreadError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return RedirectResponse(
        url=f"/care/{care.subject_id}/messages/{thread_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/{subject_id}/messages/{thread_id}/attachments/{attachment_id}")
async def download_message_attachment(
    thread_id: uuid.UUID,
    attachment_id: uuid.UUID,
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Download one attachment after re-checking live thread access."""

    try:
        resolved = await care_threads.resolve_attachment_download(
            db,
            context=care.access,
            thread_id=thread_id,
            attachment_id=attachment_id,
        )
        verified = await run_in_threadpool(
            open_verified_file,
            storage_backend=resolved.file_asset.storage_backend,
            storage_ref=resolved.file_asset.storage_ref,
            static_dir=STATIC_DIR,
            private_root=get_web_config().private_file_root,
            expected_size=resolved.file_asset.byte_size,
            expected_sha256=resolved.file_asset.sha256_hex,
        )
    except (care_threads.CareThreadError, OSError, ValueError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None

    encoded_filename = quote(resolved.attachment.original_filename, safe="")
    return StreamingResponse(
        iter_verified_file(verified),
        media_type=safe_medical_media_type(resolved.file_asset.media_type),
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": (
                "attachment; filename*=utf-8''" + encoded_filename
            ),
            "Content-Length": str(verified.byte_size),
            "X-Content-Type-Options": "nosniff",
        },
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


@router.post("/{subject_id}/plan/{plan_id}/status")
async def change_plan_status(
    request: Request,
    plan_id: uuid.UUID,
    plan_status: str = Form(""),
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
):
    """Move the current professional's plan through its visible lifecycle."""

    try:
        resolved = CarePlanStatus(plan_status)
        await records.set_plan_status(
            db,
            context=care.access,
            plan_id=plan_id,
            status=resolved,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid plan status"
        ) from exc
    except records.ProfessionalRecordValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except (records.NotInLiveCare, records.NotTheAuthor):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return RedirectResponse(
        url=f"/care/{care.subject_id}", status_code=status.HTTP_303_SEE_OTHER
    )


__all__ = ["router"]
