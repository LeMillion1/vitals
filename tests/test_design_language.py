"""Contracts for mobile pass 5 — "one language instead of three".

Same shape as tests/test_mobile_shell.py: the stylesheets are read as text,
because what these fixes are about is which values exist at all. A rendered
page can tell you the size of one heading; it cannot tell you that the app
stopped having twenty of them.

Every check here is an inventory: a closed set of sizes, of corner radii, of
border colours, of rebuild points, of display faces. The defect they all guard
against is the same one — a second way of saying a thing, added because the
first was hard to find.
"""

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP_CSS = (ROOT / "web/static/vitals.css").read_text(encoding="utf-8")
MASTHEAD_CSS = (ROOT / "web/static/vitals-masthead.css").read_text(encoding="utf-8")
FONTS_CSS = (ROOT / "web/static/fonts.css").read_text(encoding="utf-8")
BOTH = {"vitals.css": APP_CSS, "vitals-masthead.css": MASTHEAD_CSS}
TEMPLATES = sorted((ROOT / "web/templates").rglob("*.html"))


def _rule(css: str, selector: str) -> str:
    found = re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert found, f"no rule for {selector!r}"
    return "\n".join(found)


# ── C2 — one type ladder ─────────────────────────────────────────────────────

LADDER = ["--text-eyebrow", "--text-micro", "--text-label", "--text-body",
          "--text-card", "--text-heading", "--text-lead", "--text-title",
          "--text-display", "--text-hero"]


def test_the_ladder_is_declared_once_and_climbs():
    declared = re.findall(r"(--text-[\w-]+):\s*(\d+)px", APP_CSS)
    assert [name for name, _ in declared] == LADDER, declared
    sizes = [int(px) for _, px in declared]
    assert sizes == sorted(sizes), sizes
    assert sizes == sorted(set(sizes)), "two names for one size"
    # 11px is the floor: the bottom bar's captions. Nothing is set smaller.
    assert sizes[0] == 11
    # …and nowhere is a size re-declared under a breakpoint, which is what made
    # --text-heading mean 18px and 16px at the same time.
    assert len(re.findall(r"--text-[\w-]+:", APP_CSS + MASTHEAD_CSS)) == len(LADDER)


def test_the_masthead_no_longer_carries_a_scale_of_its_own():
    assert "--mh-text" not in MASTHEAD_CSS


@pytest.mark.parametrize("name", list(BOTH))
def test_no_rule_invents_a_size(name):
    """The audit counted twenty actual sizes, thirteen of them written straight
    into a rule and two of them fractional (19.2px, 13.5px) because an em-based
    utility measured itself against its parent."""
    css = BOTH[name]
    raw = re.findall(r"font-size:\s*([\d.]+(?:px|rem|em))", css)
    # The one exception, and it is not a type decision: iOS zooms the page when
    # a focused form control is under 16px.
    assert [r for r in raw if r != "16px"] == [], raw
    shorthand = re.findall(r"(?<!-)\bfont:\s*[\d ]*?([\d.]+(?:px|rem|em))", css)
    assert shorthand == [], shorthand


def test_every_step_of_the_ladder_is_used():
    used = set(re.findall(r"var\((--text-[\w-]+)\)", APP_CSS + MASTHEAD_CSS))
    assert set(LADDER) == used, set(LADDER) ^ used


# ── C3 — one set of corners ──────────────────────────────────────────────────

RADIUS_TOKENS = {"--radius-xs", "--radius-sm", "--radius", "--radius-lg", "--radius-pill"}


@pytest.mark.parametrize("name", list(BOTH))
def test_every_corner_comes_from_a_token(name):
    """Ten values were in use, including `999px` and `9999px` — the same corner
    written two ways — plus 4/8/9/12/18px chosen one rule at a time."""
    for value in re.findall(r"border-radius:\s*([^;]+);", BOTH[name]):
        for part in value.replace("var(", " var(").split():
            part = part.strip()
            if part in ("0", "50%", "inherit", ""):
                continue
            assert part.startswith("var(--radius"), f"{name}: {value.strip()}"
    assert "border-radius: 9999px" not in BOTH[name]


