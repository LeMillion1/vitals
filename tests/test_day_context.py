"""T4 — the evening block, the week template, and whose answer wins.

Three things here fail *silently* if they regress, which is why each gets a test:

  * an evening block that asks about the wrong date (a job at 00:00 instead of
    23:45, or a button that says "today") collects context for a day that already
    happened — and nothing in the data ever looks wrong;
  * a tap that lands on a day where the template's guess was never parked erases
    the only material the template could learn from;
  * a brief that prefers the template to his actual answer gives advice for a day
    he explicitly said he isn't having.
"""
from datetime import date, timedelta

from sqlalchemy import select

from vitals.enums import Source
from vitals.models.app_settings import AppSetting
from vitals.models.proactive import Notification
from vitals.models.signals import Signal
from vitals.services import garmin_service, signals_service
from vitals.services.proactive import brief, day_plan, delivery, inbound

DAY = date(2026, 7, 26)          # Sunday
TOMORROW = DAY + timedelta(days=1)

GARMIN_RAW = {
    "summary": {
        "totalSteps": 8412,
        "activeKilocalories": 520,
        "moderateIntensityMinutes": 30,
        "vigorousIntensityMinutes": 15,
        "restingHeartRate": 52,
        "bodyBatteryHighestValue": 84,
    },
    "sleep": {"dailySleepDTO": {"sleepScores": {"overall": {"value": 80}}}},
    "hrv": {"hrvSummary": {"lastNightAvg": 61}},
}


class FakeNotifier:
    channel = "telegram"

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, text, *, buttons=None, reply_to=None) -> str:
        self.sent.append({"text": text, "buttons": buttons})
        return str(800 + len(self.sent))

    async def answer_callback(self, callback_id, text="") -> None:
        pass


class FakeLLM:
    digest_model = "fake/digest"
    brief_model = "fake/brief"

    def __init__(self):
        self.calls: list[dict] = []

    async def complete_text(self, prompt, *, model=None, system=None, max_tokens=None, **kw):
        self.calls.append({"prompt": prompt})
        return "Норм, можно грузиться."


def _patch_evening(monkeypatch, notifier):
    from vitals.services.proactive import channels

    monkeypatch.setattr(channels, "build_notifier", lambda *a, **kw: notifier)
    monkeypatch.setattr(day_plan, "today_local", lambda: DAY)


# ── The 23:45 job (E2) ────────────────────────────────────────────────────────
async def test_evening_block_asks_about_calendar_tomorrow(
    db_session, session_factory, monkeypatch
):
    """Every button carries tomorrow's date, so a tap after midnight still lands
    on the day that was asked about."""
    notifier = FakeNotifier()
    _patch_evening(monkeypatch, notifier)
    await garmin_service.ingest_daily(db_session, DAY, GARMIN_RAW)
    await db_session.commit()

    await day_plan.evening_job(session_factory)

    message = notifier.sent[0]
    assert "8412 шагов" in message["text"]
    assert "520 ккал" in message["text"]
    assert "45 мин интенсивности" in message["text"]
    assert "Завтра:" in message["text"]
    assert message["buttons"], "an evening block with no buttons asks nothing"
    for _, payload in message["buttons"]:
        assert payload.startswith(f"{inbound.CB_CONTEXT}{TOMORROW.isoformat()}:")

    journal = (await db_session.execute(select(Notification))).scalars().one()
    assert journal.category == delivery.CATEGORY_EVENING
    assert journal.dedupe_key == day_plan.dedupe_key(DAY)


async def test_evening_block_parks_the_guess_even_if_nothing_is_tapped(
    db_session, session_factory, monkeypatch
):
    """``planned`` is only collectable at the moment the guess is made — a template
    that learns from the gap needs it written down whether or not he answers."""
    _patch_evening(monkeypatch, FakeNotifier())

    await day_plan.evening_job(session_factory)

    row = await signals_service.get_day_context(db_session, TOMORROW)
    assert row.planned == day_plan.DEFAULT_DAY
    assert not row.answers          # a parked guess is not an answer
    assert row.source == Source.TEMPLATE.value


async def test_evening_block_runs_twice_and_sends_once(
    db_session, session_factory, monkeypatch
):
    notifier = FakeNotifier()
    _patch_evening(monkeypatch, notifier)

    await day_plan.evening_job(session_factory)
    await day_plan.evening_job(session_factory)

    assert len(notifier.sent) == 1


async def test_evening_block_drops_the_summary_when_garmin_has_nothing(
    db_session, session_factory, monkeypatch
):
    """No Garmin row is a missing line, not a missing message: the question about
    tomorrow is the point of the evening block."""
    notifier = FakeNotifier()
    _patch_evening(monkeypatch, notifier)

    await day_plan.evening_job(session_factory)

    text = notifier.sent[0]["text"]
    assert "Итог дня" not in text
    assert "Как день?" in text


# ── Taps (E3) ─────────────────────────────────────────────────────────────────
async def test_tap_overwrites_the_answer_and_keeps_the_guess(db_session):
    await day_plan.record_plan(db_session, TOMORROW, {"remote": False, "gym": False, "load": "normal"})

    await day_plan.record_answer(db_session, TOMORROW, "gym", True)
    await day_plan.record_answer(db_session, TOMORROW, "load", "heavy")
    await db_session.commit()

    row = await signals_service.get_day_context(db_session, TOMORROW)
    assert row.answers == {"gym": True, "load": "heavy"}
    assert row.planned == {"remote": False, "gym": False, "load": "normal"}
    assert row.source == Source.MANUAL.value


