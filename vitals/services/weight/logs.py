"""Weight-log ownership, provenance validation, and scoped persistence reads."""
from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import and_, exists, or_, select
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
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset
from vitals.models.weight import DOMAIN, WeightLog
from vitals.ownership import WriteIdentity
from vitals.services.files import contracts as file_contracts
from vitals.services.conflicts import engine
from vitals.utils.timeutils import today_local

from .contracts import WeightOwnershipError, _ORIGIN_ACTOR_UNSET

_WEIGHT_OWNERSHIP_CHECKPOINT_PHASE = (
    "stage3.channel_optional.weight_logs.v1.weight_logs"
)
_WEIGHT_HISTORY_CACHE_KEY = "weight_ownership_historical_high_watermarks"

_SOURCE_PRIORITY: dict[str, int] = {
    Source.MANUAL.value: 2,
    Source.MCP.value: 2,
    Source.BODY_SCAN.value: 2,
}


def _source_priority(source: str) -> int:
    """Priority of a weight source for the one-active-per-date invariant."""
    return _SOURCE_PRIORITY.get(source, 1)


def _historical_provider_raw_scope(subject_id: uuid.UUID):
    """Exact post-backfill raw roots for a still-unowned historical Weight."""

    from vitals.models.identity import HealthSubject
    from vitals.models.tenancy import IntegrationConnection

    historical_statuses = tuple(
        status.value
        for status in IntegrationConnectionStatus
        if status is not IntegrationConnectionStatus.PENDING
    )
    exact_one_subject = and_(
        exists(select(1).where(HealthSubject.id == subject_id)),
        ~exists(select(1).where(HealthSubject.id != subject_id)),
    )
    matching_connection = exists(
        select(1).where(
            IntegrationConnection.id == RawPayload.integration_connection_id,
            IntegrationConnection.subject_id == subject_id,
            IntegrationConnection.status.in_(historical_statuses),
            or_(
                and_(
                    WeightLog.source == Source.GARMIN_API.value,
                    RawPayload.domain == Domain.GARMIN.value,
                    RawPayload.source == Source.GARMIN_API.value,
                    IntegrationConnection.provider
                    == IntegrationProvider.GARMIN.value,
                    IntegrationConnection.connection_type
                    == IntegrationConnectionType.ACCOUNT.value,
                ),
                and_(
                    WeightLog.source == Source.BODY_SCAN.value,
                    RawPayload.domain == Domain.BODY_COMPOSITION.value,
                    RawPayload.source == Source.BODY_SCAN.value,
                    IntegrationConnection.provider
                    == IntegrationProvider.OPENROUTER.value,
                    IntegrationConnection.connection_type
                    == IntegrationConnectionType.AI_GATEWAY.value,
                ),
            ),
        ).correlate(RawPayload, WeightLog)
    )
    body_has_no_invocation = or_(
        WeightLog.source != Source.BODY_SCAN.value,
        ~exists(
            select(1)
            .where(AIInvocation.raw_payload_id == RawPayload.id)
            .correlate(RawPayload)
        ),
    )
    provider_history = and_(
        RawPayload.integration_connection_id.is_not(None),
        matching_connection,
    )
    body_mcp_history = and_(
        WeightLog.source == Source.BODY_SCAN.value,
        RawPayload.domain == Domain.BODY_COMPOSITION.value,
        RawPayload.source == Source.MCP.value,
        RawPayload.integration_connection_id.is_(None),
    )
    return and_(
        RawPayload.subject_id == subject_id,
        RawPayload.actor_user_id.is_(None),
        RawPayload.file_asset_id.is_(None),
        exact_one_subject,
        or_(provider_history, body_mcp_history),
        body_has_no_invocation,
    )


