"""Exporters for the legacy portability-v1 full and personal archives."""

from __future__ import annotations

import os
from types import MappingProxyType
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.base import Base
from vitals.services.portability.v1_contract import (
    BACKUP_VERSION,
    GENERIC_OUTPUT_SUPPRESSED_COLUMNS,
    KIND_FULL,
    _EXCLUDED_TABLES,
    _SUBJECT_BOUND_MARKER,
    _contract_error,
    _is_secret_setting_key,
    _live_schema_columns,
    _refuse_unrestorable_v1_rows,
    _serialize_value,
    _single_local_subject_id,
)
from vitals.utils.timeutils import now_local

async def export_full(session: AsyncSession) -> dict[str, Any]:
    """Snapshot portable tables into ``{table_name: [rows]}`` plus metadata.

    Tables are walked in FK order (``sorted_tables``); ``app_settings`` secret-ish
    rows and ownership/private-resource plumbing are dropped. The result is a
    plain dict ready for ``json.dumps``.
    """
    local_subject_id = await _single_local_subject_id(session)
    live_schema = await _live_schema_columns(session)
    out: dict[str, Any] = {
        "metadata": {
            "version": BACKUP_VERSION,
            "kind": KIND_FULL,
            "exported_at": now_local().isoformat(timespec="seconds"),
            "timezone": os.getenv("VITALS_TIMEZONE", "Europe/Chisinau"),
        }
    }

    for table in Base.metadata.sorted_tables:
        if table.name in _EXCLUDED_TABLES or table.name not in live_schema:
            continue
        installed_columns = live_schema[table.name]
        selected_columns = tuple(
            column for column in table.columns if column.name in installed_columns
        )
        result = await session.execute(select(*selected_columns))
        column_names = [
            name
            for name in table.columns.keys()
            if name in installed_columns
            if name not in GENERIC_OUTPUT_SUPPRESSED_COLUMNS
        ]
        rows: list[dict[str, Any]] = []
        for mapping in result.mappings().all():
            if table.name == "app_settings" and _is_secret_setting_key(mapping.get("key")):
                continue
            row = {col: _serialize_value(mapping[col]) for col in column_names}
            if "subject_id" in installed_columns:
                subject_bound = mapping["subject_id"] is not None
                if local_subject_id is None and subject_bound:
                    raise _contract_error("portability.error.v1_missing_subject")
                row[_SUBJECT_BOUND_MARKER] = subject_bound
            rows.append(row)
        out[table.name] = rows

    return out


#: What a subject export is, so the whole-database importer can tell them apart.
#: Feeding one to the full-v1 restore coordinator would wipe every table for
#: everybody and then
#: restore one person into the hole — a plausible mistake with an implausible
#: blast radius, so the two shapes are named rather than merely shaped.
KIND_SUBJECT = "subject_export"


#: Where one exported row points at another. Derived from the schema rather than
#: listed, for the same reason the table walk is: a reference added later must
#: not silently become an id from one installation pasted into another.
#:
#: ``subject_id`` legs of composite foreign keys are excluded — the subject is
#: assigned by the boundary on import, never carried, so it is not a reference
#: that needs resolving.
def _portable_references() -> Mapping[str, Mapping[str, str]]:
    references: dict[str, dict[str, str]] = {}
    for table in Base.metadata.sorted_tables:
        if table.name in _EXCLUDED_TABLES or "subject_id" not in table.columns:
            continue
        for foreign_key in table.foreign_keys:
            column = foreign_key.parent.name
            target = foreign_key.column.table.name
            if column == "subject_id" or target == table.name:
                continue
            if target in _EXCLUDED_TABLES or "subject_id" not in (
                Base.metadata.tables[target].columns
            ):
                continue
            references.setdefault(table.name, {})[column] = target
    return MappingProxyType(
        {name: MappingProxyType(columns) for name, columns in references.items()}
    )


PORTABLE_REFERENCES = _portable_references()

#: A reference can point at a row a personal export does not carry: the
#: installation's shared catalog, which lives under a NULL subject and is seeded
#: by the receiver's own migrations. An id would be meaningless there — two
#: installations number their catalogs independently — so those references
#: travel as the target's natural key and are resolved on arrival.
CATALOG_NATURAL_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "hrt_compounds": "key",
    }
)

