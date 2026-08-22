"""Tests for the data-portability service + settings routes.

Round-trip fidelity (export → wipe+import → export gives the same snapshot),
idempotency, FK integrity across the Hevy tree, strict validation (clean errors,
no silent failures), secret exclusion, and the curated LLM export shape. The
Postgres sequence-reset behaviour is an ``@pytest.mark.integration`` test (SQLite
can't exercise it).
"""
import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionType,
    IntegrationProvider,
    NotificationDeliveryStatus,
    Source,
)
from vitals.models.ai import AIInvocation
from vitals.models.app_settings import AppSetting
from vitals.models.conflict_rule import ConflictRule
from vitals.models.garmin import GarminActivity, GarminDaily, GarminIntraday
from vitals.models.glp1 import Injection
from vitals.models.hevy import HevyExercise, HevySet, HevyWorkout
from vitals.models.hrt import (
    HrtCompound,
    HrtCompoundComponent,
    HrtCycle,
    HrtCycleItem,
    HrtCycleTemplate,
    HrtCycleTemplateItem,
)
from vitals.models.body_scan import BodyScan, BodyScanMetric
from vitals.models.garmin import GarminWeightExport
from vitals.models.genetics import GeneticVariant
from vitals.models.labs import LabResult
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.proactive import NotificationDeliveryIntent
from vitals.models.raw_payload import RawPayload
from vitals.models.share import SharedReport
from vitals.models.signals import DayContext, Signal
from vitals.models.supplements import Supplement
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.models.weight import BodyMeasurement, ProgressPhoto, WeightLog
from vitals.services import conflict_catalog, data_portability_service
from vitals.services.data_portability_service import (
    PortabilityError,
    export_full,
    export_llm,
    import_full,
)
from vitals.services.conflict_rule_ownership_backfill_service import (
    CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    CONFLICT_RULE_OWNERSHIP_BACKFILL_TABLES,
)
from vitals.services.day_context_ownership_backfill_service import (
    DAY_CONTEXT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    DAY_CONTEXT_OWNERSHIP_BACKFILL_TABLES,
    DayContextOwnershipBackfillStateError,
)
from vitals.services.hevy_child_ownership_backfill_service import (
    HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES,
)
from vitals.services.hrt_child_ownership_backfill_service import (
    HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    HRT_CHILD_OWNERSHIP_BACKFILL_TABLES,
)
from vitals.services.hrt_compound_ownership_backfill_service import (
    HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES,
)
from vitals.services.normalized_ownership_backfill_service import (
    NORMALIZED_MANUAL_CHECKPOINT_PHASES,
    NORMALIZED_MANUAL_TABLES,
)
from vitals.services.provider_raw_ownership_backfill_service import (
    PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES,
)
from vitals.services.progress_photo_ownership_backfill_service import (
    PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    PROGRESS_PHOTO_OWNERSHIP_BACKFILL_TABLES,
    ProgressPhotoOwnershipBackfillStateError,
)
from vitals.services.raw_ownership_backfill_service import (
    RAW_OWNERSHIP_BACKFILL_PHASE,
    RawOwnershipBackfillIdentityError,
    RawOwnershipBackfillStateError,
)
from vitals.services.signal_ownership_backfill_service import (
    SIGNAL_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    SIGNAL_OWNERSHIP_BACKFILL_TABLES,
    SignalOwnershipBackfillStateError,
)
from vitals.services.shared_report_ownership_backfill_service import (
    SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    SharedReportOwnershipBackfillStateError,
)
from vitals.services.system_alert_ownership_backfill_service import (
    SYSTEM_ALERT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    SystemAlertOwnershipBackfillStateError,
)
from vitals.services.notification_ownership_backfill_service import (
    NOTIFICATION_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    NotificationOwnershipBackfillStateError,
)
from vitals.services.weekly_digest_ownership_backfill_service import (
    WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    WeeklyDigestOwnershipBackfillStateError,
)
from vitals.services.garmin_weight_export_ownership_backfill_service import (
    GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    GarminWeightExportOwnershipBackfillStateError,
)
from vitals.services.body_scan_metric_ownership_backfill_service import (
    BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    BodyScanMetricOwnershipBackfillStateError,
)
from vitals.services.body_scan_ownership_backfill_service import (
    BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    BODY_SCAN_OWNERSHIP_BACKFILL_TABLES,
    BodyScanOwnershipBackfillStateError,
)
from vitals.services.genetic_variant_ownership_backfill_service import (
    GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    GENETIC_VARIANT_OWNERSHIP_BACKFILL_TABLES,
    GeneticVariantOwnershipBackfillStateError,
)
from vitals.services.lab_result_ownership_backfill_service import (
    LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    LAB_RESULT_OWNERSHIP_BACKFILL_TABLES,
    LabResultOwnershipBackfillStateError,
)
from vitals.services.weight_log_ownership_backfill_service import (
    WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES,
    WEIGHT_LOG_OWNERSHIP_BACKFILL_TABLES,
    WeightLogOwnershipBackfillStateError,
)


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader does when the ownership backfill has not reached a row yet, which is
# a state the application itself can no longer create. The schema says so, so
# this module asks for the one that stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract

_IDENTITY_CONTROL_PLANE_TABLES = {
    "users",
    "user_roles",
    "health_subjects",
    "support_access_grants",
    "support_access_scopes",
    "audit_events",
}
_EMPTY_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb924"
    "27ae41e4649b934ca495991b7852b855"
)


async def _seed(session, *, garmin_connection_id, hevy_connection_id, legacy_owner_roots) -> None:
    """Populate a few rows across domains, including a raw payload + FK link, a
    superseded weight, the Hevy tree, and a secret-looking app setting."""
    rp = RawPayload(subject_id=legacy_owner_roots.subject_id, domain="garmin", source="garmin_api", external_id="g1", payload={"steps": 8000})
    session.add(rp)
    await session.flush()  # need rp.id for the FK link below

    session.add_all(
        [
            # Two weights on one date: a superseded Garmin row + the active manual one.
            WeightLog(subject_id=legacy_owner_roots.subject_id, 
                date=date(2026, 4, 29), domain="weight", source="garmin_api",
                weight_kg=119.1, raw_payload_id=rp.id, superseded=True,
            ),
            WeightLog(subject_id=legacy_owner_roots.subject_id, 
                date=date(2026, 4, 29), domain="weight", source="manual",
                weight_kg=118.5, superseded=False, note="утро",
            ),
            BodyMeasurement(subject_id=legacy_owner_roots.subject_id, 
                date=date(2026, 4, 29), domain="weight", source="manual",
                waist_cm=100.0, neck_cm=42.0,
            ),
            Injection(subject_id=legacy_owner_roots.subject_id, 
                date=date(2026, 4, 28), domain="glp1", source="manual",
                drug="tirzepatide", dose_mg=5.0, site="abdomen_left",
            ),
            GarminDaily(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id, 
                date=date(2026, 4, 29), domain="garmin", source="garmin_api",
                raw_payload_id=rp.id, steps=8000, sleep_seconds=27000,
                sleep_score=80, resting_hr=55,
                # The night's interval series — JSONB round-trip stability.
                sleep_stages=[
                    {"start": "2026-04-28T23:00:00", "end": "2026-04-28T23:30:00", "stage": "light"},
                    {"start": "2026-04-28T23:30:00", "end": "2026-04-29T01:00:00", "stage": "deep"},
                ],
                breathing_events=[
                    {"start": "2026-04-28T23:00:00", "end": "2026-04-29T01:00:00", "value": 0},
                ],
            ),
            # Per-activity detail — exercises JSONB round-trip stability.
            GarminActivity(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id, 
                date=date(2026, 4, 29), domain="garmin", source="garmin_api",
                external_id="act1", activity_type="running", name="Run",
                elevation_gain_m=42.0, training_effect_aerobic=3.4,
                hr_zone_seconds=[{"zone": 1, "secs": 120.0, "low_hr": 101}],
                splits=[{"index": 1, "distance_m": 1000.0, "avg_hr": 150}],
            ),
            # Intraday samples — the tall series table backing the daily scalars.
            GarminIntraday(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id, 
                date=date(2026, 4, 29), domain="garmin", source="garmin_api",
                raw_payload_id=rp.id, series_type="stress",
                ts=datetime(2026, 4, 29, 8, 0), value=43.0,
            ),
            GarminIntraday(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id, 
                date=date(2026, 4, 29), domain="garmin", source="garmin_api",
                raw_payload_id=rp.id, series_type="body_battery",
                ts=datetime(2026, 4, 29, 8, 0), value=72.0,
            ),
            # A nightly series, timestamped the evening before the date it's filed
            # under — the backup must not "helpfully" re-date it.
            GarminIntraday(subject_id=legacy_owner_roots.subject_id, integration_connection_id=garmin_connection_id, 
                date=date(2026, 4, 29), domain="garmin", source="garmin_api",
                raw_payload_id=rp.id, series_type="sleep_hr",
                ts=datetime(2026, 4, 28, 23, 10), value=58.0,
            ),
            LabResult(subject_id=legacy_owner_roots.subject_id, 
                date=date(2026, 4, 1), domain="labs", source="lab_parser",
                marker="glucose", value=5.1, unit="mmol/L", ref_low=3.9, ref_high=5.5,
                flag="normal",
            ),
            Supplement(subject_id=legacy_owner_roots.subject_id, 
                domain="supplements", source="manual", name="Omega-3", key="omega3",
                dose="2g", evidence="A", active=True,
            ),
            AppSetting(key="ui_pref", value={"theme": "dim"}),
            AppSetting(key="garmin_oauth_token", value="super-secret-xyz"),
        ]
    )
    await session.flush()

    w = HevyWorkout(subject_id=legacy_owner_roots.subject_id, integration_connection_id=hevy_connection_id, 
        date=date(2026, 4, 27), domain="workouts", source="hevy_api",
        external_id="w1", title="Push", program="A", duration_seconds=3600,
    )
    session.add(w)
    await session.flush()
    # The children inherit the workout's roots; the composite parent FK requires
    # them to match, and the restore copies them down the same way.
    ex = HevyExercise(
        subject_id=w.subject_id,
        integration_connection_id=w.integration_connection_id,
        workout_id=w.id, exercise_index=0, title="Bench Press", exercise_template_id="bp",
    )
    session.add(ex)
    await session.flush()
    session.add_all(
        [
            HevySet(
                subject_id=ex.subject_id,
                integration_connection_id=ex.integration_connection_id,
                exercise_id=ex.id, set_index=0, set_type="normal",
                weight_kg=80.0, reps=8, rpe=8.0,
            ),
            HevySet(
                subject_id=ex.subject_id,
                integration_connection_id=ex.integration_connection_id,
                exercise_id=ex.id, set_index=1, set_type="normal",
                weight_kg=80.0, reps=7,
            ),
        ]
    )
    await conflict_catalog.sync_catalog(session)
    await session.commit()


def _normalize(snapshot: dict) -> dict:
    """Snapshot minus the (timestamped) metadata, with each table's rows sorted so
    the comparison is order-insensitive."""
    out = {}
    for key, rows in snapshot.items():
        if key == "metadata":
            continue
        out[key] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str))
    return out


# ── Full backup round-trip ─────────────────────────────────────────────────────


async def test_full_roundtrip_replace_is_stable(garmin_connection_id, hevy_connection_id, legacy_owner_roots, db_session):
    await _seed(db_session, garmin_connection_id=garmin_connection_id, hevy_connection_id=hevy_connection_id, legacy_owner_roots=legacy_owner_roots)
    snap1 = await export_full(db_session)

    # Replace the whole DB from the snapshot, then re-export.
    stats = await import_full(db_session, snap1)
    await db_session.flush()
    snap2 = await export_full(db_session)

    assert _normalize(snap1) == _normalize(snap2)
    assert snap1["metadata"]["kind"] == "full_backup"
    # Both weight rows survive (active + superseded); the Hevy tree is intact.
    assert stats.counts["weight_logs"] == 2
    assert stats.counts["hevy_exercises"] == 1
    assert stats.counts["hevy_sets"] == 2
    # The intraday series is in the backup (it rides the generic sorted_tables
    # walk, so this guards the walk actually reaching new tables) — whole-day and
    # nightly series alike.
    assert stats.counts["garmin_intraday"] == 3
    assert {r["series_type"] for r in snap1["garmin_intraday"]} == {
        "stress", "body_battery", "sleep_hr",
    }
    # The night's interval series survive as structure, not as a stringified blob.
    daily = snap1["garmin_daily"][0]
    assert [s["stage"] for s in daily["sleep_stages"]] == ["light", "deep"]
    assert daily["breathing_events"][0]["value"] == 0


async def test_import_is_idempotent(garmin_connection_id, hevy_connection_id, legacy_owner_roots, db_session):
    await _seed(db_session, garmin_connection_id=garmin_connection_id, hevy_connection_id=hevy_connection_id, legacy_owner_roots=legacy_owner_roots)
    snap = await export_full(db_session)

    await import_full(db_session, snap)
    await db_session.flush()
    await import_full(db_session, snap)  # second run must not duplicate or fail
    await db_session.flush()

    after = await export_full(db_session)
    assert _normalize(snap) == _normalize(after)


async def test_import_preserves_fk_links(garmin_connection_id, hevy_connection_id, legacy_owner_roots, db_session):
    await _seed(db_session, garmin_connection_id=garmin_connection_id, hevy_connection_id=hevy_connection_id, legacy_owner_roots=legacy_owner_roots)
    snap = await export_full(db_session)
    await import_full(db_session, snap)
    await db_session.flush()

    # The Hevy set → exercise → workout chain and the weight → raw_payload link
    # must still resolve after the id-preserving restore.
    from sqlalchemy import select

    sets = (await db_session.execute(select(HevySet))).scalars().all()
    exercises = {e.id for e in (await db_session.execute(select(HevyExercise))).scalars().all()}
    workouts = {w.id for w in (await db_session.execute(select(HevyWorkout))).scalars().all()}
    assert sets and all(s.exercise_id in exercises for s in sets)

    exrows = (await db_session.execute(select(HevyExercise))).scalars().all()
    assert all(e.workout_id in workouts for e in exrows)

    raw_ids = {r.id for r in (await db_session.execute(select(RawPayload))).scalars().all()}
    linked = (
        await db_session.execute(select(WeightLog).where(WeightLog.raw_payload_id.isnot(None)))
    ).scalars().all()
    assert linked and all(w.raw_payload_id in raw_ids for w in linked)


# ── Validation (clean 400s, never silent) ──────────────────────────────────────


async def test_import_rejects_non_object(db_session):
    with pytest.raises(PortabilityError):
        await import_full(db_session, ["not", "a", "dict"])


async def test_import_rejects_missing_metadata(db_session):
    with pytest.raises(PortabilityError, match="metadata"):
        await import_full(db_session, {"weight_logs": []})


