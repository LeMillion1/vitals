"""Frozen medical snapshot projection and selectable-domain contract."""

from __future__ import annotations

from vitals.services.genetics import queries as genetics_queries

from vitals.services.supplements import queries as supplement_queries

import secrets
import uuid
from datetime import date as date_type, datetime
from typing import Any, Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain
from vitals.models.share import SharedReport
from vitals.ownership_transition import bridges as ownership_bridges
from vitals.services.digest.projection import assembly as digest_projection
from vitals.services.digest import window as digest_window
from vitals.services.glp1 import queries as glp1_queries
from vitals.services.share.ownership import (
    SNAPSHOT_VERSION,
    PreparedShareOwner,
    ShareOwnershipError,
    SharePreparedOwnerError,
    _PublicReportOwnershipError,
    _owner_or_zero_subject_legacy,
)
from vitals.services.share.queries import clamp_window
from vitals.utils.timeutils import now_local


# Which module switch gates each selectable domain. A domain missing from this
# map cannot be published at all — the map, not the form, is the gate.
DOMAIN_MODULE: dict[str, str] = {
    Domain.WEIGHT.value: "weight",
    Domain.BODY_COMPOSITION.value: "body_comp",
    Domain.LABS.value: "labs",
    Domain.GLP1.value: "glp1",
    Domain.HRT.value: "hrt",
    Domain.SUPPLEMENTS.value: "supplements",
    Domain.GARMIN.value: "garmin",
    Domain.WORKOUTS.value: "hevy",
    Domain.NUTRITION.value: "nutrition",
    Domain.SKINCARE.value: "skincare",
    Domain.GENETICS.value: "genetics",
}

# Render order of the document's sections, so a snapshot's key order never
# decides what a doctor reads first.
DOMAIN_ORDER: tuple[str, ...] = tuple(DOMAIN_MODULE)

_ALL = list(DOMAIN_MODULE)

# Built-in presets — constants, not a table. Six cover the appointments that
# actually happen; the form also pre-fills from the last report, which is a
# personal preset without a schema to maintain.
PRESETS: dict[str, dict[str, Any]] = {
    "full": {"domains": _ALL, "labs_flagged_only": False},
    "labs_meds": {
        "domains": [
            Domain.LABS.value,
            Domain.GLP1.value,
            Domain.HRT.value,
            Domain.SUPPLEMENTS.value,
        ],
        "labs_flagged_only": False,
    },
    "endocrinologist": {
        "domains": [
            Domain.LABS.value,
            Domain.GLP1.value,
            Domain.HRT.value,
            Domain.WEIGHT.value,
            Domain.BODY_COMPOSITION.value,
            Domain.SUPPLEMENTS.value,
            Domain.NUTRITION.value,
        ],
        "labs_flagged_only": False,
    },
    "gp": {
        "domains": [
            Domain.LABS.value,
            Domain.GLP1.value,
            Domain.HRT.value,
            Domain.SUPPLEMENTS.value,
            Domain.WEIGHT.value,
        ],
        # A first-contact doctor wants what is wrong, not the whole panel.
        "labs_flagged_only": True,
    },
    "dermatologist": {
        "domains": [
            Domain.SKINCARE.value,
            Domain.LABS.value,
            Domain.HRT.value,
            Domain.GLP1.value,
            Domain.SUPPLEMENTS.value,
        ],
        "labs_flagged_only": False,
    },
    "sports": {
        "domains": [
            Domain.GARMIN.value,
            Domain.WORKOUTS.value,
            Domain.WEIGHT.value,
            Domain.BODY_COMPOSITION.value,
            Domain.LABS.value,
            Domain.NUTRITION.value,
        ],
        "labs_flagged_only": False,
    },
}

PERIOD_CHOICES: tuple[int, ...] = (30, 90, 180)
EXPIRY_CHOICES: tuple[int, ...] = (7, 30, 90)
DEFAULT_EXPIRY_DAYS = 30

# Password shown once, then only its hash exists. Six words of base32-ish
# alphabet read aloud over a desk without "was that an l or a 1".
_PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
_PASSWORD_LENGTH = 12


