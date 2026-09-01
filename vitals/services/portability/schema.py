"""Reviewed, machine-readable database contract for portability format v2.

The graph exporter and importer must agree on more than table names.  This
module freezes the ownership boundary, column codecs, reference kinds, rebuilt
fields, and dependency order into one canonical descriptor.  Its digest is a
ratchet: model or ownership drift fails closed until the new contract is
explicitly reviewed and the pinned digest is updated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import MetaData, Table
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import sqltypes

from vitals.enums import FileAssetPurpose
from vitals.models import Base
from vitals.ownership import (
    OWNERSHIP_REGISTRY,
    OwnershipClass,
    OwnershipSpec,
    TargetColumn,
)


SCHEMA_FORMAT: Final = "vitals-portability-schema"
SCHEMA_VERSION: Final = 1
EXPLICIT_EXCLUDED_TABLES: Final = frozenset(
    {"garmin_weight_exports", "system_alerts"}
)
SUBJECT_GRAPH_CLASSES: Final = frozenset(
    {
        OwnershipClass.SUBJECT_DATA,
        OwnershipClass.SUBJECT_CHILD,
        OwnershipClass.MIXED_CATALOG,
        OwnershipClass.MIXED_CATALOG_CHILD,
    }
)

_OWNERSHIP_FIELDS: Final = (
    "subject",
    "actor",
    "connection",
    "platform_connection",
    "file_asset",
)
_REBUILT_FILE_KEYS: Final = {
    ("body_scans", "file_key"): "resource_storage_ref_or_null",
    ("progress_photos", "file_key"): "resource_storage_ref",
}
_CONDITIONAL_REBUILDS: Final = {
    ("raw_payloads", "external_id"): "resource_storage_ref_when_file_link",
}
_RESOURCE_PURPOSE_RULES: Final = {
    "body_scans": {
        "mode": "fixed",
        "purpose": FileAssetPurpose.BODY_SCAN_DOCUMENT.value,
    },
    "progress_photos": {
        "mode": "fixed",
        "purpose": FileAssetPurpose.PROGRESS_PHOTO.value,
    },
    "raw_payloads": {
        "column": "domain",
        "mapping": {
            "body_comp": FileAssetPurpose.BODY_SCAN_DOCUMENT.value,
            "labs": FileAssetPurpose.LAB_DOCUMENT.value,
        },
        "mode": "value_map",
    },
}


class SchemaContractError(RuntimeError):
    """A model/registry shape cannot be represented by the reviewed contract."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def _error(code: str, detail: str) -> SchemaContractError:
    return SchemaContractError(code, detail)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _error("schema_not_canonical", "schema descriptor is not canonical JSON") from exc


def schema_digest(descriptor: Mapping[str, Any]) -> str:
    """Return the lowercase SHA-256 of the canonical schema descriptor."""

    return hashlib.sha256(_canonical_json(descriptor)).hexdigest()


def _type_descriptor(column_type: object) -> dict[str, Any]:
    """Describe one SQLAlchemy type without dialect compilation or repr fallback."""

    type_class = type(column_type)
    if type_class is JSONB:
        return {"codec": "json", "sqlalchemy_type": "JSONB"}
    if type_class is sqltypes.JSON:
        return {"codec": "json", "sqlalchemy_type": "JSON"}
    if type_class is sqltypes.Boolean:
        return {
            "codec": "boolean",
            "create_constraint": bool(column_type.create_constraint),
            "native": bool(column_type.native),
            "sqlalchemy_type": "Boolean",
        }
    if type_class is sqltypes.Integer:
        return {"bits": 32, "codec": "integer", "sqlalchemy_type": "Integer"}
    if type_class is sqltypes.Float:
        return {
            "asdecimal": bool(column_type.asdecimal),
            "codec": "decimal_string" if column_type.asdecimal else "float",
            "precision": column_type.precision,
            "sqlalchemy_type": "Float",
        }
    if type_class is sqltypes.Text:
        return {"codec": "string", "sqlalchemy_type": "Text"}
    if type_class is sqltypes.String:
        length = column_type.length
        if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
            raise _error("string_length_invalid", "String requires a positive fixed limit")
        return {
            "codec": "string",
            "length": length,
            "sqlalchemy_type": "String",
        }
    if type_class is sqltypes.DateTime:
        return {
            "codec": "datetime_iso8601",
            "sqlalchemy_type": "DateTime",
            "timezone": bool(column_type.timezone),
        }
    if type_class is sqltypes.Date:
        return {"codec": "date_iso8601", "sqlalchemy_type": "Date"}
    if type_class is sqltypes.Time:
        return {
            "codec": "time_iso8601",
            "sqlalchemy_type": "Time",
            "timezone": bool(column_type.timezone),
        }
    if type_class is sqltypes.Uuid:
        return {
            "codec": "uuid",
            "native": bool(column_type.native_uuid),
            "sqlalchemy_type": "Uuid",
        }
    raise _error(
        "unsupported_column_type",
        f"unsupported SQLAlchemy type on reviewed column: {type_class.__module__}.{type_class.__name__}",
    )


