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
from dataclasses import dataclass
from datetime import date as date_type, timedelta
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
    LabFlag,
    Severity,
    Source,
)
from vitals.i18n import t
from vitals.models.ai import AIInvocation
from vitals.models.identity import HealthSubject
from vitals.models.labs import DOMAIN, LabMarker, LabResult
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
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


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: conflict_engine.PreparedConflictWrite,
) -> conflict_engine.ConflictWriteContext:
    """Prove the write names a subject and the decision that authorized it."""

    if identity is None or prepared is None:
        raise conflict_engine.ConflictPreparedWriteError(
            "scoped lab writes require identity and a prepared conflict write"
        )
    return conflict_engine.require_prepared_identity(
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
            "lab result date does not match prepared conflict evaluation date"
        )


def _subject_scope(model, subject_id: uuid.UUID):
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


def _result_entity_key(result_id: int) -> str:
    return str(result_id)


def _proposed_result(
    *, marker: str, value: float, flag: str | None, result_id: int | None = None
) -> dict[str, Any]:
    proposed: dict[str, Any] = {"marker": marker, "value": value, "flag": flag}
    if result_id is not None:
        proposed[conflict_engine.CONFLICT_ENTITY_KEY] = _result_entity_key(result_id)
    return proposed


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
    subject_id: uuid.UUID,
) -> Optional[LabMarker]:
    name = normalize_marker(name)
    stmt = select(LabMarker).where(LabMarker.name == name)
    stmt = stmt.where(_subject_scope(LabMarker, subject_id))
    result = await session.execute(stmt)
    return result.scalars().first()


async def _marker_for_update(
    session: AsyncSession,
    name: str,
    *,
    subject_id: uuid.UUID,
) -> LabMarker | None:
    name = normalize_marker(name)
    stmt = select(LabMarker).where(LabMarker.name == name)
    stmt = stmt.where(_subject_scope(LabMarker, subject_id))
    marker = await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )
    return marker


def _apply_marker_defaults(
    marker: LabMarker,
    *,
    unit: Optional[str],
    ref_low: Optional[float],
    ref_high: Optional[float],
) -> None:
    """Backfill null defaults without clobbering user catalog settings."""

    if marker.unit is None and unit is not None:
        marker.unit = unit
    if marker.ref_low is None and ref_low is not None:
        marker.ref_low = ref_low
    if marker.ref_high is None and ref_high is not None:
        marker.ref_high = ref_high


async def _ensure_marker(
    session: AsyncSession,
    name: str,
    *,
    unit: Optional[str] = None,
    ref_low: Optional[float] = None,
    ref_high: Optional[float] = None,
    identity: WriteIdentity,
) -> LabMarker:
    """Compatibility helper for non-conflicting catalog-only callers."""

    marker = await _marker_for_update(
        session,
        name,
        subject_id=identity.subject_id,
    )
    if marker is None:
        marker = LabMarker(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            domain=DOMAIN,
            name=normalize_marker(name),
        )
        session.add(marker)
    _apply_marker_defaults(
        marker,
        unit=unit,
        ref_low=ref_low,
        ref_high=ref_high,
    )
    await session.flush()
    return marker


