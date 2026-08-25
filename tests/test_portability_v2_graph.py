"""The v2 archive graph is complete for one subject and opaque outside it."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime

import pytest
from sqlalchemy import insert, update

from vitals.enums import (
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionStatus,
    Source,
    UserStatus,
)
from vitals.models import Base
from vitals.models.body_scan import BodyScan, BodyScanMetric
from vitals.models.hevy import HevyWorkout
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.supplements import Supplement
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.models.weight import ProgressPhoto, WeightLog
from vitals.ownership import OWNERSHIP_REGISTRY, OwnershipSpec, TargetColumn
from vitals.services.portability import graph
from vitals.services.portability.schema import PORTABILITY_SCHEMA_DIGEST


async def _subject(session, slug: str) -> HealthSubject:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=f"private subject name {slug}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return subject


def _row(manifest: dict, table_name: str, *, where: tuple[str, object] | None = None):
    table = next(item for item in manifest["tables"] if item["name"] == table_name)
    if where is None:
        assert len(table["rows"]) == 1
        return table["rows"][0]
    key, value = where
    return next(row for row in table["rows"] if row["values"].get(key) == value)


async def _connection(session, subject_id: uuid.UUID, suffix: str) -> IntegrationConnection:
    connection = IntegrationConnection(
        subject_id=subject_id,
        provider="hevy",
        connection_type="account",
        external_account_discriminator=f"never-export-discriminator-{suffix}",
        credential_ref=f"never-export-credential-{suffix}",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    session.add(connection)
    await session.flush()
    return connection


async def _asset(
    session,
    subject_id: uuid.UUID,
    *,
    suffix: str,
    purpose: str,
) -> FileAsset:
    asset = FileAsset(
        subject_id=subject_id,
        opaque_key=uuid.uuid4(),
        purpose=purpose,
        storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
        storage_ref=f"private/never-export-storage-{suffix}",
        media_type="image/jpeg",
        byte_size=17,
        sha256_hex=("a" if suffix == "a" else "b") * 64,
        status=FileAssetStatus.ACTIVE.value,
    )
    session.add(asset)
    await session.flush()
    return asset


@pytest.mark.asyncio
async def test_graph_is_subject_scoped_opaque_connected_and_deterministic(db_session):
    mine = await _subject(db_session, "mine")
    theirs = await _subject(db_session, "theirs")
    mine_connection = await _connection(db_session, mine.id, "mine")
    their_connection = await _connection(db_session, theirs.id, "theirs")
    scan_asset = await _asset(
        db_session,
        mine.id,
        suffix="a",
        purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT.value,
    )
    photo_asset = await _asset(
        db_session,
        mine.id,
        suffix="b",
        purpose=FileAssetPurpose.PROGRESS_PHOTO.value,
    )
    their_asset = await _asset(
        db_session,
        theirs.id,
        suffix="other",
        purpose=FileAssetPurpose.PROGRESS_PHOTO.value,
    )

    raw = RawPayload(
        subject_id=mine.id,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
        external_id="private/file/path-must-be-replaced",
        fetched_at=datetime(2026, 1, 2, 3, 4, 5),
        payload={
            "source_label": "preserved raw provenance",
            "nested": [True, None, 3, {"ordered": "value"}],
        },
        file_asset_id=scan_asset.id,
    )
    db_session.add(raw)
    await db_session.flush()
    scan = BodyScan(
        subject_id=mine.id,
        actor_user_id=mine.owner_user_id,
        file_asset_id=scan_asset.id,
        file_key="legacy/never-export-scan-key.jpg",
        raw_payload_id=raw.id,
        device="Synthetic scanner",
        date=date(2026, 1, 2),
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
    )
    weight = WeightLog(
        subject_id=mine.id,
        raw_payload_id=raw.id,
        weight_kg=81.25,
        superseded=False,
        note="ordinary value survives",
        date=date(2026, 1, 3),
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
    )
    workout = HevyWorkout(
        subject_id=mine.id,
        integration_connection_id=mine_connection.id,
        external_id="provider-workout-id",
        title="Required connection row",
        date=date(2026, 1, 4),
        domain=Domain.WORKOUTS.value,
        source=Source.HEVY_API.value,
    )
    photo = ProgressPhoto(
        subject_id=mine.id,
        actor_user_id=mine.owner_user_id,
        file_asset_id=photo_asset.id,
        file_key="legacy/never-export-photo-key.jpg",
        note="photo metadata",
        date=date(2026, 1, 5),
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
    )
    their_supplement = Supplement(
        subject_id=theirs.id,
        actor_user_id=theirs.owner_user_id,
        domain=Domain.SUPPLEMENTS.value,
        source=Source.MANUAL.value,
        name="NEVER EXPORT OTHER SUBJECT NAME",
        key="never-export-other-subject-key",
        active=True,
    )
    db_session.add_all([scan, weight, workout, photo, their_supplement])
    await db_session.flush()
    metric = BodyScanMetric(
        scan_id=scan.id,
        subject_id=None,  # inherited from the selected parent, still portable
        metric_key="skeletal_muscle_mass",
        label="SMM",
        value=38.4,
        unit="kg",
        category="composition",
    )
    db_session.add(metric)
    await db_session.flush()

    # Derived state, provider outbox state, and installation control state do not
    # enter v2 even when they carry this subject and memorable sentinel values.
    await db_session.execute(
        insert(Base.metadata.tables["system_alerts"]).values(
            subject_id=mine.id,
            domain=Domain.SYSTEM.value,
            severity="warn",
            message="NEVER EXPORT DERIVED ALERT",
            alert_key="never.export",
            entity_ref="derived-control",
        )
    )
    await db_session.execute(
        insert(Base.metadata.tables["garmin_weight_exports"]).values(
            subject_id=mine.id,
            integration_connection_id=mine_connection.id,
            requested_by_user_id=mine.owner_user_id,
            date=date(2026, 1, 3),
            weight_log_id=weight.id,
            weight_kg=81.25,
            measured_at=datetime(2026, 1, 3, 8, 0),
            last_error="NEVER EXPORT PROVIDER OUTBOX",
        )
    )
    await db_session.flush()

    first = await graph.build_subject_graph(db_session, subject_id=mine.id)
    second = await graph.build_subject_graph(db_session, subject_id=mine.id)
    assert first == second
    assert set(first.manifest) == {
        "format",
        "version",
        "schema_digest",
        "tables",
        "connections",
        "resources",
        "totals",
    }
    assert first.manifest["schema_digest"] == PORTABILITY_SCHEMA_DIGEST
    json_text = json.dumps(first.manifest, sort_keys=True)

    table_names = {item["name"] for item in first.manifest["tables"]}
    assert table_names == {
        name
        for name, spec in OWNERSHIP_REGISTRY.items()
        if spec.user_portable
        and name not in graph.EXCLUDED_PORTABLE_TABLES
        and spec.ownership in graph._SUBJECT_GRAPH_CLASSES
    }
    assert "garmin_weight_exports" not in table_names
    assert "system_alerts" not in table_names
    assert "app_settings" not in table_names
    assert "integration_connections" not in table_names
    assert "file_assets" not in table_names
    assert table_names.isdisjoint(
        {
            "users",
            "health_subjects",
            "integration_credentials",
            "care_threads",
            "care_messages",
            "care_relationships",
            "consent_grants",
            "professional_notes",
            "support_access_grants",
            "support_repair_actions",
            "break_glass_sessions",
            "audit_events",
            "subject_settings",
        }
    )

    raw_public = _row(first.manifest, "raw_payloads")
    scan_public = _row(first.manifest, "body_scans")
    metric_public = _row(first.manifest, "body_scan_metrics")
    weight_public = _row(first.manifest, "weight_logs")
    workout_public = _row(first.manifest, "hevy_workouts")
    photo_public = _row(first.manifest, "progress_photos")
    assert raw_public["values"]["payload"] == raw.payload
    assert raw_public["values"]["source"] == Source.BODY_SCAN.value
    assert "external_id" not in raw_public["values"]
    assert scan_public["links"]["raw_payload_id"] == raw_public["ref"]
    assert metric_public["links"]["scan_id"] == scan_public["ref"]
    assert weight_public["links"]["raw_payload_id"] == raw_public["ref"]
    assert scan_public["links"]["file_asset_id"] == raw_public["links"]["file_asset_id"]
    assert photo_public["links"]["file_asset_id"] != scan_public["links"]["file_asset_id"]
    assert workout_public["links"]["integration_connection_id"].startswith("c")
    assert "integration_connection_id" not in weight_public.get("links", {})
    assert weight_public["values"]["note"] == "ordinary value survives"

    assert first.manifest["connections"] == [
        {
            "ref": workout_public["links"]["integration_connection_id"],
            "provider": "hevy",
            "connection_type": "account",
        }
    ]
    assert len(first.manifest["resources"]) == 2
    assert all(
        set(item) == {"ref", "purpose", "media_type", "byte_size", "sha256_hex"}
        for item in first.manifest["resources"]
    )
    assert {item["ref"] for item in first.manifest["resources"]} == {
        handle.resource_ref for handle in first.prepared_resources
    }
    assert {handle.file_asset_id for handle in first.prepared_resources} == {
        scan_asset.id,
        photo_asset.id,
    }
    assert {handle.storage_ref for handle in first.prepared_resources} == {
        scan_asset.storage_ref,
        photo_asset.storage_ref,
    }

    # Public text contains no local identity, locator, credential, or other
    # subject's data.  The private prepared handles are intentionally separate.
    private_sentinels = {
        str(mine.id),
        str(theirs.id),
        str(mine.owner_user_id),
        str(theirs.owner_user_id),
        str(mine_connection.id),
        str(their_connection.id),
        str(scan_asset.id),
        str(photo_asset.id),
        str(their_asset.id),
        str(scan_asset.opaque_key),
        str(their_asset.opaque_key),
        scan_asset.storage_ref,
        their_asset.storage_ref,
        mine_connection.external_account_discriminator,
        mine_connection.credential_ref,
        "legacy/never-export-scan-key.jpg",
        "legacy/never-export-photo-key.jpg",
        "NEVER EXPORT OTHER SUBJECT NAME",
        "NEVER EXPORT DERIVED ALERT",
        "NEVER EXPORT PROVIDER OUTBOX",
    }
    assert all(sentinel not in json_text for sentinel in private_sentinels)
    assert all(
        "id" not in row["values"]
        and "subject_id" not in row["values"]
        and "actor_user_id" not in row["values"]
        for table in first.manifest["tables"]
        for row in table["rows"]
    )
    assert first.manifest["totals"] == {
        "tables": len(first.manifest["tables"]),
        "rows": sum(len(item["rows"]) for item in first.manifest["tables"]),
        "connections": 1,
        "resources": 2,
    }


@pytest.mark.asyncio
async def test_optional_connection_and_file_references_may_be_null(db_session):
    subject = await _subject(db_session, "optional-roots")
    raw = RawPayload(
        subject_id=subject.id,
        domain=Domain.LABS.value,
        source=Source.MANUAL.value,
        fetched_at=datetime(2026, 2, 1),
        payload={"plain": "payload"},
        integration_connection_id=None,
        file_asset_id=None,
    )
    db_session.add(raw)
    await db_session.flush()
    scan = BodyScan(
        subject_id=subject.id,
        file_key=None,
        file_asset_id=None,
        raw_payload_id=raw.id,
        date=date(2026, 2, 1),
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MANUAL.value,
    )
    db_session.add(scan)
    await db_session.flush()

    prepared = await graph.build_subject_graph(db_session, subject_id=subject.id)
    assert prepared.manifest["connections"] == []
    assert prepared.manifest["resources"] == []
    assert prepared.prepared_resources == ()
    assert "integration_connection_id" not in _row(
        prepared.manifest, "raw_payloads"
    ).get("links", {})
    assert "file_asset_id" not in _row(prepared.manifest, "body_scans").get(
        "links", {}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["connection", "file"])
async def test_required_connection_and_file_references_fail_closed(
    db_session, monkeypatch, kind
):
    subject = await _subject(db_session, f"required-{kind}")
    raw = RawPayload(
        subject_id=subject.id,
        domain=Domain.LABS.value,
        source=Source.MANUAL.value,
        fetched_at=datetime(2026, 2, 2),
        payload={},
    )
    db_session.add(raw)
    await db_session.flush()

    original = OWNERSHIP_REGISTRY["raw_payloads"]
    replacement = OwnershipSpec(
        original.ownership,
        subject=original.subject,
        actor=original.actor,
        connection=(
            TargetColumn.REQUIRED if kind == "connection" else original.connection
        ),
        platform_connection=original.platform_connection,
        file_asset=(TargetColumn.REQUIRED if kind == "file" else original.file_asset),
        user_portable=original.user_portable,
    )
    monkeypatch.setattr(
        graph,
        "OWNERSHIP_REGISTRY",
        {**OWNERSHIP_REGISTRY, "raw_payloads": replacement},
    )
    with pytest.raises(graph.GraphBuildError) as raised:
        await graph.build_subject_graph(db_session, subject_id=subject.id)
    assert raised.value.code == f"required_{kind}_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dangling", "cross_subject"])
async def test_portable_foreign_keys_cannot_leave_the_subject_graph(db_session, mode):
    mine = await _subject(db_session, f"fk-mine-{mode}")
    theirs = await _subject(db_session, f"fk-theirs-{mode}")
    other_raw = RawPayload(
        subject_id=theirs.id,
        domain=Domain.WEIGHT.value,
        source=Source.GARMIN_API.value,
        fetched_at=datetime(2026, 3, 1),
        payload={"owner": "theirs"},
    )
    db_session.add(other_raw)
    await db_session.flush()
    db_session.add(
        WeightLog(
            subject_id=mine.id,
            raw_payload_id=(other_raw.id if mode == "cross_subject" else 987_654),
            weight_kg=80,
            superseded=False,
            date=date(2026, 3, 1),
            domain=Domain.WEIGHT.value,
            source=Source.GARMIN_API.value,
        )
    )
    await db_session.flush()

    with pytest.raises(graph.GraphBuildError) as raised:
        await graph.build_subject_graph(db_session, subject_id=mine.id)
    assert raised.value.code == "foreign_key_dangling"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("parent_mine_child_theirs", "inherited_row_cross_subject"),
        ("parent_theirs_child_mine", "inherited_row_unreachable"),
    ],
)
async def test_inherited_child_subject_stamp_must_match_its_reachable_parent(
    db_session, mode, expected_code
):
    mine = await _subject(db_session, f"child-mine-{mode}")
    theirs = await _subject(db_session, f"child-theirs-{mode}")
    parent_subject = mine if mode == "parent_mine_child_theirs" else theirs
    child_subject = theirs if mode == "parent_mine_child_theirs" else mine
    scan = BodyScan(
        subject_id=parent_subject.id,
        date=date(2026, 3, 1),
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MANUAL.value,
    )
    db_session.add(scan)
    await db_session.flush()
    db_session.add(
        BodyScanMetric(
            scan_id=scan.id,
            subject_id=child_subject.id,
            metric_key="cross_subject_metric",
            label="Cross subject metric",
            value=1.0,
            category="other",
        )
    )
    await db_session.flush()

    with pytest.raises(graph.GraphBuildError) as raised:
        await graph.build_subject_graph(db_session, subject_id=mine.id)
    assert raised.value.code == expected_code


@pytest.mark.asyncio
async def test_cross_subject_connection_is_refused(db_session):
    mine = await _subject(db_session, "connection-mine")
    theirs = await _subject(db_session, "connection-theirs")
    other_connection = await _connection(db_session, theirs.id, "foreign")
    db_session.add(
        RawPayload(
            subject_id=mine.id,
            domain=Domain.GARMIN.value,
            source=Source.GARMIN_API.value,
            fetched_at=datetime(2026, 3, 2),
            payload={},
            integration_connection_id=other_connection.id,
        )
    )
    await db_session.flush()

    with pytest.raises(graph.GraphBuildError) as raised:
        await graph.build_subject_graph(db_session, subject_id=mine.id)
    assert raised.value.code == "connection_cross_subject"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["dangling", "cross_subject", "not_live", "corrupt_hash", "wrong_purpose"],
)
async def test_file_asset_must_be_reachable_same_subject_and_coherent(db_session, mode):
    mine = await _subject(db_session, f"asset-mine-{mode}")
    theirs = await _subject(db_session, f"asset-theirs-{mode}")
    owner_id = theirs.id if mode == "cross_subject" else mine.id
    asset = await _asset(
        db_session,
        owner_id,
        suffix="a",
        purpose=(
            FileAssetPurpose.BODY_SCAN_DOCUMENT.value
            if mode == "wrong_purpose"
            else FileAssetPurpose.PROGRESS_PHOTO.value
        ),
    )
    if mode == "not_live":
        await db_session.execute(
            update(Base.metadata.tables["file_assets"])
            .where(Base.metadata.tables["file_assets"].c.id == asset.id)
            .values(status=FileAssetStatus.PENDING.value)
        )
    elif mode == "corrupt_hash":
        # The historical DB check proves lowercase/length but not hexadecimal;
        # the graph must still refuse metadata the resource verifier cannot use.
        await db_session.execute(
            update(Base.metadata.tables["file_assets"])
            .where(Base.metadata.tables["file_assets"].c.id == asset.id)
            .values(sha256_hex="z" * 64)
        )
    target_id = uuid.uuid4() if mode == "dangling" else asset.id
    db_session.add(
        ProgressPhoto(
            subject_id=mine.id,
            file_asset_id=target_id,
            file_key="legacy-key-never-exported",
            date=date(2026, 3, 3),
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
        )
    )
    await db_session.flush()

    with pytest.raises(graph.GraphBuildError) as raised:
        await graph.build_subject_graph(db_session, subject_id=mine.id)
    assert raised.value.code == {
        "dangling": "resource_dangling",
        "cross_subject": "resource_cross_subject",
        "not_live": "resource_not_live",
        "corrupt_hash": "resource_metadata_invalid",
        "wrong_purpose": "resource_purpose_mismatch",
    }[mode]


@pytest.mark.asyncio
async def test_hard_totals_fail_before_a_graph_is_returned(db_session):
    subject = await _subject(db_session, "limits")
    db_session.add(
        Supplement(
            subject_id=subject.id,
            domain=Domain.SUPPLEMENTS.value,
            source=Source.MANUAL.value,
            name="one",
            key="one",
            active=True,
        )
    )
    await db_session.flush()

    with pytest.raises(graph.GraphBuildError) as raised:
        await graph.build_subject_graph(
            db_session,
            subject_id=subject.id,
            limits=graph.GraphLimits(max_total_rows=0),
        )
    assert raised.value.code == "total_row_limit_exceeded"

    with pytest.raises(graph.GraphBuildError) as raised:
        await graph.build_subject_graph(
            db_session,
            subject_id=subject.id,
            limits=graph.GraphLimits(max_rows_per_table=0),
        )
    assert raised.value.code == "table_row_limit_exceeded"

    with pytest.raises(graph.GraphBuildError) as raised:
        await graph.build_subject_graph(
            db_session,
            subject_id=subject.id,
            limits=graph.GraphLimits(max_tables=0),
        )
    assert raised.value.code == "table_limit_exceeded"


def test_registry_completeness_is_rechecked_at_build_time(monkeypatch):
    incomplete = dict(OWNERSHIP_REGISTRY)
    incomplete.pop("weight_logs")
    monkeypatch.setattr(graph, "OWNERSHIP_REGISTRY", incomplete)
    with pytest.raises(graph.GraphBuildError) as raised:
        graph._portable_tables()
    assert raised.value.code == "registry_incomplete"
