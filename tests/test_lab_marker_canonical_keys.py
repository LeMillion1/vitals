"""Stable, lossless identity for subject-scoped lab markers."""

from __future__ import annotations

import importlib
import uuid
from datetime import date, datetime, timedelta

import sqlalchemy as sa
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select

from vitals.enums import Domain, Source
from vitals.models.labs import LabMarker, LabResult
from vitals.models.system_alert import SystemAlert
from vitals.services.portability import v1_contract
import vitals.services.labs.alerts as lab_alerts
import vitals.services.labs.markers as lab_markers
import vitals.services.labs.results as lab_results


def test_marker_key_normalizes_only_safe_identity_variants():
    assert lab_markers.normalize_marker_key("TSH") == "tsh"
    assert lab_markers.normalize_marker_key("  tＳh\n") == "tsh"
    assert lab_markers.normalize_marker_key("Фёрритин") == "ферритин"
    assert (
        lab_markers.normalize_marker_key("тиреотропный гормон (ттг)")
        == lab_markers.normalize_marker_key("ТТГ")
    )
    assert lab_markers.normalize_marker_key("Free T4") != (
        lab_markers.normalize_marker_key("Free-T4")
    )


def test_migration_aliases_are_frozen_from_the_runtime_identity_map():
    migration = importlib.import_module(
        "migrations.versions.0077_canonical_lab_marker_keys"
    )

    assert migration._ALIASES == lab_markers.MARKER_ALIASES


def test_v1_results_only_archive_uses_lowest_id_display_for_the_marker_key():
    payload = {
        "lab_results": [
            {"id": 12, "marker": "tsh"},
            {"id": 11, "marker": "TSH"},
        ]
    }

    upgraded = v1_contract._upgrade_lab_marker_identity(payload)

    assert [row["marker"] for row in upgraded["lab_results"]] == ["TSH", "TSH"]
    assert [row["marker_original"] for row in upgraded["lab_results"]] == [
        "tsh",
        "TSH",
    ]
    assert [row["marker_key"] for row in upgraded["lab_results"]] == [
        "tsh",
        "tsh",
    ]
    assert "lab_markers" in upgraded
    assert upgraded["lab_markers"] == []


async def test_case_variants_share_one_catalog_history_and_display(
    db_session,
    owner_write,
):
    first = await lab_results.add_result(
        db_session,
        on_date=date(2026, 8, 1),
        marker="TSH",
        value=4.1,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 8, 1)),
    )
    second = await lab_results.add_result(
        db_session,
        on_date=date(2026, 8, 2),
        marker=" tSh  ",
        value=3.2,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 8, 2)),
    )
    await db_session.commit()

    assert (first.marker, first.marker_original, first.marker_key) == (
        "TSH",
        "TSH",
        "tsh",
    )
    assert (second.marker, second.marker_original, second.marker_key) == (
        "TSH",
        " tSh  ",
        "tsh",
    )
    markers = list(
        await db_session.scalars(
            select(LabMarker).where(
                LabMarker.subject_id == owner_write.subject_id,
                LabMarker.is_canonical.is_(True),
            )
        )
    )
    assert [(row.name, row.normalized_name) for row in markers] == [("TSH", "tsh")]
    history = await lab_results.marker_history(
        db_session,
        "tsh",
        subject_id=owner_write.subject_id,
    )
    assert [point["value"] for point in history] == [4.1, 3.2]
    latest = await lab_results.latest_per_marker(
        db_session,
        subject_id=owner_write.subject_id,
    )
    assert [(row.marker, row.value) for row in latest] == [("TSH", 3.2)]


