"""Ownership and capability contracts for the scoped Garmin Weight outbox."""

from __future__ import annotations

from datetime import date, datetime, timedelta

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
from vitals.models.app_settings import AppSetting
from vitals.models.garmin import WEIGHT_EXPORT_DELETED, GarminWeightExport
from vitals.models.identity import HealthSubject, User
from vitals.models.scoped_settings import IntegrationConnectionSetting
from vitals.models.tenancy import IntegrationConnection
from vitals.models.weight import WeightLog
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine, garmin_weight_service
from vitals.services.scoped_settings_service import ScopedSettingKey


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader does when the ownership backfill has not reached a row yet, which is
# a state the application itself can no longer create. The schema says so, so
# this module asks for the one that stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract


DAY = date(2026, 8, 20)
NOW = datetime(2026, 8, 20, 9, 30)


async def _garmin_connection(session, subject_id):
    return await session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == subject_id,
            IntegrationConnection.provider == IntegrationProvider.GARMIN.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.ACCOUNT.value,
        )
    )


async def _context(
    session,
    legacy_owner_roots,
    *,
    bridge: bool = False,
    system: bool = False,
):
    connection = await _garmin_connection(
        session,
        legacy_owner_roots.subject_id,
    )
    assert connection is not None
    return garmin_weight_service.GarminWeightExportContext(
        identity=WriteIdentity(
            legacy_owner_roots.subject_id,
            None if system else legacy_owner_roots.user_id,
        ),
        integration_connection_id=connection.id,
        legacy_bridge=(
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
            if bridge
            else conflict_engine.LegacyConflictBridge.REJECT
        ),
    )


async def _weight(
    session,
    *,
    identity: WriteIdentity,
    on_date: date = DAY,
    value: float = 84.5,
):
    row = WeightLog(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        date=on_date,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        weight_kg=value,
    )
    session.add(row)
    await session.flush()
    return row


async def _enable_direct(session, connection_id):
    session.add(
        IntegrationConnectionSetting(
            integration_connection_id=connection_id,
            key=ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED.value,
            value=True,
        )
    )
    await session.flush()


