"""Settings, and the two screens a support grant is decided on."""
from __future__ import annotations

from tests.ui.pages.base import Page


class SettingsPage(Page):
    PATH = "/settings"
    NAME = "settings"

    HEIGHT = 'input[name="height_cm"]'
    SAVE_PROFILE = 'button:has-text("Save profile")'
    ACCESS_HISTORY_LINK = "/settings/access"

    def set_height(self, centimetres: int) -> "SettingsPage":
        self.page.fill(self.HEIGHT, str(centimetres))
        self._act(self.SAVE_PROFILE)
        return self

    @property
    def height(self) -> str:
        return self.page.input_value(self.HEIGHT)


class AccessHistoryPage(Page):
    """The patient's side: who has asked to open this record, and the answers.

    Every ask ever made, including the refused ones — the question this list is
    asked is "has anybody been reading my record", and a history of only the
    yeses cannot answer it.
    """

    PATH = "/settings/access"
    NAME = "access history"

    ALLOW = 'button:has-text("Allow")'
    REFUSE = 'button:has-text("Refuse")'
    STOP_NOW = 'button:has-text("Stop it now")'

    #: The banner the chrome draws on *every* page while a grant is live. Read
    #: from here because that is where its link points, and the point of the
    #: banner is that it is reachable from wherever the patient happens to be.
    BANNER_LINK = "/settings/access"

    def allow(self) -> "AccessHistoryPage":
        self._act(self.ALLOW)
        return self

    def refuse(self) -> "AccessHistoryPage":
        self._act(self.REFUSE)
        return self

    def stop_the_live_grant(self) -> "AccessHistoryPage":
        self._act(self.STOP_NOW)
        return self

    @property
    def has_a_pending_ask(self) -> bool:
        return self.page.locator(self.ALLOW).count() > 0


class SupportConsolePage(Page):
    """The administrator's side: what they hold, what they asked for, and asking."""

    PATH = "/settings/platform/support"
    NAME = "support console"

    SUBJECT = 'select[name="subject_id"]'
    REASON = 'textarea[name="reason"]'
    HOURS = 'input[name="hours"]'
    TICKET = 'input[name="ticket_reference"]'
    SUBMIT = 'button:has-text("Ask")'
    OPEN_RECORD = 'a:has-text("Open the record")'
    HAND_BACK = 'button:has-text("Hand it back")'
    WITHDRAW = 'button:has-text("Take it back")'

    @staticmethod
    def domain(key: str) -> str:
        return f'input[name="domains"][value="{key}"]'

    def ask_for(
        self,
        *,
        record: str,
        reason: str,
        domains: tuple[str, ...],
        hours: int | None = None,
        ticket: str | None = None,
    ) -> "SupportConsolePage":
        """Fill the form a patient will read every field of."""

        self.page.select_option(self.SUBJECT, label=record)
        self.page.fill(self.REASON, reason)
        if hours is not None:
            self.page.fill(self.HOURS, str(hours))
        if ticket is not None:
            self.page.fill(self.TICKET, ticket)
        for key in domains:
            self.page.check(self.domain(key))
        self._act(self.SUBMIT)
        return self

    def open_the_record(self):
        from tests.ui.pages.care import CareRecordPage

        self._act(self.OPEN_RECORD)
        return self._become(CareRecordPage)

    def hand_the_grant_back(self) -> "SupportConsolePage":
        self._act(self.HAND_BACK)
        return self

    def withdraw_the_ask(self) -> "SupportConsolePage":
        self._act(self.WITHDRAW)
        return self

    @property
    def holds_a_grant(self) -> bool:
        return self.page.locator(self.OPEN_RECORD).count() > 0

    @property
    def has_an_unanswered_ask(self) -> bool:
        return self.page.locator(self.WITHDRAW).count() > 0
