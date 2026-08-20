"""Endpoints for managing weight logs, measurements, noise markers, photos, and
body-composition scans (InBody / МедАсс — the optional ``body_comp`` module)."""
from __future__ import annotations

from datetime import date as date_type
import hashlib
import logging
import os
import uuid
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import (
    Domain,
    FileAssetPurpose,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
)
from vitals.i18n import t
from vitals.integrations.llm_client import LLMClient, LLMNotConfigured
from vitals.services import (
    alerts_service,
    body_scan_service,
    conflict_engine,
    file_asset_service,
    garmin_weight_service,
    raw_payload_service,
    weight_service,
)
from vitals.services.analytics import body_metrics
from vitals.services.conflict_engine import ConflictBlocked
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from vitals.services.upload_ownership_service import require_live_upload_connection
from vitals.utils.timeutils import today_local
from web.deps import get_session, require_auth, require_module
from web.ratelimit import rate_limit
from web.templating import STATIC_DIR, templates
from web.uploads import (
    DOC_EXTS,
    IMAGE_EXTS,
    file_ext,
    legacy_upload_disk_path,
    read_capped,
    validate_extension,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/weight", tags=["weight"])

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
) -> tuple[conflict_engine.ConflictWriteContext, weight_service.PreparedWeightWrite]:
    """Resolve the legacy owner and Garmin destination under one governance lock."""

    conflict_context = await conflict_engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=on_date,
    )
    export_context = await garmin_weight_service.resolve_optional_legacy_export_context(
        db,
        actor_username=username,
    )
    prepared = await weight_service.prepare_weight_write(
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
    conflict_engine.ConflictWriteContext,
    conflict_engine.PreparedConflictWrite,
]:
    """Prepare a subject-scoped measurement/noise write without the outbox lock."""

    context = await conflict_engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=on_date,
    )
    prepared = await conflict_engine.prepare_scoped_write(db, context=context)
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


