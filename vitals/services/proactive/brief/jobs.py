"""Daily Brief compatibility refusal and scheduled job orchestration."""

from __future__ import annotations

import logging
import uuid
from datetime import date as date_type
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    Severity,
    Source,
)
from vitals.i18n import t
from vitals.models.identity import HealthSubject
from vitals.models.milestones import WeeklyDigest
from vitals.models.system_alert import SystemAlert
from vitals.ownership import WriteIdentity
from vitals.services.ai_gateway import contracts as ai_gateway_service_contracts
from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.alerts import lifecycle as alerts_service_lifecycle
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.proactive import compose
from vitals.services.proactive.preferences import contracts as preference_contracts
from vitals.services.proactive.preferences import queries as preference_queries
from vitals.services.proactive.delivery import contracts as delivery_contracts
from vitals.services.proactive.delivery import dispatch as delivery_dispatch
from vitals.services.proactive.delivery import preparation as delivery_preparation
from vitals.services.proactive.delivery import queries as delivery_queries
from vitals.utils.timeutils import now_local, today_local

from .context import _require_llm_connection_scope, build_context
from .contracts import (
    BRIEF_WAIT_HOURS,
    EMPTY_DAY_ALERT_KEY,
    BriefOwnershipError,
    BriefSurface,
    PreparedBrief,
    _PreparedBrief,
)
from .persistence import (
    cancel_and_persist_header_brief,
    existing_brief_for_prepared,
    persist_brief,
)
from .preparation import prepare_brief
from .rendering import render_brief, start_brief_dispatch

logger = logging.getLogger(__name__)

async def generate_brief(
    session: AsyncSession,
    llm: Any,
    *,
    on_date: Optional[date_type] = None,
    source: str = Source.MANUAL.value,
    identity: WriteIdentity | None,
    llm_connection_id: uuid.UUID | None,
) -> Optional[WeeklyDigest]:
    """The retired zero-subject entry point; it now only refuses.

    Every domain the brief reads is closed, so there is no context to assemble
    without a subject — and with one, the phased gateway APIs are the only way
    in. What survives here is the refusal itself, so a caller that still reaches
    for this spelling fails loudly instead of quietly producing nothing.
    """
    del llm, on_date, source
    await acquire_identity_governance_lock(session)
    if identity is not None or llm_connection_id is not None:
        raise BriefOwnershipError(
            "identity-bearing Daily Brief generation requires phased gateway APIs"
        )
    if await session.scalar(select(HealthSubject.id).limit(1)) is not None:
        raise BriefOwnershipError(
            "identity-bearing Daily Brief generation requires phased gateway APIs"
        )
    raise BriefOwnershipError(
        "Daily Brief generation requires phased gateway APIs"
    )


async def _prepare_brief(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    source: str = Source.MANUAL.value,
    identity: WriteIdentity,
    llm_connection_id: uuid.UUID | None = None,
) -> _PreparedBrief | None:
    """Read and freeze one brief context without calling an external service."""
    if not isinstance(identity, WriteIdentity):
        raise BriefOwnershipError("identity must be a WriteIdentity")
    if llm_connection_id is not None:
        await _require_llm_connection_scope(
            session,
            identity=identity,
            connection_id=llm_connection_id,
        )
    on_date = on_date or today_local()
    ctx = await build_context(
        session,
        on_date=on_date,
        subject_id=identity.subject_id,
    )
    if compose.is_empty_day(ctx, on_date=on_date):
        logger.info("no brief for %s: no sleep and nothing new", on_date)
        return None
    # Unconditional, not a flag the caller may forget: whether to *wait* for the
    # night is the job's call, but nobody — job, web button, MCP — gets to build a
    # brief on numbers taken mid-night.
    if compose.night_pending(ctx, on_date=on_date):
        logger.info("brief for %s: last night is not scored, recovery dropped", on_date)
        ctx = compose.drop_unscored_night(ctx)

    return _PreparedBrief(
        on_date=on_date,
        source=source,
        identity=identity,
        llm_connection_id=llm_connection_id,
        context=ctx,
    )



