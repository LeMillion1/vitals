"""Pure helpers for stable, human-derived identifiers."""

from __future__ import annotations

import re

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _transliterate(text: str) -> str:
    """Transliterate Cyrillic while leaving other characters unchanged."""

    return "".join(_TRANSLIT.get(character, character) for character in text)


def slugify(name: str) -> str:
    """Build the stable ASCII-ish key used for conflict matching.

    Transliteration happens before punctuation is stripped so Russian names do
    not collapse to the generic fallback key.
    """

    normalized = _transliterate(name.strip().lower())
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    return normalized.strip("_") or "supplement"
