"""Portable replacement applies one decoded subject graph and only flushes."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import date, datetime, time
from types import MappingProxyType

import pytest
from sqlalchemy import func, select

from vitals.enums import (
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionStatus,
    Source,
    UserStatus,
)
from vitals.models.body_scan import BodyScan, BodyScanMetric
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.models.weight import WeightLog
from vitals.services.portability.connection_mapping import resolve_connection_mapping
from vitals.services.portability.record_decoder import (
    DecodedConnection,
    DecodedLink,
    DecodedRecord,
    DecodedResource,
    DecodedRow,
    DecodedTable,
)
from vitals.services.portability.replacement_apply import (
    ReplacementApplyError,
    apply_record_replacement,
)
from vitals.services.portability.resource_staging import (
    StagedResourceBinding,
    StagedResourceMapping,
)
from vitals.services.portability.schema import (
    PORTABILITY_SCHEMA_DESCRIPTOR,
    PORTABILITY_SCHEMA_DIGEST,
)


_SCHEMA = {table["name"]: table for table in PORTABILITY_SCHEMA_DESCRIPTOR["tables"]}


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
        display_name=f"Subject {slug}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return subject


async def _asset(
    session,
    subject_id: uuid.UUID,
    *,
    storage_ref: str,
    purpose: str,
    body: bytes,
    media_type: str = "application/pdf",
) -> FileAsset:
    asset = FileAsset(
        subject_id=subject_id,
        opaque_key=uuid.uuid4(),
        purpose=purpose,
        storage_backend=FileStorageBackend.PRIVATE_LOCAL.value,
        storage_ref=storage_ref,
        media_type=media_type,
        byte_size=len(body),
        sha256_hex=hashlib.sha256(body).hexdigest(),
        status=FileAssetStatus.ACTIVE.value,
    )
    session.add(asset)
    await session.flush()
    return asset


def _value(column: dict) -> object:
    codec = column["type"]["codec"]
    if codec == "boolean":
        return False
    if codec == "integer":
        return 7
    if codec == "float":
        return 1.5
    if codec == "decimal_string":
        return "1.5"
    if codec == "string":
        return "x"
    if codec == "date_iso8601":
        return date(2026, 8, 25)
    if codec == "datetime_iso8601":
        return datetime(2026, 8, 25, 12, 34, 56)
    if codec == "time_iso8601":
        return time(12, 34, 56)
    if codec == "json":
        return MappingProxyType({"preserved": (1, True, None, MappingProxyType({"key": "value"}))})
    raise AssertionError(codec)


def _link(table_name: str, column_name: str, target_ref: str) -> DecodedLink:
    item = next(link for link in _SCHEMA[table_name]["links"] if link["name"] == column_name)
    return DecodedLink(
        column=column_name,
        kind=item["kind"],
        target_ref=target_ref,
        target_table=item["target_table"],
        target_column=item["target_column"],
        required=item["required"],
    )


def _row(
    table_name: str,
    ref: str,
    *,
    links: dict[str, DecodedLink] | None = None,
) -> DecodedRow:
    values = {column["name"]: _value(column) for column in _SCHEMA[table_name]["value_columns"]}
    if table_name == "raw_payloads":
        values.update(domain=Domain.LABS.value, source=Source.MANUAL.value)
        if links and "file_asset_id" in links:
            values.pop("external_id")
    elif table_name == "body_scans":
        values.update(
            domain=Domain.BODY_COMPOSITION.value,
            source=Source.BODY_SCAN.value,
        )
    elif table_name == "weight_logs":
        values.update(domain=Domain.WEIGHT.value, source=Source.MANUAL.value)
    return DecodedRow(
        table=table_name,
        ref=ref,
        values=MappingProxyType(values),
        links=MappingProxyType(links or {}),
    )


def _record(raw_resource: DecodedResource, scan_resource: DecodedResource) -> DecodedRecord:
    raw = _row(
        "raw_payloads",
        "r000000000001",
        links={"file_asset_id": _link("raw_payloads", "file_asset_id", "f00000001")},
    )
    scan = _row(
        "body_scans",
        "r000000000002",
        links={
            "file_asset_id": _link("body_scans", "file_asset_id", "f00000002"),
            "raw_payload_id": _link("body_scans", "raw_payload_id", raw.ref),
        },
    )
    metric = _row(
        "body_scan_metrics",
        "r000000000003",
        links={"scan_id": _link("body_scan_metrics", "scan_id", scan.ref)},
    )
    weight = _row(
        "weight_logs",
        "r000000000004",
        links={
            "integration_connection_id": _link(
                "weight_logs", "integration_connection_id", "c00000001"
            ),
            "raw_payload_id": _link("weight_logs", "raw_payload_id", raw.ref),
        },
    )
    rows = {row.table: (row,) for row in (raw, scan, metric, weight)}
    tables = tuple(
        DecodedTable(name=name, rows=rows.get(name, ()))
        for name in PORTABILITY_SCHEMA_DESCRIPTOR["insert_order"]
    )
    return DecodedRecord(
        record_ref="replacement_record",
        schema_digest=PORTABILITY_SCHEMA_DIGEST,
        connections=(
            DecodedConnection(
                ref="c00000001",
                provider="garmin",
                connection_type="account",
            ),
        ),
        resources=(raw_resource, scan_resource),
        tables=tables,
        row_count=4,
    )


def _resource(
    ref: str,
    *,
    purpose: str,
    body: bytes,
    media_type: str = "application/pdf",
) -> DecodedResource:
    digest = hashlib.sha256(body).hexdigest()
    return DecodedResource(
        ref=ref,
        purpose=purpose,
        media_type=media_type,
        byte_size=len(body),
        sha256_hex=digest,
        object_path=f"objects/sha256/{digest}",
    )


async def _scenario(session):
    subject = await _subject(session, "replacement-mine")
    other = await _subject(session, "replacement-other")
    connection = IntegrationConnection(
        subject_id=subject.id,
        provider="garmin",
        connection_type="account",
        external_account_discriminator="synthetic-account",
        credential_ref="synthetic-credential",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    session.add(connection)

    old_raw_body = b"old raw file"
    old_scan_body = b"old scan file"
    retained_body = b"retained audit file"
    new_raw_body = b"new raw file"
    new_scan_body = b"new scan file"
    old_raw_asset = await _asset(
        session,
        subject.id,
        storage_ref="old/raw.pdf",
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        body=old_raw_body,
    )
    old_scan_asset = await _asset(
        session,
        subject.id,
        storage_ref="old/scan.pdf",
        purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT.value,
        body=old_scan_body,
    )
    retained_asset = await _asset(
        session,
        subject.id,
        storage_ref="old/retained.pdf",
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        body=retained_body,
    )
    new_raw_asset = await _asset(
        session,
        subject.id,
        storage_ref="new/raw.pdf",
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        body=new_raw_body,
    )
    new_scan_asset = await _asset(
        session,
        subject.id,
        storage_ref="new/scan.pdf",
        purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT.value,
        body=new_scan_body,
    )
    other_asset = await _asset(
        session,
        other.id,
        storage_ref="other/raw.pdf",
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        body=new_raw_body,
    )
    await session.flush()

    old_raw = RawPayload(
        subject_id=subject.id,
        file_asset_id=old_raw_asset.id,
        domain=Domain.LABS.value,
        source=Source.MANUAL.value,
        external_id="old/raw.pdf",
        fetched_at=datetime(2026, 1, 1),
        payload={"old": True},
    )
    retained_raw = RawPayload(
        subject_id=subject.id,
        file_asset_id=retained_asset.id,
        domain=Domain.LABS.value,
        source=Source.MANUAL.value,
        external_id="old/retained.pdf",
        fetched_at=datetime(2026, 1, 2),
        payload={"retained": True},
    )
    session.add_all([old_raw, retained_raw])
    await session.flush()
    old_scan = BodyScan(
        subject_id=subject.id,
        file_asset_id=old_scan_asset.id,
        raw_payload_id=old_raw.id,
        file_key="old/scan.pdf",
        date=date(2026, 1, 3),
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.BODY_SCAN.value,
    )
    old_weight = WeightLog(
        subject_id=subject.id,
        raw_payload_id=old_raw.id,
        date=date(2026, 1, 4),
        weight_kg=90.0,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
    )
    session.add_all([old_scan, old_weight])
    await session.flush()
    old_metric = BodyScanMetric(
        scan_id=old_scan.id,
        subject_id=None,
        metric_key="old_metric",
        label="Old metric",
        value=42.0,
        category="composition",
    )
    session.add(old_metric)
    await session.flush()

    raw_resource = _resource(
        "f00000001",
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        body=new_raw_body,
    )
    scan_resource = _resource(
        "f00000002",
        purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT.value,
        body=new_scan_body,
    )
    record = _record(raw_resource, scan_resource)
    ids = {
        "subject": subject.id,
        "other": other.id,
        "connection": connection.id,
        "old_raw_asset": old_raw_asset.id,
        "old_scan_asset": old_scan_asset.id,
        "retained_asset": retained_asset.id,
        "new_raw_asset": new_raw_asset.id,
        "new_scan_asset": new_scan_asset.id,
        "other_asset": other_asset.id,
        "retained_raw": retained_raw.id,
    }
    bindings = StagedResourceMapping(
        bindings=(
            StagedResourceBinding(
                ref="f00000001",
                file_asset_id=new_raw_asset.id,
                storage_ref=new_raw_asset.storage_ref,
                purpose=new_raw_asset.purpose,
                media_type=new_raw_asset.media_type,
                byte_size=new_raw_asset.byte_size,
                sha256_hex=new_raw_asset.sha256_hex,
            ),
            StagedResourceBinding(
                ref="f00000002",
                file_asset_id=new_scan_asset.id,
                storage_ref=new_scan_asset.storage_ref,
                purpose=new_scan_asset.purpose,
                media_type=new_scan_asset.media_type,
                byte_size=new_scan_asset.byte_size,
                sha256_hex=new_scan_asset.sha256_hex,
            ),
        ),
        newly_written_objects=(),
    )
    await session.commit()
    mapping = await resolve_connection_mapping(
        session,
        target_subject_id=subject.id,
        archive_connections=record.connections,
        connection_ids_by_ref={"c00000001": connection.id},
    )
    return ids, record, mapping, bindings


@pytest.mark.asyncio
async def test_apply_replaces_dependencies_rebuilds_locators_and_rollback_restores_old_graph(
    db_session,
):
    ids, record, mapping, bindings = await _scenario(db_session)

    result = await apply_record_replacement(
        db_session,
        target_subject_id=ids["subject"],
        record=record,
        connection_mapping=mapping,
        resource_bindings=bindings,
        retained_raw_payload_ids=(ids["retained_raw"],),
    )

    assert set(result.row_ids_by_ref) == {
        "r000000000001",
        "r000000000002",
        "r000000000003",
        "r000000000004",
    }
    assert result.inserted_rows == 4
    inserted_counts = {item.table: item.rows for item in result.inserted}
    assert {
        table: inserted_counts[table]
        for table in (
            "raw_payloads",
            "body_scans",
            "body_scan_metrics",
            "weight_logs",
        )
    } == {
        "raw_payloads": 1,
        "body_scans": 1,
        "body_scan_metrics": 1,
        "weight_logs": 1,
    }
    assert set(result.old_file_asset_ids) == {
        ids["old_raw_asset"],
        ids["old_scan_asset"],
    }
    assert result.old_file_asset_count == 2
    assert ids["retained_asset"] not in result.old_file_asset_ids
    with pytest.raises(TypeError):
        result.row_ids_by_ref["future"] = 99

    new_raw_id = result.row_ids_by_ref["r000000000001"]
    new_scan_id = result.row_ids_by_ref["r000000000002"]
    raw = (
        await db_session.execute(select(RawPayload).where(RawPayload.id == new_raw_id))
    ).scalar_one()
    scan = (
        await db_session.execute(select(BodyScan).where(BodyScan.id == new_scan_id))
    ).scalar_one()
    metric = (
        await db_session.execute(
            select(BodyScanMetric).where(
                BodyScanMetric.id == result.row_ids_by_ref["r000000000003"]
            )
        )
    ).scalar_one()
    weight = (
        await db_session.execute(
            select(WeightLog).where(WeightLog.id == result.row_ids_by_ref["r000000000004"])
        )
    ).scalar_one()
    assert raw.subject_id == ids["subject"]
    assert raw.actor_user_id is None
    assert raw.file_asset_id == ids["new_raw_asset"]
    assert raw.external_id == "new/raw.pdf"
    assert raw.payload == {"preserved": [1, True, None, {"key": "value"}]}
    assert scan.raw_payload_id == raw.id
    assert scan.file_asset_id == ids["new_scan_asset"]
    assert scan.file_key == "new/scan.pdf"
    assert scan.actor_user_id is None
    assert metric.scan_id == scan.id and metric.subject_id == ids["subject"]
    assert weight.raw_payload_id == raw.id
    assert weight.integration_connection_id == ids["connection"]
    assert weight.actor_user_id is None
    assert (
        await db_session.scalar(
            select(func.count()).select_from(RawPayload).where(RawPayload.id == ids["retained_raw"])
        )
    ) == 1

    await db_session.rollback()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(RawPayload)
            .where(
                RawPayload.subject_id == ids["subject"],
                RawPayload.external_id == "old/raw.pdf",
            )
        )
    ) == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(RawPayload)
            .where(
                RawPayload.subject_id == ids["subject"],
                RawPayload.external_id == "new/raw.pdf",
            )
        )
    ) == 0
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(BodyScanMetric)
            .where(BodyScanMetric.metric_key == "old_metric")
        )
    ) == 1


@pytest.mark.asyncio
async def test_apply_refuses_incomplete_or_cross_subject_resource_bindings_before_delete(
    db_session,
):
    ids, record, mapping, bindings = await _scenario(db_session)

    with pytest.raises(ReplacementApplyError) as incomplete:
        await apply_record_replacement(
            db_session,
            target_subject_id=ids["subject"],
            record=record,
            connection_mapping=mapping,
            resource_bindings={"f00000001": bindings["f00000001"]},
            retained_raw_payload_ids=(ids["retained_raw"],),
        )
    assert incomplete.value.code == "resource_bindings_incomplete"

    crossed = {
        **bindings,
        "f00000001": StagedResourceBinding(
            ref="f00000001",
            file_asset_id=ids["other_asset"],
            storage_ref="other/raw.pdf",
            purpose=FileAssetPurpose.LAB_DOCUMENT.value,
            media_type="application/pdf",
            byte_size=len(b"new raw file"),
            sha256_hex=hashlib.sha256(b"new raw file").hexdigest(),
        ),
    }
    with pytest.raises(ReplacementApplyError) as cross_subject:
        await apply_record_replacement(
            db_session,
            target_subject_id=ids["subject"],
            record=record,
            connection_mapping=mapping,
            resource_bindings=crossed,
            retained_raw_payload_ids=(ids["retained_raw"],),
        )
    assert cross_subject.value.code == "resource_binding_invalid"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(RawPayload)
            .where(
                RawPayload.subject_id == ids["subject"],
                RawPayload.external_id == "old/raw.pdf",
            )
        )
    ) == 1


@pytest.mark.asyncio
async def test_apply_refuses_retained_raw_from_another_subject(db_session):
    ids, record, mapping, bindings = await _scenario(db_session)
    other_raw = RawPayload(
        subject_id=ids["other"],
        domain=Domain.LABS.value,
        source=Source.MANUAL.value,
        external_id="other/retained.pdf",
        fetched_at=datetime(2026, 1, 1),
        payload={},
    )
    db_session.add(other_raw)
    await db_session.flush()

    with pytest.raises(ReplacementApplyError) as raised:
        await apply_record_replacement(
            db_session,
            target_subject_id=ids["subject"],
            record=record,
            connection_mapping=mapping,
            resource_bindings=bindings,
            retained_raw_payload_ids=(other_raw.id,),
        )
    assert raised.value.code == "retained_raw_scope_invalid"


@pytest.mark.asyncio
async def test_apply_refuses_connection_mapping_for_another_subject_before_delete(
    db_session,
):
    ids, record, mapping, bindings = await _scenario(db_session)
    crossed_mapping = replace(mapping, target_subject_id=ids["other"])

    with pytest.raises(ReplacementApplyError) as raised:
        await apply_record_replacement(
            db_session,
            target_subject_id=ids["subject"],
            record=record,
            connection_mapping=crossed_mapping,
            resource_bindings=bindings,
            retained_raw_payload_ids=(ids["retained_raw"],),
        )
    assert raised.value.code == "connection_mapping_subject_invalid"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(RawPayload)
            .where(
                RawPayload.subject_id == ids["subject"],
                RawPayload.external_id == "old/raw.pdf",
            )
        )
    ) == 1
