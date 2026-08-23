"""Where a professional's contribution goes, which is not into the patient's facts.

A doctor's reading of a lab panel is not the lab panel. Keeping them apart is
partly about the record — a year later the two would be indistinguishable — and
partly about permission: if a professional's thinking lived inside the patient's
measurements, a professional would need to be able to write into them.

Three rules run through everything here.

Only a professional in live care may write, which means the same pair of records
access needs everywhere else: a relationship and a consent, both live. Nothing
in this module decides that itself; the caller brings a decided
:class:`~vitals.access.AccessContext` and the policy is what said yes.

Only the author may change what they wrote. Not the patient, not another
professional, not an operator — a note somebody else can edit is not that
person's note, and the record of who thought what stops meaning anything.

And nothing is deleted. A clinical note that can disappear is a worse record
than one that stays and is superseded, and a plan that can vanish is one the
patient cannot hold anybody to. Plans are archived; notes simply accumulate.
"""

from __future__ import annotations

import uuid
from datetime import date as date_type
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import (
    AccessContext,
    AccessRequest,
    PolicyAction,
    PolicyResourceType,
    is_allowed,
)
from vitals.enums import CarePlanStatus, CareRelationshipStatus
from vitals.models.professional import CarePlan, CareRelationship, ProfessionalNote

#: The artifact keys these records are authorized under. They match the entries
#: ``care_service.default_scopes`` writes into a consent, and a mismatch would
#: silently make every write unauthorized — so both sides read this pair.
NOTE_ARTIFACT = "professional_note"
PLAN_ARTIFACT = "care_plan"

_MAX_BODY = 20000
_MAX_TITLE = 200


class ProfessionalRecordError(RuntimeError):
    """Base class for authored-record failures."""


class ProfessionalRecordValidationError(ValueError):
    """A submitted value is not usable."""


class NotInLiveCare(ProfessionalRecordError):
    """This professional is not currently in care for this patient."""


class NotTheAuthor(ProfessionalRecordError):
    """Only the person who wrote it may change it."""


class RecordNotFound(ProfessionalRecordError):
    """No such record in this subject's scope."""


