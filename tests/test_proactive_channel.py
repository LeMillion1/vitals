"""T2 — the delivery channel: the webhook door, the budget, and the echo loop.

What's guarded here is everything that is silent when it breaks. A webhook that
accepts a forged call, a retry that logs the same evening twice, a budget that
counts replies and quietly gags the bot mid-conversation — none of those show up
as an error anywhere, so each gets a test.
"""
from datetime import date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select

from vitals.models.proactive import Notification
from vitals.models.raw_payload import RawPayload
from vitals.models.signals import Signal
from vitals.services import signals_service
from vitals.services.proactive import delivery, inbound

# The bot only speaks when the ``signals`` module is on — the same switch the
# owner flips in Settings, and it defaults off.
pytestmark = pytest.mark.usefixtures("signals_module_on")

CHAT_ID = "424242"
WEBHOOK_PATH = "s3cr3t-path"
WEBHOOK_SECRET = "s3cr3t-header"
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}

NOON = datetime(2026, 7, 26, 12, 0)
NIGHT = datetime(2026, 7, 26, 3, 0)


class FakeNotifier:
    """A channel that records instead of sending (шов 1 in one screenful)."""

    channel = "telegram"

    def __init__(self, *, fail: bool = False):
        self.sent: list[dict] = []
        self.acks: list[tuple[str, str]] = []
        self._fail = fail
        self._next_id = 700

    async def send(self, text, *, buttons=None, reply_to=None) -> str:
        if self._fail:
            raise RuntimeError("telegram is having a bad minute")
        self._next_id += 1
        self.sent.append(
            {"text": text, "buttons": buttons, "reply_to": reply_to, "id": str(self._next_id)}
        )
        return str(self._next_id)

    async def answer_callback(self, callback_id, text="") -> None:
        self.acks.append((callback_id, text))


# ── Fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture
def bot_env(monkeypatch):
    monkeypatch.setenv("VITALS_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("VITALS_TELEGRAM_CHAT_ID", CHAT_ID)
    monkeypatch.setenv("VITALS_TELEGRAM_WEBHOOK_PATH", WEBHOOK_PATH)
    monkeypatch.setenv("VITALS_TELEGRAM_WEBHOOK_SECRET", WEBHOOK_SECRET)


@pytest_asyncio.fixture
async def bot_client(client, bot_env):
    """The app with a fake channel wired in. Yields ``(client, notifier)``."""
    from web.main import app
    from web.routers.telegram import get_notifier

    fake = FakeNotifier()
    app.dependency_overrides[get_notifier] = lambda: fake
    yield client, fake


@pytest.fixture
def parses_to(monkeypatch):
    """Pin what the "LLM" returns for any message; no network, no key."""
    items: list[dict] = []

    def _set(new_items):
        items[:] = new_items

    monkeypatch.setattr(
        inbound, "make_signal_parser", lambda known=None: (lambda _text: list(items))
    )
    return _set


def _text_update(update_id, text, *, chat=CHAT_ID, message_id=5, reply_to=None):
    message = {"message_id": message_id, "chat": {"id": int(chat)}, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": message}


def _tap_update(update_id, data, *, chat=CHAT_ID, callback_id="cb-1"):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": callback_id,
            "data": data,
            "message": {"message_id": 9, "chat": {"id": int(chat)}},
        },
    }


async def _signals(session) -> list[Signal]:
    return list((await session.execute(select(Signal))).scalars().all())


# ── The door ──────────────────────────────────────────────────────────────────
async def test_wrong_secret_header_is_rejected(bot_client):
    c, fake = bot_client
    r = await c.post(f"/tg/{WEBHOOK_PATH}", json=_text_update(1, "привет"),
                     headers={"X-Telegram-Bot-Api-Secret-Token": "nope"})
    assert r.status_code == 401
    assert fake.sent == []


async def test_missing_secret_header_is_rejected(bot_client):
    c, _ = bot_client
    r = await c.post(f"/tg/{WEBHOOK_PATH}", json=_text_update(1, "привет"))
    assert r.status_code == 401


async def test_wrong_path_is_rejected(bot_client):
    c, _ = bot_client
    r = await c.post("/tg/guessed-it", json=_text_update(1, "привет"), headers=HEADERS)
    assert r.status_code == 401


async def test_non_ascii_path_is_rejected_not_crashed(bot_client):
    """compare_digest refuses non-ASCII str — a prober must get 401, not a 500."""
    c, _ = bot_client
    r = await c.post("/tg/привет", json=_text_update(1, "hi"), headers=HEADERS)
    assert r.status_code == 401


