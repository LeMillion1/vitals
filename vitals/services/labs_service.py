"""Lab results & parser service (module 7).

Owns the labs domain:

  * **Manual entry & CRUD** — add a marker value with its reference range; the
    out-of-range ``flag`` is computed (pure :func:`compute_flag`).
  * **Marker catalog** — a row per marker is auto-created on first sight, holding
    the importance ``tier``, an optional retest interval, and the ``defer_until``
    used by "Defer Retest".
  * **History** — per-marker series for the charts.
  * **Alerts** — the latest value per marker drives an out-of-range alert
    (``info`` for a deferrable tier-2 low/high, ``warn`` for a tier-1 or critical
    value); overdue retests raise a passive ``info`` (suppressed while deferred).
  * **LLM extraction** — a PDF/image upload is turned into structured results by
    an OpenRouter vision model (the document is also kept raw). The LLM client is
    injected so the parser is unit-tested without network or a key.

Extraction is *optional* — every result can be entered manually, so the module
works with no LLM configured. :func:`add_result` runs the value through the
conflict engine's ``lab_safety`` rules, so logging a high potassium result while
a potassium supplement is active surfaces immediately. It uses the same
``enforce()``/override flow as every other write path rather than a quieter
read-only check: a hard rule stops the save and hands back the violation, and the
caller decides — the web form and the MCP tools both offer "save anyway".
"""
from __future__ import annotations

import base64
import logging
import math
import uuid
from datetime import date as date_type, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, FileAssetPurpose, LabFlag, Severity, Source
from vitals.i18n import t
from vitals.models.labs import DOMAIN, LabMarker, LabResult
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services import alerts_service, conflict_engine, raw_payload_service
from vitals.services.upload_ownership_service import resolve_owned_upload_reference
from vitals.utils.timeutils import now_local, today_local

logger = logging.getLogger(__name__)

OUT_OF_RANGE_KEY = "labs.out_of_range"
RETEST_DUE_KEY = "labs.retest_due"

# Sanity ceiling for a written value. The write path is reachable over MCP (an
# LLM) and from vision extraction, neither of which goes through the HTML form,
# so nonsense has to be stopped here. Real markers span many orders of magnitude
# (ng/ml up to cells per µl) and some are legitimately negative (blood-gas base
# excess), so this only catches the absurd — it is not a per-marker range.
_VALUE_ABS_MAX = 1_000_000.0

# "Critical" thresholds. For a two-sided range, a value more than this fraction of
# the range's *width* beyond a bound is critical (scales sensibly with the range).
# For a one-sided range (only one bound known) we fall back to a relative margin
# off that bound.
CRITICAL_WIDTH_FACTOR = 0.5
CRITICAL_MARGIN = 0.30


# ── Pure flag logic ───────────────────────────────────────────────────────────
def compute_flag(
    value: float,
    ref_low: Optional[float],
    ref_high: Optional[float],
    *,
    width_factor: float = CRITICAL_WIDTH_FACTOR,
    critical_margin: float = CRITICAL_MARGIN,
) -> Optional[str]:
    """Classify ``value`` against its reference range. Returns a ``LabFlag`` value,
    or ``None`` when no range is known. Either bound may be absent (one-sided
    ranges like "LDL < 3.0"). "Critical" scales with the range width for a
    two-sided range, else with a relative margin off the known bound."""
    if ref_low is None and ref_high is None:
        return None
    width = (ref_high - ref_low) if (ref_low is not None and ref_high is not None) else None

    if ref_low is not None and value < ref_low:
        critical = (
            value < ref_low - width_factor * width
            if width is not None
            else value <= ref_low * (1 - critical_margin)
        )
        return LabFlag.CRITICAL_LOW.value if critical else LabFlag.LOW.value

    if ref_high is not None and value > ref_high:
        critical = (
            value > ref_high + width_factor * width
            if width is not None
            else value >= ref_high * (1 + critical_margin)
        )
        return LabFlag.CRITICAL_HIGH.value if critical else LabFlag.HIGH.value

    return LabFlag.NORMAL.value