def test_the_pill_is_spelled_one_way():
    assert re.search(r"--radius-pill:\s*999px", APP_CSS)
    # 999px only ever appears as that token's value, never inline in a rule.
    assert "border-radius: 999px" not in APP_CSS + MASTHEAD_CSS


# ── C5 — one set of borders ──────────────────────────────────────────────────

@pytest.mark.parametrize("name", list(BOTH))
def test_no_border_mixes_its_own_colour(name):
    """Twenty border colours for five semantic tones: `warn` was drawn at .35,
    .28 and .3, and half the `bad` borders still used the hex the token stopped
    using when it was lightened to clear AA."""
    for decl in re.findall(r"border(?:-color|-top|-bottom|-left|-right)?:\s*([^;]+);", BOTH[name]):
        assert "rgba(" not in decl, f"{name}: {decl.strip()}"


def test_every_semantic_tone_has_the_same_three_parts():
    for tone in ("accent", "good", "bad", "warn", "cool", "violet"):
        for part in ("", "-soft", "-line"):
            assert f"--{tone}{part}:" in APP_CSS, f"--{tone}{part}"


# ── C7 — three rebuild points ────────────────────────────────────────────────

def test_the_layout_rebuilds_at_three_widths():
    """Eight: 480 / 560 / 640 / 767 / 768 / 900 / 1024 / 1200. Which of them won
    a property was a question of which had been written last."""
    for name, css in BOTH.items():
        found = set(re.findall(r"@media \((?:min|max)-width: (\d+)px\)", css))
        assert found <= {"767", "768", "1199", "1200"}, (name, sorted(found))


# ── C6 — one display face ────────────────────────────────────────────────────

def test_one_display_family_and_it_can_fall_back_to_cyrillic():
    """Inter on 4305 elements, one display face on 283 and a second on 127 —
    the second sat on every card title while the first held the page title
    above it."""
    assert FONTS_CSS.count("font-family: 'Outfit'") == 0
    assert not (ROOT / "web/static/fonts/outfit-latin.woff2").exists()
    families = set(re.findall(r"@font-face \{\s*font-family: '([^']+)'", FONTS_CSS))
    assert families == {"Inter", "Bricolage Grotesque"}, families
    display = _rule(APP_CSS, ":root").split("--display:")[1].split(";")[0]
    assert display.index("'Inter'") > display.index("'Bricolage Grotesque'")


def test_display_type_is_set_through_the_one_token():
    """Named once, in :root, and referenced everywhere else — the file used to
    spell the whole stack out in nine places and two of them disagreed."""
    for name, css in BOTH.items():
        spelled = re.findall(r"(?<!--display: )'Bricolage Grotesque'", css)
        assert spelled == [], name


# ── D11 — two icon sizes ─────────────────────────────────────────────────────

def test_icons_come_in_two_sizes():
    """Six were in use — 15/16/17/20/22/24 — with nothing saying which belonged
    where."""
    assert "--ico: 16px" in APP_CSS and "--ico-lg: 22px" in APP_CSS
    for name, css in BOTH.items():
        for rule in re.findall(r"[\w.\-\[\]=\"]+ svg\s*\{([^}]*)\}", css):
            for w in re.findall(r"(?<!stroke-)width:\s*([^;]+);", rule):
                assert w.strip().startswith("var(--ico"), f"{name}: {w}"


# ── §6 — the 44px pass ───────────────────────────────────────────────────────

def _phone(css: str) -> str:
    out, start = [], css.find("@media (max-width: 767px)")
    while start != -1:
        i = css.index("{", start)
        depth, j = 0, i
        while True:
            depth += (css[j] == "{") - (css[j] == "}")
            if depth == 0:
                break
            j += 1
        out.append(css[i + 1:j])
        start = css.find("@media (max-width: 767px)", j)
    return "\n".join(out)


