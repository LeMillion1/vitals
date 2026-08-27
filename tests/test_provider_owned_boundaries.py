"""Transaction-boundary coverage for owned Garmin and Hevy writers."""

from __future__ import annotations

from tests.job_runner import run_job_for_every_subject

import asyncio
import json
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import IntegrationProvider, Source
from vitals.models.garmin import GarminDaily
from vitals.models.hevy import HevyWorkout
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services.data_lake import sweep as raw_sweep
from vitals.services.garmin import ingestion as garmin_ingestion
from vitals.services.garmin import ownership as garmin_ownership
from vitals.services.garmin import raw_payloads as garmin_raw_payloads
from vitals.services.garmin import sync as garmin_sync
from vitals.services.hevy import raw_payloads as hevy_raw_payloads
from vitals.services.hevy import sync as hevy_sync
from vitals.services.labs import ingestion as labs_ingestion
from vitals.services.body_scan.scans import reparse as body_scan_reparse


async def _legacy_roots(session):
    owner = await session.scalar(select(User).where(User.normalized_username == "tester"))
    subject = await session.scalar(
        select(HealthSubject).where(HealthSubject.owner_user_id == owner.id)
    )
    connections = {
        row.provider: row
        for row in await session.scalars(
            select(IntegrationConnection).where(
                IntegrationConnection.subject_id == subject.id
            )
        )
    }
    return owner, subject, connections


async def test_nightly_raw_sweep_passes_exact_system_owned_roots(
    auth_client,
    db_session,
    session_factory,
    monkeypatch,
):
    _owner, subject, connections = await _legacy_roots(db_session)
    calls = []

    async def _garmin(session, *, identity, integration_connection_id, **kwargs):
        calls.append(("garmin", identity, integration_connection_id))
        return 0

    async def _hevy(session, *, identity, integration_connection_id, **kwargs):
        calls.append(("hevy", identity, integration_connection_id))
        return 0

    async def _labs(session, *, identity, prepared_conflict_write, **kwargs):
        calls.append(("labs", identity, prepared_conflict_write))
        return 0

    async def _body_comp(session, *, identity, **kwargs):
        # body_comp is closed: the sweep carries the subject, not an escape hatch.
        calls.append(("body_comp", identity, identity.subject_id))
        return 0

    monkeypatch.setattr(garmin_raw_payloads, "reparse_owned_pending", _garmin)
    monkeypatch.setattr(hevy_raw_payloads, "reparse_owned_pending", _hevy)
    monkeypatch.setattr(labs_ingestion, "reparse_owned_pending", _labs)
    monkeypatch.setattr(
        body_scan_reparse,
        "reparse_owned_pending",
        _body_comp,
    )

    await run_job_for_every_subject(raw_sweep.sweep_pending_job, session_factory)

    owned = {name: (identity, connection_id) for name, identity, connection_id in calls[:2]}
    garmin_identity, garmin_connection_id = owned["garmin"]
    hevy_identity, hevy_connection_id = owned["hevy"]
    assert (garmin_identity.subject_id, garmin_identity.actor_user_id) == (
        subject.id,
        None,
    )
    assert (hevy_identity.subject_id, hevy_identity.actor_user_id) == (
        subject.id,
        None,
    )
    assert garmin_connection_id == connections[IntegrationProvider.GARMIN.value].id
    assert hevy_connection_id == connections[IntegrationProvider.HEVY.value].id
    labs_identity = calls[2][1]
    assert (labs_identity.subject_id, labs_identity.actor_user_id) == (
        subject.id,
        None,
    )
    assert calls[2][2] is not None
    body_identity = calls[3][1]
    assert (body_identity.subject_id, body_identity.actor_user_id) == (
        subject.id,
        None,
    )
    assert calls[3][2] == subject.id
    assert [name for name, _identity, _connection in calls] == [
        "garmin",
        "hevy",
        "labs",
        "body_comp",
    ]


async def test_garmin_import_route_attributes_owner_subject_and_connection(
    auth_client,
    db_session,
):
    owner, subject, connections = await _legacy_roots(db_session)
    payload = {
        "data": {
            "metrics": [
                {
                    "name": "step_count",
                    "data": [
                        {"date": "2026-08-19 00:00:00 +0000", "qty": 7200}
                    ],
                }
            ]
        }
    }

    response = await auth_client.post(
        "/garmin/import",
        files={"file": ("export.json", json.dumps(payload), "application/json")},
    )

    assert response.status_code == 303
    raw = await db_session.scalar(
        select(RawPayload).where(RawPayload.source == Source.HEALTH_AUTO_EXPORT.value)
    )
    daily = await db_session.scalar(select(GarminDaily))
    garmin_connection = connections[IntegrationProvider.GARMIN.value]
    assert (raw.subject_id, raw.actor_user_id, raw.integration_connection_id) == (
        subject.id,
        owner.id,
        garmin_connection.id,
    )
    assert (daily.subject_id, daily.actor_user_id, daily.integration_connection_id) == (
        subject.id,
        owner.id,
        garmin_connection.id,
    )


