"""Marker identity normalization for the Labs bounded context."""
from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.labs import DOMAIN, LabMarker, LabResult
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine

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

_MARKER_WHITESPACE = re.compile(r"\s+")


def _clean_marker_name(name: str) -> str:
    """Conservatively clean presentation text without changing punctuation."""

    return _MARKER_WHITESPACE.sub(" ", unicodedata.normalize("NFKC", name).strip())


def _marker_comparison_key(name: str) -> str:
    return _clean_marker_name(name).casefold().replace("ё", "е")


def normalize_marker(name: str) -> str:
    """Standardize spelling, casing and known synonym names of a marker."""
    cleaned = _clean_marker_name(name)
    if not cleaned:
        return ""
    lowered = _marker_comparison_key(cleaned)
    if lowered in MARKER_ALIASES:
        return MARKER_ALIASES[lowered]
    # Fallback: capitalize first character, keep the rest
    return cleaned[0].upper() + cleaned[1:]


def normalize_marker_key(name: str) -> str:
    """Return the stable identity for a marker inside one health record.

    The key intentionally does not fold punctuation or medically meaningful
    suffixes.  Known synonyms are first resolved through the existing reviewed
    alias map; every other name receives only Unicode, whitespace and casing
    normalization.
    """

    display = normalize_marker(name)
    return _marker_comparison_key(display) if display else ""


def _validated_marker_identity(name: str) -> tuple[str, str, str]:
    original = name
    display = normalize_marker(name)
    key = normalize_marker_key(display)
    if not display:
        raise ValueError("marker is required")
    if len(original) > 128 or len(display) > 128 or len(key) > 256:
        raise ValueError("marker is too long")
    return original, display, key


def _subject_scope(model, subject_id: uuid.UUID):
    return model.subject_id == subject_id


async def get_marker(
    session: AsyncSession,
    name: str,
    *,
    subject_id: uuid.UUID,
) -> Optional[LabMarker]:
    key = normalize_marker_key(name)
    if not key:
        return None
    stmt = select(LabMarker).where(
        LabMarker.normalized_name == key,
        LabMarker.is_canonical.is_(True),
    )
    stmt = stmt.where(_subject_scope(LabMarker, subject_id))
    result = await session.execute(stmt)
    return result.scalars().first()


async def _marker_for_update(
    session: AsyncSession,
    name: str,
    *,
    subject_id: uuid.UUID,
) -> LabMarker | None:
    key = normalize_marker_key(name)
    if not key:
        return None
    stmt = select(LabMarker).where(
        LabMarker.normalized_name == key,
        LabMarker.is_canonical.is_(True),
    )
    stmt = stmt.where(_subject_scope(LabMarker, subject_id))
    marker = await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )
    return marker


async def _existing_marker_display(
    session: AsyncSession,
    *,
    marker_key: str,
    subject_id: uuid.UUID,
) -> str | None:
    """Recover the stable display for old portable facts lacking a catalog row."""

    return await session.scalar(
        select(LabResult.marker)
        .where(
            LabResult.subject_id == subject_id,
            LabResult.marker_key == marker_key,
        )
        .order_by(LabResult.id)
        .limit(1)
    )


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


async def list_markers(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> Sequence[LabMarker]:
    stmt = select(LabMarker).where(LabMarker.is_canonical.is_(True))
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
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> tuple[LabMarker, bool, bool]:
    """Create or backfill one scoped catalog row for startup/domain seeds.

    Existing non-null user configuration is never overwritten. The booleans are
    ``(created, updated)``; compatibility adoption counts as an update while the
    unknown historical actor remains unchanged.
    """

    if identity is None or prepared_conflict_write is None:
        raise engine.ConflictPreparedWriteError(
            "scoped lab writes require identity and a prepared conflict write"
        )
    engine.require_prepared_identity(
        session,
        prepared=prepared_conflict_write,
        identity=identity,
    )
    _original, normalized, normalized_key = _validated_marker_identity(name)
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
            normalized_name=normalized_key,
            is_canonical=True,
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



__all__ = [
    "MARKER_ALIASES",
    "ensure_marker_catalog_entry",
    "get_marker",
    "list_markers",
    "normalize_marker",
    "normalize_marker_key",
]