def _column_descriptor(column: object) -> dict[str, Any]:
    return {
        "name": column.name,
        "nullable": bool(column.nullable),
        "type": _type_descriptor(column.type),
    }


def _ownership_descriptor(spec: OwnershipSpec) -> dict[str, Any]:
    return {
        "class": spec.ownership.value,
        **{field: getattr(spec, field).value for field in _OWNERSHIP_FIELDS},
        "user_portable": spec.user_portable,
    }


def expected_resource_purpose(
    table_name: str,
    values: Mapping[str, Any],
) -> str:
    """Resolve the reviewed logical purpose for one file-backed portable row."""

    rule = _RESOURCE_PURPOSE_RULES.get(table_name)
    if rule is None:
        raise _error(
            "resource_owner_unknown",
            f"table {table_name!r} has no reviewed resource purpose rule",
        )
    if rule["mode"] == "fixed":
        return rule["purpose"]
    discriminator = values.get(rule["column"])
    purpose = rule["mapping"].get(discriminator)
    if purpose is None:
        raise _error(
            "resource_purpose_unknown",
            f"table {table_name!r} has no purpose for its discriminator",
        )
    return purpose


def _is_portable(table_name: str, spec: OwnershipSpec) -> bool:
    return (
        spec.user_portable
        and spec.ownership in SUBJECT_GRAPH_CLASSES
        and table_name not in EXPLICIT_EXCLUDED_TABLES
    )


def _foreign_key_targets(
    table: Table, *, portable_names: frozenset[str]
) -> dict[str, tuple[str, str]]:
    grouped: dict[str, set[tuple[str, str]]] = {}
    for foreign_key in table.foreign_keys:
        grouped.setdefault(foreign_key.parent.name, set()).add(
            (foreign_key.column.table.name, foreign_key.column.name)
        )

    resolved: dict[str, tuple[str, str]] = {}
    for local_name, targets in sorted(grouped.items()):
        if len(targets) == 1:
            resolved[local_name] = next(iter(targets))
            continue
        if local_name == "subject_id" and all(
            (target_table == "health_subjects" and target_column == "id")
            or (target_table in portable_names and target_column == "subject_id")
            for target_table, target_column in targets
        ):
            continue
        raise _error(
            "ambiguous_foreign_key",
            f"table {table.name!r} column {local_name!r} has ambiguous FK targets",
        )
    return resolved


def _validate_ownership_columns(table: Table, spec: OwnershipSpec) -> None:
    fields = {
        "subject": "subject_id",
        "actor": "actor_user_id",
        "connection": "integration_connection_id",
        "file_asset": "file_asset_id",
    }
    for field, column_name in fields.items():
        target = getattr(spec, field)
        if target is not TargetColumn.NONE and column_name not in table.c:
            raise _error(
                "ownership_column_missing",
                f"table {table.name!r} declares {field}={target.value} without {column_name!r}",
            )
    if spec.platform_connection is not TargetColumn.NONE:
        raise _error(
            "portable_platform_connection_unsupported",
            f"portable table {table.name!r} has a platform connection",
        )


def _link_descriptor(
    column: object,
    *,
    kind: str,
    ref_kind: str,
    target_table: str,
    target_column: str,
    required: bool,
    ownership_target: TargetColumn | None = None,
) -> dict[str, Any]:
    result = {
        **_column_descriptor(column),
        "kind": kind,
        "ref_kind": ref_kind,
        "required": required,
        "target_column": target_column,
        "target_table": target_table,
    }
    if ownership_target is not None:
        result["ownership_target"] = ownership_target.value
    return result