async def test_unconfigured_webhook_fails_closed(client, monkeypatch):
    monkeypatch.setenv("VITALS_TELEGRAM_WEBHOOK_PATH", "")
    monkeypatch.setenv("VITALS_TELEGRAM_WEBHOOK_SECRET", "")
    r = await client.post("/tg/anything", json=_text_update(1, "hi"), headers=HEADERS)
    assert r.status_code == 401


async def test_foreign_chat_is_swallowed_silently(bot_client, parses_to, db_session):
    """200 and the bin: a 403 would tell a prober they found a live endpoint."""
    c, fake = bot_client
    parses_to([{"kind": "state", "key": "sleepiness", "value_num": 4}])

    r = await c.post(f"/tg/{WEBHOOK_PATH}", json=_text_update(1, "спать хочу", chat="999"),
                     headers=HEADERS)

    assert r.status_code == 200
    assert await _signals(db_session) == []
    assert fake.sent == []


async def test_cross_origin_header_does_not_block_the_webhook(bot_client, parses_to):
    """C5: the CSRF origin check must not fire on a path that has its own secret."""
    c, fake = bot_client
    parses_to([{"kind": "state", "key": "sleepiness", "value_num": 4}])

    r = await c.post(f"/tg/{WEBHOOK_PATH}", json=_text_update(1, "спать хочу"),
                     headers={**HEADERS, "Origin": "https://api.telegram.org"})

    assert r.status_code == 200
    assert len(fake.sent) == 1


# ── Capture + echo ────────────────────────────────────────────────────────────
async def test_text_becomes_signals_plus_an_echo_with_an_undo_button(
    bot_client, parses_to, db_session
):
    c, fake = bot_client
    parses_to([
        {"kind": "symptom", "key": "headache", "value_num": 4, "note": "голова раскалывается"},
        {"kind": "exposure", "key": "caffeine_late", "at_time": "22:00", "note": "кофе в 22"},
    ])

    r = await c.post(f"/tg/{WEBHOOK_PATH}",
                     json=_text_update(1, "Голова раскалывается, кофе в 22"), headers=HEADERS)
    assert r.status_code == 200

    rows = await _signals(db_session)
    assert {row.key for row in rows} == {"headache", "caffeine_late"}
    assert len({row.batch_id for row in rows}) == 1

    # The raw message is in the lake under the update's own id.
    raw = (await db_session.execute(
        select(RawPayload).where(RawPayload.external_id == "tg:1")
    )).scalars().first()
    assert raw is not None and raw.payload["text"].startswith("Голова")

    assert len(fake.sent) == 1
    echo = fake.sent[0]
    # His own words next to the key they became — that is what makes a bad parse
    # visible at a glance.
    assert "голова раскалывается → headache 4/5" in echo["text"]
    assert "кофе в 22 → caffeine_late в 22:00" in echo["text"]
    label, payload = echo["buttons"][0]
    assert label == "не то"
    assert payload == f"{inbound.CB_MISPARSE}{rows[0].batch_id}"


async def test_unparseable_message_is_still_kept_and_answered(
    bot_client, parses_to, db_session
):
    c, fake = bot_client
    parses_to([])

    await c.post(f"/tg/{WEBHOOK_PATH}", json=_text_update(1, "ну такое"), headers=HEADERS)

    assert await _signals(db_session) == []
    raw = (await db_session.execute(
        select(RawPayload).where(RawPayload.external_id == "tg:1")
    )).scalars().first()
    assert raw is not None
    assert "Сохранил как есть" in fake.sent[0]["text"]
    assert fake.sent[0]["buttons"] is None


async def test_repeated_update_id_is_not_processed_twice(bot_client, parses_to, db_session):
    """Telegram retries until it gets a 200 — a retry must be a no-op."""
    c, fake = bot_client
    parses_to([{"kind": "state", "key": "sleepiness", "value_num": 5}])
    update = _text_update(77, "спать пиздец хочу")

    await c.post(f"/tg/{WEBHOOK_PATH}", json=update, headers=HEADERS)
    r = await c.post(f"/tg/{WEBHOOK_PATH}", json=update, headers=HEADERS)

    assert r.status_code == 200
    assert len(await _signals(db_session)) == 1
    assert len(fake.sent) == 1


# ── Taps ──────────────────────────────────────────────────────────────────────
async def test_undo_tap_flags_the_whole_batch_but_keeps_it(bot_client, parses_to, db_session):
    c, fake = bot_client
    parses_to([
        {"kind": "state", "key": "sleepiness", "value_num": 5},
        {"kind": "symptom", "key": "headache", "value_num": 2},
    ])
    await c.post(f"/tg/{WEBHOOK_PATH}", json=_text_update(1, "всё плохо"), headers=HEADERS)
    batch_id = (await _signals(db_session))[0].batch_id

    r = await c.post(f"/tg/{WEBHOOK_PATH}",
                     json=_tap_update(2, f"{inbound.CB_MISPARSE}{batch_id}"), headers=HEADERS)

    assert r.status_code == 200
    rows = await _signals(db_session)
    assert len(rows) == 2 and all(row.misparse for row in rows)
    # Gone from the charts, still on the table.
    assert await signals_service.list_signals(db_session) == []
    assert fake.acks == [("cb-1", "Убрал из графиков")]


