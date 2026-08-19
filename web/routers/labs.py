"""Endpoints for the Labs module: dashboard, manual entry, document upload
(LLM extraction) with an edit-before-save preview, per-marker history,
defer-retest, delete."""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import date as date_type
from urllib.parse import urlencode
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import Domain, FileAssetPurpose, IntegrationProvider, Source
from vitals.i18n import t
from vitals.integrations.llm_client import LLMClient, LLMNotConfigured
from vitals.services import (
    alerts_service,
    file_asset_service,
    labs_service,
    raw_payload_service,
)
from vitals.services.conflict_engine import ConflictBlocked
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from web.deps import get_session, require_auth
from web.ratelimit import rate_limit
from web.templating import STATIC_DIR, templates
from web.uploads import (
    DOC_EXTS,
    file_ext,
    legacy_upload_disk_path,
    read_capped,
    validate_extension,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/labs", tags=["labs"])


@router.get("", response_class=HTMLResponse)
async def labs_dashboard(
    request: Request,
    marker: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """Labs dashboard: latest value per marker, the selected marker's history, the
    marker catalog (with retest/defer), and out-of-range alerts."""
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    await labs_service.refresh_alerts(
        db,
        subject_id=ownership.subject_id,
        include_legacy_unowned=True,
    )
    await db.commit()

    latest = await labs_service.latest_per_marker(
        db,
        subject_id=ownership.subject_id,
        include_legacy_unowned=True,
    )
    # Sort latest: out-of-range first (newest to oldest), then normal (newest to oldest)
    latest = sorted(
        latest,
        key=lambda r: (labs_service.is_out_of_range(r.flag), r.date),
        reverse=True
    )
    markers = await labs_service.list_markers(
        db,
        subject_id=ownership.subject_id,
        include_legacy_unowned=True,
    )
    alerts = await alerts_service.list_active(db, domain=Domain.LABS.value)

    selected = marker or (latest[0].marker if latest else None)
    history = (
        await labs_service.marker_history(
            db,
            selected,
            subject_id=ownership.subject_id,
            include_legacy_unowned=True,
        )
        if selected
        else []
    )

    out_of_range = sum(1 for r in latest if labs_service.is_out_of_range(r.flag))

    from vitals.utils.timeutils import today_local

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
            "llm_configured": bool(load_config().openrouter_api_key),
            "today": today_local().isoformat(),
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
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    try:
        await labs_service.add_result(
            db,
            on_date=date_type.fromisoformat(date),
            marker=marker.strip(),
            value=value,
            unit=unit,
            ref_low=ref_low,
            ref_high=ref_high,
            lab_name=lab_name,
            note=note,
            override=override,
            identity=ownership.owner_action(),
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
    return _redirect(request, marker=marker.strip(), added=1)


class LabMarkerIn(BaseModel):
    marker: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    ref_low: Optional[float] = None
    ref_high: Optional[float] = None


class LabConfirm(BaseModel):
    date: str
    lab_name: Optional[str] = None
    file_key: Optional[str] = None
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

    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
        required_connections=(IntegrationProvider.OPENROUTER,),
    )
    identity = ownership.owner_action()

    # 415/413 surface as HTTP errors (handled by the client's error branch).
    validate_extension(file.filename, DOC_EXTS)
    contents = await read_capped(file)

    try:
        llm = LLMClient()
    except LLMNotConfigured:
        return JSONResponse({"ok": False, "reason": "not_configured", "message": t("labs.upload_not_configured")})

    try:
        extracted = await labs_service.extract_from_file(
            contents,
            llm=llm,
            content_type=file.content_type or "image/jpeg",
            filename=file.filename,
        )
    except LLMNotConfigured:
        return JSONResponse({"ok": False, "reason": "not_configured", "message": t("labs.upload_not_configured")})
    except Exception as e:  # noqa: BLE001 — surface parse failures softly
        logger.warning("Lab extraction failed for %s: %s", file.filename, e)
        return JSONResponse({"ok": False, "reason": "error", "message": t("labs.upload_error")})

    # Persist the original document for reference (served at /static/uploads/...).
    # Written only once extraction succeeded: on the failure branches above no DB
    # row references the file, so writing first left unreferenced files piling up
    # on disk with nothing pointing at them.
    ext = file_ext(file.filename) or ".bin"
    file_key = f"labs/{uuid.uuid4().hex}{ext}"
    file_path = legacy_upload_disk_path(STATIC_DIR, file_key)
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    try:
        with open(file_path, "wb") as fh:
            fh.write(contents)
        asset = await file_asset_service.register_legacy_local(
            db,
            subject_id=identity.subject_id,
            uploaded_by_user_id=identity.actor_user_id,
            purpose=FileAssetPurpose.LAB_DOCUMENT,
            storage_ref=file_key,
            media_type=file.content_type or None,
            size_bytes=len(contents),
            content_sha256=hashlib.sha256(contents).hexdigest(),
        )
        raw_row = await raw_payload_service.upsert_owned_raw_payload(
            db,
            identity=identity,
            integration_connection_id=ownership.connection_id(
                IntegrationProvider.OPENROUTER
            ),
            file_asset_id=asset.id,
            domain=Domain.LABS.value,
            source=Source.LAB_PARSER.value,
            external_id=file_key,
            payload=extracted,
        )
    except BaseException:
        try:
            await db.rollback()
        except BaseException:
            logger.exception("Could not roll back failed lab-upload transaction")
        try:
            os.remove(file_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not clean up failed lab upload %s: %s", file_path, exc)
        raise

    try:
        await db.commit()
    except BaseException:
        # A lost/cancelled COMMIT acknowledgement is not proof that PostgreSQL
        # rolled back. Preserve the medical document so a committed metadata row
        # can be reconciled instead of becoming an irreversible broken pointer.
        try:
            await db.rollback()
        except BaseException:
            logger.exception("Could not reset session after lab-upload commit failure")
        logger.exception(
            "Lab-upload commit outcome is ambiguous; preserved uploaded bytes"
        )
        raise

    rows = labs_service.normalize_extracted(extracted)
    try:
        lab_date = date_type.fromisoformat(str(extracted.get("date"))[:10]).isoformat()
    except (ValueError, TypeError):
        lab_date = today_local().isoformat()

    return JSONResponse({
        "ok": True,
        "lab": {
            "date": lab_date,
            "lab_name": extracted.get("lab_name"),
            "file_key": file_key,
            "raw_payload_id": raw_row.id,
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

    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    identity = ownership.owner_action()
    try:
        created = await labs_service.confirm_extracted(
            db,
            on_date=on_date,
            markers=[m.model_dump() for m in payload.markers],
            lab_name=payload.lab_name,
            raw_payload_id=payload.raw_payload_id,
            file_key=payload.file_key,
            override=payload.override,
            identity=identity,
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
    await labs_service.refresh_alerts(
        db,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
    )
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
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    await labs_service.defer_retest(
        db,
        name,
        until=date_type.fromisoformat(until),
        note=note,
        subject_id=ownership.subject_id,
        include_legacy_unowned=True,
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
    ownership = await resolve_legacy_ownership_context(
        db,
        actor_username=username,
    )
    await labs_service.delete_result(
        db,
        result_id,
        subject_id=ownership.subject_id,
        include_legacy_unowned=True,
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