def _table_descriptor(
    table: Table,
    spec: OwnershipSpec,
    *,
    portable_names: frozenset[str],
) -> tuple[dict[str, Any], set[str]]:
    _validate_ownership_columns(table, spec)
    primary_columns = list(table.primary_key.columns)
    if len(primary_columns) != 1:
        raise _error(
            "composite_primary_key_unsupported",
            f"portable table {table.name!r} must have exactly one primary key column",
        )
    primary = primary_columns[0]
    targets = _foreign_key_targets(table, portable_names=portable_names)
    values: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    rebuilt: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    dependencies: set[str] = set()

    for column in table.columns:
        descriptor = _column_descriptor(column)
        name = column.name
        if name == primary.name:
            continue
        if name == "subject_id":
            rebuilt.append(
                {
                    **descriptor,
                    "mode": "target_subject",
                    "ownership_target": spec.subject.value,
                }
            )
            continue
        if name == "actor_user_id":
            suppressed.append(
                {
                    **descriptor,
                    "mode": "not_personally_portable_identity",
                    "ownership_target": spec.actor.value,
                }
            )
            continue
        if name == "integration_connection_id":
            target = spec.connection
            if target is TargetColumn.NONE:
                raise _error(
                    "unclassified_connection_column",
                    f"table {table.name!r} has an unclassified connection column",
                )
            links.append(
                _link_descriptor(
                    column,
                    kind="connection",
                    ref_kind="c",
                    target_table="integration_connections",
                    target_column="id",
                    required=target is TargetColumn.REQUIRED,
                    ownership_target=target,
                )
            )
            continue
        if name == "file_asset_id":
            target = spec.file_asset
            if target is TargetColumn.NONE:
                raise _error(
                    "unclassified_file_column",
                    f"table {table.name!r} has an unclassified file asset column",
                )
            purpose_rule = _RESOURCE_PURPOSE_RULES.get(table.name)
            if purpose_rule is None:
                raise _error(
                    "resource_owner_unknown",
                    f"table {table.name!r} has no reviewed resource purpose rule",
                )
            link = _link_descriptor(
                    column,
                    kind="resource",
                    ref_kind="f",
                    target_table="file_assets",
                    target_column="id",
                    required=target is TargetColumn.REQUIRED,
                    ownership_target=target,
                )
            link["purpose_rule"] = json.loads(json.dumps(purpose_rule))
            links.append(link)
            continue
        rebuilt_mode = _REBUILT_FILE_KEYS.get((table.name, name))
        if rebuilt_mode is not None:
            rebuilt.append({**descriptor, "mode": rebuilt_mode})
            continue

        foreign_target = targets.get(name)
        if foreign_target is not None:
            target_table, target_column = foreign_target
            if target_table not in portable_names:
                raise _error(
                    "unclassified_nonportable_foreign_key",
                    f"table {table.name!r} column {name!r} targets nonportable table {target_table!r}",
                )
            target = table.metadata.tables[target_table]
            if target_column not in {item.name for item in target.primary_key.columns}:
                raise _error(
                    "non_primary_foreign_key",
                    f"table {table.name!r} column {name!r} targets a non-primary column",
                )
            dependencies.add(target_table)
            links.append(
                _link_descriptor(
                    column,
                    kind="row",
                    ref_kind="r",
                    target_table=target_table,
                    target_column=target_column,
                    required=not column.nullable,
                )
            )
            continue

        value = descriptor
        conditional = _CONDITIONAL_REBUILDS.get((table.name, name))
        if conditional is not None:
            value = {**value, "conditional_rebuild": conditional}
            rebuilt.append({**descriptor, "mode": conditional})
        values.append(value)

    classified = {
        primary.name,
        *(entry["name"] for entry in values),
        *(entry["name"] for entry in links),
        *(entry["name"] for entry in rebuilt),
        *(entry["name"] for entry in suppressed),
    }
    if classified != set(table.c.keys()):
        raise _error(
            "column_classification_incomplete",
            f"portable table {table.name!r} has unclassified columns",
        )
    return (
        {
            "links": sorted(links, key=lambda item: item["name"]),
            "name": table.name,
            "ownership": _ownership_descriptor(spec),
            "primary_key": {
                "columns": [_column_descriptor(primary)],
                "strategy": "regenerated",
            },
            "rebuilt_fields": sorted(rebuilt, key=lambda item: item["name"]),
            "suppressed_fields": sorted(suppressed, key=lambda item: item["name"]),
            "value_columns": sorted(values, key=lambda item: item["name"]),
        },
        dependencies,
    )


def _dependency_order(
    dependencies: Mapping[str, set[str]],
) -> tuple[list[str], list[str]]:
    remaining = {name: set(targets) for name, targets in dependencies.items()}
    insert_order: list[str] = []
    while remaining:
        ready = sorted(name for name, targets in remaining.items() if not targets)
        if not ready:
            raise _error("portable_dependency_cycle", "portable table graph is cyclic")
        insert_order.extend(ready)
        for name in ready:
            del remaining[name]
        for targets in remaining.values():
            targets.difference_update(ready)
    return insert_order, list(reversed(insert_order))


