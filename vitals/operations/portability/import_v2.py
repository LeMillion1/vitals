"""Atomic orchestration for one already-authorized portability-v2 replace.

The caller authenticates the owner approval and opens the validated archive.
This operation owns the database transaction from the idempotency check through
the completion receipt.  Private files cannot participate in that transaction,
so newly staged objects are removed after a definite rollback and deliberately
preserved when a failed commit cannot be reconciled authoritatively.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import AsyncContextManager, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import FileStorageBackend
from vitals.persistence import file_storage
from vitals.services.portability.archive_reader import ValidatedArchive
from vitals.services.portability.connection_mapping import (
    FORMAT_NAME as CONNECTION_MAPPING_FORMAT,
    FORMAT_VERSION as CONNECTION_MAPPING_VERSION,
    CanonicalConnectionMapping,
    resolve_connection_mapping,
)
from vitals.services.portability.receipts import (
    ImportReceiptRequest,
    ImportReceiptResult,
    ReceiptServiceError,
    find_import_receipt,
    record_completed_import,
)
from vitals.services.portability.file_retirement import (
    FileRetirementPlan,
    prepare_old_file_retirement,
)
from vitals.services.portability.record_decoder import (
    DecodedRecord,
    decode_validated_record,
)
from vitals.services.portability.replacement_apply import (
    ReplacementApplyResult,
    apply_record_replacement,
)
from vitals.services.portability.replacement_preflight import prepare_replacement_preflight
from vitals.services.portability.resource_staging import (
    NewlyWrittenPrivateObject,
    ResourceStagingError,
    stage_record_resources,
)


class SessionFactory(Protocol):
    """Minimal async-session factory shape used by application and tests."""

    def __call__(self) -> AsyncContextManager[AsyncSession]: ...


class ImportV2OperationError(RuntimeError):
    """A PHI-free coordinator failure requiring no interpretation of payloads."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        preserved_objects: tuple[NewlyWrittenPrivateObject, ...] = (),
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.preserved_objects = preserved_objects


@dataclass(frozen=True, slots=True)
class ImportV2Result:
    """Authoritative receipt plus side-effect metadata safe for the caller."""

    receipt: ImportReceiptResult
    apply_result: ReplacementApplyResult | None
    newly_written_objects: tuple[NewlyWrittenPrivateObject, ...]
    retirement_plan: FileRetirementPlan | None

    @property
    def replayed(self) -> bool:
        return self.receipt.replayed


def _error(
    code: str,
    detail: str,
    *,
    preserved_objects: tuple[NewlyWrittenPrivateObject, ...] = (),
) -> ImportV2OperationError:
    return ImportV2OperationError(
        code,
        detail,
        preserved_objects=preserved_objects,
    )


