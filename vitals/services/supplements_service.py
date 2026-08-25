"""Supplements catalog service (Phase 3).

Reference catalog only (no daily logging — Ritual owns that). The catalog's
**active** rows are exposed to the conflict engine via :func:`resolve_active`, so
e.g. activating an iron supplement while a hemochromatosis-carrier genetics row
exists raises a ``block`` (overridable).

Mutating fns run ``conflict_engine.enforce`` so the override flow is wired: the
router turns ``ConflictBlocked`` into a 409 + violations payload.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, Source
from vitals.models.supplements import DOMAIN, Supplement
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _transliterate(text: str) -> str:
    """Cyrillic -> Latin, character by character. Non-Cyrillic characters pass
    through unchanged so mixed RU/EN names transliterate only the RU part."""
    return "".join(_TRANSLIT.get(ch, ch) for ch in text)


def slugify(name: str) -> str:
    """Stable conflict-match slug from a display name (ascii-ish, lowercase).

    Transliterates Cyrillic first so a Russian name (e.g. "Железо") yields a
    real, stable, non-empty slug ("zhelezo") instead of collapsing to the
    fallback "supplement" — the ascii-only regex used to strip Cyrillic
    entirely, silently breaking conflict-rule matching for RU-named rows."""
    s = name.strip().lower()
    s = _transliterate(s)
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_") or "supplement"


# Coarse timing slots a supplement's free-text `timing` field is parsed into.
# The conflict engine's timing_separation rules only fire when both sides of a
# rule share the same slot (see conflict_engine._slots) — taking iron in the
# morning and zinc at night are already separated, no warning needed.
_SLOT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("MEAL", ("с едой", "с пищей", "во время еды", "after food", "with food", "with meal", "with meals")),
    ("PM", ("вечер", "ночь", "перед сном", "evening", "night", "bedtime")),
    ("AM", ("утро", "morning")),
    ("DAY", ("день", "днем", "днём", "полдень", "day", "afternoon", "midday")),
)


def _parse_slot(timing: Optional[str]) -> Optional[str]:
    """Coarse AM/PM/MEAL/DAY timing slot from a free-text ``timing`` value
    (RU/EN), or ``None`` when it's blank or doesn't match a known keyword."""
    if not timing:
        return None
    text = timing.strip().lower().replace("ё", "е")
    for slot, keywords in _SLOT_KEYWORDS:
        if any(kw in text for kw in keywords):
            return slot
    return None


# The /supplements page groups active supplements into four display rows
# (morning/day/evening/night) — a finer split than _parse_slot's AM/DAY/MEAL/PM
# (which folds evening+night into one PM slot for the conflict engine's
# timing-separation rules). Keyed by the same RU bucket labels the template
# already uses, so an EN or RU free-text `timing` value lands in the right row.
_DISPLAY_BUCKET_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("утро", ("утро", "morning")),
    ("день", ("день", "днем", "day", "afternoon", "midday")),
    ("вечер", ("вечер", "evening")),
    ("ночь", ("ночь", "night", "bedtime", "перед сном")),
)


def timing_bucket(timing: Optional[str]) -> Optional[str]:
    """Canonical display-bucket key ('утро'/'день'/'вечер'/'ночь') a free-text
    ``timing`` value (RU or EN) belongs to on the /supplements page, or
    ``None`` when it matches none of them (renders under "Other")."""
    if not timing:
        return None
    text = timing.strip().lower().replace("ё", "е")
    for bucket, keywords in _DISPLAY_BUCKET_KEYWORDS:
        if any(kw in text for kw in keywords):
            return bucket
    return None


def _proposed(key: str, active: bool, timing_slot: Optional[str] = None) -> dict:
    return {"key": key, "active": active, "timing_slot": timing_slot}


def _supplement_subject_scope(subject_id: uuid.UUID):
    """Restrict a supplement query to one person's regimen."""

    return Supplement.subject_id == subject_id


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: conflict_engine.PreparedConflictWrite,
) -> conflict_engine.ConflictWriteContext:
    """Bind one supplement write to its subject and its conflict decision."""

    return conflict_engine.require_prepared_identity(
        session,
        prepared=prepared,
        identity=identity,
    )


def _supplement_by_id_stmt(supplement_id: int, *, subject_id: uuid.UUID):
    return (
        select(Supplement)
        .where(Supplement.id == supplement_id)
        .where(_supplement_subject_scope(subject_id))
    )


