"""Shared HTTP orchestration for the Weight route leaves."""

from __future__ import annotations

from vitals.services.alerts import lifecycle as alerts_service_lifecycle

from vitals.services.alerts import contracts as alerts_service_contracts

from datetime import date as date_type
import logging
from urllib.parse import urlsplit

from fastapi import Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import (
    Domain,
)
from vitals.services.files import queries as file_queries
from vitals.services.garmin_weight import outbox as garmin_weight_outbox
from vitals.services.conflicts import engine
from vitals.services.body_scan.ai import projection as body_scan_ai_projection
from vitals.services.body_scan.scans import alerts as body_scan_alerts
from vitals.services.body_scan.scans import queries as body_scan_queries
from vitals.services.weight import alerts as weight_alerts
from vitals.services.weight import analytics as weight_analytics
from vitals.services.weight import governance as weight_governance
from vitals.services.weight import logs as weight_logs
from vitals.services.weight import measurements as weight_measurements
from vitals.services.weight import noise as weight_noise
from vitals.services.weight import photos as weight_photos
from vitals.services.weight.contracts import (
    PreparedWeightWrite,
)
from vitals.analytics import body_metrics
from vitals.utils.timeutils import today_local
from web.templating import STATIC_DIR

logger = logging.getLogger(__name__)

# The pages that post here — the only redirect targets ``_back`` will honour.
# ``/today`` is on the list because its quick-log card posts to /weight/log too,
# and a save made there must land back on Today rather than bounce the owner
# into the weight section.
SECTION_PAGES = ("/weight", "/weight/measures", "/today")

# Render order of metric categories in the body-composition detail view.
BODY_CAT_ORDER = ["composition", "water", "segmental", "score", "derived", "other"]


async def _prepare_weight_write(
    db: AsyncSession,
    *,
    username: str,
    on_date: date_type,
) -> tuple[engine.ConflictWriteContext, PreparedWeightWrite]:
    """Resolve the legacy owner and Garmin destination under one governance lock."""

    conflict_context = await engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=on_date,
    )
    export_context = await garmin_weight_outbox.resolve_optional_legacy_export_context(
        db,
        actor_username=username,
    )
    prepared = await weight_governance.prepare_weight_write(
        db,
        context=conflict_context,
        garmin_weight_export_context=export_context,
    )
    return conflict_context, prepared


async def _prepare_aux_write(
    db: AsyncSession,
    *,
    username: str,
    on_date: date_type,
) -> tuple[
    engine.ConflictWriteContext,
    engine.PreparedConflictWrite,
]:
    """Prepare a subject-scoped measurement/noise write without the outbox lock."""

    context = await engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=on_date,
    )
    prepared = await engine.prepare_scoped_write(db, context=context)
    return context, prepared


def _back(request: Request, default: str = "/weight"):
    """POST → 303 back to the page the form was posted from.

    Every handler below used to hardcode ``/weight``. The section now has two
    pages — the trend (``/weight``) and the measurements desk
    (``/weight/measures``) — and which one a form belongs to is only knowable
    from where it was rendered, so the Referer decides. It has to be *our own*
    Referer naming one of this section's own pages; anything else (cross-site,
    stale, absent) falls back to ``default``, so the header can never steer the
    save anywhere the app did not already choose to go.
    """
    referer = urlsplit(request.headers.get("referer", ""))
    same_site = referer.netloc == urlsplit(str(request.base_url)).netloc
    url = referer.path if same_site and referer.path in SECTION_PAGES else default
    response = RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    if "hx-request" in request.headers:
        response.headers["HX-Redirect"] = url
    return response


