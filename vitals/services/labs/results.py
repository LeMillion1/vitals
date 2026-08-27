"""Lab result commands, queries, and provenance enforcement."""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import date as date_type
from typing import Any, Optional, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
)
from vitals.models.ai import AIInvocation
from vitals.models.identity import HealthSubject
from vitals.models.labs import DOMAIN, LabMarker, LabResult
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import file_asset_service
from vitals.services.conflicts import engine
from .flags import compute_flag
from .markers import (
    _apply_marker_defaults,
    _existing_marker_display,
    _marker_for_update,
    _validated_marker_identity,
    normalize_marker_key,
)
from vitals.utils.timeutils import today_local

_VALUE_ABS_MAX = 1_000_000.0

def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: engine.PreparedConflictWrite,
) -> engine.ConflictWriteContext:
    """Prove the write names a subject and the decision that authorized it."""

    if identity is None or prepared is None:
        raise engine.ConflictPreparedWriteError(
            "scoped lab writes require identity and a prepared conflict write"
        )
    return engine.require_prepared_identity(
        session,
        prepared=prepared,
        identity=identity,
    )


def _require_evaluation_date(
    context: engine.ConflictWriteContext,
    on_date: date_type,
) -> None:
    if context.evaluation_date != on_date:
        raise engine.ConflictPreparedWriteError(
            "lab result date does not match prepared conflict evaluation date"
        )


def _subject_scope(model, subject_id: uuid.UUID):
    return model.subject_id == subject_id



def _result_entity_key(result_id: int) -> str:
    return str(result_id)


def _proposed_result(
    *, marker: str, value: float, flag: str | None, result_id: int | None = None
) -> dict[str, Any]:
    proposed: dict[str, Any] = {"marker": marker, "value": value, "flag": flag}
    if result_id is not None:
        proposed[engine.CONFLICT_ENTITY_KEY] = _result_entity_key(result_id)
    return proposed



def _result_by_id_stmt(
    result_id: int,
    *,
    subject_id: uuid.UUID,
):
    stmt = select(LabResult).where(LabResult.id == result_id)
    stmt = stmt.where(_subject_scope(LabResult, subject_id))
    return stmt


