"""Supplements MCP tool registration without a router dependency."""
from __future__ import annotations

from vitals.services.supplements import queries as supplement_queries
from vitals.services.supplements import writes as supplement_writes

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from vitals.enums import Domain, Source

from vitals.services.conflicts import catalog, engine
from vitals.services.conflicts.engine import ConflictBlocked


@dataclass(frozen=True)
class SupplementsToolDependencies:
    get_session_factory: Callable[[], Any]
    legacy_owner: Callable[[Any], Awaitable[Any]]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    conflict_write_context: Callable[..., Awaitable[Any]]
    conflict_payload: Callable[[ConflictBlocked], dict]
    serialize_row: Callable[[Any], dict]
    serialize_written: Callable[[Any, Any], Awaitable[dict]]
    gated: Callable[[str], Callable[[Any], Any]]


@dataclass(frozen=True)
class RegisteredSupplementsReadTools:
    get_supplements_catalog: Callable[..., Awaitable[list[dict]]]


@dataclass(frozen=True)
class RegisteredSupplementsConflictTools:
    check_supplement_conflicts: Callable[..., Awaitable[list[dict]]]


@dataclass(frozen=True)
class RegisteredSupplementsWriteTools:
    add_supplement: Callable[..., Awaitable[dict]]
    update_supplement: Callable[..., Awaitable[dict]]
    set_supplement_active: Callable[..., Awaitable[dict]]


def register_supplements_read_tools(
    server: Any,
    deps: SupplementsToolDependencies,
) -> RegisteredSupplementsReadTools:
    """Register the catalog read at its frozen surface position."""

    @server.tool()
    async def get_supplements_catalog() -> list[dict]:
        """Retrieves the active supplement catalog, including dosages and evidence tiers."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            ownership = await deps.legacy_owner(session)
            supplements = await supplement_queries.list_supplements(
                session,
                subject_id=ownership.subject_id,
            )
            return [deps.serialize_row(supplement) for supplement in supplements]

    return RegisteredSupplementsReadTools(
        get_supplements_catalog=get_supplements_catalog,
    )


def register_supplements_conflict_tools(
    server: Any,
    deps: SupplementsToolDependencies,
) -> RegisteredSupplementsConflictTools:
    """Register the supplement conflict preview at its frozen position."""

    @server.tool()
    async def check_supplement_conflicts(supplement_name: str) -> list[dict]:
        """Evaluates a proposed supplement (by free-text name) against the curated
        conflict-rule catalog — active supplements, genetics, skincare routine,
        labs, and GLP-1 state. The name is normalized to the same stable ``key``
        the catalog matches rules on (e.g. "Железо" -> "iron"), so this works
        regardless of spelling/language. Read-only — never writes, never blocks."""
        session_factory = deps.get_session_factory()
        key = catalog.normalize_ingredient(supplement_name)
        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            try:
                violations = await engine.evaluate_scoped(
                    session,
                    scope=scope,
                    domain=Domain.SUPPLEMENTS,
                    proposed_state={
                        "key": key,
                        "name": supplement_name,
                        "active": True,
                    },
                )
            except engine.ConflictResolverUnavailable as exc:
                return [{"error": str(exc)}]
            return [violation.to_dict() for violation in violations]

    return RegisteredSupplementsConflictTools(
        check_supplement_conflicts=check_supplement_conflicts,
    )


def register_supplements_write_tools(
    server: Any,
    deps: SupplementsToolDependencies,
) -> RegisteredSupplementsWriteTools:
    """Register catalog writes at their frozen surface position."""

    @server.tool()
    @deps.gated("supplements")
    async def add_supplement(
        name: str,
        key: Optional[str] = None,
        dose: Optional[str] = None,
        timing: Optional[str] = None,
        evidence: Optional[str] = None,
        active: bool = True,
        contraindications: Optional[str] = None,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Adds a supplement to the catalog (reference, not a daily log). ``key`` is the
        stable conflict-matching slug — omit it and it's derived from ``name`` (RU/EN
        aware). ``evidence`` is tier A/B/C. Activating a contraindicated supplement can
        hard-block → ``{"blocked": true, ...}``; retry with ``override=True``. WRITE tool."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            conflict_context = await deps.conflict_write_context(session)
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            try:
                row = await supplement_writes.add_supplement(
                    session,
                    name=name,
                    key=key,
                    dose=dose,
                    timing=timing,
                    evidence=evidence,
                    active=active,
                    contraindications=contraindications,
                    note=note,
                    override=override,
                    source=Source.MCP.value,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ConflictBlocked as exc:
                return deps.conflict_payload(exc)
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    @deps.gated("supplements")
    async def update_supplement(
        supplement_id: int,
        name: Optional[str] = None,
        key: Optional[str] = None,
        dose: Optional[str] = None,
        timing: Optional[str] = None,
        evidence: Optional[str] = None,
        active: Optional[bool] = None,
        contraindications: Optional[str] = None,
        note: Optional[str] = None,
        override: bool = False,
    ) -> dict:
        """Updates a catalog supplement by ID. Only the fields you pass are changed —
        a rename does not clear the dose or switch a paused supplement back on; use
        ``set_supplement_active`` (or pass ``active``) for that. Same conflict gate as
        add — a hard block returns ``{"blocked": true, ...}``; retry with
        ``override=True``. WRITE tool."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            conflict_context = await deps.conflict_write_context(session)
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            current = await supplement_queries.get_supplement_for_update(
                session,
                supplement_id,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
            if current is None:
                return {"error": f"Supplement {supplement_id} not found"}
            merged = {
                "name": current.name if name is None else name,
                "key": current.key if key is None else key,
                "dose": current.dose if dose is None else dose,
                "timing": current.timing if timing is None else timing,
                "evidence": current.evidence if evidence is None else evidence,
                "active": current.active if active is None else active,
                "contraindications": (
                    current.contraindications
                    if contraindications is None
                    else contraindications
                ),
                "note": current.note if note is None else note,
            }
            try:
                row = await supplement_writes.update_supplement(
                    session,
                    supplement_id,
                    override=override,
                    **merged,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ConflictBlocked as exc:
                return deps.conflict_payload(exc)
            await session.commit()
            return await deps.serialize_written(session, row)

    @server.tool()
    @deps.gated("supplements")
    async def set_supplement_active(
        supplement_id: int,
        active: bool,
        override: bool = False,
    ) -> dict:
        """Toggles a supplement's active flag. Activating a contraindicated one runs the
        conflict check → ``{"blocked": true, ...}`` unless ``override=True``. WRITE tool."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            conflict_context = await deps.conflict_write_context(session)
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            try:
                row = await supplement_writes.set_active(
                    session,
                    supplement_id,
                    active,
                    override=override,
                    identity=conflict_context.identity,
                    prepared_conflict_write=prepared,
                )
            except ConflictBlocked as exc:
                return deps.conflict_payload(exc)
            if row is None:
                return {"error": f"Supplement {supplement_id} not found"}
            await session.commit()
            return await deps.serialize_written(session, row)

    return RegisteredSupplementsWriteTools(
        add_supplement=add_supplement,
        update_supplement=update_supplement,
        set_supplement_active=set_supplement_active,
    )


__all__ = [
    "RegisteredSupplementsConflictTools",
    "RegisteredSupplementsReadTools",
    "RegisteredSupplementsWriteTools",
    "SupplementsToolDependencies",
    "register_supplements_conflict_tools",
    "register_supplements_read_tools",
    "register_supplements_write_tools",
]
