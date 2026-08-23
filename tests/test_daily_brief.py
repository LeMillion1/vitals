"""The morning brief: the numbers, the fallback, and the silence.

Each of these guards something that fails quietly. A header that drifts from the
database is worse than no brief at all; a dead model that takes the whole message
down with it turns the one proactive feature into nothing; and a brief that keeps
arriving with "нет данных", or that carries the hormone protocol into Telegram,
is a product decision reversed by accident.
"""
import uuid
from datetime import date, datetime, timedelta

import pytest

from vitals.ownership import WriteIdentity
from sqlalchemy import select

from vitals.enums import (
    DigestKind,
    Domain,
    IntegrationConnectionStatus,
    IntegrationProvider,
    Severity,
    Source,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.milestones import WeeklyDigest
from vitals.models.proactive import Notification
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.services import (
    alerts_service,
    digest_service,
    garmin_service,
    weight_service,
)
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from vitals.services.proactive import brief, channels, compose, day_plan, delivery
from vitals.services.proactive.ownership import ProactiveOwnershipContext

# The bot only speaks when the ``signals`` module is on — the same switch the
# owner flips in Settings, and it defaults off.
# Every row these tests create belongs to the one person the brief is about.
pytestmark = pytest.mark.usefixtures("all_modules_on", "owned_by_legacy_subject")

DAY = date(2026, 7, 26)

# One day of Garmin as it arrives from the API, with exactly the five numbers the
# header is specified to print.
GARMIN_RAW = {
    "summary": {"restingHeartRate": 52, "bodyBatteryHighestValue": 84},
    "sleep": {"dailySleepDTO": {"sleepScores": {"overall": {"value": 80}}}},
    "hrv": {"hrvSummary": {"lastNightAvg": 61}},
}


class FakeLLM:
    """Records what it was asked; answers with one paragraph."""

    digest_model = "fake/digest"
    brief_model = "fake/brief"

    def __init__(self, answer="Восстановление в норме, можно грузиться."):
        self.calls: list[dict] = []
        self._answer = answer

    async def complete_text(self, prompt, *, model=None, system=None, max_tokens=None, **kw):
        self.calls.append({"prompt": prompt, "model": model, "system": system})
        return self._answer


class BoomLLM:
    """The upstream is down / out of balance / has no key."""

    digest_model = "fake/digest"
    brief_model = "fake/brief"

    async def complete_text(self, *a, **kw):
        raise RuntimeError("openrouter is having a bad day")


class FakeNotifier:
    channel = "telegram"

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, text, *, buttons=None, reply_to=None) -> str:
        self.sent.append({"text": text, "buttons": buttons})
        return str(900 + len(self.sent))

    async def answer_callback(self, callback_id, text="") -> None:
        pass

    async def edit(self, message_id, text, *, buttons=None) -> None:
        pass


async def _telegram_ownership(session) -> ProactiveOwnershipContext:
    legacy = await resolve_legacy_ownership_context(
        session,
        actor_username=None,
        required_connections=(IntegrationProvider.TELEGRAM,),
    )
    return channels.ownership_from_legacy(legacy)


def _patch_bound_delivery(monkeypatch, notifier) -> None:
    """Bind one fake transport to the exact S/Q/C passed by production code."""

    async def build_bound(session, ownership, **kwargs):
        del session, kwargs
        notifier.binding = channels.DeliveryEndpointBinding(
            subject_id=ownership.subject_id,
            recipient_user_id=ownership.recipient_user_id,
            integration_connection_id=ownership.connection_id,
            channel=notifier.channel,
        )
        return notifier

    def resolve_bound(binding, credential_ref, **kwargs):
        del credential_ref, kwargs
        notifier.binding = binding
        return notifier

    monkeypatch.setattr(channels, "build_legacy_bound_notifier", build_bound)
    monkeypatch.setattr(channels, "resolve_legacy_bound_notifier", resolve_bound)


async def _durably_send(
    session,
    notifier,
    *,
    ownership,
    text,
    category,
    idempotency_key,
    legacy_dedupe_key=None,
):
    bound = await channels.build_legacy_bound_notifier(session, ownership)
    prepared = await delivery.prepare_delivery_intent(
        session,
        bound,
        text=text,
        category=category,
        idempotency_key=idempotency_key,
        legacy_dedupe_key=legacy_dedupe_key,
        ownership=ownership,
    )
    await session.commit()
    assert prepared is not None
    lease = await delivery.start_delivery_dispatch(
        session,
        prepared,
        notifier_resolver=channels.resolve_legacy_bound_notifier,
    )
    await session.commit()
    assert lease is not None
    completion = await delivery.dispatch_delivery(lease)
    journal = await delivery.finalize_delivery(session, completion)
    await session.commit()
    assert journal is not None
    return journal


