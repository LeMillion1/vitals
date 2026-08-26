"""Contracts for mobile pass 2 — "make the execution tidy".

Same style as tests/test_mobile_shell.py: the CSS is read as text, because what
these fixes are about is which value a rule ends up with, and a rendered page
can't tell us that. The two runtime checks are the card header macro and the
metric strip, which a render *can*.
"""

import re

import pytest

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_CSS = (ROOT / "web/static/vitals.css").read_text(encoding="utf-8")
MASTHEAD_CSS = (ROOT / "web/static/vitals-masthead.css").read_text(encoding="utf-8")
TEMPLATES = sorted((ROOT / "web/templates").rglob("*.html"))


def _rule(css: str, selector: str) -> str:
    found = re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert found, f"no rule for {selector!r}"
    return "\n".join(found)


def _radii(css: str, selector: str) -> list[str]:
    """Every border-radius `selector` is given anywhere in the file, including
    inside media blocks — a nesting rule that only holds at one width is not a
    rule."""
    out = []
    for body in re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", css):
        out += re.findall(r"border-radius:\s*([^;]+);", body)
    return [v.strip() for v in out]


def _luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _token(name: str) -> str:
    return re.search(rf"{re.escape(name)}:\s*(#[0-9A-Fa-f]{{6}})", APP_CSS).group(1)


def test_inner_radius_is_always_smaller_than_the_card_it_sits_in():
    """An inset used to carry --radius while the card was knocked down to
    --radius too on a phone: same arc outside and inside, which is what made a
    well read as a sticker stuck on top."""
    card = set(_radii(APP_CSS, ".v-card")) | set(_radii(APP_CSS, ".v-page .v-card"))
    assert card == {"var(--radius-lg)"}, card
    assert set(_radii(APP_CSS, ".v-card-inset")) == {"var(--radius)"}


def test_segmented_pill_is_not_the_same_capsule_as_its_track():
    """Track and pill were both 999px, 4px apart — the end arcs nearly touched.
    The track also drops its border: the pill (--surface-2) is lighter than
    --line, which turned the visible sliver of track into a crescent."""
    track = _rule(APP_CSS, ".v-seg")
    assert "border: 0;" in track
    assert "padding: 6px;" in track
    assert set(_radii(APP_CSS, ".v-seg-btn")) == {"var(--radius)"}
    # …and no media block quietly squeezes the gap back shut.
    assert "padding: 0.2rem" not in APP_CSS


def test_segmented_control_wraps_instead_of_hiding_its_last_tab():
    """Four tabs need 368px; the entry sidebar gives them 291. The track used to
    scroll sideways with its bar hidden, so the last label was sliced mid-word
    ("Соста") with nothing on screen to say the row continued."""
    track = _rule(APP_CSS, ".v-seg")
    assert "flex-wrap: wrap;" in track
    assert "overflow-x" not in track
    assert ".v-seg::-webkit-scrollbar" not in APP_CSS
    # A wrapped track breaks into even rows of two, not three and a lonely
    # fourth — and it stops being a capsule, which only reads as one on a
    # single row. The phone fits all four on that row and resets the basis.
    assert set(_radii(APP_CSS, ".v-seg")) == {"var(--radius-lg)"}
    assert "flex: 1 1 40%;" in _rule(APP_CSS, ".v-seg-btn")
    assert "flex: 1 1 auto;" in _rule(APP_CSS, ".v-seg-btn")


def test_stepper_button_radius_fits_its_6px_track():
    assert _radii(APP_CSS, ".v-weight-step") == ["var(--radius-xs)"]


def test_bad_clears_aa_on_a_card():
    """4.27:1 before — the same lightening already applied to --violet."""
    bad, surface = _luminance(_token("--bad")), _luminance(_token("--surface"))
    contrast = (max(bad, surface) + 0.05) / (min(bad, surface) + 0.05)
    assert contrast >= 4.5, round(contrast, 2)