def _text(value: object, field: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise ProfessionalRecordValidationError(f"{field} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ProfessionalRecordValidationError(f"{field} must not be blank")
    if len(stripped) > limit:
        raise ProfessionalRecordValidationError(
            f"{field} must be at most {limit} characters"
        )
    return stripped


async def _now(session: AsyncSession) -> datetime:
    stamp = await session.scalar(select(func.now()))
    if stamp is None:  # pragma: no cover - supported DBs always return now()
        return datetime.now(timezone.utc)
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc)


def _require_scope(
    context: AccessContext, *, artifact: str, action: PolicyAction
) -> None:
    """Ask the policy, rather than infer permission from being in care.

    Being in care is necessary and not sufficient — the patient's consent names
    exact resource/action pairs, and a patient who narrowed a consent to reading
    has not agreed to be written about.
    """

    if not is_allowed(
        context,
        AccessRequest(
            subject_id=context.subject_id,
            resource_type=PolicyResourceType.ARTIFACT,
            resource_key=artifact,
            action=action,
        ),
    ):
        raise NotInLiveCare("this record is not open to you for that")


async def _live_relationship(
    session: AsyncSession, *, context: AccessContext
) -> CareRelationship:
    """The care this contribution is written under.

    Stored on the row so it stays reviewable: a note with no relationship behind
    it is one nobody can say was authorized. Resolved here rather than passed
    in, because a caller choosing which relationship to name would be choosing
    the record's own provenance.
    """

    relationship = await session.scalar(
        select(CareRelationship).where(
            CareRelationship.subject_id == context.subject_id,
            CareRelationship.professional_user_id == context.principal.user_id,
            CareRelationship.status == CareRelationshipStatus.ACTIVE.value,
        )
    )
    if relationship is None:
        raise NotInLiveCare("you are not currently in care for this record")
    return relationship


async def write_note(
    session: AsyncSession, *, context: AccessContext, body: str
) -> ProfessionalNote:
    """Record what this professional thinks, in their own record of it."""

    _require_scope(context, artifact=NOTE_ARTIFACT, action=PolicyAction.CREATE)
    relationship = await _live_relationship(session, context=context)
    note = ProfessionalNote(
        subject_id=context.subject_id,
        relationship_id=relationship.id,
        actor_user_id=context.principal.user_id,
        body=_text(body, "body", limit=_MAX_BODY),
    )
    session.add(note)
    await session.flush()
    return note


async def revise_note(
    session: AsyncSession,
    *,
    context: AccessContext,
    note_id: uuid.UUID,
    body: str,
) -> ProfessionalNote:
    """Change a note, if it is yours.

    The author condition is in the query rather than checked after the read, so
    another professional's note is not found rather than refused — the same
    reason relationships are resolved that way.
    """

    _require_scope(context, artifact=NOTE_ARTIFACT, action=PolicyAction.UPDATE)
    note = await session.scalar(
        select(ProfessionalNote)
        .where(
            ProfessionalNote.id == note_id,
            ProfessionalNote.subject_id == context.subject_id,
            ProfessionalNote.actor_user_id == context.principal.user_id,
        )
        .with_for_update()
    )
    if note is None:
        raise NotTheAuthor("no note of yours with that id in this record")
    note.body = _text(body, "body", limit=_MAX_BODY)
    await session.flush()
    return note


async def list_notes(
    session: AsyncSession, *, context: AccessContext
) -> list[ProfessionalNote]:
    """Every professional's notes on this record, newest first.

    Not filtered to the caller's own. A patient reading their record sees what
    was written about them by everybody, and a second professional joining a
    case needs to see what the first concluded — that is what a shared record
    is for. Editing is what stays with the author.
    """

    _require_scope(context, artifact=NOTE_ARTIFACT, action=PolicyAction.LIST)
    return list(
        await session.scalars(
            select(ProfessionalNote)
            .where(ProfessionalNote.subject_id == context.subject_id)
            .order_by(ProfessionalNote.created_at.desc(), ProfessionalNote.id)
        )
    )


async def write_plan(
    session: AsyncSession,
    *,
    context: AccessContext,
    title: str,
    body: str,
    effective_from: date_type,
    effective_to: date_type | None = None,
) -> CarePlan:
    """Draft what this professional is asking the patient to do."""

    _require_scope(context, artifact=PLAN_ARTIFACT, action=PolicyAction.CREATE)
    if not isinstance(effective_from, date_type):
        raise ProfessionalRecordValidationError("effective_from must be a date")
    if effective_to is not None:
        if not isinstance(effective_to, date_type):
            raise ProfessionalRecordValidationError("effective_to must be a date")
        if effective_to < effective_from:
            raise ProfessionalRecordValidationError(
                "a plan cannot stop before it starts"
            )

    relationship = await _live_relationship(session, context=context)
    plan = CarePlan(
        subject_id=context.subject_id,
        relationship_id=relationship.id,
        actor_user_id=context.principal.user_id,
        title=_text(title, "title", limit=_MAX_TITLE),
        body=_text(body, "body", limit=_MAX_BODY),
        status=CarePlanStatus.DRAFT.value,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    session.add(plan)
    await session.flush()
    return plan


async def set_plan_status(
    session: AsyncSession,
    *,
    context: AccessContext,
    plan_id: uuid.UUID,
    status: CarePlanStatus | str,
) -> CarePlan:
    """Move a plan between drafting, being followed, and being over.

    Archived is where a plan ends. There is no delete: what somebody was told to
    do last spring is part of the record of their care, and a plan that can
    vanish is one the patient cannot hold anybody to.
    """

    _require_scope(context, artifact=PLAN_ARTIFACT, action=PolicyAction.UPDATE)
    resolved = (
        status if isinstance(status, CarePlanStatus) else CarePlanStatus(str(status))
    )
    plan = await session.scalar(
        select(CarePlan)
        .where(
            CarePlan.id == plan_id,
            CarePlan.subject_id == context.subject_id,
            CarePlan.actor_user_id == context.principal.user_id,
        )
        .with_for_update()
    )
    if plan is None:
        raise NotTheAuthor("no plan of yours with that id in this record")
    if plan.status == CarePlanStatus.ARCHIVED.value:
        raise ProfessionalRecordValidationError(
            "an archived plan is history; write a new one instead"
        )
    plan.status = resolved.value
    await session.flush()
    return plan


async def list_plans(
    session: AsyncSession, *, context: AccessContext, include_archived: bool = False
) -> list[CarePlan]:
    """What this record is being asked to do, and optionally what it once was."""

    _require_scope(context, artifact=PLAN_ARTIFACT, action=PolicyAction.LIST)
    statement = select(CarePlan).where(CarePlan.subject_id == context.subject_id)
    if not include_archived:
        statement = statement.where(
            CarePlan.status != CarePlanStatus.ARCHIVED.value
        )
    return list(
        await session.scalars(
            statement.order_by(CarePlan.effective_from.desc(), CarePlan.id)
        )
    )


__all__ = [
    "NOTE_ARTIFACT",
    "PLAN_ARTIFACT",
    "NotInLiveCare",
    "NotTheAuthor",
    "ProfessionalRecordError",
    "ProfessionalRecordValidationError",
    "RecordNotFound",
    "list_notes",
    "list_plans",
    "revise_note",
    "set_plan_status",
    "write_note",
    "write_plan",
]
