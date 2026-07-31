"""``alerts_service`` — the alert lifecycle six other modules depend on.

Six services (weight, glp1, labs, body_scan, hrt_reminders, scheduler) call these
functions, so the contracts tested here — raising is idempotent while active,
resolving frees the slot, the two "was it dismissed" questions mean different
things — are load-bearing for every domain's badge.

The ``@pytest.mark.integration`` cases pin what SQLite only *fakes*: the
partial-unique index that makes ``raise_alert`` idempotent even against a raw
INSERT, and ``func.date()`` over a timestamp column.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from vitals.enums import Domain, Severity
from vitals.models.system_alert import SystemAlert
from vitals.services import alerts_service
from vitals.utils.timeutils import now_local, today_local

KEY = "weight.noisy_period_active"


async def _raise(session, *, key: str = KEY, entity: str = "", severity: str = Severity.INFO.value,
                 message: str = "шумный период", domain: str = Domain.WEIGHT.value,
                 overridden: bool = False):
    return await alerts_service.raise_alert(
        session, domain=domain, severity=severity, message=message,
        alert_key=key, entity_ref=entity, overridden=overridden,
    )


async def _all_rows(session) -> list[SystemAlert]:
    return list((await session.execute(select(SystemAlert))).scalars().all())


# ── raise_alert is idempotent while the alert stays active ────────────────────


async def test_raise_twice_refreshes_the_same_row(db_session):
    first = await _raise(db_session, severity=Severity.INFO.value, message="старое")
    second = await _raise(db_session, severity=Severity.WARN.value, message="новое")
    await db_session.commit()

    assert second.id == first.id
    assert len(await _all_rows(db_session)) == 1
    # The refresh updates the payload rather than piling up a duplicate.
    assert second.severity == Severity.WARN.value
    assert second.message == "новое"


async def test_different_entity_ref_is_a_different_alert(db_session):
    a = await _raise(db_session, entity="glucose:1")
    b = await _raise(db_session, entity="glucose:2")
    await db_session.commit()

    assert a.id != b.id
    assert len(await alerts_service.list_active(db_session)) == 2


async def test_raise_overridden_stamps_override_once(db_session):
    a = await _raise(db_session, overridden=True)
    stamped = a.override_at
    assert stamped is not None
    # Re-raising must not move the original override timestamp.
    again = await _raise(db_session, overridden=True)
    assert again.override_at == stamped


# ── resolve frees the dedupe slot ─────────────────────────────────────────────


async def test_resolve_by_key_frees_the_slot(db_session):
    first = await _raise(db_session)
    resolved = await alerts_service.resolve_by_key(db_session, alert_key=KEY)
    assert resolved is not None and resolved.id == first.id and resolved.resolved_at is not None

    # The condition comes back → a NEW active row, the old one stays as history.
    second = await _raise(db_session)
    await db_session.commit()
    assert second.id != first.id
    assert len(await _all_rows(db_session)) == 2
    assert [a.id for a in await alerts_service.list_active(db_session)] == [second.id]


async def test_resolve_by_key_is_a_noop_when_nothing_active(db_session):
    assert await alerts_service.resolve_by_key(db_session, alert_key=KEY) is None


async def test_resolve_alert_touches_only_its_own_row(db_session):
    """Alert identity is (alert_key, entity_ref) — two rows sharing message text
    are distinct alerts and must not be collapsed."""
    a = await _raise(db_session, entity="glucose:1", message="одинаковый текст")
    b = await _raise(db_session, entity="glucose:2", message="одинаковый текст")
    await alerts_service.resolve_alert(db_session, a.id)
    await db_session.commit()

    assert [x.id for x in await alerts_service.list_active(db_session)] == [b.id]


# ── resolve_superseded: prefix logic ──────────────────────────────────────────


async def test_resolve_superseded_keeps_current_row_only(db_session):
    """Singleton case (marker=None): everything for the key except keep_entity."""
    old = await _raise(db_session, entity="scan:1")
    current = await _raise(db_session, entity="scan:2")
    await alerts_service.resolve_superseded(db_session, alert_key=KEY, keep_entity="scan:2")
    await db_session.commit()

    assert (await db_session.get(SystemAlert, old.id)).resolved_at is not None
    assert (await db_session.get(SystemAlert, current.id)).resolved_at is None


async def test_resolve_superseded_scoped_to_one_marker(db_session):
    """With ``marker`` given, other markers sharing the alert_key are untouched —
    both the ``marker:id`` form and the bare legacy ``marker`` form."""
    stale = await _raise(db_session, entity="glucose:1")
    legacy = await _raise(db_session, entity="glucose")
    current = await _raise(db_session, entity="glucose:2")
    other_marker = await _raise(db_session, entity="ferritin:9")

    await alerts_service.resolve_superseded(
        db_session, alert_key=KEY, keep_entity="glucose:2", marker="glucose"
    )
    await db_session.commit()

    assert (await db_session.get(SystemAlert, stale.id)).resolved_at is not None
    assert (await db_session.get(SystemAlert, legacy.id)).resolved_at is not None
    assert (await db_session.get(SystemAlert, current.id)).resolved_at is None
    assert (await db_session.get(SystemAlert, other_marker.id)).resolved_at is None


async def test_resolve_superseded_with_no_keep_resolves_everything(db_session):
    a = await _raise(db_session, entity="scan:1")
    b = await _raise(db_session, entity="scan:2")
    await alerts_service.resolve_superseded(db_session, alert_key=KEY, keep_entity=None)
    await db_session.commit()

    assert (await db_session.get(SystemAlert, a.id)).resolved_at is not None
    assert (await db_session.get(SystemAlert, b.id)).resolved_at is not None


# ── the two "was it dismissed?" questions ─────────────────────────────────────


async def test_dismissed_today_vs_ever_dismissed(db_session):
    """``_was_dismissed_today`` is the daily-nag contract (weight noise, GLP-1
    plateau); ``_was_ever_dismissed`` is the forever contract (labs, body scans).
    A dismissal dated yesterday must separate the two."""
    alert = await _raise(db_session, entity="glucose:1")
    alert.resolved_at = now_local() - timedelta(days=1)
    await db_session.flush()

    assert await alerts_service._was_dismissed_today(db_session, KEY, "glucose:1") is False
    assert await alerts_service._was_ever_dismissed(db_session, KEY, "glucose:1") is True

    # Same alert dismissed today → both say yes.
    alert.resolved_at = now_local()
    await db_session.flush()
    assert await alerts_service._was_dismissed_today(db_session, KEY, "glucose:1") is True
    assert await alerts_service._was_ever_dismissed(db_session, KEY, "glucose:1") is True


async def test_dismissed_today_is_per_entity(db_session):
    a = await _raise(db_session, entity="glucose:1")
    await alerts_service.resolve_alert(db_session, a.id)
    await db_session.flush()

    assert await alerts_service._was_dismissed_today(db_session, KEY, "glucose:1") is True
    assert await alerts_service._was_dismissed_today(db_session, KEY, "glucose:2") is False


async def test_dismissed_today_accepts_an_explicit_date(db_session):
    a = await _raise(db_session)
    await alerts_service.resolve_alert(db_session, a.id)
    await db_session.flush()

    tomorrow = today_local() + timedelta(days=1)
    assert await alerts_service._was_dismissed_today(db_session, KEY, "", tomorrow) is False


# ── resolve_all / list_active ─────────────────────────────────────────────────


async def test_resolve_all_can_be_scoped_to_a_domain(db_session):
    w = await _raise(db_session, entity="w", domain=Domain.WEIGHT.value)
    l = await _raise(db_session, entity="l", domain=Domain.LABS.value)
    await alerts_service.resolve_all(db_session, domain=Domain.WEIGHT.value)
    await db_session.commit()

    assert (await db_session.get(SystemAlert, w.id)).resolved_at is not None
    assert (await db_session.get(SystemAlert, l.id)).resolved_at is None


def test_is_blocking_only_for_block():
    assert alerts_service.is_blocking(Severity.BLOCK.value) is True
    assert alerts_service.is_blocking(Severity.WARN.value) is False
    assert alerts_service.is_blocking(Severity.INFO.value) is False
    # ``note`` is an interpretation of the data — it must never stop a save.
    assert alerts_service.is_blocking(Severity.NOTE.value) is False


async def test_note_tone_dedupes_like_every_other_severity(db_session):
    """The fourth rung is a plain severity string, not a special case: raising the
    same ``note`` twice still refreshes one row rather than stacking two."""
    first = await _raise(db_session, severity=Severity.NOTE.value, message="плато")
    second = await _raise(db_session, severity=Severity.NOTE.value, message="плато, 18 дней")
    assert first.id == second.id
    assert second.severity == Severity.NOTE.value
    assert len(await _all_rows(db_session)) == 1
    assert [a.id for a in await alerts_service.list_active(db_session)] == [first.id]


# ── Postgres-only invariants (SQLite fakes these) ─────────────────────────────


@pytest.mark.integration
async def test_partial_unique_index_blocks_a_second_active_row(db_session):
    """The dedupe guarantee must hold at the DB level, not only inside
    ``raise_alert`` — a raw INSERT of a second unresolved row is rejected."""
    await _raise(db_session)
    db_session.add(
        SystemAlert(
            domain=Domain.WEIGHT.value, severity=Severity.INFO.value,
            message="дубликат", alert_key=KEY, entity_ref="",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_partial_unique_index_allows_a_resolved_duplicate(db_session):
    """...and it must NOT block history: once resolved, the same (key, entity)
    can be raised again, which is exactly what the partial WHERE clause buys."""
    first = await _raise(db_session)
    await alerts_service.resolve_by_key(db_session, alert_key=KEY)
    second = await _raise(db_session)
    await db_session.commit()

    assert first.id != second.id
    assert len(await _all_rows(db_session)) == 2


@pytest.mark.integration
async def test_func_date_over_resolved_at_matches_the_local_day(db_session):
    """``func.date(resolved_at) == today`` (alerts_service.py:59) must mean the
    same thing on Postgres as on SQLite — including "23:59 still counts as
    today" and "00:01 next day does not"."""
    alert = await _raise(db_session)
    today = today_local()

    alert.resolved_at = datetime.combine(today, datetime.min.time()).replace(hour=23, minute=59)
    await db_session.flush()
    assert await alerts_service._was_dismissed_today(db_session, KEY, "", today) is True

    alert.resolved_at = datetime.combine(today + timedelta(days=1), datetime.min.time())
    await db_session.flush()
    assert await alerts_service._was_dismissed_today(db_session, KEY, "", today) is False
    assert await alerts_service._was_dismissed_today(
        db_session, KEY, "", today + timedelta(days=1)
    ) is True


@pytest.mark.integration
async def test_alert_survives_a_date_typed_roundtrip(db_session):
    """Sanity: the row really lands in Postgres with the naive local timestamps
    the schema expects (no tz coercion surprises)."""
    a = await _raise(db_session)
    await db_session.commit()
    alert_id = a.id
    db_session.expire_all()

    fresh = await db_session.get(SystemAlert, alert_id)
    assert fresh.created_at.tzinfo is None
    assert isinstance(fresh.created_at, datetime)