async def _seed_day(db_session, owner_write, *, on_date=DAY, weight_kg=88.0, garmin_owned_scope):
    await garmin_service.ingest_owned_daily(db_session, on_date, GARMIN_RAW, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    if weight_kg is not None:
        await weight_service.log_weight(
            db_session,
            on_date=on_date,
            weight_kg=weight_kg,
            identity=owner_write.identity,
            prepared_weight_write=await owner_write.weight_write(on_date),
        )
    await db_session.commit()


@pytest.mark.parametrize(
    "inactive_status",
    (
        IntegrationConnectionStatus.PENDING.value,
        IntegrationConnectionStatus.DISABLED.value,
    ),
)
async def test_brief_rejects_inactive_llm_connection_before_network(
    db_session,
    legacy_owner_roots,
    inactive_status,
):
    ownership = await resolve_legacy_ownership_context(
        db_session,
        actor_username=None,
        required_connections=(IntegrationProvider.OPENROUTER,),
    )
    connection_id = ownership.connection_id(IntegrationProvider.OPENROUTER)
    connection = await db_session.get(IntegrationConnection, connection_id)
    assert connection is not None
    connection.status = inactive_status
    await db_session.flush()
    llm = FakeLLM()

    with pytest.raises(brief.BriefOwnershipError, match="phased gateway"):
        await brief.generate_brief(
            db_session,
            llm,
            on_date=DAY,
            identity=ownership.owner_action(),
            llm_connection_id=connection_id,
        )

    assert llm.calls == []


# ── The header ────────────────────────────────────────────────────────────────
async def test_header_numbers_match_the_database(garmin_owned_scope, db_session, legacy_owner_roots, owner_write):
    """Every number in the header is printed by code from the stored row — the
    model is never in a position to get one wrong."""
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)

    ctx = await brief.build_context(
        db_session,
        on_date=DAY,
        subject_id=legacy_owner_roots.subject_id,
    )
    header = compose.render(compose.header_blocks(ctx))

    stored = await garmin_service.latest_daily(
        db_session, before_or_on=DAY, subject_id=legacy_owner_roots.subject_id
    )
    assert f"Сон {stored.sleep_score}" in header
    assert f"HRV {int(stored.hrv_avg)}" in header
    assert f"Пульс покоя {stored.resting_hr}" in header
    assert f"Body Battery {stored.body_battery_high}" in header
    assert "Вес 88 кг" in header
    # A normal morning carries no recovery warning at all.
    assert ctx["garmin"]["advice"] is None


async def test_noisy_weight_never_prints_a_bare_trend(db_session, legacy_owner_roots, owner_write, *, garmin_owned_scope):
    """A noise marker means the scale is lying in a known direction. The trend is
    the one header number that can mislead while being technically correct."""
    for i in range(8):
        day = DAY - timedelta(days=7 - i)
        await weight_service.log_weight(
            db_session,
            on_date=day,
            weight_kg=90.0 - i * 0.2,
            identity=owner_write.identity,
            prepared_weight_write=await owner_write.weight_write(day),
        )
    await weight_service.add_noise_marker(
        db_session,
        start_date=DAY - timedelta(days=3),
        reason="загрузка креатином",
        direction="up",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY - timedelta(days=3)),
    )
    await garmin_service.ingest_owned_daily(db_session, DAY, GARMIN_RAW, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    await db_session.commit()

    ctx = await brief.build_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        on_date=DAY)
    text = compose.render(compose.header_blocks(ctx))
    if "тренд" in text:
        assert "зашумлён" in text
        assert "креатином" in text


async def test_the_header_carries_his_own_norm_only_when_today_is_off_it(db_session, legacy_owner_roots, owner_write, *, garmin_owned_scope):
    """A single day of absolutes is the same line every morning, and whether 78 is
    good is a comparison. Printed by code from his own fortnight — and printed
    only when it says something, so the parenthesis stays worth reading."""
    for i in range(1, 11):  # ten days of a steady sleep score of 85, RHR 52
        day = DAY - timedelta(days=i)
        await garmin_service.ingest_owned_daily(db_session, day, {
            "summary": {"restingHeartRate": 52, "bodyBatteryHighestValue": 84},
            "sleep": {"dailySleepDTO": {"sleepScores": {"overall": {"value": 85}}}},
            "hrv": {"hrvSummary": {"lastNightAvg": 61}},
        }, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)  # today: sleep 80, RHR 52 — same as every other day

    ctx = await brief.build_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        on_date=DAY)
    assert ctx["garmin"]["baseline"]["sleep_score"] == 85
    # Today's own row must not be averaged into the yardstick it is judged by.
    assert ctx["garmin"]["baseline"]["resting_hr"] == 52

    text = compose.render(compose.header_blocks(ctx))
    assert "Сон 80 (норма 85)" in text          # ~6% off — worth saying
    assert "Пульс покоя 52 ·" in text or text.rstrip().endswith("Пульс покоя 52")
    assert "Пульс покоя 52 (" not in text       # on the norm — say nothing


async def test_no_norm_until_there_is_enough_history(db_session, legacy_owner_roots, owner_write, *, garmin_owned_scope):
    """Two nights is not a baseline. Left unguarded the model gets a "norm" made
    of noise and calls a просадка against it — the invented comparison this whole
    block exists to stop."""
    await garmin_service.ingest_owned_daily(db_session, DAY - timedelta(days=1), GARMIN_RAW, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)

    ctx = await brief.build_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        on_date=DAY)
    assert ctx["garmin"]["baseline"] is None
    assert "норма" not in compose.render(compose.header_blocks(ctx))


