"""Downloads addressed by a rotatable key instead of by a path.

The old route took the storage path as the URL: ``/static/uploads/labs/x.pdf``.
Three things came with that. The name is a guess away from being known, so the
route had to be careful never to answer differently for a file that exists and
a file that does not. The same bytes had two spellings — ``uploads/labs/x`` and
``labs/x`` — which meant two metadata rows could claim one path and disagree
about whether it was deleted. And the URL could not be withdrawn without moving
the file, because the URL *was* the file.

``FileAsset.opaque_key`` is none of those. It is a UUID with no relationship to
the bytes, unique across the installation, and rotatable: replacing it revokes
every link that ever leaked without touching the row it belongs to or the file
on disk. Storage paths stop appearing in URLs at all, which is what lets the
private tree be sealed off from the static mount entirely.
"""
from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import FileStorageBackend
from vitals.services import file_asset_service
from web.deps import get_session, require_auth
from web.templating import STATIC_DIR
from web.uploads import legacy_upload_disk_path

router = APIRouter(prefix="/files", tags=["files"])

#: Everything below answers with this and only this. A key that is malformed,
#: unknown, another subject's, deleted, purged, or backed by bytes that are no
#: longer on disk are six different facts, and telling them apart is exactly the
#: oracle a holder of a guessed URL would use.
_MISSING = HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@router.get("/{opaque_key}")
async def download(
    opaque_key: str,
    db: AsyncSession = Depends(get_session),
    username: str = Depends(require_auth),
) -> FileResponse:
    """Serve one private medical file to the subject that owns it."""

    try:
        key = uuid.UUID(opaque_key)
    except (ValueError, AttributeError, TypeError):
        raise _MISSING from None

    # A session proves who is driving the browser, not whose medical file this
    # is. Resolve the subject independently and let the lookup do the deciding.
    from vitals.services.legacy_ownership import (
        LegacyOwnershipError,
        resolve_legacy_ownership_context,
    )

    try:
        ownership = await resolve_legacy_ownership_context(db, actor_username=username)
    except LegacyOwnershipError:
        raise _MISSING from None

    try:
        asset = await file_asset_service.resolve_for_download(
            db,
            opaque_key=key,
            subject_id=ownership.subject_id,
        )
    except file_asset_service.FileAssetServiceError:
        raise _MISSING from None

    if asset.storage_backend != FileStorageBackend.LEGACY_LOCAL.value:
        # The only backend that exists today. A future one gets its own branch
        # here rather than a path built from an unvalidated locator.
        raise _MISSING

    try:
        path = legacy_upload_disk_path(STATIC_DIR, asset.storage_ref)
    except ValueError:
        raise _MISSING from None
    if not os.path.isfile(path):
        raise _MISSING

    # Never written to disk cache: the file is readable again on the next
    # request, and a logged-out browser should keep nothing of it.
    headers = {"Cache-Control": "private, no-store"}
    return FileResponse(
        path,
        media_type=asset.media_type or "application/octet-stream",
        headers=headers,
    )
