"""Personal export/import and installation operation delivery routes."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.access import PolicyAction, PolicyResourceType
from vitals.i18n import t
from vitals.operations.ownership import portability_v1
from vitals.services.authorization.subject_access import AccessDeniedError, require_access
from vitals.services.authorization.installation import (
    NotAnOperator,
    require_installation_operator_user,
)
from vitals.services.tenancy.ownership import resolve_legacy_ownership_context
from vitals.services.portability import llm_projection, v1_contract, v1_export, v1_import
from vitals.utils.timeutils import today_local
from web.care_context import principal_user_id
from web.deps import get_session, require_recent_auth
from web.downloads import private_json_download
from web.ratelimit import rate_limit
from web.templating import templates
from web.uploads import JSON_EXTS, VCF_MAX_BYTES, read_capped, validate_extension

from .common import compatibility_override

logger = logging.getLogger(__name__)
router = APIRouter()

async def _authorize_export(db: AsyncSession, username: str):
    """Decide the export, rather than infer it from being logged in.

    Downloading the record is the one routine operation that takes the data out
    of the boundary everything else keeps it inside, so it is the first to be
    *decided* by the policy engine rather than merely resolved. Today the answer
    is always yes — self-ownership authorizes it — and the value is that there is
    now one place for the answer to become no.
    """

    ownership = await resolve_legacy_ownership_context(db, actor_username=username)
    if ownership.access is None:  # pragma: no cover - require_auth names an actor
        raise AccessDeniedError("an export needs a principal behind it")
    require_access(
        ownership.access,
        resource_type=PolicyResourceType.OPERATION,
        resource_key="data_portability.export",
        action=PolicyAction.EXPORT,
    )
    return ownership



async def _authorize_installation_operation(
    request: Request, db: AsyncSession, *, operation: str
) -> None:
    """Decide an operation that is about the installation, not about a record.

    Restoring a backup replaces portable data for everybody in the database, and
    restarting takes the whole process down. Neither is a question about one
    subject, so neither goes through the subject-scoped policy — see
    ``vitals.services.authorization.installation`` for why passing the caller's own
    subject in would read as a check while always saying yes.
    """

    try:
        await compatibility_override(
            "require_installation_operator_user",
            require_installation_operator_user,
        )(
            db,
            user_id=await principal_user_id(request, db),
            operation=operation,
        )
    except NotAnOperator as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc


@router.get("/export")
async def export_backup(
    request: Request,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    _rl: None = Depends(rate_limit("data_export", limit=2, window=60)),
):
    """Download portable health data without identity/control-plane state.

    This is the whole-installation file, and format v1 describes an installation
    holding one person. In a shared one it therefore has nothing honest to
    write, which is a thing to say — with the export that *does* work named in
    the same breath — rather than a stack trace to serve as a 500. The personal
    export below is not a lesser version of this one: it is the right file for
    anybody who is not the whole installation.
    """
    await _authorize_installation_operation(
        request, db, operation="a full portability export"
    )
    try:
        snapshot = await v1_export.export_full(db)
    except v1_contract.MultiSubjectBackupError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=t("portability.error.v1_multi_subject_alternative"),
        ) from exc
    except v1_contract.PortabilityError as exc:
        # Anything else this raises is about the data being unrepresentable in
        # the format, which is the caller's answer to have — not a 500.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    body = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    filename = f"vitals_backup_{today_local().strftime('%Y%m%d')}.json"
    return private_json_download(body=body, filename=filename)


@router.get("/export-subject")
async def export_subject_backup(
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    _rl: None = Depends(rate_limit("data_export", limit=2, window=60)),
):
    """Download exactly this subject's record — no installation configuration.

    The other export answers "what is in this installation" and is an operator's
    file. This one answers "what is mine": one subject's rows, no app settings,
    and none of the installation's curated catalog, which the receiving
    installation seeds for itself.
    """

    ownership = await _authorize_export(db, username)
    try:
        snapshot = await v1_export.export_subject(
            db, subject_id=ownership.subject_id
        )
    except v1_contract.PortabilityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    body = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    filename = f"vitals_record_{today_local().strftime('%Y%m%d')}.json"
    return private_json_download(body=body, filename=filename)


@router.get("/export-llm")
async def export_llm(
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    _rl: None = Depends(rate_limit("data_export", limit=2, window=60)),
):
    """Download a curated, flat, secret-free digest for pasting into an LLM chat."""
    # The subject the authorization just resolved, handed on rather than
    # dropped. It used to be dropped, and ``export_llm`` read every table
    # unfiltered — so this download returned everybody's record on an
    # installation with more than one person in it.
    ownership = await _authorize_export(db, username)
    snapshot = await llm_projection.export_llm(
        db, subject_id=ownership.subject_id
    )
    body = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    filename = f"vitals_llm_{today_local().strftime('%Y%m%d')}.json"
    return private_json_download(body=body, filename=filename)


@router.post("/import")
async def import_backup(
    request: Request,
    _username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    backup_file: UploadFile = File(...),
    _rl: None = Depends(rate_limit("data_import", limit=2, window=60)),
):
    """Restore (replace) the whole DB from an uploaded full-backup JSON file.

    Atomic: the import runs in this request's transaction, so a malformed file
    rolls everything back. Validation failures return a clean 400 (no silent
    errors); success returns an OOB fragment with the per-domain stats.
    """
    await _authorize_installation_operation(
        request, db, operation="a restore"
    )
    validate_extension(backup_file.filename, JSON_EXTS)
    # Backups can be large (the raw_payloads data-lake), so allow the bigger cap.
    raw = await read_capped(backup_file, max_bytes=VCF_MAX_BYTES)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("import.error.bad_json", msg=exc.msg, line=exc.lineno),
        )

    try:
        stats = await portability_v1.import_full(db, payload)
    except v1_contract.MultiSubjectBackupError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=t("portability.error.v1_multi_subject_alternative"),
        ) from exc
    except v1_contract.PortabilityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    await db.commit()
    return templates.TemplateResponse(
        request,
        "settings/import_result.html",
        {"summary": stats.summary()},
    )


@router.post("/import-subject")
async def import_subject_record(
    request: Request,
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    backup_file: UploadFile = File(...),
    _rl: None = Depends(rate_limit("data_import", limit=2, window=60)),
):
    """Restore this subject's own record, and nobody else's.

    Not the operator's restore: that one empties every portable table and is
    correct only for a whole-database backup. This deletes and reloads exactly
    the caller's subject, so it needs the same authorization as an export rather
    than an operator's, and it refuses a full backup outright.
    """

    ownership = await _authorize_export(db, username)
    validate_extension(backup_file.filename, JSON_EXTS)
    raw = await read_capped(backup_file, max_bytes=VCF_MAX_BYTES)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("import.error.bad_json", msg=exc.msg, line=exc.lineno),
        )

    try:
        stats = await v1_import.import_subject(
            db, payload, subject_id=ownership.subject_id
        )
    except v1_contract.PortabilityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    await db.commit()
    return templates.TemplateResponse(
        request,
        "settings/import_result.html",
        {"summary": stats.summary()},
    )


@router.post("/restart")
async def restart_container(
    request: Request,
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
):
    import asyncio
    import os
    import signal

    from fastapi.responses import JSONResponse

    await _authorize_installation_operation(
        request, db, operation="a restart"
    )

    logger.info("User %s requested container restart. Terminating process in 500ms...", username)

    async def shutdown():
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(shutdown())
    return JSONResponse(content={"status": "restarting"})