def _weight_scope_condition(
    *,
    subject_id: uuid.UUID,
    evaluation_date: date_type,
):
    from vitals.models.identity import HealthSubject
    from vitals.models.tenancy import IntegrationConnection

    owner_user_id = (
        select(HealthSubject.owner_user_id)
        .where(HealthSubject.id == subject_id)
        .scalar_subquery()
    )
    historical_statuses = tuple(
        status.value
        for status in IntegrationConnectionStatus
        if status is not IntegrationConnectionStatus.PENDING
    )
    raw_scope = engine.ConflictScope(
        subject_id=subject_id,
        evaluation_date=evaluation_date,
    )
    exact_raw, fully_unowned_raw = engine.raw_payload_scope_conditions(raw_scope)
    exact_fact_raw = or_(exact_raw, fully_unowned_raw)
    return and_(
        WeightLog.subject_id == subject_id,
        or_(
            WeightLog.actor_user_id.is_(None),
            WeightLog.actor_user_id == owner_user_id,
        ),
        or_(
            WeightLog.integration_connection_id.is_(None),
            exists(
                select(1).where(
                    IntegrationConnection.id == WeightLog.integration_connection_id,
                    IntegrationConnection.subject_id == subject_id,
                    IntegrationConnection.status.in_(historical_statuses),
                )
            ),
        ),
        or_(
            WeightLog.raw_payload_id.is_(None),
            exists(
                select(1).where(
                    RawPayload.id == WeightLog.raw_payload_id,
                    exact_fact_raw,
                )
            ),
        ),
    )


async def _assert_weight_scope_integrity(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    evaluation_date: date_type,
    filters: Sequence = (),
) -> None:
    """Reject partial roots instead of silently treating them as absent."""

    raw_scope = engine.ConflictScope(
        subject_id=subject_id,
        evaluation_date=evaluation_date,
        legacy_bridge=engine.LegacyConflictBridge.FULLY_UNOWNED,
    )
    exact_raw, fully_unowned_raw = engine.raw_payload_scope_conditions(raw_scope)
    historical_provider_raw = _historical_provider_raw_scope(subject_id)
    exact_fact_raw = or_(exact_raw, fully_unowned_raw)
    invalid_raw = await session.scalar(
        select(WeightLog.id)
        .where(
            WeightLog.subject_id == subject_id,
            *filters,
            WeightLog.raw_payload_id.is_not(None),
            or_(
                and_(
                    WeightLog.subject_id == subject_id,
                    exists(
                        select(1).where(
                            RawPayload.id == WeightLog.raw_payload_id,
                            exact_fact_raw,
                        )
                    ),
                ),
                and_(
                    WeightLog.subject_id.is_(None),
                    exists(
                        select(1).where(
                            RawPayload.id == WeightLog.raw_payload_id,
                            or_(fully_unowned_raw, historical_provider_raw),
                        )
                    ),
                ),
            ).is_not(True),
        )
        .limit(1)
    )
    if invalid_raw is not None:
        raise engine.ConflictRawOwnershipError(
            "weight fact links to raw provenance outside its subject scope"
        )

    structurally_valid_scope = _weight_scope_condition(
        subject_id=subject_id,
        evaluation_date=evaluation_date,
    )
    invalid = await session.scalar(
        select(WeightLog.id)
        .where(
            WeightLog.subject_id == subject_id,
            *filters,
            structurally_valid_scope.is_not(True),
        )
        .limit(1)
    )
    if invalid is not None:
        raise WeightOwnershipError(
            "weight fact has partial or conflicting ownership provenance"
        )


async def get_active_weight(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
    for_update: bool = False,
) -> Optional[WeightLog]:
    stmt = select(WeightLog).where(
        WeightLog.date == on_date,
        WeightLog.superseded.is_(False),
    )
    scope = _weight_scope_condition(
        subject_id=subject_id,
        evaluation_date=on_date,
    )
    await _assert_weight_scope_integrity(
        session,
        subject_id=subject_id,
        evaluation_date=on_date,
        filters=(
            WeightLog.date == on_date,
            WeightLog.superseded.is_(False),
        ),
    )
    stmt = stmt.where(scope)
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt.execution_options(populate_existing=True))
    row = result.scalar_one_or_none()
    if row is not None:
        await _validate_persisted_weight_provenance(
            session,
            row,
            subject_id=subject_id,
        )
    return row


