
"""Scheduled GLP-1 application jobs."""
from __future__ import annotations

import uuid

from sqlalchemy import select

from vitals.services import modules_service
from vitals.services.conflicts import engine
from vitals.services.glp1.plateau import refresh_plateau_alert
from vitals.utils.timeutils import today_local


async def plateau_job(
    session_factory, redis=None, *, subject_id: uuid.UUID
) -> None:
    """Daily plateau check (registered in vitals/scheduler/jobs.py). Runs the same
    refresh the dashboard does, so the alert is fresh even without a page load."""
    async with session_factory() as session:
        today = today_local()
        context = await engine.resolve_subject_conflict_write_context(
            session,
            subject_id=subject_id,
            evaluation_date=today,
        )
        prepared = await engine.prepare_scoped_write(
            session,
            context=context,
        )
        enabled = await modules_service.get_enabled_modules(
            session,
            redis,
            subject_id=context.identity.subject_id,
        )
        if not enabled.get("glp1", False):
            await session.commit()
            return

        from vitals.services.language_service import get_language
        from vitals.i18n import current_lang
        from vitals.models.identity import HealthSubject

        owner_user_id = await session.scalar(
            select(HealthSubject.owner_user_id).where(
                HealthSubject.id == context.identity.subject_id
            )
        )
        lang = await get_language(session, redis, user_id=owner_user_id)
        current_lang.set(lang)

        await refresh_plateau_alert(
            session,
            identity=context.identity,
            prepared_conflict_write=prepared,
        )
        await session.commit()
