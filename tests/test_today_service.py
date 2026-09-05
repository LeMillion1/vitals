"""The landing screen's assembly (``GET /today``).

Every one of these is really the same question asked from a different angle: can
the first screen the owner sees be assembled from whatever happens to be in the
lake — nothing at all, one domain, all of them — without waiting on the model and
without rendering a card for a module that is switched off.
"""
from __future__ import annotations

from vitals.services.milestones import goals as milestone_goals

from vitals.services.nutrition import writes as nutrition_writes

from vitals.services.digest import ownership as digest_ownership

import pytest

from datetime import timedelta

from vitals.enums import DigestKind, Domain, Severity, Source
from vitals.models.milestones import DOMAIN as INSIGHTS_DOMAIN, WeeklyDigest
from vitals.models.system_alert import SystemAlert
from vitals.ownership import WriteIdentity
from vitals.services import weight as weight_domain
from vitals.services.dashboard import today as today_service
from vitals.utils.timeutils import today_local

ALL_OFF: dict[str, bool] = {}
ALL_ON = {"nutrition": True, "timeline": True, "signals": True, "glp1": True, "hevy": True}

# The first screen belongs to the person looking at it.
pytestmark = pytest.mark.usefixtures("owned_by_legacy_subject")




async def test_build_survives_an_empty_database(db_session, legacy_owner_roots):
    """Nothing logged, no integrations, no digest — still a coherent screen."""
    ctx = await today_service.build(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
            db_session,
            identity=WriteIdentity(
                legacy_owner_roots.subject_id, legacy_owner_roots.user_id
            ),
            owner_user_id=legacy_owner_roots.user_id,
        ),
        enabled_modules=ALL_ON)

    assert ctx["date"] == today_local()
    assert ctx["narrative"]  # never blank: the fallback sentence stands in
    assert ctx["narrative_source"] == "computed"
    assert ctx["feed"] == []
    assert ctx["changes"] == []
    assert ctx["goal"] is None
    # The figures row is the page's skeleton — it renders with em dashes rather
    # than collapsing, so the screen doesn't change shape once data arrives.
    assert [f["key"] for f in ctx["figures"]][:4] == [
        "weight",
        "sleep_score",
        "hrv_avg",
        "body_battery_high",
    ]
    # Nothing measured reads as an em dash, not a zero. "Съедено 0" is the one
    # honest zero here — no meal logged today is a real number of calories.
    assert all(f["value"] == "—" for f in ctx["figures"][:4])


# What ``generate_brief`` actually stores: the whole message that went to
# Telegram — the deterministic header, the day line, then the model's block.
BRIEF_CONTEXT = {
    "garmin": {"sleep_score": 75, "hrv_avg": 42, "resting_hr": 63, "body_battery_high": 86},
    "weight": {"latest_kg": 106.8, "trend_kg_per_week": -1.06},
    "day": {"answers": {"where": "дома", "gym": "нет"}, "source": Source.TEMPLATE.value},
}
BRIEF_PROSE = "HRV и пульс покоя держатся на твоём базовом уровне — крути день как обычно."


def _stored_brief(
    prose: str = BRIEF_PROSE,
    *,
    model: str | None = "test-model",
    actor_user_id=None,
    integration_connection_id=None, legacy_owner_roots,
):
    """A brief row shaped the way the morning job writes one."""
    from vitals.services.proactive import compose

    blocks = compose.header_blocks(BRIEF_CONTEXT)
    if prose:
        blocks.append(compose.Block(compose.KIND_NARRATIVE, prose, 90))
    return WeeklyDigest(subject_id=legacy_owner_roots.subject_id,
        date=today_local(),
        actor_user_id=actor_user_id,
        integration_connection_id=integration_connection_id,
        domain=INSIGHTS_DOMAIN,
        source=Source.MANUAL.value,
        kind=DigestKind.DAILY_BRIEF.value,
        content=compose.render(blocks),
        context_json=BRIEF_CONTEXT,
        model=model,
    )


async def _openrouter_connection(session, roots):
    from sqlalchemy import select

    from vitals.enums import IntegrationConnectionType, IntegrationProvider
    from vitals.models.tenancy import IntegrationConnection

    return await session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == roots.subject_id,
            IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.AI_GATEWAY.value,
        )
    )




