"""Flush-only, PHI-free idempotency receipts for portability-v2 imports."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.portability import (
    PORTABILITY_IMPORT_MODE_REPLACE,
    PORTABILITY_RECORD_REF_MAX_LENGTH,
    PortabilityImportReceipt,
)


_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_RECORD_REF_RE: Final = re.compile(rf"[A-Za-z0-9_-]{{1,{PORTABILITY_RECORD_REF_MAX_LENGTH}}}\Z")
_MAX_BIGINT: Final = 2**63 - 1


class ReceiptServiceError(RuntimeError):
    """A stable, PHI-free receipt validation or replay failure."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, slots=True)
class ImportReceiptRequest:
    """Immutable metadata proving exactly one completed record application."""

    subject_id: uuid.UUID
    actor_user_id: uuid.UUID
    operation_id: uuid.UUID
    archive_id: uuid.UUID
    manifest_digest: str
    record_ref: str
    record_digest: str
    mapping_digest: str
    row_count: int
    resource_count: int
    mode: str = PORTABILITY_IMPORT_MODE_REPLACE

    def __post_init__(self) -> None:
        for field_name in (
            "subject_id",
            "actor_user_id",
            "operation_id",
            "archive_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, uuid.UUID) or value.int == 0:
                raise _error("receipt_uuid_invalid", "receipt UUID metadata is invalid")
        for field_name in (
            "manifest_digest",
            "record_digest",
            "mapping_digest",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
                raise _error("receipt_digest_invalid", "receipt digest metadata is invalid")
        if type(self.record_ref) is not str or _RECORD_REF_RE.fullmatch(self.record_ref) is None:
            raise _error("receipt_record_ref_invalid", "receipt record ref is invalid")
        for field_name in ("row_count", "resource_count"):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_BIGINT
            ):
                raise _error("receipt_count_invalid", "receipt count is invalid")
        if type(self.mode) is not str or self.mode != PORTABILITY_IMPORT_MODE_REPLACE:
            raise _error("receipt_mode_invalid", "receipt mode is unsupported")


@dataclass(frozen=True, slots=True)
class ImportReceiptResult:
    """Immutable create/replay result containing no payload or display text."""

    request: ImportReceiptRequest
    receipt_id: uuid.UUID
    completed_at: datetime
    replayed: bool

    @property
    def created(self) -> bool:
        return not self.replayed


def _error(code: str, detail: str) -> ReceiptServiceError:
    return ReceiptServiceError(code, detail)


def _validate_lookup_uuid(value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise _error("receipt_uuid_invalid", "receipt UUID metadata is invalid")
    return value


def _request_values(request: ImportReceiptRequest) -> dict[str, object]:
    return {
        "subject_id": request.subject_id,
        "actor_user_id": request.actor_user_id,
        "operation_id": request.operation_id,
        "archive_id": request.archive_id,
        "manifest_digest": request.manifest_digest,
        "record_ref": request.record_ref,
        "record_digest": request.record_digest,
        "mapping_digest": request.mapping_digest,
        "mode": request.mode,
        "row_count": request.row_count,
        "resource_count": request.resource_count,
    }


def _model_request(receipt: PortabilityImportReceipt) -> ImportReceiptRequest:
    try:
        return ImportReceiptRequest(
            subject_id=receipt.subject_id,
            actor_user_id=receipt.actor_user_id,
            operation_id=receipt.operation_id,
            archive_id=receipt.archive_id,
            manifest_digest=receipt.manifest_digest,
            record_ref=receipt.record_ref,
            record_digest=receipt.record_digest,
            mapping_digest=receipt.mapping_digest,
            mode=receipt.mode,
            row_count=receipt.row_count,
            resource_count=receipt.resource_count,
        )
    except ReceiptServiceError:
        raise _error(
            "receipt_persisted_metadata_invalid",
            "persisted receipt metadata is invalid",
        ) from None


async def _find_model(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    operation_id: uuid.UUID,
) -> PortabilityImportReceipt | None:
    return await session.scalar(
        select(PortabilityImportReceipt)
        .where(
            PortabilityImportReceipt.subject_id == subject_id,
            PortabilityImportReceipt.operation_id == operation_id,
        )
        .execution_options(populate_existing=True)
    )


async def _result(
    session: AsyncSession,
    receipt: PortabilityImportReceipt,
    *,
    replayed: bool,
) -> ImportReceiptResult:
    if receipt.id is None or receipt.completed_at is None:
        await session.refresh(receipt)
    if not isinstance(receipt.id, uuid.UUID) or not isinstance(receipt.completed_at, datetime):
        raise _error(
            "receipt_persisted_metadata_invalid",
            "persisted receipt control metadata is invalid",
        )
    return ImportReceiptResult(
        request=_model_request(receipt),
        receipt_id=receipt.id,
        completed_at=receipt.completed_at,
        replayed=replayed,
    )


async def _replay_or_conflict(
    session: AsyncSession,
    receipt: PortabilityImportReceipt,
    request: ImportReceiptRequest,
) -> ImportReceiptResult:
    persisted_request = _model_request(receipt)
    if persisted_request != request:
        raise _error(
            "receipt_metadata_mismatch",
            "idempotency key belongs to different receipt metadata",
        )
    return await _result(session, receipt, replayed=True)


async def find_import_receipt(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    operation_id: uuid.UUID,
) -> ImportReceiptResult | None:
    """Look up one receipt by its complete subject-scoped idempotency key."""

    if not isinstance(session, AsyncSession):
        raise TypeError("session must be an AsyncSession")
    subject_id = _validate_lookup_uuid(subject_id)
    operation_id = _validate_lookup_uuid(operation_id)
    receipt = await _find_model(
        session,
        subject_id=subject_id,
        operation_id=operation_id,
    )
    if receipt is None:
        return None
    return await _result(session, receipt, replayed=True)


async def record_completed_import(
    session: AsyncSession,
    request: ImportReceiptRequest,
) -> ImportReceiptResult:
    """Flush one completed import receipt or return its exact replay.

    The caller owns the outer transaction and commit.  PostgreSQL gets a
    savepoint so a concurrent unique-key loser can re-read the winner without
    poisoning that outer transaction.  SQLite deliberately uses an ordinary
    flush: releasing its first savepoint can commit an otherwise implicit outer
    transaction, violating this service's rollback contract.
    """

    if not isinstance(session, AsyncSession):
        raise TypeError("session must be an AsyncSession")
    if not isinstance(request, ImportReceiptRequest):
        raise TypeError("request must be an ImportReceiptRequest")
    existing = await _find_model(
        session,
        subject_id=request.subject_id,
        operation_id=request.operation_id,
    )
    if existing is not None:
        return await _replay_or_conflict(session, existing, request)

    receipt = PortabilityImportReceipt(**_request_values(request))
    if session.get_bind().dialect.name != "postgresql":
        session.add(receipt)
        try:
            await session.flush()
        except IntegrityError:
            # Compatibility databases are not the production concurrency truth.
            # Do not rollback or commit the caller's transaction behind its back.
            raise _error(
                "receipt_insert_conflict",
                "receipt insert conflicted; caller transaction requires rollback",
            ) from None
        return await _result(session, receipt, replayed=False)

    try:
        async with session.begin_nested():
            session.add(receipt)
            await session.flush()
    except IntegrityError:
        winner = await _find_model(
            session,
            subject_id=request.subject_id,
            operation_id=request.operation_id,
        )
        if winner is None:
            raise _error(
                "receipt_insert_conflict",
                "receipt insert failed without an authoritative replay",
            ) from None
        return await _replay_or_conflict(session, winner, request)
    return await _result(session, receipt, replayed=False)


__all__ = [
    "ImportReceiptRequest",
    "ImportReceiptResult",
    "ReceiptServiceError",
    "find_import_receipt",
    "record_completed_import",
]