def generate_password() -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(_PASSWORD_LENGTH))


def resolve_domains(domains: Sequence[str], enabled: Optional[dict[str, bool]] = None) -> list[str]:
    """Requested domains ∩ known domains ∩ switched-on modules, in render order.

    The single choke point for "a disabled module never leaves the building":
    both the form and the snapshot builder go through it, so they cannot disagree.
    """
    from vitals.services.modules_service import CORE_KEYS

    wanted = set(domains or ())
    em = enabled or {}
    return [
        d
        for d in DOMAIN_ORDER
        if d in wanted and em.get(DOMAIN_MODULE[d], DOMAIN_MODULE[d] in CORE_KEYS)
    ]


def available_domains(enabled: Optional[dict[str, bool]] = None) -> list[str]:
    """Every domain the owner could tick right now, in render order."""
    return resolve_domains(DOMAIN_ORDER, enabled)


def _owner_scope(prepared_owner: PreparedShareOwner):
    identity = prepared_owner._identity
    return or_(
        SharedReport.subject_id == identity.subject_id,
        and_(
            SharedReport.subject_id.is_(None),
            SharedReport.created_by_user_id.is_(None),
            SharedReport.revoked_by_user_id.is_(None),
        ),
    )


def _bridge_is_absent(state: Any) -> bool:
    return (
        state.processed_high_watermark_id == 0
        and state.snapshot_high_watermark_id == 0
        and not state.completed
    )


async def _historical_bridge_state(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    public: bool = False,
) -> Any:
    """Load the read-only Stage-3K compatibility boundary."""

    try:
        return await ownership_bridges.shared_report_historical_bridge_state(
            session,
            subject_id=subject_id,
        )
    except ownership_bridges.SharedReportOwnershipBackfillError as exc:
        error = _PublicReportOwnershipError if public else ShareOwnershipError
        raise error("shared-report migration checkpoint is not authoritative") from exc


def _validate_report_roots(
    *,
    report_id: int,
    expected_subject_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    created_by_user_id: uuid.UUID | None,
    revoked_by_user_id: uuid.UUID | None,
    revoked_at: datetime | None,
    bridge_state: Any,
    error_type: type[ShareOwnershipError] = ShareOwnershipError,
) -> bool:
    """Validate stored roots; return whether this is fully-null legacy history."""
    checkpoint_absent = _bridge_is_absent(bridge_state)
    within_snapshot = report_id <= bridge_state.snapshot_high_watermark_id
    historical_subject_id = getattr(bridge_state, "historical_subject_id", None)
    if subject_id is None:
        if created_by_user_id is not None or revoked_by_user_id is not None:
            raise error_type("shared report has partial legacy ownership roots")
        if not checkpoint_absent and (
            bridge_state.completed
            or report_id <= bridge_state.processed_high_watermark_id
            or not within_snapshot
        ):
            raise error_type("fully-unowned shared report is outside the historical bridge")
        return True
    if revoked_by_user_id is not None and revoked_at is None:
        raise error_type("shared report has revocation actor without revocation timestamp")
    if subject_id != expected_subject_id:
        raise error_type("shared report belongs to another subject")
    if (
        not checkpoint_absent
        and within_snapshot
        and historical_subject_id is not None
        and expected_subject_id != historical_subject_id
    ):
        raise error_type("shared report is attributed outside its historical subject")
    if not checkpoint_absent and not within_snapshot:
        if created_by_user_id != owner_user_id:
            raise error_type("live shared report creator does not own its health subject")
        if (revoked_at is None) != (revoked_by_user_id is None):
            raise error_type("live shared report has inconsistent revocation provenance")
    for actor_user_id in (created_by_user_id, revoked_by_user_id):
        # An exact-S row may legitimately lack historical actor provenance.  A
        # non-null actor, however, must be the subject owner.
        if actor_user_id is not None and actor_user_id != owner_user_id:
            raise error_type("shared report has a foreign actor for its health subject")
    return False