PHONE_APP = _phone(APP_CSS)
PHONE_MH = _phone(MASTHEAD_CSS)


@pytest.mark.parametrize("selector,css", [
    (".v-pill", PHONE_APP),
    (".v-seg-btn", PHONE_APP),
    ("summary", PHONE_APP),
    ("body.ui-masthead .mh-tab", PHONE_MH),
    ("body.ui-masthead .mh-date-nav-btn", PHONE_MH),
])
def test_small_controls_are_44px_on_a_phone(selector, css):
    """A rubric chip was 33px, a date arrow 28, a disclosure triangle 17. What a
    control is drawn as is not what a thumb has to hit."""
    assert "2.75rem" in _rule(css, selector), selector


def test_the_segmented_control_fills_its_card_on_a_phone():
    """Fit-content left the track ending 65px short of the card holding it —
    every other control in that card runs edge to edge, so the switch read as
    something dropped in rather than as part of the form."""
    assert "width: 100%;" in _rule(PHONE_APP, ".v-seg")
    assert "width: fit-content;" in _rule(APP_CSS, ".v-seg")


def test_the_stepper_keys_are_44px():
    assert "grid-template-columns: 44px minmax(0, 1fr) 44px;" in APP_CSS


def test_a_checkbox_is_not_a_13px_glyph():
    assert "width: 22px" in _rule(PHONE_APP, 'input[type="checkbox"]:not([role="switch"])')


# ── D6 / D13 — a filter is not a place, a sub-tab is not a switch ────────────

def test_a_filter_pill_cannot_be_mistaken_for_a_section_chip():
    """/genetics stacked a row of filters straight under the row of section
    chips, in the same shape, with "on" drawn differently in each."""
    assert "border-radius: var(--radius-sm);" in _rule(APP_CSS, ".v-pill")
    assert "border-radius: var(--radius-pill);" in _rule(PHONE_MH, "body.ui-masthead .mh-tab")
    assert "var(--accent" not in _rule(APP_CSS, ".v-pill-on")


def test_route_sub_tabs_are_chips_not_a_segmented_control():
    """Two navigation rows in two different shapes, one under the other. The
    segmented control is left to what it is for — switching state inside a
    page — and never to linking between routes."""
    for path in (ROOT / "web/templates/garmin/_tabs.html",
                 ROOT / "web/templates/weight/_tabs.html"):
        text = path.read_text(encoding="utf-8")
        assert "mh-subtabs" in text, path
        assert not re.search(r'class="[^"]*v-seg', text), path
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<a [^>]*v-seg-btn[^>]*>", text):
            pytest.fail(f"{path.name}: a route link wearing a segmented button: {tag[:80]}")
    # …and only one row of chips may pin itself to the top of the scroll.
    assert "position: static;" in _rule(PHONE_MH, "body.ui-masthead .mh-subtabs")


# ── D2 — a card inside a card is raised, not sunk ────────────────────────────

def test_a_nested_card_is_lighter_than_the_one_it_sits_in():
    """/skincare read as four frames deep with a hole in the middle: the tile's
    own surface is darker than the page behind the card holding it."""
    assert "background: var(--surface-2);" in _rule(APP_CSS, ".v-card .v-card-tile")
    skincare = (ROOT / "web/templates/skincare/index.html").read_text(encoding="utf-8")
    assert "ring-1" not in skincare
    assert "bg-[var(--bg-inset)] border border-[var(--line)]" not in skincare


# ── D14 / M10 — copy and formats ─────────────────────────────────────────────

