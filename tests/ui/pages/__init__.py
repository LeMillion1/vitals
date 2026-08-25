"""One class per screen: where its controls are, and what it can do next.

A locator lives in exactly one place. That is the whole point — the selector
`button:has-text("Allow")` scattered through nine tests is nine edits when the
button is renamed and eight silent passes when only some get made.

Navigation returns the page it lands on, so a flow reads as the sentence it is:
``console.open_the_record(scope_label="Labs").shows_section("Labs")``.
"""
from tests.ui.pages.base import Page, RefusalPage
from tests.ui.pages.care import (
    CareRecordPage,
    CareRosterPage,
    ConversationPage,
    ConversationsPage,
)
from tests.ui.pages.settings import (
    AccessHistoryPage,
    ExternalKeysCard,
    SettingsPage,
    SupportConsolePage,
)

__all__ = [
    "AccessHistoryPage",
    "CareRecordPage",
    "CareRosterPage",
    "ExternalKeysCard",
    "ConversationPage",
    "ConversationsPage",
    "Page",
    "RefusalPage",
    "SettingsPage",
    "SupportConsolePage",
]
