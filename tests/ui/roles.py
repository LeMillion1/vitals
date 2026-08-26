"""One signed-in person, and the screens they can ask for.

The layer between a test and a page object. It exists so a test never builds a
URL: ``doctor.record_of("timur")`` says who is looking at whose record, which is
the thing worth reading in a failure, and it resolves the subject id from the
seeded database rather than from a constant that a reseed invalidates.
"""
from __future__ import annotations

from typing import Iterable, TypeVar

from tests.ui.pages import (
    AccessHistoryPage,
    CareRecordPage,
    CareRosterPage,
    ConversationsPage,
    Page,
    RefusalPage,
    SettingsPage,
    SupportConsolePage,
)
from tests.ui.pages.care import PatientConversationsPage

P = TypeVar("P", bound=Page)


class Role:
    """A person with a browser."""

    def __init__(self, username: str, browser_page, installation, complaints):
        self.username = username
        self.page = browser_page
        self.installation = installation
        self.complaints = complaints

    def __repr__(self) -> str:  # pragma: no cover - failure output only
        return f"<{self.username}>"

    # ── Opening a screen ─────────────────────────────────────────────────────

    def _open(
        self, page_class: type[P], *parts: str, expect_status: Iterable[int] = ()
    ) -> P:
        screen = page_class(
            self.page, self.installation.base_url, self.username, self.complaints
        )
        return screen.open(*parts, expect_status=expect_status)

    def settings(self) -> SettingsPage:
        return self._open(SettingsPage)

    def access_history(self) -> AccessHistoryPage:
        return self._open(AccessHistoryPage)

    def support_console(self) -> SupportConsolePage:
        record_id = getattr(self, "_support_record_id", None)
        if record_id is None:
            page = self._open(SupportConsolePage)
        else:
            screen = SupportConsolePage(
                self.page,
                self.installation.base_url,
                self.username,
                self.complaints,
            )
            screen.PATH = f"{SupportConsolePage.PATH}?record_id={record_id}"
            page = screen.open()
        page.record_id_for = self.installation.subject_named
        page.remember_record = lambda value: setattr(
            self, "_support_record_id", value
        )
        return page

    def roster(self) -> CareRosterPage:
        return self._open(CareRosterPage)

    def record_of(self, username: str) -> CareRecordPage:
        return self._open(CareRecordPage, self.installation.subject_of(username))

    def conversations_about(self, username: str) -> ConversationsPage:
        return self._open(
            ConversationsPage, self.installation.subject_of(username)
        )

    def my_conversations(self) -> PatientConversationsPage:
        return self._open(PatientConversationsPage)

    def visit(self, path: str, *, expect_status: Iterable[int] = ()) -> Page:
        """Somewhere with no page object, for checking chrome or a refusal."""

        class _Anywhere(Page):
            PATH = path
            NAME = path

        return self._open(_Anywhere, expect_status=expect_status)

    # ── Being refused ────────────────────────────────────────────────────────

    def is_refused_from(self, screen: str, *, status: int) -> RefusalPage:
        """Ask for a screen expecting a no, and get the refusal to check.

        The status is named by the caller because *which* no matters: 403 is
        "not you", 404 is "no such thing or not for you", 409 is "this page is
        about a record you do not have". A test that accepts any of them is not
        checking the thing it looks like it is checking.
        """

        path, parts = self._resolve(screen)
        refusal = RefusalPage(
            self.page, self.installation.base_url, self.username, self.complaints
        )
        refusal.PATH = path
        refusal.NAME = screen
        refusal.open(*parts, expect_status=(status,))
        assert refusal.status == status, (
            f"{self.username} asked for {screen} and got "
            f"{refusal.status}, not {status}"
        )
        return refusal

    def _resolve(self, screen: str) -> tuple[str, tuple[str, ...]]:
        known = {
            "the support console": (SupportConsolePage.PATH, ()),
            "their access history": (AccessHistoryPage.PATH, ()),
            "the dashboard": ("/today", ()),
            "the settings page": (SettingsPage.PATH, ()),
        }
        if screen in known:
            return known[screen]
        if screen.startswith("the record of "):
            who = screen.removeprefix("the record of ")
            return CareRecordPage.PATH, (self.installation.subject_of(who),)
        raise AssertionError(f"no screen called {screen!r}")