async def test_import_rejects_unknown_table(db_session):
    payload = {"metadata": {"version": "1.0"}, "not_a_real_table": [{"x": 1}]}
    with pytest.raises(PortabilityError, match="(Неизвестн|Unknown)"):
        await import_full(db_session, payload)


async def test_import_rejects_non_list_section(db_session):
    payload = {"metadata": {"version": "1.0"}, "weight_logs": {"oops": True}}
    with pytest.raises(PortabilityError, match="(списком|list)"):
        await import_full(db_session, payload)


# ── Secret exclusion ───────────────────────────────────────────────────────────


async def test_export_excludes_secret_settings(garmin_connection_id, hevy_connection_id, legacy_owner_roots, db_session):
    await _seed(db_session, garmin_connection_id=garmin_connection_id, hevy_connection_id=hevy_connection_id, legacy_owner_roots=legacy_owner_roots)
    snap = await export_full(db_session)
    keys = {row["key"] for row in snap["app_settings"]}
    assert "ui_pref" in keys
    assert "garmin_oauth_token" not in keys  # dropped by the secret guard


async def test_full_backup_excludes_identity_control_plane(db_session):
    """A user backup carries health history, never login or access-control state."""
    from vitals.models.identity import User
    from vitals.services.identity_bootstrap import bootstrap_legacy_owner

    password_hash = "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
    result = await bootstrap_legacy_owner(
        db_session,
        username="Portability Owner",
        password_hash=password_hash,
        timezone="UTC",
    )
    await db_session.commit()

    assert await db_session.get(User, result.user_id) is not None
    snapshot = await export_full(db_session)
    assert _IDENTITY_CONTROL_PLANE_TABLES.isdisjoint(snapshot)
    assert password_hash not in json.dumps(snapshot, ensure_ascii=False)


async def test_import_cannot_delete_or_replace_identity_control_plane(db_session):
    """Legacy and forged backups cannot mutate the durable restore principal."""
    from sqlalchemy import select as sa_select

    from vitals.models.identity import AuditEvent, HealthSubject, User, UserRole
    from vitals.services.identity_bootstrap import bootstrap_legacy_owner

    password_hash = "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
    result = await bootstrap_legacy_owner(
        db_session,
        username="Durable Owner",
        password_hash=password_hash,
        timezone="Asia/Almaty",
    )
    await db_session.commit()

    async def identity_state():
        db_session.expire_all()
        user = await db_session.scalar(
            sa_select(User).where(User.id == result.user_id)
        )
        subject = await db_session.scalar(
            sa_select(HealthSubject).where(HealthSubject.id == result.subject_id)
        )
        roles = tuple(
            sorted(
                await db_session.scalars(
                    sa_select(UserRole.role).where(UserRole.user_id == result.user_id)
                )
            )
        )
        audits = tuple(
            (
                row.id,
                row.event_type,
                row.subject_id,
                row.metadata_json,
            )
            for row in await db_session.scalars(
                sa_select(AuditEvent).order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        )
        return (
            user.id,
            user.username,
            user.password_hash,
            user.status,
            user.session_version,
            subject.id,
            subject.owner_user_id,
            subject.timezone,
            roles,
            audits,
        )

    before = await identity_state()
    legacy_snapshot = {
        "metadata": {"version": "1.0", "kind": "full_backup"},
        "app_settings": [],
    }

    # A pre-identity backup has no control-plane sections. Restoring it must not
    # delete the account, subject, roles, or audit event created during bootstrap.
    await import_full(db_session, legacy_snapshot)
    await db_session.flush()
    assert await identity_state() == before

    # Even a file that claims to carry control-plane rows cannot overwrite them.
    # Invalid placeholder shapes are intentional: excluded sections are never
    # deserialized or loaded, just as they are never exported or deleted.
    forged_snapshot = dict(legacy_snapshot)
    forged_snapshot.update(
        {
            "users": [{"id": "forged", "password_hash": "planted"}],
            "user_roles": [{"id": "forged", "role": "platform_superadmin"}],
            "health_subjects": [{"id": "forged", "timezone": "UTC"}],
            "support_access_grants": [{"id": "forged"}],
            "support_access_scopes": [{"id": "forged"}],
            "audit_events": [{"id": "forged", "event_type": "planted"}],
        }
    )
    stats = await import_full(db_session, forged_snapshot)
    await db_session.flush()

    assert await identity_state() == before
    assert _IDENTITY_CONTROL_PLANE_TABLES.isdisjoint(stats.counts)


async def test_full_import_blocks_raw_backfill_atomically_and_preserves_other_phase(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    order: list[str] = []
    original_governance = data_portability_service.acquire_identity_governance_lock
    original_subject = data_portability_service._single_local_subject_id
    original_preflight = data_portability_service._refuse_retained_raw_references
    original_block = (
        data_portability_service.block_raw_ownership_backfill_for_portability_v1_restore
    )

    async def tracked_governance(*args, **kwargs):
        order.append("governance")
        return await original_governance(*args, **kwargs)

    async def tracked_subject(*args, **kwargs):
        order.append("local-subject")
        return await original_subject(*args, **kwargs)

    async def tracked_preflight(*args, **kwargs):
        order.append("retained-reference-preflight")
        return await original_preflight(*args, **kwargs)

    async def tracked_block(*args, **kwargs):
        order.append("restore-block")
        return await original_block(*args, **kwargs)

    monkeypatch.setattr(
        data_portability_service,
        "acquire_identity_governance_lock",
        tracked_governance,
    )
    monkeypatch.setattr(
        data_portability_service,
        "_single_local_subject_id",
        tracked_subject,
    )
    monkeypatch.setattr(
        data_portability_service,
        "_refuse_retained_raw_references",
        tracked_preflight,
    )
    monkeypatch.setattr(
        data_portability_service,
        "block_raw_ownership_backfill_for_portability_v1_restore",
        tracked_block,
    )
    old_raw = RawPayload(subject_id=legacy_owner_roots.subject_id, 
        domain=Domain.GENETICS.value,
        source="vcf_import",
        external_id="old-portability-raw",
        payload={"old": True},
    )
    db_session.add(old_raw)
    await db_session.flush()
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    checkpoint = OwnershipBackfillCheckpoint(
        phase_key=RAW_OWNERSHIP_BACKFILL_PHASE,
        subject_id=legacy_owner_roots.subject_id,
        status="completed",
        scan_high_watermark_id=old_raw.id,
        snapshot_rows=1,
        last_scanned_id=old_raw.id,
        scanned_rows=1,
        updated_rows=1,
        unchanged_rows=0,
        data_checksum_before="a" * 64,
        data_checksum_after="a" * 64,
        ownership_checksum_after="c" * 64,
        started_at=now,
        updated_at=now,
        completed_at=now,
    )
    unrelated = OwnershipBackfillCheckpoint(
        phase_key="synthetic.unrelated.v1",
        subject_id=legacy_owner_roots.subject_id,
        status="running",
        scan_high_watermark_id=0,
        snapshot_rows=0,
        last_scanned_id=0,
        scanned_rows=0,
        updated_rows=0,
        unchanged_rows=0,
        data_checksum_before="d" * 64,
        data_checksum_after="e" * 64,
        ownership_checksum_after="f" * 64,
        started_at=now,
        updated_at=now,
        completed_at=None,
    )
    db_session.add_all([checkpoint, unrelated])
    await db_session.commit()
    old_raw_id = old_raw.id

    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [
                {
                    "id": 11,
                    "domain": Domain.GENETICS.value,
                    "source": "vcf_import",
                    "external_id": "restored-portability-raw-11",
                    "payload": {"restored": True},
                    "_vitals_subject_bound": True,
                },
                {
                    "id": 37,
                    "domain": Domain.GENETICS.value,
                    "source": "vcf_import",
                    "external_id": "restored-portability-raw-37",
                    "payload": {"restored": True},
                    "_vitals_subject_bound": True,
                }
            ],
        },
    )
    assert order == [
        "governance",
        "local-subject",
        "retained-reference-preflight",
        "restore-block",
    ]

    blocked = await db_session.get(
        OwnershipBackfillCheckpoint,
        RAW_OWNERSHIP_BACKFILL_PHASE,
    )
    other = await db_session.get(
        OwnershipBackfillCheckpoint,
        unrelated.phase_key,
    )
    restored_low = await db_session.get(RawPayload, 11)
    restored = await db_session.get(RawPayload, 37)
    assert (
        blocked is not None
        and other is not None
        and restored_low is not None
        and restored is not None
    )
    assert (
        blocked.status,
        blocked.scan_high_watermark_id,
        blocked.snapshot_rows,
        blocked.last_scanned_id,
        blocked.scanned_rows,
        blocked.updated_rows,
        blocked.unchanged_rows,
        blocked.completed_at,
    ) == ("restore_blocked", 37, 2, 0, 0, 0, 0, None)
    assert other.status == "running" and other.data_checksum_before == "d" * 64
    assert (
        restored.subject_id,
        restored.actor_user_id,
        restored.integration_connection_id,
        restored.file_asset_id,
    ) == (legacy_owner_roots.subject_id, None, None, None)
    assert await db_session.scalar(
        select(RawPayload.id).where(RawPayload.id == old_raw_id)
    ) is None

    await db_session.rollback()
    original_checkpoint = (
        await db_session.execute(
            select(
                OwnershipBackfillCheckpoint.status,
                OwnershipBackfillCheckpoint.scan_high_watermark_id,
                OwnershipBackfillCheckpoint.snapshot_rows,
                OwnershipBackfillCheckpoint.completed_at,
            ).where(
                OwnershipBackfillCheckpoint.phase_key
                == RAW_OWNERSHIP_BACKFILL_PHASE
            )
        )
    ).one()
    assert (
        original_checkpoint.status,
        original_checkpoint.scan_high_watermark_id,
        original_checkpoint.snapshot_rows,
        original_checkpoint.completed_at is not None,
    ) == ("completed", old_raw_id, 1, True)
    assert await db_session.scalar(
        select(RawPayload.id).where(RawPayload.id == old_raw_id)
    ) == old_raw_id
    assert await db_session.scalar(
        select(RawPayload.id).where(RawPayload.id == 11)
    ) is None
    assert await db_session.scalar(
        select(RawPayload.id).where(RawPayload.id == 37)
    ) is None


async def test_full_import_completes_an_empty_raw_snapshot(
    db_session,
    legacy_owner_roots,
):
    old_raw = RawPayload(subject_id=legacy_owner_roots.subject_id, 
        domain=Domain.GENETICS.value,
        source=Source.VCF_IMPORT.value,
        external_id="empty-restore-removes-old-raw",
        payload={"old": True},
    )
    db_session.add(old_raw)
    await db_session.commit()
    old_raw_id = old_raw.id

    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    checkpoint = await db_session.get(
        OwnershipBackfillCheckpoint,
        RAW_OWNERSHIP_BACKFILL_PHASE,
    )
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
    ) == ("completed", 0, 0, 0, 0, 0, 0)
    assert checkpoint.completed_at is not None
    assert checkpoint.data_checksum_before == _EMPTY_SHA256
    assert checkpoint.data_checksum_after == _EMPTY_SHA256
    assert checkpoint.ownership_checksum_after == _EMPTY_SHA256
    assert await db_session.scalar(
        select(RawPayload.id).where(RawPayload.id == old_raw_id)
    ) is None


async def test_full_import_atomically_rebases_normalized_stage3b_checkpoints(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )
    await db_session.commit()

    checkpoints = list(
        await db_session.scalars(
            select(OwnershipBackfillCheckpoint).where(
                OwnershipBackfillCheckpoint.phase_key.in_(
                    tuple(NORMALIZED_MANUAL_CHECKPOINT_PHASES.values())
                )
            )
        )
    )
    assert len(checkpoints) == len(NORMALIZED_MANUAL_TABLES) == 17
    assert all(
        checkpoint.status == "completed"
        and checkpoint.scan_high_watermark_id == 0
        and checkpoint.snapshot_rows == 0
        and checkpoint.completed_at is not None
        for checkpoint in checkpoints
    )

    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "body_measurements": [
                {
                    "id": 37,
                    "date": "2026-08-21",
                    "domain": Domain.WEIGHT.value,
                    "source": Source.MANUAL.value,
                    "waist_cm": 91.5,
                    "_vitals_subject_bound": True,
                }
            ],
        },
    )

    body_phase = NORMALIZED_MANUAL_CHECKPOINT_PHASES["body_measurements"]
    body_checkpoint = await db_session.get(
        OwnershipBackfillCheckpoint, body_phase
    )
    restored = await db_session.get(BodyMeasurement, 37)
    assert body_checkpoint is not None and restored is not None
    assert (
        body_checkpoint.status,
        body_checkpoint.scan_high_watermark_id,
        body_checkpoint.snapshot_rows,
        body_checkpoint.last_scanned_id,
        body_checkpoint.scanned_rows,
        body_checkpoint.completed_at,
    ) == ("running", 37, 1, 0, 0, None)
    assert restored.subject_id == legacy_owner_roots.subject_id
    assert restored.actor_user_id is None
    assert all(
        checkpoint.status == "completed"
        for checkpoint in await db_session.scalars(
            select(OwnershipBackfillCheckpoint).where(
                OwnershipBackfillCheckpoint.phase_key.in_(
                    tuple(
                        phase
                        for table, phase in
                        NORMALIZED_MANUAL_CHECKPOINT_PHASES.items()
                        if table != "body_measurements"
                    )
                )
            )
        )
    )


async def test_full_import_atomically_rebases_hrt_child_stage3c_checkpoints(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )
    await db_session.commit()

    checkpoints = list(
        await db_session.scalars(
            select(OwnershipBackfillCheckpoint).where(
                OwnershipBackfillCheckpoint.phase_key.in_(
                    tuple(HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
                )
            )
        )
    )
    assert len(checkpoints) == len(HRT_CHILD_OWNERSHIP_BACKFILL_TABLES) == 2
    assert all(
        checkpoint.status == "completed"
        and checkpoint.scan_high_watermark_id == 0
        and checkpoint.snapshot_rows == 0
        and checkpoint.completed_at is not None
        for checkpoint in checkpoints
    )

    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "hrt_cycles": [
                {
                    "id": 31,
                    "domain": Domain.HRT.value,
                    "source": Source.MANUAL.value,
                    "name": "Historical cycle",
                    "kind": "course",
                    "start_date": "2026-01-01",
                }
            ],
            "hrt_cycle_items": [
                {
                    "id": 37,
                    "cycle_id": 31,
                    "compound_key": "testosterone_enanthate",
                    "unit": "mg",
                    "start_offset_days": 0,
                    "schedule": [
                        {
                            "dose": 125,
                            "interval_days": 3.5,
                            "duration_days": 28,
                        }
                    ],
                }
            ],
            "hrt_cycle_templates": [
                {
                    "id": 41,
                    "domain": Domain.HRT.value,
                    "source": Source.MANUAL.value,
                    "name": "Historical template",
                    "kind": "course",
                }
            ],
            "hrt_cycle_template_items": [
                {
                    "id": 43,
                    "template_id": 41,
                    "compound_key": "testosterone_enanthate",
                    "unit": "mg",
                    "start_offset_days": 0,
                    "schedule": [
                        {
                            "dose": 125,
                            "interval_days": 3.5,
                            "duration_days": 28,
                        }
                    ],
                }
            ],
        },
    )

    cycle = await db_session.get(HrtCycle, 31)
    cycle_item = await db_session.get(HrtCycleItem, 37)
    template = await db_session.get(HrtCycleTemplate, 41)
    template_item = await db_session.get(HrtCycleTemplateItem, 43)
    assert cycle is not None and cycle_item is not None
    assert template is not None and template_item is not None
    assert cycle.subject_id == legacy_owner_roots.subject_id
    assert template.subject_id == legacy_owner_roots.subject_id
    assert cycle_item.subject_id == legacy_owner_roots.subject_id
    assert template_item.subject_id == legacy_owner_roots.subject_id

    expected = {
        "hrt_cycle_items": (37, 1),
        "hrt_cycle_template_items": (43, 1),
    }
    for table_name, phase_key in (
        HRT_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.items()
    ):
        checkpoint = await db_session.get(
            OwnershipBackfillCheckpoint, phase_key
        )
        assert checkpoint is not None
        assert (
            checkpoint.status,
            checkpoint.scan_high_watermark_id,
            checkpoint.snapshot_rows,
            checkpoint.last_scanned_id,
            checkpoint.scanned_rows,
            checkpoint.completed_at,
        ) == ("running", *expected[table_name], 0, 0, None)


