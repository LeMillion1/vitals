"""Reusable ownership columns, in nullable and mandatory forms.

The nullable mixins were the PR-03 expansion: a column could be added and
backfilled while every write path was converted, without a single write failing
in between.  The mandatory mixins are the PR-04 contract — a table whose
ownership registry entry says ``REQUIRED`` uses them, so the model, the
create-all schema, and the contract migration state the same guarantee.

Which form a table gets is not a style choice: it is read off
:data:`vitals.ownership.OWNERSHIP_REGISTRY`, and a test compares the two so a
table cannot claim ``REQUIRED`` while carrying a nullable column, or the other
way round.  ``MIXED``, ``OPTIONAL`` and ``INHERITED`` tables keep the nullable
form, because for them a missing reference is a real state: a curated catalog
row belongs to nobody, a platform alert belongs to no patient, and a manually
entered fact arrived through no integration.

``Source`` remains ingestion provenance; these references identify the subject,
originating human, integration, and private file independently.
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


class RequiredSubjectOwnershipMixin:
    """Mandatory health-subject boundary: this row is somebody's health data.

    Every table registered ``subject=REQUIRED`` uses this. A row here without a
    subject is not "not yet migrated" — the backfill has run and the contract
    migration refused to proceed while any were left — it is a row no scoped
    reader could return and no scoped unique key could constrain.
    """

    subject_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("health_subjects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


class RequiredIntegrationConnectionOwnershipMixin:
    """Mandatory connection: this row exists only because a provider sent it.

    Used by the vendor tables, where every row arrived through an account
    connection. A weigh-in typed into the web form has no connection and stays
    on the nullable mixin; a Garmin day that has none is unattributable.
    """

    integration_connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


class RequiredFileAssetOwnershipMixin:
    """Mandatory private-file root: the row is a pointer, the asset is the file."""

    file_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("file_assets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


__all__ = [
    "FileAssetOwnershipMixin",
    "IntegrationConnectionOwnershipMixin",
    "OriginActorMixin",
    "RequiredFileAssetOwnershipMixin",
    "RequiredIntegrationConnectionOwnershipMixin",
    "RequiredSubjectOwnershipMixin",
    "SubjectOwnershipMixin",
]