async def _section_context(
    request: Request, db: AsyncSession, username: str
) -> dict:
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
    else:
        conflict_context, prepared = await _prepare_aux_write(
            db,
            username=username,
            on_date=today,
        )
    identity = conflict_context.identity

    # Refresh noise alerts for today (+ body-scan alerts when the module is on)
    await weight_service.refresh_noise_alert(
        db,
        on_date=today,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    if body_comp_enabled:
        assert prepared_weight_write is not None
        await body_scan_service.refresh_alerts(
            db,
            on_date=today,
            identity=identity,
            include_legacy_unowned=True,
            prepared_weight_write=prepared_weight_write,
        )
    await db.commit()

    # Load data
    weights = await weight_service.list_active_weights(
        db,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
    )
    measurements = await weight_service.list_body_measurements(
        db,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
    )
    noise_markers = await weight_service.list_noise_markers(
        db,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
    )
    photos = await weight_service.list_progress_photos(
        db,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
    )
    alerts = await alerts_service.list_active_scoped(
        db,
        context=alerts_service.HealthAlertContext(identity),
        domain=Domain.WEIGHT,
        legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
    )
    if body_comp_enabled:
        alerts.extend(
            await alerts_service.list_active_scoped(
                db,
                context=alerts_service.HealthAlertContext(identity),
                domain=Domain.BODY_COMPOSITION,
                legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
            )
        )
    series = await weight_service.chart_series(
        db,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
        include_bia=body_comp_enabled,
        include_timeline=timeline_enabled,
    )

    # Body-composition scans + the compact summary chips for the latest one.
    bc_scans = (
        await body_scan_service.list_scans(
            db,
            subject_id=identity.subject_id,
            include_legacy_unowned=True,
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
                bc_headline.append({
                    "key": key,
                    "name": body_metrics.display_name(key, lang) or m.label,
                    "value": m.value,
                    "unit": body_metrics.METRIC_REGISTRY[key].unit or "",
                })

    # Reverse logs list for table view (newest first)
    sorted_weights = sorted(weights, key=lambda w: w.date, reverse=True)
    # Build a unified list of body composition measurements (Navy + InBody scans)
    unified_measures = []
    # Add Navy entries
    for m in measurements:
        unified_measures.append({
            "date": m.date,
            "neck_cm": m.neck_cm,
            "waist_cm": m.waist_cm,
            "hips_cm": m.hips_cm,
            "body_fat_pct": m.body_fat_pct,
            "lbm_kg": m.lbm_kg,
            "source": "navy",
            "source_label": "Navy",
            "note": getattr(m, "note", "") or "",
            "id": m.id
        })

    # Add InBody scan entries if enabled
    if body_comp_enabled:
        for s in bc_scans:
            bf_val = body_metrics.body_fat_pct_from_scan(s.metrics)
            lbm_val = body_metrics.lbm_from_scan(s.metrics)
            if bf_val is not None or lbm_val is not None:
                unified_measures.append({
                    "date": s.date,
                    "neck_cm": None,
                    "waist_cm": None,
                    "hips_cm": None,
                    "body_fat_pct": bf_val,
                    "lbm_kg": lbm_val,
                    "source": "scan",
                    "source_label": s.device or "InBody",
                    "note": getattr(s, "note", "") or "",
                    "id": s.id
                })

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

    # Default today's date for forms
    today_str = today.isoformat()

    return {
        "username": username,
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
        "llm_configured": bool(load_config().openrouter_api_key),
    }


@router.get("", response_class=HTMLResponse)
async def weight_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """The trend: key figures, the chart, the history table and weight entry."""
    return templates.TemplateResponse(
        request, "weight/index.html", await _section_context(request, db, username)
    )


@router.get("/measures", response_class=HTMLResponse)
async def weight_measures(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """The measurements desk: circumferences, BIA scans, photos, noise markers.

    Split out of ``/weight`` (which was six domains stacked in one page) so the
    chart reaches the fold on a laptop and neither page is a place to hunt in.
    """
    return templates.TemplateResponse(
        request, "weight/measures.html", await _section_context(request, db, username)
    )


@router.post("/log")
async def log_weight_entry(
    request: Request,
    id: Optional[int] = Form(None),
    weight_kg: float = Form(...),
    date: str = Form(...),
    note: Optional[str] = Form(None),
    override: bool = Form(False),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Logs or edits a weight, returning 409 JSON on rule violation for override confirmation."""
    on_date = date_type.fromisoformat(date)
    try:
        conflict_context, prepared = await _prepare_weight_write(
            db,
            username=username,
            on_date=on_date,
        )
        if id is not None:
            await weight_service.update_weight_log(
                db,
                log_id=id,
                on_date=on_date,
                weight_kg=weight_kg,
                note=note,
                override=override,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_weight_write=prepared,
            )
        else:
            await weight_service.log_weight(
                db,
                on_date=on_date,
                weight_kg=weight_kg,
                note=note,
                override=override,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_weight_write=prepared,
            )
        await db.commit()
    except ConflictBlocked as e:
        await db.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"violations": [v.to_dict() for v in e.violations]},
        )
    except ValueError as e:
        await db.rollback()
        # The service validates ranges for every caller (MCP included); the form
        # must surface that as a 400, not fall through to a 500.
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"error": str(e)}
        )

    return _back(request)


@router.post("/measurement")
async def log_measurement_entry(
    request: Request,
    id: Optional[int] = Form(None),
    date: str = Form(...),
    neck_cm: Optional[float] = Form(None),
    waist_cm: Optional[float] = Form(None),
    hips_cm: Optional[float] = Form(None),
    note: Optional[str] = Form(None),
    override: bool = Form(False),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Upserts or edits a body measurement log, returning 409 JSON on rule violation."""
    on_date = date_type.fromisoformat(date)
    try:
        conflict_context, prepared = await _prepare_aux_write(
            db,
            username=username,
            on_date=on_date,
        )
        if id is not None:
            # partial=False: this form renders every field it edits and posts
            # them all, so an empty one is the owner deleting a value, not an
            # omission. (A male profile has no hips input — hips_cm is therefore
            # cleared on a web edit, which is moot: nothing sets it for a male
            # profile in the first place.)
            await weight_service.update_body_measurement(
                db,
                measurement_id=id,
                on_date=on_date,
                neck_cm=neck_cm,
                waist_cm=waist_cm,
                hips_cm=hips_cm,
                note=note,
                override=override,
                partial=False,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_conflict_write=prepared,
            )
        else:
            await weight_service.upsert_body_measurement(
                db,
                on_date=on_date,
                neck_cm=neck_cm,
                waist_cm=waist_cm,
                hips_cm=hips_cm,
                note=note,
                source=Source.MANUAL.value,
                override=override,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_conflict_write=prepared,
            )
        await db.commit()
    except ConflictBlocked as e:
        await db.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"violations": [v.to_dict() for v in e.violations]},
        )
    except ValueError as e:
        await db.rollback()
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"error": str(e)}
        )

    return _back(request)


@router.post("/noise")
async def add_noise_entry(
    request: Request,
    start_date: str = Form(...),
    end_date: Optional[str] = Form(None),
    reason: str = Form(...),
    direction: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Exclude a period from calculations to filter out creatine or salt spikes."""
    start = date_type.fromisoformat(start_date)
    end = date_type.fromisoformat(end_date) if end_date else None
    # Normalise: empty string → None
    dir_value = direction.strip() if direction and direction.strip() else None

    try:
        conflict_context, prepared = await _prepare_aux_write(
            db,
            username=username,
            on_date=today_local(),
        )
        await weight_service.add_noise_marker(
            db,
            start_date=start,
            end_date=end,
            reason=reason,
            direction=dir_value,
            source=Source.MANUAL.value,
            identity=conflict_context.identity,
            include_legacy_unowned=True,
            prepared_conflict_write=prepared,
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": str(exc)},
        )

    return _back(request)


@router.post("/photo")
async def add_photo_entry(
    request: Request,
    date: str = Form(...),
    note: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    files: Optional[list[UploadFile]] = File(None),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Saves up to 5 daily progress photos to static/uploads/ and references them in the DB."""
    on_date = date_type.fromisoformat(date)

    # Gather all uploaded files from both "file" (single-field tests) and "files" (multiple files input)
    uploaded_files: list[UploadFile] = []
    if file is not None and file.filename:
        uploaded_files.append(file)
    if files is not None:
        for f in files:
            if f.filename:
                uploaded_files.append(f)

    if not uploaded_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("weight.error.no_files")
        )

    if len(uploaded_files) > 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("weight.error.too_many_files")
        )

    written_paths: list[str] = []
    prepared_files: list[tuple[str, str | None, bytes]] = []
    try:
        for f in uploaded_files:
            file_extension = validate_extension(f.filename, IMAGE_EXTS)
            contents = await read_capped(f)
            unique_filename = f"{uuid.uuid4().hex}{file_extension}"
            file_key = f"uploads/{unique_filename}"
            file_path = legacy_upload_disk_path(STATIC_DIR, file_key)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            written_paths.append(file_path)

            with open(file_path, "wb") as buffer:
                buffer.write(contents)

            prepared_files.append((file_key, f.content_type or None, contents))

        conflict_context, prepared = await _prepare_aux_write(
            db,
            username=username,
            on_date=on_date,
        )
        identity = conflict_context.identity
        for file_key, content_type, contents in prepared_files:
            asset = await file_asset_service.register_legacy_local(
                db,
                subject_id=identity.subject_id,
                uploaded_by_user_id=identity.actor_user_id,
                purpose=FileAssetPurpose.PROGRESS_PHOTO,
                storage_ref=file_key,
                media_type=content_type,
                size_bytes=len(contents),
                content_sha256=hashlib.sha256(contents).hexdigest(),
            )
            await weight_service.add_progress_photo(
                db,
                on_date=on_date,
                file_key=file_key,
                note=note,
                identity=identity,
                file_asset_id=asset.id,
                prepared_conflict_write=prepared,
            )

    except BaseException:
        try:
            await db.rollback()
        except BaseException:
            logger.exception("Could not roll back failed progress-photo transaction")
        for path in written_paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("Could not clean up failed progress upload %s: %s", path, exc)
        raise

    try:
        await db.commit()
    except BaseException:
        # COMMIT can be ambiguous (server committed, client lost the response or
        # the coroutine was cancelled). Deleting bytes here could turn a
        # committed metadata row into permanent data loss. Preserve the files
        # for operator reconciliation and only roll the local session back.
        try:
            await db.rollback()
        except BaseException:
            logger.exception("Could not reset session after progress-photo commit failure")
        logger.exception(
            "Progress-photo commit outcome is ambiguous; preserved %d upload file(s)",
            len(written_paths),
        )
        raise

    return _back(request)


