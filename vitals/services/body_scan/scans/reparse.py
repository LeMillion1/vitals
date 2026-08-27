"""Idempotent replay of owned body-scan raw payloads."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Source,
)
from vitals.models.ai import AIInvocation
from vitals.models.body_scan import DOMAIN, BodyScan
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services import raw_payload_service
from vitals.services.conflicts import engine
from vitals.services.weight import governance as weight_governance
from vitals.utils.timeutils import now_local, today_local

from .alerts import refresh_alerts
from .contracts import (
    BodyScanOwnershipError,
    require_evaluation_date as _require_evaluation_date,
)
from .ingestion import _lock_owned_raw, save_scan
from .normalization import _parse_date, normalize_extracted
from .queries import _validate_persisted_scan

logger = logging.getLogger(__name__)

async def reparse_owned_pending(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    limit: int = raw_payload_service.REPARSE_BATCH,
    since_days: int = raw_payload_service.REPARSE_WINDOW_DAYS,
) -> int:
    """Replay pending upload raws in isolated per-raw savepoints."""

    if not isinstance(identity, WriteIdentity):
        raise engine.ConflictPreparedWriteError(
            "owned body-scan replay requires a WriteIdentity"
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if (
        not isinstance(since_days, int)
        or isinstance(since_days, bool)
        or since_days < 0
    ):
        raise ValueError("since_days must be a non-negative integer")

    raw_scope = RawPayload.subject_id == identity.subject_id
    raw_scope = or_(
        raw_scope,
        and_(
            RawPayload.subject_id.is_(None),
            RawPayload.actor_user_id.is_(None),
            RawPayload.integration_connection_id.is_(None),
            RawPayload.file_asset_id.is_(None),
        ),
    )
    cutoff = now_local() - timedelta(days=since_days)
    linked_scans = list(
        await session.scalars(
            select(BodyScan)
            .join(RawPayload, BodyScan.raw_payload_id == RawPayload.id)
            .where(
                raw_scope,
                RawPayload.domain == DOMAIN,
                RawPayload.source == Source.BODY_SCAN.value,
                RawPayload.processed_at.is_(None),
                RawPayload.fetched_at >= cutoff,
            )
            .options(selectinload(BodyScan.metrics))
            .order_by(BodyScan.id)
        )
    )
    for linked_scan in linked_scans:
        try:
            await _validate_persisted_scan(
                session,
                linked_scan,
                subject_id=identity.subject_id,
            )
        except (
            BodyScanOwnershipError,
            engine.ConflictRawOwnershipError,
        ) as exc:
            raise engine.ConflictRawOwnershipError(
                "pending body-scan raw links to foreign or partial normalized "
                "provenance"
            ) from exc
    has_normalized = (
        select(BodyScan.id).where(BodyScan.raw_payload_id == RawPayload.id).exists()
    )
    succeeded_platform_parse = (
        select(AIInvocation.id)
        .where(
            AIInvocation.subject_id == identity.subject_id,
            AIInvocation.actor_user_id == RawPayload.actor_user_id,
            AIInvocation.raw_payload_id == RawPayload.id,
            AIInvocation.purpose == AIInvocationPurpose.BODY_SCAN_PARSE.value,
            AIInvocation.source == AIInvocationSource.WEB.value,
            AIInvocation.status == AIInvocationStatus.SUCCEEDED.value,
        )
        .correlate(RawPayload)
        .exists()
    )
    eligible_parser_provenance = or_(
        RawPayload.integration_connection_id.is_not(None),
        and_(
            RawPayload.subject_id == identity.subject_id,
            RawPayload.actor_user_id.is_not(None),
            RawPayload.integration_connection_id.is_(None),
            RawPayload.file_asset_id.is_not(None),
            succeeded_platform_parse,
        ),
    )
    eligible_parser_provenance = or_(
        eligible_parser_provenance,
        and_(
            RawPayload.subject_id.is_(None),
            RawPayload.actor_user_id.is_(None),
            RawPayload.integration_connection_id.is_(None),
            RawPayload.file_asset_id.is_(None),
        ),
    )
    done = 0
    last_raw_id = 0
    while done < limit:
        raw_ids = list(
            await session.scalars(
                select(RawPayload.id)
                .where(
                    raw_scope,
                    RawPayload.id > last_raw_id,
                    RawPayload.domain == DOMAIN,
                    RawPayload.source == Source.BODY_SCAN.value,
                    RawPayload.processed_at.is_(None),
                    RawPayload.fetched_at >= cutoff,
                    eligible_parser_provenance,
                    ~has_normalized,
                )
                .order_by(RawPayload.id)
                .limit(limit)
            )
        )
        if not raw_ids:
            break
        last_raw_id = raw_ids[-1]
        for raw_id in raw_ids:
            try:
                async with session.begin_nested():
                    # Probe without a row lock; Weight preparation must retain
                    # governance -> Garmin advisory -> S/A before raw/F/parser.
                    probe = await session.scalar(
                        select(RawPayload)
                        .where(RawPayload.id == raw_id)
                        .execution_options(populate_existing=True)
                    )
                    if probe is None:
                        continue
                    extracted = (
                        probe.payload if isinstance(probe.payload, dict) else {}
                    )
                    on_date = _parse_date(extracted.get("date")) or today_local()
                    is_legacy = probe.subject_id is None
                    is_historical_parser = (
                        probe.subject_id == identity.subject_id
                        and probe.actor_user_id is None
                        and probe.integration_connection_id is not None
                        and probe.file_asset_id is None
                        and probe.domain == DOMAIN
                        and probe.source == Source.BODY_SCAN.value
                    )
                    origin_identity = WriteIdentity(
                        identity.subject_id,
                        (
                            None
                            if is_legacy or is_historical_parser
                            else probe.actor_user_id
                        ),
                    )
                    # The replay is the one reader body_comp keeps that can see
                    # a raw belonging to nobody: adopting that payload into this
                    # subject's history is the whole point of the sweep. Every
                    # other raw is judged by its own roots.
                    bridge = (
                        engine.LegacyConflictBridge.FULLY_UNOWNED
                        if is_legacy or is_historical_parser
                        else engine.LegacyConflictBridge.REJECT
                    )
                    prepared = await weight_governance.prepare_weight_write(
                        session,
                        context=engine.ConflictWriteContext(
                            identity=origin_identity,
                            evaluation_date=on_date,
                            legacy_bridge=bridge,
                        ),
                    )
                    context = prepared.context
                    raw, asset = await _lock_owned_raw(
                        session,
                        raw_payload_id=raw_id,
                        context=context,
                        expected_source=Source.BODY_SCAN.value,
                        require_boundary_actor=False,
                        file_key=(
                            None if is_historical_parser else probe.external_id
                        ),
                        # A Stage-3A parser raw has this subject and a
                        # connection but no file root, so the replay names it by
                        # the raw's own shape rather than by a caller's flag.
                        allow_historical_parser_raw=is_historical_parser,
                    )
                    if raw.processed_at is not None:
                        continue
                    existing_scan_id = await session.scalar(
                        select(BodyScan.id)
                        .where(BodyScan.raw_payload_id == raw.id)
                        .order_by(BodyScan.id)
                        .limit(1)
                        .with_for_update()
                    )
                    if existing_scan_id is not None:
                        continue
                    locked_extracted = (
                        raw.payload if isinstance(raw.payload, dict) else {}
                    )
                    locked_date = (
                        _parse_date(locked_extracted.get("date")) or today_local()
                    )
                    _require_evaluation_date(context, locked_date)
                    locked_is_legacy = raw.subject_id is None
                    locked_is_historical_parser = (
                        raw.subject_id == identity.subject_id
                        and raw.actor_user_id is None
                        and raw.integration_connection_id is not None
                        and raw.file_asset_id is None
                        and raw.domain == DOMAIN
                        and raw.source == Source.BODY_SCAN.value
                    )
                    locked_origin_identity = WriteIdentity(
                        identity.subject_id,
                        (
                            None
                            if locked_is_legacy or locked_is_historical_parser
                            else raw.actor_user_id
                        ),
                    )
                    if locked_origin_identity != origin_identity:
                        raise engine.ConflictRawOwnershipError(
                            "body-scan ownership changed while acquiring locks"
                        )
                    await save_scan(
                        session,
                        on_date=locked_date,
                        device=locked_extracted.get("device"),
                        file_key=asset.storage_ref if asset is not None else None,
                        raw_payload_id=raw.id,
                        metrics=normalize_extracted(locked_extracted),
                        identity=origin_identity,
                        prepared_weight_write=prepared,
                        allow_historical_parser_raw=locked_is_historical_parser,
                    )
                    await refresh_alerts(
                        session,
                        subject_id=origin_identity.subject_id,
                        on_date=locked_date,
                        identity=origin_identity,
                        prepared_weight_write=prepared,
                    )
                    raw.processed_at = now_local()
                    await session.flush()
            except Exception:
                logger.warning(
                    "owned BodyScan re-parse failed for raw payload %s",
                    raw_id,
                    exc_info=True,
                )
                continue
            done += 1
            if done >= limit:
                break
    return done
