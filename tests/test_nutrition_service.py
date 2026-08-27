"""Nutrition service tests — meal logging, daily summary, period summary."""
from __future__ import annotations

from vitals.services.nutrition import analytics as nutrition_analytics
from vitals.services.nutrition import queries as nutrition_queries
from vitals.services.nutrition import writes as nutrition_writes

from datetime import date, time






def _make_cfg(**overrides):
    from vitals.config import Config
    defaults = dict(
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        nutrition_protein_target_g=150.0,
        nutrition_calories_min=1300,
        nutrition_calories_max=1700,
    )
    defaults.update(overrides)
    return Config(**defaults)


async def test_log_meal_creates_row(db_session, owner_write):
    m = await nutrition_writes.log_meal(
        db_session,
        on_date=date(2026, 6, 1),
        name="2 eggs, toast",
        eaten_at=time(8, 30),
        calories=350.0,
        protein_g=20.0,
        fat_g=12.0,
        carbs_g=30.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 1)),
    )
    await db_session.commit()
    assert m.id is not None
    assert m.name == "2 eggs, toast"
    assert m.calories == 350.0
    assert m.domain == "nutrition"
    assert m.source == "manual"


async def test_multiple_meals_same_day(db_session, owner_write):
    d = date(2026, 6, 2)
    await nutrition_writes.log_meal(
        db_session, on_date=d, name="breakfast", calories=300,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await nutrition_writes.log_meal(
        db_session, on_date=d, name="lunch", calories=500,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await nutrition_writes.log_meal(
        db_session, on_date=d, name="dinner", calories=600,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await db_session.commit()

    meals = await nutrition_queries.list_meals_for_date(
        db_session, d, subject_id=owner_write.subject_id
    )
    assert len(meals) == 3


async def test_update_meal(db_session, owner_write):
    m = await nutrition_writes.log_meal(
        db_session, on_date=date(2026, 6, 3), name="original", calories=100,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 3)),
    )
    await db_session.commit()

    updated = await nutrition_writes.update_meal(
        db_session, m.id, on_date=date(2026, 6, 3), name="updated", calories=200,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 3)),
    )
    await db_session.commit()
    assert updated is not None
    assert updated.name == "updated"
    assert updated.calories == 200


async def test_delete_meal(db_session, owner_write):
    m = await nutrition_writes.log_meal(
        db_session, on_date=date(2026, 6, 4), name="to delete",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 4)),
    )
    await db_session.commit()

    result = await nutrition_writes.delete_meal(
        db_session, m.id,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await db_session.commit()
    assert result is True

    meals = await nutrition_queries.list_meals_for_date(
        db_session, date(2026, 6, 4), subject_id=owner_write.subject_id
    )
    assert len(meals) == 0


async def test_delete_nonexistent_returns_false(db_session, owner_write):
    result = await nutrition_writes.delete_meal(
        db_session, 9999,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    assert result is False


async def test_daily_summary_totals_and_on_track(db_session, owner_write):
    d = date(2026, 6, 5)
    await nutrition_writes.log_meal(
        db_session, on_date=d, name="meal1", calories=600, protein_g=50,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await nutrition_writes.log_meal(
        db_session, on_date=d, name="meal2", calories=700, protein_g=60,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await nutrition_writes.log_meal(
        db_session, on_date=d, name="meal3", calories=300, protein_g=50,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await db_session.commit()

    summary = await nutrition_analytics.daily_summary(
        db_session, d, subject_id=owner_write.subject_id
    )

    assert summary["meal_count"] == 3
    assert summary["totals"]["calories"] == 1600
    assert summary["totals"]["protein_g"] == 160
    assert summary["on_track"]["calories"] is True   # 1300 <= 1600 <= 1700
    assert summary["on_track"]["protein"] is True    # 160 >= 150


async def test_daily_summary_under_target(db_session, owner_write):
    d = date(2026, 6, 6)
    await nutrition_writes.log_meal(
        db_session, on_date=d, name="snack", calories=500, protein_g=20,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await db_session.commit()

    summary = await nutrition_analytics.daily_summary(
        db_session, d, subject_id=owner_write.subject_id
    )

    assert summary["on_track"]["calories"] is False   # 500 < 1300
    assert summary["on_track"]["protein"] is False    # 20 < 150


async def test_daily_summary_over_target(db_session, owner_write):
    d = date(2026, 6, 7)
    await nutrition_writes.log_meal(
        db_session, on_date=d, name="feast", calories=2000, protein_g=200,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await db_session.commit()

    summary = await nutrition_analytics.daily_summary(
        db_session, d, subject_id=owner_write.subject_id
    )

    assert summary["on_track"]["calories"] is False   # 2000 > 1700
    assert summary["on_track"]["protein"] is True     # 200 >= 150


async def test_nutrition_summary_period(db_session, owner_write):
    d1, d2 = date(2026, 6, 10), date(2026, 6, 11)
    await nutrition_writes.log_meal(
        db_session, on_date=d1, name="day1-meal", calories=1500, protein_g=100,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d1),
    )
    await nutrition_writes.log_meal(
        db_session, on_date=d2, name="day2-meal", calories=1400, protein_g=120,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d2),
    )
    await db_session.commit()

    summary = await nutrition_analytics.nutrition_summary(
        db_session, d1, d2, subject_id=owner_write.subject_id
    )

    assert summary["meal_count"] == 2
    assert summary["days_with_logs"] == 2
    assert summary["totals"]["calories"] == 2900
    assert summary["totals"]["protein_g"] == 220
    assert len(summary["per_day"]) == 2
    assert summary["per_day"][0]["date"] == "2026-06-10"
    assert summary["per_day"][0]["calories"] == 1500


async def test_goals_come_from_this_subjects_record(db_session, owner_write):
    """They came from ``.env``, so every patient was measured against one set."""

    from vitals.services import health_profile_service

    await health_profile_service.set_profile(
        db_session,
        subject_id=owner_write.subject_id,
        raw={
            "protein_target_g": "180",
            "calories_min": "1500",
            "calories_max": "2000",
        },
    )
    goals = await nutrition_analytics.get_goals(
        db_session, subject_id=owner_write.subject_id
    )

    assert goals["protein_target_g"] == 180.0
    assert goals["calories_min"] == 1500
    assert goals["calories_max"] == 2000


async def test_eaten_at_defaults_to_now(db_session, owner_write):
    m = await nutrition_writes.log_meal(
        db_session, on_date=date(2026, 6, 8), name="no time given",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 8)),
    )
    await db_session.commit()
    assert m.eaten_at is not None


async def test_list_meals_date_range(db_session, owner_write):
    await nutrition_writes.log_meal(
        db_session, on_date=date(2026, 6, 1),
        name="a",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 1)),
    )
    await nutrition_writes.log_meal(
        db_session, on_date=date(2026, 6, 5),
        name="b",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 5)),
    )
    await nutrition_writes.log_meal(
        db_session, on_date=date(2026, 6, 10),
        name="c",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 10)),
    )
    await db_session.commit()

    meals = await nutrition_queries.list_meals(
        db_session,
        start=date(2026, 6, 3),
        end=date(2026, 6, 7),
        subject_id=owner_write.subject_id,
    )
    assert len(meals) == 1
    assert meals[0].name == "b"
