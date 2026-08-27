"""HRT cycle templates — save a cycle's plan as a reusable, shareable recipe.

A template is a **date-free, relative** snapshot of a cycle: kind + one row per
compound holding ``start_offset_days`` and the segment ``schedule``, nothing
anchored to a calendar. Three flows:

  * **Save** — :func:`save_cycle_as_template` snapshots an existing cycle's
    items verbatim.
  * **Apply** — :func:`create_cycle_from_template` materializes a template into
    a real cycle at a chosen start date (delegating to ``cycles`` so
    auto-close / catalog resolution behave exactly like a hand-built cycle).
  * **Share** — :func:`export_template` / :func:`import_template` round-trip a
    template through portable JSON. Portable because items reference compounds
    by the shared catalog slug (``hrt_compounds.yaml`` — identical on every
    instance); import re-validates everything (keys against the local catalog,
    schedules via ``validate_schedule``) since pasted JSON bypasses the form.

Harm-reduction stance: a template is structure the *user* authored — the app
never ships built-in dose protocols and never recommends one.
"""
from __future__ import annotations

import json
import uuid
from datetime import date as date_type
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import CycleKind, DoseUnit, Source
from vitals.models.hrt import DOMAIN, HrtCycle, HrtCycleTemplate, HrtCycleTemplateItem
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.hrt import cycles

# Portable-JSON envelope. Bump the version if the item shape ever changes so an
# importer can tell an old payload from a malformed one.
EXPORT_FORMAT = "vitals.hrt_cycle_template"
EXPORT_VERSION = 1

_VALID_KINDS = {k.value for k in CycleKind}
_VALID_UNITS = {u.value for u in DoseUnit}
_MAX_ITEMS = 50  # no real protocol stacks this many compounds


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: engine.PreparedConflictWrite,
) -> engine.ConflictWriteContext:
    """Prove the session/transaction/identity before any target lookup.

    Templates are date-free, so the prepared context's evaluation date is a
    governance serialization token rather than a semantic template field.
    """

    context = engine.require_prepared_identity(
        session,
        prepared=prepared,
        identity=identity,
    )
    return context


def _subject_scope(model, subject_id: uuid.UUID):
    """An HRT template belongs to the person it was written for."""

    return model.subject_id == subject_id


def _row_in_scope(
    row,
    *,
    subject_id: uuid.UUID,
) -> bool:
    return row.subject_id == subject_id


def _validate_template_graph(
    template: HrtCycleTemplate,
    items: Sequence[HrtCycleTemplateItem],
    *,
    subject_id: uuid.UUID,
) -> None:
    if subject_id is None:
        return
    if not _row_in_scope(
        template,
        subject_id=subject_id,
    ):
        raise engine.ConflictScopeError(
            "HRT template is outside the requested subject scope"
        )
    for item in items:
        if not _row_in_scope(
            item,
            subject_id=subject_id,
        ):
            raise engine.ConflictScopeError(
                "HRT template contains an item outside the requested subject scope"
            )