# ── Body composition (InBody / МедАсс) — optional module ──────────────────────
class BodyScanMetricIn(BaseModel):
    metric_key: Optional[str] = None
    label: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    ref_low: Optional[float] = None
    ref_high: Optional[float] = None
    segment: Optional[str] = None
    category: Optional[str] = None


class BodyScanConfirm(BaseModel):
    date: str
    device: Optional[str] = None
    file_key: Optional[str] = None
    raw_payload_id: Optional[int] = None
    note: Optional[str] = None
    override: bool = False
    metrics: list[BodyScanMetricIn] = []


@router.post("/body-scan/upload")
async def body_scan_upload(
    request: Request,
    file: UploadFile = File(...),
    date: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
    _gate: None = Depends(require_module("body_comp")),
    _rl: None = Depends(rate_limit("body_scan_upload", limit=20, window=60)),
):
    """Step 1: a photo/PDF of a scan sheet → vision extraction → editable preview.

    The original file + verbatim vision payload are stored now (data-lake); the
    normalized ``BodyScan`` rows are only written on confirm, with the owner's
    edits. Returns JSON the client renders as an editable table."""
    from vitals.utils.timeutils import today_local

    # Admit the upload only through a live AI-gateway root, then release every
    # database lock before file IO and the external extraction await. The durable
    # write transaction below resolves and validates the roots again.
    preflight_ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
        required_connections=(IntegrationProvider.OPENROUTER,),
    )
    await require_live_upload_connection(
        db,
        identity=preflight_ownership.owner_action(),
        connection_id=preflight_ownership.connection_id(
            IntegrationProvider.OPENROUTER
        ),
        provider=IntegrationProvider.OPENROUTER,
        connection_type=IntegrationConnectionType.AI_GATEWAY,
    )
    await db.rollback()

    # 415/413 surface as HTTP errors (handled by the client's error branch).
    validate_extension(file.filename, DOC_EXTS)
    contents = await read_capped(file)

    try:
        llm = LLMClient()
    except LLMNotConfigured:
        return JSONResponse({"ok": False, "reason": "not_configured", "message": t("body.not_configured")})

    try:
        extracted = await body_scan_service.extract_from_file(
            contents,
            llm=llm,
            content_type=file.content_type or "image/jpeg",
            filename=file.filename,
        )
    except LLMNotConfigured:
        return JSONResponse({"ok": False, "reason": "not_configured", "message": t("body.not_configured")})
    except Exception as e:  # noqa: BLE001 — surface parse failures softly
        logger.warning("Body-scan extraction failed for %s: %s", file.filename, e)
        return JSONResponse({"ok": False, "reason": "error", "message": t("body.upload.error")})

    # Persist the original sheet image for reference (served at /static/uploads/...).
    # Written only once extraction succeeded — see the labs upload for why.
    ext = file_ext(file.filename) or ".bin"
    file_key = f"body/{uuid.uuid4().hex}{ext}"
    file_path = legacy_upload_disk_path(STATIC_DIR, file_key)
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    try:
        with open(file_path, "wb") as fh:
            fh.write(contents)
        ownership = await resolve_legacy_ownership_context(
            db,
            actor_username=username,
            required_connections=(IntegrationProvider.OPENROUTER,),
        )
        identity = ownership.owner_action()
        if identity != preflight_ownership.owner_action():
            raise ValueError("body-scan upload identity changed during extraction")
        openrouter_connection_id = ownership.connection_id(
            IntegrationProvider.OPENROUTER
        )
        await require_live_upload_connection(
            db,
            identity=identity,
            connection_id=openrouter_connection_id,
            provider=IntegrationProvider.OPENROUTER,
            connection_type=IntegrationConnectionType.AI_GATEWAY,
        )
        asset = await file_asset_service.register_legacy_local(
            db,
            subject_id=identity.subject_id,
            uploaded_by_user_id=identity.actor_user_id,
            purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT,
            storage_ref=file_key,
            media_type=file.content_type or None,
            size_bytes=len(contents),
            content_sha256=hashlib.sha256(contents).hexdigest(),
        )
        raw_row = await raw_payload_service.upsert_owned_raw_payload(
            db,
            identity=identity,
            integration_connection_id=openrouter_connection_id,
            file_asset_id=asset.id,
            domain=Domain.BODY_COMPOSITION.value,
            source=Source.BODY_SCAN.value,
            external_id=file_key,
            payload=extracted,
        )
    except BaseException:
        try:
            await db.rollback()
        except BaseException:
            logger.exception("Could not roll back failed body-upload transaction")
        try:
            os.remove(file_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not clean up failed body upload %s: %s", file_path, exc)
        raise

    try:
        await db.commit()
    except BaseException:
        try:
            await db.rollback()
        except BaseException:
            logger.exception("Could not reset session after body-upload commit failure")
        logger.exception(
            "Body-upload commit outcome is ambiguous; preserved uploaded bytes"
        )
        raise

    rows = body_scan_service.normalize_extracted(extracted)
    raw_date = date or extracted.get("date")
    try:
        scan_date = date_type.fromisoformat(str(raw_date)[:10]).isoformat()
    except (ValueError, TypeError):
        scan_date = today_local().isoformat()

    return JSONResponse({
        "ok": True,
        "scan": {
            "date": scan_date,
            "device": extracted.get("device"),
            "file_key": file_key,
            "raw_payload_id": raw_row.id,
            "metrics": rows,
        },
    })


@router.post("/body-scan/confirm")
async def body_scan_confirm(
    request: Request,
    payload: BodyScanConfirm,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
    _gate: None = Depends(require_module("body_comp")),
):
    """Step 2: persist the owner-edited scan rows. 409 + violations on a block."""
    try:
        on_date = date_type.fromisoformat(payload.date)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid date")

    conflict_context, prepared_weight_write = await _prepare_weight_write(
        db,
        username=username,
        on_date=on_date,
    )
    identity = conflict_context.identity
    try:
        await body_scan_service.save_scan(
            db,
            on_date=on_date,
            device=payload.device,
            file_key=payload.file_key,
            raw_payload_id=payload.raw_payload_id,
            metrics=[m.model_dump() for m in payload.metrics],
            note=payload.note,
            override=payload.override,
            identity=identity,
            include_legacy_unowned=True,
            prepared_weight_write=prepared_weight_write,
        )
        await body_scan_service.refresh_alerts(
            db,
            on_date=on_date,
            identity=identity,
            include_legacy_unowned=True,
            prepared_weight_write=prepared_weight_write,
        )
        await db.commit()
    except ConflictBlocked as e:
        await db.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"violations": [v.to_dict() for v in e.violations]},
        )
    except ValueError as e:
        await db.rollback()
        # A scan bridges its weight into the weight domain, which validates it.
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"error": str(e)}
        )
    return JSONResponse({"ok": True})