async def test_a_tap_on_an_unasked_day_still_records_what_the_template_thought(
    db_session,
):
    """The brief's exception buttons can be the first thing ever tapped for a day."""
    await day_plan.record_answer(db_session, DAY, "remote", True)
    await db_session.commit()

    row = await signals_service.get_day_context(db_session, DAY)
    assert row.answers == {"remote": True}
    assert row.planned == day_plan.DEFAULT_DAY


async def test_exception_buttons_offer_every_other_answer(db_session):
    buttons = day_plan.exception_buttons({"remote": False, "gym": False, "load": "normal"}, DAY)
    payloads = dict((payload, label) for label, payload in buttons)

    assert f"ctx:{DAY.isoformat()}:remote:1" in payloads
    assert f"ctx:{DAY.isoformat()}:gym:1" in payloads
    assert f"ctx:{DAY.isoformat()}:load:heavy" in payloads
    # Never a button for what he is already down as doing.
    assert f"ctx:{DAY.isoformat()}:load:normal" not in payloads
    assert f"ctx:{DAY.isoformat()}:remote:0" not in payloads


# ── The template (E1) ─────────────────────────────────────────────────────────
async def test_template_is_stored_per_weekday_and_sanitized(db_session):
    await day_plan.set_week_template(
        db_session,
        {"sun": {"remote": True, "gym": True, "load": "heavy", "нечто": 1}, "junk": {}},
    )
    await db_session.commit()

    template = await day_plan.get_week_template(db_session)
    assert set(template) == set(day_plan.WEEKDAYS)
    assert template["sun"] == {"remote": True, "gym": True, "load": "heavy"}
    assert template["mon"] == day_plan.DEFAULT_DAY
    assert day_plan.guess_for(template, DAY) == template["sun"]  # DAY is a Sunday


async def test_a_broken_template_row_does_not_take_the_evening_block_down(db_session):
    """Hand-editable JSON is a trust boundary: garbage degrades to the default."""
    db_session.add(AppSetting(key=day_plan.SETTINGS_KEY, value="что-то не то"))
    await db_session.commit()

    assert await day_plan.get_week_template(db_session) == day_plan.DEFAULT_TEMPLATE


async def test_an_out_of_registry_value_falls_back_instead_of_reaching_the_message(
    db_session,
):
    await day_plan.set_week_template(db_session, {"sun": {"load": "чудовищный"}})
    await db_session.commit()

    template = await day_plan.get_week_template(db_session)
    assert template["sun"]["load"] == "normal"


# ── The brief (E4) ────────────────────────────────────────────────────────────
async def _seed_brief_day(db_session):
    await garmin_service.ingest_daily(db_session, DAY, GARMIN_RAW)
    await db_session.commit()


async def test_brief_prefers_his_answer_to_the_template(db_session, monkeypatch):
    """And only for the question he actually answered: one tap must not silently
    cancel the rest of the guess he was correcting."""
    await _seed_brief_day(db_session)
    await day_plan.set_week_template(db_session, {"sun": {"remote": True, "gym": False}})
    await day_plan.record_answer(db_session, DAY, "gym", True)
    await db_session.commit()

    llm = FakeLLM()
    row = await brief.generate_brief(db_session, llm, on_date=DAY)
    await db_session.commit()

    assert "Сегодня: удалёнка · зал · обычный день" in row.content
    assert "по шаблону" not in row.content
    # …and the model is told this is his answer, not a guess.
    assert '"source": "manual"' in llm.calls[0]["prompt"]
    assert day_plan.buttons_from_context(row.context_json["day"], DAY) is None


async def test_brief_falls_back_to_the_template_and_offers_the_exceptions(
    db_session, session_factory, monkeypatch
):
    notifier = FakeNotifier()
    from vitals.integrations import llm_client
    from vitals.services.proactive import channels

    async def _no_sync(*a, **kw):
        return None

    monkeypatch.setattr(garmin_service, "sync_job", _no_sync)
    monkeypatch.setattr(channels, "build_notifier", lambda *a, **kw: notifier)
    monkeypatch.setattr(llm_client, "LLMClient", lambda *a, **kw: FakeLLM())
    monkeypatch.setattr(brief, "today_local", lambda: DAY)
    monkeypatch.setattr(day_plan, "today_local", lambda: DAY)

    await day_plan.set_week_template(db_session, {"sun": {"remote": True, "load": "heavy"}})
    await _seed_brief_day(db_session)

    await brief.brief_job(session_factory)

    message = notifier.sent[0]
    assert "Сегодня по шаблону: удалёнка · без зала · тяжёлый день" in message["text"]
    assert message["buttons"], "an unanswered day has to be correctable in one tap"
    for _, payload in message["buttons"]:
        assert payload.startswith(f"{inbound.CB_CONTEXT}{DAY.isoformat()}:")


# ── The reply the evening block invites ───────────────────────────────────────
async def test_a_reply_to_the_evening_block_is_captured_not_answered(
    db_session, monkeypatch
):
    """«Как день?» is a question *to him* — his reply is data, and routing it to the
    Q&A path would answer it instead of recording it."""
    notifier = FakeNotifier()
    sent = await delivery.send(
        db_session, notifier, text="Как день?", category=delivery.CATEGORY_EVENING
    )

    async def _never(*a, **kw):
        raise AssertionError("the evening answer must not go to the Q&A path")

    monkeypatch.setattr(inbound, "answer_reply", _never)

    async def _parse(text):
        return [{"kind": "state", "key": "fatigue", "value_num": 4, "note": text}]

    await inbound.handle_text(
        db_session,
        "устал как собака",
        notifier=notifier,
        message_id=51,
        reply_to_message_id=sent.external_id,
        parse=_parse,
        on_date=DAY,
    )
    await db_session.commit()

    rows = (await db_session.execute(select(Signal))).scalars().all()
    assert [r.key for r in rows] == ["fatigue"]