def _require_uuid(value: object, *, field: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise _error("import_identity_invalid", f"{field} must be a non-zero UUID")
    return value


def _validated_record(archive: ValidatedArchive, record: DecodedRecord) -> None:
    if not isinstance(archive, ValidatedArchive):
        raise TypeError("archive must be a ValidatedArchive")
    if not isinstance(record, DecodedRecord):
        raise TypeError("record must be a DecodedRecord")
    # Re-decoding through the public authenticated reader is intentional.  It
    # prevents a hand-constructed DecodedRecord from borrowing another
    # archive's receipt identity, including on the replay fast path.
    if decode_validated_record(archive) != record:
        raise _error(
            "import_record_mismatch",
            "decoded record does not match the validated archive",
        )


def _mapping_digest(
    *,
    subject_id: uuid.UUID,
    record: DecodedRecord,
    connection_ids_by_ref: Mapping[str, uuid.UUID],
) -> str:
    """Compute the public canonical mapping fingerprint without a DB lookup.

    A completed replay must remain available if a connection is later retired.
    The new-import path compares this fingerprint with the resolver's result,
    making drift between this pure replay calculation and live validation a
    fail-closed error.
    """

    if not isinstance(connection_ids_by_ref, Mapping):
        raise _error("import_connection_mapping_invalid", "connection mapping must be an object")
    descriptors = tuple(sorted(record.connections, key=lambda item: item.ref))
    expected_refs = {item.ref for item in descriptors}
    copied: dict[str, uuid.UUID] = {}
    for ref, connection_id in connection_ids_by_ref.items():
        if type(ref) is not str or ref not in expected_refs:
            raise _error(
                "import_connection_mapping_invalid",
                "connection mapping contains an invalid archive ref",
            )
        copied[ref] = _require_uuid(connection_id, field="connection id")
    if set(copied) != expected_refs:
        raise _error(
            "import_connection_mapping_incomplete",
            "connection mapping must cover every archive connection",
        )
    if len(set(copied.values())) != len(copied):
        raise _error(
            "import_connection_mapping_not_one_to_one",
            "one live connection cannot satisfy multiple archive refs",
        )
    body = {
        "connections": [
            {
                "connection_id": str(copied[item.ref]),
                "connection_type": item.connection_type,
                "provider": item.provider,
                "ref": item.ref,
            }
            for item in descriptors
        ],
        "format": CONNECTION_MAPPING_FORMAT,
        "target_subject_id": str(subject_id),
        "version": CONNECTION_MAPPING_VERSION,
    }
    canonical = json.dumps(
        body,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _receipt_request(
    *,
    archive: ValidatedArchive,
    record: DecodedRecord,
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    operation_id: uuid.UUID,
    mapping_digest: str,
) -> ImportReceiptRequest:
    return ImportReceiptRequest(
        subject_id=subject_id,
        actor_user_id=actor_user_id,
        operation_id=operation_id,
        archive_id=archive.archive_id,
        manifest_digest=archive.manifest_digest,
        record_ref=archive.record_ref,
        record_digest=archive.record_digest,
        mapping_digest=mapping_digest,
        row_count=record.row_count,
        resource_count=len(record.resources),
    )


def _exact_replay(
    existing: ImportReceiptResult,
    request: ImportReceiptRequest,
) -> ImportReceiptResult:
    if existing.request != request:
        raise ReceiptServiceError(
            "receipt_metadata_mismatch",
            "idempotency key belongs to different receipt metadata",
        )
    return existing


def _cleanup_staged(
    objects: tuple[NewlyWrittenPrivateObject, ...],
    *,
    private_root: str,
) -> tuple[NewlyWrittenPrivateObject, ...]:
    failed: list[NewlyWrittenPrivateObject] = []
    for item in reversed(objects):
        try:
            file_storage.remove_stored_file(
                storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
                storage_ref=item.storage_ref,
                static_dir=private_root,
                private_root=private_root,
            )
        except Exception:
            failed.append(item)
    return tuple(reversed(failed))


async def _reconcile_receipt(
    session_factory: SessionFactory,
    *,
    request: ImportReceiptRequest,
) -> ImportReceiptResult | None:
    async with session_factory() as session:
        if not isinstance(session, AsyncSession):
            raise TypeError("session_factory must yield an AsyncSession")
        found = await find_import_receipt(
            session,
            subject_id=request.subject_id,
            operation_id=request.operation_id,
        )
        if found is None:
            return None
        return _exact_replay(found, request)


async def import_validated_record_v2(
    session_factory: SessionFactory,
    *,
    archive: ValidatedArchive,
    record: DecodedRecord,
    target_subject_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    operation_id: uuid.UUID,
    connection_ids_by_ref: Mapping[str, uuid.UUID],
    private_root: str,
) -> ImportV2Result:
    """Atomically replace one subject and commit exactly one completion receipt.

    The target, actor, operation identifier and explicit connection choices are
    owner-approved inputs supplied by an authenticated delivery boundary.  This
    API accepts no passphrase and retains no plaintext archive representation.
    """

    if not callable(session_factory):
        raise TypeError("session_factory must be callable")
    subject_id = _require_uuid(target_subject_id, field="target subject id")
    actor_id = _require_uuid(actor_user_id, field="actor user id")
    operation_id = _require_uuid(operation_id, field="operation id")
    if type(private_root) is not str:
        raise _error("import_private_root_invalid", "private root is invalid")
    # Staging performs the authoritative absolute-path check.  Checking here
    # keeps resource-free imports from accidentally accepting a relative root.
    if not os.path.isabs(private_root):
        raise _error("import_private_root_invalid", "private root must be absolute")
    _validated_record(archive, record)
    expected_mapping_digest = _mapping_digest(
        subject_id=subject_id,
        record=record,
        connection_ids_by_ref=connection_ids_by_ref,
    )
    request = _receipt_request(
        archive=archive,
        record=record,
        subject_id=subject_id,
        actor_user_id=actor_id,
        operation_id=operation_id,
        mapping_digest=expected_mapping_digest,
    )

    staged_objects: tuple[NewlyWrittenPrivateObject, ...] = ()
    commit_started = False
    apply_result: ReplacementApplyResult | None = None
    retirement_plan: FileRetirementPlan | None = None
    receipt: ImportReceiptResult | None = None
    try:
        async with session_factory() as session:
            if not isinstance(session, AsyncSession):
                raise TypeError("session_factory must yield an AsyncSession")
            try:
                existing = await find_import_receipt(
                    session,
                    subject_id=subject_id,
                    operation_id=operation_id,
                )
                if existing is not None:
                    replay = _exact_replay(existing, request)
                    await session.rollback()
                    return ImportV2Result(
                        receipt=replay,
                        apply_result=None,
                        newly_written_objects=(),
                        retirement_plan=None,
                    )

                connection_mapping: CanonicalConnectionMapping = await resolve_connection_mapping(
                    session,
                    target_subject_id=subject_id,
                    archive_connections=record.connections,
                    connection_ids_by_ref=connection_ids_by_ref,
                )
                if connection_mapping.sha256_hex != expected_mapping_digest:
                    raise _error(
                        "import_mapping_digest_mismatch",
                        "resolved connection mapping is not canonical",
                    )
                preflight = await prepare_replacement_preflight(
                    session,
                    subject_id=subject_id,
                    actor_user_id=actor_id,
                )
                staged = await stage_record_resources(
                    session,
                    archive=archive,
                    record=record,
                    target_subject_id=subject_id,
                    actor_user_id=actor_id,
                    private_root=private_root,
                )
                staged_objects = staged.newly_written_objects
                apply_result = await apply_record_replacement(
                    session,
                    target_subject_id=subject_id,
                    record=record,
                    connection_mapping=connection_mapping,
                    resource_bindings=staged,
                    retained_raw_payload_ids=preflight.retained_raw_payload_ids,
                )
                retirement_plan = await prepare_old_file_retirement(
                    session,
                    target_subject_id=subject_id,
                    old_file_asset_ids=apply_result.old_file_asset_ids,
                )
                receipt = await record_completed_import(session, request)
                if receipt.replayed:
                    raise _error(
                        "import_receipt_race",
                        "completion receipt changed during replacement",
                    )
                commit_started = True
                await session.commit()
            except BaseException as exc:
                if isinstance(exc, ResourceStagingError):
                    staged_objects = exc.newly_written_objects
                if commit_started:
                    raise
                rollback_error: BaseException | None = None
                try:
                    await session.rollback()
                except BaseException as rollback_exc:
                    rollback_error = rollback_exc
                failed_cleanup = _cleanup_staged(
                    staged_objects,
                    private_root=private_root,
                )
                if failed_cleanup:
                    raise _error(
                        "import_rollback_cleanup_failed",
                        "staged object cleanup was incomplete after import failure",
                        preserved_objects=failed_cleanup,
                    ) from (rollback_error or exc)
                if rollback_error is not None:
                    raise _error(
                        "import_rollback_failed",
                        "import failed and database rollback did not complete",
                    ) from rollback_error
                raise
    except BaseException as exc:
        if not commit_started:
            raise
        # A commit exception is not proof of rollback.  Re-open an independent
        # session and trust success only when the exact receipt is visible.
        try:
            reconciled = await _reconcile_receipt(session_factory, request=request)
        except BaseException as reconcile_exc:
            raise _error(
                "import_commit_outcome_unknown",
                "commit failed and no authoritative receipt could be reconciled",
                preserved_objects=staged_objects,
            ) from reconcile_exc
        if reconciled is not None:
            return ImportV2Result(
                receipt=reconciled,
                apply_result=apply_result,
                newly_written_objects=staged_objects,
                retirement_plan=retirement_plan,
            )
        failed_cleanup = _cleanup_staged(staged_objects, private_root=private_root)
        if failed_cleanup:
            raise _error(
                "import_commit_failed_cleanup_incomplete",
                "commit was absent but staged object cleanup was incomplete",
                preserved_objects=failed_cleanup,
            ) from exc
        raise _error(
            "import_commit_not_applied",
            "commit failed and no completion receipt exists",
        ) from exc

    if receipt is None or apply_result is None:
        raise _error("import_result_invalid", "import completed without authoritative metadata")
    return ImportV2Result(
        receipt=receipt,
        apply_result=apply_result,
        newly_written_objects=staged_objects,
        retirement_plan=retirement_plan,
    )


__all__ = [
    "ImportV2OperationError",
    "ImportV2Result",
    "SessionFactory",
    "import_validated_record_v2",
]
