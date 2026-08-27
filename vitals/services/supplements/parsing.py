"""Pure normalization helpers for supplement timing and identifiers."""

from __future__ import annotations

from typing import Optional

from vitals.utils.identifiers import slugify as slugify

_SLOT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "MEAL",
        ("с едой", "с пищей", "во время еды", "after food", "with food", "with meal", "with meals"),
    ),
    ("PM", ("вечер", "ночь", "перед сном", "evening", "night", "bedtime")),
    ("AM", ("утро", "morning")),
    ("DAY", ("день", "днем", "днём", "полдень", "day", "afternoon", "midday")),
)
_DISPLAY_BUCKET_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("утро", ("утро", "morning")),
    ("день", ("день", "днем", "day", "afternoon", "midday")),
    ("вечер", ("вечер", "evening")),
    ("ночь", ("ночь", "night", "bedtime", "перед сном")),
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
