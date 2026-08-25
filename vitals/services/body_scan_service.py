"""Body-composition scan service — InBody / МедАсс (BIA) (optional module).

Owns the ``body_comp`` domain. A scan is an upload (or agent/manual entry) of a
bicompedance analyzer sheet; we capture **every** printed metric generically.

Pipeline (two-step because of the edit-before-save preview):

  1. :func:`extract_from_file` — a photo/PDF → a structured dict via the same
     OpenRouter vision model the labs parser uses (the document is also kept raw).
  2. :func:`normalize_extracted` — pure mapping of printed labels onto canonical
     metric keys (the editable preview rows).
  3. :func:`save_scan` — persist the owner-edited rows as a ``BodyScan`` + child
     ``BodyScanMetric`` rows, stamp the raw payload processed, and **bridge** the
     scan's weight / body-fat% / LBM into the weight domain (a second source that
     coexists with Navy — see ``weight_service``).

Like labs, nothing here blocks and the LLM is optional — every value can be
entered by hand, so the module works with no key configured.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date as date_type, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
    Severity,
    Source,
)
from vitals.i18n import t
from vitals.models.ai import AIInvocation
from vitals.models.body_scan import DOMAIN, BodyScan, BodyScanMetric
from vitals.models.identity import HealthSubject
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import (
    alerts_service,
    conflict_engine,
    file_asset_service,
    raw_payload_service,
    weight_service,
)
from vitals.analytics.body_metrics import (
    CAT_OTHER,
    METRIC_REGISTRY,
    body_fat_pct_from_scan,
    canonical_segment,
    display_name,
    lbm_from_scan,
    normalize_metric,
    weight_from_scan,
)
from vitals.integrations.vision import file_to_image_urls
from vitals.utils.timeutils import now_local, today_local

logger = logging.getLogger(__name__)

VISCERAL_ALERT_KEY = "body_comp.visceral_high"
PHASE_ALERT_KEY = "body_comp.phase_low"


class BodyScanOwnershipError(ValueError):
    """A scan, metric, or provenance root is outside the requested scope."""


class BodyScanRawAlreadyNormalizedError(BodyScanOwnershipError):
    """One immutable raw payload already owns a normalized BodyScan fact."""


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity | None,
    prepared: weight_service.PreparedWeightWrite | None,
) -> conflict_engine.ConflictWriteContext | None:
    if identity is None and prepared is None:
        return None
    if identity is None or prepared is None:
        raise conflict_engine.ConflictPreparedWriteError(
            "scoped body-scan writes require identity and a prepared Weight write"
        )
    return weight_service.require_prepared_weight_identity(
        session,
        prepared=prepared,
        identity=identity,
    )


def _require_evaluation_date(
    context: conflict_engine.ConflictWriteContext,
    on_date: date_type,
) -> None:
    if context.evaluation_date != on_date:
        raise conflict_engine.ConflictPreparedWriteError(
            "body-scan date does not match prepared Weight capability"
        )


def _subject_scope(model, subject_id: uuid.UUID):
    """A body scan and its metrics belong to the body they measured."""

    return model.subject_id == subject_id


def _alert_bridge(
    context: conflict_engine.ConflictWriteContext,
) -> alerts_service.LegacyAlertBridge:
    if context.legacy_bridge is conflict_engine.LegacyConflictBridge.FULLY_UNOWNED:
        return alerts_service.LegacyAlertBridge.FULLY_UNOWNED
    return alerts_service.LegacyAlertBridge.REJECT


def _system_alert_context(
    context: conflict_engine.ConflictWriteContext,
) -> alerts_service.HealthAlertContext:
    return alerts_service.HealthAlertContext(
        WriteIdentity(context.identity.subject_id, None)
    )


def _scan_entity_key(scan: BodyScan) -> str:
    return f"body_scan:{scan.id}"


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


# ── LLM extraction (optional auto-fill) ───────────────────────────────────────
_EXTRACT_SYSTEM = (
    "You are a body-composition analyzer parser (InBody / МедАсс / bioimpedance). "
    "Extract EVERY printed metric from the device sheet image(s). Respond ONLY with "
    'JSON of the form: {"date": "YYYY-MM-DD"|null, "device": string|null, "metrics": '
    '[{"label": string, "value": number, "unit": string|null, "ref_low": number|null, '
    '"ref_high": number|null, "segment": string|null}]}. '
    "label = the metric name exactly as printed (keep its original language). "
    "value = a plain number only (no ranges or units inside it). "
    "unit = the printed unit or null. ref_low/ref_high = the normal/target range "
    "bounds when shown, else null. segment = one of right_arm,left_arm,trunk,"
    "right_leg,left_leg for per-limb segmental rows, otherwise null. Use the "
    "measurement date. If a field is unknown use null. Never invent metrics."
)


async def extract_from_file(
    file_bytes: bytes,
    *,
    llm: Any,
    content_type: str = "image/jpeg",
    filename: Optional[str] = None,
) -> dict:
    """Send the sheet to the vision model and return the parsed structured dict.
    PDFs are rendered to images first. Raises whatever the LLM client raises
    (e.g. ``LLMNotConfigured``) so the router can surface a clear message."""
    image_urls = file_to_image_urls(
        file_bytes, content_type=content_type, filename=filename
    )
    return await llm.extract_json(
        "Extract every metric from this body-composition analyzer report.",
        system=_EXTRACT_SYSTEM,
        image_urls=image_urls,
    )


def prepare_file_for_extraction(
    file_bytes: bytes,
    *,
    content_type: str = "image/jpeg",
    filename: Optional[str] = None,
) -> tuple[str, ...]:
    """Convert local document bytes before any paid provider dispatch."""

    return tuple(
        file_to_image_urls(
            file_bytes,
            content_type=content_type,
            filename=filename,
        )
    )


async def extract_prepared_file_with_usage(
    image_urls: tuple[str, ...],
    *,
    llm: Any,
    model: str,
    max_tokens: int,
):
    """Send a locally prepared scan through one usage-aware AI call."""

    if not image_urls:
        raise ValueError("prepared body-scan document has no images")
    return await llm.extract_json_with_usage(
        "Extract every metric from this body-composition analyzer report.",
        model=model,
        system=_EXTRACT_SYSTEM,
        image_urls=list(image_urls),
        max_tokens=max_tokens,
    )


async def extract_from_file_with_usage(
    file_bytes: bytes,
    *,
    llm: Any,
    content_type: str = "image/jpeg",
    filename: Optional[str] = None,
    model: str,
    max_tokens: int,
):
    """Usage-aware platform-gateway adapter for body-scan recognition."""

    return await extract_prepared_file_with_usage(
        prepare_file_for_extraction(
            file_bytes,
            content_type=content_type,
            filename=filename,
        ),
        llm=llm,
        model=model,
        max_tokens=max_tokens,
    )


def normalize_extracted(extracted: dict) -> list[dict]:
    """Pure: turn a raw vision dict into normalized, editable metric rows.

    Each row is ``{metric_key, label, value, unit, ref_low, ref_high, segment,
    category}``. Unparseable rows (no label / non-numeric value) are dropped."""
    rows: list[dict] = []
    for item in extracted.get("metrics") or []:
        row = _normalize_item(item)
        if row is not None:
            rows.append(row)
    return rows


def _normalize_item(item: dict) -> Optional[dict]:
    """Normalize one metric dict (from vision, the preview, or an agent call).

    Driven by the printed ``label`` when present (so editing/auditing is stable);
    falls back to an explicit ``metric_key`` for agent calls with no label."""
    label = (item.get("label") or "").strip()
    value = _num(item.get("value"))
    if value is None:
        return None
    seg_in = item.get("segment")
    if label:
        key, category, segment = normalize_metric(label, seg_in)
    else:
        key = item.get("metric_key")
        if not key:
            return None
        spec = METRIC_REGISTRY.get(key)
        category = item.get("category") or (spec.category if spec else CAT_OTHER)
        segment = canonical_segment(seg_in)
    return {
        "metric_key": key,
        "label": label or (display_name(key) or key),
        "value": value,
        "unit": (item.get("unit") or None),
        "ref_low": _num(item.get("ref_low")),
        "ref_high": _num(item.get("ref_high")),
        "segment": segment,
        "category": category,
    }


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
        raise conflict_engine.ConflictRawOwnershipError(
            "body-scan upload is outside the prepared subject"
        )
    if (
        identity.actor_user_id != owner_user_id
        or raw.actor_user_id != owner_user_id
        or asset.uploaded_by_user_id != owner_user_id
    ):
        raise conflict_engine.ConflictRawOwnershipError(
            "body-scan upload actor does not match the subject owner"
        )
    if require_boundary_actor:
        if identity.actor_user_id is None:
            raise conflict_engine.ConflictPreparedWriteError(
                "body-scan upload confirmation requires an active human actor"
            )
        if raw.actor_user_id != identity.actor_user_id:
            raise conflict_engine.ConflictRawOwnershipError(
                "body-scan upload actor does not match the prepared writer"
            )
    if (
        raw.domain != DOMAIN
        or raw.source != Source.BODY_SCAN.value
        or raw.file_asset_id != asset.id
        or asset.purpose != FileAssetPurpose.BODY_SCAN_DOCUMENT.value
        or not file_asset_service.local_asset_is_live(asset)
        or raw.external_id != asset.storage_ref
    ):
        raise conflict_engine.ConflictRawOwnershipError(
            "body-scan upload provenance is inconsistent"
        )
    invocations = await _body_scan_parse_invocations(
        session,
        raw_payload_id=raw.id,
        for_update=for_update,
    )
    if raw.integration_connection_id is None:
        if len(invocations) != 1:
            raise conflict_engine.ConflictRawOwnershipError(
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
            raise conflict_engine.ConflictRawOwnershipError(
                "platform body-scan parser provenance is invalid"
            )
        return
    if invocations:
        raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
            "historical MCP body-scan raw requires exactly one subject"
        )
    return True


async def _lock_owned_raw(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    context: conflict_engine.ConflictWriteContext,
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
    exact_raw, fully_unowned_raw = conflict_engine.raw_payload_scope_conditions(
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
        raise conflict_engine.ConflictRawOwnershipError(
            "body-scan raw provenance is outside the prepared subject"
        )
    if raw.domain != DOMAIN or raw.source != expected_source:
        raise conflict_engine.ConflictRawOwnershipError(
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
            raise conflict_engine.ConflictRawOwnershipError(
                "historical body-scan provenance changed while acquiring locks"
            )
        if file_key is not None:
            raise conflict_engine.ConflictRawOwnershipError(
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
            raise conflict_engine.ConflictRawOwnershipError(
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
            raise conflict_engine.ConflictRawOwnershipError(
                "structured MCP body-scan raw must have exact S/A and null C/F"
            )
        if await _body_scan_parse_invocations(
            session,
            raw_payload_id=raw.id,
            for_update=True,
        ):
            raise conflict_engine.ConflictRawOwnershipError(
                "structured MCP body-scan raw cannot claim an AI parser invocation"
            )
        return raw, None

    if raw.file_asset_id is None:
        raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
            "body-scan file provenance is outside the prepared subject"
        )
    if asset.status in {FileAssetStatus.DELETED.value, FileAssetStatus.PURGED.value}:
        raise conflict_engine.ConflictRawOwnershipError(
            "body-scan file provenance is no longer available"
        )
    if file_key is not None and file_key != asset.storage_ref:
        raise conflict_engine.ConflictRawOwnershipError(
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
    prepared_weight_write: weight_service.PreparedWeightWrite,
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
            raise conflict_engine.ConflictRawOwnershipError(
                "manual body scans cannot claim raw, file, or provider provenance"
            )
        if source == Source.MANUAL.value:
            owner_user_id = await _subject_owner_user_id(
                session,
                identity.subject_id,
            )
            if identity.actor_user_id != owner_user_id:
                raise conflict_engine.ConflictPreparedWriteError(
                    "manual body scans require the subject owner actor"
                )
        if source == Source.MCP.value and any(
            value is not None for value in (file_key, file_asset_id)
        ):
            raise conflict_engine.ConflictRawOwnershipError(
                "structured MCP body scans require null file provenance"
            )
        if source in {Source.MCP.value, Source.BODY_SCAN.value} and raw_payload_id is None:
            raise conflict_engine.ConflictRawOwnershipError(
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
    await conflict_engine.enforce_prepared(
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
    # supersedes a scan). Enforced in weight_service.
    w = weight_from_scan(normalized)
    if w is not None and w > 0:
        origin_actor_user_id = (
            owned_raw.actor_user_id
            if owned_raw is not None
            else (identity.actor_user_id if identity is not None else None)
        )
        await weight_service.log_weight(
            session,
            on_date=on_date,
            weight_kg=w,
            source=Source.BODY_SCAN.value,
            override=override,
            identity=identity,
            integration_connection_id=(
                owned_raw.integration_connection_id
                if owned_raw is not None
                else None
            ),
            raw_payload_id=(owned_raw.id if owned_raw is not None else None),
            prepared_weight_write=prepared_weight_write,
            origin_actor_user_id=origin_actor_user_id,
            allow_historical_parser_raw=allow_historical_parser_raw,
        )
    await session.flush()
    return scan


async def ingest_structured_scan(
    session: AsyncSession,
    extracted: dict,
    *,
    raw_payload: RawPayload,
    identity: WriteIdentity,
    prepared_weight_write: weight_service.PreparedWeightWrite,
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
        raise conflict_engine.ConflictRawOwnershipError(
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


async def reparse_owned_pending(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    limit: int = raw_payload_service.REPARSE_BATCH,
    since_days: int = raw_payload_service.REPARSE_WINDOW_DAYS,
) -> int:
    """Replay pending upload raws in isolated per-raw savepoints."""

    if not isinstance(identity, WriteIdentity):
        raise conflict_engine.ConflictPreparedWriteError(
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
            conflict_engine.ConflictRawOwnershipError,
        ) as exc:
            raise conflict_engine.ConflictRawOwnershipError(
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
                        conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
                        if is_legacy or is_historical_parser
                        else conflict_engine.LegacyConflictBridge.REJECT
                    )
                    prepared = await weight_service.prepare_weight_write(
                        session,
                        context=conflict_engine.ConflictWriteContext(
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
                        raise conflict_engine.ConflictRawOwnershipError(
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
            raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
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
            raise conflict_engine.ConflictRawOwnershipError(
                "manual body scan carries raw or file provenance"
            )
        return
    if scan.raw_payload_id is None:
        raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
            "body scan links to foreign or partial raw provenance"
        )
    if raw_is_exact and raw.actor_user_id != scan.actor_user_id:
        raise conflict_engine.ConflictRawOwnershipError(
            "body scan actor does not match durable raw provenance"
        )
    if raw.source != scan.source:
        raise conflict_engine.ConflictRawOwnershipError(
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
            raise conflict_engine.ConflictRawOwnershipError(
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
            raise conflict_engine.ConflictRawOwnershipError(
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
            raise conflict_engine.ConflictRawOwnershipError(
                "structured MCP body-scan provenance must have null C/F"
            )
        if await _body_scan_parse_invocations(
            session,
            raw_payload_id=raw.id,
            for_update=for_update,
        ):
            raise conflict_engine.ConflictRawOwnershipError(
                "structured MCP body-scan cannot claim an AI parser invocation"
            )
        return
    if raw.source != Source.BODY_SCAN.value:
        raise conflict_engine.ConflictRawOwnershipError(
            "body scan has unsupported raw provenance"
        )
    if raw_is_legacy:
        # Registration-disabled compatibility for pre-ownership parser rows.
        # New scoped BODY_SCAN writes can never create this graph.
        if scan.actor_user_id is not None:
            raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
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
    scope: conflict_engine.ConflictScope,
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
            conflict_engine.CONFLICT_ENTITY_KEY: _scan_entity_key(row),
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
    ``hevy_service.exercise_catalog``)."""
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


