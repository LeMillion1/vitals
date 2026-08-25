"""Owner-only HTTP boundary for encrypted personal portability-v2 records."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.background import BackgroundTask

from vitals.access import PolicyAction, PolicyResourceType
from vitals.enums import IntegrationConnectionStatus
from vitals.i18n import t
from vitals.models.tenancy import IntegrationConnection
from vitals.operations.portability.export_v2 import export_subject_encrypted
from vitals.operations.portability.import_v2 import (
    ImportV2OperationError,
    import_validated_record_v2,
)
from vitals.services.access_resolution import AccessDeniedError, require_access
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from vitals.services.portability.archive_reader import (
    ArchiveReadError,
    open_validated_encrypted_archive,
)
from vitals.services.portability.connection_mapping import ConnectionMappingError
from vitals.services.portability.crypto import PortabilityCryptoError
from vitals.services.portability.file_retirement import (
    FileRetirementError,
    purge_retired_files_post_commit,
)
from vitals.services.portability.receipts import ReceiptServiceError
from vitals.services.portability.record_decoder import (
    RecordDecodeError,
    decode_validated_record,
)
from vitals.services.portability.replacement_apply import ReplacementApplyError
from vitals.services.portability.replacement_preflight import ReplacementPreflightError
from vitals.services.portability.resource_staging import ResourceStagingError
from vitals.services.portability.resources import ResourceLocations
from web.config import get_web_config
from web.deps import get_session, get_session_factory, require_recent_auth
from web.downloads import PRIVATE_DOWNLOAD_HEADERS
from web.ratelimit import rate_limit
from web.uploads import validate_extension


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings/portability-v2", tags=["portability-v2"])

PORTABILITY_EXTENSIONS = frozenset({".vitals"})
_STATIC_ROOT = os.path.realpath(Path(__file__).resolve().parents[1] / "static")
_USABLE_CONNECTION_STATUSES = (
    IntegrationConnectionStatus.LEGACY.value,
    IntegrationConnectionStatus.ACTIVE.value,
)
_MAX_MAPPING_JSON_BYTES = 64 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_PROVIDER_LABELS = {
    "garmin": "Garmin",
    "hevy": "Hevy",
    "openrouter": "OpenRouter",
    "telegram": "Telegram",
}


async def _authorize_personal_portability(db: AsyncSession, username: str):
    ownership = await resolve_legacy_ownership_context(db, actor_username=username)
    if ownership.access is None:  # pragma: no cover - recent auth names an actor
        raise AccessDeniedError("personal portability requires an authenticated principal")
    require_access(
        ownership.access,
        resource_type=PolicyResourceType.OPERATION,
        resource_key="data_portability.export",
        action=PolicyAction.EXPORT,
    )
    return ownership


def _invalid_archive() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=t("portability.v2.invalid_archive"),
    )


def _private_json(content: dict[str, object], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=content,
        status_code=status_code,
        headers=PRIVATE_DOWNLOAD_HEADERS,
    )


def _connection_mapping(value: str) -> dict[str, uuid.UUID]:
    if type(value) is not str or len(value.encode("utf-8")) > _MAX_MAPPING_JSON_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("portability.v2.invalid_mapping"),
        )
    try:
        raw = json.loads(value)
    except (json.JSONDecodeError, UnicodeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("portability.v2.invalid_mapping"),
        ) from None
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("portability.v2.invalid_mapping"),
        )
    result: dict[str, uuid.UUID] = {}
    try:
        for ref, connection_id in raw.items():
            if type(ref) is not str or type(connection_id) is not str:
                raise ValueError
            parsed = uuid.UUID(connection_id)
            if str(parsed) != connection_id or parsed.int == 0:
                raise ValueError
            result[ref] = parsed
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("portability.v2.invalid_mapping"),
        ) from None
    return result


def _iter_download(source):
    while chunk := source.read(_DOWNLOAD_CHUNK_BYTES):
        yield chunk


def _connection_label(provider: str, connection_type: str) -> str:
    return t(
        "portability.v2.connection_descriptor",
        provider=_PROVIDER_LABELS.get(provider, provider),
        connection_type=t(f"portability.v2.connection_type.{connection_type}"),
    )


@router.post("/export")
async def export_personal_record_v2(
    passphrase: str = Form(...),
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    _rl: None = Depends(rate_limit("data_export", limit=2, window=60)),
):
    """Create an encrypted, authenticated record without a plaintext spool."""

    ownership = await _authorize_personal_portability(db, username)
    config = get_web_config()
    destination = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        await export_subject_encrypted(
            db,
            subject_id=ownership.subject_id,
            passphrase=passphrase,
            destination=destination,
            locations=ResourceLocations(
                static_dir=_STATIC_ROOT,
                private_root=config.private_file_root,
            ),
        )
        destination.seek(0)
    except PortabilityCryptoError as exc:
        destination.close()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("portability.v2.invalid_passphrase"),
        ) from exc
    except Exception:
        destination.close()
        raise

    filename = f"vitals_record_{uuid.uuid4().hex[:12]}.vitals"
    return StreamingResponse(
        _iter_download(destination),
        media_type="application/vnd.vitals.portability",
        headers={
            **PRIVATE_DOWNLOAD_HEADERS,
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(destination.close),
    )


@router.post("/inspect")
async def inspect_personal_record_v2(
    archive_file: UploadFile = File(...),
    passphrase: str = Form(...),
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    _rl: None = Depends(rate_limit("data_import_inspect", limit=6, window=60)),
):
    """Authenticate an upload and return only the choices needed for apply."""

    ownership = await _authorize_personal_portability(db, username)
    validate_extension(archive_file.filename, PORTABILITY_EXTENSIONS)
    try:
        with open_validated_encrypted_archive(
            archive_file.file,
            passphrase=passphrase,
        ) as archive:
            record = decode_validated_record(archive)
            archive_id = archive.archive_id
            schema_digest = record.schema_digest
            row_count = record.row_count
            resource_count = len(record.resources)
            descriptors = record.connections
    except (ArchiveReadError, PortabilityCryptoError, RecordDecodeError) as exc:
        raise _invalid_archive() from exc

    candidates = tuple(
        await db.scalars(
            select(IntegrationConnection)
            .where(
                IntegrationConnection.subject_id == ownership.subject_id,
                IntegrationConnection.status.in_(_USABLE_CONNECTION_STATUSES),
            )
            .order_by(
                IntegrationConnection.provider,
                IntegrationConnection.connection_type,
                IntegrationConnection.created_at,
                IntegrationConnection.id,
            )
        )
    )
    connections: list[dict[str, object]] = []
    for descriptor in descriptors:
        descriptor_label = _connection_label(
            descriptor.provider,
            descriptor.connection_type,
        )
        matching = tuple(
            candidate
            for candidate in candidates
            if candidate.provider == descriptor.provider
            and candidate.connection_type == descriptor.connection_type
        )
        connections.append(
            {
                "ref": descriptor.ref,
                "provider": descriptor.provider,
                "connection_type": descriptor.connection_type,
                "label": descriptor_label,
                "candidates": [
                    {
                        "id": str(candidate.id),
                        "label": t(
                            "portability.v2.connection_candidate",
                            connection=descriptor_label,
                            suffix=str(candidate.id)[:8],
                        ),
                    }
                    for candidate in matching
                ],
            }
        )
    return _private_json(
        {
            "format": "vitals-portability-inspection",
            "version": 1,
            "operation_id": str(uuid.uuid4()),
            "archive_id": str(archive_id),
            "schema_digest": schema_digest,
            "row_count": row_count,
            "resource_count": resource_count,
            "connections": connections,
        }
    )


@router.post("/apply")
async def apply_personal_record_v2(
    archive_file: UploadFile = File(...),
    passphrase: str = Form(...),
    operation_id: uuid.UUID = Form(...),
    connection_mapping: str = Form(...),
    confirmation: str = Form(...),
    username: str = Depends(require_recent_auth),
    db: AsyncSession = Depends(get_session),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    _rl: None = Depends(rate_limit("data_import", limit=2, window=60)),
):
    """Re-authenticate the same file and atomically replace the owner's record."""

    ownership = await _authorize_personal_portability(db, username)
    if confirmation != "replace":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("portability.v2.confirmation_required"),
        )
    validate_extension(archive_file.filename, PORTABILITY_EXTENSIONS)
    mapping = _connection_mapping(connection_mapping)
    config = get_web_config()
    try:
        with open_validated_encrypted_archive(
            archive_file.file,
            passphrase=passphrase,
        ) as archive:
            record = decode_validated_record(archive)
            result = await import_validated_record_v2(
                session_factory,
                archive=archive,
                record=record,
                target_subject_id=ownership.subject_id,
                actor_user_id=ownership.actor_user_id,
                operation_id=operation_id,
                connection_ids_by_ref=mapping,
                private_root=config.private_file_root,
            )
    except (ArchiveReadError, PortabilityCryptoError, RecordDecodeError) as exc:
        raise _invalid_archive() from exc
    except (ReplacementPreflightError, ReceiptServiceError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=t("portability.v2.import_conflict"),
        ) from exc
    except (
        ConnectionMappingError,
        FileRetirementError,
        ImportV2OperationError,
        ReplacementApplyError,
        ResourceStagingError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=t("portability.v2.import_rejected"),
        ) from exc

    purge_report = None
    if result.retirement_plan is not None:
        try:
            purge_report = await purge_retired_files_post_commit(
                session_factory,
                plan=result.retirement_plan,
                static_dir=_STATIC_ROOT,
                private_root=config.private_file_root,
            )
        except Exception:
            logger.exception(
                "Post-commit portability file purge failed for %d asset(s)",
                len(result.retirement_plan.objects),
            )
        else:
            if not purge_report.complete:
                logger.error(
                    "Post-commit portability file purge left %d retryable failure(s)",
                    len(purge_report.failures),
                )

    cleanup_pending = bool(
        purge_report is None
        and result.retirement_plan is not None
        and result.retirement_plan.objects
    ) or bool(purge_report is not None and not purge_report.complete)
    return _private_json(
        {
            "status": "replayed" if result.replayed else "imported",
            "row_count": result.receipt.request.row_count,
            "resource_count": result.receipt.request.resource_count,
            "cleanup_pending": cleanup_pending,
        }
    )


__all__ = ["PORTABILITY_EXTENSIONS", "router"]