async def test_context_tap_answers_the_day_it_was_asked_about(bot_client, db_session):
    """The date rides in the payload: the evening block asks about tomorrow, and a
    tap that lands after midnight must still answer that day."""
    c, fake = bot_client
    tomorrow = date(2026, 7, 27)

    await c.post(f"/tg/{WEBHOOK_PATH}",
                 json=_tap_update(1, f"{inbound.CB_CONTEXT}{tomorrow.isoformat()}:remote:1"),
                 headers=HEADERS)
    await c.post(f"/tg/{WEBHOOK_PATH}",
                 json=_tap_update(2, f"{inbound.CB_CONTEXT}{tomorrow.isoformat()}:gym:0",
                                  callback_id="cb-2"),
                 headers=HEADERS)

    ctx = await signals_service.get_day_context(db_session, tomorrow)
    assert ctx is not None
    # Second tap merges into the first answer rather than replacing it.
    assert ctx.answers == {"remote": True, "gym": False}
    assert fake.acks[-1] == ("cb-2", "Записал")


# ── Replies ───────────────────────────────────────────────────────────────────
async def test_reply_to_our_message_is_answered_not_captured(
    bot_client, parses_to, db_session, monkeypatch
):
    c, fake = bot_client
    parses_to([{"kind": "state", "key": "sleepiness", "value_num": 5}])
    sent = await delivery.send(db_session, fake, text="Утро: сон 6:10, HRV 42.",
                               category=delivery.CATEGORY_BRIEF, now=NOON)
    asked = {}

    async def _answer(question, context):
        asked["question"], asked["context"] = question, context
        return "HRV чуть ниже твоей нормы."

    monkeypatch.setattr(inbound, "answer_reply", _answer)

    await c.post(f"/tg/{WEBHOOK_PATH}",
                 json=_text_update(1, "а HRV это плохо?", reply_to=int(sent.external_id)),
                 headers=HEADERS)

    # A question is a question, not a symptom — nothing lands in signals.
    assert await _signals(db_session) == []
    assert asked["question"] == "а HRV это плохо?"
    assert "HRV 42" in asked["context"]
    assert fake.sent[-1]["text"] == "HRV чуть ниже твоей нормы."


async def test_reply_falls_back_to_a_line_when_the_model_is_down(
    bot_client, db_session, monkeypatch
):
    c, fake = bot_client
    sent = await delivery.send(db_session, fake, text="Утро: сон 6:10.",
                               category=delivery.CATEGORY_BRIEF, now=NOON)

    async def _boom(question, context):
        raise RuntimeError("no key")

    monkeypatch.setattr(inbound, "answer_reply", _boom)

    await c.post(f"/tg/{WEBHOOK_PATH}",
                 json=_text_update(1, "почему?", reply_to=int(sent.external_id)),
                 headers=HEADERS)

    assert "Сейчас не отвечу" in fake.sent[-1]["text"]


# ── Budget & quiet hours ──────────────────────────────────────────────────────
async def test_budget_cuts_the_fifth_self_initiated_message(db_session):
    fake = FakeNotifier()
    for i in range(delivery.DAILY_BUDGET):
        assert await delivery.send(db_session, fake, text=f"нудж {i}",
                                   category=delivery.CATEGORY_NUDGE, now=NOON) is not None

    assert await delivery.send(db_session, fake, text="пятый",
                               category=delivery.CATEGORY_NUDGE, now=NOON) is None
    assert len(fake.sent) == delivery.DAILY_BUDGET


async def test_the_budget_never_gags_a_reply(db_session):
    """The rule that is easiest to get wrong: after four nudges the bot must still
    answer you, or a spent budget reads as a broken bot."""
    fake = FakeNotifier()
    for i in range(delivery.DAILY_BUDGET):
        await delivery.send(db_session, fake, text=f"нудж {i}",
                            category=delivery.CATEGORY_NUDGE, now=NOON)

    assert await delivery.send(db_session, fake, text="ответ",
                               category=delivery.CATEGORY_REPLY, now=NOON) is not None
    assert await delivery.send(db_session, fake, text="эхо",
                               category=delivery.CATEGORY_ECHO, now=NOON) is not None
    # …and the exempt ones didn't quietly eat tomorrow's budget either.
    assert await delivery.sent_today(db_session, on_date=NOON.date()) == delivery.DAILY_BUDGET