async def test_hero_takes_only_the_prose_out_of_todays_brief(db_session, legacy_owner_roots):
    """The stored brief is the whole Telegram message, header numbers included —
    printed whole it made the hero repeat every key figure back in 38px."""
    # A stored model name needs the provider connection it came through.
    connection = await _openrouter_connection(db_session, legacy_owner_roots)
    db_session.add(
        _stored_brief(
            actor_user_id=legacy_owner_roots.user_id,
            integration_connection_id=connection.id,
        legacy_owner_roots=legacy_owner_roots)
    )
    await db_session.commit()

    ctx = await today_service.build(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
            db_session,
            identity=WriteIdentity(
                legacy_owner_roots.subject_id, legacy_owner_roots.user_id
            ),
            owner_user_id=legacy_owner_roots.user_id,
        ),
        enabled_modules=ALL_OFF)

    assert ctx["narrative"] == BRIEF_PROSE
    assert ctx["narrative_source"] == "digest"
    assert "Body Battery" not in ctx["narrative"]
    assert "HRV 42" not in ctx["narrative"]
    # The brief is the one feed row allowed to carry the accent.
    assert [row["dot"] for row in ctx["feed"]] == ["amber"]


async def test_a_header_only_brief_falls_back_to_the_computed_sentence(db_session, legacy_owner_roots):
    """The model stayed silent that morning, so the row is numbers only — promoting
    a number line into the headline is worse than composing one."""
    db_session.add(_stored_brief(prose="", model=None, actor_user_id=legacy_owner_roots.user_id, legacy_owner_roots=legacy_owner_roots))
    await db_session.commit()

    ctx = await today_service.build(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
            db_session,
            identity=WriteIdentity(
                legacy_owner_roots.subject_id, legacy_owner_roots.user_id
            ),
            owner_user_id=legacy_owner_roots.user_id,
        ),
        enabled_modules=ALL_OFF)

    assert ctx["narrative_source"] == "computed"
    assert ctx["feed"] == []


async def test_yesterdays_brief_does_not_stand_in_for_today(db_session, legacy_owner_roots):
    """A narrative is about a day. Yesterday's read as today's is worse than none."""
    db_session.add(
        WeeklyDigest(subject_id=legacy_owner_roots.subject_id,
            date=today_local() - timedelta(days=1),
            actor_user_id=legacy_owner_roots.user_id,
            domain=INSIGHTS_DOMAIN,
            source=Source.MANUAL.value,
            kind=DigestKind.DAILY_BRIEF.value,
            content="Вчерашний текст.",
        )
    )
    await db_session.commit()

    ctx = await today_service.build(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
            db_session,
            identity=WriteIdentity(
                legacy_owner_roots.subject_id, legacy_owner_roots.user_id
            ),
            owner_user_id=legacy_owner_roots.user_id,
        ),
        enabled_modules=ALL_OFF)

    assert ctx["narrative_source"] == "computed"
    assert "Вчерашний текст." != ctx["narrative"]


async def test_weight_drives_the_figure_and_the_fallback_sentence(db_session, legacy_owner_roots, owner_write):
    for offset, kg in ((14, 95.0), (7, 93.5), (0, 92.0)):
        await weight_domain.writes.log_weight(
            db_session,
            on_date=today_local() - timedelta(days=offset),
            weight_kg=kg,
            identity=owner_write.identity,
            prepared_weight_write=await owner_write.weight_write(today_local() - timedelta(days=offset)),
        )
    await db_session.commit()

    ctx = await today_service.build(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
            db_session,
            identity=WriteIdentity(
                legacy_owner_roots.subject_id, legacy_owner_roots.user_id
            ),
            owner_user_id=legacy_owner_roots.user_id,
        ),
        enabled_modules=ALL_OFF)

    weight_figure = ctx["figures"][0]
    assert weight_figure["key"] == "weight"
    assert weight_figure["value"] == "92"
    assert "92" in ctx["narrative"]
    # A week-over-week weight row needs both ends of the window; with three
    # weigh-ins spanning a fortnight there is one.
    assert [c["key"] for c in ctx["changes"]] == ["weight"]
    assert ctx["changes"][0]["href"] == "/weight"


async def test_a_disabled_module_contributes_nothing(db_session, legacy_owner_roots, owner_write):
    """Nutrition off: no calories figure, no meal in the feed — not an empty card."""
    await nutrition_writes.log_meal(
        db_session,
        on_date=today_local(),
        name="Курица с рисом",
        calories=520,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(today_local()),
    )
    await db_session.commit()

    off = await today_service.build(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
            db_session,
            identity=WriteIdentity(
                legacy_owner_roots.subject_id, legacy_owner_roots.user_id
            ),
            owner_user_id=legacy_owner_roots.user_id,
        ),
        enabled_modules=ALL_OFF)
    assert "calories" not in [f["key"] for f in off["figures"]]
    assert off["feed"] == []

    on = await today_service.build(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
            db_session,
            identity=WriteIdentity(
                legacy_owner_roots.subject_id, legacy_owner_roots.user_id
            ),
            owner_user_id=legacy_owner_roots.user_id,
        ),
        enabled_modules={"nutrition": True})
    assert "calories" in [f["key"] for f in on["figures"]]
    assert [row["text"] for row in on["feed"]] == ["Курица с рисом"]