async def list_markers(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> Sequence[LabMarker]:
    stmt = select(LabMarker)
    stmt = stmt.where(_subject_scope(LabMarker, subject_id))
    result = await session.execute(stmt.order_by(LabMarker.name))
    return result.scalars().all()


async def ensure_marker_catalog_entry(
    session: AsyncSession,
    *,
    name: str,
    category: str | None = None,
    retest_interval_days: int | None = None,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> tuple[LabMarker, bool, bool]:
    """Create or backfill one scoped catalog row for startup/domain seeds.

    Existing non-null user configuration is never overwritten. The booleans are
    ``(created, updated)``; compatibility adoption counts as an update while the
    unknown historical actor remains unchanged.
    """

    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    normalized = normalize_marker(name)
    if not normalized:
        raise ValueError("marker is required")
    if retest_interval_days is not None and retest_interval_days < 1:
        raise ValueError("retest_interval_days must be positive")
    row = await _marker_for_update(
        session,
        normalized,
        subject_id=identity.subject_id,
    )
    created = row is None
    updated = False
    if row is None:
        row = LabMarker(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            domain=DOMAIN,
            name=normalized,
            category=category,
            retest_interval_days=retest_interval_days,
        )
        session.add(row)
    else:
        if row.subject_id is None:
            row.subject_id = identity.subject_id
            updated = True
        if row.category is None and category is not None:
            row.category = category
            updated = True
        if row.retest_interval_days is None and retest_interval_days is not None:
            row.retest_interval_days = retest_interval_days
            updated = True
    await session.flush()
    return row, created, updated


async def defer_retest(
    session: AsyncSession,
    marker: str,
    *,
    until: date_type,
    note: Optional[str] = None,
    subject_id: uuid.UUID,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> Optional[LabMarker]:
    """Pause the overdue-retest alert for a marker until ``until``."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if identity is None or identity.actor_user_id is None:
        raise conflict_engine.ConflictPreparedWriteError(
            "lab retest deferral requires an active human actor"
        )
    if subject_id is not None and subject_id != identity.subject_id:
        raise conflict_engine.ConflictPreparedWriteError(
            "subject_id does not match prepared lab write identity"
        )
    subject_id = identity.subject_id
    marker = normalize_marker(marker)
    row = await _marker_for_update(
        session,
        marker,
        subject_id=subject_id,
    )
    if row is None:
        return None
    row.defer_until = until
    if note is not None:
        row.note = note
    await session.flush()
    await alerts_service.resolve_scoped_superseded(
        session,
        context=alerts_service.HealthAlertContext(identity),
        alert_key=RETEST_DUE_KEY,
        marker=marker,
        keep_entity=None,
        legacy_bridge=_alert_bridge(context),
    )
    return row


# ── Results ───────────────────────────────────────────────────────────────────
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
    context: conflict_engine.ConflictWriteContext,
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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
        raise conflict_engine.ConflictRawOwnershipError(
            "lab result provenance changed while acquiring write locks"
        )
    return row


async def _lock_historical_parser_connection_before_raw(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    context: conflict_engine.ConflictWriteContext,
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
        raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
            "historical lab parser AI gateway provenance is invalid"
        )
    return connection.id


async def _lock_result_raw(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    context: conflict_engine.ConflictWriteContext,
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
            "lab result raw provenance is outside the prepared subject scope"
        )
    if raw.domain != DOMAIN or raw.source != source:
        raise conflict_engine.ConflictRawOwnershipError(
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
            raise conflict_engine.ConflictRawOwnershipError(
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
            raise conflict_engine.ConflictRawOwnershipError(
                "historical lab parser raw mixes subject and platform AI provenance"
            )
        return raw
    if require_mcp_roots and (
        raw.integration_connection_id is not None or raw.file_asset_id is not None
    ):
        raise conflict_engine.ConflictRawOwnershipError(
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
            raise conflict_engine.ConflictRawOwnershipError(
                "lab result file provenance is outside the prepared subject scope"
            )
    if source == Source.LAB_PARSER.value:
        if asset is None:
            if raw.subject_id is not None:
                raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
            "lab parser actor is not the subject owner"
        )
    if raw.actor_user_id != asset.uploaded_by_user_id:
        raise conflict_engine.ConflictRawOwnershipError(
            "lab parser raw actor does not match the file uploader"
        )
    if require_boundary_actor:
        if identity.actor_user_id is None:
            raise conflict_engine.ConflictPreparedWriteError(
                "lab upload confirmation requires an active human actor"
            )
        if (
            raw.actor_user_id != identity.actor_user_id
            or asset.uploaded_by_user_id != identity.actor_user_id
        ):
            raise conflict_engine.ConflictRawOwnershipError(
                "lab upload actor does not match the prepared writer"
            )
    if (
        asset.subject_id != identity.subject_id
        or asset.purpose != FileAssetPurpose.LAB_DOCUMENT.value
        or raw.external_id != asset.storage_ref
    ):
        raise conflict_engine.ConflictRawOwnershipError(
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
            raise conflict_engine.ConflictRawOwnershipError(
                "lab parser raw lacks one successful platform AI invocation"
            )
        invocation = succeeded[0]
        if (
            invocation.subject_id != identity.subject_id
            or invocation.actor_user_id != raw.actor_user_id
            or invocation.source != AIInvocationSource.WEB.value
            or invocation.raw_payload_id != raw.id
        ):
            raise conflict_engine.ConflictRawOwnershipError(
                "lab parser platform AI provenance is inconsistent"
            )
        if (
            not isinstance(raw.payload, dict)
            or "_ai_parse" in raw.payload
            or "_unparsed" in raw.payload
            or not isinstance(raw.payload.get("results"), list)
        ):
            raise conflict_engine.ConflictRawOwnershipError(
                "successful platform lab raw has no validated extraction"
            )
        return
    if platform_rows:
        raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
            "lab parser AI gateway provenance is invalid"
        )


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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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
    marker = normalize_marker(marker)
    if not marker:
        raise ValueError("marker is required")
    if value is None or not math.isfinite(value) or abs(value) > _VALUE_ABS_MAX:
        raise ValueError(f"implausible lab value for {marker}: {value!r}")
    catalog = await _marker_for_update(
        session,
        marker,
        subject_id=identity.subject_id,
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
    await conflict_engine.enforce_prepared(
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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
    if marker is not None:
        normalized = normalize_marker(marker)
        if not normalized:
            raise ValueError("marker is required")
        next_marker = normalized
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
        next_marker,
        subject_id=identity.subject_id,
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
    await conflict_engine.enforce_prepared(
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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
        marker = normalize_marker(marker)
        filters.append(LabResult.marker == marker)
    if start is not None:
        filters.append(LabResult.date >= start)
    if end is not None:
        filters.append(LabResult.date <= end)
    if has_note:
        filters.extend((LabResult.note.is_not(None), LabResult.note != ""))

    stmt = select(LabResult).where(*filters)
    scope = conflict_engine.ConflictScope(
        subject_id=subject_id,
        evaluation_date=end or today_local(),
    )
    exact_raw, fully_unowned_raw = conflict_engine.raw_payload_scope_conditions(
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
        raise conflict_engine.ConflictRawOwnershipError(
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
class BoundedLatestLabResults:
    """Latest result per marker with an honest marker-cap signal."""

    rows: tuple[LabResult, ...]
    truncated: bool


async def bounded_latest_results_by_marker(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    end: date_type,
    marker_limit: int = 200,
) -> BoundedLatestLabResults:
    """Return one newest row per marker; busy markers cannot displace others."""

    if not 1 <= marker_limit <= 500:
        raise ValueError("care lab marker_limit must be between 1 and 500")
    filters = (
        _subject_scope(LabResult, subject_id),
        LabResult.date <= end,
    )
    scope = conflict_engine.ConflictScope(
        subject_id=subject_id,
        evaluation_date=end,
    )
    exact_raw, fully_unowned_raw = conflict_engine.raw_payload_scope_conditions(
        scope
    )
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
        raise conflict_engine.ConflictRawOwnershipError(
            "lab result links to foreign or partial raw provenance"
        )

    ranked = (
        select(
            LabResult.id.label("result_id"),
            func.row_number()
            .over(
                partition_by=LabResult.marker,
                order_by=(LabResult.date.desc(), LabResult.id.desc()),
            )
            .label("marker_rank"),
        )
        .outerjoin(RawPayload, LabResult.raw_payload_id == RawPayload.id)
        .where(
            *filters,
            or_(LabResult.raw_payload_id.is_(None), allowed_linked_raw),
        )
        .subquery()
    )
    rows = list(
        await session.scalars(
            select(LabResult)
            .join(ranked, LabResult.id == ranked.c.result_id)
            .where(ranked.c.marker_rank == 1)
            .order_by(LabResult.marker, LabResult.id)
            .limit(marker_limit + 1)
        )
    )
    return BoundedLatestLabResults(
        rows=tuple(rows[:marker_limit]),
        truncated=len(rows) > marker_limit,
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
        seen.setdefault(r.marker, r)
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
    return await conflict_engine.legacy_unowned_raw_present(session)


async def resolve_latest_scoped(
    session: AsyncSession,
    *,
    scope: conflict_engine.ConflictScope,
) -> list[dict]:
    """Conflict resolver restricted to one explicit subject boundary."""

    exact_raw, fully_unowned_raw = conflict_engine.raw_payload_scope_conditions(
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
        raise conflict_engine.ConflictRawOwnershipError(
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
        latest_by_marker.setdefault(row.marker, row)
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> bool:
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if subject_id is not None and subject_id != identity.subject_id:
        raise conflict_engine.ConflictPreparedWriteError(
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
    await refresh_alerts(
        session,
        subject_id=identity.subject_id,
        on_date=context.evaluation_date,
        identity=identity,
        prepared_conflict_write=prepared_conflict_write,
    )
    return True


# ── Alerts ────────────────────────────────────────────────────────────────────
async def refresh_alerts(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    subject_id: uuid.UUID,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> None:
    """Raise/clear out-of-range + overdue-retest alerts from the latest values.
    Idempotent — safe on every dashboard load / scheduler tick. Each alert is
    bound to the specific LabResult row that triggered it (``entity_ref =
    f"{marker}:{result_id}"``), so a dismissal sticks forever for that row —
    only a new result for the marker can raise it again."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if on_date is not None:
        _require_evaluation_date(context, on_date)
    if subject_id is not None and subject_id != identity.subject_id:
        raise conflict_engine.ConflictPreparedWriteError(
            "subject_id does not match prepared lab write identity"
        )
    subject_id = identity.subject_id
    on_date = context.evaluation_date
    # Derived health alerts are system reconciliations even when a human
    # write caused the refresh. Lock facts/catalog before alert-key locks.
    result_scope = _subject_scope(
        LabResult,
        subject_id,
    )
    marker_scope = _subject_scope(
        LabMarker,
        subject_id,
    )
    list(
        await session.scalars(
            select(LabResult)
            .where(result_scope)
            .order_by(LabResult.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    list(
        await session.scalars(
            select(LabMarker)
            .where(marker_scope)
            .order_by(LabMarker.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    today = on_date or today_local()
    latest = await latest_per_marker(
        session,
        end=today,
        subject_id=subject_id,
    )
    markers = {
        m.name: m
        for m in await list_markers(
            session,
            subject_id=subject_id,
        )
    }

    latest_by_name = {row.marker: row for row in latest}
    names = sorted(set(markers) | set(latest_by_name))
    alert_context = _system_alert_context(context)
    bridge = _alert_bridge(context)

    async def resolve_superseded(key: str, marker: str, keep: str | None) -> None:
        await alerts_service.resolve_scoped_superseded(
            session,
            context=alert_context,
            alert_key=key,
            marker=marker,
            keep_entity=keep,
            legacy_bridge=bridge,
        )

    async def was_dismissed(key: str, entity: str) -> bool:
        return await alerts_service.was_scoped_ever_dismissed(
            session,
            context=alert_context,
            alert_key=key,
            entity_ref=entity,
            legacy_bridge=bridge,
        )

    async def resolve_current(key: str, entity: str) -> None:
        await alerts_service.resolve_scoped_by_key(
            session,
            context=alert_context,
            alert_key=key,
            entity_ref=entity,
            legacy_bridge=bridge,
        )

    async def raise_derived(
        *, key: str, entity: str, severity: Severity, message: str
    ) -> None:
        await alerts_service.raise_scoped_alert(
            session,
            context=alert_context,
            domain=Domain.LABS,
            severity=severity,
            message=message,
            alert_key=key,
            entity_ref=entity,
            legacy_bridge=bridge,
        )

    for marker_name in names:
        r = latest_by_name.get(marker_name)
        entity = f"{marker_name}:{r.id}" if r is not None else None
        await resolve_superseded(OUT_OF_RANGE_KEY, marker_name, entity)
        if r is not None and is_out_of_range(r.flag):
            if not await was_dismissed(OUT_OF_RANGE_KEY, entity):
                tier = markers.get(marker_name).tier if markers.get(marker_name) else 2
                critical = _is_critical(r.flag) or tier == 1
                severity = Severity.WARN if critical else Severity.INFO
                await raise_derived(
                    key=OUT_OF_RANGE_KEY,
                    entity=entity,
                    severity=severity,
                    message=t(
                        "alert.lab_out_of_range",
                        marker=r.marker,
                        value=r.value,
                        unit=(" " + r.unit) if r.unit else "",
                        flag=t(f"enum.flag.{r.flag}"),
                    ),
                )
        elif entity is not None:
            await resolve_current(OUT_OF_RANGE_KEY, entity)

        marker_row = markers.get(marker_name)
        has_schedule = (
            r is not None
            and marker_row is not None
            and marker_row.retest_interval_days is not None
        )
        await resolve_superseded(
            RETEST_DUE_KEY,
            marker_name,
            entity if has_schedule else None,
        )
        if not has_schedule:
            continue
        assert r is not None and marker_row is not None and entity is not None
        due = r.date + timedelta(days=marker_row.retest_interval_days)
        deferred = marker_row.defer_until is not None and marker_row.defer_until >= today
        if today > due and not deferred:
            if not await was_dismissed(RETEST_DUE_KEY, entity):
                await raise_derived(
                    key=RETEST_DUE_KEY,
                    entity=entity,
                    severity=Severity.INFO,
                    message=t("alert.lab_retest", marker=r.marker, date=r.date),
                )
        else:
            await resolve_current(RETEST_DUE_KEY, entity)


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
    context: conflict_engine.ConflictWriteContext,
    override: bool,
) -> None:
    """Prove a batch has no hard blocker before its first normalized mutation."""

    if override:
        if context.identity.actor_user_id is None:
            raise conflict_engine.ConflictOverrideActorRequired(
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
    violations = await conflict_engine.evaluate_scoped(
        session,
        scope=context.scope,
        domain=Domain.LABS,
        proposed_state=proposed,
    )
    blocking = [violation for violation in violations if violation.is_blocking]
    if blocking:
        raise conflict_engine.ConflictBlocked(
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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
        raise conflict_engine.ConflictRawOwnershipError(
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
        raise conflict_engine.ConflictRawOwnershipError(
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
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
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
    limit: int = raw_payload_service.REPARSE_BATCH,
    since_days: int = raw_payload_service.REPARSE_WINDOW_DAYS,
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
        raise conflict_engine.ConflictRawOwnershipError(
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
                    row_context = conflict_engine.ConflictWriteContext(
                        identity=origin_identity,
                        evaluation_date=prepared_date,
                        # The replay decides adoption per raw, from the raw's
                        # own roots, rather than from a caller's flag.
                        legacy_bridge=(
                            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
                            if probe_is_legacy or probe_is_historical_parser
                            else conflict_engine.LegacyConflictBridge.REJECT
                        ),
                    )
                    prepared = await conflict_engine.prepare_scoped_write(
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
                        raise conflict_engine.ConflictPreparedWriteError(
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
                        raise conflict_engine.ConflictRawOwnershipError(
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
