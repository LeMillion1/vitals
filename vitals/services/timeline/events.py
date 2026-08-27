"""Read-only cross-domain projection for the unified Timeline feed."""

from __future__ import annotations

from vitals.services.milestones import queries as milestone_queries

import uuid
from dataclasses import dataclass
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import LabFlag, MilestoneStatus
from vitals.i18n import t
from vitals.services.timeline.annotations import DOMAIN, _TONE_BY_KIND, list_annotations


@dataclass(frozen=True)
class TimelineEvent:
    date: date_type
    end_date: Optional[date_type]
    domain: str
    kind: str
    title: str
    detail: Optional[str]
    tone: str  # 'good' | 'bad' | 'warn' | ''
    source: str  # 'manual' | 'derived'
    ref: str

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "domain": self.domain,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "tone": self.tone,
            "source": self.source,
            "ref": self.ref,
        }


async def _derived_events(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: Optional[date_type],
    end: Optional[date_type],
) -> list[TimelineEvent]:
    events: list[TimelineEvent] = []

    def scoped(stmt, model):
        return stmt.where(model.subject_id == subject_id)

    # GLP-1 dose phase starts — the injection log itself is too frequent to
    # surface individually here (it would flood the feed); a phase *change* is
    # the meaningful protocol event, and it's the same transition already
    # painted as a band on the weight chart.
    from vitals.models.glp1 import DOMAIN as GLP1_DOMAIN, DosePhase

    p_stmt = scoped(
        select(DosePhase).where(DosePhase.domain == GLP1_DOMAIN),
        DosePhase,
    )
    if start is not None:
        p_stmt = p_stmt.where(func.coalesce(DosePhase.end_date, DosePhase.start_date) >= start)
    if end is not None:
        p_stmt = p_stmt.where(DosePhase.start_date <= end)
    phases = (await session.execute(p_stmt)).scalars().all()
    for p in phases:
        events.append(
            TimelineEvent(
                date=p.start_date,
                end_date=None,
                domain=GLP1_DOMAIN,
                kind="protocol_change",
                title=t(
                    "timeline.derived.dose_phase_start",
                    drug=t("enum.drug." + p.drug),
                    dose=p.dose_mg,
                ),
                detail=None,
                tone="",
                source="derived",
                ref=f"dose_phase:{p.id}",
            )
        )

    # Side effects, severity >= 3 — mild (1-2) is frequent/noisy (same
    # "too frequent to log individually" reasoning as the injections above);
    # a moderate-to-severe reaction is the "why did the trend wobble" context
    # the weekly digest already wants correlated. Thresholds match the
    # existing severity color-coding on the GLP-1 page itself.
    from vitals.models.glp1 import SideEffect

    se_stmt = scoped(
        select(SideEffect).where(SideEffect.domain == GLP1_DOMAIN, SideEffect.severity >= 3),
        SideEffect,
    )
    if start is not None:
        se_stmt = se_stmt.where(SideEffect.date >= start)
    if end is not None:
        se_stmt = se_stmt.where(SideEffect.date <= end)
    for se in (await session.execute(se_stmt)).scalars().all():
        events.append(
            TimelineEvent(
                date=se.date,
                end_date=None,
                domain=GLP1_DOMAIN,
                kind="side_effect",
                title=t("timeline.derived.side_effect", type=se.effect_type, severity=se.severity),
                detail=se.note,
                tone="bad" if se.severity >= 4 else "warn",
                source="derived",
                ref=f"side_effect:{se.id}",
            )
        )

    # Lab draws — one event per collection date, tone follows the worst flag
    # in that day's batch (a critical result reads very differently from a
    # routine check that happened to include the same marker count).
    from vitals.models.labs import DOMAIN as LABS_DOMAIN, LabResult

    l_stmt = scoped(
        select(LabResult.date, LabResult.marker, LabResult.flag).where(
            LabResult.domain == LABS_DOMAIN
        ),
        LabResult,
    )
    if start is not None:
        l_stmt = l_stmt.where(LabResult.date >= start)
    if end is not None:
        l_stmt = l_stmt.where(LabResult.date <= end)
    labs_by_day: dict[date_type, list[tuple[str, Optional[str]]]] = {}
    for on_date, marker, flag in (await session.execute(l_stmt)).all():
        labs_by_day.setdefault(on_date, []).append((marker, flag))
    for on_date, markers in labs_by_day.items():
        flagged = [(mk, fl) for mk, fl in markers if fl and fl != LabFlag.NORMAL.value]
        critical = any(
            fl in (LabFlag.CRITICAL_LOW.value, LabFlag.CRITICAL_HIGH.value) for _, fl in flagged
        )
        tone = "bad" if critical else ("warn" if flagged else "")
        detail = ", ".join(f"{mk} ({t('enum.flag.' + fl)})" for mk, fl in flagged) or None
        events.append(
            TimelineEvent(
                date=on_date,
                end_date=None,
                domain=LABS_DOMAIN,
                kind="note",
                title=t("timeline.derived.labs_batch", n=len(markers)),
                detail=detail,
                tone=tone,
                source="derived",
                ref=f"labs:{on_date.isoformat()}",
            )
        )

    # BIA / InBody scans.
    from vitals.models.body_scan import BodyScan
    from vitals.models.body_scan import DOMAIN as BODY_DOMAIN

    b_stmt = scoped(
        select(BodyScan).where(BodyScan.domain == BODY_DOMAIN),
        BodyScan,
    )
    if start is not None:
        b_stmt = b_stmt.where(BodyScan.date >= start)
    if end is not None:
        b_stmt = b_stmt.where(BodyScan.date <= end)
    for scan in (await session.execute(b_stmt)).scalars().all():
        device = scan.device or t("timeline.derived.device_unknown")
        events.append(
            TimelineEvent(
                date=scan.date,
                end_date=None,
                domain=BODY_DOMAIN,
                kind="note",
                title=t("timeline.derived.body_scan", device=device),
                detail=None,
                tone="",
                source="derived",
                ref=f"body_scan:{scan.id}",
            )
        )

    # Progress photos — a checkpoint marker only. No thumbnail: these are often
    # nude/intimate reference shots for a body-recomposition dashboard, and this
    # feed mixes freely with unrelated, non-sensitive events (goals, supplements,
    # labs) — an always-visible inline image here is real, unwanted exposure.
    # The photo itself stays reachable only from its deliberate destination
    # (the /weight gallery), never surfaced ambiently.
    from vitals.models.weight import DOMAIN as WEIGHT_DOMAIN
    from vitals.services.weight import photos as weight_photos

    photos = await weight_photos.list_progress_photos(
        session,
        subject_id=subject_id,
        start=start,
        end=end,
    )
    for p in photos:
        events.append(
            TimelineEvent(
                date=p.date,
                end_date=None,
                domain=WEIGHT_DOMAIN,
                kind="photo",
                title=t("timeline.derived.progress_photo"),
                detail=p.note,
                tone="",
                source="derived",
                ref=f"progress_photo:{p.id}",
            )
        )

    # Milestones — created (any status, from created_at) and the achieved/
    # missed transition (from updated_at; same accepted limitation as before:
    # editing an already-resolved goal would also bump updated_at, and there's
    # no dedicated history table to do better). `domain` here names the
    # *related* health area (weight/glp1/...), not a milestones-module domain,
    # so there's no domain filter on the query.

    milestones = await milestone_queries.list_milestones(
        session,
        subject_id=subject_id,
    )
    for m in milestones:
        created_date = m.created_at.date()
        if (start is None or created_date >= start) and (end is None or created_date <= end):
            events.append(
                TimelineEvent(
                    date=created_date,
                    end_date=None,
                    domain=m.domain,
                    kind="milestone",
                    title=t("timeline.derived.milestone_created", name=m.name),
                    detail=None,
                    tone="",
                    source="derived",
                    ref=f"milestone_created:{m.id}",
                )
            )

        if m.status == MilestoneStatus.ACHIEVED.value:
            resolved_date = m.updated_at.date()
            if (start is None or resolved_date >= start) and (end is None or resolved_date <= end):
                events.append(
                    TimelineEvent(
                        date=resolved_date,
                        end_date=None,
                        domain=m.domain,
                        kind="milestone",
                        title=t("timeline.derived.milestone_achieved", name=m.name),
                        detail=None,
                        tone="good",
                        source="derived",
                        ref=f"milestone_achieved:{m.id}",
                    )
                )
        elif m.status == MilestoneStatus.MISSED.value:
            resolved_date = m.updated_at.date()
            if (start is None or resolved_date >= start) and (end is None or resolved_date <= end):
                events.append(
                    TimelineEvent(
                        date=resolved_date,
                        end_date=None,
                        domain=m.domain,
                        kind="milestone",
                        title=t("timeline.derived.milestone_missed", name=m.name),
                        detail=None,
                        tone="bad",
                        source="derived",
                        ref=f"milestone_missed:{m.id}",
                    )
                )

    # Noise markers (weight) — surfaced in the feed even though the chart
    # already shades them, so the timeline reads as a complete log of "why".
    from vitals.models.weight import DOMAIN as WEIGHT_DOMAIN, NoiseMarker

    n_stmt = scoped(
        select(NoiseMarker).where(NoiseMarker.domain == WEIGHT_DOMAIN),
        NoiseMarker,
    )
    if start is not None:
        n_stmt = n_stmt.where(func.coalesce(NoiseMarker.end_date, NoiseMarker.start_date) >= start)
    if end is not None:
        n_stmt = n_stmt.where(NoiseMarker.start_date <= end)
    for n in (await session.execute(n_stmt)).scalars().all():
        events.append(
            TimelineEvent(
                date=n.start_date,
                end_date=n.end_date,
                domain=WEIGHT_DOMAIN,
                kind="note",
                title=t("timeline.derived.noise_period", reason=n.reason),
                detail=None,
                tone="warn",
                source="derived",
                ref=f"noise_marker:{n.id}",
            )
        )

    # Supplements — started (created_at) and stopped (active flips false,
    # updated_at stands in for "stopped on"; same limitation as milestones
    # above — no dedicated history table, so an unrelated edit after stopping
    # would also move this date).
    from vitals.models.supplements import DOMAIN as SUPP_DOMAIN, Supplement

    sup_stmt = scoped(
        select(Supplement).where(Supplement.domain == SUPP_DOMAIN),
        Supplement,
    )
    for s in (await session.execute(sup_stmt)).scalars().all():
        started = s.created_at.date()
        if (start is None or started >= start) and (end is None or started <= end):
            events.append(
                TimelineEvent(
                    date=started,
                    end_date=None,
                    domain=SUPP_DOMAIN,
                    kind="protocol_change",
                    title=t("timeline.derived.supplement_started", name=s.name),
                    detail=None,
                    tone="",
                    source="derived",
                    ref=f"supplement_started:{s.id}",
                )
            )
        if not s.active:
            stopped = s.updated_at.date()
            if (start is None or stopped >= start) and (end is None or stopped <= end):
                events.append(
                    TimelineEvent(
                        date=stopped,
                        end_date=None,
                        domain=SUPP_DOMAIN,
                        kind="protocol_change",
                        title=t("timeline.derived.supplement_stopped", name=s.name),
                        detail=None,
                        tone="",
                        source="derived",
                        ref=f"supplement_stopped:{s.id}",
                    )
                )

    # Skincare products — added/removed from the active routine. No domain/
    # source column on this table (unlike Supplement/GeneticVariant), so no
    # domain filter on the query; the module's own DOMAIN constant is used
    # when building the event instead.
    from vitals.models.skincare import DOMAIN as SKINCARE_DOMAIN, SkincareProduct

    sp_stmt = scoped(
        select(SkincareProduct),
        SkincareProduct,
    )
    for sp in (await session.execute(sp_stmt)).scalars().all():
        added = sp.created_at.date()
        if (start is None or added >= start) and (end is None or added <= end):
            events.append(
                TimelineEvent(
                    date=added,
                    end_date=None,
                    domain=SKINCARE_DOMAIN,
                    kind="protocol_change",
                    title=t("timeline.derived.skincare_added", name=sp.name),
                    detail=None,
                    tone="",
                    source="derived",
                    ref=f"skincare_added:{sp.id}",
                )
            )
        if not sp.active:
            removed = sp.updated_at.date()
            if (start is None or removed >= start) and (end is None or removed <= end):
                events.append(
                    TimelineEvent(
                        date=removed,
                        end_date=None,
                        domain=SKINCARE_DOMAIN,
                        kind="protocol_change",
                        title=t("timeline.derived.skincare_removed", name=sp.name),
                        detail=None,
                        tone="",
                        source="derived",
                        ref=f"skincare_removed:{sp.id}",
                    )
                )

    # Genetics — one-off VCF/manual import, grouped by day in Python (no
    # InsightsMixin date on this table, and no portable way to truncate a
    # DateTime column to a day identically on SQLite/Postgres). The table is
    # small — one row per variant — so fetch-then-group costs nothing here.
    from vitals.models.genetics import DOMAIN as GENETICS_DOMAIN, GeneticVariant

    gv_stmt = scoped(
        select(GeneticVariant).where(GeneticVariant.domain == GENETICS_DOMAIN),
        GeneticVariant,
    )
    variants_by_day: dict[date_type, int] = {}
    for v in (await session.execute(gv_stmt)).scalars().all():
        d = v.created_at.date()
        variants_by_day[d] = variants_by_day.get(d, 0) + 1
    for d, count in variants_by_day.items():
        if (start is None or d >= start) and (end is None or d <= end):
            events.append(
                TimelineEvent(
                    date=d,
                    end_date=None,
                    domain=GENETICS_DOMAIN,
                    kind="note",
                    title=t("timeline.derived.genetics_import", n=count),
                    detail=None,
                    tone="",
                    source="derived",
                    ref=f"genetics_import:{d.isoformat()}",
                )
            )

    return events


async def list_events(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    domains: Optional[Sequence[str]] = None,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    limit: int = 200,
) -> list[TimelineEvent]:
    """The unified feed: manual annotations + derived events, newest first."""
    events: list[TimelineEvent] = []

    for a in await list_annotations(
        session,
        subject_id=subject_id,
        start=start,
        end=end,
    ):
        events.append(
            TimelineEvent(
                date=a.date,
                end_date=a.end_date,
                domain=a.domain,
                kind=a.kind,
                title=a.title,
                detail=a.note,
                tone=_TONE_BY_KIND.get(a.kind, ""),
                source="manual",
                ref=f"annotation:{a.id}",
            )
        )

    events.extend(
        await _derived_events(
            session,
            subject_id=subject_id,
            start=start,
            end=end,
        )
    )

    if domains is not None:
        allowed = set(domains) | {DOMAIN}
        events = [e for e in events if e.domain in allowed]

    events.sort(key=lambda e: e.date, reverse=True)
    return events[:limit]