def test_primary_button_shadow_stays_off_its_neighbours():
    """An 18px amber blur under the button washed over the chip row below it."""
    shadow = re.search(r"box-shadow:\s*0 (\d+)px (\d+)px -(\d+)px rgba\(245", _rule(APP_CSS, ".v-btn"))
    offset, blur, spread = (int(g) for g in shadow.groups())
    assert offset + blur - spread <= 6


def test_danger_button_is_a_complete_mobile_control():
    rule = _rule(APP_CSS, ".v-btn-danger")
    assert "display: inline-flex;" in rule
    assert "padding: .7rem 1.15rem;" in rule
    assert "min-height: 2.75rem;" in rule


def test_scrollbars_are_thin_everywhere_not_per_element():
    """A 15px platform bar inside a card sits on the last table column and on
    the right end of every divider in a scrolling list."""
    assert "scrollbar-width: thin;" in _rule(APP_CSS, "*")
    assert "width: 6px;" in _rule(APP_CSS, "::-webkit-scrollbar")
    # …and no list keeps a hand-made gutter for the old fat one.
    for path in TEMPLATES:
        assert "overflow-y-auto pr-1" not in path.read_text(encoding="utf-8"), path


def test_no_template_rolls_its_own_card_header():
    """Five variants used to coexist, three of them regularly on one screen.
    The bar lives in the macro now, so there is exactly one place to change it —
    and no template can grow a sixth variant without reintroducing .v-bar."""
    for path in TEMPLATES:
        if path.name == "masthead.html":
            continue
        text = path.read_text(encoding="utf-8")
        assert 'class="v-bar' not in text, path
        assert "v-bar-sm" not in text and "v-bar-accent" not in text, path
    assert 'class="v-bar"' in (ROOT / "web/templates/partials/masthead.html").read_text(encoding="utf-8")
    # The one variant is neutral: a coloured heading bar is what made amber mean
    # nothing in particular.
    assert "background: var(--line-2);" in _rule(APP_CSS, ".v-bar")


def test_metric_strip_fills_its_last_row():
    """Three figures in a hard two-column grid left a hole bottom-right."""
    assert "repeat(auto-fit, minmax(110px, 1fr))" in MASTHEAD_CSS
    assert "repeat(2, minmax(0, 1fr));\n        width: 100%" not in MASTHEAD_CSS


def test_non_numbers_are_not_drawn_as_numbers():
    """Each of these carries one more class than every `.mh-metric-value
    .is-primary` size rule in the file, so it wins wherever the breakpoint
    wrote it."""
    for cls in (
        ".mh-metric-value.is-primary.is-text",
        ".mh-metric-value.is-primary.is-empty",
        ".mh-metric-value.is-primary.is-compact",
    ):
        assert cls in MASTHEAD_CSS
    assert "margin-left: -.14em;" in _rule(MASTHEAD_CSS, "body.ui-masthead .mh-metric-value.is-neg")


@pytest.mark.asyncio
async def test_card_headers_render_from_the_macro(auth_client):
    r = await auth_client.get("/today")
    assert r.status_code == 200
    assert r.text.count('class="v-card-head"') >= 4
    head = r.text.split('class="v-card-head"', 1)[1]
    assert '<span class="v-bar"></span>' in head


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/garmin", "/glp1", "/hrt", "/weight"])
async def test_the_strip_sorts_words_dates_and_gaps_from_figures(auth_client, route):
    """A "—" for data that hasn't arrived was drawn at 32px, larger than the
    page title it stood under; "Tirzepatide" set as a figure ran over the column
    beside it; "24-07-2026" wrapped in the middle of the date."""
    r = await auth_client.get(route)
    assert r.status_code == 200
    seen = 0
    for chunk in r.text.split('class="mh-metric-value')[1:]:
        classes, _, rest = chunk.partition(">")
        value = rest.split("<", 1)[0].strip()
        seen += 1
        if value in ("—", "–", "-"):
            assert "is-empty" in classes, chunk[:120]
        elif not any(c.isdigit() for c in value):
            assert "is-text" in classes, chunk[:120]
        elif len(value) > 8:
            assert "is-compact" in classes, chunk[:120]
        elif value.startswith(("−", "-")):
            assert "is-neg" in classes, chunk[:120]
    assert seen, f"{route} renders no key figures"
