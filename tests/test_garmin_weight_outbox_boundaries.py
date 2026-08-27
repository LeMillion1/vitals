"""Production-boundary contracts for the scoped Garmin Weight outbox."""

from __future__ import annotations

import asyncio
import ast
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from vitals.enums import (
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionStatus,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.integrations.garmin_client import GarminClient
from vitals.models.app_settings import AppSetting
from vitals.models.garmin import GarminWeightExport
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.scoped_settings import IntegrationConnectionSetting
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.models.weight import WeightLog
from vitals.ownership import WriteIdentity
from vitals.services import weight as weight_domain
from vitals.services.garmin_weight import contracts as garmin_weight_contracts
from vitals.services.garmin_weight import dispatch as garmin_weight_dispatch
from vitals.services.garmin_weight import jobs as garmin_weight_jobs
from vitals.services.garmin_weight import outbox as garmin_weight_outbox
from vitals.services.garmin_weight import settings as garmin_weight_settings
from vitals.services.conflicts import engine
from vitals.services.garmin import alerts as garmin_alerts
from vitals.services.settings.contracts import ScopedSettingKey


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader or writer does when the ownership backfill has not reached a row yet,
# which is a state the application itself can no longer create. The schema
# says so, so this module asks for the one that stood before the contract.
pytestmark = pytest.mark.pre_ownership_contract


DAY = date(2026, 8, 20)
NOW = datetime(2026, 8, 20, 9, 30)
ROOT = Path(__file__).resolve().parents[1]


def _identity(roots) -> WriteIdentity:
    return WriteIdentity(roots.subject_id, roots.user_id)


async def _connection(
    session,
    *,
    subject_id,
    provider: IntegrationProvider,
) -> IntegrationConnection:
    row = await session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == subject_id,
            IntegrationConnection.provider == provider.value,
        )
    )
    assert row is not None
    return row


def _export_context(
    identity: WriteIdentity,
    garmin: IntegrationConnection,
) -> garmin_weight_contracts.GarminWeightExportContext:
    return garmin_weight_contracts.GarminWeightExportContext(
        identity=identity,
        integration_connection_id=garmin.id,
        legacy_bridge=engine.LegacyConflictBridge.FULLY_UNOWNED,
    )


def _conflict_context(identity: WriteIdentity):
    return engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=DAY,
        legacy_bridge=engine.LegacyConflictBridge.FULLY_UNOWNED,
    )


async def _enable_scoped(session, garmin: IntegrationConnection) -> None:
    session.add(
        IntegrationConnectionSetting(
            integration_connection_id=garmin.id,
            key=ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED.value,
            value=True,
        )
    )
    await session.flush()


async def test_weight_capability_projects_exact_destination_without_rewriting_scan_origin(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        subject_id=identity.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    openrouter = await _connection(
        db_session,
        subject_id=identity.subject_id,
        provider=IntegrationProvider.OPENROUTER,
    )
    asset = FileAsset(
        subject_id=identity.subject_id,
        uploaded_by_user_id=identity.actor_user_id,
        purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT.value,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        storage_ref="synthetic/body-scan.png",
        status=FileAssetStatus.LEGACY_PLACEHOLDER.value,
    )
    db_session.add(asset)
    await db_session.flush()
    raw = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        integration_connection_id=openrouter.id,
        file_asset_id=asset.id,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
        external_id="synthetic/body-scan.png",
        payload={"synthetic": True},
    )
    db_session.add(raw)
    await _enable_scoped(db_session, garmin)
    await db_session.flush()

    export_context = _export_context(identity, garmin)
    prepared = await weight_domain.governance.prepare_weight_write(
        db_session,
        context=_conflict_context(identity),
        garmin_weight_export_context=export_context,
    )

    assert prepared.garmin_weight_export is not None
    assert prepared.garmin_weight_export.context == export_context

    weight = await weight_domain.writes.log_weight(
        db_session,
        on_date=DAY,
        weight_kg=81.25,
        source=Source.BODY_SCAN.value,
        identity=identity,
        integration_connection_id=openrouter.id,
        raw_payload_id=raw.id,
        prepared_weight_write=prepared,
    )
    outbox = await db_session.scalar(
        select(GarminWeightExport).where(GarminWeightExport.date == DAY)
    )

    assert outbox is not None
    assert (
        outbox.subject_id,
        outbox.integration_connection_id,
        outbox.requested_by_user_id,
    ) == (identity.subject_id, garmin.id, identity.actor_user_id)
    assert outbox.weight_log_id == weight.id
    assert (
        weight.integration_connection_id,
        weight.raw_payload_id,
        raw.integration_connection_id,
    ) == (openrouter.id, raw.id, openrouter.id)


