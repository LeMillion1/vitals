"""Bounded, resumable ownership backfill for the reviewed raw-payload slice.

The service owns no transaction boundary.  Mutation APIs acquire the canonical
identity/root locks, mutate one checkpoint/batch, and flush; callers commit or
roll back.  The preflight API is deliberately read-only and projects no raw JSON.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    Domain,
    FileAssetPurpose,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.ai import AIInvocation
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.services.identity.governance import acquire_identity_governance_lock
from vitals.services.tenancy.bootstrap import LEGACY_ACCOUNT_DISCRIMINATOR
from vitals.utils.timeutils import now_utc

RAW_OWNERSHIP_BACKFILL_PHASE = "stage3.raw_payloads.v1"
DEFAULT_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE = 250
MAX_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE = 1000

_PREFLIGHT_PAGE_SIZE = 1000
_PAYLOAD_MATERIALIZATION_ROWS = 1
_SIGNED_BIGINT_MAX = (1 << 63) - 1
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_GARMIN_PAIRS = frozenset(
    {
        (Domain.GARMIN.value, Source.GARMIN_API.value),
        (Domain.GARMIN.value, Source.HEALTH_AUTO_EXPORT.value),
    }
)
_HEVY_PAIRS = frozenset({(Domain.WORKOUTS.value, Source.HEVY_API.value)})
# Literal strings, not enum members: these classify raw payloads the Telegram
# bot wrote before it was removed. ``Domain.SIGNALS`` is gone because nothing
# writes it any more, and a stored value that no live domain matches is exactly
# what this pair is for.
_TELEGRAM_PAIRS = frozenset({("signals", "telegram")})
_PARSER_PAIRS = frozenset(
    {
        (Domain.LABS.value, Source.LAB_PARSER.value),
        (Domain.BODY_COMPOSITION.value, Source.BODY_SCAN.value),
    }
)
_CONNECTIONLESS_PAIRS = frozenset(
    {
        (Domain.LABS.value, Source.MCP.value),
        (Domain.BODY_COMPOSITION.value, Source.MCP.value),
        (Domain.GENETICS.value, Source.VCF_IMPORT.value),
    }
)
_ALLOWED_PAIRS = (
    _GARMIN_PAIRS
    | _HEVY_PAIRS
    | _TELEGRAM_PAIRS
    | _PARSER_PAIRS
    | _CONNECTIONLESS_PAIRS
)
_PAIR_CONNECTION_ROOT: dict[
    tuple[str, str], tuple[IntegrationProvider, IntegrationConnectionType]
] = {
    **{
        pair: (IntegrationProvider.GARMIN, IntegrationConnectionType.ACCOUNT)
        for pair in _GARMIN_PAIRS
    },
    **{
        pair: (IntegrationProvider.HEVY, IntegrationConnectionType.ACCOUNT)
        for pair in _HEVY_PAIRS
    },
    **{
        pair: (IntegrationProvider.TELEGRAM, IntegrationConnectionType.RECIPIENT)
        for pair in _TELEGRAM_PAIRS
    },
    **{
        pair: (IntegrationProvider.OPENROUTER, IntegrationConnectionType.AI_GATEWAY)
        for pair in _PARSER_PAIRS
    },
}
_INFERABLE_CONNECTION_STATUSES = frozenset(
    {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    }
)
_KNOWN_CONNECTION_STATUSES = frozenset(
    item.value for item in IntegrationConnectionStatus
)
_KNOWN_PROVIDERS = frozenset(item.value for item in IntegrationProvider)
_KNOWN_CONNECTION_TYPES = frozenset(item.value for item in IntegrationConnectionType)
#: Domains a raw payload may carry. The live enum, plus the ones a removed
#: feature stamped on rows that are still in the lake — this backfill's whole
#: job is classifying what is already there, and "the app no longer writes it"
#: is not the same as "no row has it".
_RETIRED_DOMAINS = frozenset({"signals"})
_KNOWN_DOMAINS = frozenset(item.value for item in Domain) | _RETIRED_DOMAINS
_KNOWN_SOURCES = frozenset(item.value for item in Source)


class RawOwnershipBackfillStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"
    RESTORE_BLOCKED = "restore_blocked"


class RawOwnershipBackfillError(RuntimeError):
    """Base class for fail-closed raw ownership backfill failures."""


class RawOwnershipBackfillValidationError(RawOwnershipBackfillError, ValueError):
    """A caller argument or persisted scalar cannot be represented safely."""


class RawOwnershipBackfillIdentityError(RawOwnershipBackfillError):
    """The database does not have exactly one active authoritative owner."""


class RawOwnershipBackfillStateError(RawOwnershipBackfillError):
    """Checkpoint progress or a raw ownership shape is internally inconsistent."""


class RawOwnershipBackfillMappingError(RawOwnershipBackfillError):
    """A reviewed domain/source row has no unambiguous ownership-root mapping."""


class RawOwnershipBackfillDuplicateError(RawOwnershipBackfillError):
    """Two rows would occupy the same future scoped raw-payload key."""


@dataclass(frozen=True, slots=True)
class RawOwnershipBackfillPreflightResult:
    phase_key: str
    subject_id: uuid.UUID
    status: RawOwnershipBackfillStatus
    scan_high_watermark_id: int
    snapshot_rows: int
    last_scanned_id: int
    scanned_rows: int
    updated_rows: int
    unchanged_rows: int
    remaining_rows: int
    rows_above_high_watermark: int
    data_checksum_before: str
    data_checksum_after: str
    ownership_checksum_after: str

    @property
    def completed(self) -> bool:
        return self.status is RawOwnershipBackfillStatus.COMPLETED

    def to_safe_dict(self) -> dict[str, str | int]:
        """Return the fixed non-PHI CLI/status projection.

        Subject and raw-row identifiers intentionally remain available only on
        the typed in-process result.
        """

        return {
            "phase_key": self.phase_key,
            "status": self.status.value,
            "snapshot_rows": self.snapshot_rows,
            "scanned_rows": self.scanned_rows,
            "updated_rows": self.updated_rows,
            "unchanged_rows": self.unchanged_rows,
            "remaining_rows": self.remaining_rows,
            "rows_above_high_watermark": self.rows_above_high_watermark,
            "data_checksum_before": self.data_checksum_before,
            "data_checksum_after": self.data_checksum_after,
            "ownership_checksum_after": self.ownership_checksum_after,
        }


@dataclass(frozen=True, slots=True)
class RawOwnershipBackfillBatchResult(RawOwnershipBackfillPreflightResult):
    batch_scanned_rows: int
    batch_updated_rows: int
    batch_unchanged_rows: int

    @property
    def changed(self) -> bool:
        return self.batch_updated_rows > 0

    def to_safe_dict(self) -> dict[str, str | int]:
        projection = RawOwnershipBackfillPreflightResult.to_safe_dict(self)
        projection.update(
            {
                "batch_scanned_rows": self.batch_scanned_rows,
                "batch_updated_rows": self.batch_updated_rows,
                "batch_unchanged_rows": self.batch_unchanged_rows,
            }
        )
        return projection


@dataclass(frozen=True, slots=True)
class RawOwnershipBackfillRestoreBlockedResult:
    phase_key: str
    subject_id: uuid.UUID
    status: RawOwnershipBackfillStatus
    scan_high_watermark_id: int
    snapshot_rows: int
    data_checksum_before: str
    data_checksum_after: str
    ownership_checksum_after: str


@dataclass(slots=True)
class _Scope:
    subject_id: uuid.UUID
    owner_user_id: uuid.UUID
    connections: dict[uuid.UUID, Any]


@dataclass(frozen=True, slots=True)
class _ConnectionProjection:
    id: uuid.UUID
    subject_id: uuid.UUID
    provider: str
    connection_type: str
    external_account_discriminator: str
    status: str


@dataclass(frozen=True, slots=True)
class _CheckpointProjection:
    phase_key: str
    subject_id: uuid.UUID
    status: str
    scan_high_watermark_id: int
    snapshot_rows: int
    last_scanned_id: int
    scanned_rows: int
    updated_rows: int
    unchanged_rows: int
    data_checksum_before: str
    data_checksum_after: str
    ownership_checksum_after: str
    completed_at: Any


@dataclass(frozen=True, slots=True)
class _RawProjection:
    id: int
    domain: str
    source: str
    external_id: str | None
    subject_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    integration_connection_id: uuid.UUID | None
    file_asset_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class _RowPlan:
    subject_id: uuid.UUID
    integration_connection_id: uuid.UUID | None
    changed: bool


@dataclass(slots=True)
class _ScanSummary:
    rows_above_high_watermark: int
    referenced_connection_ids: set[uuid.UUID]
    inferred_connections: dict[tuple[str, str], uuid.UUID]


def _validate_batch_size(batch_size: object) -> int:
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= MAX_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE
    ):
        raise RawOwnershipBackfillValidationError(
            "batch_size must be an integer between 1 and "
            f"{MAX_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE}"
        )
    return batch_size


def _validate_nonnegative_bigint(value: object, *, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _SIGNED_BIGINT_MAX
    ):
        raise RawOwnershipBackfillValidationError(
            f"{field_name} must be a nonnegative signed-BIGINT integer"
        )
    return value


def _validate_high_watermark(value: object) -> int:
    return _validate_nonnegative_bigint(
        value,
        field_name="scan_high_watermark_id",
    )


def _validate_snapshot_rows(value: object, *, high_watermark: int) -> int:
    snapshot_rows = _validate_nonnegative_bigint(
        value,
        field_name="snapshot_rows",
    )
    if snapshot_rows > high_watermark:
        raise RawOwnershipBackfillValidationError(
            "snapshot_rows cannot exceed scan_high_watermark_id"
        )
    return snapshot_rows


async def _load_scope(session: AsyncSession, *, for_update: bool) -> _Scope:
    if for_update:
        await acquire_identity_governance_lock(session)
        subjects = list(
            await session.scalars(
                select(HealthSubject)
                .order_by(HealthSubject.id)
                .limit(2)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if len(subjects) != 1:
            raise RawOwnershipBackfillIdentityError(
                "raw ownership backfill requires exactly one health subject"
            )
        subject_id = subjects[0].id
        owner_user_id = subjects[0].owner_user_id
        owner = await session.scalar(
            select(User)
            .where(User.id == owner_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        owner_status = owner.status if owner is not None else None
        connections: list[Any] = list(
            await session.scalars(
                select(IntegrationConnection)
                .order_by(IntegrationConnection.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
    else:
        subject_rows = list(
            await session.execute(
                select(HealthSubject.id, HealthSubject.owner_user_id)
                .order_by(HealthSubject.id)
                .limit(2)
            )
        )
        if len(subject_rows) != 1:
            raise RawOwnershipBackfillIdentityError(
                "raw ownership backfill requires exactly one health subject"
            )
        subject_id, owner_user_id = subject_rows[0]
        owner_row = (
            await session.execute(
                select(User.id, User.status).where(User.id == owner_user_id)
            )
        ).one_or_none()
        owner_status = owner_row.status if owner_row is not None else None
        connections = [
            _ConnectionProjection(
                id=row.id,
                subject_id=row.subject_id,
                provider=row.provider,
                connection_type=row.connection_type,
                external_account_discriminator=(
                    row.external_account_discriminator
                ),
                status=row.status,
            )
            for row in await session.execute(
                select(
                    IntegrationConnection.id,
                    IntegrationConnection.subject_id,
                    IntegrationConnection.provider,
                    IntegrationConnection.connection_type,
                    IntegrationConnection.external_account_discriminator,
                    IntegrationConnection.status,
                ).order_by(IntegrationConnection.id)
            )
        ]

    if owner_status != UserStatus.ACTIVE.value:
        raise RawOwnershipBackfillIdentityError(
            "the sole health subject must have an active owner"
        )

    by_id: dict[uuid.UUID, Any] = {}
    for connection in connections:
        if connection.subject_id != subject_id:
            raise RawOwnershipBackfillIdentityError(
                "an integration connection belongs to a foreign subject"
            )
        if (
            connection.provider not in _KNOWN_PROVIDERS
            or connection.connection_type not in _KNOWN_CONNECTION_TYPES
            or connection.status not in _KNOWN_CONNECTION_STATUSES
        ):
            raise RawOwnershipBackfillMappingError(
                "an integration connection has an unknown persisted mapping"
            )
        by_id[connection.id] = connection

    return _Scope(
        subject_id=subject_id,
        owner_user_id=owner_user_id,
        connections=by_id,
    )


async def _load_checkpoint(
    session: AsyncSession, *, for_update: bool
) -> OwnershipBackfillCheckpoint | _CheckpointProjection | None:
    if for_update:
        return await session.scalar(
            select(OwnershipBackfillCheckpoint)
            .where(
                OwnershipBackfillCheckpoint.phase_key
                == RAW_OWNERSHIP_BACKFILL_PHASE
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    row = (
        await session.execute(
            select(
                OwnershipBackfillCheckpoint.phase_key,
                OwnershipBackfillCheckpoint.subject_id,
                OwnershipBackfillCheckpoint.status,
                OwnershipBackfillCheckpoint.scan_high_watermark_id,
                OwnershipBackfillCheckpoint.snapshot_rows,
                OwnershipBackfillCheckpoint.last_scanned_id,
                OwnershipBackfillCheckpoint.scanned_rows,
                OwnershipBackfillCheckpoint.updated_rows,
                OwnershipBackfillCheckpoint.unchanged_rows,
                OwnershipBackfillCheckpoint.data_checksum_before,
                OwnershipBackfillCheckpoint.data_checksum_after,
                OwnershipBackfillCheckpoint.ownership_checksum_after,
                OwnershipBackfillCheckpoint.completed_at,
            ).where(
                OwnershipBackfillCheckpoint.phase_key
                == RAW_OWNERSHIP_BACKFILL_PHASE
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return _CheckpointProjection(*row)


def _validate_checkpoint(
    checkpoint: OwnershipBackfillCheckpoint,
    *,
    subject_id: uuid.UUID,
) -> RawOwnershipBackfillStatus:
    if checkpoint.subject_id != subject_id:
        raise RawOwnershipBackfillStateError(
            "raw ownership checkpoint belongs to another subject"
        )
    try:
        status = RawOwnershipBackfillStatus(checkpoint.status)
    except ValueError as exc:
        raise RawOwnershipBackfillStateError(
            "raw ownership checkpoint has an unknown status"
        ) from exc
    if status is RawOwnershipBackfillStatus.NOT_STARTED:
        raise RawOwnershipBackfillStateError(
            "not_started is not a persisted checkpoint status"
        )
    scalar_values = (
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _SIGNED_BIGINT_MAX
        for value in scalar_values
    ):
        raise RawOwnershipBackfillStateError(
            "raw ownership checkpoint has invalid counters"
        )
    if checkpoint.last_scanned_id > checkpoint.scan_high_watermark_id:
        raise RawOwnershipBackfillStateError(
            "raw ownership checkpoint cursor exceeds its high-water mark"
        )
    if checkpoint.snapshot_rows > checkpoint.scan_high_watermark_id:
        raise RawOwnershipBackfillStateError(
            "raw ownership checkpoint snapshot count exceeds its high-water mark"
        )
    if checkpoint.scanned_rows > checkpoint.snapshot_rows:
        raise RawOwnershipBackfillStateError(
            "raw ownership checkpoint scanned count exceeds its snapshot"
        )
    if checkpoint.scanned_rows != checkpoint.updated_rows + checkpoint.unchanged_rows:
        raise RawOwnershipBackfillStateError(
            "raw ownership checkpoint counters do not balance"
        )
    digests = (
        checkpoint.data_checksum_before,
        checkpoint.data_checksum_after,
        checkpoint.ownership_checksum_after,
    )
    if any(
        not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
        for value in digests
    ):
        raise RawOwnershipBackfillStateError(
            "raw ownership checkpoint has an invalid SHA-256 digest"
        )
    if checkpoint.data_checksum_before != checkpoint.data_checksum_after:
        raise RawOwnershipBackfillStateError(
            "raw ownership checkpoint data checksums differ"
        )
    if status is RawOwnershipBackfillStatus.COMPLETED:
        if (
            checkpoint.completed_at is None
            or checkpoint.last_scanned_id != checkpoint.scan_high_watermark_id
            or checkpoint.scanned_rows != checkpoint.snapshot_rows
        ):
            raise RawOwnershipBackfillStateError(
                "completed raw ownership checkpoint is incomplete"
            )
    elif checkpoint.completed_at is not None:
        raise RawOwnershipBackfillStateError(
            "non-completed raw ownership checkpoint has a completion timestamp"
        )
    if status is RawOwnershipBackfillStatus.RESTORE_BLOCKED and (
        checkpoint.last_scanned_id != 0
        or checkpoint.scanned_rows != 0
        or checkpoint.updated_rows != 0
        or checkpoint.unchanged_rows != 0
        or checkpoint.data_checksum_before != _EMPTY_SHA256
        or checkpoint.data_checksum_after != _EMPTY_SHA256
        or checkpoint.ownership_checksum_after != _EMPTY_SHA256
    ):
        raise RawOwnershipBackfillStateError(
            "restore-blocked raw ownership checkpoint contains progress"
        )
    return status


def _expected_parser_purpose(pair: tuple[str, str]) -> AIInvocationPurpose:
    if pair[0] == Domain.LABS.value:
        return AIInvocationPurpose.LAB_DOCUMENT_PARSE
    return AIInvocationPurpose.BODY_SCAN_PARSE


def _expected_file_purpose(pair: tuple[str, str]) -> FileAssetPurpose:
    if pair[0] == Domain.LABS.value:
        return FileAssetPurpose.LAB_DOCUMENT
    return FileAssetPurpose.BODY_SCAN_DOCUMENT


def _validate_existing_connection(
    scope: _Scope,
    *,
    connection_id: uuid.UUID,
    expected: tuple[IntegrationProvider, IntegrationConnectionType],
    raw_id: int,
) -> None:
    connection = scope.connections.get(connection_id)
    if connection is None or connection.subject_id != scope.subject_id:
        raise RawOwnershipBackfillMappingError(
            f"raw row {raw_id} references a missing or foreign connection"
        )
    provider, connection_type = expected
    if (
        connection.provider != provider.value
        or connection.connection_type != connection_type.value
        or connection.status == IntegrationConnectionStatus.PENDING.value
    ):
        raise RawOwnershipBackfillMappingError(
            f"raw row {raw_id} has invalid historical connection provenance"
        )


def _infer_connection(
    scope: _Scope,
    *,
    pair: tuple[str, str],
    inferred: dict[tuple[str, str], uuid.UUID],
) -> uuid.UUID:
    cached = inferred.get(pair)
    if cached is not None:
        return cached
    provider, connection_type = _PAIR_CONNECTION_ROOT[pair]
    candidates = [
        row
        for row in scope.connections.values()
        if row.provider == provider.value
        and row.connection_type == connection_type.value
        and row.external_account_discriminator == LEGACY_ACCOUNT_DISCRIMINATOR
        and row.status in _INFERABLE_CONNECTION_STATUSES
    ]
    if len(candidates) != 1:
        detail = "missing" if not candidates else "ambiguous"
        raise RawOwnershipBackfillMappingError(
            f"required {provider.value} {connection_type.value} root is {detail}"
        )
    inferred[pair] = candidates[0].id
    return candidates[0].id


async def _validate_parser_file(
    session: AsyncSession,
    *,
    row: Any,
    scope: _Scope,
    pair: tuple[str, str],
) -> None:
    if row.file_asset_id is None or row.actor_user_id is None:
        raise RawOwnershipBackfillMappingError(
            f"raw row {row.id} has incomplete platform file provenance"
        )
    asset = (
        await session.execute(
            select(
                FileAsset.subject_id,
                FileAsset.uploaded_by_user_id,
                FileAsset.purpose,
                FileAsset.storage_ref,
            ).where(FileAsset.id == row.file_asset_id)
        )
    ).one_or_none()
    expected_purpose = _expected_file_purpose(pair)
    if (
        asset is None
        or asset.subject_id != scope.subject_id
        or asset.uploaded_by_user_id != row.actor_user_id
        or asset.purpose != expected_purpose.value
        or row.external_id != asset.storage_ref
    ):
        raise RawOwnershipBackfillMappingError(
            f"raw row {row.id} has invalid file provenance"
        )


async def _parser_invocation_counts(
    session: AsyncSession,
    *,
    row: Any,
    scope: _Scope,
    pair: tuple[str, str],
) -> tuple[int, int]:
    purpose = _expected_parser_purpose(pair)
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(AIInvocation)
            .where(AIInvocation.raw_payload_id == row.id)
        )
        or 0
    )
    matching = int(
        await session.scalar(
            select(func.count())
            .select_from(AIInvocation)
            .where(
                AIInvocation.raw_payload_id == row.id,
                AIInvocation.subject_id == scope.subject_id,
                AIInvocation.actor_user_id == row.actor_user_id,
                AIInvocation.purpose == purpose.value,
                AIInvocation.source == AIInvocationSource.WEB.value,
            )
        )
        or 0
    )
    return total, matching


async def _classify_row(
    session: AsyncSession,
    *,
    row: Any,
    scope: _Scope,
    high_watermark: int,
    inferred: dict[tuple[str, str], uuid.UUID],
) -> _RowPlan:
    if not isinstance(row.id, int) or row.id <= 0:
        raise RawOwnershipBackfillStateError(
            "raw payload primary keys must be positive"
        )
    if row.domain not in _KNOWN_DOMAINS or row.source not in _KNOWN_SOURCES:
        raise RawOwnershipBackfillMappingError(
            f"raw row {row.id} has an unknown domain or source"
        )
    pair = (row.domain, row.source)
    if pair not in _ALLOWED_PAIRS:
        raise RawOwnershipBackfillMappingError(
            f"raw row {row.id} has an unreviewed domain/source pair"
        )

    if row.subject_id is None and any(
        value is not None
        for value in (
            row.actor_user_id,
            row.integration_connection_id,
            row.file_asset_id,
        )
    ):
        raise RawOwnershipBackfillStateError(
            f"raw row {row.id} has partial ownership roots"
        )
    if row.subject_id is not None and row.subject_id != scope.subject_id:
        raise RawOwnershipBackfillIdentityError(
            f"raw row {row.id} belongs to a foreign subject"
        )
    if row.actor_user_id is not None and row.actor_user_id != scope.owner_user_id:
        raise RawOwnershipBackfillIdentityError(
            f"raw row {row.id} has a foreign origin actor"
        )

    adoption_shape = (
        row.actor_user_id is None
        and row.integration_connection_id is None
        and row.file_asset_id is None
        and row.subject_id in {None, scope.subject_id}
    )
    above_watermark = row.id > high_watermark
    if above_watermark and (
        row.subject_id != scope.subject_id
        or row.actor_user_id != scope.owner_user_id
    ):
        raise RawOwnershipBackfillStateError(
            f"raw row {row.id} above the high-water mark lacks live ownership"
        )

    if pair in _PAIR_CONNECTION_ROOT and pair not in _PARSER_PAIRS:
        if row.file_asset_id is not None:
            raise RawOwnershipBackfillMappingError(
                f"raw row {row.id} cannot have file provenance"
            )
        if row.integration_connection_id is None:
            if not adoption_shape or above_watermark:
                raise RawOwnershipBackfillStateError(
                    f"raw row {row.id} has incomplete provider ownership"
                )
            target_connection_id = _infer_connection(
                scope, pair=pair, inferred=inferred
            )
        else:
            _validate_existing_connection(
                scope,
                connection_id=row.integration_connection_id,
                expected=_PAIR_CONNECTION_ROOT[pair],
                raw_id=row.id,
            )
            target_connection_id = row.integration_connection_id

    elif pair in _PARSER_PAIRS:
        if row.integration_connection_id is not None:
            if above_watermark:
                raise RawOwnershipBackfillStateError(
                    f"raw row {row.id} above the high-water mark has historical "
                    "parser ownership"
                )
            _validate_existing_connection(
                scope,
                connection_id=row.integration_connection_id,
                expected=_PAIR_CONNECTION_ROOT[pair],
                raw_id=row.id,
            )
            total_invocations, _matching = await _parser_invocation_counts(
                session, row=row, scope=scope, pair=pair
            )
            if total_invocations:
                raise RawOwnershipBackfillMappingError(
                    f"raw row {row.id} mixes historical and platform parser roots"
                )
            if (row.actor_user_id is None) != (row.file_asset_id is None):
                raise RawOwnershipBackfillMappingError(
                    f"raw row {row.id} has partial historical parser provenance"
                )
            if row.file_asset_id is not None:
                await _validate_parser_file(
                    session, row=row, scope=scope, pair=pair
                )
            target_connection_id = row.integration_connection_id
        elif adoption_shape:
            if above_watermark:
                raise RawOwnershipBackfillStateError(
                    f"raw row {row.id} above the high-water mark is not owned"
                )
            target_connection_id = _infer_connection(
                scope, pair=pair, inferred=inferred
            )
        else:
            if row.subject_id is None:
                raise RawOwnershipBackfillStateError(
                    f"raw row {row.id} has partial parser ownership"
                )
            await _validate_parser_file(session, row=row, scope=scope, pair=pair)
            total_invocations, matching_invocations = await _parser_invocation_counts(
                session, row=row, scope=scope, pair=pair
            )
            if total_invocations < 1 or total_invocations != matching_invocations:
                raise RawOwnershipBackfillMappingError(
                    f"raw row {row.id} has invalid platform parser provenance"
                )
            target_connection_id = None

    else:
        if row.integration_connection_id is not None or row.file_asset_id is not None:
            raise RawOwnershipBackfillMappingError(
                f"raw row {row.id} must remain connection/file neutral"
            )
        target_connection_id = None

    changed = (
        row.subject_id != scope.subject_id
        or row.integration_connection_id != target_connection_id
    )
    return _RowPlan(
        subject_id=scope.subject_id,
        integration_connection_id=target_connection_id,
        changed=changed,
    )


def _projection_from_row(row: Any) -> _RawProjection:
    return _RawProjection(
        id=row.id,
        domain=row.domain,
        source=row.source,
        external_id=row.external_id,
        subject_id=row.subject_id,
        actor_user_id=row.actor_user_id,
        integration_connection_id=row.integration_connection_id,
        file_asset_id=row.file_asset_id,
    )


async def _scan_and_validate_snapshot(
    session: AsyncSession,
    *,
    scope: _Scope,
    high_watermark: int,
    checkpoint_cursor: int | None,
    page_size: int = _PREFLIGHT_PAGE_SIZE,
    expected_ownership_checksum: str | None = None,
    for_update: bool = False,
) -> _ScanSummary:
    last_id: int | None = None
    rows_above = 0
    referenced_connection_ids: set[uuid.UUID] = set()
    inferred: dict[tuple[str, str], uuid.UUID] = {}
    ownership_checksum = _EMPTY_SHA256
    while True:
        stmt = select(
            RawPayload.id,
            RawPayload.domain,
            RawPayload.source,
            RawPayload.external_id,
            RawPayload.subject_id,
            RawPayload.actor_user_id,
            RawPayload.integration_connection_id,
            RawPayload.file_asset_id,
        ).order_by(RawPayload.id).limit(page_size)
        if last_id is not None:
            stmt = stmt.where(RawPayload.id > last_id)
        if for_update:
            stmt = stmt.with_for_update()
        rows = list(await session.execute(stmt))
        if not rows:
            break
        for raw_row in rows:
            projection = _projection_from_row(raw_row)
            plan = await _classify_row(
                session,
                row=projection,
                scope=scope,
                high_watermark=high_watermark,
                inferred=inferred,
            )
            if (
                checkpoint_cursor is not None
                and projection.id <= checkpoint_cursor
                and plan.changed
            ):
                raise RawOwnershipBackfillStateError(
                    "a previously scanned raw row requires ownership repair"
                )
            if (
                expected_ownership_checksum is not None
                and projection.id <= high_watermark
            ):
                ownership_checksum = _extend_checksum(
                    ownership_checksum,
                    _ownership_envelope(projection),
                )
            if projection.id > high_watermark:
                rows_above += 1
            if plan.integration_connection_id is not None:
                referenced_connection_ids.add(plan.integration_connection_id)
        last_id = rows[-1].id
        if len(rows) < page_size:
            break
    if (
        expected_ownership_checksum is not None
        and ownership_checksum != expected_ownership_checksum
    ):
        raise RawOwnershipBackfillStateError(
            "completed raw ownership checksum no longer matches current provenance"
        )
    return _ScanSummary(
        rows_above_high_watermark=rows_above,
        referenced_connection_ids=referenced_connection_ids,
        inferred_connections=inferred,
    )


async def _scan_and_validate_above_high_watermark(
    session: AsyncSession,
    *,
    scope: _Scope,
    high_watermark: int,
) -> _ScanSummary:
    """Validate only rows appended after a durable snapshot, in keyset pages."""

    last_id = high_watermark
    rows_above = 0
    referenced_connection_ids: set[uuid.UUID] = set()
    inferred: dict[tuple[str, str], uuid.UUID] = {}
    while True:
        rows = list(
            await session.execute(
                select(
                    RawPayload.id,
                    RawPayload.domain,
                    RawPayload.source,
                    RawPayload.external_id,
                    RawPayload.subject_id,
                    RawPayload.actor_user_id,
                    RawPayload.integration_connection_id,
                    RawPayload.file_asset_id,
                )
                .where(RawPayload.id > last_id)
                .order_by(RawPayload.id)
                .limit(_PREFLIGHT_PAGE_SIZE)
            )
        )
        if not rows:
            break
        for raw_row in rows:
            projection = _projection_from_row(raw_row)
            plan = await _classify_row(
                session,
                row=projection,
                scope=scope,
                high_watermark=high_watermark,
                inferred=inferred,
            )
            if plan.changed:
                raise RawOwnershipBackfillStateError(
                    "a raw row above the high-water mark requires ownership repair"
                )
            rows_above += 1
            if plan.integration_connection_id is not None:
                referenced_connection_ids.add(plan.integration_connection_id)
        last_id = rows[-1].id
        if len(rows) < _PREFLIGHT_PAGE_SIZE:
            break
    return _ScanSummary(
        rows_above_high_watermark=rows_above,
        referenced_connection_ids=referenced_connection_ids,
        inferred_connections=inferred,
    )


async def _checkpoint_row_counts(
    session: AsyncSession,
    *,
    checkpoint: OwnershipBackfillCheckpoint,
) -> int:
    """Validate durable prefix cardinality and return the remaining row count."""

    prefix_rows = int(
        await session.scalar(
            select(func.count())
            .select_from(RawPayload)
            .where(RawPayload.id <= checkpoint.last_scanned_id)
        )
        or 0
    )
    remaining_rows = await _remaining_rows(
        session,
        high_watermark=checkpoint.scan_high_watermark_id,
        last_scanned=checkpoint.last_scanned_id,
    )
    snapshot_rows = await _snapshot_row_count(
        session,
        high_watermark=checkpoint.scan_high_watermark_id,
    )
    if prefix_rows != checkpoint.scanned_rows:
        raise RawOwnershipBackfillStateError(
            "raw ownership checkpoint prefix count drifted"
        )
    if snapshot_rows != checkpoint.snapshot_rows:
        raise RawOwnershipBackfillStateError(
            "raw ownership checkpoint snapshot count drifted"
        )
    if checkpoint.scanned_rows + remaining_rows != checkpoint.snapshot_rows:
        raise RawOwnershipBackfillStateError(
            "raw ownership checkpoint progress no longer matches its snapshot"
        )
    return remaining_rows


async def _duplicate_summary_from_database(
    session: AsyncSession,
    *,
    scope: _Scope,
    high_watermark: int,
) -> _ScanSummary:
    """Build the finite duplicate-gate mapping without scanning raw contents."""

    referenced = set(
        await session.scalars(
            select(RawPayload.integration_connection_id)
            .where(RawPayload.integration_connection_id.is_not(None))
            .distinct()
        )
    )
    inferred: dict[tuple[str, str], uuid.UUID] = {}
    adoption = _adoption_clause(
        subject_id=scope.subject_id,
        high_watermark=high_watermark,
    )
    adoption_pairs = list(
        await session.execute(
            select(RawPayload.domain, RawPayload.source)
            .where(adoption)
            .distinct()
        )
    )
    for domain, source in adoption_pairs:
        pair = (domain, source)
        if domain not in _KNOWN_DOMAINS or source not in _KNOWN_SOURCES:
            raise RawOwnershipBackfillMappingError(
                "an adoption candidate has an unknown domain or source"
            )
        if pair not in _ALLOWED_PAIRS:
            raise RawOwnershipBackfillMappingError(
                "an adoption candidate has an unreviewed domain/source pair"
            )
        if pair in _PAIR_CONNECTION_ROOT:
            referenced.add(_infer_connection(scope, pair=pair, inferred=inferred))
    return _ScanSummary(
        rows_above_high_watermark=0,
        referenced_connection_ids=referenced,
        inferred_connections=inferred,
    )


def _pair_clause(pair: tuple[str, str]):
    return and_(RawPayload.domain == pair[0], RawPayload.source == pair[1])


def _adoption_clause(*, subject_id: uuid.UUID, high_watermark: int):
    return and_(
        RawPayload.id <= high_watermark,
        RawPayload.actor_user_id.is_(None),
        RawPayload.integration_connection_id.is_(None),
        RawPayload.file_asset_id.is_(None),
        or_(RawPayload.subject_id.is_(None), RawPayload.subject_id == subject_id),
    )


async def _has_duplicate_for_clause(session: AsyncSession, clause: Any) -> bool:
    count_rows = func.count(RawPayload.id)
    candidate = await session.scalar(
        select(count_rows)
        .where(RawPayload.external_id.is_not(None), clause)
        .group_by(RawPayload.domain, RawPayload.source, RawPayload.external_id)
        .having(count_rows > 1)
        .limit(1)
    )
    return candidate is not None


async def _reject_duplicate_candidates(
    session: AsyncSession,
    *,
    scope: _Scope,
    high_watermark: int,
    scan: _ScanSummary,
) -> None:
    adoption = _adoption_clause(
        subject_id=scope.subject_id, high_watermark=high_watermark
    )
    inferred_by_connection: dict[uuid.UUID, list[tuple[str, str]]] = {}
    for pair, connection_id in scan.inferred_connections.items():
        inferred_by_connection.setdefault(connection_id, []).append(pair)

    for connection_id in sorted(scan.referenced_connection_ids, key=str):
        clauses = [RawPayload.integration_connection_id == connection_id]
        clauses.extend(
            and_(adoption, _pair_clause(pair))
            for pair in inferred_by_connection.get(connection_id, ())
        )
        if await _has_duplicate_for_clause(session, or_(*clauses)):
            raise RawOwnershipBackfillDuplicateError(
                "duplicate connection-scoped raw payload candidates exist"
            )

    inferred_adoptions = [
        and_(adoption, _pair_clause(pair)) for pair in scan.inferred_connections
    ]
    final_null_connection = RawPayload.integration_connection_id.is_(None)
    if inferred_adoptions:
        final_null_connection = and_(
            final_null_connection,
            ~or_(*inferred_adoptions),
        )
    if await _has_duplicate_for_clause(session, final_null_connection):
        raise RawOwnershipBackfillDuplicateError(
            "duplicate subject-scoped raw payload candidates exist"
        )


async def _max_raw_id(session: AsyncSession) -> int:
    value = max(0, int(await session.scalar(select(func.max(RawPayload.id))) or 0))
    if value > _SIGNED_BIGINT_MAX:
        raise RawOwnershipBackfillStateError(
            "raw payload high-water mark exceeds signed BIGINT"
        )
    return value


async def _snapshot_row_count(
    session: AsyncSession,
    *,
    high_watermark: int,
) -> int:
    count = int(
        await session.scalar(
            select(func.count())
            .select_from(RawPayload)
            .where(RawPayload.id <= high_watermark)
        )
        or 0
    )
    if count > _SIGNED_BIGINT_MAX:
        raise RawOwnershipBackfillStateError(
            "raw payload snapshot count exceeds signed BIGINT"
        )
    return count


async def _remaining_rows(
    session: AsyncSession, *, high_watermark: int, last_scanned: int
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(RawPayload)
            .where(
                RawPayload.id > last_scanned,
                RawPayload.id <= high_watermark,
            )
        )
        or 0
    )


async def _rows_above_high_watermark(
    session: AsyncSession,
    *,
    high_watermark: int,
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(RawPayload)
            .where(RawPayload.id > high_watermark)
        )
        or 0
    )


def _canonical_timestamp(value: Any) -> str | None:
    return value.isoformat(timespec="microseconds") if value is not None else None


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RawOwnershipBackfillStateError(
            "a raw row cannot be represented by the canonical checksum format"
        ) from exc


def _extend_checksum(previous: str, envelope: Any) -> str:
    if _SHA256_RE.fullmatch(previous) is None:
        raise RawOwnershipBackfillStateError("rolling checksum state is invalid")
    encoded = _canonical_json(envelope)
    digest = hashlib.sha256()
    digest.update(bytes.fromhex(previous))
    digest.update(len(encoded).to_bytes(8, byteorder="big", signed=False))
    digest.update(encoded)
    return digest.hexdigest()


def _data_envelope(row: Any) -> list[Any]:
    return [
        row.id,
        row.domain,
        row.source,
        row.external_id,
        _canonical_timestamp(row.fetched_at),
        row.payload,
        _canonical_timestamp(row.processed_at),
    ]


def _ownership_envelope(row: Any) -> list[Any]:
    return [
        row.id,
        str(row.subject_id) if row.subject_id is not None else None,
        str(row.actor_user_id) if row.actor_user_id is not None else None,
        str(row.integration_connection_id)
        if row.integration_connection_id is not None
        else None,
        str(row.file_asset_id) if row.file_asset_id is not None else None,
    ]


async def _verify_final_snapshot_checksums(
    session: AsyncSession,
    *,
    checkpoint: OwnershipBackfillCheckpoint,
    page_size: int,
) -> None:
    """Lock and rehash the final snapshot once before marking it completed.

    Intermediate batches never reread their processed prefix.  Finalization is
    the single bounded-memory verification boundary: PK-ordered pages are
    locked and discarded as their hashes are extended, so a cross-batch raw
    data or ownership update cannot leave a completed stale digest chain.
    """

    data_checksum = _EMPTY_SHA256
    ownership_checksum = _EMPTY_SHA256
    scanned_rows = 0
    last_id = 0
    payload_page_size = min(page_size, _PAYLOAD_MATERIALIZATION_ROWS)
    while True:
        rows = list(
            await session.execute(
                select(
                    RawPayload.id,
                    RawPayload.domain,
                    RawPayload.source,
                    RawPayload.external_id,
                    RawPayload.fetched_at,
                    RawPayload.payload,
                    RawPayload.processed_at,
                    RawPayload.subject_id,
                    RawPayload.actor_user_id,
                    RawPayload.integration_connection_id,
                    RawPayload.file_asset_id,
                )
                .where(
                    RawPayload.id > last_id,
                    RawPayload.id <= checkpoint.scan_high_watermark_id,
                )
                .order_by(RawPayload.id)
                .limit(payload_page_size)
                .with_for_update()
            )
        )
        if not rows:
            break
        for row in rows:
            data_checksum = _extend_checksum(data_checksum, _data_envelope(row))
            ownership_checksum = _extend_checksum(
                ownership_checksum,
                _ownership_envelope(row),
            )
        scanned_rows += len(rows)
        last_id = rows[-1].id
        if len(rows) < payload_page_size:
            break

    if scanned_rows != checkpoint.snapshot_rows:
        raise RawOwnershipBackfillStateError(
            "raw ownership final snapshot cardinality drifted"
        )
    if (
        data_checksum != checkpoint.data_checksum_before
        or data_checksum != checkpoint.data_checksum_after
    ):
        raise RawOwnershipBackfillStateError(
            "raw data changed across ownership backfill batches"
        )
    if ownership_checksum != checkpoint.ownership_checksum_after:
        raise RawOwnershipBackfillStateError(
            "raw ownership changed across backfill batches"
        )


def _preflight_result(
    *,
    scope: _Scope,
    checkpoint: OwnershipBackfillCheckpoint | None,
    high_watermark: int,
    snapshot_rows: int,
    remaining_rows: int,
    rows_above: int,
) -> RawOwnershipBackfillPreflightResult:
    if checkpoint is None:
        return RawOwnershipBackfillPreflightResult(
            phase_key=RAW_OWNERSHIP_BACKFILL_PHASE,
            subject_id=scope.subject_id,
            status=RawOwnershipBackfillStatus.NOT_STARTED,
            scan_high_watermark_id=high_watermark,
            snapshot_rows=snapshot_rows,
            last_scanned_id=0,
            scanned_rows=0,
            updated_rows=0,
            unchanged_rows=0,
            remaining_rows=remaining_rows,
            rows_above_high_watermark=rows_above,
            data_checksum_before=_EMPTY_SHA256,
            data_checksum_after=_EMPTY_SHA256,
            ownership_checksum_after=_EMPTY_SHA256,
        )
    return RawOwnershipBackfillPreflightResult(
        phase_key=checkpoint.phase_key,
        subject_id=scope.subject_id,
        status=RawOwnershipBackfillStatus(checkpoint.status),
        scan_high_watermark_id=checkpoint.scan_high_watermark_id,
        snapshot_rows=checkpoint.snapshot_rows,
        last_scanned_id=checkpoint.last_scanned_id,
        scanned_rows=checkpoint.scanned_rows,
        updated_rows=checkpoint.updated_rows,
        unchanged_rows=checkpoint.unchanged_rows,
        remaining_rows=remaining_rows,
        rows_above_high_watermark=rows_above,
        data_checksum_before=checkpoint.data_checksum_before,
        data_checksum_after=checkpoint.data_checksum_after,
        ownership_checksum_after=checkpoint.ownership_checksum_after,
    )


async def preflight_raw_ownership_backfill(
    session: AsyncSession,
) -> RawOwnershipBackfillPreflightResult:
    """Validate and project the exact raw phase without mutation or raw JSON."""

    with session.no_autoflush:
        scope = await _load_scope(session, for_update=False)
        checkpoint = await _load_checkpoint(session, for_update=False)
        if checkpoint is None:
            high_watermark = await _max_raw_id(session)
            snapshot_rows = await _snapshot_row_count(
                session,
                high_watermark=high_watermark,
            )
            checkpoint_cursor = None
            checkpoint_remaining: int | None = None
            expected_ownership_checksum: str | None = None
        else:
            persisted_status = _validate_checkpoint(
                checkpoint,
                subject_id=scope.subject_id,
            )
            high_watermark = checkpoint.scan_high_watermark_id
            snapshot_rows = checkpoint.snapshot_rows
            checkpoint_cursor = checkpoint.last_scanned_id
            expected_ownership_checksum = (
                checkpoint.ownership_checksum_after
                if persisted_status is RawOwnershipBackfillStatus.COMPLETED
                else None
            )
            checkpoint_remaining = await _checkpoint_row_counts(
                session, checkpoint=checkpoint
            )
            if persisted_status is RawOwnershipBackfillStatus.RESTORE_BLOCKED:
                return _preflight_result(
                    scope=scope,
                    checkpoint=checkpoint,
                    high_watermark=high_watermark,
                    snapshot_rows=snapshot_rows,
                    remaining_rows=checkpoint_remaining,
                    rows_above=await _rows_above_high_watermark(
                        session,
                        high_watermark=high_watermark,
                    ),
                )
        scan = await _scan_and_validate_snapshot(
            session,
            scope=scope,
            high_watermark=high_watermark,
            checkpoint_cursor=checkpoint_cursor,
            expected_ownership_checksum=expected_ownership_checksum,
        )
        await _reject_duplicate_candidates(
            session,
            scope=scope,
            high_watermark=high_watermark,
            scan=scan,
        )
        last_scanned = checkpoint.last_scanned_id if checkpoint is not None else 0
        remaining = (
            checkpoint_remaining
            if checkpoint_remaining is not None
            else await _remaining_rows(
                session,
                high_watermark=high_watermark,
                last_scanned=last_scanned,
            )
        )
        return _preflight_result(
            scope=scope,
            checkpoint=checkpoint,
            high_watermark=high_watermark,
            snapshot_rows=snapshot_rows,
            remaining_rows=remaining,
            rows_above=scan.rows_above_high_watermark,
        )


async def run_raw_ownership_backfill_batch(
    session: AsyncSession,
    *,
    batch_size: int,
) -> RawOwnershipBackfillBatchResult:
    """Apply at most one PK-ordered raw ownership batch and flush only."""

    size = _validate_batch_size(batch_size)
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        checkpoint = await _load_checkpoint(session, for_update=True)
        if checkpoint is None:
            high_watermark = await _max_raw_id(session)
            snapshot_rows = await _snapshot_row_count(
                session,
                high_watermark=high_watermark,
            )
            scan = await _scan_and_validate_snapshot(
                session,
                scope=scope,
                high_watermark=high_watermark,
                checkpoint_cursor=None,
            )
            await _reject_duplicate_candidates(
                session,
                scope=scope,
                high_watermark=high_watermark,
                scan=scan,
            )
            checkpoint = OwnershipBackfillCheckpoint(
                phase_key=RAW_OWNERSHIP_BACKFILL_PHASE,
                subject_id=scope.subject_id,
                status=RawOwnershipBackfillStatus.RUNNING.value,
                scan_high_watermark_id=high_watermark,
                snapshot_rows=snapshot_rows,
                last_scanned_id=0,
                scanned_rows=0,
                updated_rows=0,
                unchanged_rows=0,
                data_checksum_before=_EMPTY_SHA256,
                data_checksum_after=_EMPTY_SHA256,
                ownership_checksum_after=_EMPTY_SHA256,
                completed_at=None,
            )
            session.add(checkpoint)
            await session.flush()
        else:
            persisted_status = _validate_checkpoint(
                checkpoint, subject_id=scope.subject_id
            )
            high_watermark = checkpoint.scan_high_watermark_id
            await _checkpoint_row_counts(session, checkpoint=checkpoint)
            if persisted_status is RawOwnershipBackfillStatus.RESTORE_BLOCKED:
                raise RawOwnershipBackfillStateError(
                    "portability-v1 restore removed raw provenance; "
                    "trusted recovery is required"
                )
            if persisted_status is RawOwnershipBackfillStatus.COMPLETED:
                scan = await _scan_and_validate_snapshot(
                    session,
                    scope=scope,
                    high_watermark=high_watermark,
                    checkpoint_cursor=checkpoint.last_scanned_id,
                    page_size=size,
                    expected_ownership_checksum=(
                        checkpoint.ownership_checksum_after
                    ),
                    for_update=True,
                )
                await _reject_duplicate_candidates(
                    session,
                    scope=scope,
                    high_watermark=high_watermark,
                    scan=scan,
                )
            else:
                above_scan = await _scan_and_validate_above_high_watermark(
                    session,
                    scope=scope,
                    high_watermark=high_watermark,
                )
                duplicate_scan = await _duplicate_summary_from_database(
                    session,
                    scope=scope,
                    high_watermark=high_watermark,
                )
                await _reject_duplicate_candidates(
                    session,
                    scope=scope,
                    high_watermark=high_watermark,
                    scan=duplicate_scan,
                )
                scan = _ScanSummary(
                    rows_above_high_watermark=above_scan.rows_above_high_watermark,
                    referenced_connection_ids=(
                        duplicate_scan.referenced_connection_ids
                    ),
                    inferred_connections=duplicate_scan.inferred_connections,
                )

        if checkpoint.status == RawOwnershipBackfillStatus.COMPLETED.value:
            completed = _preflight_result(
                scope=scope,
                checkpoint=checkpoint,
                high_watermark=high_watermark,
                snapshot_rows=checkpoint.snapshot_rows,
                remaining_rows=0,
                rows_above=scan.rows_above_high_watermark,
            )
            return RawOwnershipBackfillBatchResult(
                phase_key=completed.phase_key,
                subject_id=completed.subject_id,
                status=completed.status,
                scan_high_watermark_id=completed.scan_high_watermark_id,
                snapshot_rows=completed.snapshot_rows,
                last_scanned_id=completed.last_scanned_id,
                scanned_rows=completed.scanned_rows,
                updated_rows=completed.updated_rows,
                unchanged_rows=completed.unchanged_rows,
                remaining_rows=completed.remaining_rows,
                rows_above_high_watermark=completed.rows_above_high_watermark,
                data_checksum_before=completed.data_checksum_before,
                data_checksum_after=completed.data_checksum_after,
                ownership_checksum_after=completed.ownership_checksum_after,
                batch_scanned_rows=0,
                batch_updated_rows=0,
                batch_unchanged_rows=0,
            )

        batch_ids = list(
            await session.scalars(
                select(RawPayload.id)
                .where(
                    RawPayload.id > checkpoint.last_scanned_id,
                    RawPayload.id <= checkpoint.scan_high_watermark_id,
                )
                .order_by(RawPayload.id)
                .limit(size)
                .with_for_update()
            )
        )

    before_checksum = checkpoint.data_checksum_before
    after_checksum = checkpoint.data_checksum_after
    ownership_checksum = checkpoint.ownership_checksum_after
    batch_updated = 0
    batch_unchanged = 0
    for raw_id in batch_ids:
        raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_id)
            .limit(_PAYLOAD_MATERIALIZATION_ROWS)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if raw is None:
            raise RawOwnershipBackfillStateError(
                "a locked raw payload disappeared before processing"
            )
        before_checksum = _extend_checksum(before_checksum, _data_envelope(raw))
        plan = await _classify_row(
            session,
            row=raw,
            scope=scope,
            high_watermark=checkpoint.scan_high_watermark_id,
            inferred=scan.inferred_connections,
        )
        if plan.changed:
            raw.subject_id = plan.subject_id
            raw.integration_connection_id = plan.integration_connection_id
            batch_updated += 1
        else:
            batch_unchanged += 1
        after_checksum = _extend_checksum(after_checksum, _data_envelope(raw))
        ownership_checksum = _extend_checksum(
            ownership_checksum, _ownership_envelope(raw)
        )
        await session.flush([raw])
        session.expire(raw, ["payload"])

    if before_checksum != after_checksum:
        raise RawOwnershipBackfillStateError(
            "raw data changed while ownership was being backfilled"
        )

    batch_scanned = len(batch_ids)
    checkpoint.scanned_rows += batch_scanned
    checkpoint.updated_rows += batch_updated
    checkpoint.unchanged_rows += batch_unchanged
    checkpoint.data_checksum_before = before_checksum
    checkpoint.data_checksum_after = after_checksum
    checkpoint.ownership_checksum_after = ownership_checksum
    if batch_ids:
        checkpoint.last_scanned_id = batch_ids[-1]

    remaining = await _remaining_rows(
        session,
        high_watermark=checkpoint.scan_high_watermark_id,
        last_scanned=checkpoint.last_scanned_id,
    )
    if remaining == 0:
        await session.flush()
        await _checkpoint_row_counts(session, checkpoint=checkpoint)
        await _verify_final_snapshot_checksums(
            session,
            checkpoint=checkpoint,
            page_size=size,
        )
        checkpoint.last_scanned_id = checkpoint.scan_high_watermark_id
        checkpoint.status = RawOwnershipBackfillStatus.COMPLETED.value
        checkpoint.completed_at = now_utc()
    await session.flush()

    return RawOwnershipBackfillBatchResult(
        phase_key=checkpoint.phase_key,
        subject_id=scope.subject_id,
        status=RawOwnershipBackfillStatus(checkpoint.status),
        scan_high_watermark_id=checkpoint.scan_high_watermark_id,
        snapshot_rows=checkpoint.snapshot_rows,
        last_scanned_id=checkpoint.last_scanned_id,
        scanned_rows=checkpoint.scanned_rows,
        updated_rows=checkpoint.updated_rows,
        unchanged_rows=checkpoint.unchanged_rows,
        remaining_rows=remaining,
        rows_above_high_watermark=scan.rows_above_high_watermark,
        data_checksum_before=checkpoint.data_checksum_before,
        data_checksum_after=checkpoint.data_checksum_after,
        ownership_checksum_after=checkpoint.ownership_checksum_after,
        batch_scanned_rows=batch_scanned,
        batch_updated_rows=batch_updated,
        batch_unchanged_rows=batch_unchanged,
    )


async def block_raw_ownership_backfill_for_portability_v1_restore(
    session: AsyncSession,
    *,
    scan_high_watermark_id: int,
    snapshot_rows: int,
) -> RawOwnershipBackfillRestoreBlockedResult:
    """Block Stage-3A before a provenance-stripping portability-v1 restore.

    The caller supplies the already-validated maximum incoming raw ID, invokes
    this before deleting portable rows, performs the replacement in the same
    transaction, and owns commit.  Portability v1 omits actor, connection, file,
    and linked AI/file provenance, so a nonempty restore remains blocked until a
    future trusted recovery path supplies that provenance.  A truly empty 0/0
    replacement completes with empty digests because it lost no provenance.
    Locking existing raw IDs is intentionally a full-replacement maintenance
    boundary; rows are fetched in keyset pages without materializing their JSON.
    """

    high_watermark = _validate_high_watermark(scan_high_watermark_id)
    expected_rows = _validate_snapshot_rows(
        snapshot_rows,
        high_watermark=high_watermark,
    )
    reset_at = now_utc().replace(microsecond=0)
    target_status = (
        RawOwnershipBackfillStatus.COMPLETED
        if high_watermark == 0 and expected_rows == 0
        else RawOwnershipBackfillStatus.RESTORE_BLOCKED
    )
    with session.no_autoflush:
        scope = await _load_scope(session, for_update=True)
        checkpoint = await _load_checkpoint(session, for_update=True)
        if checkpoint is not None:
            _validate_checkpoint(checkpoint, subject_id=scope.subject_id)
        else:
            checkpoint = OwnershipBackfillCheckpoint(
                phase_key=RAW_OWNERSHIP_BACKFILL_PHASE,
                subject_id=scope.subject_id,
                status=target_status.value,
                scan_high_watermark_id=high_watermark,
                snapshot_rows=expected_rows,
                last_scanned_id=0,
                scanned_rows=0,
                updated_rows=0,
                unchanged_rows=0,
                data_checksum_before=_EMPTY_SHA256,
                data_checksum_after=_EMPTY_SHA256,
                ownership_checksum_after=_EMPTY_SHA256,
                started_at=reset_at,
                updated_at=reset_at,
                completed_at=(
                    reset_at
                    if target_status is RawOwnershipBackfillStatus.COMPLETED
                    else None
                ),
            )
            session.add(checkpoint)

        checkpoint.status = target_status.value
        checkpoint.scan_high_watermark_id = high_watermark
        checkpoint.snapshot_rows = expected_rows
        checkpoint.last_scanned_id = 0
        checkpoint.scanned_rows = 0
        checkpoint.updated_rows = 0
        checkpoint.unchanged_rows = 0
        checkpoint.data_checksum_before = _EMPTY_SHA256
        checkpoint.data_checksum_after = _EMPTY_SHA256
        checkpoint.ownership_checksum_after = _EMPTY_SHA256
        checkpoint.started_at = reset_at
        checkpoint.updated_at = reset_at
        checkpoint.completed_at = (
            reset_at
            if target_status is RawOwnershipBackfillStatus.COMPLETED
            else None
        )
        await session.flush()

        last_locked_id: int | None = None
        while True:
            stmt = select(RawPayload.id).order_by(RawPayload.id).limit(
                _PREFLIGHT_PAGE_SIZE
            )
            if last_locked_id is not None:
                stmt = stmt.where(RawPayload.id > last_locked_id)
            ids = list(await session.scalars(stmt.with_for_update()))
            if not ids:
                break
            last_locked_id = ids[-1]
            if len(ids) < _PREFLIGHT_PAGE_SIZE:
                break

    await session.flush()
    return RawOwnershipBackfillRestoreBlockedResult(
        phase_key=checkpoint.phase_key,
        subject_id=scope.subject_id,
        status=target_status,
        scan_high_watermark_id=high_watermark,
        snapshot_rows=expected_rows,
        data_checksum_before=_EMPTY_SHA256,
        data_checksum_after=_EMPTY_SHA256,
        ownership_checksum_after=_EMPTY_SHA256,
    )


__all__ = [
    "DEFAULT_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "MAX_RAW_OWNERSHIP_BACKFILL_BATCH_SIZE",
    "RAW_OWNERSHIP_BACKFILL_PHASE",
    "RawOwnershipBackfillBatchResult",
    "RawOwnershipBackfillDuplicateError",
    "RawOwnershipBackfillError",
    "RawOwnershipBackfillIdentityError",
    "RawOwnershipBackfillMappingError",
    "RawOwnershipBackfillPreflightResult",
    "RawOwnershipBackfillRestoreBlockedResult",
    "RawOwnershipBackfillStateError",
    "RawOwnershipBackfillStatus",
    "RawOwnershipBackfillValidationError",
    "preflight_raw_ownership_backfill",
    "block_raw_ownership_backfill_for_portability_v1_restore",
    "run_raw_ownership_backfill_batch",
]
