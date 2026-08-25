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
    RESTRICTED = "Sections approved for this opening"

    def open_conversations(self) -> "ConversationsPage":
        self._act(self.CONVERSATIONS)
        return self._become(ConversationsPage)

    @property
    def offers_the_note_form(self) -> bool:
        return self.page.locator(self.NOTE_FORM).count() > 0

    @property
    def names_only_approved_sections(self) -> bool:
        return self.RESTRICTED in self.text

    def shows_section(self, label: str) -> bool:
        """Whether an exact record-card heading is rendered."""

        headings = {
            heading.strip().casefold()
            for heading in self.page.locator(".mh-eyebrow").all_inner_texts()
        }
        return label.strip().casefold() in headings


class ConversationsPage(Page):
    """The list of care-team threads about one person."""

    PATH = "/care/{}/messages"
    NAME = "conversations"

    TITLE = 'input[name="title"]'
    FIRST_MESSAGE = 'textarea[name="body"]'
    START = 'button:has-text("Start")'

    def start_a_thread(self, title: str, first_message: str) -> "ConversationPage":
        title_field = self.page.locator(self.TITLE)
        if not title_field.is_visible():
            # A populated inbox keeps creation collapsed so current messages
            # stay above the phone fold. Open the native disclosure just as a
            # person must before interacting with the form.
            title_field.locator("xpath=ancestor::details[1]/summary").click()
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
