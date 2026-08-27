"""Endpoints for managing weight logs, measurements, noise markers, photos, and
body-composition scans (InBody / МедАсс — the optional ``body_comp`` module)."""

from __future__ import annotations


from datetime import date as date_type
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from vitals.enums import (
    FileAssetPurpose,
    FileStorageBackend,
)
from vitals.i18n import t
from vitals.services.files import lifecycle as file_lifecycle
from vitals.services.files import queries as file_queries
from vitals.services.body_scan.ai import contracts as body_scan_ai_contracts
from vitals.services.body_scan.ai import workflow as body_scan_ai_workflow
from vitals.services.body_scan.scans import alerts as body_scan_alerts
from vitals.services.body_scan.scans import ingestion as body_scan_ingestion
from vitals.services.body_scan.scans import normalization as body_scan_normalization
from vitals.services.body_scan.scans import queries as body_scan_queries
from vitals.services.body_scan.scans import writes as body_scan_writes
from vitals.services.conflicts.engine import ConflictBlocked
from vitals.utils.timeutils import today_local
from web.config import get_web_config
from web.deps import get_session, require_auth, require_module
from web.medical_ai_upload import MedicalAIUploadReason, run_medical_ai_upload
from web.ratelimit import rate_limit
from web.routers.weight_routes import common
from web.uploads import (
    prepare_medical_document,
    private_storage_ref,
    remove_stored_file,
    write_private_file,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/weight", tags=["weight"])


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
    outcome = await run_medical_ai_upload(
        db,
        label="body-scan",
        logger=logger,
        file_bytes=contents,
        storage_ref=file_key,
        private_root=get_web_config().private_file_root,
        static_dir=common.STATIC_DIR,
        write_file=write_private_file,
        remove_file=remove_stored_file,
        run_in_threadpool=run_in_threadpool,
        prepare=lambda: body_scan_ai_workflow.prepare_body_scan_parse(
            db,
            actor_username=username,
            storage_ref=file_key,
            media_type=document.media_type,
            byte_size=document.byte_size,
            sha256_hex=document.sha256_hex,
            storage_backend=FileStorageBackend.PRIVATE_LOCAL,
        ),
        prepare_content=lambda prepared, body: body_scan_ai_workflow.prepare_body_scan_content(
            prepared,
            file_bytes=body,
        ),
        validation_error=body_scan_ai_contracts.BodyScanAIValidationError,
        cancel=lambda prepared: body_scan_ai_workflow.cancel_prepared_body_scan_parse(
            db,
            prepared,
        ),
        start=lambda prepared, content: body_scan_ai_workflow.start_body_scan_dispatch(
            db,
            prepared,
            content=content,
        ),
        render=lambda prepared, lease, body, content: body_scan_ai_workflow.render_body_scan(
            prepared,
            lease,
            file_bytes=body,
            content=content,
        ),
        persist=lambda prepared, completion: body_scan_ai_workflow.persist_body_scan_parse(
            db,
            prepared,
            completion,
        ),
    )
    if outcome.reason is not MedicalAIUploadReason.SUCCEEDED:
        if outcome.reason is MedicalAIUploadReason.QUOTA:
            message = t("body.quota")
        elif outcome.reason is MedicalAIUploadReason.NOT_CONFIGURED:
            message = t("body.not_configured")
        else:
            message = t("body.upload.error")
        return JSONResponse(
            {
                "ok": False,
                "reason": outcome.reason.value,
                "message": message,
            }
        )

    extracted = outcome.extracted
    assert extracted is not None
    rows = body_scan_normalization.normalize_extracted(extracted)
    raw_date = date or extracted.get("date")
    try:
        scan_date = date_type.fromisoformat(str(raw_date)[:10]).isoformat()
    except (ValueError, TypeError):
        scan_date = today_local().isoformat()

    return JSONResponse(
        {
            "ok": True,
            "scan": {
                "date": scan_date,
                "device": extracted.get("device"),
                "raw_payload_id": outcome.raw_payload_id,
                "metrics": rows,
            },
        }
    )


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

    conflict_context, prepared_weight_write = await common._prepare_weight_write(
        db,
        username=username,
        on_date=on_date,
    )
    identity = conflict_context.identity
    try:
        await body_scan_ingestion.save_scan(
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
        await body_scan_alerts.refresh_alerts(
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
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": str(e)})
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
    conflict_context, prepared_weight_write = await common._prepare_weight_write(
        db,
        username=username,
        on_date=operation_date,
    )
    identity = conflict_context.identity
    scan = await body_scan_queries.get_scan(
        db,
        scan_id,
        subject_id=identity.subject_id,
    )
    if scan is None:
        return common._back(request)

    file_asset_id = scan.file_asset_id if scan is not None else None
    deleted = await body_scan_writes.delete_scan(
        db,
        scan_id,
        subject_id=identity.subject_id,
        identity=identity,
        prepared_weight_write=prepared_weight_write,
    )
    if deleted:
        await body_scan_alerts.refresh_alerts(
            db,
            subject_id=identity.subject_id,
            on_date=operation_date,
            identity=identity,
            prepared_weight_write=prepared_weight_write,
        )
    if deleted and file_asset_id is not None:
        asset = await file_queries.resolve_local_asset(
            db,
            file_asset_id=file_asset_id,
            subject_id=identity.subject_id,
        )
        physical_backend = asset.storage_backend
        physical_ref = asset.storage_ref
        await file_lifecycle.mark_local_deleted(
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
                static_dir=common.STATIC_DIR,
                private_root=get_web_config().private_file_root,
            )
            bytes_purged = True
        except (OSError, ValueError) as e:
            logger.warning("Could not remove body-scan bytes: %s", e)

    if bytes_purged and file_asset_id is not None:
        await file_lifecycle.mark_local_deleted(
            db,
            file_asset_id=file_asset_id,
            subject_id=identity.subject_id,
            purged=True,
        )
        await db.commit()

    return common._back(request)
