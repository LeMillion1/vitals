"""The patient's door into their own care-team conversations.

There is one set of screens, under ``/care/{subject_id}/messages``, and the
patient reaches the same ones a professional does — because they are in the same
rooms, seeing the same words. That is not a shortcut: it is the feature. A
separate patient-facing view of a clinical conversation would be a place for the
two to drift apart, and the whole argument for a patient-visible thread is that
they cannot.

What the patient does not have is a subject id to type. A professional gets one
from their roster; the patient's record is *whoever they are*, so this resolves
it from the session and sends them on. Which is the same rule the rest of their
own pages follow: the subject comes from whichever source cannot go stale.

An account with no record of its own lands on the refusal every personal page
gives them, and is redirected to their roster if they hold one. Their door into
a conversation is the patient it is about, and that is ``/care``.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.care import threads as care_threads
from vitals.services.authorization.subject_access import (
    AccessResolutionError,
    resolve_access_context,
)
from vitals.services.tenancy.contracts import NoPersonalRecordError
from web.care_context import principal_user_id
from web.deps import get_session, require_auth

router = APIRouter(tags=["messages"])


@router.get("/messages")
async def my_conversations(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
) -> RedirectResponse:
    """Send the patient to the conversations about their own record."""

    del username
    user_id = await principal_user_id(request, db)
    try:
        access = await resolve_access_context(db, user_id=user_id, subject_id=None)
    except AccessResolutionError as exc:
        raise NoPersonalRecordError(
            "this account keeps no health record of its own"
        ) from exc
    return RedirectResponse(
        url=f"/care/{access.subject_id}/messages",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/messages/relationship/{relationship_id}")
async def open_relationship_conversation(
    relationship_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
) -> RedirectResponse:
    """Open the patient's stable conversation with one chosen professional."""

    del username
    user_id = await principal_user_id(request, db)
    try:
        access = await resolve_access_context(db, user_id=user_id, subject_id=None)
        thread = await care_threads.open_relationship_thread(
            db,
            context=access,
            relationship_id=relationship_id,
        )
    except (AccessResolutionError, care_threads.CareThreadError):
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    await db.commit()
    return RedirectResponse(
        url=f"/care/{access.subject_id}/messages/{thread.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
