"""The nudge engine and the Garmin light pulse.

Three things here fail silently if nobody watches them:

  * a condition that fires when it shouldn't turns the product into the nagging
    app it was explicitly designed not to be;
  * a cooldown that doesn't hold sends the same sentence every hour;
  * and the pulse — which re-ingests a *partial* payload every quarter hour — is
    one careless merge away from wiping last night's sleep and HRV off the day.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from vitals.enums import NotificationDeliveryStatus
from vitals.models.proactive import Notification, NotificationDeliveryIntent
from vitals.services import garmin_service, nutrition_service
from vitals.services.proactive import channels, delivery, nudges
from vitals.utils.timeutils import now_local

# The bot only speaks when the ``signals`` module is on — the same switch the
# owner flips in Settings, and it defaults off.
pytestmark = pytest.mark.usefixtures("signals_module_on")

TODAY = now_local().date()
EVENING = datetime.combine(TODAY, datetime.min.time()).replace(hour=19)
AFTERNOON = datetime.combine(TODAY, datetime.min.time()).replace(hour=17)

# A full day as the sync stores it — sleep and HRV included, which is exactly
# what a careless pulse would destroy.
FULL_DAY = {
    "summary": {"totalSteps": 3000, "restingHeartRate": 52},
    "sleep": {"dailySleepDTO": {"sleepScores": {"overall": {"value": 80}}}},
    "hrv": {"hrvSummary": {"lastNightAvg": 61}},
}


class FakeNotifier:
    channel = "telegram"

    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text, *, buttons=None, reply_to=None) -> str:
        self.sent.append(text)
        return str(500 + len(self.sent))

    async def answer_callback(self, callback_id, text="") -> None:
        pass

    async def edit(self, message_id, text, *, buttons=None) -> None:
        pass


class PulseClient:
    """One method, one call — the light pulse's whole contract."""

    def __init__(self, summary=None, raise_exc=None):
        self._summary = summary if summary is not None else {"totalSteps": 9100}
        self._raise = raise_exc
        self.is_configured = True
        self.calls: list[date] = []

    async def fetch_summary(self, on_date):
        self.calls.append(on_date)
        if self._raise is not None:
            raise self._raise
        return self._summary