@pytest.mark.parametrize("bad_id", (0, -1, True, None, 2_147_483_648))
async def test_hrt_child_replacement_rejects_invalid_ids_before_mutation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    bad_id,
):
    called = False

    async def unexpected_reset(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        data_portability_service,
        "reset_hrt_child_backfill_for_portability_v1_restore",
        unexpected_reset,
    )
    row = {
        "cycle_id": 1,
        "compound_key": "testosterone_enanthate",
        "unit": "mg",
        "schedule": [],
    }
    if bad_id is not None:
        row["id"] = bad_id
    with pytest.raises(PortabilityError, match="positive integer id"):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                "hrt_cycle_items": [row],
            },
        )
    assert called is False


async def test_full_import_blocks_nonempty_provider_stage3d_snapshot(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [
                {
                    "id": 11,
                    "domain": Domain.GARMIN.value,
                    "source": Source.GARMIN_API.value,
                    "external_id": "daily:2026-08-21",
                    "payload": {"calendarDate": "2026-08-21"},
                    "_vitals_subject_bound": True,
                }
            ],
            "garmin_daily": [
                {
                    "id": 37,
                    "raw_payload_id": 11,
                    "date": "2026-08-21",
                    "domain": Domain.GARMIN.value,
                    "source": Source.GARMIN_API.value,
                    "steps": 1234,
                    "_vitals_subject_bound": True,
                }
            ],
        },
    )

    raw = await db_session.get(RawPayload, 11)
    daily = await db_session.get(GarminDaily, 37)
    assert raw is not None and daily is not None
    assert (
        raw.subject_id,
        raw.actor_user_id,
        raw.integration_connection_id,
        raw.file_asset_id,
    ) == (legacy_owner_roots.subject_id, None, None, None)
    assert (
        daily.subject_id,
        daily.actor_user_id,
        daily.integration_connection_id,
    ) == (legacy_owner_roots.subject_id, None, None)

    checkpoints = {
        row.phase_key: row
        for row in await db_session.scalars(
            select(OwnershipBackfillCheckpoint).where(
                OwnershipBackfillCheckpoint.phase_key.in_(
                    tuple(
                        PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values()
                    )
                )
            )
        )
    }
    assert len(checkpoints) == len(PROVIDER_RAW_OWNERSHIP_BACKFILL_TABLES) == 4
    daily_checkpoint = checkpoints[
        PROVIDER_RAW_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["garmin_daily"]
    ]
    assert (
        daily_checkpoint.status,
        daily_checkpoint.scan_high_watermark_id,
        daily_checkpoint.snapshot_rows,
        daily_checkpoint.last_scanned_id,
        daily_checkpoint.scanned_rows,
        daily_checkpoint.completed_at,
    ) == ("restore_blocked", 37, 1, 0, 0, None)
    assert all(
        checkpoint.status == "completed"
        and checkpoint.scan_high_watermark_id == 0
        and checkpoint.snapshot_rows == 0
        and checkpoint.completed_at is not None
        for phase, checkpoint in checkpoints.items()
        if phase != daily_checkpoint.phase_key
    )


@pytest.mark.parametrize("bad_id", (0, -1, True, None, 2_147_483_648))
async def test_provider_replacement_rejects_invalid_ids_before_mutation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    bad_id,
):
    called = False

    async def unexpected_block(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        data_portability_service,
        "block_provider_raw_ownership_backfill_for_portability_v1_restore",
        unexpected_block,
    )
    row = {
        "date": "2026-08-21",
        "domain": Domain.GARMIN.value,
        "source": Source.GARMIN_API.value,
    }
    if bad_id is not None:
        row["id"] = bad_id
    with pytest.raises(PortabilityError, match="positive integer id"):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                "garmin_daily": [row],
            },
        )
    assert called is False


async def test_full_import_blocks_nonempty_hevy_child_stage3e_snapshot(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "hevy_workouts": [
                {
                    "id": 5,
                    "external_id": "synthetic-workout",
                    "date": "2026-08-21",
                    "domain": Domain.WORKOUTS.value,
                    "source": Source.HEVY_API.value,
                    "_vitals_subject_bound": True,
                }
            ],
            "hevy_exercises": [
                {
                    "id": 11,
                    "workout_id": 5,
                    "exercise_index": 0,
                    "title": "Synthetic exercise",
                    "_vitals_subject_bound": True,
                }
            ],
            "hevy_sets": [
                {
                    "id": 17,
                    "exercise_id": 11,
                    "set_index": 0,
                    "set_type": "normal",
                    "reps": 5,
                    "_vitals_subject_bound": True,
                }
            ],
        },
    )

    exercise = await db_session.get(HevyExercise, 11)
    hevy_set = await db_session.get(HevySet, 17)
    assert exercise is not None and hevy_set is not None
    assert (
        exercise.subject_id,
        exercise.integration_connection_id,
        hevy_set.subject_id,
        hevy_set.integration_connection_id,
    ) == (legacy_owner_roots.subject_id, None, legacy_owner_roots.subject_id, None)

    checkpoints = {
        row.phase_key: row
        for row in await db_session.scalars(
            select(OwnershipBackfillCheckpoint).where(
                OwnershipBackfillCheckpoint.phase_key.in_(
                    tuple(HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values())
                )
            )
        )
    }
    assert len(checkpoints) == len(HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES) == 2
    for table_name, high_watermark in (
        ("hevy_exercises", 11),
        ("hevy_sets", 17),
    ):
        checkpoint = checkpoints[
            HEVY_CHILD_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[table_name]
        ]
        assert (
            checkpoint.status,
            checkpoint.scan_high_watermark_id,
            checkpoint.snapshot_rows,
            checkpoint.last_scanned_id,
            checkpoint.scanned_rows,
            checkpoint.completed_at,
        ) == ("restore_blocked", high_watermark, 1, 0, 0, None)


@pytest.mark.parametrize("table_name", HEVY_CHILD_OWNERSHIP_BACKFILL_TABLES)
@pytest.mark.parametrize("bad_id", (0, -1, True, None, 2_147_483_648))
async def test_hevy_child_replacement_rejects_invalid_ids_before_mutation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    table_name,
    bad_id,
):
    called = False

    async def unexpected_block(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        data_portability_service,
        "block_hevy_child_ownership_backfill_for_portability_v1_restore",
        unexpected_block,
    )
    row = {"workout_id": 1, "exercise_index": 0, "title": "Synthetic"}
    if table_name == "hevy_sets":
        row = {"exercise_id": 1, "set_index": 0, "set_type": "normal"}
    if bad_id is not None:
        row["id"] = bad_id
    with pytest.raises(PortabilityError, match="positive integer id"):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                table_name: [row],
            },
        )
    assert called is False


async def test_full_import_rebases_nonempty_hrt_compound_stage3f_snapshot(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "hrt_compounds": [
                {
                    "id": 31,
                    "domain": Domain.HRT.value,
                    "source": Source.MANUAL.value,
                    "key": "synthetic_custom_blend",
                    "name": "Synthetic custom blend",
                    "compound_class": "testosterone",
                    "route": "injection",
                    "dose_unit": "mg",
                    "active_fraction": 1.0,
                    "active": True,
                    "_vitals_subject_bound": True,
                }
            ],
            "hrt_compound_components": [
                {
                    "id": 37,
                    "compound_id": 31,
                    "ester": "synthetic_ester",
                    "mg": 100.0,
                    "_vitals_subject_bound": True,
                }
            ],
        },
    )

    compound = await db_session.get(HrtCompound, 31)
    component = await db_session.get(HrtCompoundComponent, 37)
    assert compound is not None and component is not None
    assert (
        compound.subject_id,
        compound.actor_user_id,
        component.subject_id,
    ) == (legacy_owner_roots.subject_id, None, legacy_owner_roots.subject_id)

    checkpoints = {
        row.phase_key: row
        for row in await db_session.scalars(
            select(OwnershipBackfillCheckpoint).where(
                OwnershipBackfillCheckpoint.phase_key.in_(
                    tuple(
                        HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES.values()
                    )
                )
            )
        )
    }
    assert len(checkpoints) == len(HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES) == 2
    for table_name, high_watermark in (
        ("hrt_compounds", 31),
        ("hrt_compound_components", 37),
    ):
        checkpoint = checkpoints[
            HRT_COMPOUND_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[table_name]
        ]
        assert (
            checkpoint.status,
            checkpoint.scan_high_watermark_id,
            checkpoint.snapshot_rows,
            checkpoint.last_scanned_id,
            checkpoint.scanned_rows,
            checkpoint.completed_at,
        ) == ("running", high_watermark, 1, 0, 0, None)


async def test_full_import_rebases_nonempty_conflict_rule_stage3g_snapshot(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "conflict_rules": [
                {
                    "id": 41,
                    "code": None,
                    "rule_type": "soft_warn",
                    "domain_a": Domain.WEIGHT.value,
                    "condition_a": {},
                    "domain_b": Domain.LABS.value,
                    "condition_b": {},
                    "severity": "warn",
                    "message": "Synthetic custom conflict rule",
                    "params": None,
                    "category": "synthetic",
                    "source": "synthetic evidence citation",
                    "evidence": "C",
                    "active": True,
                    "_vitals_subject_bound": True,
                }
            ],
        },
    )

    rule = await db_session.get(ConflictRule, 41)
    assert rule is not None
    assert rule.subject_id == legacy_owner_roots.subject_id
    phase = CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["conflict_rules"]
    checkpoint = await db_session.scalar(
        select(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key == phase
        )
    )
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.completed_at,
    ) == ("running", 41, 1, 0, 0, None)


async def test_full_import_atomically_restores_current_conflict_catalog(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    definitions = conflict_catalog.load_rule_catalog()
    rows = list(await db_session.scalars(select(ConflictRule)))
    assert {row.code for row in rows} == {entry["code"] for entry in definitions}
    phase = CONFLICT_RULE_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["conflict_rules"]
    checkpoint = await db_session.scalar(
        select(OwnershipBackfillCheckpoint).where(
            OwnershipBackfillCheckpoint.phase_key == phase
        )
    )
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
    ) == ("completed", 0, 0)


async def test_full_import_rejects_orphaned_conflict_alert_atomically(
    db_session,
    legacy_owner_roots,
):
    custom = ConflictRule(
        subject_id=legacy_owner_roots.subject_id,
        code=None,
        rule_type="soft_warn",
        domain_a=Domain.WEIGHT.value,
        condition_a={},
        domain_b=Domain.LABS.value,
        condition_b={},
        severity="warn",
        message="Synthetic retained custom conflict",
        active=True,
    )
    db_session.add(custom)
    await db_session.flush()
    db_session.add(
        SystemAlert(
            subject_id=legacy_owner_roots.subject_id,
            domain=Domain.WEIGHT.value,
            severity="warn",
            message="Synthetic retained alert",
            alert_key="conflict:999999",
            entity_ref="synthetic",
        )
    )
    custom_id = custom.id
    await db_session.commit()
    db_session.expunge(custom)

    with pytest.raises(
        PortabilityError,
        match="conflict-rule catalog validation rejected",
    ):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                "system_alerts": [
                    {
                        "id": 77,
                        "domain": Domain.WEIGHT.value,
                        "severity": "warn",
                        "message": "Orphaned imported conflict alert",
                        "alert_key": "conflict:999999",
                        "entity_ref": "synthetic-import",
                        "_vitals_subject_bound": True,
                    }
                ],
            },
        )
    await db_session.rollback()

    assert await db_session.get(ConflictRule, custom_id) is not None
    assert await db_session.scalar(
        select(SystemAlert.id).where(SystemAlert.alert_key == "conflict:999999")
    ) is not None


@pytest.mark.parametrize("bad_id", (0, -1, True, None, 2_147_483_648))
async def test_conflict_rule_replacement_rejects_invalid_ids_before_mutation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    bad_id,
):
    called = False

    async def unexpected_reset(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        data_portability_service,
        "reset_conflict_rule_backfill_for_portability_v1_restore",
        unexpected_reset,
    )
    row = {
        "code": None,
        "rule_type": "soft_warn",
        "domain_a": Domain.WEIGHT.value,
        "condition_a": {},
        "domain_b": Domain.LABS.value,
        "condition_b": {},
        "severity": "warn",
        "message": "Synthetic custom conflict rule",
        "active": True,
        "_vitals_subject_bound": True,
    }
    if bad_id is not None:
        row["id"] = bad_id
    with pytest.raises(PortabilityError, match="positive integer id"):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                CONFLICT_RULE_OWNERSHIP_BACKFILL_TABLES[0]: [row],
            },
        )
    assert called is False


def _portable_progress_photo(
    *,
    row_id: int = 41,
    file_key: str = "uploads/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
) -> dict:
    return {
        "id": row_id,
        "date": "2026-08-21",
        "domain": Domain.WEIGHT.value,
        "source": Source.MANUAL.value,
        "file_key": file_key,
        "note": "synthetic portable photo",
        "_vitals_subject_bound": True,
    }


async def test_full_import_blocks_nonempty_progress_photo_stage3h_without_assets(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "progress_photos": [_portable_progress_photo()],
        },
    )

    photo = await db_session.get(ProgressPhoto, 41)
    assert photo is not None
    assert (
        photo.subject_id,
        photo.actor_user_id,
        photo.file_asset_id,
    ) == (legacy_owner_roots.subject_id, None, None)
    assert await db_session.scalar(select(FileAsset.id)) is None

    phase = PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
        "progress_photos"
    ]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
        checkpoint.completed_at,
    ) == ("restore_blocked", 41, 1, 0, 0, 0, 0, None)
    assert checkpoint.data_checksum_before == _EMPTY_SHA256
    assert checkpoint.data_checksum_after == _EMPTY_SHA256
    assert checkpoint.ownership_checksum_after == _EMPTY_SHA256