@router.post("/body-scan/{scan_id}/delete")
async def delete_body_scan_entry(
    request: Request,
    scan_id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
    _gate: None = Depends(require_module("body_comp")),
):
    operation_date = today_local()
    conflict_context, prepared_weight_write = await _prepare_weight_write(
        db,
        username=username,
        on_date=operation_date,
    )
    identity = conflict_context.identity
    scan = await body_scan_service.get_scan(
        db,
        scan_id,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
    )
    if scan is None:
        return _back(request)

    file_key = scan.file_key if scan is not None else None
    file_asset_id = scan.file_asset_id if scan is not None else None
    deleted = await body_scan_service.delete_scan(
        db,
        scan_id,
        identity=identity,
        include_legacy_unowned=True,
        prepared_weight_write=prepared_weight_write,
    )
    if deleted:
        await body_scan_service.refresh_alerts(
            db,
            on_date=operation_date,
            identity=identity,
            include_legacy_unowned=True,
            prepared_weight_write=prepared_weight_write,
        )
    if deleted and file_asset_id is not None:
        await file_asset_service.mark_legacy_local_deleted(
            db,
            file_asset_id=file_asset_id,
            subject_id=identity.subject_id,
            purged=False,
        )
    await db.commit()

    bytes_purged = False
    if deleted and file_key:
        try:
            file_path = legacy_upload_disk_path(STATIC_DIR, file_key)
            if os.path.exists(file_path):
                os.remove(file_path)
            bytes_purged = True
        except (OSError, ValueError) as e:
            logger.warning("Could not remove scan file %s: %s", file_key, e)

    if bytes_purged and file_asset_id is not None:
        await file_asset_service.mark_legacy_local_deleted(
            db,
            file_asset_id=file_asset_id,
            subject_id=identity.subject_id,
            purged=True,
        )
        await db.commit()

    return _back(request)