async def test_dashboard_resolves_a_results_only_alias_without_catalog(
    auth_client,
    db_session,
    legacy_owner_roots,
):
    db_session.add_all(
        [
            LabResult(
                subject_id=legacy_owner_roots.subject_id,
                date=date(2026, 8, 1),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="TSH",
                value=3.2,
            ),
            LabResult(
                subject_id=legacy_owner_roots.subject_id,
                date=date(2026, 8, 2),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="Ferritin",
                value=80.0,
            ),
        ]
    )
    await db_session.commit()

    response = await auth_client.get(
        "/labs?marker=tsh",
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 200
    assert "Динамика: TSH" in response.text
    assert "3.2" in response.text


async def test_defer_alias_resolves_the_canonical_retest_alert(db_session, owner_write):
    measured = date(2026, 1, 1)
    await lab_results.add_result(
        db_session,
        on_date=measured,
        marker="TSH",
        value=3.2,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(measured),
    )
    marker = await lab_markers.get_marker(
        db_session, "TSH", subject_id=owner_write.subject_id
    )
    assert marker is not None
    marker.retest_interval_days = 30
    await db_session.commit()
    later = date(2026, 3, 1)
    prepared = await owner_write.write(later)
    await lab_alerts.refresh_alerts(
        db_session,
        on_date=later,
        subject_id=owner_write.subject_id,
        identity=owner_write.identity,
        prepared_conflict_write=prepared,
    )
    assert await db_session.scalar(
        select(SystemAlert.id).where(
            SystemAlert.subject_id == owner_write.subject_id,
            SystemAlert.alert_key == lab_alerts.RETEST_DUE_KEY,
            SystemAlert.resolved_at.is_(None),
        )
    ) is not None

    await lab_alerts.defer_retest(
        db_session,
        "tSh",
        until=date(2026, 4, 1),
        subject_id=owner_write.subject_id,
        identity=owner_write.identity,
        prepared_conflict_write=prepared,
    )
    assert await db_session.scalar(
        select(SystemAlert.id).where(
            SystemAlert.subject_id == owner_write.subject_id,
            SystemAlert.alert_key == lab_alerts.RETEST_DUE_KEY,
            SystemAlert.resolved_at.is_(None),
        )
    ) is None


def test_0077_losslessly_merges_and_reverses_marker_spellings(monkeypatch):
    migration = importlib.import_module(
        "migrations.versions.0077_canonical_lab_marker_keys"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    markers = sa.Table(
        "lab_markers",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("unit", sa.String(32), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    results = sa.Table(
        "lab_results",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("marker", sa.String(128), nullable=False),
    )
    alerts = sa.Table(
        "system_alerts",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("alert_key", sa.String(128), nullable=False),
        sa.Column("entity_ref", sa.String(255), nullable=False),
    )
    first_subject = uuid.uuid4()
    second_subject = uuid.uuid4()
    result_only_subject = uuid.uuid4()
    stamp = datetime(2026, 8, 1, 12, 0)

    try:
        with engine.begin() as connection:
            metadata.create_all(connection)
            connection.execute(
                markers.insert(),
                [
                    {
                        "id": 1,
                        "subject_id": first_subject,
                        "actor_user_id": uuid.uuid4(),
                        "name": "TSH",
                        "unit": "mIU/L",
                        "note": "first configuration",
                        "updated_at": stamp,
                    },
                    {
                        "id": 2,
                        "subject_id": first_subject,
                        "actor_user_id": uuid.uuid4(),
                        "name": "TSh",
                        "unit": "uIU/mL",
                        "note": "newer configuration",
                        "updated_at": stamp + timedelta(seconds=1),
                    },
                    {
                        "id": 3,
                        "subject_id": second_subject,
                        "actor_user_id": None,
                        "name": "tsh",
                        "unit": "mIU/L",
                        "note": "other subject",
                        "updated_at": stamp,
                    },
                    {
                        "id": 4,
                        "subject_id": first_subject,
                        "actor_user_id": None,
                        "name": "tSH",
                        "unit": "seed-unit",
                        "note": "newest actorless seed",
                        "updated_at": stamp + timedelta(seconds=2),
                    },
                ],
            )
            connection.execute(
                results.insert(),
                [
                    {
                        "id": 11,
                        "subject_id": first_subject,
                        "date": date(2026, 8, 1),
                        "marker": "TSH",
                    },
                    {
                        "id": 12,
                        "subject_id": first_subject,
                        "date": date(2026, 8, 2),
                        "marker": "TSh",
                    },
                    {
                        "id": 13,
                        "subject_id": second_subject,
                        "date": date(2026, 8, 2),
                        "marker": "tsh",
                    },
                    {
                        "id": 14,
                        "subject_id": result_only_subject,
                        "date": date(2026, 8, 1),
                        "marker": "Free T4",
                    },
                    {
                        "id": 15,
                        "subject_id": result_only_subject,
                        "date": date(2026, 8, 2),
                        "marker": "free t4",
                    },
                ],
            )
            connection.execute(
                alerts.insert(),
                {
                    "id": 21,
                    "subject_id": first_subject,
                    "alert_key": "labs.out_of_range",
                    "entity_ref": "TSH:11",
                },
            )
            operations = Operations(MigrationContext.configure(connection))
            monkeypatch.setattr(migration, "op", operations)

            migration.upgrade()
            upgraded_markers = connection.execute(
                sa.text(
                    "SELECT id, subject_id, name, normalized_name, is_canonical "
                    "FROM lab_markers ORDER BY id"
                )
            ).mappings().all()
            assert [row["normalized_name"] for row in upgraded_markers] == [
                "tsh",
                "tsh",
                "tsh",
                "tsh",
            ]
            assert [bool(row["is_canonical"]) for row in upgraded_markers] == [
                False,
                True,
                True,
                False,
            ]
            assert connection.execute(
                sa.text("SELECT unit, note FROM lab_markers ORDER BY id")
            ).all() == [
                ("mIU/L", "first configuration"),
                ("uIU/mL", "newer configuration"),
                ("mIU/L", "other subject"),
                ("seed-unit", "newest actorless seed"),
            ]
            with pytest.raises(sa.exc.IntegrityError):
                migrated_markers = sa.Table(
                    "lab_markers", sa.MetaData(), autoload_with=connection
                )
                connection.execute(
                    migrated_markers.insert(),
                    {
                        "id": 5,
                        "subject_id": first_subject.hex,
                        "name": "TsH alias",
                        "updated_at": stamp,
                        "normalized_name": "tsh",
                        "is_canonical": True,
                    },
                )
            upgraded_results = connection.execute(
                sa.text(
                    "SELECT id, marker, marker_key, marker_original "
                    "FROM lab_results ORDER BY id"
                )
            ).mappings().all()
            assert [tuple(row.values()) for row in upgraded_results] == [
                (11, "TSh", "tsh", "TSH"),
                (12, "TSh", "tsh", "TSh"),
                (13, "tsh", "tsh", "tsh"),
                (14, "Free T4", "free t4", "Free T4"),
                (15, "Free T4", "free t4", "free t4"),
            ]
            assert connection.scalar(
                sa.text("SELECT entity_ref FROM system_alerts WHERE id = 21")
            ) == "TSh:11"
            assert {
                item["name"] for item in inspect(connection).get_indexes("lab_markers")
            } >= {
                "ix_lab_markers_subject_normalized_name",
                "uq_lab_markers_subject_normalized_canonical",
            }

            migration.downgrade()
            assert connection.execute(
                sa.text("SELECT marker FROM lab_results ORDER BY id")
            ).scalars().all() == ["TSH", "TSh", "tsh", "Free T4", "free t4"]
            assert connection.scalar(
                sa.text("SELECT entity_ref FROM system_alerts WHERE id = 21")
            ) == "TSH:11"
            assert "marker_key" not in {
                column["name"] for column in inspect(connection).get_columns("lab_results")
            }
            assert "normalized_name" not in {
                column["name"] for column in inspect(connection).get_columns("lab_markers")
            }
    finally:
        engine.dispose()


def test_0077_refuses_an_empty_historical_result_key(monkeypatch):
    migration = importlib.import_module(
        "migrations.versions.0077_canonical_lab_marker_keys"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "lab_markers",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    results = sa.Table(
        "lab_results",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("marker", sa.String(128), nullable=False),
    )
    sa.Table(
        "system_alerts",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("alert_key", sa.String(128), nullable=False),
        sa.Column("entity_ref", sa.String(255), nullable=False),
    )
    try:
        with engine.begin() as connection:
            metadata.create_all(connection)
            connection.execute(
                results.insert(),
                {
                    "id": 1,
                    "subject_id": uuid.uuid4(),
                    "date": date(2026, 8, 1),
                    "marker": "   ",
                },
            )
            monkeypatch.setattr(
                migration,
                "op",
                Operations(MigrationContext.configure(connection)),
            )
            with pytest.raises(RuntimeError, match="invalid normalized lab result"):
                migration.upgrade()
    finally:
        engine.dispose()
