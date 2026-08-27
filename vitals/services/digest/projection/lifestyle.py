"""Timeline and milestone collectors."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.digest.window import ReportWindow, _coverage
from vitals.services.timeline import annotations as timeline_annotations
from vitals.services.digest.projection.contracts import (
    ClinicalProjection,
    DomainVisibility,
    ModuleGate,
    ProviderProjection,
    _TIMELINE_LIMIT,
)


async def collect_lifestyle(
    session: AsyncSession,
    *,
    ctx: dict[str, Any],
    subject_id: uuid.UUID,
    window: ReportWindow,
    module_on: ModuleGate,
    domain_visible: DomainVisibility,
    providers: ProviderProjection,
    clinical: ClinicalProjection,
) -> None:
    period_start = window.period_start
    period_end = window.period_end
    since = period_start
    all_supps = clinical.all_supplements
    all_products = clinical.all_products
    variants = clinical.variants
    measurement_history = providers.measurement_history
    latest_weight = providers.latest_weight
    scan = providers.scan
    # Timeline — manual annotations (illness, travel, protocol change) overlapping
    # the period. These are exactly the "why" behind a wobble in every other domain,
    # so the narrative has to see them.

    timeline_enabled = module_on("timeline")
    timeline_entries: list[dict[str, Any]] = []
    if timeline_enabled:
        annotations = await timeline_annotations.list_annotations(
            session, start=since, end=period_end, subject_id=subject_id
        )
        timeline_entries.extend(
            {
                "date": row.date.isoformat(),
                "end_date": row.end_date.isoformat() if row.end_date else None,
                "kind": row.kind,
                "domain": row.domain,
                "title": row.title,
                "note": row.note,
                "source": "manual",
                "ref": f"annotation:{row.id}",
                "certainty": "exact",
            }
            for row in annotations
            if domain_visible(row.domain)
        )

        # Only lifecycle facts not already represented by a first-class context
        # block are added here. ``updated_at`` is explicitly labelled as an audit
        # timestamp, because these catalogs do not have a true stop-history table.
        for row in all_supps:
            started = row.created_at.date()
            if since <= started <= period_end:
                timeline_entries.append(
                    {
                        "date": started.isoformat(),
                        "end_date": None,
                        "kind": "supplement_started",
                        "domain": "supplements",
                        "title": row.name,
                        "note": None,
                        "source": "derived",
                        "ref": f"supplement_started:{row.id}",
                        "certainty": "audit_timestamp",
                    }
                )
            stopped = row.updated_at.date()
            if not row.active and since <= stopped <= period_end:
                timeline_entries.append(
                    {
                        "date": stopped.isoformat(),
                        "end_date": None,
                        "kind": "supplement_stopped",
                        "domain": "supplements",
                        "title": row.name,
                        "note": None,
                        "source": "derived",
                        "ref": f"supplement_stopped:{row.id}",
                        "certainty": "audit_timestamp",
                    }
                )
        for row in all_products:
            added = row.created_at.date()
            if since <= added <= period_end:
                timeline_entries.append(
                    {
                        "date": added.isoformat(),
                        "end_date": None,
                        "kind": "skincare_product_added",
                        "domain": "skincare",
                        "title": row.name,
                        "note": row.active_ingredient,
                        "source": "derived",
                        "ref": f"skincare_added:{row.id}",
                        "certainty": "audit_timestamp",
                    }
                )
            removed = row.updated_at.date()
            if not row.active and since <= removed <= period_end:
                timeline_entries.append(
                    {
                        "date": removed.isoformat(),
                        "end_date": None,
                        "kind": "skincare_product_removed",
                        "domain": "skincare",
                        "title": row.name,
                        "note": None,
                        "source": "derived",
                        "ref": f"skincare_removed:{row.id}",
                        "certainty": "audit_timestamp",
                    }
                )
        genetics_by_day: dict[date_type, int] = {}
        for row in variants:
            imported = row.created_at.date()
            if since <= imported <= period_end:
                genetics_by_day[imported] = genetics_by_day.get(imported, 0) + 1
        for imported, count in genetics_by_day.items():
            timeline_entries.append(
                {
                    "date": imported.isoformat(),
                    "end_date": None,
                    "kind": "genetics_import",
                    "domain": "genetics",
                    "title": f"{count} curated variants imported",
                    "note": None,
                    "source": "derived",
                    "ref": f"genetics_import:{imported.isoformat()}",
                    "certainty": "audit_timestamp",
                }
            )

    # ``signals`` stood here — what the person said about how they felt, the one
    # block that could say *why* a measurement moved. It is gone with the chat it
    # was parsed from, and nothing replaces it: no device produces a sentence.
    from vitals.enums import MilestoneStatus
    from vitals.models.milestones import Milestone

    milestone_rows = list(
        (
            await session.execute(
                select(Milestone)
                .where(Milestone.subject_id == subject_id)
                .order_by(Milestone.deadline.is_(None), Milestone.deadline, Milestone.id)
            )
        )
        .scalars()
        .all()
    )
    active_milestones = [
        row
        for row in milestone_rows
        if row.status == MilestoneStatus.ACTIVE.value
        and row.created_at.date() <= period_end
        and domain_visible(row.domain)
    ]

    latest_navy_bf = next(
        (
            row["body_fat_pct"]
            for row in reversed(measurement_history)
            if row["body_fat_pct"] is not None
        ),
        None,
    )
    latest_bia_bf = None
    if scan is not None:
        latest_bia_bf = next(
            (
                metric.value
                for metric in scan.metrics
                if metric.metric_key == "body_fat_pct" and metric.segment is None
            ),
            None,
        )

    milestone_context = []
    for row in active_milestones:
        current = None
        if row.domain == "weight" and row.target_value is not None and latest_weight:
            current = latest_weight.weight_kg
        elif row.domain == "body_comp" and row.target_value is not None:
            current = latest_bia_bf if latest_bia_bf is not None else latest_navy_bf
        milestone_context.append(
            {
                "id": row.id,
                "name": row.name,
                "domain": row.domain,
                "status": row.status,
                "target_value": row.target_value,
                "target_unit": row.target_unit,
                "deadline": row.deadline.isoformat() if row.deadline else None,
                "days_left": ((row.deadline - period_end).days if row.deadline else None),
                "current": round(current, 2) if current is not None else None,
                "remaining": (
                    round(current - row.target_value, 2)
                    if current is not None and row.target_value is not None
                    else None
                ),
                "note": row.note,
                "state_is_current_catalog": True,
            }
        )
    ctx["milestones"] = milestone_context
    ctx["coverage"]["milestones"] = _coverage(
        module="reports",
        enabled=True,
        window=window,
        rows=len(active_milestones),
        extra={"historical_state_reliable": False},
    )

    if timeline_enabled:
        for row in milestone_rows:
            if not domain_visible(row.domain):
                continue
            created = row.created_at.date()
            if since <= created <= period_end:
                timeline_entries.append(
                    {
                        "date": created.isoformat(),
                        "end_date": None,
                        "kind": "milestone_created",
                        "domain": row.domain,
                        "title": row.name,
                        "note": row.note,
                        "source": "derived",
                        "ref": f"milestone_created:{row.id}",
                        "certainty": "audit_timestamp",
                    }
                )
            resolved = row.updated_at.date()
            if (
                row.status
                in {
                    MilestoneStatus.ACHIEVED.value,
                    MilestoneStatus.MISSED.value,
                }
                and since <= resolved <= period_end
            ):
                timeline_entries.append(
                    {
                        "date": resolved.isoformat(),
                        "end_date": None,
                        "kind": f"milestone_{row.status}",
                        "domain": row.domain,
                        "title": row.name,
                        "note": row.note,
                        "source": "derived",
                        "ref": f"milestone_{row.status}:{row.id}",
                        "certainty": "audit_timestamp",
                    }
                )

    timeline_entries.sort(key=lambda item: (item["date"], item["ref"]))
    timeline_truncated = len(timeline_entries) > _TIMELINE_LIMIT
    timeline_entries = timeline_entries[-_TIMELINE_LIMIT:]
    ctx["timeline"] = timeline_entries or None
    ctx["coverage"]["timeline"] = _coverage(
        module="timeline",
        enabled=timeline_enabled,
        dates=[date_type.fromisoformat(item["date"]) for item in timeline_entries],
        window=window,
        truncated=timeline_truncated,
        extra={"event_limit": _TIMELINE_LIMIT},
    )
