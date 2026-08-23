"""A modifier a template names has to be one its base actually defines.

The design system is written as base-plus-modifier: `.v-dot.good`,
`.v-alert.warn`, `.mh-rail-btn.is-active`. Nothing enforces that a template
reaching for one has picked a modifier that base defines, and a wrong guess
fails silently — the element renders in the base style, so a lab value out of
range looks exactly like one inside it, and the page looks fine.

Not hypothetical. `class="v-dot {{ 'bad' if flagged else 'warn' }}"` was written
into the care record on 2026-08-23. Both words are real modifiers — of
`mh-metric` and `mh-stat-sub` — and neither is one of `v-dot`'s, which are
`amber`, `cool`, `violet`, `good`. It was caught by opening the page and
noticing the dot was grey, which is not a repeatable way to catch anything.

**The check is deliberately narrow.** A word counts as a modifier only if some
base in the stylesheets defines it; anything else in a `class` attribute is a
Tailwind utility or plain markup and is left alone. So this catches the real
failure — a modifier borrowed from the wrong base, which is what gets typed from
memory — and does not catch a word invented from nothing. Widening it to every
unknown token means maintaining a list of every Tailwind utility in use, which
would be a second inventory to keep in step and would fail for the wrong reasons
far more often than it caught anything.

A modifier arriving entirely through a variable — `class="v-alert {{ alert.severity }}"`
— cannot be resolved from source and is not checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STYLESHEETS = ("web/static/vitals.css", "web/static/vitals-masthead.css")
TEMPLATES = sorted((ROOT / "web/templates").rglob("*.html"))

#: Each styled base, and every modifier the stylesheets define for it.
DEFINED: dict[str, set[str]] = {}
for _sheet in STYLESHEETS:
    _css = (ROOT / _sheet).read_text(encoding="utf-8")
    for _base, _modifier in re.findall(
        r"\.((?:v|mh)-[a-z0-9-]+)\.([a-z][a-z0-9-]*)", _css
    ):
        DEFINED.setdefault(_base, set()).add(_modifier)

#: Every modifier word the system knows, whichever base it belongs to. A word
#: outside this set is not a modifier at all, and not this test's business.
VOCABULARY: set[str] = {mod for mods in DEFINED.values() for mod in mods}

_CLASS_ATTR = re.compile(r'class="([^"]*)"')
_LITERAL = re.compile(r"'([a-z][a-z0-9-]*)'|\"([a-z][a-z0-9-]*)\"")


def _bases_and_modifiers(value: str) -> tuple[set[str], set[str]]:
    """The styled bases in one class attribute, and the modifiers it names."""

    bases = {
        token
        for token in re.findall(r"\b((?:v|mh)-[a-z0-9-]+)\b", value)
        if token in DEFINED
    }
    named = {token for token in value.split() if token in VOCABULARY}
    # Modifiers chosen inside a Jinja expression are the interesting case: they
    # are written from memory, next to a base the author is looking straight at.
    for match in _LITERAL.finditer(value):
        literal = match.group(1) or match.group(2)
        if literal in VOCABULARY:
            named.add(literal)
    return bases, named


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_every_named_modifier_exists_for_its_base(template: Path):
    source = template.read_text(encoding="utf-8")
    unknown: set[str] = set()

    for value in _CLASS_ATTR.findall(source):
        bases, named = _bases_and_modifiers(value)
        if not bases or not named:
            continue
        # A modifier beside more than one styled base could belong to either, so
        # it passes if any of them defines it. Narrower would fail on markup
        # that is perfectly correct.
        allowed: set[str] = set()
        for base in bases:
            allowed |= DEFINED.get(base, set())
        for modifier in named - allowed:
            unknown.add(
                f"{'/'.join(sorted(bases))} has no modifier {modifier!r} "
                f"(it defines: {', '.join(sorted(allowed)) or 'none'})"
            )

    assert not unknown, (
        f"{template.relative_to(ROOT)} names modifiers its base does not define, "
        "so the element renders in the base style and the distinction it was "
        "reaching for is invisible:\n  " + "\n  ".join(sorted(unknown))
    )


def test_the_vocabulary_was_actually_read():
    """Guard the guard: an empty inventory would pass everything above."""

    assert len(DEFINED) > 20
    assert {"amber", "good"} <= DEFINED.get("v-dot", set())
    assert {"bad", "warn"} <= VOCABULARY
