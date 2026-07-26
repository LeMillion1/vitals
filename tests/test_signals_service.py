"""T1 — the signals capture domain.

The invariants worth guarding here are the ones that are expensive to discover
later: the raw text surviving a broken parse, one phrase producing several rows,
alias folding happening on read, and a misparse leaving the charts without
leaving the table.
"""
from datetime import date, time

import pytest
from sqlalchemy import select

from vitals.enums import Domain, SignalKind, Source
from vitals.models.raw_payload import RawPayload
from vitals.models.signals import DayContext, Signal
from vitals.services import signals_service as svc

D1 = date(2026, 7, 20)
D2 = date(2026, 7, 21)


def _parse_fixed(items):
    """A parser stub: ignores the text, returns exactly these items."""
    return lambda _text: items


# ── The three shapes ──────────────────────────────────────────────────────────
async def test_three_kinds_persist_with_their_own_fields(db_session):
    rows = await svc.create_signals(
        db_session,
        items=[
            {"kind": "state", "key": "sleepiness", "value_num": 4, "note": "спать хочу"},
            {"kind": "symptom", "key": "headache", "value_num": 3},
            {"kind": "exposure", "key": "caffeine_late", "value_num": 200,
             "unit": "mg", "at_time": "22:00"},
        ],
        on_date=D1,
    )
    await db_session.commit()

    assert len(rows) == 3
    by_kind = {r.kind: r for r in rows}
    assert set(by_kind) == {k.value for k in SignalKind}
    assert by_kind["state"].value_num == 4
    assert by_kind["state"].note == "спать хочу"
    assert by_kind["exposure"].unit == "mg"
    assert by_kind["exposure"].at_time == time(22, 0)
    # Every row carries the Insights triple.
    assert all(r.domain == Domain.SIGNALS.value and r.date == D1 for r in rows)


async def test_one_phrase_becomes_several_rows_sharing_a_batch(db_session):
    rows = await svc.ingest_text(
        db_session,
        text="Голова раскалывается, спал 4 часа, кофе в 22",
        parse=_parse_fixed([
            {"kind": "symptom", "key": "headache", "value_num": 4},
            {"kind": "state", "key": "sleepiness", "value_num": 5},
            {"kind": "exposure", "key": "caffeine_late", "at_time": "22:00"},
        ]),
        on_date=D1,
    )
    await db_session.commit()

    assert len(rows) == 3
    assert len({r.batch_id for r in rows}) == 1


async def test_bad_kind_or_missing_key_is_dropped_not_guessed(db_session):
    rows = await svc.create_signals(
        db_session,
        items=[
            {"kind": "mood", "key": "whatever"},   # not a SignalKind
            {"kind": "state", "key": "   "},        # no key
            {"kind": "state", "key": "Energy Level", "value_num": "nonsense"},
        ],
        on_date=D1,
    )
    await db_session.commit()

    assert len(rows) == 1
    assert rows[0].key == "energy_level"  # slugged on write
    assert rows[0].value_num is None      # unparseable number dropped, row kept


# ── D6: raw survives a broken parse ───────────────────────────────────────────
async def test_raw_is_stored_even_when_the_parser_blows_up(db_session):
    def _explode(_text):
        raise RuntimeError("model timed out")

    rows = await svc.ingest_text(
        db_session, text="спать пиздец хочу", parse=_explode, on_date=D1
    )
    await db_session.commit()

    assert rows == []
    raws = (await db_session.execute(
        select(RawPayload).where(RawPayload.domain == Domain.SIGNALS.value)
    )).scalars().all()
    assert len(raws) == 1
    assert raws[0].payload["text"] == "спать пиздец хочу"
    assert raws[0].source == Source.TELEGRAM.value


async def test_signals_link_back_to_their_raw_row(db_session):
    rows = await svc.ingest_text(
        db_session,
        text="голова болит",
        parse=_parse_fixed([{"kind": "symptom", "key": "headache", "value_num": 3}]),
        on_date=D1,
    )
    await db_session.commit()

    raw = (await db_session.execute(select(RawPayload))).scalars().one()
    assert rows[0].raw_id == raw.id


async def test_same_external_id_refreshes_one_raw_row(db_session):
    """A webhook retry must not pile up duplicate raw rows for one message."""
    for _ in range(2):
        await svc.store_raw_text(db_session, text="кофе в 22", external_id="tg:991")
    await db_session.commit()

    raws = (await db_session.execute(select(RawPayload))).scalars().all()
    assert len(raws) == 1