def test_one_decimal_mark_per_language():
    """A number input is drawn by the browser in the user's own locale — Chrome
    renders 86.1 as "86,1" under ru and no attribute changes that — so the
    readouts follow the platform instead of arguing with it. Three code paths
    printed a number to this dashboard and each rounded it its own way; they all
    go through `decimal()` now, so a weight cannot read two ways on one screen.
    """
    from vitals.i18n import STRINGS, current_lang
    from vitals.services.nav_status_service import decimal as nav_decimal
    from vitals.services.today_service import _num, _signed
    from web.templating import format_number

    assert STRINGS["ru"]["common.decimal_sep"] == ","
    assert STRINGS["en"]["common.decimal_sep"] == "."
    assert nav_decimal is not None

    token = current_lang.set("ru")
    try:
        assert format_number(12345.67) == "12 345,7"
        assert _num(86.13) == "86,1"
        assert _signed(-0.63) == "−0,6"
    finally:
        current_lang.reset(token)
    assert format_number(12345.67) == "12 345.7"


def test_durations_are_not_written_in_english_next_to_russian():
    """"8h 40m" sat beside "Оценка сна"."""
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\}\}h \{\{|~ 'h '", text), path
    from vitals.i18n import STRINGS
    assert STRINGS["ru"]["common.hour_abbr"] == "ч"


def test_service_strings_and_case_errors_are_gone():
    from vitals.i18n import STRINGS

    ru = STRINGS["ru"]
    # "2 ПРОМАХОВ" — a caption takes the plain plural, not the form that only
    # agrees with five.
    assert ru["signals.metric_misparse"] == "Промахи"
    assert ru["glp1.toggle_form"] == "Новая запись"
    # "Метрика #1" restated the label of the field directly under it.
    charts = (ROOT / "web/templates/charts/index.html").read_text(encoding="utf-8")
    assert "#' + (index + 1)" not in charts
    assert "{n}" in ru["charts.series_n"]


def test_a_noise_marker_is_not_good_news():
    """The "scale inflated" marker was drawn green. Green means the number is
    good; this one says the number is dirty."""
    measures = (ROOT / "web/templates/weight/measures.html").read_text(encoding="utf-8")
    for line in measures.splitlines():
        if "noise_direction" in line or "noise_up_label" in line or "noise_down_label" in line:
            assert "var(--good)" not in line and "var(--bad)" not in line, line


# ── M3 / V9 — the last two ───────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/skincare", "/supplements"])
async def test_the_modal_heading_is_a_valid_alpine_expression(auth_client, route):
    """The card-header pass moved these two headings into the macro's `attrs`
    escape hatch and the ternary did not survive the trip: the rendered
    attribute read `isEditing ?  ~ t(` and Alpine threw on every open. `tojson`
    is not the fix either — it returns Markup, and `~` against Markup escapes
    the other operand, attribute quotes included."""
    r = await auth_client.get(route)
    assert r.status_code == 200
    expr = re.search(r'x-text="(isEditing[^"]*)"', r.text)
    assert expr, f"{route}: the heading lost its expression"
    assert "~" not in expr.group(1) and "&#34;" not in expr.group(1), expr.group(1)


def test_the_login_page_is_sized_in_dvh_like_the_app_frame():
    login = (ROOT / "web/templates/login.html").read_text(encoding="utf-8")
    assert "85dvh" in login and "85vh" not in login


def test_a_sideways_ribbon_says_it_scrolls():
    """A row of thumbnails ends flush with the card and reads as "that is all of
    them". The chip rows are exempt: a chip clipped by the screen edge is
    itself the signal."""
    assert "mask-image" in _rule(APP_CSS, ".v-ribbon")
    measures = (ROOT / "web/templates/weight/measures.html").read_text(encoding="utf-8")
    assert "v-ribbon flex gap-2 overflow-x-auto" in measures


def test_the_active_chip_is_brought_to_the_row_lead_not_its_middle():
    """Centring puts the previous chip half off-screen, and half a word at the
    left edge reads as a rendering fault."""
    base = (ROOT / "web/templates/base.html").read_text(encoding="utf-8")
    assert "active.offsetLeft - first.offsetLeft" in base
    assert "row.clientWidth - active.offsetWidth" not in base
    # …and a chip that is already on screen is left where it is. /skincare
    # overflows by 24px: scrolling to the lead clamped there and cut 8px off
    # the first chip for nothing.
    assert "lead >= gutter && lead + active.offsetWidth <= row.clientWidth - gutter" in base
