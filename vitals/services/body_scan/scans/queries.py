"""Owned body-scan projections and validated read models."""

from __future__ import annotations

import uuid
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vitals.analytics.body_metrics import (
    body_fat_pct_from_scan,
    canonical_segment,
    display_name,
    lbm_from_scan,
)
from vitals.enums import FileAssetPurpose, FileAssetStatus, Source
from vitals.models.body_scan import DOMAIN, BodyScan, BodyScanMetric
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset
from vitals.ownership import WriteIdentity
from vitals.ownership_transition import bridges as ownership_bridges
from vitals.services import file_asset_service
from vitals.services.conflicts import engine

from .contracts import (
    BodyScanOwnershipError,
    scan_entity_key as _scan_entity_key,
)
from .ingestion import (
    _body_scan_parse_invocations,
    _historical_mcp_raw_before_lock,
    _lock_historical_parser_connection_before_raw,
    _reject_historical_parser_invocation,
    _subject_owner_user_id,
    _validate_upload_chain,
)

def _subject_scope(model, subject_id: uuid.UUID):
    """A body scan and its metrics belong to the body they measured."""

    return model.subject_id == subject_id

async def _validate_migrated_sheet_root(
    session: AsyncSession,
    *,
    scan: BodyScan,
    subject_id: uuid.UUID,
    for_update: bool,
) -> None:
    """Accept only the reviewed Stage-3O placeholder for a migrated sheet.

    A historical scan never proves who uploaded its sheet, so the migration
    registers metadata-only file roots.  Reads must recognise exactly that
    shape; anything else is forged or half-migrated provenance.
    """

    if scan.file_key is None:
        if scan.file_asset_id is not None:
            raise engine.ConflictRawOwnershipError(
                "a sheet-less historical body scan cannot claim a file root"
            )
        return
    if scan.file_asset_id is None:
        # The unprocessed Stage-3O tail: the sheet is not registered yet.
        return
    asset_stmt = select(FileAsset).where(FileAsset.id == scan.file_asset_id)
    if for_update:
        asset_stmt = asset_stmt.with_for_update().execution_options(
            populate_existing=True
        )
    asset = await session.scalar(asset_stmt)
    if (
        asset is None
        or asset.subject_id != subject_id
        or asset.uploaded_by_user_id is not None
        or asset.purpose != FileAssetPurpose.BODY_SCAN_DOCUMENT.value
        or asset.storage_ref != scan.file_key
        or not file_asset_service.local_asset_is_live(asset)
    ):
        raise engine.ConflictRawOwnershipError(
            "historical body-scan sheet root is not the reviewed placeholder"
        )


