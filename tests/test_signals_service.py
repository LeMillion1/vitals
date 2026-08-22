"""The signals capture domain.

The invariants worth guarding here are the ones that are expensive to discover
later: the raw text surviving a broken parse, one phrase producing several rows,
alias folding happening on read, and a misparse leaving the charts without
leaving the table.
"""
from datetime import date, time, timedelta

import pytest
from sqlalchemy import select

from vitals.enums import Domain, SignalKind, Source
from vitals.models.raw_payload import RawPayload
from vitals.models.signals import DayContext, Signal
from vitals.services import alerts_service, signals_service as svc

D1 = date(2026, 7, 20)
D2 = date(2026, 7, 21)


def _parse_fixed(items):
    """A parser stub: ignores the text, returns exactly these items."""
    return lambda _text: items


# ── The three shapes ──────────────────────────────────────────────────────────
async def test_three_kinds_persist_with_their_own_fields(db_session, signals_owner):
    rows = await svc.create_signals(
        db_session,
        items=[
            {"kind": "state", "key": "sleepiness", "value_num": 4, "note": "спать хочу"},
            {"kind": "symptom", "key": "headache", "value_num": 3},
            {"kind": "exposure", "key": "caffeine_late", "value_num": 200,
             "unit": "mg", "at_time": "22:00"},
        ],
        on_date=D1,
        identity=signals_owner.identity,
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


async def test_one_phrase_becomes_several_rows_sharing_a_batch(db_session, signals_owner):
    rows = await svc.ingest_text(
        db_session,
        text="Голова раскалывается, спал 4 часа, кофе в 22",
        parse=_parse_fixed([
            {"kind": "symptom", "key": "headache", "value_num": 4},
            {"kind": "state", "key": "sleepiness", "value_num": 5},
            {"kind": "exposure", "key": "caffeine_late", "at_time": "22:00"},
        ]),
        on_date=D1,
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    await db_session.commit()

    assert len(rows) == 3
    assert len({r.batch_id for r in rows}) == 1


async def test_bad_kind_or_missing_key_is_dropped_not_guessed(db_session, signals_owner):
    rows = await svc.create_signals(
        db_session,
        items=[
            {"kind": "mood", "key": "whatever"},   # not a SignalKind
            {"kind": "state", "key": "   "},        # no key
            {"kind": "state", "key": "Energy Level", "value_num": "nonsense"},
        ],
        on_date=D1,
        identity=signals_owner.identity,
    )
    await db_session.commit()

    assert len(rows) == 1
    assert rows[0].key == "energy_level"  # slugged on write
    assert rows[0].value_num is None      # unparseable number dropped, row kept


# ── Raw survives a broken parse ───────────────────────────────────────────────
async def test_raw_is_stored_even_when_the_parser_blows_up(db_session, signals_owner):
    def _explode(_text):
        raise RuntimeError("model timed out")

    rows = await svc.ingest_text(
        db_session, text="спать пиздец хочу", parse=_explode, on_date=D1,
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    await db_session.commit()

    assert rows == []
    raws = (await db_session.execute(
        select(RawPayload).where(RawPayload.domain == Domain.SIGNALS.value)
    )).scalars().all()
    assert len(raws) == 1
    assert raws[0].payload["text"] == "спать пиздец хочу"
    assert raws[0].source == Source.TELEGRAM.value


async def test_nonempty_junk_parser_output_stays_pending_and_reports_failure(
    db_session,
    signals_owner,
):
    outcome = svc.ParserOutcome()
    rows = await svc.ingest_text(
        db_session,
        text="голова болит",
        parse=_parse_fixed([{"kind": "symptm", "key": "headache"}]),
        external_id="tg:junk",
        on_date=D1,
        parser_outcome=outcome,
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    await db_session.commit()

    assert rows == []
    raw = await db_session.scalar(
        select(RawPayload).where(RawPayload.external_id == "tg:junk")
    )
    assert raw is not None and raw.processed_at is None
    assert (outcome.successes, outcome.failures) == (0, 1)
    assert await alerts_service.list_active(
        db_session, domain=Domain.SIGNALS.value
    ) == []


async def test_signals_link_back_to_their_raw_row(db_session, signals_owner):
    rows = await svc.ingest_text(
        db_session,
        text="голова болит",
        parse=_parse_fixed([{"kind": "symptom", "key": "headache", "value_num": 3}]),
        on_date=D1,
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    await db_session.commit()

    raw = (await db_session.execute(select(RawPayload))).scalars().one()
    assert rows[0].raw_id == raw.id


async def test_same_external_id_refreshes_one_raw_row(db_session, signals_owner):
    """A webhook retry must not pile up duplicate raw rows for one message."""
    for _ in range(2):
        await svc.store_raw_text(
            db_session,
            text="кофе в 22",
            external_id="tg:991",
            identity=signals_owner.identity,
            integration_connection_id=signals_owner.connection_id,
        )
    await db_session.commit()

    raws = (await db_session.execute(select(RawPayload))).scalars().all()
    assert len(raws) == 1


# ── Aliases fold on read ──────────────────────────────────────────────────────
async def test_alias_folds_on_read_without_touching_stored_rows(db_session, signals_owner):
    await svc.create_signals(
        db_session,
        items=[{"kind": "state", "key": "sleepy"}, {"kind": "state", "key": "sleepiness"}],
        on_date=D1,
        identity=signals_owner.identity,
    )
    await db_session.commit()

    # Stored exactly as written — the drift stays visible for consolidation.
    stored = {r.key for r in (await db_session.execute(select(Signal))).scalars().all()}
    assert stored == {"sleepy", "sleepiness"}

    # But both spellings answer to the canonical key, from either direction.
    assert len(await svc.list_signals(db_session, key="sleepiness", subject_id=signals_owner.subject_id)) == 2
    assert len(await svc.list_signals(db_session, key="sleepy", subject_id=signals_owner.subject_id)) == 2

    stats = await svc.key_frequency(db_session, subject_id=signals_owner.subject_id)
    assert [(s.key, s.count) for s in stats] == [("sleepiness", 2)]
    # …and the drifted spelling is named on the screen, not hidden by the folding.
    assert stats[0].variants == ("sleepy",)


def test_normalize_key_slugs_and_maps():
    assert svc.normalize_key("  Head-Ache ") == "headache"
    assert svc.normalize_key("unknown key") == "unknown_key"  # untouched, just slugged


def test_how_the_day_went_folds_onto_one_key_each():
    """«Весь день за компом» is the most natural answer to the evening question and
    the one with the most ways to spell it. The anchors the parser is pointed at
    have to survive the model reaching for a synonym instead."""
    for alias in ("computer_day", "desk_day", "inactive", "low_activity"):
        assert svc.normalize_key(alias) == "sedentary"
    assert svc.normalize_key("on_the_move") == "on_feet"
    assert svc.normalize_key("overtime") == "long_work_day"
    assert svc.normalize_key("busy_day") == "workload_high"


# ── misparse: out of the charts, still in the table ───────────────────────────
async def test_misparse_leaves_charts_but_not_the_table(db_session, signals_owner):
    rows = await svc.ingest_text(
        db_session,
        text="кофе в 22",
        parse=_parse_fixed([
            {"kind": "exposure", "key": "caffeine_late"},
            {"kind": "state", "key": "sleepiness", "value_num": 2},
        ]),
        on_date=D1,
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    await db_session.commit()
    batch = rows[0].batch_id

    assert await svc.mark_misparse(
        db_session, batch, subject_id=signals_owner.subject_id
    ) == 2
    await db_session.commit()

    # Gone from the analysis reads…
    assert await svc.list_signals(db_session, subject_id=signals_owner.subject_id) == []
    assert await svc.key_frequency(
        db_session, include_misparse=False, subject_id=signals_owner.subject_id
    ) == []
    # …still on disk, with the raw text, as material for consolidation.
    assert len((await db_session.execute(select(Signal))).scalars().all()) == 2
    assert len(await svc.key_frequency(db_session, subject_id=signals_owner.subject_id)) == 2
    assert len(await svc.list_signals(db_session, include_misparse=True, subject_id=signals_owner.subject_id)) == 2


async def test_misparse_cancels_the_whole_batch_only(db_session, signals_owner):
    keep = await svc.ingest_text(
        db_session, text="a",
        parse=_parse_fixed([{"kind": "state", "key": "energy_level"}]), on_date=D1,
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    drop = await svc.ingest_text(
        db_session, text="b",
        parse=_parse_fixed([{"kind": "state", "key": "sleepiness"}]), on_date=D1,
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    await db_session.commit()

    await svc.mark_misparse(
        db_session, drop[0].batch_id, subject_id=signals_owner.subject_id
    )
    await db_session.commit()

    left = await svc.list_signals(db_session, subject_id=signals_owner.subject_id)
    assert [r.id for r in left] == [keep[0].id]


# ── list_signals filters ──────────────────────────────────────────────────────
async def test_list_signals_filters_by_kind_and_date(db_session, signals_owner):
    await svc.create_signals(
        db_session, items=[{"kind": "symptom", "key": "headache"}], on_date=D1,
        identity=signals_owner.identity,
    )
    await svc.create_signals(
        db_session, items=[{"kind": "state", "key": "sleepiness"}], on_date=D2,
        identity=signals_owner.identity,
    )
    await db_session.commit()

    assert len(await svc.list_signals(db_session, kind="symptom", subject_id=signals_owner.subject_id)) == 1
    assert len(await svc.list_signals(db_session, start=D2, subject_id=signals_owner.subject_id)) == 1
    assert len(await svc.list_signals(db_session, end=D1, subject_id=signals_owner.subject_id)) == 1
    # Newest first.
    assert [r.date for r in await svc.list_signals(db_session, subject_id=signals_owner.subject_id)] == [D2, D1]


# ── R1: the revision screen's raw material ────────────────────────────────────
async def test_key_stats_carry_the_phrasings_they_came_from(db_session, signals_owner):
    """Counting keys can't answer "is this the same thing?" — the wording can."""
    await svc.create_signals(
        db_session,
        items=[
            {"kind": "state", "key": "sleepiness", "note": "спать пиздец хочу"},
            {"kind": "state", "key": "sleepy", "note": "вырубает"},
            {"kind": "state", "key": "sleepiness", "note": "вырубает"},  # a repeat
            {"kind": "symptom", "key": "headache", "note": "голова раскалывается"},
        ],
        on_date=D1,
        identity=signals_owner.identity,
    )
    await db_session.commit()

    stats = {s.key: s for s in await svc.key_frequency(db_session, subject_id=signals_owner.subject_id)}
    # Most-used first, so the consolidation candidates surface on their own.
    assert [s.key for s in await svc.key_frequency(db_session, subject_id=signals_owner.subject_id)] == ["sleepiness", "headache"]
    assert set(stats["sleepiness"].examples) == {"спать пиздец хочу", "вырубает"}
    assert stats["headache"].examples == ("голова раскалывается",)
    assert stats["headache"].variants == ()


async def test_key_stats_cap_the_examples_per_key(db_session, signals_owner):
    await svc.create_signals(
        db_session,
        items=[
            {"kind": "state", "key": "sleepiness", "note": f"фраза {n}"}
            for n in range(svc.EXAMPLES_PER_KEY + 3)
        ],
        on_date=D1,
        identity=signals_owner.identity,
    )
    await db_session.commit()

    stat = (await svc.key_frequency(db_session, subject_id=signals_owner.subject_id))[0]
    assert stat.count == svc.EXAMPLES_PER_KEY + 3      # every row counted…
    assert len(stat.examples) == svc.EXAMPLES_PER_KEY  # …a readable few shown


# ── R3: a second pass at what the model choked on ─────────────────────────────
def _explode(_text):
    raise RuntimeError("model timed out")


async def _unparsed_message(
    db_session, signals_owner, text="спать хочу", external_id="tg:1"
):
    """Ingest a message with a broken parser → raw stored, no rows, still pending."""
    assert await svc.ingest_text(
        db_session, text=text, parse=_explode, external_id=external_id, on_date=D1,
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    ) == []
    await db_session.commit()
    return (await db_session.execute(select(RawPayload))).scalars().one()


async def test_a_parsed_message_is_marked_done_a_failed_one_stays_pending(db_session, signals_owner):
    raw = await _unparsed_message(db_session, signals_owner)
    assert raw.processed_at is None

    await svc.ingest_text(
        db_session,
        text="голова болит",
        parse=_parse_fixed([{"kind": "symptom", "key": "headache"}]),
        external_id="tg:2",
        on_date=D1,
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    await db_session.commit()

    done = (await db_session.execute(
        select(RawPayload).where(RawPayload.external_id == "tg:2")
    )).scalars().one()
    assert done.processed_at is not None


# ── Parser outcomes are returned to the production boundary.  This reusable
# service never manufactures an unscoped/global alert. ─────────────────────────
async def test_parser_failure_and_success_are_reported_without_global_alerts(
    db_session,
    signals_owner,
):
    outcome = svc.ParserOutcome()
    await svc.ingest_text(
        db_session,
        text="спать хочу",
        parse=_explode,
        on_date=D1,
        parser_outcome=outcome,
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    await db_session.commit()

    await svc.ingest_text(
        db_session,
        text="голова болит",
        parse=_parse_fixed([{"kind": "symptom", "key": "headache"}]),
        on_date=D1,
        parser_outcome=outcome,
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    await db_session.commit()
    assert (outcome.successes, outcome.failures) == (1, 1)
    assert await alerts_service.list_active(
        db_session, domain=Domain.SIGNALS.value
    ) == []


async def test_reparse_success_and_explicit_empty_report_success(db_session, signals_owner):
    await _unparsed_message(db_session, signals_owner)
    await svc.store_raw_text(
        db_session,
        text="не факт",
        external_id="tg:empty",
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    outcome = svc.ParserOutcome()

    def _parse(text):
        if text == "не факт":
            return []
        return [{"kind": "state", "key": "sleepiness"}]

    await svc.reparse_unparsed(
        db_session,
        parse=_parse,
        parser_outcome=outcome,
        subject_id=signals_owner.subject_id,
    )
    await db_session.commit()
    assert (outcome.successes, outcome.failures) == (2, 0)
    assert await alerts_service.list_active(
        db_session, domain=Domain.SIGNALS.value
    ) == []


async def test_reparse_recovers_a_message_the_model_choked_on(db_session, signals_owner):
    raw = await _unparsed_message(db_session, signals_owner)

    rows = await svc.reparse_unparsed(
        db_session,
        parse=_parse_fixed([{"kind": "state", "key": "sleepiness", "value_num": 4}]),
        subject_id=signals_owner.subject_id,
    )
    await db_session.commit()

    assert [r.key for r in rows] == ["sleepiness"]
    assert rows[0].raw_id == raw.id
    # Dated when it was said, not when the sweep happened to run.
    assert rows[0].date == raw.fetched_at.date()
    await db_session.refresh(raw)
    assert raw.processed_at is not None


async def test_reparse_is_safe_to_run_twice(db_session, signals_owner):
    await _unparsed_message(db_session, signals_owner)
    parse = _parse_fixed([{"kind": "state", "key": "sleepiness"}])

    assert len(await svc.reparse_unparsed(
        db_session, parse=parse, subject_id=signals_owner.subject_id
    )) == 1
    await db_session.commit()
    assert await svc.reparse_unparsed(
        db_session, parse=parse, subject_id=signals_owner.subject_id
    ) == []
    await db_session.commit()

    assert len((await db_session.execute(select(Signal))).scalars().all()) == 1


async def test_reparse_leaves_the_row_pending_when_the_model_is_still_down(db_session, signals_owner):
    """A second outage must not burn the message's last chance."""
    raw = await _unparsed_message(db_session, signals_owner)

    assert await svc.reparse_unparsed(
        db_session, parse=_explode, subject_id=signals_owner.subject_id
    ) == []
    await db_session.commit()

    await db_session.refresh(raw)
    assert raw.processed_at is None


async def test_reparse_batch_any_failure_wins_over_success(db_session, signals_owner):
    raw = await svc.store_raw_text(
        db_session,
        text="голова болит",
        external_id="tg:reparse-junk",
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    await svc.store_raw_text(
        db_session,
        text="устал",
        external_id="tg:reparse-good",
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    outcome = svc.ParserOutcome()

    def _mixed(text):
        if text == "голова болит":
            return [{"kind": "symptm", "key": "headache"}]
        return [{"kind": "state", "key": "fatigue"}]

    rows = await svc.reparse_unparsed(
        db_session,
        parse=_mixed,
        parser_outcome=outcome,
        subject_id=signals_owner.subject_id,
    )
    await db_session.commit()

    assert [row.key for row in rows] == ["fatigue"]
    assert (outcome.successes, outcome.failures) == (1, 1)
    await db_session.refresh(raw)
    assert raw.processed_at is None
    assert await alerts_service.list_active(
        db_session, domain=Domain.SIGNALS.value
    ) == []


async def test_reparse_ignores_taps_and_anything_older_than_the_window(db_session, signals_owner):
    from vitals.services.raw_payload_service import upsert_raw_payload
    from vitals.utils.timeutils import now_local

    # A button tap: stored like everything else, but nobody said it.
    await upsert_raw_payload(
        db_session, domain=svc.DOMAIN, source=Source.TELEGRAM.value,
        external_id="tg:tap", payload={"data": "mis:abc"},
    )
    stale = await svc.store_raw_text(
        db_session,
        text="кофе в 22",
        external_id="tg:old",
        identity=signals_owner.identity,
        integration_connection_id=signals_owner.connection_id,
    )
    stale.fetched_at = now_local() - timedelta(days=svc.REPARSE_WINDOW_DAYS + 3)
    await db_session.commit()

    assert await svc.reparse_unparsed(
        db_session, parse=_parse_fixed([{"kind": "state", "key": "sleepiness"}]),
        subject_id=signals_owner.subject_id,
    ) == []


# ── day_context ───────────────────────────────────────────────────────────────
async def test_day_context_is_idempotent_and_last_answer_wins(db_session, signals_owner):
    first = await svc.set_day_context(
        db_session, D1,
        answers={"remote": False, "gym": True},
        planned={"remote": True},
        source=Source.TEMPLATE.value,
        identity=signals_owner.identity,
    )
    await db_session.commit()

    second = await svc.set_day_context(
        db_session, D1, answers={"remote": True, "gym": False},
        identity=signals_owner.identity,
    )
    await db_session.commit()

    assert second.id == first.id
    assert len((await db_session.execute(select(DayContext))).scalars().all()) == 1
    assert second.answers == {"remote": True, "gym": False}
    assert second.source == Source.MANUAL.value
    # The template's guess is not erased by an answer that omits it — it is what
    # the template later learns from.
    assert second.planned == {"remote": True}


async def test_get_day_context_missing_day_is_none(db_session, signals_owner):
    assert await svc.get_day_context(
        db_session, D1, subject_id=signals_owner.subject_id
    ) is None


@pytest.mark.integration
async def test_day_context_unique_per_subject_date_is_enforced_by_the_db(
    db_session, legacy_owner_roots
):
    """The UNIQUE(subject_id, date) index, not just the read-then-write.

    A day belongs to one person, so the key is scoped to the subject; two people
    answering the same date is not a collision.
    """
    from sqlalchemy.exc import IntegrityError

    for _ in range(2):
        db_session.add(
            DayContext(
                subject_id=legacy_owner_roots.subject_id,
                date=D1,
                domain=Domain.SIGNALS.value,
                answers={},
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
