"""Stream one subject record into an authenticated portability-v2 container."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import BinaryIO

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.services.portability.archive import (
    DEFAULT_ARCHIVE_LIMITS,
    ArchiveLimits,
    write_inner_archive,
)
from vitals.services.portability.crypto import EncryptingWriter
from vitals.services.portability.graph import (
    DEFAULT_GRAPH_LIMITS,
    GraphLimits,
    build_subject_graph,
)
from vitals.services.portability.resources import ResourceLocations
from vitals.services.portability.schema import PORTABILITY_SCHEMA_DIGEST


@dataclass(frozen=True, slots=True)
class SubjectExportResult:
    """Non-PHI control metadata for one fully authenticated output."""

    archive_id: uuid.UUID
    record_ref: str
    schema_digest: str
    table_count: int
    row_count: int
    connection_count: int
    resource_count: int
    plaintext_bytes: int
    encrypted_bytes: int


async def export_subject_encrypted(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    passphrase: str,
    destination: BinaryIO,
    locations: ResourceLocations,
    graph_limits: GraphLimits = DEFAULT_GRAPH_LIMITS,
    archive_limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> SubjectExportResult:
    """Write exactly one personal record without plaintext filesystem output.

    Database access is read-only and never commits.  The caller owns the
    destination and must discard it if this function raises; an aborted writer
    deliberately omits the GCM tag, so a partial result cannot authenticate.
    """

    if not isinstance(session, AsyncSession):
        raise TypeError("session must be an AsyncSession")
    if not isinstance(subject_id, uuid.UUID) or subject_id.int == 0:
        raise TypeError("subject_id must be a non-zero UUID")
    if not hasattr(destination, "write"):
        raise TypeError("destination must be a binary writer")
    if not isinstance(locations, ResourceLocations):
        raise TypeError("locations must be ResourceLocations")

    graph = await build_subject_graph(
        session,
        subject_id=subject_id,
        limits=graph_limits,
    )
    archive_id = uuid.uuid4()
    record_ref = secrets.token_urlsafe(24)
    writer = EncryptingWriter(destination, passphrase=passphrase)
    with writer:
        plaintext_bytes = write_inner_archive(
            graph,
            writer,
            archive_id=archive_id,
            record_ref=record_ref,
            locations=locations,
            limits=archive_limits,
        )
    totals = graph.manifest["totals"]
    return SubjectExportResult(
        archive_id=archive_id,
        record_ref=record_ref,
        schema_digest=PORTABILITY_SCHEMA_DIGEST,
        table_count=totals["tables"],
        row_count=totals["rows"],
        connection_count=totals["connections"],
        resource_count=totals["resources"],
        plaintext_bytes=plaintext_bytes,
        encrypted_bytes=writer.encrypted_size,
    )


__all__ = ["SubjectExportResult", "export_subject_encrypted"]