async def test_the_brief_sees_yesterdays_signals(db_session, legacy_owner_roots):
    """"Кофе в 22" is yesterday's row and this morning's HRV is what it explains.
    A one-day window would cut every exposure away from the number it caused."""
    from vitals.services import signals_service

    await signals_service.create_signals(
        db_session,
        items=[{"kind": "exposure", "key": "caffeine_late", "at_time": "22:00"}],
        on_date=DAY - timedelta(days=1),
        identity=WriteIdentity(legacy_owner_roots.subject_id, legacy_owner_roots.user_id),
    )
    await signals_service.create_signals(
        db_session,
        items=[{"kind": "state", "key": "sleepiness", "value_num": 5}],
        on_date=DAY,
        identity=WriteIdentity(legacy_owner_roots.subject_id, legacy_owner_roots.user_id),
    )
    # Two days back is outside the window: the brief is about this morning.
    await signals_service.create_signals(
        db_session,
        items=[{"kind": "symptom", "key": "headache", "value_num": 3}],
        on_date=DAY - timedelta(days=2),
        identity=WriteIdentity(legacy_owner_roots.subject_id, legacy_owner_roots.user_id),
    )
    await db_session.commit()

    ctx = await brief.build_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        on_date=DAY)

    assert [s["key"] for s in ctx["signals"]] == ["caffeine_late", "sleepiness"]
    assert ctx["signals"][0]["at_time"] == "22:00"


# ── The fallback ──────────────────────────────────────────────────────────────
async def test_brief_survives_a_dead_model(garmin_owned_scope, db_session, legacy_owner_roots, owner_write):
    """No narrative is a missing block, not a missing brief."""
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)

    ctx = await brief.build_context(
        db_session,
        on_date=DAY,
        subject_id=legacy_owner_roots.subject_id,
    )
    # The header is printed by code, so it stands with no model at all.
    assert "Сон 80" in compose.render(compose.header_blocks(ctx))




# ── Storage ───────────────────────────────────────────────────────────────────


async def test_protocol_never_reaches_the_brief(garmin_owned_scope, db_session, legacy_owner_roots, owner_write):
    """No doses, no compounds, no injection schedule, no supplements — not in
    the stored context and not in the prompt. The weekly digest still sees it all."""
    from vitals.services import glp1_service, supplements_service

    await glp1_service.add_dose_phase(
        db_session,
        start_date=DAY - timedelta(days=30),
        drug="semaglutide",
        dose_mg=1.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(DAY - timedelta(days=30)),
    )
    await supplements_service.add_supplement(
        db_session, name="Ашваганда", key="ashwagandha", active=True,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write()
    )
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)

    ctx = await brief.build_context(
        db_session,
        on_date=DAY,
        subject_id=legacy_owner_roots.subject_id,
    )

    for key in compose.PROTOCOL_KEYS:
        assert key not in ctx
    prompt = brief.build_prompt(ctx)
    assert "semaglutide" not in prompt
    assert "Ашваганда" not in prompt

    # …and the weekly digest is untouched by any of this.
    full = await digest_service.assemble_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id, on_date=DAY)
    assert full["glp1"]["drug"] == "semaglutide"
    assert full["supplements"][0]["name"] == "Ашваганда"


def test_protocol_is_removed_from_secondary_context_surfaces():
    stripped = compose.strip_protocol(
        {
            "glp1": {"drug": "private"},
            "hrt": {"cycle": "private"},
            "coverage": {"glp1": {}, "hrt": {}, "weight": {}},
            "alerts": [
                {"domain": "hrt", "message": "private"},
                {"domain": "labs", "message": "visible"},
            ],
            "timeline": [
                {"domain": "glp1", "title": "private"},
                {"domain": "timeline", "title": "visible"},
            ],
            "milestones": [
                {"domain": "hrt", "name": "private"},
                {"domain": "weight", "name": "visible"},
            ],
        }
    )

    assert set(stripped["coverage"]) == {"weight"}
    assert stripped["alerts"] == [{"domain": "labs", "message": "visible"}]
    assert stripped["timeline"] == [
        {"domain": "timeline", "title": "visible"}
    ]
    assert stripped["milestones"] == [
        {"domain": "weight", "name": "visible"}
    ]


# ── The night that hasn't ended yet ───────────────────────────────────────────
# Today's row as it looks while he is still asleep: Garmin fills the day-so-far
# numbers, and the sleep DTO — score, duration, sleep end — does not exist until
# the night is closed. Body Battery here is the overnight *low*, not a peak.
GARMIN_MID_NIGHT = {
    "summary": {"restingHeartRate": 68, "bodyBatteryHighestValue": 24},
}


async def test_a_running_night_never_reaches_the_brief(db_session, legacy_owner_roots, owner_write, *, garmin_owned_scope):
    """The prod bug: the 11:00 brief caught him asleep, read Body Battery 24 and
    resting HR 68 off a night still in progress, called recovery wrecked and told
    him to skip the gym — then stored all of it, where the weekly digest reads it
    back as what that morning actually was."""
    await garmin_service.ingest_owned_daily(db_session, DAY, GARMIN_MID_NIGHT, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    await weight_service.log_weight(
        db_session,
        on_date=DAY,
        weight_kg=88.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(DAY),
    )
    await db_session.commit()

    ctx = await brief.build_context(
        db_session,
        on_date=DAY,
        subject_id=legacy_owner_roots.subject_id,
    )
    guarded = compose.drop_unscored_night(ctx)
    header = compose.render(compose.header_blocks(guarded))
    assert "Пульс покоя" not in header
    assert "Body Battery" not in header
    assert compose.LINE_NIGHT_PENDING in header
    assert "Вес 88 кг" in header  # what *is* known still goes out
    # And the context says why, so nothing downstream fills the gap in.
    assert guarded["garmin"]["night_pending"] is True
    assert guarded["garmin"]["resting_hr"] is None
    assert guarded["garmin"]["advice"] is None


async def test_a_scored_night_is_untouched(garmin_owned_scope, db_session, legacy_owner_roots, owner_write):
    """The guard must not fire on a normal morning — that would delete the header."""
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)

    ctx = await brief.build_context(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        on_date=DAY)
    assert compose.night_pending(ctx, on_date=DAY) is False
    assert await brief.night_scored(db_session, DAY) is True