@router.post("/log/{id}/delete")
async def delete_weight_entry(
    request: Request,
    id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    from vitals.utils.timeutils import today_local

    conflict_context, prepared = await _prepare_weight_write(
        db,
        username=username,
        on_date=today_local(),
    )
    await weight_service.delete_weight_log(
        db,
        id,
        identity=conflict_context.identity,
        include_legacy_unowned=True,
        prepared_weight_write=prepared,
    )
    await db.commit()

    return _back(request)


@router.post("/measurement/{id}/delete")
async def delete_measurement_entry(
    request: Request,
    id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    conflict_context, prepared = await _prepare_aux_write(
        db,
        username=username,
        on_date=today_local(),
    )
    await weight_service.delete_body_measurement(
        db,
        id,
        identity=conflict_context.identity,
        include_legacy_unowned=True,
        prepared_conflict_write=prepared,
    )
    await db.commit()

    return _back(request)


@router.post("/noise/{id}/delete")
async def delete_noise_marker_entry(
    request: Request,
    id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    conflict_context, prepared = await _prepare_aux_write(
        db,
        username=username,
        on_date=today_local(),
    )
    await weight_service.delete_noise_marker(
        db,
        id,
        identity=conflict_context.identity,
        include_legacy_unowned=True,
        prepared_conflict_write=prepared,
    )
    await db.commit()

    return _back(request)


@router.post("/photo/delete")
async def delete_photo_entry(
    request: Request,
    id: int = Form(...),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    today = today_local()
    conflict_context, prepared = await _prepare_aux_write(
        db,
        username=username,
        on_date=today,
    )
    receipt = await weight_service.delete_progress_photo(
        db,
        id,
        identity=conflict_context.identity,
        include_legacy_unowned=True,
        prepared_conflict_write=prepared,
    )
    await db.commit()

    bytes_purged = False
    if receipt is not None:
        try:
            file_path = legacy_upload_disk_path(STATIC_DIR, receipt.file_key)
            if os.path.exists(file_path):
                os.remove(file_path)
            bytes_purged = not os.path.exists(file_path)
        except (OSError, ValueError) as e:
            logger.warning(
                "Could not remove progress photo %s: %s",
                receipt.file_key,
                e,
            )

    if (
        bytes_purged
        and receipt is not None
        and receipt.file_asset_id is not None
    ):
        purge_context, _purge_prepared = await _prepare_aux_write(
            db,
            username=username,
            on_date=today,
        )
        if purge_context.identity != conflict_context.identity:
            raise weight_service.ProgressPhotoOwnershipError(
                "progress-photo identity changed during physical purge"
            )
        await file_asset_service.mark_legacy_local_deleted(
            db,
            file_asset_id=receipt.file_asset_id,
            subject_id=purge_context.identity.subject_id,
            purged=True,
        )
        await db.commit()

    return _back(request)