def build_schema_descriptor(
    *,
    metadata: MetaData = Base.metadata,
    registry: Mapping[str, OwnershipSpec] = OWNERSHIP_REGISTRY,
) -> dict[str, Any]:
    """Build the complete reviewed descriptor from metadata and ownership policy."""

    metadata_names = set(metadata.tables)
    registry_names = set(registry)
    if metadata_names != registry_names:
        raise _error(
            "registry_incomplete",
            "ownership registry and SQLAlchemy metadata contain different tables",
        )
    for name, spec in registry.items():
        if not isinstance(name, str) or not isinstance(spec, OwnershipSpec):
            raise _error("registry_invalid", "ownership registry has an invalid entry")

    portable_names = frozenset(
        name for name, spec in registry.items() if _is_portable(name, spec)
    )
    inventory: list[dict[str, Any]] = []
    for name, spec in sorted(registry.items()):
        reasons: list[str] = []
        if not spec.user_portable:
            reasons.append("registry_not_user_portable")
        if spec.ownership not in SUBJECT_GRAPH_CLASSES:
            reasons.append("ownership_class_not_personal_data")
        if name in EXPLICIT_EXCLUDED_TABLES:
            reasons.append("derived_control_or_outbox")
        inventory.append(
            {
                "disposition": "portable" if name in portable_names else "excluded",
                "name": name,
                "ownership": _ownership_descriptor(spec),
                "reasons": reasons,
            }
        )

    tables: list[dict[str, Any]] = []
    dependencies: dict[str, set[str]] = {}
    for name in sorted(portable_names):
        table = metadata.tables[name]
        if "subject_id" not in table.c:
            raise _error(
                "portable_table_unscoped",
                f"portable table {name!r} has no subject_id column",
            )
        descriptor, table_dependencies = _table_descriptor(
            table,
            registry[name],
            portable_names=portable_names,
        )
        tables.append(descriptor)
        dependencies[name] = table_dependencies

    insert_order, delete_order = _dependency_order(dependencies)
    return {
        "delete_order": delete_order,
        "format": SCHEMA_FORMAT,
        "insert_order": insert_order,
        "table_inventory": inventory,
        "tables": tables,
        "version": SCHEMA_VERSION,
    }


def portable_tables(
    *,
    metadata: MetaData = Base.metadata,
    registry: Mapping[str, OwnershipSpec] = OWNERSHIP_REGISTRY,
) -> tuple[tuple[Table, OwnershipSpec], ...]:
    """Return the portable tables after validating the complete schema contract."""

    descriptor = build_schema_descriptor(metadata=metadata, registry=registry)
    names = [table["name"] for table in descriptor["tables"]]
    return tuple((metadata.tables[name], registry[name]) for name in names)


def validate_schema_contract(
    *,
    metadata: MetaData = Base.metadata,
    registry: Mapping[str, OwnershipSpec] = OWNERSHIP_REGISTRY,
    expected_digest: str,
) -> dict[str, Any]:
    """Build and match a descriptor against an explicitly reviewed digest."""

    if (
        type(expected_digest) is not str
        or len(expected_digest) != 64
        or expected_digest != expected_digest.lower()
        or any(character not in "0123456789abcdef" for character in expected_digest)
    ):
        raise _error("schema_digest_invalid", "expected schema digest is not lowercase SHA-256")
    descriptor = build_schema_descriptor(metadata=metadata, registry=registry)
    if schema_digest(descriptor) != expected_digest:
        raise _error("schema_digest_mismatch", "database schema is not the reviewed v2 contract")
    return descriptor


# Filled with the digest of the descriptor above.  Updating this value requires
# a conscious portability-format review, not merely a model migration.
REVIEWED_SCHEMA_DIGEST: Final = (
    "5883cdbd70c367bc93032b72b50bf25a5802d4bd56f38766363e8cc253e75506"
)
PORTABILITY_SCHEMA_DESCRIPTOR: Final = build_schema_descriptor()
PORTABILITY_SCHEMA_DIGEST: Final = schema_digest(PORTABILITY_SCHEMA_DESCRIPTOR)
if PORTABILITY_SCHEMA_DIGEST != REVIEWED_SCHEMA_DIGEST:
    raise _error("schema_digest_mismatch", "database schema is not the reviewed v2 contract")


__all__ = [
    "EXPLICIT_EXCLUDED_TABLES",
    "PORTABILITY_SCHEMA_DESCRIPTOR",
    "PORTABILITY_SCHEMA_DIGEST",
    "REVIEWED_SCHEMA_DIGEST",
    "SCHEMA_FORMAT",
    "SCHEMA_VERSION",
    "SUBJECT_GRAPH_CLASSES",
    "SchemaContractError",
    "build_schema_descriptor",
    "expected_resource_purpose",
    "portable_tables",
    "schema_digest",
    "validate_schema_contract",
]
