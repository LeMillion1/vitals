"""Subject-scoped validation for persisted medical-upload references.

The browser upload preview necessarily carries an integer raw-payload id and a
legacy storage key back to the confirm endpoint.  Neither value is an authority:
the authenticated subject is resolved independently, then this service locks and
validates the complete ``subject -> raw payload -> file asset`` chain before a
normalized fact may reference it.

This module is deliberately storage-agnostic.  It does not read, write, or delete
file bytes and it never commits; callers own the surrounding transaction.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    FileAssetPurpose,
    FileAssetStatus,
)
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset
from vitals.ownership import WriteIdentity


class UploadOwnershipError(ValueError):
    """A client-supplied upload reference is invalid in the subject scope."""


@dataclass(frozen=True, slots=True)
class OwnedUploadReference:
    """Authoritative rows behind one uploaded-document preview."""

    raw_payload: RawPayload
    file_asset: FileAsset

    @property
    def storage_ref(self) -> str:
        return self.file_asset.storage_ref


def _coerce_purpose(purpose: FileAssetPurpose | str) -> FileAssetPurpose:
    try:
        return FileAssetPurpose(purpose)
    except (TypeError, ValueError) as exc:
        raise UploadOwnershipError("unsupported upload purpose") from exc


async def resolve_owned_upload_reference(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    raw_payload_id: int,
    client_storage_ref: str | None,
    domain: str,
    source: str,
    purpose: FileAssetPurpose | str,
) -> OwnedUploadReference:
    """Lock and validate one subject-owned raw/file pair.

    ``client_storage_ref`` is checked only as a stale/tampering signal.  The
    returned storage key always comes from ``FileAsset``; callers must not reuse
    the client value after this function returns.
    """

    if not isinstance(identity, WriteIdentity):
        raise UploadOwnershipError("identity must be a WriteIdentity")
    if not isinstance(raw_payload_id, int) or isinstance(raw_payload_id, bool):
        raise UploadOwnershipError("raw_payload_id must be an integer")
    if raw_payload_id <= 0:
        raise UploadOwnershipError("raw_payload_id must be positive")
    if client_storage_ref is not None and not isinstance(client_storage_ref, str):
        raise UploadOwnershipError("file_key must be a string or null")
    normalized_purpose = _coerce_purpose(purpose)

    raw = await session.scalar(
        select(RawPayload)
        .where(
            RawPayload.id == raw_payload_id,
            RawPayload.subject_id == identity.subject_id,
        )
        .with_for_update()
    )
    if raw is None:
        # Do not distinguish a missing id from a row owned by another subject.
        raise UploadOwnershipError("upload reference does not exist in subject scope")
    if raw.domain != domain or raw.source != source:
        raise UploadOwnershipError("upload reference has the wrong domain or source")
    if raw.file_asset_id is None:
        raise UploadOwnershipError("upload reference has no owned file asset")

    asset = await session.scalar(
        select(FileAsset)
        .where(
            FileAsset.id == raw.file_asset_id,
            FileAsset.subject_id == identity.subject_id,
            FileAsset.purpose == normalized_purpose.value,
        )
        .with_for_update()
    )
    if asset is None:
        raise UploadOwnershipError("file asset does not exist in subject scope")
    if asset.status in {
        FileAssetStatus.DELETED.value,
        FileAssetStatus.PURGED.value,
    }:
        raise UploadOwnershipError("file asset is no longer available")
    if raw.external_id != asset.storage_ref:
        raise UploadOwnershipError("raw payload and file asset are not linked")
    if client_storage_ref is not None and client_storage_ref != asset.storage_ref:
        raise UploadOwnershipError("file_key does not match the owned upload")

    return OwnedUploadReference(raw_payload=raw, file_asset=asset)


__all__ = [
    "OwnedUploadReference",
    "UploadOwnershipError",
    "resolve_owned_upload_reference",
]