@pytest.mark.parametrize(
    "status",
    [
        IntegrationConnectionStatus.PENDING,
        IntegrationConnectionStatus.DISABLED,
    ],
)
async def test_fresh_capability_rejects_inactive_connection(
    db_session,
    legacy_owner_roots,
    status,
):
    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        subject_id=identity.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    garmin.status = status.value
    await db_session.flush()

    with pytest.raises(garmin_weight_contracts.GarminWeightExportOwnershipError):
        await garmin_weight_outbox.prepare_scoped_export(
            db_session,
            context=_export_context(identity, garmin),
            historical=False,
        )


@pytest.mark.parametrize(
    "status",
    [
        IntegrationConnectionStatus.DISABLED,
        IntegrationConnectionStatus.RETIRED,
    ],
)
async def test_historical_capability_can_read_and_disable_closed_connection(
    db_session,
    legacy_owner_roots,
    status,
):
    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        subject_id=identity.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    await _enable_scoped(db_session, garmin)
    garmin.status = status.value
    if status is IntegrationConnectionStatus.RETIRED:
        garmin.retired_at = NOW
    await db_session.flush()
    context = garmin_weight_contracts.GarminWeightExportContext(
        identity=identity,
        integration_connection_id=garmin.id,
        legacy_bridge=engine.LegacyConflictBridge.REJECT,
    )
    prepared = await garmin_weight_outbox.prepare_scoped_export(
        db_session,
        context=context,
        historical=True,
    )

    status_projection = await garmin_weight_jobs.get_status_scoped(
        db_session,
        prepared=prepared,
    )
    assert status_projection["enabled"] is True
    assert (
        await garmin_weight_settings.set_enabled_scoped(
            db_session,
            False,
            prepared=prepared,
        )
        is False
    )
    scoped = await db_session.get(
        IntegrationConnectionSetting,
        (garmin.id, ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED.value),
    )
    assert scoped is not None and scoped.value is False


