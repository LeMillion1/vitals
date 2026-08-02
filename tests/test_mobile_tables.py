"""Contracts for the phone's table rows (mobile pass 3 — "no sideways data").

Ten pages carried tables wider than the phone column; they scrolled sideways
inside the card and cut the values that didn't fit. Under 768px those tables lay
themselves out as stacked rows instead, driven entirely by CSS — so the contract
is (a) every data table opts in, (b) every cell in an opted-in table says what
it is, and (c) the phone block actually undoes width, truncation and the inner
scroll cap.

Read as text like tests/test_mobile_shell.py: a rendered page can't tell us which
media block a rule lives in, and on an empty database it can't tell us about the
cells either.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP_CSS = (ROOT / "web/static/vitals.css").read_text(encoding="utf-8")

# Every page the review found scrolling sideways, in the order it ranked them.
TABLE_TEMPLATES = [
    "labs/index.html",
    "nutrition/index.html",
    "weight/index.html",
    "glp1/index.html",
    "signals/index.html",
    "garmin/index.html",
    "genetics/index.html",
    "hrt/index.html",
    "weight/measures.html",
]

# A stacked cell is either a labelled value, one of the full-width lines, or the
# "no records" row.
ROW_CLASSES = {"v-row-date", "v-row-title", "v-row-wide", "v-row-actions"}


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


PHONE_CSS = "\n".join(_blocks(APP_CSS, "(max-width: 767px)"))


class _StackedCells(HTMLParser):
    """Collect the <td> attributes of every table marked .v-rows.

    Parses the Jinja source, not a render: on an empty database most of these
    tables have nothing but their empty-state row, and it's the columns that
    need checking.
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.depth = 0          # how deep we are inside a .v-rows table
        self.cells: list[dict] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.depth += 1 if "v-rows" in (attrs.get("class") or "") else 0
        elif tag == "td" and self.depth:
            self.cells.append(attrs)

    def handle_endtag(self, tag):
        if tag == "table" and self.depth:
            self.depth -= 1


def _stacked_cells(template: str) -> list[dict]:
    parser = _StackedCells()
    parser.feed((ROOT / "web/templates" / template).read_text(encoding="utf-8"))
    return parser.cells


@pytest.mark.parametrize("template", TABLE_TEMPLATES)
def test_every_history_table_stacks_on_a_phone(template):
    assert "v-table v-rows" in (ROOT / "web/templates" / template).read_text(encoding="utf-8")


@pytest.mark.parametrize("template", TABLE_TEMPLATES)
def test_every_stacked_cell_says_what_it_is(template):
    """A value with no header above it is a number with no name. Once the
    header row is gone, the cell has to carry its own label — or declare itself
    a date, a title, prose or the actions."""
    cells = _stacked_cells(template)
    assert cells, f"{template}: no stacked cells found"
    for attrs in cells:
        classes = set((attrs.get("class") or "").split())
        described = (
            "data-label" in attrs
            or "colspan" in attrs
            or classes & ROW_CLASSES
        )
        assert described, f"{template}: <td class={attrs.get('class')!r}> has no label"


@pytest.mark.parametrize("template", TABLE_TEMPLATES)
def test_no_stacked_table_forces_a_minimum_width(template):
    """/nutrition pinned its tables at 560px and 620px inline, which no media
    query can undo — the row would still have been wider than the phone."""
    source = (ROOT / "web/templates" / template).read_text(encoding="utf-8")
    for tag in re.findall(r"<table[^>]*>", source):
        if "v-rows" in tag:
            assert "min-width" not in tag


def test_phone_lays_the_row_out_as_a_grid():
    rule = re.search(
        r"\.v-table\.v-rows > tbody > tr \{([^}]*)\}", PHONE_CSS
    )
    assert rule, "no stacked-row rule in the phone block"
    assert "display: grid;" in rule.group(1)
    assert "repeat(2, minmax(0, 1fr))" in rule.group(1)
    assert "content: attr(data-label);" in PHONE_CSS


def test_phone_undoes_every_cut_the_desktop_cell_makes():
    """Truncation is what the review actually complained about: a reference
    range that read "3.6–5.…" is worse than no range. Both the cell's own cut
    and the one on the div inside it have to go."""
    cell = re.search(r"\.v-table\.v-rows > tbody > tr > td \{([^}]*)\}", PHONE_CSS)
    assert cell
    assert "white-space: normal;" in cell.group(1)
    assert "max-width: none;" in cell.group(1)
    assert "text-overflow: clip;" in cell.group(1)
    # …but not by breaking anywhere: a half-width cell is narrow enough that
    # `anywhere` split the number itself — /labs showed a vitamin D of 28 as
    # "2" over "8", which is a wrong reading, not a cramped one.
    assert "overflow-wrap: break-word;" in cell.group(1)
    assert "overflow-wrap: anywhere" not in cell.group(1)
    inner = re.search(
        r"\.v-table\.v-rows > tbody > tr > td \.truncate \{([^}]*)\}", PHONE_CSS
    )
    assert inner and "white-space: normal;" in inner.group(1)


def test_phone_drops_the_inner_scroll_cap():
    """A capped list inside a page is a scroll trap under a finger: the list
    moves and the page behind it stands still. Nothing is too wide to need the
    cap any more."""
    rule = re.search(
        r"\.v-page \.v-table-wrap:has\(\.v-rows\),\s*"
        r"\.v-page \.v-card-inset:has\(\.v-rows\) \{([^}]*)\}",
        PHONE_CSS,
    )
    assert rule, "the stacked wrappers keep their max-height"
    assert "max-height: none;" in rule.group(1)


def test_no_comment_is_left_half_closed():
    """A `*/` with no `/*` in front of it ends the comment early and turns the
    prose that follows into a selector — the browser then throws away the rule
    behind it, silently. That is how the scroll-cap rule above went missing."""
    assert APP_CSS.count("/*") == APP_CSS.count("*/")


def test_a_cell_that_says_nothing_new_drops_out_on_a_phone():
    """A stacked row prints its label even when the cell is blank, so "source:
    manual" ×8 and "note: —" ×8 cost the weight history sixteen lines to say
    nothing. Gate them and the phone block takes them out."""
    assert ".v-table.v-rows .hide-xs" in PHONE_CSS
    gated = {
        "weight/index.html": ("w.source == 'manual'", "not w.note"),
        "glp1/index.html": ("not inj.site",),
        "hrt/index.html": ("not d.site",),
    }
    for template, conditions in gated.items():
        source = (ROOT / "web/templates" / template).read_text(encoding="utf-8")
        for condition in conditions:
            assert f"{{% if {condition} %}}hide-xs" in source, f"{template}: {condition}"


def test_the_digest_contains_its_own_tables():
    """The report body is written by the model, so its width isn't ours to
    predict — one wide table in "previous reports" moved the whole page."""
    rule = re.search(r"\.v-digest table \{([^}]*)\}", APP_CSS)
    assert rule and "overflow-x: auto;" in rule.group(1)


@pytest.mark.asyncio
async def test_pages_render_the_stacked_table(auth_client):
    for path in ("/weight", "/labs", "/garmin", "/nutrition"):
        r = await auth_client.get(path)
        assert r.status_code == 200
        assert "v-table v-rows" in r.text, path
