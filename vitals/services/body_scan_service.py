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
from datetime import date as date_type
from typing import Any, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from vitals.enums import Domain, FileAssetPurpose, Severity, Source
from vitals.i18n import t
from vitals.models.body_scan import DOMAIN, BodyScan, BodyScanMetric
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services import alerts_service, conflict_engine, raw_payload_service, weight_service
from vitals.services.upload_ownership_service import resolve_owned_upload_reference
from vitals.services.analytics.body_metrics import (
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
    identity: WriteIdentity | None = None,
    file_asset_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
    prepared_weight_write: weight_service.PreparedWeightWrite | None = None,
) -> BodyScan:
    """Persist a scan and its metrics (owner-edited rows), stamp the raw payload
    processed, and bridge weight into the weight domain. Does not commit.

    May raise ``ConflictBlocked`` if a cross-domain block rule fires without
    ``override`` (override plumbing kept consistent with the weight domain)."""
    weight_context = None
    if identity is not None or prepared_weight_write is not None:
        if identity is None or prepared_weight_write is None:
            raise conflict_engine.ConflictPreparedWriteError(
                "owned body-scan weight bridge requires identity and Weight capability"
            )
        weight_context = weight_service.require_prepared_weight_identity(
            session,
            prepared=prepared_weight_write,
            identity=identity,
        )
        if weight_context.evaluation_date != on_date:
            raise conflict_engine.ConflictPreparedWriteError(
                "body-scan date does not match prepared Weight capability"
            )
        if (
            include_legacy_unowned
            and weight_context.legacy_bridge
            is not conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
        ):
            raise conflict_engine.ConflictPreparedWriteError(
                "legacy body-scan bridge requires fully-unowned compatibility"
            )
    elif include_legacy_unowned:
        raise ValueError("legacy body-scan bridge requires a scoped writer")

    owned_raw: RawPayload | None = None
    authoritative_file_key = file_key
    authoritative_file_asset_id = file_asset_id
    if identity is not None and raw_payload_id is not None:
        upload = await resolve_owned_upload_reference(
            session,
            identity=identity,
            raw_payload_id=raw_payload_id,
            client_storage_ref=file_key,
            domain=DOMAIN,
            source=Source.BODY_SCAN.value,
            purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT,
        )
        if file_asset_id is not None and file_asset_id != upload.file_asset.id:
            raise ValueError("file_asset_id does not match the owned upload")
        owned_raw = upload.raw_payload
        authoritative_file_key = upload.storage_ref
        authoritative_file_asset_id = upload.file_asset.id
    elif identity is not None and any(
        value is not None for value in (file_key, raw_payload_id, file_asset_id)
    ):
        raise ValueError("owned scan file references require a raw upload")
    elif identity is None and file_asset_id is not None:
        raise ValueError("file_asset_id requires an explicit write identity")

    # Authorization precedes the conflict engine because a rejected client id
    # must not be able to trigger even an alert side effect in this transaction.
    await conflict_engine.enforce(
        session,
        Domain.BODY_COMPOSITION.value,
        {"scan": True},
        override=override,
        entity_ref=f"body_scan:{on_date.isoformat()}",
    )

    scan = BodyScan(
        subject_id=identity.subject_id if identity is not None else None,
        actor_user_id=identity.actor_user_id if identity is not None else None,
        file_asset_id=authoritative_file_asset_id,
        date=on_date,
        domain=DOMAIN,
        source=source,
        device=(device or None),
        file_key=authoritative_file_key,
        raw_payload_id=raw_payload_id,
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
        raw = owned_raw or await session.get(RawPayload, raw_payload_id)
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
            include_legacy_unowned=include_legacy_unowned,
            prepared_weight_write=prepared_weight_write,
            origin_actor_user_id=origin_actor_user_id,
        )
    await session.flush()
    return scan


async def ingest_extracted(
    session: AsyncSession,
    extracted: dict,
    *,
    file_key: Optional[str] = None,
    device: Optional[str] = None,
) -> BodyScan:
    """Convenience: store the raw payload + save a scan straight from a vision
    dict (no preview). Used by tests and any auto-ingest path; the web flow uses
    the two-step upload→confirm instead so the owner can edit first."""
    on_date = _parse_date(extracted.get("date")) or today_local()
    dev = device or extracted.get("device")
    raw_row = await raw_payload_service.upsert_raw_payload(
        session,
        domain=DOMAIN,
        source=Source.BODY_SCAN.value,
        external_id=file_key or f"body_scan:{on_date.isoformat()}",
        payload=extracted,
    )
    rows = normalize_extracted(extracted)
    return await save_scan(
        session,
        on_date=on_date,
        device=dev,
        file_key=file_key,
        raw_payload_id=raw_row.id,
        metrics=rows,
    )


