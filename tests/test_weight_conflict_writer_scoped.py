"""Focused contracts for the subject-scoped WeightLog conflict writer."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    RuleType,
    Severity,
    Source,
    UserStatus,
)
from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.models.weight import WeightLog
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine, weight_service


EVALUATION_DATE = date(2026, 8, 20)
OTHER_DATE = date(2026, 8, 19)


def _identity(legacy_owner_roots) -> WriteIdentity:
    return WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )


def _context(
    identity: WriteIdentity,
    *,
    on_date: date = EVALUATION_DATE,
    legacy_bridge: bool = False,
) -> conflict_engine.ConflictWriteContext:
    return conflict_engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=on_date,
        legacy_bridge=(
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
            if legacy_bridge
            else conflict_engine.LegacyConflictBridge.REJECT
        ),
    )


async def _prepared(
    session: AsyncSession,
    context: conflict_engine.ConflictWriteContext,
) -> weight_service.PreparedWeightWrite:
    return await weight_service.prepare_weight_write(session, context=context)


async def _legacy_prepared(
    session: AsyncSession,
    *,
    on_date: date = EVALUATION_DATE,
) -> tuple[
    conflict_engine.ConflictWriteContext,
    weight_service.PreparedWeightWrite,
]:
    context = await conflict_engine.resolve_legacy_conflict_write_context(
        session,
        actor_username="tester",
        evaluation_date=on_date,
    )
    return context, await _prepared(session, context)


async def _new_identity(session: AsyncSession, slug: str) -> WriteIdentity:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    subject = HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    return WriteIdentity(subject.id, user.id)


async def _connection(
    session: AsyncSession,
    identity: WriteIdentity,
    provider: IntegrationProvider,
) -> IntegrationConnection:
    row = await session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == identity.subject_id,
            IntegrationConnection.provider == provider.value,
        )
    )
    assert row is not None
    return row


async def _raw(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    connection: IntegrationConnection,
    domain: Domain,
    source: Source,
    external_id: str,
    file_asset: FileAsset | None = None,
) -> RawPayload:
    row = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        integration_connection_id=connection.id,
        file_asset_id=file_asset.id if file_asset is not None else None,
        domain=domain.value,
        source=source.value,
        external_id=external_id,
        payload={"synthetic": True},
    )
    session.add(row)
    await session.flush()
    return row


async def test_prepared_weight_write_is_opaque_and_immutable(
    db_session,
    legacy_owner_roots,
):
    with pytest.raises(
        conflict_engine.ConflictPreparedWriteError,
        match="issued only",
    ):
        weight_service.PreparedWeightWrite()
    prepared = await _prepared(
        db_session,
        _context(_identity(legacy_owner_roots)),
    )
    with pytest.raises(AttributeError, match="immutable"):
        prepared.context = prepared.context


async def test_scoped_create_update_note_delete_and_date_move(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    context = _context(identity)
    prepared = await _prepared(db_session, context)

    older = await weight_service.log_weight(
        db_session,
        on_date=EVALUATION_DATE,
        weight_kg=85.0,
        source=Source.MANUAL.value,
        identity=identity,
        prepared_weight_write=prepared,
    )
    newest = await weight_service.log_weight(
        db_session,
        on_date=EVALUATION_DATE,
        weight_kg=84.5,
        source=Source.MCP.value,
        identity=identity,
        prepared_weight_write=prepared,
    )
    assert older.superseded is True
    assert newest.superseded is False
    assert (newest.subject_id, newest.actor_user_id, newest.source) == (
        identity.subject_id,
        identity.actor_user_id,
        Source.MCP.value,
    )

    edited = await weight_service.update_weight_log(
        db_session,
        newest.id,
        on_date=EVALUATION_DATE,
        weight_kg=84.0,
        note="edited",
        identity=identity,
        prepared_weight_write=prepared,
    )
    assert edited is newest
    assert (newest.weight_kg, newest.note, newest.source) == (
        84.0,
        "edited",
        Source.MCP.value,
    )

    noted = await weight_service.update_weight_note(
        db_session,
        newest.id,
        note="note only",
        identity=identity,
        prepared_weight_write=prepared,
    )
    assert noted is newest
    assert (newest.note, newest.source, newest.actor_user_id) == (
        "note only",
        Source.MCP.value,
        identity.actor_user_id,
    )

    assert await weight_service.delete_weight_log(
        db_session,
        newest.id,
        identity=identity,
        prepared_weight_write=prepared,
    ) is True
    assert older.superseded is False

    move_context = _context(identity, on_date=OTHER_DATE)
    moved = await weight_service.update_weight_log(
        db_session,
        older.id,
        on_date=OTHER_DATE,
        weight_kg=83.5,
        note="moved",
        identity=identity,
        prepared_weight_write=await _prepared(db_session, move_context),
    )
    assert moved is not None
    assert moved.id != older.id
    assert (
        moved.date,
        moved.weight_kg,
        moved.note,
        moved.subject_id,
        moved.actor_user_id,
        moved.source,
    ) == (
        OTHER_DATE,
        83.5,
        "moved",
        identity.subject_id,
        identity.actor_user_id,
        Source.MANUAL.value,
    )
    assert await db_session.get(WeightLog, older.id) is None

    assert await weight_service.delete_weight_log(
        db_session,
        moved.id,
        identity=identity,
        prepared_weight_write=await _prepared(db_session, move_context),
    ) is True
    assert await db_session.scalar(select(func.count()).select_from(WeightLog)) == 0


async def test_exact_subject_reads_and_mutations_hide_foreign_rows(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    foreign_identity = await _new_identity(db_session, "foreign-weight-owner")
    owned = WeightLog(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        date=EVALUATION_DATE,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        weight_kg=85.0,
    )
    foreign = WeightLog(
        subject_id=foreign_identity.subject_id,
        actor_user_id=foreign_identity.actor_user_id,
        date=OTHER_DATE,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        weight_kg=75.0,
    )
    db_session.add_all([owned, foreign])
    await db_session.commit()

    assert [row.id for row in await weight_service.list_active_weights(
        db_session,
        subject_id=identity.subject_id,
    )] == [owned.id]
    context = _context(identity)
    prepared = await _prepared(db_session, context)
    assert await weight_service.update_weight_note(
        db_session,
        foreign.id,
        note="forged",
        identity=identity,
        prepared_weight_write=prepared,
    ) is None
    assert await weight_service.delete_weight_log(
        db_session,
        foreign.id,
        identity=identity,
        prepared_weight_write=prepared,
    ) is False
    assert foreign.note is None


async def test_fully_unowned_row_is_visible_and_adopted_only_through_bridge(
    db_session,
    legacy_owner_roots,
):
    legacy = WeightLog(
        date=EVALUATION_DATE,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        weight_kg=85.0,
    )
    db_session.add(legacy)
    await db_session.commit()
    identity = _identity(legacy_owner_roots)

    assert await weight_service.list_active_weights(
        db_session,
        subject_id=identity.subject_id,
    ) == []
    assert [row.id for row in await weight_service.list_active_weights(
        db_session,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
    )] == [legacy.id]

    context, prepared = await _legacy_prepared(db_session)
    adopted = await weight_service.update_weight_note(
        db_session,
        legacy.id,
        note="claimed safely",
        identity=context.identity,
        include_legacy_unowned=True,
        prepared_weight_write=prepared,
    )
    assert adopted is legacy
    assert (legacy.subject_id, legacy.actor_user_id, legacy.note) == (
        identity.subject_id,
        None,
        "claimed safely",
    )


async def test_partial_actor_connection_and_raw_roots_fail_closed(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        identity,
        IntegrationProvider.GARMIN,
    )
    raw = await _raw(
        db_session,
        identity=identity,
        connection=garmin,
        domain=Domain.GARMIN,
        source=Source.GARMIN_API,
        external_id="daily:2026-08-17",
    )
    partial_rows = [
        WeightLog(
            actor_user_id=identity.actor_user_id,
            date=date(2026, 8, 18),
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            weight_kg=84.0,
        ),
        WeightLog(
            integration_connection_id=garmin.id,
            date=date(2026, 8, 17),
            domain=Domain.WEIGHT.value,
            source=Source.GARMIN_API.value,
            weight_kg=83.0,
        ),
        WeightLog(
            raw_payload_id=raw.id,
            date=date(2026, 8, 16),
            domain=Domain.WEIGHT.value,
            source=Source.GARMIN_API.value,
            weight_kg=82.0,
        ),
    ]
    db_session.add_all(partial_rows)
    await db_session.commit()

    accepted_partial_roots: list[str] = []
    for root_name, row in zip(
        ("actor", "connection", "raw"),
        partial_rows,
        strict=True,
    ):
        try:
            await weight_service.get_active_weight(
                db_session,
                row.date,
                subject_id=identity.subject_id,
                include_legacy_unowned=True,
            )
        except (
            weight_service.WeightOwnershipError,
            conflict_engine.ConflictRawOwnershipError,
        ) as exc:
            if root_name == "raw":
                assert isinstance(exc, conflict_engine.ConflictRawOwnershipError)
            else:
                assert isinstance(exc, weight_service.WeightOwnershipError)
        else:
            accepted_partial_roots.append(root_name)
    assert accepted_partial_roots == []


async def test_manual_mcp_garmin_and_body_scan_provenance(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        identity,
        IntegrationProvider.GARMIN,
    )
    openrouter = await _connection(
        db_session,
        identity,
        IntegrationProvider.OPENROUTER,
    )
    scan_asset = FileAsset(
        subject_id=identity.subject_id,
        uploaded_by_user_id=identity.actor_user_id,
        purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT.value,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        storage_ref="body/synthetic-scan.png",
        status=FileAssetStatus.LEGACY_PLACEHOLDER.value,
    )
    db_session.add(scan_asset)
    await db_session.flush()
    garmin_raw = await _raw(
        db_session,
        identity=identity,
        connection=garmin,
        domain=Domain.GARMIN,
        source=Source.GARMIN_API,
        external_id="daily:2026-08-18",
    )
    scan_raw = await _raw(
        db_session,
        identity=identity,
        connection=openrouter,
        domain=Domain.BODY_COMPOSITION,
        source=Source.BODY_SCAN,
        external_id="body/synthetic-scan.png",
        file_asset=scan_asset,
    )

    async def write(
        on_date: date,
        source: Source,
        *,
        connection: IntegrationConnection | None = None,
        raw: RawPayload | None = None,
    ) -> WeightLog:
        context = _context(identity, on_date=on_date)
        return await weight_service.log_weight(
            db_session,
            on_date=on_date,
            weight_kg=80.0 + on_date.day / 100,
            source=source.value,
            raw_payload_id=raw.id if raw is not None else None,
            identity=identity,
            integration_connection_id=(
                connection.id if connection is not None else None
            ),
            prepared_weight_write=await _prepared(db_session, context),
        )

    manual = await write(date(2026, 8, 20), Source.MANUAL)
    mcp = await write(date(2026, 8, 19), Source.MCP)
    garmin_row = await write(
        date(2026, 8, 18),
        Source.GARMIN_API,
        connection=garmin,
        raw=garmin_raw,
    )
    scan_row = await write(
        date(2026, 8, 17),
        Source.BODY_SCAN,
        connection=openrouter,
        raw=scan_raw,
    )

    for row, source in (
        (manual, Source.MANUAL),
        (mcp, Source.MCP),
        (garmin_row, Source.GARMIN_API),
        (scan_row, Source.BODY_SCAN),
    ):
        assert (row.subject_id, row.actor_user_id, row.source) == (
            identity.subject_id,
            identity.actor_user_id,
            source.value,
        )
    assert (manual.integration_connection_id, manual.raw_payload_id) == (None, None)
    assert (mcp.integration_connection_id, mcp.raw_payload_id) == (None, None)
    assert (garmin_row.integration_connection_id, garmin_row.raw_payload_id) == (
        garmin.id,
        garmin_raw.id,
    )
    assert (scan_row.integration_connection_id, scan_row.raw_payload_id) == (
        openrouter.id,
        scan_raw.id,
    )


@pytest.mark.parametrize(
    ("source", "provider"),
    [
        (Source.GARMIN_API, IntegrationProvider.GARMIN),
        (Source.BODY_SCAN, IntegrationProvider.OPENROUTER),
    ],
)
async def test_provider_weight_without_raw_payload_is_rejected_write_free(
    db_session,
    legacy_owner_roots,
    source,
    provider,
):
    identity = _identity(legacy_owner_roots)
    connection = await _connection(db_session, identity, provider)
    before_weights = await db_session.scalar(
        select(func.count()).select_from(WeightLog)
    )
    before_alerts = await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    )

    with pytest.raises(
        conflict_engine.ConflictRawOwnershipError,
        match="raw",
    ):
        await weight_service.log_weight(
            db_session,
            on_date=EVALUATION_DATE,
            weight_kg=85.0,
            source=source.value,
            identity=identity,
            integration_connection_id=connection.id,
            raw_payload_id=None,
            prepared_weight_write=await _prepared(
                db_session,
                _context(identity),
            ),
        )

    assert await db_session.scalar(
        select(func.count()).select_from(WeightLog)
    ) == before_weights
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == before_alerts


async def test_garmin_rejects_strict_weight_capability_before_provider_mutation(
    db_session,
    legacy_owner_roots,
):
    from vitals.models.garmin import GarminDaily
    from vitals.services import garmin_service

    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        identity,
        IntegrationProvider.GARMIN,
    )
    prepared = await _prepared(db_session, _context(identity))

    with pytest.raises(
        garmin_service.GarminOwnershipValidationError,
        match="fully-unowned",
    ):
        await garmin_service.ingest_owned_daily(
            db_session,
            EVALUATION_DATE,
            {"summary": {"totalSteps": 12, "weight": 85000}},
            identity=identity,
            integration_connection_id=garmin.id,
            prepared_weight_write=prepared,
        )

    assert await db_session.scalar(
        select(func.count()).select_from(GarminDaily)
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(RawPayload)
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(WeightLog)
    ) == 0


@pytest.mark.parametrize(
    ("mismatch", "expected_error"),
    [
        ("weight_raw_connection", conflict_engine.ConflictRawOwnershipError),
        ("weight_raw_actor", conflict_engine.ConflictRawOwnershipError),
        ("weight_raw_source", conflict_engine.ConflictRawOwnershipError),
        ("raw_domain", conflict_engine.ConflictRawOwnershipError),
        ("wrong_provider", weight_service.WeightOwnershipError),
        ("wrong_connection_type", weight_service.WeightOwnershipError),
    ],
)
async def test_persisted_exact_subject_provenance_mismatch_fails_closed_everywhere(
    db_session,
    legacy_owner_roots,
    mismatch,
    expected_error,
):
    """A matching S never makes inconsistent C/A/source/raw roots trustworthy."""

    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        identity,
        IntegrationProvider.GARMIN,
    )
    openrouter = await _connection(
        db_session,
        identity,
        IntegrationProvider.OPENROUTER,
    )
    connection = garmin
    raw_connection = garmin
    row_actor = identity.actor_user_id
    raw_actor = identity.actor_user_id
    row_source = Source.GARMIN_API.value
    raw_source = Source.GARMIN_API
    raw_domain = Domain.GARMIN
    raw_required = True

    if mismatch == "weight_raw_connection":
        raw_connection = openrouter
    elif mismatch == "weight_raw_actor":
        raw_actor = None
    elif mismatch == "weight_raw_source":
        raw_source = Source.BODY_SCAN
    elif mismatch == "raw_domain":
        raw_domain = Domain.BODY_COMPOSITION
    elif mismatch == "wrong_provider":
        connection = openrouter
        raw_required = False
    else:
        connection = IntegrationConnection(
            subject_id=identity.subject_id,
            provider=IntegrationProvider.GARMIN.value,
            connection_type=IntegrationConnectionType.IMPORT.value,
            external_account_discriminator="synthetic-weight-import",
            status=IntegrationConnectionStatus.ACTIVE.value,
        )
        db_session.add(connection)
        await db_session.flush()
        raw_connection = connection

    raw = None
    if raw_required:
        raw = await _raw(
            db_session,
            identity=WriteIdentity(identity.subject_id, raw_actor),
            connection=raw_connection,
            domain=raw_domain,
            source=raw_source,
            external_id=f"mismatch:{mismatch}",
        )
    row = WeightLog(
        subject_id=identity.subject_id,
        actor_user_id=row_actor,
        integration_connection_id=connection.id,
        raw_payload_id=raw.id if raw is not None else None,
        date=EVALUATION_DATE,
        domain=Domain.WEIGHT.value,
        source=row_source,
        weight_kg=85.0,
        note="unchanged",
    )
    db_session.add(row)
    await db_session.flush()

    with pytest.raises(expected_error):
        await weight_service.resolve_active_scoped(
            db_session,
            scope=_context(identity).scope,
        )
    with pytest.raises(expected_error):
        await weight_service.get_active_weight(
            db_session,
            EVALUATION_DATE,
            subject_id=identity.subject_id,
        )
    with pytest.raises(expected_error):
        await weight_service.update_weight_note(
            db_session,
            row.id,
            note="must not be written",
            identity=identity,
            prepared_weight_write=await _prepared(
                db_session,
                _context(identity),
            ),
        )
    assert row.note == "unchanged"


async def _unsafe_reactivation_fixture(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
) -> tuple[WeightLog, WeightLog, ConflictRule]:
    garmin = await _connection(
        session,
        identity,
        IntegrationProvider.GARMIN,
    )
    raw = await _raw(
        session,
        identity=identity,
        connection=garmin,
        domain=Domain.GARMIN,
        source=Source.GARMIN_API,
        external_id=f"daily:{EVALUATION_DATE.isoformat()}",
    )
    unsafe = await weight_service.log_weight(
        session,
        on_date=EVALUATION_DATE,
        weight_kg=90.0,
        source=Source.GARMIN_API.value,
        raw_payload_id=raw.id,
        identity=identity,
        integration_connection_id=garmin.id,
        prepared_weight_write=await _prepared(
            session,
            _context(identity),
        ),
    )
    active = await weight_service.log_weight(
        session,
        on_date=EVALUATION_DATE,
        weight_kg=80.0,
        source=Source.MANUAL.value,
        identity=identity,
        prepared_weight_write=await _prepared(
            session,
            _context(identity),
        ),
    )
    rule = ConflictRule(
        subject_id=identity.subject_id,
        rule_type=RuleType.HARD_BLOCK.value,
        domain_a=Domain.LABS.value,
        condition_a={"marker": "synthetic-risk"},
        domain_b=Domain.WEIGHT.value,
        condition_b={"weight_kg": 90.0},
        severity=Severity.BLOCK.value,
        message="Synthetic unsafe replacement.",
        active=True,
    )
    session.add(rule)
    await session.flush()

    async def labs(_session, *, scope):
        del _session, scope
        return [{"marker": "synthetic-risk"}]

    conflict_engine.register_domain_resolver(Domain.LABS.value, labs)
    conflict_engine.register_domain_resolver(
        Domain.WEIGHT.value,
        weight_service.resolve_active_scoped,
    )
    assert unsafe.superseded is True
    assert active.superseded is False
    return active, unsafe, rule


async def test_delete_does_not_reactivate_hard_conflicting_replacement(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    active, unsafe, rule = await _unsafe_reactivation_fixture(
        db_session,
        identity=identity,
    )
    before_alerts = await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    )

    assert await weight_service.delete_weight_log(
        db_session,
        active.id,
        identity=identity,
        prepared_weight_write=await _prepared(
            db_session,
            _context(identity),
        ),
    ) is True

    assert await db_session.get(WeightLog, active.id) is None
    assert unsafe.superseded is True
    assert await weight_service.get_active_weight(
        db_session,
        EVALUATION_DATE,
        subject_id=identity.subject_id,
    ) is None
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == before_alerts
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert).where(
            SystemAlert.alert_key == f"conflict:{rule.id}"
        )
    ) == 0


async def test_date_move_does_not_reactivate_hard_conflict_on_old_date(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    active, unsafe, rule = await _unsafe_reactivation_fixture(
        db_session,
        identity=identity,
    )
    before_alerts = await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    )

    moved = await weight_service.update_weight_log(
        db_session,
        active.id,
        on_date=OTHER_DATE,
        weight_kg=80.0,
        note="moved safely",
        identity=identity,
        prepared_weight_write=await _prepared(
            db_session,
            _context(identity, on_date=OTHER_DATE),
        ),
    )

    assert moved is not None
    assert (moved.date, moved.superseded) == (OTHER_DATE, False)
    assert await db_session.get(WeightLog, active.id) is None
    assert unsafe.superseded is True
    assert await weight_service.get_active_weight(
        db_session,
        EVALUATION_DATE,
        subject_id=identity.subject_id,
    ) is None
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == before_alerts
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert).where(
            SystemAlert.alert_key == f"conflict:{rule.id}"
        )
    ) == 0


async def test_resolver_replacement_excludes_the_edited_weight(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    context = _context(identity)
    row = await weight_service.log_weight(
        db_session,
        on_date=EVALUATION_DATE,
        weight_kg=90.0,
        identity=identity,
        prepared_weight_write=await _prepared(db_session, context),
    )
    await db_session.commit()
    rule = ConflictRule(
        subject_id=identity.subject_id,
        rule_type=RuleType.HARD_BLOCK.value,
        domain_a=Domain.LABS.value,
        condition_a={"marker": "synthetic-risk"},
        domain_b=Domain.WEIGHT.value,
        condition_b={"weight_kg": 90.0},
        severity=Severity.BLOCK.value,
        message="Synthetic weight replacement conflict.",
        active=True,
    )
    db_session.add(rule)
    await db_session.commit()

    async def labs(session, *, scope):
        del session, scope
        return [{"marker": "synthetic-risk"}]

    conflict_engine.register_domain_resolver(Domain.LABS.value, labs)
    conflict_engine.register_domain_resolver(
        Domain.WEIGHT.value,
        weight_service.resolve_active_scoped,
    )

    updated = await weight_service.update_weight_log(
        db_session,
        row.id,
        on_date=EVALUATION_DATE,
        weight_kg=80.0,
        identity=identity,
        prepared_weight_write=await _prepared(db_session, context),
    )
    assert updated is row
    assert row.weight_kg == 80.0

    with pytest.raises(conflict_engine.ConflictBlocked):
        await weight_service.update_weight_log(
            db_session,
            row.id,
            on_date=EVALUATION_DATE,
            weight_kg=90.0,
            identity=identity,
            prepared_weight_write=await _prepared(db_session, context),
        )
    assert row.weight_kg == 80.0


async def test_wrong_session_transaction_identity_and_date_are_rejected(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    context = _context(identity)
    prepared = await _prepared(db_session, context)

    with pytest.raises(conflict_engine.ConflictPreparedWriteError, match="date"):
        await weight_service.log_weight(
            db_session,
            on_date=OTHER_DATE,
            weight_kg=85.0,
            identity=identity,
            prepared_weight_write=prepared,
        )
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await weight_service.log_weight(
            db_session,
            on_date=EVALUATION_DATE,
            weight_kg=85.0,
            identity=WriteIdentity(identity.subject_id, uuid.uuid4()),
            prepared_weight_write=prepared,
        )

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    async with factory() as other_session:
        with pytest.raises(
            conflict_engine.ConflictPreparedWriteError,
            match="session",
        ):
            await weight_service.log_weight(
                other_session,
                on_date=EVALUATION_DATE,
                weight_kg=85.0,
                identity=identity,
                prepared_weight_write=prepared,
            )

    await db_session.commit()
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await weight_service.log_weight(
            db_session,
            on_date=EVALUATION_DATE,
            weight_kg=85.0,
            identity=identity,
            prepared_weight_write=prepared,
        )
    assert await db_session.scalar(select(func.count()).select_from(WeightLog)) == 0


@pytest.mark.integration
async def test_postgres_owned_reparse_candidate_does_not_lock_raw_before_prepare(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    """Candidate discovery must leave raw free for callback lock preparation."""

    from vitals.services import garmin_service

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    identity = _identity(legacy_owner_roots)
    garmin = await _connection(
        db_session,
        identity,
        IntegrationProvider.GARMIN,
    )
    raw = await _raw(
        db_session,
        identity=identity,
        connection=garmin,
        domain=Domain.GARMIN,
        source=Source.GARMIN_API,
        external_id=f"daily:{EVALUATION_DATE.isoformat()}",
    )
    raw_id = raw.id
    connection_id = garmin.id
    await db_session.commit()

    probe_acquired_raw = False

    class _PreparationProbeComplete(Exception):
        pass

    async def probe_prepare(
        session,
        *,
        context,
        garmin_weight_export_context=None,
    ):
        nonlocal probe_acquired_raw
        del session, context, garmin_weight_export_context
        async with factory() as probe:
            locked = await probe.scalar(
                select(RawPayload)
                .where(RawPayload.id == raw_id)
                .with_for_update(nowait=True)
            )
            assert locked is not None
            probe_acquired_raw = True
            await probe.rollback()
        raise _PreparationProbeComplete

    monkeypatch.setattr(weight_service, "prepare_weight_write", probe_prepare)

    assert await garmin_service.reparse_owned_pending(
        db_session,
        identity=identity,
        integration_connection_id=connection_id,
    ) == 0
    assert probe_acquired_raw is True
    persisted = await db_session.get(RawPayload, raw_id)
    assert persisted is not None
    assert persisted.processed_at is None


@pytest.mark.integration
async def test_postgres_prepare_order_and_concurrent_same_day_writes_serialize(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    """Governance precedes outbox advisory and concurrent writers do not deadlock."""

    from vitals.services import garmin_weight_service

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    identity = _identity(legacy_owner_roots)
    context = _context(identity)
    await db_session.commit()

    order: dict[int, list[str]] = {}
    original_governance = weight_service.acquire_identity_governance_lock
    original_outbox = garmin_weight_service.lock_active_weight_change

    async def governance(session):
        order.setdefault(id(session), []).append("governance")
        await original_governance(session)

    async def outbox(session):
        order.setdefault(id(session), []).append("outbox")
        await original_outbox(session)

    monkeypatch.setattr(
        weight_service,
        "acquire_identity_governance_lock",
        governance,
    )
    monkeypatch.setattr(
        garmin_weight_service,
        "lock_active_weight_change",
        outbox,
    )

    async def create(weight_kg: float) -> None:
        async with factory() as session:
            await weight_service.log_weight(
                session,
                on_date=EVALUATION_DATE,
                weight_kg=weight_kg,
                identity=identity,
                prepared_weight_write=await _prepared(session, context),
            )
            await session.commit()

    await asyncio.wait_for(
        asyncio.gather(create(85.0), create(84.5)),
        timeout=10,
    )

    assert len(order) == 2
    assert all(events[:2] == ["governance", "outbox"] for events in order.values())
    async with factory() as verify:
        rows = list(
            await verify.scalars(
                select(WeightLog)
                .where(WeightLog.subject_id == identity.subject_id)
                .order_by(WeightLog.id)
            )
        )
    assert len(rows) == 2
    assert sum(not row.superseded for row in rows) == 1
