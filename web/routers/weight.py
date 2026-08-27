"""Endpoints for managing weight logs, measurements, noise markers, photos, and
body-composition scans (InBody / МедАсс — the optional ``body_comp`` module)."""
from __future__ import annotations

from datetime import date as date_type
import logging
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from vitals.config import load_config
from vitals.enums import (
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    FileStorageBackend,
    Source,
)
from vitals.i18n import t
from vitals.services import (
    ai_gateway_service,
    alerts_service,
    conflict_engine,
    file_asset_service,
    garmin_weight_service,
    weight_service,
)
from vitals.services.body_scan import ai as body_scan_ai
from vitals.services.body_scan import scans
from vitals.analytics import body_metrics
from vitals.services.conflict_engine import ConflictBlocked
from vitals.utils.timeutils import today_local
from web.config import get_web_config
from web.deps import get_session, require_auth, require_module
from web.ratelimit import rate_limit
from web.templating import STATIC_DIR, templates
from web.uploads import (
    IMAGE_EXTS,
    PreparedMedicalDocument,
    prepare_medical_document,
    private_storage_ref,
    remove_stored_file,
    write_private_file,
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
        body_ai_availability = (
            await body_scan_ai.project_body_scan_ai_availability(
                db,
                actor_username=username,
            )
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
    await weight_service.refresh_noise_alert(
        db,
        on_date=today,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    if body_comp_enabled:
        assert prepared_weight_write is not None
        await scans.refresh_alerts(
            db,
            subject_id=identity.subject_id,
            on_date=today,
            identity=identity,
            prepared_weight_write=prepared_weight_write,
        )
    await db.commit()

    # Load data
    weights = await weight_service.list_active_weights(
        db,
        subject_id=identity.subject_id,
    )
    measurements = await weight_service.list_body_measurements(
        db,
        subject_id=identity.subject_id,
    )
    noise_markers = await weight_service.list_noise_markers(
        db,
        subject_id=identity.subject_id,
    )
    photos = await weight_service.list_progress_photos(
        db,
        subject_id=identity.subject_id,
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
        include_bia=body_comp_enabled,
        include_timeline=timeline_enabled,
    )

    # Body-composition scans + the compact summary chips for the latest one.
    bc_scans = (
        await scans.list_scans(
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

    # Download URLs for everything on this page, resolved in one query. The
    # rows carry an asset id; the link needs the rotatable key, and an id with
    # no entry here is deleted, purged, or not this subject's — in every case
    # the template renders no link rather than a link that 404s.
    file_keys = await file_asset_service.opaque_keys_for(
        db,
        subject_id=identity.subject_id,
        file_asset_ids=[p.file_asset_id for p in photos]
        + [s.file_asset_id for s in bc_scans],
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
        "body_ai_ready": bool(
            body_ai_availability is not None
            and body_ai_availability.available
        ),
        "body_ai_availability_code": (
            body_ai_availability.code.value
            if body_ai_availability is not None
            else "not_configured"
        ),
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
    """Save up to five daily progress photos in the private file root."""
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

    written_refs: list[str] = []
    prepared_files: list[tuple[str, PreparedMedicalDocument]] = []
    try:
        for f in uploaded_files:
            document = await prepare_medical_document(
                f,
                allowed_extensions=IMAGE_EXTS,
            )
            assert document is not None  # The collection excludes blank fields.
            file_key = private_storage_ref(
                FileAssetPurpose.PROGRESS_PHOTO,
                document.extension,
            )
            await run_in_threadpool(
                write_private_file,
                get_web_config().private_file_root,
                file_key,
                document.body,
            )
            written_refs.append(file_key)

            prepared_files.append((file_key, document))

        conflict_context, prepared = await _prepare_aux_write(
            db,
            username=username,
            on_date=on_date,
        )
        identity = conflict_context.identity
        for file_key, document in prepared_files:
            asset = await file_asset_service.register_private_local(
                db,
                subject_id=identity.subject_id,
                uploaded_by_user_id=identity.actor_user_id,
                purpose=FileAssetPurpose.PROGRESS_PHOTO,
                storage_ref=file_key,
                media_type=document.media_type,
                size_bytes=document.byte_size,
                content_sha256=document.sha256_hex,
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
        for storage_ref in written_refs:
            try:
                await run_in_threadpool(
                    remove_stored_file,
                    storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
                    storage_ref=storage_ref,
                    static_dir=STATIC_DIR,
                    private_root=get_web_config().private_file_root,
                )
            except OSError as exc:
                logger.warning("Could not clean up failed progress upload: %s", exc)
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
            len(written_refs),
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
    model_config = ConfigDict(extra="forbid")

    date: str
    device: Optional[str] = None
    raw_payload_id: Optional[int] = None
    note: Optional[str] = None
    override: bool = False
    metrics: list[BodyScanMetricIn] = []


async def _cleanup_uncommitted_body_upload(
    db: AsyncSession,
    *,
    storage_ref: str,
    file_written: bool,
) -> None:
    """Best-effort rollback and byte cleanup before the first durable commit."""

    try:
        await db.rollback()
    except BaseException:
        logger.exception("Could not roll back failed body-upload transaction")
    if file_written:
        try:
            await run_in_threadpool(
                remove_stored_file,
                storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
                storage_ref=storage_ref,
                static_dir=STATIC_DIR,
                private_root=get_web_config().private_file_root,
            )
        except OSError as exc:
            logger.warning("Could not clean up failed body upload: %s", exc)


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

    # 415/413 surface as HTTP errors (handled by the client's error branch).
    document = await prepare_medical_document(file)
    assert document is not None  # FastAPI requires this upload field.
    ext = document.extension
    contents = document.body
    file_key = private_storage_ref(FileAssetPurpose.BODY_SCAN_DOCUMENT, ext)
    file_written = False
    prepared = None
    try:
        await run_in_threadpool(
            write_private_file,
            get_web_config().private_file_root,
            file_key,
            contents,
        )
        file_written = True
        prepared = await body_scan_ai.prepare_body_scan_parse(
            db,
            actor_username=username,
            storage_ref=file_key,
            media_type=document.media_type,
            byte_size=document.byte_size,
            sha256_hex=document.sha256_hex,
            storage_backend=FileStorageBackend.PRIVATE_LOCAL,
        )
    except (
        ai_gateway_service.AIGatewayConfigurationError,
        ai_gateway_service.AIQuotaExceededError,
    ) as exc:
        await _cleanup_uncommitted_body_upload(
            db,
            storage_ref=file_key,
            file_written=file_written,
        )
        reason = (
            "quota"
            if isinstance(exc, ai_gateway_service.AIQuotaExceededError)
            else "not_configured"
        )
        return JSONResponse(
            {
                "ok": False,
                "reason": reason,
                "message": (
                    t("body.quota")
                    if reason == "quota"
                    else t("body.not_configured")
                ),
            }
        )
    except BaseException:
        await _cleanup_uncommitted_body_upload(
            db,
            storage_ref=file_key,
            file_written=file_written,
        )
        raise

    try:
        await db.commit()
    except BaseException:
        try:
            await db.rollback()
        except BaseException:
            logger.exception("Could not reset session after body-upload commit failure")
        logger.exception(
            "Body-scan preparation commit is ambiguous; preserved uploaded bytes"
        )
        raise

    assert prepared is not None
    if prepared.reservation_status is AIInvocationStatus.SUCCEEDED:
        extracted = prepared.existing_extracted
    elif not prepared.dispatchable:
        return JSONResponse(
            {"ok": False, "reason": "pending", "message": t("body.upload.error")}
        )
    else:
        try:
            prepared_content = body_scan_ai.prepare_body_scan_content(
                prepared,
                file_bytes=contents,
            )
        except body_scan_ai.BodyScanAIValidationError:
            try:
                await body_scan_ai.cancel_prepared_body_scan_parse(
                    db,
                    prepared,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                logger.warning(
                    "Could not release a locally invalid body-scan AI reservation"
                )
            return JSONResponse(
                {"ok": False, "reason": "error", "message": t("body.upload.error")}
            )
        try:
            lease = await body_scan_ai.start_body_scan_dispatch(
                db,
                prepared,
                content=prepared_content,
            )
            await db.commit()
        except (
            ai_gateway_service.AIGatewayConfigurationError,
            ai_gateway_service.AIQuotaExceededError,
        ) as exc:
            await db.rollback()
            try:
                await body_scan_ai.cancel_prepared_body_scan_parse(
                    db,
                    prepared,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                logger.warning(
                    "Could not release a zero-network body-scan AI reservation"
                )
            reason = (
                "quota"
                if isinstance(exc, ai_gateway_service.AIQuotaExceededError)
                else "not_configured"
            )
            return JSONResponse(
                {
                    "ok": False,
                    "reason": reason,
                    "message": (
                        t("body.quota")
                        if reason == "quota"
                        else t("body.not_configured")
                    ),
                }
            )
        completion = await body_scan_ai.render_body_scan(
            prepared,
            lease,
            file_bytes=contents,
            content=prepared_content,
        )
        result = None
        for attempt in range(2):
            try:
                result = await body_scan_ai.persist_body_scan_parse(
                    db,
                    prepared,
                    completion,
                )
                break
            except Exception:
                await db.rollback()
                if attempt == 0:
                    logger.warning(
                        "Retrying transient body-scan AI finalization with the "
                        "same paid completion"
                    )
                    continue
                logger.exception(
                    "Body-scan AI finalization failed after internal retry"
                )
                raise
        assert result is not None
        try:
            await db.commit()
        except BaseException:
            try:
                await db.rollback()
            except BaseException:
                logger.exception("Could not reset failed body-scan AI finalization")
            logger.exception("Body-scan AI finalization commit outcome is ambiguous")
            raise
        if result.status is not AIInvocationStatus.SUCCEEDED:
            logger.warning(
                "Body-scan AI extraction ended with status %s",
                result.status.value,
            )
            return JSONResponse(
                {"ok": False, "reason": "error", "message": t("body.upload.error")}
            )
        extracted = result.extracted

    if not isinstance(extracted, dict):
        return JSONResponse(
            {"ok": False, "reason": "error", "message": t("body.upload.error")}
        )
    rows = scans.normalize_extracted(extracted)
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
            "raw_payload_id": prepared.raw_payload_id,
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
        await scans.save_scan(
            db,
            on_date=on_date,
            device=payload.device,
            file_key=None,
            raw_payload_id=payload.raw_payload_id,
            metrics=[m.model_dump() for m in payload.metrics],
            note=payload.note,
            override=payload.override,
            identity=identity,
            prepared_weight_write=prepared_weight_write,
        )
        await scans.refresh_alerts(
            db,
            subject_id=identity.subject_id,
            on_date=on_date,
            identity=identity,
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
    scan = await scans.get_scan(
        db,
        scan_id,
        subject_id=identity.subject_id,
    )
    if scan is None:
        return _back(request)

    file_asset_id = scan.file_asset_id if scan is not None else None
    deleted = await scans.delete_scan(
        db,
        scan_id,
        subject_id=identity.subject_id,
        identity=identity,
        prepared_weight_write=prepared_weight_write,
    )
    if deleted:
        await scans.refresh_alerts(
            db,
            subject_id=identity.subject_id,
            on_date=operation_date,
            identity=identity,
            prepared_weight_write=prepared_weight_write,
        )
    if deleted and file_asset_id is not None:
        asset = await file_asset_service.resolve_local_asset(
            db,
            file_asset_id=file_asset_id,
            subject_id=identity.subject_id,
        )
        physical_backend = asset.storage_backend
        physical_ref = asset.storage_ref
        await file_asset_service.mark_local_deleted(
            db,
            file_asset_id=file_asset_id,
            subject_id=identity.subject_id,
            purged=False,
        )
    else:
        physical_backend = None
        physical_ref = None
    await db.commit()

    bytes_purged = False
    if deleted and physical_backend is not None and physical_ref is not None:
        try:
            await run_in_threadpool(
                remove_stored_file,
                storage_backend=physical_backend,
                storage_ref=physical_ref,
                static_dir=STATIC_DIR,
                private_root=get_web_config().private_file_root,
            )
            bytes_purged = True
        except (OSError, ValueError) as e:
            logger.warning("Could not remove body-scan bytes: %s", e)

    if bytes_purged and file_asset_id is not None:
        await file_asset_service.mark_local_deleted(
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
        prepared_conflict_write=prepared,
    )
    if receipt is not None and receipt.file_asset_id is not None:
        asset = await file_asset_service.resolve_local_asset(
            db,
            file_asset_id=receipt.file_asset_id,
            subject_id=conflict_context.identity.subject_id,
        )
        physical_backend = asset.storage_backend
        physical_ref = asset.storage_ref
    else:
        physical_backend = None
        physical_ref = None
    await db.commit()

    bytes_purged = False
    if physical_backend is not None and physical_ref is not None:
        try:
            await run_in_threadpool(
                remove_stored_file,
                storage_backend=physical_backend,
                storage_ref=physical_ref,
                static_dir=STATIC_DIR,
                private_root=get_web_config().private_file_root,
            )
            bytes_purged = True
        except (OSError, ValueError) as e:
            logger.warning("Could not remove progress-photo bytes: %s", e)

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
        await file_asset_service.mark_local_deleted(
            db,
            file_asset_id=receipt.file_asset_id,
            subject_id=purge_context.identity.subject_id,
            purged=True,
        )
        await db.commit()

    return _back(request)