def _validate_owner_roots(
    prepared_owner: PreparedShareOwner,
    **roots: Any,
) -> bool:
    return _validate_report_roots(
        expected_subject_id=prepared_owner._identity.subject_id,
        owner_user_id=prepared_owner._owner_user_id,
        **roots,
    )


async def _reject_selected_scope_corruption(
    session: AsyncSession,
    prepared_owner: PreparedShareOwner,
) -> Any:
    identity = prepared_owner._identity
    bridge_state = await _historical_bridge_state(
        session,
        subject_id=identity.subject_id,
    )
    owner_user_id = prepared_owner._owner_user_id
    root_columns = (
        SharedReport.id,
        SharedReport.subject_id,
        SharedReport.created_by_user_id,
        SharedReport.revoked_by_user_id,
        SharedReport.revoked_at,
    )
    invalid_shapes = or_(
        and_(
            SharedReport.subject_id.is_(None),
            or_(
                SharedReport.created_by_user_id.is_not(None),
                SharedReport.revoked_by_user_id.is_not(None),
            ),
        ),
        and_(
            SharedReport.subject_id == identity.subject_id,
            or_(
                and_(
                    SharedReport.created_by_user_id.is_not(None),
                    SharedReport.created_by_user_id != owner_user_id,
                ),
                and_(
                    SharedReport.revoked_by_user_id.is_not(None),
                    SharedReport.revoked_by_user_id != owner_user_id,
                ),
                and_(
                    SharedReport.revoked_by_user_id.is_not(None),
                    SharedReport.revoked_at.is_(None),
                ),
            ),
        ),
    )
    candidates = [invalid_shapes]
    if not _bridge_is_absent(bridge_state):
        invalid_fully_null = SharedReport.subject_id.is_(None)
        if not bridge_state.completed:
            invalid_fully_null = and_(
                invalid_fully_null,
                or_(
                    SharedReport.id <= bridge_state.processed_high_watermark_id,
                    SharedReport.id > bridge_state.snapshot_high_watermark_id,
                ),
            )
        candidates.append(invalid_fully_null)
        candidates.append(
            and_(
                SharedReport.subject_id == identity.subject_id,
                SharedReport.id > bridge_state.snapshot_high_watermark_id,
                or_(
                    SharedReport.created_by_user_id.is_(None),
                    SharedReport.created_by_user_id != owner_user_id,
                    and_(
                        SharedReport.revoked_at.is_not(None),
                        or_(
                            SharedReport.revoked_by_user_id.is_(None),
                            SharedReport.revoked_by_user_id != owner_user_id,
                        ),
                    ),
                ),
            )
        )
    for predicate in candidates:
        root = (
            await session.execute(
                select(*root_columns).where(predicate).order_by(SharedReport.id).limit(1)
            )
        ).one_or_none()
        if root is None:
            continue
        _validate_owner_roots(
            prepared_owner,
            report_id=root.id,
            subject_id=root.subject_id,
            created_by_user_id=root.created_by_user_id,
            revoked_by_user_id=root.revoked_by_user_id,
            revoked_at=root.revoked_at,
            bridge_state=bridge_state,
        )
        raise ShareOwnershipError("shared report ownership validation failed")
    return bridge_state


# ── Snapshot ──────────────────────────────────────────────────────────────────


