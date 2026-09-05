
"""Scheduled Nutrition application jobs."""
from __future__ import annotations

import uuid

from vitals.enums import Domain
from vitals.services.conflicts import engine
from vitals.services.modules import preferences as modules_service
from vitals.utils.timeutils import today_local


async def day_end_job(
    session_factory, redis=None, *, subject_id: uuid.UUID
) -> None:
    """Once-daily check (registered in vitals/scheduler/jobs.py, 23:00 local) for
    nutrition rules that need a *complete* day's totals — e.g. the very-low-
    calorie/protein GLP-1 warnings, which would false-positive off a partial
    running total if evaluated live on every meal save (see log_meal's
    ``enforce`` call, which never passes ``include_day_end``). By 23:00 the
    day's logged totals are effectively final.

    Uses scoped day-end reconciliation (not live ``enforce``) so a rule that
    stops matching on a later, better day also gets its alert cleared
    automatically — not just raised."""
    async with session_factory() as session:
        on_date = today_local()
        context = await engine.resolve_subject_conflict_write_context(
            session,
            subject_id=subject_id,
            evaluation_date=on_date,
        )
        enabled = await modules_service.get_enabled_modules(
            session,
            redis,
            subject_id=context.identity.subject_id,
        )
        if not enabled.get("nutrition", False):
            await session.commit()
            return
        await engine.reconcile_day_end_scoped(
            session,
            context=context,
            domain=Domain.NUTRITION,
            entity_ref=f"meal:{on_date.isoformat()}",
        )
        await session.commit()