async def test_budget_is_per_calendar_day(db_session):
    fake = FakeNotifier()
    for i in range(delivery.DAILY_BUDGET):
        await delivery.send(db_session, fake, text=f"нудж {i}",
                            category=delivery.CATEGORY_NUDGE, now=NOON)

    tomorrow_noon = NOON.replace(day=NOON.day + 1)
    assert await delivery.send(db_session, fake, text="завтрашний",
                               category=delivery.CATEGORY_NUDGE, now=tomorrow_noon) is not None


async def test_quiet_hours_hold_initiative_but_not_answers(db_session):
    fake = FakeNotifier()
    assert await delivery.send(db_session, fake, text="нудж в три ночи",
                               category=delivery.CATEGORY_NUDGE, now=NIGHT) is None
    assert await delivery.send(db_session, fake, text="эхо в три ночи",
                               category=delivery.CATEGORY_ECHO, now=NIGHT) is not None
    assert len(fake.sent) == 1


def test_quiet_window_can_wrap_past_midnight():
    """прогон 6 lets the owner set the window; 23:00–07:00 must not mean "never"."""
    from datetime import time

    assert delivery.in_quiet_hours(time(23, 30), start=time(23, 0), end=time(7, 0))
    assert delivery.in_quiet_hours(time(2, 0), start=time(23, 0), end=time(7, 0))
    assert not delivery.in_quiet_hours(time(12, 0), start=time(23, 0), end=time(7, 0))


async def test_dedupe_key_makes_a_second_send_a_no_op(db_session):
    fake = FakeNotifier()
    first = await delivery.send(db_session, fake, text="бриф", category=delivery.CATEGORY_BRIEF,
                                dedupe_key="brief:2026-07-26", now=NOON)
    second = await delivery.send(db_session, fake, text="бриф", category=delivery.CATEGORY_BRIEF,
                                 dedupe_key="brief:2026-07-26", now=NOON)

    assert first is not None and second is None
    assert len(fake.sent) == 1


async def test_a_failed_send_is_not_journalled_and_costs_no_budget(db_session):
    """Telegram having a bad minute must not roll back the caller's DB work, and
    must not silently spend a slot on a message nobody received."""
    broken = FakeNotifier(fail=True)

    assert await delivery.send(db_session, broken, text="нудж",
                               category=delivery.CATEGORY_NUDGE, now=NOON) is None
    assert (await db_session.execute(select(Notification))).scalars().all() == []
    assert await delivery.sent_today(db_session, on_date=NOON.date()) == 0


async def test_no_channel_configured_is_silence_not_an_error(db_session):
    assert await delivery.send(db_session, None, text="нудж",
                               category=delivery.CATEGORY_NUDGE, now=NOON) is None


# ── The Telegram wire format ──────────────────────────────────────────────────
@pytest.fixture
def captured_payload(monkeypatch):
    """Intercept the Bot API call — the payload shape is invisible until prod."""
    from vitals.services.proactive import channels

    seen: dict = {}

    async def _call(self, method, payload):
        seen["method"], seen["payload"] = method, payload
        return {"message_id": 4242}

    monkeypatch.setattr(channels.TelegramNotifier, "_call", _call)
    return seen


async def test_buttons_and_reply_are_sent_in_telegram_shape(captured_payload):
    from vitals.services.proactive import channels

    notifier = channels.TelegramNotifier("token", CHAT_ID)
    message_id = await notifier.send("текст", buttons=[("не то", "mis:abc")], reply_to="55")

    assert message_id == "4242"
    payload = captured_payload["payload"]
    assert payload["chat_id"] == CHAT_ID
    assert payload["reply_markup"] == {
        "inline_keyboard": [[{"text": "не то", "callback_data": "mis:abc"}]]
    }
    # A reply whose target was deleted must still arrive.
    assert payload["reply_to_message_id"] == 55
    assert payload["allow_sending_without_reply"] is True


async def test_an_overlong_message_is_truncated_not_dropped(captured_payload):
    """Telegram rejects >4096 chars outright: a long brief would just never land."""
    from vitals.services.proactive import channels

    await channels.TelegramNotifier("token", CHAT_ID).send("я" * 5000)

    text = captured_payload["payload"]["text"]
    assert len(text) == 4096 and text.endswith("…")


def test_build_notifier_needs_both_token_and_chat(monkeypatch):
    from vitals.config import load_config
    from vitals.services.proactive import channels

    monkeypatch.setenv("VITALS_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("VITALS_TELEGRAM_CHAT_ID", "")
    assert channels.build_notifier(load_config()) is None

    monkeypatch.setenv("VITALS_TELEGRAM_CHAT_ID", CHAT_ID)
    notifier = channels.build_notifier(load_config())
    assert isinstance(notifier, channels.Notifier)
    assert notifier.channel == "telegram"