async def _seed_steps(db_session, steps: int, *, on_date=TODAY, garmin_owned_scope):
    """A day of watch data belonging to this installation's sole owner.

    The legacy ingest still writes an unstamped row, so the seeder attaches the
    owner afterwards: the silence nudge reads its history through the scoped
    reader, and a row nobody owns is a row it correctly refuses to speak for.
    """
    from sqlalchemy import select

    from vitals.models.identity import HealthSubject

    row = await garmin_service.ingest_owned_daily(
        db_session, on_date, {"summary": {"totalSteps": steps}}
    , identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    row.subject_id = await db_session.scalar(select(HealthSubject.id).limit(2))
    await db_session.commit()


def _patch_owned_delivery(monkeypatch, notifier) -> None:
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


# ── Conditions stay quiet unless they actually hold ───────────────────────────
@pytest_asyncio.fixture
async def scoped_nudges(db_session, monkeypatch):
    """Run the nudge sweep for the sole owner, through the owned delivery path.

    Nudges are addressed to a person on a channel, so the run carries the
    ownership context the scheduler resolves in production rather than the
    zero-subject compatibility one.
    """

    from types import SimpleNamespace

    notifier = FakeNotifier()
    _patch_owned_delivery(monkeypatch, notifier)
    ownership = await channels.resolve_legacy_channel_ownership(
        db_session,
        actor_username=None,
    )
    await db_session.commit()

    async def run(**kwargs):
        return await nudges.run(db_session, notifier, ownership=ownership, **kwargs)

    return SimpleNamespace(run=run, notifier=notifier, ownership=ownership)


async def test_nothing_holds_nothing_is_sent(garmin_owned_scope, db_session, scoped_nudges):
    """The common case: an ordinary evening with the day on track."""
    await _seed_steps(db_session, nudges.STEPS_TARGET + 500, garmin_owned_scope=garmin_owned_scope)

    assert await scoped_nudges.run(now=EVENING) == []
    assert scoped_nudges.notifier.sent == []


async def test_steps_nudge_waits_for_the_evening(garmin_owned_scope, db_session, scoped_nudges):
    """Same data, wrong hour: at 17:00 a low step count is not yet news."""
    await _seed_steps(db_session, 3000, garmin_owned_scope=garmin_owned_scope)

    assert await scoped_nudges.run(now=AFTERNOON) == []

    sent = await scoped_nudges.run(now=EVENING)
    assert len(sent) == 1
    assert "3000" in scoped_nudges.notifier.sent[0]
    assert sent[0].category == delivery.CATEGORY_NUDGE


async def test_missing_data_is_not_a_nudge(garmin_owned_scope, db_session, scoped_nudges):
    """No Garmin row means the watch hasn't synced, not that he sat still — and
    no meals logged means an untracked day, not a missed protein target."""
    assert await scoped_nudges.run(now=EVENING) == []

    await _seed_steps(db_session, 0, garmin_owned_scope=garmin_owned_scope)
    assert await scoped_nudges.run(now=EVENING) == []


async def test_protein_nudge_fires_while_the_day_is_still_open(
    db_session, owner_write, monkeypatch
):
    await nutrition_service.log_meal(
        db_session,
        on_date=TODAY,
        name="Курица с рисом",
        protein_g=60.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(TODAY),
    )
    notifier = FakeNotifier()
    _patch_owned_delivery(monkeypatch, notifier)
    # A protein nudge is about one person's day, so the run is scoped.
    ownership = await channels.resolve_legacy_channel_ownership(
        db_session,
        actor_username=None,
    )
    await db_session.commit()

    sent = await nudges.run(
        db_session, notifier, now=AFTERNOON, ownership=ownership
    )
    assert len(sent) == 1
    assert "60" in notifier.sent[0] and "150" in notifier.sent[0]

    # …and not at 22:00, when there is nothing left to fix.
    await db_session.execute(Notification.__table__.delete())
    await db_session.commit()
    assert await nudges.run(
        db_session, notifier, now=EVENING.replace(hour=22), ownership=ownership
    ) == []


async def test_garmin_silence_needs_two_days_and_some_history(garmin_owned_scope, db_session, scoped_nudges):
    """Never fires on an empty lake: that's an integration nobody set up."""
    assert await scoped_nudges.run(now=AFTERNOON) == []

    # One day behind is an ordinary gap between polls, not a broken watch.
    await _seed_steps(db_session, 5000, on_date=TODAY - timedelta(days=1), garmin_owned_scope=garmin_owned_scope)
    assert await scoped_nudges.run(now=AFTERNOON) == []


async def test_garmin_silence_fires_after_two_days(garmin_owned_scope, db_session, scoped_nudges):
    await _seed_steps(db_session, 5000, on_date=TODAY - timedelta(days=2), garmin_owned_scope=garmin_owned_scope)

    sent = await scoped_nudges.run(now=AFTERNOON)
    assert len(sent) == 1
    assert "Garmin" in scoped_nudges.notifier.sent[0]


async def test_garmin_silence_is_announced_once_per_episode(garmin_owned_scope, db_session, scoped_nudges):
    """The 24-hour cooldown alone re-sends the same sentence every morning for
    as long as the watch stays unworn. It is only news once."""
    await _seed_steps(db_session, 5000, on_date=TODAY - timedelta(days=2), garmin_owned_scope=garmin_owned_scope)

    assert len(await scoped_nudges.run(now=AFTERNOON)) == 1
    await db_session.commit()

    # A day later the cooldown is spent — but it is the same silence.
    tomorrow = AFTERNOON + timedelta(days=1)
    assert await scoped_nudges.run(now=tomorrow) == []
    assert len(scoped_nudges.notifier.sent) == 1

    # The watch syncs, then goes quiet again: a new episode, worth saying once.
    await _seed_steps(db_session, 5000, on_date=tomorrow.date(), garmin_owned_scope=garmin_owned_scope)
    assert len(await scoped_nudges.run(now=tomorrow + timedelta(days=2))) == 1


# ── The cooldown ──────────────────────────────────────────────────────────────
async def test_cooldown_holds_the_second_run(garmin_owned_scope, db_session, scoped_nudges):
    """The job runs hourly; without the cooldown the same sentence arrives at
    19:05, 20:05 and 21:05."""
    await _seed_steps(db_session, 3000, garmin_owned_scope=garmin_owned_scope)

    assert len(await scoped_nudges.run(now=EVENING)) == 1
    await db_session.commit()

    assert await scoped_nudges.run(now=EVENING + timedelta(hours=1)) == []
    assert len(scoped_nudges.notifier.sent) == 1

    # A day later — same story, new day — the nudge is allowed again.
    later = EVENING + timedelta(hours=25)
    await _seed_steps(db_session, 3000, on_date=later.date(), garmin_owned_scope=garmin_owned_scope)
    assert len(await scoped_nudges.run(now=later)) == 1


async def test_owned_nudge_uses_opaque_durable_claim_and_transaction_free_send(
    garmin_owned_scope,
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    class TransactionCheckingNotifier(FakeNotifier):
        async def send(self, text, *, buttons=None, reply_to=None) -> str:
            assert not db_session.in_transaction()
            return await super().send(
                text,
                buttons=buttons,
                reply_to=reply_to,
            )

    notifier = TransactionCheckingNotifier()
    _patch_owned_delivery(monkeypatch, notifier)
    ownership = await channels.resolve_legacy_channel_ownership(
        db_session,
        actor_username=None,
    )
    await db_session.commit()
    await _seed_steps(db_session, 3000, garmin_owned_scope=garmin_owned_scope)

    sent = await nudges.run(
        db_session,
        notifier,
        now=EVENING,
        ownership=ownership,
    )

    assert len(sent) == 1
    intent = await db_session.scalar(select(NotificationDeliveryIntent))
    assert intent.status == NotificationDeliveryStatus.SENT.value
    assert intent.idempotency_key == delivery.make_delivery_idempotency_key(
        "nudge",
        "steps_short",
        EVENING.date(),
        EVENING.hour,
    )
    assert intent.policy_key == delivery.make_delivery_policy_key(
        "nudge",
        "steps_short",
    )
    assert sent[0].delivery_intent_id == intent.id
    assert sent[0].dedupe_key == intent.idempotency_key


async def test_ambiguous_owned_nudge_claim_holds_cooldown_without_retry(
    garmin_owned_scope,
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    class AmbiguousNotifier(FakeNotifier):
        async def send(self, text, *, buttons=None, reply_to=None) -> str:
            self.sent.append(text)
            return "invalid-provider-id"

    notifier = AmbiguousNotifier()
    _patch_owned_delivery(monkeypatch, notifier)
    ownership = await channels.resolve_legacy_channel_ownership(
        db_session,
        actor_username=None,
    )
    await db_session.commit()
    await _seed_steps(db_session, 3000, garmin_owned_scope=garmin_owned_scope)

    assert await nudges.run(
        db_session,
        notifier,
        now=EVENING,
        ownership=ownership,
    ) == []
    assert await nudges.run(
        db_session,
        notifier,
        now=EVENING + timedelta(hours=1),
        ownership=ownership,
    ) == []

    assert len(notifier.sent) == 1
    intent = await db_session.scalar(select(NotificationDeliveryIntent))
    assert intent.status == NotificationDeliveryStatus.AMBIGUOUS.value


async def test_ambiguous_garmin_silence_claim_blocks_the_whole_episode(
    garmin_owned_scope,
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    class AmbiguousNotifier(FakeNotifier):
        async def send(self, text, *, buttons=None, reply_to=None) -> str:
            self.sent.append(text)
            return "invalid-provider-id"

    notifier = AmbiguousNotifier()
    _patch_owned_delivery(monkeypatch, notifier)
    ownership = await channels.resolve_legacy_channel_ownership(
        db_session,
        actor_username=None,
    )
    await db_session.commit()
    await _seed_steps(db_session, 5000, on_date=TODAY - timedelta(days=2), garmin_owned_scope=garmin_owned_scope)

    assert await nudges.run(
        db_session,
        notifier,
        now=AFTERNOON,
        ownership=ownership,
    ) == []
    assert await nudges.run(
        db_session,
        notifier,
        now=AFTERNOON + timedelta(days=2),
        ownership=ownership,
    ) == []

    assert len(notifier.sent) == 1
    intent = await db_session.scalar(select(NotificationDeliveryIntent))
    assert intent.status == NotificationDeliveryStatus.AMBIGUOUS.value


# ── The budget ────────────────────────────────────────────────────────────────
async def test_budget_cuts_the_nudge_that_does_not_fit(
    garmin_owned_scope, db_session, owner_write, monkeypatch,
    legacy_owner_roots, telegram_connection_id,
):
    """Two conditions hold, one slot is left — so exactly one goes out. The brief
    and the evening block spend two of the four every day, which is why a nudge
    is never the message that matters most."""
    await _seed_steps(db_session, 3000, garmin_owned_scope=garmin_owned_scope)  # steps: short
    await nutrition_service.log_meal(
        db_session,
        on_date=TODAY,
        name="Творог",
        protein_g=40.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(TODAY),
    )  # protein: behind
    for i in range(delivery.DAILY_BUDGET - 1):  # brief, evening, one earlier nudge
        db_session.add(
            Notification(
                # A dedupe_key demands the whole root: subject, recipient, channel.
                subject_id=owner_write.subject_id,
                recipient_user_id=legacy_owner_roots.user_id,
                integration_connection_id=telegram_connection_id,
                sent_at=EVENING,
                category=delivery.CATEGORY_BRIEF,
                channel="telegram",
                dedupe_key=f"spent:{i}",
            )
        )
    notifier = FakeNotifier()
    _patch_owned_delivery(monkeypatch, notifier)
    ownership = await channels.resolve_legacy_channel_ownership(
        db_session,
        actor_username=None,
    )
    await db_session.commit()

    sent = await nudges.run(
        db_session, notifier, now=EVENING, ownership=ownership
    )
    assert len(sent) == 1
    assert len(notifier.sent) == 1


async def test_a_broken_condition_does_not_take_the_others_down(garmin_owned_scope, db_session, monkeypatch, scoped_nudges):
    """A registry is only safe to extend if one bad entry can't silence the rest."""

    async def _boom(session, ctx):
        raise RuntimeError("bad rule")

    await _seed_steps(db_session, 3000, garmin_owned_scope=garmin_owned_scope)
    broken = nudges.NudgeSpec("broken", "test", _boom, lambda ctx: "never")
    monkeypatch.setattr(nudges, "NUDGES", (broken,) + nudges.NUDGES)
    assert len(await scoped_nudges.run(now=EVENING)) == 1


# ── The light pulse (N3) ──────────────────────────────────────────────────────
async def test_pulse_refreshes_steps_without_losing_the_night(db_session, *, garmin_owned_scope):
    """The merge is the whole point: a bare {"summary": …} normalised over the
    day would blank sleep and HRV, and the morning brief would go out empty."""
    await garmin_service.ingest_owned_daily(db_session, TODAY, FULL_DAY, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    await db_session.commit()

    client = PulseClient({"totalSteps": 9100, "restingHeartRate": 52})
    out = await garmin_service.pulse_owned(db_session, client, on_date=TODAY, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    await db_session.commit()

    row = await garmin_service.get_daily(db_session, TODAY)
    assert out["steps"] == 9100 and row.steps == 9100
    assert row.sleep_score == 80
    assert row.hrv_avg == 61
    assert client.calls == [TODAY]  # exactly one upstream call


async def test_pulse_ignores_an_empty_response(db_session, *, garmin_owned_scope):
    """An empty summary is a failed call, not a day without steps."""
    await garmin_service.ingest_owned_daily(db_session, TODAY, FULL_DAY, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    await db_session.commit()

    out = await garmin_service.pulse_owned(db_session, PulseClient({}), on_date=TODAY, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    await db_session.commit()

    assert out["error"] == "empty"
    row = await garmin_service.get_daily(db_session, TODAY)
    assert row.steps == 3000 and row.sleep_score == 80


async def test_pulse_swallows_a_throttled_login(db_session, *, garmin_owned_scope):
    """The breaker refusing a login is the system working. Every 15 minutes it
    must not raise, and it must not raise an alert either — the 4×/day sync owns
    that one and would otherwise flap it."""
    from vitals.integrations.garmin_client import GarminLoginThrottled
    from vitals.models.system_alert import SystemAlert
    from sqlalchemy import select

    client = PulseClient(raise_exc=GarminLoginThrottled("daily allowance spent"))
    out = await garmin_service.pulse_owned(db_session, client, on_date=TODAY, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    await db_session.commit()

    assert out["error"] == "throttled"
    assert (await db_session.execute(select(SystemAlert))).scalars().all() == []


@pytest.mark.parametrize("hour, runs", [(3, False), (9, True), (23, True)])
async def test_pulse_job_only_runs_in_active_hours(
    session_factory, garmin_connected, monkeypatch, hour, runs
):
    """``garmin_connected`` because "is Garmin configured" is now a question
    about this record rather than about the process: the job resolves the
    subject's own account before it builds a client, so a fake client claiming
    ``is_configured`` is no longer what decides."""

    calls: list[str] = []

    class _Client:
        is_configured = True

        @classmethod
        def from_config(cls, config=None, redis=None):
            calls.append("built")
            return cls()

        async def fetch_summary(self, on_date):
            return {"totalSteps": 1000}

    monkeypatch.setattr(
        garmin_service, "now_local", lambda: datetime.combine(TODAY, datetime.min.time()).replace(hour=hour)
    )
    import vitals.integrations.garmin_client as gc

    monkeypatch.setattr(gc, "GarminClient", _Client)
    await garmin_service.pulse_job(session_factory)
    assert bool(calls) is runs
