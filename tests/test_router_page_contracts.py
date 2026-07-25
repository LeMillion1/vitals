"""Static contracts between routers and the pages that drive them.

Two failure modes, both invisible to every other test because the code on each
side is individually correct:

* a router answers **409** but its page has no violations modal, so the save
  dies silently — and the conflict engine is data-driven, so a rule flipping
  from ``soft_warn`` to ``hard_block`` in ``conflict_rules.yaml`` starts this
  with no code change at all;
* a **delete** endpoint exists that no page ever posts to, so the only way to
  remove a bad row is the API.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTERS = ROOT / "web/routers"
TEMPLATES = ROOT / "web/templates"

CONFLICT_MODAL = '{% include "partials/conflict_modal.html" %}'
_TEMPLATE_REF = re.compile(r'"([\w/]+\.html)"')


def _pages_that_can_409() -> dict[str, list[str]]:
    """{router name: templates it renders} for routers handling ConflictBlocked."""
    pages: dict[str, list[str]] = {}
    for router in sorted(ROUTERS.glob("*.py")):
        source = router.read_text(encoding="utf-8")
        if "ConflictBlocked" not in source:
            continue
        pages[router.name] = [
            name
            for name in _TEMPLATE_REF.findall(source)
            if (TEMPLATES / name).exists()
        ]
    return pages


def test_every_conflict_aware_page_renders_the_override_modal():
    pages = _pages_that_can_409()
    # Guard the scan itself: a rename that stops matching would make this pass
    # vacuously.
    assert len(pages) >= 6, pages

    missing = [
        f"{router} → {name}"
        for router, names in pages.items()
        for name in names
        if CONFLICT_MODAL not in (TEMPLATES / name).read_text(encoding="utf-8")
    ]
    assert not missing, f"409 with no violations modal: {missing}"


def test_every_delete_route_has_a_button_somewhere():
    """A delete endpoint no page ever posts to is data the owner can only remove
    through the API — four of them (a lab result, a skincare diary entry, a skin
    observation, a whole HRT cycle) were unreachable from the UI."""
    templates = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(TEMPLATES.rglob("*.html"))
    )
    unreachable = []
    for router in sorted(ROUTERS.glob("*.py")):
        source = router.read_text(encoding="utf-8")
        prefix_match = re.search(r'APIRouter\(prefix="([^"]*)"', source)
        prefix = prefix_match.group(1) if prefix_match else ""
        for path in re.findall(r'@router\.post\("([^"]*delete[^"]*)"\)', source):
            route = prefix + path
            # {id} placeholders become "whatever the template renders there".
            pattern = re.sub(r"\\\{[^}]+\\\}", '[^"]+', re.escape(route))
            if not re.search(f'action="{pattern}"', templates):
                unreachable.append(route)
    assert not unreachable, f"delete routes with no UI: {unreachable}"


def test_hrt_dose_form_is_wired_to_the_override_controller():
    """The modal is inert without the controller that fills it: the page scope
    must be ``protocolForm()`` and the dose form must submit through it."""
    page = (TEMPLATES / "hrt/index.html").read_text(encoding="utf-8")
    assert 'x-data="protocolForm()"' in page
    assert 'action="/hrt/dose"' in page
    form = page[page.index('action="/hrt/dose"') :]
    form = form[: form.index("</form>")]
    assert "submitForm($event)" in form
    assert 'hx-boost="false"' in form
