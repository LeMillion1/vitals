"""Raw-first document extraction and ingestion for the Labs bounded context."""
from __future__ import annotations

import base64
import logging
import math
import uuid
from datetime import date as date_type, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    Source,
)
from vitals.models.ai import AIInvocation
from vitals.models.labs import DOMAIN, LabResult
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services.data_lake import sweep as raw_sweep
from vitals.services.conflicts import engine
from vitals.services.files.upload_references import resolve_owned_upload_reference
from vitals.utils.timeutils import now_local, today_local

from .alerts import refresh_alerts
from .flags import compute_flag
from .markers import _marker_for_update, normalize_marker, normalize_marker_key
from .results import (
    _VALUE_ABS_MAX,
    _lock_result_raw,
    _proposed_result,
    _require_evaluation_date,
    _require_scoped_prepared_write,
    _validate_parser_upload_chain,
    add_result,
)

logger = logging.getLogger(__name__)

async def _resolve_confirm_upload(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    raw_payload_id: int,
    file_key: str | None,
):
    upload = await resolve_owned_upload_reference(
        session,
        identity=identity,
        raw_payload_id=raw_payload_id,
        client_storage_ref=file_key,
        domain=DOMAIN,
        source=Source.LAB_PARSER.value,
        purpose=FileAssetPurpose.LAB_DOCUMENT,
    )
    await _validate_parser_upload_chain(
        session,
        raw=upload.raw_payload,
        asset=upload.file_asset,
        identity=identity,
        require_boundary_actor=True,
    )
    return upload


async def _resolve_replay_upload(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    raw_payload_id: int,
    file_key: str | None,
):
    upload = await resolve_owned_upload_reference(
        session,
        identity=identity,
        raw_payload_id=raw_payload_id,
        client_storage_ref=file_key,
        domain=DOMAIN,
        source=Source.LAB_PARSER.value,
        purpose=FileAssetPurpose.LAB_DOCUMENT,
    )
    await _validate_parser_upload_chain(
        session,
        raw=upload.raw_payload,
        asset=upload.file_asset,
        identity=identity,
        require_boundary_actor=False,
    )
    return upload




# ── LLM extraction (optional auto-fill) ───────────────────────────────────────
_EXTRACT_SYSTEM = (
    "You are a medical lab-report parser. Extract every marker from the provided "
    "lab document image. Respond ONLY with JSON of the form: "
    '{"date": "YYYY-MM-DD", "lab_name": string|null, "results": '
    '[{"marker": string, "value": number, "unit": string|null, '
    '"ref_low": number|null, "ref_high": number|null}]}. '
    "Use the collection date. Numbers must be plain (no ranges in value). "
    "If a field is unknown use null."
)


# Shared PDF→PNG rasteriser (kept under this name for the call below).
from vitals.integrations.vision import pdf_pages_png as _pdf_pages_png


