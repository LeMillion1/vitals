"""Ownership and immutable-root locking for body-scan AI workflows."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    Source,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset
from vitals.ownership import WriteIdentity
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.legacy_ownership import resolve_legacy_ownership_context

from .contracts import (
    BodyScanAIOwnershipError,
    PreparedBodyScanParse,
    _LockedBodyScanScope,
    _asset_fingerprint,
    _raw_fingerprint,
)

async def _lock_owner(
    session: AsyncSession,
    *,
    actor_username: str,
) -> tuple[HealthSubject, User, WriteIdentity]:
    await acquire_identity_governance_lock(session)
    ownership = await resolve_legacy_ownership_context(
        session,
        actor_username=actor_username,
    )
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == ownership.subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    owner = await session.scalar(
        select(User)
        .where(User.id == ownership.owner_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        subject is None
        or owner is None
        or subject.owner_user_id != owner.id
        or owner.status != UserStatus.ACTIVE.value
        or ownership.actor_user_id != owner.id
    ):
        raise BodyScanAIOwnershipError("body-scan upload owner authorization failed")
    return subject, owner, WriteIdentity(subject.id, owner.id)


async def _lock_prepared_scope(
    session: AsyncSession,
    prepared: PreparedBodyScanParse,
    *,
    require_active_owner: bool,
) -> _LockedBodyScanScope:
    await acquire_identity_governance_lock(session)
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == prepared._subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    owner = await session.scalar(
        select(User)
        .where(User.id == prepared._owner_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        subject is None
        or owner is None
        or prepared._actor_user_id != prepared._owner_user_id
    ):
        raise BodyScanAIOwnershipError("prepared body-scan owner provenance is missing")
    if require_active_owner and (
        subject.owner_user_id != prepared._owner_user_id
        or owner.status != UserStatus.ACTIVE.value
    ):
        raise BodyScanAIOwnershipError("prepared body-scan owner is no longer active")
    raw = await session.scalar(
        select(RawPayload)
        .where(RawPayload.id == prepared._raw_payload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    asset = await session.scalar(
        select(FileAsset)
        .where(FileAsset.id == prepared._file_asset_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if asset is None or raw is None:
        raise BodyScanAIOwnershipError("prepared body-scan file/raw roots are missing")
    if (
        _asset_fingerprint(asset) != prepared._asset_fingerprint
        or _raw_fingerprint(raw) != prepared._raw_fingerprint
    ):
        raise BodyScanAIOwnershipError("prepared body-scan file/raw roots changed")
    return _LockedBodyScanScope(subject, owner, asset, raw)


def _validate_existing_roots(
    *,
    asset: FileAsset,
    raw: RawPayload,
    identity: WriteIdentity,
    storage_ref: str,
    media_type: str,
    byte_size: int,
    sha256_hex: str,
    storage_backend: FileStorageBackend,
) -> None:
    expected_status = (
        FileAssetStatus.ACTIVE.value
        if storage_backend is FileStorageBackend.PRIVATE_LOCAL
        else FileAssetStatus.LEGACY_PLACEHOLDER.value
    )
    if (
        asset.subject_id != identity.subject_id
        or asset.uploaded_by_user_id != identity.actor_user_id
        or asset.purpose != FileAssetPurpose.BODY_SCAN_DOCUMENT.value
        or asset.storage_backend != storage_backend.value
        or asset.storage_ref != storage_ref
        or asset.media_type != media_type
        or asset.byte_size != byte_size
        or asset.sha256_hex != sha256_hex
        or asset.status != expected_status
        or asset.deleted_at is not None
        or asset.purged_at is not None
    ):
        raise BodyScanAIOwnershipError("body-scan file provenance is inconsistent")
    if (
        raw.subject_id != identity.subject_id
        or raw.actor_user_id != identity.actor_user_id
        or raw.integration_connection_id is not None
        or raw.file_asset_id != asset.id
        or raw.domain != Domain.BODY_COMPOSITION.value
        or raw.source != Source.BODY_SCAN.value
        or raw.external_id != storage_ref
        or raw.processed_at is not None
    ):
        raise BodyScanAIOwnershipError("body-scan raw provenance is inconsistent")
