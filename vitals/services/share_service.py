"""Doctor reports — build a frozen document and publish it behind a password.

Vitals had two exports and neither is readable by a person: ``export_full`` is a
machine dump of every table, ``export_llm`` a flat JSON for a chat window. This
is the third kind — one appointment's worth of data, chosen by domain and period,
events listed line by line and metrics reduced to aggregates, handed over as a
link plus a password.

Three properties do the work:

  * **Frozen.** The snapshot is built once and never refreshed. There is no
    "update" button: data moved, make a new report. A number a doctor read in the
    appointment still reads the same next week.
  * **Filtered twice.** A domain reaches the document only if it was ticked *and*
    its module is on. A module the owner switched off behaves as absent here
    exactly as it does everywhere else.
  * **Two independent clocks.** How much history the report covers
    (``period_start``/``period_end``) and how long the link stays alive
    (``expires_at``) have nothing to do with each other — a 180-day report can
    live for a week.

Most of the assembly is ``digest_service.assemble_context``, which already reads
every domain for a window. Three things are gathered directly on top of it,
because a doctor needs the full list where the digest only needs the headline:
every lab result in the period (not only the abnormal ones), every GLP-1/HRT dose
line by line, and the current supplement stack.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import date as date_type, datetime, timedelta
from typing import Any, Optional, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import Domain, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.share import SharedReport
from vitals.ownership import WriteIdentity
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.rls_session import enter_platform_scope
from vitals.utils.passwords import hash_password
from vitals.utils.timeutils import now_local, today_local

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1


class ShareOwnershipError(ValueError):
    """A report is outside, or corrupt within, its validated subject scope."""


class SharePreparedOwnerError(ShareOwnershipError):
    """A scoped report operation lacks a live service-issued owner proof."""


class _PublicReportOwnershipError(ShareOwnershipError):
    """Internal public-token validation failure, always mapped to not-found."""


class PreparedShareOwner:
    """Opaque exact-one owner proof bound to one session transaction.

    The capability keeps the identity-governance, subject, and active-owner locks
    alive while legacy whole-lake snapshot readers run.  It cannot be reused
    after a commit, rollback, or savepoint boundary.
    """

    __slots__ = (
        "_identity",
        "_identity_fingerprint",
        "_nested_transaction",
        "_owner_user_id",
        "_seal",
        "_session",
        "_transaction",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise SharePreparedOwnerError(
            "prepared share owners are issued only by prepare_legacy_owner"
        )

    @classmethod
    def _issue(
        cls,
        *,
        session: AsyncSession,
        identity: WriteIdentity,
        owner_user_id: uuid.UUID,
    ) -> "PreparedShareOwner":
        prepared = object.__new__(cls)
        object.__setattr__(prepared, "_identity", identity)
        object.__setattr__(prepared, "_owner_user_id", owner_user_id)
        object.__setattr__(
            prepared,
            "_identity_fingerprint",
            (identity.subject_id, identity.actor_user_id, owner_user_id),
        )
        object.__setattr__(prepared, "_session", session)
        object.__setattr__(
            prepared,
            "_transaction",
            session.sync_session.get_transaction(),
        )
        object.__setattr__(
            prepared,
            "_nested_transaction",
            session.sync_session.get_nested_transaction(),
        )
        object.__setattr__(prepared, "_seal", _PREPARED_OWNER_SEAL)
        return prepared

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedShareOwner is immutable")

    @property
    def identity(self) -> WriteIdentity:
        return self._identity


_PREPARED_OWNER_SEAL = object()


async def prepare_legacy_owner(
    session: AsyncSession,
    *,
    actor_username: str,
) -> PreparedShareOwner:
    """Lock and authenticate the exact-one legacy owner for one transaction."""
    from vitals.services.legacy_ownership import resolve_legacy_ownership_context

    await acquire_identity_governance_lock(session)
    ownership = await resolve_legacy_ownership_context(
        session,
        actor_username=actor_username,
    )
    identity = ownership.owner_action()
    with session.no_autoflush:
        subject_ids = list(
            await session.scalars(
                select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
            )
        )
        if subject_ids != [identity.subject_id]:
            raise SharePreparedOwnerError(
                "share compatibility requires exactly one matching health subject"
            )
        subject = await session.scalar(
            select(HealthSubject)
            .where(HealthSubject.id == identity.subject_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if subject is None or subject.owner_user_id != ownership.owner_user_id:
            raise SharePreparedOwnerError("share subject owner changed during validation")
        owner = await session.scalar(
            select(User)
            .where(User.id == ownership.owner_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None or owner.status != UserStatus.ACTIVE.value:
            raise SharePreparedOwnerError("share owner is missing or inactive")
        if identity.actor_user_id != owner.id:
            raise SharePreparedOwnerError("share actions require the active subject owner")
    if session.sync_session.get_transaction() is None:  # pragma: no cover
        raise SharePreparedOwnerError("share owner proof has no active transaction")
    return PreparedShareOwner._issue(
        session=session,
        identity=identity,
        owner_user_id=owner.id,
    )


def _require_prepared_owner(
    session: AsyncSession,
    prepared_owner: PreparedShareOwner,
) -> PreparedShareOwner:
    if not isinstance(prepared_owner, PreparedShareOwner):
        raise SharePreparedOwnerError(
            "prepared share owner belongs to another session"
        )
    try:
        identity = prepared_owner._identity
        owner_user_id = prepared_owner._owner_user_id
        valid_fingerprint = prepared_owner._identity_fingerprint == (
            identity.subject_id,
            identity.actor_user_id,
            owner_user_id,
        )
        valid_seal = prepared_owner._seal is _PREPARED_OWNER_SEAL
        prepared_session = prepared_owner._session
        transaction = prepared_owner._transaction
        nested_transaction = prepared_owner._nested_transaction
    except (AttributeError, TypeError) as exc:
        raise SharePreparedOwnerError(
            "prepared share owner is not a valid issued capability"
        ) from exc
    if not valid_seal or not valid_fingerprint:
        raise SharePreparedOwnerError(
            "prepared share owner identity was not issued by the validator"
        )
    if prepared_session is not session:
        raise SharePreparedOwnerError(
            "prepared share owner belongs to another session"
        )
    if session.sync_session.get_transaction() is not transaction:
        raise SharePreparedOwnerError(
            "prepared share owner transaction is no longer active"
        )
    if (
        session.sync_session.get_nested_transaction()
        is not nested_transaction
    ):
        raise SharePreparedOwnerError(
            "prepared share owner savepoint is no longer active"
        )
    return prepared_owner


async def _owner_or_zero_subject_legacy(
    session: AsyncSession,
    prepared_owner: PreparedShareOwner | None,
) -> PreparedShareOwner | None:
    """Validate a production capability or quarantine the old zero-subject API.

    Commercial startup always materializes one subject before serving traffic.
    The zero-subject arm exists only for direct legacy/service consumers and the
    pure snapshot test suite; it cannot authorize a production owner route.
    """
    if prepared_owner is not None:
        return _require_prepared_owner(session, prepared_owner)
    await acquire_identity_governance_lock(session)
    if await session.scalar(select(HealthSubject.id).limit(1)) is not None:
        raise SharePreparedOwnerError(
            "share operations require a prepared owner once identity exists"
        )
    return None

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
    Domain.SIGNALS.value: "signals",
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
            Domain.SIGNALS.value,
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
            Domain.SIGNALS.value,
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


def resolve_domains(
    domains: Sequence[str], enabled: Optional[dict[str, bool]] = None
) -> list[str]:
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
    """Load the service-validated Stage-3K boundary without an import cycle."""

    from vitals.services.shared_report_ownership_backfill_service import (
        SharedReportOwnershipBackfillError,
        shared_report_historical_bridge_state,
    )

    try:
        return await shared_report_historical_bridge_state(
            session,
            subject_id=subject_id,
        )
    except SharedReportOwnershipBackfillError as exc:
        error = (
            _PublicReportOwnershipError
            if public
            else ShareOwnershipError
        )
        raise error(
            "shared-report migration checkpoint is not authoritative"
        ) from exc


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
    if subject_id is None:
        if created_by_user_id is not None or revoked_by_user_id is not None:
            raise error_type(
                "shared report has partial legacy ownership roots"
            )
        if not checkpoint_absent and (
            bridge_state.completed
            or report_id <= bridge_state.processed_high_watermark_id
            or not within_snapshot
        ):
            raise error_type(
                "fully-unowned shared report is outside the historical bridge"
            )
        return True
    if revoked_by_user_id is not None and revoked_at is None:
        raise error_type(
            "shared report has revocation actor without revocation timestamp"
        )
    if subject_id != expected_subject_id:
        raise error_type("shared report belongs to another subject")
    if not checkpoint_absent and not within_snapshot:
        if created_by_user_id != owner_user_id:
            raise error_type(
                "live shared report creator does not own its health subject"
            )
        if (revoked_at is None) != (revoked_by_user_id is None):
            raise error_type(
                "live shared report has inconsistent revocation provenance"
            )
    for actor_user_id in (created_by_user_id, revoked_by_user_id):
        # An exact-S row may legitimately lack historical actor provenance.  A
        # non-null actor, however, must be the subject owner.
        if actor_user_id is not None and actor_user_id != owner_user_id:
            raise error_type(
                "shared report has a foreign actor for its health subject"
            )
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
                    SharedReport.id
                    <= bridge_state.processed_high_watermark_id,
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
                select(*root_columns)
                .where(predicate)
                .order_by(SharedReport.id)
                .limit(1)
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
        raise SharePreparedOwnerError(
            "composing a report requires the subject it is about"
        )

    from vitals.config import load_config
    from vitals.i18n import current_lang
    from vitals.services import digest_service

    chosen = resolve_domains(domains, enabled)
    start, end = clamp_window(period_start, period_end)
    span_days = max((end - start).days + 1, 1)
    ctx = await digest_service.assemble_context(
        session,
        subject_id=owner._identity.subject_id,
        on_date=end,
        period_days=span_days,
        mode=digest_service.REPORT_MODE_CLOSED,
        enabled_modules=enabled,
        max_period_days=max(PERIOD_CHOICES),
    )
    stats = ctx.get("period_stats") or {}
    cfg = load_config()

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
        "profile": {
            "age": cfg.user_age,
            "sex": cfg.sex,
            "height_cm": cfg.height_cm,
        },
        # What the document actually holds, not what was ticked. An empty domain
        # draws no section, so a contents line naming one sends a doctor looking
        # for labs that never arrived — and a missing section reads as "nothing
        # to report" when it means "nothing in this window".
        "domains": [d for d in chosen if d in blocks],
        "labs_flagged_only": bool(labs_flagged_only),
        "blocks": blocks,
    }


async def _weight_block(
    session, ctx, stats, start, end, flagged_only, *, subject_id
) -> dict:
    """Where the scale started and where it ended, plus the series for the chart.

    Points come from ``chart_series`` rather than the raw table so the average
    drawn here is the same noise-excluded one the owner sees on /weight.
    """
    from vitals.services import weight_service

    series = await weight_service.chart_series(session, subject_id=subject_id)
    in_window = [
        p for p in series["raw"] if start.isoformat() <= p["date"] <= end.isoformat()
    ]
    ma_window = [
        p for p in series["trend_ma"] if start.isoformat() <= p["date"] <= end.isoformat()
    ]
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


async def _body_comp_block(
    session, ctx, stats, start, end, flagged_only, *, subject_id
) -> dict:
    from vitals.i18n import current_lang
    from vitals.services import body_scan_service
    from vitals.services.analytics.body_metrics import METRIC_REGISTRY, display_name

    lang = current_lang.get()
    scans = await body_scan_service.list_scans(
        session, start=start, end=end, subject_id=subject_id
    )
    rows = []
    for scan in scans:
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
            rows.append(
                {"date": scan.date.isoformat(), "device": scan.device, "metrics": metrics}
            )
    return {"scans": rows} if rows else {}


async def _labs_block(
    session, ctx, stats, start, end, flagged_only, *, subject_id
) -> dict:
    """Every marker measured in the window, with its reference range and the two
    readings before it — a value without its range and its direction is a number
    a doctor has to go and look up."""
    from vitals.services import labs_service

    # Anchored at the window's end, not at "the newest results in the table": read
    # the other way round, a report about last spring is filled by every draw taken
    # since, and the markers it is actually about fall off the bottom of the cap.
    # ponytail: the cap now limits how far *back* history reaches, which is all the
    # two previous readings need.
    rows = await labs_service.list_results(  # newest first
        session, end=end, limit=2000, subject_id=subject_id
    )
    by_marker: dict[str, list] = {}
    for r in rows:
        by_marker.setdefault(r.marker, []).append(r)

    markers = []
    for marker, history in by_marker.items():
        current = next((r for r in history if r.date >= start), None)
        if current is None:
            continue
        if flagged_only and not labs_service.is_out_of_range(current.flag):
            continue
        earlier = [r for r in history if r.date < current.date][:2]
        markers.append(
            {
                "marker": marker,
                "value": current.value,
                "unit": current.unit,
                "flag": current.flag,
                "date": current.date.isoformat(),
                "ref_low": current.ref_low,
                "ref_high": current.ref_high,
                # Oldest first, so the line reads left to right in time.
                "history": [
                    {"date": r.date.isoformat(), "value": r.value}
                    for r in reversed(earlier)
                ],
            }
        )
    markers.sort(key=lambda m: (not labs_service.is_out_of_range(m["flag"]), m["marker"]))
    return {"markers": markers} if markers else {}


async def _glp1_block(
    session, ctx, stats, start, end, flagged_only, *, subject_id
) -> dict:
    from vitals.services import glp1_service

    phase = ctx.get("glp1") or {}
    injections = [
        i
        for i in await glp1_service.list_injections(session, subject_id=subject_id)
        if start <= i.date <= end
    ]
    effects = [
        e
        for e in await glp1_service.list_side_effects(
            session, subject_id=subject_id
        )
        if start <= e.date <= end
    ]
    current = (
        {"drug": phase.get("drug"), "dose_mg": phase.get("dose_mg")}
        if phase.get("drug")
        else None
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


async def _hrt_block(
    session, ctx, stats, start, end, flagged_only, *, subject_id
) -> dict:
    from vitals.i18n import current_lang
    from vitals.services import hrt_service

    hrt = ctx.get("hrt") or {}
    doses = await hrt_service.list_doses(
        session, start=start, end=end, subject_id=subject_id
    )
    effects = [
        e
        for e in await hrt_service.list_side_effects(session, subject_id=subject_id)
        if start <= e.date <= end
    ]
    cycle = hrt.get("cycle")
    if not cycle and not doses and not effects:
        return {}

    # A doctor gets the molecule's name, not the catalog slug ("test_enanthate").
    lang = current_lang.get()
    compounds = await hrt_service.list_compounds(
        session, subject_id=subject_id, active_only=False
    )
    names = {
        c.key: ((c.name_ru or c.name) if lang == "ru" else (c.name or c.name_ru))
        for c in compounds
    }
    return {
        "cycle": (
            {
                "name": cycle.get("name"),
                "start_date": cycle.get("start_date"),
                "end_date": cycle.get("end_date"),
                "compounds": [
                    names.get(k, k) for k in cycle.get("compounds") or ()
                ],
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


async def _supplements_block(
    session, ctx, stats, start, end, flagged_only, *, subject_id
) -> dict:
    from vitals.services import supplements_service

    items = await supplements_service.list_supplements(
        session, subject_id=subject_id, active_only=True
    )
    return {
        "items": [
            {"name": s.name, "dose": s.dose, "timing": s.timing} for s in items
        ]
    } if items else {}


async def _garmin_block(
    session, ctx, stats, start, end, flagged_only, *, subject_id
) -> dict:
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


async def _workouts_block(
    session, ctx, stats, start, end, flagged_only, *, subject_id
) -> dict:
    hevy = ctx.get("hevy") or {}
    current = stats.get("current") or {}
    if not current.get("workouts"):
        return {}
    return {
        "sessions": current.get("workouts"),
        "mean_gap_days": hevy.get("mean_gap_days"),
        "volume_per_session_kg": current.get("volume_per_session_kg"),
    }


async def _nutrition_block(
    session, ctx, stats, start, end, flagged_only, *, subject_id
) -> dict:
    current = stats.get("current") or {}
    if not current.get("nutrition_days_logged"):
        return {}
    return {
        "days_logged": current.get("nutrition_days_logged"),
        "calories_per_day": current.get("calories_per_day"),
        "protein_per_day_g": current.get("protein_per_day_g"),
    }


async def _skincare_block(
    session, ctx, stats, start, end, flagged_only, *, subject_id
) -> dict:
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


async def _genetics_block(
    session, ctx, stats, start, end, flagged_only, *, subject_id
) -> dict:
    from vitals.services import genetics_service

    variants = await genetics_service.list_variants(session, subject_id=subject_id)
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


async def _signals_block(
    session, ctx, stats, start, end, flagged_only, *, subject_id
) -> dict:
    """What the patient said about how he felt — symptoms, in his own words.

    ``state`` rows are a daily mood/energy score he keeps for himself; on a
    clinical document they are noise between the symptoms that matter.

    Two filters that only a real document makes obvious. A row with no note
    carries nothing but its normalized key — "low_heart_rate" is this app's
    vocabulary, not a complaint, and a doctor reading it learns nothing. And
    ``value_num`` is a 1-5 severity for a symptom but a raw measurement for
    anything the parser tagged loosely, which is how "40 of 5" ends up on a
    clinical document; outside that range it is not a severity and is dropped.
    """
    from vitals.enums import SignalKind
    from vitals.services import signals_service

    rows = await signals_service.list_signals(
        session,
        kind=SignalKind.SYMPTOM.value,
        start=start,
        end=end,
        limit=1000,
        subject_id=subject_id,
    )
    items = [
        {
            "date": s.date.isoformat(),
            "what": (s.note or "").strip(),
            "severity": (
                int(s.value_num)
                if s.value_num is not None and 1 <= s.value_num <= 5
                else None
            ),
        }
        for s in rows
        if (s.note or "").strip()
    ]
    items.sort(key=lambda x: x["date"])
    return {"symptoms": items} if items else {}


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
    Domain.SIGNALS.value: _signals_block,
}


# ── Lifecycle ─────────────────────────────────────────────────────────────────


async def create_report(
    session: AsyncSession,
    *,
    title: str,
    domains: Sequence[str],
    period_start: date_type,
    period_end: date_type,
    expires_days: int = DEFAULT_EXPIRY_DAYS,
    note: Optional[str] = None,
    labs_flagged_only: bool = False,
    preset: Optional[str] = None,
    enabled: Optional[dict[str, bool]] = None,
    prepared_owner: PreparedShareOwner | None = None,
) -> tuple[SharedReport, str]:
    """Freeze a document and publish it. Flushes; the caller commits.

    Returns the row **and the plaintext password**, which exists only in this
    return value — after this call there is nothing but the bcrypt hash.
    """
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)
    snapshot = await build_snapshot(
        session,
        domains=domains,
        period_start=period_start,
        period_end=period_end,
        labs_flagged_only=labs_flagged_only,
        enabled=enabled,
        prepared_owner=owner,
    )
    password = generate_password()
    row = SharedReport(
        subject_id=(owner._identity.subject_id if owner is not None else None),
        created_by_user_id=(
            owner._identity.actor_user_id if owner is not None else None
        ),
        token=secrets.token_urlsafe(32),
        password_hash=hash_password(password),
        title=title.strip()[:120],
        preset=preset,
        domains=snapshot["domains"],
        period_start=date_type.fromisoformat(snapshot["period"]["start"]),
        period_end=date_type.fromisoformat(snapshot["period"]["end"]),
        labs_flagged_only=bool(labs_flagged_only),
        note=(note or "").strip() or None,
        snapshot=snapshot,
        expires_at=now_local() + timedelta(days=max(int(expires_days), 1)),
    )
    session.add(row)
    await session.flush()
    return row, password


async def list_reports(
    session: AsyncSession,
    *,
    prepared_owner: PreparedShareOwner | None = None,
) -> Sequence[SharedReport]:
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)
    if owner is None:
        stmt = select(SharedReport).where(
            SharedReport.subject_id.is_(None),
            SharedReport.created_by_user_id.is_(None),
            SharedReport.revoked_by_user_id.is_(None),
        )
    else:
        bridge_state = await _reject_selected_scope_corruption(session, owner)
        stmt = select(SharedReport).where(_owner_scope(owner))
    result = await session.execute(
        stmt.order_by(SharedReport.created_at.desc(), SharedReport.id.desc())
        .execution_options(populate_existing=True)
    )
    rows = result.scalars().all()
    if owner is not None:
        for row in rows:
            _validate_owner_roots(
                owner,
                report_id=row.id,
                subject_id=row.subject_id,
                created_by_user_id=row.created_by_user_id,
                revoked_by_user_id=row.revoked_by_user_id,
                revoked_at=row.revoked_at,
                bridge_state=bridge_state,
            )
    return rows


async def get_report(
    session: AsyncSession,
    report_id: int,
    *,
    prepared_owner: PreparedShareOwner | None = None,
) -> Optional[SharedReport]:
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)
    if owner is None:
        stmt = select(SharedReport).where(
            SharedReport.id == report_id,
            SharedReport.subject_id.is_(None),
            SharedReport.created_by_user_id.is_(None),
            SharedReport.revoked_by_user_id.is_(None),
        )
    else:
        bridge_state = await _reject_selected_scope_corruption(session, owner)
        stmt = select(SharedReport).where(
            SharedReport.id == report_id,
            _owner_scope(owner),
        )
    row = await session.scalar(stmt.execution_options(populate_existing=True))
    if row is None:
        return None
    if owner is None:
        return row
    _validate_owner_roots(
        owner,
        report_id=row.id,
        subject_id=row.subject_id,
        created_by_user_id=row.created_by_user_id,
        revoked_by_user_id=row.revoked_by_user_id,
        revoked_at=row.revoked_at,
        bridge_state=bridge_state,
    )
    return row


async def _public_subject_owner(
    session: AsyncSession,
    *,
    report_id: int,
    subject_id: uuid.UUID | None,
    created_by_user_id: uuid.UUID | None,
    revoked_by_user_id: uuid.UUID | None,
    revoked_at: datetime | None,
    for_update: bool,
) -> tuple[uuid.UUID, uuid.UUID, Any]:
    """Validate roots selected by an opaque public token, never infer actors."""
    if revoked_by_user_id is not None and revoked_at is None:
        raise _PublicReportOwnershipError(
            "public report has revocation actor without revocation timestamp"
        )
    if subject_id is None:
        if created_by_user_id is not None or revoked_by_user_id is not None:
            raise _PublicReportOwnershipError(
                "public report has partial legacy ownership roots"
            )
        from vitals.services.legacy_ownership import (
            LegacyOwnershipError,
            resolve_legacy_ownership_context,
        )

        try:
            ownership = await resolve_legacy_ownership_context(
                session,
                actor_username=None,
            )
        except LegacyOwnershipError as exc:
            raise _PublicReportOwnershipError(
                "public legacy report requires exactly one active owner"
            ) from exc
        resolved_subject_id = ownership.subject_id
        owner_user_id = ownership.owner_user_id
    else:
        resolved_subject_id = subject_id
        subject_stmt = select(HealthSubject).where(
            HealthSubject.id == resolved_subject_id
        )
        if for_update:
            subject_stmt = subject_stmt.with_for_update().execution_options(
                populate_existing=True
            )
        else:
            subject_stmt = subject_stmt.execution_options(populate_existing=True)
        subject = await session.scalar(subject_stmt)
        if subject is None:
            raise _PublicReportOwnershipError("public report subject is missing")
        owner_user_id = subject.owner_user_id

    if subject_id is None and for_update:
        subject = await session.scalar(
            select(HealthSubject)
            .where(HealthSubject.id == resolved_subject_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if subject is None or subject.owner_user_id != owner_user_id:
            raise _PublicReportOwnershipError(
                "public report subject owner changed during validation"
            )

    owner_stmt = select(User).where(User.id == owner_user_id)
    if for_update:
        owner_stmt = owner_stmt.with_for_update().execution_options(
            populate_existing=True
        )
    else:
        owner_stmt = owner_stmt.execution_options(populate_existing=True)
    owner = await session.scalar(owner_stmt)
    if owner is None or owner.status != UserStatus.ACTIVE.value:
        raise _PublicReportOwnershipError(
            "public report owner is missing or inactive"
        )
    bridge_state = await _historical_bridge_state(
        session,
        subject_id=resolved_subject_id,
        public=True,
    )
    _validate_report_roots(
        report_id=report_id,
        expected_subject_id=resolved_subject_id,
        owner_user_id=owner_user_id,
        subject_id=subject_id,
        created_by_user_id=created_by_user_id,
        revoked_by_user_id=revoked_by_user_id,
        revoked_at=revoked_at,
        bridge_state=bridge_state,
        error_type=_PublicReportOwnershipError,
    )
    return resolved_subject_id, owner_user_id, bridge_state


def _report_is_publicly_live(row: SharedReport) -> bool:
    return bool(
        row.revoked_at is None
        and row.snapshot is not None
        and row.expires_at > now_local()
    )


async def resolve_public(session: AsyncSession, token: str) -> Optional[SharedReport]:
    """The row behind a public token, or ``None``.

    One ``None`` for all four ways a link can fail — unknown, revoked, expired,
    purged — because the visitor must not be able to tell them apart, and a page
    that says "this was revoked" tells them.
    """
    if not token:
        return None
    # No account, so no subject to bind: the token is what authorizes this
    # read, and the row is one the policies would otherwise hide from a visitor
    # who is entitled to see it.
    await enter_platform_scope(session)
    await acquire_identity_governance_lock(session)
    row = await session.scalar(
        select(SharedReport)
        .where(SharedReport.token == token)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    try:
        await _public_subject_owner(
            session,
            report_id=row.id,
            subject_id=row.subject_id,
            created_by_user_id=row.created_by_user_id,
            revoked_by_user_id=row.revoked_by_user_id,
            revoked_at=row.revoked_at,
            for_update=False,
        )
    except _PublicReportOwnershipError:
        logger.warning(
            "shared report %s has invalid public ownership roots",
            row.id,
        )
        return None
    return row if _report_is_publicly_live(row) else None


async def register_open(
    session: AsyncSession,
    token: str,
) -> Optional[SharedReport]:
    """Lock and count one still-live token after password verification."""
    if not token:
        return None
    # Same visitor, same token, one step later: still no account to bind.
    await enter_platform_scope(session)
    await acquire_identity_governance_lock(session)
    roots = (
        await session.execute(
            select(
                SharedReport.id,
                SharedReport.subject_id,
                SharedReport.created_by_user_id,
                SharedReport.revoked_by_user_id,
                SharedReport.revoked_at,
            ).where(SharedReport.token == token)
        )
    ).one_or_none()
    if roots is None:
        return None
    report_id, subject_id, created_by_user_id, revoked_by_user_id, revoked_at = roots
    try:
        await _public_subject_owner(
            session,
            report_id=report_id,
            subject_id=subject_id,
            created_by_user_id=created_by_user_id,
            revoked_by_user_id=revoked_by_user_id,
            revoked_at=revoked_at,
            for_update=True,
        )
    except _PublicReportOwnershipError:
        logger.warning(
            "shared report %s has invalid open ownership roots",
            report_id,
        )
        return None
    row = await session.scalar(
        select(SharedReport)
        .where(SharedReport.id == report_id, SharedReport.token == token)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    if (
        row.subject_id != subject_id
        or row.created_by_user_id != created_by_user_id
        or row.revoked_by_user_id != revoked_by_user_id
        or row.revoked_at != revoked_at
    ):
        logger.warning(
            "shared report %s ownership changed while registering an open",
            report_id,
        )
        return None
    if not _report_is_publicly_live(row):
        return None
    row.opened_count = (row.opened_count or 0) + 1
    row.last_opened_at = now_local()
    await session.flush()
    return row


async def _lock_owner_report(
    session: AsyncSession,
    report_id: int,
    *,
    prepared_owner: PreparedShareOwner,
) -> SharedReport | None:
    owner = _require_prepared_owner(session, prepared_owner)
    bridge_state = await _reject_selected_scope_corruption(session, owner)
    roots = (
        await session.execute(
            select(
                SharedReport.subject_id,
                SharedReport.created_by_user_id,
                SharedReport.revoked_by_user_id,
                SharedReport.revoked_at,
            ).where(
                SharedReport.id == report_id,
                _owner_scope(owner),
            )
        )
    ).one_or_none()
    if roots is None:
        return None
    subject_id, created_by_user_id, revoked_by_user_id, revoked_at = roots
    _validate_owner_roots(
        owner,
        report_id=report_id,
        subject_id=subject_id,
        created_by_user_id=created_by_user_id,
        revoked_by_user_id=revoked_by_user_id,
        revoked_at=revoked_at,
        bridge_state=bridge_state,
    )
    row = await session.scalar(
        select(SharedReport)
        .where(
            SharedReport.id == report_id,
            _owner_scope(owner),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    _validate_owner_roots(
        owner,
        report_id=row.id,
        subject_id=row.subject_id,
        created_by_user_id=row.created_by_user_id,
        revoked_by_user_id=row.revoked_by_user_id,
        revoked_at=row.revoked_at,
        bridge_state=bridge_state,
    )
    return row


async def revoke(
    session: AsyncSession,
    report_id: int,
    *,
    prepared_owner: PreparedShareOwner | None = None,
) -> bool:
    """Kill the link now. The snapshot goes with it — a revoked report is one the
    owner decided should stop existing, not one to keep a copy of."""
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)
    if owner is None:
        row = await session.scalar(
            select(SharedReport)
            .where(
                SharedReport.id == report_id,
                SharedReport.subject_id.is_(None),
                SharedReport.created_by_user_id.is_(None),
                SharedReport.revoked_by_user_id.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    else:
        row = await _lock_owner_report(
            session,
            report_id,
            prepared_owner=owner,
        )
    if row is None or row.revoked_at is not None:
        return False
    if owner is not None:
        if row.subject_id is None:
            row.subject_id = owner._identity.subject_id
        # Preserve a known creator and preserve NULL when legacy history did not
        # record one; only this authenticated lifecycle action gets a new actor.
        row.revoked_by_user_id = owner._identity.actor_user_id
    row.revoked_at = now_local()
    row.snapshot = None
    await session.flush()
    return True


async def delete_report(
    session: AsyncSession,
    report_id: int,
    *,
    prepared_owner: PreparedShareOwner | None = None,
) -> bool:
    owner = await _owner_or_zero_subject_legacy(session, prepared_owner)
    if owner is None:
        row = await session.scalar(
            select(SharedReport)
            .where(
                SharedReport.id == report_id,
                SharedReport.subject_id.is_(None),
                SharedReport.created_by_user_id.is_(None),
                SharedReport.revoked_by_user_id.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    else:
        row = await _lock_owner_report(
            session,
            report_id,
            prepared_owner=owner,
        )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def purge_expired(session: AsyncSession, *, now: Optional[datetime] = None) -> int:
    """Empty the snapshot of every dead link; keep the metadata.

    An expired report is unreachable already — this is about not keeping a full
    copy of the medical record for every appointment ever attended. The row stays
    so /share can still say what was shared and when.
    """
    await acquire_identity_governance_lock(session)
    moment = now or now_local()
    root_rows = list(
        await session.execute(
            select(
                SharedReport.id,
                SharedReport.subject_id,
                SharedReport.created_by_user_id,
                SharedReport.revoked_by_user_id,
                SharedReport.revoked_at,
            )
            .where(SharedReport.expires_at <= moment)
            .where(SharedReport.snapshot.is_not(None))
            .order_by(SharedReport.id)
        )
    )
    if not root_rows:
        return 0

    legacy_owner: tuple[uuid.UUID, uuid.UUID] | None = None
    subject_ids = {
        row.subject_id for row in root_rows if row.subject_id is not None
    }
    if any(row.subject_id is None for row in root_rows):
        null_rows = [row for row in root_rows if row.subject_id is None]
        if any(
            row.created_by_user_id is not None or row.revoked_by_user_id is not None
            for row in null_rows
        ):
            raise ShareOwnershipError(
                "expired shared report has partial legacy ownership roots"
            )
        # A fully-null report has no stored S to lock.  Under governance, map it
        # only when there is exactly one subject, then validate that subject and
        # its owner through the same ordered locks below.  Owner suspension must
        # not retain expired PHI, and this actorless purge never adopts the roots.
        with session.no_autoflush:
            legacy_subjects = list(
                await session.execute(
                    select(HealthSubject.id, HealthSubject.owner_user_id)
                    .order_by(HealthSubject.id)
                    .limit(2)
                )
            )
        if len(legacy_subjects) != 1:
            raise ShareOwnershipError(
                "expired legacy reports require exactly one health subject"
            )
        legacy_subject_id, legacy_owner_user_id = legacy_subjects[0]
        legacy_owner = (legacy_subject_id, legacy_owner_user_id)
        subject_ids.add(legacy_subject_id)

    subjects = {
        subject.id: subject
        for subject in await session.scalars(
            select(HealthSubject)
            .where(HealthSubject.id.in_(tuple(subject_ids)))
            .order_by(HealthSubject.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    if set(subjects) != subject_ids:
        raise ShareOwnershipError("expired shared report subject is missing")
    if legacy_owner is not None:
        legacy_subject_id, legacy_owner_user_id = legacy_owner
        if subjects[legacy_subject_id].owner_user_id != legacy_owner_user_id:
            raise ShareOwnershipError(
                "expired legacy report owner changed during purge"
            )
    owner_ids = {subject.owner_user_id for subject in subjects.values()}
    owners = {
        owner.id: owner
        for owner in await session.scalars(
            select(User)
            .where(User.id.in_(tuple(owner_ids)))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    # Suspension closes public access, but it must not retain an already-expired
    # PHI snapshot.  Purge is actorless data minimization: the owner root must
    # still exist and match the subject/actor graph, but it need not be active.
    if any(owners.get(owner_id) is None for owner_id in owner_ids):
        raise ShareOwnershipError("expired shared report owner is missing")

    bridge_states = {
        subject_id: await _historical_bridge_state(
            session,
            subject_id=subject_id,
        )
        for subject_id in sorted(subject_ids, key=str)
    }
    expected_roots = {}
    for root in root_rows:
        if root.subject_id is None:
            assert legacy_owner is not None
            expected_subject_id, owner_user_id = legacy_owner
        else:
            expected_subject_id = root.subject_id
            owner_user_id = subjects[root.subject_id].owner_user_id
        _validate_report_roots(
            report_id=root.id,
            expected_subject_id=expected_subject_id,
            owner_user_id=owner_user_id,
            subject_id=root.subject_id,
            created_by_user_id=root.created_by_user_id,
            revoked_by_user_id=root.revoked_by_user_id,
            revoked_at=root.revoked_at,
            bridge_state=bridge_states[expected_subject_id],
        )
        expected_roots[root.id] = (
            root.subject_id,
            root.created_by_user_id,
            root.revoked_by_user_id,
            root.revoked_at,
        )

    rows = list(
        await session.scalars(
            select(SharedReport)
            .where(SharedReport.id.in_(tuple(expected_roots)))
            .order_by(SharedReport.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if {row.id for row in rows} != set(expected_roots):
        raise ShareOwnershipError("expired shared report changed during purge")
    purged = 0
    for row in rows:
        if (
            row.subject_id,
            row.created_by_user_id,
            row.revoked_by_user_id,
            row.revoked_at,
        ) != expected_roots[row.id]:
            raise ShareOwnershipError(
                "expired shared report ownership changed during purge"
            )
        if row.expires_at > moment or row.snapshot is None:
            continue
        row.snapshot = None
        purged += 1
    await session.flush()
    return purged


async def purge_job(session_factory, redis=None) -> None:
    """Daily sweep — see :func:`purge_expired`."""
    async with session_factory() as session:
        # Housekeeping across every subject's expired snapshots: there is no
        # person this job acts as.
        await enter_platform_scope(session)
        purged = await purge_expired(session)
        await session.commit()
    if purged:
        logger.info("shared reports: cleared %s expired snapshot(s)", purged)


def window_for(days: int) -> tuple[date_type, date_type]:
    """``days`` **complete** days, ending yesterday.

    Counting back from today would hand the document a day with no sleep in it
    and, before dinner, no food either — and then average over it.
    """
    end = today_local() - timedelta(days=1)
    return end - timedelta(days=max(days, 1) - 1), end


def clamp_window(start: date_type, end: date_type) -> tuple[date_type, date_type]:
    """A window the reader picked, trimmed to days that are actually over."""
    yesterday = today_local() - timedelta(days=1)
    end = min(end, yesterday)
    return min(start, end), end


def default_period(days: int = 90) -> tuple[date_type, date_type]:
    """What the custom-range inputs open on."""
    return window_for(days)


async def earliest_data_date(
    session: AsyncSession,
    *,
    prepared_owner: PreparedShareOwner | None = None,
) -> Optional[date_type]:
    """The oldest dated row in any domain a report can carry — what "all time"
    means. Nine cheap ``MIN()`` reads on one form submit, so a report that says
    it covers everything starts where the record actually starts rather than at
    some round number of years ago."""
    await _owner_or_zero_subject_legacy(session, prepared_owner)

    from sqlalchemy import func

    from vitals.models.body_scan import BodyScan
    from vitals.models.garmin import GarminDaily
    from vitals.models.glp1 import Injection
    from vitals.models.hevy import HevyWorkout
    from vitals.models.hrt import HrtDose
    from vitals.models.labs import LabResult
    from vitals.models.nutrition import MealLog
    from vitals.models.signals import Signal
    from vitals.models.weight import WeightLog

    columns = (
        WeightLog.date, LabResult.date, GarminDaily.date, HevyWorkout.date,
        MealLog.date, HrtDose.date, Injection.date, BodyScan.date, Signal.date,
    )
    found = []
    for column in columns:
        value = (await session.execute(select(func.min(column)))).scalar()
        if value is not None:
            found.append(value)
    return min(found) if found else None
