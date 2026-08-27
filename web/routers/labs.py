"""Endpoints for the Labs module: dashboard, manual entry, document upload
(LLM extraction) with an edit-before-save preview, per-marker history,
defer-retest, delete."""
from __future__ import annotations

from vitals.services.alerts import lifecycle as alerts_service_lifecycle

from vitals.services.alerts import contracts as alerts_service_contracts

import logging
from datetime import date as date_type
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from vitals.enums import (
    Domain,
    FileAssetPurpose,
    FileStorageBackend,
)
from vitals.i18n import t
from vitals.services.labs import ai as lab_document_ai_service
from vitals.services.labs import alerts as lab_alerts
from vitals.services.labs import flags as lab_flags
from vitals.services.labs import ingestion as lab_ingestion
from vitals.services.labs import markers as lab_markers
from vitals.services.labs import results as lab_results
from vitals.services.conflicts import engine
from vitals.services.conflicts.engine import ConflictBlocked
from web.config import get_web_config
from web.deps import get_session, require_auth
from web.medical_ai_upload import MedicalAIUploadReason, run_medical_ai_upload
from web.ratelimit import rate_limit
from web.templating import STATIC_DIR, templates
from web.uploads import (
    prepare_medical_document,
    private_storage_ref,
    remove_stored_file,
    write_private_file,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/labs", tags=["labs"])


async def _prepared_owner_write(
    db: AsyncSession,
    *,
    username: str,
    evaluation_date: date_type,
):
    context = await engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=evaluation_date,
    )
    prepared = await engine.prepare_scoped_write(
        db,
        context=context,
    )
    return context, prepared


