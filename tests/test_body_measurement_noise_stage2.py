"""Stage-2 scoped boundaries for body measurements and Weight noise markers."""

from __future__ import annotations

import asyncio
import ast
import uuid
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import Domain, RuleType, Severity, Source, UserStatus
from vitals.models.conflict_rule import ConflictRule
from vitals.models.glp1 import DosePhase
from vitals.models.identity import HealthSubject, User
from vitals.models.system_alert import SystemAlert
from vitals.models.timeline import Annotation
from vitals.models.weight import BodyMeasurement, NoiseMarker, WeightLog
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine, weight_service


MEASUREMENT_DATE = date(2026, 8, 20)
MOVED_DATE = date(2026, 8, 21)


def _identity(legacy_owner_roots, *, system: bool = False) -> WriteIdentity:
    return WriteIdentity(
        legacy_owner_roots.subject_id,
        None if system else legacy_owner_roots.user_id,
    )


def _context(
    identity: WriteIdentity,
    *,
    on_date: date = MEASUREMENT_DATE,
    legacy: bool = False,
) -> conflict_engine.ConflictWriteContext:
    return conflict_engine.ConflictWriteContext(
        identity=identity,
        evaluation_date=on_date,
        legacy_bridge=(
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
            if legacy
            else conflict_engine.LegacyConflictBridge.REJECT
        ),
    )


async def _prepared(
    session: AsyncSession,
    identity: WriteIdentity,
    *,
    on_date: date = MEASUREMENT_DATE,
    legacy: bool = False,
) -> conflict_engine.PreparedConflictWrite:
    return await conflict_engine.prepare_scoped_write(
        session,
        context=_context(identity, on_date=on_date, legacy=legacy),
    )


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


