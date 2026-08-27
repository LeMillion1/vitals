"""Progress-photo facts and private-file ownership for Weight."""
from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import FileAssetPurpose, FileAssetStatus, Source
from vitals.models.tenancy import FileAsset
from vitals.models.weight import DOMAIN, ProgressPhoto
from vitals.ownership import WriteIdentity
from vitals.ownership_transition import bridges as ownership_bridges
from vitals.services.files import contracts as file_contracts
from vitals.services.files import lifecycle as file_lifecycle
from vitals.services.conflicts import engine

from .contracts import ProgressPhotoDeletion, ProgressPhotoOwnershipError
from .governance import (
    require_aux_prepared_write as _require_aux_prepared_write,
    require_evaluation_date as _require_evaluation_date,
)

# ── Progress photos ───────────────────────────────────────────────────────────
_PROGRESS_PHOTO_LIVE_ASSET_STATUSES = (
    FileAssetStatus.LEGACY_PLACEHOLDER.value,
    FileAssetStatus.PENDING.value,
)
_PROGRESS_PHOTO_IMAGE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".heic",
    ".heif",
)
_PROGRESS_PHOTO_IMAGE_KEY_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789._-"
)


async def _progress_photo_historical_processed_bound(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> int | None:
    """Return the read-only Stage-3H historical compatibility bound."""

    try:
        return await ownership_bridges.progress_photo_historical_processed_bound(
            session,
            subject_id=subject_id,
        )
    except ownership_bridges.ProgressPhotoOwnershipBackfillError as exc:
        raise ProgressPhotoOwnershipError(
            "progress-photo migration checkpoint is not authoritative"
        ) from exc


def _is_stage3h_historical_file_key(file_key: str) -> bool:
    """Accept only the root-level image namespace migrated by Stage 3H."""

    if not file_key.startswith("uploads/"):
        return False
    basename = file_key.removeprefix("uploads/")
    return (
        basename == basename.lower()
        and "/" not in basename
        and "\\" not in basename
        and ".." not in basename
        and basename.startswith(tuple("abcdefghijklmnopqrstuvwxyz0123456789"))
        and basename.endswith(_PROGRESS_PHOTO_IMAGE_EXTENSIONS)
        and all(
            character in _PROGRESS_PHOTO_IMAGE_KEY_CHARACTERS
            for character in basename
        )
    )


def _progress_photo_document_alias(file_key: str) -> str | None:
    """Return the lab/body metadata locator sharing this local disk path."""

    if file_key.startswith(("uploads/labs/", "uploads/body/")):
        return file_key.removeprefix("uploads/")
    return None


async def _progress_photo_scope_rows(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    filters: Sequence = (),
    for_update: bool = False,
) -> list[ProgressPhoto]:
    """Load and validate every photo that can affect one subject scope.

    The compatibility arm deliberately samples every ``S IS NULL`` candidate,
    not only the fully-null rows that would be returned. That makes a partial
    legacy root a typed integrity failure instead of silently hiding it.
    """

    from vitals.models.identity import HealthSubject

    if not isinstance(subject_id, uuid.UUID):
        raise ProgressPhotoOwnershipError("progress-photo subject_id must be a UUID")
    candidate_scope = ProgressPhoto.subject_id == subject_id
    stmt = select(ProgressPhoto).where(candidate_scope, *filters)
    if for_update:
        stmt = stmt.with_for_update()
    rows = list(
        (
            await session.scalars(
                stmt.execution_options(populate_existing=True)
            )
        ).all()
    )
    if not rows:
        return []

    owner_user_id = await session.scalar(
        select(HealthSubject.owner_user_id).where(HealthSubject.id == subject_id)
    )
    if owner_user_id is None:
        raise ProgressPhotoOwnershipError("progress-photo subject does not exist")
    historical_processed_bound = await _progress_photo_historical_processed_bound(
        session,
        subject_id=subject_id,
    )

    file_asset_ids = {
        row.file_asset_id for row in rows if row.file_asset_id is not None
    }
    legacy_file_keys = {
        row.file_key for row in rows if row.subject_id is None
    }
    document_aliases_by_key = {
        row.file_key: alias
        for row in rows
        if (alias := _progress_photo_document_alias(row.file_key)) is not None
    }
    assets: dict[uuid.UUID, FileAsset] = {}
    counts: dict[uuid.UUID, int] = {}
    shadowed_document_aliases: set[str] = set()
    key_counts = {
        file_key: count
        for file_key, count in (
            await session.execute(
                select(ProgressPhoto.file_key, func.count(ProgressPhoto.id))
                .where(ProgressPhoto.file_key.in_({row.file_key for row in rows}))
                .group_by(ProgressPhoto.file_key)
            )
        ).all()
    }
    if file_asset_ids:
        asset_rows = (
            await session.scalars(
                select(FileAsset)
                .where(FileAsset.id.in_(file_asset_ids))
                .execution_options(populate_existing=True)
            )
        ).all()
        assets = {row.id: row for row in asset_rows}
        counts = {
            file_asset_id: count
            for file_asset_id, count in (
                await session.execute(
                    select(ProgressPhoto.file_asset_id, func.count(ProgressPhoto.id))
                    .where(ProgressPhoto.file_asset_id.in_(file_asset_ids))
                    .group_by(ProgressPhoto.file_asset_id)
                )
            ).all()
            if file_asset_id is not None
        }
    if legacy_file_keys:
        set(
            (
                await session.scalars(
                    select(FileAsset.storage_ref).where(
                        FileAsset.storage_ref.in_(legacy_file_keys)
                    )
                )
            ).all()
        )
    if document_aliases_by_key:
        shadowed_document_aliases = set(
            (
                await session.scalars(
                    select(FileAsset.storage_ref).where(
                        FileAsset.storage_ref.in_(document_aliases_by_key.values())
                    )
                )
            ).all()
        )

    for row in rows:
        if key_counts.get(row.file_key) != 1:
            raise ProgressPhotoOwnershipError(
                "progress-photo file key is linked by more than one fact"
            )
        if row.domain != DOMAIN or row.source != Source.MANUAL.value:
            raise ProgressPhotoOwnershipError(
                "progress photo has invalid domain or source provenance"
            )
        document_alias = document_aliases_by_key.get(row.file_key)
        if document_alias in shadowed_document_aliases:
            raise ProgressPhotoOwnershipError(
                "progress photo aliases document file metadata"
            )
        if row.subject_id != subject_id:
            raise ProgressPhotoOwnershipError(
                "progress photo belongs to another subject"
            )
        migrated_historical = (
            row.actor_user_id is None
            and historical_processed_bound is not None
            and row.id <= historical_processed_bound
        )
        if row.actor_user_id != owner_user_id and not migrated_historical:
            raise ProgressPhotoOwnershipError(
                "progress photo actor does not match the subject owner"
            )
        if row.file_asset_id is None:
            raise ProgressPhotoOwnershipError(
                "owned progress photo is missing its file asset"
            )
        if migrated_historical and not _is_stage3h_historical_file_key(row.file_key):
            raise ProgressPhotoOwnershipError(
                "historical progress photo has an unsafe file key"
            )
        asset = assets.get(row.file_asset_id)
        if asset is None:
            raise ProgressPhotoOwnershipError(
                "progress photo links to a missing file asset"
            )
        expected_uploaders = {None} if migrated_historical else {owner_user_id}
        if (
            asset.subject_id != subject_id
            or asset.uploaded_by_user_id not in expected_uploaders
            or asset.purpose != FileAssetPurpose.PROGRESS_PHOTO.value
            or not file_contracts.local_asset_is_live(asset)
            or asset.storage_ref != row.file_key
        ):
            raise ProgressPhotoOwnershipError(
                "progress photo file asset has conflicting ownership or lifecycle"
            )
        if counts.get(row.file_asset_id) != 1:
            raise ProgressPhotoOwnershipError(
                "progress photo file asset is linked by more than one fact"
            )
    return rows


async def add_progress_photo(
    session: AsyncSession,
    *,
    on_date: date_type,
    file_key: str | None = None,
    note: Optional[str] = None,
    identity: WriteIdentity,
    file_asset_id: uuid.UUID | None = None,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> ProgressPhoto:
    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    from vitals.models.identity import HealthSubject

    _require_evaluation_date(context, on_date)
    if identity.actor_user_id is None:
        raise ProgressPhotoOwnershipError(
            "progress photo creation requires a human owner actor"
        )
    owner_user_id = await session.scalar(
        select(HealthSubject.owner_user_id).where(
            HealthSubject.id == identity.subject_id
        )
    )
    if owner_user_id != identity.actor_user_id:
        raise ProgressPhotoOwnershipError(
            "progress photo actor does not match the subject owner"
        )
    if not isinstance(file_asset_id, uuid.UUID):
        raise ProgressPhotoOwnershipError(
            "owned progress photo requires a file_asset_id"
        )
    asset = await session.scalar(
        select(FileAsset)
        .where(FileAsset.id == file_asset_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if asset is None or (
        asset.subject_id != identity.subject_id
        or asset.uploaded_by_user_id != identity.actor_user_id
        or asset.purpose != FileAssetPurpose.PROGRESS_PHOTO.value
        or not file_contracts.local_asset_is_live(asset)
    ):
        raise ProgressPhotoOwnershipError(
            "progress photo file asset is not authoritative in subject scope"
        )
    if file_key is not None and file_key != asset.storage_ref:
        raise ProgressPhotoOwnershipError(
            "progress photo file key conflicts with its file asset"
        )
    document_alias = _progress_photo_document_alias(asset.storage_ref)
    if document_alias is not None:
        aliased_asset_id = await session.scalar(
            select(FileAsset.id)
            .where(FileAsset.storage_ref == document_alias)
            .with_for_update()
        )
        if aliased_asset_id is not None:
            raise ProgressPhotoOwnershipError(
                "progress photo aliases document file metadata"
            )
    existing = await session.scalar(
        select(ProgressPhoto.id)
        .where(
            or_(
                ProgressPhoto.file_asset_id == file_asset_id,
                ProgressPhoto.file_key == asset.storage_ref,
            )
        )
        .with_for_update()
    )
    if existing is not None:
        raise ProgressPhotoOwnershipError(
            "progress photo file asset already has a fact"
        )
    authoritative_file_key = asset.storage_ref

    photo = ProgressPhoto(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        file_asset_id=file_asset_id,
        date=on_date,
        domain=DOMAIN,
        source=Source.MANUAL.value,
        file_key=authoritative_file_key,
        note=note,
    )
    session.add(photo)
    await session.flush()
    return photo


async def list_progress_photos(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    start: date_type | None = None,
    end: date_type | None = None,
) -> Sequence[ProgressPhoto]:
    filters = []
    if start is not None:
        filters.append(ProgressPhoto.date >= start)
    if end is not None:
        filters.append(ProgressPhoto.date <= end)
    rows = await _progress_photo_scope_rows(
        session,
        subject_id=subject_id,
        filters=tuple(filters),
    )
    return sorted(rows, key=lambda row: (row.date, row.id), reverse=True)


async def get_progress_photo_by_file_key(
    session: AsyncSession,
    *,
    file_key: str,
    subject_id: uuid.UUID,
) -> ProgressPhoto | None:
    if not isinstance(file_key, str) or not file_key:
        raise ProgressPhotoOwnershipError("progress-photo file_key must be non-blank")
    rows = await _progress_photo_scope_rows(
        session,
        subject_id=subject_id,
        filters=(ProgressPhoto.file_key == file_key,),
    )
    if len(rows) > 1:
        raise ProgressPhotoOwnershipError(
            "progress-photo file key resolves to more than one fact"
        )
    return rows[0] if rows else None


async def delete_progress_photo(
    session: AsyncSession,
    photo_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> ProgressPhotoDeletion | None:
    """Delete a photo fact and retire its file metadata in one transaction."""

    _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    from vitals.models.identity import HealthSubject

    owner_user_id = await session.scalar(
        select(HealthSubject.owner_user_id).where(
            HealthSubject.id == identity.subject_id
        )
    )
    if identity.actor_user_id is None or owner_user_id != identity.actor_user_id:
        raise ProgressPhotoOwnershipError(
            "progress photo deletion requires the subject owner actor"
        )
    candidate = (
        await session.execute(
            select(
                ProgressPhoto.subject_id,
                ProgressPhoto.actor_user_id,
                ProgressPhoto.file_asset_id,
                ProgressPhoto.file_key,
            ).where(
                ProgressPhoto.id == photo_id,
                or_(
                    ProgressPhoto.subject_id == identity.subject_id,
                    ProgressPhoto.subject_id.is_(None),
                ),
            )
        )
    ).one_or_none()
    if candidate is None:
        return None

    candidate_subject_id, candidate_actor_id, candidate_file_id, candidate_key = (
        candidate
    )
    asset = None
    if candidate_file_id is not None:
        asset = await session.scalar(
            select(FileAsset)
            .where(FileAsset.id == candidate_file_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    rows = await _progress_photo_scope_rows(
        session,
        subject_id=identity.subject_id,
        filters=(ProgressPhoto.id == photo_id,),
        for_update=True,
    )
    if not rows:
        return None
    row = rows[0]
    if (
        row.subject_id != candidate_subject_id
        or row.actor_user_id != candidate_actor_id
        or row.file_asset_id != candidate_file_id
        or row.file_key != candidate_key
    ):
        raise ProgressPhotoOwnershipError(
            "progress photo provenance changed while deletion was being authorized"
        )

    receipt = ProgressPhotoDeletion(row.file_key, row.file_asset_id)
    if row.file_asset_id is not None:
        if asset is None or asset.id != row.file_asset_id:
            raise ProgressPhotoOwnershipError(
                "progress photo file asset disappeared during deletion"
            )
        await file_lifecycle.mark_local_deleted(
            session,
            file_asset_id=row.file_asset_id,
            subject_id=identity.subject_id,
            purged=False,
        )
    await session.delete(row)
    await session.flush()
    return receipt