# ── Reads ─────────────────────────────────────────────────────────────────────
async def _validate_persisted_scan(
    session: AsyncSession,
    scan: BodyScan,
    *,
    subject_id: uuid.UUID,
    for_update: bool = False,
) -> None:
    if scan.domain != DOMAIN or scan.subject_id != subject_id:
        raise BodyScanOwnershipError("body scan is outside the requested scope")
    owner_user_id = await _subject_owner_user_id(session, subject_id)
    if any(metric.subject_id != scan.subject_id for metric in scan.metrics):
        raise BodyScanOwnershipError(
            "body-scan metric ownership does not inherit its scan"
        )
    if scan.source not in {
        Source.MANUAL.value,
        Source.MCP.value,
        Source.BODY_SCAN.value,
    }:
        raise BodyScanOwnershipError(
            "body scan has an unsupported scoped provenance source"
        )
    if scan.source == Source.MANUAL.value:
        # A migrated manual scan keeps its unknown actor null, so the reviewed
        # compatibility bridge must recognise that shape as well as the owner.
        if scan.actor_user_id not in {owner_user_id, None}:
            raise BodyScanOwnershipError(
                "manual body-scan actor does not match the subject owner"
            )
        if (
            scan.raw_payload_id is not None
            or scan.file_asset_id is not None
            or scan.file_key is not None
        ):
            raise engine.ConflictRawOwnershipError(
                "manual body scan carries raw or file provenance"
            )
        return
    if scan.raw_payload_id is None:
        historical_bound = await ownership_bridges.body_scan_historical_processed_bound(
            session,
            subject_id=subject_id,
        )
        if (
            historical_bound is not None
            and scan.id <= historical_bound
            and scan.actor_user_id is None
        ):
            # Stage-3O proved the subject for pre-raw-first parser history but
            # deliberately retained the absent raw/actor roots. Any historical
            # sheet still has to match its reviewed FileAsset graph.
            await _validate_migrated_sheet_root(
                session,
                scan=scan,
                subject_id=subject_id,
                for_update=for_update,
            )
            return
        raise engine.ConflictRawOwnershipError(
            f"{scan.source} body scan has no durable raw provenance"
        )

    historical_connection_id = (
        await _lock_historical_parser_connection_before_raw(
            session,
            raw_payload_id=scan.raw_payload_id,
            subject_id=subject_id,
            allow_historical_parser_raw=scan.source == Source.BODY_SCAN.value,
            for_update=for_update,
        )
    )
    historical_mcp_raw = await _historical_mcp_raw_before_lock(
        session,
        raw_payload_id=scan.raw_payload_id,
        subject_id=subject_id,
        allow_historical_mcp_raw=scan.source == Source.MCP.value,
    )
    stmt = select(RawPayload).where(RawPayload.id == scan.raw_payload_id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    raw = await session.scalar(stmt)
    if raw is None or raw.domain != DOMAIN:
        raise engine.ConflictRawOwnershipError(
            "body scan references missing or incompatible raw provenance"
        )
    raw_is_exact = raw.subject_id == subject_id
    raw_is_legacy = all(
        value is None
        for value in (
            raw.subject_id,
            raw.actor_user_id,
            raw.integration_connection_id,
            raw.file_asset_id,
        )
    )
    if not raw_is_exact and not raw_is_legacy:
        raise engine.ConflictRawOwnershipError(
            "body scan links to foreign or partial raw provenance"
        )
    if raw_is_exact and raw.actor_user_id != scan.actor_user_id:
        raise engine.ConflictRawOwnershipError(
            "body scan actor does not match durable raw provenance"
        )
    if raw.source != scan.source:
        raise engine.ConflictRawOwnershipError(
            "body scan source does not match durable raw provenance"
        )
    if historical_mcp_raw:
        if (
            scan.actor_user_id is not None
            or scan.file_asset_id is not None
            or scan.file_key is not None
            or raw.subject_id != subject_id
            or raw.actor_user_id is not None
            or raw.integration_connection_id is not None
            or raw.file_asset_id is not None
            or raw.source != Source.MCP.value
        ):
            raise engine.ConflictRawOwnershipError(
                "historical MCP body-scan normalized provenance is inconsistent"
            )
        await _reject_historical_parser_invocation(
            session,
            raw_payload_id=raw.id,
            for_update=for_update,
        )
        return
    if historical_connection_id is not None:
        if (
            scan.actor_user_id is not None
            or raw.subject_id != subject_id
            or raw.actor_user_id is not None
            or raw.integration_connection_id != historical_connection_id
            or raw.file_asset_id is not None
            or raw.source != Source.BODY_SCAN.value
        ):
            raise engine.ConflictRawOwnershipError(
                "historical body-scan normalized provenance is inconsistent"
            )
        await _validate_migrated_sheet_root(
            session,
            scan=scan,
            subject_id=subject_id,
            for_update=for_update,
        )
        await _reject_historical_parser_invocation(
            session,
            raw_payload_id=raw.id,
            for_update=for_update,
        )
        return
    if raw.source == Source.MCP.value:
        if (
            scan.actor_user_id != owner_user_id
            or raw.actor_user_id != owner_user_id
            or raw.integration_connection_id is not None
            or raw.file_asset_id is not None
            or scan.file_asset_id is not None
            or scan.file_key is not None
        ):
            raise engine.ConflictRawOwnershipError(
                "structured MCP body-scan provenance must have null C/F"
            )
        if await _body_scan_parse_invocations(
            session,
            raw_payload_id=raw.id,
            for_update=for_update,
        ):
            raise engine.ConflictRawOwnershipError(
                "structured MCP body-scan cannot claim an AI parser invocation"
            )
        return
    if raw.source != Source.BODY_SCAN.value:
        raise engine.ConflictRawOwnershipError(
            "body scan has unsupported raw provenance"
        )
    if raw_is_legacy:
        # Registration-disabled compatibility for pre-ownership parser rows.
        # New scoped BODY_SCAN writes can never create this graph.
        if scan.actor_user_id is not None:
            raise engine.ConflictRawOwnershipError(
                "legacy body-scan raw cannot authorize an actor"
            )
        await _validate_migrated_sheet_root(
            session,
            scan=scan,
            subject_id=subject_id,
            for_update=for_update,
        )
        return
    if raw.file_asset_id is None or scan.file_asset_id != raw.file_asset_id:
        raise engine.ConflictRawOwnershipError(
            "body scan file root does not match durable raw provenance"
        )
    asset_stmt = select(FileAsset).where(FileAsset.id == raw.file_asset_id)
    if for_update:
        asset_stmt = asset_stmt.with_for_update().execution_options(
            populate_existing=True
        )
    asset = await session.scalar(asset_stmt)
    if (
        asset is None
        or asset.subject_id != subject_id
        or scan.actor_user_id != owner_user_id
        or raw.actor_user_id != owner_user_id
        or asset.uploaded_by_user_id != owner_user_id
        or asset.purpose != FileAssetPurpose.BODY_SCAN_DOCUMENT.value
        or asset.status
        in {FileAssetStatus.DELETED.value, FileAssetStatus.PURGED.value}
        or asset.storage_ref != raw.external_id
        or scan.file_key != asset.storage_ref
    ):
        raise engine.ConflictRawOwnershipError(
            "body scan raw/file graph is inconsistent"
        )
    await _validate_upload_chain(
        session,
        raw=raw,
        asset=asset,
        identity=WriteIdentity(subject_id, scan.actor_user_id),
        require_boundary_actor=False,
        for_update=for_update,
    )


async def _assert_no_partial_legacy_scans(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> None:
    # A scan that belongs to nobody is out of every scope, so it can no longer
    # be read into one. An actor or a file hanging off an ownerless row is a
    # different thing: a graph migrated halfway, which nobody should read past.
    # A bare raw link is not, because that is exactly what an in-flight
    # ownership backfill looks like from here.
    invalid = await session.scalar(
        select(BodyScan.id)
        .where(
            BodyScan.subject_id.is_(None),
            or_(
                BodyScan.actor_user_id.is_not(None),
                BodyScan.file_asset_id.is_not(None),
            ),
        )
        .limit(1)
    )
    if invalid is not None:
        raise BodyScanOwnershipError(
            "body-scan scope contains a partially owned legacy fact"
        )


async def list_scans(
    session: AsyncSession,
    *,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    subject_id: uuid.UUID,
) -> Sequence[BodyScan]:
    stmt = select(BodyScan).options(selectinload(BodyScan.metrics))
    if subject_id is not None:
        await _assert_no_partial_legacy_scans(
            session,
            subject_id=subject_id,
        )
        stmt = stmt.where(_subject_scope(BodyScan, subject_id))
    if start is not None:
        stmt = stmt.where(BodyScan.date >= start)
    if end is not None:
        stmt = stmt.where(BodyScan.date <= end)
    stmt = stmt.order_by(BodyScan.date.desc(), BodyScan.id.desc())
    rows = (await session.execute(stmt)).scalars().all()
    if subject_id is not None:
        for row in rows:
            await _validate_persisted_scan(
                session,
                row,
                subject_id=subject_id,
            )
    return rows


async def get_scan(
    session: AsyncSession,
    scan_id: int,
    *,
    subject_id: uuid.UUID,
) -> Optional[BodyScan]:
    stmt = (
        select(BodyScan)
        .where(BodyScan.id == scan_id)
        .options(selectinload(BodyScan.metrics))
    )
    if subject_id is not None:
        await _assert_no_partial_legacy_scans(
            session,
            subject_id=subject_id,
        )
        stmt = stmt.where(_subject_scope(BodyScan, subject_id))
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        await _validate_persisted_scan(
            session,
            row,
            subject_id=subject_id,
        )
    return row


async def latest_scan(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    before_or_on: date_type | None = None,
) -> Optional[BodyScan]:
    stmt = (
        select(BodyScan)
        .options(selectinload(BodyScan.metrics))
        .order_by(BodyScan.date.desc(), BodyScan.id.desc())
        .limit(1)
    )
    if subject_id is not None:
        await _assert_no_partial_legacy_scans(
            session,
            subject_id=subject_id,
        )
        stmt = stmt.where(_subject_scope(BodyScan, subject_id))
    if before_or_on is not None:
        stmt = stmt.where(BodyScan.date <= before_or_on)
    row = (await session.execute(stmt)).scalars().first()
    if row is not None:
        await _validate_persisted_scan(
            session,
            row,
            subject_id=subject_id,
        )
    return row


async def resolve_active_scoped(
    session: AsyncSession,
    *,
    scope: engine.ConflictScope,
) -> list[dict]:
    """Return every latest-date BodyScan visible in one conflict scope.

    Multiple same-day scans are independent facts. Create operations therefore
    never use replacement semantics and the resolver must not collapse them to
    one row. Older dates are historical rather than permanently active state.
    """

    rows = await list_scans(
        session,
        end=scope.evaluation_date,
        subject_id=scope.subject_id,
    )
    if not rows:
        return []
    latest_date = rows[0].date
    return [
        {
            engine.CONFLICT_ENTITY_KEY: _scan_entity_key(row),
            "scan": True,
            "source": row.source,
        }
        for row in rows
        if row.date == latest_date
    ]


async def metric_history(
    session: AsyncSession,
    metric_key: str,
    *,
    segment: Optional[str] = None,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    subject_id: uuid.UUID,
) -> list[dict]:
    """Chronological series for one metric (optionally a single segment)."""
    stmt = (
        select(BodyScanMetric, BodyScan.date)
        .join(BodyScan, BodyScanMetric.scan_id == BodyScan.id)
        .where(BodyScanMetric.metric_key == metric_key)
    )
    seg = canonical_segment(segment)
    if seg is not None:
        stmt = stmt.where(BodyScanMetric.segment == seg)
    if start is not None:
        stmt = stmt.where(BodyScan.date >= start)
    if end is not None:
        stmt = stmt.where(BodyScan.date <= end)
    stmt = stmt.where(_subject_scope(BodyScan, subject_id))
    stmt = stmt.order_by(BodyScan.date, BodyScanMetric.id)
    rows = (await session.execute(stmt)).all()
    if subject_id is not None:
        scan_ids = {metric.scan_id for metric, _ in rows}
        for scan_id in scan_ids:
            scan = await get_scan(
                session,
                scan_id,
                subject_id=subject_id,
            )
            if scan is None:
                raise BodyScanOwnershipError(
                    "body-scan metric parent is outside the requested scope"
                )
    return [
        {
            "date": d.isoformat(),
            "value": m.value,
            "unit": m.unit,
            "segment": m.segment,
            "ref_low": m.ref_low,
            "ref_high": m.ref_high,
        }
        for (m, d) in rows
    ]


# Display labels for the canonical segments (chart-builder picklist only — the
# label->segment direction already lives in body_metrics.SEGMENT_ALIASES).
# Public (not underscore-prefixed): reused by chart_data_service for auto-labeling
# saved chart series.
SEGMENT_LABELS_RU = {
    "right_arm": "правая рука",
    "left_arm": "левая рука",
    "trunk": "туловище",
    "right_leg": "правая нога",
    "left_leg": "левая нога",
}


async def available_metrics(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> list[dict]:
    """Distinct (metric_key, segment) pairs actually present across all scans,
    each with a display label and a stable ``value`` (``metric_key`` for
    whole-body rows, ``"metric_key:segment"`` for segmental rows) — the
    parameter picklist for the chart-builder catalog (analogous to
    ``hevy.queries.exercise_catalog``)."""
    stmt = (
        select(BodyScanMetric.metric_key, BodyScanMetric.segment)
        .join(BodyScan, BodyScanMetric.scan_id == BodyScan.id)
        .distinct()
        .order_by(BodyScanMetric.metric_key, BodyScanMetric.segment)
    )
    if subject_id is not None:
        # Validate the complete graph before exposing its metric catalog.
        await list_scans(
            session,
            subject_id=subject_id,
        )
        stmt = stmt.where(_subject_scope(BodyScan, subject_id))
    result = await session.execute(stmt)
    out: list[dict] = []
    for metric_key, segment in result.all():
        label = display_name(metric_key) or metric_key
        if segment:
            out.append({
                "value": f"{metric_key}:{segment}",
                "label": f"{label} — {SEGMENT_LABELS_RU.get(segment, segment)}",
            })
        else:
            out.append({"value": metric_key, "label": label})
    return out


async def bia_chart_points(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> dict:
    """BIA body-fat % and LBM series (latest scan per date) for the weight chart.
    Coexists with the Navy series — both are drawn."""
    scans = list(
        reversed(
            await list_scans(
                session,
                subject_id=subject_id,
            )
        )
    )

    by_date: dict[date_type, BodyScan] = {}
    for s in scans:
        by_date[s.date] = s  # ascending order → latest id per date wins

    bf: list[dict] = []
    lbm: list[dict] = []
    for d in sorted(by_date):
        ms = by_date[d].metrics
        b = body_fat_pct_from_scan(ms)
        if b is not None:
            bf.append({"date": d.isoformat(), "value": b})
        lbm_val = lbm_from_scan(ms)
        if lbm_val is not None:
            lbm.append({"date": d.isoformat(), "value": lbm_val})
    return {"bf": bf, "lbm": lbm}
