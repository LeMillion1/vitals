"""Model Context Protocol (MCP) server integration for Vitals.

Exposes access to all health domains using FastMCP and standard SQLAlchemy
preloading patterns. Read tools cover every domain; write tools let Claude
record and edit meals, weight, GLP-1, skincare, supplements, measurements,
body scans, labs, goals, timeline events and notes directly from the
conversation. Two resources (``vitals://profile``, ``vitals://digest/latest``)
and a ``weekly_review`` prompt round out the surface.

Response conventions (a stable contract the model can rely on):
  * Success — the tool's normal payload (a dict, or a list of dicts).
  * A recoverable problem (bad id, unknown key, missing dependency) — a dict
    ``{"error": "<human message>"}`` (list-returning tools wrap it: ``[{"error": ...}]``).
  * A hard conflict block on a write — a dict ``{"blocked": true, "violations":
    [...], "message": ..., "hint": ...}`` (see ``_conflict_payload``); the model
    can retry the same call with ``override=True``.
  * A delete — ``{"deleted": <bool>, "domain": <str>, "record_id": <id>}``
    (one ``delete_record`` tool serves every domain; see ``_DELETE_TARGETS``).
  * A write to a switched-off optional domain — ``{"error": "module '<key>' is
    disabled"}``; ``get_modules`` says which are on.
"""
from __future__ import annotations

import functools
import importlib
import logging
import os
import uuid
from datetime import date as date_type, timedelta
from typing import Optional

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from vitals.config import load_config
from vitals.enums import (
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    IntegrationProvider,
    MilestoneStatus,
    Source,
)
from vitals.models import (
    Annotation,
    BodyMeasurement,
    BodyScan,
    DayContext,
    DosePhase,
    GarminActivity,
    GarminDaily,
    GarminIntraday,
    GeneticVariant,
    HevyExercise,
    HevyWorkout,
    HrtCycle,
    HrtDose,
    HrtSideEffect,
    Injection,
    LabResult,
    MealLog,
    Milestone,
    NoiseMarker,
    SideEffect,
    Signal,
    SkincareLog,
    SkincareObservation,
    Supplement,
    WeightLog,
    WeeklyDigest,
)
from vitals.services import conflict_engine, genetics_service, modules_service
from vitals.services.conflict_engine import ConflictBlocked
from vitals.services.data_portability_service import GENERIC_OUTPUT_SUPPRESSED_COLUMNS
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from vitals.utils.timeutils import now_local, today_local
from web.config import get_web_config
from web.deps import get_redis_client, get_session_factory

logger = logging.getLogger(__name__)

mcp = FastMCP("Vitals")


# Columns every row carries and no tool ever accepts back: bookkeeping the model
# cannot act on. Dropped from serialized rows along with every ``None`` value —
# at a hundred rows per read the key names alone outweigh the data. ``id`` and
# ``date`` stay (edits and deletes address rows by id); ``source`` stays (weight
# priority and provenance are answers in their own right).
_ROW_NOISE = (
    frozenset(
        {
            "domain",
            "created_at",
            "updated_at",
            "raw_payload_id",
            "raw_id",
            "ai_invocation_id",
            "weight_log_id",
        }
    )
    | GENERIC_OUTPUT_SUPPRESSED_COLUMNS
)


def serialize_row(row) -> dict:
    """Helper to convert any SQLAlchemy model instance into a JSON-serializable dict.

    Omits bookkeeping columns (``_ROW_NOISE``) and unset fields: an absent key and
    a ``null`` one read the same to the model, and the null costs tokens per row.
    """
    if row is None:
        return {}
    d = {}
    for column in row.__table__.columns:
        if column.name in _ROW_NOISE:
            continue
        val = getattr(row, column.name)
        if val is None:
            continue
        d[column.name] = val.isoformat() if hasattr(val, "isoformat") else val
    return d


async def serialize_written(session, row) -> dict:
    """Serialize a row that was just written. After an UPDATE flush, server-side
    ``onupdate``/``server_default`` columns (e.g. ``updated_at``) are *expired*;
    reading them in the sync ``serialize_row`` would trigger a lazy SELECT outside
    the async greenlet and fail with ``greenlet_spawn has not been called``. An
    explicit ``await session.refresh`` reloads them inside the async context first.
    """
    if row is None:
        return {}
    await session.refresh(row)
    return serialize_row(row)


def _conflict_payload(exc: ConflictBlocked) -> dict:
    """Structured result for a write blocked by a hard conflict rule.

    The HTML UI gets a 409 + violations and renders "Save anyway (Override)".
    A tool call has no HTTP status the model can act on, so we return the same
    violation list as a plain dict instead of letting the exception escape as an
    opaque 500 — the model can inspect the block and retry the call with
    ``override=True`` (the MCP equivalent of the override button)."""
    return {
        "blocked": True,
        "message": str(exc),
        "violations": [v.to_dict() for v in exc.violations],
        "hint": "Retry the same call with override=True to save anyway.",
    }


def _parse_date(value: Optional[str], default=None, *, field: str):
    """Parse a ``YYYY-MM-DD`` tool argument, falling back to ``default`` when omitted.

    A model writes dates the way a person says them ("вчера", "01.07.2026"), and
    the stdlib answers with "Invalid isoformat string: ..." — which names neither
    the argument nor the shape expected, so the model can't fix its own call.
    """
    if value is None:
        return default
    try:
        return date_type.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError(f"{field} must be a YYYY-MM-DD date, got {value!r}") from None


def _parse_time(value: Optional[str], *, field: str):
    """Same as ``_parse_date`` for an ``HH:MM`` argument."""
    from datetime import time as time_type

    if value is None:
        return None
    try:
        return time_type.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError(f"{field} must be an HH:MM time, got {value!r}") from None


async def _merged(session, model, record_id: int, **fields) -> Optional[dict]:
    """Fill a partial tool edit in from the stored row: a field left ``None`` keeps
    its current value. Keys are column names on ``model``; ``None`` if the row is gone.

    The update services replace every field they are handed, because the web forms
    post the whole form and clearing an input there has to clear the column. A tool
    call carries only what the conversation mentioned, so the same call would blank
    everything the model didn't repeat — a rename would cost the meal its calories.
    """
    row = await session.get(model, record_id)
    if row is None:
        return None
    return {k: (getattr(row, k) if v is None else v) for k, v in fields.items()}


async def _module_enabled(session, key: str) -> bool:
    """True when an optional module is on (write tools honour the toggle)."""
    from vitals.services import modules_service

    ownership = await _mcp_v1_legacy_owner(session)
    state = await modules_service.get_enabled_modules(
        session,
        subject_id=ownership.subject_id,
    )
    return bool(state.get(key))


async def _mcp_v1_legacy_owner(session):
    """Resolve the configured single owner for one legacy MCP v1 operation.

    The current connector token authenticates the installation, not a selected
    subject. Mapping it to the configured owner is attribution plus a fail-closed
    single-subject compatibility gate; it is not MCP v2 subject authorization.
    """
    return await resolve_legacy_ownership_context(
        session,
        actor_username=get_web_config().auth_username,
    )


async def _mcp_v1_legacy_alert_owner(session):
    """Resolve every current provider root needed by the alert aggregate."""

    return await resolve_legacy_ownership_context(
        session,
        actor_username=get_web_config().auth_username,
        required_connections=tuple(IntegrationProvider),
    )


async def _mcp_v1_conflict_scope(session) -> conflict_engine.ConflictScope:
    """Authenticate and bind an MCP conflict read under governance lock."""

    return await conflict_engine.resolve_legacy_conflict_scope(
        session,
        actor_username=get_web_config().auth_username,
        evaluation_date=today_local(),
    )


async def _mcp_v1_composition_scope(session) -> conflict_engine.ConflictScope:
    """Bind a legacy whole-lake read and reject corrupt Milestone roots.

    The v1 connector still has no selected subject.  The governance-locked
    exact-one proof prevents cross-subject composition, while the scoped
    Milestone read makes partial legacy rows fail before a raw compatibility
    aggregate can serialize or send them to an LLM.
    """

    from vitals.services import digest_service, milestones_service

    scope = await _mcp_v1_conflict_scope(session)
    await milestones_service.list_milestones(
        session,
        subject_id=scope.subject_id,
    )
    # Whole-lake compatibility tools still query globally, so validate every
    # WeeklyDigest root before export/overview can serialize or count it.
    await digest_service.prepare_digest_owner(
        session,
        actor_username=get_web_config().auth_username,
    )
    return scope


async def _mcp_v1_conflict_write_context(
    session,
    *,
    evaluation_date: date_type | None = None,
) -> conflict_engine.ConflictWriteContext:
    """Authenticate the configured owner for a scoped MCP v1 conflict write."""

    return await conflict_engine.resolve_legacy_conflict_write_context(
        session,
        actor_username=get_web_config().auth_username,
        evaluation_date=evaluation_date or today_local(),
    )


async def _mcp_v1_weight_write(
    session,
    *,
    evaluation_date: date_type | None = None,
):
    """Prepare Weight plus its distinct Garmin destination outbox."""

    from vitals.services import garmin_weight_service, weight_service

    conflict_context = await _mcp_v1_conflict_write_context(
        session,
        evaluation_date=evaluation_date,
    )
    export_context = await garmin_weight_service.resolve_optional_legacy_export_context(
        session,
        actor_username=get_web_config().auth_username,
    )
    prepared = await weight_service.prepare_weight_write(
        session,
        context=conflict_context,
        garmin_weight_export_context=export_context,
    )
    return conflict_context, prepared


async def _mcp_v1_aux_weight_write(
    session,
    *,
    evaluation_date: date_type | None = None,
):
    """Prepare a BodyMeasurement/NoiseMarker write without the outbox advisory."""

    conflict_context = await _mcp_v1_conflict_write_context(
        session,
        evaluation_date=evaluation_date,
    )
    prepared = await conflict_engine.prepare_scoped_write(
        session,
        context=conflict_context,
    )
    return conflict_context, prepared


# tool name → the optional module it belongs to. Writes register themselves through
# ``gated``; the reads of those same domains are listed below. Used only to hide a
# switched-off module's tools from ``tools/list`` — the surface is 75 tools and
# their schemas are re-sent with every message of every conversation, so a domain
# the owner does not track is pure weight. Reads are classified separately below;
# ownership-sensitive reads may also use this decorator to reject direct calls.
TOOL_MODULES: dict[str, str] = {}


def gated(module_key: str):
    """Refuse a write when its optional module is switched off.

    Turning a module off in settings is the owner saying "I don't track this" —
    the web routes honour it (``require_module``), and until now the tool surface
    honoured it on three writes out of forty, so a conversation could refill a
    domain the owner had just emptied out of the UI. One decorator per write tool
    of an optional domain; ``tests/test_mcp_module_gate.py`` holds the full list,
    so a new tool has to be classified rather than quietly ungated."""
    def decorator(fn):
        TOOL_MODULES[fn.__name__] = module_key

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            session_factory = get_session_factory()
            async with session_factory() as session:
                if not await _module_enabled(session, module_key):
                    return {"error": f"module '{module_key}' is disabled"}
            return await fn(*args, **kwargs)

        return wrapper

    return decorator


# ── Tool Definitions ─────────────────────────────────────────────────────────

@mcp.tool()
async def get_user_profile() -> dict:
    """Returns the user's physical profile, active goals, and program overview."""
    cfg = load_config()
    return {
        "height_cm": cfg.height_cm,
        "sex": cfg.sex,
        "age": cfg.user_age,
        "timezone": str(cfg.timezone),
        "goals": cfg.user_goals,
        "program": cfg.user_program,
    }


@mcp.tool()
async def get_weight_logs(
    start_date: Optional[str] = None, end_date: Optional[str] = None, limit: int = 100
) -> dict:
    """Retrieves active weight logs, body measurements, and noise markers for a
    date range (YYYY-MM-DD). Weights/measurements default to the most recent 100."""
    from vitals.services import weight_service

    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        scope = await _mcp_v1_conflict_scope(session)
        # Weight logs — the "active weight" invariant (superseded filter, source
        # priority) lives in weight_service; call it instead of re-encoding the
        # rule here, then apply this tool's newest-first, most-recent-`limit`
        # contract on top (the service returns all matching rows, ascending).
        weights = await weight_service.list_active_weights(
            session,
            start=start,
            end=end,
            subject_id=scope.subject_id,
            include_legacy_unowned=scope.include_legacy_unowned,
        )
        weights = sorted(weights, key=lambda w: w.date, reverse=True)[:limit]

        measurements = await weight_service.list_body_measurements(
            session,
            subject_id=scope.subject_id,
            include_legacy_unowned=scope.include_legacy_unowned,
            start=start,
            end=end,
        )
        measurements = sorted(
            measurements, key=lambda row: row.date, reverse=True
        )[:limit]

        noise = await weight_service.list_noise_markers(
            session,
            subject_id=scope.subject_id,
            include_legacy_unowned=scope.include_legacy_unowned,
            start=start,
            end=end,
        )
        noise = sorted(noise, key=lambda row: row.start_date, reverse=True)

        return {
            "weights": [serialize_row(w) for w in weights],
            "measurements": [serialize_row(m) for m in measurements],
            "noise_markers": [serialize_row(n) for n in noise],
        }


@mcp.tool()
@gated("glp1")
async def get_glp1_logs(
    start_date: Optional[str] = None, end_date: Optional[str] = None, limit: int = 100
) -> dict:
    """Retrieves GLP-1 injection logs, active dosage phases, and recorded side
    effects. Injections/side effects default to the most recent 100."""
    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        from vitals.services import glp1_service

        scope = await _mcp_v1_conflict_scope(session)
        scope_kwargs = {
            "subject_id": scope.subject_id,
            "include_legacy_unowned": scope.include_legacy_unowned,
        }
        injections = await glp1_service.list_injections(
            session,
            start=start,
            end=end,
            limit=limit,
            **scope_kwargs,
        )
        phases = sorted(
            await glp1_service.list_dose_phases(session, **scope_kwargs),
            key=lambda phase: (phase.start_date, phase.id),
            reverse=True,
        )
        effects = await glp1_service.list_side_effects(
            session,
            start=start,
            end=end,
            limit=limit,
            **scope_kwargs,
        )

        return {
            "injections": [serialize_row(i) for i in injections],
            "dose_phases": [serialize_row(p) for p in phases],
            "side_effects": [serialize_row(s) for s in effects],
        }


# Ceiling on intraday points in one get_garmin_metrics response (~5 days of a
# single series at Garmin's 3-minute cadence). The table is the densest in the
# project — a year is ~350k rows — so an unbounded read would blow the context.
INTRADAY_POINT_CAP = 5000

# The two per-night timelines on a daily row: a hypnogram is ~30 intervals and the
# breathing spans a handful more, so together they are ~70% of the row's JSON and
# ride along on every read of the last hundred nights. Replaced by a breadcrumb
# unless asked for — hiding the data outright would read as "there are no sleep
# stages" and the model would stop asking.
_SLEEP_DETAIL_COLUMNS = ("sleep_stages", "breathing_events")


def _fold_sleep_detail(row: dict) -> dict:
    """Swap each present sleep-detail column for a count + how to get the real thing."""
    for name in _SLEEP_DETAIL_COLUMNS:
        value = row.get(name)
        if value:
            row[name] = f"{len(value)} entries — call again with sleep_detail=True"
    return row


