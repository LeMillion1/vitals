"""Endpoints for managing weight logs, measurements, noise markers, photos, and
body-composition scans (InBody / МедАсс — the optional ``body_comp`` module)."""

from __future__ import annotations


from datetime import date as date_type
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from vitals.enums import (
    FileAssetPurpose,
    FileStorageBackend,
    Source,
)
from vitals.i18n import t
from vitals.services.files import lifecycle as file_lifecycle
from vitals.services.files import queries as file_queries
from vitals.services.weight import measurements as weight_measurements
from vitals.services.weight import noise as weight_noise
from vitals.services.weight import photos as weight_photos
from vitals.services.weight import writes as weight_writes
from vitals.services.weight.contracts import (
    ProgressPhotoOwnershipError,
)
from vitals.services.conflicts.engine import ConflictBlocked
from vitals.utils.timeutils import today_local
from web.config import get_web_config
from web.deps import get_session, require_auth
from web.templating import templates
from web.routers.weight_routes import common
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


@router.get("", response_class=HTMLResponse)
async def weight_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    """The trend: key figures, the chart, the history table and weight entry."""
    return templates.TemplateResponse(
        request,
        "weight/index.html",
        await common._section_context(request, db, username),
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
        request,
        "weight/measures.html",
        await common._section_context(request, db, username),
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
        conflict_context, prepared = await common._prepare_weight_write(
            db,
            username=username,
            on_date=on_date,
        )
        if id is not None:
            await weight_writes.update_weight_log(
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
            await weight_writes.log_weight(
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
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": str(e)})

    return common._back(request)


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
        conflict_context, prepared = await common._prepare_aux_write(
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
            await weight_measurements.update_body_measurement(
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
            await weight_measurements.upsert_body_measurement(
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
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": str(e)})

    return common._back(request)


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
        conflict_context, prepared = await common._prepare_aux_write(
            db,
            username=username,
            on_date=today_local(),
        )
        await weight_noise.add_noise_marker(
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

    return common._back(request)


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
            status_code=status.HTTP_400_BAD_REQUEST, detail=t("weight.error.no_files")
        )

    if len(uploaded_files) > 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=t("weight.error.too_many_files")
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

        conflict_context, prepared = await common._prepare_aux_write(
            db,
            username=username,
            on_date=on_date,
        )
        identity = conflict_context.identity
        for file_key, document in prepared_files:
            asset = await file_lifecycle.register_private_local(
                db,
                subject_id=identity.subject_id,
                uploaded_by_user_id=identity.actor_user_id,
                purpose=FileAssetPurpose.PROGRESS_PHOTO,
                storage_ref=file_key,
                media_type=document.media_type,
                size_bytes=document.byte_size,
                content_sha256=document.sha256_hex,
            )
            await weight_photos.add_progress_photo(
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
                    static_dir=common.STATIC_DIR,
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

    return common._back(request)


@router.post("/log/{id}/delete")
async def delete_weight_entry(
    request: Request,
    id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    from vitals.utils.timeutils import today_local

    conflict_context, prepared = await common._prepare_weight_write(
        db,
        username=username,
        on_date=today_local(),
    )
    await weight_writes.delete_weight_log(
        db,
        id,
        identity=conflict_context.identity,
        prepared_weight_write=prepared,
    )
    await db.commit()

    return common._back(request)


@router.post("/measurement/{id}/delete")
async def delete_measurement_entry(
    request: Request,
    id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    conflict_context, prepared = await common._prepare_aux_write(
        db,
        username=username,
        on_date=today_local(),
    )
    await weight_measurements.delete_body_measurement(
        db,
        id,
        identity=conflict_context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()

    return common._back(request)


@router.post("/noise/{id}/delete")
async def delete_noise_marker_entry(
    request: Request,
    id: int,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    conflict_context, prepared = await common._prepare_aux_write(
        db,
        username=username,
        on_date=today_local(),
    )
    await weight_noise.delete_noise_marker(
        db,
        id,
        identity=conflict_context.identity,
        prepared_conflict_write=prepared,
    )
    await db.commit()

    return common._back(request)


@router.post("/photo/delete")
async def delete_photo_entry(
    request: Request,
    id: int = Form(...),
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
):
    today = today_local()
    conflict_context, prepared = await common._prepare_aux_write(
        db,
        username=username,
        on_date=today,
    )
    receipt = await weight_photos.delete_progress_photo(
        db,
        id,
        identity=conflict_context.identity,
        prepared_conflict_write=prepared,
    )
    if receipt is not None and receipt.file_asset_id is not None:
        asset = await file_queries.resolve_local_asset(
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
                static_dir=common.STATIC_DIR,
                private_root=get_web_config().private_file_root,
            )
            bytes_purged = True
        except (OSError, ValueError) as e:
            logger.warning("Could not remove progress-photo bytes: %s", e)

    if bytes_purged and receipt is not None and receipt.file_asset_id is not None:
        purge_context, _purge_prepared = await common._prepare_aux_write(
            db,
            username=username,
            on_date=today,
        )
        if purge_context.identity != conflict_context.identity:
            raise ProgressPhotoOwnershipError(
                "progress-photo identity changed during physical purge"
            )
        await file_lifecycle.mark_local_deleted(
            db,
            file_asset_id=receipt.file_asset_id,
            subject_id=purge_context.identity.subject_id,
            purged=True,
        )
        await db.commit()

    return common._back(request)