async def _second_graph(session, slug: str):
    owner = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(
        owner_user_id=owner.id,
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    connection = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.GARMIN.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator=f"synthetic-{slug}",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    session.add(connection)
    await session.flush()
    return WriteIdentity(subject.id, owner.id), connection


async def test_capability_is_opaque_session_and_transaction_bound(
    db_session,
    legacy_owner_roots,
):
    context = await _context(db_session, legacy_owner_roots)

    with pytest.raises(garmin_weight_service.GarminWeightExportPreparedError):
        garmin_weight_service.PreparedGarminWeightExport()

    prepared = await garmin_weight_service.prepare_scoped_export(
        db_session,
        context=context,
    )
    await db_session.commit()
    with pytest.raises(garmin_weight_service.GarminWeightExportPreparedError):
        await garmin_weight_service.get_status_scoped(
            db_session,
            prepared=prepared,
        )


async def test_scoped_enable_projects_exact_roots_and_requester(
    db_session,
    legacy_owner_roots,
):
    context = await _context(db_session, legacy_owner_roots, bridge=True)
    weight = await _weight(db_session, identity=context.identity)
    prepared = await garmin_weight_service.prepare_scoped_export(
        db_session,
        context=context,
    )

    assert await garmin_weight_service.set_enabled_scoped(
        db_session,
        True,
        prepared=prepared,
        now=NOW,
    ) is True

    outbox = await db_session.scalar(select(GarminWeightExport))
    assert outbox is not None
    assert (
        outbox.subject_id,
        outbox.integration_connection_id,
        outbox.requested_by_user_id,
        outbox.weight_log_id,
    ) == (
        context.identity.subject_id,
        context.integration_connection_id,
        context.identity.actor_user_id,
        weight.id,
    )
    scoped = await db_session.get(
        IntegrationConnectionSetting,
        (
            context.integration_connection_id,
            ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED.value,
        ),
    )
    legacy = await db_session.get(
        AppSetting,
        ScopedSettingKey.GARMIN_WEIGHT_EXPORT_ENABLED.value,
    )
    assert scoped is not None and scoped.value is True
    assert legacy is not None and legacy.value is True


async def test_reconcile_ignores_foreign_newer_weight(
    db_session,
    legacy_owner_roots,
):
    context = await _context(db_session, legacy_owner_roots)
    local = await _weight(
        db_session,
        identity=context.identity,
        on_date=DAY - timedelta(days=1),
        value=84.5,
    )
    foreign_identity, _foreign_connection = await _second_graph(
        db_session,
        "foreign-weight-owner",
    )
    await _weight(
        db_session,
        identity=foreign_identity,
        on_date=DAY,
        value=63.0,
    )
    await _enable_direct(db_session, context.integration_connection_id)
    prepared = await garmin_weight_service.prepare_scoped_export(
        db_session,
        context=context,
    )

    outbox = await garmin_weight_service.reconcile_latest_scoped(
        db_session,
        prepared=prepared,
        now=NOW,
        max_age_days=7,
    )

    assert outbox is not None
    assert (outbox.weight_log_id, outbox.weight_kg, outbox.subject_id) == (
        local.id,
        local.weight_kg,
        context.identity.subject_id,
    )


async def test_another_accounts_intent_for_the_same_date_is_left_alone(
    db_session,
    legacy_owner_roots,
):
    context = await _context(db_session, legacy_owner_roots)
    local = await _weight(db_session, identity=context.identity)
    foreign_identity, foreign_connection = await _second_graph(
        db_session,
        "foreign-outbox-owner",
    )
    foreign = GarminWeightExport(
        subject_id=foreign_identity.subject_id,
        integration_connection_id=foreign_connection.id,
        requested_by_user_id=foreign_identity.actor_user_id,
        date=DAY,
        weight_kg=61.0,
        measured_at=NOW,
        status=WEIGHT_EXPORT_DELETED,
        remote_sample_pk="foreign-token",
        remote_owned=True,
    )
    db_session.add(foreign)
    await _enable_direct(db_session, context.integration_connection_id)
    await db_session.flush()
    prepared = await garmin_weight_service.prepare_scoped_export(
        db_session,
        context=context,
    )

    # One queued export per destination account per date: the other account's
    # intent is neither read into this reconciliation nor changed by it.
    mine = await garmin_weight_service.reconcile_latest_scoped(
        db_session,
        prepared=prepared,
        now=NOW,
    )
    assert mine is not None
    assert mine.integration_connection_id == context.integration_connection_id
    assert mine.id != foreign.id

    assert local.subject_id == context.identity.subject_id
    assert (
        foreign.subject_id,
        foreign.integration_connection_id,
        foreign.remote_sample_pk,
        foreign.remote_owned,
    ) == (
        foreign_identity.subject_id,
        foreign_connection.id,
        "foreign-token",
        True,
    )


async def test_partial_outbox_roots_fail_closed(
    db_session,
    legacy_owner_roots,
):
    context = await _context(db_session, legacy_owner_roots)
    await _enable_direct(db_session, context.integration_connection_id)
    db_session.add(
        GarminWeightExport(
            subject_id=context.identity.subject_id,
            integration_connection_id=None,
            date=DAY,
            weight_kg=84.5,
            measured_at=NOW,
            status=WEIGHT_EXPORT_DELETED,
        )
    )
    await db_session.flush()
    prepared = await garmin_weight_service.prepare_scoped_export(
        db_session,
        context=context,
    )

    with pytest.raises(garmin_weight_service.GarminWeightExportOwnershipError):
        await garmin_weight_service.get_status_scoped(
            db_session,
            prepared=prepared,
        )


async def test_fully_null_legacy_outbox_adopts_without_inventing_requester(
    db_session,
    legacy_owner_roots, *, garmin_connection_id,
):
    context = await _context(db_session, legacy_owner_roots, bridge=True)
    weight = await _weight(db_session, identity=context.identity)
    legacy = GarminWeightExport(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id, 
        date=DAY,
        weight_log_id=weight.id,
        weight_kg=weight.weight_kg,
        measured_at=NOW,
        status=WEIGHT_EXPORT_DELETED,
    )
    db_session.add(legacy)
    await db_session.flush()
    prepared = await garmin_weight_service.prepare_scoped_export(
        db_session,
        context=context,
    )

    await garmin_weight_service.set_enabled_scoped(
        db_session,
        True,
        prepared=prepared,
        now=NOW,
    )

    assert (
        legacy.subject_id,
        legacy.integration_connection_id,
        legacy.requested_by_user_id,
    ) == (
        context.identity.subject_id,
        context.integration_connection_id,
        None,
    )


async def test_legacy_context_resolves_owner_and_explicit_garmin_connection(
    db_session,
    legacy_owner_roots,
):
    context = await garmin_weight_service.resolve_legacy_export_context(
        db_session,
        actor_username="tester",
    )
    connection = await _garmin_connection(db_session, legacy_owner_roots.subject_id)

    assert connection is not None
    assert context.identity == WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    assert context.integration_connection_id == connection.id
    assert (
        context.legacy_bridge
        is conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
    )