@mcp.tool()
async def get_garmin_metrics(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 100,
    intraday: bool = False,
    sleep_detail: bool = False,
) -> dict:
    """Retrieves daily Garmin recovery/sleep scores and recorded activity sessions.
    Each series defaults to the most recent 100 rows.

    Set ``intraday=True`` to also get the curves behind the daily summaries, as
    ``intraday: {series_type: [{ts, value}]}``. Two families of series:

      * the whole day — ``stress``, ``body_battery``, ``heart_rate`` (a sample
        every ~2–3 minutes, so ~480 points per series per day);
      * the night — ``sleep_hr``, ``sleep_spo2``, ``sleep_respiration``,
        ``sleep_stress``, ``sleep_bb``, ``sleep_hrv``, ``sleep_movement``
        (~2000 points across the seven).

    A night's samples are dated to the daily row they belong to (the morning of
    waking), including the ones recorded the previous evening, so one night reads
    as one date.

    Off by default because it is orders of magnitude more data than the daily
    rows: use it to answer *when* something happened (a stress spike, a Body
    Battery drain, an SpO2 dip and which sleep stage it fell in), always with a
    narrow start_date/end_date window. The response caps at 5000 points and sets
    ``intraday_truncated`` to true when the window held more than that.

    The night's *stage* timeline is not a series — it's ``sleep_stages`` on the
    daily row (``[{start, end, stage}]``, stage being deep/light/rem/awake), next
    to ``breathing_events``. Both are folded to a count by default and returned in
    full with ``sleep_detail=True`` — a separate switch from ``intraday`` so that
    reading one night's hypnogram doesn't drag every curve along with it. Ask for
    it with a narrow window when the question is about the shape of a night.
    """
    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        # Daily metrics
        d_stmt = select(GarminDaily)
        if start:
            d_stmt = d_stmt.where(GarminDaily.date >= start)
        if end:
            d_stmt = d_stmt.where(GarminDaily.date <= end)
        d_stmt = d_stmt.order_by(GarminDaily.date.desc()).limit(limit)
        daily = (await session.execute(d_stmt)).scalars().all()

        # Activities
        a_stmt = select(GarminActivity)
        if start:
            a_stmt = a_stmt.where(GarminActivity.date >= start)
        if end:
            a_stmt = a_stmt.where(GarminActivity.date <= end)
        a_stmt = a_stmt.order_by(GarminActivity.date.desc(), GarminActivity.start_time.desc()).limit(limit)
        activities = (await session.execute(a_stmt)).scalars().all()

        rows = [serialize_row(d) for d in daily]
        if not sleep_detail:
            rows = [_fold_sleep_detail(r) for r in rows]
        result = {
            "daily_recovery": rows,
            "activities": [serialize_row(a) for a in activities],
        }

        if intraday:
            # Grouped per series and trimmed to {ts, value} rather than run through
            # serialize_row: at thousands of rows the per-row id/domain/source/
            # timestamps would dwarf the actual curve. Fetch one over the cap to
            # tell "exactly full" from "truncated".
            i_stmt = select(GarminIntraday)
            if start:
                i_stmt = i_stmt.where(GarminIntraday.date >= start)
            if end:
                i_stmt = i_stmt.where(GarminIntraday.date <= end)
            i_stmt = i_stmt.order_by(GarminIntraday.ts).limit(INTRADAY_POINT_CAP + 1)
            points = (await session.execute(i_stmt)).scalars().all()
            result["intraday_truncated"] = len(points) > INTRADAY_POINT_CAP
            series: dict[str, list[dict]] = {}
            for p in points[:INTRADAY_POINT_CAP]:
                series.setdefault(p.series_type, []).append(
                    {"ts": p.ts.isoformat(), "value": p.value}
                )
            result["intraday"] = series

        return result


@mcp.tool()
async def get_hevy_workouts(
    start_date: Optional[str] = None, end_date: Optional[str] = None, limit: int = 100
) -> list[dict]:
    """Retrieves Hevy strength training workouts, including exercises, sets,
    weights, and reps. Defaults to the most recent 100 workouts."""
    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        stmt = select(HevyWorkout)
        if start:
            stmt = stmt.where(HevyWorkout.date >= start)
        if end:
            stmt = stmt.where(HevyWorkout.date <= end)
        stmt = stmt.options(selectinload(HevyWorkout.exercises).selectinload(HevyExercise.sets))
        stmt = stmt.order_by(HevyWorkout.date.desc()).limit(limit)
        workouts = (await session.execute(stmt)).scalars().all()

        serialized = []
        for w in workouts:
            w_dict = serialize_row(w)
            w_dict["exercises"] = []
            for e in w.exercises:
                e_dict = serialize_row(e)
                e_dict["sets"] = [serialize_row(s) for s in e.sets]
                w_dict["exercises"].append(e_dict)
            serialized.append(w_dict)
        return serialized


@mcp.tool()
async def get_supplements_catalog() -> list[dict]:
    """Retrieves the active supplement catalog, including dosages and evidence tiers."""
    from vitals.services import supplements_service

    session_factory = get_session_factory()
    async with session_factory() as session:
        ownership = await _mcp_v1_legacy_owner(session)
        supps = await supplements_service.list_supplements(
            session,
            subject_id=ownership.subject_id,
        )
        return [serialize_row(s) for s in supps]


@mcp.tool()
@gated("skincare")
async def get_skincare_logs(
    start_date: Optional[str] = None, end_date: Optional[str] = None, limit: int = 100
) -> dict:
    """Retrieves skincare routine application logs and skin status observations.
    Each series defaults to the most recent 100 rows."""
    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        scope = await _mcp_v1_conflict_scope(session)
        from vitals.services import skincare_service

        logs = await skincare_service.list_logs(
            session,
            subject_id=scope.subject_id,
            start=start,
            end=end,
            limit=limit,
        )
        observations = await skincare_service.list_observations(
            session,
            subject_id=scope.subject_id,
            start=start,
            end=end,
            limit=limit,
        )

        return {
            "logs": [serialize_row(l) for l in logs],
            "observations": [serialize_row(o) for o in observations],
        }


