"""Subject-scoped conflict and Weight write preparation for MCP adapters."""

from __future__ import annotations

from vitals.services.milestones import queries as milestone_queries

from vitals.services.digest import ownership as digest_ownership

from datetime import date

from vitals.persistence.rls import bind_session_subject
from vitals.services.conflicts import engine
from vitals.utils.timeutils import today_local
from web.mcp.identity import current_grant_binding
from web.mcp.ownership import actor_username


async def conflict_scope(session) -> engine.ConflictScope:
    """Authenticate and bind an MCP conflict read under governance lock."""

    binding = current_grant_binding()
    if binding is not None:
        await bind_session_subject(session, binding.subject_id)
        return engine.ConflictScope(
            subject_id=binding.subject_id,
            evaluation_date=today_local(),
        )

    return await engine.resolve_legacy_conflict_scope(
        session,
        actor_username=await actor_username(session),
        evaluation_date=today_local(),
    )


async def composition_scope(session) -> engine.ConflictScope:
    """Bind a whole-lake projection and validate subject-owned artifact roots."""



    resolved = await conflict_scope(session)
    await milestone_queries.list_milestones(
        session,
        subject_id=resolved.subject_id,
    )
    await digest_ownership.prepare_digest_owner(
        session,
        actor_username=await actor_username(session),
    )
    return resolved


async def conflict_write_context(
    session,
    *,
    evaluation_date: date | None = None,
) -> engine.ConflictWriteContext:
    """Authenticate the MCP actor for a scoped conflict write."""

    binding = current_grant_binding()
    if binding is not None:
        from vitals.ownership import WriteIdentity

        await bind_session_subject(session, binding.subject_id)
        return engine.ConflictWriteContext(
            identity=WriteIdentity(
                subject_id=binding.subject_id,
                actor_user_id=binding.user_id,
            ),
            evaluation_date=evaluation_date or today_local(),
        )
    return await engine.resolve_legacy_conflict_write_context(
        session,
        actor_username=await actor_username(session),
        evaluation_date=evaluation_date or today_local(),
    )


async def weight_write(
    session,
    *,
    evaluation_date: date | None = None,
):
    """Prepare Weight plus its distinct Garmin destination outbox."""

    from vitals.services.garmin_weight import outbox as garmin_weight_outbox
    from vitals.services.weight import governance as weight_governance

    context = await conflict_write_context(
        session,
        evaluation_date=evaluation_date,
    )
    export_context = await garmin_weight_outbox.resolve_optional_legacy_export_context(
        session,
        actor_username=await actor_username(session),
    )
    prepared = await weight_governance.prepare_weight_write(
        session,
        context=context,
        garmin_weight_export_context=export_context,
    )
    return context, prepared


async def auxiliary_weight_write(
    session,
    *,
    evaluation_date: date | None = None,
):
    """Prepare a BodyMeasurement/NoiseMarker write without an outbox advisory."""

    context = await conflict_write_context(
        session,
        evaluation_date=evaluation_date,
    )
    prepared = await engine.prepare_scoped_write(
        session,
        context=context,
    )
    return context, prepared


__all__ = [
    "auxiliary_weight_write",
    "composition_scope",
    "conflict_scope",
    "conflict_write_context",
    "weight_write",
]