async def build_snapshot(
    session: AsyncSession,
    *,
    domains: Sequence[str],
    period_start: date_type,
    period_end: date_type,
    labs_flagged_only: bool = False,
    enabled: Optional[dict[str, bool]] = None,
    prepared_owner: PreparedShareOwner | None = None,
) -> dict[str, Any]:
    """Assemble the document's data for one window. No DB writes.

    A report covers days that are **over**. Today is still being lived — it has
    no sleep in it yet and possibly no meals — so a window asked for "through
    today" is read through yesterday and the row stores that, rather than a
    header claiming a day the numbers don't cover.
    """
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)
    if owner is None:
        raise SharePreparedOwnerError("composing a report requires the subject it is about")

    from vitals.i18n import current_lang
    from vitals.services import health_profile_service

    chosen = resolve_domains(domains, enabled)
    start, end = clamp_window(period_start, period_end)
    span_days = max((end - start).days + 1, 1)
    ctx = await digest_projection.assemble_context(
        session,
        subject_id=owner._identity.subject_id,
        on_date=end,
        period_days=span_days,
        mode=digest_window.REPORT_MODE_CLOSED,
        enabled_modules=enabled,
        max_period_days=max(PERIOD_CHOICES),
    )
    stats = ctx.get("period_stats") or {}
    subject_profile = await health_profile_service.get_profile(
        session, subject_id=owner._identity.subject_id
    )

    blocks: dict[str, Any] = {}
    for domain in chosen:
        builder = _BUILDERS[domain]
        block = await builder(
            session,
            ctx,
            stats,
            start,
            end,
            labs_flagged_only,
            subject_id=owner._identity.subject_id,
        )
        # An empty domain draws no section at all — a "no data" placeholder is a
        # line a doctor reads and gets nothing from.
        if block:
            blocks[domain] = block

    return {
        "version": SNAPSHOT_VERSION,
        "lang": current_lang.get(),
        "generated_at": now_local().isoformat(timespec="minutes"),
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": (end - start).days + 1,
        },
        # Whose body this document is about, from their own row rather than
        # from ``.env`` — which described the installation owner and was being
        # printed on every patient's doctor's report.
        "profile": {
            "age": subject_profile.age,
            "sex": subject_profile.sex,
            "height_cm": subject_profile.height_cm,
        },
        # What the document actually holds, not what was ticked. An empty domain
        # draws no section, so a contents line naming one sends a doctor looking
        # for labs that never arrived — and a missing section reads as "nothing
        # to report" when it means "nothing in this window".
        "domains": [d for d in chosen if d in blocks],
        "labs_flagged_only": bool(labs_flagged_only),
        "blocks": blocks,
    }


async def _weight_block(session, ctx, stats, start, end, flagged_only, *, subject_id) -> dict:
    """Where the scale started and where it ended, plus the series for the chart.

    Points come from ``chart_series`` rather than the raw table so the average
    drawn here is the same noise-excluded one the owner sees on /weight.
    """
    from vitals.services.weight import analytics as weight_analytics

    series = await weight_analytics.chart_series(session, subject_id=subject_id)
    in_window = [p for p in series["raw"] if start.isoformat() <= p["date"] <= end.isoformat()]
    ma_window = [p for p in series["trend_ma"] if start.isoformat() <= p["date"] <= end.isoformat()]
    if not in_window:
        return {}
    first, last = in_window[0], in_window[-1]
    return {
        "first": {"date": first["date"], "kg": first["weight_kg"]},
        "last": {"date": last["date"], "kg": last["weight_kg"]},
        "delta_kg": round(last["weight_kg"] - first["weight_kg"], 1),
        "readings": len(in_window),
        "points": [[p["date"], p["weight_kg"]] for p in in_window],
        "ma": [[p["date"], p["weight_kg"]] for p in ma_window],
    }


async def _body_comp_block(session, ctx, stats, start, end, flagged_only, *, subject_id) -> dict:
    from vitals.i18n import current_lang
    from vitals.analytics.body_metrics import METRIC_REGISTRY, display_name
    from vitals.services.body_scan.scans import queries as body_scan_queries

    lang = current_lang.get()
    scan_rows = await body_scan_queries.list_scans(
        session, start=start, end=end, subject_id=subject_id
    )
    rows = []
    for scan in scan_rows:
        metrics = []
        for m in scan.metrics:
            spec = METRIC_REGISTRY.get(m.metric_key)
            if spec is None or not spec.headline:
                continue
            metrics.append(
                {
                    "label": display_name(m.metric_key, lang) or m.metric_key,
                    "value": m.value,
                    # The registry's unit wins over the one read off the sheet:
                    # an InBody printout is in English, and "41,7 kg" sitting
                    # next to "33,7 %" is the document speaking two languages.
                    # Falls back for metrics the registry leaves unitless.
                    "unit": spec.unit or m.unit,
                }
            )
        if metrics:
            rows.append({"date": scan.date.isoformat(), "device": scan.device, "metrics": metrics})
    return {"scans": rows} if rows else {}


