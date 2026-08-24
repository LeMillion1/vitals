"""Read-only JSON API for an external personal dashboard (glance cards).

A separate single-user app — same owner — shows a few calm health *glance*
cards: weight trend, today's macros, Garmin recovery, and simple logging
streaks. It reaches this API server-to-server with a static Bearer token
(``VITALS_EXTERNAL_API_TOKEN``); that app's own frontend never talks to Vitals
directly.

Design rules for this module:
  * **Read-only.** Nothing here writes, and it only ever *reads through the
    existing domain services* — no business logic (weight MA/slope, macro goals,
    recovery thresholds) is re-implemented. If a number needs computing, the
    service that owns it computes it.
  * **Locale-agnostic.** It returns raw numbers/codes only, never rendered text
    (e.g. no ``recovery_advice`` string): the caller applies its own i18n.
  * **Fails closed.** A missing/blank server token disables the endpoint (503);
    a wrong/absent Bearer is 401. The token is constant-time compared.

Auth deliberately bypasses the session/OAuth stack: the caller holds one
long-lived token in its own env and presents ``Authorization: Bearer <token>``.

**The token names the record.** It used to be one installation-wide string
(``VITALS_EXTERNAL_API_TOKEN``) and the endpoint resolved its subject from
whoever ``.env`` said the owner was — a per-subject credential by accident on a
single-user machine, and a credential with no boundary the moment a second
person exists. Credentials are now rows: issued by the record's owner, hashed at
rest, expiring, revocable, and answering "whose data is this" by themselves.

The environment token still works while the installation holds exactly one
subject, which is the same fail-closed rule the rest of this migration uses: it
cannot name a record, so it is refused as soon as there is a choice to make. The
answer then is to issue one from Settings, not to guess.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, MilestoneStatus
from vitals.models.identity import HealthSubject
from vitals.services import conflict_engine
from vitals.utils.timeutils import today_local
from web.config import get_web_config
from web.deps import get_session

router = APIRouter(prefix="/external", tags=["external"])

# How far back the streak/activity date lists reach. 60 days is plenty for a
# "current consecutive-day" streak while keeping the payload tiny.
_ACTIVITY_WINDOW_DAYS = 60


def _presented(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token if scheme.lower() == "bearer" else ""


async def resolve_external_caller(
    request: Request, session: AsyncSession = Depends(get_session)
) -> uuid.UUID:
    """Which record this bearer token opens.

    Returns the subject rather than nothing, because "who is asking" and "whose
    data may they have" are the same question here and answering them in two
    places is how they drift apart.

    Two credentials are accepted, and the difference is the whole point of this
    change. A row in ``external_api_tokens`` names its record, so it is checked
    and its subject is used. The environment token names nothing, so it is
    honoured only while the installation holds exactly one subject — where "the
    record" is unambiguous — and refused as soon as it would have to guess.

    503 (not 401) when neither credential is configured at all, so the caller
    can tell "switched off here" from "my token is wrong".
    """

    from vitals.services import external_api_token_service as tokens

    presented = _presented(request)
    configured = get_web_config().external_api_token

    if presented:
        record = await tokens.authenticate(session, presented=presented)
        if record is not None:
            return record.subject_id

    if not configured:
        # No environment token and the presented one matched no row. If nothing
        # is configured and nothing is issued, the endpoint is off rather than
        # picky — but a database that holds credentials is switched on, and a
        # wrong token there is a wrong token.
        if await _any_token_exists(session):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token"
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="external_api_disabled",
        )

    if not presented or not secrets.compare_digest(presented, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token"
        )

    # The environment token, which cannot say whose record it means.
    subject_ids = tuple(
        await session.scalars(select(HealthSubject.id).limit(2))
    )
    if len(subject_ids) != 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="external_api_token_cannot_name_a_record",
        )
    return subject_ids[0]


async def _any_token_exists(session: AsyncSession) -> bool:
    from vitals.models.identity import ExternalApiToken

    return (
        await session.scalar(select(ExternalApiToken.id).limit(1))
    ) is not None


async def _weight_block(
    session: AsyncSession,
    scope: conflict_engine.ConflictScope,
) -> dict[str, Any]:
    """Latest weight, a noise-excluded MA7 sparkline, the trend slope, and — if an
    active weight goal exists — the projected date to reach it (all from
    ``weight_service``; nothing recomputed here)."""
    from vitals.services import milestones_service, weight_service

    weights = await weight_service.list_active_weights(
        session,
        subject_id=scope.subject_id,
    )
    latest = weights[-1] if weights else None

    # An active weight goal (soonest deadline first, per list_milestones' order)
    # feeds chart_series so it returns a projection date for the goal.
    active = await milestones_service.list_milestones(
        session,
        status=MilestoneStatus.ACTIVE.value,
        subject_id=scope.subject_id,
    )
    goal_ms = next(
        (m for m in active if m.domain == Domain.WEIGHT.value and m.target_value is not None),
        None,
    )
    goal_kg = goal_ms.target_value if goal_ms else None

    series = await weight_service.chart_series(
        session,
        subject_id=scope.subject_id,
        goal_kg=goal_kg,
    )
    sparkline = [{"date": p["date"], "kg": p["weight_kg"]} for p in series["trend_ma"]]
    slope = series["trend"]["slope_per_week"] if series.get("trend") else None
    projection = series.get("projection")

    goal = None
    if goal_ms is not None:
        today = today_local()
        goal = {
            "target_kg": goal_ms.target_value,
            "eta_date": projection["date"] if projection else None,
            "deadline": goal_ms.deadline.isoformat() if goal_ms.deadline else None,
            "days_left": (goal_ms.deadline - today).days if goal_ms.deadline else None,
        }

    return {
        "latest_kg": latest.weight_kg if latest else None,
        "latest_date": latest.date.isoformat() if latest else None,
        "sparkline": sparkline,
        "slope_kg_per_week": slope,
        "goal": goal,
    }


async def _recovery_block(session: AsyncSession, scope) -> Optional[dict[str, Any]]:
    """The most recent Garmin daily row's recovery numbers — raw values only, so
    the caller renders its own advice/labels from thresholds it owns."""
    from vitals.services import garmin_service

    g = await garmin_service.latest_daily(session, subject_id=scope.subject_id)
    if g is None:
        return None
    return {
        "date": g.date.isoformat(),
        "sleep_score": g.sleep_score,
        "body_battery_high": g.body_battery_high,
        "training_readiness": g.training_readiness,
        "resting_hr": g.resting_hr,
        "hrv_avg": g.hrv_avg,
    }


async def _activity_block(
    session: AsyncSession,
    scope: conflict_engine.ConflictScope,
) -> dict[str, list[str]]:
    """Recent per-domain log dates (last ``_ACTIVITY_WINDOW_DAYS`` days), newest
    first. The caller derives "current streak" from these — a presentation
    concept the caller owns, not a Vitals metric, so only the raw dates cross
    the wire."""
    from vitals.services import garmin_service, nutrition_service, weight_service

    today = today_local()
    since = today - timedelta(days=_ACTIVITY_WINDOW_DAYS - 1)

    weights = await weight_service.list_active_weights(
        session,
        start=since,
        end=today,
        subject_id=scope.subject_id,
    )
    meals = await nutrition_service.list_meals(
        session,
        start=since,
        end=today,
        subject_id=scope.subject_id,
    )
    # Garmin's current compatibility reader has no subject arguments; the
    # summary-level exact-one governance proof remains its read boundary.
    daily = await garmin_service.list_daily(session, limit=_ACTIVITY_WINDOW_DAYS)

    def _dates(rows) -> list[str]:
        seen = {r.date for r in rows}
        return [d.isoformat() for d in sorted(seen, reverse=True)]

    return {
        "weight_days": _dates(weights),
        "nutrition_days": _dates(meals),
        # Garmin rows always carry a real date; keep only days with a recovery signal
        # so an empty ghost sync row doesn't inflate the streak.
        "recovery_days": _dates([g for g in daily if g.sleep_score is not None or g.body_battery_high is not None]),
    }


@router.get("/summary")
async def external_summary(
    session: AsyncSession = Depends(get_session),
    subject_id: uuid.UUID = Depends(resolve_external_caller),
) -> dict[str, Any]:
    """One compact payload for the caller's four health glance cards.

    The subject comes from the credential rather than from ``.env``. That is the
    entire difference: every read below was already scoped, and was scoped to
    whoever the environment named.
    """
    from vitals.services import nutrition_service

    scope = conflict_engine.ConflictScope(
        subject_id=subject_id,
        evaluation_date=today_local(),
        legacy_bridge=conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
    )
    nutrition_today = await nutrition_service.daily_summary(
        session,
        today_local(),
        subject_id=scope.subject_id,
    )

    return {
        "weight": await _weight_block(session, scope),
        "nutrition_today": nutrition_today,
        "recovery": await _recovery_block(session, scope),
        "activity": await _activity_block(session, scope),
    }