async def test_calories_change_compares_logged_days(db_session, legacy_owner_roots, owner_write):
    """Intake week over week is per *logged* day: a week with three days filled in
    must not read as a crash in intake that never happened."""
    for offset, cal in ((10, 1800.0), (9, 2000.0), (2, 1500.0)):
        await nutrition_writes.log_meal(
            db_session,
            on_date=today_local() - timedelta(days=offset),
            name="Обед",
            calories=cal,
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(today_local() - timedelta(days=offset)),
        )
    await db_session.commit()

    ctx = await today_service.build(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
            db_session,
            identity=WriteIdentity(
                legacy_owner_roots.subject_id, legacy_owner_roots.user_id
            ),
            owner_user_id=legacy_owner_roots.user_id,
        ),
        enabled_modules={"nutrition": True})

    row = next(c for c in ctx["changes"] if c["key"] == "calories")
    assert row["href"] == "/nutrition"
    assert row["sentence"].startswith("1900 → 1500")   # (1800+2000)/2 → 1500/1
    assert row["delta"] == "−400"


async def test_goal_reads_as_distance_covered(db_session, legacy_owner_roots, owner_write):
    """The bar needs a starting point, and milestone progress has no notion of one
    — the first logged weight is what "11.2 of 17.5 covered" is measured from."""
    for offset, kg in ((30, 100.0), (0, 94.0)):
        await weight_domain.writes.log_weight(
            db_session,
            on_date=today_local() - timedelta(days=offset),
            weight_kg=kg,
            identity=owner_write.identity,
            prepared_weight_write=await owner_write.weight_write(today_local() - timedelta(days=offset)),
        )
    await milestone_goals.create_milestone(
        db_session, name="Дойти до 85", domain=Domain.WEIGHT.value,
        target_value=85.0, target_unit="кг",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await db_session.commit()

    goal = (await today_service.build(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
            db_session,
            identity=WriteIdentity(
                legacy_owner_roots.subject_id, legacy_owner_roots.user_id
            ),
            owner_user_id=legacy_owner_roots.user_id,
        ),
        enabled_modules=ALL_OFF))["goal"]

    assert goal["done"] == "6"     # 100 → 94
    assert goal["total"] == "15"   # 100 → 85
    assert goal["pct"] == 40