def is_out_of_range(flag: Optional[str]) -> bool:
    return flag in (
        LabFlag.LOW.value,
        LabFlag.HIGH.value,
        LabFlag.CRITICAL_LOW.value,
        LabFlag.CRITICAL_HIGH.value,
    )


def _is_critical(flag: Optional[str]) -> bool:
    return flag in (LabFlag.CRITICAL_LOW.value, LabFlag.CRITICAL_HIGH.value)


# ── Marker name normalization ──────────────────────────────────────────────────
MARKER_ALIASES = {
    "определение иммунореактивного инсулина": "Инсулин",
    "определение тиреотропина, тиротропина, тиреоидного гормона (ттг)": "ТТГ",
    "тиреотропный гормон (ттг)": "ТТГ",
    "определение свободного тироксина (т4)": "Т4 свободный",
    "исследование антител к тиреоглобулину (ат-тг)": "АТ-ТГ",
    "исследование антител к тиреоидной пероксидазе (ат-тпо)": "АТ-ТПО",
    "определение холестерина общего": "Холестерин общий",
    "холестерин": "Холестерин общий",
    "определение триглицеридов общих": "Триглицериды",
    "определение липопротеинов высокой плотности (лпвп-альфа)": "Холестерин-ЛПВП",
    "холестерин липопротеидов низкой плотности (лпнп, ldl)": "Холестерин-ЛПНП",
    "холестерин-лпнп": "Холестерин-ЛПНП",
    "определение липопротеинов низкой плотности (лпнп-бета)": "Холестерин-ЛПНП",
    "холестерин-лпонп": "Холестерин-ЛПОНП",
    "определение липопротеинов очень низкой плотности (лпонп), пребета-лп": "Холестерин-ЛПОНП",
    "определение аланинаминотрансферазы (алт)": "АЛТ",
    "аланинаминотрансфераза (алт)": "АЛТ",
    "определение аспартатаминотрансферазы (аст)": "АСТ",
    "аспартатаминотрансфераза (аст)": "АСТ",
    "определение глюкозы": "Глюкоза",
    "глюкоза плазмы": "Глюкоза",
    "глюкоза полуколичественно": "Глюкоза",
    "определение гемоглобина a1c (гликированный гемоглобин)": "Гликированный гемоглобин (HbA1c)",
    "hba1c (гликированный гемоглобин)": "Гликированный гемоглобин (HbA1c)",
    "гемоглобин общий": "Гемоглобин",
    "количество эритроцитов": "Эритроциты",
    "средний объем эритроцита": "Средний объем эритроцитов",
    "средний объем эритроцитов (mcv)": "Средний объем эритроцитов",
    "среднее содержание hb в эритроците": "Среднее содержание гемоглобина в эритроците",
    "среднее содержание гемоглобина в эритроците": "Среднее содержание гемоглобина в эритроците",
    "среднее содержание гемоглобина в эритроците (mch)": "Среднее содержание гемоглобина в эритроците",
    "средняя концентрация гемоглобина в эритроците": "Средняя концентрация гемоглобина в эритроците",
    "средняя концентрация hb в эритроците (mchc)": "Средняя концентрация гемоглобина в эритроците",
    "ширина распределения эритроцитов по объему": "Гетерогенность эритроцитов по объему",
    "гетерогенность эритроцитов по объёму": "Гетерогенность эритроцитов по объему",
    "количество тромбоцитов": "Тромбоциты",
    "средний объем тромбоцитов в крови": "Средний объем тромбоцитов",
    "средний объем тромбоцитов (mpv)": "Средний объем тромбоцитов",
    "ширина распределения тромбоцитов по объему": "Гетерогенность тромбоцитов по объему",
    "гетерогенность тромбоцитов по объёму": "Гетерогенность тромбоцитов по объему",
    "отн.ширина распред.тромбоцитов по объему (pdw)": "Гетерогенность тромбоцитов по объему",
    "общий объем тромбоцитов в крови (тромбокрит, pct)": "Тромбокрит",
    "тромбокрит (pct)": "Тромбокрит",
    "количество лейкоцитов": "Лейкоциты",
    "абсолютное количество нейтрофилов": "Нейтрофилы",
    "нейтрофилы сегментоядерные": "Нейтрофилы",
    "нейтрофилы (общее число), %": "Нейтрофилы %",
    "абсолютное количество эозинофилов": "Эозинофилы",
    "эозинофилы %": "Эозинофилы %",
    "абсолютное количество базофилов": "Базофилы",
    "базофилы %": "Базофилы %",
    "абсолютное количество моноцитов": "Моноциты",
    "моноциты %": "Моноциты %",
    "абсолютное количество лимфоцитов": "Лимфоциты",
    "лимфоциты (общее число), %": "Лимфоциты %",
    "лимфоциты %": "Лимфоциты %",
    "скорость оседания эритроцитов (по вестергрену)": "СОЭ",
    "определение кальция общего": "Кальций общий",
    "определение альбумина": "Альбумин",
    "определение кортизола": "Кортизол",
    "исследование пролактина (прл)": "Пролактин",
    "25-он витамин d, ихла, суммарный (кальциферол)": "25-ОН витамин D",
}