async def _lock_template_graph(
    session: AsyncSession,
    template_id: int,
    *,
    subject_id: uuid.UUID,
) -> tuple[HrtCycleTemplate, list[HrtCycleTemplateItem]] | None:
    stmt = select(HrtCycleTemplate).where(
        HrtCycleTemplate.id == template_id,
        HrtCycleTemplate.domain == DOMAIN,
    )
    stmt = stmt.where(_subject_scope(HrtCycleTemplate, subject_id))
    template = await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )
    if template is None:
        return None
    items = list(
        await session.scalars(
            select(HrtCycleTemplateItem)
            .where(HrtCycleTemplateItem.template_id == template.id)
            .order_by(HrtCycleTemplateItem.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    _validate_template_graph(
        template,
        items,
        subject_id=subject_id,
    )
    return template, items


def _validate_export_scope(
    template: HrtCycleTemplate,
    *,
    subject_id: uuid.UUID,
) -> None:
    _validate_template_graph(
        template,
        template.items,
        subject_id=subject_id,
    )


# ── CRUD ──────────────────────────────────────────────────────────────────────
async def list_templates(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> Sequence[HrtCycleTemplate]:
    stmt = select(HrtCycleTemplate).where(HrtCycleTemplate.domain == DOMAIN)
    stmt = stmt.where(_subject_scope(HrtCycleTemplate, subject_id))
    templates = list(
        await session.scalars(
            stmt.order_by(HrtCycleTemplate.name, HrtCycleTemplate.id)
            .execution_options(populate_existing=True)
        )
    )
    for template in templates:
        _validate_template_graph(
            template,
            template.items,
            subject_id=subject_id,
        )
    return templates


async def get_template(
    session: AsyncSession,
    template_id: int,
    *,
    subject_id: uuid.UUID,
) -> Optional[HrtCycleTemplate]:
    # populate_existing: the instance may sit expired in the identity map after
    # a commit — a lazy .items load on it would MissingGreenlet under asyncio.
    stmt = select(HrtCycleTemplate).where(
        HrtCycleTemplate.id == template_id,
        HrtCycleTemplate.domain == DOMAIN,
    )
    stmt = stmt.where(_subject_scope(HrtCycleTemplate, subject_id))
    template = await session.scalar(stmt.execution_options(populate_existing=True))
    if template is not None:
        _validate_template_graph(
            template,
            template.items,
            subject_id=subject_id,
        )
    return template


async def delete_template(
    session: AsyncSession,
    template_id: int,
    *,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> bool:
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    graph = await _lock_template_graph(
        session,
        template_id,
        subject_id=identity.subject_id if identity is not None else None,
    )
    if graph is None:
        return False
    template, _items = graph
    await session.delete(template)
    await session.flush()
    return True


# ── Save / apply ──────────────────────────────────────────────────────────────
async def save_cycle_as_template(
    session: AsyncSession,
    cycle_id: int,
    *,
    name: str,
    note: Optional[str] = None,
    source: str | Source = Source.MANUAL.value,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[HrtCycleTemplate]:
    """Snapshot a cycle's plan into a new template. The snapshot is by value —
    later edits to the cycle don't touch the template."""
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    graph = await cycles._lock_cycle_graph(
        session,
        cycle_id,
        subject_id=identity.subject_id if identity is not None else None,
    )
    if graph is None:
        return None
    cycle, cycle_items = graph
    name = (name or "").strip()
    if not name:
        raise ValueError("template name is required")
    if not cycle_items:
        raise ValueError("cycle has no compounds to save")
    template = HrtCycleTemplate(
        subject_id=identity.subject_id if identity is not None else None,
        actor_user_id=identity.actor_user_id if identity is not None else None,
        domain=DOMAIN,
        source=cycles._source_value(source),
        name=name,
        kind=cycle.kind,
        note=note,
    )
    session.add(template)
    await session.flush()
    for item in cycle_items:
        session.add(
            HrtCycleTemplateItem(
                subject_id=template.subject_id,
                template=template,
                compound_key=item.compound_key,
                unit=item.unit,
                start_offset_days=item.start_offset_days or 0,
                schedule=item.schedule,
                note=item.note,
            )
        )
    await session.flush()
    locked = await _lock_template_graph(
        session,
        template.id,
        subject_id=identity.subject_id if identity is not None else None,
    )
    assert locked is not None
    return locked[0]


async def create_cycle_from_template(
    session: AsyncSession,
    template_id: int,
    *,
    start_date: date_type,
    name: Optional[str] = None,
    source: str | Source = Source.MANUAL.value,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> Optional[HrtCycle]:
    """Materialize a template into a real cycle starting on ``start_date``.
    Goes through ``cycles`` item-by-item so compound resolution and
    the open-cycle auto-close behave exactly as if built by hand."""
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    graph = await _lock_template_graph(
        session,
        template_id,
        subject_id=identity.subject_id if identity is not None else None,
    )
    if graph is None:
        return None
    template, template_items = graph
    cycle = await cycles.add_cycle(
        session,
        kind=template.kind,
        start_date=start_date,
        name=(name or "").strip() or template.name,
        note=template.note,
        source=source,
        identity=identity,
        prepared_conflict_write=prepared_conflict_write,
    )
    for item in template_items:
        await cycles.add_cycle_item(
            session,
            cycle.id,
            compound_key=item.compound_key,
            schedule=item.schedule,
            unit=item.unit,
            start_offset_days=item.start_offset_days or 0,
            note=item.note,
            identity=identity,
            prepared_conflict_write=prepared_conflict_write,
        )
    await session.flush()
    return cycle


def _signature(kind: str, items: Sequence[HrtCycleTemplateItem]) -> tuple:
    """Content identity of a template — what makes two imports 'the same'."""
    return (
        kind,
        tuple(
            (
                it.compound_key,
                it.unit,
                int(it.start_offset_days or 0),
                json.dumps(it.schedule, sort_keys=True),
            )
            for it in items
        ),
    )


# ── Share: portable JSON ──────────────────────────────────────────────────────
def export_template(
    template: HrtCycleTemplate,
    *,
    subject_id: uuid.UUID,
) -> dict:
    """A template as a portable dict — self-describing envelope, relative items
    only. ``json.dumps(..., ensure_ascii=False, indent=2)`` of this is the
    copy-paste share payload."""
    _validate_export_scope(
        template,
        subject_id=subject_id,
    )
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "name": template.name,
        "kind": template.kind,
        "note": template.note,
        "items": [
            {
                "compound_key": item.compound_key,
                "unit": item.unit,
                "start_offset_days": item.start_offset_days or 0,
                "schedule": item.schedule,
                "note": item.note,
            }
            for item in template.items
        ],
    }


def export_template_json(
    template: HrtCycleTemplate,
    *,
    subject_id: uuid.UUID,
) -> str:
    return json.dumps(
        export_template(
            template,
            subject_id=subject_id,
        ),
        ensure_ascii=False,
        indent=2,
    )


async def import_template(
    session: AsyncSession,
    payload: dict | str,
    *,
    source: str | Source = Source.MANUAL.value,
    identity: WriteIdentity,
    prepared_conflict_write: engine.PreparedConflictWrite,
) -> HrtCycleTemplate:
    """Validate a pasted share payload and save it as a new local template.
    Rejects (with a message naming the problem) rather than half-importing:
    unknown envelope, bad kind/unit/offset, malformed schedule, or a compound
    key missing from the local catalog."""
    _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as e:
            raise ValueError(f"not valid JSON: {e.msg}") from None
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if payload.get("format") != EXPORT_FORMAT:
        raise ValueError(f"unrecognized format — expected '{EXPORT_FORMAT}'")
    try:
        version = int(payload.get("version") or 0)
    except (TypeError, ValueError):
        raise ValueError("version must be a number") from None
    if version < 1 or version > EXPORT_VERSION:
        raise ValueError(f"unsupported version {version} (this app reads up to {EXPORT_VERSION})")

    name = str(payload.get("name") or "").strip()[:128]
    if not name:
        raise ValueError("template name is required")
    kind = str(payload.get("kind") or "").strip()
    if kind not in _VALID_KINDS:
        raise ValueError(f"unknown cycle kind '{kind}'")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items must be a non-empty list")
    if len(raw_items) > _MAX_ITEMS:
        raise ValueError(f"too many items (max {_MAX_ITEMS})")

    clean_items: list[HrtCycleTemplateItem] = []
    missing: list[str] = []
    for idx, raw in enumerate(raw_items):
        where = f"item {idx + 1}"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: must be an object")
        key = str(raw.get("compound_key") or "").strip()
        if not key:
            raise ValueError(f"{where}: compound_key is required")
        compound = await cycles._resolve_scoped_compound(
            session,
            key,
            subject_id=identity.subject_id if identity is not None else None,
        )
        if compound is None:
            missing.append(key)
            continue
        unit = str(raw.get("unit") or compound.dose_unit or DoseUnit.MG.value)
        if unit not in _VALID_UNITS:
            raise ValueError(f"{where}: unknown unit '{unit}'")
        try:
            offset = int(raw.get("start_offset_days") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"{where}: start_offset_days must be an integer") from None
        if offset < 0:
            raise ValueError(f"{where}: start_offset_days must be >= 0")
        try:
            schedule = cycles.validate_schedule(raw.get("schedule"))
        except ValueError as e:
            raise ValueError(f"{where}: {e}") from None
        note = raw.get("note")
        clean_items.append(
            HrtCycleTemplateItem(
                subject_id=identity.subject_id if identity is not None else None,
                compound_key=key,
                unit=unit,
                start_offset_days=offset,
                schedule=schedule,
                note=str(note) if note is not None else None,
            )
        )
    if missing:
        raise ValueError(
            "unknown compound keys (not in this instance's catalog): " + ", ".join(missing)
        )

    # Duplicate handling: pasting the same share code twice is a mistake, not a
    # request for a copy — reject an exact duplicate. A mere name clash with
    # different content gets a numbered name instead of silently shadowing.
    existing_stmt = (
        select(HrtCycleTemplate)
        .where(HrtCycleTemplate.domain == DOMAIN)
    )
    if identity is not None:
        existing_stmt = existing_stmt.where(
            _subject_scope(
                HrtCycleTemplate,
                identity.subject_id,
            )
        )
    existing_roots = list(
        await session.scalars(
            existing_stmt.order_by(HrtCycleTemplate.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    existing: list[HrtCycleTemplate] = []
    for root in existing_roots:
        graph = await _lock_template_graph(
            session,
            root.id,
            subject_id=identity.subject_id if identity is not None else None,
        )
        assert graph is not None
        existing.append(graph[0])
    new_sig = _signature(kind, clean_items)
    for tp in existing:
        if tp.name == name and _signature(tp.kind, tp.items) == new_sig:
            raise ValueError(f"an identical template '{name}' is already imported")
    taken = {tp.name for tp in existing}
    if name in taken:
        base = name[:118]
        n = 2
        while f"{base} ({n})" in taken:
            n += 1
        name = f"{base} ({n})"

    note = payload.get("note")
    template = HrtCycleTemplate(
        subject_id=identity.subject_id if identity is not None else None,
        actor_user_id=identity.actor_user_id if identity is not None else None,
        domain=DOMAIN,
        source=cycles._source_value(source),
        name=name,
        kind=kind,
        note=str(note) if note is not None else None,
    )
    session.add(template)
    await session.flush()
    for item in clean_items:
        item.template = template
        session.add(item)
    await session.flush()
    locked = await _lock_template_graph(
        session,
        template.id,
        subject_id=identity.subject_id if identity is not None else None,
    )
    assert locked is not None
    return locked[0]
