"""Endpoints for the Labs module: dashboard, manual entry, document upload
(LLM extraction) with an edit-before-save preview, per-marker history,
defer-retest, delete."""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import date as date_type
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationStatus,
    Domain,
)
from vitals.i18n import t
from vitals.services import (
    ai_gateway_service,
    alerts_service,
    conflict_engine,
    lab_document_ai_service,
    labs_service,
)
from vitals.services.conflict_engine import ConflictBlocked
from web.deps import get_session, require_auth
from web.ratelimit import rate_limit
from web.templating import STATIC_DIR, templates
from web.uploads import (
    DOC_EXTS,
    legacy_upload_disk_path,
    read_capped,
    validate_extension,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/labs", tags=["labs"])


async def _prepared_owner_write(
    db: AsyncSession,
    *,
    username: str,
    evaluation_date: date_type,
):
    context = await conflict_engine.resolve_legacy_conflict_write_context(
        db,
        actor_username=username,
        evaluation_date=evaluation_date,
    )
    prepared = await conflict_engine.prepare_scoped_write(
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
    await labs_service.refresh_alerts(
        db,
        subject_id=conflict_context.identity.subject_id,
        identity=conflict_context.identity,
        prepared_conflict_write=prepared,
    )

    latest = await labs_service.latest_per_marker(
        db,
        subject_id=conflict_context.identity.subject_id,
    )
    # Sort latest: out-of-range first (newest to oldest), then normal (newest to oldest)
    latest = sorted(
        latest,
        key=lambda r: (labs_service.is_out_of_range(r.flag), r.date),
        reverse=True
    )
    markers = await labs_service.list_markers(
        db,
        subject_id=conflict_context.identity.subject_id,
    )
    alerts = await alerts_service.list_active_scoped(
        db,
        context=alerts_service.HealthAlertContext(conflict_context.identity),
        domain=Domain.LABS,
        legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
    )

    selected = marker or (latest[0].marker if latest else None)
    history = (
        await labs_service.marker_history(
            db,
            selected,
            subject_id=conflict_context.identity.subject_id,
        )
        if selected
        else []
    )

    out_of_range = sum(1 for r in latest if labs_service.is_out_of_range(r.flag))
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
        await labs_service.add_result(
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
        await labs_service.refresh_alerts(
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

    # Validate/cap and persist private bytes before taking governance locks. The
    # first database phase either durably attaches these bytes to F/raw or rolls
    # back and removes them; no provider call happens until that phase commits.
    ext = validate_extension(file.filename, DOC_EXTS)
    contents = await read_capped(file)
    media_type = (
        "application/pdf"
        if ext == ".pdf"
        else (
            file.content_type
            if (file.content_type or "").lower().startswith("image/")
            else "image/jpeg"
        )
    )
    file_key = f"labs/{uuid.uuid4().hex}{ext}"
    file_path = legacy_upload_disk_path(STATIC_DIR, file_key)
    prepared = None
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as fh:
            fh.write(contents)
        prepared = await lab_document_ai_service.prepare_lab_document_parse(
            db,
            actor_username=username,
            storage_ref=file_key,
            media_type=media_type,
            byte_size=len(contents),
            sha256_hex=hashlib.sha256(contents).hexdigest(),
        )
    except (
        ai_gateway_service.AIGatewayConfigurationError,
        ai_gateway_service.AIQuotaExceededError,
    ) as exc:
        await db.rollback()
        try:
            os.remove(file_path)
        except FileNotFoundError:
            pass
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
                    t("labs.upload_quota")
                    if reason == "quota"
                    else t("labs.upload_not_configured")
                ),
            }
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
            "Lab-upload preparation commit is ambiguous; preserved uploaded bytes"
        )
        raise

    assert prepared is not None
    if prepared.reservation_status is AIInvocationStatus.SUCCEEDED:
        extracted = prepared.existing_extracted
    elif not prepared.dispatchable:
        return JSONResponse(
            {"ok": False, "reason": "pending", "message": t("labs.upload_error")}
        )
    else:
        try:
            prepared_content = (
                lab_document_ai_service.prepare_lab_document_content(
                    prepared,
                    file_bytes=contents,
                )
            )
        except lab_document_ai_service.LabDocumentAIValidationError:
            try:
                await lab_document_ai_service.cancel_prepared_lab_document_parse(
                    db,
                    prepared,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                logger.warning(
                    "Could not release a locally invalid lab AI reservation"
                )
            return JSONResponse(
                {"ok": False, "reason": "error", "message": t("labs.upload_error")}
            )
        try:
            lease = await lab_document_ai_service.start_lab_document_dispatch(
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
                await lab_document_ai_service.cancel_prepared_lab_document_parse(
                    db,
                    prepared,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                logger.warning(
                    "Could not release a zero-network lab AI reservation"
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
                        t("labs.upload_quota")
                        if reason == "quota"
                        else t("labs.upload_not_configured")
                    ),
                }
            )
        completion = await lab_document_ai_service.render_lab_document(
            prepared,
            lease,
            file_bytes=contents,
            content=prepared_content,
        )
        result = None
        for attempt in range(2):
            try:
                result = await lab_document_ai_service.persist_lab_document_parse(
                    db,
                    prepared,
                    completion,
                )
                break
            except Exception:
                await db.rollback()
                if attempt == 0:
                    logger.warning(
                        "Retrying transient lab AI finalization with the same "
                        "paid completion"
                    )
                    continue
                logger.exception("Lab AI finalization failed after internal retry")
                raise
        assert result is not None
        try:
            await db.commit()
        except BaseException:
            # If COMMIT reached PostgreSQL, rolling back locally cannot undo it.
            # Preserve the file and surface the ambiguity instead of inviting a
            # second paid upload with the same medical document.
            try:
                await db.rollback()
            except BaseException:
                logger.exception("Could not reset failed lab AI finalization")
            logger.exception("Lab AI finalization commit outcome is ambiguous")
            raise
        if result.status is not AIInvocationStatus.SUCCEEDED:
            logger.warning(
                "Lab AI extraction ended with status %s",
                result.status.value,
            )
            return JSONResponse(
                {"ok": False, "reason": "error", "message": t("labs.upload_error")}
            )
        extracted = result.extracted

    if not isinstance(extracted, dict):
        return JSONResponse(
            {"ok": False, "reason": "error", "message": t("labs.upload_error")}
        )
    try:
        lab_date = date_type.fromisoformat(str(extracted.get("date"))[:10])
    except (ValueError, TypeError):
        lab_date = today_local()
    rows = labs_service.normalize_extracted(extracted)

    return JSONResponse({
        "ok": True,
        "lab": {
            "date": lab_date.isoformat(),
            "lab_name": extracted.get("lab_name"),
            "file_key": file_key,
            "raw_payload_id": prepared.raw_payload_id,
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
        created = await labs_service.confirm_extracted(
            db,
            on_date=on_date,
            markers=[m.model_dump() for m in payload.markers],
            lab_name=payload.lab_name,
            raw_payload_id=payload.raw_payload_id,
            file_key=payload.file_key,
            override=payload.override,
            identity=conflict_context.identity,
            prepared_conflict_write=prepared,
        )
        await labs_service.refresh_alerts(
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
    await labs_service.defer_retest(
        db,
        name,
        until=date_type.fromisoformat(until),
        note=note,
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
    await labs_service.delete_result(
        db,
        result_id,
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