async def test_scoped_measurement_crud_preserves_identity_and_origin(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    row = await weight_service.upsert_body_measurement(
        db_session,
        on_date=MEASUREMENT_DATE,
        neck_cm=39,
        waist_cm=86,
        note="original",
        source=Source.MCP.value,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    original_id = row.id
    provenance = (row.subject_id, row.actor_user_id, row.source)
    assert provenance == (
        identity.subject_id,
        identity.actor_user_id,
        Source.MCP.value,
    )

    noted = await weight_service.update_body_measurement_note(
        db_session,
        row.id,
        note="note only",
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    assert noted is row and row.note == "note only"
    assert (row.subject_id, row.actor_user_id, row.source) == provenance

    moved = await weight_service.update_body_measurement(
        db_session,
        row.id,
        on_date=MOVED_DATE,
        waist_cm=85,
        identity=identity,
        prepared_conflict_write=await _prepared(
            db_session,
            identity,
            on_date=MOVED_DATE,
        ),
    )
    assert moved is row
    assert (moved.id, moved.date, moved.neck_cm, moved.note) == (
        original_id,
        MOVED_DATE,
        39,
        "note only",
    )
    assert (moved.subject_id, moved.actor_user_id, moved.source) == provenance

    assert await weight_service.delete_body_measurement(
        db_session,
        moved.id,
        identity=identity,
        prepared_conflict_write=await _prepared(
            db_session,
            identity,
            on_date=MOVED_DATE,
        ),
    ) is True
    assert await weight_service.list_body_measurements(
        db_session,
        subject_id=identity.subject_id,
    ) == []


async def test_measurement_rows_without_a_subject_are_out_of_every_scope(
    db_session,
    legacy_owner_roots,
):
    """Neither a fully-unowned nor a half-owned measurement is reachable now.

    The bridge used to distinguish the two — surfacing and adopting the first,
    reporting the second. Closing the domain removes the distinction: the
    subject is the whole scope, and neither row carries one.
    """
    identity = _identity(legacy_owner_roots)
    legacy = BodyMeasurement(
        date=MEASUREMENT_DATE,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        waist_cm=87,
    )
    partial = BodyMeasurement(
        actor_user_id=identity.actor_user_id,
        date=MOVED_DATE,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        waist_cm=88,
    )
    db_session.add_all([legacy, partial])
    await db_session.commit()

    assert await weight_service.list_body_measurements(
        db_session,
        subject_id=identity.subject_id,
    ) == []
    assert await weight_service.update_body_measurement(
        db_session,
        partial.id,
        on_date=MOVED_DATE,
        waist_cm=89,
        identity=identity,
        prepared_conflict_write=await _prepared(
            db_session,
            identity,
            on_date=MOVED_DATE,
            legacy=True,
        ),
    ) is None

    adopted = await weight_service.update_body_measurement(
        db_session,
        legacy.id,
        on_date=MEASUREMENT_DATE,
        waist_cm=86,
        identity=identity,
        prepared_conflict_write=await _prepared(
            db_session,
            identity,
            legacy=True,
        ),
    )
    assert adopted is None
    assert (legacy.subject_id, legacy.actor_user_id, legacy.waist_cm) == (
        None,
        None,
        87,
    )


async def test_measurement_and_noise_foreign_ids_are_non_enumerating(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    foreign_identity = await _new_identity(db_session, "foreign-body-noise")
    foreign_measurement = BodyMeasurement(
        subject_id=foreign_identity.subject_id,
        actor_user_id=foreign_identity.actor_user_id,
        date=MOVED_DATE,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        waist_cm=90,
    )
    foreign_noise = NoiseMarker(
        subject_id=foreign_identity.subject_id,
        actor_user_id=foreign_identity.actor_user_id,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        start_date=MOVED_DATE,
        reason="foreign",
        direction="up",
    )
    db_session.add_all([foreign_measurement, foreign_noise])
    await db_session.commit()

    owned_measurement = await weight_service.upsert_body_measurement(
        db_session,
        on_date=MEASUREMENT_DATE,
        waist_cm=85,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    owned_noise = await weight_service.add_noise_marker(
        db_session,
        start_date=MEASUREMENT_DATE,
        reason="owned",
        direction="down",
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    assert (owned_measurement.subject_id, owned_measurement.actor_user_id) == (
        identity.subject_id,
        identity.actor_user_id,
    )
    assert (
        owned_noise.subject_id,
        owned_noise.actor_user_id,
        owned_noise.source,
    ) == (identity.subject_id, identity.actor_user_id, Source.MANUAL.value)

    assert [row.id for row in await weight_service.list_body_measurements(
        db_session,
        subject_id=identity.subject_id,
    )] == [owned_measurement.id]
    assert [row.id for row in await weight_service.list_noise_markers(
        db_session,
        subject_id=identity.subject_id,
    )] == [owned_noise.id]
    assert await weight_service.update_body_measurement_note(
        db_session,
        foreign_measurement.id,
        note="must not write",
        identity=identity,
        prepared_conflict_write=await _prepared(
            db_session,
            identity,
            on_date=MOVED_DATE,
        ),
    ) is None
    assert await weight_service.delete_noise_marker(
        db_session,
        foreign_noise.id,
        identity=identity,
        prepared_conflict_write=await _prepared(
            db_session,
            identity,
            on_date=MOVED_DATE,
        ),
    ) is False


async def test_partial_root_noise_marker_is_out_of_scope_everywhere(
    db_session,
    legacy_owner_roots,
):
    """A half-owned marker used to be reported; now it is simply not found.

    The read passes over it, the alert refresh does not see an active interval,
    and the delete has nothing to remove — the row survives untouched either
    way, which is what actually protects it.
    """
    identity = _identity(legacy_owner_roots)
    partial = NoiseMarker(
        actor_user_id=identity.actor_user_id,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        start_date=MEASUREMENT_DATE,
        reason="partial provenance",
        direction="neutral",
    )
    db_session.add(partial)
    await db_session.commit()

    assert await weight_service.list_noise_markers(
        db_session,
        subject_id=identity.subject_id,
    ) == []

    prepared = await _prepared(db_session, identity, legacy=True)
    assert await weight_service.refresh_noise_alert(
        db_session,
        on_date=MEASUREMENT_DATE,
        identity=identity,
        prepared_conflict_write=prepared,
    ) is None
    assert await weight_service.delete_noise_marker(
        db_session,
        partial.id,
        identity=identity,
        prepared_conflict_write=prepared,
    ) is False
    assert await db_session.get(NoiseMarker, partial.id) is partial
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 0


async def test_two_subjects_measure_on_the_same_date(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    foreign_identity = await _new_identity(db_session, "foreign-date-owner")
    theirs = BodyMeasurement(
        subject_id=foreign_identity.subject_id,
        actor_user_id=foreign_identity.actor_user_id,
        date=MEASUREMENT_DATE,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        waist_cm=90,
    )
    db_session.add(theirs)
    await db_session.commit()

    # A measurement date is unique inside one record, not across the
    # installation, so the other subject's row is neither read nor changed.
    mine = await weight_service.upsert_body_measurement(
        db_session,
        on_date=MEASUREMENT_DATE,
        waist_cm=85,
        identity=identity,
        prepared_conflict_write=await _prepared(db_session, identity),
    )
    assert mine.subject_id == identity.subject_id
    assert mine.waist_cm == 85
    assert theirs.subject_id == foreign_identity.subject_id
    assert theirs.waist_cm == 90
    assert await db_session.scalar(
        select(func.count()).select_from(BodyMeasurement)
    ) == 2


async def test_measurement_block_is_write_free_and_override_is_attributed(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    db_session.add(
        ConflictRule(
            subject_id=identity.subject_id,
            rule_type=RuleType.HARD_BLOCK.value,
            domain_a=Domain.WEIGHT.value,
            condition_a={"measurement": True},
            domain_b=Domain.LABS.value,
            condition_b={"marker": "synthetic-risk"},
            severity=Severity.BLOCK.value,
            message="Synthetic measurement conflict.",
            active=True,
        )
    )
    await db_session.commit()

    async def labs(session, *, scope):
        del session, scope
        return [{"marker": "synthetic-risk"}]

    conflict_engine.register_domain_resolver(
        Domain.WEIGHT.value,
        weight_service.resolve_active_scoped,
    )
    conflict_engine.register_domain_resolver(Domain.LABS.value, labs)
    prepared = await _prepared(db_session, identity)

    with pytest.raises(conflict_engine.ConflictBlocked):
        await weight_service.upsert_body_measurement(
            db_session,
            on_date=MEASUREMENT_DATE,
            waist_cm=85,
            source=Source.MCP.value,
            identity=identity,
            prepared_conflict_write=prepared,
        )
    assert await db_session.scalar(
        select(func.count()).select_from(BodyMeasurement)
    ) == 0

    saved = await weight_service.upsert_body_measurement(
        db_session,
        on_date=MEASUREMENT_DATE,
        waist_cm=85,
        source=Source.MCP.value,
        override=True,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    assert (saved.subject_id, saved.actor_user_id, saved.source) == (
        identity.subject_id,
        identity.actor_user_id,
        Source.MCP.value,
    )


async def test_noise_refresh_writes_actorless_scoped_alert_and_resolves_stale_state(
    db_session,
    legacy_owner_roots,
):
    owner = _identity(legacy_owner_roots)
    system = _identity(legacy_owner_roots, system=True)
    marker = await weight_service.add_noise_marker(
        db_session,
        start_date=MEASUREMENT_DATE,
        reason="synthetic water shift",
        direction="up",
        source=Source.MCP.value,
        identity=owner,
        prepared_conflict_write=await _prepared(db_session, owner),
    )
    await db_session.commit()

    alert = await weight_service.refresh_noise_alert(
        db_session,
        on_date=MEASUREMENT_DATE,
        identity=system,
        prepared_conflict_write=await _prepared(db_session, system),
    )
    assert isinstance(alert, SystemAlert)
    assert (
        alert.subject_id,
        alert.integration_connection_id,
        alert.overridden_by_user_id,
        alert.resolved_by_user_id,
        alert.alert_key,
        alert.resolved_at,
    ) == (
        owner.subject_id,
        None,
        None,
        None,
        weight_service.NOISE_ALERT_KEY,
        None,
    )
    await db_session.commit()

    assert await weight_service.delete_noise_marker(
        db_session,
        marker.id,
        identity=owner,
        prepared_conflict_write=await _prepared(db_session, owner),
    ) is True
    await db_session.refresh(alert)
    assert alert.resolved_at is not None
    assert alert.resolved_by_user_id is None
    await db_session.commit()
    stale = await weight_service.refresh_noise_alert(
        db_session,
        on_date=MEASUREMENT_DATE,
        identity=system,
        prepared_conflict_write=await _prepared(db_session, system),
    )
    assert stale is None


async def test_chart_series_composes_only_the_selected_subject_direct_page_data(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    foreign = await _new_identity(db_session, "foreign-weight-chart")
    db_session.add_all(
        [
            WeightLog(
                subject_id=identity.subject_id,
                actor_user_id=identity.actor_user_id,
                date=date(2026, 8, 10),
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                weight_kg=80,
                superseded=False,
            ),
            WeightLog(
                subject_id=foreign.subject_id,
                actor_user_id=foreign.actor_user_id,
                date=date(2026, 8, 11),
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                weight_kg=90,
                superseded=False,
            ),
            BodyMeasurement(
                subject_id=identity.subject_id,
                actor_user_id=identity.actor_user_id,
                date=date(2026, 8, 12),
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                waist_cm=85,
                lbm_kg=60,
            ),
            BodyMeasurement(
                subject_id=foreign.subject_id,
                actor_user_id=foreign.actor_user_id,
                date=date(2026, 8, 13),
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                waist_cm=95,
                lbm_kg=70,
            ),
            NoiseMarker(
                subject_id=identity.subject_id,
                actor_user_id=identity.actor_user_id,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                start_date=date(2026, 8, 14),
                end_date=date(2026, 8, 14),
                reason="owner noise",
            ),
            NoiseMarker(
                subject_id=foreign.subject_id,
                actor_user_id=foreign.actor_user_id,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                start_date=date(2026, 8, 15),
                reason="foreign noise",
            ),
            DosePhase(
                subject_id=identity.subject_id,
                actor_user_id=identity.actor_user_id,
                domain=Domain.GLP1.value,
                source=Source.MANUAL.value,
                start_date=date(2026, 8, 16),
                drug="semaglutide",
                dose_mg=1,
            ),
            DosePhase(
                subject_id=foreign.subject_id,
                actor_user_id=foreign.actor_user_id,
                domain=Domain.GLP1.value,
                source=Source.MANUAL.value,
                start_date=date(2026, 8, 17),
                drug="tirzepatide",
                dose_mg=5,
            ),
            Annotation(
                subject_id=identity.subject_id,
                actor_user_id=identity.actor_user_id,
                date=date(2026, 8, 18),
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                kind="note",
                title="owner flag",
            ),
            Annotation(
                subject_id=foreign.subject_id,
                actor_user_id=foreign.actor_user_id,
                date=date(2026, 8, 19),
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                kind="note",
                title="foreign flag",
            ),
        ]
    )
    await db_session.commit()

    series = await weight_service.chart_series(
        db_session,
        subject_id=identity.subject_id,
        include_bia=False,
        include_glp1=True,
        include_timeline=True,
    )

    assert series["raw"] == [{"date": "2026-08-10", "weight_kg": 80}]
    assert series["lbm"] == [{"date": "2026-08-12", "lbm_kg": 60}]
    assert series["noise"] == [
        {"start": "2026-08-14", "end": "2026-08-14"}
    ]
    assert [(phase["drug"], phase["dose_mg"]) for phase in series["phases"]] == [
        ("semaglutide", 1)
    ]
    assert [annotation["label"] for annotation in series["annotations"]] == [
        "owner flag"
    ]
    assert series["bia"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"neck_cm": 0},
        {"waist_cm": 301},
        {"hips_cm": float("nan")},
    ],
)
async def test_scoped_measurement_validation_is_write_free(
    db_session,
    legacy_owner_roots,
    kwargs,
):
    identity = _identity(legacy_owner_roots)
    with pytest.raises(ValueError):
        await weight_service.upsert_body_measurement(
            db_session,
            on_date=MEASUREMENT_DATE,
            identity=identity,
            prepared_conflict_write=await _prepared(db_session, identity),
            **kwargs,
        )
    assert await db_session.scalar(
        select(func.count()).select_from(BodyMeasurement)
    ) == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reason": ""},
        {"reason": "bad range", "end_date": date(2026, 8, 19)},
        {"reason": "bad direction", "direction": "sideways"},
    ],
)
async def test_scoped_noise_validation_is_write_free(
    db_session,
    legacy_owner_roots,
    kwargs,
):
    identity = _identity(legacy_owner_roots)
    with pytest.raises(ValueError):
        await weight_service.add_noise_marker(
            db_session,
            start_date=MEASUREMENT_DATE,
            identity=identity,
            prepared_conflict_write=await _prepared(db_session, identity),
            **kwargs,
        )
    assert await db_session.scalar(select(func.count()).select_from(NoiseMarker)) == 0


async def test_wrong_identity_and_committed_capability_are_rejected_before_write(
    db_session,
    legacy_owner_roots,
):
    identity = _identity(legacy_owner_roots)
    prepared = await _prepared(db_session, identity)
    wrong = WriteIdentity(identity.subject_id, uuid.uuid4())

    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await weight_service.upsert_body_measurement(
            db_session,
            on_date=MEASUREMENT_DATE,
            waist_cm=85,
            identity=wrong,
            prepared_conflict_write=prepared,
        )
    await db_session.commit()
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await weight_service.add_noise_marker(
            db_session,
            start_date=MEASUREMENT_DATE,
            reason="stale token",
            identity=identity,
            prepared_conflict_write=prepared,
        )
    assert await db_session.scalar(
        select(func.count()).select_from(BodyMeasurement)
    ) == 0
    assert await db_session.scalar(select(func.count()).select_from(NoiseMarker)) == 0


async def test_web_and_mcp_writes_are_scoped_and_keep_surface_provenance(
    auth_client,
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    web_measurement = await auth_client.post(
        "/weight/measurement",
        data={"date": "2026-08-20", "waist_cm": "86", "note": "web"},
    )
    web_noise = await auth_client.post(
        "/weight/noise",
        data={
            "start_date": "2026-08-20",
            "reason": "web noise",
            "direction": "neutral",
        },
    )
    assert (web_measurement.status_code, web_noise.status_code) == (303, 303)
    web_measurement_row = await db_session.scalar(
        select(BodyMeasurement).where(BodyMeasurement.date == MEASUREMENT_DATE)
    )
    web_noise_row = await db_session.scalar(
        select(NoiseMarker).where(NoiseMarker.reason == "web noise")
    )
    assert web_measurement_row is not None and web_noise_row is not None

    mcp_router = pytest.importorskip("web.routers.mcp")
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    captured = []

    def capture_scoped_call(name, original):
        async def wrapped(session, *args, **kwargs):
            context = conflict_engine.require_prepared_identity(
                session,
                prepared=kwargs["prepared_conflict_write"],
                identity=kwargs["identity"],
            )
            captured.append(
                (
                    name,
                    kwargs["identity"],
                    # The boundary carries the subject, not an escape hatch.
                    kwargs["identity"].subject_id,
                    context.legacy_bridge,
                    kwargs.get("source"),
                )
            )
            return await original(session, *args, **kwargs)

        return wrapped

    for service_name in (
        "upsert_body_measurement",
        "update_body_measurement",
        "update_body_measurement_note",
        "add_noise_marker",
        "delete_noise_marker",
    ):
        monkeypatch.setattr(
            weight_service,
            service_name,
            capture_scoped_call(
                service_name,
                getattr(weight_service, service_name),
            ),
        )

    mcp_measurement = await mcp_router.log_measurement(
        on_date="2026-08-21",
        waist_cm=85,
        note="mcp",
    )
    mcp_noise = await mcp_router.add_noise_marker(
        start_date="2026-08-21",
        reason="mcp noise",
        direction="down",
    )
    updated = await mcp_router.update_measurement(
        mcp_measurement["id"],
        on_date="2026-08-21",
        waist_cm=84,
    )
    assert updated["waist_cm"] == 84
    noted = await mcp_router.log_note(
        "measurement",
        mcp_measurement["id"],
        "mcp note",
    )
    assert noted["note"] == "mcp note"
    notes = await mcp_router.get_notes(domain="measurement")
    assert {row["id"] for row in notes} == {
        web_measurement_row.id,
        mcp_measurement["id"],
    }
    logs = await mcp_router.get_weight_logs(
        start_date="2026-08-20",
        end_date="2026-08-21",
    )
    assert {row["id"] for row in logs["measurements"]} == {
        web_measurement_row.id,
        mcp_measurement["id"],
    }
    assert {row["id"] for row in logs["noise_markers"]} == {
        web_noise_row.id,
        mcp_noise["id"],
    }
    assert await mcp_router.delete_record("noise_marker", mcp_noise["id"]) == {
        "deleted": True,
        "domain": "noise_marker",
        "record_id": mcp_noise["id"],
    }
    assert captured == [
        (
            "upsert_body_measurement",
            _identity(legacy_owner_roots),
            legacy_owner_roots.subject_id,
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
            Source.MCP.value,
        ),
        (
            "add_noise_marker",
            _identity(legacy_owner_roots),
            legacy_owner_roots.subject_id,
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
            Source.MCP.value,
        ),
        (
            "update_body_measurement",
            _identity(legacy_owner_roots),
            legacy_owner_roots.subject_id,
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
            None,
        ),
        (
            "update_body_measurement_note",
            _identity(legacy_owner_roots),
            legacy_owner_roots.subject_id,
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
            None,
        ),
        (
            "delete_noise_marker",
            _identity(legacy_owner_roots),
            legacy_owner_roots.subject_id,
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
            None,
        ),
    ]

    rows = list(await db_session.scalars(select(BodyMeasurement)))
    assert {
        (row.note, row.subject_id, row.actor_user_id, row.source) for row in rows
    } == {
        (
            "web",
            legacy_owner_roots.subject_id,
            legacy_owner_roots.user_id,
            Source.MANUAL.value,
        ),
        (
            "mcp note",
            legacy_owner_roots.subject_id,
            legacy_owner_roots.user_id,
            Source.MCP.value,
        ),
    }
    remaining_noise = list(await db_session.scalars(select(NoiseMarker)))
    assert [
        (
            row.reason,
            row.subject_id,
            row.actor_user_id,
            row.source,
        )
        for row in remaining_noise
    ] == [
        (
            "web noise",
            legacy_owner_roots.subject_id,
            legacy_owner_roots.user_id,
            Source.MANUAL.value,
        )
    ]


@pytest.mark.integration
async def test_postgres_concurrent_first_measurement_upserts_serialize(
    db_session,
    legacy_owner_roots,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    identity = _identity(legacy_owner_roots)
    await db_session.commit()

    session_a = factory()
    prepared_a = await _prepared(session_a, identity)
    await weight_service.upsert_body_measurement(
        session_a,
        on_date=MEASUREMENT_DATE,
        waist_cm=86,
        source=Source.MCP.value,
        identity=identity,
        prepared_conflict_write=prepared_a,
    )

    async def writer_b() -> None:
        async with factory() as session_b:
            prepared_b = await _prepared(session_b, identity)
            await weight_service.upsert_body_measurement(
                session_b,
                on_date=MEASUREMENT_DATE,
                waist_cm=85,
                source=Source.MCP.value,
                identity=identity,
                prepared_conflict_write=prepared_b,
            )
            await session_b.commit()

    task_b = asyncio.create_task(writer_b())
    await asyncio.sleep(0.25)
    assert not task_b.done(), "writer B must wait on the prepared subject root"
    await session_a.commit()
    await session_a.close()
    await asyncio.wait_for(task_b, timeout=5)

    async with factory() as verify:
        rows = list(
            await verify.scalars(
                select(BodyMeasurement).where(
                    BodyMeasurement.subject_id == identity.subject_id,
                    BodyMeasurement.date == MEASUREMENT_DATE,
                )
            )
        )
    assert len(rows) == 1
    assert (rows[0].waist_cm, rows[0].source, rows[0].actor_user_id) == (
        85,
        Source.MCP.value,
        identity.actor_user_id,
    )


_STRICT_ROUTER_SURFACES = {
    "web/routers/weight.py": {
        "_section_context": {"list_body_measurements", "list_noise_markers"},
        "log_measurement_entry": {
            "upsert_body_measurement",
            "update_body_measurement",
        },
        "add_noise_entry": {"add_noise_marker"},
        "delete_measurement_entry": {"delete_body_measurement"},
        "delete_noise_marker_entry": {"delete_noise_marker"},
    },
    "web/routers/mcp.py": {
        "get_weight_logs": {"list_body_measurements", "list_noise_markers"},
        "log_measurement": {"upsert_body_measurement"},
        "get_measurements": {"list_body_measurements"},
        "update_measurement": {"update_body_measurement"},
        "add_noise_marker": {"add_noise_marker"},
    },
}

# Whole-lake/composition tools are intentionally outside this direct-domain gate.
# Their broader cutover belongs to the overview/export/chart-data inventory.
_DEFERRED_MCP_COMPOSITION_SURFACES = frozenset(
    {"get_full_snapshot", "export_everything", "get_data_overview", "get_trend"}
)


def _function_node(tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing async router surface {name}")


def _callee_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _direct_db_violations(node: ast.AST) -> list[tuple[int, str]]:
    violations = []
    for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
        callee = _callee_name(call)
        if callee in {"BodyMeasurement", "NoiseMarker", "select"}:
            violations.append((call.lineno, callee))
            continue
        if callee in {"get", "add", "add_all", "merge", "delete"} and isinstance(
            call.func, ast.Attribute
        ):
            root = call.func.value
            if isinstance(root, ast.Name) and root.id in {"db", "session"}:
                violations.append((call.lineno, f"{root.id}.{callee}"))
    return violations


def _branch_for_literal(
    function: ast.AsyncFunctionDef,
    *,
    variable: str,
    literal: str,
) -> ast.If:
    for node in ast.walk(function):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        if not isinstance(node.test.left, ast.Name) or node.test.left.id != variable:
            continue
        values = {
            value.value
            for comparator in node.test.comparators
            for value in (
                comparator.elts
                if isinstance(comparator, (ast.Set, ast.Tuple, ast.List))
                else [comparator]
            )
            if isinstance(value, ast.Constant)
        }
        if literal in values:
            return node
    raise AssertionError(f"missing {variable} branch for {literal}")


def test_direct_body_measurement_noise_router_surfaces_use_only_service_api():
    repo_root = Path(__file__).resolve().parents[1]
    trees = {}
    for relative, expected_functions in _STRICT_ROUTER_SURFACES.items():
        path = repo_root / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        trees[relative] = tree
        for function_name, required_service_calls in expected_functions.items():
            function = _function_node(tree, function_name)
            assert _direct_db_violations(function) == []
            callees = {
                _callee_name(call)
                for call in ast.walk(function)
                if isinstance(call, ast.Call)
            }
            assert required_service_calls <= callees

    mcp_tree = trees["web/routers/mcp.py"]
    measurement_note = _branch_for_literal(
        _function_node(mcp_tree, "log_note"),
        variable="domain",
        literal="measurement",
    )
    measurement_notes_read = _branch_for_literal(
        _function_node(mcp_tree, "get_notes"),
        variable="d_name",
        literal="measurement",
    )
    measurement_delete = _branch_for_literal(
        _function_node(mcp_tree, "delete_record"),
        variable="domain",
        literal="measurement",
    )
    for branch, required_call in (
        (measurement_note, "update_body_measurement_note"),
        (measurement_notes_read, "list_body_measurements"),
        (measurement_delete, "_mcp_v1_aux_weight_write"),
    ):
        assert _direct_db_violations(branch) == []
        assert required_call in {
            _callee_name(call)
            for call in ast.walk(branch)
            if isinstance(call, ast.Call)
        }

    delete_targets = next(
        node.value
        for node in mcp_tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_DELETE_TARGETS"
    )
    targets = ast.literal_eval(delete_targets)
    assert targets["measurement"][2] == "delete_body_measurement"
    assert targets["noise_marker"][2] == "delete_noise_marker"
    assert _DEFERRED_MCP_COMPOSITION_SURFACES.isdisjoint(
        _STRICT_ROUTER_SURFACES["web/routers/mcp.py"]
    )