async def _section_context(request: Request, db: AsyncSession, username: str) -> dict:
    """Everything both weight pages render.

    One loader for two templates on purpose: the masthead key figures (latest,
    7-day mean, body fat, weekly delta) are identical on both, and computing
    them twice is how the two headers would drift apart.
    """
    # Is the optional body-composition module on? Gates the tab, the BIA chart
    # overlay, and the scan section — disabled behaves as if it isn't there.
    em = getattr(request.state, "enabled_modules", None) or {}
    body_comp_enabled = bool(em.get("body_comp"))
    timeline_enabled = bool(em.get("timeline"))

    today = today_local()
    prepared_weight_write = None
    if body_comp_enabled:
        conflict_context, prepared_weight_write = await _prepare_weight_write(
            db,
            username=username,
            on_date=today,
        )
        prepared = prepared_weight_write.conflict_write
        body_ai_availability = await body_scan_ai_projection.project_body_scan_ai_availability(
            db,
            actor_username=username,
        )
    else:
        conflict_context, prepared = await _prepare_aux_write(
            db,
            username=username,
            on_date=today,
        )
        body_ai_availability = None
    identity = conflict_context.identity

    # Refresh noise alerts for today (+ body-scan alerts when the module is on)
    await weight_alerts.refresh_noise_alert(
        db,
        on_date=today,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    if body_comp_enabled:
        assert prepared_weight_write is not None
        await body_scan_alerts.refresh_alerts(
            db,
            subject_id=identity.subject_id,
            on_date=today,
            identity=identity,
            prepared_weight_write=prepared_weight_write,
        )
    await db.commit()

    # Load data
    weights = await weight_logs.list_active_weights(
        db,
        subject_id=identity.subject_id,
    )
    measurements = await weight_measurements.list_body_measurements(
        db,
        subject_id=identity.subject_id,
    )
    noise_markers = await weight_noise.list_noise_markers(
        db,
        subject_id=identity.subject_id,
    )
    photos = await weight_photos.list_progress_photos(
        db,
        subject_id=identity.subject_id,
    )
    alerts = await alerts_service_lifecycle.list_active_scoped(
        db,
        context=alerts_service_contracts.HealthAlertContext(identity),
        domain=Domain.WEIGHT,
        legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
    )
    if body_comp_enabled:
        alerts.extend(
            await alerts_service_lifecycle.list_active_scoped(
                db,
                context=alerts_service_contracts.HealthAlertContext(identity),
                domain=Domain.BODY_COMPOSITION,
                legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
            )
        )
    series = await weight_analytics.chart_series(
        db,
        subject_id=identity.subject_id,
        include_bia=body_comp_enabled,
        include_timeline=timeline_enabled,
    )

    # Body-composition scans + the compact summary chips for the latest one.
    bc_scans = (
        await body_scan_queries.list_scans(
            db,
            subject_id=identity.subject_id,
        )
        if body_comp_enabled
        else []
    )
    bc_latest = bc_scans[0] if bc_scans else None
    lang = getattr(request.state, "lang", "ru")
    bc_headline = []
    if bc_latest is not None:
        by_key: dict = {}
        for m in bc_latest.metrics:
            by_key.setdefault(m.metric_key, m)
        for key in body_metrics.HEADLINE_KEYS:
            m = by_key.get(key)
            if m is not None:
                bc_headline.append(
                    {
                        "key": key,
                        "name": body_metrics.display_name(key, lang) or m.label,
                        "value": m.value,
                        "unit": body_metrics.METRIC_REGISTRY[key].unit or "",
                    }
                )

    # Reverse logs list for table view (newest first)
    sorted_weights = sorted(weights, key=lambda w: w.date, reverse=True)
    # Build a unified list of body composition measurements (Navy + InBody scans)
    unified_measures = []
    # Add Navy entries
    for m in measurements:
        unified_measures.append(
            {
                "date": m.date,
                "neck_cm": m.neck_cm,
                "waist_cm": m.waist_cm,
                "hips_cm": m.hips_cm,
                "body_fat_pct": m.body_fat_pct,
                "lbm_kg": m.lbm_kg,
                "source": "navy",
                "source_label": "Navy",
                "note": getattr(m, "note", "") or "",
                "id": m.id,
            }
        )

    # Add InBody scan entries if enabled
    if body_comp_enabled:
        for s in bc_scans:
            bf_val = body_metrics.body_fat_pct_from_scan(s.metrics)
            lbm_val = body_metrics.lbm_from_scan(s.metrics)
            if bf_val is not None or lbm_val is not None:
                unified_measures.append(
                    {
                        "date": s.date,
                        "neck_cm": None,
                        "waist_cm": None,
                        "hips_cm": None,
                        "body_fat_pct": bf_val,
                        "lbm_kg": lbm_val,
                        "source": "scan",
                        "source_label": s.device or "InBody",
                        "note": getattr(s, "note", "") or "",
                        "id": s.id,
                    }
                )

    # Sort unified measurements (newest first)
    sorted_measurements = sorted(unified_measures, key=lambda x: x["date"], reverse=True)

    # Top "body fat %" card: a BIA scan is a direct measurement, Navy is a
    # tape-measure estimate — so whenever a scan exists it wins, even if a Navy
    # row happens to be newer. Within a source, newest row that actually carries
    # a body-fat value (rows can be partial). The card labels its own source.
    with_bf = [m for m in sorted_measurements if m["body_fat_pct"] is not None]
    latest_bf_row = next(
        (m for m in with_bf if m["source"] == "scan"),
        next(iter(with_bf), None),
    )
    latest_bf = latest_bf_row["body_fat_pct"] if latest_bf_row else None
    latest_bf_source = latest_bf_row["source_label"] if latest_bf_row else None

    # Download URLs for everything on this page, resolved in one query. The
    # rows carry an asset id; the link needs the rotatable key, and an id with
    # no entry here is deleted, purged, or not this subject's — in every case
    # the template renders no link rather than a link that 404s.
    file_keys = await file_queries.opaque_keys_for(
        db,
        subject_id=identity.subject_id,
        file_asset_ids=[p.file_asset_id for p in photos] + [s.file_asset_id for s in bc_scans],
    )

    # Default today's date for forms
    today_str = today.isoformat()

    return {
        "username": username,
        "file_keys": file_keys,
        "weights": sorted_weights,
        "measurements": sorted_measurements,
        "latest_bf": latest_bf,
        "latest_bf_source": latest_bf_source,
        "noise_markers": noise_markers,
        "photos": photos,
        "alerts": alerts,
        "series": series,
        "today": today_str,
        "sex": load_config().sex,
        # Body composition (optional module)
        "body_comp_enabled": body_comp_enabled,
        "bc_scans": bc_scans,
        "bc_latest": bc_latest,
        "bc_headline": bc_headline,
        "bc_cat_order": BODY_CAT_ORDER,
        "body_ai_ready": bool(body_ai_availability is not None and body_ai_availability.available),
        "body_ai_availability_code": (
            body_ai_availability.code.value
            if body_ai_availability is not None
            else "not_configured"
        ),
    }


__all__ = [
    "BODY_CAT_ORDER",
    "SECTION_PAGES",
    "STATIC_DIR",
    "_back",
    "_prepare_aux_write",
    "_prepare_weight_write",
    "_section_context",
]
