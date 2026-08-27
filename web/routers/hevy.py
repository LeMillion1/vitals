"""Endpoints for the Hevy workouts module: dashboard, manual sync, per-exercise
history + progression."""
from __future__ import annotations

from vitals.services.alerts import contracts as alerts_service_contracts
from vitals.services.alerts import lifecycle as alerts_service_lifecycle

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, IntegrationProvider, Severity
from vitals.integrations.hevy_client import HevyAPIError, HevyClient, HevyNotConfigured
from vitals.services.alerts import legacy_subject as legacy_subject_alerts
from vitals.services.credentials import providers
from vitals.services.hevy import ownership as hevy_ownership
import vitals.services.hevy.queries as hevy_queries
import vitals.services.hevy.sync as hevy_sync
from vitals.services.tenancy.ownership import resolve_legacy_ownership_context
from web.deps import get_redis, get_session, require_auth
from web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hevy", tags=["hevy"])

SYNC_ALERT_KEY = "hevy.sync_failed"


@router.get("", response_class=HTMLResponse)
async def hevy_dashboard(
    request: Request,
    ex: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
    redis = Depends(get_redis),
    username: str = Depends(require_auth),
):
    """Workouts dashboard: recent sessions, exercise catalog, and — when an
    exercise is selected (``?ex=<template_id>``) — its working-weight history +
    progression verdict."""
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
        required_connections=tuple(IntegrationProvider),
    )
    workouts = await hevy_queries.list_workouts(
        db, subject_id=ownership.subject_id, limit=30
    )
    catalog = await hevy_queries.exercise_catalog(
        db, subject_id=ownership.subject_id
    )
    count = await hevy_queries.workout_count(
        db, subject_id=ownership.subject_id
    )
    last_date = await hevy_queries.latest_workout_date(
        db, subject_id=ownership.subject_id
    )
    alerts = await legacy_subject_alerts.list_active(
        db,
        ownership=ownership,
        domain=Domain.WORKOUTS,
    )

    # Default the selected exercise to the most recently trained one.
    selected = ex or (catalog[0]["exercise_template_id"] if catalog else None)
    series: list = []
    verdict = None
    notes = None
    selected_title = None
    if selected:
        series = await hevy_queries.working_weight_series(
            db, selected, subject_id=ownership.subject_id
        )
        verdict = await hevy_queries.progression_for_exercise(
            db, selected, subject_id=ownership.subject_id
        )
        notes = await hevy_queries.latest_notes(
            db, selected, subject_id=ownership.subject_id
        )
        selected_title = next(
            (c["title"] for c in catalog if c["exercise_template_id"] == selected), selected
        )

    # This subject's Hevy account, not the process's — see the same change in
    # the Garmin dashboard.
    account = await providers.resolve_hevy_account(
        db, subject_id=ownership.subject_id
    )
    is_configured = bool(account and account.configured)
    namespace = account.namespace if account else ""

    last_sync = None
    last_sync_raw = await redis.get(
        providers.sync_marker_key(
            IntegrationProvider.HEVY, namespace
        )
    )
    if last_sync_raw:
        try:
            from datetime import datetime, timezone
            from vitals.utils.timeutils import to_local_naive
            dt = datetime.fromtimestamp(int(last_sync_raw), timezone.utc)
            local_dt = to_local_naive(dt)
            if local_dt:
                last_sync = local_dt.strftime("%d-%m-%Y %H:%M")
        except Exception:
            pass

    return templates.TemplateResponse(
        request,
        "hevy/index.html",
        {
            "username": username,
            "workouts": workouts,
            "catalog": catalog,
            "count": count,
            "last_date": last_date.isoformat() if last_date else None,
            "alerts": alerts,
            "selected": selected,
            "selected_title": selected_title,
            "series": {"points": series},
            "verdict": verdict,
            "notes": notes,
            "is_configured": is_configured,
            "last_sync": last_sync,
            "sync": request.query_params.get("sync"),
            "synced": request.query_params.get("synced"),
        },
    )


@router.post("/sync")
async def sync_now(
    request: Request,
    db: AsyncSession = Depends(get_session),
    redis = Depends(get_redis),
    username: str = Depends(require_auth),
):
    """Pull the latest workouts from Hevy on demand. Failures surface as a passive
    ``warn`` alert (never a hard error) so the page still renders."""
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
        required_connections=(IntegrationProvider.HEVY,),
    )
    account = await providers.resolve_hevy_account(
        db, subject_id=ownership.subject_id
    )
    if account is None or not account.configured:
        return _redirect(request, "?sync=not_configured")
    client = HevyClient.from_config(account.config)
    alert_context = alerts_service_contracts.ProviderAlertContext(
        identity=ownership.system_action(),
        provider=IntegrationProvider.HEVY,
        integration_connection_id=ownership.connection_id(
            IntegrationProvider.HEVY
        ),
    )
    try:
        summary = await hevy_sync.sync_owned(
            db,
            client,
            identity=ownership.owner_action(),
            integration_connection_id=ownership.connection_id(
                IntegrationProvider.HEVY
            ),
        )
        await alerts_service_lifecycle.resolve_scoped_by_key(
            db,
            context=alert_context,
            alert_key=SYNC_ALERT_KEY,
            legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
        )
        await db.commit()

        import time
        await redis.set(
            providers.sync_marker_key(
                IntegrationProvider.HEVY, account.namespace
            ),
            str(int(time.time())),
        )
    except hevy_ownership.HevyOwnershipInactiveConnectionError:
        await db.rollback()
        return _redirect(request, "?sync=not_configured")
    except (HevyNotConfigured, HevyAPIError) as e:
        logger.warning("Hevy sync failed: %s", e)
        await alerts_service_lifecycle.raise_scoped_alert(
            db,
            context=alert_context,
            domain=Domain.WORKOUTS,
            severity=Severity.WARN,
            message=f"Не удалось синхронизировать Hevy: {e}",
            alert_key=SYNC_ALERT_KEY,
            legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
        )
        await db.commit()
        return _redirect(request, "?sync=error")

    created = summary["created"] + summary["updated"]
    return _redirect(request, f"?sync=ok&synced={created}")


def _redirect(request: Request, suffix: str = "") -> RedirectResponse:
    url = f"/hevy{suffix}"
    response = RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    if "hx-request" in request.headers:
        response.headers["HX-Redirect"] = url
    return response