@router.get("", response_class=HTMLResponse)
async def labs_dashboard(
    request: Request,
    marker: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Labs dashboard: latest value per marker, the selected marker's history, the
    marker catalog (with retest/defer), and out-of-range alerts."""
    from vitals.utils.timeutils import today_local

    today = today_local()
    conflict_context, prepared = await _prepared_owner_write(
        db,
        username=username,
        evaluation_date=today,
    )
    await lab_alerts.refresh_alerts(
        db,
        subject_id=conflict_context.identity.subject_id,
        identity=conflict_context.identity,
        prepared_conflict_write=prepared,
    )

    latest = await lab_results.latest_per_marker(
        db,
        subject_id=conflict_context.identity.subject_id,
    )
    # Sort latest: out-of-range first (newest to oldest), then normal (newest to oldest)
    latest = sorted(
        latest,
        key=lambda r: (lab_flags.is_out_of_range(r.flag), r.date),
        reverse=True
    )
    markers = await lab_markers.list_markers(
        db,
        subject_id=conflict_context.identity.subject_id,
    )
    alerts = await alerts_service_lifecycle.list_active_scoped(
        db,
        context=alerts_service_contracts.HealthAlertContext(conflict_context.identity),
        domain=Domain.LABS,
        legacy_bridge=alerts_service_contracts.LegacyAlertBridge.FULLY_UNOWNED,
    )

    selected_marker = (
        await lab_markers.get_marker(
            db,
            marker,
            subject_id=conflict_context.identity.subject_id,
        )
        if marker
        else None
    )
    selected = (
        selected_marker.name
        if selected_marker is not None
        else (
            next(
                (
                    row.marker
                    for row in latest
                    if marker
                    and row.marker_key == lab_markers.normalize_marker_key(marker)
                ),
                marker,
            )
            if marker
            else (latest[0].marker if latest else None)
        )
    )
    history = (
        await lab_results.marker_history(
            db,
            selected,
            subject_id=conflict_context.identity.subject_id,
        )
        if selected
        else []
    )

    out_of_range = sum(1 for r in latest if lab_flags.is_out_of_range(r.flag))
    ai_availability = await lab_document_ai_service.project_lab_ai_availability(
        db,
        actor_username=username,
    )
    await db.commit()

    return templates.TemplateResponse(
        request,
        "labs/index.html",
        {
            "username": username,
            "latest": latest,
            "markers": markers,
            "alerts": alerts,
            "selected": selected,
            "series": {"points": history},
            "out_of_range": out_of_range,
            "llm_configured": ai_availability.available,
            "today": today.isoformat(),
            "upload": request.query_params.get("upload"),
            "added": request.query_params.get("added"),
            "failed": request.query_params.get("failed"),
        },
    )


@router.post("/result")
async def add_result(
    request: Request,
    date: str = Form(...),
    marker: str = Form(...),
    value: float = Form(...),
    unit: Optional[str] = Form(None),
    ref_low: Optional[float] = Form(None),
    ref_high: Optional[float] = Form(None),
    lab_name: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    override: bool = Form(False),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    on_date = date_type.fromisoformat(date)
    conflict_context, prepared = await _prepared_owner_write(
        db,
        username=username,
        evaluation_date=on_date,
    )
    try:
        row = await lab_results.add_result(
            db,
            on_date=on_date,
            marker=marker.strip(),
            value=value,
            unit=unit,
            ref_low=ref_low,
            ref_high=ref_high,
            lab_name=lab_name,
            note=note,
            override=override,
            identity=conflict_context.identity,
            prepared_conflict_write=prepared,
        )
        await lab_alerts.refresh_alerts(
            db,
            subject_id=conflict_context.identity.subject_id,
            identity=conflict_context.identity,
            prepared_conflict_write=prepared,
        )
    except ConflictBlocked as e:
        await db.rollback()
        # Same 409 + violations contract the weight/GLP-1/supplement forms use, so
        # the shared "save anyway" modal works here without its own flow.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"violations": [v.to_dict() for v in e.violations]},
        )
    except ValueError as e:
        await db.rollback()
        # The service validates for every caller (MCP included) — the form has to
        # surface that as a 400 rather than fall through to a 500.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await db.commit()
    return _redirect(request, marker=row.marker, added=1)


class LabMarkerIn(BaseModel):
    marker: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    ref_low: Optional[float] = None
    ref_high: Optional[float] = None


class LabConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str
    lab_name: Optional[str] = None
    raw_payload_id: Optional[int] = None
    markers: list[LabMarkerIn] = []
    override: bool = False


@router.post("/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
    _rl: None = Depends(rate_limit("labs_upload", limit=20, window=60)),
):
    """Step 1: a photo/PDF of a lab report -> vision extraction -> editable
    preview. The original file + verbatim vision payload are stored now
    (data-lake); the normalized ``LabResult`` rows are only written on confirm,
    with the owner's edits. Returns JSON the client renders as an editable
    table — a multi-file selection is queued and uploaded one file at a time by
    the client, each getting its own preview."""
    from vitals.utils.timeutils import today_local

    # Validate/cap and persist private bytes before taking governance locks. The
    # first database phase either durably attaches these bytes to F/raw or rolls
    # back and removes them; no provider call happens until that phase commits.
    document = await prepare_medical_document(file)
    assert document is not None  # FastAPI requires this upload field.
    ext = document.extension
    contents = document.body
    storage_ref = private_storage_ref(FileAssetPurpose.LAB_DOCUMENT, ext)
    outcome = await run_medical_ai_upload(
        db,
        label="lab-upload",
        logger=logger,
        file_bytes=contents,
        storage_ref=storage_ref,
        private_root=get_web_config().private_file_root,
        static_dir=STATIC_DIR,
        write_file=write_private_file,
        remove_file=remove_stored_file,
        run_in_threadpool=run_in_threadpool,
        prepare=lambda: lab_document_ai_service.prepare_lab_document_parse(
            db,
            actor_username=username,
            storage_ref=storage_ref,
            media_type=document.media_type,
            byte_size=document.byte_size,
            sha256_hex=document.sha256_hex,
            storage_backend=FileStorageBackend.PRIVATE_LOCAL,
        ),
        prepare_content=lambda prepared, body: (
            lab_document_ai_service.prepare_lab_document_content(
                prepared,
                file_bytes=body,
            )
        ),
        validation_error=lab_document_ai_service.LabDocumentAIValidationError,
        cancel=lambda prepared: (
            lab_document_ai_service.cancel_prepared_lab_document_parse(
                db,
                prepared,
            )
        ),
        start=lambda prepared, content: (
            lab_document_ai_service.start_lab_document_dispatch(
                db,
                prepared,
                content=content,
            )
        ),
        render=lambda prepared, lease, body, content: (
            lab_document_ai_service.render_lab_document(
                prepared,
                lease,
                file_bytes=body,
                content=content,
            )
        ),
        persist=lambda prepared, completion: (
            lab_document_ai_service.persist_lab_document_parse(
                db,
                prepared,
                completion,
            )
        ),
    )
    if outcome.reason is not MedicalAIUploadReason.SUCCEEDED:
        if outcome.reason is MedicalAIUploadReason.QUOTA:
            message = t("labs.upload_quota")
        elif outcome.reason is MedicalAIUploadReason.NOT_CONFIGURED:
            message = t("labs.upload_not_configured")
        else:
            message = t("labs.upload_error")
        return JSONResponse(
            {
                "ok": False,
                "reason": outcome.reason.value,
                "message": message,
            }
        )

    extracted = outcome.extracted
    assert extracted is not None
    try:
        lab_date = date_type.fromisoformat(str(extracted.get("date"))[:10])
    except (ValueError, TypeError):
        lab_date = today_local()
    rows = lab_ingestion.normalize_extracted(extracted)

    return JSONResponse({
        "ok": True,
        "lab": {
            "date": lab_date.isoformat(),
            "lab_name": extracted.get("lab_name"),
            "raw_payload_id": outcome.raw_payload_id,
            "markers": rows,
        },
    })


@router.post("/confirm")
async def labs_confirm(
    request: Request,
    payload: LabConfirm,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Step 2: persist the owner-edited marker rows from the upload preview."""
    try:
        on_date = date_type.fromisoformat(payload.date)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid date")

    conflict_context, prepared = await _prepared_owner_write(
        db,
        username=username,
        evaluation_date=on_date,
    )
    try:
        created = await lab_ingestion.confirm_extracted(
            db,
            on_date=on_date,
            markers=[m.model_dump() for m in payload.markers],
            lab_name=payload.lab_name,
            raw_payload_id=payload.raw_payload_id,
            file_key=None,
            override=payload.override,
            identity=conflict_context.identity,
            prepared_conflict_write=prepared,
        )
        await lab_alerts.refresh_alerts(
            db,
            subject_id=conflict_context.identity.subject_id,
            identity=conflict_context.identity,
            prepared_conflict_write=prepared,
        )
    except ConflictBlocked as e:
        await db.rollback()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"violations": [v.to_dict() for v in e.violations]},
        )
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await db.commit()
    return JSONResponse({"ok": True, "created": len(created)})