async def _labs_block(session, ctx, stats, start, end, flagged_only, *, subject_id) -> dict:
    """Every marker measured in the window, with its reference range and the two
    readings before it — a value without its range and its direction is a number
    a doctor has to go and look up."""
    from vitals.services.labs.flags import is_out_of_range
    from vitals.services.labs.results import list_results

    # Anchored at the window's end, not at "the newest results in the table": read
    # the other way round, a report about last spring is filled by every draw taken
    # since, and the markers it is actually about fall off the bottom of the cap.
    # ponytail: the cap now limits how far *back* history reaches, which is all the
    # two previous readings need.
    rows = await list_results(  # newest first
        session, end=end, limit=2000, subject_id=subject_id
    )
    by_marker: dict[str, list] = {}
    for r in rows:
        by_marker.setdefault(r.marker_key, []).append(r)

    markers = []
    for _marker_key, history in by_marker.items():
        current = next((r for r in history if r.date >= start), None)
        if current is None:
            continue
        if flagged_only and not is_out_of_range(current.flag):
            continue
        earlier = [r for r in history if r.date < current.date][:2]
        markers.append(
            {
                "marker": current.marker,
                "value": current.value,
                "unit": current.unit,
                "flag": current.flag,
                "date": current.date.isoformat(),
                "ref_low": current.ref_low,
                "ref_high": current.ref_high,
                # Oldest first, so the line reads left to right in time.
                "history": [
                    {"date": r.date.isoformat(), "value": r.value} for r in reversed(earlier)
                ],
            }
        )
    markers.sort(key=lambda m: (not is_out_of_range(m["flag"]), m["marker"]))
    return {"markers": markers} if markers else {}


async def _glp1_block(session, ctx, stats, start, end, flagged_only, *, subject_id) -> dict:

    phase = ctx.get("glp1") or {}
    injections = [
        i
        for i in await glp1_queries.list_injections(session, subject_id=subject_id)
        if start <= i.date <= end
    ]
    effects = [
        e
        for e in await glp1_queries.list_side_effects(session, subject_id=subject_id)
        if start <= e.date <= end
    ]
    current = (
        {"drug": phase.get("drug"), "dose_mg": phase.get("dose_mg")} if phase.get("drug") else None
    )
    if not current and not injections and not effects:
        return {}
    return {
        "current": current,
        "doses": [
            {"date": i.date.isoformat(), "drug": i.drug, "dose_mg": i.dose_mg}
            for i in sorted(injections, key=lambda x: x.date)
        ],
        "side_effects": [
            {
                "date": e.date.isoformat(),
                "effect_type": e.effect_type,
                "severity": e.severity,
            }
            for e in sorted(effects, key=lambda x: x.date)
        ],
    }


async def _hrt_block(session, ctx, stats, start, end, flagged_only, *, subject_id) -> dict:
    from vitals.i18n import current_lang
    from vitals.services.hrt import records

    hrt = ctx.get("hrt") or {}
    doses = await records.list_doses(session, start=start, end=end, subject_id=subject_id)
    effects = [
        e
        for e in await records.list_side_effects(session, subject_id=subject_id)
        if start <= e.date <= end
    ]
    cycle = hrt.get("cycle")
    if not cycle and not doses and not effects:
        return {}

    # A doctor gets the molecule's name, not the catalog slug ("test_enanthate").
    lang = current_lang.get()
    compounds = await records.list_compounds(session, subject_id=subject_id, active_only=False)
    names = {
        c.key: ((c.name_ru or c.name) if lang == "ru" else (c.name or c.name_ru)) for c in compounds
    }
    return {
        "cycle": (
            {
                "name": cycle.get("name"),
                "start_date": cycle.get("start_date"),
                "end_date": cycle.get("end_date"),
                "compounds": [names.get(k, k) for k in cycle.get("compounds") or ()],
            }
            if cycle
            else None
        ),
        "doses": [
            {
                "date": d.date.isoformat(),
                "compound": names.get(d.compound_key, d.compound_key),
                "dose": d.dose,
                "unit": d.unit,
            }
            for d in sorted(doses, key=lambda x: x.date)
        ],
        "side_effects": [
            {
                "date": e.date.isoformat(),
                "effect_type": e.effect_type,
                "severity": e.severity,
            }
            for e in sorted(effects, key=lambda x: x.date)
        ],
    }