async def _validate_body_scan_ai_origin(
    session: AsyncSession,
    *,
    raw: RawPayload,
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    for_update: bool,
    require_live_file: bool,
) -> None:
    """Prove the mutually exclusive historical-C/platform-AI raw lineage."""

    if all(
        root is None
        for root in (
            raw.subject_id,
            raw.actor_user_id,
            raw.integration_connection_id,
            raw.file_asset_id,
        )
    ):
        return
    if raw.file_asset_id is None:
        raise engine.ConflictRawOwnershipError(
            "body-scan Weight raw has no document provenance"
        )
    asset_stmt = select(FileAsset).where(FileAsset.id == raw.file_asset_id)
    if for_update:
        asset_stmt = asset_stmt.with_for_update()
    asset = await session.scalar(
        asset_stmt.execution_options(populate_existing=True)
    )
    live_file = asset is not None and file_contracts.local_asset_is_live(asset)
    retired_file = (
        asset is not None and file_contracts.local_asset_is_retired(asset)
    )
    if (
        asset is None
        or asset.subject_id != subject_id
        or asset.uploaded_by_user_id != actor_user_id
        or asset.purpose != FileAssetPurpose.BODY_SCAN_DOCUMENT.value
        or (not live_file and (require_live_file or not retired_file))
        or raw.external_id != asset.storage_ref
    ):
        raise engine.ConflictRawOwnershipError(
            "body-scan Weight document provenance is invalid"
        )
    stmt = select(AIInvocation).where(
        AIInvocation.raw_payload_id == raw.id,
        AIInvocation.purpose == AIInvocationPurpose.BODY_SCAN_PARSE.value,
    ).order_by(AIInvocation.created_at, AIInvocation.id)
    if for_update:
        stmt = stmt.with_for_update()
    invocations = list(
        await session.scalars(stmt.execution_options(populate_existing=True))
    )
    if raw.integration_connection_id is not None:
        if invocations:
            raise engine.ConflictRawOwnershipError(
                "body-scan Weight mixes subject and platform parser provenance"
            )
        return
    if len(invocations) != 1:
        raise engine.ConflictRawOwnershipError(
            "platform body-scan Weight requires one parser invocation"
        )
    invocation = invocations[0]
    if (
        invocation.subject_id != subject_id
        or invocation.actor_user_id != actor_user_id
        or invocation.raw_payload_id != raw.id
        or invocation.source != AIInvocationSource.WEB.value
        or invocation.status != AIInvocationStatus.SUCCEEDED.value
    ):
        raise engine.ConflictRawOwnershipError(
            "platform body-scan Weight parser provenance is invalid"
        )


async def _validate_historical_provider_raw(
    session: AsyncSession,
    *,
    raw: RawPayload,
    subject_id: uuid.UUID,
    fact_source: str,
    for_update: bool,
) -> bool:
    """Prove one exact Stage-3A raw shape without adopting its C or actor."""

    from vitals.models.identity import HealthSubject
    from vitals.models.tenancy import IntegrationConnection

    if not (
        raw.subject_id == subject_id
        and raw.actor_user_id is None
        and raw.file_asset_id is None
    ):
        return False
    if fact_source == Source.GARMIN_API.value:
        expected_domain = Domain.GARMIN.value
        expected_raw_source = Source.GARMIN_API.value
        expected_provider = IntegrationProvider.GARMIN.value
        expected_type = IntegrationConnectionType.ACCOUNT.value
    elif fact_source == Source.BODY_SCAN.value and raw.source == Source.BODY_SCAN.value:
        expected_domain = Domain.BODY_COMPOSITION.value
        expected_raw_source = Source.BODY_SCAN.value
        expected_provider = IntegrationProvider.OPENROUTER.value
        expected_type = IntegrationConnectionType.AI_GATEWAY.value
    elif (
        fact_source == Source.BODY_SCAN.value
        and raw.domain == Domain.BODY_COMPOSITION.value
        and raw.source == Source.MCP.value
        and raw.integration_connection_id is None
    ):
        expected_domain = Domain.BODY_COMPOSITION.value
        expected_raw_source = Source.MCP.value
        expected_provider = None
        expected_type = None
    else:
        return False
    if raw.domain != expected_domain or raw.source != expected_raw_source:
        return False
    subject_ids = list(
        await session.scalars(
            select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
        )
    )
    if subject_ids != [subject_id]:
        raise engine.ConflictRawOwnershipError(
            "historical Weight raw requires exactly one subject"
        )
    if expected_provider is None:
        if raw.integration_connection_id is not None:
            raise engine.ConflictRawOwnershipError(
                "historical Weight connectionless raw claims a provider"
            )
    else:
        if raw.integration_connection_id is None:
            return False
        historical_statuses = tuple(
            status.value
            for status in IntegrationConnectionStatus
            if status is not IntegrationConnectionStatus.PENDING
        )
        connection_stmt = select(IntegrationConnection).where(
            IntegrationConnection.id == raw.integration_connection_id,
            IntegrationConnection.subject_id == subject_id,
            IntegrationConnection.provider == expected_provider,
            IntegrationConnection.connection_type == expected_type,
            IntegrationConnection.status.in_(historical_statuses),
        )
        if for_update:
            connection_stmt = connection_stmt.with_for_update()
        connection = await session.scalar(
            connection_stmt.execution_options(populate_existing=True)
        )
        if connection is None:
            raise engine.ConflictRawOwnershipError(
                "historical Weight provider connection is invalid"
            )
    if fact_source == Source.BODY_SCAN.value:
        invocation_stmt = (
            select(AIInvocation.id)
            .where(AIInvocation.raw_payload_id == raw.id)
            .order_by(AIInvocation.created_at, AIInvocation.id)
            .limit(1)
        )
        if for_update:
            invocation_stmt = invocation_stmt.with_for_update()
        if await session.scalar(invocation_stmt) is not None:
            raise engine.ConflictRawOwnershipError(
                "historical body-scan Weight mixes subject and platform AI provenance"
            )
    return True


