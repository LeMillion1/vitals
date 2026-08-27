"""Scoped MCP note and delete hubs with explicit domain dispatch."""
from __future__ import annotations

from vitals.services.glp1 import queries as glp1_queries
from vitals.services.glp1 import writes as glp1_writes
from vitals.services.nutrition import queries as nutrition_queries
from vitals.services.nutrition import writes as nutrition_writes
from vitals.services.skincare import queries as skincare_queries
from vitals.services.skincare import writes as skincare_writes

import importlib
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


from vitals.services.body_scan.scans import alerts as body_scan_alerts
from vitals.services.body_scan.scans import queries as body_scan_queries
from vitals.services.body_scan.scans import writes as body_scan_writes
from vitals.services.conflicts import engine
from vitals.services.labs import results as lab_results
from vitals.services.weight import measurements as weight_measurements
from vitals.services.weight import queries as weight_queries
from vitals.services.weight import writes as weight_writes
from web.mcp.record_catalog import DELETE_TARGETS, NOTE_MODELS


@dataclass(frozen=True)
class RecordToolDependencies:
    get_session_factory: Callable[[], Any]
    parse_date: Callable[..., Any]
    module_enabled: Callable[[Any, str], Awaitable[bool]]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    conflict_write_context: Callable[..., Awaitable[Any]]
    weight_write: Callable[..., Awaitable[Any]]
    auxiliary_weight_write: Callable[..., Awaitable[Any]]
    legacy_owner: Callable[[Any], Awaitable[Any]]
    serialize_row: Callable[[Any], dict]
    serialize_written: Callable[[Any, Any], Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredNoteTools:
    log_note: Callable[..., Awaitable[dict]]
    get_notes: Callable[..., Awaitable[list[dict]]]


@dataclass(frozen=True)
class RegisteredDeleteTools:
    delete_record: Callable[..., Awaitable[dict]]


def _not_found(domain: str, record_id: int) -> dict:
    return {"error": f"{domain} record {record_id} not found"}


def register_note_tools(
    server: Any,
    deps: RecordToolDependencies,
) -> RegisteredNoteTools:
    """Register the note write/read pair at its frozen surface position."""

    @server.tool()
    async def log_note(
        domain: str,
        record_id: int,
        note: str,
    ) -> dict:
        """Adds or updates the note field on any domain record by its ID.
        Supported domains: weight, nutrition, glp1, skincare, measurement, body_comp, labs.
        WRITE tool — saved immediately."""
        if domain not in NOTE_MODELS:
            return {"error": f"Unknown domain '{domain}'. Use: {', '.join(NOTE_MODELS)}"}

        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            if domain == "weight":
                conflict_context, prepared = await deps.weight_write(session)
                row = await weight_writes.update_weight_note(
                    session,
                    record_id,
                    note=note,
                    identity=conflict_context.identity,
                    prepared_weight_write=prepared,
                )
            elif domain == "measurement":
                conflict_context, prepared = await deps.auxiliary_weight_write(session)
                row = await weight_measurements.update_body_measurement_note(
                    session,
                    record_id,
                    note=note,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            elif domain == "body_comp":
                if not await deps.module_enabled(session, "body_comp"):
                    return {"error": "module 'body_comp' is disabled"}
                conflict_context, prepared = await deps.weight_write(session)
                row = await body_scan_writes.update_scan_note(
                    session,
                    record_id,
                    note=note,
                    identity=conflict_context.identity,
                    prepared_weight_write=prepared,
                )
            elif domain == "nutrition":
                if not await deps.module_enabled(session, "nutrition"):
                    return {"error": "module 'nutrition' is disabled"}
                conflict_context = await deps.conflict_write_context(session)
                prepared = await engine.prepare_scoped_write(
                    session,
                    context=conflict_context,
                )
                row = await nutrition_writes.update_meal_note(
                    session,
                    record_id,
                    note=note,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            elif domain == "skincare":
                if not await deps.module_enabled(session, "skincare"):
                    return {"error": "module 'skincare' is disabled"}
                conflict_context = await deps.conflict_write_context(session)
                prepared = await engine.prepare_scoped_write(
                    session,
                    context=conflict_context,
                )
                row = await skincare_writes.update_log_note(
                    session,
                    record_id,
                    note=note,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            elif domain == "glp1":
                if not await deps.module_enabled(session, "glp1"):
                    return {"error": "module 'glp1' is disabled"}
                conflict_context = await deps.conflict_write_context(session)
                prepared = await engine.prepare_scoped_write(
                    session,
                    context=conflict_context,
                )
                row = await glp1_writes.update_injection_note(
                    session,
                    record_id,
                    note=note,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            else:
                conflict_context = await deps.conflict_write_context(session)
                prepared = await engine.prepare_scoped_write(
                    session,
                    context=conflict_context,
                )
                row = await lab_results.update_result_note(
                    session,
                    record_id,
                    note=note,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            if row is None:
                return _not_found(domain, record_id)
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    async def get_notes(
        domain: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Retrieves records that have non-empty notes, optionally filtered by domain
        and date range. Returns records from: weight, nutrition, glp1, skincare,
        measurement, body_comp, labs."""
        if domain and domain not in NOTE_MODELS:
            return [
                {"error": f"Unknown domain '{domain}'. Use: {', '.join(NOTE_MODELS)}"}
            ]

        targets = {domain: NOTE_MODELS[domain]} if domain else dict(NOTE_MODELS)
        session_factory = deps.get_session_factory()
        start = deps.parse_date(start_date, field="start_date")
        end = deps.parse_date(end_date, field="end_date")

        results = []
        async with session_factory() as session:
            weight_scope = None
            measurement_scope = None
            nutrition_scope = None
            skincare_scope = None
            glp1_scope = None
            labs_scope = None
            body_comp_scope = None
            if "weight" in targets:
                weight_scope = await deps.conflict_scope(session)
            if "measurement" in targets:
                measurement_scope = weight_scope or await deps.conflict_scope(session)
            if "nutrition" in targets:
                if not await deps.module_enabled(session, "nutrition"):
                    if domain == "nutrition":
                        return [{"error": "module 'nutrition' is disabled"}]
                    targets.pop("nutrition")
                else:
                    nutrition_scope = await deps.conflict_scope(session)
            if "skincare" in targets:
                if not await deps.module_enabled(session, "skincare"):
                    if domain == "skincare":
                        return [{"error": "module 'skincare' is disabled"}]
                    targets.pop("skincare")
                else:
                    skincare_scope = await deps.conflict_scope(session)
            if "glp1" in targets:
                if not await deps.module_enabled(session, "glp1"):
                    if domain == "glp1":
                        return [{"error": "module 'glp1' is disabled"}]
                    targets.pop("glp1")
                else:
                    glp1_scope = await deps.conflict_scope(session)
            if "labs" in targets:
                labs_scope = await deps.conflict_scope(session)
            if "body_comp" in targets:
                if not await deps.module_enabled(session, "body_comp"):
                    if domain == "body_comp":
                        return [{"error": "module 'body_comp' is disabled"}]
                    targets.pop("body_comp")
                else:
                    body_comp_scope = await deps.conflict_scope(session)

            for domain_name in targets:
                if domain_name == "weight":
                    assert weight_scope is not None
                    rows = await weight_queries.list_weight_notes(
                        session,
                        subject_id=weight_scope.subject_id,
                        start=start,
                        end=end,
                        limit=limit,
                    )
                elif domain_name == "nutrition":
                    assert nutrition_scope is not None
                    rows = await nutrition_queries.list_meals(
                        session,
                        start=start,
                        end=end,
                        subject_id=nutrition_scope.subject_id,
                        has_note=True,
                        limit=limit,
                    )
                elif domain_name == "measurement":
                    assert measurement_scope is not None
                    rows = await weight_measurements.list_body_measurements(
                        session,
                        subject_id=measurement_scope.subject_id,
                        start=start,
                        end=end,
                        has_note=True,
                    )
                elif domain_name == "skincare":
                    assert skincare_scope is not None
                    rows = await skincare_queries.list_logs(
                        session,
                        subject_id=skincare_scope.subject_id,
                        start=start,
                        end=end,
                        has_note=True,
                        limit=limit,
                    )
                elif domain_name == "glp1":
                    assert glp1_scope is not None
                    rows = await glp1_queries.list_injections(
                        session,
                        subject_id=glp1_scope.subject_id,
                        start=start,
                        end=end,
                        has_note=True,
                        limit=limit,
                    )
                elif domain_name == "labs":
                    assert labs_scope is not None
                    rows = await lab_results.list_results(
                        session,
                        subject_id=labs_scope.subject_id,
                        start=start,
                        end=end,
                        has_note=True,
                        limit=limit,
                    )
                else:
                    assert body_comp_scope is not None
                    scans_with_notes = await body_scan_queries.list_scans(
                        session,
                        subject_id=body_comp_scope.subject_id,
                        start=start,
                        end=end,
                    )
                    rows = [row for row in scans_with_notes if row.note]
                for row in rows:
                    entry = deps.serialize_row(row)
                    entry["_domain"] = domain_name
                    results.append(entry)

        results.sort(key=lambda item: item.get("date", ""), reverse=True)
        return results[:limit]

    return RegisteredNoteTools(log_note=log_note, get_notes=get_notes)


def register_delete_tools(
    server: Any,
    deps: RecordToolDependencies,
) -> RegisteredDeleteTools:
    """Register the explicit service-command delete hub at its frozen position."""

    @server.tool()
    async def delete_record(domain: str, record_id: int) -> dict:
        """Deletes one record from any domain by its ID. WRITE tool — immediate.

        ``domain`` is one of: weight, measurement (body tape), noise_marker, labs (one
        result), milestones (a goal card), nutrition (a meal), glp1 (an injection),
        glp1_side_effect, glp1_dose_phase, hrt_dose, hrt_side_effect, hrt_cycle
        (with its compound plans), hrt_cycle_item (one plan, cycle kept), body_comp
        (a scan with its metrics), timeline (a manual event), skincare_observation,
        supplements (a catalog entry), genetics (a variant).

        Deleting a weight log reactivates the next-highest-priority log for that date.
        Returns ``{"deleted": false, ...}`` when nothing has that id."""
        target = DELETE_TARGETS.get(domain)
        if target is None:
            return {
                "error": f"Unknown domain '{domain}'. Use: {', '.join(DELETE_TARGETS)}"
            }
        module_key, service_name, command_name = target

        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            if module_key and not await deps.module_enabled(session, module_key):
                return {"error": f"module '{module_key}' is disabled"}
            service = importlib.import_module(f"vitals.services.{service_name}")
            owned_kwargs = {}
            if domain == "weight":
                conflict_context, prepared = await deps.weight_write(session)
                owned_kwargs = {
                    "identity": conflict_context.identity,
                    "prepared_weight_write": prepared,
                }
            elif domain in {"measurement", "noise_marker"}:
                conflict_context, prepared = await deps.auxiliary_weight_write(session)
                owned_kwargs = {
                    "identity": conflict_context.identity,
                    "prepared_conflict_write": prepared,
                }
            elif domain == "body_comp":
                conflict_context, prepared = await deps.weight_write(session)
                owned_kwargs = {
                    "subject_id": conflict_context.identity.subject_id,
                    "identity": conflict_context.identity,
                    "prepared_weight_write": prepared,
                }
            elif domain == "milestones":
                conflict_context = await deps.conflict_write_context(session)
                prepared = await engine.prepare_scoped_write(
                    session,
                    context=conflict_context,
                )
                owned_kwargs = {
                    "identity": conflict_context.identity,
                    "prepared_conflict_write": prepared,
                }
            elif domain in {"supplements", "timeline"}:
                ownership = await deps.legacy_owner(session)
                owned_kwargs = {"identity": ownership.owner_action()}
            else:
                conflict_context = await deps.conflict_write_context(session)
                prepared = await engine.prepare_scoped_write(
                    session,
                    context=conflict_context,
                )
                owned_kwargs = {
                    "identity": conflict_context.identity,
                    "prepared_conflict_write": prepared,
                }
                if domain == "labs":
                    owned_kwargs["subject_id"] = conflict_context.identity.subject_id

            deleted = await getattr(service, command_name)(
                session,
                record_id,
                **owned_kwargs,
            )
            if domain == "body_comp" and deleted:
                await body_scan_alerts.refresh_alerts(
                    session,
                    subject_id=conflict_context.identity.subject_id,
                    identity=conflict_context.identity,
                    prepared_weight_write=prepared,
                )
            await session.commit()
            return {"deleted": deleted, "domain": domain, "record_id": record_id}

    return RegisteredDeleteTools(delete_record=delete_record)


__all__ = [
    "DELETE_TARGETS",
    "NOTE_MODELS",
    "RecordToolDependencies",
    "RegisteredDeleteTools",
    "RegisteredNoteTools",
    "register_delete_tools",
    "register_note_tools",
]