async def test_job_waits_for_the_night_instead_of_briefing_over_it(
    db_session, session_factory, monkeypatch, legacy_owner_roots, owner_write
, *, garmin_owned_scope):
    """Inside the wait window an un-scored night costs nothing: no message, no
    stored brief, no model call, no empty-day alert — the next hourly fire looks
    again. Once the night lands, the brief goes out normally."""
    notifier = FakeNotifier()
    llm = FakeLLM()
    _patch_job(monkeypatch, notifier, llm)
    monkeypatch.setattr(brief, "today_local", lambda: DAY)
    monkeypatch.setattr(brief, "now_local", lambda: datetime(2026, 7, 26, 11, 0))
    await garmin_service.ingest_owned_daily(db_session, DAY, GARMIN_MID_NIGHT, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    await weight_service.log_weight(
        db_session,
        on_date=DAY,
        weight_kg=88.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(DAY),
    )
    await db_session.commit()

    await brief.brief_job(session_factory)

    assert notifier.sent == []
    assert (await db_session.execute(select(WeeklyDigest))).scalars().all() == []
    assert (await db_session.execute(select(SystemAlert))).scalars().all() == []
    assert llm.calls == []

    # He wakes up, the watch closes the night, the next fire an hour later sends.
    await garmin_service.ingest_owned_daily(db_session, DAY, GARMIN_RAW, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    await db_session.commit()

    await brief.brief_job(session_factory)

    assert len(notifier.sent) == 1
    assert "Сон 80" in notifier.sent[0]["text"]


async def test_job_stops_waiting_at_the_end_of_the_window(
    db_session, session_factory, monkeypatch, legacy_owner_roots, owner_write
, *, garmin_owned_scope):
    """Waiting forever is its own failure: the last fire sends what there is,
    minus the numbers the night never produced."""
    notifier = FakeNotifier()
    _patch_job(monkeypatch, notifier, FakeLLM())
    monkeypatch.setattr(brief, "today_local", lambda: DAY)
    monkeypatch.setattr(
        brief, "now_local", lambda: datetime(2026, 7, 26, brief.last_attempt_hour(11), 0)
    )
    await garmin_service.ingest_owned_daily(db_session, DAY, GARMIN_MID_NIGHT, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    await weight_service.log_weight(
        db_session,
        on_date=DAY,
        weight_kg=88.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(DAY),
    )
    await db_session.commit()

    await brief.brief_job(session_factory)

    assert len(notifier.sent) == 1
    assert compose.LINE_NIGHT_PENDING in notifier.sent[0]["text"]
    assert "Body Battery" not in notifier.sent[0]["text"]


# ── The empty day ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw, when",
    [
        (GARMIN_RAW, DAY + timedelta(days=5)),   # data exists, but it's stale
        ({"summary": {"totalSteps": 200}}, DAY),  # today's row carries no recovery
    ],
)
async def test_empty_day_builds_nothing(db_session, raw, when, legacy_owner_roots, *, garmin_owned_scope):
    await garmin_service.ingest_owned_daily(db_session, DAY, raw, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    await db_session.commit()

    ctx = await brief.build_context(
        db_session,
        on_date=when,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert compose.is_empty_day(ctx, on_date=when)


async def test_a_day_without_garmin_is_not_an_empty_day(db_session, legacy_owner_roots, owner_write):
    """The watch on the charger used to silence the brief outright, even with
    the scale, the food log and his own words all filling normally."""
    from vitals.services import signals_service

    await weight_service.log_weight(
        db_session,
        on_date=DAY,
        weight_kg=88.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(DAY),
    )
    await signals_service.create_signals(
        db_session,
        items=[{"kind": "state", "key": "fatigue", "value_num": 4}],
        on_date=DAY,
        identity=WriteIdentity(legacy_owner_roots.subject_id, legacy_owner_roots.user_id),
    )
    await db_session.commit()

    ctx = await brief.build_context(
        db_session,
        on_date=DAY,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert not compose.is_empty_day(ctx, on_date=DAY)
    assert "Вес 88 кг" in compose.render(compose.header_blocks(ctx))


async def test_a_weight_from_months_ago_does_not_keep_the_brief_talking(db_session, legacy_owner_roots, owner_write):
    """The other edge: ``latest_kg`` is the newest weigh-in *ever*, so counting it
    without a date would mean one trip to the scale in March buys a brief every
    morning after — including mornings where nothing at all happened."""
    await weight_service.log_weight(
        db_session,
        on_date=DAY - timedelta(days=90),
        weight_kg=88.0,
        identity=owner_write.identity,
        prepared_weight_write=await owner_write.weight_write(DAY - timedelta(days=90)),
    )
    await db_session.commit()

    ctx = await brief.build_context(
        db_session,
        on_date=DAY,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert compose.is_empty_day(ctx, on_date=DAY)


async def test_job_stays_quiet_on_an_empty_day_and_says_so_in_the_web(
    db_session, session_factory, monkeypatch, legacy_owner_roots
):
    """Silence beats "нет данных" three mornings running — but the gap still
    has to be visible somewhere, so it becomes a passive info alert."""
    notifier = FakeNotifier()
    _patch_job(monkeypatch, notifier, FakeLLM())
    monkeypatch.setattr(brief, "today_local", lambda: DAY)

    await brief.brief_job(session_factory)

    assert notifier.sent == []
    assert (await db_session.execute(select(WeeklyDigest))).scalars().all() == []
    alert = (await db_session.execute(select(SystemAlert))).scalars().one()
    assert alert.alert_key == brief.EMPTY_DAY_ALERT_KEY
    assert alert.severity == Severity.INFO.value
    ownership = await _telegram_ownership(db_session)
    assert alert.subject_id == ownership.subject_id
    assert alert.integration_connection_id is None
    assert alert.resolved_by_user_id is None


async def test_job_sends_once_a_day_and_clears_the_empty_alert(
    garmin_owned_scope,
    db_session, session_factory, monkeypatch, legacy_owner_roots, owner_write,
):
    """A re-run of the 11:00 job is a no-op, not a second ping: Telegram retries
    and APScheduler misfires both replay a job."""
    notifier = FakeNotifier()
    _patch_job(monkeypatch, notifier, FakeLLM())
    monkeypatch.setattr(brief, "today_local", lambda: DAY)

    # An earlier empty morning left its alert behind.
    await alerts_service.raise_alert(
        db_session, domain=Domain.SYSTEM.value, severity=Severity.INFO.value,
        message="stale", alert_key=brief.EMPTY_DAY_ALERT_KEY,
    )
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)

    await brief.brief_job(session_factory)
    await brief.brief_job(session_factory)

    assert len(notifier.sent) == 1
    assert "Сон 80" in notifier.sent[0]["text"]
    journal = (await db_session.execute(select(Notification))).scalars().all()
    assert [n.category for n in journal] == [delivery.CATEGORY_BRIEF]
    assert journal[0].dedupe_key == delivery.make_delivery_idempotency_key(
        "brief",
        DAY,
    )
    assert journal[0].external_id == "901"
    channel_ownership = await _telegram_ownership(db_session)
    stored = (await db_session.execute(select(WeeklyDigest))).scalars().one()
    assert (
        stored.subject_id,
        stored.actor_user_id,
        stored.integration_connection_id,
    ) == (
        channel_ownership.subject_id,
        None,
        None,
    )
    assert (
        journal[0].subject_id,
        journal[0].recipient_user_id,
        journal[0].integration_connection_id,
    ) == (
        channel_ownership.subject_id,
        channel_ownership.recipient_user_id,
        channel_ownership.connection_id,
    )
    alert = (await db_session.execute(select(SystemAlert))).scalars().one()
    assert alert.resolved_at is not None
    assert alert.subject_id == channel_ownership.subject_id
    assert alert.integration_connection_id is None
    assert alert.resolved_by_user_id is None


async def test_brief_network_awaits_have_no_open_database_transaction(
    garmin_owned_scope,
    db_session, session_factory, monkeypatch, legacy_owner_roots, owner_write,
):
    """Neither OpenRouter nor Telegram may inherit ownership/read transactions."""

    class TransactionCheckingLLM(FakeLLM):
        async def complete_text(self, *args, **kwargs):
            assert not db_session.in_transaction()
            return await super().complete_text(*args, **kwargs)

    class TransactionCheckingNotifier(FakeNotifier):
        async def send(self, *args, **kwargs):
            assert not db_session.in_transaction()
            return await super().send(*args, **kwargs)

    notifier = TransactionCheckingNotifier()
    _patch_job(monkeypatch, notifier, TransactionCheckingLLM())

    async def transaction_checking_sync(*args, **kwargs):
        assert not db_session.in_transaction()

    monkeypatch.setattr(garmin_service, "sync_job", transaction_checking_sync)
    monkeypatch.setattr(brief, "today_local", lambda: DAY)
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)

    await brief.brief_job(session_factory)

    assert len(notifier.sent) == 1


async def test_already_sent_replay_clears_stale_alert_without_network(
    db_session, session_factory, monkeypatch, legacy_owner_roots
):
    """A post-journal alert failure is repaired by the next hourly replay."""
    ownership = await _telegram_ownership(db_session)
    sent = FakeNotifier()
    _patch_bound_delivery(monkeypatch, sent)
    notification = await _durably_send(
        db_session,
        sent,
        ownership=ownership,
        text="already delivered",
        category=delivery.CATEGORY_BRIEF,
        idempotency_key=delivery.make_delivery_idempotency_key("brief", DAY),
        legacy_dedupe_key=brief.dedupe_key(DAY),
    )
    assert notification is not None
    stale = await alerts_service.raise_alert(
        db_session,
        domain=Domain.SYSTEM.value,
        severity=Severity.INFO.value,
        message="stale",
        alert_key=brief.EMPTY_DAY_ALERT_KEY,
    )
    await db_session.commit()

    class NoNetworkNotifier(FakeNotifier):
        async def send(self, *args, **kwargs):
            raise AssertionError("already-sent recovery must not call Telegram")

    class NoNetworkLLM(FakeLLM):
        async def complete_text(self, *args, **kwargs):
            raise AssertionError("already-sent recovery must not call OpenRouter")

    async def no_garmin(*args, **kwargs):
        raise AssertionError("already-sent recovery must not call Garmin")

    _patch_job(monkeypatch, NoNetworkNotifier(), NoNetworkLLM())
    monkeypatch.setattr(garmin_service, "sync_job", no_garmin)
    monkeypatch.setattr(brief, "today_local", lambda: DAY)

    await brief.brief_job(session_factory)

    assert stale.resolved_at is not None
    assert stale.subject_id == ownership.subject_id
    assert stale.integration_connection_id is None
    assert stale.resolved_by_user_id is None


async def test_empty_alert_clear_adopts_only_the_matching_fully_unowned_row(
    db_session,
    legacy_owner_roots,
):
    ownership = await _telegram_ownership(db_session)
    legacy_brief = await alerts_service.raise_alert(
        db_session,
        domain=Domain.SYSTEM.value,
        severity=Severity.INFO.value,
        message="legacy brief",
        alert_key=brief.EMPTY_DAY_ALERT_KEY,
    )
    other = await alerts_service.raise_alert(
        db_session,
        domain=Domain.WEIGHT.value,
        severity=Severity.INFO.value,
        message="other health alert",
        alert_key="weight.noisy_period_active",
    )
    await db_session.commit()

    resolved = await brief._reconcile_empty_day_alert(
        db_session,
        identity=ownership.system_action(),
        empty=False,
    )

    assert resolved is legacy_brief
    assert (
        resolved.subject_id,
        resolved.integration_connection_id,
        resolved.resolved_by_user_id,
    ) == (ownership.subject_id, None, None)
    assert resolved.resolved_at is not None
    assert (other.subject_id, other.integration_connection_id) == (None, None)
    assert other.resolved_at is None


async def test_empty_alert_bridge_rejects_partial_ownership(
    db_session, legacy_owner_roots
):
    ownership = await _telegram_ownership(db_session)
    llm_ownership = await resolve_legacy_ownership_context(
        db_session,
        actor_username=None,
        required_connections=(IntegrationProvider.OPENROUTER,),
    )
    partial = SystemAlert(
        subject_id=None,
        integration_connection_id=llm_ownership.connection_id(
            IntegrationProvider.OPENROUTER
        ),
        domain=Domain.SYSTEM.value,
        severity=Severity.INFO.value,
        message="partial",
        alert_key=brief.EMPTY_DAY_ALERT_KEY,
        entity_ref="",
    )
    db_session.add(partial)
    await db_session.commit()

    with pytest.raises(alerts_service.AlertScopeConflictError):
        await brief._reconcile_empty_day_alert(
            db_session,
            identity=ownership.system_action(),
            empty=False,
        )
    assert partial.subject_id is None


async def _second_person(db_session) -> None:
    suffix = uuid.uuid4().hex
    owner = User(
        username=f"second-{suffix}",
        normalized_username=f"second-{suffix}",
        password_hash="test-only",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(owner)
    await db_session.flush()
    db_session.add(
        HealthSubject(
            owner_user_id=owner.id,
            display_name="second",
            timezone="Asia/Almaty",
        )
    )
    await db_session.commit()


async def test_the_empty_day_alert_is_written_for_its_own_subject(
    db_session, legacy_owner_roots
):
    """A second person does not stop the brief from marking an empty day.

    It used to: the reconciliation asks for the fully-unowned bridge, and the
    bridge demanded a sole subject whenever it was asked for. With no unowned
    alert in the installation there is nothing for it to adopt, so the write is
    an ordinary scoped one — and it lands on the identity's own subject, which
    is the property worth pinning here.
    """

    ownership = await _telegram_ownership(db_session)
    await _second_person(db_session)

    row = await brief._reconcile_empty_day_alert(
        db_session,
        identity=ownership.system_action(),
        empty=True,
    )
    assert row is not None
    assert row.subject_id == ownership.subject_id


async def test_an_unowned_alert_still_stops_the_empty_day_write(
    db_session, legacy_owner_roots
):
    """When there *is* something to adopt, two people close the bridge again."""

    ownership = await _telegram_ownership(db_session)
    db_session.add(
        SystemAlert(
            domain=Domain.SYSTEM.value,
            severity=Severity.INFO.value,
            message="orphaned",
            alert_key=brief.EMPTY_DAY_ALERT_KEY,
            entity_ref="",
        )
    )
    await db_session.commit()
    await _second_person(db_session)

    with pytest.raises(alerts_service.AlertLegacyBridgeError):
        await brief._reconcile_empty_day_alert(
            db_session,
            identity=ownership.system_action(),
            empty=True,
        )


async def test_empty_alert_reconciliation_rejects_actor_attribution(
    db_session, legacy_owner_roots
):
    ownership = await _telegram_ownership(db_session)

    with pytest.raises(brief.BriefOwnershipError, match="actorless"):
        await brief._reconcile_empty_day_alert(
            db_session,
            identity=ownership.owner_action(),
            empty=True,
        )


async def test_the_brief_says_what_its_buttons_are_for(
    garmin_owned_scope,
    db_session, session_factory, monkeypatch, legacy_owner_roots, owner_write,
):
    """Telegram renders the keyboard under the *whole* message, so four unlabelled
    taps ("зал", "лёгкий/обычный/тяжёлый день") arrive attached to nothing — and
    «тяжёлый день» is a question asked nowhere in the text at all. The stored
    brief keeps no hint: /reports shows it with no buttons underneath."""
    notifier = FakeNotifier()
    _patch_job(monkeypatch, notifier, FakeLLM())
    monkeypatch.setattr(brief, "today_local", lambda: DAY)
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)

    await brief.brief_job(session_factory)

    sent = notifier.sent[0]
    assert sent["buttons"]
    assert sent["text"].endswith(day_plan.HINT_FIX)
    stored = (await db_session.execute(select(WeeklyDigest))).scalars().one()
    assert day_plan.HINT_FIX not in stored.content


async def test_a_brief_with_nothing_left_to_ask_carries_no_hint(
    garmin_owned_scope,
    db_session, session_factory, monkeypatch, legacy_owner_roots, owner_write,
):
    """The hint leaves with the last button — a line pointing at a keyboard that
    isn't there is worse than no line."""
    notifier = FakeNotifier()
    _patch_job(monkeypatch, notifier, FakeLLM())
    monkeypatch.setattr(brief, "today_local", lambda: DAY)
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)
    ownership = await _telegram_ownership(db_session)
    for question in day_plan.QUESTIONS:
        await day_plan.record_answer(
            db_session,
            DAY,
            question.key,
            next(iter(question.labels)),
            identity=ownership.owner_action(),
            integration_connection_id=ownership.connection_id,
        )
    await db_session.commit()

    await brief.brief_job(session_factory)

    assert notifier.sent[0]["buttons"] is None
    assert day_plan.HINT_FIX not in notifier.sent[0]["text"]


async def test_job_sends_the_brief_even_when_garmin_sync_explodes(
    garmin_owned_scope,
    db_session, session_factory, monkeypatch, legacy_owner_roots, owner_write,
):
    """B6 pulls Garmin first, but a failed pull is not a reason to go quiet — the
    brief goes out on whatever is already in the lake."""
    notifier = FakeNotifier()
    _patch_job(monkeypatch, notifier, FakeLLM())
    monkeypatch.setattr(brief, "today_local", lambda: DAY)

    async def _boom(*a, **kw):
        raise RuntimeError("garmin said no")

    monkeypatch.setattr(garmin_service, "sync_job", _boom)
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)

    await brief.brief_job(session_factory)
    assert len(notifier.sent) == 1


def _patch_job(monkeypatch, notifier, llm):
    """Wire brief_job to a fake channel, a fake model and a no-op Garmin sync."""
    from vitals.integrations import llm_client

    async def _no_sync(*a, **kw):
        return None

    monkeypatch.setattr(garmin_service, "sync_job", _no_sync)
    _patch_bound_delivery(monkeypatch, notifier)
    monkeypatch.setattr(llm_client, "LLMClient", lambda *a, **kw: llm)


# ── The two buttons ───────────────────────────────────────────────────────────
async def test_build_button_shows_the_brief_and_sends_nothing(
    garmin_owned_scope,
    auth_client, db_session, monkeypatch, owner_write,
):
    from web.routers import reports as reports_router

    monkeypatch.setattr(brief, "today_local", lambda: DAY)
    monkeypatch.setattr(reports_router, "today_local", lambda: DAY)
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)

    r = await auth_client.post(
        "/reports/brief",
        data={"request_token": "build_token_1234567890123456"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/reports?brief=header"

    rows = (await db_session.execute(select(WeeklyDigest))).scalars().all()
    assert [row.kind for row in rows] == [DigestKind.DAILY_BRIEF.value]
    assert rows[0].source == Source.MANUAL.value
    ownership = await _telegram_ownership(db_session)
    assert (
        rows[0].subject_id,
        rows[0].actor_user_id,
        rows[0].integration_connection_id,
    ) == (
        ownership.subject_id,
        ownership.recipient_user_id,
        None,
    )
    assert (await db_session.execute(select(Notification))).scalars().all() == []

    page = await auth_client.get("/reports")
    assert "Сон 80" in page.text


async def test_build_button_does_not_require_a_delivery_channel(
    garmin_owned_scope,
    auth_client,
    db_session,
    monkeypatch, owner_write,
):
    """A web-only composition must not depend on a Telegram recipient root."""
    from web.routers import reports as reports_router

    telegram = await db_session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value
        )
    )
    assert telegram is not None
    await db_session.delete(telegram)
    await db_session.commit()

    monkeypatch.setattr(brief, "today_local", lambda: DAY)
    monkeypatch.setattr(reports_router, "today_local", lambda: DAY)
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)

    response = await auth_client.post(
        "/reports/brief",
        data={"request_token": "build_token_1234567890123457"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/reports?brief=header"
    stored = (await db_session.execute(select(WeeklyDigest))).scalars().one()
    assert stored.subject_id is not None
    assert stored.actor_user_id is not None
    assert stored.integration_connection_id is None


async def test_test_send_goes_out_off_budget(
    garmin_owned_scope, auth_client, db_session, monkeypatch, owner_write,
    legacy_owner_roots, telegram_connection_id,
):
    """The point of this button is catching broken formatting, so it must send even
    after the day's budget is spent — and it isn't the bot talking first."""
    from vitals.utils.timeutils import now_local
    from web.routers import reports as reports_router

    notifier = FakeNotifier()
    _patch_bound_delivery(monkeypatch, notifier)
    monkeypatch.setattr(brief, "today_local", lambda: DAY)
    monkeypatch.setattr(reports_router, "today_local", lambda: DAY)
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)

    # Today's budget, fully spent (dated *now*, which is what the budget counts).
    for i in range(delivery.DAILY_BUDGET):
        db_session.add(
            Notification(
                # A dedupe_key demands the whole root: subject, recipient, channel.
                subject_id=owner_write.subject_id,
                recipient_user_id=legacy_owner_roots.user_id,
                integration_connection_id=telegram_connection_id,
                sent_at=now_local(), category=delivery.CATEGORY_BRIEF,
                channel="telegram", dedupe_key=f"spent:{i}",
            )
        )
    await db_session.commit()
    ownership = await _telegram_ownership(db_session)
    assert (
        await delivery.sent_today(db_session, ownership=ownership)
        >= delivery.DAILY_BUDGET
    )
    await db_session.commit()

    r = await auth_client.post(
        "/reports/brief/test",
        data={"request_token": "test_token_12345678901234567"},
    )
    assert r.headers["location"] == "/reports?brief=sent"
    assert len(notifier.sent) == 1
    assert notifier.sent[0]["text"].startswith("Сон 80")


async def test_test_send_is_not_duplicated_by_a_second_tap(garmin_owned_scope, auth_client, db_session, monkeypatch, owner_write):
    """B2a: the test-send endpoint had no dedupe_key at all — the only delivery
    category with no dupe protection. A repeat call within the same day (a
    double-tap, or a retried request) must not fire a second Telegram message
    or pay for a second LLM call."""
    from web.routers import reports as reports_router

    notifier = FakeNotifier()
    _patch_bound_delivery(monkeypatch, notifier)
    monkeypatch.setattr(brief, "today_local", lambda: DAY)
    monkeypatch.setattr(reports_router, "today_local", lambda: DAY)
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)

    form = {"request_token": "test_token_12345678901234568"}
    r1 = await auth_client.post("/reports/brief/test", data=form)
    r2 = await auth_client.post("/reports/brief/test", data=form)

    assert r1.headers["location"] == "/reports?brief=sent"
    assert r2.headers["location"] == "/reports?brief=sent"
    assert len(notifier.sent) == 1
    journal = (await db_session.execute(select(Notification))).scalars().all()
    assert [n.category for n in journal] == [delivery.CATEGORY_TEST]
    digest = (await db_session.execute(select(WeeklyDigest))).scalars().one()
    ownership = await _telegram_ownership(db_session)
    assert (
        digest.actor_user_id,
        digest.integration_connection_id,
    ) == (
        ownership.recipient_user_id,
        None,
    )
    assert (
        journal[0].actor_user_id,
        journal[0].integration_connection_id,
    ) == (ownership.recipient_user_id, ownership.connection_id)


