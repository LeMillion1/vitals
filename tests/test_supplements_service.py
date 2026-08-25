"""Supplements catalog tests — CRUD, slug, resolver, and the genetics→iron
cross-domain block (override flow)."""
from __future__ import annotations

import pytest

from vitals.models.conflict_rule import ConflictRule
from vitals.services import (
    alerts_service,
    conflict_engine,
    conflict_registrations,
    genetics_service,
    supplements_service,
)
from vitals.services.conflict_engine import ConflictBlocked
from vitals.utils.identifiers import slugify
from vitals.utils.timeutils import today_local

# No module-level asyncio mark: async DB tests are auto-detected (asyncio_mode=
# auto); test_slugify below is a pure sync test.


def test_slugify():
    assert supplements_service.slugify("Iron (ferrous bisglycinate)") == "iron_ferrous_bisglycinate"
    assert supplements_service.slugify("  Vitamin D3 ") == "vitamin_d3"


def test_slugify_service_facade_reexports_shared_helper():
    assert supplements_service.slugify is slugify


def test_slugify_transliterates_cyrillic_name():
    """Кириллица must not collapse to the useless "supplement" fallback."""
    assert supplements_service.slugify("Креатин") == "kreatin"


def test_parse_slot_am_pm_meal_day():
    assert supplements_service._parse_slot("утро") == "AM"
    assert supplements_service._parse_slot("Morning") == "AM"
    assert supplements_service._parse_slot("вечер") == "PM"
    assert supplements_service._parse_slot("ночь") == "PM"
    assert supplements_service._parse_slot("Night") == "PM"
    assert supplements_service._parse_slot("с едой") == "MEAL"
    assert supplements_service._parse_slot("день") == "DAY"


def test_parse_slot_unknown_or_blank_is_none():
    assert supplements_service._parse_slot(None) is None
    assert supplements_service._parse_slot("") is None
    assert supplements_service._parse_slot("перед тренировкой") is None


def test_timing_bucket_ru_and_en():
    """The /supplements page's 4 display rows must accept English timing text
    too — an English-named supplement's "Morning"/"Evening" used to fall into
    the "Other" bucket because the template compared against raw RU strings."""
    assert supplements_service.timing_bucket("утро") == "утро"
    assert supplements_service.timing_bucket("Morning") == "утро"
    assert supplements_service.timing_bucket("день") == "день"
    assert supplements_service.timing_bucket("Afternoon") == "день"
    assert supplements_service.timing_bucket("вечер") == "вечер"
    assert supplements_service.timing_bucket("Evening") == "вечер"
    assert supplements_service.timing_bucket("ночь") == "ночь"
    assert supplements_service.timing_bucket("Night") == "ночь"


def test_timing_bucket_unknown_or_blank_is_none():
    assert supplements_service.timing_bucket(None) is None
    assert supplements_service.timing_bucket("") is None
    assert supplements_service.timing_bucket("before workout") is None


async def test_add_list_toggle_delete(db_session, owner_write):
    s = await supplements_service.add_supplement(
        db_session, name="Креатин", dose="5 г", evidence="A",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write()
    )
    await db_session.commit()
    assert s.key == "креатин" or s.key  # slug derived
    assert s.active is True

    await supplements_service.set_active(db_session, s.id, False, identity=owner_write.identity, prepared_conflict_write=await owner_write.write())
    await db_session.commit()
    await db_session.refresh(s)
    assert s.active is False

    active_only = await supplements_service.list_supplements(
        db_session, subject_id=owner_write.subject_id, active_only=True
    )
    assert s.id not in [x.id for x in active_only]

    assert (
        await supplements_service.delete_supplement(
            db_session, s.id, identity=owner_write.identity
        )
        is True
    )
    await db_session.commit()
    assert (
        len(
            await supplements_service.list_supplements(
                db_session, subject_id=owner_write.subject_id
            )
        )
        == 0
    )


async def test_resolver_shape(db_session, owner_write):
    await supplements_service.add_supplement(db_session, name="Iron", key="iron", active=True, identity=owner_write.identity, prepared_conflict_write=await owner_write.write())
    await db_session.commit()
    items = await supplements_service.resolve_active_scoped(
        db_session,
        scope=conflict_engine.ConflictScope(
            subject_id=owner_write.subject_id,
            evaluation_date=today_local(),
        ),
    )
    assert [
        {k: v for k, v in item.items() if k != conflict_engine.CONFLICT_ENTITY_KEY}
        for item in items
    ] == [{"key": "iron", "active": True, "name": "Iron", "timing_slot": None}]


async def _seed_iron_rule(db_session):
    db_session.add(
        ConflictRule(
            rule_type="hard_block",
            domain_a="genetics",
            condition_a={"marker": "hemochromatosis_carrier"},
            domain_b="supplements",
            condition_b={"key": "iron", "active": True},
            severity="block",
            message="Носительство гемохроматоза — препараты железа противопоказаны.",
            active=True,
        )
    )
    await db_session.commit()


async def test_iron_blocked_for_hemochromatosis_carrier(db_session, owner_write):
    conflict_registrations.register_all_resolvers()
    await _seed_iron_rule(db_session)
    await genetics_service.add_variant(
        db_session, gene="HFE", rsid="rs1800562", marker="hemochromatosis_carrier",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await db_session.commit()

    with pytest.raises(ConflictBlocked):
        await supplements_service.add_supplement(
            db_session, name="Iron", key="iron", active=True,
            identity=owner_write.identity,
            prepared_conflict_write=await owner_write.write()
        )
    await db_session.rollback()


async def test_cyrillic_name_no_explicit_key_still_blocked(db_session, owner_write):
    """The bug this plan set out to fix: adding "Железо" (no explicit key) used
    to slugify to the useless "supplement" fallback, silently never matching
    the iron rule. It must now resolve to "iron" via the dictionary and block."""
    conflict_registrations.register_all_resolvers()
    await _seed_iron_rule(db_session)
    await genetics_service.add_variant(
        db_session, gene="HFE", rsid="rs1800562", marker="hemochromatosis_carrier",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await db_session.commit()

    with pytest.raises(ConflictBlocked):
        await supplements_service.add_supplement(db_session, name="Железо", active=True, identity=owner_write.identity, prepared_conflict_write=await owner_write.write())
    await db_session.rollback()


async def test_iron_override_saves_and_stamps_alert(db_session, owner_write):
    conflict_registrations.register_all_resolvers()
    await _seed_iron_rule(db_session)
    await genetics_service.add_variant(
        db_session, gene="HFE", rsid="rs1800562", marker="hemochromatosis_carrier",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await db_session.commit()

    s = await supplements_service.add_supplement(
        db_session, name="Iron", key="iron", active=True, override=True,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write()
    )
    await db_session.commit()
    assert s.id is not None

    active = await alerts_service.list_active(db_session, domain="supplements", subject_id=owner_write.subject_id)
    assert len(active) == 1
    assert active[0].override_at is not None


async def test_inactive_iron_not_blocked(db_session, owner_write):
    """An archived (inactive) iron row must not trip the active-only condition."""
    conflict_registrations.register_all_resolvers()
    await _seed_iron_rule(db_session)
    await genetics_service.add_variant(
        db_session, gene="HFE", rsid="rs1800562", marker="hemochromatosis_carrier",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await db_session.commit()

    s = await supplements_service.add_supplement(
        db_session, name="Iron", key="iron", active=False,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write()
    )
    await db_session.commit()
    assert s.active is False