@mcp.tool()
@gated("genetics")
async def get_genetics_snps(
    gene: Optional[str] = None, rsid: Optional[str] = None, limit: int = 100
) -> list[dict]:
    """Retrieves digitized SNPs (genetic variants) with a description of their effect.
    Filter by ``gene`` ("MTHFR") or ``rsid`` ("rs1801133") — both match regardless of
    case. Unfiltered it returns the first ``limit`` variants in (gene, rsid) order;
    a whole-genome import is far larger than that, so ask for the marker you mean.
    READ tool."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        scope = await _mcp_v1_conflict_scope(session)
        variants = await genetics_service.list_variants(
            session,
            subject_id=scope.subject_id,
            gene=gene,
            rsid=rsid,
            limit=limit,
        )
        return [serialize_row(v) for v in variants]


@mcp.tool()
@gated("genetics")
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
        return {
            "error": "clear_fields contains unknown fields: "
            + ", ".join(sorted(unknown))
        }
    overlapping = sorted(name for name in clear if patch_fields[name] is not None)
    if overlapping:
        return {
            "error": "fields cannot be set and cleared together: "
            + ", ".join(overlapping)
        }
    for name, value in tuple(patch_fields.items()):
        if name in clear:
            patch_fields[name] = None
        elif value is None:
            patch_fields[name] = genetics_service.PATCH_UNSET

    session_factory = get_session_factory()
    async with session_factory() as session:
        context = await _mcp_v1_conflict_write_context(session)
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=context,
        )
        try:
            row = await genetics_service.upsert_by_rsid(
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
        return await serialize_written(session, row)


@mcp.tool()
async def get_active_alerts() -> list[dict]:
    """Returns currently active warning alerts and conflict notifications."""
    from vitals.services import legacy_subject_alerts

    session_factory = get_session_factory()
    async with session_factory() as session:
        ownership = await _mcp_v1_legacy_alert_owner(session)
        alerts = await legacy_subject_alerts.list_active(
            session,
            ownership=ownership,
        )
        return [serialize_row(a) for a in alerts]


@mcp.tool()
async def resolve_alert(alert_id: int) -> dict:
    """Marks one alert resolved — it disappears from ``get_active_alerts`` and
    from the dashboard. Use it once the thing the alert is about has actually been
    dealt with in the conversation, so the discussion and the closing are the same
    step instead of leaving the owner a button to press afterwards. WRITE tool."""
    from vitals.services import legacy_subject_alerts

    session_factory = get_session_factory()
    async with session_factory() as session:
        ownership = await _mcp_v1_legacy_alert_owner(session)
        row = await legacy_subject_alerts.resolve(
            session,
            alert_id,
            ownership=ownership,
        )
        if row is None:
            return {"error": f"Alert {alert_id} not found"}
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
async def override_alert(alert_id: int) -> dict:
    """Marks a blocking alert overridden — "noted, doing it anyway". The alert
    stays active and visible; only the block it represents stops being treated as
    unanswered. For resolving it instead, use ``resolve_alert``. WRITE tool."""
    from vitals.services import legacy_subject_alerts

    session_factory = get_session_factory()
    async with session_factory() as session:
        ownership = await _mcp_v1_legacy_alert_owner(session)
        row = await legacy_subject_alerts.override(
            session,
            alert_id,
            ownership=ownership,
        )
        if row is None:
            return {"error": f"Alert {alert_id} not found"}
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
async def get_weekly_digests(limit: int = 5) -> list[dict]:
    """Retrieves historical Claude-generated weekly summaries for continuity."""
    from vitals.services import digest_service

    session_factory = get_session_factory()
    async with session_factory() as session:
        owner = await digest_service.prepare_digest_owner(
            session,
            actor_username=get_web_config().auth_username,
        )
        # Through the service, so this stays weekly-only: the same table now also
        # holds the daily Telegram briefs.
        digests = await digest_service.list_digests(
            session,
            limit=limit,
            prepared_owner=owner,
        )
        return [serialize_row(d) for d in digests]


@mcp.tool()
async def check_supplement_conflicts(supplement_name: str) -> list[dict]:
    """Evaluates a proposed supplement (by free-text name) against the curated
    conflict-rule catalog — active supplements, genetics, skincare routine,
    labs, and GLP-1 state. The name is normalized to the same stable ``key``
    the catalog matches rules on (e.g. "Железо" -> "iron"), so this works
    regardless of spelling/language. Read-only — never writes, never blocks."""
    from vitals.services import conflict_catalog

    session_factory = get_session_factory()
    key = conflict_catalog.normalize_ingredient(supplement_name)
    async with session_factory() as session:
        scope = await _mcp_v1_conflict_scope(session)
        try:
            violations = await conflict_engine.evaluate_scoped(
                session,
                scope=scope,
                domain=Domain.SUPPLEMENTS,
                proposed_state={
                    "key": key,
                    "name": supplement_name,
                    "active": True,
                },
            )
        except conflict_engine.ConflictResolverUnavailable as exc:
            return [{"error": str(exc)}]
        return [v.to_dict() for v in violations]


_VALID_CONFLICT_DOMAINS = {d.value for d in Domain}


@mcp.tool()
async def list_conflict_rules(
    domain: Optional[str] = None, category: Optional[str] = None
) -> list[dict]:
    """Lists the curated cross-domain conflict rules (vitals/data/conflict_rules.yaml),
    optionally filtered by ``domain`` (matches either side of the rule) and/or
    ``category`` (absorption, pharmacogenomics, dermatology, lab_safety, glp1,
    contraindication). Only ``active`` rules are meaningful for evaluation, but
    inactive ones are included too so a caller can see the full catalog."""
    from vitals.services import conflict_activation_service

    session_factory = get_session_factory()
    async with session_factory() as session:
        scope = await _mcp_v1_conflict_scope(session)
        rows = await conflict_engine.load_scoped_rules(
            session,
            scope=scope,
            domain=domain,
            active_only=False,
        )
        activation_state = await conflict_activation_service.read_activation_state(
            session,
            subject_id=scope.subject_id,
            legacy_bridge=scope.legacy_bridge,
        )
        activation = conflict_activation_service.effective_rule_activation(
            rows,
            activation_state,
        )
        if category:
            rows = [row for row in rows if row.category == category]
        payloads = []
        for row in rows:
            payload = serialize_row(row)
            payload["active"] = activation[row.id]
            payloads.append(payload)
        return payloads


@mcp.tool()
async def check_conflicts(domain: str, payload: dict) -> list[dict]:
    """Evaluates an arbitrary proposed state against the active conflict rules
    for ``domain`` (one of: weight, glp1, supplements, genetics, skincare,
    labs, nutrition, workouts, garmin, milestones, system, body_comp). E.g.
    ``check_conflicts("labs", {"marker": "Калий", "value": 5.5})`` or
    ``check_conflicts("supplements", {"key": "iron", "active": True})``.
    Read-only — never writes, never blocks; returns the violations that would
    fire if this state were saved."""
    if domain not in _VALID_CONFLICT_DOMAINS:
        return [{"error": f"Unknown domain '{domain}'. Use one of: {', '.join(sorted(_VALID_CONFLICT_DOMAINS))}"}]

    session_factory = get_session_factory()
    async with session_factory() as session:
        scope = await _mcp_v1_conflict_scope(session)
        try:
            violations = await conflict_engine.evaluate_scoped(
                session,
                scope=scope,
                domain=domain,
                proposed_state=payload,
            )
        except conflict_engine.ConflictResolverUnavailable as exc:
            return [{"error": str(exc)}]
        return [v.to_dict() for v in violations]


# ── Nutrition tools ──────────────────────────────────────────────────────────

@mcp.tool()
@gated("nutrition")
async def log_meal(
    name: str,
    calories: Optional[float] = None,
    protein_g: Optional[float] = None,
    fat_g: Optional[float] = None,
    carbs_g: Optional[float] = None,
    eaten_at: Optional[str] = None,
    note: Optional[str] = None,
    on_date: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Records a meal or snack with optional macros (KCAL, protein, fat, carbs).

    This is a WRITE tool — the meal is saved to the database immediately.
    Defaults: on_date = today, eaten_at = current time. If a hard conflict rule
    blocks the save, returns ``{"blocked": true, "violations": [...]}`` instead
    of saving; call again with ``override=True`` to save anyway.
    """
    from datetime import time as time_type
    from vitals.services import nutrition_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")
    parsed_time = _parse_time(eaten_at, field="eaten_at")

    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_date,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        try:
            row = await nutrition_service.log_meal(
                session,
                on_date=parsed_date,
                name=name,
                eaten_at=parsed_time,
                calories=calories,
                protein_g=protein_g,
                fat_g=fat_g,
                carbs_g=carbs_g,
                note=note,
                source=Source.MCP.value,
                override=override,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
@gated("nutrition")
async def get_nutrition_summary(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """Returns a nutrition summary with total KCAL/protein/fat/carbs, meal counts,
    per-day breakdown, and goal tracking. Defaults to today if no dates given."""
    from vitals.services import nutrition_service
    from vitals.utils.timeutils import today_local

    cfg = load_config()
    session_factory = get_session_factory()
    today = today_local()

    start = _parse_date(start_date, today, field="start_date")
    end = _parse_date(end_date, today, field="end_date")

    if start == end:
        async with session_factory() as session:
            scope = await _mcp_v1_conflict_scope(session)
            return await nutrition_service.daily_summary(
                session,
                start,
                cfg,
                subject_id=scope.subject_id,
                include_unowned_legacy=scope.include_legacy_unowned,
            )
    else:
        async with session_factory() as session:
            scope = await _mcp_v1_conflict_scope(session)
            return await nutrition_service.nutrition_summary(
                session,
                start,
                end,
                cfg,
                subject_id=scope.subject_id,
                include_unowned_legacy=scope.include_legacy_unowned,
            )


# ── Meal CRUD tools ─────────────────────────────────────────────────────────

@mcp.tool()
@gated("nutrition")
async def update_meal(
    meal_id: int,
    name: Optional[str] = None,
    calories: Optional[float] = None,
    protein_g: Optional[float] = None,
    fat_g: Optional[float] = None,
    carbs_g: Optional[float] = None,
    eaten_at: Optional[str] = None,
    note: Optional[str] = None,
    on_date: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Updates an existing meal by ID. Returns the updated meal or an error.

    Only the fields you pass are changed — anything left out keeps its stored
    value, including ``on_date``, which stays the meal's own date rather than
    moving the meal to today. WRITE tool — changes are saved immediately.
    """
    from vitals.services import nutrition_service

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, field="on_date")
    parsed_time = _parse_time(eaten_at, field="eaten_at")

    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_date or today_local(),
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        current = await nutrition_service.get_meal_for_update(
            session,
            meal_id,
            identity=conflict_context.identity,
            include_unowned_legacy=True,
            prepared_conflict_write=prepared,
        )
        if current is None:
            return {"error": f"Meal {meal_id} not found"}
        final_date = current.date if parsed_date is None else parsed_date
        if conflict_context.evaluation_date != final_date:
            conflict_context = conflict_engine.ConflictWriteContext(
                identity=conflict_context.identity,
                evaluation_date=final_date,
                legacy_bridge=conflict_context.legacy_bridge,
            )
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
        merged = {
            "name": current.name if name is None else name,
            "eaten_at": current.eaten_at if eaten_at is None else parsed_time,
            "calories": current.calories if calories is None else calories,
            "protein_g": current.protein_g if protein_g is None else protein_g,
            "fat_g": current.fat_g if fat_g is None else fat_g,
            "carbs_g": current.carbs_g if carbs_g is None else carbs_g,
            "note": current.note if note is None else note,
        }
        try:
            row = await nutrition_service.update_meal(
                session,
                meal_id,
                on_date=final_date,
                override=override,
                identity=conflict_context.identity,
                include_unowned_legacy=True,
                prepared_conflict_write=prepared,
                **merged,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
@gated("nutrition")
async def search_meals(
    query: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Searches meals by name substring and/or date range. Returns matching meals
    ordered by date descending."""
    from vitals.services import nutrition_service

    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        scope = await _mcp_v1_conflict_scope(session)
        rows = await nutrition_service.list_meals(
            session,
            start=start,
            end=end,
            subject_id=scope.subject_id,
            include_unowned_legacy=scope.include_legacy_unowned,
            name_query=query,
            limit=limit,
        )
        return [serialize_row(r) for r in rows]


# ── Weight tools ────────────────────────────────────────────────────────────

@mcp.tool()
async def log_weight(
    weight_kg: float,
    on_date: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Records a manual weight entry (kg). One active weight per date — manual
    entries override Garmin imports. WRITE tool — saved immediately. If a hard
    conflict rule blocks the save, returns ``{"blocked": true, ...}``; call again
    with ``override=True`` to save anyway."""
    from vitals.services import weight_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")

    async with session_factory() as session:
        try:
            conflict_context, prepared = await _mcp_v1_weight_write(
                session,
                evaluation_date=parsed_date,
            )
            row = await weight_service.log_weight(
                session,
                on_date=parsed_date,
                weight_kg=weight_kg,
                note=note,
                source=Source.MCP.value,
                override=override,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_weight_write=prepared,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        except ValueError as e:
            await session.rollback()
            return {"error": str(e)}
        await session.commit()
        return await serialize_written(session, row)


# ── GLP-1 tools ─────────────────────────────────────────────────────────────

@mcp.tool()
@gated("glp1")
async def log_glp1(
    drug: str,
    dose_mg: float,
    on_date: Optional[str] = None,
    site: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Records a GLP-1 injection (drug name, dose in mg, optional injection site).
    WRITE tool — saved immediately. If a hard conflict rule blocks the save,
    returns ``{"blocked": true, ...}``; call again with ``override=True`` to save
    anyway."""
    from vitals.services import glp1_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")

    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_date,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        try:
            row = await glp1_service.log_injection(
                session, on_date=parsed_date, drug=drug, dose_mg=dose_mg,
                site=site, note=note, source=Source.MCP.value, override=override,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        except ValueError as e:
            # An LLM bypasses the HTML form, so bad input (dose_mg<=0, garbage site)
            # comes back as a clean error instead of an opaque DB failure.
            return {"error": str(e)}
        await session.commit()
        return await serialize_written(session, row)


# ── HRT / TRT tools ─────────────────────────────────────────────────────────

@mcp.tool()
@gated("hrt")
async def get_hrt_logs(
    start_date: Optional[str] = None, end_date: Optional[str] = None, limit: int = 100
) -> dict:
    """Retrieves HRT/TRT dose administrations, side effects, and the active cycle
    with its per-compound plan. Doses/side effects default to the most recent 100.
    READ tool."""
    from vitals.services import hrt_cycle_service, hrt_service

    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        scope = await _mcp_v1_conflict_scope(session)
        scope_kwargs = {"subject_id": scope.subject_id}
        doses = await hrt_service.list_doses(
            session,
            start=start,
            end=end,
            limit=limit,
            **scope_kwargs,
        )
        effects = await hrt_service.list_side_effects(
            session,
            start=start,
            end=end,
            limit=limit,
            **scope_kwargs,
        )
        active = await hrt_cycle_service.active_cycle(session, **scope_kwargs)
        active_cycle = None
        if active is not None:
            active_cycle = serialize_row(active)
            active_cycle["items"] = [serialize_row(it) for it in active.items]

        return {
            "doses": [serialize_row(d) for d in doses],
            "side_effects": [serialize_row(e) for e in effects],
            "active_cycle": active_cycle,
        }


@mcp.tool()
@gated("hrt")
async def log_hrt_dose(
    compound_key: str,
    dose: Optional[float] = None,
    unit: Optional[str] = None,
    volume_ml: Optional[float] = None,
    concentration_mg_ml: Optional[float] = None,
    on_date: Optional[str] = None,
    brand: Optional[str] = None,
    lab: Optional[str] = None,
    batch: Optional[str] = None,
    site: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Records an HRT/TRT administration. ``compound_key`` is a catalog slug (e.g.
    'testosterone_enanthate'). Give either ``dose`` (in ``unit`` — mg/iu/mcg) or a
    ``volume_ml`` with ``concentration_mg_ml`` (or the catalog concentration) to
    compute mg. Grey-market ``brand``/``lab``/``batch`` are optional. WRITE tool —
    on a hard block returns ``{"blocked": true, ...}``; retry with
    ``override=True``."""
    from vitals.services import hrt_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")

    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_date,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        try:
            row = await hrt_service.log_dose(
                session, compound_key=compound_key, on_date=parsed_date, dose=dose,
                unit=unit, volume_ml=volume_ml, concentration_mg_ml=concentration_mg_ml,
                brand=brand, lab=lab, batch=batch, site=site, note=note, override=override,
                source=Source.MCP.value,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        except ValueError as e:
            await session.rollback()
            return {"error": str(e)}
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
@gated("hrt")
async def add_hrt_cycle(
    kind: str,
    start_date: Optional[str] = None,
    name: Optional[str] = None,
    end_date: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Starts an HRT cycle (``kind``: course | pct — put nuance like TRT/blast/
    cruise in ``name``). An open-ended cycle closes the previous open one. WRITE
    tool. Add compounds with ``add_hrt_cycle_item``."""
    from vitals.services import hrt_cycle_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    start = _parse_date(start_date, today_local(), field="start_date")
    end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=start,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        try:
            cycle = await hrt_cycle_service.add_cycle(
                session, kind=kind, start_date=start, name=name, end_date=end, note=note,
                source=Source.MCP.value,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        except ValueError as e:
            await session.rollback()
            return {"error": str(e)}
        await session.commit()
        return await serialize_written(session, cycle)


@mcp.tool()
@gated("hrt")
async def add_hrt_cycle_item(
    cycle_id: int,
    compound_key: str,
    schedule: Optional[list] = None,
    dose: Optional[float] = None,
    interval_days: Optional[float] = None,
    duration_days: Optional[int] = None,
    start_offset_days: Optional[int] = None,
    unit: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Adds a compound plan to a cycle. Pass a full ``schedule`` (a list of
    segments — flat ``{dose, interval_days, duration_days}`` or a linear ramp
    ``{dose_start, dose_end, step, step_every_days, interval_days, duration_days}``)
    for titration/ramps, or the simple ``dose``+``interval_days`` for one flat
    segment. ``start_offset_days`` delays the compound's grid relative to the
    cycle start (week 5 → 28) for staggered courses. WRITE tool."""
    from vitals.services import hrt_cycle_service

    if not schedule:
        if dose is None or interval_days is None:
            return {"error": "provide schedule, or both dose and interval_days"}
        segment: dict = {"dose": dose, "interval_days": interval_days}
        if duration_days:
            segment["duration_days"] = int(duration_days)
        schedule = [segment]

    session_factory = get_session_factory()
    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=today_local(),
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        try:
            item = await hrt_cycle_service.add_cycle_item(
                session, cycle_id, compound_key=compound_key, schedule=schedule,
                unit=unit, start_offset_days=int(start_offset_days or 0), note=note,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        except ValueError as e:
            await session.rollback()
            return {"error": str(e)}
        if item is None:
            return {"error": f"cycle {cycle_id} not found"}
        await session.commit()
        return await serialize_written(session, item)


@mcp.tool()
@gated("hrt")
async def update_hrt_dose(
    dose_id: int,
    compound_key: Optional[str] = None,
    dose: Optional[float] = None,
    unit: Optional[str] = None,
    volume_ml: Optional[float] = None,
    concentration_mg_ml: Optional[float] = None,
    on_date: Optional[str] = None,
    brand: Optional[str] = None,
    lab: Optional[str] = None,
    batch: Optional[str] = None,
    site: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Updates a recorded HRT/TRT administration by ID. Only the fields you pass are
    changed; everything left out keeps its stored value, including the dose's own
    date. A new ``volume_ml`` or ``concentration_mg_ml`` without a ``dose`` recomputes
    the mg. WRITE tool — on a hard block returns ``{"blocked": true, ...}``; retry
    with ``override=True``."""
    from vitals.services import hrt_service

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, field="on_date")

    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_date or today_local(),
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        current = await hrt_service.get_dose_for_update(
            session,
            dose_id,
            identity=conflict_context.identity,
            prepared_conflict_write=prepared,
        )
        if current is None:
            return {"error": f"HRT dose {dose_id} not found"}
        final_date = current.date if parsed_date is None else parsed_date
        if conflict_context.evaluation_date != final_date:
            conflict_context = conflict_engine.ConflictWriteContext(
                identity=conflict_context.identity,
                evaluation_date=final_date,
                legacy_bridge=conflict_context.legacy_bridge,
            )
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
        merged = {
            "compound_key": (
                current.compound_key if compound_key is None else compound_key
            ),
            "dose": current.dose if dose is None else dose,
            "unit": current.unit if unit is None else unit,
            "volume_ml": current.volume_ml if volume_ml is None else volume_ml,
            "concentration_mg_ml": (
                current.concentration_mg_ml
                if concentration_mg_ml is None
                else concentration_mg_ml
            ),
            "brand": current.brand if brand is None else brand,
            "lab": current.lab if lab is None else lab,
            "batch": current.batch if batch is None else batch,
            "site": current.site if site is None else site,
            "note": current.note if note is None else note,
        }
        # A new volume or concentration is a request to recompute the mg, and an
        # explicit dose wins over both — so carrying the stored one forward here
        # would silently ignore what the call actually changed.
        if dose is None and (volume_ml is not None or concentration_mg_ml is not None):
            merged["dose"] = None
        try:
            row = await hrt_service.update_dose(
                session,
                dose_id,
                on_date=final_date,
                override=override,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
                **merged,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        except ValueError as e:
            await session.rollback()
            return {"error": str(e)}
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
@gated("hrt")
async def log_hrt_side_effect(
    effect_type: str,
    severity: int,
    on_date: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Records an HRT/TRT side effect (e.g. "акне", "отёки") with a severity 1–5 for
    a date (default today). Distinct from ``log_side_effect``, which belongs to
    GLP-1. WRITE tool — saved immediately."""
    from vitals.services import hrt_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")

    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_date,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        try:
            row = await hrt_service.log_side_effect(
                session, on_date=parsed_date, effect_type=effect_type,
                severity=severity, note=note,
                source=Source.MCP.value,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        except ValueError as e:
            await session.rollback()
            return {"error": str(e)}
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
@gated("hrt")
async def close_hrt_cycle(cycle_id: int, end_date: Optional[str] = None) -> dict:
    """Closes an HRT cycle by giving it an end date (default today). WRITE tool."""
    from vitals.services import hrt_cycle_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    end = _parse_date(end_date, today_local(), field="end_date")

    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=end,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        try:
            cycle = await hrt_cycle_service.close_cycle(
                session,
                cycle_id,
                end_date=end,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        except ValueError as e:
            await session.rollback()
            return {"error": str(e)}
        if cycle is None:
            return {"error": f"cycle {cycle_id} not found"}
        await session.commit()
        return await serialize_written(session, cycle)


@mcp.tool()
@gated("hrt")
async def get_hrt_cycles() -> dict:
    """Lists all HRT cycles (newest first) with their per-compound plans. READ tool."""
    from vitals.services import hrt_cycle_service

    session_factory = get_session_factory()
    async with session_factory() as session:
        scope = await _mcp_v1_conflict_scope(session)
        cycles = await hrt_cycle_service.list_cycles(
            session,
            subject_id=scope.subject_id,
        )
        out = []
        for c in cycles:
            row = serialize_row(c)
            row["items"] = [serialize_row(it) for it in c.items]
            out.append(row)
        return {"cycles": out}


# ── Skincare tools ──────────────────────────────────────────────────────────

@mcp.tool()
@gated("skincare")
async def log_skincare(
    on_date: Optional[str] = None,
    retinoid: bool = False,
    azelaic: bool = False,
    peel: bool = False,
    niacinamide_spf: bool = False,
    moisturizer: bool = False,
    vitamin_c: bool = False,
    benzoyl_peroxide: bool = False,
    note: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Records or updates the daily skincare routine checklist (one per day, upsert).
    Boolean flags indicate which products were applied. WRITE tool — saved
    immediately. If a hard conflict rule blocks the save, returns
    ``{"blocked": true, ...}``; call again with ``override=True`` to save anyway."""
    from vitals.services import skincare_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")

    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_date,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        try:
            row = await skincare_service.upsert_log(
                session, on_date=parsed_date, retinoid=retinoid, azelaic=azelaic,
                peel=peel, niacinamide_spf=niacinamide_spf, moisturizer=moisturizer,
                vitamin_c=vitamin_c, benzoyl_peroxide=benzoyl_peroxide,
                note=note, source=Source.MCP.value, override=override,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        await session.commit()
        return await serialize_written(session, row)


# ── Body measurement tools ──────────────────────────────────────────────────

@mcp.tool()
async def log_measurement(
    on_date: Optional[str] = None,
    neck_cm: Optional[float] = None,
    waist_cm: Optional[float] = None,
    hips_cm: Optional[float] = None,
    note: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Records body circumference measurements (neck, waist, hips in cm). Upserts
    per date. Auto-computes Navy body-fat % and LBM if weight exists for the date.
    WRITE tool — saved immediately. If a hard conflict rule blocks the save,
    returns ``{"blocked": true, ...}``; call again with ``override=True``."""
    from vitals.services import weight_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")

    async with session_factory() as session:
        conflict_context, prepared = await _mcp_v1_aux_weight_write(
            session,
            evaluation_date=parsed_date,
        )
        try:
            row = await weight_service.upsert_body_measurement(
                session, on_date=parsed_date, neck_cm=neck_cm, waist_cm=waist_cm,
                hips_cm=hips_cm, note=note, source=Source.MCP.value,
                override=override, identity=conflict_context.identity,
                include_legacy_unowned=True, prepared_conflict_write=prepared,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        except ValueError as e:
            await session.rollback()
            return {"error": str(e)}
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
async def get_measurements(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Retrieves body measurements (neck, waist, hips, body-fat %, LBM) for a date
    range. Defaults to the most recent 100 rows."""
    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        scope = await _mcp_v1_conflict_scope(session)
        rows = await weight_service.list_body_measurements(
            session,
            subject_id=scope.subject_id,
            include_legacy_unowned=scope.include_legacy_unowned,
            start=start,
            end=end,
        )
        rows = sorted(rows, key=lambda row: row.date, reverse=True)[:limit]
        return [serialize_row(r) for r in rows]


# ── Notes tools ─────────────────────────────────────────────────────────────

# Domains whose per-row ``note`` field the note tools can read/write, mapped to
# their model. Single source of truth for both log_note and get_notes so the two
# never drift out of sync.
_NOTE_MODELS = {
    "weight": WeightLog,
    "nutrition": MealLog,
    "glp1": Injection,
    "skincare": SkincareLog,
    "measurement": BodyMeasurement,
    "body_comp": BodyScan,
    "labs": LabResult,
}


@mcp.tool()
async def log_note(
    domain: str,
    record_id: int,
    note: str,
) -> dict:
    """Adds or updates the note field on any domain record by its ID.
    Supported domains: weight, nutrition, glp1, skincare, measurement, body_comp, labs.
    WRITE tool — saved immediately."""
    model = _NOTE_MODELS.get(domain)
    if model is None:
        return {"error": f"Unknown domain '{domain}'. Use: {', '.join(_NOTE_MODELS)}"}

    session_factory = get_session_factory()
    async with session_factory() as session:
        if domain == "weight":
            from vitals.services import weight_service

            conflict_context, prepared = await _mcp_v1_weight_write(session)
            row = await weight_service.update_weight_note(
                session,
                record_id,
                note=note,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_weight_write=prepared,
            )
            if row is None:
                return {"error": f"{domain} record {record_id} not found"}
            await session.commit()
            return await serialize_written(session, row)
        if domain == "measurement":
            from vitals.services import weight_service

            conflict_context, prepared = await _mcp_v1_aux_weight_write(session)
            row = await weight_service.update_body_measurement_note(
                session,
                record_id,
                note=note,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_conflict_write=prepared,
            )
            if row is None:
                return {"error": f"{domain} record {record_id} not found"}
            await session.commit()
            return await serialize_written(session, row)
        if domain == "body_comp":
            if not await _module_enabled(session, "body_comp"):
                return {"error": "module 'body_comp' is disabled"}
            from vitals.services import body_scan_service

            conflict_context, prepared = await _mcp_v1_weight_write(session)
            row = await body_scan_service.update_scan_note(
                session,
                record_id,
                note=note,
                identity=conflict_context.identity,
                prepared_weight_write=prepared,
            )
            if row is None:
                return {"error": f"{domain} record {record_id} not found"}
            await session.commit()
            return await serialize_written(session, row)
        if domain == "nutrition":
            if not await _module_enabled(session, "nutrition"):
                return {"error": "module 'nutrition' is disabled"}
            from vitals.services import nutrition_service

            conflict_context = await _mcp_v1_conflict_write_context(session)
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            row = await nutrition_service.update_meal_note(
                session,
                record_id,
                note=note,
                identity=conflict_context.identity,
                include_unowned_legacy=True,
                prepared_conflict_write=prepared,
            )
            if row is None:
                return {"error": f"{domain} record {record_id} not found"}
            await session.commit()
            return await serialize_written(session, row)
        if domain == "skincare":
            if not await _module_enabled(session, "skincare"):
                return {"error": "module 'skincare' is disabled"}
            from vitals.services import skincare_service

            conflict_context = await _mcp_v1_conflict_write_context(session)
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            row = await skincare_service.update_log_note(
                session,
                record_id,
                note=note,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
            if row is None:
                return {"error": f"{domain} record {record_id} not found"}
            await session.commit()
            return await serialize_written(session, row)
        if domain == "glp1":
            if not await _module_enabled(session, "glp1"):
                return {"error": "module 'glp1' is disabled"}
            from vitals.services import glp1_service

            conflict_context = await _mcp_v1_conflict_write_context(session)
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            row = await glp1_service.update_injection_note(
                session,
                record_id,
                note=note,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_conflict_write=prepared,
            )
            if row is None:
                return {"error": f"{domain} record {record_id} not found"}
            await session.commit()
            return await serialize_written(session, row)
        if domain == "labs":
            from vitals.services import labs_service

            conflict_context = await _mcp_v1_conflict_write_context(session)
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            row = await labs_service.update_result_note(
                session,
                record_id,
                note=note,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_conflict_write=prepared,
            )
            if row is None:
                return {"error": f"{domain} record {record_id} not found"}
            await session.commit()
            return await serialize_written(session, row)
        row = await session.get(model, record_id)
        if row is None:
            return {"error": f"{domain} record {record_id} not found"}
        row.note = note
        await session.flush()
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
async def get_notes(
    domain: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Retrieves records that have non-empty notes, optionally filtered by domain
    and date range. Returns records from: weight, nutrition, glp1, skincare,
    measurement, body_comp, labs."""
    if domain and domain not in _NOTE_MODELS:
        return [{"error": f"Unknown domain '{domain}'. Use: {', '.join(_NOTE_MODELS)}"}]

    targets = (
        {domain: _NOTE_MODELS[domain]}
        if domain
        else dict(_NOTE_MODELS)
    )
    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")

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
            weight_scope = await _mcp_v1_conflict_scope(session)
        if "measurement" in targets:
            measurement_scope = weight_scope or await _mcp_v1_conflict_scope(session)
        if "nutrition" in targets:
            if not await _module_enabled(session, "nutrition"):
                if domain == "nutrition":
                    return [{"error": "module 'nutrition' is disabled"}]
                targets.pop("nutrition")
            else:
                nutrition_scope = await _mcp_v1_conflict_scope(session)
        if "skincare" in targets:
            if not await _module_enabled(session, "skincare"):
                if domain == "skincare":
                    return [{"error": "module 'skincare' is disabled"}]
                targets.pop("skincare")
            else:
                skincare_scope = await _mcp_v1_conflict_scope(session)
        if "glp1" in targets:
            if not await _module_enabled(session, "glp1"):
                if domain == "glp1":
                    return [{"error": "module 'glp1' is disabled"}]
                targets.pop("glp1")
            else:
                glp1_scope = await _mcp_v1_conflict_scope(session)
        if "labs" in targets:
            labs_scope = await _mcp_v1_conflict_scope(session)
        if "body_comp" in targets:
            if not await _module_enabled(session, "body_comp"):
                if domain == "body_comp":
                    return [{"error": "module 'body_comp' is disabled"}]
                targets.pop("body_comp")
            else:
                body_comp_scope = await _mcp_v1_conflict_scope(session)
        for d_name, model in targets.items():
            if d_name == "weight":
                assert weight_scope is not None
                from vitals.services import weight_service

                rows = await weight_service.list_weight_notes(
                    session,
                    subject_id=weight_scope.subject_id,
                    include_legacy_unowned=(
                        weight_scope.include_legacy_unowned
                    ),
                    start=start,
                    end=end,
                    limit=limit,
                )
                for row in rows:
                    entry = serialize_row(row)
                    entry["_domain"] = d_name
                    results.append(entry)
                continue
            if d_name == "nutrition":
                assert nutrition_scope is not None
                from vitals.services import nutrition_service

                rows = await nutrition_service.list_meals(
                    session,
                    start=start,
                    end=end,
                    subject_id=nutrition_scope.subject_id,
                    include_unowned_legacy=(
                        nutrition_scope.include_legacy_unowned
                    ),
                    has_note=True,
                    limit=limit,
                )
                for row in rows:
                    entry = serialize_row(row)
                    entry["_domain"] = d_name
                    results.append(entry)
                continue
            if d_name == "measurement":
                assert measurement_scope is not None
                from vitals.services import weight_service

                rows = await weight_service.list_body_measurements(
                    session,
                    subject_id=measurement_scope.subject_id,
                    include_legacy_unowned=(
                        measurement_scope.include_legacy_unowned
                    ),
                    start=start,
                    end=end,
                    has_note=True,
                )
                for row in rows:
                    entry = serialize_row(row)
                    entry["_domain"] = d_name
                    results.append(entry)
                continue
            if d_name == "skincare":
                assert skincare_scope is not None
                from vitals.services import skincare_service

                rows = await skincare_service.list_logs(
                    session,
                    subject_id=skincare_scope.subject_id,
                    start=start,
                    end=end,
                    has_note=True,
                    limit=limit,
                )
                for row in rows:
                    entry = serialize_row(row)
                    entry["_domain"] = d_name
                    results.append(entry)
                continue
            if d_name == "glp1":
                assert glp1_scope is not None
                from vitals.services import glp1_service

                rows = await glp1_service.list_injections(
                    session,
                    subject_id=glp1_scope.subject_id,
                    include_legacy_unowned=(
                        glp1_scope.include_legacy_unowned
                    ),
                    start=start,
                    end=end,
                    has_note=True,
                    limit=limit,
                )
                for row in rows:
                    entry = serialize_row(row)
                    entry["_domain"] = d_name
                    results.append(entry)
                continue
            if d_name == "labs":
                assert labs_scope is not None
                from vitals.services import labs_service

                rows = await labs_service.list_results(
                    session,
                    subject_id=labs_scope.subject_id,
                    include_legacy_unowned=(
                        labs_scope.include_legacy_unowned
                    ),
                    start=start,
                    end=end,
                    has_note=True,
                    limit=limit,
                )
                for row in rows:
                    entry = serialize_row(row)
                    entry["_domain"] = d_name
                    results.append(entry)
                continue
            if d_name == "body_comp":
                assert body_comp_scope is not None
                from vitals.services import body_scan_service

                rows = await body_scan_service.list_scans(
                    session,
                    subject_id=body_comp_scope.subject_id,
                    start=start,
                    end=end,
                )
                for row in (r for r in rows if r.note):
                    entry = serialize_row(row)
                    entry["_domain"] = d_name
                    results.append(entry)
                continue
            stmt = select(model).where(model.note.isnot(None), model.note != "")
            if start:
                stmt = stmt.where(model.date >= start)
            if end:
                stmt = stmt.where(model.date <= end)
            stmt = stmt.order_by(model.date.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            for r in rows:
                entry = serialize_row(r)
                entry["_domain"] = d_name
                results.append(entry)

    results.sort(key=lambda x: x.get("date", ""), reverse=True)
    return results[:limit]


# ── Deletion (one tool, every domain) ─────────────────────────────────────────

# domain → (module key gating the write, service module, delete function).
# Every delete service happens to share one signature — ``(session, id) -> bool`` —
# which is what lets a single tool stand in for the eighteen near-identical ones
# that used to live here, each differing only in the noun it echoed back. The tool
# list is re-read at the top of every conversation, so a fifth of it was spent
# spelling out delete_meal / delete_glp1 / delete_hrt_cycle_item.
_DELETE_TARGETS: dict[str, tuple[Optional[str], str, str]] = {
    "weight": (None, "weight_service", "delete_weight_log"),
    "measurement": (None, "weight_service", "delete_body_measurement"),
    "noise_marker": (None, "weight_service", "delete_noise_marker"),
    "labs": (None, "labs_service", "delete_result"),
    "milestones": (None, "milestones_service", "delete_milestone"),
    "nutrition": ("nutrition", "nutrition_service", "delete_meal"),
    "glp1": ("glp1", "glp1_service", "delete_injection"),
    "glp1_side_effect": ("glp1", "glp1_service", "delete_side_effect"),
    "glp1_dose_phase": ("glp1", "glp1_service", "delete_dose_phase"),
    "hrt_dose": ("hrt", "hrt_service", "delete_dose"),
    "hrt_side_effect": ("hrt", "hrt_service", "delete_side_effect"),
    "hrt_cycle": ("hrt", "hrt_cycle_service", "delete_cycle"),
    "hrt_cycle_item": ("hrt", "hrt_cycle_service", "delete_cycle_item"),
    "body_comp": ("body_comp", "body_scan_service", "delete_scan"),
    "timeline": ("timeline", "timeline_service", "delete_annotation"),
    "skincare_observation": ("skincare", "skincare_service", "delete_observation"),
    "supplements": ("supplements", "supplements_service", "delete_supplement"),
    "genetics": ("genetics", "genetics_service", "delete_variant"),
    "signals": ("signals", "signals_service", "delete_signal"),
}


@mcp.tool()
async def delete_record(domain: str, record_id: int) -> dict:
    """Deletes one record from any domain by its ID. WRITE tool — immediate.

    ``domain`` is one of: weight, measurement (body tape), noise_marker, labs (one
    result), milestones (a goal card), nutrition (a meal), glp1 (an injection),
    glp1_side_effect, glp1_dose_phase, hrt_dose, hrt_side_effect, hrt_cycle
    (with its compound plans), hrt_cycle_item (one plan, cycle kept), body_comp
    (a scan with its metrics), timeline (a manual event), skincare_observation,
    supplements (a catalog entry), genetics (a variant), signals (one parsed
    signal — the raw message stays in the lake; for a whole batch parsed wrongly
    out of one message use ``mark_signal_misparse`` instead).

    Deleting a weight log reactivates the next-highest-priority log for that date.
    Returns ``{"deleted": false, ...}`` when nothing has that id."""
    target = _DELETE_TARGETS.get(domain)
    if target is None:
        return {"error": f"Unknown domain '{domain}'. Use: {', '.join(_DELETE_TARGETS)}"}
    module_key, service_name, fn_name = target

    session_factory = get_session_factory()
    async with session_factory() as session:
        if module_key and not await _module_enabled(session, module_key):
            return {"error": f"module '{module_key}' is disabled"}
        service = importlib.import_module(f"vitals.services.{service_name}")
        owned_kwargs = {}
        if domain == "weight":
            from vitals.services import weight_service

            conflict_context, prepared = await _mcp_v1_weight_write(session)
            owned_kwargs = {
                "identity": conflict_context.identity,
                "include_legacy_unowned": True,
                "prepared_weight_write": prepared,
            }
        elif domain in {"measurement", "noise_marker"}:
            conflict_context, prepared = await _mcp_v1_aux_weight_write(session)
            owned_kwargs = {
                "identity": conflict_context.identity,
                "include_legacy_unowned": True,
                "prepared_conflict_write": prepared,
            }
        elif domain == "body_comp":
            conflict_context, prepared = await _mcp_v1_weight_write(session)
            owned_kwargs = {
                "subject_id": conflict_context.identity.subject_id,
                "identity": conflict_context.identity,
                "prepared_weight_write": prepared,
            }
        elif domain == "milestones":
            conflict_context = await _mcp_v1_conflict_write_context(session)
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            owned_kwargs = {
                "identity": conflict_context.identity,
                "prepared_conflict_write": prepared,
            }
        elif domain == "supplements":
            ownership = await _mcp_v1_legacy_owner(session)
            owned_kwargs = {"identity": ownership.owner_action()}
        elif domain == "timeline":
            ownership = await _mcp_v1_legacy_owner(session)
            owned_kwargs = {
                "identity": ownership.owner_action(),
            }
        elif domain == "nutrition":
            conflict_context = await _mcp_v1_conflict_write_context(session)
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            owned_kwargs = {
                "identity": conflict_context.identity,
                "include_unowned_legacy": True,
                "include_legacy_unowned": True,
                "prepared_conflict_write": prepared,
            }
        elif domain == "labs":
            conflict_context = await _mcp_v1_conflict_write_context(session)
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            owned_kwargs = {
                "identity": conflict_context.identity,
                "include_legacy_unowned": True,
                "prepared_conflict_write": prepared,
            }
        elif domain == "genetics":
            conflict_context = await _mcp_v1_conflict_write_context(session)
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            owned_kwargs = {
                "identity": conflict_context.identity,
                "prepared_conflict_write": prepared,
            }
        elif domain == "skincare_observation":
            conflict_context = await _mcp_v1_conflict_write_context(session)
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            owned_kwargs = {
                "identity": conflict_context.identity,
                "prepared_conflict_write": prepared,
            }
        elif domain in {"glp1", "glp1_side_effect", "glp1_dose_phase"}:
            conflict_context = await _mcp_v1_conflict_write_context(session)
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            owned_kwargs = {
                "identity": conflict_context.identity,
                "include_legacy_unowned": True,
                "prepared_conflict_write": prepared,
            }
        elif domain in {
            "hrt_dose",
            "hrt_side_effect",
            "hrt_cycle",
            "hrt_cycle_item",
        }:
            conflict_context = await _mcp_v1_conflict_write_context(session)
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            owned_kwargs = {
                "identity": conflict_context.identity,
                "prepared_conflict_write": prepared,
            }
        elif domain == "signals":
            ownership = await _mcp_v1_legacy_owner(session)
            owned_kwargs = {"subject_id": ownership.subject_id}
        ok = await getattr(service, fn_name)(session, record_id, **owned_kwargs)
        if domain == "body_comp" and ok:
            await service.refresh_alerts(
                session,
                subject_id=conflict_context.identity.subject_id,
                identity=conflict_context.identity,
                prepared_weight_write=prepared,
            )
        await session.commit()
        return {"deleted": ok, "domain": domain, "record_id": record_id}


# ── Body composition tools (InBody / МедАсс — optional module) ────────────────
def _serialize_scan(scan: BodyScan) -> dict:
    """A scan plus its metrics nested (relationship must be loaded already)."""
    d = serialize_row(scan)
    d["metrics"] = [serialize_row(m) for m in scan.metrics]
    return d


@mcp.tool()
async def get_body_scans(
    start_date: Optional[str] = None, end_date: Optional[str] = None, limit: int = 100
) -> list[dict]:
    """Retrieves body-composition scans (InBody / МедАсс) with every parsed metric
    (skeletal muscle, body water, visceral fat, segmental analysis, phase angle…).
    Defaults to the most recent 100 scans."""
    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        from vitals.services import body_scan_service

        if not await _module_enabled(session, "body_comp"):
            return [{"error": "module 'body_comp' is disabled"}]
        scope = await _mcp_v1_conflict_scope(session)
        scans = await body_scan_service.list_scans(
            session,
            start=start,
            end=end,
            subject_id=scope.subject_id,
        )
        return [_serialize_scan(s) for s in scans[:limit]]


@mcp.tool()
async def get_body_scan(scan_id: int) -> dict:
    """Retrieves a single body-composition scan with its full metric sheet."""
    from vitals.services import body_scan_service

    session_factory = get_session_factory()
    async with session_factory() as session:
        if not await _module_enabled(session, "body_comp"):
            return {"error": "module 'body_comp' is disabled"}
        scope = await _mcp_v1_conflict_scope(session)
        scan = await body_scan_service.get_scan(
            session,
            scan_id,
            subject_id=scope.subject_id,
        )
        if scan is None:
            return {"error": f"Body scan {scan_id} not found"}
        return _serialize_scan(scan)


@mcp.tool()
async def get_body_metric_history(
    metric_key: str,
    segment: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Time series for one body-composition metric (e.g. ``skeletal_muscle_mass``,
    ``phase_angle``, ``visceral_fat_area``), optionally for a single body segment."""
    from vitals.services import body_scan_service

    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")
    async with session_factory() as session:
        if not await _module_enabled(session, "body_comp"):
            return [{"error": "module 'body_comp' is disabled"}]
        scope = await _mcp_v1_conflict_scope(session)
        return await body_scan_service.metric_history(
            session,
            metric_key,
            segment=segment,
            start=start,
            end=end,
            subject_id=scope.subject_id,
        )


@mcp.tool()
@gated("body_comp")
async def log_body_scan(
    metrics: list[dict],
    on_date: Optional[str] = None,
    device: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Records a body-composition scan from structured metrics (no photo needed).

    Each metric is ``{"label" or "metric_key": str, "value": number, "unit": str?,
    "ref_low": number?, "ref_high": number?, "segment": str?}``. The scan's weight /
    body-fat% / LBM are bridged into the weight domain. WRITE tool — saved
    immediately. No-op with an error if the body_comp module is disabled. If a hard
    conflict rule blocks the save, returns ``{"blocked": true, ...}``; call again
    with ``override=True``."""
    from vitals.services import body_scan_service, weight_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")

    async with session_factory() as session:
        extracted = {
            "date": parsed_date.isoformat(),
            "device": device,
            "note": note,
            "metrics": metrics,
            "override": override,
        }
        try:
            conflict_context, prepared_weight_write = await _mcp_v1_weight_write(
                session,
                evaluation_date=parsed_date,
            )
            from vitals.services import raw_payload_service

            raw = await raw_payload_service.upsert_owned_raw_payload(
                session,
                identity=conflict_context.identity,
                integration_connection_id=None,
                file_asset_id=None,
                domain=Domain.BODY_COMPOSITION.value,
                source=Source.MCP.value,
                external_id=f"mcp:{uuid.uuid4().hex}",
                payload=extracted,
            )
            scan = await body_scan_service.ingest_structured_scan(
                session,
                extracted,
                raw_payload=raw,
                identity=conflict_context.identity,
                prepared_weight_write=prepared_weight_write,
                override=override,
            )
            await body_scan_service.refresh_alerts(
                session,
                subject_id=conflict_context.identity.subject_id,
                on_date=parsed_date,
                identity=conflict_context.identity,
                prepared_weight_write=prepared_weight_write,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        except ValueError as e:
            await session.rollback()
            return {"error": str(e)}
        await session.commit()
        full = await body_scan_service.get_scan(
            session,
            scan.id,
            subject_id=conflict_context.identity.subject_id,
        )
        return _serialize_scan(full) if full else {"scan_id": scan.id}


# ── Labs tools ──────────────────────────────────────────────────────────────
@mcp.tool()
async def get_lab_results(
    marker: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Retrieves lab results (biomarker, value, unit, reference range, computed
    out-of-range flag), optionally filtered by marker name and/or date range
    (YYYY-MM-DD). Defaults to the most recent 100 rows across all markers."""
    from vitals.services import labs_service

    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        scope = await _mcp_v1_conflict_scope(session)
        results = await labs_service.list_results(
            session,
            marker=marker,
            start=start,
            end=end,
            limit=limit,
            subject_id=scope.subject_id,
            include_legacy_unowned=scope.include_legacy_unowned,
        )
        return [serialize_row(r) for r in results]


@mcp.tool()
async def log_lab_result(
    marker: str,
    value: float,
    on_date: Optional[str] = None,
    unit: Optional[str] = None,
    ref_low: Optional[float] = None,
    ref_high: Optional[float] = None,
    lab_name: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Records a single lab marker value (one biomarker from a blood/urine test).
    The out-of-range flag is computed automatically; a range left out here falls
    back to the marker's catalog range if one is already on file. WRITE tool —
    saved immediately. Defaults: on_date = today. A hard conflict rule (e.g. a
    hyperkalemic potassium result while a potassium supplement is active) returns
    ``{"blocked": true, ...}``; retry with ``override=True`` to save anyway."""
    from vitals.services import labs_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")

    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_date,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        try:
            from vitals.services import raw_payload_service

            raw = await raw_payload_service.upsert_owned_raw_payload(
                session,
                identity=conflict_context.identity,
                integration_connection_id=None,
                file_asset_id=None,
                domain=Domain.LABS.value,
                source=Source.MCP.value,
                external_id=f"mcp:{uuid.uuid4().hex}",
                payload={
                    "date": parsed_date.isoformat(),
                    "marker": marker,
                    "value": value,
                    "unit": unit,
                    "ref_low": ref_low,
                    "ref_high": ref_high,
                    "lab_name": lab_name,
                    "note": note,
                    "override": override,
                },
            )
            row = await labs_service.add_result(
                session,
                on_date=parsed_date,
                marker=marker,
                value=value,
                unit=unit,
                ref_low=ref_low,
                ref_high=ref_high,
                lab_name=lab_name,
                note=note,
                source=Source.MCP.value,
                raw_payload_id=raw.id,
                override=override,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_conflict_write=prepared,
            )
            raw.processed_at = now_local()
            await session.flush()
            await labs_service.refresh_alerts(
                session,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        except ValueError as e:
            await session.rollback()
            return {"error": str(e)}
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
async def update_lab_result(
    result_id: int,
    value: Optional[float] = None,
    marker: Optional[str] = None,
    on_date: Optional[str] = None,
    unit: Optional[str] = None,
    ref_low: Optional[float] = None,
    ref_high: Optional[float] = None,
    lab_name: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Corrects an existing lab result by ID — a mistyped value, a range read off
    the wrong column. Only the fields you pass are changed; the out-of-range flag
    is recomputed and the alerts derived from it refreshed. Use this instead of
    delete + re-add: a measurement is never thrown away here. WRITE tool."""
    from vitals.services import labs_service

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, field="on_date")

    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_date or today_local(),
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        current = await labs_service.get_result_for_update(
            session,
            result_id,
            identity=conflict_context.identity,
            include_legacy_unowned=True,
            prepared_conflict_write=prepared,
        )
        if current is None:
            return {"error": f"Lab result {result_id} not found"}
        final_date = parsed_date or current.date
        if final_date != conflict_context.evaluation_date:
            conflict_context = await _mcp_v1_conflict_write_context(
                session,
                evaluation_date=final_date,
            )
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
        try:
            row = await labs_service.update_result(
                session,
                result_id,
                on_date=parsed_date,
                marker=marker,
                value=value,
                unit=unit,
                ref_low=ref_low,
                ref_high=ref_high,
                lab_name=lab_name,
                note=note,
                override=override,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_conflict_write=prepared,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        except ValueError as e:
            await session.rollback()
            return {"error": str(e)}
        if row is None:
            return {"error": f"Lab result {result_id} not found"}
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
async def log_lab_results(
    results: list[dict],
    on_date: Optional[str] = None,
    lab_name: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Records every marker from one lab report at once (e.g. a full blood panel
    read from a photo/PDF shared in the conversation) — the natural way to push a
    whole report in one call instead of calling log_lab_result per marker.

    Each item in ``results`` is ``{"marker": str, "value": number, "unit": str?,
    "ref_low": number?, "ref_high": number?}``. Identical (date, marker, value)
    rows are deduped, so retrying a call is safe. The verbatim payload is kept in
    raw_payloads, same as a document uploaded through the web UI. WRITE tool —
    saved immediately. Defaults: on_date = today. A hard conflict rule on any
    marker in the panel returns ``{"blocked": true, ...}`` and saves nothing;
    retry with ``override=True`` to save the whole panel anyway."""
    from vitals.services import labs_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")

    async with session_factory() as session:
        extracted = {
            "date": parsed_date.isoformat(),
            "lab_name": lab_name,
            "results": results,
        }
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_date,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        from vitals.services import raw_payload_service

        raw = await raw_payload_service.upsert_owned_raw_payload(
            session,
            identity=conflict_context.identity,
            integration_connection_id=None,
            file_asset_id=None,
            domain=Domain.LABS.value,
            source=Source.MCP.value,
            external_id=f"mcp:{uuid.uuid4().hex}",
            payload=extracted,
        )
        try:
            summary = await labs_service.ingest_structured_results(
                session,
                extracted,
                raw_payload=raw,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
                override=override,
            )
            await labs_service.refresh_alerts(
                session,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        except ValueError as e:
            await session.rollback()
            return {"error": str(e)}
        await session.commit()
        return {
            "created": summary["created"],
            "skipped": summary["skipped"],
            "results": [await serialize_written(session, r) for r in summary["results"]],
        }


# ── Timeline tools ───────────────────────────────────────────────────────────
@mcp.tool()
async def get_timeline(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    domain: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Retrieves the cross-domain event feed — manual annotations (trips,
    illness, protocol changes) plus derived events (GLP-1 dose changes, lab
    draws, BIA scans, achieved milestones, noisy weight periods), newest first.
    Optionally filtered by date range (YYYY-MM-DD) and/or domain (weight, glp1,
    garmin, workouts, labs, nutrition, skincare, supplements, genetics,
    body_comp, or "timeline" for global flags)."""
    from vitals.services import timeline_service

    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")
    domains = [domain] if domain else None

    async with session_factory() as session:
        scope = await _mcp_v1_conflict_scope(session)
        events = await timeline_service.list_events(
            session,
            subject_id=scope.subject_id,
            include_legacy_unowned=scope.include_legacy_unowned,
            domains=domains,
            start=start,
            end=end,
            limit=limit,
        )
        return [e.to_dict() for e in events]


@mcp.tool()
@gated("timeline")
async def log_event(
    title: str,
    on_date: Optional[str] = None,
    end_date: Optional[str] = None,
    kind: str = "note",
    domain: str = "timeline",
    note: Optional[str] = None,
) -> dict:
    """Records a manual Timeline annotation — a flag shown on every chart and
    in the event feed (a trip, an illness, a protocol change, a free-form
    note). ``kind`` is one of: life_event, illness, travel, protocol_change,
    note. ``domain`` scopes the flag to one chart (weight, glp1, ...) or
    "timeline" (default) to show it on every chart. ``end_date`` makes it a
    range (e.g. a week-long trip); omit it for a single-day event. WRITE tool —
    saved immediately. No-op with an error if the timeline module is disabled."""
    from vitals.services import timeline_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")
    parsed_end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        ownership = await _mcp_v1_legacy_owner(session)
        row = await timeline_service.create_annotation(
            session,
            title=title,
            on_date=parsed_date,
            end_date=parsed_end,
            kind=kind,
            domain=domain,
            note=note,
            source=Source.MCP.value,
            identity=ownership.owner_action(),
        )
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
@gated("timeline")
async def update_event(
    event_id: int,
    title: Optional[str] = None,
    on_date: Optional[str] = None,
    end_date: Optional[str] = None,
    kind: Optional[str] = None,
    domain: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Updates a manual Timeline annotation by ID — the ``id`` of a row from
    ``get_timeline`` whose source is manual (derived events are computed and
    cannot be edited). Only the fields you pass are changed; everything left out
    keeps its stored value, including the event's own date. WRITE tool."""
    from vitals.services import timeline_service

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, field="on_date")
    parsed_end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        ownership = await _mcp_v1_legacy_owner(session)
        current = await timeline_service.get_annotation(
            session,
            event_id,
            subject_id=ownership.subject_id,
            include_legacy_unowned=True,
        )
        if current is None:
            return {"error": f"Event {event_id} not found"}
        merged = {
            "title": current.title if title is None else title,
            "date": current.date if parsed_date is None else parsed_date,
            "end_date": current.end_date if parsed_end is None else parsed_end,
            "kind": current.kind if kind is None else kind,
            "domain": current.domain if domain is None else domain,
            "note": current.note if note is None else note,
        }
        row = await timeline_service.update_annotation(
            session,
            event_id,
            on_date=merged.pop("date"),
            identity=ownership.owner_action(),
            include_legacy_unowned=True,
            **merged,
        )
        await session.commit()
        return await serialize_written(session, row)


# ── Cross-domain + whole-lake tools ──────────────────────────────────────────
@mcp.tool()
async def get_full_snapshot(
    on_date: Optional[str] = None,
    period_days: int = 7,
) -> dict:
    """Returns context-v2 for a closed period (1..90 days): profile, coverage,
    weight/body composition, GLP-1/HRT plans and facts, every lab result in the
    period, Garmin recovery and activities, Hevy, nutrition, skincare, signals,
    timeline and active goals. Every dated fact is bounded by the effective
    period end. When ``on_date`` is today the closed period ends yesterday."""
    from vitals.services import digest_service

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, field="on_date")
    async with session_factory() as session:
        scope = await _mcp_v1_composition_scope(session)
        try:
            return await digest_service.assemble_context(
                session,
                subject_id=scope.subject_id,
                on_date=parsed_date,
                period_days=period_days,
            )
        except ValueError as exc:
            return {"error": str(exc)}


EXPORT_DEFAULT_DAYS = 90


@mcp.tool()
async def export_everything(
    domains: Optional[list[str]] = None, since: Optional[str] = None
) -> dict:
    """Returns the health history as one compact, secret-free, LLM-ready export
    grouped by domain (weight, measurements, body scans, GLP-1, HRT, labs, Garmin,
    workouts, nutrition, skincare, supplements, genetics, signals, day context,
    milestones, timeline). This is the way to read long-term history in a single
    call rather than paging each domain's newest-100 read tool. Read-only.

    Defaults to the **last 90 days**: the whole lake is years of daily Garmin rows
    with per-minute sleep and would fill the conversation before the question is
    asked. Widen deliberately — ``since="2020-01-01"`` (any early date) for the
    entire history, and/or ``domains=["biomarkers", "weight_history"]`` to pull a
    couple of areas in full instead of everything. Unknown domain names are
    rejected with the list of valid ones."""
    from vitals.services import data_portability_service
    from vitals.utils.timeutils import today_local

    default_since = today_local() - timedelta(days=EXPORT_DEFAULT_DAYS)
    cutoff = _parse_date(since, default_since, field="since")

    session_factory = get_session_factory()
    async with session_factory() as session:
        await _mcp_v1_composition_scope(session)
        try:
            return await data_portability_service.export_llm(
                session, domains=domains, since=cutoff
            )
        except ValueError as e:
            return {"error": str(e)}


@mcp.tool()
async def get_data_overview() -> dict:
    """Returns a per-domain map of what data exists: row count, earliest and latest
    date, and last-updated timestamp for each domain. Call this first to orient —
    it tells you the real date coverage and density before you query a domain, so
    you don't page blindly through empty or out-of-range windows. Read-only."""
    # Dated log/metric tables: report count + min/max of their date column.
    dated = [
        ("weight", WeightLog, WeightLog.date),
        ("measurements", BodyMeasurement, BodyMeasurement.date),
        ("body_scans", BodyScan, BodyScan.date),
        ("glp1_injections", Injection, Injection.date),
        ("side_effects", SideEffect, SideEffect.date),
        ("garmin_daily", GarminDaily, GarminDaily.date),
        ("garmin_activities", GarminActivity, GarminActivity.date),
        ("garmin_intraday", GarminIntraday, GarminIntraday.date),
        ("workouts", HevyWorkout, HevyWorkout.date),
        ("labs", LabResult, LabResult.date),
        ("nutrition", MealLog, MealLog.date),
        ("skincare_logs", SkincareLog, SkincareLog.date),
        ("skincare_observations", SkincareObservation, SkincareObservation.date),
        ("weekly_digests", WeeklyDigest, WeeklyDigest.date),
        ("timeline", Annotation, Annotation.date),
        ("noise_markers", NoiseMarker, NoiseMarker.start_date),
        ("signals", Signal, Signal.date),
        ("day_context", DayContext, DayContext.date),
        ("hrt_doses", HrtDose, HrtDose.date),
        ("hrt_side_effects", HrtSideEffect, HrtSideEffect.date),
        ("hrt_cycles", HrtCycle, HrtCycle.start_date),
    ]
    # Config/catalog tables have no per-day date — report count only.
    count_only = [
        ("supplements", Supplement),
        ("genetics", GeneticVariant),
        ("milestones", Milestone),
        ("dose_phases", DosePhase),
    ]

    session_factory = get_session_factory()
    overview: dict = {}
    async with session_factory() as session:
        await _mcp_v1_composition_scope(session)
        for name, model, date_col in dated:
            cols = [func.count(), func.min(date_col), func.max(date_col)]
            updated_col = getattr(model, "updated_at", None)
            if updated_col is not None:
                cols.append(func.max(updated_col))
            row = (await session.execute(select(*cols))).one()
            entry = {
                "count": row[0],
                "earliest": row[1].isoformat() if row[1] else None,
                "latest": row[2].isoformat() if row[2] else None,
            }
            if updated_col is not None:
                entry["last_updated"] = row[3].isoformat() if row[3] else None
            overview[name] = entry

        for name, model in count_only:
            count = (await session.execute(select(func.count()).select_from(model))).scalar_one()
            overview[name] = {"count": count}

    return overview


# ── Milestones / goals tools ──────────────────────────────────────────────────
_MILESTONE_STATUSES = {s.value for s in MilestoneStatus}


@mcp.tool()
async def get_milestones(status: Optional[str] = None) -> list[dict]:
    """Returns goal cards with live progress (current value, remaining, days left)
    computed for weight/body-comp goals. Optionally filtered by ``status`` (active,
    achieved, missed, paused). Read-only."""
    from vitals.services import milestones_service

    session_factory = get_session_factory()
    async with session_factory() as session:
        scope = await _mcp_v1_conflict_scope(session)
        rows = await milestones_service.list_milestones(
            session,
            status=status,
            subject_id=scope.subject_id,
        )
        return [
            await milestones_service.progress(
                session,
                milestone,
                subject_id=scope.subject_id,
            )
            for milestone in rows
        ]


@mcp.tool()
async def create_milestone(
    name: str,
    domain: str = Domain.WEIGHT.value,
    target_value: Optional[float] = None,
    target_unit: Optional[str] = None,
    deadline: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Creates a goal card (e.g. "reach 85 kg by 2026-12-31"). ``domain`` is the
    related health area (weight, glp1, labs, body_comp, ...); ``deadline`` is
    YYYY-MM-DD. WRITE tool — saved immediately."""
    from vitals.services import milestones_service

    session_factory = get_session_factory()
    parsed_deadline = _parse_date(deadline, field="deadline")
    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(session)
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        row = await milestones_service.create_milestone(
            session, name=name, domain=domain, target_value=target_value,
            target_unit=target_unit, deadline=parsed_deadline, note=note,
            identity=conflict_context.identity,
            prepared_conflict_write=prepared,
        )
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
async def update_milestone(
    milestone_id: int,
    name: Optional[str] = None,
    domain: Optional[str] = None,
    target_value: Optional[float] = None,
    target_unit: Optional[str] = None,
    deadline: Optional[str] = None,
    status: Optional[str] = None,
    note: Optional[str] = None,
    clear_fields: Optional[list[str]] = None,
) -> dict:
    """Updates a goal card by ID. Only the fields you pass are changed. Use
    ``status`` to mark a goal achieved/missed/paused/active. To remove an
    optional value, name it in ``clear_fields`` (target_value, target_unit,
    deadline, or note). WRITE tool."""
    from vitals.services import milestones_service

    nullable_fields = {"target_value", "target_unit", "deadline", "note"}
    clear = set(clear_fields or ())
    unknown = clear.difference(nullable_fields)
    if unknown:
        return {
            "error": "clear_fields contains unknown fields: "
            + ", ".join(sorted(unknown))
        }
    supplied = {
        "target_value": target_value,
        "target_unit": target_unit,
        "deadline": deadline,
        "note": note,
    }
    overlapping = sorted(field for field in clear if supplied[field] is not None)
    if overlapping:
        return {
            "error": "fields cannot be set and cleared together: "
            + ", ".join(overlapping)
        }
    if status is not None and status not in _MILESTONE_STATUSES:
        return {"error": f"Unknown status '{status}'. Use: {', '.join(sorted(_MILESTONE_STATUSES))}"}

    session_factory = get_session_factory()
    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(session)
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        kwargs: dict = {}
        if name is not None:
            kwargs["name"] = name
        if domain is not None:
            kwargs["domain"] = domain
        if target_value is not None:
            kwargs["target_value"] = target_value
        if target_unit is not None:
            kwargs["target_unit"] = target_unit
        if deadline is not None:
            kwargs["deadline"] = _parse_date(deadline, field="deadline")
        if status is not None:
            kwargs["status"] = status
        if note is not None:
            kwargs["note"] = note
        for field in clear:
            kwargs[field] = None
        row = await milestones_service.update_milestone(
            session,
            milestone_id,
            identity=conflict_context.identity,
            prepared_conflict_write=prepared,
            **kwargs,
        )
        if row is None:
            return {"error": f"Milestone {milestone_id} not found"}
        await session.commit()
        return await serialize_written(session, row)


# ── GLP-1 write completeness (edit/delete injection, side effects, phases) ────
@mcp.tool()
@gated("glp1")
async def update_glp1(
    injection_id: int,
    drug: Optional[str] = None,
    dose_mg: Optional[float] = None,
    on_date: Optional[str] = None,
    site: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Edits an existing GLP-1 injection by ID. Only the fields you pass are
    changed; ``on_date`` left out keeps the injection's own date. Runs the same
    conflict gate as a fresh log — on a hard block returns ``{"blocked": true,
    ...}``; retry with ``override=True``. WRITE tool."""
    from vitals.services import glp1_service

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, field="on_date")
    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_date or today_local(),
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        current = await glp1_service.get_injection_for_update(
            session,
            injection_id,
            identity=conflict_context.identity,
            include_legacy_unowned=True,
            prepared_conflict_write=prepared,
        )
        if current is None:
            return {"error": f"Injection {injection_id} not found"}
        final_date = current.date if parsed_date is None else parsed_date
        if conflict_context.evaluation_date != final_date:
            conflict_context = conflict_engine.ConflictWriteContext(
                identity=conflict_context.identity,
                evaluation_date=final_date,
                legacy_bridge=conflict_context.legacy_bridge,
            )
            prepared = await conflict_engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
        merged = {
            "drug": current.drug if drug is None else drug,
            "dose_mg": current.dose_mg if dose_mg is None else dose_mg,
            "site": current.site if site is None else site,
            "note": current.note if note is None else note,
        }
        try:
            row = await glp1_service.update_injection(
                session,
                injection_id,
                on_date=final_date,
                override=override,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_conflict_write=prepared,
                **merged,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        except ValueError as e:
            return {"error": str(e)}
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
@gated("glp1")
async def log_side_effect(
    effect_type: str,
    severity: int,
    on_date: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Records a GLP-1 side effect (e.g. "nausea") with a severity 1–5 for a date
    (default today). WRITE tool — saved immediately."""
    from vitals.services import glp1_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")
    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_date,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        row = await glp1_service.log_side_effect(
            session, on_date=parsed_date, effect_type=effect_type,
            severity=severity, note=note, source=Source.MCP.value,
            identity=conflict_context.identity,
            prepared_conflict_write=prepared,
        )
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
@gated("glp1")
async def add_dose_phase(
    start_date: str,
    drug: str,
    dose_mg: float,
    end_date: Optional[str] = None,
    note: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Adds a GLP-1 dose phase (a period on a given drug + dose, overlaid on the
    weight chart). Open-ended phases are bounded at adjacent phase starts so only
    the newest one remains current. WRITE tool."""
    from vitals.services import glp1_service

    session_factory = get_session_factory()
    parsed_start = _parse_date(start_date, field="start_date")
    parsed_end = _parse_date(end_date, field="end_date")
    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_start,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        try:
            row = await glp1_service.add_dose_phase(
                session,
                start_date=parsed_start,
                drug=drug,
                dose_mg=dose_mg,
                end_date=parsed_end,
                note=note,
                source=Source.MCP.value,
                override=override,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_conflict_write=prepared,
            )
        except ConflictBlocked as exc:
            await session.rollback()
            return _conflict_payload(exc)
        except ValueError as exc:
            return {"error": str(exc)}
        await session.commit()
        return await serialize_written(session, row)


# ── Skincare observations ─────────────────────────────────────────────────────
@mcp.tool()
@gated("skincare")
async def log_skincare_observation(
    on_date: Optional[str] = None,
    inflammation: Optional[int] = None,
    pih: Optional[int] = None,
    zone: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Records a skin-status observation — inflammation and PIH (post-inflammatory
    hyperpigmentation) scores, an optional face ``zone``, and a note. Distinct from
    the daily routine checklist (log_skincare). WRITE tool — saved immediately."""
    from vitals.services import skincare_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")
    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(
            session,
            evaluation_date=parsed_date,
        )
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        row = await skincare_service.add_observation(
            session, on_date=parsed_date, inflammation=inflammation,
            pih=pih, zone=zone, note=note, source=Source.MCP.value,
            identity=conflict_context.identity,
            prepared_conflict_write=prepared,
        )
        await session.commit()
        return await serialize_written(session, row)


# ── Supplements catalog CRUD ──────────────────────────────────────────────────
@mcp.tool()
@gated("supplements")
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
    from vitals.services import supplements_service

    session_factory = get_session_factory()
    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(session)
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        try:
            row = await supplements_service.add_supplement(
                session, name=name, key=key, dose=dose, timing=timing,
                evidence=evidence, active=active,
                contraindications=contraindications, note=note, override=override,
                source=Source.MCP.value,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        except ConflictBlocked as e:
            return _conflict_payload(e)
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
@gated("supplements")
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
    from vitals.services import supplements_service

    session_factory = get_session_factory()
    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(session)
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        current = await supplements_service.get_supplement_for_update(
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
            row = await supplements_service.update_supplement(
                session, supplement_id, override=override, **merged,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        except ConflictBlocked as e:
            return _conflict_payload(e)
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
@gated("supplements")
async def set_supplement_active(
    supplement_id: int, active: bool, override: bool = False
) -> dict:
    """Toggles a supplement's active flag. Activating a contraindicated one runs the
    conflict check → ``{"blocked": true, ...}`` unless ``override=True``. WRITE tool."""
    from vitals.services import supplements_service

    session_factory = get_session_factory()
    async with session_factory() as session:
        conflict_context = await _mcp_v1_conflict_write_context(session)
        prepared = await conflict_engine.prepare_scoped_write(
            session,
            context=conflict_context,
        )
        try:
            row = await supplements_service.set_active(
                session,
                supplement_id,
                active,
                override=override,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
        except ConflictBlocked as e:
            return _conflict_payload(e)
        if row is None:
            return {"error": f"Supplement {supplement_id} not found"}
        await session.commit()
        return await serialize_written(session, row)


# ── Body measurement edit/delete + noise markers ──────────────────────────────
@mcp.tool()
async def update_measurement(
    measurement_id: int,
    on_date: str,
    neck_cm: Optional[float] = None,
    waist_cm: Optional[float] = None,
    hips_cm: Optional[float] = None,
    note: Optional[str] = None,
    override: bool = False,
) -> dict:
    """Edits a body-measurement row by ID (recomputes Navy body-fat % / LBM). On a
    hard block returns ``{"blocked": true, ...}``; retry with ``override=True``.
    WRITE tool."""
    from vitals.services import weight_service

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, field="on_date")
    async with session_factory() as session:
        conflict_context, prepared = await _mcp_v1_aux_weight_write(
            session,
            evaluation_date=parsed_date,
        )
        try:
            row = await weight_service.update_body_measurement(
                session, measurement_id, on_date=parsed_date, neck_cm=neck_cm,
                waist_cm=waist_cm, hips_cm=hips_cm, note=note, override=override,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_conflict_write=prepared,
            )
        except ConflictBlocked as e:
            await session.rollback()
            return _conflict_payload(e)
        except ValueError as e:
            await session.rollback()
            return {"error": str(e)}
        if row is None:
            return {"error": f"Measurement {measurement_id} not found"}
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
async def add_noise_marker(
    start_date: str,
    reason: str,
    end_date: Optional[str] = None,
    direction: Optional[str] = None,
) -> dict:
    """Marks a date range as noisy so it's excluded from the weight moving average
    and trend (e.g. "sick week", "creatine loading"). ``direction`` is up (scale
    inflated), down (scale deflated), or neutral. Omit ``end_date`` for an open
    period. WRITE tool — the weight trend recomputes without this range."""
    from vitals.services import weight_service

    session_factory = get_session_factory()
    parsed_start = _parse_date(start_date, field="start_date")
    parsed_end = _parse_date(end_date, field="end_date")
    async with session_factory() as session:
        conflict_context, prepared = await _mcp_v1_aux_weight_write(
            session,
            evaluation_date=today_local(),
        )
        try:
            row = await weight_service.add_noise_marker(
                session, start_date=parsed_start, end_date=parsed_end,
                reason=reason, direction=direction, source=Source.MCP.value,
                identity=conflict_context.identity,
                include_legacy_unowned=True,
                prepared_conflict_write=prepared,
            )
        except ValueError as exc:
            await session.rollback()
            return {"error": str(exc)}
        await session.commit()
        return await serialize_written(session, row)


# ── Modules (optional-domain toggles) ─────────────────────────────────────────
@mcp.tool()
async def get_modules() -> dict:
    """Returns which optional domains are enabled, plus which module keys are core
    (always-on, locked) vs optional (toggleable). Check this before calling a
    module-gated write tool (log_body_scan, log_event) so you know if it's on."""
    from vitals.services import modules_service

    session_factory = get_session_factory()
    async with session_factory() as session:
        ownership = await _mcp_v1_legacy_owner(session)
        enabled = await modules_service.get_enabled_modules(
            session,
            subject_id=ownership.subject_id,
        )
    return {
        "enabled": enabled,
        "core": sorted(modules_service.CORE_KEYS),
        "optional": sorted(modules_service.OPTIONAL_KEYS),
    }


@mcp.tool()
async def set_module(key: str, enabled: bool) -> dict:
    """Enables or disables an optional module (e.g. body_comp, timeline, glp1,
    nutrition). Core modules are locked and return an error. WRITE tool — returns
    the new enabled-module map."""
    from vitals.services import modules_service

    session_factory = get_session_factory()
    async with session_factory() as session:
        ownership = await _mcp_v1_legacy_owner(session)
        try:
            state = await modules_service.set_module_enabled(
                session,
                key=key,
                enabled=enabled,
                subject_id=ownership.subject_id,
            )
        except modules_service.ModuleToggleError as e:
            return {"error": str(e)}
        await session.commit()
        await modules_service.prime_cache(
            get_redis_client(),
            state,
            subject_id=ownership.subject_id,
        )
        return {"enabled": state}


# ── Weekly digest generation ──────────────────────────────────────────────────
@mcp.tool()
async def generate_digest_now(period_days: int = 7) -> dict:
    """Generates a fresh weekly AI digest right now (assembles the cross-domain
    context, asks the configured LLM for the narrative, saves it) and returns it.
    Errors cleanly if platform AI is unavailable. WRITE tool."""
    from vitals.services import (
        ai_gateway_service,
        digest_service,
        milestones_service,
    )

    session_factory = get_session_factory()
    async with session_factory() as session:
        prepared = None

        async def release_reservation() -> None:
            await session.rollback()
            if prepared is None or not prepared.dispatchable:
                return
            if await digest_service.release_prepared_digest(session, prepared):
                await session.commit()
            else:
                await session.rollback()

        try:
            prepared = await digest_service.prepare_digest(
                session,
                actor_username=get_web_config().auth_username,
                invocation_source=AIInvocationSource.MCP,
                period_days=period_days,
            )
            await session.commit()
            if prepared.existing_artifact_id is not None:
                owner = await digest_service.prepare_digest_owner(
                    session,
                    actor_username=get_web_config().auth_username,
                )
                row = await digest_service.existing_digest_for_prepared(
                    session,
                    prepared,
                    prepared_owner=owner,
                )
                if row is None:
                    return {"error": "digest provenance is unavailable"}
                return await serialize_written(session, row)
            if not prepared.dispatchable:
                if prepared.reservation_status is AIInvocationStatus.DISPATCHING:
                    return {
                        "error": "digest generation is already pending",
                        "code": "dispatching",
                    }
                return {
                    "error": "digest generation attempt failed",
                    "code": prepared.reservation_status.value,
                }
            lease = await digest_service.start_digest_dispatch(session, prepared)
            await session.commit()
            completion = await digest_service.render_digest(prepared, lease)
            row = await digest_service.persist_digest(
                session,
                prepared,
                completion,
            )
            await session.commit()
            if row is None:
                return {
                    "error": "AI provider did not produce a digest",
                    "code": (
                        completion.error_code.value
                        if completion.error_code is not None
                        else "invalid_response"
                    ),
                }
            return await serialize_written(session, row)
        except ai_gateway_service.AIQuotaExceededError:
            await session.rollback()
            return {"error": "AI quota is unavailable", "code": "quota_exceeded"}
        except ai_gateway_service.AIGatewayConfigurationError:
            await release_reservation()
            return {
                "error": "platform AI is not configured",
                "code": "provider_unconfigured",
            }
        except (
            ai_gateway_service.AIGatewayAuthorizationError,
            digest_service.DigestOwnershipError,
            milestones_service.MilestoneOwnershipError,
        ):
            await release_reservation()
            raise
        except ai_gateway_service.AIInvocationStateError:
            await session.rollback()
            return {"error": "digest generation is already pending"}
        except ValueError:
            await session.rollback()
            return {"error": "invalid digest request"}


# ── Trend analytics ───────────────────────────────────────────────────────────
@mcp.tool()
async def get_trend(
    metric_key: str,
    param: Optional[str] = None,
    target: Optional[float] = None,
    rolling_window_days: int = 7,
    exclude_noise: bool = True,
) -> dict:
    """Computes the trend for one metric instead of returning raw rows: linear slope
    (per day and per week), the latest rolling-mean value, and — if ``target`` is
    given — the projected date the trend reaches it. For weight metrics, noise-marked
    ranges are excluded (``exclude_noise``).

    ``metric_key`` is a registry key such as ``weight.weight_kg``,
    ``weight.body_fat_pct``, ``garmin.hrv_avg``, ``nutrition.calories``, or a
    parametrized one: ``labs.marker`` (``param`` = marker name),
    ``hevy.working_weight`` (``param`` = exercise id), ``body_comp.metric``
    (``param`` = ``metric_key`` or ``metric_key:segment``). Read-only."""
    from vitals.services import chart_data_service, weight_service
    from vitals.services.analytics import exclude_ranges
    from vitals.services.analytics.regression import fit_trend, project_date_for_value
    from vitals.services.analytics.rolling import rolling_mean_by_date
    from vitals.services.analytics.chart_registry import get as get_metric

    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            field = get_metric(metric_key)
        except KeyError:
            return {"error": f"Unknown metric '{metric_key}'"}
        try:
            trend_scope = await _mcp_v1_conflict_scope(session)
            raw = await chart_data_service.series_for(
                session,
                subject_id=trend_scope.subject_id,
                metric_key=metric_key,
                param=param,
            )
        except ValueError as e:
            return {"error": str(e)}

        points = [(date_type.fromisoformat(p["date"]), float(p["value"])) for p in raw]

        noise_applied = False
        if exclude_noise and field.domain == "weight":
            scope = await _mcp_v1_conflict_scope(session)
            markers = await weight_service.list_noise_markers(
                session,
                subject_id=scope.subject_id,
                include_legacy_unowned=scope.include_legacy_unowned,
            )
            ranges = [(m.start_date, m.end_date) for m in markers]
            if ranges:
                points = exclude_ranges(points, ranges)
                noise_applied = True

        points = sorted(points, key=lambda p: p[0])
        if not points:
            return {"metric_key": metric_key, "param": param, "unit": field.unit, "points": 0}

        trend = fit_trend(points)
        rolling = rolling_mean_by_date(points, window_days=rolling_window_days)
        result: dict = {
            "metric_key": metric_key,
            "param": param,
            "unit": field.unit,
            "points": len(points),
            "first": {"date": points[0][0].isoformat(), "value": points[0][1]},
            "last": {"date": points[-1][0].isoformat(), "value": points[-1][1]},
            "rolling_mean": {
                "window_days": rolling_window_days,
                "last": {"date": rolling[-1][0].isoformat(), "value": rolling[-1][1]},
            },
            "trend": None if trend is None else {
                "slope_per_day": round(trend.slope_per_day, 5),
                "slope_per_week": round(trend.slope_per_week, 4),
                "n": trend.n,
            },
            "noise_excluded": noise_applied,
        }
        if target is not None:
            crossing = project_date_for_value(points, target)
            result["projection"] = {
                "target": target,
                "date": crossing.isoformat() if crossing else None,
            }
        return result


# ── Signals tools (free-text capture — optional module) ───────────────────────
@mcp.tool()
async def get_signals(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    kind: Optional[str] = None,
    key: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """Retrieves signals — the owner's own words about how a day felt, parsed into
    rows: states ("энергии ноль"), symptoms ("голова раскалывается"), exposures
    ("кофе в 22"). This is the domain that *explains* the Garmin numbers. Filter by
    ``kind`` (state/symptom/exposure) and/or ``key`` (matches every stored spelling
    that folds to it, e.g. ``sleepiness`` also finds ``sleepy_af``). Rows the owner
    flagged as misparsed are excluded. Newest first, most recent 200 by default."""
    from vitals.services import signals_service

    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        ownership = await _mcp_v1_legacy_owner(session)
        rows = await signals_service.list_signals(
            session,
            key=key,
            kind=kind,
            start=start,
            end=end,
            limit=limit,
            subject_id=ownership.subject_id,
        )
        return [serialize_row(r) for r in rows]


@mcp.tool()
@gated("signals")
async def log_signal(
    key: str,
    kind: str,
    value_num: Optional[float] = None,
    unit: Optional[str] = None,
    note: Optional[str] = None,
    at_time: Optional[str] = None,
    on_date: Optional[str] = None,
) -> dict:
    """Records one signal — a state, symptom or exposure the owner mentioned in
    conversation. ``kind`` must be state, symptom or exposure; ``key`` is a short
    slug (``headache``, ``caffeine_late``); ``value_num`` is intensity 1-5 for
    state/symptom or an amount for exposure; ``at_time`` is HH:MM (matters for
    exposures — "кофе в 22" only means something with the hour attached).
    WRITE tool — saved immediately."""
    from vitals.services import signals_service
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")

    async with session_factory() as session:
        ownership = await _mcp_v1_legacy_owner(session)
        rows = await signals_service.create_signals(
            session,
            items=[{
                "kind": kind, "key": key, "value_num": value_num,
                "unit": unit, "note": note, "at_time": at_time,
            }],
            on_date=parsed_date,
            source=Source.MCP.value,
            identity=ownership.owner_action(),
        )
        # create_signals drops unusable rows silently (it batch-parses LLM output,
        # where one bad fact must not cost the message). A single-row tool call has
        # no such batch to protect — an empty result means this call was rejected.
        if not rows:
            return {"error": "kind must be state, symptom or exposure, and key must be non-empty"}
        await session.commit()
        return await serialize_written(session, rows[0])


@mcp.tool()
@gated("signals")
async def mark_signal_misparse(batch_id: str) -> dict:
    """Flags every signal parsed out of one message as misparsed — the "не то"
    button. The rows and the raw text stay, they just drop out of ``get_signals``
    and out of the charts. ``batch_id`` is the field shared by all rows from the
    same message. WRITE tool — immediate."""
    from vitals.services import signals_service

    session_factory = get_session_factory()
    async with session_factory() as session:
        ownership = await _mcp_v1_legacy_owner(session)
        marked = await signals_service.mark_misparse(
            session,
            batch_id,
            subject_id=ownership.subject_id,
        )
        await session.commit()
        return {"marked": marked, "batch_id": batch_id}


@mcp.tool()
async def get_day_context(
    start_date: Optional[str] = None, end_date: Optional[str] = None, limit: int = 100
) -> list[dict]:
    """Retrieves per-day context — what kind of day it was (remote/office, gym or
    not, workload), as answered by the owner or guessed by the week template
    (``planned``). One row per date, newest first. Read this before explaining a
    day's Garmin numbers: a heavy office day and a rest day at home look the same
    in the metrics and mean opposite things."""
    session_factory = get_session_factory()
    start = _parse_date(start_date, field="start_date")
    end = _parse_date(end_date, field="end_date")

    async with session_factory() as session:
        from vitals.services import signals_service

        ownership = await _mcp_v1_legacy_owner(session)
        rows = await signals_service.list_day_contexts(
            session,
            start=start,
            end=end,
            limit=limit,
            subject_id=ownership.subject_id,
        )
        return [serialize_row(r) for r in rows]


@mcp.tool()
@gated("signals")
async def log_day_context(answers: dict, on_date: Optional[str] = None) -> dict:
    """Records what kind of day it was — the same answers the owner taps in
    Telegram, when he says them here instead ("сегодня удалёнка, зала не будет").
    Keys: ``where`` (office/remote/off), ``gym`` (true/false), ``load``
    (light/normal/heavy — about a day already spent, not a plan). Only the keys
    you pass are changed, and the week template's own guess is kept beside the
    answer rather than overwritten. ``on_date`` defaults to today. WRITE tool."""
    from vitals.services.proactive import day_plan
    from vitals.utils.timeutils import today_local

    session_factory = get_session_factory()
    parsed_date = _parse_date(on_date, today_local(), field="on_date")

    legal = "; ".join(f"{q.key}: {list(q.labels)}" for q in day_plan.QUESTIONS)
    if not answers:
        return {"error": f"answers must contain at least one of — {legal}"}
    for key, value in answers.items():
        question = day_plan.QUESTIONS_BY_KEY.get(key)
        # Validated before anything is written: half-applied answers would leave
        # the day in a state neither the owner nor the template ever produced.
        if question is None or value not in question.labels:
            return {"error": f"{key}={value!r} is not a day-context answer — {legal}"}

    async with session_factory() as session:
        ownership = await _mcp_v1_legacy_owner(session)
        for key, value in answers.items():
            row = await day_plan.record_answer(
                session,
                parsed_date,
                key,
                value,
                source=Source.MCP.value,
                identity=ownership.owner_action(),
            )
        await session.commit()
        return await serialize_written(session, row)


@mcp.tool()
async def get_proactive_state(limit: int = 10) -> dict:
    """Retrieves the state of the proactive Telegram layer: whether it is on, its
    settings (message times, daily budget, which nudge categories are allowed), the
    week template (what each weekday is assumed to be until the owner says
    otherwise), and the last messages the bot actually sent. Read this before
    explaining why the bot did or didn't say something. READ tool — the settings are
    read-only here; retiming or muting the bot is done in Settings, by the owner."""
    from vitals.services.proactive import channels, day_plan, delivery, prefs

    session_factory = get_session_factory()
    async with session_factory() as session:
        ownership = await channels.resolve_legacy_channel_ownership(
            session,
            actor_username=get_web_config().auth_username,
        )
        preference_scope = await prefs.resolve_legacy_preferences_scope(
            session,
            actor_username=get_web_config().auth_username,
        )
        sent = list(
            reversed(
                await delivery.recent_sent(
                    session,
                    limit=limit,
                    ownership=ownership,
                )
            )
        )
        enabled_modules = await modules_service.get_enabled_modules(
            session,
            subject_id=ownership.subject_id,
        )
        return {
            "enabled": bool(enabled_modules.get("signals")),
            "prefs": (
                await prefs.get_preferences_bundle(
                    session,
                    scope=preference_scope,
                    actor_username=get_web_config().auth_username,
                )
            ).as_flat_dict(),
            "week_template": await day_plan.get_week_template(
                session,
                subject_id=ownership.subject_id,
            ),
            "recent_notifications": [serialize_row(n) for n in sent],
        }


@mcp.tool()
@gated("signals")
async def set_week_template(template: dict) -> dict:
    """Stores the week template — what each weekday is assumed to be until the owner
    answers otherwise ("по вторникам я всегда на удалёнке"). Keys are "mon".."sun",
    each a dict of ``where`` (office/remote/off) and ``gym`` (true/false). Only the
    weekdays and keys you pass are changed; the rest keep their stored values. How
    heavy a day is can't be predicted from a weekday, so it isn't part of the
    template. WRITE tool — returns the full stored template."""
    from vitals.services.proactive import day_plan

    legal = "/".join(day_plan.WEEKDAYS)
    if not isinstance(template, dict) or not template:
        return {"error": f"template must be a dict of weekday → answers ({legal})"}
    unknown = sorted(k for k in template if k not in day_plan.WEEKDAYS)
    if unknown:
        return {"error": f"unknown weekday(s) {unknown} — use {legal}"}

    session_factory = get_session_factory()
    async with session_factory() as session:
        ownership = await _mcp_v1_legacy_owner(session)
        questions = {question.key: question for question in day_plan.TEMPLATE_QUESTIONS}
        for day, values in template.items():
            if not isinstance(values, dict):
                return {"error": f"{day} must be a dict of answers, got {values!r}"}
            unknown_answers = sorted(set(values) - set(questions))
            if unknown_answers:
                return {
                    "error": f"{day} has unknown answer key(s) {unknown_answers}"
                }
            for key, value in values.items():
                question = questions[key]
                if isinstance(question.default, bool):
                    if type(value) is not bool:
                        return {"error": f"{day}.{key} must be true or false"}
                elif value not in question.labels:
                    legal_values = "/".join(question.labels)
                    return {
                        "error": f"{day}.{key} must be one of {legal_values}"
                    }
        clean = await day_plan.update_week_template(
            session,
            template,
            subject_id=ownership.subject_id,
        )
        await session.commit()
        return clean


# ── Sync tools (pull from Garmin / Hevy on demand) ────────────────────────────
# A sync is an outbound call to someone else's API — Garmin's in particular
# throttles logins — and the scheduler already polls both several times a day.
# These exist for the gap case ("the last two days are empty"), so three calls a
# day each is plenty. Counter is per calendar day, in Redis; fail-open like
# web/ratelimit.py — a counter must never be the reason a sync can't run.
SYNC_DAILY_LIMIT = 3


async def _spend_sync_quota(bucket: str, limit: int = SYNC_DAILY_LIMIT) -> Optional[dict]:
    """Count one call against today's quota. Returns an error dict once it's spent."""
    key = f"mcp:sync_quota:{bucket}:{today_local().isoformat()}"
    try:
        redis = get_redis_client()
        used = await redis.incr(key)
        if used == 1:
            await redis.expire(key, 86400)
    except Exception:
        logger.warning("sync quota backend unavailable for %s; allowing", bucket, exc_info=True)
        return None
    if used > limit:
        return {
            "error": f"{bucket} has already run {limit} times today, which is the daily "
                     "cap for on-demand syncs. The scheduled sync keeps running regardless; "
                     "the quota resets at midnight."
        }
    return None


@mcp.tool()
async def sync_garmin(days: int = 2) -> dict:
    """Pulls fresh Garmin data now — daily metrics plus activities for the last
    ``days`` (default 2: yesterday and today; up to 30 to fill a longer gap).

    Use it when the data looks stale or a day is missing, not before every read:
    the scheduler already polls several times a day. Capped at 3 calls a day.
    Returns ``{days, activities, error}``; an auth/MFA/throttle failure comes back
    as ``error`` (and raises an alert) rather than as an exception."""
    from vitals.services import garmin_service

    spent = await _spend_sync_quota("sync_garmin")
    if spent:
        return spent

    summary = await garmin_service.sync_job(
        get_session_factory(),
        get_redis_client(),
        days=max(1, min(int(days), 30)),
        actor_username=get_web_config().auth_username,
    )
    if summary is None:
        return {"error": "Garmin is not configured — no credentials in settings"}
    return summary


@mcp.tool()
@gated("hevy")
async def sync_hevy() -> dict:
    """Pulls the latest Hevy workouts now. Same rules as ``sync_garmin``: for a gap
    in the data, not for routine reads (the scheduler syncs every 6 hours), capped
    at 3 calls a day. Returns ``{fetched, created, updated, skipped}``."""
    from vitals.integrations.hevy_client import HevyAPIError, HevyNotConfigured
    from vitals.services import hevy_service

    spent = await _spend_sync_quota("sync_hevy")
    if spent:
        return spent

    try:
        summary = await hevy_service.sync_job(
            get_session_factory(),
            get_redis_client(),
            actor_username=get_web_config().auth_username,
        )
    except (HevyNotConfigured, HevyAPIError) as e:
        return {"error": f"Hevy sync failed: {e}"}
    if summary is None:
        return {"error": "Hevy is not configured — no API key in settings"}
    return summary


# ── Resources & prompts ───────────────────────────────────────────────────────
@mcp.resource("vitals://profile")
async def profile_resource() -> dict:
    """The user's physical profile, goals, and program — attachable as lightweight
    context without spending a tool call."""
    return await get_user_profile()


@mcp.resource("vitals://digest/latest")
async def latest_digest_resource() -> dict:
    """The most recent weekly AI digest (narrative + date) for conversation
    continuity."""
    from vitals.services import digest_service

    session_factory = get_session_factory()
    async with session_factory() as session:
        owner = await digest_service.prepare_digest_owner(
            session,
            actor_username=get_web_config().auth_username,
        )
        row = await digest_service.latest_digest(
            session,
            prepared_owner=owner,
        )
        if row is None:
            return {"error": "No digests yet"}
        return {"date": row.date.isoformat(), "content": row.content, "model": row.model}


@mcp.prompt()
async def weekly_review() -> str:
    """A ready-made prompt that drives a full cross-domain weekly review."""
    return (
        "Review my last 7 days across every domain. First call get_full_snapshot "
        "for the aligned cross-domain picture (weight trend, GLP-1 state, recent "
        "labs, activity/recovery, workouts, nutrition, skincare, goals). Then pull "
        "get_trend for weight and any lab marker that looks off. Summarize what "
        "changed, call out cross-domain correlations (e.g. sleep vs training load, "
        "dose changes vs side effects), surface anything from get_active_alerts, and "
        "give at most three concrete, non-alarmist suggestions. This is decision "
        "support, not medical advice."
    )


# The read side of the same map (the writes registered themselves via ``gated``).
# ``tests/test_mcp_tool_budget.py`` checks every name here is a real tool, so a
# rename can't quietly leave a domain's reads visible forever.
TOOL_MODULES.update({
    "get_glp1_logs": "glp1",
    "get_hevy_workouts": "hevy",
    "get_supplements_catalog": "supplements",
    "get_skincare_logs": "skincare",
    "get_genetics_snps": "genetics",
    "get_hrt_logs": "hrt",
    "get_hrt_cycles": "hrt",
    "get_nutrition_summary": "nutrition",
    "search_meals": "nutrition",
    "get_body_scans": "body_comp",
    "get_body_scan": "body_comp",
    "get_body_metric_history": "body_comp",
    "get_timeline": "timeline",
    "get_signals": "signals",
    "get_day_context": "signals",
    "get_proactive_state": "signals",
})


class ModuleVisibilityMiddleware(Middleware):
    """Hide a switched-off module's tools from ``tools/list``.

    Writes and ownership-sensitive reads refuse disabled modules. Hiding every
    classified tool also saves the conversation budget and avoids inviting the
    model into a domain the owner does not track. Resolved per request rather than
    latched at import, so flipping a toggle in Settings takes effect on the next
    reconnect without a restart. Fails open: if module state cannot be read, the
    full surface is listed rather than an empty one.
    """

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        try:
            session_factory = get_session_factory()
            async with session_factory() as session:
                ownership = await _mcp_v1_legacy_owner(session)
                enabled = await modules_service.get_enabled_modules(
                    session,
                    subject_id=ownership.subject_id,
                )
        except Exception:
            logger.warning("mcp: module state unavailable; listing every tool", exc_info=True)
            return tools
        return [t for t in tools if enabled.get(TOOL_MODULES.get(t.name, ""), True)]


mcp.add_middleware(ModuleVisibilityMiddleware())


def _www_authenticate(scope) -> bytes:
    """The 401 challenge, pointing at this resource's metadata (RFC 9728 §5.1).

    A bare ``Bearer`` leaves a fresh client guessing where tokens come from; the
    ``resource_metadata`` link is how it finds the authorization server. Built from
    the request's own host so it stays right behind the reverse proxy (uvicorn runs
    with --forwarded-allow-ips, so the scheme is the external one)."""
    from web.routers.oauth import PROTECTED_RESOURCE_PATH

    host = dict(scope.get("headers", [])).get(b"host", b"").decode("utf-8", "ignore")
    if not host:
        return b"Bearer"
    url = f"{scope.get('scheme', 'https')}://{host}{PROTECTED_RESOURCE_PATH}"
    return f'Bearer resource_metadata="{url}"'.encode("utf-8")


class MCPAuthMiddleware:
    """ASGI middleware that intercepts all requests to the MCP application

    and validates the signed Bearer access token in the Authorization header.
    """
    def __init__(self, app, client_id: str):
        self.app = app
        self.client_id = client_id

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("method") == "OPTIONS":
            # No access-control-allow-origin: the actual MCP responses carry no CORS
            # headers, so a wildcard here grants nothing. Claude.ai's connector is
            # server-side (not a browser), so it never sends a preflight anyway.
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS"),
                    (b"access-control-allow-headers", b"Authorization, Content-Type"),
                    (b"content-length", b"0"),
                ]
            })
            await send({
                "type": "http.response.body",
                "body": b"",
                "more_body": False
            })
            return

        # Check Authorization header
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("utf-8")

        # Bearer header ONLY. We deliberately do not accept the token via a query
        # param (?token=/?access_token=): query strings leak into reverse-proxy
        # access logs, browser history and Referer headers, and this token is
        # long-lived. Claude.ai's connector sends the Authorization header.
        token = None
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:]

        authenticated = False
        if token:
            from web.auth import _get_mcp_serializer
            from itsdangerous import SignatureExpired, BadSignature
            serializer = _get_mcp_serializer()
            try:
                # Validate access token with 1 year TTL limit
                payload = serializer.loads(token, max_age=31536000)
                if (
                    isinstance(payload, dict)
                    and payload.get("type") == "mcp_access_token"
                    and payload.get("client_id") == self.client_id
                ):
                    authenticated = True
            except (SignatureExpired, BadSignature):
                pass

        if not authenticated:
            response_body = b'{"detail":"Unauthorized. Invalid or missing MCP access token."}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(response_body)).encode("utf-8")),
                    (b"www-authenticate", _www_authenticate(scope)),
                ]
            })
            await send({
                "type": "http.response.body",
                "body": response_body,
                "more_body": False
            })
            return

        # Track whether the downstream app already began the response, so on a
        # mid-stream failure we don't try to start a second one (that would raise).
        response_started = False
        response_done = False

        async def _send(message):
            nonlocal response_started, response_done
            if response_done:
                return
            if message["type"] == "http.response.start":
                if response_started:
                    # A streaming endpoint can emit a second response start after
                    # the stream is over (e.g. an empty Response() once the client
                    # hangs up). Forwarding it trips an assertion inside the
                    # BaseHTTPMiddleware wrappers from web/csrf.py and logs a
                    # traceback on every connector reconnect. Drop it and anything
                    # after it — the response is finished either way.
                    response_done = True
                    return
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except TypeError:
            logger.exception("MCP app raised TypeError handling %s", scope.get("path"))
            if not response_started:
                body = b'{"detail":"Internal server error in MCP handler."}'
                await send({
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("utf-8")),
                    ],
                })
                await send({
                    "type": "http.response.body",
                    "body": body,
                    "more_body": False,
                })


def get_mcp_app() -> tuple[object, object]:
    """Wraps the FastMCP Starlette app with Bearer authorization middleware.

    Returns ``(app, lifespan)``. Streamable HTTP builds its session manager inside
    the lifespan, and ``app.mount()`` does not run a sub-app's lifespan — so the
    caller must enter it explicitly or every request fails with "manager not
    initialized". See web/main.py.
    """
    from web.config import get_web_config
    cfg = get_web_config()
    # Streamable HTTP (the SSE transport is deprecated in the MCP spec since
    # 2025-03). path="/" so that mounting on /mcp lands the endpoint on /mcp/
    # rather than /mcp/mcp — the library's own default path would be appended.
    raw_app = mcp.http_app(transport="http", path="/")
    return MCPAuthMiddleware(raw_app, client_id=cfg.mcp_client_id), raw_app.router.lifespan_context