async def _lock_scan_for_update(
    session: AsyncSession,
    scan_id: int,
    *,
    context: conflict_engine.ConflictWriteContext,
) -> BodyScan | None:
    candidate = await get_scan(
        session,
        scan_id,
        subject_id=context.identity.subject_id,
    )
    if candidate is None:
        return None
    await _validate_persisted_scan(
        session,
        candidate,
        subject_id=context.identity.subject_id,
        for_update=True,
    )
    # Provenance roots were validated before the fact lock. Lock the parent and
    # children in their stable order, then reject a concurrent provenance swap.
    row = await session.scalar(
        select(BodyScan)
        .where(
            BodyScan.id == scan_id,
            _subject_scope(
                BodyScan,
                context.identity.subject_id,
            ),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    list(
        await session.scalars(
            select(BodyScanMetric)
            .where(BodyScanMetric.scan_id == row.id)
            .order_by(BodyScanMetric.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    refreshed = await get_scan(
        session,
        row.id,
        subject_id=context.identity.subject_id,
    )
    if refreshed is None:
        return None
    if (
        refreshed.raw_payload_id != candidate.raw_payload_id
        or refreshed.source != candidate.source
        or refreshed.file_asset_id != candidate.file_asset_id
    ):
        raise conflict_engine.ConflictRawOwnershipError(
            "body-scan provenance changed while acquiring write locks"
        )
    return refreshed


async def update_scan_note(
    session: AsyncSession,
    scan_id: int,
    *,
    note: str | None,
    identity: WriteIdentity,
    prepared_weight_write: weight_service.PreparedWeightWrite,
) -> BodyScan | None:
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    assert context is not None
    row = await _lock_scan_for_update(
        session,
        scan_id,
        context=context,
    )
    if row is None:
        return None
    row.note = note
    await session.flush()
    return row


async def delete_scan(
    session: AsyncSession,
    scan_id: int,
    *,
    subject_id: uuid.UUID,
    identity: WriteIdentity,
    prepared_weight_write: weight_service.PreparedWeightWrite,
) -> bool:
    """Delete a scan (cascades to its metrics). Returns False if not found.

    The bridged weight row is left as-is (it's an independent weight log); the
    owner can remove it from the weight tab if desired."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    if context is not None:
        if subject_id is not None and subject_id != identity.subject_id:
            raise conflict_engine.ConflictPreparedWriteError(
                "subject_id does not match prepared body-scan identity"
            )
        scan = await _lock_scan_for_update(
            session,
            scan_id,
            context=context,
        )
    else:
        scan = await get_scan(
            session,
            scan_id,
            subject_id=subject_id,
        )
    if scan is None:
        return False
    await session.delete(scan)
    await session.flush()
    return True


# ── Alerts (light) ────────────────────────────────────────────────────────────
async def refresh_alerts(
    session: AsyncSession,
    *,
    on_date: date_type | None = None,
    subject_id: uuid.UUID,
    identity: WriteIdentity,
    prepared_weight_write: weight_service.PreparedWeightWrite,
) -> None:
    """Raise/clear passive ``info`` alerts from the latest scan: visceral fat above
    its printed range, or phase angle below its printed range. Idempotent. Each
    alert is bound to the triggering scan's id, so a dismissal sticks forever
    for that scan — only a newer scan can raise it again."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    if context is not None:
        if on_date is not None:
            _require_evaluation_date(context, on_date)
        if subject_id is not None and subject_id != identity.subject_id:
            raise conflict_engine.ConflictPreparedWriteError(
                "subject_id does not match prepared body-scan identity"
            )
        subject_id = identity.subject_id

    scan = await latest_scan(
        session,
        subject_id=subject_id,
    )
    alert_context = _system_alert_context(context) if context is not None else None
    alert_bridge = _alert_bridge(context) if context is not None else None

    if context is not None and scan is not None:
        # Raw/F/parser-C validation and locks precede the scan and its children;
        # typed alerts acquire their natural-key locks only after this block.
        await _validate_persisted_scan(
            session,
            scan,
            subject_id=identity.subject_id,
            for_update=True,
        )
        await session.scalar(
            select(BodyScan)
            .where(BodyScan.id == scan.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        list(
            await session.scalars(
                select(BodyScanMetric)
                .where(BodyScanMetric.scan_id == scan.id)
                .order_by(BodyScanMetric.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        scan = await get_scan(
            session,
            scan.id,
            subject_id=identity.subject_id,
        )
        if scan is None:
            raise BodyScanOwnershipError(
                "body scan disappeared while acquiring alert locks"
            )

    if scan is None:
        if alert_context is None:
            await alerts_service.resolve_superseded(
                session, alert_key=VISCERAL_ALERT_KEY, keep_entity=None
            )
            await alerts_service.resolve_superseded(
                session, alert_key=PHASE_ALERT_KEY, keep_entity=None
            )
        else:
            assert alert_bridge is not None
            await alerts_service.resolve_scoped_superseded(
                session,
                context=alert_context,
                alert_key=VISCERAL_ALERT_KEY,
                keep_entity=None,
                legacy_bridge=alert_bridge,
            )
            await alerts_service.resolve_scoped_superseded(
                session,
                context=alert_context,
                alert_key=PHASE_ALERT_KEY,
                keep_entity=None,
                legacy_bridge=alert_bridge,
            )
        return

    entity = str(scan.id)
    if alert_context is None:
        await alerts_service.resolve_superseded(
            session, alert_key=VISCERAL_ALERT_KEY, keep_entity=entity
        )
        await alerts_service.resolve_superseded(
            session, alert_key=PHASE_ALERT_KEY, keep_entity=entity
        )
    else:
        assert alert_bridge is not None
        await alerts_service.resolve_scoped_superseded(
            session,
            context=alert_context,
            alert_key=VISCERAL_ALERT_KEY,
            keep_entity=entity,
            legacy_bridge=alert_bridge,
        )
        await alerts_service.resolve_scoped_superseded(
            session,
            context=alert_context,
            alert_key=PHASE_ALERT_KEY,
            keep_entity=entity,
            legacy_bridge=alert_bridge,
        )

    by_key = {m.metric_key: m for m in scan.metrics}

    vfa = by_key.get("visceral_fat_area") or by_key.get("visceral_fat_level")
    if vfa is not None and vfa.ref_high is not None and vfa.value > vfa.ref_high:
        dismissed = (
            await alerts_service._was_ever_dismissed(
                session, VISCERAL_ALERT_KEY, entity
            )
            if alert_context is None
            else await alerts_service.was_scoped_ever_dismissed(
                session,
                context=alert_context,
                alert_key=VISCERAL_ALERT_KEY,
                entity_ref=entity,
                legacy_bridge=alert_bridge,
            )
        )
        if not dismissed:
            message = t(
                "alert.body_visceral_high",
                value=vfa.value,
                unit=((" " + vfa.unit) if vfa.unit else ""),
            )
            if alert_context is None:
                await alerts_service.raise_alert(
                    session,
                    domain=Domain.BODY_COMPOSITION.value,
                    severity=Severity.INFO.value,
                    message=message,
                    alert_key=VISCERAL_ALERT_KEY,
                    entity_ref=entity,
                )
            else:
                await alerts_service.raise_scoped_alert(
                    session,
                    context=alert_context,
                    domain=Domain.BODY_COMPOSITION,
                    severity=Severity.INFO,
                    message=message,
                    alert_key=VISCERAL_ALERT_KEY,
                    entity_ref=entity,
                    legacy_bridge=alert_bridge,
                )
    else:
        if alert_context is None:
            await alerts_service.resolve_by_key(
                session, alert_key=VISCERAL_ALERT_KEY, entity_ref=entity
            )
        else:
            await alerts_service.resolve_scoped_by_key(
                session,
                context=alert_context,
                alert_key=VISCERAL_ALERT_KEY,
                entity_ref=entity,
                legacy_bridge=alert_bridge,
            )

    phase = by_key.get("phase_angle")
    if phase is not None and phase.ref_low is not None and phase.value < phase.ref_low:
        dismissed = (
            await alerts_service._was_ever_dismissed(session, PHASE_ALERT_KEY, entity)
            if alert_context is None
            else await alerts_service.was_scoped_ever_dismissed(
                session,
                context=alert_context,
                alert_key=PHASE_ALERT_KEY,
                entity_ref=entity,
                legacy_bridge=alert_bridge,
            )
        )
        if not dismissed:
            message = t("alert.body_phase_low", value=phase.value)
            if alert_context is None:
                await alerts_service.raise_alert(
                    session,
                    domain=Domain.BODY_COMPOSITION.value,
                    severity=Severity.INFO.value,
                    message=message,
                    alert_key=PHASE_ALERT_KEY,
                    entity_ref=entity,
                )
            else:
                await alerts_service.raise_scoped_alert(
                    session,
                    context=alert_context,
                    domain=Domain.BODY_COMPOSITION,
                    severity=Severity.INFO,
                    message=message,
                    alert_key=PHASE_ALERT_KEY,
                    entity_ref=entity,
                    legacy_bridge=alert_bridge,
                )
    else:
        if alert_context is None:
            await alerts_service.resolve_by_key(
                session, alert_key=PHASE_ALERT_KEY, entity_ref=entity
            )
        else:
            await alerts_service.resolve_scoped_by_key(
                session,
                context=alert_context,
                alert_key=PHASE_ALERT_KEY,
                entity_ref=entity,
                legacy_bridge=alert_bridge,
            )


# ── Helpers ───────────────────────────────────────────────────────────────────
def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (ValueError, TypeError):
        return None


def _parse_date(v: Any) -> Optional[date_type]:
    if not v:
        return None
    try:
        return date_type.fromisoformat(str(v)[:10])
    except ValueError:
        return None