def normalize_marker(name: str) -> str:
    """Standardize spelling, casing and known synonym names of a marker."""
    cleaned = name.strip()
    if not cleaned:
        return ""
    # Normalize ё -> е for spelling consistency
    lowered = cleaned.lower().replace("ё", "е")
    if lowered in MARKER_ALIASES:
        return MARKER_ALIASES[lowered]
    # Fallback: capitalize first character, keep the rest
    return cleaned[0].upper() + cleaned[1:]


# ── Marker catalog ────────────────────────────────────────────────────────────
async def get_marker(
    session: AsyncSession,
    name: str,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> Optional[LabMarker]:
    name = normalize_marker(name)
    stmt = select(LabMarker).where(LabMarker.name == name)
    if subject_id is not None:
        subject_scope = LabMarker.subject_id == subject_id
        if include_legacy_unowned:
            subject_scope = or_(subject_scope, LabMarker.subject_id.is_(None))
        stmt = stmt.where(subject_scope)
    result = await session.execute(stmt)
    return result.scalars().first()


async def _ensure_marker(
    session: AsyncSession,
    name: str,
    *,
    unit: Optional[str] = None,
    ref_low: Optional[float] = None,
    ref_high: Optional[float] = None,
    identity: WriteIdentity | None = None,
) -> LabMarker:
    """Auto-create a catalog row on first sight; backfill null defaults but never
    clobber a tier/defer the user has set."""
    name = normalize_marker(name)
    marker = await get_marker(
        session,
        name,
        subject_id=identity.subject_id if identity is not None else None,
    )
    if marker is None and identity is not None:
        # During expand/contract, adopt only the sole unowned catalog row.  A
        # marker already assigned to another subject is never reused as a
        # cross-subject catalog entry (the later scoped-key migration removes
        # the old global uniqueness constraint).
        marker = await session.scalar(
            select(LabMarker)
            .where(LabMarker.name == name, LabMarker.subject_id.is_(None))
            .with_for_update()
        )
        if marker is not None:
            marker.subject_id = identity.subject_id
        elif await session.scalar(
            select(LabMarker.id).where(LabMarker.name == name).limit(1)
        ) is not None:
            raise ValueError("lab marker belongs to another subject")
    if marker is None:
        marker = LabMarker(
            subject_id=identity.subject_id if identity is not None else None,
            actor_user_id=identity.actor_user_id if identity is not None else None,
            domain=DOMAIN,
            name=name,
            unit=unit,
            ref_low=ref_low,
            ref_high=ref_high,
        )
        session.add(marker)
        await session.flush()
        return marker
    if marker.unit is None and unit is not None:
        marker.unit = unit
    if marker.ref_low is None and ref_low is not None:
        marker.ref_low = ref_low
    if marker.ref_high is None and ref_high is not None:
        marker.ref_high = ref_high
    await session.flush()
    return marker


async def list_markers(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> Sequence[LabMarker]:
    stmt = select(LabMarker)
    if subject_id is not None:
        subject_scope = LabMarker.subject_id == subject_id
        if include_legacy_unowned:
            subject_scope = or_(subject_scope, LabMarker.subject_id.is_(None))
        stmt = stmt.where(subject_scope)
    result = await session.execute(stmt.order_by(LabMarker.name))
    return result.scalars().all()


async def defer_retest(
    session: AsyncSession,
    marker: str,
    *,
    until: date_type,
    note: Optional[str] = None,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> Optional[LabMarker]:
    """Pause the overdue-retest alert for a marker until ``until``."""
    marker = normalize_marker(marker)
    row = await get_marker(
        session,
        marker,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    if row is None:
        return None
    row.defer_until = until
    if note is not None:
        row.note = note
    await session.flush()
    await alerts_service.resolve_by_key(
        session, alert_key=RETEST_DUE_KEY, entity_ref=marker
    )
    return row


# ── Results ───────────────────────────────────────────────────────────────────
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
    identity: WriteIdentity | None = None,
) -> LabResult:
    """Record a marker value, computing its flag and ensuring its catalog row.

    If the result carries no range, fall back to the catalog's default range so a
    flag can still be computed. Raises ``ValueError`` on a nameless marker or an
    implausible value, and :class:`ConflictBlocked` when a hard cross-domain rule
    fires without ``override``."""
    marker = normalize_marker(marker)
    if not marker:
        raise ValueError("marker is required")
    if value is None or not math.isfinite(value) or abs(value) > _VALUE_ABS_MAX:
        raise ValueError(f"implausible lab value for {marker}: {value!r}")
    catalog = await _ensure_marker(
        session,
        marker,
        unit=unit,
        ref_low=ref_low,
        ref_high=ref_high,
        identity=identity,
    )
    eff_low = ref_low if ref_low is not None else catalog.ref_low
    eff_high = ref_high if ref_high is not None else catalog.ref_high
    flag = compute_flag(value, eff_low, eff_high)

    # The same gate every other write path runs, before the row exists: a hard
    # rule (an active potassium supplement meeting a hyperkalemic potassium
    # result) stops the save unless the caller overrides, and soft rules keep
    # doing what they did — an alert, never a block.
    await conflict_engine.enforce(
        session,
        Domain.LABS.value,
        {"marker": marker, "value": value, "flag": flag},
        override=override,
        entity_ref=f"labs:{marker}",
    )

    row = LabResult(
        subject_id=identity.subject_id if identity is not None else None,
        actor_user_id=identity.actor_user_id if identity is not None else None,
        date=on_date,
        domain=DOMAIN,
        source=source,
        marker=marker,
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
) -> Optional[LabResult]:
    """Correct an existing result — a mistyped value or a range read off the wrong
    column. Only the fields passed are changed; ``flag`` is recomputed from the
    resulting value + range, and the alerts derived from it are refreshed.

    Without this, fixing a typo meant deleting the row and re-adding it, which is
    the one thing this project promises never to do to a measurement."""
    row = await session.get(LabResult, result_id)
    if row is None:
        return None

    if marker is not None:
        normalized = normalize_marker(marker)
        if not normalized:
            raise ValueError("marker is required")
        row.marker = normalized
    if value is not None:
        if not math.isfinite(value) or abs(value) > _VALUE_ABS_MAX:
            raise ValueError(f"implausible lab value for {row.marker}: {value!r}")
        row.value = value
    if on_date is not None:
        row.date = on_date
    if unit is not None:
        row.unit = unit
    if ref_low is not None:
        row.ref_low = ref_low
    if ref_high is not None:
        row.ref_high = ref_high
    if lab_name is not None:
        row.lab_name = lab_name
    if note is not None:
        row.note = note

    catalog = await _ensure_marker(
        session, row.marker, unit=row.unit, ref_low=row.ref_low, ref_high=row.ref_high
    )
    if row.ref_low is None:
        row.ref_low = catalog.ref_low
    if row.ref_high is None:
        row.ref_high = catalog.ref_high
    row.flag = compute_flag(row.value, row.ref_low, row.ref_high)

    await session.flush()
    await refresh_alerts(session)
    return row


async def list_results(
    session: AsyncSession,
    *,
    marker: Optional[str] = None,
    end: Optional[date_type] = None,
    limit: int = 200,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> Sequence[LabResult]:
    """Newest first. ``end`` anchors the read at a date instead of at "now", so a
    report about a past window is not filled by results drawn after it."""
    stmt = select(LabResult)
    if subject_id is not None:
        subject_scope = LabResult.subject_id == subject_id
        if include_legacy_unowned:
            subject_scope = or_(subject_scope, LabResult.subject_id.is_(None))
        stmt = stmt.where(subject_scope)
    if marker is not None:
        marker = normalize_marker(marker)
        stmt = stmt.where(LabResult.marker == marker)
    if end is not None:
        stmt = stmt.where(LabResult.date <= end)
    stmt = stmt.order_by(LabResult.date.desc(), LabResult.id.desc()).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def marker_history(
    session: AsyncSession,
    marker: str,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> list[dict]:
    """Chronological series for one marker (the per-marker chart)."""
    marker = normalize_marker(marker)
    stmt = select(LabResult).where(LabResult.marker == marker)
    if subject_id is not None:
        subject_scope = LabResult.subject_id == subject_id
        if include_legacy_unowned:
            subject_scope = or_(subject_scope, LabResult.subject_id.is_(None))
        stmt = stmt.where(subject_scope)
    result = await session.execute(stmt.order_by(LabResult.date))
    return [
        {
            "date": r.date.isoformat(),
            "value": r.value,
            "flag": r.flag,
            "ref_low": r.ref_low,
            "ref_high": r.ref_high,
        }
        for r in result.scalars().all()
    ]


async def latest_per_marker(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> list[LabResult]:
    """The most recent result for each marker (table + alert source)."""
    stmt = select(LabResult)
    if subject_id is not None:
        subject_scope = LabResult.subject_id == subject_id
        if include_legacy_unowned:
            subject_scope = or_(subject_scope, LabResult.subject_id.is_(None))
        stmt = stmt.where(subject_scope)
    result = await session.execute(
        stmt.order_by(LabResult.date.desc(), LabResult.id.desc())
    )
    seen: dict[str, LabResult] = {}
    for r in result.scalars().all():
        seen.setdefault(r.marker, r)
    return list(seen.values())


async def resolve_latest(session: AsyncSession) -> list[dict]:
    """Conflict-engine resolver: the latest value+flag per marker as match items
    — lets a lab_safety rule reference e.g. {"marker": "Калий", "value": {"$gt":
    5.0}} against the current panel, not just a freshly logged result."""
    latest = await latest_per_marker(session)
    return [{"marker": r.marker, "value": r.value, "flag": r.flag} for r in latest]


async def delete_result(
    session: AsyncSession,
    result_id: int,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> bool:
    stmt = select(LabResult).where(LabResult.id == result_id)
    if subject_id is not None:
        subject_scope = LabResult.subject_id == subject_id
        if include_legacy_unowned:
            subject_scope = or_(subject_scope, LabResult.subject_id.is_(None))
        stmt = stmt.where(subject_scope)
    row = await session.scalar(stmt)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


# ── Alerts ────────────────────────────────────────────────────────────────────
async def refresh_alerts(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> None:
    """Raise/clear out-of-range + overdue-retest alerts from the latest values.
    Idempotent — safe on every dashboard load / scheduler tick. Each alert is
    bound to the specific LabResult row that triggered it (``entity_ref =
    f"{marker}:{result_id}"``), so a dismissal sticks forever for that row —
    only a new result for the marker can raise it again."""
    today = on_date or today_local()
    latest = await latest_per_marker(
        session,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    markers = {
        m.name: m
        for m in await list_markers(
            session,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )
    }

    for r in latest:
        key = OUT_OF_RANGE_KEY
        entity = f"{r.marker}:{r.id}"
        await alerts_service.resolve_superseded(session, alert_key=key, marker=r.marker, keep_entity=entity)
        if is_out_of_range(r.flag):
            if await alerts_service._was_ever_dismissed(session, key, entity):
                continue
            tier = markers.get(r.marker).tier if markers.get(r.marker) else 2
            critical = _is_critical(r.flag) or tier == 1
            severity = Severity.WARN.value if critical else Severity.INFO.value
            await alerts_service.raise_alert(
                session,
                domain=Domain.LABS.value,
                severity=severity,
                message=t(
                    "alert.lab_out_of_range",
                    marker=r.marker,
                    value=r.value,
                    unit=(' ' + r.unit) if r.unit else '',
                    # Localized flag label ("crit. high"), not the raw enum value.
                    flag=t(f"enum.flag.{r.flag}"),
                ),
                alert_key=key,
                entity_ref=entity,
            )
        else:
            await alerts_service.resolve_by_key(session, alert_key=key, entity_ref=entity)

        # Overdue retest (respecting a deferral) — bound to the same result row.
        marker_row = markers.get(r.marker)
        if marker_row and marker_row.retest_interval_days:
            due = r.date + timedelta(days=marker_row.retest_interval_days)
            deferred = marker_row.defer_until is not None and marker_row.defer_until >= today
            await alerts_service.resolve_superseded(
                session, alert_key=RETEST_DUE_KEY, marker=r.marker, keep_entity=entity
            )
            if today > due and not deferred:
                if await alerts_service._was_ever_dismissed(session, RETEST_DUE_KEY, entity):
                    continue
                await alerts_service.raise_alert(
                    session,
                    domain=Domain.LABS.value,
                    severity=Severity.INFO.value,
                    message=t("alert.lab_retest", marker=r.marker, date=r.date),
                    alert_key=RETEST_DUE_KEY,
                    entity_ref=entity,
                )
            else:
                await alerts_service.resolve_by_key(
                    session, alert_key=RETEST_DUE_KEY, entity_ref=entity
                )


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


async def confirm_extracted(
    session: AsyncSession,
    *,
    on_date: date_type,
    markers: Sequence[dict],
    lab_name: Optional[str] = None,
    raw_payload_id: Optional[int] = None,
    file_key: Optional[str] = None,
    override: bool = False,
    identity: WriteIdentity | None = None,
) -> list[LabResult]:
    """Persist the owner-edited marker rows from the upload preview (step 2 of
    upload -> preview -> confirm). Marks the raw payload processed. Does not
    commit — mirrors :func:`ingest_extracted` but trusts the caller's edits
    instead of re-deriving from the raw vision dict, and never drops a row as a
    'duplicate' (the owner already reviewed it)."""
    owned_raw: RawPayload | None = None
    if identity is not None and raw_payload_id is not None:
        upload = await resolve_owned_upload_reference(
            session,
            identity=identity,
            raw_payload_id=raw_payload_id,
            client_storage_ref=file_key,
            domain=DOMAIN,
            source=Source.LAB_PARSER.value,
            purpose=FileAssetPurpose.LAB_DOCUMENT,
        )
        owned_raw = upload.raw_payload
    elif identity is not None and file_key is not None:
        raise ValueError("owned lab file reference requires a raw upload")

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
        )
        created.append(row)

    if raw_payload_id is not None:
        raw = owned_raw or await session.get(RawPayload, raw_payload_id)
        if raw is not None:
            raw.processed_at = now_local()

    return created


async def ingest_extracted(
    session: AsyncSession,
    extracted: dict,
    *,
    file_key: Optional[str] = None,
    override: bool = False,
    identity: WriteIdentity | None = None,
    existing_raw_payload: RawPayload | None = None,
) -> dict:
    """Persist an extracted document: keep it raw, then create a result row per
    marker (deduping identical (date, marker, value)). Does not commit.

    Returns ``{"created": int, "skipped": int, "results": list[LabResult]}`` — the
    freshly created rows (already flushed, so ``.flag``/``.id`` are populated),
    handy for a caller that wants to report back exactly what was saved (e.g. the
    MCP batch tool) without a follow-up query."""
    on_date = _parse_date(extracted.get("date")) or today_local()
    lab_name = extracted.get("lab_name")
    results = extracted.get("results") or []

    if existing_raw_payload is not None:
        if identity is None:
            raise ValueError("existing owned raw payload requires a write identity")
        upload = await resolve_owned_upload_reference(
            session,
            identity=identity,
            raw_payload_id=existing_raw_payload.id,
            client_storage_ref=file_key,
            domain=DOMAIN,
            source=Source.LAB_PARSER.value,
            purpose=FileAssetPurpose.LAB_DOCUMENT,
        )
        raw_row = upload.raw_payload
    elif identity is not None:
        raise ValueError(
            "owned extraction requires an existing raw payload; create it at the boundary"
        )
    else:
        raw_row = await raw_payload_service.upsert_raw_payload(
            session,
            domain=DOMAIN,
            source=Source.LAB_PARSER.value,
            external_id=file_key or f"lab:{on_date.isoformat()}:{lab_name or '?'}",
            payload=extracted,
        )

    summary = {"created": 0, "skipped": 0, "results": []}
    for item in results:
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
            subject_id=identity.subject_id if identity is not None else None,
        ):
            summary["skipped"] += 1
            continue
        try:
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
            )
        except ValueError as e:
            # One garbled row must not cost the whole document — it stays in the
            # raw payload either way, so it can be re-parsed later.
            logger.warning("Skipping unusable extracted marker: %s", e)
            summary["skipped"] += 1
            continue
        summary["results"].append(row)
        summary["created"] += 1

    raw_row.processed_at = now_local()
    return summary


async def reparse_from_raw(session: AsyncSession, raw_row: RawPayload) -> None:
    """Re-run extraction ingest against a lab payload already on disk — no new
    upload. Covers uploads the owner never confirmed (extracted but abandoned
    at the preview step), so those markers aren't lost. Reuses
    :func:`ingest_extracted` (dedupes by date+marker+value, so this is safe even
    if some rows were confirmed by hand before the sweep got to it). Preserves
    ``fetched_at``: this is a reparse, not a new upload. Used by
    :func:`reparse_pending` (the nightly sweep — raw_payload_service.
    sweep_pending_job)."""
    extracted = raw_row.payload if isinstance(raw_row.payload, dict) else {}
    original_fetched_at = raw_row.fetched_at
    if raw_row.subject_id is not None and raw_row.file_asset_id is not None:
        await ingest_extracted(
            session,
            extracted,
            file_key=raw_row.external_id,
            identity=WriteIdentity(raw_row.subject_id, raw_row.actor_user_id),
            existing_raw_payload=raw_row,
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
    """Sweep lab raw payloads (extractions never confirmed by the owner) still
    pending a normalized row. Does not commit."""
    has_normalized = (
        select(LabResult.id).where(LabResult.raw_payload_id == RawPayload.id).exists()
    )
    return await raw_payload_service.sweep_domain(
        session,
        domain=DOMAIN,
        reparse=reparse_from_raw,
        has_normalized=has_normalized,
        limit=limit,
        since_days=since_days,
    )


async def _result_exists(
    session: AsyncSession,
    on_date: date_type,
    marker: str,
    value: float,
    *,
    subject_id: uuid.UUID | None = None,
) -> bool:
    stmt = select(LabResult.id).where(
        LabResult.date == on_date,
        LabResult.marker == marker,
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