#: Reserved row key carrying the natural keys above. Same reservation rule as
#: the subject marker: accepted only in the shape this module writes.
_REFERENCE_MARKER = "_vitals_refs"


async def export_subject(
    session: AsyncSession, *, subject_id: Any
) -> dict[str, Any]:
    """Snapshot exactly one subject's portable rows, and nothing else.

    Different question from :func:`export_full`, which answers "what is in this
    installation". This one answers "what is mine", and the difference is not
    only a ``WHERE`` clause:

    ``app_settings`` is left out entirely. It is the installation's
    configuration, not a person's — the timezone the scheduler runs on, the
    modules that are switched on for the deployment. Carrying it in a personal
    export would make the file a way to reconfigure whatever imports it.

    Rows with a NULL subject are left out for the same reason. In the mixed
    tables those are the installation's curated catalog — the safety rules, the
    compound reference data — which the receiving installation has its own copy
    of, seeded by its own migrations. Including them would let a personal export
    overwrite somebody's safety catalog.

    What remains is the subject's own rows, with ownership and private-resource
    columns suppressed exactly as the full backup suppresses them: those are
    assigned by a trusted boundary on the way back in, never read from a file.
    """

    if subject_id is None:
        raise _contract_error("portability.error.v1_missing_subject")

    out: dict[str, Any] = {
        "metadata": {
            "version": BACKUP_VERSION,
            "kind": KIND_SUBJECT,
            "exported_at": now_local().isoformat(timespec="seconds"),
            "timezone": os.getenv("VITALS_TIMEZONE", "Europe/Chisinau"),
        }
    }

    carried: dict[str, set[Any]] = {}
    for table in Base.metadata.sorted_tables:
        if table.name in _EXCLUDED_TABLES:
            continue
        if "subject_id" not in table.columns:
            # Installation configuration, not this person's record.
            continue
        result = await session.execute(
            select(table).where(table.c.subject_id == subject_id)
        )
        column_names = [
            name
            for name in table.columns.keys()
            if name not in GENERIC_OUTPUT_SUPPRESSED_COLUMNS
        ]
        mappings = result.mappings().all()
        _refuse_unrestorable_v1_rows(table, mappings)
        carried[table.name] = {mapping["id"] for mapping in mappings}
        out[table.name] = [
            {col: _serialize_value(mapping[col]) for col in column_names}
            for mapping in mappings
        ]

    await _describe_outbound_references(session, out, carried)
    return out


async def _describe_outbound_references(
    session: AsyncSession,
    snapshot: dict[str, Any],
    carried: dict[str, set[Any]],
) -> None:
    """Give every reference that leaves the file a name it can be found by.

    Inside the file an id is fine: the importer renumbers both ends together.
    A reference *out* of it is a different thing — it points at the
    installation's shared catalog, which the receiving installation seeded for
    itself and numbered its own way. Carrying the integer would either dangle or,
    worse, land on an unrelated row that happens to hold that number.

    So those travel as the target's natural key. A reference that is neither in
    the file nor resolvable to a natural key is refused here rather than written
    out to fail on arrival, where the person holding the file can do nothing
    about it.
    """

    for table_name, columns in PORTABLE_REFERENCES.items():
        rows = snapshot.get(table_name)
        if not rows:
            continue
        for row in rows:
            descriptors: dict[str, Any] = {}
            for column, target in columns.items():
                value = row.get(column)
                if value is None or value in carried.get(target, ()):
                    continue
                natural_key = CATALOG_NATURAL_KEYS.get(target)
                if natural_key is None:
                    raise _contract_error(
                        "portability.error.v1_unportable_reference",
                        table=table_name,
                        column=column,
                    )
                target_table = Base.metadata.tables[target]
                key_value = await session.scalar(
                    select(target_table.c[natural_key]).where(
                        target_table.c.id == value
                    )
                )
                if key_value is None:
                    raise _contract_error(
                        "portability.error.v1_unportable_reference",
                        table=table_name,
                        column=column,
                    )
                descriptors[column] = {"table": target, "key": key_value}
                # The id is local to this installation and means nothing
                # elsewhere; the descriptor replaces it rather than joining it.
                row[column] = None
            if descriptors:
                row[_REFERENCE_MARKER] = descriptors


# ── Full backup: import (replace) ──────────────────────────────────────────────