async def test_full_import_completes_exact_empty_progress_photo_stage3h(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    phase = PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
        "progress_photos"
    ]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
    ) == ("completed", 0, 0, 0, 0, 0, 0)
    assert checkpoint.completed_at is not None


async def test_full_import_retires_only_outgoing_progress_asset_without_bytes(
    db_session,
    legacy_owner_roots,
):
    key = "uploads/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.webp"
    asset = FileAsset(
        subject_id=legacy_owner_roots.subject_id,
        uploaded_by_user_id=legacy_owner_roots.user_id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO.value,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        storage_ref=key,
        status=FileAssetStatus.LEGACY_PLACEHOLDER.value,
    )
    db_session.add(asset)
    await db_session.flush()
    photo = ProgressPhoto(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        file_asset_id=asset.id,
        date=date(2026, 8, 20),
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        file_key=key,
    )
    db_session.add(photo)
    await db_session.commit()
    asset_id = asset.id
    photo_id = photo.id

    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    db_session.expire_all()
    assert await db_session.get(ProgressPhoto, photo_id) is None
    retired = await db_session.get(FileAsset, asset_id)
    assert retired is not None
    assert retired.status == FileAssetStatus.DELETED.value
    assert retired.deleted_at is not None
    assert retired.purged_at is None


@pytest.mark.parametrize(
    "bad_key",
    (
        "uploads/labs/aliased.jpg",
        "uploads/body/aliased.png",
        "uploads/../escaped.jpg",
        "uploads/not-an-image.svg",
    ),
)
async def test_full_import_rejects_unsafe_progress_photo_atomically(
    db_session,
    legacy_owner_roots,
    bad_key,
):
    key = "uploads/cccccccccccccccccccccccccccccccc.jpeg"
    asset = FileAsset(
        subject_id=legacy_owner_roots.subject_id,
        uploaded_by_user_id=legacy_owner_roots.user_id,
        purpose=FileAssetPurpose.PROGRESS_PHOTO.value,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        storage_ref=key,
        status=FileAssetStatus.LEGACY_PLACEHOLDER.value,
    )
    db_session.add(asset)
    await db_session.flush()
    photo = ProgressPhoto(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        file_asset_id=asset.id,
        date=date(2026, 8, 20),
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        file_key=key,
    )
    db_session.add(photo)
    await db_session.commit()
    asset_id = asset.id
    photo_id = photo.id

    with pytest.raises(
        PortabilityError,
        match="progress-photo validation rejected",
    ):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                "progress_photos": [
                    _portable_progress_photo(row_id=77, file_key=bad_key)
                ],
            },
        )
    await db_session.rollback()

    restored_photo = await db_session.get(ProgressPhoto, photo_id)
    restored_asset = await db_session.get(FileAsset, asset_id)
    assert restored_photo is not None
    assert restored_asset is not None
    assert restored_asset.status == FileAssetStatus.LEGACY_PLACEHOLDER.value
    assert restored_asset.deleted_at is None


async def test_full_import_rejects_partial_outgoing_progress_graph_atomically(
    db_session,
    legacy_owner_roots,
):
    partial = ProgressPhoto(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=None,
        file_asset_id=None,
        date=date(2026, 8, 20),
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        file_key="uploads/dddddddddddddddddddddddddddddddd.jpg",
    )
    db_session.add(partial)
    await db_session.commit()
    partial_id = partial.id

    with pytest.raises(
        PortabilityError,
        match="progress-photo ownership restore block",
    ):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
            },
        )
    await db_session.rollback()

    restored = await db_session.get(ProgressPhoto, partial_id)
    assert restored is not None
    assert (
        restored.subject_id,
        restored.actor_user_id,
        restored.file_asset_id,
    ) == (legacy_owner_roots.subject_id, None, None)
    phase = PROGRESS_PHOTO_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
        "progress_photos"
    ]
    assert await db_session.get(OwnershipBackfillCheckpoint, phase) is None


@pytest.mark.parametrize("bad_id", (0, -1, True, None, 2_147_483_648))
async def test_progress_photo_replacement_rejects_invalid_ids_before_mutation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    bad_id,
):
    called = False

    async def unexpected_block(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        data_portability_service,
        "block_progress_photo_ownership_backfill_for_portability_v1_restore",
        unexpected_block,
    )
    row = _portable_progress_photo()
    if bad_id is None:
        row.pop("id")
    else:
        row["id"] = bad_id
    with pytest.raises(PortabilityError, match="positive integer id"):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                PROGRESS_PHOTO_OWNERSHIP_BACKFILL_TABLES[0]: [row],
            },
        )
    assert called is False


async def test_full_import_calls_progress_block_after_conflict_reset(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    order: list[str] = []
    original_conflict_reset = (
        data_portability_service.reset_conflict_rule_backfill_for_portability_v1_restore
    )

    async def tracked_conflict_reset(*args, **kwargs):
        order.append("conflict")
        return await original_conflict_reset(*args, **kwargs)

    async def stopping_progress_block(*args, **kwargs):
        order.append("progress")
        raise ProgressPhotoOwnershipBackfillStateError("synthetic stop")

    monkeypatch.setattr(
        data_portability_service,
        "reset_conflict_rule_backfill_for_portability_v1_restore",
        tracked_conflict_reset,
    )
    monkeypatch.setattr(
        data_portability_service,
        "block_progress_photo_ownership_backfill_for_portability_v1_restore",
        stopping_progress_block,
    )
    with pytest.raises(PortabilityError, match="progress-photo ownership restore block"):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
            },
        )
    await db_session.rollback()
    assert order == ["conflict", "progress"]


async def test_full_import_preflights_progress_after_conflict_rules(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    order: list[str] = []
    original_conflict_preflight = (
        data_portability_service.preflight_conflict_rule_ownership_backfill
    )
    original_progress_preflight = (
        data_portability_service.preflight_progress_photo_ownership_backfill
    )

    async def tracked_conflict_preflight(*args, **kwargs):
        order.append("conflict")
        return await original_conflict_preflight(*args, **kwargs)

    async def tracked_progress_preflight(*args, **kwargs):
        order.append("progress")
        return await original_progress_preflight(*args, **kwargs)

    monkeypatch.setattr(
        data_portability_service,
        "preflight_conflict_rule_ownership_backfill",
        tracked_conflict_preflight,
    )
    monkeypatch.setattr(
        data_portability_service,
        "preflight_progress_photo_ownership_backfill",
        tracked_progress_preflight,
    )
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    assert order == ["conflict", "progress"]


def _portable_day_context(*, row_id: int = 51) -> dict:
    return {
        "id": row_id,
        "date": "2026-08-21",
        "domain": Domain.SIGNALS.value,
        "source": Source.MANUAL.value,
        "answers": {"gym": True},
        "planned": {"where": "remote"},
        "_vitals_subject_bound": True,
    }


async def test_full_import_resets_nonempty_day_context_stage3i_snapshot(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "day_context": [_portable_day_context()],
        },
    )

    context = await db_session.get(DayContext, 51)
    assert context is not None
    assert (
        context.subject_id,
        context.actor_user_id,
        context.integration_connection_id,
        context.answers,
        context.planned,
    ) == (
        legacy_owner_roots.subject_id,
        None,
        None,
        {"gym": True},
        {"where": "remote"},
    )
    phase = DAY_CONTEXT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["day_context"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
        checkpoint.completed_at,
    ) == ("running", 51, 1, 0, 0, 0, 0, None)


async def test_full_import_completes_exact_empty_day_context_stage3i(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    phase = DAY_CONTEXT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["day_context"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
    ) == ("completed", 0, 0, 0, 0, 0, 0)
    assert checkpoint.completed_at is not None


@pytest.mark.parametrize("bad_id", (0, -1, True, None, 2_147_483_648))
async def test_day_context_replacement_rejects_invalid_ids_before_mutation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    bad_id,
):
    called = False

    async def unexpected_reset(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        data_portability_service,
        "reset_day_context_ownership_backfill_for_portability_v1_restore",
        unexpected_reset,
    )
    row = _portable_day_context()
    if bad_id is None:
        row.pop("id")
    else:
        row["id"] = bad_id
    with pytest.raises(PortabilityError, match="positive integer id"):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                DAY_CONTEXT_OWNERSHIP_BACKFILL_TABLES[0]: [row],
            },
        )
    assert called is False


async def test_full_import_calls_day_context_reset_after_progress_block(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    order: list[str] = []
    original_progress_block = (
        data_portability_service.block_progress_photo_ownership_backfill_for_portability_v1_restore
    )

    async def tracked_progress_block(*args, **kwargs):
        order.append("progress")
        return await original_progress_block(*args, **kwargs)

    async def stopping_day_context_reset(*args, **kwargs):
        order.append("day_context")
        raise DayContextOwnershipBackfillStateError("synthetic stop")

    monkeypatch.setattr(
        data_portability_service,
        "block_progress_photo_ownership_backfill_for_portability_v1_restore",
        tracked_progress_block,
    )
    monkeypatch.setattr(
        data_portability_service,
        "reset_day_context_ownership_backfill_for_portability_v1_restore",
        stopping_day_context_reset,
    )
    with pytest.raises(PortabilityError, match="day-context ownership restore reset"):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
            },
        )
    await db_session.rollback()
    assert order == ["progress", "day_context"]


async def test_full_import_preflights_day_context_after_progress(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    order: list[str] = []
    original_progress_preflight = (
        data_portability_service.preflight_progress_photo_ownership_backfill
    )
    original_day_context_preflight = (
        data_portability_service.preflight_day_context_ownership_backfill
    )

    async def tracked_progress_preflight(*args, **kwargs):
        order.append("progress")
        return await original_progress_preflight(*args, **kwargs)

    async def tracked_day_context_preflight(*args, **kwargs):
        order.append("day_context")
        return await original_day_context_preflight(*args, **kwargs)

    monkeypatch.setattr(
        data_portability_service,
        "preflight_progress_photo_ownership_backfill",
        tracked_progress_preflight,
    )
    monkeypatch.setattr(
        data_portability_service,
        "preflight_day_context_ownership_backfill",
        tracked_day_context_preflight,
    )
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    assert order == ["progress", "day_context"]


async def test_day_context_post_load_rejection_rolls_back_whole_replacement(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    old = DayContext(subject_id=legacy_owner_roots.subject_id, 
        date=date(2026, 8, 20),
        domain=Domain.SIGNALS.value,
        source=Source.MANUAL.value,
        answers={"load": "heavy"},
        planned={"where": "office"},
    )
    db_session.add(old)
    await db_session.commit()
    old_id = old.id

    async def rejected_preflight(*args, **kwargs):
        raise DayContextOwnershipBackfillStateError("sensitive synthetic state")

    monkeypatch.setattr(
        data_portability_service,
        "preflight_day_context_ownership_backfill",
        rejected_preflight,
    )
    with pytest.raises(
        PortabilityError,
        match="day-context validation rejected the portable restore",
    ):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                "day_context": [_portable_day_context(row_id=52)],
            },
        )
    await db_session.rollback()

    restored = await db_session.get(DayContext, old_id)
    assert restored is not None
    assert restored.answers == {"load": "heavy"}
    assert await db_session.get(DayContext, 52) is None


def _portable_signal(*, row_id: int = 61) -> dict:
    return {
        "id": row_id,
        "date": "2026-08-21",
        "domain": Domain.SIGNALS.value,
        "source": Source.MCP.value,
        "kind": "state",
        "key": "synthetic_restore_signal",
        "value_num": 3.0,
        "unit": None,
        "note": "synthetic only",
        "at_time": None,
        "raw_id": None,
        "batch_id": "restore-batch",
        "misparse": False,
        "_vitals_subject_bound": True,
    }


async def test_full_import_resets_nonempty_signal_stage3j_snapshot(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "signals": [_portable_signal()],
        },
    )

    signal = await db_session.get(Signal, 61)
    assert signal is not None
    assert (
        signal.subject_id,
        signal.actor_user_id,
        signal.integration_connection_id,
        signal.key,
        signal.raw_id,
    ) == (
        legacy_owner_roots.subject_id,
        None,
        None,
        "synthetic_restore_signal",
        None,
    )
    phase = SIGNAL_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["signals"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
        checkpoint.completed_at,
    ) == ("running", 61, 1, 0, 0, 0, 0, None)


async def test_full_import_completes_exact_empty_signal_stage3j(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    phase = SIGNAL_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["signals"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
    ) == ("completed", 0, 0, 0, 0, 0, 0)
    assert checkpoint.completed_at is not None


@pytest.mark.parametrize("bad_id", (0, -1, True, None, 2_147_483_648))
async def test_signal_replacement_rejects_invalid_ids_before_mutation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    bad_id,
):
    called = False

    async def unexpected_reset(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        data_portability_service,
        "reset_signal_ownership_backfill_for_portability_v1_restore",
        unexpected_reset,
    )
    row = _portable_signal()
    if bad_id is None:
        row.pop("id")
    else:
        row["id"] = bad_id
    with pytest.raises(PortabilityError, match="positive integer id"):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                SIGNAL_OWNERSHIP_BACKFILL_TABLES[0]: [row],
            },
        )
    assert called is False


async def test_full_import_calls_signal_reset_after_day_context_reset(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    order: list[str] = []
    original_day_context_reset = (
        data_portability_service.reset_day_context_ownership_backfill_for_portability_v1_restore
    )

    async def tracked_day_context_reset(*args, **kwargs):
        order.append("day_context")
        return await original_day_context_reset(*args, **kwargs)

    async def stopping_signal_reset(*args, **kwargs):
        order.append("signals")
        raise SignalOwnershipBackfillStateError("synthetic stop")

    monkeypatch.setattr(
        data_portability_service,
        "reset_day_context_ownership_backfill_for_portability_v1_restore",
        tracked_day_context_reset,
    )
    monkeypatch.setattr(
        data_portability_service,
        "reset_signal_ownership_backfill_for_portability_v1_restore",
        stopping_signal_reset,
    )
    with pytest.raises(PortabilityError, match="signal ownership restore reset"):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
            },
        )
    await db_session.rollback()
    assert order == ["day_context", "signals"]


