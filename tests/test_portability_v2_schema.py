"""The v2 portability schema is complete, explicit, and digest-ratcheted."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import Column, ForeignKeyConstraint, LargeBinary, MetaData, String

from vitals.models import Base
from vitals.ownership import OWNERSHIP_REGISTRY, TargetColumn
from vitals.services.portability.schema import (
    PORTABILITY_SCHEMA_DESCRIPTOR,
    PORTABILITY_SCHEMA_DIGEST,
    REVIEWED_SCHEMA_DIGEST,
    SchemaContractError,
    build_schema_descriptor,
    expected_resource_purpose,
    schema_digest,
    validate_schema_contract,
)


EXPECTED_PORTABLE_TABLES = {
    "annotations",
    "body_measurements",
    "body_scan_metrics",
    "body_scans",
    "conflict_rules",
    "garmin_activities",
    "garmin_daily",
    "garmin_intraday",
    "genetic_variants",
    "glp1_dose_phases",
    "glp1_injections",
    "glp1_side_effects",
    "hevy_exercises",
    "hevy_sets",
    "hevy_workouts",
    "hrt_compound_components",
    "hrt_compounds",
    "hrt_cycle_items",
    "hrt_cycle_template_items",
    "hrt_cycle_templates",
    "hrt_cycles",
    "hrt_doses",
    "hrt_side_effects",
    "lab_markers",
    "lab_results",
    "meal_logs",
    "milestones",
    "noise_markers",
    "progress_photos",
    "raw_payloads",
    "skincare_logs",
    "skincare_observations",
    "skincare_products",
    "supplements",
    "weight_logs",
}
EXPECTED_DIGEST = "8aec9099d557d8b136e0dd9044b7de50867b368c53e9f0c9d10d2d274f6d2f87"


def _metadata_copy() -> MetaData:
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    return metadata


def _tables_by_name(descriptor):
    return {table["name"]: table for table in descriptor["tables"]}


def test_current_descriptor_has_the_reviewed_digest_and_complete_inventory():
    assert PORTABILITY_SCHEMA_DIGEST == REVIEWED_SCHEMA_DIGEST == EXPECTED_DIGEST
    assert schema_digest(PORTABILITY_SCHEMA_DESCRIPTOR) == EXPECTED_DIGEST
    assert len(EXPECTED_PORTABLE_TABLES) == 35
    assert {table["name"] for table in PORTABILITY_SCHEMA_DESCRIPTOR["tables"]} == (
        EXPECTED_PORTABLE_TABLES
    )

    inventory = PORTABILITY_SCHEMA_DESCRIPTOR["table_inventory"]
    assert {entry["name"] for entry in inventory} == set(OWNERSHIP_REGISTRY)
    assert {
        entry["name"] for entry in inventory if entry["disposition"] == "portable"
    } == EXPECTED_PORTABLE_TABLES
    assert "derived_control_or_outbox" in next(
        entry for entry in inventory if entry["name"] == "system_alerts"
    )["reasons"]
    assert "derived_control_or_outbox" in next(
        entry for entry in inventory if entry["name"] == "garmin_weight_exports"
    )["reasons"]


def test_every_portable_column_has_one_explicit_transport_contract():
    for table in PORTABILITY_SCHEMA_DESCRIPTOR["tables"]:
        model = Base.metadata.tables[table["name"]]
        primary = {item["name"] for item in table["primary_key"]["columns"]}
        values = {item["name"] for item in table["value_columns"]}
        links = {item["name"] for item in table["links"]}
        rebuilt = {item["name"] for item in table["rebuilt_fields"]}
        suppressed = {item["name"] for item in table["suppressed_fields"]}
        # raw_payloads.external_id is intentionally both an ordinary value and
        # conditionally rebuilt when its row has a file resource link.
        allowed_overlap = {"external_id"} if table["name"] == "raw_payloads" else set()
        categories = (primary, values, links, rebuilt, suppressed)
        overlap = {
            name
            for index, category in enumerate(categories)
            for other in categories[index + 1 :]
            for name in category & other
        }
        assert overlap == allowed_overlap
        assert set(model.c.keys()) == set().union(*categories)
        for column in (*table["value_columns"], *table["links"]):
            assert set(column) >= {"name", "nullable", "type"}
            assert set(column["type"]) >= {"codec", "sqlalchemy_type"}
        for link in table["links"]:
            assert link["kind"] in {"row", "connection", "resource"}
            assert link["ref_kind"] in {"r", "c", "f"}
            assert type(link["required"]) is bool
            assert link["target_table"]


def test_dependency_orders_cover_all_tables_and_put_targets_first():
    descriptor = PORTABILITY_SCHEMA_DESCRIPTOR
    insert_order = descriptor["insert_order"]
    delete_order = descriptor["delete_order"]
    assert len(insert_order) == len(set(insert_order)) == 35
    assert set(insert_order) == EXPECTED_PORTABLE_TABLES
    assert delete_order == list(reversed(insert_order))
    position = {name: index for index, name in enumerate(insert_order)}
    for table in descriptor["tables"]:
        for link in table["links"]:
            if link["kind"] == "row":
                assert position[link["target_table"]] < position[table["name"]]


def test_registry_table_and_ownership_drift_fail_the_reviewed_ratchet():
    incomplete = dict(OWNERSHIP_REGISTRY)
    del incomplete["annotations"]
    with pytest.raises(SchemaContractError, match="different tables") as missing:
        build_schema_descriptor(registry=incomplete)
    assert missing.value.code == "registry_incomplete"

    metadata = _metadata_copy()
    metadata.remove(metadata.tables["annotations"])
    with pytest.raises(SchemaContractError) as missing_table:
        build_schema_descriptor(metadata=metadata)
    assert missing_table.value.code == "registry_incomplete"

    changed = dict(OWNERSHIP_REGISTRY)
    changed["annotations"] = replace(
        changed["annotations"], actor=TargetColumn.NONE
    )
    changed_descriptor = build_schema_descriptor(registry=changed)
    assert schema_digest(changed_descriptor) != EXPECTED_DIGEST
    with pytest.raises(SchemaContractError) as mismatch:
        validate_schema_contract(registry=changed, expected_digest=EXPECTED_DIGEST)
    assert mismatch.value.code == "schema_digest_mismatch"


def test_column_and_fk_mutations_change_or_refuse_the_contract():
    metadata = _metadata_copy()
    metadata.tables["annotations"].append_column(Column("future_note", String(20)))
    changed = build_schema_descriptor(metadata=metadata)
    assert schema_digest(changed) != EXPECTED_DIGEST
    with pytest.raises(SchemaContractError) as mismatch:
        validate_schema_contract(metadata=metadata, expected_digest=EXPECTED_DIGEST)
    assert mismatch.value.code == "schema_digest_mismatch"

    ambiguous = _metadata_copy()
    ambiguous.tables["body_scans"].append_constraint(
        ForeignKeyConstraint(["raw_payload_id"], ["weight_logs.id"])
    )
    with pytest.raises(SchemaContractError) as foreign_key:
        build_schema_descriptor(metadata=ambiguous)
    assert foreign_key.value.code == "ambiguous_foreign_key"


def test_unknown_sqlalchemy_type_is_refused_without_repr_fallback():
    metadata = _metadata_copy()
    metadata.tables["annotations"].c.note.type = LargeBinary()
    with pytest.raises(SchemaContractError) as unsupported:
        build_schema_descriptor(metadata=metadata)
    assert unsupported.value.code == "unsupported_column_type"


def test_type_and_ownership_semantics_are_part_of_the_digest():
    metadata = _metadata_copy()
    metadata.tables["annotations"].c.note.type = String(511)
    descriptor = build_schema_descriptor(metadata=metadata)
    assert schema_digest(descriptor) != EXPECTED_DIGEST

    tables = _tables_by_name(PORTABILITY_SCHEMA_DESCRIPTOR)
    assert tables["hevy_workouts"]["ownership"]["connection"] == "required"
    inherited_link = next(
        link
        for link in tables["hevy_exercises"]["links"]
        if link["name"] == "integration_connection_id"
    )
    assert inherited_link["ownership_target"] == "inherited"
    assert inherited_link["required"] is False
    assert next(
        link
        for link in tables["progress_photos"]["links"]
        if link["name"] == "file_asset_id"
    )["required"] is True
    assert next(
        field
        for field in tables["raw_payloads"]["rebuilt_fields"]
        if field["name"] == "external_id"
    )["mode"] == "resource_storage_ref_when_file_link"

    resource_links = {
        table["name"]: next(
            link for link in table["links"] if link["kind"] == "resource"
        )
        for table in tables.values()
        if any(link["kind"] == "resource" for link in table["links"])
    }
    assert set(resource_links) == {"body_scans", "progress_photos", "raw_payloads"}
    assert resource_links["progress_photos"]["purpose_rule"] == {
        "mode": "fixed",
        "purpose": "progress_photo",
    }
    assert expected_resource_purpose("raw_payloads", {"domain": "labs"}) == (
        "lab_document"
    )
    with pytest.raises(SchemaContractError) as unknown_purpose:
        expected_resource_purpose("raw_payloads", {"domain": "genetics"})
    assert unknown_purpose.value.code == "resource_purpose_unknown"
