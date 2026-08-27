"""Two-phase retirement of file objects orphaned by record replacement.

Metadata retirement belongs to the replacement transaction.  Physical file
deletion cannot join that transaction and is therefore a separate, explicitly
post-commit operation with one independently retryable checkpoint per object.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import FileAssetStatus, FileStorageBackend
from vitals.models import Base
from vitals.models.identity import HealthSubject
from vitals.models.tenancy import FileAsset
from vitals.persistence import file_storage
from vitals.persistence.rls import bind_session_subject
from vitals.services.files import lifecycle as file_lifecycle


_MAX_RETIREMENT_ASSETS: Final = 10_000
_LOCAL_BACKENDS: Final = frozenset(
    {
        FileStorageBackend.LEGACY_LOCAL.value,
        FileStorageBackend.PRIVATE_LOCAL.value,
    }
)
_LIVE_OR_DELETED_STATUSES: Final = frozenset(
    {
        FileAssetStatus.LEGACY_PLACEHOLDER.value,
        FileAssetStatus.PENDING.value,
        FileAssetStatus.ACTIVE.value,
        FileAssetStatus.DELETED.value,
    }
)


class SessionFactory(Protocol):
    def __call__(self) -> AsyncSession: ...


class FileRetirementError(RuntimeError):
    """A stable, locator-free retirement validation failure."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class _TransactionGuard:
    """Keep the producing transaction observable without exposing its session."""

    __slots__ = ("_transaction",)

    def __init__(self, transaction: object) -> None:
        self._transaction = transaction

    @property
    def active(self) -> bool:
        return bool(getattr(self._transaction, "is_active", False))


@dataclass(frozen=True, slots=True)
class RetiredStorageObject:
    """One exact local object eligible for deletion after replacement commit."""

    file_asset_id: uuid.UUID
    subject_id: uuid.UUID
    storage_backend: str
    storage_ref: str


@dataclass(frozen=True, slots=True)
class FileRetirementPlan:
    """Immutable metadata result; storage locators remain an internal capability."""

    subject_id: uuid.UUID
    objects: tuple[RetiredStorageObject, ...]
    preserved_referenced_asset_ids: tuple[uuid.UUID, ...]
    already_purged_asset_ids: tuple[uuid.UUID, ...]
    _transaction_guard: _TransactionGuard = field(repr=False, compare=False)

    @property
    def retired_asset_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(item.file_asset_id for item in self.objects)


@dataclass(frozen=True, slots=True)
class FilePurgeFailure:
    """Retryable locator-free failure for one planned object."""

    file_asset_id: uuid.UUID
    code: str


@dataclass(frozen=True, slots=True)
class PostCommitPurgeReport:
    """Observable best-effort outcome without private storage locators."""

    requested: int
    purged_asset_ids: tuple[uuid.UUID, ...]
    already_purged_asset_ids: tuple[uuid.UUID, ...]
    failures: tuple[FilePurgeFailure, ...]

    @property
    def complete(self) -> bool:
        return not self.failures


def _error(code: str, detail: str) -> FileRetirementError:
    return FileRetirementError(code, detail)


