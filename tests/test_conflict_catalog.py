"""Ingredient-normalization unit tests (pure logic, no DB).

normalize_ingredient() is the fix for the Cyrillic-matching bug: a Russian
supplement name used to fall through supplements_service.slugify's ascii-only
regex and collapse to the useless "supplement" slug, silently disabling any
conflict rule written against a real key like "iron".
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from vitals.enums import UserStatus
from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject, User
from vitals.services import conflict_catalog
from vitals.services.supplements_service import slugify
from vitals.utils.identifiers import slugify as shared_slugify


# ── Dictionary hits (RU/EN aliases -> canonical key) ────────────────────────

def test_cyrillic_dictionary_hit():
    assert conflict_catalog.normalize_ingredient("Железо") == "iron"


def test_english_canonical_key_itself():
    assert conflict_catalog.normalize_ingredient("Iron") == "iron"


def test_multiword_alias():
    assert conflict_catalog.normalize_ingredient("Iron Bisglycinate") == "iron"


def test_vitamin_d_variants():
    assert conflict_catalog.normalize_ingredient("Витамин Д3") == "vitamin_d"
    assert conflict_catalog.normalize_ingredient("vitamin d3") == "vitamin_d"
    assert conflict_catalog.normalize_ingredient("Холекальциферол") == "vitamin_d"


def test_ascorbic_acid_is_vitamin_c():
    assert conflict_catalog.normalize_ingredient("Аскорбиновая кислота") == "vitamin_c"


def test_short_word_boundary_alias():
    # "Fe" is a real shorthand for iron, but must not match as a substring of
    # an unrelated word (e.g. inside "coffee").
    assert conflict_catalog.normalize_ingredient("Fe complex 30mg") == "iron"
    assert conflict_catalog.normalize_ingredient("Coffee break") != "iron"


def test_apostrophe_handling():
    assert conflict_catalog.normalize_ingredient("St. John's Wort") == "st_johns_wort"


def test_case_and_whitespace_insensitive():
    assert conflict_catalog.normalize_ingredient("  ЦИНК  ") == "zinc"


# ── Fallback: unknown ingredient -> transliterating slug (never crashes,
#    never collapses to a useless constant like the old ascii-only slugify) ──

def test_unknown_cyrillic_name_falls_back_to_transliteration():
    name = "Мнимая Добавка Экспериментальная"
    key = conflict_catalog.normalize_ingredient(name)
    assert key == slugify(name)
    assert key not in ("supplement", "")


def test_unknown_name_is_stable_and_deterministic():
    a = conflict_catalog.normalize_ingredient("Совершенно новая добавка")
    b = conflict_catalog.normalize_ingredient("Совершенно новая добавка")
    assert a == b


def test_empty_name_does_not_crash():
    assert conflict_catalog.normalize_ingredient("") == "supplement"


# ── slugify transliteration (the underlying fallback primitive) ────────────

def test_slugify_transliterates_cyrillic():
    assert slugify("Железо") == "zhelezo"
    assert slugify("Креатин") == "kreatin"


def test_slugify_ascii_unchanged():
    assert slugify("Iron (ferrous bisglycinate)") == "iron_ferrous_bisglycinate"
    assert slugify("  Vitamin D3 ") == "vitamin_d3"


def test_catalog_and_service_share_dependency_neutral_slugify():
    assert conflict_catalog.slugify is shared_slugify
    assert slugify is shared_slugify


# ── Curated rule catalog: load/validate + idempotent sync (Phase 3.3) ───────

def test_catalog_loads_and_validates():
    catalog = conflict_catalog.load_rule_catalog()
    assert len(catalog) >= 40
    codes = [entry["code"] for entry in catalog]
    assert len(codes) == len(set(codes))  # no duplicates slipped through


def test_glp1_low_intake_rules_are_day_end_only():
    """Both rules compare a same-day running total against a lower-bound
    threshold (calories < 800, protein < 60) — trivially true early in the
    day. If this flag is ever dropped, they'll false-positive mid-day again
    (see conflict_engine.evaluate's include_day_end)."""
    by_code = {e["code"]: e for e in conflict_catalog.load_rule_catalog()}
    for code in ("glp1_low_protein_lbm_risk", "glp1_very_low_calorie_extreme"):
        assert (by_code[code].get("params") or {}).get("day_end_only") is True, code


async def test_sync_catalog_inserts_everything_once(db_session):
    from vitals.models.conflict_rule import ConflictRule
    from sqlalchemy import select

    stats = await conflict_catalog.sync_catalog(db_session)
    await db_session.commit()
    expected = len(conflict_catalog.load_rule_catalog())
    assert stats == {"inserted": expected, "updated": 0, "total": expected}

    rows = (await db_session.execute(select(ConflictRule))).scalars().all()
    assert len(rows) == expected


async def test_sync_catalog_is_idempotent(db_session):
    from vitals.models.conflict_rule import ConflictRule
    from sqlalchemy import select

    await conflict_catalog.sync_catalog(db_session)
    await db_session.commit()
    expected = len(conflict_catalog.load_rule_catalog())

    stats = await conflict_catalog.sync_catalog(db_session)
    await db_session.commit()
    assert stats == {"inserted": 0, "updated": expected, "total": expected}

    rows = (await db_session.execute(select(ConflictRule))).scalars().all()
    assert len(rows) == expected  # no duplicate rows on the second run


async def test_sync_catalog_preserves_user_active_toggle(db_session):
    from vitals.models.conflict_rule import ConflictRule
    from sqlalchemy import select

    await conflict_catalog.sync_catalog(db_session)
    await db_session.commit()

    one_code = conflict_catalog.load_rule_catalog()[0]["code"]
    result = await db_session.execute(select(ConflictRule).where(ConflictRule.code == one_code))
    row = result.scalar_one()
    row.active = False
    await db_session.commit()

    await conflict_catalog.sync_catalog(db_session)
    await db_session.commit()

    result = await db_session.execute(select(ConflictRule).where(ConflictRule.code == one_code))
    assert result.scalar_one().active is False


async def test_sync_catalog_refreshes_changed_fields(db_session):
    """A stale row (e.g. from an older catalog version) gets its message/severity
    refreshed on the next sync — only `active` is left alone."""
    from vitals.models.conflict_rule import ConflictRule

    real_entry = conflict_catalog.load_rule_catalog()[0]
    stale = ConflictRule(
        code=real_entry["code"],
        rule_type=real_entry["rule_type"],
        domain_a=real_entry["domain_a"], condition_a=real_entry["condition_a"],
        domain_b=real_entry["domain_b"], condition_b=real_entry["condition_b"],
        severity=real_entry["severity"],
        message="устаревший текст, который должен быть заменён",
        active=False,  # simulate a user having disabled this rule
    )
    db_session.add(stale)
    await db_session.commit()

    await conflict_catalog.sync_catalog(db_session)
    await db_session.commit()
    await db_session.refresh(stale)

    assert stale.message == real_entry["message"]
    assert stale.active is False  # untouched despite the refresh


async def test_sync_catalog_cannot_see_a_subject_owned_catalog_code(db_session):
    entry = conflict_catalog.load_rule_catalog()[0]
    owner = User(
        username="catalog-collision-owner",
        normalized_username="catalog-collision-owner",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(owner)
    await db_session.flush()
    subject = HealthSubject(owner_user_id=owner.id, timezone="Asia/Almaty")
    db_session.add(subject)
    await db_session.flush()

    protected = ConflictRule(
        subject_id=subject.id,
        code=entry["code"],
        rule_type=entry["rule_type"],
        domain_a=entry["domain_a"],
        condition_a=entry["condition_a"],
        domain_b=entry["domain_b"],
        condition_b=entry["condition_b"],
        severity=entry["severity"],
        message="synthetic protected custom rule",
    )
    db_session.add(protected)
    await db_session.flush()

    # The curated catalog owns only the platform half of the code, so it seeds
    # its own definition beside the subject's rule instead of overwriting it.
    result = await conflict_catalog.sync_catalog(db_session)
    assert result["inserted"] > 0
    assert protected.message == "synthetic protected custom rule"
    assert protected.subject_id == subject.id
    curated = await db_session.scalar(
        select(ConflictRule).where(
            ConflictRule.code == entry["code"],
            ConflictRule.subject_id.is_(None),
        )
    )
    assert curated is not None and curated.id != protected.id


@pytest.mark.integration
async def test_postgres_catalog_sync_serializes_subject_recategorization(
    db_session,
    monkeypatch,
):
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row-lock semantics")
    owner = User(
        username="catalog-race-owner",
        normalized_username="catalog-race-owner",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(owner)
    await db_session.flush()
    subject = HealthSubject(owner_user_id=owner.id, timezone="Asia/Almaty")
    db_session.add(subject)
    await db_session.flush()
    subject_id = subject.id
    await conflict_catalog.sync_catalog(db_session)
    await db_session.commit()

    code = conflict_catalog.load_rule_catalog()[0]["code"]
    rows_locked = asyncio.Event()
    release_sync = asyncio.Event()
    writer_started = asyncio.Event()
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async def pause_after_locks():
        rows_locked.set()
        await release_sync.wait()

    monkeypatch.setattr(
        conflict_catalog,
        "_after_catalog_rows_locked_for_test",
        pause_after_locks,
    )

    async def synchronize():
        async with factory() as session:
            await conflict_catalog.sync_catalog(session)
            await session.commit()

    async def recategorize():
        async with factory() as session:
            writer_started.set()
            await session.execute(
                update(ConflictRule)
                .where(ConflictRule.code == code)
                .values(
                    subject_id=subject_id,
                    message="Protected concurrent custom definition",
                )
            )
            await session.commit()

    sync_task = asyncio.create_task(synchronize())
    await asyncio.wait_for(rows_locked.wait(), timeout=5)
    writer_task = asyncio.create_task(recategorize())
    await asyncio.wait_for(writer_started.wait(), timeout=5)
    done, _pending = await asyncio.wait({writer_task}, timeout=0.1)
    assert not done, "the concurrent writer must wait for catalog row locks"
    release_sync.set()
    await asyncio.wait_for(sync_task, timeout=5)
    await asyncio.wait_for(writer_task, timeout=5)

    async with factory() as session:
        row = await session.scalar(select(ConflictRule).where(ConflictRule.code == code))
        assert row is not None
        assert row.subject_id == subject_id
        assert row.message == "Protected concurrent custom definition"
