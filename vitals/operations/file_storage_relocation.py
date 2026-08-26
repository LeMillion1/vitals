"""Resumable legacy-local to private-local medical-file relocation.

Each successful call moves one ``FileAsset`` graph and commits it independently.
The FileAsset backend is the durable checkpoint, so a stopped run simply resumes
at the next legacy row.  The legacy source is deliberately retained: deleting
it in this phase would need a second durable cleanup checkpoint to remain safe
across a crash immediately after the database commit.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    AuditOutcome,
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
)
from vitals.models.body_scan import BodyScan
from vitals.models.identity import AuditEvent
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.models.weight import ProgressPhoto
from vitals.persistence.file_storage import (
    copy_legacy_file_to_private,
    extension_for_relocation,
    media_type_for_relocation,
    private_storage_ref,
    remove_stored_file,
)
from vitals.persistence.rls import enter_platform_scope

EVENT_TYPE = "file_asset.private_relocated"
MAX_BATCH_SIZE = 100
DEFAULT_BATCH_SIZE = 25
_ELIGIBLE_PURPOSES = frozenset(
    {
        FileAssetPurpose.PROGRESS_PHOTO.value,
        FileAssetPurpose.LAB_DOCUMENT.value,
        FileAssetPurpose.BODY_SCAN_DOCUMENT.value,
    }
)
_ELIGIBLE_STATUSES = frozenset(
    {
        FileAssetStatus.LEGACY_PLACEHOLDER.value,
        FileAssetStatus.PENDING.value,
    }
)


class SessionFactory(Protocol):
    def __call__(self) -> AsyncSession: ...


class FileStorageRelocationError(RuntimeError):
    """A graph or filesystem fact made an asset unsafe to relocate."""


class FileStorageCommitAmbiguous(FileStorageRelocationError):
    """Commit may have succeeded; both byte copies were preserved."""


@dataclass(frozen=True, slots=True)
class RelocationInspection:
    eligible_assets: int
    relocated_assets: int

    def to_safe_dict(self) -> dict[str, int]:
        return {
            "eligible_assets": self.eligible_assets,
            "relocated_assets": self.relocated_assets,
        }


@dataclass(frozen=True, slots=True)
class RelocationRun:
    requested_assets: int
    relocated_assets: int
    remaining_assets: int

    def to_safe_dict(self) -> dict[str, int | bool]:
        return {
            "requested_assets": self.requested_assets,
            "relocated_assets": self.relocated_assets,
            "remaining_assets": self.remaining_assets,
            "completed": self.remaining_assets == 0,
        }


@dataclass(frozen=True, slots=True)
class _PreparedRelocation:
    destination_ref: str


def _eligible_clause():
    return (
        FileAsset.storage_backend == FileStorageBackend.LEGACY_LOCAL.value,
        FileAsset.status.in_(sorted(_ELIGIBLE_STATUSES)),
        FileAsset.purpose.in_(sorted(_ELIGIBLE_PURPOSES)),
        FileAsset.deleted_at.is_(None),
        FileAsset.purged_at.is_(None),
    )


async def inspect(session: AsyncSession) -> RelocationInspection:
    """Return counts only; never project locators, names, or subject IDs."""

    await enter_platform_scope(session)
    eligible = int(
        await session.scalar(
            select(func.count()).select_from(FileAsset).where(*_eligible_clause())
        )
        or 0
    )
    relocated = int(
        await session.scalar(
            select(func.count())
            .select_from(FileAsset)
            .where(
                FileAsset.storage_backend == FileStorageBackend.PRIVATE_LOCAL.value,
                FileAsset.purpose.in_(sorted(_ELIGIBLE_PURPOSES)),
                FileAsset.status == FileAssetStatus.ACTIVE.value,
            )
        )
        or 0
    )
    return RelocationInspection(eligible, relocated)


async def _lock_next_asset(session: AsyncSession) -> FileAsset | None:
    return await session.scalar(
        select(FileAsset)
        .where(*_eligible_clause())
        .order_by(FileAsset.created_at, FileAsset.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )


async def _lock_and_validate_graph(
    session: AsyncSession,
    *,
    asset: FileAsset,
) -> tuple[list[RawPayload], list[BodyScan], list[ProgressPhoto]]:
    raw_rows = list(
        await session.scalars(
            select(RawPayload)
            .where(RawPayload.file_asset_id == asset.id)
            .order_by(RawPayload.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    scans = list(
        await session.scalars(
            select(BodyScan)
            .where(BodyScan.file_asset_id == asset.id)
            .order_by(BodyScan.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    photos = list(
        await session.scalars(
            select(ProgressPhoto)
            .where(ProgressPhoto.file_asset_id == asset.id)
            .order_by(ProgressPhoto.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )

    if any(row.external_id != asset.storage_ref for row in raw_rows):
        raise FileStorageRelocationError("linked raw locator is inconsistent")
    if any(row.file_key != asset.storage_ref for row in scans):
        raise FileStorageRelocationError("linked body-scan locator is inconsistent")
    if any(row.file_key != asset.storage_ref for row in photos):
        raise FileStorageRelocationError("linked progress-photo locator is inconsistent")

    if asset.purpose == FileAssetPurpose.PROGRESS_PHOTO.value:
        if raw_rows or scans or not photos:
            raise FileStorageRelocationError("progress-photo graph is inconsistent")
    elif asset.purpose == FileAssetPurpose.LAB_DOCUMENT.value:
        if scans or photos:
            raise FileStorageRelocationError("lab-document graph is inconsistent")
        if any(row.domain != Domain.LABS.value for row in raw_rows):
            raise FileStorageRelocationError("lab-document raw domain is inconsistent")
    elif asset.purpose == FileAssetPurpose.BODY_SCAN_DOCUMENT.value:
        if photos:
            raise FileStorageRelocationError("body-scan graph is inconsistent")
        if any(row.domain != Domain.BODY_COMPOSITION.value for row in raw_rows):
            raise FileStorageRelocationError("body-scan raw domain is inconsistent")
    else:  # pragma: no cover - filtered in the selecting query
        raise FileStorageRelocationError("file purpose is not eligible")

    # Old subject-funded OpenRouter parses predate FileAsset linkage.  Their raw
    # row and the later registered document can legitimately name the same
    # physical legacy object while retaining different provenance roots.  Move
    # that reviewed alias with the document, but never forge a FileAsset link or
    # replace its historical integration connection.
    raw_aliases = list(
        await session.scalars(
            select(RawPayload)
            .where(
                RawPayload.external_id == asset.storage_ref,
                or_(
                    RawPayload.file_asset_id.is_(None),
                    RawPayload.file_asset_id != asset.id,
                ),
                RawPayload.domain.in_(
                    (Domain.LABS.value, Domain.BODY_COMPOSITION.value)
                ),
            )
            .order_by(RawPayload.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    expected_raw_pair = {
        FileAssetPurpose.LAB_DOCUMENT.value: (
            Domain.LABS.value,
            Source.LAB_PARSER.value,
        ),
        FileAssetPurpose.BODY_SCAN_DOCUMENT.value: (
            Domain.BODY_COMPOSITION.value,
            Source.BODY_SCAN.value,
        ),
    }.get(asset.purpose)
    connection_ids = {
        row.integration_connection_id
        for row in raw_aliases
        if row.integration_connection_id is not None
    }
    connections = {
        row.id: row
        for row in await session.scalars(
            select(IntegrationConnection)
            .where(IntegrationConnection.id.in_(connection_ids))
            .with_for_update()
        )
    }
    for alias in raw_aliases:
        connection = connections.get(alias.integration_connection_id)
        reviewed_historical_alias = (
            expected_raw_pair is not None
            and (alias.domain, alias.source) == expected_raw_pair
            and alias.subject_id == asset.subject_id
            and alias.actor_user_id is None
            and alias.file_asset_id is None
            and connection is not None
            and connection.subject_id == asset.subject_id
            and connection.provider == IntegrationProvider.OPENROUTER.value
            and connection.connection_type
            == IntegrationConnectionType.AI_GATEWAY.value
            and connection.status == IntegrationConnectionStatus.LEGACY.value
        )
        if not reviewed_historical_alias:
            raise FileStorageRelocationError(
                "legacy locator has an ambiguous alias"
            )

    scan_alias = await session.scalar(
        select(BodyScan.id)
        .where(
            BodyScan.file_key == asset.storage_ref,
            or_(BodyScan.file_asset_id.is_(None), BodyScan.file_asset_id != asset.id),
        )
        .limit(1)
        .with_for_update()
    )
    photo_alias = await session.scalar(
        select(ProgressPhoto.id)
        .where(
            ProgressPhoto.file_key == asset.storage_ref,
            or_(
                ProgressPhoto.file_asset_id.is_(None),
                ProgressPhoto.file_asset_id != asset.id,
            ),
        )
        .limit(1)
        .with_for_update()
    )
    if any(value is not None for value in (scan_alias, photo_alias)):
        raise FileStorageRelocationError("legacy locator has an ambiguous alias")
    return [*raw_rows, *raw_aliases], scans, photos


async def _prepare_one(
    session: AsyncSession,
    *,
    static_dir: str,
    private_root: str,
) -> _PreparedRelocation | None:
    asset = await _lock_next_asset(session)
    if asset is None:
        return None
    raw_rows, scans, photos = await _lock_and_validate_graph(session, asset=asset)

    try:
        purpose = FileAssetPurpose(asset.purpose)
        extension = extension_for_relocation(asset.storage_ref)
        media_type = media_type_for_relocation(extension)
        destination_ref = private_storage_ref(purpose, extension)
    except ValueError as exc:
        raise FileStorageRelocationError("legacy locator is not relocatable") from exc

    existing_destination = await session.scalar(
        select(FileAsset.id).where(
            FileAsset.storage_backend == FileStorageBackend.PRIVATE_LOCAL.value,
            FileAsset.storage_ref == destination_ref,
        )
    )
    if existing_destination is not None:
        raise FileStorageRelocationError("generated private locator collided")

    copied = await asyncio.to_thread(
        copy_legacy_file_to_private,
        static_dir=static_dir,
        legacy_storage_ref=asset.storage_ref,
        private_root=private_root,
        private_storage_ref=destination_ref,
        expected_size=asset.byte_size,
        expected_sha256=asset.sha256_hex,
    )
    try:
        for raw in raw_rows:
            raw.external_id = destination_ref
        for scan in scans:
            scan.file_key = destination_ref
        for photo in photos:
            photo.file_key = destination_ref
        changed_fields = [
            "storage_backend",
            "storage_ref",
            "byte_size",
            "sha256_hex",
        ]
        asset.storage_backend = FileStorageBackend.PRIVATE_LOCAL.value
        asset.storage_ref = destination_ref
        asset.byte_size = copied.byte_size
        asset.sha256_hex = copied.sha256_hex
        if asset.media_type is None:
            asset.media_type = media_type
            changed_fields.append("media_type")
        asset.status = FileAssetStatus.ACTIVE.value
        changed_fields.append("status")
        session.add(
            AuditEvent(
                actor_user_id=None,
                subject_id=asset.subject_id,
                support_access_grant_id=None,
                event_type=EVENT_TYPE,
                outcome=AuditOutcome.SUCCESS.value,
                resource_type="file_asset",
                resource_id=str(asset.id),
                metadata_json={
                    "source_surface": "operator",
                    "reason_code": "private_storage_cutover",
                    "resource_type": "file_asset",
                    "resource_id": str(asset.id),
                    "changed_fields": changed_fields,
                    "record_count": 1 + len(raw_rows) + len(scans) + len(photos),
                },
            )
        )
        await session.flush()
    except BaseException:
        try:
            remove_stored_file(
                storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
                storage_ref=destination_ref,
                static_dir=static_dir,
                private_root=private_root,
            )
        except OSError:
            pass
        raise
    return _PreparedRelocation(destination_ref)


async def _commit_one(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    static_dir: str,
    private_root: str,
) -> bool:
    async with session_factory() as session:
        await enter_platform_scope(session)
        prepared: _PreparedRelocation | None = None
        try:
            prepared = await _prepare_one(
                session,
                static_dir=static_dir,
                private_root=private_root,
            )
        except BaseException:
            await session.rollback()
            raise
        if prepared is None:
            await session.rollback()
            return False
        try:
            await session.commit()
        except BaseException as exc:
            # COMMIT acknowledgement loss is not proof of rollback. Both the
            # legacy source and the published destination remain, allowing an
            # operator to inspect the authoritative DB row and resume safely.
            try:
                await session.rollback()
            except BaseException:
                pass
            raise FileStorageCommitAmbiguous(
                "private relocation commit outcome is ambiguous"
            ) from exc
        return True


async def relocate(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    static_dir: str,
    private_root: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> RelocationRun:
    """Commit up to ``batch_size`` independently resumable asset graphs."""

    if isinstance(batch_size, bool) or not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if not os.path.isabs(static_dir) or not os.path.isabs(private_root):
        raise ValueError("storage roots must be absolute")
    static_real = os.path.realpath(static_dir)
    private_real = os.path.realpath(private_root)
    if private_real == static_real or private_real.startswith(static_real + os.sep):
        raise ValueError("private root must be outside static storage")

    moved = 0
    while moved < batch_size and await _commit_one(
        session_factory,
        static_dir=static_real,
        private_root=private_real,
    ):
        moved += 1
    async with session_factory() as session:
        remaining = (await inspect(session)).eligible_assets
    return RelocationRun(batch_size, moved, remaining)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "EVENT_TYPE",
    "FileStorageCommitAmbiguous",
    "FileStorageRelocationError",
    "MAX_BATCH_SIZE",
    "RelocationInspection",
    "RelocationRun",
    "inspect",
    "relocate",
]