async def test_historical_capability_rejects_pending_connection(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        subject_id=identity.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    garmin.status = IntegrationConnectionStatus.PENDING.value
    await db_session.flush()

    with pytest.raises(garmin_weight_contracts.GarminWeightExportOwnershipError):
        await garmin_weight_outbox.prepare_scoped_export(
            db_session,
            context=_export_context(identity, garmin),
            historical=True,
        )


async def test_capability_rejects_cross_session_and_closed_savepoint(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        subject_id=identity.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    context = _export_context(identity, garmin)
    prepared = await garmin_weight_outbox.prepare_scoped_export(
        db_session,
        context=context,
        historical=True,
    )

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with factory() as other_session:
        with pytest.raises(garmin_weight_contracts.GarminWeightExportPreparedError):
            await garmin_weight_jobs.get_status_scoped(
                other_session,
                prepared=prepared,
            )

    nested = await db_session.begin_nested()
    nested_prepared = await garmin_weight_outbox.prepare_scoped_export(
        db_session,
        context=context,
        historical=True,
    )
    await nested.commit()
    with pytest.raises(garmin_weight_contracts.GarminWeightExportPreparedError):
        await garmin_weight_jobs.get_status_scoped(
            db_session,
            prepared=nested_prepared,
        )


@pytest.mark.integration
async def test_pg_scoped_capability_preparation_serializes_root_lock_order(
    db_session,
    legacy_owner_roots,
):
    """A second scope preparation cannot pass the first transaction's roots."""

    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        subject_id=identity.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    context = _export_context(identity, garmin)
    await db_session.commit()

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    first_prepared = asyncio.Event()
    release_first = asyncio.Event()
    second_prepared = asyncio.Event()

    async def hold_first() -> None:
        async with factory() as session:
            await garmin_weight_outbox.prepare_scoped_export(
                session,
                context=context,
                historical=True,
            )
            first_prepared.set()
            await release_first.wait()
            await session.commit()

    async def prepare_second() -> None:
        async with factory() as session:
            await garmin_weight_outbox.prepare_scoped_export(
                session,
                context=context,
                historical=True,
            )
            second_prepared.set()
            await session.commit()

    first_task = asyncio.create_task(hold_first())
    await asyncio.wait_for(first_prepared.wait(), timeout=2)
    second_task = asyncio.create_task(prepare_second())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(second_prepared.wait()),
                timeout=0.2,
            )
    finally:
        release_first.set()
    await asyncio.wait_for(first_task, timeout=2)
    await asyncio.wait_for(second_task, timeout=2)
    assert second_prepared.is_set()


async def test_strict_scope_rejects_fully_null_legacy_outbox(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        subject_id=identity.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    db_session.add(
        GarminWeightExport(
            date=DAY,
            weight_kg=80.0,
            measured_at=NOW,
            status="deleted",
        )
    )
    await db_session.flush()
    context = garmin_weight_contracts.GarminWeightExportContext(
        identity=identity,
        integration_connection_id=garmin.id,
        legacy_bridge=engine.LegacyConflictBridge.REJECT,
    )
    prepared = await garmin_weight_outbox.prepare_scoped_export(
        db_session,
        context=context,
        historical=True,
    )

    with pytest.raises(garmin_weight_contracts.GarminWeightExportOwnershipError):
        await garmin_weight_jobs.get_status_scoped(
            db_session,
            prepared=prepared,
        )


@pytest.mark.parametrize("weight_scope", ["foreign", "partial"])
async def test_exact_outbox_rejects_linked_weight_with_untrusted_roots(
    db_session,
    legacy_owner_roots,
    weight_scope,
):
    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        subject_id=identity.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    await _enable_scoped(db_session, garmin)
    if weight_scope == "foreign":
        foreign_owner = User(
            username="foreign-outbox-weight",
            normalized_username="foreign-outbox-weight",
            password_hash="$synthetic-test-hash",
            status=UserStatus.ACTIVE.value,
        )
        db_session.add(foreign_owner)
        await db_session.flush()
        foreign_subject = HealthSubject(
            owner_user_id=foreign_owner.id,
            timezone="Asia/Almaty",
        )
        db_session.add(foreign_subject)
        await db_session.flush()
        weight_subject_id = foreign_subject.id
        weight_actor_id = foreign_owner.id
    else:
        weight_subject_id = None
        weight_actor_id = identity.actor_user_id
    weight = WeightLog(
        subject_id=weight_subject_id,
        actor_user_id=weight_actor_id,
        date=DAY,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        weight_kg=79.0,
        superseded=False,
    )
    db_session.add(weight)
    await db_session.flush()
    db_session.add(
        GarminWeightExport(
            subject_id=identity.subject_id,
            integration_connection_id=garmin.id,
            requested_by_user_id=identity.actor_user_id,
            date=DAY,
            weight_log_id=weight.id,
            weight_kg=weight.weight_kg,
            measured_at=NOW,
            status="deleted",
        )
    )
    await db_session.flush()
    context = garmin_weight_contracts.GarminWeightExportContext(
        identity=identity,
        integration_connection_id=garmin.id,
        legacy_bridge=engine.LegacyConflictBridge.REJECT,
    )
    prepared = await garmin_weight_outbox.prepare_scoped_export(
        db_session,
        context=context,
        historical=True,
    )

    with pytest.raises(
        (
            garmin_weight_contracts.GarminWeightExportOwnershipError,
            weight_domain.contracts.WeightOwnershipError,
        )
    ):
        await garmin_weight_jobs.get_status_scoped(
            db_session,
            prepared=prepared,
        )


async def test_strict_scoped_opt_in_does_not_fall_back_to_global_setting(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        subject_id=identity.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    db_session.add(
        AppSetting(
            key=ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED.value,
            value=True,
        )
    )
    await db_session.flush()
    context = garmin_weight_contracts.GarminWeightExportContext(
        identity=identity,
        integration_connection_id=garmin.id,
        legacy_bridge=engine.LegacyConflictBridge.REJECT,
    )
    prepared = await garmin_weight_outbox.prepare_scoped_export(
        db_session,
        context=context,
        historical=True,
    )

    assert (
        await garmin_weight_settings.is_enabled_scoped(
            db_session,
            prepared=prepared,
        )
        is False
    )


def _empty_status() -> dict[str, object]:
    return {
        "enabled": False,
        "status": None,
        "date": None,
        "weight_kg": None,
        "exported_at": None,
        "next_attempt_at": None,
        "last_error": None,
    }


async def test_settings_status_toggle_and_send_now_use_human_owner_and_exact_garmin(
    auth_client,
    db_session,
    legacy_owner_roots,
    monkeypatch,
    garmin_connected,
):
    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        subject_id=identity.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    calls: list[tuple[str, object]] = []

    def assert_human_scope(prepared) -> None:
        context = prepared.context
        assert context.identity == identity
        assert context.identity.actor_user_id == legacy_owner_roots.user_id
        assert context.integration_connection_id == garmin.id

    async def get_status_scoped(session, *, prepared):
        assert session is db_session
        assert_human_scope(prepared)
        calls.append(("status", prepared))
        return _empty_status()

    async def set_enabled_scoped(session, enabled, *, prepared, now=None):
        del now
        assert session is db_session
        assert enabled is True
        assert_human_scope(prepared)
        calls.append(("toggle", prepared))
        return True

    async def send_now_scoped(session, *, prepared, redis=None):
        assert session is db_session
        assert redis is not None
        assert_human_scope(prepared)
        calls.append(("send", prepared))
        return {"status": "sent", "sent": True}

    async def forbidden_legacy(*args, **kwargs):
        del args, kwargs
        pytest.fail("Settings called a legacy unscoped Garmin Weight API")

    monkeypatch.setattr(
        garmin_weight_jobs,
        "get_status_scoped",
        get_status_scoped,
        raising=False,
    )
    monkeypatch.setattr(
        garmin_weight_settings,
        "set_enabled_scoped",
        set_enabled_scoped,
    )
    monkeypatch.setattr(
        garmin_weight_jobs,
        "send_now_scoped",
        send_now_scoped,
        raising=False,
    )
    monkeypatch.setattr(garmin_weight_jobs, "get_status", forbidden_legacy)
    monkeypatch.setattr(garmin_weight_settings, "set_enabled", forbidden_legacy)
    monkeypatch.setattr(garmin_weight_jobs, "send_now", forbidden_legacy)
    from web.routers import settings as settings_router

    order: list[str] = []
    original_breaker = settings_router.login_breaker_state
    original_prepare = garmin_weight_outbox.prepare_scoped_export

    async def tracked_breaker(redis, namespace=""):
        order.append("redis")
        return await original_breaker(redis)

    async def tracked_prepare(session, *, context, historical=False):
        order.append("prepare")
        return await original_prepare(
            session,
            context=context,
            historical=historical,
        )

    monkeypatch.setattr(settings_router, "login_breaker_state", tracked_breaker)
    monkeypatch.setattr(
        garmin_weight_outbox,
        "prepare_scoped_export",
        tracked_prepare,
    )

    page = await auth_client.get("/settings", headers={"Accept": "text/html"})
    assert order[:2] == ["redis", "prepare"]
    toggled = await auth_client.post(
        "/settings/garmin/weight-toggle",
        data={"enabled": "true"},
        headers={"HX-Request": "true"},
    )
    sent = await auth_client.post(
        "/settings/garmin/weight/send-now",
        headers={"HX-Request": "true"},
    )

    assert (page.status_code, toggled.status_code, sent.status_code) == (200, 200, 200)
    assert [name for name, _prepared in calls].count("status") >= 3
    assert {name for name, _prepared in calls} == {"status", "toggle", "send"}


class _ConfiguredWeightClient:
    is_configured = True

    def __init__(self) -> None:
        self.network_calls: list[str] = []

    async def fetch_daily_weigh_ins(self, on_date: date):
        self.network_calls.append(f"fetch:{on_date.isoformat()}")
        return {"dateWeightList": []}

    async def add_weigh_in(self, weight_kg: float, measured_at: datetime):
        del weight_kg, measured_at
        self.network_calls.append("add")
        return {"samplePk": "synthetic-owned-sample"}

    async def delete_weigh_in(self, sample_pk: str, on_date: date):
        del sample_pk, on_date
        self.network_calls.append("delete")


async def test_send_now_releases_db_locks_before_redis_unlock(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    garmin_connected,
):
    from vitals.scheduler import scheduler_lock

    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        subject_id=identity.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    await _enable_scoped(db_session, garmin)
    prepared = await garmin_weight_outbox.prepare_scoped_export(
        db_session,
        context=_export_context(identity, garmin),
    )
    client = _ConfiguredWeightClient()
    monkeypatch.setattr(
        GarminClient,
        "from_config",
        classmethod(lambda cls, config=None, redis=None: client),
    )

    async def leave_transaction_open(session, client, *, prepared, **kwargs):
        del client, prepared, kwargs
        assert session.in_transaction()
        return {"status": "empty", "sent": False}

    unlocked_after_db_release = False

    async def run_and_unlock(redis, name, ttl, callback, *args, **kwargs):
        nonlocal unlocked_after_db_release
        del redis, name, ttl
        result = await callback(*args, **kwargs)
        assert not db_session.in_transaction()
        unlocked_after_db_release = True
        return result

    async def ignore_token_alert(*args, **kwargs):
        del args, kwargs

    monkeypatch.setattr(
        garmin_weight_dispatch,
        "export_latest_scoped",
        leave_transaction_open,
    )
    monkeypatch.setattr(scheduler_lock, "with_scheduler_lock", run_and_unlock)
    monkeypatch.setattr(
        garmin_alerts,
        "_refresh_owned_token_cache_alert",
        ignore_token_alert,
    )

    result = await garmin_weight_jobs.send_now_scoped(
        db_session,
        prepared=prepared,
        redis=object(),
    )

    assert result == {"status": "empty", "sent": False}
    assert unlocked_after_db_release is True


async def _seed_job_candidate(
    session,
    *,
    roots,
    garmin: IntegrationConnection,
) -> WeightLog:
    await _enable_scoped(session, garmin)
    row = WeightLog(
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id,
        integration_connection_id=None,
        date=DAY,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        weight_kg=82.0,
        superseded=False,
    )
    session.add(row)
    await session.commit()
    return row


async def test_export_job_projects_with_system_requester_null(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
    garmin_connected,
):
    garmin = await _connection(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    weight = await _seed_job_candidate(
        db_session,
        roots=legacy_owner_roots,
        garmin=garmin,
    )
    client = _ConfiguredWeightClient()
    monkeypatch.setattr(
        GarminClient,
        "from_config",
        classmethod(lambda cls, config=None, redis=None: client),
    )

    async def ignore_token_alert(*args, **kwargs):
        del args, kwargs

    monkeypatch.setattr(
        garmin_alerts,
        "refresh_token_cache_alert",
        ignore_token_alert,
    )

    await garmin_weight_jobs.export_job(
        session_factory, redis=None, subject_id=legacy_owner_roots.subject_id
    )

    outbox = await db_session.scalar(
        select(GarminWeightExport).where(GarminWeightExport.date == DAY)
    )
    assert outbox is not None
    assert (
        outbox.subject_id,
        outbox.integration_connection_id,
        outbox.requested_by_user_id,
        outbox.weight_log_id,
    ) == (
        legacy_owner_roots.subject_id,
        garmin.id,
        None,
        weight.id,
    )
    assert client.network_calls


@pytest.mark.parametrize(
    "status",
    [
        IntegrationConnectionStatus.PENDING,
        IntegrationConnectionStatus.DISABLED,
    ],
)
async def test_export_job_inactive_connection_never_constructs_client_or_networks(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
    status,
):
    garmin = await _connection(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    garmin.status = status.value
    await _seed_job_candidate(
        db_session,
        roots=legacy_owner_roots,
        garmin=garmin,
    )

    def forbidden_client(*args, **kwargs):
        del args, kwargs
        pytest.fail("inactive Garmin connection constructed a vendor client")

    monkeypatch.setattr(GarminClient, "from_config", forbidden_client)

    await garmin_weight_jobs.export_job(
        session_factory, redis=None, subject_id=legacy_owner_roots.subject_id
    )

    assert await db_session.scalar(select(GarminWeightExport.id)) is None


async def test_export_job_unconfigured_client_makes_no_vendor_call(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    garmin = await _connection(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    await _seed_job_candidate(
        db_session,
        roots=legacy_owner_roots,
        garmin=garmin,
    )

    class UnconfiguredClient:
        is_configured = False

        def __getattr__(self, name):
            pytest.fail(f"unconfigured Garmin client attempted {name}")

    client = UnconfiguredClient()
    monkeypatch.setattr(
        GarminClient,
        "from_config",
        classmethod(lambda cls, config=None, redis=None: client),
    )

    await garmin_weight_jobs.export_job(
        session_factory, redis=None, subject_id=legacy_owner_roots.subject_id
    )

    assert await db_session.scalar(select(GarminWeightExport.id)) is None


def _garmin_weight_call_lines(path: Path) -> dict[str, list[int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_aliases = {"garmin_weight_service"}
    direct_imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "vitals.services":
            for name in node.names:
                if name.name == "garmin_weight_service":
                    module_aliases.add(name.asname or name.name)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module == "vitals.services.garmin_weight_service"
        ):
            direct_imports.update({name.asname or name.name: name.name for name in node.names})
        elif isinstance(node, ast.Import):
            for name in node.names:
                if name.name == "vitals.services.garmin_weight_service":
                    module_aliases.add(name.asname or name.name)

    calls: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in module_aliases
        ):
            name = node.func.attr
        elif isinstance(node.func, ast.Name) and node.func.id in direct_imports:
            name = direct_imports[node.func.id]
        else:
            continue
        calls.setdefault(name, []).append(node.lineno)
    return calls


@pytest.mark.parametrize(
    ("relative_path", "forbidden"),
    [
        (
            "vitals/services/weight/writes.py",
            {"handle_active_weight_changed", "handle_active_weight_deleted"},
        ),
        (
            "web/routers/settings.py",
            {"get_status", "set_enabled", "send_now"},
        ),
        (
            "vitals/scheduler/jobs.py",
            {
                "get_status",
                "set_enabled",
                "send_now",
                "export_latest",
                "handle_active_weight_changed",
                "handle_active_weight_deleted",
            },
        ),
    ],
)
def test_production_boundaries_do_not_call_legacy_garmin_weight_apis(
    relative_path,
    forbidden,
):
    called = _garmin_weight_call_lines(ROOT / relative_path)
    matches = {name: called[name] for name in sorted(forbidden & called.keys())}
    assert not matches, f"{relative_path} still calls legacy Garmin Weight APIs: {matches}"