async def test_test_send_reports_an_existing_pending_claim_without_network(
    auth_client,
    db_session,
    monkeypatch,
):
    from web.routers import reports as reports_router

    notifier = FakeNotifier()
    _patch_bound_delivery(monkeypatch, notifier)
    monkeypatch.setattr(reports_router, "today_local", lambda: DAY)
    request_token = "test_token_pending_1234567890"
    ownership = await _telegram_ownership(db_session)
    bound = await channels.build_legacy_bound_notifier(db_session, ownership)
    prepared = await delivery.prepare_delivery_intent(
        db_session,
        bound,
        text="reserved test payload",
        category=delivery.CATEGORY_TEST,
        idempotency_key=delivery.make_delivery_idempotency_key(
            "brief-test",
            DAY,
            request_token,
        ),
        ownership=ownership,
        actor_user_id=ownership.recipient_user_id,
    )
    assert prepared is not None
    await db_session.commit()

    response = await auth_client.post(
        "/reports/brief/test",
        data={"request_token": request_token},
    )

    assert response.headers["location"] == "/reports?brief=pending"
    assert notifier.sent == []
    assert list(await db_session.scalars(select(WeeklyDigest))) == []


async def test_test_send_without_a_channel_says_so(garmin_owned_scope, auth_client, db_session, monkeypatch, owner_write):
    from vitals.services.proactive import channels
    from web.routers import reports as reports_router

    async def no_bound_channel(session, ownership, **kwargs):
        del session, ownership, kwargs
        return None

    monkeypatch.setattr(
        channels,
        "build_legacy_bound_notifier",
        no_bound_channel,
    )
    monkeypatch.setattr(brief, "today_local", lambda: DAY)
    monkeypatch.setattr(reports_router, "today_local", lambda: DAY)
    await _seed_day(
        db_session,
        owner_write,
    garmin_owned_scope=garmin_owned_scope)
    r = await auth_client.post(
        "/reports/brief/test",
        data={"request_token": "test_token_12345678901234569"},
    )
    assert r.headers["location"] == "/reports?brief=no_channel"
