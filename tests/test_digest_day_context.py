"""The weekly digest reads day_context — what kind of day each one was.

Without this block the report sees the numbers and none of the circumstances that
produced them: it cannot tell a bad HRV week from a week of heavy office days.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from vitals.enums import Source
from vitals.services import digest_service, signals_service

DAY = date(2026, 6, 10)

pytestmark = pytest.mark.usefixtures("all_modules_on")


class FakeLLM:
    digest_model = "fake/model"

    def __init__(self):
        self.prompts = []

    async def complete_text(self, prompt, *, system=None, max_tokens=None, **kw):
        self.prompts.append((system, prompt))
        return "Три тяжёлых дня подряд — отсюда и просадка."


async def test_day_context_reaches_the_digest_context(db_session):
    await signals_service.set_day_context(
        db_session, DAY - timedelta(days=1), answers={"where": "remote", "gym": True}
    )
    await signals_service.set_day_context(
        db_session,
        DAY,
        answers={"where": "office", "gym": False, "load": "heavy"},
    )
    # The template's guess for a day he never corrected — present, but flagged as
    # a guess rather than an answer.
    await signals_service.set_day_context(
        db_session,
        DAY - timedelta(days=2),
        answers={"where": "office", "gym": False},
        source=Source.TEMPLATE.value,
    )
    # Outside the 7-day window — must not leak in.
    await signals_service.set_day_context(
        db_session, DAY - timedelta(days=30), answers={"where": "off", "gym": False}
    )
    await db_session.commit()

    ctx = await digest_service.assemble_context(db_session, on_date=DAY, period_days=7)

    days = ctx["day_context"]
    assert [d["date"] for d in days] == [
        (DAY - timedelta(days=2)).isoformat(),
        (DAY - timedelta(days=1)).isoformat(),
        DAY.isoformat(),
    ], "chronological, and the old day stays out of the period"
    assert days[-1]["answers"] == {"where": "office", "gym": False, "load": "heavy"}
    assert days[0]["source"] == Source.TEMPLATE.value
    assert days[-1]["source"] == Source.MANUAL.value

    # The model ignores keys the system prompt never names.
    llm = FakeLLM()
    await digest_service.generate_digest(db_session, llm, on_date=DAY, period_days=7)
    assert "day_context:" in llm.prompts[0][0]


async def test_day_context_is_none_when_the_period_has_no_days(db_session):
    ctx = await digest_service.assemble_context(db_session, on_date=DAY, period_days=7)
    assert ctx["day_context"] is None
