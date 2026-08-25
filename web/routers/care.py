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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import PolicyAction, PolicyResourceType
from vitals.enums import (
    CarePlanStatus,
    FileStorageBackend,
    ProfessionalKind,
    ProfessionalVerificationStatus,
    UserRoleName,
)
from vitals.models.identity import UserRole
from vitals.models.professional import ProfessionalProfile
from vitals.services import modules_service, support_access_service
from vitals.services.care import invitations, professionals, records, relationships
from vitals.services.care import record_projection
from vitals.services.care import threads as care_threads
from web.care_context import CareContext, principal_user_id, require_care_context
from web.config import get_web_config
from web.deps import get_session, require_auth
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


async def _professional_display_names(
    db: AsyncSession, user_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Human names professionals submitted, keyed without changing identity.

    Usernames remain the safe fallback: identity lookup still uses immutable
    IDs, while this small presentation map keeps technical login handles out of
    clinical guidance and conversations whenever a profile exists.
    """

    if not user_ids:
        return {}
    return dict(
        (
            await db.execute(
                select(
                    ProfessionalProfile.user_id,
                    ProfessionalProfile.display_name,
                ).where(ProfessionalProfile.user_id.in_(user_ids))
            )
        ).all()
    )


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
    professional_roles = set(
        await db.scalars(
            select(UserRole.role).where(
                UserRole.user_id == user_id,
                UserRole.role.in_(
                    (UserRoleName.DOCTOR.value, UserRoleName.TRAINER.value)
                ),
            )
        )
    )
    profile = await db.scalar(
        select(ProfessionalProfile).where(ProfessionalProfile.user_id == user_id)
    )
    available_kinds = [
        kind
        for kind in ProfessionalKind
        if professionals.ROLE_FOR_KIND[kind].value in professional_roles
    ]
    onboarding_kind = (
        ProfessionalKind(profile.kind)
        if profile is not None
        else available_kinds[0]
        if len(available_kinds) == 1
        else None
    )
    profile_verified = (
        profile is not None
        and profile.verification_status
        == ProfessionalVerificationStatus.VERIFIED.value
    )
    patients = await relationships.list_professional_roster(
        db, professional_user_id=user_id
    )
    return templates.TemplateResponse(
        request,
        "care/roster.html",
        {
            "patients": patients,
            "username": username,
            "accepted": accepted,
            "submitted": submitted,
            "professional_profile": profile,
            "onboarding_kind": (
                onboarding_kind.value if onboarding_kind is not None else None
            ),
            "profile_verified": profile_verified,
            "is_professional_account": bool(professional_roles),
        },
    )


@router.post("/profile")
async def submit_professional_profile(
    request: Request,
    display_name: str = Form(""),
    credential_reference: str = Form(""),
    db: AsyncSession = Depends(get_session),
    _username: str = Depends(require_auth),
):
    """Submit or correct this account's professional claim.

    Kind comes only from an assigned role. No form value can turn a member into
    a doctor or let one professional relabel themselves as the other kind.
    """

    user_id = await principal_user_id(request, db)
    roles = set(
        await db.scalars(
            select(UserRole.role).where(
                UserRole.user_id == user_id,
                UserRole.role.in_(
                    (UserRoleName.DOCTOR.value, UserRoleName.TRAINER.value)
                ),
            )
        )
    )
    profile = await db.scalar(
        select(ProfessionalProfile).where(ProfessionalProfile.user_id == user_id)
    )
    try:
        if profile is None:
            kinds = [
                kind
                for kind in ProfessionalKind
                if professionals.ROLE_FOR_KIND[kind].value in roles
            ]
            if len(kinds) != 1:
                raise professionals.ProfessionalValidationError(
                    "professional onboarding requires one assigned kind"
                )
            await professionals.submit_profile(
                db,
                user_id=user_id,
                kind=kinds[0],
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
    except professionals.ProfessionalValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except professionals.ProfessionalConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    await db.commit()
    return RedirectResponse(
        url="/care?submitted=1",
        status_code=status.HTTP_303_SEE_OTHER,
    )


async def _visible_record(
    db: AsyncSession, care: CareContext
) -> record_projection.RecordProjection:
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

    enabled = await modules_service.get_enabled_modules(
        db, subject_id=care.subject_id
    )
    return await record_projection.assemble_record_projection(
        db,
        context=care.access,
        enabled_modules=enabled,
        subject_timezone_name=care.subject_timezone,
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
    author_names = await _professional_display_names(
        db,
        {item.actor_user_id for item in (*notes, *plans)},
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
            "may_update_plan": care.may(
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
            await support_access_service.record_record_opened(
                db,
                context=care.access,
                domain_keys=visible.loaded_domains,
                artifact_keys=artifact_keys,
            )
            # Rendering has succeeded, but the response has not left this
            # boundary. No committed audit event means no medical HTML leaves.
            await db.commit()
        except (
            support_access_service.NotASupportSession,
            support_access_service.NotAPlatformAdmin,
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
    names = await _professional_display_names(
        db, set(participant_users) | set(message_users)
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
    body: str = Form(""),
    attachment: UploadFile | None = File(None),
    care: CareContext = Depends(require_care_context),
    db: AsyncSession = Depends(get_session),
):
    """Start a conversation with this patient — the one named in the path."""

    document = await prepare_medical_document(attachment)
    if document is not None and not body.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An attachment needs a message",
        )
    try:
        opened = await care_threads.open_thread(
            db, context=care.access, title=title
        )
        if body.strip():
            message = await care_threads.send_message(
                db,
                context=care.access,
                thread_id=opened.id,
                body=body,
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    # If commit's outcome is indeterminate, retain the private bytes. An orphan
    # in an inaccessible volume can be reconciled; deleting bytes after the DB
    # may have committed would turn a preserved clinical message into data loss.
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