async def test_equal_baseline_goal_stays_visible_without_invented_progress(
    auth_client,
    db_session,
    legacy_owner_roots,
    owner_write,
):
    """A valid active goal exists even when its progress span is zero."""

    for offset, kg in ((1, 72.0), (0, 72.5)):
        on_date = today_local() - timedelta(days=offset)
        await weight_domain.writes.log_weight(
            db_session,
            on_date=on_date,
            weight_kg=kg,
            identity=owner_write.identity,
            prepared_weight_write=await owner_write.weight_write(on_date),
        )
    await milestone_goals.create_milestone(
        db_session,
        name="QA active target",
        domain=Domain.WEIGHT.value,
        target_value=72.0,
        target_unit="kg",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await db_session.commit()

    goal = (
        await today_service.build(
            db_session,
            subject_id=legacy_owner_roots.subject_id,
            prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
                db_session,
                identity=WriteIdentity(
                    legacy_owner_roots.subject_id, legacy_owner_roots.user_id
                ),
                owner_user_id=legacy_owner_roots.user_id,
            ),
            enabled_modules=ALL_OFF,
        )
    )["goal"]

    assert goal["name"] == "QA active target"
    assert goal["target"] == "72"
    assert goal["current"] == "72.5"
    assert goal["pct"] is None
    assert goal["done"] is None
    assert goal["total"] is None

    response = await auth_client.get("/today", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "QA active target" in response.text
    assert "72,5 kg" in response.text
    assert '<div class="v-meter">' not in response.text
    assert 'href="/reports"' in response.text


async def test_today_selects_an_active_goal_without_a_weight_baseline(
    db_session,
    legacy_owner_roots,
    owner_write,
):
    """Inactive or unit-incompatible goals cannot hide a current weight goal."""

    inactive = await milestone_goals.create_milestone(
        db_session,
        name="Old paused goal",
        domain=Domain.WEIGHT.value,
        target_value=80.0,
        target_unit="kg",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await milestone_goals.set_status(
        db_session,
        inactive.id,
        "paused",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await milestone_goals.create_milestone(
        db_session,
        name="Mislabeled body-fat goal",
        domain=Domain.WEIGHT.value,
        target_value=15.0,
        target_unit="%",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await milestone_goals.create_milestone(
        db_session,
        name="Current active goal",
        domain=Domain.WEIGHT.value,
        target_value=72.0,
        target_unit="kg",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await db_session.commit()

    goal = (
        await today_service.build(
            db_session,
            subject_id=legacy_owner_roots.subject_id,
            prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
                db_session,
                identity=WriteIdentity(
                    legacy_owner_roots.subject_id, legacy_owner_roots.user_id
                ),
                owner_user_id=legacy_owner_roots.user_id,
            ),
            enabled_modules=ALL_OFF,
        )
    )["goal"]

    assert goal["name"] == "Current active goal"
    assert goal["current"] is None
    assert goal["pct"] is None


async def test_recovery_advice_arrives_as_an_observation(db_session, legacy_owner_roots, *, garmin_connection_id):
    """An interpretation of the numbers is not a failure: it joins the attention
    card on the quietest rung, never as a warning."""
    from vitals.models.garmin import GarminDaily

    db_session.add(
        GarminDaily(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id,
            date=today_local(),
            domain=Domain.GARMIN.value,
            source=Source.GARMIN_API.value,
            sleep_score=41,
            hrv_avg=28,
            body_battery_high=44,
        )
    )
    await db_session.commit()

    ctx = await today_service.build(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
            db_session,
            identity=WriteIdentity(
                legacy_owner_roots.subject_id, legacy_owner_roots.user_id
            ),
            owner_user_id=legacy_owner_roots.user_id,
        ),
        enabled_modules=ALL_OFF)

    notes = [a for a in ctx["attention"] if a["severity"] == Severity.NOTE.value]
    assert notes, ctx["attention"]
    assert all(a["severity"] != Severity.WARN.value for a in notes)


async def test_platform_scheduler_diagnostics_never_reach_today_attention(
    db_session,
    legacy_owner_roots,
):
    sentinel = "secret-path:/srv/private/trace.sql"
    # All four rows name this subject, the sentinel included: the reader already
    # refuses anyone else's alerts, so leaving the platform diagnostic ownerless
    # would let that scope filter pass the test and never exercise the key
    # classifier this test is about.
    db_session.add_all(
        [
            SystemAlert(
                subject_id=legacy_owner_roots.subject_id,
                domain=Domain.SYSTEM.value,
                severity=Severity.WARN.value,
                message=sentinel,
                alert_key="scheduler.job_failed:raw_payload_sweep",
                entity_ref="raw_payload_sweep",
            ),
            SystemAlert(
                subject_id=legacy_owner_roots.subject_id,
                domain=Domain.SYSTEM.value,
                severity=Severity.INFO.value,
                message="subject-visible",
                alert_key="brief_empty_day",
                entity_ref="",
            ),
            SystemAlert(
                subject_id=legacy_owner_roots.subject_id,
                domain=Domain.SYSTEM.value,
                severity=Severity.WARN.value,
                message="subject-job-visible",
                alert_key="scheduler.job_failed:weekly_digest",
                entity_ref="weekly_digest",
            ),
            SystemAlert(
                subject_id=legacy_owner_roots.subject_id,
                domain=Domain.SYSTEM.value,
                severity=Severity.WARN.value,
                message="provider-job-visible",
                alert_key="scheduler.job_failed:garmin_sync",
                entity_ref="garmin_sync",
            ),
        ]
    )
    await db_session.commit()

    ctx = await today_service.build(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
            db_session,
            identity=WriteIdentity(
                legacy_owner_roots.subject_id, legacy_owner_roots.user_id
            ),
            owner_user_id=legacy_owner_roots.user_id,
        ),
        enabled_modules=ALL_OFF)

    messages = {row["message"] for row in ctx["attention"]}
    assert "subject-visible" in messages
    assert "subject-job-visible" in messages
    assert "provider-job-visible" in messages
    assert sentinel not in messages


async def test_feed_stays_a_glance_on_a_busy_day(db_session, legacy_owner_roots, owner_write):
    """Every seeded goal and supplement carries today's date on a first run — the
    card is the day at a glance, so it is capped rather than left unbounded."""
    for i in range(20):
        await nutrition_writes.log_meal(
            db_session,
            on_date=today_local(),
            name=f"Приём {i}",
            calories=100,
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write(today_local()),
        )
    await db_session.commit()

    ctx = await today_service.build(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        prepared_digest_owner=await digest_ownership.prepare_digest_owner_for_identity(
            db_session,
            identity=WriteIdentity(
                legacy_owner_roots.subject_id, legacy_owner_roots.user_id
            ),
            owner_user_id=legacy_owner_roots.user_id,
        ),
        enabled_modules={"nutrition": True})

    assert len(ctx["feed"]) == today_service._FEED_LIMIT