async def test_garmin_sync_route_attributes_owner_subject_and_connection(
    auth_client,
    db_session,
    garmin_connected,
    monkeypatch,
):
    owner, subject, connections = await _legacy_roots(db_session)

    class _Client:
        is_configured = True
        token_warnings = []

        async def fetch_daily(self, on_date):
            return {"summary": {"totalSteps": 4321}}

        async def fetch_activities(self, start, end):
            return []

    client = _Client()

    class _Factory:
        @classmethod
        def from_config(cls, config=None, redis=None):
            return client

    from web.routers import garmin as garmin_router

    monkeypatch.setattr(garmin_router, "GarminClient", _Factory)
    response = await auth_client.post("/garmin/sync")

    assert response.status_code == 303
    raw_rows = list(
        await db_session.scalars(
            select(RawPayload).where(
                RawPayload.source == Source.GARMIN_API.value
            )
        )
    )
    garmin_connection = connections[IntegrationProvider.GARMIN.value]
    assert len(raw_rows) == 2
    assert {
        (raw.subject_id, raw.actor_user_id, raw.integration_connection_id)
        for raw in raw_rows
    } == {(subject.id, owner.id, garmin_connection.id)}
    daily_rows = list(await db_session.scalars(select(GarminDaily)))
    assert len(daily_rows) == 2
    assert {
        (daily.subject_id, daily.actor_user_id, daily.integration_connection_id)
        for daily in daily_rows
    } == {(subject.id, owner.id, garmin_connection.id)}


async def test_hevy_sync_route_attributes_owner_subject_and_connection(
    auth_client,
    db_session,
    hevy_connected,
    monkeypatch,
):
    owner, subject, connections = await _legacy_roots(db_session)

    class _Client:
        is_configured = True

        async def fetch_workouts(self, *, max_pages=50):
            return [
                {
                    "id": "route-owned",
                    "title": "Owned route",
                    "start_time": "2026-08-19T10:00:00Z",
                    "updated_at": "2026-08-19T11:00:00Z",
                    "exercises": [],
                }
            ]

    class _Factory:
        @classmethod
        def from_config(cls, config=None):
            return _Client()

    from web.routers import hevy as hevy_router

    legacy_alert = SystemAlert(
        domain="workouts",
        severity="warn",
        message="legacy Hevy failure",
        alert_key=hevy_router.SYNC_ALERT_KEY,
        entity_ref="",
    )
    db_session.add(legacy_alert)
    await db_session.flush()

    monkeypatch.setattr(hevy_router, "HevyClient", _Factory)
    response = await auth_client.post("/hevy/sync")

    assert response.status_code == 303
    raw = await db_session.scalar(
        select(RawPayload).where(RawPayload.source == Source.HEVY_API.value)
    )
    workout = await db_session.scalar(select(HevyWorkout))
    hevy_connection = connections[IntegrationProvider.HEVY.value]
    assert (raw.subject_id, raw.actor_user_id, raw.integration_connection_id) == (
        subject.id,
        owner.id,
        hevy_connection.id,
    )
    assert (
        workout.subject_id,
        workout.actor_user_id,
        workout.integration_connection_id,
    ) == (subject.id, owner.id, hevy_connection.id)
    assert (legacy_alert.subject_id, legacy_alert.integration_connection_id) == (
        subject.id,
        hevy_connection.id,
    )
    assert legacy_alert.resolved_at is not None
    assert legacy_alert.resolved_by_user_id is None


async def test_hevy_sync_failure_alert_is_provider_owned(
    auth_client,
    db_session,
    hevy_connected,
    monkeypatch,
):
    from vitals.integrations.hevy_client import HevyAPIError
    from web.routers import hevy as hevy_router

    _owner, subject, connections = await _legacy_roots(db_session)

    class _Client:
        is_configured = True

        async def fetch_workouts(self, *, max_pages=50):
            raise HevyAPIError(503, "synthetic outage")

    class _Factory:
        @classmethod
        def from_config(cls, config=None):
            return _Client()

    monkeypatch.setattr(hevy_router, "HevyClient", _Factory)
    response = await auth_client.post("/hevy/sync")

    assert response.status_code == 303
    assert response.headers["location"] == "/hevy?sync=error"
    row = await db_session.scalar(
        select(SystemAlert).where(SystemAlert.alert_key == hevy_router.SYNC_ALERT_KEY)
    )
    hevy_connection = connections[IntegrationProvider.HEVY.value]
    assert row is not None
    assert (row.subject_id, row.integration_connection_id) == (
        subject.id,
        hevy_connection.id,
    )