async def test_full_import_preflights_signals_after_day_context(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    order: list[str] = []
    original_day_context_preflight = (
        data_portability_service.preflight_day_context_ownership_backfill
    )
    original_signal_preflight = (
        data_portability_service.preflight_signal_ownership_backfill
    )

    async def tracked_day_context_preflight(*args, **kwargs):
        order.append("day_context")
        return await original_day_context_preflight(*args, **kwargs)

    async def tracked_signal_preflight(*args, **kwargs):
        order.append("signals")
        return await original_signal_preflight(*args, **kwargs)

    monkeypatch.setattr(
        data_portability_service,
        "preflight_day_context_ownership_backfill",
        tracked_day_context_preflight,
    )
    monkeypatch.setattr(
        data_portability_service,
        "preflight_signal_ownership_backfill",
        tracked_signal_preflight,
    )
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    assert order == ["day_context", "signals"]


async def test_signal_post_load_rejection_rolls_back_whole_replacement(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    old = Signal(subject_id=legacy_owner_roots.subject_id, 
        date=date(2026, 8, 20),
        domain=Domain.SIGNALS.value,
        source=Source.MCP.value,
        kind="state",
        key="old_signal",
        value_num=2.0,
        batch_id="old-batch",
        misparse=False,
    )
    db_session.add(old)
    await db_session.commit()
    old_id = old.id

    async def rejected_preflight(*args, **kwargs):
        raise SignalOwnershipBackfillStateError("sensitive synthetic state")

    monkeypatch.setattr(
        data_portability_service,
        "preflight_signal_ownership_backfill",
        rejected_preflight,
    )
    with pytest.raises(
        PortabilityError,
        match="signal validation rejected the portable restore",
    ):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                "signals": [_portable_signal(row_id=62)],
            },
        )
    await db_session.rollback()

    restored = await db_session.get(Signal, old_id)
    assert restored is not None
    assert restored.key == "old_signal"
    assert await db_session.get(Signal, 62) is None


def _retained_shared_report(*, token: str = "retained-stage3k", legacy_owner_roots) -> SharedReport:
    return SharedReport(subject_id=legacy_owner_roots.subject_id, 
        token=token,
        password_hash="$2b$04$synthetic-stage3k-hash",
        title="Synthetic retained report",
        domains=[Domain.LABS.value],
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 21),
        snapshot={"synthetic": {"marker": "redacted"}},
        expires_at=datetime(2030, 1, 1),
    )


async def test_full_import_prepares_nonempty_retained_shared_report_stage3k(
    db_session,
    legacy_owner_roots,
):
    report = _retained_shared_report(legacy_owner_roots=legacy_owner_roots)
    db_session.add(report)
    await db_session.commit()
    before = (
        report.id,
        report.subject_id,
        report.created_by_user_id,
        report.revoked_by_user_id,
        report.token,
        report.password_hash,
        report.snapshot,
        report.created_at,
        report.updated_at,
    )

    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    retained = await db_session.get(SharedReport, report.id)
    assert retained is not None
    assert (
        retained.id,
        retained.subject_id,
        retained.created_by_user_id,
        retained.revoked_by_user_id,
        retained.token,
        retained.password_hash,
        retained.snapshot,
        retained.created_at,
        retained.updated_at,
    ) == before
    phase = SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["shared_reports"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
        checkpoint.completed_at,
    ) == ("running", report.id, 1, 0, 0, 0, 0, None)


async def test_full_import_completes_exact_empty_retained_shared_report_stage3k(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    phase = SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["shared_reports"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
    ) == ("completed", 0, 0, 0, 0, 0, 0)
    assert checkpoint.completed_at is not None


async def test_full_import_prepares_shared_reports_after_signal_reset(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    order: list[str] = []
    original_signal_reset = (
        data_portability_service.reset_signal_ownership_backfill_for_portability_v1_restore
    )

    async def tracked_signal_reset(*args, **kwargs):
        order.append("signals")
        return await original_signal_reset(*args, **kwargs)

    async def stopping_shared_report_prepare(*args, **kwargs):
        order.append("shared_reports")
        raise SharedReportOwnershipBackfillStateError("synthetic stop")

    monkeypatch.setattr(
        data_portability_service,
        "reset_signal_ownership_backfill_for_portability_v1_restore",
        tracked_signal_reset,
    )
    monkeypatch.setattr(
        data_portability_service,
        "prepare_shared_report_ownership_backfill_for_portability_v1_restore",
        stopping_shared_report_prepare,
    )
    with pytest.raises(
        PortabilityError,
        match="shared-report ownership restore preparation",
    ):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
            },
        )
    await db_session.rollback()
    assert order == ["signals", "shared_reports"]


async def test_full_import_preflights_shared_reports_after_signals(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    order: list[str] = []
    original_signal_preflight = (
        data_portability_service.preflight_signal_ownership_backfill
    )
    original_shared_report_preflight = (
        data_portability_service.preflight_shared_report_ownership_backfill
    )

    async def tracked_signal_preflight(*args, **kwargs):
        order.append("signals")
        return await original_signal_preflight(*args, **kwargs)

    async def tracked_shared_report_preflight(*args, **kwargs):
        order.append("shared_reports")
        return await original_shared_report_preflight(*args, **kwargs)

    monkeypatch.setattr(
        data_portability_service,
        "preflight_signal_ownership_backfill",
        tracked_signal_preflight,
    )
    monkeypatch.setattr(
        data_portability_service,
        "preflight_shared_report_ownership_backfill",
        tracked_shared_report_preflight,
    )
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    assert order == ["signals", "shared_reports"]


async def test_shared_report_post_load_rejection_rolls_back_whole_replacement(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    old = Signal(subject_id=legacy_owner_roots.subject_id, 
        date=date(2026, 8, 20),
        domain=Domain.SIGNALS.value,
        source=Source.MCP.value,
        kind="state",
        key="retained_report_rollback_signal",
        value_num=2.0,
        batch_id="old-batch",
        misparse=False,
    )
    report = _retained_shared_report(token="retained-stage3k-rollback", legacy_owner_roots=legacy_owner_roots)
    db_session.add_all([old, report])
    await db_session.commit()
    old_id = old.id
    report_id = report.id

    async def rejected_preflight(*args, **kwargs):
        raise SharedReportOwnershipBackfillStateError("sensitive synthetic state")

    monkeypatch.setattr(
        data_portability_service,
        "preflight_shared_report_ownership_backfill",
        rejected_preflight,
    )
    with pytest.raises(
        PortabilityError,
        match="shared-report validation rejected the portable restore",
    ):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                "signals": [_portable_signal(row_id=63)],
            },
        )
    await db_session.rollback()

    restored = await db_session.get(Signal, old_id)
    retained = await db_session.get(SharedReport, report_id)
    assert restored is not None
    assert restored.key == "retained_report_rollback_signal"
    assert await db_session.get(Signal, 63) is None
    assert retained is not None
    assert retained.token == "retained-stage3k-rollback"
    phase = SHARED_REPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["shared_reports"]
    assert await db_session.get(OwnershipBackfillCheckpoint, phase) is None


def _portable_weight_log(*, row_id: int = 71) -> dict:
    return {
        "id": row_id,
        "date": "2026-08-21",
        "domain": Domain.WEIGHT.value,
        "source": Source.MANUAL.value,
        "weight_kg": 81.5,
        "note": "synthetic only",
        "superseded": False,
        "raw_payload_id": None,
        "_vitals_subject_bound": True,
    }


async def test_full_import_resets_nonempty_weight_log_stage3l_snapshot(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "weight_logs": [_portable_weight_log()],
        },
    )

    row = await db_session.get(WeightLog, 71)
    assert row is not None
    assert (
        row.subject_id,
        row.actor_user_id,
        row.integration_connection_id,
        row.raw_payload_id,
        row.weight_kg,
    ) == (legacy_owner_roots.subject_id, None, None, None, 81.5)
    phase = WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["weight_logs"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
        checkpoint.completed_at,
    ) == ("running", 71, 1, 0, 0, 0, 0, None)


async def test_full_import_completes_exact_empty_weight_log_stage3l(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    phase = WEIGHT_LOG_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["weight_logs"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
    ) == ("completed", 0, 0, 0, 0, 0, 0)
    assert checkpoint.completed_at is not None


def _portable_lab_result(*, row_id: int = 81) -> dict:
    return {
        "id": row_id,
        "date": "2026-08-21",
        "domain": Domain.LABS.value,
        "source": Source.MANUAL.value,
        "marker": "ferritin",
        "value": 45.0,
        "unit": "ng/mL",
        "ref_low": 30.0,
        "ref_high": 400.0,
        "flag": "normal",
        "lab_name": "Synthetic Lab",
        "note": "synthetic only",
        "raw_payload_id": None,
        "_vitals_subject_bound": True,
    }


async def test_full_import_resets_nonempty_lab_result_stage3m_snapshot(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "lab_results": [_portable_lab_result()],
        },
    )

    row = await db_session.get(LabResult, 81)
    assert row is not None
    assert (
        row.subject_id,
        row.actor_user_id,
        row.raw_payload_id,
        row.marker,
        row.value,
    ) == (legacy_owner_roots.subject_id, None, None, "ferritin", 45.0)
    phase = LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["lab_results"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
        checkpoint.completed_at,
    ) == ("running", 81, 1, 0, 0, 0, 0, None)


async def test_full_import_completes_exact_empty_lab_result_stage3m(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    phase = LAB_RESULT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["lab_results"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
    ) == ("completed", 0, 0, 0, 0, 0, 0)
    assert checkpoint.completed_at is not None


def _portable_genetic_variant(*, row_id: int = 91) -> dict:
    return {
        "id": row_id,
        "domain": Domain.GENETICS.value,
        "source": Source.MANUAL.value,
        "gene": "HFE",
        "rsid": "rs-synthetic-restore",
        "genotype": "CG",
        "marker": "hemochromatosis_carrier",
        "impact": "carrier",
        "impact_domain": Domain.SUPPLEMENTS.value,
        "interpretation": "synthetic only",
        "action_notes": None,
        "raw_payload_id": None,
        "_vitals_subject_bound": True,
    }


async def test_full_import_resets_nonempty_genetic_variant_stage3n_snapshot(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "genetic_variants": [_portable_genetic_variant()],
        },
    )

    row = await db_session.get(GeneticVariant, 91)
    assert row is not None
    assert (
        row.subject_id,
        row.actor_user_id,
        row.raw_payload_id,
        row.gene,
        row.rsid,
    ) == (legacy_owner_roots.subject_id, None, None, "HFE", "rs-synthetic-restore")
    phase = GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["genetic_variants"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.last_scanned_id,
        checkpoint.scanned_rows,
        checkpoint.updated_rows,
        checkpoint.unchanged_rows,
        checkpoint.completed_at,
    ) == ("running", 91, 1, 0, 0, 0, 0, None)


async def test_full_import_completes_exact_empty_genetic_variant_stage3n(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    phase = GENETIC_VARIANT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["genetic_variants"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.scanned_rows,
    ) == ("completed", 0, 0, 0)
    assert checkpoint.completed_at is not None


def _portable_body_scan(*, row_id: int = 101) -> dict:
    return {
        "id": row_id,
        "date": "2026-08-21",
        "domain": Domain.BODY_COMPOSITION.value,
        "source": Source.MANUAL.value,
        "device": "InBody 770",
        "file_key": None,
        "raw_payload_id": None,
        "note": "synthetic only",
        "_vitals_subject_bound": True,
    }


async def test_full_import_blocks_nonempty_body_scan_stage3o_snapshot(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "body_scans": [_portable_body_scan()],
        },
    )

    row = await db_session.get(BodyScan, 101)
    assert row is not None
    assert (row.subject_id, row.actor_user_id, row.file_asset_id) == (
        legacy_owner_roots.subject_id,
        None,
        None,
    )
    phase = BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["body_scans"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.completed_at,
    ) == ("restore_blocked", 101, 1, None)


async def test_full_import_completes_exact_empty_body_scan_stage3o(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    phase = BODY_SCAN_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["body_scans"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
    ) == ("completed", 0, 0)
    assert checkpoint.completed_at is not None


async def test_full_import_resets_system_alert_stage3t_snapshot(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "system_alerts": [
                {
                    "id": 151,
                    "domain": Domain.WEIGHT.value,
                    "severity": "warn",
                    "message": "synthetic only",
                    "alert_key": "weight.noisy_period_active",
                    "entity_ref": "weight:1",
                    "_vitals_subject_bound": True,
                }
            ],
        },
    )

    row = await db_session.get(SystemAlert, 151)
    assert row is not None
    assert row.subject_id == legacy_owner_roots.subject_id
    assert row.integration_connection_id is None
    phase = SYSTEM_ALERT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["system_alerts"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.completed_at,
    ) == ("running", 151, 1, None)


async def test_system_alert_post_load_rejection_rolls_back_replacement(
    db_session,
    legacy_owner_roots,
):
    async def rejected_preflight(*args, **kwargs):
        raise SystemAlertOwnershipBackfillStateError("sensitive synthetic state")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            data_portability_service,
            "preflight_system_alert_ownership_backfill",
            rejected_preflight,
        )
        with pytest.raises(
            PortabilityError,
            match="system-alert validation rejected the portable restore",
        ):
            await import_full(
                db_session,
                {
                    "metadata": {"version": "1.0", "kind": "full_backup"},
                    "raw_payloads": [],
                },
            )
    await db_session.rollback()


async def test_retained_notifications_survive_import_and_prepare_stage3s(
    db_session,
    legacy_owner_roots,
):
    from vitals.models.proactive import Notification

    retained = Notification(
        subject_id=legacy_owner_roots.subject_id,
        recipient_user_id=legacy_owner_roots.user_id,
        integration_connection_id=await _legacy_telegram_recipient_id(db_session),
        sent_at=datetime(2026, 8, 21, 8, 0),
        category="brief",
        dedupe_key="brief:2026-08-21",
        channel="telegram",
        external_id="4242",
        payload={"text": "synthetic retained message"},
    )
    db_session.add(retained)
    await db_session.commit()
    retained_id = retained.id

    snapshot = await export_full(db_session)
    assert "notifications" not in snapshot

    await import_full(db_session, snapshot)
    await db_session.commit()

    survivor = await db_session.get(Notification, retained_id)
    assert survivor is not None
    assert survivor.recipient_user_id == legacy_owner_roots.user_id
    assert survivor.dedupe_key == "brief:2026-08-21"
    phase = NOTIFICATION_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["notifications"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert checkpoint.status == "running"
    assert (checkpoint.scan_high_watermark_id, checkpoint.snapshot_rows) == (
        retained_id,
        1,
    )


async def _legacy_telegram_recipient_id(session):
    from sqlalchemy import select as sa_select

    from vitals.models.tenancy import IntegrationConnection

    return await session.scalar(
        sa_select(IntegrationConnection.id).where(
            IntegrationConnection.provider == "telegram",
            IntegrationConnection.connection_type == "recipient",
        )
    )


async def test_notification_post_load_rejection_rolls_back_replacement(
    db_session,
    legacy_owner_roots,
):
    async def rejected_preflight(*args, **kwargs):
        raise NotificationOwnershipBackfillStateError("sensitive synthetic state")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            data_portability_service,
            "preflight_notification_ownership_backfill",
            rejected_preflight,
        )
        with pytest.raises(
            PortabilityError,
            match="notification validation rejected the portable restore",
        ):
            await import_full(
                db_session,
                {
                    "metadata": {"version": "1.0", "kind": "full_backup"},
                    "raw_payloads": [],
                },
            )
    await db_session.rollback()


