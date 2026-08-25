"""The patient-visible care-team thread, and the four rules that make it safe.

**The subject is always in the room.** Every thread is created with its subject
as a participant, and nothing here can take them out. That is the difference
between this feature and a hidden clinical channel, and it is enforced rather
than documented: :func:`remove_participant` refuses the subject's own row.

**Being in the room is a row, and it is not enough.** A professional joins
because somebody added them, and that row records the care they joined under.
Whether they may still read or still send is asked of the policy on every
single call — so a patient who pauses a consent stops the conversation without
deleting it, and a patient who revokes one stops it permanently.

**Reading and sending are separate permissions.** The consent carries
``care_team.message`` as an operation with two actions: ``read`` for seeing the
thread and ``message`` for writing into it. A patient who wants a doctor to be
able to look back at what was said without being able to add to it can have
exactly that, which is a narrowing worth being able to express.

**Nothing is deleted.** A message is corrected in place, keeping its author and
gaining an edit time; a participant who leaves keeps their row with a
``removed_at``. Both for the reason a professional's note is never deleted: a
clinical conversation somebody can make disappear is a worse record than one
that stays, and the patient cannot review a history they cannot see.

The subject's own access needs no consent at all — ``is_allowed`` short-circuits
on self-ownership — which is what "patient-visible" means structurally rather
than as a promise.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from vitals.access import (
    AccessContext,
    AccessRequest,
    PolicyAction,
    PolicyResourceType,
    is_allowed,
)
from vitals.enums import (
    CareRelationshipStatus,
    CareThreadStatus,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
)
from vitals.models.care_thread import (
    CareMessage,
    CareMessageAttachment,
    CareThread,
    CareThreadParticipant,
)
from vitals.models.identity import HealthSubject
from vitals.models.tenancy import FileAsset
from vitals.models.professional import CareRelationship
from vitals.services import file_asset_service
from vitals.services.notifications import care_push_outbox

#: The operation key a consent carries for this feature. It matches what
#: ``relationships.default_scopes`` writes, and a mismatch would silently make
#: every message unauthorized — so both sides read this constant.
MESSAGE_OPERATION = "care_team.message"

#: Reading the thread and writing into it, separately revocable. ``MESSAGE`` had
#: been in ``PolicyAction`` and in the ``consent_scopes`` check constraint since
#: the vocabulary was laid down, with no caller; this is its first.
READ_ACTION = PolicyAction.READ
SEND_ACTION = PolicyAction.MESSAGE

_MAX_BODY = 20000
_MAX_TITLE = 200


class CareThreadError(RuntimeError):
    """Base class for care-team conversation failures."""


class CareThreadValidationError(ValueError):
    """A submitted value is not usable."""


class NotInTheConversation(CareThreadError):
    """This thread is not open to this account, for this action, right now.

    One error for "you were never in it", "you were removed", "your consent was
    paused" and "your consent does not cover sending". Told apart they map who
    is being treated by whom and how far the patient has narrowed it, which is
    the same reason the invitation refusals are uniform.
    """


class ThreadNotFound(CareThreadError):
    """No such thread in this subject's scope."""


class AttachmentNotFound(CareThreadError):
    """No downloadable attachment in this conversation and subject scope."""


class NotTheAuthor(CareThreadError):
    """Only the person who said it may correct it."""


@dataclass(frozen=True, slots=True)
class CareThreadSummary:
    """One inbox row, including reader-specific state."""

    thread: CareThread
    last_message_at: datetime | None
    unread: bool


@dataclass(frozen=True, slots=True)
class CareAttachmentDownload:
    """Private-local metadata already authorized for this one request."""

    attachment: CareMessageAttachment
    file_asset: FileAsset