async def _reconcile_empty_day_alert(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    empty: bool,
) -> SystemAlert | None:
    """Raise or clear the actorless subject alert for the durable brief outcome."""
    if not isinstance(identity, WriteIdentity) or identity.actor_user_id is not None:
        raise BriefOwnershipError("empty-day alert reconciliation must be actorless")
    context = alerts_service_contracts.HealthAlertContext(identity=identity)
    if empty:
        return await alerts_service_lifecycle.raise_scoped_alert(
            session,
            context=context,
            domain=Domain.SYSTEM,
            severity=Severity.INFO,
            message=t("alert.brief_empty_day"),
            alert_key=EMPTY_DAY_ALERT_KEY,
            legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
        )
    return await alerts_service_lifecycle.resolve_scoped_by_key(
        session,
        context=context,
        alert_key=EMPTY_DAY_ALERT_KEY,
        legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
    )


def dedupe_key(on_date: date_type) -> str:
    """One brief per day, enforced in the delivery journal rather than hoped for."""
    return f"brief:{on_date.isoformat()}"


def last_attempt_hour(brief_hour: int) -> int:
    """The final hour of the retry window. Clamped to 23 so a late brief time can
    never schedule a fire past midnight — that one would be about the wrong day."""
    return min(brief_hour + BRIEF_WAIT_HOURS, 23)


async def night_scored(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
) -> bool:
    """Has Garmin closed last night yet? ``False`` = worth waiting for.

    No row for the day at all counts as *scored*: that is a watch that has not
    synced, or is not used at all, and holding the brief for it would make an
    optional device a hard dependency of the one proactive feature.
    """
    from vitals.services.garmin import queries as garmin_queries

    row = await garmin_queries.get_daily(
        session,
        on_date,
        subject_id=subject_id,
    )
    if row is None:
        return True
    return any(getattr(row, key, None) is not None for key in compose.NIGHT_SCORED_KEYS)


async def _run_scheduled_brief_generation(
    session_factory,
    *,
    on_date: date_type,
) -> tuple[WeeklyDigest | None, PreparedBrief | None, str]:
    """Run scheduler T1/T2/provider/T3 with ambiguous-commit reconciliation."""

    prepared = None
    for prepare_try in range(2):
        async with session_factory() as session:
            prepared = await prepare_brief(
                session,
                actor_username=None,
                invocation_source=AIInvocationSource.SCHEDULER,
                surface=BriefSurface.SCHEDULER,
                on_date=on_date,
            )
            try:
                await session.commit()
                break
            except Exception:
                await session.rollback()
                if prepare_try:
                    raise
    if prepared is None:
        return None, None, "empty"

    if prepared.existing_artifact_id is not None:
        async with session_factory() as session:
            row = await existing_brief_for_prepared(session, prepared)
            await session.commit()
        return row, prepared, "existing"
    if not prepared.dispatchable:
        if prepared.reservation_status is AIInvocationStatus.DISPATCHING:
            return None, prepared, "pending"
        async with session_factory() as session:
            row = await persist_brief(session, prepared, None)
            await session.commit()
        return row, prepared, "header"

    lease = None
    for start_try in range(2):
        async with session_factory() as session:
            try:
                lease = await start_brief_dispatch(session, prepared)
            except ai_gateway_service_contracts.AIGatewayConfigurationError:
                await session.rollback()
                async with session_factory() as fallback_session:
                    row = await cancel_and_persist_header_brief(
                        fallback_session,
                        prepared,
                    )
                    await fallback_session.commit()
                return row, prepared, "header"
            except ai_gateway_service_contracts.AIInvocationStateError:
                await session.rollback()
                lease = None
            else:
                try:
                    await session.commit()
                    break
                except Exception:
                    # Drop the credential-bearing lease on an ambiguous COMMIT.
                    lease = None
                    await session.rollback()
        if lease is None:
            async with session_factory() as recovery_session:
                prepared = await prepare_brief(
                    recovery_session,
                    actor_username=None,
                    invocation_source=AIInvocationSource.SCHEDULER,
                    surface=BriefSurface.SCHEDULER,
                    on_date=on_date,
                )
                await recovery_session.commit()
            if prepared is None:
                return None, None, "empty"
            if prepared.existing_artifact_id is not None:
                async with session_factory() as reload_session:
                    row = await existing_brief_for_prepared(
                        reload_session,
                        prepared,
                    )
                    await reload_session.commit()
                return row, prepared, "existing"
            if not prepared.dispatchable:
                if prepared.reservation_status is AIInvocationStatus.DISPATCHING:
                    return None, prepared, "pending"
                async with session_factory() as header_session:
                    row = await persist_brief(header_session, prepared, None)
                    await header_session.commit()
                return row, prepared, "header"
            if start_try:
                return None, prepared, "pending"
    if lease is None:  # pragma: no cover - all ordinary branches return or assign
        return None, prepared, "pending"

    completion = await render_brief(prepared, lease)
    for persist_try in range(2):
        async with session_factory() as session:
            try:
                row = await persist_brief(session, prepared, completion)
                await session.commit()
                return row, prepared, "ok" if row.model is not None else "header"
            except Exception:
                await session.rollback()
                if persist_try:
                    raise
    raise RuntimeError("scheduled Daily Brief persistence did not resolve")