async def test_retained_weekly_digests_survive_import_and_prepare_stage3r(
    db_session,
    legacy_owner_roots,
):
    from vitals.models.milestones import WeeklyDigest

    retained = WeeklyDigest(
        subject_id=legacy_owner_roots.subject_id,
        date=date(2026, 8, 15),
        domain=Domain.MILESTONES.value,
        source=Source.SCHEDULER.value,
        kind="weekly",
        content="synthetic retained narrative",
        model="synthetic/digest",
    )
    db_session.add(retained)
    await db_session.commit()
    retained_id = retained.id

    snapshot = await export_full(db_session)
    assert "weekly_digests" not in snapshot

    await import_full(db_session, snapshot)
    await db_session.commit()

    survivor = await db_session.get(WeeklyDigest, retained_id)
    assert survivor is not None
    assert survivor.content == "synthetic retained narrative"
    phase = WEEKLY_DIGEST_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES["weekly_digests"]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert checkpoint.status == "running"
    assert (checkpoint.scan_high_watermark_id, checkpoint.snapshot_rows) == (
        retained_id,
        1,
    )


async def test_weekly_digest_post_load_rejection_rolls_back_replacement(
    db_session,
    legacy_owner_roots,
):
    async def rejected_preflight(*args, **kwargs):
        raise WeeklyDigestOwnershipBackfillStateError("sensitive synthetic state")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            data_portability_service,
            "preflight_weekly_digest_ownership_backfill",
            rejected_preflight,
        )
        with pytest.raises(
            PortabilityError,
            match="weekly-digest validation rejected the portable restore",
        ):
            await import_full(
                db_session,
                {
                    "metadata": {"version": "1.0", "kind": "full_backup"},
                    "raw_payloads": [],
                },
            )
    await db_session.rollback()


def _portable_garmin_weight_export(*, row_id: int = 131) -> dict:
    return {
        "id": row_id,
        "date": "2026-08-21",
        "weight_log_id": None,
        "weight_kg": 81.5,
        "measured_at": "2026-08-21T07:30:00",
        "dispatch_timestamp_ms": None,
        "status": "pending",
        "attempts": 0,
        "last_attempt_at": None,
        "next_attempt_at": None,
        "exported_at": None,
        "remote_sample_pk": None,
        "remote_weight_kg": None,
        "remote_owned": False,
        "last_error": None,
        "_vitals_subject_bound": True,
    }


async def test_full_import_blocks_nonempty_garmin_outbox_stage3q_snapshot(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "garmin_weight_exports": [_portable_garmin_weight_export()],
        },
    )

    row = await db_session.get(GarminWeightExport, 131)
    assert row is not None
    assert row.subject_id == legacy_owner_roots.subject_id
    # Backup v1 cannot carry the destination account it was queued for.
    assert row.integration_connection_id is None
    assert row.requested_by_user_id is None
    phase = GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
        "garmin_weight_exports"
    ]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.completed_at,
    ) == ("restore_blocked", 131, 1, None)


async def test_full_import_completes_exact_empty_garmin_outbox_stage3q(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
        },
    )

    phase = GARMIN_WEIGHT_EXPORT_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
        "garmin_weight_exports"
    ]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
    ) == ("completed", 0, 0)
    assert checkpoint.completed_at is not None


async def test_garmin_outbox_post_load_rejection_rolls_back_replacement(
    db_session,
    legacy_owner_roots,
):
    async def rejected_preflight(*args, **kwargs):
        raise GarminWeightExportOwnershipBackfillStateError(
            "sensitive synthetic state"
        )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            data_portability_service,
            "preflight_garmin_weight_export_ownership_backfill",
            rejected_preflight,
        )
        with pytest.raises(
            PortabilityError,
            match="Garmin outbox validation rejected the portable restore",
        ):
            await import_full(
                db_session,
                {
                    "metadata": {"version": "1.0", "kind": "full_backup"},
                    "raw_payloads": [],
                },
            )
    await db_session.rollback()


async def test_full_import_resets_body_scan_metric_stage3p_snapshot(
    db_session,
    legacy_owner_roots,
):
    await import_full(
        db_session,
        {
            "metadata": {"version": "1.0", "kind": "full_backup"},
            "raw_payloads": [],
            "body_scans": [_portable_body_scan(row_id=111)],
            "body_scan_metrics": [
                {
                    "id": 121,
                    "scan_id": 111,
                    "metric_key": "skeletal_muscle_mass",
                    "label": "SMM",
                    "value": 38.4,
                    "unit": "kg",
                    "ref_low": 33.0,
                    "ref_high": 41.0,
                    "segment": None,
                    "category": "composition",
                }
            ],
        },
    )

    metric = await db_session.get(BodyScanMetric, 121)
    assert metric is not None
    assert metric.subject_id == legacy_owner_roots.subject_id
    assert metric.scan_id == 111 and metric.value == 38.4
    phase = BODY_SCAN_METRIC_OWNERSHIP_BACKFILL_CHECKPOINT_PHASES[
        "body_scan_metrics"
    ]
    checkpoint = await db_session.get(OwnershipBackfillCheckpoint, phase)
    assert checkpoint is not None
    assert (
        checkpoint.status,
        checkpoint.scan_high_watermark_id,
        checkpoint.snapshot_rows,
        checkpoint.completed_at,
    ) == ("running", 121, 1, None)


async def test_body_scan_metric_post_load_rejection_rolls_back_replacement(
    db_session,
    legacy_owner_roots,
):
    async def rejected_preflight(*args, **kwargs):
        raise BodyScanMetricOwnershipBackfillStateError("sensitive synthetic state")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            data_portability_service,
            "preflight_body_scan_metric_ownership_backfill",
            rejected_preflight,
        )
        with pytest.raises(
            PortabilityError,
            match="body-scan metric validation rejected the portable restore",
        ):
            await import_full(
                db_session,
                {
                    "metadata": {"version": "1.0", "kind": "full_backup"},
                    "raw_payloads": [],
                },
            )
    await db_session.rollback()


@pytest.mark.parametrize("table_name", BODY_SCAN_OWNERSHIP_BACKFILL_TABLES)
@pytest.mark.parametrize("bad_id", (0, -1, True, None, 2_147_483_648))
async def test_body_scan_replacement_rejects_invalid_ids_before_mutation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    table_name,
    bad_id,
):
    called = False

    async def unexpected_block(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        data_portability_service,
        "block_body_scan_ownership_backfill_for_portability_v1_restore",
        unexpected_block,
    )
    row = _portable_body_scan()
    row["id"] = bad_id

    with pytest.raises(PortabilityError):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                table_name: [row],
            },
        )
    assert called is False


async def test_full_import_blocks_body_scans_after_genetic_variant_reset(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    order: list[str] = []
    original_genetic_reset = (
        data_portability_service
        .reset_genetic_variant_ownership_backfill_for_portability_v1_restore
    )

    async def tracked_genetic_reset(*args, **kwargs):
        order.append("genetic_variants")
        return await original_genetic_reset(*args, **kwargs)

    async def stopping_body_scan_block(*args, **kwargs):
        order.append("body_scans")
        raise BodyScanOwnershipBackfillStateError("sensitive synthetic state")

    monkeypatch.setattr(
        data_portability_service,
        "reset_genetic_variant_ownership_backfill_for_portability_v1_restore",
        tracked_genetic_reset,
    )
    monkeypatch.setattr(
        data_portability_service,
        "block_body_scan_ownership_backfill_for_portability_v1_restore",
        stopping_body_scan_block,
    )
    with pytest.raises(
        PortabilityError, match="body-scan ownership restore block"
    ):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                "body_scans": [_portable_body_scan(row_id=102)],
            },
        )
    await db_session.rollback()

    assert order == ["genetic_variants", "body_scans"]


@pytest.mark.parametrize("table_name", GENETIC_VARIANT_OWNERSHIP_BACKFILL_TABLES)
@pytest.mark.parametrize("bad_id", (0, -1, True, None, 2_147_483_648))
async def test_genetic_variant_replacement_rejects_invalid_ids_before_mutation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    table_name,
    bad_id,
):
    called = False

    async def unexpected_reset(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        data_portability_service,
        "reset_genetic_variant_ownership_backfill_for_portability_v1_restore",
        unexpected_reset,
    )
    row = _portable_genetic_variant()
    row["id"] = bad_id

    with pytest.raises(PortabilityError):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                table_name: [row],
            },
        )
    assert called is False


async def test_full_import_calls_genetic_variant_reset_after_lab_result_reset(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    order: list[str] = []
    original_lab_reset = (
        data_portability_service
        .reset_lab_result_ownership_backfill_for_portability_v1_restore
    )

    async def tracked_lab_reset(*args, **kwargs):
        order.append("lab_results")
        return await original_lab_reset(*args, **kwargs)

    async def stopping_genetic_reset(*args, **kwargs):
        order.append("genetic_variants")
        raise GeneticVariantOwnershipBackfillStateError("sensitive synthetic state")

    monkeypatch.setattr(
        data_portability_service,
        "reset_lab_result_ownership_backfill_for_portability_v1_restore",
        tracked_lab_reset,
    )
    monkeypatch.setattr(
        data_portability_service,
        "reset_genetic_variant_ownership_backfill_for_portability_v1_restore",
        stopping_genetic_reset,
    )
    with pytest.raises(
        PortabilityError, match="genetic-variant ownership restore reset"
    ):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                "genetic_variants": [_portable_genetic_variant(row_id=92)],
            },
        )
    await db_session.rollback()

    assert order == ["lab_results", "genetic_variants"]


async def test_genetic_variant_post_load_rejection_rolls_back_whole_replacement(
    db_session,
    legacy_owner_roots,
):
    old = GeneticVariant(subject_id=legacy_owner_roots.subject_id, 
        domain=Domain.GENETICS.value,
        source=Source.MANUAL.value,
        gene="OLDGENE",
    )
    db_session.add(old)
    await db_session.commit()
    old_id = old.id

    async def rejected_preflight(*args, **kwargs):
        raise GeneticVariantOwnershipBackfillStateError("sensitive synthetic state")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            data_portability_service,
            "preflight_genetic_variant_ownership_backfill",
            rejected_preflight,
        )
        with pytest.raises(
            PortabilityError,
            match="genetic-variant validation rejected the portable restore",
        ):
            await import_full(
                db_session,
                {
                    "metadata": {"version": "1.0", "kind": "full_backup"},
                    "raw_payloads": [],
                    "genetic_variants": [_portable_genetic_variant(row_id=93)],
                },
            )
    await db_session.rollback()

    restored = await db_session.get(GeneticVariant, old_id)
    assert restored is not None and restored.gene == "OLDGENE"
    assert await db_session.get(GeneticVariant, 93) is None


@pytest.mark.parametrize("table_name", LAB_RESULT_OWNERSHIP_BACKFILL_TABLES)
@pytest.mark.parametrize("bad_id", (0, -1, True, None, 2_147_483_648))
async def test_lab_result_replacement_rejects_invalid_ids_before_mutation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    table_name,
    bad_id,
):
    called = False

    async def unexpected_reset(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        data_portability_service,
        "reset_lab_result_ownership_backfill_for_portability_v1_restore",
        unexpected_reset,
    )
    row = _portable_lab_result()
    row["id"] = bad_id

    with pytest.raises(PortabilityError):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                table_name: [row],
            },
        )
    assert called is False


async def test_full_import_calls_lab_result_reset_after_weight_log_reset(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    order: list[str] = []
    original_weight_reset = (
        data_portability_service
        .reset_weight_log_ownership_backfill_for_portability_v1_restore
    )

    async def tracked_weight_reset(*args, **kwargs):
        order.append("weight_logs")
        return await original_weight_reset(*args, **kwargs)

    async def stopping_lab_result_reset(*args, **kwargs):
        order.append("lab_results")
        raise LabResultOwnershipBackfillStateError("sensitive synthetic state")

    monkeypatch.setattr(
        data_portability_service,
        "reset_weight_log_ownership_backfill_for_portability_v1_restore",
        tracked_weight_reset,
    )
    monkeypatch.setattr(
        data_portability_service,
        "reset_lab_result_ownership_backfill_for_portability_v1_restore",
        stopping_lab_result_reset,
    )
    with pytest.raises(
        PortabilityError, match="lab-result ownership restore reset"
    ):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                "lab_results": [_portable_lab_result(row_id=82)],
            },
        )
    await db_session.rollback()

    assert order == ["weight_logs", "lab_results"]


async def test_lab_result_post_load_rejection_rolls_back_whole_replacement(
    db_session,
    legacy_owner_roots,
):
    old = LabResult(subject_id=legacy_owner_roots.subject_id, 
        date=date(2026, 8, 20),
        domain=Domain.LABS.value,
        source=Source.MANUAL.value,
        marker="old_marker",
        value=1.0,
    )
    db_session.add(old)
    await db_session.commit()
    old_id = old.id

    async def rejected_preflight(*args, **kwargs):
        raise LabResultOwnershipBackfillStateError("sensitive synthetic state")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            data_portability_service,
            "preflight_lab_result_ownership_backfill",
            rejected_preflight,
        )
        with pytest.raises(
            PortabilityError,
            match="lab-result validation rejected the portable restore",
        ):
            await import_full(
                db_session,
                {
                    "metadata": {"version": "1.0", "kind": "full_backup"},
                    "raw_payloads": [],
                    "lab_results": [_portable_lab_result(row_id=83)],
                },
            )
    await db_session.rollback()

    restored = await db_session.get(LabResult, old_id)
    assert restored is not None and restored.marker == "old_marker"
    assert await db_session.get(LabResult, 83) is None


@pytest.mark.parametrize("table_name", WEIGHT_LOG_OWNERSHIP_BACKFILL_TABLES)
@pytest.mark.parametrize("bad_id", (0, -1, True, None, 2_147_483_648))
async def test_weight_log_replacement_rejects_invalid_ids_before_mutation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    table_name,
    bad_id,
):
    called = False

    async def unexpected_reset(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        data_portability_service,
        "reset_weight_log_ownership_backfill_for_portability_v1_restore",
        unexpected_reset,
    )
    row = _portable_weight_log()
    row["id"] = bad_id

    with pytest.raises(PortabilityError):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                table_name: [row],
            },
        )
    assert called is False


async def test_full_import_calls_weight_log_reset_after_shared_report_preparation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    order: list[str] = []
    original_prepare = (
        data_portability_service
        .prepare_shared_report_ownership_backfill_for_portability_v1_restore
    )

    async def tracked_prepare(*args, **kwargs):
        order.append("shared_reports")
        return await original_prepare(*args, **kwargs)

    async def stopping_weight_log_reset(*args, **kwargs):
        order.append("weight_logs")
        raise WeightLogOwnershipBackfillStateError("sensitive synthetic state")

    monkeypatch.setattr(
        data_portability_service,
        "prepare_shared_report_ownership_backfill_for_portability_v1_restore",
        tracked_prepare,
    )
    monkeypatch.setattr(
        data_portability_service,
        "reset_weight_log_ownership_backfill_for_portability_v1_restore",
        stopping_weight_log_reset,
    )
    with pytest.raises(
        PortabilityError, match="weight-log ownership restore reset"
    ):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                "weight_logs": [_portable_weight_log(row_id=72)],
            },
        )
    await db_session.rollback()

    assert order == ["shared_reports", "weight_logs"]


