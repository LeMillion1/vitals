"""Body-scan document extraction and pure metric normalization."""

from __future__ import annotations

from datetime import date as date_type
from typing import Any, Optional

from vitals.analytics.body_metrics import (
    CAT_OTHER,
    METRIC_REGISTRY,
    canonical_segment,
    display_name,
    normalize_metric,
)
from vitals.integrations.vision import file_to_image_urls

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