async def _reject_body_scan_ai_invocation(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    for_update: bool,
) -> None:
    stmt = (
        select(AIInvocation.id)
        .where(
            AIInvocation.raw_payload_id == raw_payload_id,
            AIInvocation.purpose == AIInvocationPurpose.BODY_SCAN_PARSE.value,
        )
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    if await session.scalar(stmt) is not None:
        raise engine.ConflictRawOwnershipError(
            "MCP body-composition raw cannot claim an AI parser invocation"
        )


async def _validate_new_weight_provenance(
    session: AsyncSession,
    *,
    context: engine.ConflictWriteContext,
    source: str,
    integration_connection_id: uuid.UUID | None,
    raw_payload_id: int | None,
    origin_actor_user_id: uuid.UUID | None | object,
    allow_historical_parser_raw: bool,
) -> None:
    from vitals.models.tenancy import IntegrationConnection

    scope = context.scope
    exact_raw, fully_unowned_raw = engine.raw_payload_scope_conditions(scope)
    connection = None
    if integration_connection_id is not None:
        statuses = tuple(
            status.value
            for status in IntegrationConnectionStatus
            if status is not IntegrationConnectionStatus.PENDING
        )
        connection = await session.scalar(
            select(IntegrationConnection)
            .where(
                IntegrationConnection.id == integration_connection_id,
                IntegrationConnection.subject_id == context.identity.subject_id,
                IntegrationConnection.status.in_(statuses),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if connection is None:
            raise WeightOwnershipError(
                "weight origin connection is outside the prepared subject"
            )
    if source in {Source.MANUAL.value, Source.MCP.value}:
        if integration_connection_id is not None or raw_payload_id is not None:
            raise WeightOwnershipError(
                "manual and MCP weight facts cannot claim provider provenance"
            )
    elif source == Source.GARMIN_API.value:
        if (
            connection is None
            or connection.provider != IntegrationProvider.GARMIN.value
            or connection.connection_type != IntegrationConnectionType.ACCOUNT.value
        ):
            raise WeightOwnershipError(
                "Garmin weight facts require a Garmin account connection"
            )
        if raw_payload_id is None:
            raise engine.ConflictRawOwnershipError(
                "Garmin weight facts require durable raw provenance"
            )
    elif source == Source.BODY_SCAN.value and connection is not None:
        if (
            connection.provider != IntegrationProvider.OPENROUTER.value
            or connection.connection_type
            != IntegrationConnectionType.AI_GATEWAY.value
        ):
            raise WeightOwnershipError(
                "body-scan weight provenance requires an OpenRouter AI connection"
            )
        if raw_payload_id is None:
            raise engine.ConflictRawOwnershipError(
                "provider-backed body-scan weight requires durable raw provenance"
            )
    elif source not in {Source.BODY_SCAN.value}:
        raise WeightOwnershipError("unsupported scoped weight provenance source")

    requested_actor = (
        context.identity.actor_user_id
        if origin_actor_user_id is _ORIGIN_ACTOR_UNSET
        else origin_actor_user_id
    )
    if raw_payload_id is None:
        if requested_actor not in {None, context.identity.actor_user_id}:
            raise WeightOwnershipError(
                "weight actor is not authorized by the prepared writer"
            )
        return
    raw_allowed = exact_raw
    if context.legacy_bridge is engine.LegacyConflictBridge.FULLY_UNOWNED:
        raw_allowed = or_(raw_allowed, fully_unowned_raw)
    raw = await session.scalar(
        select(RawPayload)
        .where(RawPayload.id == raw_payload_id, raw_allowed)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if raw is None:
        raise engine.ConflictRawOwnershipError(
            "weight raw payload is outside the prepared subject"
        )
    if raw.integration_connection_id != integration_connection_id:
        raise engine.ConflictRawOwnershipError(
            "weight raw payload belongs to a different origin connection"
        )
    if raw.actor_user_id != requested_actor:
        raise engine.ConflictRawOwnershipError(
            "weight actor does not match durable raw provenance"
        )
    allowed_raw_sources = {source}
    if source == Source.BODY_SCAN.value:
        allowed_raw_sources.add(Source.MCP.value)
    if raw.source not in allowed_raw_sources:
        raise engine.ConflictRawOwnershipError(
            "weight source does not match durable raw provenance"
        )
    if source == Source.BODY_SCAN.value and raw.source == Source.MCP.value and (
        raw.actor_user_id is None
        or integration_connection_id is not None
        or raw.file_asset_id is not None
    ):
        raise engine.ConflictRawOwnershipError(
            "MCP body-composition lineage must have null connection and file roots"
        )
    if source == Source.BODY_SCAN.value and raw.source == Source.MCP.value:
        await _reject_body_scan_ai_invocation(
            session,
            raw_payload_id=raw.id,
            for_update=True,
        )
    expected_raw_domain = (
        Domain.GARMIN.value
        if source == Source.GARMIN_API.value
        else Domain.BODY_COMPOSITION.value
    )
    if raw.domain != expected_raw_domain:
        raise engine.ConflictRawOwnershipError(
            "weight raw payload belongs to a different domain"
        )
    if (
        allow_historical_parser_raw
        and context.scope.include_legacy_unowned
        and source == Source.BODY_SCAN.value
        and raw.source == Source.BODY_SCAN.value
        and requested_actor is None
        and await _validate_historical_provider_raw(
            session,
            raw=raw,
            subject_id=context.identity.subject_id,
            fact_source=source,
            for_update=True,
        )
    ):
        return
    if source == Source.BODY_SCAN.value and raw.source == Source.BODY_SCAN.value:
        await _validate_body_scan_ai_origin(
            session,
            raw=raw,
            subject_id=context.identity.subject_id,
            actor_user_id=requested_actor,
            for_update=True,
            require_live_file=True,
        )


async def _validate_persisted_weight_provenance(
    session: AsyncSession,
    row: WeightLog,
    *,
    subject_id: uuid.UUID,
) -> None:
    """Validate the durable source -> C/raw chain for one scoped Weight fact."""

    from vitals.models.tenancy import IntegrationConnection

    cached_watermarks = session.info.setdefault(_WEIGHT_HISTORY_CACHE_KEY, {})
    if subject_id not in cached_watermarks:
        cached_watermarks[subject_id] = int(
            await session.scalar(
                select(OwnershipBackfillCheckpoint.scan_high_watermark_id).where(
                    OwnershipBackfillCheckpoint.phase_key
                    == _WEIGHT_OWNERSHIP_CHECKPOINT_PHASE,
                    OwnershipBackfillCheckpoint.subject_id == subject_id,
                    OwnershipBackfillCheckpoint.status == "completed",
                )
            )
            or 0
        )
    historical = row.id <= cached_watermarks[subject_id]

    if row.subject_id != subject_id:
        raise WeightOwnershipError("weight fact belongs to another subject")
    if row.domain != DOMAIN:
        raise WeightOwnershipError("weight fact has an invalid domain")

    connection = None
    if row.integration_connection_id is not None:
        connection = await session.scalar(
            select(IntegrationConnection)
            .where(IntegrationConnection.id == row.integration_connection_id)
            .execution_options(populate_existing=True)
        )
        historical_statuses = {
            status.value
            for status in IntegrationConnectionStatus
            if status is not IntegrationConnectionStatus.PENDING
        }
        if (
            connection is None
            or connection.subject_id != subject_id
            or connection.status not in historical_statuses
        ):
            raise WeightOwnershipError(
                "weight fact references an invalid subject connection"
            )

    raw = None
    if row.raw_payload_id is not None:
        raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == row.raw_payload_id)
            .execution_options(populate_existing=True)
        )
        if raw is None:
            raise engine.ConflictRawOwnershipError(
                "weight fact references a missing raw payload"
            )
        historical_provider_raw = await _validate_historical_provider_raw(
            session,
            raw=raw,
            subject_id=subject_id,
            fact_source=row.source,
            for_update=False,
        )
        if historical_provider_raw:
            if (
                row.actor_user_id is not None
                or row.integration_connection_id
                not in {None, raw.integration_connection_id}
            ):
                raise engine.ConflictRawOwnershipError(
                    "historical Weight fact has conflicting normalized roots"
                )
            return
        raw_is_fully_unowned = all(
            root is None
            for root in (
                raw.subject_id,
                raw.actor_user_id,
                raw.integration_connection_id,
                raw.file_asset_id,
            )
        )
        if raw.subject_id != subject_id and not raw_is_fully_unowned:
            raise engine.ConflictRawOwnershipError(
                "weight fact links to raw provenance outside its subject scope"
            )
        if raw.actor_user_id != row.actor_user_id:
            raise engine.ConflictRawOwnershipError(
                "weight actor does not match durable raw provenance"
            )
        if raw.integration_connection_id != row.integration_connection_id:
            raise engine.ConflictRawOwnershipError(
                "weight connection does not match durable raw provenance"
            )

    if row.source in {Source.MANUAL.value, Source.MCP.value}:
        if connection is not None:
            raise WeightOwnershipError(
                "manual and MCP weight facts cannot claim provider provenance"
            )
        if raw is not None and (
            raw.domain != Domain.WEIGHT.value
            or raw.source != row.source
            or raw.file_asset_id is not None
        ):
            raise engine.ConflictRawOwnershipError(
                "manual or MCP weight raw provenance is incompatible"
            )
        return

    if row.source == Source.GARMIN_API.value:
        if historical and connection is None and raw is None:
            return
        if (
            connection is None
            or connection.provider != IntegrationProvider.GARMIN.value
            or connection.connection_type != IntegrationConnectionType.ACCOUNT.value
        ):
            raise WeightOwnershipError(
                "Garmin weight fact has invalid connection provenance"
            )
        if raw is None:
            raise engine.ConflictRawOwnershipError(
                "Garmin weight fact has no durable raw provenance"
            )
        if (
            raw.domain != Domain.GARMIN.value
            or raw.source != Source.GARMIN_API.value
            or raw.file_asset_id is not None
        ):
            raise engine.ConflictRawOwnershipError(
                "Garmin weight raw provenance is incompatible"
            )
        return

    if row.source == Source.BODY_SCAN.value:
        if connection is not None and (
            connection.provider != IntegrationProvider.OPENROUTER.value
            or connection.connection_type
            != IntegrationConnectionType.AI_GATEWAY.value
        ):
            raise WeightOwnershipError(
                "body-scan weight has invalid connection provenance"
            )
        if connection is not None and raw is None:
            raise engine.ConflictRawOwnershipError(
                "provider-backed body-scan weight has no durable raw provenance"
            )
        if raw is not None and (
            raw.domain != Domain.BODY_COMPOSITION.value
            or raw.source not in {Source.BODY_SCAN.value, Source.MCP.value}
            or (
                raw.source == Source.MCP.value
                and (
                    raw.actor_user_id is None
                    or connection is not None
                    or raw.integration_connection_id is not None
                    or raw.file_asset_id is not None
                )
            )
        ):
            raise engine.ConflictRawOwnershipError(
                "body-scan weight raw provenance is incompatible"
            )
        if raw is not None and raw.source == Source.BODY_SCAN.value:
            await _validate_body_scan_ai_origin(
                session,
                raw=raw,
                subject_id=subject_id,
                actor_user_id=row.actor_user_id,
                for_update=False,
                require_live_file=False,
            )
        elif raw is not None and raw.source == Source.MCP.value:
            await _reject_body_scan_ai_invocation(
                session,
                raw_payload_id=raw.id,
                for_update=False,
            )
        return

    raise WeightOwnershipError("weight fact has unsupported provenance source")


def _weight_entity_key(row: WeightLog) -> str:
    return f"weight:{row.id}"


def _weight_provenance_is_reusable(
    row: WeightLog,
    *,
    identity: WriteIdentity | None,
    integration_connection_id: uuid.UUID | None,
    raw_payload_id: int | None,
) -> bool:
    if identity is None:
        return True
    if integration_connection_id is not None or raw_payload_id is not None:
        return (
            row.subject_id == identity.subject_id
            and row.integration_connection_id == integration_connection_id
            and row.raw_payload_id == raw_payload_id
        )
    return (
        row.subject_id in {None, identity.subject_id}
        and row.integration_connection_id is None
        and row.raw_payload_id is None
    )


async def _get_weight_log_for_update(
    session: AsyncSession,
    weight_id: int,
    *,
    subject_id: uuid.UUID,
    evaluation_date: date_type,
) -> WeightLog | None:
    stmt = select(WeightLog).where(WeightLog.id == weight_id)
    scope = _weight_scope_condition(
        subject_id=subject_id,
        evaluation_date=evaluation_date,
    )
    invalid = await session.scalar(
        select(WeightLog.id)
        .where(
            WeightLog.id == weight_id,
            WeightLog.subject_id == subject_id,
            scope.is_not(True),
        )
        .limit(1)
    )
    if invalid is not None:
        raise WeightOwnershipError(
            "weight fact has partial or conflicting ownership provenance"
        )
    stmt = stmt.where(scope)
    row = await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )
    if row is not None:
        await _validate_persisted_weight_provenance(
            session,
            row,
            subject_id=subject_id,
        )
    return row


async def _get_weight_log_date_in_scope(
    session: AsyncSession,
    weight_id: int,
    *,
    subject_id: uuid.UUID,
    evaluation_date: date_type,
) -> date_type | None:
    """Read only the target date after the caller has locked its subject roots."""

    scope = _weight_scope_condition(
        subject_id=subject_id,
        evaluation_date=evaluation_date,
    )
    await _assert_weight_scope_integrity(
        session,
        subject_id=subject_id,
        evaluation_date=evaluation_date,
        filters=(WeightLog.id == weight_id,),
    )
    return await session.scalar(
        select(WeightLog.date).where(WeightLog.id == weight_id, scope)
    )


async def list_active_weights(
    session: AsyncSession,
    *,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    subject_id: uuid.UUID,
) -> Sequence[WeightLog]:
    stmt = select(WeightLog).where(WeightLog.superseded.is_(False))
    date_filters = []
    if start is not None:
        date_filters.append(WeightLog.date >= start)
    if end is not None:
        date_filters.append(WeightLog.date <= end)
    scope = _weight_scope_condition(
        subject_id=subject_id,
        evaluation_date=end or start or today_local(),
    )
    await _assert_weight_scope_integrity(
        session,
        subject_id=subject_id,
        evaluation_date=end or start or today_local(),
        filters=(WeightLog.superseded.is_(False), *date_filters),
    )
    stmt = stmt.where(scope)
    if date_filters:
        stmt = stmt.where(*date_filters)
    stmt = stmt.order_by(WeightLog.date)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    if subject_id is not None:
        for row in rows:
            await _validate_persisted_weight_provenance(
                session,
                row,
                subject_id=subject_id,
            )
    return rows