async def extract_from_file(
    file_bytes: bytes,
    *,
    llm: Any,
    content_type: str = "image/jpeg",
    filename: Optional[str] = None,
) -> dict:
    """Send the document to a vision model and return the parsed structured dict.
    PDFs are rendered to images first (all pages up to a limit). Raises whatever
    the LLM client raises (e.g. ``LLMNotConfigured``) so the router can surface
    a clear message."""
    is_pdf = (content_type or "").lower() == "application/pdf" or (
        filename or ""
    ).lower().endswith(".pdf")

    if is_pdf:
        pages_png = _pdf_pages_png(file_bytes)
        image_urls = []
        for png_bytes in pages_png:
            b64 = base64.b64encode(png_bytes).decode("ascii")
            image_urls.append(f"data:image/png;base64,{b64}")

        return await llm.extract_json(
            "Extract all lab markers from this report.",
            system=_EXTRACT_SYSTEM,
            image_urls=image_urls,
        )
    else:
        if not (content_type or "").startswith("image/"):
            content_type = "image/jpeg"
        b64 = base64.b64encode(file_bytes).decode("ascii")
        image_url = f"data:{content_type};base64,{b64}"
        return await llm.extract_json(
            "Extract all lab markers from this report image.",
            system=_EXTRACT_SYSTEM,
            image_url=image_url,
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
    """Usage-aware variant used only by the platform AI dispatch adapter.

    Media conversion is intentionally identical to :func:`extract_from_file`,
    while the caller supplies the exact sealed model/output ceiling and receives
    the in-memory ``LLMCallResult`` needed for sanitized quota accounting.
    """

    is_pdf = (content_type or "").lower() == "application/pdf" or (
        filename or ""
    ).lower().endswith(".pdf")
    image_urls = prepare_file_for_extraction(
        file_bytes,
        content_type=content_type,
        filename=filename,
    )
    return await extract_prepared_file_with_usage(
        image_urls,
        llm=llm,
        model=model,
        max_tokens=max_tokens,
        is_document=is_pdf,
    )


def prepare_file_for_extraction(
    file_bytes: bytes,
    *,
    content_type: str = "image/jpeg",
    filename: Optional[str] = None,
) -> tuple[str, ...]:
    """Convert local document bytes before any paid provider dispatch.

    PDF rendering is local and can fail for malformed or encrypted documents.
    Platform-funded callers run this pure preprocessing phase before charging
    an AI invocation so a zero-network validation error cannot consume quota.
    The returned data URLs are memory-only and must never be persisted or
    logged.
    """

    is_pdf = (content_type or "").lower() == "application/pdf" or (
        filename or ""
    ).lower().endswith(".pdf")
    if is_pdf:
        return tuple(
            f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
            for png_bytes in _pdf_pages_png(file_bytes)
        )
    if not (content_type or "").startswith("image/"):
        content_type = "image/jpeg"
    b64 = base64.b64encode(file_bytes).decode("ascii")
    return (f"data:{content_type};base64,{b64}",)


async def extract_prepared_file_with_usage(
    image_urls: tuple[str, ...],
    *,
    llm: Any,
    model: str,
    max_tokens: int,
    is_document: bool = False,
):
    """Send a locally prepared document through one usage-aware AI call."""

    if not image_urls:
        raise ValueError("prepared lab document has no images")
    if len(image_urls) == 1 and not is_document:
        return await llm.extract_json_with_usage(
            "Extract all lab markers from this report image.",
            model=model,
            system=_EXTRACT_SYSTEM,
            image_url=image_urls[0],
            max_tokens=max_tokens,
        )
    return await llm.extract_json_with_usage(
        "Extract all lab markers from this report.",
        model=model,
        system=_EXTRACT_SYSTEM,
        image_urls=list(image_urls),
        max_tokens=max_tokens,
    )


def normalize_extracted(extracted: dict) -> list[dict]:
    """Pure: turn a raw vision dict into normalized, editable marker rows for the
    upload preview. Each row is ``{marker, value, unit, ref_low, ref_high}``.
    Unparseable rows (no marker / non-numeric value) are dropped."""
    rows: list[dict] = []
    for item in extracted.get("results") or []:
        marker = (item.get("marker") or "").strip()
        value = _num(item.get("value"))
        if not marker or value is None:
            continue
        rows.append({
            "marker": normalize_marker(marker),
            "value": value,
            "unit": item.get("unit"),
            "ref_low": _num(item.get("ref_low")),
            "ref_high": _num(item.get("ref_high")),
        })
    return rows


async def _preflight_scoped_panel(
    session: AsyncSession,
    *,
    markers: Sequence[dict],
    context: engine.ConflictWriteContext,
    override: bool,
) -> None:
    """Prove a batch has no hard blocker before its first normalized mutation."""

    if override:
        if context.identity.actor_user_id is None:
            raise engine.ConflictOverrideActorRequired(
                "conflict override requires an active human actor"
            )
        return
    proposed: list[dict[str, Any]] = []
    for item in markers:
        marker = normalize_marker((item.get("marker") or "").strip())
        value = _num(item.get("value"))
        if not marker or value is None:
            continue
        if not math.isfinite(value) or abs(value) > _VALUE_ABS_MAX:
            # A garbled row costs that row, not the document: the ingest loop
            # skips it, and there is nothing here for a conflict rule to judge.
            continue
        catalog = await _marker_for_update(
            session,
            marker,
            subject_id=context.identity.subject_id,
        )
        low = _num(item.get("ref_low"))
        high = _num(item.get("ref_high"))
        if low is None and catalog is not None:
            low = catalog.ref_low
        if high is None and catalog is not None:
            high = catalog.ref_high
        proposed.append(
            _proposed_result(
                marker=marker,
                value=value,
                flag=compute_flag(value, low, high),
            )
        )
    violations = await engine.evaluate_scoped(
        session,
        scope=context.scope,
        domain=Domain.LABS,
        proposed_state=proposed,
    )
    blocking = [violation for violation in violations if violation.is_blocking]
    if blocking:
        raise engine.ConflictBlocked(
            sorted(
                violations,
                key=lambda violation: (
                    violation.rule_id is None,
                    violation.rule_id or 0,
                ),
            )
        )


async def confirm_extracted(
    session: AsyncSession,
    *,
    on_date: date_type,
    markers: Sequence[dict],
    lab_name: Optional[str] = None,
    raw_payload_id: Optional[int] = None,
    file_key: Optional[str] = None,
    override: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> list[LabResult]:
    """Persist the owner-edited marker rows from the upload preview (step 2 of
    upload -> preview -> confirm). Marks the raw payload processed. Does not
    commit — mirrors :func:`ingest_extracted` but trusts the caller's edits
    instead of re-deriving from the raw vision dict, and never drops a row as a
    'duplicate' (the owner already reviewed it)."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    _require_evaluation_date(context, on_date)
    owned_raw: RawPayload | None = None
    if identity is not None and raw_payload_id is not None:
        upload = await _resolve_confirm_upload(
            session,
            identity=identity,
            raw_payload_id=raw_payload_id,
            file_key=file_key,
        )
        owned_raw = upload.raw_payload
    elif identity is not None:
        raise ValueError("owned lab upload confirmation requires a raw upload")

    await _preflight_scoped_panel(
        session,
        markers=markers,
        context=context,
        override=override,
    )

    created: list[LabResult] = []
    for item in markers:
        marker = (item.get("marker") or "").strip()
        value = _num(item.get("value"))
        if not marker or value is None:
            continue
        row = await add_result(
            session,
            on_date=on_date,
            marker=marker,
            value=value,
            unit=item.get("unit"),
            ref_low=_num(item.get("ref_low")),
            ref_high=_num(item.get("ref_high")),
            lab_name=lab_name,
            source=Source.LAB_PARSER.value,
            raw_payload_id=raw_payload_id,
            override=override,
            identity=identity,
            prepared_conflict_write=prepared_conflict_write,
        )
        created.append(row)

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
            await session.flush()

    return created


async def ingest_structured_results(
    session: AsyncSession,
    extracted: dict,
    *,
    raw_payload: RawPayload,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
    override: bool = False,
) -> dict:
    """Persist an MCP-authored structured panel with exact MCP provenance.

    The caller creates the raw row at its authenticated boundary. This service
    accepts only an exact-subject ``Source.MCP`` raw row without connection/file
    roots, then links every normalized result to that same immutable provenance.
    """

    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    on_date = _parse_date(extracted.get("date")) or context.evaluation_date
    _require_evaluation_date(context, on_date)
    if not isinstance(raw_payload, RawPayload) or raw_payload.id is None:
        raise engine.ConflictRawOwnershipError(
            "structured MCP labs require a persisted raw payload"
        )
    raw_row = await _lock_result_raw(
        session,
        raw_payload_id=raw_payload.id,
        context=context,
        source=Source.MCP.value,
        require_mcp_roots=True,
    )
    if raw_row.actor_user_id != identity.actor_user_id:
        raise engine.ConflictRawOwnershipError(
            "structured MCP raw actor does not match the prepared writer"
        )

    await _preflight_scoped_panel(
        session,
        markers=extracted.get("results") or [],
        context=context,
        override=override,
    )

    summary = {"created": 0, "skipped": 0, "results": []}
    for item in extracted.get("results") or []:
        marker = (item.get("marker") or "").strip()
        value = _num(item.get("value"))
        if not marker or value is None:
            summary["skipped"] += 1
            continue
        if await _result_exists(
            session,
            on_date,
            marker,
            value,
            subject_id=identity.subject_id,
        ):
            summary["skipped"] += 1
            continue
        row = await add_result(
            session,
            on_date=on_date,
            marker=marker,
            value=value,
            unit=item.get("unit"),
            ref_low=_num(item.get("ref_low")),
            ref_high=_num(item.get("ref_high")),
            lab_name=extracted.get("lab_name"),
            note=item.get("note"),
            source=Source.MCP.value,
            raw_payload_id=raw_row.id,
            override=override,
            identity=identity,
            prepared_conflict_write=prepared_conflict_write,
        )
        summary["results"].append(row)
        summary["created"] += 1

    raw_row.processed_at = now_local()
    await session.flush()
    return summary


async def ingest_extracted(
    session: AsyncSession,
    extracted: dict,
    *,
    file_key: Optional[str] = None,
    override: bool = False,
    identity: WriteIdentity,
    existing_raw_payload: RawPayload,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> dict:
    """Persist an extracted document: keep it raw, then create a result row per
    marker (deduping identical (date, marker, value)). Does not commit.

    Returns ``{"created": int, "skipped": int, "results": list[LabResult]}`` — the
    freshly created rows (already flushed, so ``.flag``/``.id`` are populated),
    handy for a caller that wants to report back exactly what was saved (e.g. the
    MCP batch tool) without a follow-up query."""
    on_date = _parse_date(extracted.get("date")) or today_local()
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    _require_evaluation_date(context, on_date)
    lab_name = extracted.get("lab_name")
    results = extracted.get("results") or []

    if existing_raw_payload is None:
        raise ValueError(
            "owned extraction requires an existing raw payload; create it at the "
            "boundary"
        )
    upload = await _resolve_replay_upload(
        session,
        identity=identity,
        raw_payload_id=existing_raw_payload.id,
        file_key=file_key,
    )
    raw_row = upload.raw_payload

    await _preflight_scoped_panel(
        session,
        markers=results,
        context=context,
        override=override,
    )

    summary = {"created": 0, "skipped": 0, "results": []}
    for item in results:
        marker = (item.get("marker") or "").strip()
        value = _num(item.get("value"))
        if not marker or value is None:
            summary["skipped"] += 1
            continue
        if not math.isfinite(value) or abs(value) > _VALUE_ABS_MAX:
            logger.warning(
                "Skipping unusable extracted marker: implausible value for %s",
                marker,
            )
            summary["skipped"] += 1
            continue
        if await _result_exists(
            session,
            on_date,
            marker,
            value,
            subject_id=identity.subject_id,
        ):
            summary["skipped"] += 1
            continue
        row = await add_result(
            session,
            on_date=on_date,
            marker=marker,
            value=value,
            unit=item.get("unit"),
            ref_low=_num(item.get("ref_low")),
            ref_high=_num(item.get("ref_high")),
            lab_name=lab_name,
            source=Source.LAB_PARSER.value,
            raw_payload_id=raw_row.id,
            override=override,
            identity=identity,
            prepared_conflict_write=prepared_conflict_write,
        )
        summary["results"].append(row)
        summary["created"] += 1

    raw_row.processed_at = now_local()
    await session.flush()
    return summary


async def reparse_owned_pending(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
    limit: int = raw_sweep.REPARSE_BATCH,
    since_days: int = raw_sweep.REPARSE_WINDOW_DAYS,
) -> int:
    """Replay pending parser raws inside one prevalidated subject boundary."""

    boundary = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    assert boundary is not None
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if (
        not isinstance(since_days, int)
        or isinstance(since_days, bool)
        or since_days < 0
    ):
        raise ValueError("since_days must be a non-negative integer")

    # The replay is the one reader labs keeps that can see a raw belonging to
    # nobody: adopting that payload into this subject's history is the whole
    # point of the sweep.
    raw_scope = or_(
        RawPayload.subject_id == identity.subject_id,
        and_(
            RawPayload.subject_id.is_(None),
            RawPayload.actor_user_id.is_(None),
            RawPayload.integration_connection_id.is_(None),
            RawPayload.file_asset_id.is_(None),
        ),
    )
    cutoff = now_local() - timedelta(days=since_days)
    allowed_result_scope = or_(
        LabResult.subject_id == identity.subject_id,
        and_(
            LabResult.subject_id.is_(None),
            LabResult.actor_user_id.is_(None),
        ),
    )
    invalid_link = await session.scalar(
        select(LabResult.id)
        .join(RawPayload, LabResult.raw_payload_id == RawPayload.id)
        .where(
            raw_scope,
            RawPayload.domain == DOMAIN,
            RawPayload.source == Source.LAB_PARSER.value,
            RawPayload.processed_at.is_(None),
            RawPayload.fetched_at >= cutoff,
            allowed_result_scope.is_not(True),
        )
        .limit(1)
    )
    if invalid_link is not None:
        raise engine.ConflictRawOwnershipError(
            "pending lab raw links to foreign or partial normalized provenance"
        )
    # One parser raw represents one atomic panel. Any permitted linked result
    # means that panel was already handled, including a pre-ownership legacy
    # result; replaying it would manufacture a second medical fact.
    has_normalized = (
        select(LabResult.id)
        .where(LabResult.raw_payload_id == RawPayload.id)
        .exists()
    )
    succeeded_platform_parse = (
        select(AIInvocation.id)
        .where(
            AIInvocation.subject_id == identity.subject_id,
            AIInvocation.actor_user_id == RawPayload.actor_user_id,
            AIInvocation.raw_payload_id == RawPayload.id,
            AIInvocation.purpose
            == AIInvocationPurpose.LAB_DOCUMENT_PARSE.value,
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
        # Failures remain pending for repair, but they do not consume the
        # successful-work limit. Keyset pagination guarantees that a full head
        # batch of corrupt historical rows cannot starve later valid panels.
        raw_ids = list(
            await session.scalars(
                select(RawPayload.id)
                .where(
                    raw_scope,
                    RawPayload.id > last_raw_id,
                    RawPayload.domain == DOMAIN,
                    RawPayload.source == Source.LAB_PARSER.value,
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
                    # This probe supplies only the preliminary date/identity
                    # needed for governance preparation. It intentionally takes
                    # no raw lock; the canonical C -> raw acquisition below
                    # refreshes and revalidates every value used to normalize.
                    probe = await session.scalar(
                        select(RawPayload)
                        .where(RawPayload.id == raw_id)
                        .execution_options(populate_existing=True)
                    )
                    if probe is None:
                        continue
                    probe_is_legacy = probe.subject_id is None
                    probe_is_historical_parser = (
                        probe.subject_id == identity.subject_id
                        and probe.actor_user_id is None
                        and probe.integration_connection_id is not None
                        and probe.file_asset_id is None
                        and probe.domain == DOMAIN
                        and probe.source == Source.LAB_PARSER.value
                    )
                    origin_identity = WriteIdentity(
                        identity.subject_id,
                        (
                            None
                            if probe_is_legacy or probe_is_historical_parser
                            else probe.actor_user_id
                        ),
                    )
                    probe_payload = (
                        probe.payload
                        if isinstance(probe.payload, dict)
                        else {}
                    )
                    prepared_date = (
                        _parse_date(probe_payload.get("date"))
                        or boundary.evaluation_date
                    )
                    row_context = engine.ConflictWriteContext(
                        identity=origin_identity,
                        evaluation_date=prepared_date,
                        # The replay decides adoption per raw, from the raw's
                        # own roots, rather than from a caller's flag.
                        legacy_bridge=(
                            engine.LegacyConflictBridge.FULLY_UNOWNED
                            if probe_is_legacy or probe_is_historical_parser
                            else engine.LegacyConflictBridge.REJECT
                        ),
                    )
                    prepared = await engine.prepare_scoped_write(
                        session,
                        context=row_context,
                    )
                    raw = await _lock_result_raw(
                        session,
                        raw_payload_id=raw_id,
                        context=row_context,
                        source=Source.LAB_PARSER.value,
                        allow_historical_parser_raw=probe_is_historical_parser,
                    )
                    if raw.processed_at is not None:
                        continue
                    existing_result_id = await session.scalar(
                        select(LabResult.id)
                        .where(LabResult.raw_payload_id == raw.id)
                        .order_by(LabResult.id)
                        .limit(1)
                        .with_for_update()
                    )
                    if existing_result_id is not None:
                        continue
                    extracted = raw.payload if isinstance(raw.payload, dict) else {}
                    on_date = (
                        _parse_date(extracted.get("date"))
                        or boundary.evaluation_date
                    )
                    if on_date != prepared_date:
                        raise engine.ConflictPreparedWriteError(
                            "lab parser date changed while acquiring provenance locks"
                        )
                    is_legacy = raw.subject_id is None
                    is_historical_parser = (
                        raw.subject_id == identity.subject_id
                        and raw.actor_user_id is None
                        and raw.integration_connection_id is not None
                        and raw.file_asset_id is None
                        and raw.domain == DOMAIN
                        and raw.source == Source.LAB_PARSER.value
                    )
                    locked_origin_identity = WriteIdentity(
                        identity.subject_id,
                        (
                            None
                            if is_legacy or is_historical_parser
                            else raw.actor_user_id
                        ),
                    )
                    if locked_origin_identity != origin_identity:
                        raise engine.ConflictRawOwnershipError(
                            "lab parser ownership changed while acquiring locks"
                        )
                    if is_legacy or is_historical_parser:
                        await _preflight_scoped_panel(
                            session,
                            markers=extracted.get("results") or [],
                            context=row_context,
                            override=False,
                        )
                        for item in extracted.get("results") or []:
                            marker = (item.get("marker") or "").strip()
                            value = _num(item.get("value"))
                            if not marker or value is None:
                                continue
                            await add_result(
                                session,
                                on_date=on_date,
                                marker=marker,
                                value=value,
                                unit=item.get("unit"),
                                ref_low=_num(item.get("ref_low")),
                                ref_high=_num(item.get("ref_high")),
                                lab_name=extracted.get("lab_name"),
                                source=Source.LAB_PARSER.value,
                                raw_payload_id=raw.id,
                                identity=origin_identity,
                                prepared_conflict_write=prepared,
                                allow_historical_parser_raw=(
                                    is_historical_parser
                                ),
                            )
                        # A fully-unowned historical parser raw has no
                        # authoritative provider/file roots to adopt. Keep the
                        # raw legacy and bridge only the normalized facts.
                    else:
                        await ingest_extracted(
                            session,
                            extracted,
                            file_key=raw.external_id,
                            identity=origin_identity,
                            existing_raw_payload=raw,
                            prepared_conflict_write=prepared,
                        )
                    await refresh_alerts(
                        session,
                        subject_id=identity.subject_id,
                        identity=origin_identity,
                        prepared_conflict_write=prepared,
                    )
                    raw.processed_at = now_local()
                    await session.flush()
            except Exception:
                logger.warning(
                    "owned Labs re-parse failed for raw payload %s",
                    raw_id,
                    exc_info=True,
                )
                continue
            done += 1
            if done >= limit:
                break
    return done


async def _result_exists(
    session: AsyncSession,
    on_date: date_type,
    marker: str,
    value: float,
    *,
    subject_id: uuid.UUID,
) -> bool:
    marker_key = normalize_marker_key(marker)
    stmt = select(LabResult.id).where(
        LabResult.date == on_date,
        LabResult.marker_key == marker_key,
        LabResult.value == value,
    )
    if subject_id is not None:
        stmt = stmt.where(LabResult.subject_id == subject_id)
    result = await session.execute(stmt)
    return result.first() is not None


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