@router.post("/marker/{name}/defer")
async def defer_marker(
    request: Request,
    name: str,
    until: str = Form(...),
    note: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    from vitals.utils.timeutils import today_local

    conflict_context, prepared = await _prepared_owner_write(
        db,
        username=username,
        evaluation_date=today_local(),
    )
    await lab_alerts.defer_retest(
        db,
        name,
        until=date_type.fromisoformat(until),
        note=note,
        subject_id=conflict_context.identity.subject_id,
        identity=conflict_context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request, marker=name)


@router.post("/result/{result_id}/delete")
async def delete_result(
    request: Request,
    result_id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    from vitals.utils.timeutils import today_local

    conflict_context, prepared = await _prepared_owner_write(
        db,
        username=username,
        evaluation_date=today_local(),
    )
    await lab_results.delete_result(
        db,
        result_id,
        subject_id=conflict_context.identity.subject_id,
        identity=conflict_context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()
    return _redirect(request)


def _redirect(request: Request, **params) -> RedirectResponse:
    """Back to the labs page, optionally selecting a marker.

    Params are urlencoded, not interpolated: a marker name is Cyrillic here, and
    it ends up in the ``HX-Redirect`` **header**, which is latin-1 only — raw
    "Ферритин" in there is a 500, not a broken link.
    """
    url = "/labs" + (f"?{urlencode(params)}" if params else "")
    response = RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    if "hx-request" in request.headers:
        response.headers["HX-Redirect"] = url
    return response
