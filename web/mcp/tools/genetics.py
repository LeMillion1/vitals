"""Genetics MCP tool registration without a router or ORM dependency."""

from __future__ import annotations

from vitals.services.genetics import contracts as genetics_contracts
from vitals.services.genetics import queries as genetics_queries
from vitals.services.genetics import writes as genetics_writes

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from vitals.enums import Source
from vitals.services.conflicts import engine


@dataclass(frozen=True)
class GeneticsToolDependencies:
    get_session_factory: Callable[[], Any]
    conflict_scope: Callable[[Any], Awaitable[Any]]
    conflict_write_context: Callable[..., Awaitable[Any]]
    serialize_row: Callable[[Any], dict]
    serialize_written: Callable[[Any, Any], Awaitable[dict]]
    gated: Callable[[str], Callable[[Any], Any]]


@dataclass(frozen=True)
class RegisteredGeneticsTools:
    get_genetics_snps: Callable[..., Awaitable[list[dict]]]
    upsert_genetic_variant: Callable[..., Awaitable[dict]]


def register_genetics_tools(
    server: Any,
    deps: GeneticsToolDependencies,
) -> RegisteredGeneticsTools:
    """Register the frozen Genetics read/write pair in its existing order."""

    @server.tool()
    @deps.gated("genetics")
    async def get_genetics_snps(
        gene: Optional[str] = None,
        rsid: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Retrieves digitized SNPs (genetic variants) with a description of their effect.
        Filter by ``gene`` ("MTHFR") or ``rsid`` ("rs1801133") — both match regardless of
        case. Unfiltered it returns the first ``limit`` variants in (gene, rsid) order;
        a whole-genome import is far larger than that, so ask for the marker you mean.
        READ tool."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            scope = await deps.conflict_scope(session)
            variants = await genetics_queries.list_variants(
                session,
                subject_id=scope.subject_id,
                gene=gene,
                rsid=rsid,
                limit=limit,
            )
            return [deps.serialize_row(variant) for variant in variants]

    @server.tool()
    @deps.gated("genetics")
    async def upsert_genetic_variant(
        gene: str,
        rsid: str,
        genotype: Optional[str] = None,
        marker: Optional[str] = None,
        impact: Optional[str] = None,
        impact_domain: Optional[str] = None,
        interpretation: Optional[str] = None,
        action_notes: Optional[str] = None,
        clear_fields: Optional[list[str]] = None,
    ) -> dict:
        """Adds or updates one genetic variant, keyed by ``rsid`` — restating a known
        rsid edits that row instead of duplicating it. ``marker`` is the slug the
        conflict rules match on (e.g. "mthfr_c677t_tt"); without one the variant is
        reference-only. Fields left out keep their stored value. To explicitly clear
        an optional value, name it in ``clear_fields`` (for example
        ``["action_notes"]``). WRITE tool."""
        patch_fields = {
            "genotype": genotype,
            "marker": marker,
            "impact": impact,
            "impact_domain": impact_domain,
            "interpretation": interpretation,
            "action_notes": action_notes,
        }
        clear = set(clear_fields or ())
        unknown = clear.difference(patch_fields)
        if unknown:
            return {"error": "clear_fields contains unknown fields: " + ", ".join(sorted(unknown))}
        overlapping = sorted(name for name in clear if patch_fields[name] is not None)
        if overlapping:
            return {"error": "fields cannot be set and cleared together: " + ", ".join(overlapping)}
        for name, value in tuple(patch_fields.items()):
            if name in clear:
                patch_fields[name] = None
            elif value is None:
                patch_fields[name] = genetics_contracts.PATCH_UNSET

        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            context = await deps.conflict_write_context(session)
            prepared = await engine.prepare_scoped_write(
                session,
                context=context,
            )
            try:
                row = await genetics_writes.upsert_by_rsid(
                    session,
                    gene=gene,
                    rsid=rsid,
                    **patch_fields,
                    source=Source.MCP.value,
                    identity=context.identity,
                    prepared_conflict_write=prepared,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            return await deps.serialize_written(session, row)

    return RegisteredGeneticsTools(
        get_genetics_snps=get_genetics_snps,
        upsert_genetic_variant=upsert_genetic_variant,
    )


__all__ = ["GeneticsToolDependencies", "RegisteredGeneticsTools", "register_genetics_tools"]