async def reparse_from_raw(session: AsyncSession, raw_row: RawPayload) -> None:
    """Re-run extraction ingest against a scan payload already on disk — no new
    upload. Covers uploads the owner never confirmed (extracted but abandoned at
    the preview step). Reuses :func:`ingest_extracted`, which calls
    :func:`save_scan` with ``override=False`` — a still-active hard-block
    conflict rule raises ``ConflictBlocked``, which the sweep's generic
    try/except logs and skips, leaving the row pending for the next pass rather
    than forcing it through. Preserves ``fetched_at``: this is a reparse, not a
    new upload. Used by :func:`reparse_pending` (the nightly sweep —
    raw_payload_service.sweep_pending_job)."""
    extracted = raw_row.payload if isinstance(raw_row.payload, dict) else {}
    original_fetched_at = raw_row.fetched_at
    if raw_row.subject_id is not None and raw_row.file_asset_id is not None:
        on_date = _parse_date(extracted.get("date")) or today_local()
        identity = WriteIdentity(raw_row.subject_id, raw_row.actor_user_id)
        prepared_weight_write = await weight_service.prepare_weight_write(
            session,
            context=conflict_engine.ConflictWriteContext(
                identity=identity,
                evaluation_date=on_date,
                legacy_bridge=conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
            ),
        )
        await save_scan(
            session,
            on_date=on_date,
            device=extracted.get("device"),
            file_key=raw_row.external_id,
            raw_payload_id=raw_row.id,
            metrics=normalize_extracted(extracted),
            identity=identity,
            include_legacy_unowned=True,
            prepared_weight_write=prepared_weight_write,
        )
    else:
        await ingest_extracted(session, extracted, file_key=raw_row.external_id)
    raw_row.fetched_at = original_fetched_at


async def reparse_pending(
    session: AsyncSession,
    *,
    limit: int = raw_payload_service.REPARSE_BATCH,
    since_days: int = raw_payload_service.REPARSE_WINDOW_DAYS,
) -> int:
    """Sweep body-comp raw payloads (extractions never confirmed by the owner)
    still pending a normalized row. Does not commit."""
    has_normalized = (
        select(BodyScan.id).where(BodyScan.raw_payload_id == RawPayload.id).exists()
    )
    return await raw_payload_service.sweep_domain(
        session,
        domain=DOMAIN,
        reparse=reparse_from_raw,
        has_normalized=has_normalized,
        limit=limit,
        since_days=since_days,
    )


