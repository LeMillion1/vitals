"""Subject-scoped metadata queries for private file delivery."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import FileAssetStatus, FileStorageBackend
from vitals.models.tenancy import FileAsset
from vitals.services.files.contracts import FileAssetNotFoundError, validate_uuid

SERVABLE_STATUSES = frozenset(
    {
        FileAssetStatus.LEGACY_PLACEHOLDER.value,
        FileAssetStatus.PENDING.value,
        FileAssetStatus.ACTIVE.value,
    }
)


async def resolve_for_download(
    session: AsyncSession,
    *,
    opaque_key: uuid.UUID,
    subject_id: uuid.UUID,
) -> FileAsset:
    """Find one servable asset by opaque key without leaking foreign rows."""

    validate_uuid(opaque_key, "opaque_key")
    validate_uuid(subject_id, "subject_id")
    asset = await session.scalar(
        select(FileAsset).where(
            FileAsset.opaque_key == opaque_key,
            FileAsset.subject_id == subject_id,
            FileAsset.status.in_(sorted(SERVABLE_STATUSES)),
        )
    )
    if asset is None:
        raise FileAssetNotFoundError("no servable asset for that key in this scope")
    return asset


async def resolve_local_asset(
    session: AsyncSession,
    *,
    file_asset_id: uuid.UUID,
    subject_id: uuid.UUID,
) -> FileAsset:
    """Resolve local storage metadata by an already-authorized asset id."""

    validate_uuid(file_asset_id, "file_asset_id")
    validate_uuid(subject_id, "subject_id")
    asset = await session.scalar(
        select(FileAsset).where(
            FileAsset.id == file_asset_id,
            FileAsset.subject_id == subject_id,
            FileAsset.storage_backend.in_(
                (
                    FileStorageBackend.LEGACY_LOCAL.value,
                    FileStorageBackend.PRIVATE_LOCAL.value,
                )
            ),
        )
    )
    if asset is None:
        raise FileAssetNotFoundError("local asset does not exist in subject scope")
    return asset


async def opaque_keys_for(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    file_asset_ids,
) -> dict[uuid.UUID, uuid.UUID]:
    """Map a page's subject-owned live asset ids to opaque download keys."""

    validate_uuid(subject_id, "subject_id")
    wanted = {value for value in file_asset_ids if value is not None}
    for value in wanted:
        validate_uuid(value, "file_asset_id")
    if not wanted:
        return {}
    rows = await session.execute(
        select(FileAsset.id, FileAsset.opaque_key).where(
            FileAsset.id.in_(sorted(wanted)),
            FileAsset.subject_id == subject_id,
            FileAsset.status.in_(sorted(SERVABLE_STATUSES)),
        )
    )
    return {row.id: row.opaque_key for row in rows}


__all__ = [
    "SERVABLE_STATUSES",
    "opaque_keys_for",
    "resolve_for_download",
    "resolve_local_asset",
]