# ── Scheduler job ─────────────────────────────────────────────────────────────
async def brief_job(session_factory, redis=None, *, subject_id) -> None:
    """The 11:00 brief — fired hourly across the wait window, sent once.

    Pulls Garmin first, on its own, instead of hoping the poll schedule happened
    to run this morning — last night's sleep is the whole point of the message.
    A Garmin failure is not a reason to stay quiet: the brief goes out with
    whatever is in the lake.

    11:00 is a guess at when he is up, and one morning it was wrong: the brief
    went out while he was still asleep, read the middle of the night as a wrecked
    recovery and advised skipping the gym over it — then stored that, where the
    weekly digest reads it back as what the morning actually was. So
    the job no longer assumes: with today's row present but the night un-scored it
    sends nothing and lets the next hourly fire look again, up to
    ``BRIEF_WAIT_HOURS``. In practice the brief now lands within the hour of
    waking rather than on the hour of the clock. The last fire gives up and sends
    what there is, minus the numbers the night never produced.
    """
    from vitals.services.garmin import jobs as garmin_jobs
    from vitals.services.language_service import get_language
    from vitals.i18n import current_lang
    from vitals.services.proactive import channels

    today = today_local()
    legacy_delivery_key = dedupe_key(today)
    delivery_key = delivery_contracts.make_delivery_idempotency_key("brief", today)
    # Before the Garmin pull, not after: on a normal day the brief left at 11:00
    # and every later fire in the window is a no-op that must not cost a login.
    async with session_factory() as session:
        ownership = await channels.resolve_subject_channel_ownership(
            session,
            subject_id=subject_id,
        )
        confirmed_journal = await delivery_queries.confirmed_delivery_journal(
            session,
            idempotency_key=delivery_key,
            category=delivery_contracts.CATEGORY_BRIEF,
            ownership=ownership,
            legacy_dedupe_key=legacy_delivery_key,
        )
        claimed = confirmed_journal is not None or await delivery_queries.delivery_claim_exists(
            session,
            idempotency_key=delivery_key,
            ownership=ownership,
        )
        brief_hour = None
        if not claimed:
            try:
                subject_policy = await preference_queries.get_subject_policy(
                    session,
                    subject_id=ownership.subject_id,
                )
            except preference_contracts.ProactivePreferencesNotConfiguredError:
                # New accounts intentionally have no delivery policy until the
                # owner saves notification settings. That is not a failed job
                # and must not become a permanent dashboard alert.
                await session.commit()
                return
            brief_hour = subject_policy.brief_time.hour
        # End all ownership/settings reads before Garmin can touch the network.
        await session.commit()
    if confirmed_journal is not None:
        # The journal is durable evidence that generation succeeded, but alert
        # reconciliation may have failed in a later transaction. Retry that local
        # bookkeeping on every replay without calling Garmin, OpenRouter, or the
        # delivery channel. Keep its governance/S/key locks in a fresh transaction.
        async with session_factory() as session:
            await _reconcile_empty_day_alert(
                session,
                identity=ownership.system_action(),
                empty=False,
            )
            await session.commit()
        return
    if claimed:
        # In-flight, ambiguous, and cancelled occurrences are authoritative
        # claims but not evidence that generation/delivery succeeded.
        return
    assert brief_hour is not None
    out_of_patience = now_local().hour >= last_attempt_hour(brief_hour)

    try:
        # This subject's watch. It pulled "the sole subject's" before the brief
        # was fanned out, which on a two-person installation meant the brief
        # either refused or composed one person's morning from another's night.
        await garmin_jobs.sync_job(session_factory, redis, subject_id=subject_id)
    except Exception:
        logger.warning("garmin sync before the brief failed; using stored data", exc_info=True)

    async with session_factory() as session:
        ownership = await channels.resolve_subject_channel_ownership(
            session,
            subject_id=subject_id,
        )
        current_lang.set(
            await get_language(
                session,
                redis,
                user_id=ownership.recipient_user_id,
            )
        )

        # A second pass at yesterday's unparsed messages used to run here: the
        # brief was the deadline signals had to be reparsed before. Signals and
        # the inbound channel that fed them are both gone, so there is nothing
        # left to recover and no rollback to re-establish roots after.
        current_lang.set(
            await get_language(
                session,
                redis,
                user_id=ownership.recipient_user_id,
            )
        )

        # Nothing is built, so nothing is stored and no model call is spent: an
        # un-scored night is not an empty day either, so it raises no alert — the
        # next fire is an hour away and this is the normal state of a lie-in.
        if not out_of_patience and not await night_scored(
            session,
            today,
            subject_id=ownership.subject_id,
        ):
            logger.info("brief for %s postponed: last night is not scored yet", today)
            await session.commit()
            return

        await session.commit()

    system_identity = ownership.system_action()
    row, prepared, generation_outcome = await _run_scheduled_brief_generation(
        session_factory,
        on_date=today,
    )
    if generation_outcome == "empty":
        async with session_factory() as session:
            await _reconcile_empty_day_alert(
                session,
                identity=system_identity,
                empty=True,
            )
            await session.commit()
        return
    if generation_outcome == "pending" or row is None or prepared is None:
        return

    # No keyboard and no correction hint: both were how a Telegram message got
    # its day-context answer corrected in one tap, and both went with it.
    text = row.content
    buttons = None

    async with session_factory() as session:
        ownership = await channels.resolve_subject_channel_ownership(
            session,
            subject_id=subject_id,
        )
        bound_notifier = await channels.build_legacy_bound_notifier(
            session,
            ownership,
        )
        prepared_delivery = await delivery_preparation.prepare_delivery_intent(
            session,
            bound_notifier,
            text=text,
            category=delivery_contracts.CATEGORY_BRIEF,
            idempotency_key=delivery_key,
            legacy_dedupe_key=legacy_delivery_key,
            buttons=buttons,
            ownership=ownership,
        )
        await session.commit()

    if prepared_delivery is not None:
        async with session_factory() as session:
            dispatch_lease = await delivery_dispatch.start_delivery_dispatch(
                session,
                prepared_delivery,
                notifier_resolver=channels.resolve_legacy_bound_notifier,
            )
            await session.commit()
        if dispatch_lease is not None:
            completion = await delivery_dispatch.dispatch_delivery(dispatch_lease)
            for finalize_try in range(2):
                async with session_factory() as session:
                    try:
                        await delivery_dispatch.finalize_delivery(session, completion)
                        await session.commit()
                        break
                    except Exception:
                        await session.rollback()
                        if finalize_try:
                            raise

    # Successful generation clears only this subject's actorless empty-day alert.
    # It is intentionally separate from both durable brief storage and delivery.
    async with session_factory() as session:
        await _reconcile_empty_day_alert(
            session,
            identity=system_identity,
            empty=False,
        )
        await session.commit()
