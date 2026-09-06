"""Migrated BIA/weight history stays readable after another account joins."""

from datetime import UTC, date, datetime
import os

import pytest
from sqlalchemy import select

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.models.body_scan import BodyScan, BodyScanMetric
from vitals.models.identity import HealthSubject, User
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.models.weight import WeightLog
from vitals.ownership_transition.bridges import BodyScanOwnershipBackfillStateError
from vitals.services.body_scan.scans import ingestion
from vitals.services.body_scan.scans import queries as scans
from vitals.services.conflicts import engine
from vitals.services.modules import preferences as modules
from vitals.services.weight import logs as weights


DAY = date(2026, 8, 20)
BODY_PHASE = "stage3.file_backed.body_scans.v1.body_scans"
WEIGHT_PHASE = "stage3.channel_optional.weight_logs.v1.weight_logs"


def _checkpoint(phase, subject_id, row_id):
    stamp = datetime(2026, 8, 21, tzinfo=UTC)
    return OwnershipBackfillCheckpoint(
        phase_key=phase,
        subject_id=subject_id,
        status="completed",
        scan_high_watermark_id=row_id,
        snapshot_rows=1,
        last_scanned_id=row_id,
        scanned_rows=1,
        updated_rows=1,
        unchanged_rows=0,
        data_checksum_before="b" * 64,
        data_checksum_after="b" * 64,
        ownership_checksum_after="c" * 64,
        started_at=stamp,
        updated_at=stamp,
        completed_at=stamp,
    )