async def test_weight_log_post_load_rejection_rolls_back_whole_replacement(
    db_session,
    legacy_owner_roots,
):
    old = WeightLog(subject_id=legacy_owner_roots.subject_id, 
        date=date(2026, 8, 20),
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        weight_kg=80.0,
        superseded=False,
    )
    db_session.add(old)
    await db_session.commit()
    old_id = old.id

    async def rejected_preflight(*args, **kwargs):
        raise WeightLogOwnershipBackfillStateError("sensitive synthetic state")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            data_portability_service,
            "preflight_weight_log_ownership_backfill",
            rejected_preflight,
        )
        with pytest.raises(
            PortabilityError,
            match="weight-log validation rejected the portable restore",
        ):
            await import_full(
                db_session,
                {
                    "metadata": {"version": "1.0", "kind": "full_backup"},
                    "raw_payloads": [],
                    "weight_logs": [_portable_weight_log(row_id=73)],
                },
            )
    await db_session.rollback()

    restored = await db_session.get(WeightLog, old_id)
    assert restored is not None and restored.weight_kg == 80.0
    assert await db_session.get(WeightLog, 73) is None


@pytest.mark.parametrize("table_name", HRT_COMPOUND_OWNERSHIP_BACKFILL_TABLES)
@pytest.mark.parametrize("bad_id", (0, -1, True, None, 2_147_483_648))
async def test_hrt_compound_replacement_rejects_invalid_ids_before_mutation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    table_name,
    bad_id,
):
    called = False

    async def unexpected_reset(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        data_portability_service,
        "reset_hrt_compound_backfill_for_portability_v1_restore",
        unexpected_reset,
    )
    row = {
        "domain": Domain.HRT.value,
        "source": Source.MANUAL.value,
        "key": "synthetic_custom",
        "name": "Synthetic custom",
        "compound_class": "testosterone",
        "route": "injection",
    }
    if table_name == "hrt_compound_components":
        row = {"compound_id": 1, "ester": "synthetic", "mg": 1.0}
    if bad_id is not None:
        row["id"] = bad_id
    with pytest.raises(PortabilityError, match="positive integer id"):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
                table_name: [row],
            },
        )
    assert called is False


@pytest.mark.parametrize("bad_id", (0, -1, True, None, 2_147_483_648))
async def test_raw_replacement_rejects_nonpositive_ids_before_mutation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    bad_id,
):
    old_raw = RawPayload(subject_id=legacy_owner_roots.subject_id, 
        domain=Domain.GENETICS.value,
        source="vcf_import",
        external_id="positive-id-guard",
        payload={"old": True},
    )
    db_session.add(old_raw)
    await db_session.commit()
    old_raw_id = old_raw.id
    called = False

    async def unexpected_block(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(
        data_portability_service,
        "block_raw_ownership_backfill_for_portability_v1_restore",
        unexpected_block,
    )
    row = {
        "domain": Domain.GENETICS.value,
        "source": "vcf_import",
        "payload": {},
    }
    if bad_id is not None:
        row["id"] = bad_id
    with pytest.raises(PortabilityError, match="positive integer id"):
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [row],
            },
        )
    assert called is False
    assert await db_session.get(RawPayload, old_raw_id) is not None


@pytest.mark.parametrize(
    "block_error",
    (RawOwnershipBackfillIdentityError, RawOwnershipBackfillStateError),
)
async def test_raw_restore_block_failure_is_bounded_and_does_not_start_replacement(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    block_error,
):
    old_raw = RawPayload(subject_id=legacy_owner_roots.subject_id, 
        domain=Domain.GENETICS.value,
        source="vcf_import",
        external_id="restore-block-failure-guard",
        payload={"old": True},
    )
    db_session.add(old_raw)
    await db_session.commit()
    old_raw_id = old_raw.id

    async def rejected_block(*args, **kwargs):
        raise block_error("sensitive raw id 999 must never escape")

    monkeypatch.setattr(
        data_portability_service,
        "block_raw_ownership_backfill_for_portability_v1_restore",
        rejected_block,
    )
    with pytest.raises(PortabilityError) as caught:
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [],
            },
        )
    assert "sensitive" not in str(caught.value)
    assert "999" not in str(caught.value)
    assert await db_session.get(RawPayload, old_raw_id) is not None


async def test_database_failure_is_bounded_and_caller_rollback_is_atomic(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    old_raw = RawPayload(subject_id=legacy_owner_roots.subject_id, 
        domain=Domain.GENETICS.value,
        source=Source.VCF_IMPORT.value,
        external_id="database-error-rollback-guard",
        payload={"old": True},
    )
    db_session.add(old_raw)
    await db_session.commit()
    old_raw_id = old_raw.id

    async def rejected_database_read(*args, **kwargs):
        from sqlalchemy.exc import SQLAlchemyError

        raise SQLAlchemyError(
            "sensitive payload marker and raw id 987654 must never escape"
        )

    monkeypatch.setattr(
        data_portability_service,
        "_secret_settings",
        rejected_database_read,
    )
    with pytest.raises(PortabilityError) as caught:
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [
                    {
                        "id": 37,
                        "domain": Domain.GENETICS.value,
                        "source": Source.VCF_IMPORT.value,
                        "external_id": "incoming-database-error",
                        "payload": {"incoming": True},
                        "_vitals_subject_bound": True,
                    }
                ],
            },
        )
    message = str(caught.value)
    assert "database rejected the portable restore" in message
    assert "sensitive payload marker" not in message
    assert "987654" not in message

    await db_session.rollback()
    assert await db_session.get(RawPayload, old_raw_id) is not None
    assert await db_session.get(RawPayload, 37) is None
    assert (
        await db_session.get(
            OwnershipBackfillCheckpoint,
            RAW_OWNERSHIP_BACKFILL_PHASE,
        )
        is None
    )


@pytest.mark.parametrize("retained_table", ("ai", "delivery_intent"))
async def test_full_import_refuses_retained_raw_control_provenance_before_mutation(
    db_session,
    legacy_owner_roots,
    platform_ai_ready,
    retained_table,
):
    connection_id = await db_session.scalar(
        select(IntegrationConnection.id).where(
            IntegrationConnection.subject_id == legacy_owner_roots.subject_id,
            IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.RECIPIENT.value,
        )
    )
    assert connection_id is not None
    raw = RawPayload(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        integration_connection_id=connection_id,
        domain=Domain.SIGNALS.value,
        source=Source.TELEGRAM.value,
        external_id=f"retained-control-sensitive-{retained_table}",
        payload={"synthetic": True},
    )
    db_session.add(raw)
    await db_session.flush()
    if retained_table == "ai":
        db_session.add(
            AIInvocation(
                subject_id=legacy_owner_roots.subject_id,
                actor_user_id=legacy_owner_roots.user_id,
                raw_payload_id=raw.id,
                platform_integration_connection_id=platform_ai_ready.id,
                purpose=AIInvocationPurpose.SIGNAL_PARSE.value,
                source=AIInvocationSource.TELEGRAM.value,
                model="synthetic/portability-guard",
                config_version=platform_ai_ready.config_version,
                idempotency_key="retained-portability-ai",
                quota_period_start=date(2020, 1, 1),
                quota_period_end=date(2100, 1, 1),
                reserved_cost_microunits=1,
                reserved_units=1,
                charged_cost_microunits=0,
                charged_units=0,
                status=AIInvocationStatus.PREPARED.value,
            )
        )
    else:
        db_session.add(
            NotificationDeliveryIntent(
                subject_id=legacy_owner_roots.subject_id,
                recipient_user_id=legacy_owner_roots.user_id,
                actor_user_id=legacy_owner_roots.user_id,
                integration_connection_id=connection_id,
                raw_payload_id=raw.id,
                category="reply",
                channel="telegram",
                idempotency_key="d" * 64,
                policy_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
                policy_date=date(2026, 8, 21),
                status=NotificationDeliveryStatus.PENDING.value,
            )
        )
    await db_session.commit()
    raw_id = raw.id

    with pytest.raises(PortabilityError) as caught:
        await import_full(
            db_session,
            {
                "metadata": {"version": "1.0", "kind": "full_backup"},
                "raw_payloads": [
                    {
                        "id": raw_id,
                        "domain": Domain.SIGNALS.value,
                        "source": Source.TELEGRAM.value,
                        "external_id": "replacement-must-not-start",
                        "payload": {"replacement": True},
                        "_vitals_subject_bound": True,
                    }
                ],
            },
        )

    message = str(caught.value)
    assert "control-plane provenance" in message
    assert "retained-control-sensitive" not in message
    assert await db_session.get(RawPayload, raw_id) is not None
    assert (
        await db_session.get(
            OwnershipBackfillCheckpoint,
            RAW_OWNERSHIP_BACKFILL_PHASE,
        )
        is None
    )


# ── LLM export shape ───────────────────────────────────────────────────────────


async def test_llm_export_is_clean(garmin_connection_id, hevy_connection_id, legacy_owner_roots, db_session):
    await _seed(db_session, garmin_connection_id=garmin_connection_id, hevy_connection_id=hevy_connection_id, legacy_owner_roots=legacy_owner_roots)
    out = await export_llm(db_session)

    # No raw dumps, no service tables.
    assert "raw_payloads" not in out
    assert "system_alerts" not in out
    # Profile header present.
    assert "profile" in out and "exported_at" in out["profile"]
    # Only the active weight (superseded row excluded), and no internal ids leak.
    assert len(out["weight_history"]) == 1
    assert out["weight_history"][0]["weight_kg"] == 118.5
    assert all("id" not in row for row in out["weight_history"])
    # Biomarkers + nested workouts present.
    assert out["biomarkers"][0]["marker"] == "glucose"
    assert out["workouts"][0]["exercises"][0]["title"] == "Bench Press"
    assert out["workouts"][0]["exercises"][0]["sets"][0]["weight_kg"] == 80.0
    # body_comp key always present (empty here — the seed has no scan).
    assert out["body_scans"] == []


async def test_llm_export_includes_full_garmin_rows(garmin_connection_id, hevy_connection_id, legacy_owner_roots, db_session):
    """The Garmin blocks used to be a hand-picked dozen fields out of ~45, so sleep
    phases, HR zones and splits never reached the AI export. Both rows now go out
    whole — minus ids/plumbing."""
    await _seed(db_session, garmin_connection_id=garmin_connection_id, hevy_connection_id=hevy_connection_id, legacy_owner_roots=legacy_owner_roots)
    out = await export_llm(db_session)

    daily = out["garmin_daily"][0]
    assert daily["date"] == "2026-04-29"
    assert daily["sleep_seconds"] == 27000
    assert [s["stage"] for s in daily["sleep_stages"]] == ["light", "deep"]
    assert daily["breathing_events"][0]["value"] == 0
    # Plumbing stays out.
    assert not {"id", "raw_payload_id", "domain", "source"} & set(daily)

    act = out["garmin_activities"][0]
    assert act["activity_type"] == "running"
    assert act["elevation_gain_m"] == 42.0
    assert act["training_effect_aerobic"] == 3.4
    assert act["hr_zone_seconds"][0]["secs"] == 120.0
    assert act["splits"][0]["distance_m"] == 1000.0
    assert not {"id", "external_id", "raw_payload_id"} & set(act)


async def test_llm_export_includes_body_scans(db_session, *, legacy_owner_roots):
    """D3: the body_comp domain (BIA/InBody scans + every captured metric) must
    appear in the curated LLM export — previously it was dropped entirely."""
    from vitals.models.body_scan import BodyScan, BodyScanMetric

    scan = BodyScan(subject_id=legacy_owner_roots.subject_id, 
        date=date(2026, 4, 20), domain="body_comp", source="body_scan", device="InBody 770"
    )
    db_session.add(scan)
    await db_session.flush()
    db_session.add_all(
        [
            BodyScanMetric(
                scan_id=scan.id, metric_key="body_fat_pct", label="Percent Body Fat",
                value=18.5, unit="%", category="composition",
            ),
            BodyScanMetric(
                scan_id=scan.id, metric_key="skeletal_muscle_mass", label="SMM",
                value=42.0, unit="кг", category="composition",
            ),
        ]
    )
    await db_session.commit()

    out = await export_llm(db_session)
    assert len(out["body_scans"]) == 1
    block = out["body_scans"][0]
    assert block["date"] == "2026-04-20"
    assert block["device"] == "InBody 770"
    metrics = {m["metric"]: m["value"] for m in block["metrics"]}
    assert metrics == {"body_fat_pct": 18.5, "skeletal_muscle_mass": 42.0}


async def test_llm_export_since_keeps_open_periods_and_catalogs(db_session, owner_write):
    """``since`` narrows the digest for the MCP tool. Two things must survive the cut
    regardless of when they started: a period still running today (an open dose
    phase), and the catalogs, which are current state rather than history."""
    from vitals.services import glp1_service, supplements_service

    await glp1_service.add_dose_phase(
        db_session,
        start_date=date(2020, 1, 1),
        drug="semaglutide",
        dose_mg=1.0,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2020, 1, 1)),
    )
    await glp1_service.add_dose_phase(
        db_session,
        start_date=date(2019, 1, 1),
        end_date=date(2019, 6, 1),
        drug="semaglutide",
        dose_mg=0.5,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2019, 1, 1)),
    )
    await supplements_service.add_supplement(db_session, name="Creatine", identity=owner_write.identity, prepared_conflict_write=await owner_write.write())
    await db_session.commit()

    out = await export_llm(db_session, since=date(2026, 1, 1))
    assert [p["dose_mg"] for p in out["glp1_dose_phases"]] == [1.0]  # open phase kept
    assert [s["name"] for s in out["supplements"]] == ["Creatine"]

    # And with no arguments the export is still the whole history (the web download).
    full = await export_llm(db_session)
    assert len(full["glp1_dose_phases"]) == 2


# ── Every domain reaches the LLM export ────────────────────────────────────────
#
# ``export_llm`` is a long hand-written function, and its real failure mode isn't
# its length — it's that a new domain gets added to ``Domain`` and nobody
# remembers to give it a block, so the AI report silently loses a whole module.
# The map below is the contract: every enum member names the export key(s) it must
# fill. Adding a Domain member without touching this map fails immediately.