async def test_garmin_sync_takes_governance_before_provider_row_locks(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    owner, subject, connections = await _legacy_roots(db_session)
    events: list[str] = []
    original_governance = garmin_ingestion.acquire_identity_governance_lock
    original_scope_lock = garmin_ownership._lock_owned_garmin_scope

    async def _governance(session):
        events.append("governance")
        return await original_governance(session)

    async def _scope_lock(*args, **kwargs):
        events.append("provider-lock")
        return await original_scope_lock(*args, **kwargs)

    class _Client:
        token_warnings = []

        async def fetch_daily(self, on_date):
            events.append("network")
            return {"summary": {"totalSteps": 1000}}

        async def fetch_activities(self, start, end):
            events.append("network")
            return []

    monkeypatch.setattr(
        garmin_sync,
        "acquire_identity_governance_lock",
        _governance,
    )
    monkeypatch.setattr(
        garmin_ingestion,
        "_lock_owned_garmin_scope",
        _scope_lock,
    )
    await garmin_sync.sync_owned(
        db_session,
        _Client(),
        identity=WriteIdentity(subject.id, owner.id),
        integration_connection_id=connections[
            IntegrationProvider.GARMIN.value
        ].id,
        days=1,
        on_date=date(2026, 8, 19),
    )

    assert max(i for i, item in enumerate(events) if item == "network") < events.index(
        "governance"
    )
    assert events.index("governance") < events.index("provider-lock")


async def test_hevy_sync_takes_governance_before_provider_row_locks(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    owner, subject, connections = await _legacy_roots(db_session)
    events: list[str] = []
    original_governance = hevy_sync.acquire_identity_governance_lock
    original_scope_lock = hevy_sync._lock_owned_hevy_scope

    async def _governance(session):
        events.append("governance")
        return await original_governance(session)

    async def _scope_lock(*args, **kwargs):
        events.append("provider-lock")
        return await original_scope_lock(*args, **kwargs)

    class _Client:
        async def fetch_workouts(self, *, max_pages=50):
            events.append("network")
            return []

    monkeypatch.setattr(
        hevy_sync,
        "acquire_identity_governance_lock",
        _governance,
    )
    monkeypatch.setattr(
        hevy_sync,
        "_lock_owned_hevy_scope",
        _scope_lock,
    )
    await hevy_sync.sync_owned(
        db_session,
        _Client(),
        identity=WriteIdentity(subject.id, owner.id),
        integration_connection_id=connections[IntegrationProvider.HEVY.value].id,
    )

    assert events == ["network", "governance", "provider-lock"]


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["garmin", "hevy"])
async def test_postgres_provider_sync_waits_for_governance_before_root_lock(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    provider,
):
    from vitals.services.identity.governance import acquire_identity_governance_lock

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    provider_enum = (
        IntegrationProvider.GARMIN
        if provider == "garmin"
        else IntegrationProvider.HEVY
    )
    connection_id = await db_session.scalar(
        select(IntegrationConnection.id).where(
            IntegrationConnection.subject_id == legacy_owner_roots.subject_id,
            IntegrationConnection.provider == provider_enum.value,
        )
    )
    assert connection_id is not None
    await db_session.commit()

    governance_held = asyncio.Event()
    probe_root_now = asyncio.Event()
    sync_waiting_for_governance = asyncio.Event()

    async def _signaled_governance(session):
        sync_waiting_for_governance.set()
        await acquire_identity_governance_lock(session)

    service = garmin_sync if provider == "garmin" else hevy_sync
    monkeypatch.setattr(
        service,
        "acquire_identity_governance_lock",
        _signaled_governance,
    )

    async def _hold_governance_then_probe_root():
        async with factory() as session:
            await acquire_identity_governance_lock(session)
            governance_held.set()
            await probe_root_now.wait()
            row = await session.scalar(
                select(HealthSubject)
                .where(HealthSubject.id == legacy_owner_roots.subject_id)
                .with_for_update(nowait=True)
            )
            assert row is not None
            await session.commit()

    async def _run_sync():
        async with factory() as session:
            identity = WriteIdentity(
                legacy_owner_roots.subject_id,
                legacy_owner_roots.user_id,
            )
            if provider == "garmin":
                class _GarminClient:
                    token_warnings = []

                    async def fetch_daily(self, on_date):
                        return {"summary": {"totalSteps": 1000}}

                    async def fetch_activities(self, start, end):
                        return []

                await garmin_sync.sync_owned(
                    session,
                    _GarminClient(),
                    identity=identity,
                    integration_connection_id=connection_id,
                    days=1,
                    on_date=date(2026, 8, 19),
                )
            else:
                class _HevyClient:
                    async def fetch_workouts(self, *, max_pages=50):
                        return []

                await hevy_sync.sync_owned(
                    session,
                    _HevyClient(),
                    identity=identity,
                    integration_connection_id=connection_id,
                )
            await session.commit()

    holder = asyncio.create_task(_hold_governance_then_probe_root())
    await asyncio.wait_for(governance_held.wait(), timeout=5)
    sync = asyncio.create_task(_run_sync())
    await asyncio.wait_for(sync_waiting_for_governance.wait(), timeout=5)
    probe_root_now.set()
    await asyncio.wait_for(holder, timeout=5)
    await asyncio.wait_for(sync, timeout=5)