async def _get_supplement_for_update(
    session: AsyncSession,
    supplement_id: int,
    *,
    subject_id: uuid.UUID,
) -> Optional[Supplement]:
    return await session.scalar(
        _supplement_by_id_stmt(supplement_id, subject_id=subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def get_supplement_for_update(
    session: AsyncSession,
    supplement_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> Optional[Supplement]:
    """Lock and refresh one scoped row for a caller-side partial update merge."""

    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    return await _get_supplement_for_update(
        session,
        supplement_id,
        subject_id=identity.subject_id,
    )


async def list_supplements(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    active_only: bool = False,
    limit: int | None = None,
) -> Sequence[Supplement]:
    """Return one person's regimen. A regimen without a person is not a thing."""

    stmt = select(Supplement).where(_supplement_subject_scope(subject_id))
    if active_only:
        stmt = stmt.where(Supplement.active.is_(True))
    stmt = stmt.order_by(Supplement.active.desc(), Supplement.name)
    if limit is not None:
        if limit < 1:
            raise ValueError("supplement limit must be positive")
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def add_supplement(
    session: AsyncSession,
    *,
    name: str,
    key: Optional[str] = None,
    dose: Optional[str] = None,
    timing: Optional[str] = None,
    evidence: Optional[str] = None,
    active: bool = True,
    contraindications: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
    source: str = Source.MANUAL.value,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> Supplement:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if key:
        resolved_key = key
    else:
        # Deferred import: conflict_catalog imports this module (slugify is its
        # dictionary-miss fallback), so importing it back at module level here
        # would be circular.
        from vitals.services import conflict_catalog

        resolved_key = conflict_catalog.normalize_ingredient(name)
    proposed = _proposed(resolved_key, active, _parse_slot(timing))
    await conflict_engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.SUPPLEMENTS,
        proposed_state=proposed,
        override=override,
        entity_ref=f"supplement:{resolved_key}",
    )
    row = Supplement(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=DOMAIN,
        source=source,
        name=name,
        key=resolved_key,
        dose=dose,
        timing=timing,
        evidence=evidence,
        active=active,
        contraindications=contraindications,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


async def update_supplement(
    session: AsyncSession,
    supplement_id: int,
    *,
    name: str,
    key: Optional[str] = None,
    dose: Optional[str] = None,
    timing: Optional[str] = None,
    evidence: Optional[str] = None,
    active: bool = True,
    contraindications: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> Optional[Supplement]:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_supplement_for_update(
        session,
        supplement_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return None
    if key:
        resolved_key = key
    else:
        # Deferred import: conflict_catalog imports this module (slugify is its
        # dictionary-miss fallback), so importing it back at module level here
        # would be circular.
        from vitals.services import conflict_catalog

        resolved_key = conflict_catalog.normalize_ingredient(name)
    proposed = _proposed(resolved_key, active, _parse_slot(timing))
    await conflict_engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.SUPPLEMENTS,
        proposed_state=proposed,
        override=override,
        entity_ref=f"supplement:{resolved_key}",
        replace_entity_key=str(row.id),
    )
    row.name = name
    row.key = resolved_key
    row.dose = dose
    row.timing = timing
    row.evidence = evidence
    row.active = active
    row.contraindications = contraindications
    row.note = note
    await session.flush()
    return row


async def set_active(
    session: AsyncSession,
    supplement_id: int,
    active: bool,
    *,
    override: bool = False,
    identity: WriteIdentity,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> Optional[Supplement]:
    """Toggle a catalog row's active flag — runs the conflict check so activating
    a contraindicated supplement surfaces the block/override flow."""
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    row = await _get_supplement_for_update(
        session,
        supplement_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return None
    if active:
        proposed = _proposed(row.key, True, _parse_slot(row.timing))
        await conflict_engine.enforce_prepared(
            session,
            prepared=prepared_conflict_write,
            domain=Domain.SUPPLEMENTS,
            proposed_state=proposed,
            override=override,
            entity_ref=f"supplement:{row.key}",
            replace_entity_key=str(row.id),
        )
    row.active = active
    await session.flush()
    return row


async def get_supplement(
    session: AsyncSession,
    supplement_id: int,
    *,
    subject_id: uuid.UUID,
) -> Optional[Supplement]:
    return await session.scalar(
        _supplement_by_id_stmt(supplement_id, subject_id=subject_id)
    )


async def delete_supplement(
    session: AsyncSession,
    supplement_id: int,
    *,
    identity: WriteIdentity,
) -> bool:
    row = await get_supplement(
        session,
        supplement_id,
        subject_id=identity.subject_id,
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def legacy_unowned_present(session: AsyncSession) -> bool:
    """Mirror of this module's widening in :func:`resolve_active_scoped`.

    Kept beside it so the two change together: the engine skips its
    sole-subject proof when every probe says no, so a probe that missed a row
    its resolver would still adopt is the one way this goes wrong.
    """

    found = await session.scalar(
        select(Supplement.id)
        .where(Supplement.subject_id.is_(None),
            Supplement.actor_user_id.is_(None),)
        .limit(1)
    )
    return found is not None


async def resolve_active_scoped(
    session: AsyncSession,
    *,
    scope: conflict_engine.ConflictScope,
) -> list[dict]:
    """Conflict resolver restricted to one explicit subject boundary.

    The conflict engine still offers a fully-unowned bridge to its callers, and
    a resolver has to honour the scope it is handed. This is the last place in
    the module that can see a row with no subject; it goes when the bridge does.
    """

    subject_scope = Supplement.subject_id == scope.subject_id
    if scope.include_legacy_unowned:
        subject_scope = or_(
            subject_scope,
            and_(
                Supplement.subject_id.is_(None),
                Supplement.actor_user_id.is_(None),
            ),
        )
    rows = await session.scalars(select(Supplement).where(subject_scope))
    return [
        {
            conflict_engine.CONFLICT_ENTITY_KEY: str(row.id),
            "key": row.key,
            "active": row.active,
            "name": row.name,
            "timing_slot": _parse_slot(row.timing),
        }
        for row in rows
    ]
