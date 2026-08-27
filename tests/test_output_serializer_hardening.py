"""Generic model output must not expose multi-user ownership plumbing."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Uuid,
    select,
)

from vitals.models.base import Base
from vitals.ownership import OwnershipClass, OwnershipSpec, TargetColumn
from vitals.operations.ownership import portability_v1
from vitals.services.portability import llm_projection, v1_contract, v1_export


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader or writer does when the ownership backfill has not reached a row yet,
# which is a state the application itself can no longer create. The schema
# says so, so this module asks for the one that stood before the contract.
pytestmark = pytest.mark.pre_ownership_contract


_SUPPRESSED_COLUMNS = {
    "subject_id",
    "actor_user_id",
    "created_by_user_id",
    "revoked_by_user_id",
    "overridden_by_user_id",
    "resolved_by_user_id",
    "recipient_user_id",
    "requested_by_user_id",
    "integration_connection_id",
    "ai_invocation_id",
    "file_asset_id",
    "uploaded_by_user_id",
    "credential_ref",
    "storage_ref",
    "opaque_key",
}


def _synthetic_owned_row() -> tuple[SimpleNamespace, set[uuid.UUID]]:
    """Stand in for a domain model after the nullable ownership expansion."""
    metadata = MetaData()
    columns = [
        Column("id", Integer),
        Column("date", Date),
        Column("value", Integer),
        Column("domain", String),
        Column("source", String),
        Column("external_id", String),
        Column("exercise_template_id", String),
        Column("raw_payload_id", Integer),
        Column("raw_id", Integer),
        Column("weight_log_id", Integer),
        Column("created_at", DateTime),
        Column("updated_at", DateTime),
    ]
    uuid_columns = _SUPPRESSED_COLUMNS - {"credential_ref", "storage_ref"}
    columns.extend(Column(name, Uuid(as_uuid=True)) for name in sorted(uuid_columns))
    columns.extend(
        [
            Column("credential_ref", String),
            Column("storage_ref", String),
        ]
    )
    table = Table("synthetic_owned_output", metadata, *columns)

    private_uuids = {uuid.uuid4() for _ in uuid_columns}
    values = dict(zip(sorted(uuid_columns), private_uuids, strict=True))
    values.update(
        {
            "id": 41,
            "date": date(2026, 8, 19),
            "value": 73,
            "domain": "garmin",
            "source": "garmin_api",
            "external_id": "vendor-record-17",
            "exercise_template_id": "vendor-template-9",
            "raw_payload_id": 99,
            "raw_id": 98,
            "weight_log_id": 97,
            "created_at": datetime(2026, 8, 19, 8, 0),
            "updated_at": datetime(2026, 8, 19, 8, 5),
            "credential_ref": "secret-store:connection/17",
            "storage_ref": "private/medical-file.pdf",
        }
    )
    return SimpleNamespace(__table__=table, **values), private_uuids


def _assert_no_private_plumbing(payload: dict, private_uuids: set[uuid.UUID]) -> None:
    assert _SUPPRESSED_COLUMNS.isdisjoint(payload)
    encoded = json.dumps(payload)
    assert all(str(value) not in encoded for value in private_uuids)
    assert "secret-store:connection/17" not in encoded
    assert "private/medical-file.pdf" not in encoded


def test_mcp_generic_serialization_suppresses_only_private_plumbing():
    mcp_router = pytest.importorskip("web.routers.mcp")
    row, private_uuids = _synthetic_owned_row()

    payload = mcp_router.serialize_row(row)

    _assert_no_private_plumbing(payload, private_uuids)
    # Preserve the MCP contract: addressability and ingestion provenance remain.
    assert payload["id"] == 41
    assert payload["source"] == "garmin_api"
    assert payload["external_id"] == "vendor-record-17"
    assert payload["exercise_template_id"] == "vendor-template-9"
    assert "domain" not in payload


def test_llm_generic_row_dump_has_no_uuid_or_private_plumbing():
    row, private_uuids = _synthetic_owned_row()

    payload = llm_projection._row_dump(row)

    _assert_no_private_plumbing(payload, private_uuids)
    # Preserve the curated LLM contract while retaining useful business fields.
    assert payload == {
        "date": "2026-08-19",
        "value": 73,
        "exercise_template_id": "vendor-template-9",
    }


async def test_full_backup_rebinds_subject_and_nulls_other_private_plumbing(
    db_session, monkeypatch
):
    from vitals.services.identity_bootstrap import bootstrap_legacy_owner

    owner = await bootstrap_legacy_owner(
        db_session,
        username="Serializer Owner",
        password_hash=(
            "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
        ),
        timezone="UTC",
    )
    uuid_columns = _SUPPRESSED_COLUMNS - {"credential_ref", "storage_ref"}
    table = Table(
        "serializer_owned_portable_rows",
        Base.metadata,
        Column("id", Integer, primary_key=True),
        Column("domain", String, nullable=False),
        Column("source", String, nullable=False),
        Column("external_id", String, nullable=False),
        Column("exercise_template_id", String, nullable=False),
        Column("raw_id", Integer, nullable=True),
        Column("weight_log_id", Integer, nullable=True),
        *(Column(name, Uuid(as_uuid=True)) for name in sorted(uuid_columns)),
        Column("credential_ref", String),
        Column("storage_ref", String),
    )
    connection = await db_session.connection()
    await connection.run_sync(table.create)
    monkeypatch.setattr(
        v1_contract,
        "OWNERSHIP_REGISTRY",
        {
            **v1_contract.OWNERSHIP_REGISTRY,
            table.name: OwnershipSpec(
                OwnershipClass.SUBJECT_DATA,
                subject=TargetColumn.REQUIRED,
            ),
        },
    )

    private_uuids = {uuid.uuid4() for _ in uuid_columns}
    private_values = dict(zip(sorted(uuid_columns), private_uuids, strict=True))
    private_values["subject_id"] = owner.subject_id
    private_uuids.add(owner.subject_id)
    private_values.update(
        {
            "credential_ref": "secret-store:connection/17",
            "storage_ref": "private/medical-file.pdf",
        }
    )
    try:
        await db_session.execute(
            table.insert().values(
                id=41,
                domain="workouts",
                source="hevy_api",
                external_id="hevy-workout-business-id",
                exercise_template_id="hevy-template-business-id",
                raw_id=98,
                weight_log_id=97,
                **private_values,
            )
        )

        snapshot = await v1_export.export_full(db_session)
        exported = snapshot[table.name][0]
        encoded = json.dumps(snapshot)

        _assert_no_private_plumbing(exported, private_uuids)
        assert exported == {
            "id": 41,
            "domain": "workouts",
            "source": "hevy_api",
            "external_id": "hevy-workout-business-id",
            "exercise_template_id": "hevy-template-business-id",
            "raw_id": 98,
            "weight_log_id": 97,
            "_vitals_subject_bound": True,
        }

        # A legacy or forged file may carry these fields. They are compatibility-
        # ignored, not treated as authority to attach restored PHI to another tenant.
        forged = json.loads(encoded)
        forged[table.name][0].update(
            {
                name: (
                    "secret-store:forged"
                    if name == "credential_ref"
                    else "private/forged"
                    if name == "storage_ref"
                    else str(uuid.uuid4())
                )
                for name in _SUPPRESSED_COLUMNS
            }
        )

        await portability_v1.import_full(db_session, forged)
        restored = (await db_session.execute(select(table))).mappings().one()

        assert restored["subject_id"] == owner.subject_id
        assert all(
            restored[name] is None
            for name in _SUPPRESSED_COLUMNS - {"subject_id"}
        )
        assert restored["external_id"] == "hevy-workout-business-id"
        assert restored["exercise_template_id"] == "hevy-template-business-id"
        assert restored["raw_id"] == 98
        assert restored["weight_log_id"] == 97
        assert restored["domain"] == "workouts"
        assert restored["source"] == "hevy_api"
    finally:
        try:
            await connection.run_sync(table.drop)
        finally:
            Base.metadata.remove(table)


async def test_v1_roundtrip_rebinds_required_and_preserves_mixed_subject_state(
    db_session,
):
    from vitals.models.conflict_rule import ConflictRule
    from vitals.models.hrt import HrtCompound, HrtCompoundComponent
    from vitals.models.system_alert import SystemAlert
    from vitals.models.weight import WeightLog
    from vitals.services.conflicts import catalog
    from vitals.services.identity_bootstrap import bootstrap_legacy_owner

    owner = await bootstrap_legacy_owner(
        db_session,
        username="Roundtrip Owner",
        password_hash=(
            "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
        ),
        timezone="UTC",
    )
    weight = WeightLog(
        date=date(2026, 8, 19),
        domain="weight",
        source="manual",
        weight_kg=80.0,
        subject_id=owner.subject_id,
        actor_user_id=owner.user_id,
    )
    await catalog.sync_catalog(db_session)
    global_rule_code = catalog.load_rule_catalog()[0]["code"]
    bound_rule = ConflictRule(
        code="portable-bound-rule",
        rule_type="soft_warn",
        domain_a="weight",
        condition_a={},
        domain_b="labs",
        condition_b={},
        severity="warn",
        message="bound",
        subject_id=owner.subject_id,
    )
    global_compound = HrtCompound(
        key="portable_global_compound",
        name="Global compound",
        compound_class="test",
        route="oral",
        subject_id=None,
    )
    bound_compound = HrtCompound(
        key="portable_bound_compound",
        name="Bound compound",
        compound_class="test",
        route="oral",
        subject_id=owner.subject_id,
        actor_user_id=owner.user_id,
    )
    # Reviewed keys: an installation-wide scheduler failure owns no subject,
    # while a health alert is subject-bound.
    global_alert = SystemAlert(
        domain="system",
        severity="info",
        message="global",
        alert_key="scheduler.job_failed:raw_payload_sweep",
        subject_id=None,
    )
    bound_alert = SystemAlert(
        domain="weight",
        severity="info",
        message="bound",
        alert_key="weight.noisy_period_active",
        subject_id=owner.subject_id,
        overridden_by_user_id=owner.user_id,
        override_at=datetime(2026, 1, 2, 8, 0),
    )
    db_session.add_all(
        [
            weight,
            bound_rule,
            global_compound,
            bound_compound,
            global_alert,
            bound_alert,
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            HrtCompoundComponent(
                compound_id=global_compound.id,
                ester="global",
                mg=1.0,
                subject_id=None,
            ),
            HrtCompoundComponent(
                compound_id=bound_compound.id,
                ester="bound",
                mg=2.0,
                subject_id=owner.subject_id,
            ),
        ]
    )
    await db_session.flush()

    snapshot = await v1_export.export_full(db_session)

    assert snapshot["weight_logs"][0]["_vitals_subject_bound"] is True
    assert "subject_id" not in snapshot["weight_logs"][0]
    rule_markers = {
        row["code"]: row["_vitals_subject_bound"]
        for row in snapshot["conflict_rules"]
    }
    assert rule_markers[global_rule_code] is False
    assert rule_markers["portable-bound-rule"] is True
    assert all(
        rule_markers[entry["code"]] is False
        for entry in catalog.load_rule_catalog()
    )
    assert {
        row["key"]: row["_vitals_subject_bound"]
        for row in snapshot["hrt_compounds"]
    } == {
        "portable_global_compound": False,
        "portable_bound_compound": True,
    }
    assert {
        row["ester"]: row["_vitals_subject_bound"]
        for row in snapshot["hrt_compound_components"]
    } == {"global": False, "bound": True}
    assert {
        row["alert_key"]: row["_vitals_subject_bound"]
        for row in snapshot["system_alerts"]
    } == {
        "scheduler.job_failed:raw_payload_sweep": False,
        "weight.noisy_period_active": True,
    }

    await portability_v1.import_full(db_session, snapshot)
    await db_session.flush()
    db_session.expire_all()

    restored_weight = (await db_session.scalars(select(WeightLog))).one()
    assert restored_weight.subject_id == owner.subject_id
    assert restored_weight.actor_user_id is None

    restored_rules = {
        row.code: row
        for row in await db_session.scalars(select(ConflictRule))
    }
    assert restored_rules[global_rule_code].subject_id is None
    assert restored_rules["portable-bound-rule"].subject_id == owner.subject_id

    restored_compounds = {
        row.key: row
        for row in await db_session.scalars(select(HrtCompound))
    }
    assert restored_compounds["portable_global_compound"].subject_id is None
    assert (
        restored_compounds["portable_bound_compound"].subject_id
        == owner.subject_id
    )
    assert restored_compounds["portable_bound_compound"].actor_user_id is None
    restored_components = {
        row.ester: row
        for row in await db_session.scalars(select(HrtCompoundComponent))
    }
    assert restored_components["global"].subject_id is None
    assert restored_components["bound"].subject_id == owner.subject_id

    restored_alerts = {
        row.alert_key: row
        for row in await db_session.scalars(select(SystemAlert))
    }
    assert (
        restored_alerts["scheduler.job_failed:raw_payload_sweep"].subject_id is None
    )
    assert (
        restored_alerts["weight.noisy_period_active"].subject_id == owner.subject_id
    )
    assert (
        restored_alerts["weight.noisy_period_active"].overridden_by_user_id is None
    )


async def test_legacy_v1_required_row_without_marker_rebinds_to_local_subject(
    db_session,
):
    from vitals.models.weight import WeightLog
    from vitals.services.identity_bootstrap import bootstrap_legacy_owner

    owner = await bootstrap_legacy_owner(
        db_session,
        username="Legacy Restore Owner",
        password_hash=(
            "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
        ),
        timezone="UTC",
    )
    payload = {
        "metadata": {"version": "1.0", "kind": "full_backup"},
        "weight_logs": [
            {
                "id": 91,
                "date": "2026-08-18",
                "domain": "weight",
                "source": "manual",
                "weight_kg": 79.0,
                "superseded": False,
                # Raw authority from a legacy/forged file is always ignored.
                "subject_id": str(uuid.uuid4()),
                "actor_user_id": str(uuid.uuid4()),
            }
        ],
    }

    await portability_v1.import_full(db_session, payload)
    restored = await db_session.get(WeightLog, 91)

    assert restored is not None
    assert restored.subject_id == owner.subject_id
    assert restored.actor_user_id is None


async def test_v1_export_and_import_fail_closed_with_multiple_subjects(db_session):
    from vitals.models.identity import HealthSubject, User
    from vitals.models.weight import WeightLog
    from vitals.services.identity_bootstrap import bootstrap_legacy_owner

    owner = await bootstrap_legacy_owner(
        db_session,
        username="First Owner",
        password_hash=(
            "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
        ),
        timezone="UTC",
    )
    second_user = User(
        username="Second Owner",
        normalized_username="second owner",
        password_hash=(
            "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"
        ),
        status="active",
        session_version=1,
    )
    db_session.add(second_user)
    await db_session.flush()
    db_session.add(
        HealthSubject(
            owner_user_id=second_user.id,
            display_name=second_user.username,
            timezone="UTC",
        )
    )
    weight = WeightLog(
        date=date(2026, 8, 20),
        domain="weight",
        source="manual",
        weight_kg=81.0,
        subject_id=owner.subject_id,
    )
    db_session.add(weight)
    await db_session.flush()
    weight_id = weight.id

    with pytest.raises(v1_contract.PortabilityError):
        await v1_export.export_full(db_session)

    payload = {
        "metadata": {"version": "1.0", "kind": "full_backup"},
        "weight_logs": [],
    }
    with pytest.raises(v1_contract.PortabilityError):
        await portability_v1.import_full(db_session, payload)

    preserved = await db_session.get(WeightLog, weight_id)
    assert preserved is not None
    assert preserved.subject_id == owner.subject_id


@pytest.mark.parametrize("marker", ["true", 1, None, {}, []])
async def test_v1_import_rejects_non_boolean_subject_marker_before_mutation(
    db_session, marker
):
    from vitals.models.weight import WeightLog

    weight = WeightLog(
        date=date(2026, 8, 21),
        domain="weight",
        source="manual",
        weight_kg=82.0,
    )
    db_session.add(weight)
    await db_session.flush()
    weight_id = weight.id
    payload = {
        "metadata": {"version": "1.0", "kind": "full_backup"},
        "weight_logs": [{"_vitals_subject_bound": marker}],
    }

    with pytest.raises(v1_contract.PortabilityError):
        await portability_v1.import_full(db_session, payload)

    assert await db_session.get(WeightLog, weight_id) is not None


async def test_v1_import_without_local_subject_rejects_true_marker_before_mutation(
    db_session,
):
    from vitals.models.weight import WeightLog

    weight = WeightLog(
        date=date(2026, 8, 22),
        domain="weight",
        source="manual",
        weight_kg=83.0,
    )
    db_session.add(weight)
    await db_session.flush()
    weight_id = weight.id
    payload = {
        "metadata": {"version": "1.0", "kind": "full_backup"},
        "weight_logs": [{"_vitals_subject_bound": True}],
    }

    with pytest.raises(v1_contract.PortabilityError):
        await portability_v1.import_full(db_session, payload)

    assert await db_session.get(WeightLog, weight_id) is not None


async def test_v1_zero_subject_roundtrip_keeps_legacy_rows_unbound(db_session):
    from vitals.models.weight import WeightLog

    db_session.add(
        WeightLog(
            date=date(2026, 8, 23),
            domain="weight",
            source="manual",
            weight_kg=84.0,
        )
    )
    await db_session.flush()

    snapshot = await v1_export.export_full(db_session)

    assert snapshot["weight_logs"][0]["_vitals_subject_bound"] is False
    await portability_v1.import_full(db_session, snapshot)
    restored = (await db_session.scalars(select(WeightLog))).one()
    assert restored.subject_id is None


async def test_subject_probe_treats_missing_identity_schema_as_legacy():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            assert (
                await v1_contract._single_local_subject_id(session)
                is None
            )
    finally:
        await engine.dispose()
