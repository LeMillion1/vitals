"""Focused ownership tests for Garmin raw-first ingestion and reparse."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import func, select

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.garmin import GarminActivity, GarminDaily, GarminIntraday
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.models.weight import WeightLog
from vitals.ownership import WriteIdentity
from vitals.services import garmin_service
from vitals.services.garmin_service import (
    GarminConnectionInactiveError,
    GarminOwnershipConflictError,
    GarminRawPayloadInvariantError,
)
from vitals.utils.timeutils import now_local


DAY = date(2026, 8, 19)


async def _scope(
    session,
    slug: str,
    *,
    status: IntegrationConnectionStatus = IntegrationConnectionStatus.ACTIVE,
):
    owner = User(
        username=slug,
        normalized_username=slug.casefold(),
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(owner_user_id=owner.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    connection = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator=f"opaque-{slug}",
        status=status.value,
        retired_at=(
            datetime.now(timezone.utc)
            if status is IntegrationConnectionStatus.RETIRED
            else None
        ),
    )
    session.add(connection)
    await session.flush()
    return owner, subject, connection


def _identity(owner, subject, *, system: bool = False) -> WriteIdentity:
    return WriteIdentity(
        subject_id=subject.id,
        actor_user_id=None if system else owner.id,
    )


async def test_owned_daily_stamps_raw_daily_intraday_and_weight(db_session):
    owner, subject, connection = await _scope(db_session, "owner")
    identity = _identity(owner, subject)
    raw = {
        "summary": {"totalSteps": 4321, "weight": 85000},
        "stress": {"stressValuesArray": [[1_777_000_000_000, 44]]},
    }

    daily = await garmin_service.ingest_owned_daily(
        db_session,
        DAY,
        raw,
        identity=identity,
        integration_connection_id=connection.id,
    )

    raw_row = await db_session.scalar(select(RawPayload))
    intraday = await db_session.scalar(select(GarminIntraday))
    weight = await db_session.scalar(select(WeightLog))
    assert (daily.subject_id, daily.actor_user_id) == (subject.id, owner.id)
    assert daily.integration_connection_id == connection.id
    assert daily.raw_payload_id == raw_row.id
    assert raw_row.subject_id == subject.id
    assert raw_row.actor_user_id == owner.id
    assert raw_row.integration_connection_id == connection.id
    assert raw_row.processed_at is not None
    assert intraday.subject_id == subject.id
    assert intraday.integration_connection_id == connection.id
    assert weight.subject_id == subject.id
    assert weight.actor_user_id == owner.id
    assert weight.integration_connection_id == connection.id
    assert weight.raw_payload_id == raw_row.id


async def test_owned_daily_adopts_legacy_roots_without_rewriting_actor(db_session):
    owner, subject, connection = await _scope(db_session, "owner")
    legacy_raw = RawPayload(
        actor_user_id=owner.id,
        domain="garmin",
        source="garmin_api",
        external_id=f"daily:{DAY.isoformat()}",
        payload={"summary": {"totalSteps": 1}},
        fetched_at=now_local(),
    )
    legacy_daily = GarminDaily(
        actor_user_id=owner.id,
        date=DAY,
        domain="garmin",
        source="garmin_api",
    )
    db_session.add_all([legacy_raw, legacy_daily])
    await db_session.flush()

    row = await garmin_service.ingest_owned_daily(
        db_session,
        DAY,
        {"summary": {"totalSteps": 2}},
        identity=_identity(owner, subject, system=True),
        integration_connection_id=connection.id,
    )

    assert row is legacy_daily
    assert row.actor_user_id == owner.id
    assert legacy_raw.actor_user_id == owner.id
    assert (row.subject_id, legacy_raw.subject_id) == (subject.id, subject.id)
    assert row.integration_connection_id == connection.id
    assert legacy_raw.integration_connection_id == connection.id


async def test_owned_daily_adopts_legacy_weight_raw_link(db_session):
    owner, subject, connection = await _scope(db_session, "owner")
    legacy_weight = WeightLog(
        date=DAY,
        domain="weight",
        source=Source.GARMIN_API.value,
        weight_kg=85.0,
        raw_payload_id=None,
        superseded=False,
    )
    db_session.add(legacy_weight)
    await db_session.flush()

    daily = await garmin_service.ingest_owned_daily(
        db_session,
        DAY,
        {"summary": {"totalSteps": 2, "weight": 85000}},
        identity=_identity(owner, subject),
        integration_connection_id=connection.id,
    )

    assert legacy_weight.subject_id == subject.id
    assert legacy_weight.actor_user_id is None
    assert legacy_weight.integration_connection_id == connection.id
    assert legacy_weight.raw_payload_id == daily.raw_payload_id


async def test_foreign_global_daily_key_conflicts_before_raw_mutation(db_session):
    owner_a, subject_a, connection_a = await _scope(db_session, "owner-a")
    _owner_b, subject_b, connection_b = await _scope(db_session, "owner-b")
    db_session.add(
        GarminDaily(
            subject_id=subject_b.id,
            integration_connection_id=connection_b.id,
            date=DAY,
            domain="garmin",
            source="garmin_api",
        )
    )
    await db_session.flush()

    with pytest.raises(GarminOwnershipConflictError):
        await garmin_service.ingest_owned_daily(
            db_session,
            DAY,
            {"summary": {"totalSteps": 9}},
            identity=_identity(owner_a, subject_a),
            integration_connection_id=connection_a.id,
        )

    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 0


async def test_foreign_global_activity_key_conflicts_before_raw_mutation(db_session):
    owner_a, subject_a, connection_a = await _scope(db_session, "owner-a")
    _owner_b, subject_b, connection_b = await _scope(db_session, "owner-b")
    db_session.add(
        GarminActivity(
            subject_id=subject_b.id,
            integration_connection_id=connection_b.id,
            external_id="same-id",
            date=DAY,
            domain="garmin",
            source="garmin_api",
        )
    )
    await db_session.flush()

    with pytest.raises(GarminOwnershipConflictError):
        await garmin_service.ingest_owned_activities(
            db_session,
            [{"activityId": "same-id", "activityName": "foreign"}],
            identity=_identity(owner_a, subject_a),
            integration_connection_id=connection_a.id,
        )
    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 0


async def test_owned_intraday_replacement_does_not_delete_foreign_scope(db_session):
    owner_a, subject_a, connection_a = await _scope(db_session, "owner-a")
    _owner_b, subject_b, connection_b = await _scope(db_session, "owner-b")
    old = datetime(2026, 8, 19, 8)
    db_session.add_all(
        [
            GarminIntraday(
                subject_id=subject_a.id,
                integration_connection_id=connection_a.id,
                date=DAY,
                domain="garmin",
                source="garmin_api",
                series_type="stress",
                ts=old,
                value=1,
            ),
            GarminIntraday(
                subject_id=subject_b.id,
                integration_connection_id=connection_b.id,
                date=DAY,
                domain="garmin",
                source="garmin_api",
                series_type="stress",
                ts=old,
                value=99,
            ),
        ]
    )
    await db_session.flush()

    await garmin_service.ingest_owned_intraday(
        db_session,
        DAY,
        "stress",
        [(datetime(2026, 8, 19, 9), 2)],
        identity=_identity(owner_a, subject_a),
        integration_connection_id=connection_a.id,
    )

    rows = list(await db_session.scalars(select(GarminIntraday)))
    assert {(row.subject_id, row.value) for row in rows} == {
        (subject_a.id, 2),
        (subject_b.id, 99),
    }


async def test_owned_intraday_rejects_foreign_raw_reference(db_session):
    owner_a, subject_a, connection_a = await _scope(db_session, "owner-a")
    owner_b, subject_b, connection_b = await _scope(db_session, "owner-b")
    foreign_raw = RawPayload(
        subject_id=subject_b.id,
        actor_user_id=owner_b.id,
        integration_connection_id=connection_b.id,
        domain="garmin",
        source=Source.GARMIN_API.value,
        external_id=f"daily:{DAY.isoformat()}",
        payload={"stress": {"stressValuesArray": []}},
    )
    db_session.add(foreign_raw)
    await db_session.flush()

    with pytest.raises(GarminRawPayloadInvariantError, match="subject_id"):
        await garmin_service.ingest_owned_intraday(
            db_session,
            DAY,
            "stress",
            [(datetime(2026, 8, 19, 9), 2)],
            identity=_identity(owner_a, subject_a),
            integration_connection_id=connection_a.id,
            raw_payload_id=foreign_raw.id,
        )

    assert await db_session.scalar(select(func.count()).select_from(GarminIntraday)) == 0


@pytest.mark.parametrize(
    "status",
    [IntegrationConnectionStatus.DISABLED, IntegrationConnectionStatus.RETIRED],
)
async def test_owned_reparse_derives_actor_and_allows_historical_connection(
    db_session,
    status,
):
    owner, subject, connection = await _scope(
        db_session, "owner", status=status
    )
    fetched_at = datetime(2026, 1, 1, 3)
    raw_row = RawPayload(
        subject_id=subject.id,
        actor_user_id=owner.id,
        integration_connection_id=connection.id,
        domain="garmin",
        source="garmin_api",
        external_id=f"daily:{DAY.isoformat()}",
        payload={"summary": {"totalSteps": 7654}},
        fetched_at=fetched_at,
    )
    db_session.add(raw_row)
    await db_session.flush()

    row = await garmin_service.reparse_owned_daily_from_raw(db_session, raw_row)

    assert row.actor_user_id == owner.id
    assert row.subject_id == subject.id
    assert row.integration_connection_id == connection.id
    assert row.steps == 7654
    assert row.raw_payload_id == raw_row.id
    assert raw_row.fetched_at == fetched_at


async def test_owned_activity_reparse_rejects_payload_id_substitution(db_session):
    owner, subject, connection = await _scope(db_session, "owner")
    raw_row = RawPayload(
        subject_id=subject.id,
        actor_user_id=owner.id,
        integration_connection_id=connection.id,
        domain="garmin",
        source="garmin_api",
        external_id="activity:expected",
        payload={"activityId": "other"},
        fetched_at=now_local(),
    )
    db_session.add(raw_row)
    await db_session.flush()

    with pytest.raises(GarminRawPayloadInvariantError, match="does not match"):
        await garmin_service.reparse_owned_activity_from_raw(db_session, raw_row)
    assert await db_session.scalar(select(func.count()).select_from(GarminActivity)) == 0
    assert raw_row.processed_at is None


async def test_owned_hae_is_partial_and_reparsable(db_session):
    owner, subject, connection = await _scope(db_session, "owner")
    identity = _identity(owner, subject)
    existing = GarminDaily(
        subject_id=subject.id,
        actor_user_id=owner.id,
        integration_connection_id=connection.id,
        date=DAY,
        domain="garmin",
        source="garmin_api",
        steps=8000,
        sleep_score=80,
    )
    db_session.add(existing)
    await db_session.flush()
    payload = {
        "data": {
            "metrics": [
                {
                    "name": "resting_heart_rate",
                    "data": [{"date": f"{DAY.isoformat()} 00:00:00 +0000", "qty": 51}],
                }
            ]
        }
    }

    result = await garmin_service.ingest_owned_health_auto_export(
        db_session,
        payload,
        identity=identity,
        integration_connection_id=connection.id,
    )
    hae_raw = await db_session.scalar(
        select(RawPayload).where(RawPayload.source == Source.HEALTH_AUTO_EXPORT.value)
    )
    assert result == {"dates": 1}
    assert existing.steps == 8000 and existing.sleep_score == 80
    assert existing.resting_hr == 51
    assert hae_raw.external_id == f"hae:{DAY.isoformat()}"
    assert hae_raw.subject_id == subject.id
    assert hae_raw.actor_user_id == owner.id
    assert hae_raw.integration_connection_id == connection.id
    assert hae_raw.payload["source_payload"] == payload
    assert existing.source == Source.HEALTH_AUTO_EXPORT.value
    assert existing.raw_payload_id == hae_raw.id

    existing.resting_hr = None
    hae_raw.processed_at = None
    await db_session.flush()
    await garmin_service.reparse_owned_health_auto_export_from_raw(
        db_session, hae_raw
    )
    assert existing.resting_hr == 51
    assert existing.steps == 8000 and existing.sleep_score == 80


async def test_owned_daily_weight_bridge_fails_closed_with_multiple_subjects(
    db_session,
):
    owner_a, subject_a, connection_a = await _scope(db_session, "owner-a")
    await _scope(db_session, "owner-b")

    with pytest.raises(GarminOwnershipConflictError, match="multi-subject"):
        await garmin_service.ingest_owned_daily(
            db_session,
            DAY,
            {"summary": {"totalSteps": 42, "weight": 85000}},
            identity=_identity(owner_a, subject_a),
            integration_connection_id=connection_a.id,
        )

    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 0
    assert await db_session.scalar(select(func.count()).select_from(GarminDaily)) == 0
    assert await db_session.scalar(select(func.count()).select_from(WeightLog)) == 0


async def test_owned_pulse_merges_summary_into_latest_bundle_after_fetch(db_session):
    owner, subject, connection = await _scope(db_session, "owner")
    identity = _identity(owner, subject, system=True)
    await garmin_service.ingest_owned_daily(
        db_session,
        DAY,
        {
            "summary": {"totalSteps": 100},
            "sleep": {
                "dailySleepDTO": {
                    "sleepScores": {"overall": {"value": 70}}
                }
            },
        },
        identity=identity,
        integration_connection_id=connection.id,
    )

    class _InterleavingClient:
        async def fetch_summary(self, on_date):
            assert on_date == DAY
            await garmin_service.ingest_owned_daily(
                db_session,
                DAY,
                {
                    "summary": {"totalSteps": 200},
                    "sleep": {
                        "dailySleepDTO": {
                            "sleepScores": {"overall": {"value": 88}}
                        }
                    },
                },
                identity=identity,
                integration_connection_id=connection.id,
            )
            return {"totalSteps": 300}

    result = await garmin_service.pulse_owned(
        db_session,
        _InterleavingClient(),
        identity=identity,
        integration_connection_id=connection.id,
        on_date=DAY,
    )

    row = await db_session.scalar(select(GarminDaily))
    assert result == {"steps": 300, "error": None}
    assert row is not None and row.steps == 300 and row.sleep_score == 88


@pytest.mark.parametrize(
    "status",
    [IntegrationConnectionStatus.PENDING, IntegrationConnectionStatus.DISABLED],
)
async def test_fresh_owned_sync_rejects_inactive_connection_before_network(
    db_session,
    status,
):
    owner, subject, connection = await _scope(db_session, "owner", status=status)

    class _NoFetchClient:
        is_configured = True
        token_warnings = []
        calls = 0

        async def fetch_daily(self, on_date):
            self.calls += 1
            return {"summary": {"totalSteps": 42}}

        async def fetch_activities(self, start, end):
            self.calls += 1
            return []

    client = _NoFetchClient()
    with pytest.raises(GarminConnectionInactiveError, match=status.value):
        await garmin_service.sync_owned(
            db_session,
            client,
            identity=_identity(owner, subject),
            integration_connection_id=connection.id,
            days=1,
        )

    assert client.calls == 0


async def test_owned_sync_attributes_auth_alert_and_auto_resolves_actorlessly(
    db_session,
):
    from vitals.integrations.garmin_client import GarminMFARequired

    owner, subject, connection = await _scope(db_session, "owner")
    identity = _identity(owner, subject)

    class _AuthFailureClient:
        token_warnings = []

        async def fetch_daily(self, on_date):
            raise GarminMFARequired("synthetic MFA")

        async def fetch_activities(self, start, end):
            raise AssertionError("activities must not be fetched after auth failure")

    summary = await garmin_service.sync_owned(
        db_session,
        _AuthFailureClient(),
        identity=identity,
        integration_connection_id=connection.id,
        days=1,
        on_date=DAY,
    )
    assert summary["error"] == "mfa"
    row = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.alert_key == garmin_service.AUTH_ALERT_KEY
        )
    )
    assert row is not None
    assert (row.subject_id, row.integration_connection_id) == (
        subject.id,
        connection.id,
    )

    class _HealthyClient:
        token_warnings = []

        async def fetch_daily(self, on_date):
            return {"summary": {"totalSteps": 42}}

        async def fetch_activities(self, start, end):
            return []

    await garmin_service.sync_owned(
        db_session,
        _HealthyClient(),
        identity=identity,
        integration_connection_id=connection.id,
        days=1,
        on_date=DAY,
    )
    assert row.resolved_at is not None
    assert row.resolved_by_user_id is None


async def test_owned_sync_attributes_token_cache_alert(db_session):
    owner, subject, connection = await _scope(db_session, "owner")

    class _TokenWarningClient:
        token_warnings = ["synthetic token-store warning"]

        async def fetch_daily(self, on_date):
            return {"summary": {"totalSteps": 42}}

        async def fetch_activities(self, start, end):
            return []

    await garmin_service.sync_owned(
        db_session,
        _TokenWarningClient(),
        identity=_identity(owner, subject),
        integration_connection_id=connection.id,
        days=1,
        on_date=DAY,
    )

    row = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.alert_key == garmin_service.TOKEN_ALERT_KEY
        )
    )
    assert row is not None
    assert (row.subject_id, row.integration_connection_id) == (
        subject.id,
        connection.id,
    )


async def test_owned_pending_sweep_is_scoped_and_rolls_back_bad_row(db_session):
    owner, subject, connection = await _scope(db_session, "owner")
    identity = _identity(owner, subject, system=True)
    bad = RawPayload(
        subject_id=subject.id,
        integration_connection_id=connection.id,
        domain="garmin",
        source="garmin_api",
        external_id="activity:expected",
        payload={"activityId": "wrong"},
        fetched_at=now_local(),
    )
    good = RawPayload(
        subject_id=subject.id,
        integration_connection_id=connection.id,
        domain="garmin",
        source="garmin_api",
        external_id=f"daily:{DAY.isoformat()}",
        payload={"summary": {"totalSteps": 123}},
        fetched_at=now_local(),
    )
    db_session.add_all([bad, good])
    await db_session.flush()

    done = await garmin_service.reparse_owned_pending(
        db_session,
        identity=identity,
        integration_connection_id=connection.id,
    )

    assert done == 1
    assert bad.processed_at is None
    assert good.processed_at is not None
    daily = await db_session.scalar(select(GarminDaily))
    assert daily.steps == 123
    assert daily.actor_user_id is None


@pytest.mark.parametrize("actor_username, human", [(None, False), (" OWNER ", True)])
async def test_sync_job_resolves_system_or_owner_actor(
    db_session,
    session_factory,
    monkeypatch,
    actor_username,
    human,
):
    owner, subject, connection = await _scope(db_session, "owner")

    class _Client:
        is_configured = True
        token_warnings = []

        @classmethod
        def from_config(cls, config=None, redis=None):
            return cls()

        async def fetch_daily(self, on_date):
            return {"summary": {"totalSteps": 42}}

        async def fetch_activities(self, start, end):
            return []

    import vitals.integrations.garmin_client as client_module

    monkeypatch.setattr(client_module, "GarminClient", _Client)
    await garmin_service.sync_job(
        session_factory,
        days=1,
        actor_username=actor_username,
    )

    raw_row = await db_session.scalar(select(RawPayload))
    assert raw_row.subject_id == subject.id
    assert raw_row.integration_connection_id == connection.id
    assert raw_row.actor_user_id == (owner.id if human else None)


async def test_sync_job_noops_for_disabled_connection(
    db_session,
    session_factory,
    monkeypatch,
):
    await _scope(
        db_session,
        "owner",
        status=IntegrationConnectionStatus.DISABLED,
    )

    class _Client:
        is_configured = True
        token_warnings = []
        calls = 0

        @classmethod
        def from_config(cls, config=None, redis=None):
            return client

        async def fetch_daily(self, on_date):
            self.calls += 1
            return {"summary": {"totalSteps": 42}}

        async def fetch_activities(self, start, end):
            self.calls += 1
            return []

    client = _Client()
    import vitals.integrations.garmin_client as client_module

    monkeypatch.setattr(
        client_module,
        "GarminClient",
        _Client,
    )

    result = await garmin_service.sync_job(session_factory, days=1)

    assert result is None
    assert client.calls == 0
    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 0