async def _get_result_for_update(
    session: AsyncSession,
    result_id: int,
    *,
    subject_id: uuid.UUID,
) -> LabResult | None:
    return await session.scalar(
        _result_by_id_stmt(
            result_id,
            subject_id=subject_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _lock_result_provenance_before_row(
    session: AsyncSession,
    result_id: int,
    *,
    context: engine.ConflictWriteContext,
) -> tuple[int | None, str] | None:
    """Read the scoped FK, then lock raw/file roots before the result row."""

    candidate = (
        await session.execute(
            select(LabResult.raw_payload_id, LabResult.source).where(
                LabResult.id == result_id,
                _subject_scope(
                    LabResult,
                    context.identity.subject_id,
                ),
            )
        )
    ).first()
    if candidate is None:
        return None
    raw_payload_id, source = candidate
    if raw_payload_id is not None:
        await _lock_result_raw(
            session,
            raw_payload_id=raw_payload_id,
            context=context,
            source=source,
            require_mcp_roots=source == Source.MCP.value,
            # The real gate is inside: a Stage-3A parser raw is adopted only on
            # a fully-unowned conflict bridge, and only for a parser source.
            allow_historical_parser_raw=True,
        )
    return raw_payload_id, source


async def get_result_for_update(
    session: AsyncSession,
    result_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> LabResult | None:
    """Lock one visible result for a boundary-side partial-update merge."""

    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    provenance = await _lock_result_provenance_before_row(
        session,
        result_id,
        context=context,
    )
    row = await _get_result_for_update(
        session,
        result_id,
        subject_id=identity.subject_id,
    )
    if row is not None and provenance != (row.raw_payload_id, row.source):
        raise engine.ConflictRawOwnershipError(
            "lab result provenance changed while acquiring write locks"
        )
    return row


async def _lock_historical_parser_connection_before_raw(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    context: engine.ConflictWriteContext,
    source: str,
    allow_historical_parser_raw: bool,
) -> uuid.UUID | None:
    """Lock the inferred legacy C before raw without widening live uploads."""

    if not (
        allow_historical_parser_raw
        and context.scope.include_legacy_unowned
        and source == Source.LAB_PARSER.value
    ):
        return None
    projected = (
        await session.execute(
            select(
                RawPayload.subject_id,
                RawPayload.actor_user_id,
                RawPayload.integration_connection_id,
                RawPayload.file_asset_id,
                RawPayload.domain,
                RawPayload.source,
            ).where(RawPayload.id == raw_payload_id)
        )
    ).first()
    if projected is None:
        return None
    (
        subject_id,
        actor_user_id,
        connection_id,
        file_asset_id,
        raw_domain,
        raw_source,
    ) = projected
    if not (
        subject_id == context.identity.subject_id
        and actor_user_id is None
        and connection_id is not None
        and file_asset_id is None
        and raw_domain == DOMAIN
        and raw_source == Source.LAB_PARSER.value
    ):
        return None
    subject_ids = list(
        await session.scalars(
            select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
        )
    )
    if subject_ids != [context.identity.subject_id]:
        raise engine.ConflictRawOwnershipError(
            "historical lab parser raw requires exactly one subject"
        )
    historical_statuses = tuple(
        status.value
        for status in IntegrationConnectionStatus
        if status is not IntegrationConnectionStatus.PENDING
    )
    connection = await session.scalar(
        select(IntegrationConnection)
        .where(
            IntegrationConnection.id == connection_id,
            IntegrationConnection.subject_id == context.identity.subject_id,
            IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.AI_GATEWAY.value,
            IntegrationConnection.status.in_(historical_statuses),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if connection is None:
        raise engine.ConflictRawOwnershipError(
            "historical lab parser AI gateway provenance is invalid"
        )
    return connection.id


async def _lock_result_raw(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    context: engine.ConflictWriteContext,
    source: str,
    require_mcp_roots: bool = False,
    allow_historical_parser_raw: bool = False,
) -> RawPayload:
    if not isinstance(allow_historical_parser_raw, bool):
        raise TypeError("allow_historical_parser_raw must be a bool")
    historical_connection_id = (
        await _lock_historical_parser_connection_before_raw(
            session,
            raw_payload_id=raw_payload_id,
            context=context,
            source=source,
            allow_historical_parser_raw=allow_historical_parser_raw,
        )
    )
    exact_raw, fully_unowned_raw = engine.raw_payload_scope_conditions(
        context.scope
    )
    allowed_raw = exact_raw
    if context.scope.include_legacy_unowned:
        allowed_raw = or_(allowed_raw, fully_unowned_raw)
    raw = await session.scalar(
        select(RawPayload)
        .where(RawPayload.id == raw_payload_id, allowed_raw)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if raw is None:
        raise engine.ConflictRawOwnershipError(
            "lab result raw provenance is outside the prepared subject scope"
        )
    if raw.domain != DOMAIN or raw.source != source:
        raise engine.ConflictRawOwnershipError(
            "lab result raw provenance has a mismatched domain or source"
        )
    if historical_connection_id is not None:
        if (
            raw.subject_id != context.identity.subject_id
            or raw.actor_user_id is not None
            or raw.integration_connection_id != historical_connection_id
            or raw.file_asset_id is not None
            or raw.source != Source.LAB_PARSER.value
        ):
            raise engine.ConflictRawOwnershipError(
                "historical lab parser provenance changed while acquiring locks"
            )
        invocation_id = await session.scalar(
            select(AIInvocation.id)
            .where(AIInvocation.raw_payload_id == raw.id)
            .order_by(AIInvocation.created_at, AIInvocation.id)
            .limit(1)
            .with_for_update()
        )
        if invocation_id is not None:
            raise engine.ConflictRawOwnershipError(
                "historical lab parser raw mixes subject and platform AI provenance"
            )
        return raw
    if require_mcp_roots and (
        raw.integration_connection_id is not None or raw.file_asset_id is not None
    ):
        raise engine.ConflictRawOwnershipError(
            "structured MCP lab provenance cannot carry connection or file roots"
        )
    asset: FileAsset | None = None
    if raw.file_asset_id is not None:
        asset = await session.scalar(
            select(FileAsset)
            .where(
                FileAsset.id == raw.file_asset_id,
                FileAsset.subject_id == context.identity.subject_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if asset is None:
            raise engine.ConflictRawOwnershipError(
                "lab result file provenance is outside the prepared subject scope"
            )
    if source == Source.LAB_PARSER.value:
        if asset is None:
            if raw.subject_id is not None:
                raise engine.ConflictRawOwnershipError(
                    "owned lab parser provenance has no file root"
                )
        else:
            await _validate_parser_upload_chain(
                session,
                raw=raw,
                asset=asset,
                identity=context.identity,
                require_boundary_actor=False,
            )
    return raw


async def _validate_parser_upload_chain(
    session: AsyncSession,
    *,
    raw: RawPayload,
    asset: FileAsset,
    identity: WriteIdentity,
    require_boundary_actor: bool,
) -> None:
    """Validate Labs-specific A/C/F provenance after raw/file locks."""

    owner_user_id = await session.scalar(
        select(HealthSubject.owner_user_id).where(
            HealthSubject.id == identity.subject_id
        )
    )
    if owner_user_id is None or raw.actor_user_id != owner_user_id:
        raise engine.ConflictRawOwnershipError(
            "lab parser actor is not the subject owner"
        )
    if raw.actor_user_id != asset.uploaded_by_user_id:
        raise engine.ConflictRawOwnershipError(
            "lab parser raw actor does not match the file uploader"
        )
    if require_boundary_actor:
        if identity.actor_user_id is None:
            raise engine.ConflictPreparedWriteError(
                "lab upload confirmation requires an active human actor"
            )
        if (
            raw.actor_user_id != identity.actor_user_id
            or asset.uploaded_by_user_id != identity.actor_user_id
        ):
            raise engine.ConflictRawOwnershipError(
                "lab upload actor does not match the prepared writer"
            )
    if (
        asset.subject_id != identity.subject_id
        or asset.purpose != FileAssetPurpose.LAB_DOCUMENT.value
        or not file_asset_service.local_asset_is_live(asset)
        or raw.external_id != asset.storage_ref
    ):
        raise engine.ConflictRawOwnershipError(
            "lab parser file provenance is inconsistent"
        )
    platform_rows = list(
        await session.scalars(
            select(AIInvocation)
            .where(
                AIInvocation.raw_payload_id == raw.id,
                AIInvocation.purpose
                == AIInvocationPurpose.LAB_DOCUMENT_PARSE.value,
            )
            .order_by(AIInvocation.created_at, AIInvocation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if raw.integration_connection_id is None:
        succeeded = [
            row
            for row in platform_rows
            if row.status == AIInvocationStatus.SUCCEEDED.value
        ]
        if len(succeeded) != 1:
            raise engine.ConflictRawOwnershipError(
                "lab parser raw lacks one successful platform AI invocation"
            )
        invocation = succeeded[0]
        if (
            invocation.subject_id != identity.subject_id
            or invocation.actor_user_id != raw.actor_user_id
            or invocation.source != AIInvocationSource.WEB.value
            or invocation.raw_payload_id != raw.id
        ):
            raise engine.ConflictRawOwnershipError(
                "lab parser platform AI provenance is inconsistent"
            )
        if (
            not isinstance(raw.payload, dict)
            or "_ai_parse" in raw.payload
            or "_unparsed" in raw.payload
            or not isinstance(raw.payload.get("results"), list)
        ):
            raise engine.ConflictRawOwnershipError(
                "successful platform lab raw has no validated extraction"
            )
        return
    if platform_rows:
        raise engine.ConflictRawOwnershipError(
            "lab parser raw mixes subject and platform AI provenance"
        )
    historical_statuses = tuple(
        status.value
        for status in IntegrationConnectionStatus
        if status is not IntegrationConnectionStatus.PENDING
    )
    connection = await session.scalar(
        select(IntegrationConnection)
        .where(
            IntegrationConnection.id == raw.integration_connection_id,
            IntegrationConnection.subject_id == identity.subject_id,
            IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.AI_GATEWAY.value,
            IntegrationConnection.status.in_(historical_statuses),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if connection is None:
        raise engine.ConflictRawOwnershipError(
            "lab parser AI gateway provenance is invalid"
        )



async def add_result(
    session: AsyncSession,
    *,
    on_date: date_type,
    marker: str,
    value: float,
    unit: Optional[str] = None,
    ref_low: Optional[float] = None,
    ref_high: Optional[float] = None,
    lab_name: Optional[str] = None,
    note: Optional[str] = None,
    source: str = Source.MANUAL.value,
    raw_payload_id: Optional[int] = None,
    override: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
    allow_historical_parser_raw: bool = False,
) -> LabResult:
    """Record a marker value, computing its flag and ensuring its catalog row.

    If the result carries no range, fall back to the catalog's default range so a
    flag can still be computed. Raises ``ValueError`` on a nameless marker or an
    implausible value, and :class:`ConflictBlocked` when a hard cross-domain rule
    fires without ``override``."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    _require_evaluation_date(context, on_date)
    if raw_payload_id is not None:
        await _lock_result_raw(
            session,
            raw_payload_id=raw_payload_id,
            context=context,
            source=source,
            require_mcp_roots=source == Source.MCP.value,
            allow_historical_parser_raw=allow_historical_parser_raw,
        )
    marker_original, marker_display, marker_key = _validated_marker_identity(marker)
    if value is None or not math.isfinite(value) or abs(value) > _VALUE_ABS_MAX:
        raise ValueError(f"implausible lab value for {marker_display}: {value!r}")
    catalog = await _marker_for_update(
        session,
        marker_key,
        subject_id=identity.subject_id,
    )
    marker = (
        catalog.name
        if catalog is not None
        else (
            await _existing_marker_display(
                session,
                marker_key=marker_key,
                subject_id=identity.subject_id,
            )
            or marker_display
        )
    )
    eff_low = ref_low if ref_low is not None else (catalog.ref_low if catalog else None)
    eff_high = ref_high if ref_high is not None else (catalog.ref_high if catalog else None)
    flag = compute_flag(value, eff_low, eff_high)

    # The same gate every other write path runs, before the row exists: a hard
    # rule (an active potassium supplement meeting a hyperkalemic potassium
    # result) stops the save unless the caller overrides, and soft rules keep
    # doing what they did — an alert, never a block.
    proposed = _proposed_result(marker=marker, value=value, flag=flag)
    assert prepared_conflict_write is not None
    await engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.LABS,
        proposed_state=proposed,
        override=override,
        entity_ref=f"labs:{marker}",
    )

    if catalog is None:
        catalog = LabMarker(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            domain=DOMAIN,
            name=marker,
            normalized_name=marker_key,
            is_canonical=True,
        )
        session.add(catalog)
    _apply_marker_defaults(
        catalog,
        unit=unit,
        ref_low=ref_low,
        ref_high=ref_high,
    )

    row = LabResult(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        date=on_date,
        domain=DOMAIN,
        source=source,
        marker=marker,
        marker_key=marker_key,
        marker_original=marker_original,
        value=value,
        unit=unit or catalog.unit,
        ref_low=eff_low,
        ref_high=eff_high,
        flag=flag,
        lab_name=lab_name,
        note=note,
        raw_payload_id=raw_payload_id,
    )
    session.add(row)
    await session.flush()
    return row


async def update_result(
    session: AsyncSession,
    result_id: int,
    *,
    on_date: Optional[date_type] = None,
    marker: Optional[str] = None,
    value: Optional[float] = None,
    unit: Optional[str] = None,
    ref_low: Optional[float] = None,
    ref_high: Optional[float] = None,
    lab_name: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[LabResult]:
    """Correct an existing result — a mistyped value or a range read off the wrong
    column. Only the fields passed are changed; ``flag`` is recomputed from the
    resulting value + range, and the alerts derived from it are refreshed.

    Without this, fixing a typo meant deleting the row and re-adding it, which is
    the one thing this project promises never to do to a measurement."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if on_date is not None:
        _require_evaluation_date(context, on_date)
    row = await get_result_for_update(
        session,
        result_id,
        identity=identity,
        prepared_conflict_write=prepared_conflict_write,
    )
    if row is None:
        return None

    next_marker = row.marker
    next_marker_key = row.marker_key
    next_marker_original = row.marker_original
    if marker is not None:
        next_marker_original, next_marker, next_marker_key = (
            _validated_marker_identity(marker)
        )
    next_value = row.value
    if value is not None:
        if not math.isfinite(value) or abs(value) > _VALUE_ABS_MAX:
            raise ValueError(f"implausible lab value for {next_marker}: {value!r}")
        next_value = value
    next_date = on_date if on_date is not None else row.date
    _require_evaluation_date(context, next_date)

    next_unit = unit if unit is not None else row.unit
    next_low = ref_low if ref_low is not None else row.ref_low
    next_high = ref_high if ref_high is not None else row.ref_high
    catalog = await _marker_for_update(
        session,
        next_marker_key,
        subject_id=identity.subject_id,
    )
    if catalog is not None:
        next_marker = catalog.name
    else:
        next_marker = (
            await _existing_marker_display(
                session,
                marker_key=next_marker_key,
                subject_id=identity.subject_id,
            )
            or next_marker
        )
    if next_low is None and catalog is not None:
        next_low = catalog.ref_low
    if next_high is None and catalog is not None:
        next_high = catalog.ref_high
    next_flag = compute_flag(next_value, next_low, next_high)

    proposed = _proposed_result(
        marker=next_marker,
        value=next_value,
        flag=next_flag,
        result_id=row.id,
    )
    assert prepared_conflict_write is not None
    await engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.LABS,
        proposed_state=proposed,
        override=override,
        entity_ref=f"labs:{next_marker}",
        replace_entity_key=_result_entity_key(row.id),
    )

    if catalog is None:
        catalog = LabMarker(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            domain=DOMAIN,
            name=next_marker,
            normalized_name=next_marker_key,
            is_canonical=True,
        )
        session.add(catalog)
    _apply_marker_defaults(
        catalog,
        unit=next_unit,
        ref_low=next_low,
        ref_high=next_high,
    )

    row.date = next_date
    row.marker = next_marker
    row.marker_key = next_marker_key
    row.marker_original = next_marker_original
    row.value = next_value
    row.unit = next_unit or catalog.unit
    row.ref_low = next_low
    row.ref_high = next_high
    row.flag = next_flag
    if lab_name is not None:
        row.lab_name = lab_name
    if note is not None:
        row.note = note

    await session.flush()
    from .alerts import refresh_alerts

    await refresh_alerts(
        session,
        subject_id=identity.subject_id,
        on_date=context.evaluation_date,
        identity=identity,
        prepared_conflict_write=prepared_conflict_write,
    )
    return row


async def update_result_note(
    session: AsyncSession,
    result_id: int,
    *,
    note: str,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[LabResult]:
    """Update only a result note without changing its source/raw provenance."""

    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await get_result_for_update(
        session,
        result_id,
        identity=identity,
        prepared_conflict_write=prepared_conflict_write,
    )
    if row is None:
        return None
    if row.subject_id is None:
        row.subject_id = identity.subject_id
    row.note = note
    await session.flush()
    return row


async def list_results(
    session: AsyncSession,
    *,
    marker: Optional[str] = None,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    has_note: bool = False,
    limit: int = 200,
    subject_id: uuid.UUID,
) -> Sequence[LabResult]:
    """Newest first. ``end`` anchors the read at a date instead of at "now", so a
    report about a past window is not filled by results drawn after it."""
    filters = [_subject_scope(LabResult, subject_id)]
    if marker is not None:
        marker_key = normalize_marker_key(marker)
        filters.append(LabResult.marker_key == marker_key)
    if start is not None:
        filters.append(LabResult.date >= start)
    if end is not None:
        filters.append(LabResult.date <= end)
    if has_note:
        filters.extend((LabResult.note.is_not(None), LabResult.note != ""))

    stmt = select(LabResult).where(*filters)
    scope = engine.ConflictScope(
        subject_id=subject_id,
        evaluation_date=end or today_local(),
    )
    exact_raw, fully_unowned_raw = engine.raw_payload_scope_conditions(
        scope
    )
    # The nightly replay deliberately owns the normalized facts while leaving a
    # legacy raw legacy — it has no authoritative provider or file roots to
    # adopt. That shape is valid provenance for a parsed fact that already names
    # its subject. An MCP fact has no such history: it is written raw-first and
    # must cite a raw of its own.
    allowed_linked_raw = or_(
        exact_raw,
        and_(fully_unowned_raw, LabResult.source == Source.LAB_PARSER.value),
    )
    invalid = await session.scalar(
        select(1)
        .select_from(LabResult)
        .outerjoin(RawPayload, LabResult.raw_payload_id == RawPayload.id)
        .where(
            *filters,
            LabResult.raw_payload_id.is_not(None),
            allowed_linked_raw.is_not(True),
        )
        .limit(1)
    )
    if invalid is not None:
        raise engine.ConflictRawOwnershipError(
            "lab result links to foreign or partial raw provenance"
        )
    stmt = stmt.outerjoin(
        RawPayload,
        LabResult.raw_payload_id == RawPayload.id,
    ).where(
        or_(LabResult.raw_payload_id.is_(None), allowed_linked_raw)
    )
    stmt = stmt.order_by(LabResult.date.desc(), LabResult.id.desc()).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


@dataclass(frozen=True, slots=True)
class LabResultProjection:
    """Column-minimal normalized result exposed to clinical summaries."""

    marker: str
    value: float
    unit: str | None
    flag: str | None
    date: date_type
    ref_low: float | None
    ref_high: float | None


@dataclass(frozen=True, slots=True)
class BoundedLatestLabResults:
    """Latest result per marker with an honest marker-cap signal."""

    rows: tuple[LabResultProjection, ...]
    truncated: bool


async def _bounded_latest_result_projection_by_marker(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    end: date_type,
    marker_limit: int,
    validate_linked_raw: bool,
) -> BoundedLatestLabResults:
    """Project one newest normalized row per marker under a reviewed policy."""

    if not 1 <= marker_limit <= 500:
        raise ValueError("clinical lab marker_limit must be between 1 and 500")
    filters = (
        _subject_scope(LabResult, subject_id),
        LabResult.date <= end,
    )
    ranked_stmt = select(
        LabResult.id.label("result_id"),
        func.row_number()
        .over(
            partition_by=LabResult.marker_key,
            order_by=(LabResult.date.desc(), LabResult.id.desc()),
        )
        .label("marker_rank"),
    )
    if validate_linked_raw:
        scope = engine.ConflictScope(
            subject_id=subject_id,
            evaluation_date=end,
        )
        exact_raw, fully_unowned_raw = engine.raw_payload_scope_conditions(scope)
        allowed_linked_raw = or_(
            exact_raw,
            and_(fully_unowned_raw, LabResult.source == Source.LAB_PARSER.value),
        )
        invalid = await session.scalar(
            select(1)
            .select_from(LabResult)
            .outerjoin(RawPayload, LabResult.raw_payload_id == RawPayload.id)
            .where(
                *filters,
                LabResult.raw_payload_id.is_not(None),
                allowed_linked_raw.is_not(True),
            )
            .limit(1)
        )
        if invalid is not None:
            raise engine.ConflictRawOwnershipError(
                "lab result links to foreign or partial raw provenance"
            )
        ranked_stmt = ranked_stmt.outerjoin(
            RawPayload,
            LabResult.raw_payload_id == RawPayload.id,
        ).where(
            *filters,
            or_(LabResult.raw_payload_id.is_(None), allowed_linked_raw),
        )
    else:
        # Break-glass is intentionally normalized and column-minimal.  It must
        # not traverse or even inspect raw/file provenance graphs.
        ranked_stmt = ranked_stmt.where(*filters)
    ranked = ranked_stmt.subquery()
    candidates = (
        await session.execute(
            select(
                LabResult.marker,
                LabResult.value,
                LabResult.unit,
                LabResult.flag,
                LabResult.date,
                LabResult.ref_low,
                LabResult.ref_high,
            )
            .join(ranked, LabResult.id == ranked.c.result_id)
            .where(ranked.c.marker_rank == 1)
            .order_by(LabResult.marker_key, LabResult.id)
            .limit(marker_limit + 1)
        )
    ).all()
    rows = tuple(
        LabResultProjection(
            marker=row.marker,
            value=row.value,
            unit=row.unit,
            flag=row.flag,
            date=row.date,
            ref_low=row.ref_low,
            ref_high=row.ref_high,
        )
        for row in candidates[:marker_limit]
    )
    return BoundedLatestLabResults(
        rows=rows,
        truncated=len(candidates) > marker_limit,
    )


async def bounded_latest_results_by_marker(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    end: date_type,
    marker_limit: int = 200,
) -> BoundedLatestLabResults:
    """Return the care projection, failing closed on linked raw ownership."""

    if not 1 <= marker_limit <= 500:
        raise ValueError("care lab marker_limit must be between 1 and 500")
    return await _bounded_latest_result_projection_by_marker(
        session,
        subject_id=subject_id,
        end=end,
        marker_limit=marker_limit,
        validate_linked_raw=True,
    )


async def emergency_latest_results_by_marker(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    end: date_type,
    marker_limit: int = 200,
) -> BoundedLatestLabResults:
    """Return the exact normalized Lab slice reviewed for break-glass."""

    return await _bounded_latest_result_projection_by_marker(
        session,
        subject_id=subject_id,
        end=end,
        marker_limit=marker_limit,
        validate_linked_raw=False,
    )


async def marker_history(
    session: AsyncSession,
    marker: str,
    *,
    subject_id: uuid.UUID,
) -> list[dict]:
    """Chronological series for one marker (the per-marker chart)."""
    rows = await list_results(
        session,
        marker=marker,
        limit=1_000_000,
        subject_id=subject_id,
    )
    return [
        {
            "date": r.date.isoformat(),
            "value": r.value,
            "flag": r.flag,
            "ref_low": r.ref_low,
            "ref_high": r.ref_high,
        }
        for r in reversed(rows)
    ]


async def latest_per_marker(
    session: AsyncSession,
    *,
    end: date_type | None = None,
    subject_id: uuid.UUID,
) -> list[LabResult]:
    """The most recent result for each marker (table + alert source)."""
    rows = await list_results(
        session,
        end=end,
        limit=1_000_000,
        subject_id=subject_id,
    )
    seen: dict[str, LabResult] = {}
    for r in rows:
        seen.setdefault(r.marker_key, r)
    return list(seen.values())


async def legacy_unowned_present(session: AsyncSession) -> bool:
    """Mirror of this module's widening, on both halves of it.

    The resolver widens to unowned lab rows; the upload path widens separately
    to unowned raw provenance, and a raw-first backfill can leave one without
    the other. Either alone is something the bridge would adopt.
    """

    found = await session.scalar(
        select(LabResult.id)
        .where(
            LabResult.subject_id.is_(None),
            LabResult.actor_user_id.is_(None),
        )
        .limit(1)
    )
    if found is not None:
        return True
    return await engine.legacy_unowned_raw_present(session)


async def resolve_latest_scoped(
    session: AsyncSession,
    *,
    scope: engine.ConflictScope,
) -> list[dict]:
    """Conflict resolver restricted to one explicit subject boundary."""

    exact_raw, fully_unowned_raw = engine.raw_payload_scope_conditions(
        scope
    )
    fact_scope = LabResult.subject_id == scope.subject_id
    if scope.include_legacy_unowned:
        fact_scope = or_(
            fact_scope,
            and_(
                LabResult.subject_id.is_(None),
                LabResult.actor_user_id.is_(None),
            ),
        )
    allowed_linked_raw = exact_raw
    if scope.include_legacy_unowned:
        # Raw-first backfill may already have attached the exact subject root
        # while its normalized lab row is still fully legacy-owned.
        allowed_linked_raw = or_(allowed_linked_raw, fully_unowned_raw)
    invalid_raw_id = await session.scalar(
        select(1)
        .select_from(LabResult)
        .outerjoin(
            RawPayload,
            LabResult.raw_payload_id == RawPayload.id,
        )
        .where(
            LabResult.date <= scope.evaluation_date,
            fact_scope,
            LabResult.raw_payload_id.is_not(None),
            allowed_linked_raw.is_not(True),
        )
        .limit(1)
    )
    if invalid_raw_id is not None:
        raise engine.ConflictRawOwnershipError(
            "lab result links to foreign or partial raw provenance"
        )
    rows = list(
        await session.scalars(
            select(LabResult)
            .outerjoin(
                RawPayload,
                LabResult.raw_payload_id == RawPayload.id,
            )
            .where(
                LabResult.date <= scope.evaluation_date,
                fact_scope,
                or_(
                    LabResult.raw_payload_id.is_(None),
                    allowed_linked_raw,
                ),
            )
            .order_by(LabResult.date.desc(), LabResult.id.desc())
        )
    )
    latest_by_marker: dict[str, LabResult] = {}
    for row in rows:
        latest_by_marker.setdefault(row.marker_key, row)
    latest = list(latest_by_marker.values())
    return [
        _proposed_result(
            marker=r.marker,
            value=r.value,
            flag=r.flag,
            result_id=r.id,
        )
        for r in latest
    ]


async def delete_result(
    session: AsyncSession,
    result_id: int,
    *,
    subject_id: uuid.UUID,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> bool:
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if subject_id is not None and subject_id != identity.subject_id:
        raise engine.ConflictPreparedWriteError(
            "subject_id does not match prepared lab write identity"
        )
    subject_id = identity.subject_id
    row = await get_result_for_update(
        session,
        result_id,
        identity=identity,
        prepared_conflict_write=prepared_conflict_write,
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    from .alerts import refresh_alerts

    await refresh_alerts(
        session,
        subject_id=identity.subject_id,
        on_date=context.evaluation_date,
        identity=identity,
        prepared_conflict_write=prepared_conflict_write,
    )
    return True
