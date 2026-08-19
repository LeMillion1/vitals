"""Reusable nullable columns for the PR-03 ownership expansion.

The fields stay nullable while the legacy subject is backfilled and every write
path is converted to dual-write.  A later contract migration makes the
appropriate references mandatory.  ``Source`` remains ingestion provenance;
these references identify the subject, originating human, integration, and
private file independently.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column


class SubjectOwnershipMixin:
    """Nullable health-subject boundary used during expand/backfill."""

    subject_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )


class OriginActorMixin:
    """Nullable user who originated a fact; system/history legitimately has none."""

    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )


class IntegrationConnectionOwnershipMixin:
    """Nullable provider/channel connection that produced or consumes a fact."""

    integration_connection_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )


class FileAssetOwnershipMixin:
    """Nullable private-file root linked during the compatibility rollout."""

    file_asset_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("file_assets.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )


__all__ = [
    "FileAssetOwnershipMixin",
    "IntegrationConnectionOwnershipMixin",
    "OriginActorMixin",
    "SubjectOwnershipMixin",
]