# ── Reads ─────────────────────────────────────────────────────────────────────
async def list_scans(
    session: AsyncSession,
    *,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> Sequence[BodyScan]:
    stmt = select(BodyScan).options(selectinload(BodyScan.metrics))
    if subject_id is not None:
        subject_scope = BodyScan.subject_id == subject_id
        if include_legacy_unowned:
            subject_scope = or_(subject_scope, BodyScan.subject_id.is_(None))
        stmt = stmt.where(subject_scope)
    if start is not None:
        stmt = stmt.where(BodyScan.date >= start)
    if end is not None:
        stmt = stmt.where(BodyScan.date <= end)
    stmt = stmt.order_by(BodyScan.date.desc(), BodyScan.id.desc())
    return (await session.execute(stmt)).scalars().all()


async def get_scan(
    session: AsyncSession,
    scan_id: int,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> Optional[BodyScan]:
    stmt = (
        select(BodyScan)
        .where(BodyScan.id == scan_id)
        .options(selectinload(BodyScan.metrics))
    )
    if subject_id is not None:
        subject_scope = BodyScan.subject_id == subject_id
        if include_legacy_unowned:
            subject_scope = or_(subject_scope, BodyScan.subject_id.is_(None))
        stmt = stmt.where(subject_scope)
    return (await session.execute(stmt)).scalar_one_or_none()


async def latest_scan(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> Optional[BodyScan]:
    stmt = (
        select(BodyScan)
        .options(selectinload(BodyScan.metrics))
        .order_by(BodyScan.date.desc(), BodyScan.id.desc())
        .limit(1)
    )
    if subject_id is not None:
        subject_scope = BodyScan.subject_id == subject_id
        if include_legacy_unowned:
            subject_scope = or_(subject_scope, BodyScan.subject_id.is_(None))
        stmt = stmt.where(subject_scope)
    return (await session.execute(stmt)).scalars().first()


async def metric_history(
    session: AsyncSession,
    metric_key: str,
    *,
    segment: Optional[str] = None,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
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
    stmt = stmt.order_by(BodyScan.date, BodyScanMetric.id)
    rows = (await session.execute(stmt)).all()
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


async def available_metrics(session: AsyncSession) -> list[dict]:
    """Distinct (metric_key, segment) pairs actually present across all scans,
    each with a display label and a stable ``value`` (``metric_key`` for
    whole-body rows, ``"metric_key:segment"`` for segmental rows) — the
    parameter picklist for the chart-builder catalog (analogous to
    ``hevy_service.exercise_catalog``)."""
    result = await session.execute(
        select(BodyScanMetric.metric_key, BodyScanMetric.segment)
        .distinct()
        .order_by(BodyScanMetric.metric_key, BodyScanMetric.segment)
    )
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


async def bia_chart_points(session: AsyncSession) -> dict:
    """BIA body-fat % and LBM series (latest scan per date) for the weight chart.
    Coexists with the Navy series — both are drawn."""
    scans = (
        await session.execute(
            select(BodyScan)
            .options(selectinload(BodyScan.metrics))
            .order_by(BodyScan.date, BodyScan.id)
        )
    ).scalars().all()

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


async def delete_scan(
    session: AsyncSession,
    scan_id: int,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> bool:
    """Delete a scan (cascades to its metrics). Returns False if not found.

    The bridged weight row is left as-is (it's an independent weight log); the
    owner can remove it from the weight tab if desired."""
    scan = await get_scan(
        session,
        scan_id,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
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
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> None:
    """Raise/clear passive ``info`` alerts from the latest scan: visceral fat above
    its printed range, or phase angle below its printed range. Idempotent. Each
    alert is bound to the triggering scan's id, so a dismissal sticks forever
    for that scan — only a newer scan can raise it again."""
    scan = await latest_scan(
        session,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    if scan is None:
        await alerts_service.resolve_superseded(session, alert_key=VISCERAL_ALERT_KEY, keep_entity=None)
        await alerts_service.resolve_superseded(session, alert_key=PHASE_ALERT_KEY, keep_entity=None)
        return

    entity = str(scan.id)
    await alerts_service.resolve_superseded(session, alert_key=VISCERAL_ALERT_KEY, keep_entity=entity)
    await alerts_service.resolve_superseded(session, alert_key=PHASE_ALERT_KEY, keep_entity=entity)

    by_key = {m.metric_key: m for m in scan.metrics}

    vfa = by_key.get("visceral_fat_area") or by_key.get("visceral_fat_level")
    if vfa is not None and vfa.ref_high is not None and vfa.value > vfa.ref_high:
        if not await alerts_service._was_ever_dismissed(session, VISCERAL_ALERT_KEY, entity):
            await alerts_service.raise_alert(
                session,
                domain=Domain.BODY_COMPOSITION.value,
                severity=Severity.INFO.value,
                message=t("alert.body_visceral_high", value=vfa.value, unit=((" " + vfa.unit) if vfa.unit else "")),
                alert_key=VISCERAL_ALERT_KEY,
                entity_ref=entity,
            )
    else:
        await alerts_service.resolve_by_key(session, alert_key=VISCERAL_ALERT_KEY, entity_ref=entity)

    phase = by_key.get("phase_angle")
    if phase is not None and phase.ref_low is not None and phase.value < phase.ref_low:
        if not await alerts_service._was_ever_dismissed(session, PHASE_ALERT_KEY, entity):
            await alerts_service.raise_alert(
                session,
                domain=Domain.BODY_COMPOSITION.value,
                severity=Severity.INFO.value,
                message=t("alert.body_phase_low", value=phase.value),
                alert_key=PHASE_ALERT_KEY,
                entity_ref=entity,
            )
    else:
        await alerts_service.resolve_by_key(session, alert_key=PHASE_ALERT_KEY, entity_ref=entity)


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
