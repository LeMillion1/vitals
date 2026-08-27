"""Subject-scoped Daily Brief context assembly and personal baselines."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
)
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services.digest.projection import assembly as digest_projection
from vitals.services.digest import window as digest_window
from vitals.services.proactive import compose
from vitals.utils.timeutils import today_local

from .contracts import (
    BriefOwnershipError,
    _BASELINE_DAYS,
    _BASELINE_MIN_DAYS,
)

async def _require_llm_connection_scope(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    connection_id: uuid.UUID,
) -> None:
    connection = await session.scalar(
        select(IntegrationConnection).where(IntegrationConnection.id == connection_id)
    )
    if connection is None:
        raise BriefOwnershipError("LLM integration connection does not exist")
    if connection.subject_id != identity.subject_id:
        raise BriefOwnershipError("LLM integration connection belongs to another subject")
    if (
        connection.provider != IntegrationProvider.OPENROUTER.value
        or connection.connection_type != IntegrationConnectionType.AI_GATEWAY.value
    ):
        raise BriefOwnershipError("brief generation requires an OpenRouter AI gateway")
    known_statuses = {status.value for status in IntegrationConnectionStatus}
    if connection.status not in known_statuses:
        raise BriefOwnershipError("LLM integration connection has unknown lifecycle state")
    if connection.status not in {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }:
        raise BriefOwnershipError(
            "inactive LLM integration connection cannot generate a brief"
        )

async def build_context(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    subject_id: uuid.UUID,
) -> dict:
    """Today's cross-domain snapshot, minus the protocol, plus the day context.

    The day context is the difference between "спал плохо — отдохни" and advice
    that knows there is a gym session and a heavy workday ahead, so it goes into
    the model's JSON as well as onto the header line.
    """
    if subject_id is None:
        raise ValueError("composing the brief requires the subject it is about")
    ctx = await digest_projection.assemble_context(
        session,
        subject_id=subject_id,
        on_date=on_date,
        period_days=1,
        mode=digest_window.REPORT_MODE_BRIEF,
    )
    ctx = compose.strip_protocol(ctx)
    today = on_date or today_local()
    # The one thing the brief could never do: compare. Handed a single day of
    # absolute numbers and asked what they mean, the model supplied the missing
    # half itself — "просадка SpO2 и повышенный пульс покоя" on a resting HR that
    # had not moved a beat. His own fortnight is what those words have to be true
    # against, so it goes in beside the numbers rather than being left implied.
    if ctx.get("garmin"):
        ctx["garmin"]["baseline"] = await _baseline(
            session,
            today,
            subject_id=subject_id,
        )
    # ``ctx["day"]`` stood here — what kind of day it was, his answer or the
    # template's guess. Both are gone: the evening block asked the question and
    # the chat carried the answer.
    return ctx


async def _baseline(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
) -> Optional[dict]:
    """His own mean per metric over the days *before* today.

    Strictly before: today's number is the thing being judged, and folding it into
    the yardstick pulls the yardstick toward it — worst exactly on the outlier
    mornings the comparison exists for. ``None`` until there is enough history for
    a mean to mean anything; a "norm" off two nights is noise wearing the word.
    """
    from vitals.services.garmin import queries as garmin_queries

    rows = [
        row
        for row in await garmin_queries.list_daily(
            session,
            subject_id=subject_id,
            limit=_BASELINE_DAYS + 1,
        )
        if 0 < (on_date - row.date).days <= _BASELINE_DAYS
    ]
    baseline = {}
    for key in compose.BASELINE_KEYS:
        values = [v for v in (getattr(row, key, None) for row in rows) if v is not None]
        if len(values) >= _BASELINE_MIN_DAYS:
            baseline[key] = round(sum(values) / len(values), 1)
    return baseline or None