DOMAIN_EXPORT_KEYS: dict[Domain, tuple[str, ...]] = {
    Domain.WEIGHT: ("weight_history", "body_measurements", "noise_periods"),
    Domain.BODY_COMPOSITION: ("body_scans",),
    Domain.GLP1: ("glp1_injections", "glp1_dose_phases", "glp1_side_effects"),
    Domain.HRT: ("hrt_doses", "hrt_cycles", "hrt_side_effects", "hrt_cycle_templates"),
    Domain.LABS: ("biomarkers",),
    Domain.WORKOUTS: ("workouts",),
    Domain.GARMIN: ("garmin_daily", "garmin_activities"),
    Domain.NUTRITION: ("nutrition",),
    Domain.SUPPLEMENTS: ("supplements",),
    Domain.GENETICS: ("genetics",),
    Domain.SKINCARE: ("skincare_logs", "skincare_observations"),
    Domain.MILESTONES: ("milestones", "weekly_digests"),
    Domain.TIMELINE: ("timeline_annotations",),
    Domain.SIGNALS: ("signals", "day_context"),
    # Infra/alert rows — deliberately excluded from a digest meant for a chat
    # window (test_llm_export_is_clean pins that they stay out).
    Domain.SYSTEM: (),
}


def test_every_domain_is_mapped_to_export_keys():
    """A new Domain member must be given an export block (or an explicit empty
    tuple saying it's intentionally not exported)."""
    assert set(DOMAIN_EXPORT_KEYS) == set(Domain)


async def _seed_every_domain(
    session, owner_write, *,
    garmin_connection_id, hevy_connection_id, legacy_owner_roots,
) -> None:
    from vitals.services import conflict_registrations

    # A scoped write consults every registered domain resolver.
    conflict_registrations.register_all_resolvers()
    """One row per domain — the domains _seed/_seed_hrt don't already cover."""
    from vitals.models.body_scan import BodyScan, BodyScanMetric
    from vitals.models.genetics import GeneticVariant
    from vitals.models.glp1 import DosePhase, SideEffect
    from vitals.models.milestones import Milestone, WeeklyDigest
    from vitals.models.nutrition import MealLog
    from vitals.models.signals import DayContext, Signal
    from vitals.models.skincare import SkincareLog, SkincareObservation
    from vitals.models.timeline import Annotation
    from vitals.models.weight import NoiseMarker

    await _seed(session, garmin_connection_id=garmin_connection_id, hevy_connection_id=hevy_connection_id, legacy_owner_roots=legacy_owner_roots)
    await _seed_hrt(session, owner_write)

    d = date(2026, 4, 25)
    scan = BodyScan(subject_id=owner_write.subject_id, date=d, domain="body_comp", source="body_scan", device="InBody 770")
    session.add(scan)
    await session.flush()
    session.add_all(
        [
            BodyScanMetric(
                scan_id=scan.id, metric_key="body_fat_pct", label="Percent Body Fat",
                value=18.5, unit="%", category="composition",
            ),
            NoiseMarker(subject_id=owner_write.subject_id, 
                domain="weight", source="manual", start_date=d, reason="креатин",
            ),
            DosePhase(subject_id=owner_write.subject_id, 
                domain="glp1", source="manual", start_date=d, drug="tirzepatide", dose_mg=5.0,
            ),
            SideEffect(subject_id=owner_write.subject_id, 
                date=d, domain="glp1", source="manual", effect_type="nausea", severity=1,
            ),
            GeneticVariant(subject_id=owner_write.subject_id, 
                domain="genetics", source="vcf_import", gene="MTHFR", rsid="rs1801133",
                genotype="CT", impact="Фолатный цикл", impact_domain="supplements",
            ),
            SkincareLog(subject_id=owner_write.subject_id, date=d, domain="skincare", source="manual", retinoid=True),
            SkincareObservation(subject_id=owner_write.subject_id, 
                date=d, domain="skincare", source="manual", inflammation=2, zone="лоб",
            ),
            MealLog(subject_id=owner_write.subject_id, 
                date=d, domain="nutrition", source="manual", name="Курица с рисом",
                calories=520, protein_g=45,
            ),
            Milestone(subject_id=owner_write.subject_id, domain="weight", name="100 кг", target_value=100.0, target_unit="кг"),
            WeeklyDigest(subject_id=owner_write.subject_id, 
                date=d, domain="milestones", source="manual", content="Неделя прошла ровно.",
            ),
            Annotation(subject_id=owner_write.subject_id, 
                date=d, domain="timeline", source="manual", kind="travel", title="Поездка",
            ),
            Signal(subject_id=owner_write.subject_id, 
                date=d, domain="signals", source="telegram", kind="symptom",
                key="head_ache", value_num=4, batch_id="b1", note="голова раскалывается",
            ),
            DayContext(subject_id=owner_write.subject_id, 
                date=d, domain="signals", source="manual", answers={"remote": True},
            ),
        ]
    )
    await session.commit()


async def test_llm_export_covers_every_domain(
    legacy_owner_roots, db_session, owner_write,
    garmin_connection_id, hevy_connection_id,
):
    """With one row seeded per domain, every mapped export key must be non-empty —
    the test that fails when a domain is added but never wired into export_llm."""
    await _seed_every_domain(
        db_session, owner_write,
        garmin_connection_id=garmin_connection_id,
        hevy_connection_id=hevy_connection_id,
        legacy_owner_roots=legacy_owner_roots,
    )
    out = await export_llm(db_session)

    empty = [
        key
        for domain, keys in DOMAIN_EXPORT_KEYS.items()
        for key in keys
        if not out.get(key)
    ]
    assert not empty, f"domains missing from the LLM export: {empty}"


async def test_llm_export_folds_signal_key_aliases(db_session, *, legacy_owner_roots):
    """The export ships canonical keys — otherwise the model sees 'head_ache' and
    'headache' as two unrelated things and the correlation is split in half."""
    from vitals.models.signals import Signal

    d = date(2026, 4, 25)
    db_session.add_all([
        Signal(subject_id=legacy_owner_roots.subject_id, date=d, domain="signals", source="telegram", kind="symptom",
               key="head_ache", batch_id="b1"),
        Signal(subject_id=legacy_owner_roots.subject_id, date=d, domain="signals", source="telegram", kind="symptom",
               key="headache", batch_id="b2"),
        # A cancelled batch stays out of the export entirely.
        Signal(subject_id=legacy_owner_roots.subject_id, date=d, domain="signals", source="telegram", kind="state",
               key="sleepiness", batch_id="b3", misparse=True),
    ])
    await db_session.commit()

    out = await export_llm(db_session)
    assert [s["key"] for s in out["signals"]] == ["headache", "headache"]


# ── Postgres sequence reset (real DB only) ─────────────────────────────────────


@pytest.mark.integration
async def test_import_resets_postgres_sequences(garmin_connection_id, hevy_connection_id, db_session, *, legacy_owner_roots):
    await _seed(db_session, garmin_connection_id=garmin_connection_id, hevy_connection_id=hevy_connection_id, legacy_owner_roots=legacy_owner_roots)
    snap = await export_full(db_session)
    await import_full(db_session, snap)
    await db_session.flush()

    # After restoring rows with explicit ids, a normal insert (no id) must get a
    # fresh id past the restored max — i.e. the identity sequence was advanced.
    db_session.add(
        WeightLog(subject_id=legacy_owner_roots.subject_id, date=date(2099, 1, 1), domain="weight", source="manual", weight_kg=100.0)
    )
    await db_session.flush()  # would raise duplicate-PK without the sequence reset


# ── Web routes ─────────────────────────────────────────────────────────────────


async def test_export_endpoint_downloads_backup(garmin_connection_id, hevy_connection_id, legacy_owner_roots, auth_client, db_session):
    await _seed(db_session, garmin_connection_id=garmin_connection_id, hevy_connection_id=hevy_connection_id, legacy_owner_roots=legacy_owner_roots)
    r = await auth_client.get("/settings/export")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert "vitals_backup_" in r.headers["content-disposition"]
    data = r.json()
    assert data["metadata"]["kind"] == "full_backup"
    assert "weight_logs" in data


async def test_export_llm_endpoint_downloads_digest(garmin_connection_id, hevy_connection_id, legacy_owner_roots, auth_client, db_session):
    await _seed(db_session, garmin_connection_id=garmin_connection_id, hevy_connection_id=hevy_connection_id, legacy_owner_roots=legacy_owner_roots)
    r = await auth_client.get("/settings/export-llm")
    assert r.status_code == 200
    assert "vitals_llm_" in r.headers["content-disposition"]
    data = r.json()
    assert "profile" in data
    assert "raw_payloads" not in data


async def test_import_endpoint_restores_and_reports(garmin_connection_id, hevy_connection_id, legacy_owner_roots, auth_client, db_session):
    await _seed(db_session, garmin_connection_id=garmin_connection_id, hevy_connection_id=hevy_connection_id, legacy_owner_roots=legacy_owner_roots)
    snap = await export_full(db_session)
    files = {"backup_file": ("backup.json", json.dumps(snap).encode(), "application/json")}
    r = await auth_client.post("/settings/import", files=files)
    assert r.status_code == 200
    assert "Импортировано" in r.text


async def test_import_endpoint_rejects_bad_json(auth_client):
    files = {"backup_file": ("bad.json", b"{not valid json", "application/json")}
    r = await auth_client.post("/settings/import", files=files)
    assert r.status_code == 400
    assert "JSON" in r.json()["detail"]


async def test_import_endpoint_rejects_wrong_extension(auth_client):
    files = {"backup_file": ("data.csv", b"a,b,c", "text/csv")}
    r = await auth_client.post("/settings/import", files=files)
    assert r.status_code == 415


# ── HRT in the exports (PR #7 review item) ────────────────────────────────────
async def _seed_hrt(session, owner_write):
    from vitals.services import hrt_catalog, hrt_cycle_service, hrt_service, hrt_template_service
    from vitals.utils.timeutils import today_local

    await hrt_catalog.sync_catalog(session)
    await hrt_service.log_dose(
        session, compound_key="testosterone_enanthate", on_date=today_local(),
        dose=250, unit="mg", brand="TestBrand", lab="UGL",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(today_local()),
    )
    await hrt_service.log_side_effect(
        session, on_date=today_local(), effect_type="acne", severity=2,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(today_local()),
    )
    cycle = await hrt_cycle_service.add_cycle(
        session, kind="course", start_date=today_local(), name="Cut",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await hrt_cycle_service.add_cycle_item(
        session, cycle.id, compound_key="stanozolol_oral",
        schedule=[{"dose": 30, "interval_days": 1, "duration_days": 28}],
        start_offset_days=28,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await hrt_template_service.save_cycle_as_template(session, cycle.id, name="Cut tpl",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await session.commit()


async def test_llm_export_includes_hrt(db_session, owner_write):
    await _seed_hrt(db_session, owner_write)
    out = await export_llm(db_session)
    assert out["hrt_doses"][0]["compound"] == "testosterone_enanthate"
    assert out["hrt_doses"][0]["brand"] == "TestBrand"
    assert out["hrt_side_effects"][0]["effect_type"] == "acne"
    cycle = out["hrt_cycles"][0]
    assert cycle["kind"] == "course"
    assert cycle["items"][0]["start_offset_days"] == 28
    tpl = out["hrt_cycle_templates"][0]
    assert tpl["name"] == "Cut tpl" and tpl["items"][0]["compound"] == "stanozolol_oral"


async def test_full_backup_round_trips_hrt(db_session, owner_write):
    """The generic full backup must carry every HRT table through wipe+restore."""
    from sqlalchemy import func, select
    from vitals.models.hrt import HrtCycle, HrtCycleItem, HrtCycleTemplate, HrtDose

    await _seed_hrt(db_session, owner_write)
    snapshot = await export_full(db_session)
    for table in ("hrt_doses", "hrt_cycles", "hrt_cycle_items",
                  "hrt_side_effects", "hrt_cycle_templates", "hrt_cycle_template_items"):
        assert snapshot.get(table), f"{table} missing from full backup"

    stats = await import_full(db_session, snapshot)  # wipe + reload
    await db_session.commit()
    assert stats.counts["hrt_doses"] == 1
    dose = (await db_session.execute(select(HrtDose))).scalars().one()
    assert dose.brand == "TestBrand"
    item = (await db_session.execute(select(HrtCycleItem))).scalars().one()
    assert item.start_offset_days == 28 and item.schedule[0]["dose"] == 30
    assert (await db_session.execute(select(func.count(HrtCycle.id)))).scalar() == 1
    assert (await db_session.execute(select(func.count(HrtCycleTemplate.id)))).scalar() == 1


def test_import_summary_labels_signals_and_friends():
    """The newer domains must be named in the summary, not swallowed by the
    anonymous "and N more" tail."""
    from vitals.i18n import t
    from vitals.services.data_portability_service import ImportStats

    stats = ImportStats(counts={
        "signals": 3, "day_context": 2, "body_scans": 1,
        "milestones": 4, "noise_markers": 5,
    })
    summary = stats.summary()
    for table in ("signals", "day_context", "body_scans", "milestones", "noise_markers"):
        assert t("import.label." + table) in summary
    assert t("import.summary_extra", n=15) not in summary


@pytest.mark.asyncio
async def test_backup_neither_carries_nor_resurrects_shared_reports(db_session, *, legacy_owner_roots):
    """A published doctor report is an outward-facing artifact, not data to
    round-trip. The export must not carry its password hash and its full copy of
    the record, and — the half that is easy to forget — the import must not wipe
    or recreate one, or restoring a backup would republish links the owner had
    already revoked."""
    from sqlalchemy import select as sa_select

    from vitals.models.share import SharedReport
    from vitals.utils.timeutils import now_local

    db_session.add(
        SharedReport(subject_id=legacy_owner_roots.subject_id, 
            token="tok-abc", password_hash="$2b$04$hash", title="Endocrinologist",
            domains=["labs"], period_start=date(2026, 3, 1), period_end=date(2026, 3, 30),
            snapshot={"blocks": {"labs": {"markers": [{"marker": "Ферритин"}]}}},
            expires_at=now_local() + timedelta(days=30),
        )
    )
    await db_session.commit()

    snapshot = await export_full(db_session)
    assert "shared_reports" not in snapshot
    assert "$2b$04$hash" not in json.dumps(snapshot, ensure_ascii=False)

    # An import wipes everything else; this row survives untouched.
    await import_full(db_session, snapshot)
    await db_session.commit()
    rows = (await db_session.execute(sa_select(SharedReport))).scalars().all()
    assert len(rows) == 1 and rows[0].token == "tok-abc"

    # And a file that *claims* to carry one plants nothing.
    forged = dict(snapshot)
    forged["shared_reports"] = [
        {
            "id": 999, "token": "planted", "password_hash": "x", "title": "planted",
            "domains": [], "period_start": "2026-01-01", "period_end": "2026-01-02",
            "labs_flagged_only": False, "snapshot": None,
            "expires_at": "2030-01-01T00:00:00", "opened_count": 0,
        }
    ]
    await import_full(db_session, forged)
    await db_session.commit()
    tokens = {
        r.token for r in (await db_session.execute(sa_select(SharedReport))).scalars().all()
    }
    assert tokens == {"tok-abc"}
