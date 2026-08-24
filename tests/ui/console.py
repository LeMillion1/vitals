"""What a page complained about while nobody was asserting anything.

Every browser test collects this, and every browser test fails on it. The
reasoning is the same one that makes these tests worth having at all: a flow can
pass each of its own assertions while the page it walked through logged a 500,
threw in a script, or scrolled sideways on a phone — and each of those is a
defect somebody would meet.

Deliberately not a warning to read later. A complaint nobody has to act on is a
complaint that accumulates until the list is ignored.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Complaints:
    """Everything one browser said, gathered across the whole test."""

    entries: list[str] = field(default_factory=list)
    #: Statuses a test has declared it is deliberately provoking. A 404 that a
    #: test exists to prove is not noise; the same 404 anywhere else is.
    expected_statuses: set[int] = field(default_factory=set)

    def note(self, entry: str) -> None:
        if entry not in self.entries:
            self.entries.append(entry)

    def watch(self, page, who: str) -> None:
        """Attach to one Playwright page."""

        page.on("console", lambda message: self._console(message, who))
        page.on(
            "pageerror",
            lambda error: self.note(f"{who}: page error — {str(error)[:200]}"),
        )
        page.on("response", lambda response: self._response(response, who))

    def _console(self, message, who: str) -> None:
        if message.type != "error":
            return
        # "Failed to load resource" duplicates the response listener, which says
        # the status and the URL — strictly more than this does.
        if "Failed to load resource" in message.text:
            return
        self.note(f"{who}: console error — {message.text[:200]}")

    def _response(self, response, who: str) -> None:
        if response.status < 400 or response.status in self.expected_statuses:
            return
        self.note(f"{who}: HTTP {response.status} on {response.url[:120]}")

    def check_overflow(self, page, who: str, where: str) -> None:
        """Whether the document is wider than the window it is in.

        The check a phone layout needs and the one nobody performs by eye: a
        page that scrolls sideways looks fine in a screenshot and is unusable in
        a hand.
        """

        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth"
        )
        if overflow > 1:
            self.note(f"{who}: {where} scrolls sideways by {overflow}px")

    def raise_if_any(self) -> None:
        if self.entries:
            listed = "\n  ".join(self.entries)
            raise AssertionError(
                f"the browser complained {len(self.entries)} time(s):\n  {listed}"
            )