def _text(value: object, field: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise CareThreadValidationError(f"{field} must be a string")
    stripped = value.strip()
    if not stripped:
        raise CareThreadValidationError(f"{field} must not be blank")
    if len(stripped) > limit:
        raise CareThreadValidationError(
            f"{field} must be at most {limit} characters"
        )
    return stripped


def _filename(value: object) -> str:
    if not isinstance(value, str):
        raise CareThreadValidationError("original_filename must be a string")
    clean = value.strip()
    if (
        not clean
        or len(clean) > 255
        or "/" in clean
        or "\\" in clean
        or any(ord(character) < 32 or ord(character) == 127 for character in clean)
    ):
        raise CareThreadValidationError("original_filename is invalid")
    return clean


async def _now(session: AsyncSession) -> datetime:
    """The wall clock, not the transaction's.

    ``now()`` in PostgreSQL is the instant the *transaction* began, so
    everything written inside one carries the same timestamp. A conversation
    then falls back to its tiebreak, which is a random UUID, and a reply can
    render above the message it answers — which is what the seeded demo showed.
    Two messages in one transaction are not exotic: an import, a seeder, or a
    reply written alongside a note all do it.

    ``clock_timestamp()`` is the real time now and advances within a
    transaction. SQLite has no equivalent and its ``CURRENT_TIMESTAMP`` is
    whole seconds, which ties just as readily, so there the process clock is
    used instead — that path is local and single-node, where it is the same
    clock anyway.
    """

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stamp = await session.scalar(select(func.clock_timestamp()))
        if stamp is not None:
            return (
                stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)
            )
    return datetime.now(timezone.utc)


def _require_scope(context: AccessContext, *, action: PolicyAction) -> None:
    """Ask the policy, on every call, rather than trust the participant row.

    The row says somebody was let in. Whether they may act today is a different
    question with a different answer, and the whole value of a patient-visible
    channel is that the patient can change the second one without losing the
    conversation.
    """

    if not is_allowed(
        context,
        AccessRequest(
            subject_id=context.subject_id,
            resource_type=PolicyResourceType.OPERATION,
            resource_key=MESSAGE_OPERATION,
            action=action,
        ),
    ):
        raise NotInTheConversation("this conversation is not open to you for that")


async def _subject_owner_id(
    session: AsyncSession, subject_id: uuid.UUID
) -> uuid.UUID:
    owner_id = await session.scalar(
        select(HealthSubject.owner_user_id).where(HealthSubject.id == subject_id)
    )
    if owner_id is None:
        raise CareThreadValidationError("health subject does not exist")
    return owner_id


async def _live_relationship_or_none(
    session: AsyncSession, *, context: AccessContext
) -> CareRelationship | None:
    """The care this person is speaking from, or ``None`` if they are the patient."""

    owner_id = await _subject_owner_id(session, context.subject_id)
    if owner_id == context.principal.user_id:
        return None
    relationship = await session.scalar(
        select(CareRelationship).where(
            CareRelationship.subject_id == context.subject_id,
            CareRelationship.professional_user_id == context.principal.user_id,
            CareRelationship.status == CareRelationshipStatus.ACTIVE.value,
        )
    )
    if relationship is None:
        raise NotInTheConversation("you are not currently in care for this record")
    return relationship


