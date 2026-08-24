"""The shared half of every screen: how to get to it and what to trust on it."""
from __future__ import annotations

from typing import Iterable

from tests.ui.console import Complaints


class Page:
    """One screen, for one signed-in account.

    Subclasses declare ``PATH`` and their locators, and nothing else about
    getting there: opening a page, waiting for it, and checking it did not
    scroll sideways is the same everywhere and is done once, here.
    """

    #: Where this screen lives. ``{}`` placeholders are filled by ``open``.
    PATH: str = "/"
    #: What the screen is called in a failure message.
    NAME: str = "page"

    def __init__(self, browser_page, base_url: str, who: str, complaints: Complaints):
        self.page = browser_page
        self.base_url = base_url
        self.who = who
        self.complaints = complaints

    # ── Getting there ────────────────────────────────────────────────────────

    def open(self, *parts: str, expect_status: Iterable[int] = ()) -> "Page":
        """Go to this screen and wait for it to settle.

        ``expect_status`` declares a refusal the test is deliberately
        provoking. Everything else at 400 or above is collected as a complaint,
        so a flow cannot walk past a 500 on its way to a passing assertion.
        """

        self.complaints.expected_statuses = set(expect_status)
        response = self.page.goto(
            self.base_url + self.PATH.format(*parts), wait_until="networkidle"
        )
        # htmx swaps its fragments in and Chart.js draws after the network goes
        # quiet, so "loaded" is a moment later than Playwright thinks.
        self.page.wait_for_timeout(500)
        self.complaints.check_overflow(self.page, self.who, self.NAME)
        self.complaints.expected_statuses = set()
        self.status = response.status if response else 0
        return self

    def _act(self, selector: str) -> None:
        """Click something and wait for whatever it caused."""

        self.page.click(selector)
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(400)
        self.complaints.check_overflow(self.page, self.who, self.NAME)

    def _become(self, page_class: type["Page"]) -> "Page":
        """Reinterpret the browser's current location as another screen."""

        return page_class(self.page, self.base_url, self.who, self.complaints)

    # ── Reading it ───────────────────────────────────────────────────────────

    @property
    def text(self) -> str:
        return self.page.inner_text("body")

    @property
    def html(self) -> str:
        return self.page.content()

    @property
    def url(self) -> str:
        return self.page.url

    def says(self, fragment: str) -> bool:
        return fragment in self.text

    def offers(self, href: str) -> bool:
        """Whether the page contains a link somebody could click to get there.

        Asked about the markup rather than the visible text because this is the
        question "is it reachable", and a link in a nav rail is reachable
        without being a word in the body copy.
        """

        return f'href="{href}"' in self.html


class RefusalPage(Page):
    """Any screen that said no.

    Its own class because a refusal has a contract of its own: it is a rendered
    page and it offers somewhere to go. Three handlers used to answer a browser
    with one unstyled sentence and no link, which is correct information and a
    dead end, and the difference is invisible to a status-code assertion.
    """

    NAME = "refusal"

    def assert_is_a_page_with_a_way_out(self) -> None:
        assert "<html" in self.html.lower(), (
            f"{self.who} was refused with a bare string rather than a page"
        )
        assert 'href="/' in self.html, (
            f"{self.who} was refused by a page that offers nowhere to go"
        )
