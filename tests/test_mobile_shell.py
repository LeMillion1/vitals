"""Contracts for the phone shell (mobile pass 1 — "give the screen back").

Read as text, like tests/test_design_handoff_vitals.py: the invariants here are
about which rule exists in which media block, and a rendered page can't tell us
that. The one runtime check is the macro's structure, which a render *can*.
"""

import re

import pytest

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MASTHEAD_CSS = (ROOT / "web/static/vitals-masthead.css").read_text(encoding="utf-8")
APP_CSS = (ROOT / "web/static/vitals.css").read_text(encoding="utf-8")
BASE_TEMPLATE = (ROOT / "web/templates/base.html").read_text(encoding="utf-8")
NAV_MOBILE = (ROOT / "web/templates/partials/nav_mobile.html").read_text(encoding="utf-8")


def _blocks(css: str, query: str) -> list[str]:
    """Every ``@media <query> { … }`` body in `css`, brace-matched."""
    out = []
    needle = "@media " + query
    start = css.find(needle)
    while start != -1:
        i = css.index("{", start)
        depth, j = 0, i
        while True:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(css[i + 1:j])
        start = css.find(needle, j)
    return out


PHONE_CSS = "\n".join(_blocks(MASTHEAD_CSS, "(max-width: 767px)"))


def _rule(css: str, selector: str) -> str:
    """Every declaration `css` makes for exactly `selector`, concatenated.

    Concatenated rather than "the first one": the phone's rules for .mh-tabs are
    spread over three blocks written at three different times, and what the
    element ends up with is their sum.
    """
    found = re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert found, f"no rule for {selector!r}"
    return "\n".join(found)


def test_phone_drops_the_wordmark_strip_but_keeps_the_notch():
    """52px of chrome said "Vitals" to someone already inside the app. The
    element survives as a bare safe-area spacer so <main> still starts below the
    status bar on pages that render no section bar (/today, /more)."""
    topbar = _rule(PHONE_CSS, "body.ui-masthead .mh-topbar")
    assert "height: max(env(safe-area-inset-top), var(--sat, 0px));" in topbar
    assert "min-height: 0;" in topbar
    # Hidden at both widths for a whole release, so the markup went with it.
    assert "mh-topbar-brand" not in MASTHEAD_CSS
    assert "mh-topbar-brand" not in BASE_TEMPLATE


def test_section_bar_is_sticky_against_the_page_not_the_header():
    """A sticky element positions against its nearest ancestor box, so .mh-head
    has to stop being one — otherwise the bar unsticks two swipes into the page.
    The chips pin under it and take its place when it slides away."""
    assert "display: contents" in _rule(PHONE_CSS, "body.ui-masthead .mh-head")
    bar = _rule(PHONE_CSS, "body.ui-masthead .mh-bar")
    assert "position: sticky;" in bar
    assert "top: 0;" in bar
    tabs = _rule(PHONE_CSS, "body.ui-masthead .mh-tabs")
    assert "position: sticky;" in tabs
    assert "top: 52px;" in tabs
    assert "translateY(-100%)" in _rule(PHONE_CSS, "body.mh-bar-hidden .mh-bar")
    assert "top: 0" in _rule(PHONE_CSS, "body.mh-bar-hidden .mh-tabs")


def test_chip_row_keeps_the_page_gutter_when_it_snaps():
    """The row is full-bleed (negative margin) and pays the gutter back as
    padding — but a snap port aligns to the scroll box, not to its padding, so
    the first chip snapped to x=0 and sat against the screen edge while the
    title beside it kept its 16px."""
    tabs = _rule(PHONE_CSS, "body.ui-masthead .mh-tabs")
    assert "padding: 8px var(--mh-gutter);" in tabs
    assert "scroll-padding-inline: var(--mh-gutter);" in tabs


def test_page_starts_right_under_the_status_bar():
    """The first thing on a page is the sticky bar, which already centres its
    title inside 52px. 22px of page padding on top of that made a 37px band of
    nothing between the status bar and the section name."""
    assert "padding: 8px 16px 22px !important;" in PHONE_CSS
    assert "padding: 22px 16px !important;" not in PHONE_CSS


def test_eyebrow_is_hidden_only_inside_the_section_header():
    """Third statement of the section name after the bar and the active chip.
    /today reuses .mh-eyebrow for its date/time line — that one stays."""
    assert "display: none" in _rule(PHONE_CSS, "body.ui-masthead .mh-head > .mh-eyebrow")
    assert "body.ui-masthead .mh-eyebrow {" not in PHONE_CSS


def test_scroll_direction_toggles_the_bar():
    assert "mh-bar-hidden" in BASE_TEMPLATE
    assert "Math.abs(y - last) < 8" in BASE_TEMPLATE


def test_primary_figure_never_outgrows_the_title():
    """Both sizes are declared in the same media block. They used to move on
    different ones — 900 lifted the figure to 32px, 560 dropped the title to
    27px and left the figure alone — which drew a "—" larger than the word under
    it on fourteen pages."""
    assert "body.ui-masthead .mh-title { font-size: var(--text-title); }" in PHONE_CSS
    assert (
        "body.ui-masthead .mh-metric-value.is-primary { font-size: var(--text-title); }"
        in PHONE_CSS
    )


def test_card_padding_is_declared_once():
    """24px from the utility, 16px at 640 and 18px at 560 used to stack, and the
    18px nobody chose was the one that won."""
    assert len(re.findall(r"\.v-card\.p-6\s*\{", APP_CSS)) == 1
    assert "padding: 18px" not in APP_CSS
    assert "padding: var(--space-4);" in _rule(APP_CSS, ".v-card.p-6")


def test_bottom_bar_is_56px():
    assert "calc(56px + max(env(safe-area-inset-bottom)" in NAV_MOBILE
    assert "78px" not in NAV_MOBILE


def test_role_bottom_bar_has_no_empty_personal_columns():
    assert "mh-role-bnav-{{ role_slots }}" in NAV_MOBILE
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in MASTHEAD_CSS
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in MASTHEAD_CSS


@pytest.mark.asyncio
async def test_header_renders_four_independent_blocks(auth_client):
    """The macro emits bar / eyebrow / tabs / metrics as siblings — no wrapper
    couples the H1 to the key figures any more, which is what let the phone put
    one in a 52px bar and leave the other in the content."""
    r = await auth_client.get("/labs")
    assert r.status_code == 200
    for cls in ('class="mh-bar"', 'class="mh-eyebrow"', 'class="mh-tabs"', 'class="mh-metrics"'):
        assert cls in r.text
    assert "mh-hero-row" not in r.text
    assert "mh-eyebrow-row" not in r.text
    # The action lives in the bar, beside the title, and nowhere else.
    bar = r.text.split('class="mh-bar"', 1)[1].split('class="mh-eyebrow"', 1)[0]
    assert 'class="mh-title"' in bar
    assert 'class="mh-actions"' in bar