async def _thread(
    session: AsyncSession,
    *,
    context: AccessContext,
    thread_id: uuid.UUID,
    for_update: bool = False,
) -> CareThread:
    """One thread, inside this subject's scope.

    The subject is part of the ``WHERE`` rather than checked afterwards: a
    thread id from another patient matches no row, which is indistinguishable
    from one that never existed.
    """

    statement = select(CareThread).where(
        CareThread.id == thread_id,
        CareThread.subject_id == context.subject_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    thread = await session.scalar(statement)
    if thread is None:
        raise ThreadNotFound("no such conversation")
    return thread


async def _current_participation(
    session: AsyncSession, *, thread_id: uuid.UUID, user_id: uuid.UUID
) -> CareThreadParticipant | None:
    return await session.scalar(
        select(CareThreadParticipant).where(
            CareThreadParticipant.thread_id == thread_id,
            CareThreadParticipant.user_id == user_id,
            CareThreadParticipant.removed_at.is_(None),
        )
    )


async def _require_participation(
    session: AsyncSession, *, thread_id: uuid.UUID, user_id: uuid.UUID
) -> CareThreadParticipant:
    participation = await _current_participation(
        session, thread_id=thread_id, user_id=user_id
    )
    if participation is None:
        raise NotInTheConversation("you are not in this conversation")
    return participation


async def open_thread(
    session: AsyncSession, *, context: AccessContext, title: str
) -> CareThread:
    """Start a conversation about this patient, with this patient in it.

    Either the patient or a professional in live care may start one. The subject
    is added as a participant in the same flush as the thread itself, so a
    thread they are not in does not exist even briefly. Never commits.
    """

    clean_title = _text(title, "title", limit=_MAX_TITLE)
    _require_scope(context, action=SEND_ACTION)
    relationship = await _live_relationship_or_none(session, context=context)
    owner_id = await _subject_owner_id(session, context.subject_id)

    thread = CareThread(
        subject_id=context.subject_id,
        title=clean_title,
        opened_by_user_id=context.principal.user_id,
        status=CareThreadStatus.OPEN.value,
    )
    session.add(thread)
    await session.flush()

    # The patient, always and first.
    session.add(
        CareThreadParticipant(
            thread_id=thread.id,
            subject_id=context.subject_id,
            user_id=owner_id,
            relationship_id=None,
        )
    )
    if relationship is not None:
        session.add(
            CareThreadParticipant(
                thread_id=thread.id,
                subject_id=context.subject_id,
                user_id=context.principal.user_id,
                relationship_id=relationship.id,
            )
        )
    await session.flush()
    return thread


async def add_participant(
    session: AsyncSession,
    *,
    context: AccessContext,
    thread_id: uuid.UUID,
    user_id: uuid.UUID,
) -> CareThreadParticipant:
    """Let one more professional into an existing conversation.

    They need what everybody in the room needs: an active care relationship with
    this patient. Their own consent still decides what they may do once they are
    in — being added is not being authorized, and this is the one place where
    the two could be confused.

    Rejoining somebody who left reuses their row rather than adding a second, so
    "who is in the room" stays a single answer per person.
    """

    _require_scope(context, action=SEND_ACTION)
    thread = await _thread(session, context=context, thread_id=thread_id)
    if thread.status != CareThreadStatus.OPEN.value:
        raise NotInTheConversation("this conversation is closed")
    await _require_participation(
        session, thread_id=thread.id, user_id=context.principal.user_id
    )

    owner_id = await _subject_owner_id(session, context.subject_id)
    if user_id == owner_id:
        raise CareThreadValidationError(
            "the patient is already in every conversation about them"
        )

    relationship = await session.scalar(
        select(CareRelationship).where(
            CareRelationship.subject_id == context.subject_id,
            CareRelationship.professional_user_id == user_id,
            CareRelationship.status == CareRelationshipStatus.ACTIVE.value,
        )
    )
    if relationship is None:
        raise NotInTheConversation(
            "that account is not currently in care for this record"
        )

    existing = await session.scalar(
        select(CareThreadParticipant)
        .where(
            CareThreadParticipant.thread_id == thread.id,
            CareThreadParticipant.user_id == user_id,
        )
        .with_for_update()
    )
    if existing is not None:
        existing.removed_at = None
        existing.relationship_id = relationship.id
        # Rejoining opens the room from this point forward. Messages written
        # while somebody was explicitly absent stay visible history, but do not
        # arrive as newly assigned unread work.
        existing.last_read_at = await _now(session)
        await session.flush()
        return existing

    participant = CareThreadParticipant(
        thread_id=thread.id,
        subject_id=context.subject_id,
        user_id=user_id,
        relationship_id=relationship.id,
    )
    session.add(participant)
    await session.flush()
    return participant


async def remove_participant(
    session: AsyncSession,
    *,
    context: AccessContext,
    thread_id: uuid.UUID,
    user_id: uuid.UUID,
) -> CareThreadParticipant:
    """Take somebody out of the room, keeping the record that they were in it.

    The patient cannot be removed, by anybody including themselves. A thread
    about somebody that they cannot read is the thing this feature exists not to
    be, and a rule enforced here is worth more than one written down.
    """

    _require_scope(context, action=SEND_ACTION)
    thread = await _thread(session, context=context, thread_id=thread_id)
    await _require_participation(
        session, thread_id=thread.id, user_id=context.principal.user_id
    )

    owner_id = await _subject_owner_id(session, context.subject_id)
    if user_id == owner_id:
        raise CareThreadValidationError(
            "the patient cannot be removed from a conversation about them"
        )

    participation = await _require_participation(
        session, thread_id=thread.id, user_id=user_id
    )
    participation.removed_at = await _now(session)
    await session.flush()
    return participation


async def send_message(
    session: AsyncSession,
    *,
    context: AccessContext,
    thread_id: uuid.UUID,
    body: str,
) -> CareMessage:
    """Say something. Never commits."""

    clean = _text(body, "body", limit=_MAX_BODY)
    _require_scope(context, action=SEND_ACTION)
    thread = await _thread(
        session, context=context, thread_id=thread_id, for_update=True
    )
    if thread.status != CareThreadStatus.OPEN.value:
        raise NotInTheConversation("this conversation is closed")
    participation = await _require_participation(
        session, thread_id=thread.id, user_id=context.principal.user_id
    )
    # Live care is re-checked here as well as at the policy: a relationship that
    # ended leaves the consent rows behind for a moment, and "may message" must
    # not outlive "is in care".
    await _live_relationship_or_none(session, context=context)

    # Stamped here rather than left to the column default, which is ``now()``
    # and therefore the same instant for everything in one transaction. The
    # order a conversation reads in is part of what it says.
    said_at = await _now(session)
    message = CareMessage(
        thread_id=thread.id,
        subject_id=context.subject_id,
        actor_user_id=context.principal.user_id,
        body=clean,
        created_at=said_at,
    )
    session.add(message)
    # So a roster ordered by activity is ordered by what actually happened.
    thread.updated_at = said_at
    # Saying something is also proof that the author reached this point in the
    # conversation. Their own message must never create unread work for them.
    participation.last_read_at = said_at
    await session.flush()
    await care_push_outbox.enqueue_for_message(session, message=message)
    return message


async def revise_message(
    session: AsyncSession,
    *,
    context: AccessContext,
    message_id: uuid.UUID,
    body: str,
) -> CareMessage:
    """Correct what you said, keeping that you said it and that it changed."""

    clean = _text(body, "body", limit=_MAX_BODY)
    _require_scope(context, action=SEND_ACTION)

    message = await session.scalar(
        select(CareMessage)
        .where(
            CareMessage.id == message_id,
            CareMessage.subject_id == context.subject_id,
            # The author condition is in the ``WHERE``, so somebody else's
            # message is indistinguishable from one that does not exist.
            CareMessage.actor_user_id == context.principal.user_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if message is None:
        raise NotTheAuthor("no message of yours with that id in this record")

    await _require_participation(
        session, thread_id=message.thread_id, user_id=context.principal.user_id
    )
    message.body = clean
    message.edited_at = await _now(session)
    await session.flush()
    return message


async def attach_file(
    session: AsyncSession,
    *,
    context: AccessContext,
    message_id: uuid.UUID,
    original_filename: str,
    storage_ref: str,
    media_type: str,
    size_bytes: int,
    content_sha256: str,
) -> CareMessageAttachment:
    """Attach one already-written private file to the actor's message.

    This is deliberately a second flush in the same caller-owned transaction as
    :func:`send_message`. It cannot retarget somebody else's message, another
    subject's file metadata, or a conversation the actor may no longer use.
    """

    clean_filename = _filename(original_filename)
    _require_scope(context, action=SEND_ACTION)
    message = await session.scalar(
        select(CareMessage).where(
            CareMessage.id == message_id,
            CareMessage.subject_id == context.subject_id,
            CareMessage.actor_user_id == context.principal.user_id,
        )
    )
    if message is None:
        raise AttachmentNotFound("no attachable message in this record")
    thread = await _thread(
        session, context=context, thread_id=message.thread_id, for_update=True
    )
    if thread.status != CareThreadStatus.OPEN.value:
        raise NotInTheConversation("this conversation is closed")
    await _require_participation(
        session, thread_id=thread.id, user_id=context.principal.user_id
    )
    await _live_relationship_or_none(session, context=context)

    asset = await file_asset_service.register_private_local(
        session,
        subject_id=context.subject_id,
        uploaded_by_user_id=context.principal.user_id,
        purpose=FileAssetPurpose.CARE_MESSAGE_ATTACHMENT,
        storage_ref=storage_ref,
        media_type=media_type,
        size_bytes=size_bytes,
        content_sha256=content_sha256,
    )
    attachment = CareMessageAttachment(
        message_id=message.id,
        subject_id=context.subject_id,
        file_asset_id=asset.id,
        original_filename=clean_filename,
    )
    session.add(attachment)
    await session.flush()
    return attachment


async def resolve_attachment_download(
    session: AsyncSession,
    *,
    context: AccessContext,
    thread_id: uuid.UUID,
    attachment_id: uuid.UUID,
) -> CareAttachmentDownload:
    """Authorize and resolve one attachment afresh for each download request."""

    _require_scope(context, action=READ_ACTION)
    thread = await _thread(session, context=context, thread_id=thread_id)
    await _require_participation(
        session, thread_id=thread.id, user_id=context.principal.user_id
    )
    # A participant row preserves history; it is not a perpetual professional
    # grant. Ended care must close downloads just as it closes thread reads.
    await _live_relationship_or_none(session, context=context)

    row = (
        await session.execute(
            select(CareMessageAttachment, FileAsset)
            .join(
                CareMessage,
                CareMessage.id == CareMessageAttachment.message_id,
            )
            .join(FileAsset, FileAsset.id == CareMessageAttachment.file_asset_id)
            .where(
                CareMessageAttachment.id == attachment_id,
                CareMessageAttachment.subject_id == context.subject_id,
                CareMessage.thread_id == thread.id,
                FileAsset.subject_id == context.subject_id,
                FileAsset.purpose
                == FileAssetPurpose.CARE_MESSAGE_ATTACHMENT.value,
                FileAsset.storage_backend == FileStorageBackend.PRIVATE_LOCAL.value,
                FileAsset.status == FileAssetStatus.ACTIVE.value,
            )
        )
    ).one_or_none()
    if row is None:
        raise AttachmentNotFound("no attachment in this conversation")
    attachment, asset = row
    return CareAttachmentDownload(attachment=attachment, file_asset=asset)


async def list_threads(
    session: AsyncSession, *, context: AccessContext
) -> list[CareThread]:
    """Every conversation about this patient that this account is in.

    The patient sees all of them, because they are in all of them. A
    professional sees the ones they were added to — not every conversation about
    the patient, which would let one professional read what another was asked
    privately.
    """

    _require_scope(context, action=READ_ACTION)
    return list(
        await session.scalars(
            select(CareThread)
            .join(
                CareThreadParticipant,
                CareThreadParticipant.thread_id == CareThread.id,
            )
            .where(
                CareThread.subject_id == context.subject_id,
                CareThreadParticipant.user_id == context.principal.user_id,
                CareThreadParticipant.removed_at.is_(None),
            )
            .order_by(CareThread.updated_at.desc())
        )
    )


async def list_thread_summaries(
    session: AsyncSession, *, context: AccessContext
) -> list[CareThreadSummary]:
    """Reader-specific conversation rows, unread first and then most recent."""

    _require_scope(context, action=READ_ACTION)
    participation = aliased(CareThreadParticipant)
    last_message_at = (
        select(func.max(CareMessage.created_at))
        .where(CareMessage.thread_id == CareThread.id)
        .correlate(CareThread)
        .scalar_subquery()
    )
    has_unread = (
        select(CareMessage.id)
        .where(
            CareMessage.thread_id == CareThread.id,
            CareMessage.actor_user_id != context.principal.user_id,
            CareMessage.created_at > participation.last_read_at,
        )
        .correlate(CareThread, participation)
        .exists()
    )
    rows = (
        await session.execute(
            select(
                CareThread,
                last_message_at.label("last_message_at"),
                has_unread.label("unread"),
            )
            .join(participation, participation.thread_id == CareThread.id)
            .where(
                CareThread.subject_id == context.subject_id,
                participation.user_id == context.principal.user_id,
                participation.removed_at.is_(None),
            )
            .order_by(
                has_unread.desc(),
                func.coalesce(last_message_at, CareThread.updated_at).desc(),
                CareThread.id,
            )
        )
    ).all()
    return [
        CareThreadSummary(
            thread=thread,
            last_message_at=message_at,
            unread=bool(unread),
        )
        for thread, message_at, unread in rows
    ]


async def read_thread(
    session: AsyncSession, *, context: AccessContext, thread_id: uuid.UUID
) -> tuple[CareThread, list[CareMessage], list[CareThreadParticipant]]:
    """One conversation: what it is, what was said, and who was in the room.

    Everything said, including before this reader joined. A thread somebody can
    only see the tail of is a conversation they cannot follow, and the patient —
    who is in every thread from the start — is the reader this is really for.
    """

    _require_scope(context, action=READ_ACTION)
    thread = await _thread(session, context=context, thread_id=thread_id)
    await _require_participation(
        session, thread_id=thread.id, user_id=context.principal.user_id
    )

    # The author and the participant are loaded here rather than left to the
    # relationship. A template asking for ``message.author.username`` would
    # lazy-load outside the greenlet the async driver needs, which raises rather
    # than loading — so the page 500s on exactly the rows it exists to show, and
    # no service-level test sees it because none of them render.
    messages = list(
        await session.scalars(
            select(CareMessage)
            .where(CareMessage.thread_id == thread.id)
            .options(
                selectinload(CareMessage.author),
                selectinload(CareMessage.attachment).selectinload(
                    CareMessageAttachment.file_asset
                ),
            )
            .order_by(CareMessage.created_at, CareMessage.id)
        )
    )
    participants = list(
        await session.scalars(
            select(CareThreadParticipant)
            .where(CareThreadParticipant.thread_id == thread.id)
            .options(selectinload(CareThreadParticipant.participant))
            .order_by(CareThreadParticipant.joined_at)
        )
    )
    return thread, messages, participants


async def mark_thread_read(
    session: AsyncSession, *, context: AccessContext, thread_id: uuid.UUID
) -> CareThreadParticipant:
    """Advance this reader only to the latest message currently persisted.

    The cursor is a message timestamp, not wall-clock now. A concurrent message
    committed after the aggregate read therefore remains newer and unread.
    Never commits.
    """

    _require_scope(context, action=READ_ACTION)
    thread = await _thread(session, context=context, thread_id=thread_id)
    participation = await session.scalar(
        select(CareThreadParticipant)
        .where(
            CareThreadParticipant.thread_id == thread.id,
            CareThreadParticipant.user_id == context.principal.user_id,
            CareThreadParticipant.removed_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if participation is None:
        raise NotInTheConversation("you are not in this conversation")
    latest = await session.scalar(
        select(func.max(CareMessage.created_at)).where(
            CareMessage.thread_id == thread.id
        )
    )
    if latest is not None and latest > participation.last_read_at:
        participation.last_read_at = latest
        await session.flush()
    return participation


async def close_thread(
    session: AsyncSession, *, context: AccessContext, thread_id: uuid.UUID
) -> CareThread:
    """Stop the conversation without losing it.

    Closed rather than deleted, and reopenable: what was said stays readable to
    everybody who was in the room, which is the whole shape of this feature.
    """

    _require_scope(context, action=SEND_ACTION)
    thread = await _thread(
        session, context=context, thread_id=thread_id, for_update=True
    )
    await _require_participation(
        session, thread_id=thread.id, user_id=context.principal.user_id
    )
    thread.status = CareThreadStatus.CLOSED.value
    await session.flush()
    return thread


async def reopen_thread(
    session: AsyncSession, *, context: AccessContext, thread_id: uuid.UUID
) -> CareThread:
    _require_scope(context, action=SEND_ACTION)
    thread = await _thread(
        session, context=context, thread_id=thread_id, for_update=True
    )
    await _require_participation(
        session, thread_id=thread.id, user_id=context.principal.user_id
    )
    thread.status = CareThreadStatus.OPEN.value
    await session.flush()
    return thread


async def unread_marker(
    session: AsyncSession, *, context: AccessContext
) -> int:
    """How many conversations contain a newer message from somebody else."""

    _require_scope(context, action=READ_ACTION)
    participation = aliased(CareThreadParticipant)
    has_unread = (
        select(CareMessage.id)
        .where(
            CareMessage.thread_id == CareThread.id,
            CareMessage.actor_user_id != context.principal.user_id,
            CareMessage.created_at > participation.last_read_at,
        )
        .correlate(CareThread, participation)
        .exists()
    )
    total = await session.scalar(
        select(func.count())
        .select_from(CareThread)
        .join(
            participation,
            participation.thread_id == CareThread.id,
        )
        .where(
            CareThread.subject_id == context.subject_id,
            participation.user_id == context.principal.user_id,
            participation.removed_at.is_(None),
            has_unread,
        )
    )
    return int(total or 0)


__all__ = [
    "AttachmentNotFound",
    "CareAttachmentDownload",
    "CareThreadError",
    "CareThreadValidationError",
    "CareThreadSummary",
    "MESSAGE_OPERATION",
    "NotInTheConversation",
    "NotTheAuthor",
    "READ_ACTION",
    "SEND_ACTION",
    "ThreadNotFound",
    "add_participant",
    "attach_file",
    "close_thread",
    "list_threads",
    "list_thread_summaries",
    "mark_thread_read",
    "open_thread",
    "read_thread",
    "remove_participant",
    "reopen_thread",
    "resolve_attachment_download",
    "revise_message",
    "send_message",
    "unread_marker",
]
