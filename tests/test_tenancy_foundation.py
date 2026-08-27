"""Persistence contracts for the PR-03 tenancy stage-0 foundation.

The stage creates ownership roots and scoped setting namespaces only.  It does
not activate integrations, move files, copy legacy settings, or make a second
health subject writable.
"""

from __future__ import annotations

import importlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    PrimaryKeyConstraint,
    UniqueConstraint,
    create_engine,
    inspect,
    select,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError

import vitals.models  # noqa: F401 -- register the complete metadata graph
from vitals.enums import (
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.scoped_settings import (
    IntegrationConnectionSetting,
    PlatformSetting,
    SubjectSetting,
    UserSetting,
)
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.operations.ownership.portability_v1 import import_full
from vitals.ownership import OWNERSHIP_REGISTRY
from vitals.services.portability.v1_contract import _EXCLUDED_TABLES
from vitals.services.portability.v1_export import export_full


FOUNDATION_MODELS = (
    IntegrationConnection,
    FileAsset,
    PlatformSetting,
    UserSetting,
    SubjectSetting,
    IntegrationConnectionSetting,
)
FOUNDATION_TABLES = frozenset(model.__tablename__ for model in FOUNDATION_MODELS)
NOW = datetime(2026, 8, 19, 10, tzinfo=UTC)


def _normalized_sql(value: Any) -> str:
    return " ".join(str(value).split())


def _normalized_default(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip().strip("'\"").lower()
    if rendered in {"now()", "current_timestamp"}:
        return "now"
    return rendered


def _model_schema(model: type) -> dict[str, Any]:
    table = model.__table__
    sqlite_dialect = sqlite.dialect()
    return {
        "columns": {
            column.name: (
                str(column.type.compile(dialect=sqlite_dialect)),
                column.nullable,
                column.primary_key,
                _normalized_default(
                    column.server_default.arg if column.server_default is not None else None
                ),
            )
            for column in table.columns
        },
        "primary_key": (
            table.primary_key.name,
            tuple(column.name for column in table.primary_key.columns),
        ),
        "foreign_keys": {
            (
                tuple(constraint.column_keys),
                tuple(element.target_fullname for element in constraint.elements),
                constraint.ondelete,
            )
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
        },
        "uniques": {
            constraint.name: tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        },
        "checks": {
            constraint.name: _normalized_sql(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        },
        "indexes": {
            index.name: (tuple(column.name for column in index.columns), bool(index.unique))
            for index in table.indexes
        },
    }


def _migrated_schema(inspector: Any, table_name: str) -> dict[str, Any]:
    return {
        "columns": {
            column["name"]: (
                str(column["type"]),
                column["nullable"],
                bool(column["primary_key"]),
                _normalized_default(column.get("default")),
            )
            for column in inspector.get_columns(table_name)
        },
        "primary_key": (
            inspector.get_pk_constraint(table_name).get("name"),
            tuple(inspector.get_pk_constraint(table_name)["constrained_columns"]),
        ),
        "foreign_keys": {
            (
                tuple(foreign_key["constrained_columns"]),
                tuple(
                    f"{foreign_key['referred_table']}.{column}"
                    for column in foreign_key["referred_columns"]
                ),
                (foreign_key.get("options") or {}).get("ondelete"),
            )
            for foreign_key in inspector.get_foreign_keys(table_name)
        },
        "uniques": {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table_name)
        },
        "checks": {
            constraint["name"]: _normalized_sql(constraint["sqltext"])
            for constraint in inspector.get_check_constraints(table_name)
        },
        "indexes": {
            index["name"]: (tuple(index["column_names"]), bool(index["unique"]))
            for index in inspector.get_indexes(table_name)
        },
    }


def _assert_migration_matches_models(connection: Any) -> None:
    inspector = inspect(connection)
    assert FOUNDATION_TABLES <= set(inspector.get_table_names())
    for model in FOUNDATION_MODELS:
        expected = _model_schema(model)
        # Current models include the redundant (id, subject_id) referenced keys
        # added by 0037 for future subject-equality FKs. Keep this frozen 0036
        # test honest without rewriting an already published migration.
        expected["uniques"].pop(
            {
                IntegrationConnection: "uq_integration_connections_id_subject",
                FileAsset: "uq_file_assets_id_subject",
            }.get(model),
            None,
        )
        if model is FileAsset:
            # Revision 0067 extends this enum-backed CHECK for care-message
            # attachments. This test exercises the frozen 0036 schema alone;
            # the full migration-chain test proves the current constraint.
            expected["checks"]["ck_file_assets_purpose"] = _normalized_sql(
                "purpose IN ('progress_photo', 'lab_document', "
                "'body_scan_document')"
            )
        assert _migrated_schema(inspector, model.__tablename__) == expected


def test_0036_upgrades_from_empty_0035_and_downgrades_cleanly(monkeypatch):
    """Run the real DDL in the same empty-subject state seen before app startup."""

    identity_migration = importlib.import_module(
        "migrations.versions.0035_identity_foundation"
    )
    tenancy_migration = importlib.import_module(
        "migrations.versions.0036_tenancy_roots_and_scoped_settings"
    )
    engine = create_engine("sqlite://")

    try:
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            monkeypatch.setattr(identity_migration, "op", operations)
            monkeypatch.setattr(tenancy_migration, "op", operations)

            identity_migration.upgrade()
            tables_at_0035 = set(inspect(connection).get_table_names())
            assert "health_subjects" in tables_at_0035

            tenancy_migration.upgrade()
            _assert_migration_matches_models(connection)

            tenancy_migration.downgrade()
            assert set(inspect(connection).get_table_names()) == tables_at_0035

            # The real downgrade must also leave a repeat upgrade viable.
            tenancy_migration.upgrade()
            _assert_migration_matches_models(connection)
            tenancy_migration.downgrade()
    finally:
        engine.dispose()


def test_model_metadata_exposes_exact_stage_zero_contract():
    connection = IntegrationConnection.__table__
    asset = FileAsset.__table__

    assert set(connection.columns.keys()) == {
        "id",
        "subject_id",
        "provider",
        "connection_type",
        "external_account_discriminator",
        "credential_ref",
        "status",
        "retired_at",
        "created_at",
        "updated_at",
    }
    assert set(asset.columns.keys()) == {
        "id",
        "subject_id",
        "uploaded_by_user_id",
        "opaque_key",
        "purpose",
        "storage_backend",
        "storage_ref",
        "media_type",
        "byte_size",
        "sha256_hex",
        "status",
        "deleted_at",
        "purged_at",
        "created_at",
        "updated_at",
    }
    assert asset.c.uploaded_by_user_id.nullable is True
    assert connection.c.credential_ref.nullable is True
    assert _normalized_default(connection.c.status.server_default.arg) == "pending"
    assert _normalized_default(asset.c.status.server_default.arg) == "pending"
    assert connection.c.id.default is not None and connection.c.id.server_default is None
    assert asset.c.opaque_key.default is not None and asset.c.opaque_key.server_default is None

    for model in FOUNDATION_MODELS:
        table = model.__table__
        for name in ("created_at", "updated_at"):
            column = table.c[name]
            assert isinstance(column.type, DateTime)
            assert column.type.timezone is True
            assert column.server_default is not None
        assert table.c.updated_at.onupdate is not None

    expected_setting_pks = {
        PlatformSetting: ("key",),
        UserSetting: ("user_id", "key"),
        SubjectSetting: ("subject_id", "key"),
        IntegrationConnectionSetting: ("integration_connection_id", "key"),
    }
    for model, primary_key in expected_setting_pks.items():
        table = model.__table__
        assert tuple(column.name for column in table.primary_key.columns) == primary_key
        assert not table.indexes
        assert isinstance(table.primary_key, PrimaryKeyConstraint)
        value_type = table.c.value.type
        assert isinstance(value_type.dialect_impl(sqlite.dialect()), sqlite.JSON)
        assert isinstance(
            value_type.dialect_impl(postgresql.dialect()), postgresql.JSONB
        )

    assert {member.value for member in IntegrationProvider} == {
        "garmin",
        "hevy",
        "openrouter",
        "telegram",
    }
    assert {member.value for member in IntegrationConnectionType} == {
        "account",
        "import",
        "ai_gateway",
        "recipient",
    }
    assert {member.value for member in IntegrationConnectionStatus} == {
        "legacy",
        "pending",
        "active",
        "disabled",
        "retired",
    }
    assert {member.value for member in FileAssetStatus} == {
        "legacy_placeholder",
        "pending",
        "active",
        "deleted",
        "purged",
    }


async def _identity_graph(db_session: Any, slug: str) -> tuple[User, HealthSubject]:
    user = User(
        username=slug,
        normalized_username=slug.casefold(),
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=f"Synthetic {slug}",
        timezone="Asia/Almaty",
    )
    db_session.add(subject)
    await db_session.flush()
    return user, subject


def _connection(subject_id: uuid.UUID, **changes: Any) -> IntegrationConnection:
    values: dict[str, Any] = {
        "subject_id": subject_id,
        "provider": IntegrationProvider.GARMIN.value,
        "connection_type": IntegrationConnectionType.ACCOUNT.value,
        "external_account_discriminator": "synthetic-account-v1",
        "credential_ref": "secret_store:v1:synthetic-shared",
        "status": IntegrationConnectionStatus.PENDING.value,
    }
    values.update(changes)
    return IntegrationConnection(**values)


def _asset(subject_id: uuid.UUID, **changes: Any) -> FileAsset:
    values: dict[str, Any] = {
        "subject_id": subject_id,
        "uploaded_by_user_id": None,
        "purpose": FileAssetPurpose.PROGRESS_PHOTO.value,
        "storage_backend": FileStorageBackend.PRIVATE_LOCAL.value,
        "storage_ref": f"synthetic/{uuid.uuid4().hex}",
        "status": FileAssetStatus.PENDING.value,
    }
    values.update(changes)
    return FileAsset(**values)


async def test_scoped_settings_isolate_equal_keys_and_round_trip_json(db_session):
    first_user, first_subject = await _identity_graph(db_session, "scope-first")
    second_user, second_subject = await _identity_graph(db_session, "scope-second")
    first_connection = _connection(first_subject.id)
    second_connection = _connection(second_subject.id)
    db_session.add_all([first_connection, second_connection])
    await db_session.flush()
    first_user_id = first_user.id
    second_user_id = second_user.id
    first_subject_id = first_subject.id
    second_subject_id = second_subject.id
    first_connection_id = first_connection.id
    second_connection_id = second_connection.id

    db_session.add_all(
        [
            PlatformSetting(key="feature_flags", value={"reports": True}),
            UserSetting(user_id=first_user.id, key="preference", value=["metric", 1]),
            UserSetting(user_id=second_user.id, key="preference", value="compact"),
            SubjectSetting(subject_id=first_subject.id, key="preference", value=True),
            SubjectSetting(
                subject_id=second_subject.id,
                key="preference",
                value={"window": 7},
            ),
            IntegrationConnectionSetting(
                integration_connection_id=first_connection.id,
                key="preference",
                value={"hours": 6},
            ),
            IntegrationConnectionSetting(
                integration_connection_id=second_connection.id,
                key="preference",
                value=False,
            ),
        ]
    )
    await db_session.flush()

    # Connection natural identity is subject-scoped; a shared credential handle is
    # deliberately not a uniqueness or ownership boundary.
    assert first_connection.external_account_discriminator == (
        second_connection.external_account_discriminator
    )
    assert first_connection.credential_ref == second_connection.credential_ref

    db_session.expire_all()

    assert (await db_session.get(PlatformSetting, "feature_flags")).value == {
        "reports": True
    }
    assert (
        await db_session.get(UserSetting, (first_user_id, "preference"))
    ).value == ["metric", 1]
    assert (
        await db_session.get(UserSetting, (second_user_id, "preference"))
    ).value == "compact"
    assert (
        await db_session.get(SubjectSetting, (first_subject_id, "preference"))
    ).value is True
    assert (
        await db_session.get(SubjectSetting, (second_subject_id, "preference"))
    ).value == {"window": 7}
    assert (
        await db_session.get(
            IntegrationConnectionSetting, (first_connection_id, "preference")
        )
    ).value == {"hours": 6}
    assert (
        await db_session.get(
            IntegrationConnectionSetting, (second_connection_id, "preference")
        )
    ).value is False


@pytest.mark.parametrize(
    "scope",
    ["platform", "user", "subject", "connection"],
)
async def test_setting_primary_keys_reject_duplicates_within_one_scope(
    db_session, scope
):
    user, subject = await _identity_graph(db_session, f"duplicate-{scope}")
    connection = _connection(subject.id)
    db_session.add(connection)
    await db_session.flush()

    factories = {
        "platform": lambda: PlatformSetting(key="same", value={"v": 1}),
        "user": lambda: UserSetting(user_id=user.id, key="same", value={"v": 1}),
        "subject": lambda: SubjectSetting(
            subject_id=subject.id, key="same", value={"v": 1}
        ),
        "connection": lambda: IntegrationConnectionSetting(
            integration_connection_id=connection.id, key="same", value={"v": 1}
        ),
    }
    db_session.add_all([factories[scope](), factories[scope]()])

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    "scope",
    ["platform", "user", "subject", "connection"],
)
async def test_setting_check_constraints_reject_blank_keys(db_session, scope):
    user, subject = await _identity_graph(db_session, f"blank-{scope}")
    connection = _connection(subject.id)
    db_session.add(connection)
    await db_session.flush()

    rows = {
        "platform": PlatformSetting(key="   ", value=True),
        "user": UserSetting(user_id=user.id, key="   ", value=True),
        "subject": SubjectSetting(subject_id=subject.id, key="   ", value=True),
        "connection": IntegrationConnectionSetting(
            integration_connection_id=connection.id, key="   ", value=True
        ),
    }
    db_session.add(rows[scope])

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "unknown"},
        {"connection_type": "unknown"},
        {"connection_type": IntegrationConnectionType.RECIPIENT.value},
        {"status": "unknown"},
        {"external_account_discriminator": "   "},
        {"credential_ref": "   "},
        {"status": IntegrationConnectionStatus.RETIRED.value},
        {"retired_at": NOW},
    ],
    ids=[
        "provider",
        "connection-type",
        "provider-type-pair",
        "status",
        "blank-discriminator",
        "blank-credential-ref",
        "retired-without-time",
        "time-without-retirement",
    ],
)
async def test_integration_connection_checks_reject_invalid_rows(db_session, changes):
    _, subject = await _identity_graph(db_session, "invalid-connection")
    db_session.add(_connection(subject.id, **changes))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_connection_natural_key_is_unique_only_within_subject(db_session):
    _, first_subject = await _identity_graph(db_session, "connection-first")
    _, second_subject = await _identity_graph(db_session, "connection-second")
    first = _connection(first_subject.id)
    cross_subject = _connection(second_subject.id)
    db_session.add_all([first, cross_subject])
    await db_session.flush()

    db_session.add(_connection(first_subject.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    "changes",
    [
        {"purpose": "unknown"},
        {"storage_backend": "unknown"},
        {"status": "unknown"},
        {"storage_ref": "   "},
        {"storage_ref": "/absolute/file.jpg"},
        {"storage_ref": "safe/../escape.jpg"},
        {"media_type": "   "},
        {"byte_size": -1},
        {"sha256_hex": "A" * 64},
        {"status": FileAssetStatus.ACTIVE.value},
        {
            "status": FileAssetStatus.ACTIVE.value,
            "storage_backend": FileStorageBackend.LEGACY_LOCAL.value,
            "media_type": "image/jpeg",
            "byte_size": 1,
            "sha256_hex": "a" * 64,
        },
        {"status": FileAssetStatus.DELETED.value},
        {"deleted_at": NOW},
        {"status": FileAssetStatus.PURGED.value, "deleted_at": NOW},
        {
            "status": FileAssetStatus.PURGED.value,
            "deleted_at": NOW,
            "purged_at": NOW - timedelta(seconds=1),
        },
        {
            "status": FileAssetStatus.LEGACY_PLACEHOLDER.value,
            "storage_backend": FileStorageBackend.PRIVATE_LOCAL.value,
        },
    ],
    ids=[
        "purpose",
        "backend",
        "status",
        "blank-storage-ref",
        "absolute-storage-ref",
        "traversal-storage-ref",
        "blank-media-type",
        "negative-size",
        "sha-shape",
        "active-missing-metadata",
        "active-legacy-storage",
        "deleted-without-time",
        "time-without-deletion",
        "purged-without-purge-time",
        "purge-before-delete",
        "legacy-private-storage",
    ],
)
async def test_file_asset_checks_reject_invalid_rows(db_session, changes):
    _, subject = await _identity_graph(db_session, "invalid-file")
    db_session.add(_asset(subject.id, **changes))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_file_asset_valid_lifecycle_rows_and_nullable_historical_uploader(
    db_session,
):
    _, subject = await _identity_graph(db_session, "valid-files")
    legacy = _asset(
        subject.id,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        status=FileAssetStatus.LEGACY_PLACEHOLDER.value,
    )
    active = _asset(
        subject.id,
        status=FileAssetStatus.ACTIVE.value,
        media_type="image/jpeg",
        byte_size=0,
        sha256_hex="a" * 64,
    )
    deleted = _asset(
        subject.id,
        status=FileAssetStatus.DELETED.value,
        deleted_at=NOW,
    )
    purged = _asset(
        subject.id,
        status=FileAssetStatus.PURGED.value,
        deleted_at=NOW,
        purged_at=NOW + timedelta(seconds=1),
    )
    db_session.add_all([legacy, active, deleted, purged])
    await db_session.flush()

    assert legacy.uploaded_by_user_id is None
    assert legacy.media_type is None
    assert legacy.byte_size is None
    assert legacy.sha256_hex is None
    assert all(isinstance(row.opaque_key, uuid.UUID) for row in (legacy, active, deleted, purged))


@pytest.mark.parametrize("duplicate", ["opaque-key", "storage-ref"])
async def test_file_asset_storage_identities_are_globally_unique(
    db_session, duplicate
):
    _, first_subject = await _identity_graph(db_session, f"file-first-{duplicate}")
    _, second_subject = await _identity_graph(db_session, f"file-second-{duplicate}")
    opaque_key = uuid.uuid4()
    first = _asset(
        first_subject.id,
        opaque_key=opaque_key,
        storage_ref="synthetic/shared-file.jpg",
    )
    second_changes = (
        {"opaque_key": opaque_key}
        if duplicate == "opaque-key"
        else {"storage_ref": "synthetic/shared-file.jpg"}
    )
    second = _asset(second_subject.id, **second_changes)
    db_session.add_all([first, second])

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def _foundation_state(db_session: Any) -> tuple[Any, ...]:
    db_session.expire_all()
    connections = tuple(
        (
            row.id,
            row.subject_id,
            row.provider,
            row.connection_type,
            row.external_account_discriminator,
            row.credential_ref,
            row.status,
        )
        for row in await db_session.scalars(
            select(IntegrationConnection).order_by(IntegrationConnection.id)
        )
    )
    assets = tuple(
        (
            row.id,
            row.subject_id,
            row.uploaded_by_user_id,
            row.opaque_key,
            row.purpose,
            row.storage_backend,
            row.storage_ref,
            row.status,
        )
        for row in await db_session.scalars(select(FileAsset).order_by(FileAsset.id))
    )
    settings = (
        tuple(
            (row.key, row.value)
            for row in await db_session.scalars(
                select(PlatformSetting).order_by(PlatformSetting.key)
            )
        ),
        tuple(
            (row.user_id, row.key, row.value)
            for row in await db_session.scalars(
                select(UserSetting).order_by(UserSetting.user_id, UserSetting.key)
            )
        ),
        tuple(
            (row.subject_id, row.key, row.value)
            for row in await db_session.scalars(
                select(SubjectSetting).order_by(
                    SubjectSetting.subject_id, SubjectSetting.key
                )
            )
        ),
        tuple(
            (row.integration_connection_id, row.key, row.value)
            for row in await db_session.scalars(
                select(IntegrationConnectionSetting).order_by(
                    IntegrationConnectionSetting.integration_connection_id,
                    IntegrationConnectionSetting.key,
                )
            )
        ),
    )
    return connections, assets, settings


async def test_legacy_portability_excludes_and_cannot_mutate_foundation_rows(
    db_session,
):
    user, subject = await _identity_graph(db_session, "portable-foundation")
    connection = _connection(
        subject.id,
        credential_ref="secret_store:v1:must-not-export",
    )
    asset = _asset(
        subject.id,
        uploaded_by_user_id=user.id,
        # Keep this generic control-plane exclusion fixture outside the
        # Stage-3H progress-photo graph.  A live progress-purpose asset without
        # a matching photo is intentionally rejected as an orphan.
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        storage_ref="synthetic/must-not-export.jpg",
    )
    db_session.add_all([connection, asset])
    await db_session.flush()
    db_session.add_all(
        [
            PlatformSetting(key="platform", value={"enabled": True}),
            UserSetting(user_id=user.id, key="ui", value="compact"),
            SubjectSetting(subject_id=subject.id, key="modules", value=["core"]),
            IntegrationConnectionSetting(
                integration_connection_id=connection.id,
                key="sync",
                value={"hours": 6},
            ),
        ]
    )
    await db_session.flush()

    expected_exclusions = {
        table_name
        for table_name, spec in OWNERSHIP_REGISTRY.items()
        if not spec.user_portable
    }
    assert _EXCLUDED_TABLES == expected_exclusions
    assert FOUNDATION_TABLES <= _EXCLUDED_TABLES

    before = await _foundation_state(db_session)
    snapshot = await export_full(db_session)
    assert FOUNDATION_TABLES.isdisjoint(snapshot)
    rendered = json.dumps(snapshot, ensure_ascii=False, default=str)
    assert "secret_store:v1:must-not-export" not in rendered
    assert "synthetic/must-not-export.jpg" not in rendered

    forged = {
        "metadata": {"version": "1.0", "kind": "full_backup"},
        "app_settings": [],
        **{
            table_name: [{"forged": "must-be-ignored"}]
            for table_name in FOUNDATION_TABLES
        },
    }
    stats = await import_full(db_session, forged)
    await db_session.flush()

    assert await _foundation_state(db_session) == before
    assert FOUNDATION_TABLES.isdisjoint(stats.counts)


@pytest.mark.integration
async def test_postgres_enforces_foundation_fk_delete_actions(db_session):
    """SQLite fast tests inspect FKs; PostgreSQL proves RESTRICT/CASCADE behavior."""

    owner, restricted_subject = await _identity_graph(db_session, "pg-restricted")
    standalone_user = User(
        username="pg-user-setting",
        normalized_username="pg-user-setting",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(standalone_user)
    await db_session.flush()
    _, cascading_subject = await _identity_graph(db_session, "pg-subject-setting")

    connection = _connection(restricted_subject.id)
    asset = _asset(
        restricted_subject.id,
        uploaded_by_user_id=owner.id,
    )
    db_session.add_all([connection, asset])
    await db_session.flush()
    db_session.add_all(
        [
            IntegrationConnectionSetting(
                integration_connection_id=connection.id, key="sync", value=True
            ),
            UserSetting(user_id=standalone_user.id, key="ui", value=True),
            SubjectSetting(
                subject_id=cascading_subject.id, key="modules", value=True
            ),
        ]
    )
    await db_session.flush()

    await db_session.delete(connection)
    await db_session.delete(standalone_user)
    await db_session.delete(cascading_subject)
    await db_session.flush()
    assert await db_session.scalar(select(IntegrationConnectionSetting)) is None
    assert await db_session.scalar(select(UserSetting)) is None
    assert await db_session.scalar(select(SubjectSetting)) is None

    await db_session.delete(restricted_subject)
    with pytest.raises(IntegrityError):
        await db_session.flush()
