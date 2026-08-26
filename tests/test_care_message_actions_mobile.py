"""Static phone contracts for the care-conversation actions."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "web/templates/care/messages.html").read_text(encoding="utf-8")
APP_CSS = (ROOT / "web/static/vitals.css").read_text(encoding="utf-8")


def _phone_blocks(css: str) -> str:
    blocks: list[str] = []
    needle = "@media (max-width: 767px)"
    start = css.find(needle)
    while start != -1:
        opening = css.index("{", start)
        depth = 0
        cursor = opening
        while cursor < len(css):
            depth += (css[cursor] == "{") - (css[cursor] == "}")
            if depth == 0:
                break
            cursor += 1
        blocks.append(css[opening + 1 : cursor])
        start = css.find(needle, cursor)
    return "\n".join(blocks)


PHONE_CSS = _phone_blocks(APP_CSS)


def _rule(css: str, selector: str) -> str:
    marker = selector + " {"
    return css.split(marker, 1)[1].split("}", 1)[0]


def test_message_actions_use_the_shared_touch_components():
    assert 'data-care-message-editor' in TEMPLATE
    assert '<summary class="v-btn-ghost text-xs inline-flex cursor-pointer">' in TEMPLATE
    assert 'data-care-thread-action=' in TEMPLATE
    assert 'class="v-btn-ghost text-xs"' in TEMPLATE
    assert "min-height: 2.75rem" in _rule(PHONE_CSS, ".v-btn-ghost")
    assert "min-height: 2.75rem" in _rule(PHONE_CSS, "summary")


def test_each_correction_control_has_a_unique_accessible_label():
    assert 'for="message-edit-{{ message.id }}" class="v-label"' in TEMPLATE
    assert 'id="message-edit-{{ message.id }}" name="body"' in TEMPLATE
    assert 't("care.message_edit_label")' in TEMPLATE
