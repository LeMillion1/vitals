"""Transaction-boundary coverage for owned Garmin and Hevy writers."""

from __future__ import annotations

import json

from sqlalchemy import select

from vitals.enums import IntegrationProvider, Source
from vitals.models.garmin import GarminDaily
from vitals.models.hevy import HevyWorkout
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.services import (
    body_scan_service,
    garmin_service,
    hevy_service,
    labs_service,
    raw_payload_service,
)


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

    async def _legacy(session, **kwargs):
        calls.append(("legacy", None, None))
        return 0

    monkeypatch.setattr(garmin_service, "reparse_owned_pending", _garmin)
    monkeypatch.setattr(hevy_service, "reparse_owned_pending", _hevy)
    monkeypatch.setattr(labs_service, "reparse_pending", _legacy)
    monkeypatch.setattr(body_scan_service, "reparse_pending", _legacy)

    await raw_payload_service.sweep_pending_job(session_factory)

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
    assert [name for name, _identity, _connection in calls] == [
        "garmin",
        "hevy",
        "legacy",
        "legacy",
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
        def from_config(cls):
            return _Client()

    from web.routers import hevy as hevy_router

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