async def _supplements_block(session, ctx, stats, start, end, flagged_only, *, subject_id) -> dict:

    items = await supplement_queries.list_supplements(
        session, subject_id=subject_id, active_only=True
    )
    return (
        {"items": [{"name": s.name, "dose": s.dose, "timing": s.timing} for s in items]}
        if items
        else {}
    )


async def _garmin_block(session, ctx, stats, start, end, flagged_only, *, subject_id) -> dict:
    """Averages over the window and over the one before it. Two lines, not a
    table of nights — a doctor is looking for the level, not the diary."""
    current = stats.get("current") or {}
    if not current.get("garmin_days"):
        return {}
    previous = stats.get("previous") or {}
    keys = ("sleep_hours", "sleep_score", "resting_hr", "hrv_avg", "steps")
    return {
        "days": current.get("garmin_days"),
        "current": {k: current.get(k) for k in keys},
        "previous": {k: previous.get(k) for k in keys},
    }


async def _workouts_block(session, ctx, stats, start, end, flagged_only, *, subject_id) -> dict:
    hevy = ctx.get("hevy") or {}
    current = stats.get("current") or {}
    if not current.get("workouts"):
        return {}
    return {
        "sessions": current.get("workouts"),
        "mean_gap_days": hevy.get("mean_gap_days"),
        "volume_per_session_kg": current.get("volume_per_session_kg"),
    }


async def _nutrition_block(session, ctx, stats, start, end, flagged_only, *, subject_id) -> dict:
    current = stats.get("current") or {}
    if not current.get("nutrition_days_logged"):
        return {}
    return {
        "days_logged": current.get("nutrition_days_logged"),
        "calories_per_day": current.get("calories_per_day"),
        "protein_per_day_g": current.get("protein_per_day_g"),
    }


async def _skincare_block(session, ctx, stats, start, end, flagged_only, *, subject_id) -> dict:
    skin = ctx.get("skincare") or {}
    if not skin:
        return {}
    observations = [
        {
            "date": o["date"],
            "inflammation": o["inflammation"],
            "pih": o["pih"],
            "zone": o["zone"],
        }
        for o in skin.get("recent_observations") or ()
        if start.isoformat() <= o["date"] <= end.isoformat()
    ]
    if not observations and not skin.get("active_products"):
        return {}
    return {"active_products": skin.get("active_products"), "observations": observations}


async def _genetics_block(session, ctx, stats, start, end, flagged_only, *, subject_id) -> dict:

    variants = await genetics_queries.list_variants(session, subject_id=subject_id)
    rows = [
        {
            "gene": v.gene,
            "rsid": v.rsid,
            "genotype": v.genotype,
            "interpretation": v.interpretation,
        }
        for v in variants
        if v.interpretation
    ]
    return {"variants": rows} if rows else {}


# ``_signals_block`` stood here: the symptoms a patient described in their own
# words, filtered down to the ones a clinician could actually read. It went with
# the signals domain. Nothing replaces it on the document — a symptom the patient
# typed is the one thing no device produces, so this is a real gap rather than a
# tidy-up, and it is the strongest argument for whatever captures free text next.


_BUILDERS = {
    Domain.WEIGHT.value: _weight_block,
    Domain.BODY_COMPOSITION.value: _body_comp_block,
    Domain.LABS.value: _labs_block,
    Domain.GLP1.value: _glp1_block,
    Domain.HRT.value: _hrt_block,
    Domain.SUPPLEMENTS.value: _supplements_block,
    Domain.GARMIN.value: _garmin_block,
    Domain.WORKOUTS.value: _workouts_block,
    Domain.NUTRITION.value: _nutrition_block,
    Domain.SKINCARE.value: _skincare_block,
    Domain.GENETICS.value: _genetics_block,
}


# ── Lifecycle ─────────────────────────────────────────────────────────────────
