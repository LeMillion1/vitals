"""Raw-first body-scan ingestion and immutable lineage validation."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.analytics.body_metrics import weight_from_scan
from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
)
from vitals.models.ai import AIInvocation
from vitals.models.body_scan import DOMAIN, BodyScan, BodyScanMetric
from vitals.models.identity import HealthSubject
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services.files import contracts as file_contracts
from vitals.services.conflicts import engine
from vitals.services.weight import writes as weight_writes
from vitals.services.weight.contracts import PreparedWeightWrite
from vitals.utils.timeutils import now_local

from .contracts import (
    BodyScanOwnershipError,
    BodyScanRawAlreadyNormalizedError,
    require_evaluation_date as _require_evaluation_date,
    require_scoped_prepared_write as _require_scoped_prepared_write,
)
from .normalization import (
    _normalize_item,
    _parse_date,
    normalize_extracted,
)

async def _subject_owner_user_id(
    session: AsyncSession,
    subject_id: uuid.UUID,
) -> uuid.UUID:
    owner_user_id = await session.scalar(
        select(HealthSubject.owner_user_id).where(HealthSubject.id == subject_id)
    )
    if owner_user_id is None:
        raise BodyScanOwnershipError("body-scan subject has no durable owner")
    return owner_user_id


def _create_conflict_entity_ref(raw_payload_id: int | None) -> str:
    """Unique create-operation identity for per-scan conflict alerts.

    Durable raw-backed creates use their immutable raw id, making a retried
    confirmation address the same alert. A rawless manual scan is a genuinely
    new fact even when its date and values match another scan, so it receives a
    fresh operation UUID rather than collapsing onto a date-based key.
    """

    if raw_payload_id is not None:
        return f"body_scan:raw:{raw_payload_id}"
    return f"body_scan:create:{uuid.uuid4().hex}"

async def _validate_upload_chain(
    session: AsyncSession,
    *,
    raw: RawPayload,
    asset: FileAsset,
    identity: WriteIdentity,
    require_boundary_actor: bool,
    for_update: bool = True,
) -> None:
    owner_user_id = await _subject_owner_user_id(session, identity.subject_id)
    if raw.subject_id != identity.subject_id or asset.subject_id != identity.subject_id:
        raise engine.ConflictRawOwnershipError(
            "body-scan upload is outside the prepared subject"
        )
    if (
        identity.actor_user_id != owner_user_id
        or raw.actor_user_id != owner_user_id
        or asset.uploaded_by_user_id != owner_user_id
    ):
        raise engine.ConflictRawOwnershipError(
            "body-scan upload actor does not match the subject owner"
        )
    if require_boundary_actor:
        if identity.actor_user_id is None:
            raise engine.ConflictPreparedWriteError(
                "body-scan upload confirmation requires an active human actor"
            )
        if raw.actor_user_id != identity.actor_user_id:
            raise engine.ConflictRawOwnershipError(
                "body-scan upload actor does not match the prepared writer"
            )
    if (
        raw.domain != DOMAIN
        or raw.source != Source.BODY_SCAN.value
        or raw.file_asset_id != asset.id
        or asset.purpose != FileAssetPurpose.BODY_SCAN_DOCUMENT.value
        or not file_contracts.local_asset_is_live(asset)
        or raw.external_id != asset.storage_ref
    ):
        raise engine.ConflictRawOwnershipError(
            "body-scan upload provenance is inconsistent"
        )
    invocations = await _body_scan_parse_invocations(
        session,
        raw_payload_id=raw.id,
        for_update=for_update,
    )
    if raw.integration_connection_id is None:
        if len(invocations) != 1:
            raise engine.ConflictRawOwnershipError(
                "platform body-scan raw requires one parser invocation"
            )
        invocation = invocations[0]
        if (
            invocation.subject_id != identity.subject_id
            or invocation.actor_user_id != owner_user_id
            or invocation.raw_payload_id != raw.id
            or invocation.source != AIInvocationSource.WEB.value
            or invocation.status != AIInvocationStatus.SUCCEEDED.value
        ):
            raise engine.ConflictRawOwnershipError(
                "platform body-scan parser provenance is invalid"
            )
        return
    if invocations:
        raise engine.ConflictRawOwnershipError(
            "body-scan raw mixes subject and platform parser provenance"
        )
    historical_statuses = tuple(
        status.value
        for status in IntegrationConnectionStatus
        if status is not IntegrationConnectionStatus.PENDING
    )
    connection_stmt = select(IntegrationConnection).where(
        IntegrationConnection.id == raw.integration_connection_id,
        IntegrationConnection.subject_id == identity.subject_id,
        IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
        IntegrationConnection.connection_type
        == IntegrationConnectionType.AI_GATEWAY.value,
        IntegrationConnection.status.in_(historical_statuses),
    )
    if for_update:
        connection_stmt = connection_stmt.with_for_update()
    connection = await session.scalar(
        connection_stmt.execution_options(populate_existing=True)
    )
    if connection is None:
        raise engine.ConflictRawOwnershipError(
            "body-scan parser AI gateway provenance is invalid"
        )


async def _body_scan_parse_invocations(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    for_update: bool,
) -> list[AIInvocation]:
    invocation_stmt = select(AIInvocation).where(
        AIInvocation.raw_payload_id == raw_payload_id,
        AIInvocation.purpose == AIInvocationPurpose.BODY_SCAN_PARSE.value,
    ).order_by(AIInvocation.created_at, AIInvocation.id)
    if for_update:
        invocation_stmt = invocation_stmt.with_for_update()
    invocation_stmt = invocation_stmt.execution_options(populate_existing=True)
    return list(await session.scalars(invocation_stmt))


async def _lock_historical_parser_connection_before_raw(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    subject_id: uuid.UUID,
    allow_historical_parser_raw: bool,
    for_update: bool,
) -> uuid.UUID | None:
    """Validate exact Stage-3A parser roots and acquire C before raw."""

    if not allow_historical_parser_raw:
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
        raw_subject_id,
        actor_user_id,
        connection_id,
        file_asset_id,
        raw_domain,
        raw_source,
    ) = projected
    if not (
        raw_subject_id == subject_id
        and actor_user_id is None
        and connection_id is not None
        and file_asset_id is None
        and raw_domain == DOMAIN
        and raw_source == Source.BODY_SCAN.value
    ):
        return None
    subject_ids = list(
        await session.scalars(
            select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
        )
    )
    if subject_ids != [subject_id]:
        raise engine.ConflictRawOwnershipError(
            "historical body-scan parser raw requires exactly one subject"
        )
    historical_statuses = tuple(
        status.value
        for status in IntegrationConnectionStatus
        if status is not IntegrationConnectionStatus.PENDING
    )
    stmt = select(IntegrationConnection).where(
        IntegrationConnection.id == connection_id,
        IntegrationConnection.subject_id == subject_id,
        IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
        IntegrationConnection.connection_type
        == IntegrationConnectionType.AI_GATEWAY.value,
        IntegrationConnection.status.in_(historical_statuses),
    )
    if for_update:
        stmt = stmt.with_for_update()
    connection = await session.scalar(
        stmt.execution_options(populate_existing=True)
    )
    if connection is None:
        raise engine.ConflictRawOwnershipError(
            "historical body-scan parser AI gateway provenance is invalid"
        )
    return connection.id


async def _reject_historical_parser_invocation(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    for_update: bool,
) -> None:
    stmt = (
        select(AIInvocation.id)
        .where(AIInvocation.raw_payload_id == raw_payload_id)
        .order_by(AIInvocation.created_at, AIInvocation.id)
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    if await session.scalar(stmt) is not None:
        raise engine.ConflictRawOwnershipError(
            "historical body-scan raw mixes subject and platform AI provenance"
        )


async def _historical_mcp_raw_before_lock(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    subject_id: uuid.UUID,
    allow_historical_mcp_raw: bool,
) -> bool:
    """Recognize only the exact connectionless Stage-3A MCP history shape."""

    if not allow_historical_mcp_raw:
        return False
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
    if projected != (
        subject_id,
        None,
        None,
        None,
        DOMAIN,
        Source.MCP.value,
    ):
        return False
    subject_ids = list(
        await session.scalars(
            select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
        )
    )
    if subject_ids != [subject_id]:
        raise engine.ConflictRawOwnershipError(
            "historical MCP body-scan raw requires exactly one subject"
        )
    return True


async def _lock_owned_raw(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    context: engine.ConflictWriteContext,
    expected_source: str,
    require_boundary_actor: bool,
    file_key: str | None = None,
    allow_historical_parser_raw: bool = False,
) -> tuple[RawPayload, FileAsset | None]:
    if not isinstance(allow_historical_parser_raw, bool):
        raise TypeError("allow_historical_parser_raw must be a bool")
    historical_connection_id = (
        await _lock_historical_parser_connection_before_raw(
            session,
            raw_payload_id=raw_payload_id,
            subject_id=context.identity.subject_id,
            allow_historical_parser_raw=(
                allow_historical_parser_raw
                and context.scope.include_legacy_unowned
                and expected_source == Source.BODY_SCAN.value
            ),
            for_update=True,
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
            "body-scan raw provenance is outside the prepared subject"
        )
    if raw.domain != DOMAIN or raw.source != expected_source:
        raise engine.ConflictRawOwnershipError(
            "body-scan raw provenance has a mismatched domain or source"
        )
    if historical_connection_id is not None:
        if (
            raw.subject_id != context.identity.subject_id
            or raw.actor_user_id is not None
            or raw.integration_connection_id != historical_connection_id
            or raw.file_asset_id is not None
            or raw.source != Source.BODY_SCAN.value
        ):
            raise engine.ConflictRawOwnershipError(
                "historical body-scan provenance changed while acquiring locks"
            )
        if file_key is not None:
            raise engine.ConflictRawOwnershipError(
                "historical body-scan raw cannot authorize a file key"
            )
        await _reject_historical_parser_invocation(
            session,
            raw_payload_id=raw.id,
            for_update=True,
        )
        return raw, None
    is_legacy = raw.subject_id is None
    if is_legacy:
        if any(
            value is not None
            for value in (
                raw.actor_user_id,
                raw.integration_connection_id,
                raw.file_asset_id,
            )
        ):
            raise engine.ConflictRawOwnershipError(
                "legacy body-scan raw provenance must be fully unowned"
            )
        return raw, None

    if expected_source == Source.MCP.value:
        owner_user_id = await _subject_owner_user_id(
            session,
            context.identity.subject_id,
        )
        if (
            context.identity.actor_user_id != owner_user_id
            or raw.actor_user_id != owner_user_id
            or raw.integration_connection_id is not None
            or raw.file_asset_id is not None
        ):
            raise engine.ConflictRawOwnershipError(
                "structured MCP body-scan raw must have exact S/A and null C/F"
            )
        if await _body_scan_parse_invocations(
            session,
            raw_payload_id=raw.id,
            for_update=True,
        ):
            raise engine.ConflictRawOwnershipError(
                "structured MCP body-scan raw cannot claim an AI parser invocation"
            )
        return raw, None

    if raw.file_asset_id is None:
        raise engine.ConflictRawOwnershipError(
            "owned body-scan parser provenance has no file root"
        )
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
            "body-scan file provenance is outside the prepared subject"
        )
    if asset.status in {FileAssetStatus.DELETED.value, FileAssetStatus.PURGED.value}:
        raise engine.ConflictRawOwnershipError(
            "body-scan file provenance is no longer available"
        )
    if file_key is not None and file_key != asset.storage_ref:
        raise engine.ConflictRawOwnershipError(
            "body-scan file key does not match durable provenance"
        )
    await _validate_upload_chain(
        session,
        raw=raw,
        asset=asset,
        identity=context.identity,
        require_boundary_actor=require_boundary_actor,
    )
    return raw, asset


async def save_scan(
    session: AsyncSession,
    *,
    on_date: date_type,
    device: Optional[str] = None,
    file_key: Optional[str] = None,
    raw_payload_id: Optional[int] = None,
    metrics: Sequence[dict],
    note: Optional[str] = None,
    source: str = Source.BODY_SCAN.value,
    override: bool = False,
    identity: WriteIdentity,
    file_asset_id: uuid.UUID | None = None,
    prepared_weight_write: PreparedWeightWrite,
    allow_historical_parser_raw: bool = False,
) -> BodyScan:
    """Persist a scan and its metrics (owner-edited rows), stamp the raw payload
    processed, and bridge weight into the weight domain. Does not commit.

    May raise ``ConflictBlocked`` if a cross-domain block rule fires without
    ``override`` (override plumbing kept consistent with the weight domain)."""
    weight_context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    _require_evaluation_date(weight_context, on_date)

    # Validate every client-controlled capability before locking raw/file/fact
    # roots. PreparedWeightWrite proves governance -> Garmin advisory -> S/A.
    if weight_context is not None:
        if source not in {
            Source.MANUAL.value,
            Source.MCP.value,
            Source.BODY_SCAN.value,
        }:
            raise BodyScanOwnershipError(
                "unsupported scoped body-scan provenance source"
            )
        if source == Source.MANUAL.value and any(
            value is not None
            for value in (file_key, raw_payload_id, file_asset_id)
        ):
            raise engine.ConflictRawOwnershipError(
                "manual body scans cannot claim raw, file, or provider provenance"
            )
        if source == Source.MANUAL.value:
            owner_user_id = await _subject_owner_user_id(
                session,
                identity.subject_id,
            )
            if identity.actor_user_id != owner_user_id:
                raise engine.ConflictPreparedWriteError(
                    "manual body scans require the subject owner actor"
                )
        if source == Source.MCP.value and any(
            value is not None for value in (file_key, file_asset_id)
        ):
            raise engine.ConflictRawOwnershipError(
                "structured MCP body scans require null file provenance"
            )
        if source in {Source.MCP.value, Source.BODY_SCAN.value} and raw_payload_id is None:
            raise engine.ConflictRawOwnershipError(
                f"scoped {source} body scans require durable raw provenance"
            )

    owned_raw: RawPayload | None = None
    owned_asset: FileAsset | None = None
    authoritative_file_key = file_key
    authoritative_file_asset_id = file_asset_id
    if weight_context is not None and raw_payload_id is not None:
        expected_raw_source = (
            Source.MCP.value if source == Source.MCP.value else Source.BODY_SCAN.value
        )
        owned_raw, owned_asset = await _lock_owned_raw(
            session,
            raw_payload_id=raw_payload_id,
            context=weight_context,
            expected_source=expected_raw_source,
            require_boundary_actor=expected_raw_source == Source.BODY_SCAN.value,
            file_key=file_key,
            allow_historical_parser_raw=allow_historical_parser_raw,
        )
        if file_asset_id is not None and (
            owned_asset is None or file_asset_id != owned_asset.id
        ):
            raise ValueError("file_asset_id does not match the owned upload")
        authoritative_file_key = owned_asset.storage_ref if owned_asset is not None else None
        authoritative_file_asset_id = owned_asset.id if owned_asset is not None else None
        existing_scan_id = await session.scalar(
            select(BodyScan.id)
            .where(BodyScan.raw_payload_id == owned_raw.id)
            .order_by(BodyScan.id)
            .limit(1)
            .with_for_update()
        )
        if owned_raw.processed_at is not None or existing_scan_id is not None:
            raise BodyScanRawAlreadyNormalizedError(
                "body-scan raw payload is already normalized"
            )
    elif weight_context is not None and any(
        value is not None for value in (file_key, file_asset_id)
    ):
        raise ValueError("owned scan file references require a raw upload")

    # Authorization precedes the conflict engine because a rejected client id
    # must not be able to trigger even an alert side effect in this transaction.
    proposed = {"scan": True, "source": source}
    conflict_entity_ref = _create_conflict_entity_ref(
        owned_raw.id if owned_raw is not None else raw_payload_id
    )
    await engine.enforce_prepared(
        session,
        prepared=prepared_weight_write.conflict_write,
        domain=Domain.BODY_COMPOSITION,
        proposed_state=proposed,
        override=override,
        entity_ref=conflict_entity_ref,
    )

    scan = BodyScan(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        file_asset_id=authoritative_file_asset_id,
        date=on_date,
        domain=DOMAIN,
        source=source,
        device=(device or None),
        file_key=authoritative_file_key,
        raw_payload_id=owned_raw.id if owned_raw is not None else raw_payload_id,
        note=note,
    )
    session.add(scan)
    await session.flush()

    normalized = [n for n in (_normalize_item(m) for m in metrics) if n is not None]
    for n in normalized:
        session.add(
            BodyScanMetric(
                scan_id=scan.id,
                subject_id=identity.subject_id if identity is not None else None,
                **n,
            )
        )
    await session.flush()

    # Mark the verbatim vision payload processed (it stays unchanged — the owner's
    # edits live only in the normalized rows, so the original extraction is an
    # audit trail we can always re-parse).
    if raw_payload_id is not None:
        # The fallback is scoped: an id alone proves nothing about who a
        # payload belongs to, and stamping ``processed_at`` on somebody else's
        # is a write across the boundary rather than a read past it.
        raw = owned_raw or await session.scalar(
            select(RawPayload).where(
                RawPayload.id == raw_payload_id,
                RawPayload.subject_id == identity.subject_id,
            )
        )
        if raw is not None:
            raw.processed_at = now_local()

    # Bridge the scan's weight into the weight domain as the BODY_SCAN source so it
    # appears on the weight trend. Priority: manual ≈ scan > Garmin (Garmin never
    # supersedes a scan). Enforced by the Weight write policy.
    w = weight_from_scan(normalized)
    if w is not None and w > 0:
        origin_actor_user_id = (
            owned_raw.actor_user_id
            if owned_raw is not None
            else (identity.actor_user_id if identity is not None else None)
        )
        await weight_writes.project_body_scan_weight(
            session,
            command=weight_writes.BodyScanWeightCommand(
                on_date=on_date,
                weight_kg=w,
                integration_connection_id=(
                    owned_raw.integration_connection_id
                    if owned_raw is not None
                    else None
                ),
                raw_payload_id=(owned_raw.id if owned_raw is not None else None),
                origin_actor_user_id=origin_actor_user_id,
                override=override,
                allow_historical_parser_raw=allow_historical_parser_raw,
            ),
            identity=identity,
            prepared_weight_write=prepared_weight_write,
        )
    await session.flush()
    return scan


async def ingest_structured_scan(
    session: AsyncSession,
    extracted: dict,
    *,
    raw_payload: RawPayload,
    identity: WriteIdentity,
    prepared_weight_write: PreparedWeightWrite,
    override: bool = False,
) -> BodyScan:
    """Persist an MCP-authored scan with immutable raw-first provenance."""

    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    assert context is not None
    on_date = _parse_date(extracted.get("date")) or context.evaluation_date
    _require_evaluation_date(context, on_date)
    if not isinstance(raw_payload, RawPayload) or raw_payload.id is None:
        raise engine.ConflictRawOwnershipError(
            "structured MCP body scans require a persisted raw payload"
        )
    raw, _ = await _lock_owned_raw(
        session,
        raw_payload_id=raw_payload.id,
        context=context,
        expected_source=Source.MCP.value,
        require_boundary_actor=False,
    )
    scan = await save_scan(
        session,
        on_date=on_date,
        device=extracted.get("device"),
        raw_payload_id=raw.id,
        metrics=normalize_extracted(extracted),
        note=extracted.get("note"),
        source=Source.MCP.value,
        override=override,
        identity=identity,
        prepared_weight_write=prepared_weight_write,
    )
    raw.processed_at = now_local()
    await session.flush()
    return scan