def _require_subject_id(value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise _error(
            "file_retirement_subject_invalid",
            "file retirement subject identifier is invalid",
        )
    return value


def _asset_ids(values: object) -> tuple[uuid.UUID, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise _error(
            "file_retirement_ids_invalid",
            "old file asset identifiers must be a sequence",
        )
    if len(values) > _MAX_RETIREMENT_ASSETS:
        raise _error(
            "file_retirement_ids_invalid",
            "old file asset identifier count exceeds the hard cap",
        )
    normalized: list[uuid.UUID] = []
    for value in values:
        if not isinstance(value, uuid.UUID) or value.int == 0:
            raise _error(
                "file_retirement_ids_invalid",
                "an old file asset identifier is invalid",
            )
        normalized.append(value)
    if len(normalized) != len(set(normalized)):
        raise _error(
            "file_retirement_ids_invalid",
            "old file asset identifiers are duplicated",
        )
    return tuple(sorted(normalized, key=lambda item: item.hex))


def _file_reference_tables():
    """Discover every declared FK into FileAsset and reject odd shapes."""

    tables = []
    for table in Base.metadata.tables.values():
        if table.name == FileAsset.__tablename__:
            continue
        references_assets = any(
            element.target_fullname == "file_assets.id"
            for constraint in table.foreign_key_constraints
            for element in constraint.elements
        )
        if not references_assets:
            continue
        if "file_asset_id" not in table.c or "subject_id" not in table.c:
            raise _error(
                "file_retirement_contract_invalid",
                "a file reference table lacks an exact ownership shape",
            )
        tables.append(table)
    return tuple(sorted(tables, key=lambda table: table.name))


async def _referenced_asset_ids(
    session: AsyncSession,
    asset_ids: tuple[uuid.UUID, ...],
) -> frozenset[uuid.UUID]:
    if not asset_ids:
        return frozenset()
    referenced: set[uuid.UUID] = set()
    for table in _file_reference_tables():
        rows = await session.scalars(
            select(table.c.file_asset_id)
            .where(table.c.file_asset_id.in_(asset_ids))
            .order_by(table.c.file_asset_id)
            .with_for_update()
        )
        referenced.update(rows)
    return frozenset(referenced)


async def prepare_old_file_retirement(
    session: AsyncSession,
    *,
    target_subject_id: uuid.UUID,
    old_file_asset_ids: Sequence[uuid.UUID],
) -> FileRetirementPlan:
    """Soft-delete unreferenced old assets in the outer replace transaction.

    The IDs are the exact ``ReplacementApplyResult.old_file_asset_ids``.  Every
    row is locked and subject-checked before any mutation.  Any asset still
    referenced after replacement is preserved, including references from
    non-portable control tables.  The function flushes but never commits,
    rolls back, or touches file bytes.
    """

    if not isinstance(session, AsyncSession):
        raise TypeError("session must be an AsyncSession")
    subject_id = _require_subject_id(target_subject_id)
    asset_ids = _asset_ids(old_file_asset_ids)

    with session.no_autoflush:
        subject_exists = await session.scalar(
            select(HealthSubject.id).where(HealthSubject.id == subject_id).with_for_update()
        )
        if subject_exists is None:
            raise _error(
                "file_retirement_subject_not_found",
                "file retirement subject does not exist",
            )
        rows = tuple(
            await session.scalars(
                select(FileAsset)
                .where(FileAsset.id.in_(asset_ids))
                .order_by(FileAsset.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(rows) != len(asset_ids):
            raise _error(
                "file_retirement_scope_invalid",
                "an old file asset does not exist in the target scope",
            )
        if any(row.subject_id != subject_id for row in rows):
            raise _error(
                "file_retirement_scope_invalid",
                "an old file asset does not belong to the target subject",
            )
        if any(row.storage_backend not in _LOCAL_BACKENDS for row in rows):
            raise _error(
                "file_retirement_backend_unsupported",
                "an old file asset is not stored in a local backend",
            )
        if any(
            row.status not in _LIVE_OR_DELETED_STATUSES
            and row.status != FileAssetStatus.PURGED.value
            for row in rows
        ):
            raise _error(
                "file_retirement_state_invalid",
                "an old file asset has an invalid lifecycle state",
            )
        referenced = await _referenced_asset_ids(session, asset_ids)

    objects: list[RetiredStorageObject] = []
    already_purged: list[uuid.UUID] = []
    for row in rows:
        if row.id in referenced:
            continue
        if row.status == FileAssetStatus.PURGED.value:
            already_purged.append(row.id)
            continue
        retired = await file_lifecycle.mark_local_deleted(
            session,
            file_asset_id=row.id,
            subject_id=subject_id,
            purged=False,
        )
        objects.append(
            RetiredStorageObject(
                file_asset_id=retired.id,
                subject_id=subject_id,
                storage_backend=retired.storage_backend,
                storage_ref=retired.storage_ref,
            )
        )
    await session.flush()
    transaction = session.sync_session.get_transaction()
    if transaction is None:  # pragma: no cover - the selects above open one
        raise _error(
            "file_retirement_transaction_invalid",
            "file retirement requires an outer transaction",
        )
    return FileRetirementPlan(
        subject_id=subject_id,
        objects=tuple(objects),
        preserved_referenced_asset_ids=tuple(sorted(referenced, key=lambda item: item.hex)),
        already_purged_asset_ids=tuple(sorted(already_purged, key=lambda item: item.hex)),
        _transaction_guard=_TransactionGuard(transaction),
    )


async def _purge_one(
    session_factory: SessionFactory,
    *,
    item: RetiredStorageObject,
    static_dir: str,
    private_root: str,
) -> str | FilePurgeFailure:
    try:
        async with session_factory() as session:
            if not isinstance(session, AsyncSession):
                return FilePurgeFailure(item.file_asset_id, "purge_session_invalid")
            await bind_session_subject(session, item.subject_id)
            asset = await session.scalar(
                select(FileAsset)
                .where(
                    FileAsset.id == item.file_asset_id,
                    FileAsset.subject_id == item.subject_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if asset is None:
                await session.rollback()
                return FilePurgeFailure(item.file_asset_id, "purge_asset_missing")
            if (
                asset.storage_backend != item.storage_backend
                or asset.storage_ref != item.storage_ref
            ):
                await session.rollback()
                return FilePurgeFailure(item.file_asset_id, "purge_plan_mismatch")
            if asset.status == FileAssetStatus.PURGED.value:
                await session.rollback()
                return "already_purged"
            if asset.status != FileAssetStatus.DELETED.value:
                await session.rollback()
                return FilePurgeFailure(item.file_asset_id, "purge_asset_not_retired")
            if item.file_asset_id in await _referenced_asset_ids(session, (item.file_asset_id,)):
                await session.rollback()
                return FilePurgeFailure(item.file_asset_id, "purge_asset_referenced")
            await asyncio.to_thread(
                file_storage.remove_stored_file,
                storage_backend=item.storage_backend,
                storage_ref=item.storage_ref,
                static_dir=static_dir,
                private_root=private_root,
            )
            await file_lifecycle.mark_local_deleted(
                session,
                file_asset_id=item.file_asset_id,
                subject_id=item.subject_id,
                purged=True,
            )
            await session.commit()
            return "purged"
    except Exception:
        return FilePurgeFailure(item.file_asset_id, "purge_operation_failed")


async def purge_retired_files_post_commit(
    session_factory: SessionFactory,
    *,
    plan: FileRetirementPlan,
    static_dir: str,
    private_root: str,
) -> PostCommitPurgeReport:
    """Best-effort physical purge after the replacement commit completed.

    A still-active producing transaction is rejected before filesystem access.
    Each object is then re-authorized against committed metadata and all current
    FileAsset references.  Removal is idempotent; if the metadata checkpoint
    fails after bytes disappeared, retrying the same plan safely repeats the
    absent-file removal and advances the row to ``purged``.
    """

    if not callable(session_factory):
        raise TypeError("session_factory must be callable")
    if not isinstance(plan, FileRetirementPlan):
        raise TypeError("plan must be a FileRetirementPlan")
    if plan._transaction_guard.active:
        raise _error(
            "file_retirement_not_committed",
            "physical purge requires the replacement transaction to finish",
        )
    if (
        type(static_dir) is not str
        or type(private_root) is not str
        or not os.path.isabs(static_dir)
        or not os.path.isabs(private_root)
    ):
        raise _error(
            "file_retirement_storage_invalid",
            "physical purge requires absolute storage roots",
        )

    purged: list[uuid.UUID] = []
    already_purged: list[uuid.UUID] = list(plan.already_purged_asset_ids)
    failures: list[FilePurgeFailure] = []
    for item in plan.objects:
        result = await _purge_one(
            session_factory,
            item=item,
            static_dir=static_dir,
            private_root=private_root,
        )
        if result == "purged":
            purged.append(item.file_asset_id)
        elif result == "already_purged":
            already_purged.append(item.file_asset_id)
        else:
            failures.append(result)
    return PostCommitPurgeReport(
        requested=len(plan.objects),
        purged_asset_ids=tuple(purged),
        already_purged_asset_ids=tuple(already_purged),
        failures=tuple(failures),
    )


__all__ = [
    "FilePurgeFailure",
    "FileRetirementError",
    "FileRetirementPlan",
    "PostCommitPurgeReport",
    "RetiredStorageObject",
    "prepare_old_file_retirement",
    "purge_retired_files_post_commit",
]