# ── D5 / шов 4: aliases fold on read ──────────────────────────────────────────
async def test_alias_folds_on_read_without_touching_stored_rows(db_session):
    await svc.create_signals(
        db_session,
        items=[{"kind": "state", "key": "sleepy"}, {"kind": "state", "key": "sleepiness"}],
        on_date=D1,
    )
    await db_session.commit()

    # Stored exactly as written — the drift stays visible for прогон 7.
    stored = {r.key for r in (await db_session.execute(select(Signal))).scalars().all()}
    assert stored == {"sleepy", "sleepiness"}

    # But both spellings answer to the canonical key, from either direction.
    assert len(await svc.list_signals(db_session, key="sleepiness")) == 2
    assert len(await svc.list_signals(db_session, key="sleepy")) == 2
    assert await svc.key_frequency(db_session) == [("sleepiness", 2)]


def test_normalize_key_slugs_and_maps():
    assert svc.normalize_key("  Head-Ache ") == "headache"
    assert svc.normalize_key("unknown key") == "unknown_key"  # untouched, just slugged


# ── misparse: out of the charts, still in the table ───────────────────────────
async def test_misparse_leaves_charts_but_not_the_table(db_session):
    rows = await svc.ingest_text(
        db_session,
        text="кофе в 22",
        parse=_parse_fixed([
            {"kind": "exposure", "key": "caffeine_late"},
            {"kind": "state", "key": "sleepiness", "value_num": 2},
        ]),
        on_date=D1,
    )
    await db_session.commit()
    batch = rows[0].batch_id

    assert await svc.mark_misparse(db_session, batch) == 2
    await db_session.commit()

    # Gone from the analysis reads…
    assert await svc.list_signals(db_session) == []
    assert await svc.key_frequency(db_session, include_misparse=False) == []
    # …still on disk, with the raw text, as прогон-7 material.
    assert len((await db_session.execute(select(Signal))).scalars().all()) == 2
    assert len(await svc.key_frequency(db_session)) == 2
    assert len(await svc.list_signals(db_session, include_misparse=True)) == 2


async def test_misparse_cancels_the_whole_batch_only(db_session):
    keep = await svc.ingest_text(
        db_session, text="a",
        parse=_parse_fixed([{"kind": "state", "key": "energy_level"}]), on_date=D1,
    )
    drop = await svc.ingest_text(
        db_session, text="b",
        parse=_parse_fixed([{"kind": "state", "key": "sleepiness"}]), on_date=D1,
    )
    await db_session.commit()

    await svc.mark_misparse(db_session, drop[0].batch_id)
    await db_session.commit()

    left = await svc.list_signals(db_session)
    assert [r.id for r in left] == [keep[0].id]


# ── list_signals filters ──────────────────────────────────────────────────────
async def test_list_signals_filters_by_kind_and_date(db_session):
    await svc.create_signals(
        db_session, items=[{"kind": "symptom", "key": "headache"}], on_date=D1
    )
    await svc.create_signals(
        db_session, items=[{"kind": "state", "key": "sleepiness"}], on_date=D2
    )
    await db_session.commit()

    assert len(await svc.list_signals(db_session, kind="symptom")) == 1
    assert len(await svc.list_signals(db_session, start=D2)) == 1
    assert len(await svc.list_signals(db_session, end=D1)) == 1
    # Newest first.
    assert [r.date for r in await svc.list_signals(db_session)] == [D2, D1]


# ── day_context ───────────────────────────────────────────────────────────────
async def test_day_context_is_idempotent_and_last_answer_wins(db_session):
    first = await svc.set_day_context(
        db_session, D1,
        answers={"remote": False, "gym": True},
        planned={"remote": True},
        source=Source.TEMPLATE.value,
    )
    await db_session.commit()

    second = await svc.set_day_context(
        db_session, D1, answers={"remote": True, "gym": False}
    )
    await db_session.commit()

    assert second.id == first.id
    assert len((await db_session.execute(select(DayContext))).scalars().all()) == 1
    assert second.answers == {"remote": True, "gym": False}
    assert second.source == Source.MANUAL.value
    # The template's guess is not erased by an answer that omits it — it is what
    # the template later learns from.
    assert second.planned == {"remote": True}


async def test_get_day_context_missing_day_is_none(db_session):
    assert await svc.get_day_context(db_session, D1) is None


@pytest.mark.integration
async def test_day_context_unique_per_date_is_enforced_by_the_db(db_session):
    """The UNIQUE(date) index, not just the service's read-then-write."""
    from sqlalchemy.exc import IntegrityError

    db_session.add(DayContext(date=D1, domain=Domain.SIGNALS.value, answers={}))
    db_session.add(DayContext(date=D1, domain=Domain.SIGNALS.value, answers={}))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
