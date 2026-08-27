"""Subject-scoped manual Timeline annotation records and chart overlays."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import AnnotationKind, Domain, Source
from vitals.models.timeline import Annotation
from vitals.ownership import WriteIdentity

DOMAIN = Domain.TIMELINE.value
_TONE_BY_KIND: dict[str, str] = {
    AnnotationKind.ILLNESS.value: "warn",
    AnnotationKind.TRAVEL.value: "warn",
    AnnotationKind.PROTOCOL_CHANGE.value: "",
    AnnotationKind.LIFE_EVENT.value: "",
    AnnotationKind.NOTE.value: "",
}


def _annotation_subject_scope(subject_id: uuid.UUID):
    return Annotation.subject_id == subject_id


async def create_annotation(
    session: AsyncSession,
    *,
    title: str,
    on_date: date_type,
    end_date: Optional[date_type] = None,
    kind: str = AnnotationKind.NOTE.value,
    domain: str = DOMAIN,
    note: Optional[str] = None,
    source: str = Source.MANUAL.value,
    identity: WriteIdentity,
) -> Annotation:
    row = Annotation(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        date=on_date,
        end_date=end_date,
        domain=domain,
        source=source,
        kind=kind,
        title=title,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


async def update_annotation(
    session: AsyncSession,
    annotation_id: int,
    *,
    title: str,
    on_date: date_type,
    end_date: Optional[date_type] = None,
    kind: str,
    domain: str,
    note: Optional[str] = None,
    identity: WriteIdentity,
) -> Optional[Annotation]:
    stmt = select(Annotation).where(Annotation.id == annotation_id)
    stmt = stmt.where(_annotation_subject_scope(identity.subject_id))
    row = await session.scalar(stmt)
    if row is None:
        return None
    row.title = title
    row.date = on_date
    row.end_date = end_date
    row.kind = kind
    row.domain = domain
    row.note = note
    await session.flush()
    return row


async def get_annotation(
    session: AsyncSession,
    annotation_id: int,
    *,
    subject_id: uuid.UUID,
) -> Optional[Annotation]:
    stmt = select(Annotation).where(Annotation.id == annotation_id)
    stmt = stmt.where(_annotation_subject_scope(subject_id))
    return await session.scalar(stmt)


async def delete_annotation(
    session: AsyncSession,
    annotation_id: int,
    *,
    identity: WriteIdentity,
) -> bool:
    stmt = select(Annotation).where(Annotation.id == annotation_id)
    stmt = stmt.where(_annotation_subject_scope(identity.subject_id))
    row = await session.scalar(stmt)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def list_annotations(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    domain: Optional[str] = None,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
) -> Sequence[Annotation]:
    """Annotations overlapping ``[start, end]`` (either bound optional). A point
    annotation (``end_date is None``) overlaps a range iff its ``date`` falls
    inside it; a ranged one overlaps iff the two ranges intersect."""
    stmt = select(Annotation)
    stmt = stmt.where(_annotation_subject_scope(subject_id))
    if domain is not None:
        stmt = stmt.where(Annotation.domain == domain)
    effective_end = func.coalesce(Annotation.end_date, Annotation.date)
    if start is not None:
        stmt = stmt.where(effective_end >= start)
    if end is not None:
        stmt = stmt.where(Annotation.date <= end)
    stmt = stmt.order_by(Annotation.date.desc(), Annotation.id.desc())
    result = await session.execute(stmt)
    return result.scalars().all()


async def overlays_for(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    domain: str,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
) -> list[dict]:
    """Manual-annotation overlays for one domain's chart: the domain's own flags
    plus global ones (``Domain.TIMELINE``). Shape matches the existing noise/
    phase overlay dicts (``{start, end?, label, tone, kind}``) so the same
    Chart.js annotation plugin renders them."""
    own = await list_annotations(
        session,
        subject_id=subject_id,
        domain=domain,
        start=start,
        end=end,
    )
    glob = (
        await list_annotations(
            session,
            subject_id=subject_id,
            domain=DOMAIN,
            start=start,
            end=end,
        )
        if domain != DOMAIN
        else []
    )
    overlays = []
    for a in list(own) + list(glob):
        overlays.append(
            {
                "start": a.date.isoformat(),
                "end": a.end_date.isoformat() if a.end_date else None,
                "label": a.title,
                "tone": _TONE_BY_KIND.get(a.kind, ""),
                "kind": a.kind,
            }
        )
    return overlays
