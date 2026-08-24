"""The professional's screens, which platform support now shares.

Deliberately the same page objects for both readers, because they are the same
screens: what may be shown is decided by the policy from whichever grant is in
hand, and a second set of classes would be a second place for "what is on this
page" to drift.
"""
from __future__ import annotations

from tests.ui.pages.base import Page


class CareRosterPage(Page):
    PATH = "/care"
    NAME = "roster"


class CareRecordPage(Page):
    """One person's record, as this reader is allowed to see it."""

    PATH = "/care/{}"
    NAME = "record"

    CONVERSATIONS = 'a:has-text("Conversations")'
    NOTE_FORM = 'form[action$="/note"]'
    WITHHELD = "Not shared with you"

    def open_conversations(self) -> "ConversationsPage":
        self._act(self.CONVERSATIONS)
        return self._become(ConversationsPage)

    @property
    def offers_the_note_form(self) -> bool:
        return self.page.locator(self.NOTE_FORM).count() > 0

    @property
    def withheld_line(self) -> str:
        """What the page says is outside this reader's grant.

        Empty when nothing is: a reader who may see everything gets no line, and
        a test that wants one should say so rather than match an empty string.
        """

        body = self.text
        if self.WITHHELD not in body:
            return ""
        return body.split(self.WITHHELD, 1)[1].split("\n", 1)[0]


class ConversationsPage(Page):
    """The list of care-team threads about one person."""

    PATH = "/care/{}/messages"
    NAME = "conversations"

    TITLE = 'input[name="title"]'
    FIRST_MESSAGE = 'textarea[name="body"]'
    START = 'button:has-text("Start")'

    def start_a_thread(self, title: str, first_message: str) -> "ConversationPage":
        self.page.fill(self.TITLE, title)
        self.page.fill(self.FIRST_MESSAGE, first_message)
        self._act(self.START)
        return self._become(ConversationPage)

    def open_thread(self, title: str) -> "ConversationPage":
        self._act(f'a:has-text("{title}")')
        return self._become(ConversationPage)

    def lists(self, title: str) -> bool:
        return self.page.locator(f'a:has-text("{title}")').count() > 0


class ConversationPage(Page):
    """One thread, with the patient in it."""

    PATH = "/care/{}/messages/{}"
    NAME = "conversation"

    BODY = 'textarea[name="body"]'
    SEND = 'button:has-text("Send")'

    def say(self, message: str) -> "ConversationPage":
        self.page.fill(self.BODY, message)
        self._act(self.SEND)
        return self

    def said_in_order(self, *messages: str) -> bool:
        """Whether these appear, in this order, in the rendered thread.

        Order is asserted rather than membership because it is the thing that
        broke: two messages written in one transaction shared a timestamp, the
        thread fell back to a random-UUID tiebreak, and a reply rendered above
        the message it answered.
        """

        body = self.text
        positions = []
        for message in messages:
            if message not in body:
                return False
            positions.append(body.index(message))
        return positions == sorted(positions)


class PatientConversationsPage(ConversationsPage):
    """The patient's own door, which needs no subject id they have no way to know."""

    PATH = "/messages"
