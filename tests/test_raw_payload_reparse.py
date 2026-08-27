"""Raw-payload reparse sweep.

``upsert_owned_raw_payload`` leaves ``processed_at = None`` whenever it refreshes an
existing row ("re-parse pending"). ``raw_payload_service.sweep_domain`` is the
generic sweep that picks those rows back up; garmin/hevy/labs/body_comp each
wire it in with their own ``reparse`` callback + ``has_normalized`` clause.

The first group tests ``sweep_domain`` itself, once, generically (using the
Hevy domain/model as the vehicle — any domain would do). The second group is
one smoke test per real domain, proving its own wiring actually reparses a
payload end to end.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from vitals.enums import Source
from vitals.models.garmin import DOMAIN as GARMIN_DOMAIN
from vitals.models.garmin import GarminActivity, GarminDaily
from vitals.models.hevy import DOMAIN, HevyWorkout
from vitals.models.raw_payload import RawPayload
from vitals.services import raw_payload_service
from vitals.services.garmin import raw_payloads as garmin_raw_payloads
from vitals.services.hevy import raw_payloads as hevy_raw_payloads

import pytest

# The generic ``sweep_domain`` cases are about the sweep's own bookkeeping and
# leave their rows unowned on purpose; the per-domain cases below root theirs.
# Writing an unowned raw is a state the application can no longer reach, so
# this module asks for the schema that stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract


async def _seed_raw(
    session, *, domain, source, external_id, payload, roots=None
) -> RawPayload:
    """One pending raw payload, optionally with the roots a scoped sweep needs.

    The generic ``sweep_domain`` tests are about the sweep's own bookkeeping and
    leave the row unowned; the Garmin case goes through the scoped sweep, which
    only sees payloads inside the scope it was handed.
    """

    row = RawPayload(
        subject_id=None if roots is None else roots.subject_id,
        integration_connection_id=None if roots is None else roots.connection_id,
        domain=domain, source=source, external_id=external_id, payload=payload,
    )
    session.add(row)
    await session.flush()
    return row


def _has_hevy_child():
    return select(HevyWorkout.id).where(HevyWorkout.raw_payload_id == RawPayload.id).exists()


# ── Generic sweep_domain behaviour ────────────────────────────────────────────
async def test_sweep_domain_reparses_a_pending_row_with_no_normalized_child(db_session):
    raw = await _seed_raw(
        db_session, domain=DOMAIN, source=Source.HEVY_API.value,
        external_id="w-pending", payload={"ok": True},
    )
    calls = []

    async def _reparse(session, raw_row):
        calls.append(raw_row.id)

    done = await raw_payload_service.sweep_domain(
        db_session, domain=DOMAIN, reparse=_reparse, has_normalized=_has_hevy_child(),
    )
    assert done == 1
    assert calls == [raw.id]
    assert raw.processed_at is not None


async def test_sweep_domain_skips_a_row_that_already_has_a_normalized_child(db_session, *, hevy_connection_id, legacy_owner_roots):
    raw = await _seed_raw(
        db_session, domain=DOMAIN, source=Source.HEVY_API.value,
        external_id="w-done", payload={"ok": True},
    )
    db_session.add(HevyWorkout(subject_id=legacy_owner_roots.subject_id, integration_connection_id=hevy_connection_id,
        external_id="w-done", domain=DOMAIN, date=date(2026, 6, 10), raw_payload_id=raw.id,
    ))
    await db_session.flush()
    calls = []

    async def _reparse(session, raw_row):
        calls.append(raw_row.id)

    done = await raw_payload_service.sweep_domain(
        db_session, domain=DOMAIN, reparse=_reparse, has_normalized=_has_hevy_child(),
    )
    assert done == 0
    assert calls == []
    assert raw.processed_at is None  # still pending — untouched, not wrongly stamped


async def test_sweep_domain_one_failure_does_not_abort_the_batch(db_session):
    bad = await _seed_raw(
        db_session, domain=DOMAIN, source=Source.HEVY_API.value,
        external_id="w-bad", payload={"ok": True},
    )
    good = await _seed_raw(
        db_session, domain=DOMAIN, source=Source.HEVY_API.value,
        external_id="w-good", payload={"ok": True},
    )

    async def _reparse(session, raw_row):
        if raw_row.external_id == "w-bad":
            raise ValueError("boom")

    done = await raw_payload_service.sweep_domain(
        db_session, domain=DOMAIN, reparse=_reparse, has_normalized=_has_hevy_child(),
    )
    assert done == 1
    assert bad.processed_at is None  # left pending for the next sweep
    assert good.processed_at is not None


# ── Per-domain wiring smoke tests ─────────────────────────────────────────────
async def test_garmin_reparse_pending_recovers_daily_and_activity(db_session, *, garmin_owned_scope):
    """Garmin is the one domain with two models under an ``or_`` — exercise both
    the ``daily:``/``activity:`` dispatch branches in reparse_from_raw."""
    daily_raw = await _seed_raw(
        db_session, domain=GARMIN_DOMAIN, source=Source.GARMIN_API.value,
        roots=garmin_owned_scope,
        external_id="daily:2026-06-10", payload={"summary": {"totalSteps": 4000}},
    )
    activity_raw = await _seed_raw(
        db_session, domain=GARMIN_DOMAIN, source=Source.GARMIN_API.value,
        roots=garmin_owned_scope,
        external_id="activity:act1", payload={"activityId": "act1", "activityName": "Run"},
    )

    done = await garmin_raw_payloads.reparse_owned_pending(db_session, identity=garmin_owned_scope.identity, integration_connection_id=garmin_owned_scope.connection_id)
    assert done == 2

    daily = (await db_session.execute(
        select(GarminDaily).where(GarminDaily.date == date(2026, 6, 10))
    )).scalars().first()
    assert daily is not None and daily.raw_payload_id == daily_raw.id and daily.steps == 4000

    activity = (await db_session.execute(
        select(GarminActivity).where(GarminActivity.external_id == "act1")
    )).scalars().first()
    assert activity is not None and activity.raw_payload_id == activity_raw.id


async def test_hevy_reparse_pending_recovers_pending_workout(db_session, *, hevy_owned_scope):
    raw = await _seed_raw(
        db_session, domain=DOMAIN, source=Source.HEVY_API.value,
        roots=hevy_owned_scope,
        external_id="w-reparse",
        payload={
            "id": "w-reparse", "title": "Push", "start_time": "2026-06-10T10:00:00Z",
            "updated_at": "2026-06-10T11:00:00Z", "exercises": [],
        },
    )
    done = await hevy_raw_payloads.reparse_owned_pending(db_session, identity=hevy_owned_scope.identity, integration_connection_id=hevy_owned_scope.connection_id)
    assert done == 1

    workout = (await db_session.execute(
        select(HevyWorkout).where(HevyWorkout.external_id == "w-reparse")
    )).scalars().first()
    assert workout is not None and workout.raw_payload_id == raw.id