async def _seed_historical_graph(db_session, subject_id, source):
    connection = None
    if source == Source.BODY_SCAN.value:
        connection = IntegrationConnection(
            subject_id=subject_id,
            provider=IntegrationProvider.OPENROUTER.value,
            connection_type=IntegrationConnectionType.AI_GATEWAY.value,
            external_account_discriminator="synthetic-historical-bia",
            status=IntegrationConnectionStatus.DISABLED.value,
        )
        db_session.add(connection)
        await db_session.flush()
    raw = RawPayload(
        subject_id=subject_id,
        actor_user_id=None,
        integration_connection_id=connection.id if connection else None,
        file_asset_id=None,
        domain=Domain.BODY_COMPOSITION.value,
        source=source,
        external_id="synthetic-historical-bia",
        payload={"synthetic": True, "weight": 82.0},
    )
    db_session.add(raw)
    await db_session.flush()
    scan = BodyScan(
        subject_id=subject_id,
        actor_user_id=None,
        date=DAY,
        domain=Domain.BODY_COMPOSITION.value,
        source=source,
        raw_payload_id=raw.id,
        note="Historical note must remain unchanged",
    )
    scan.metrics.extend([
        BodyScanMetric(
            subject_id=subject_id, metric_key="weight", label="Weight",
            value=82.0, unit="kg",
        ),
        BodyScanMetric(
            subject_id=subject_id, metric_key="body_fat_pct", label="Body fat",
            value=22.0, unit="%",
        ),
    ])
    weight = WeightLog(
        subject_id=subject_id,
        actor_user_id=None,
        date=DAY,
        domain=Domain.WEIGHT.value,
        source=Source.BODY_SCAN.value,
        raw_payload_id=raw.id,
        weight_kg=82.0,
        note="Historical weight must remain unchanged",
    )
    db_session.add_all([scan, weight])
    await db_session.flush()
    db_session.add_all([
        _checkpoint(BODY_PHASE, subject_id, scan.id),
        _checkpoint(WEIGHT_PHASE, subject_id, weight.id),
    ])
    await modules.set_module_enabled(
        db_session, key="body_comp", enabled=True, subject_id=subject_id,
    )
    other = User(
        username="historical-reader-other",
        normalized_username="historical-reader-other",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(other)
    await db_session.flush()
    other_subject = HealthSubject(
        owner_user_id=other.id, display_name="Synthetic other owner",
        timezone="Asia/Almaty",
    )
    db_session.add(other_subject)
    await db_session.commit()
    return subject_id, other_subject.id, scan, weight, raw, connection


@pytest.fixture(params=[Source.BODY_SCAN.value, Source.MCP.value])
async def historical_graph(db_session, legacy_owner_roots, request):
    return await _seed_historical_graph(
        db_session, legacy_owner_roots.subject_id, request.param,
    )


async def test_reviewed_historical_graph_remains_scoped_and_unchanged(
    db_session, historical_graph,
):
    owner, other, scan, weight, raw, _ = historical_graph
    before = (scan.note, weight.note, raw.payload.copy(), raw.processed_at)
    assert [row.id for row in await scans.list_scans(
        db_session, subject_id=owner,
    )] == [scan.id]
    assert await scans.bia_chart_points(db_session, subject_id=owner) == {
        "bf": [{"date": DAY.isoformat(), "value": 22.0}],
        "lbm": [{"date": DAY.isoformat(), "value": 63.96}],
    }
    assert {row["value"] for row in await scans.available_metrics(
        db_session, subject_id=owner,
    )} == {"weight", "body_fat_pct"}
    assert [row.id for row in await weights.list_active_weights(
        db_session, subject_id=owner,
    )] == [weight.id]
    assert await scans.list_scans(db_session, subject_id=other) == []
    assert await scans.get_scan(db_session, scan.id, subject_id=other) is None
    assert await weights.list_active_weights(db_session, subject_id=other) == []
    await db_session.refresh(raw)
    await db_session.refresh(weight)
    assert (scan.note, weight.note, raw.payload, raw.processed_at) == before
    assert not db_session.new and not db_session.dirty and not db_session.deleted


@pytest.mark.parametrize("path", ["/weight", "/weight/measures", "/charts"])
async def test_historical_body_composition_pages_open_after_registration(
    auth_client, historical_graph, path,
):
    response = await auth_client.get(path, headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "Internal Server Error" not in response.text


@pytest.mark.parametrize("phase,reader", [
    (BODY_PHASE, scans.list_scans), (WEIGHT_PHASE, weights.list_active_weights),
])
async def test_shared_history_still_requires_its_migration_evidence(
    db_session, historical_graph, phase, reader,
):
    owner, _, _, _, _, _ = historical_graph
    checkpoint = await db_session.scalar(select(OwnershipBackfillCheckpoint).where(
        OwnershipBackfillCheckpoint.phase_key == phase,
    ))
    await db_session.delete(checkpoint)
    await db_session.commit()
    with pytest.raises(engine.ConflictRawOwnershipError):
        await reader(db_session, subject_id=owner)


async def test_reviewed_prefix_does_not_authorize_a_new_scan(
    db_session, historical_graph,
):
    owner, _, scan, _, raw, _ = historical_graph
    later = BodyScan(
        subject_id=owner, date=DAY, domain=Domain.BODY_COMPOSITION.value,
        source=scan.source, raw_payload_id=raw.id,
    )
    db_session.add(later)
    await db_session.commit()
    assert later.id > scan.id
    with pytest.raises(engine.ConflictRawOwnershipError):
        await scans.get_scan(db_session, later.id, subject_id=owner)


async def test_reviewed_reads_do_not_reopen_unbound_legacy_ingestion(
    db_session, historical_graph,
):
    owner, _, scan, _, raw, _ = historical_graph
    with pytest.raises(engine.ConflictRawOwnershipError):
        if scan.source == Source.BODY_SCAN.value:
            await ingestion._lock_historical_parser_connection_before_raw(
                db_session, raw_payload_id=raw.id, subject_id=owner,
                allow_historical_parser_raw=True, for_update=True,
            )
        else:
            await ingestion._historical_mcp_raw_before_lock(
                db_session, raw_payload_id=raw.id, subject_id=owner,
                allow_historical_mcp_raw=True,
            )
    with pytest.raises(engine.ConflictRawOwnershipError):
        await weights._validate_historical_provider_raw(
            db_session, raw=raw, subject_id=owner,
            fact_source=Source.BODY_SCAN.value, for_update=True,
        )


@pytest.mark.parametrize("phase,reader", [
    (BODY_PHASE, scans.list_scans), (WEIGHT_PHASE, weights.list_active_weights),
])
async def test_divergent_migration_evidence_still_fails_closed(
    db_session, historical_graph, phase, reader,
):
    owner, _, _, _, _, _ = historical_graph
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    checkpoint.data_checksum_after = "d" * 64
    await db_session.commit()
    with pytest.raises((
        engine.ConflictRawOwnershipError, BodyScanOwnershipBackfillStateError,
    )):
        await reader(db_session, subject_id=owner)


async def test_reviewed_history_still_validates_raw_source(
    db_session, historical_graph,
):
    owner, _, _, _, raw, _ = historical_graph
    raw.source = Source.GARMIN_API.value
    await db_session.commit()
    with pytest.raises(engine.ConflictRawOwnershipError):
        await scans.list_scans(db_session, subject_id=owner)


async def test_reviewed_scan_cannot_lend_its_checkpoint_to_another_raw(
    db_session, historical_graph,
):
    owner, _, scan, _, raw, _ = historical_graph
    another_raw = RawPayload(
        subject_id=owner, actor_user_id=None,
        integration_connection_id=raw.integration_connection_id,
        domain=raw.domain, source=raw.source, external_id="synthetic-new-raw",
        payload={"synthetic": True},
    )
    db_session.add(another_raw)
    await db_session.commit()
    assert not await ingestion._is_reviewed_historical_scan_raw(
        db_session, scan_id=scan.id, raw_payload_id=another_raw.id,
        subject_id=owner,
    )


async def test_reviewed_prefix_does_not_authorize_a_new_weight(
    db_session, historical_graph,
):
    owner, _, _, weight, raw, _ = historical_graph
    later = WeightLog(
        subject_id=owner, date=date(2026, 8, 22), domain=Domain.WEIGHT.value,
        source=weight.source, raw_payload_id=raw.id, weight_kg=81.0,
    )
    db_session.add(later)
    await db_session.commit()
    assert later.id > weight.id
    with pytest.raises(engine.ConflictRawOwnershipError):
        await weights.list_active_weights(db_session, subject_id=owner)


async def test_reviewed_garmin_weight_keeps_its_exact_provider_boundary(
    db_session, historical_graph,
):
    owner, _, _, weight, raw, connection = historical_graph
    if connection is None:
        connection = IntegrationConnection(
            subject_id=owner, external_account_discriminator="synthetic-garmin",
            status=IntegrationConnectionStatus.DISABLED.value,
        )
        db_session.add(connection)
    connection.provider = IntegrationProvider.GARMIN.value
    connection.connection_type = IntegrationConnectionType.ACCOUNT.value
    await db_session.flush()
    raw.integration_connection_id = connection.id
    raw.source = Source.GARMIN_API.value
    raw.domain = Domain.GARMIN.value
    weight.source = Source.GARMIN_API.value
    await db_session.commit()
    assert [row.id for row in await weights.list_active_weights(
        db_session, subject_id=owner,
    )] == [weight.id]
    with pytest.raises(engine.ConflictRawOwnershipError):
        await weights._validate_historical_provider_raw(
            db_session, raw=raw, subject_id=owner,
            fact_source=Source.GARMIN_API.value, for_update=True,
        )
    connection.provider = IntegrationProvider.HEVY.value
    await db_session.commit()
    with pytest.raises(engine.ConflictRawOwnershipError):
        await weights.list_active_weights(db_session, subject_id=owner)


@pytest.mark.integration
@pytest.mark.parametrize("source", [Source.BODY_SCAN.value, Source.MCP.value])
async def test_migrated_history_reads_under_restricted_postgres_rls(
    db_session, monkeypatch, source,
):
    from alembic.config import Config as AlembicConfig
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from tests.test_row_level_security import (
        REPOSITORY_ROOT, _migrated_engine, restricted_engine,
    )
    from vitals.persistence.rls import bind_session_subject

    database_url = os.environ["VITALS_TEST_DATABASE_URL"]
    assert database_url.startswith("postgresql")
    monkeypatch.setenv("VITALS_DATABASE_URL", database_url)
    await db_session.close()
    admin = await _migrated_engine(
        database_url, AlembicConfig(str(REPOSITORY_ROOT / "alembic.ini")),
    )
    restricted = await restricted_engine(database_url)
    try:
        async with async_sessionmaker(admin, expire_on_commit=False)() as session:
            owner = await session.scalar(select(HealthSubject.id).join(User).where(
                User.username == "rls-seed-owner",
            ))
            _, other, scan, weight, _, _ = await _seed_historical_graph(
                session, owner, source,
            )
        sessions = async_sessionmaker(restricted, expire_on_commit=False)
        async with sessions() as session:
            await bind_session_subject(session, owner)
            assert [row.id for row in await scans.list_scans(
                session, subject_id=owner,
            )] == [scan.id]
            assert [row.id for row in await weights.list_active_weights(
                session, subject_id=owner,
            )] == [weight.id]
            assert len(await scans.available_metrics(session, subject_id=owner)) == 2
            assert await scans.list_scans(session, subject_id=other) == []
            assert await weights.list_active_weights(session, subject_id=other) == []
            assert not session.new and not session.dirty and not session.deleted
        for bound in (other, None):
            async with sessions() as session:
                if bound is not None:
                    await bind_session_subject(session, bound)
                assert await scans.list_scans(session, subject_id=owner) == []
                assert await scans.get_scan(session, scan.id, subject_id=owner) is None
                assert await weights.list_active_weights(session, subject_id=owner) == []
    finally:
        await restricted.dispose()
        await admin.dispose()
