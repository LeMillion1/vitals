"""Weight & Body Composition service (Phase 1).

Owns the business rules for the weight domain:

  * **Manual-over-Garmin priority** — at most one *active* weight per date; a
    manual entry supersedes a Garmin import for the same date (the Garmin row is
    kept but flagged ``superseded`` — data-lake principle, never delete).
  * **Navy body-fat + LBM** computed on measurement write (LBM needs the day's
    active weight, so it's null until one exists).
  * **Noise ranges** excluded from the trend / projection.
  * **info alerts** — a noisy-weight period being active.
  * **Chart series** assembly for the dashboard (raw points + 7-day MA + LBM +
    optional goal projection).

Every mutating fn runs the conflict-engine override plumbing (``enforce``) so the
override UX is wired end-to-end even though real cross-domain weight rules land
with later modules.
"""
from __future__ import annotations

import math
import uuid
from datetime import date as date_type, timedelta
from typing import TYPE_CHECKING, Optional, Sequence

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import Config, load_config
from vitals.enums import (
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    IntegrationConnectionStatus,
    Severity,
    Source,
)
from vitals.i18n import t
from vitals.models.weight import (
    DOMAIN,
    BodyMeasurement,
    NoiseMarker,
    ProgressPhoto,
    WeightLog,
)
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset
from vitals.ownership import WriteIdentity
from vitals.services import alerts_service, conflict_engine
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.analytics import exclude_ranges
from vitals.services.analytics.navy import lean_body_mass_kg, navy_body_fat_pct
from vitals.services.analytics.regression import fit_trend, project_date_for_value
from vitals.services.analytics.rolling import rolling_mean_by_date
from vitals.utils.timeutils import today_local

if TYPE_CHECKING:
    from vitals.services.garmin_weight_service import (
        GarminWeightExportContext,
        PreparedGarminWeightExport,
    )

NOISE_ALERT_KEY = "weight.noisy_period_active"


class WeightOwnershipError(ValueError):
    """A weight-domain row cannot be used inside the requested subject scope."""


class WeightScopedUniqueCutoverRequiredError(WeightOwnershipError):
    """A global date key is occupied by another ownership scope."""


_PREPARED_WEIGHT_WRITE_SEAL = object()
_ORIGIN_ACTOR_UNSET = object()


class PreparedWeightWrite:
    """Opaque proof that Weight's governance/advisory order was established.

    The generic conflict capability proves identity, transaction, and subject
    locks. Weight additionally has to prove that the Garmin outbox advisory was
    acquired *before* those subject locks. Only :func:`prepare_weight_write`
    issues this wrapper.
    """

    __slots__ = ("_garmin_export", "_prepared", "_seal", "_session")

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise conflict_engine.ConflictPreparedWriteError(
            "prepared weight writes are issued only by prepare_weight_write"
        )

    @classmethod
    def _issue(
        cls,
        *,
        session: AsyncSession,
        prepared: conflict_engine.PreparedConflictWrite,
        garmin_export: "PreparedGarminWeightExport | None",
    ) -> PreparedWeightWrite:
        token = object.__new__(cls)
        object.__setattr__(token, "_prepared", prepared)
        object.__setattr__(token, "_session", session)
        object.__setattr__(token, "_seal", _PREPARED_WEIGHT_WRITE_SEAL)
        object.__setattr__(token, "_garmin_export", garmin_export)
        return token

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedWeightWrite is immutable")

    @property
    def context(self) -> conflict_engine.ConflictWriteContext:
        return self._prepared.context

    @property
    def conflict_write(self) -> conflict_engine.PreparedConflictWrite:
        return self._prepared

    @property
    def garmin_weight_export(self) -> "PreparedGarminWeightExport | None":
        """Prepared destination outbox, distinct from the Weight origin roots."""

        return self._garmin_export


async def prepare_weight_write(
    session: AsyncSession,
    *,
    context: conflict_engine.ConflictWriteContext,
    garmin_weight_export_context: "GarminWeightExportContext | None" = None,
) -> PreparedWeightWrite:
    """Prepare a scoped Weight mutation in the canonical lock order.

    Identity governance precedes the installation-wide Garmin outbox advisory;
    the generic conflict preparation then locks the subject and actor roots.
    """

    from vitals.services import garmin_weight_service

    await acquire_identity_governance_lock(session)
    await garmin_weight_service.lock_active_weight_change(session)
    prepared = await conflict_engine.prepare_scoped_write(
        session,
        context=context,
    )
    prepared_export = None
    if garmin_weight_export_context is not None:
        if garmin_weight_export_context.identity != context.identity:
            raise conflict_engine.ConflictPreparedWriteError(
                "Garmin Weight export identity does not match Weight identity"
            )
        if garmin_weight_export_context.legacy_bridge is not context.legacy_bridge:
            raise conflict_engine.ConflictPreparedWriteError(
                "Garmin Weight export bridge does not match Weight bridge"
            )
        try:
            prepared_export = await garmin_weight_service.prepare_scoped_export(
                session,
                context=garmin_weight_export_context,
            )
        except garmin_weight_service.GarminWeightExportConnectionInactiveError:
            # Garmin is an optional destination. A disabled/retired account must
            # stop outbox projection, not block the local health correction.
            prepared_export = None
    return PreparedWeightWrite._issue(
        session=session,
        prepared=prepared,
        garmin_export=prepared_export,
    )


def _require_scoped_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity | None,
    prepared: PreparedWeightWrite | None,
) -> conflict_engine.ConflictWriteContext | None:
    if identity is None and prepared is None:
        return None
    if identity is None or prepared is None:
        raise conflict_engine.ConflictPreparedWriteError(
            "scoped weight writes require identity and a prepared weight write"
        )
    if (
        not isinstance(prepared, PreparedWeightWrite)
        or prepared._seal is not _PREPARED_WEIGHT_WRITE_SEAL
        or prepared._session is not session
    ):
        raise conflict_engine.ConflictPreparedWriteError(
            "prepared weight write was not issued for this session"
        )
    return conflict_engine.require_prepared_identity(
        session,
        prepared=prepared.conflict_write,
        identity=identity,
    )


def require_prepared_weight_identity(
    session: AsyncSession,
    *,
    prepared: PreparedWeightWrite,
    identity: WriteIdentity,
) -> conflict_engine.ConflictWriteContext:
    """Validate an issued Weight capability before another service locks roots."""

    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared,
    )
    assert context is not None
    return context


def _require_evaluation_date(
    context: conflict_engine.ConflictWriteContext,
    on_date: date_type,
) -> None:
    if context.evaluation_date != on_date:
        raise conflict_engine.ConflictPreparedWriteError(
            "weight write date does not match prepared conflict evaluation date"
        )


def _require_legacy_bridge(
    context: conflict_engine.ConflictWriteContext,
    *,
    include_legacy_unowned: bool,
) -> None:
    if (
        include_legacy_unowned
        and context.legacy_bridge
        is not conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
    ):
        raise conflict_engine.ConflictPreparedWriteError(
            "legacy weight access requires a fully-unowned bridge"
        )

# Cached at first use. NOTE: height/sex changes via Settings only take effect after
# a container restart (this cache + load_config() read env once) — unlike the login
# password, which is applied live. That's acceptable: body geometry rarely changes.
_config: Optional[Config] = None


def _body_config() -> tuple[float, str]:
    """(height_cm, sex) for the Navy formula, from config (cached; see note above)."""
    global _config
    if _config is None:
        _config = load_config()
    return _config.height_cm, _config.sex


# A direct measurement — a manual entry or a body-composition scan (InBody/МедАсс)
# — outranks a passive device import (Garmin). Manual and scan tie at the top, so
# the latest of the two wins; Garmin never supersedes either (owner's rule:
# "Garmin overrides nothing").
_SOURCE_PRIORITY: dict[str, int] = {
    Source.MANUAL.value: 2,
    Source.MCP.value: 2,  # a weight he told Claude is a weight he entered
    Source.BODY_SCAN.value: 2,
}


def _source_priority(source: str) -> int:
    """Priority of a weight source for the one-active-per-date invariant."""
    return _SOURCE_PRIORITY.get(source, 1)


# Sanity bounds for the write path. These tools are reachable over MCP (an LLM),
# which bypasses the HTML form's min/max entirely, so a hallucinated 900 kg or a
# 0 has to be rejected here rather than land in the data lake — the same reasoning
# as ``glp1_service._validate_injection``, plus the upper bounds GLP-1 still lacks.
_WEIGHT_KG_RANGE = (20.0, 400.0)
_CIRCUMFERENCE_CM_RANGE = (10.0, 300.0)


def _check_range(name: str, value: Optional[float], bounds: tuple[float, float]) -> Optional[float]:
    """Reject a non-finite or out-of-range number, raising ``ValueError``. ``None``
    passes through untouched (an omitted optional field), so every field can be
    handed straight in."""
    if value is None:
        return None
    low, high = bounds
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g} (got {value!r})")
    return value


# ── Weight logs ───────────────────────────────────────────────────────────────
def _weight_scope_condition(
    *,
    subject_id: uuid.UUID,
    include_legacy_unowned: bool,
    evaluation_date: date_type,
):
    from vitals.models.identity import HealthSubject
    from vitals.models.tenancy import IntegrationConnection

    owner_user_id = (
        select(HealthSubject.owner_user_id)
        .where(HealthSubject.id == subject_id)
        .scalar_subquery()
    )
    historical_statuses = tuple(
        status.value
        for status in IntegrationConnectionStatus
        if status is not IntegrationConnectionStatus.PENDING
    )
    raw_scope = conflict_engine.ConflictScope(
        subject_id=subject_id,
        evaluation_date=evaluation_date,
        legacy_bridge=(
            conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
            if include_legacy_unowned
            else conflict_engine.LegacyConflictBridge.REJECT
        ),
    )
    exact_raw, fully_unowned_raw = conflict_engine.raw_payload_scope_conditions(
        raw_scope
    )
    exact = and_(
        WeightLog.subject_id == subject_id,
        or_(
            WeightLog.actor_user_id.is_(None),
            WeightLog.actor_user_id == owner_user_id,
        ),
        or_(
            WeightLog.integration_connection_id.is_(None),
            exists(
                select(1).where(
                    IntegrationConnection.id
                    == WeightLog.integration_connection_id,
                    IntegrationConnection.subject_id == subject_id,
                    IntegrationConnection.status.in_(historical_statuses),
                )
            ),
        ),
        or_(
            WeightLog.raw_payload_id.is_(None),
            exists(
                select(1).where(
                    RawPayload.id == WeightLog.raw_payload_id,
                    exact_raw,
                )
            ),
        ),
    )
    if not include_legacy_unowned:
        return exact
    fully_unowned = and_(
        WeightLog.subject_id.is_(None),
        WeightLog.actor_user_id.is_(None),
        WeightLog.integration_connection_id.is_(None),
        or_(
            WeightLog.raw_payload_id.is_(None),
            exists(
                select(1).where(
                    RawPayload.id == WeightLog.raw_payload_id,
                    fully_unowned_raw,
                )
            ),
        ),
    )
    return or_(exact, fully_unowned)


async def _assert_weight_scope_integrity(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    evaluation_date: date_type,
    filters: Sequence = (),
) -> None:
    """Reject partial roots instead of silently treating them as absent."""

    raw_scope = conflict_engine.ConflictScope(
        subject_id=subject_id,
        evaluation_date=evaluation_date,
        legacy_bridge=conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
    )
    exact_raw, fully_unowned_raw = conflict_engine.raw_payload_scope_conditions(
        raw_scope
    )
    invalid_raw = await session.scalar(
        select(WeightLog.id)
        .where(
            or_(
                WeightLog.subject_id == subject_id,
                WeightLog.subject_id.is_(None),
            ),
            *filters,
            WeightLog.raw_payload_id.is_not(None),
            or_(
                and_(
                    WeightLog.subject_id == subject_id,
                    exists(
                        select(1).where(
                            RawPayload.id == WeightLog.raw_payload_id,
                            exact_raw,
                        )
                    ),
                ),
                and_(
                    WeightLog.subject_id.is_(None),
                    exists(
                        select(1).where(
                            RawPayload.id == WeightLog.raw_payload_id,
                            fully_unowned_raw,
                        )
                    ),
                ),
            ).is_not(True),
        )
        .limit(1)
    )
    if invalid_raw is not None:
        raise conflict_engine.ConflictRawOwnershipError(
            "weight fact links to raw provenance outside its subject scope"
        )

    structurally_valid_scope = _weight_scope_condition(
        subject_id=subject_id,
        include_legacy_unowned=True,
        evaluation_date=evaluation_date,
    )
    invalid = await session.scalar(
        select(WeightLog.id)
        .where(
            or_(
                WeightLog.subject_id == subject_id,
                WeightLog.subject_id.is_(None),
            ),
            *filters,
            structurally_valid_scope.is_not(True),
        )
        .limit(1)
    )
    if invalid is not None:
        raise WeightOwnershipError(
            "weight fact has partial or conflicting ownership provenance"
        )


async def get_active_weight(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
    for_update: bool = False,
) -> Optional[WeightLog]:
    stmt = select(WeightLog).where(
        WeightLog.date == on_date,
        WeightLog.superseded.is_(False),
    )
    if subject_id is not None:
        scope = _weight_scope_condition(
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
            evaluation_date=on_date,
        )
        await _assert_weight_scope_integrity(
            session,
            subject_id=subject_id,
            evaluation_date=on_date,
            filters=(
                WeightLog.date == on_date,
                WeightLog.superseded.is_(False),
            ),
        )
        stmt = stmt.where(scope)
    elif include_legacy_unowned:
        raise ValueError("legacy weight compatibility requires a subject_id")
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt.execution_options(populate_existing=True))
    row = result.scalar_one_or_none()
    if row is not None and subject_id is not None:
        await _validate_persisted_weight_provenance(
            session,
            row,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )
    return row


async def _validate_new_weight_provenance(
    session: AsyncSession,
    *,
    context: conflict_engine.ConflictWriteContext,
    source: str,
    integration_connection_id: uuid.UUID | None,
    raw_payload_id: int | None,
    origin_actor_user_id: uuid.UUID | None | object,
) -> None:
    from vitals.models.tenancy import IntegrationConnection

    scope = context.scope
    exact_raw, fully_unowned_raw = conflict_engine.raw_payload_scope_conditions(scope)
    connection = None
    if integration_connection_id is not None:
        statuses = tuple(
            status.value
            for status in IntegrationConnectionStatus
            if status is not IntegrationConnectionStatus.PENDING
        )
        connection = await session.scalar(
            select(IntegrationConnection)
            .where(
                IntegrationConnection.id == integration_connection_id,
                IntegrationConnection.subject_id == context.identity.subject_id,
                IntegrationConnection.status.in_(statuses),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if connection is None:
            raise WeightOwnershipError(
                "weight origin connection is outside the prepared subject"
            )
    if source in {Source.MANUAL.value, Source.MCP.value}:
        if integration_connection_id is not None or raw_payload_id is not None:
            raise WeightOwnershipError(
                "manual and MCP weight facts cannot claim provider provenance"
            )
    elif source == Source.GARMIN_API.value:
        if (
            connection is None
            or connection.provider != IntegrationProvider.GARMIN.value
            or connection.connection_type
            != IntegrationConnectionType.ACCOUNT.value
        ):
            raise WeightOwnershipError(
                "Garmin weight facts require a Garmin account connection"
            )
        if raw_payload_id is None:
            raise conflict_engine.ConflictRawOwnershipError(
                "Garmin weight facts require durable raw provenance"
            )
    elif source == Source.BODY_SCAN.value and connection is not None:
        if (
            connection.provider != IntegrationProvider.OPENROUTER.value
            or connection.connection_type
            != IntegrationConnectionType.AI_GATEWAY.value
        ):
            raise WeightOwnershipError(
                "body-scan weight provenance requires an OpenRouter AI connection"
            )
        if raw_payload_id is None:
            raise conflict_engine.ConflictRawOwnershipError(
                "provider-backed body-scan weight requires durable raw provenance"
            )
    elif source not in {Source.BODY_SCAN.value}:
        raise WeightOwnershipError("unsupported scoped weight provenance source")

    requested_actor = (
        context.identity.actor_user_id
        if origin_actor_user_id is _ORIGIN_ACTOR_UNSET
        else origin_actor_user_id
    )
    if raw_payload_id is None:
        if requested_actor not in {None, context.identity.actor_user_id}:
            raise WeightOwnershipError(
                "weight actor is not authorized by the prepared writer"
            )
        return
    raw_allowed = exact_raw
    if context.legacy_bridge is conflict_engine.LegacyConflictBridge.FULLY_UNOWNED:
        raw_allowed = or_(raw_allowed, fully_unowned_raw)
    raw = await session.scalar(
        select(RawPayload)
        .where(
            RawPayload.id == raw_payload_id,
            raw_allowed,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if raw is None:
        raise conflict_engine.ConflictRawOwnershipError(
            "weight raw payload is outside the prepared subject"
        )
    if raw.integration_connection_id != integration_connection_id:
        raise conflict_engine.ConflictRawOwnershipError(
            "weight raw payload belongs to a different origin connection"
        )
    if raw.actor_user_id != requested_actor:
        raise conflict_engine.ConflictRawOwnershipError(
            "weight actor does not match durable raw provenance"
        )
    if raw.source != source:
        raise conflict_engine.ConflictRawOwnershipError(
            "weight source does not match durable raw provenance"
        )
    expected_raw_domain = (
        Domain.GARMIN.value
        if source == Source.GARMIN_API.value
        else Domain.BODY_COMPOSITION.value
    )
    if raw.domain != expected_raw_domain:
        raise conflict_engine.ConflictRawOwnershipError(
            "weight raw payload belongs to a different domain"
        )


async def _validate_persisted_weight_provenance(
    session: AsyncSession,
    row: WeightLog,
    *,
    subject_id: uuid.UUID,
    include_legacy_unowned: bool,
) -> None:
    """Validate the durable source -> C/raw chain for one scoped Weight fact."""

    from vitals.models.tenancy import IntegrationConnection

    is_legacy = row.subject_id is None
    if row.subject_id not in {None, subject_id}:
        raise WeightOwnershipError("weight fact belongs to another subject")
    if is_legacy:
        if not include_legacy_unowned or any(
            root is not None
            for root in (
                row.actor_user_id,
                row.integration_connection_id,
            )
        ):
            raise WeightOwnershipError(
                "weight fact has partial or conflicting ownership provenance"
            )
    if row.domain != DOMAIN:
        raise WeightOwnershipError("weight fact has an invalid domain")

    connection = None
    if row.integration_connection_id is not None:
        connection = await session.scalar(
            select(IntegrationConnection)
            .where(IntegrationConnection.id == row.integration_connection_id)
            .execution_options(populate_existing=True)
        )
        historical_statuses = {
            status.value
            for status in IntegrationConnectionStatus
            if status is not IntegrationConnectionStatus.PENDING
        }
        if (
            connection is None
            or connection.subject_id != subject_id
            or connection.status not in historical_statuses
        ):
            raise WeightOwnershipError(
                "weight fact references an invalid subject connection"
            )

    raw = None
    if row.raw_payload_id is not None:
        raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == row.raw_payload_id)
            .execution_options(populate_existing=True)
        )
        if raw is None:
            raise conflict_engine.ConflictRawOwnershipError(
                "weight fact references a missing raw payload"
            )
        if is_legacy:
            if any(
                root is not None
                for root in (
                    raw.subject_id,
                    raw.actor_user_id,
                    raw.integration_connection_id,
                    raw.file_asset_id,
                )
            ):
                raise conflict_engine.ConflictRawOwnershipError(
                    "legacy weight fact links to partially owned raw provenance"
                )
        elif raw.subject_id != subject_id:
            raise conflict_engine.ConflictRawOwnershipError(
                "weight fact links to raw provenance outside its subject scope"
            )
        if raw.actor_user_id != row.actor_user_id:
            raise conflict_engine.ConflictRawOwnershipError(
                "weight actor does not match durable raw provenance"
            )
        if raw.integration_connection_id != row.integration_connection_id:
            raise conflict_engine.ConflictRawOwnershipError(
                "weight connection does not match durable raw provenance"
            )

    if row.source in {Source.MANUAL.value, Source.MCP.value}:
        if connection is not None:
            raise WeightOwnershipError(
                "manual and MCP weight facts cannot claim provider provenance"
            )
        if raw is not None and (
            raw.domain != Domain.WEIGHT.value
            or raw.source != row.source
            or raw.file_asset_id is not None
        ):
            raise conflict_engine.ConflictRawOwnershipError(
                "manual or MCP weight raw provenance is incompatible"
            )
        return

    if row.source == Source.GARMIN_API.value:
        if is_legacy:
            if raw is not None and (
                raw.domain != Domain.GARMIN.value
                or raw.source != Source.GARMIN_API.value
                or raw.file_asset_id is not None
            ):
                raise conflict_engine.ConflictRawOwnershipError(
                    "legacy Garmin weight raw provenance is incompatible"
                )
            return
        if (
            connection is None
            or connection.provider != IntegrationProvider.GARMIN.value
            or connection.connection_type
            != IntegrationConnectionType.ACCOUNT.value
        ):
            raise WeightOwnershipError(
                "Garmin weight fact has invalid connection provenance"
            )
        if raw is None:
            raise conflict_engine.ConflictRawOwnershipError(
                "Garmin weight fact has no durable raw provenance"
            )
        if (
            raw.domain != Domain.GARMIN.value
            or raw.source != Source.GARMIN_API.value
            or raw.file_asset_id is not None
        ):
            raise conflict_engine.ConflictRawOwnershipError(
                "Garmin weight raw provenance is incompatible"
            )
        return

    if row.source == Source.BODY_SCAN.value:
        if connection is not None and (
            connection.provider != IntegrationProvider.OPENROUTER.value
            or connection.connection_type
            != IntegrationConnectionType.AI_GATEWAY.value
        ):
            raise WeightOwnershipError(
                "body-scan weight has invalid connection provenance"
            )
        if connection is not None and raw is None:
            raise conflict_engine.ConflictRawOwnershipError(
                "provider-backed body-scan weight has no durable raw provenance"
            )
        if raw is not None and (
            raw.domain != Domain.BODY_COMPOSITION.value
            or raw.source != Source.BODY_SCAN.value
        ):
            raise conflict_engine.ConflictRawOwnershipError(
                "body-scan weight raw provenance is incompatible"
            )
        return

    raise WeightOwnershipError("weight fact has unsupported provenance source")


def _weight_entity_key(row: WeightLog) -> str:
    return f"weight:{row.id}"


def _weight_provenance_is_reusable(
    row: WeightLog,
    *,
    identity: WriteIdentity | None,
    integration_connection_id: uuid.UUID | None,
    raw_payload_id: int | None,
) -> bool:
    if identity is None:
        return True
    # A provider refresh must not rewrite an old unowned fact into the current
    # request's C/raw/actor history. Preserve the legacy row and append a new
    # exact-owned fact instead; manual/MCP rows without provider roots may still
    # be adopted in place.
    if integration_connection_id is not None or raw_payload_id is not None:
        return (
            row.subject_id == identity.subject_id
            and row.integration_connection_id == integration_connection_id
            and row.raw_payload_id == raw_payload_id
        )
    return (
        row.subject_id in {None, identity.subject_id}
        and row.integration_connection_id is None
        and row.raw_payload_id is None
    )


async def _get_weight_log_for_update(
    session: AsyncSession,
    weight_id: int,
    *,
    subject_id: uuid.UUID | None,
    include_legacy_unowned: bool,
    evaluation_date: date_type,
) -> WeightLog | None:
    stmt = select(WeightLog).where(WeightLog.id == weight_id)
    if subject_id is not None:
        scope = _weight_scope_condition(
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
            evaluation_date=evaluation_date,
        )
        invalid = await session.scalar(
            select(WeightLog.id)
            .where(
                WeightLog.id == weight_id,
                or_(
                    WeightLog.subject_id == subject_id,
                    WeightLog.subject_id.is_(None),
                ),
                scope.is_not(True),
            )
            .limit(1)
        )
        if invalid is not None:
            raise WeightOwnershipError(
                "weight fact has partial or conflicting ownership provenance"
            )
        stmt = stmt.where(scope)
    elif include_legacy_unowned:
        raise ValueError("legacy weight compatibility requires a subject_id")
    row = await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )
    if row is not None and subject_id is not None:
        await _validate_persisted_weight_provenance(
            session,
            row,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )
    return row


async def _get_weight_log_date_in_scope(
    session: AsyncSession,
    weight_id: int,
    *,
    subject_id: uuid.UUID,
    include_legacy_unowned: bool,
    evaluation_date: date_type,
) -> date_type | None:
    """Read only the target date after the caller has locked its subject roots."""

    scope = _weight_scope_condition(
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
        evaluation_date=evaluation_date,
    )
    await _assert_weight_scope_integrity(
        session,
        subject_id=subject_id,
        evaluation_date=evaluation_date,
        filters=(WeightLog.id == weight_id,),
    )
    return await session.scalar(
        select(WeightLog.date).where(WeightLog.id == weight_id, scope)
    )


async def _prepared_weight_write_for_date(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    prepared: PreparedWeightWrite,
    on_date: date_type,
) -> PreparedWeightWrite:
    """Reissue an already-proven Weight capability for another fact date."""

    context = require_prepared_weight_identity(
        session,
        prepared=prepared,
        identity=identity,
    )
    if context.evaluation_date == on_date:
        return prepared
    # Governance, outbox advisory, subject, and actor roots are already held by
    # ``prepared``. Re-acquiring them in the same transaction is non-blocking and
    # binds conflict evaluation to the date that may become active.
    return await prepare_weight_write(
        session,
        context=conflict_engine.ConflictWriteContext(
            identity=identity,
            evaluation_date=on_date,
            legacy_bridge=context.legacy_bridge,
        ),
        garmin_weight_export_context=(
            prepared.garmin_weight_export.context
            if prepared.garmin_weight_export is not None
            else None
        ),
    )


async def log_weight(
    session: AsyncSession,
    *,
    on_date: date_type,
    weight_kg: float,
    source: str = Source.MANUAL.value,
    raw_payload_id: Optional[int] = None,
    note: Optional[str] = None,
    override: bool = False,
    identity: WriteIdentity | None = None,
    integration_connection_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
    prepared_weight_write: PreparedWeightWrite | None = None,
    origin_actor_user_id: uuid.UUID | None | object = _ORIGIN_ACTOR_UNSET,
) -> WeightLog:
    """Record a weight for a date, honouring manual-over-Garmin priority and the
    one-active-per-date invariant.

    May raise ``ConflictBlocked`` if a (future) cross-domain block rule fires
    without ``override``, or ``ValueError`` on an implausible weight.
    """
    _check_range("weight_kg", weight_kg, _WEIGHT_KG_RANGE)
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    if context is not None:
        _require_evaluation_date(context, on_date)
        _require_legacy_bridge(
            context,
            include_legacy_unowned=include_legacy_unowned,
        )
        await _validate_new_weight_provenance(
            session,
            context=context,
            source=source,
            integration_connection_id=integration_connection_id,
            raw_payload_id=raw_payload_id,
            origin_actor_user_id=origin_actor_user_id,
        )
    elif include_legacy_unowned:
        raise ValueError("legacy weight compatibility requires a scoped writer")
    elif integration_connection_id is not None:
        raise ValueError("weight origin connection requires a scoped writer")

    # Every active-weight writer participates in the Garmin outbox lock before
    # it changes local truth (including conflict-alert writes). The hook below
    # only reconciles local DB state and never performs network I/O.
    from vitals.services import garmin_weight_service

    # Scoped callers already acquired this advisory before subject locks through
    # ``prepare_weight_write``. Re-acquiring the same xact lock is harmless and
    # keeps the legacy branch serialized too.
    await garmin_weight_service.lock_active_weight_change(session)

    existing = await get_active_weight(
        session,
        on_date,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
        for_update=True,
    )
    if existing is None and identity is not None:
        occupied = await session.scalar(
            select(WeightLog.id).where(
                WeightLog.date == on_date,
                WeightLog.superseded.is_(False),
            )
        )
        if occupied is not None:
            raise WeightScopedUniqueCutoverRequiredError(
                "active-weight date is occupied by another ownership scope"
            )

    # A re-import of a fact we already hold is not a new reading. Garmin's daily
    # bundle carries the same weigh-in on every poll, so without this each sync
    # appended another identical row and superseded the last — a day accumulated
    # a dozen clones, and deleting the visible one just promoted its twin.
    if (
        existing is not None
        and existing.source == source
        and existing.weight_kg == weight_kg
        and _weight_provenance_is_reusable(
            existing,
            identity=identity,
            integration_connection_id=integration_connection_id,
            raw_payload_id=raw_payload_id,
        )
    ):
        await _adopt_weight_provenance(
            session,
            existing,
            identity=identity,
            integration_connection_id=integration_connection_id,
            raw_payload_id=raw_payload_id,
        )
        await session.flush()
        return existing

    # The active row can be a higher-priority manual measurement while an
    # identical Garmin import already sits underneath it.  Daily Garmin polls
    # must reuse that inactive fact too; otherwise each poll appends another
    # superseded clone even though the visible manual row never changes.
    if (
        existing is not None
        and _source_priority(source) < _source_priority(existing.source)
    ):
        duplicate_stmt = select(WeightLog).where(
            WeightLog.date == on_date,
            WeightLog.source == source,
            WeightLog.weight_kg == weight_kg,
        )
        if identity is not None:
            duplicate_scope = _weight_scope_condition(
                subject_id=identity.subject_id,
                include_legacy_unowned=include_legacy_unowned,
                evaluation_date=on_date,
            )
            await _assert_weight_scope_integrity(
                session,
                subject_id=identity.subject_id,
                evaluation_date=on_date,
                filters=(
                    WeightLog.date == on_date,
                    WeightLog.source == source,
                    WeightLog.weight_kg == weight_kg,
                ),
            )
            duplicate_stmt = duplicate_stmt.where(duplicate_scope)
        duplicate_rows = list(
            await session.scalars(
                duplicate_stmt.order_by(WeightLog.id.desc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        duplicate = next(
            (
                candidate
                for candidate in duplicate_rows
                if _weight_provenance_is_reusable(
                    candidate,
                    identity=identity,
                    integration_connection_id=integration_connection_id,
                    raw_payload_id=raw_payload_id,
                )
            ),
            None,
        )
        if duplicate is not None:
            await _adopt_weight_provenance(
                session,
                duplicate,
                identity=identity,
                integration_connection_id=integration_connection_id,
                raw_payload_id=raw_payload_id,
            )
            await session.flush()
            return duplicate

    insert_as_active = existing is None or (
        _source_priority(source) >= _source_priority(existing.source)
    )
    if insert_as_active:
        proposed = {"weight_kg": weight_kg, "source": source}
        if context is None:
            await conflict_engine.enforce(
                session,
                Domain.WEIGHT.value,
                proposed,
                override=override,
                entity_ref=f"weight:{on_date.isoformat()}",
            )
        else:
            assert prepared_weight_write is not None
            await conflict_engine.enforce_prepared(
                session,
                prepared=prepared_weight_write.conflict_write,
                domain=Domain.WEIGHT,
                proposed_state=proposed,
                override=override,
                entity_ref=f"weight:{on_date.isoformat()}",
                replace_entity_key=(
                    _weight_entity_key(existing) if existing is not None else None
                ),
            )

    if existing is not None:
        if insert_as_active:
            # New row outranks (or ties — same source, same date) the active one →
            # supersede it first to keep the partial-unique invariant. The old row
            # is kept (flagged superseded), never overwritten: a re-entry or a
            # correction must not silently destroy the previous reading
            # (data-lake principle — never delete).
            existing.superseded = True
            await session.flush()
        else:
            # Lower priority (e.g. Garmin arriving while a manual entry stands) →
            # keep the data but not active.
            insert_as_active = False

    row = WeightLog(
        subject_id=identity.subject_id if identity is not None else None,
        actor_user_id=(
            identity.actor_user_id
            if origin_actor_user_id is _ORIGIN_ACTOR_UNSET and identity is not None
            else (
                None
                if origin_actor_user_id is _ORIGIN_ACTOR_UNSET
                else origin_actor_user_id
            )
        ),
        integration_connection_id=integration_connection_id,
        date=on_date,
        domain=DOMAIN,
        source=source,
        weight_kg=weight_kg,
        raw_payload_id=raw_payload_id,
        note=note,
        superseded=not insert_as_active,
    )
    session.add(row)
    await session.flush()

    active_weight = weight_kg if insert_as_active else (
        existing.weight_kg if existing else None
    )
    if active_weight is not None:
        await _recompute_lbm_for_date(
            session,
            on_date,
            active_weight,
            subject_id=identity.subject_id if identity is not None else None,
            include_legacy_unowned=include_legacy_unowned,
        )
    if insert_as_active:
        if context is None:
            await garmin_weight_service.handle_legacy_active_weight_changed(session)
        elif prepared_weight_write.garmin_weight_export is not None:
            await garmin_weight_service.handle_active_weight_changed_scoped(
                session,
                prepared=prepared_weight_write.garmin_weight_export,
            )
    return row


async def _adopt_weight_provenance(
    session: AsyncSession,
    row: WeightLog,
    *,
    identity: WriteIdentity | None,
    integration_connection_id: uuid.UUID | None,
    raw_payload_id: int | None,
) -> None:
    """Attach only missing trusted roots without rewriting actor history."""

    if identity is None:
        return
    await _validate_persisted_weight_provenance(
        session,
        row,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
    )
    if row.subject_id not in {None, identity.subject_id}:
        raise WeightOwnershipError("weight fact belongs to another subject")
    if row.subject_id is None and (
        row.actor_user_id is not None
        or row.integration_connection_id is not None
    ):
        raise WeightOwnershipError("partial legacy weight roots cannot be adopted")
    if row.integration_connection_id not in {None, integration_connection_id}:
        raise WeightOwnershipError("weight fact belongs to another origin connection")
    if row.raw_payload_id not in {None, raw_payload_id}:
        raise conflict_engine.ConflictRawOwnershipError(
            "weight fact references a different raw payload"
        )
    if row.subject_id is None and row.source == Source.GARMIN_API.value:
        raise WeightOwnershipError(
            "legacy Garmin weight requires provider backfill before adoption"
        )
    if row.subject_id is None and row.raw_payload_id is not None:
        raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == row.raw_payload_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if raw is None:
            raise conflict_engine.ConflictRawOwnershipError(
                "legacy weight fact references a missing raw payload"
            )
        if raw.subject_id is None:
            if any(
                root is not None
                for root in (
                    raw.actor_user_id,
                    raw.integration_connection_id,
                    raw.file_asset_id,
                )
            ):
                raise conflict_engine.ConflictRawOwnershipError(
                    "partial legacy weight raw roots cannot be adopted"
                )
            raw.subject_id = identity.subject_id
        elif raw.subject_id != identity.subject_id:
            raise conflict_engine.ConflictRawOwnershipError(
                "weight raw payload belongs to another subject"
            )
    if row.subject_id is None:
        row.subject_id = identity.subject_id
    if row.integration_connection_id is None:
        row.integration_connection_id = integration_connection_id
    if row.raw_payload_id is None:
        row.raw_payload_id = raw_payload_id
    await _validate_persisted_weight_provenance(
        session,
        row,
        subject_id=identity.subject_id,
        include_legacy_unowned=False,
    )


async def list_active_weights(
    session: AsyncSession,
    *,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> Sequence[WeightLog]:
    stmt = select(WeightLog).where(WeightLog.superseded.is_(False))
    date_filters = []
    if start is not None:
        date_filters.append(WeightLog.date >= start)
    if end is not None:
        date_filters.append(WeightLog.date <= end)
    if subject_id is not None:
        scope = _weight_scope_condition(
            subject_id=subject_id,
            evaluation_date=end or start or today_local(),
            include_legacy_unowned=include_legacy_unowned,
        )
        await _assert_weight_scope_integrity(
            session,
            subject_id=subject_id,
            evaluation_date=end or start or today_local(),
            filters=(WeightLog.superseded.is_(False), *date_filters),
        )
        stmt = stmt.where(scope)
    elif include_legacy_unowned:
        raise ValueError("legacy weight compatibility requires a subject_id")
    if date_filters:
        stmt = stmt.where(*date_filters)
    stmt = stmt.order_by(WeightLog.date)
    result = await session.execute(stmt)
    rows = result.scalars().all()
    if subject_id is not None:
        for row in rows:
            await _validate_persisted_weight_provenance(
                session,
                row,
                subject_id=subject_id,
                include_legacy_unowned=include_legacy_unowned,
            )
    return rows


async def list_weight_notes(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    include_legacy_unowned: bool = False,
    start: date_type | None = None,
    end: date_type | None = None,
    limit: int = 50,
) -> Sequence[WeightLog]:
    """Return scoped Weight rows carrying notes, including superseded history."""

    filters = [WeightLog.note.is_not(None), WeightLog.note != ""]
    if start is not None:
        filters.append(WeightLog.date >= start)
    if end is not None:
        filters.append(WeightLog.date <= end)
    scope = _weight_scope_condition(
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
        evaluation_date=end or start or today_local(),
    )
    await _assert_weight_scope_integrity(
        session,
        subject_id=subject_id,
        evaluation_date=end or start or today_local(),
        filters=tuple(filters),
    )
    rows = tuple(
        await session.scalars(
            select(WeightLog)
            .where(*filters, scope)
            .order_by(WeightLog.date.desc(), WeightLog.id.desc())
            .limit(limit)
        )
    )
    for row in rows:
        await _validate_persisted_weight_provenance(
            session,
            row,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )
    return rows


async def resolve_active(session: AsyncSession) -> list[dict]:
    """Legacy conflict snapshot of active Weight facts."""

    return [
        {"weight_kg": row.weight_kg, "source": row.source}
        for row in await list_active_weights(session)
    ]


async def resolve_active_scoped(
    session: AsyncSession,
    *,
    scope: conflict_engine.ConflictScope,
) -> list[dict]:
    """Return the selected subject's active weight on the evaluation date."""

    row = await get_active_weight(
        session,
        scope.evaluation_date,
        subject_id=scope.subject_id,
        include_legacy_unowned=scope.include_legacy_unowned,
    )
    if row is None:
        return []
    return [
        {
            conflict_engine.CONFLICT_ENTITY_KEY: _weight_entity_key(row),
            "weight_kg": row.weight_kg,
            "source": row.source,
        }
    ]


# ── Body measurements ─────────────────────────────────────────────────────────
def _body_measurement_scope_condition(
    *,
    subject_id: uuid.UUID,
    include_legacy_unowned: bool,
):
    from vitals.models.identity import HealthSubject

    owner_user_id = (
        select(HealthSubject.owner_user_id)
        .where(HealthSubject.id == subject_id)
        .scalar_subquery()
    )
    exact = and_(
        BodyMeasurement.subject_id == subject_id,
        or_(
            BodyMeasurement.actor_user_id.is_(None),
            BodyMeasurement.actor_user_id == owner_user_id,
        ),
    )
    if not include_legacy_unowned:
        return exact
    return or_(
        exact,
        and_(
            BodyMeasurement.subject_id.is_(None),
            BodyMeasurement.actor_user_id.is_(None),
        ),
    )


async def upsert_body_measurement(
    session: AsyncSession,
    *,
    on_date: date_type,
    neck_cm: Optional[float] = None,
    waist_cm: Optional[float] = None,
    hips_cm: Optional[float] = None,
    note: Optional[str] = None,
    override: bool = False,
    partial: bool = True,
) -> BodyMeasurement:
    """Create/update the day's measurement and (re)derive body-fat % + LBM.

    Partial merge (``partial=True``, the default): a field left ``None`` keeps
    whatever's already on file for the date instead of being blanked (e.g. MCP
    ``log_measurement`` is often called with just one of the three
    circumferences).

    ``partial=False`` means the caller is handing over the row's whole truth and
    ``None`` blanks the field. That is the HTML edit form: it always submits
    every field it renders, and FastAPI turns an emptied input into ``None`` —
    so under the merge the owner could never delete a value he had entered by
    mistake, it would silently come back."""
    _check_range("neck_cm", neck_cm, _CIRCUMFERENCE_CM_RANGE)
    _check_range("waist_cm", waist_cm, _CIRCUMFERENCE_CM_RANGE)
    _check_range("hips_cm", hips_cm, _CIRCUMFERENCE_CM_RANGE)
    await conflict_engine.enforce(
        session,
        Domain.WEIGHT.value,
        {"measurement": True},
        override=override,
        entity_ref=f"body_measurement:{on_date.isoformat()}",
    )

    result = await session.execute(
        select(BodyMeasurement).where(BodyMeasurement.date == on_date)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = BodyMeasurement(date=on_date, domain=DOMAIN, source=Source.MANUAL.value)
        session.add(row)

    if partial:
        effective_neck = neck_cm if neck_cm is not None else row.neck_cm
        effective_waist = waist_cm if waist_cm is not None else row.waist_cm
        effective_hips = hips_cm if hips_cm is not None else row.hips_cm
    else:
        effective_neck, effective_waist, effective_hips = neck_cm, waist_cm, hips_cm

    height_cm, sex = _body_config()
    body_fat_pct = None
    if effective_neck and effective_waist:
        try:
            body_fat_pct = navy_body_fat_pct(
                waist_cm=effective_waist,
                neck_cm=effective_neck,
                height_cm=height_cm,
                sex=sex,
                hips_cm=effective_hips,
            )
        except ValueError:
            body_fat_pct = None

    lbm_kg = None
    if body_fat_pct is not None:
        active = await get_active_weight(session, on_date)
        if active is not None:
            lbm_kg = lean_body_mass_kg(active.weight_kg, body_fat_pct)

    row.neck_cm = effective_neck
    row.waist_cm = effective_waist
    row.hips_cm = effective_hips
    row.body_fat_pct = body_fat_pct
    row.lbm_kg = lbm_kg
    if note is not None or not partial:
        row.note = note
    await session.flush()
    return row


async def _recompute_lbm_for_date(
    session: AsyncSession,
    on_date: date_type,
    weight_kg: float,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> None:
    """Refresh a measurement's LBM after the day's active weight changes."""
    stmt = select(BodyMeasurement).where(BodyMeasurement.date == on_date)
    if subject_id is not None:
        scope = _body_measurement_scope_condition(
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )
        invalid = await session.scalar(
            select(BodyMeasurement.id)
            .where(
                BodyMeasurement.date == on_date,
                or_(
                    BodyMeasurement.subject_id == subject_id,
                    BodyMeasurement.subject_id.is_(None),
                ),
                scope.is_not(True),
            )
            .limit(1)
        )
        if invalid is not None:
            raise WeightOwnershipError(
                "body measurement has partial ownership provenance"
            )
        stmt = stmt.where(scope)
    elif include_legacy_unowned:
        raise ValueError("legacy body-measurement compatibility requires a subject_id")
    result = await session.execute(
        stmt.with_for_update().execution_options(populate_existing=True)
    )
    row = result.scalar_one_or_none()
    if row is not None and row.body_fat_pct is not None:
        row.lbm_kg = lean_body_mass_kg(weight_kg, row.body_fat_pct)
        await session.flush()


async def list_body_measurements(
    session: AsyncSession,
) -> Sequence[BodyMeasurement]:
    result = await session.execute(
        select(BodyMeasurement).order_by(BodyMeasurement.date)
    )
    return result.scalars().all()


# ── Noise markers ─────────────────────────────────────────────────────────────
async def add_noise_marker(
    session: AsyncSession,
    *,
    start_date: date_type,
    end_date: Optional[date_type] = None,
    reason: str,
    direction: Optional[str] = None,
) -> NoiseMarker:
    marker = NoiseMarker(
        domain=DOMAIN,
        source=Source.MANUAL.value,
        start_date=start_date,
        end_date=end_date,
        reason=reason,
        direction=direction,
    )
    session.add(marker)
    await session.flush()
    return marker


async def list_noise_markers(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
    start: date_type | None = None,
    end: date_type | None = None,
) -> Sequence[NoiseMarker]:
    stmt = select(NoiseMarker).where(NoiseMarker.domain == DOMAIN)
    if subject_id is not None:
        from vitals.models.identity import HealthSubject

        owner_user_id = (
            select(HealthSubject.owner_user_id)
            .where(HealthSubject.id == subject_id)
            .scalar_subquery()
        )
        subject_scope = and_(
            NoiseMarker.subject_id == subject_id,
            or_(
                NoiseMarker.actor_user_id.is_(None),
                NoiseMarker.actor_user_id == owner_user_id,
            ),
        )
        if include_legacy_unowned:
            subject_scope = or_(
                subject_scope,
                and_(
                    NoiseMarker.subject_id.is_(None),
                    NoiseMarker.actor_user_id.is_(None),
                ),
            )
        stmt = stmt.where(subject_scope)
    elif include_legacy_unowned:
        raise ValueError("legacy weight compatibility requires a subject_id")
    if start is not None:
        stmt = stmt.where(
            or_(NoiseMarker.end_date.is_(None), NoiseMarker.end_date >= start)
        )
    if end is not None:
        stmt = stmt.where(NoiseMarker.start_date <= end)
    result = await session.execute(stmt.order_by(NoiseMarker.start_date))
    return result.scalars().all()


async def _noise_ranges(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
    start: date_type | None = None,
    end: date_type | None = None,
) -> list[tuple[date_type, Optional[date_type]]]:
    markers = await list_noise_markers(
        session,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
        start=start,
        end=end,
    )
    return [(m.start_date, m.end_date) for m in markers]


# ── Progress photos ───────────────────────────────────────────────────────────
async def add_progress_photo(
    session: AsyncSession,
    *,
    on_date: date_type,
    file_key: str,
    note: Optional[str] = None,
    identity: WriteIdentity | None = None,
    file_asset_id: uuid.UUID | None = None,
) -> ProgressPhoto:
    if identity is not None:
        if not isinstance(file_asset_id, uuid.UUID):
            raise ValueError("owned progress photo requires a file_asset_id")
        asset = await session.scalar(
            select(FileAsset)
            .where(
                FileAsset.id == file_asset_id,
                FileAsset.subject_id == identity.subject_id,
                FileAsset.purpose == FileAssetPurpose.PROGRESS_PHOTO.value,
                FileAsset.storage_ref == file_key,
            )
            .with_for_update()
        )
        if asset is None or asset.status in {
            FileAssetStatus.DELETED.value,
            FileAssetStatus.PURGED.value,
        }:
            raise ValueError("progress photo file asset is not available in subject scope")
    elif file_asset_id is not None:
        raise ValueError("file_asset_id requires an explicit write identity")

    photo = ProgressPhoto(
        subject_id=identity.subject_id if identity is not None else None,
        actor_user_id=identity.actor_user_id if identity is not None else None,
        file_asset_id=file_asset_id,
        date=on_date,
        domain=DOMAIN,
        source=Source.MANUAL.value,
        file_key=file_key,
        note=note,
    )
    session.add(photo)
    await session.flush()
    return photo


async def list_progress_photos(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> Sequence[ProgressPhoto]:
    stmt = select(ProgressPhoto)
    if subject_id is not None:
        subject_scope = ProgressPhoto.subject_id == subject_id
        if include_legacy_unowned:
            subject_scope = or_(subject_scope, ProgressPhoto.subject_id.is_(None))
        stmt = stmt.where(subject_scope)
    result = await session.execute(stmt.order_by(ProgressPhoto.date.desc()))
    return result.scalars().all()


async def get_progress_photo(
    session: AsyncSession,
    photo_id: int,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> ProgressPhoto | None:
    stmt = select(ProgressPhoto).where(ProgressPhoto.id == photo_id)
    if subject_id is not None:
        subject_scope = ProgressPhoto.subject_id == subject_id
        if include_legacy_unowned:
            subject_scope = or_(subject_scope, ProgressPhoto.subject_id.is_(None))
        stmt = stmt.where(subject_scope)
    return await session.scalar(stmt)


# ── Alerts ────────────────────────────────────────────────────────────────────
async def refresh_noise_alert(
    session: AsyncSession, *, on_date: Optional[date_type] = None
) -> Optional[object]:
    """Raise an ``info`` alert while today sits inside a noise range; resolve it
    once it doesn't. Idempotent (safe to call on every dashboard load / tick)."""
    today = on_date or today_local()
    active_reason = None
    for marker in await list_noise_markers(session):
        end = marker.end_date
        if (end is None and today >= marker.start_date) or (
            end is not None and marker.start_date <= today <= end
        ):
            active_reason = marker.reason
            break

    if active_reason is not None:
        # Don't re-raise if the user already dismissed this alert today — it will
        # reappear automatically the next calendar day.
        if await alerts_service._was_dismissed_today(session, NOISE_ALERT_KEY, ""):
            return None
        return await alerts_service.raise_alert(
            session,
            domain=Domain.WEIGHT.value,
            severity=Severity.INFO.value,
            message=t("alert.weight_noisy", reason=active_reason),
            alert_key=NOISE_ALERT_KEY,
        )
    return await alerts_service.resolve_by_key(session, alert_key=NOISE_ALERT_KEY)


# ── Chart series ──────────────────────────────────────────────────────────────
async def chart_series(
    session: AsyncSession,
    *,
    goal_kg: Optional[float] = None,
    include_bia: bool = False,
    include_timeline: bool = False,
    include_glp1: bool = True,
    end: Optional[date_type] = None,
) -> dict:
    """Assemble everything the weight dashboard chart needs.

    Returns JSON-serialisable structures:
      * ``raw``        — [{date, weight_kg}] active points (secondary scatter)
      * ``trend_ma``   — [{date, weight_kg}] 7-day MA over noise-excluded points
      * ``lbm``        — [{date, lbm_kg}] from Navy measurements
      * ``noise``      — [{start, end}] ranges (for the chart annotation overlay)
      * ``projection`` — {target_kg, date} or None
      * ``trend``      — {slope_per_week} or None — the least-squares slope over
                         the WHOLE history, i.e. an average weekly rate, not the
                         last week's movement (see ``weekly_delta`` for that)
      * ``weekly_delta`` — kg moved over the last 7 days: the 7-day MA now minus
                         the 7-day MA a week ago, or None with under a week of data
      * ``bia``        — {bf:[{date,value}], lbm:[{date,value}]} from BIA scans,
                         only when ``include_bia`` (the body_comp module is on).
                         Coexists with the Navy ``lbm`` series — both are shown.
      * ``annotations`` — [{start, end?, label, tone, kind}] manual Timeline
                         flags for this domain (+ global ones), only when
                         ``include_timeline`` (the timeline module is on).
    """
    weights = await list_active_weights(session, end=end)
    raw_points = [(w.date, w.weight_kg) for w in weights]

    # Noise ranges fully drop out of the MA / regression / projection (a core
    # invariant): the trend must reflect real trajectory, not water-weight spikes.
    # The raw scatter keeps every point (shown under the noise overlay).
    ranges = [
        (start, range_end)
        for start, range_end in await _noise_ranges(session)
        if end is None or start <= end
    ]
    clean_points = exclude_ranges(raw_points, ranges)
    ma = rolling_mean_by_date(clean_points, window_days=7)

    measurements = [
        row
        for row in await list_body_measurements(session)
        if end is None or row.date <= end
    ]
    lbm_points = [
        {"date": m.date.isoformat(), "lbm_kg": m.lbm_kg}
        for m in measurements
        if m.lbm_kg is not None
    ]

    # Actual movement over the last 7 days, measured on the noise-excluded MA (so
    # a single water-weight day can't fake a kilo). Deliberately NOT the regression
    # slope: that's the average weekly rate across the entire history, which on a
    # long log reads as a wildly overstated "last week" number.
    weekly_delta = None
    if ma:
        last_date, last_ma = ma[-1]
        cutoff = last_date - timedelta(days=7)
        prior = [v for (d, v) in ma if d <= cutoff]
        if prior:
            weekly_delta = round(last_ma - prior[-1], 2)

    trend = fit_trend(raw_points, exclude=ranges)
    projection = None
    if goal_kg is not None:
        proj_date = project_date_for_value(raw_points, goal_kg, exclude=ranges)
        if proj_date is not None:
            projection = {"target_kg": goal_kg, "date": proj_date.isoformat()}

    phases = await _glp1_phase_overlays(session) if include_glp1 else []

    # BIA overlay (InBody/МедАсс) — a second source for body-fat % / LBM shown
    # alongside the Navy series. Lazily imported so the weight module never hard-
    # depends on body_comp; only assembled when the module is enabled.
    bia = None
    if include_bia:
        from vitals.services import body_scan_service

        bia = await body_scan_service.bia_chart_points(session)

    # Timeline flags (manual annotations) — lazy import, only when the
    # optional timeline module is on (a disabled module behaves as absent).
    annotations = None
    if include_timeline:
        from vitals.services import timeline_service

        annotations = await timeline_service.overlays_for(session, domain=DOMAIN)

    return {
        "raw": [{"date": d.isoformat(), "weight_kg": v} for (d, v) in raw_points],
        "trend_ma": [{"date": d.isoformat(), "weight_kg": v} for (d, v) in ma],
        "lbm": lbm_points,
        "noise": [
            {"start": s.isoformat(), "end": (e.isoformat() if e else None)}
            for (s, e) in ranges
        ],
        "phases": phases,
        "projection": projection,
        "trend": (
            {"slope_per_week": round(trend.slope_per_week, 3)} if trend else None
        ),
        "weekly_delta": weekly_delta,
        "bia": bia,
        "annotations": annotations,
    }


async def _glp1_phase_overlays(session: AsyncSession) -> list[dict]:
    """GLP-1 dose phases for the chart overlay. Imported lazily so the weight
    module never depends on glp1 at import time (the cross-module link only
    exists for this one read, populated once Phase 2 lands)."""
    from vitals.models.glp1 import DOMAIN as GLP1_DOMAIN, DosePhase

    result = await session.execute(
        select(DosePhase)
        .where(DosePhase.domain == GLP1_DOMAIN)
        .order_by(DosePhase.start_date)
    )
    phases = result.scalars().all()
    return [
        {
            "start": p.start_date.isoformat(),
            "end": p.end_date.isoformat() if p.end_date else None,
            "drug": p.drug,
            "dose_mg": p.dose_mg,
            "label": f"{p.drug} {p.dose_mg:g} {t('common.mg')}",
        }
        for p in phases
    ]


# ── Deletion and Editing Helpers ──────────────────────────────────────────────
async def delete_weight_log(
    session: AsyncSession,
    log_id: int,
    *,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_weight_write: PreparedWeightWrite | None = None,
) -> bool:
    """Delete a weight log by ID. If it was active, reactivate the next highest
    priority safe log for that date and recompute LBM.

    A hard conflict on the historical replacement does not block deletion of the
    selected fact; it leaves the date without an active weight instead of making
    an unsafe superseded fact visible.
    """
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    if context is not None:
        _require_legacy_bridge(
            context,
            include_legacy_unowned=include_legacy_unowned,
        )
    elif include_legacy_unowned:
        raise ValueError("legacy weight compatibility requires a scoped writer")

    effective_prepared = prepared_weight_write
    if context is not None:
        assert identity is not None and prepared_weight_write is not None
        target_date_hint = await _get_weight_log_date_in_scope(
            session,
            log_id,
            subject_id=identity.subject_id,
            include_legacy_unowned=include_legacy_unowned,
            evaluation_date=context.evaluation_date,
        )
        if target_date_hint is None:
            return False
        effective_prepared = await _prepared_weight_write_for_date(
            session,
            identity=identity,
            prepared=prepared_weight_write,
            on_date=target_date_hint,
        )
        context = require_prepared_weight_identity(
            session,
            prepared=effective_prepared,
            identity=identity,
        )

    # Classify active/superseded only after joining the shared outbox lock. A
    # concurrent deletion can reactivate a row that this reusable session loaded
    # earlier; populate_existing prevents the identity map from preserving that
    # stale classification. The lock also precedes any FK ``SET NULL`` flush,
    # keeping the global advisory→weight→outbox order consistent.
    from vitals.services import garmin_weight_service

    await garmin_weight_service.lock_active_weight_change(session)
    row = await _get_weight_log_for_update(
        session,
        log_id,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
        evaluation_date=(
            context.evaluation_date if context is not None else today_local()
        ),
    )
    if not row:
        return False
    was_active = not row.superseded
    target_date = row.date
    if context is not None:
        _require_evaluation_date(context, target_date)
    deleted_id = row.id
    deleted_weight_kg = row.weight_kg

    next_row = None
    if was_active:
        remaining_stmt = select(WeightLog).where(
            WeightLog.date == target_date,
            WeightLog.id != row.id,
        )
        if identity is not None:
            remaining_scope = _weight_scope_condition(
                subject_id=identity.subject_id,
                include_legacy_unowned=include_legacy_unowned,
                evaluation_date=target_date,
            )
            await _assert_weight_scope_integrity(
                session,
                subject_id=identity.subject_id,
                evaluation_date=target_date,
                filters=(
                    WeightLog.date == target_date,
                    WeightLog.id != row.id,
                ),
            )
            remaining_stmt = remaining_stmt.where(remaining_scope)
        remaining = await session.execute(
            remaining_stmt.order_by(WeightLog.id.desc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        rows = remaining.scalars().all()
        if identity is not None:
            for candidate in rows:
                await _validate_persisted_weight_provenance(
                    session,
                    candidate,
                    subject_id=identity.subject_id,
                    include_legacy_unowned=include_legacy_unowned,
                )
        # Reactivate the highest-priority source (manual/scan beat Garmin), and
        # among ties the newest row (id desc, already the scan order).
        next_row = max(
            rows, key=lambda r: (_source_priority(r.source), r.id), default=None
        )
        if next_row is not None and context is not None:
            assert effective_prepared is not None
            # A legacy provider fact cannot be adopted without an authoritative
            # historical C. Keep it as history rather than guessing provenance.
            if (
                next_row.subject_id is None
                and next_row.source == Source.GARMIN_API.value
            ):
                next_row = None
            else:
                try:
                    await conflict_engine.enforce_prepared(
                        session,
                        prepared=effective_prepared.conflict_write,
                        domain=Domain.WEIGHT,
                        proposed_state={
                            "weight_kg": next_row.weight_kg,
                            "source": next_row.source,
                        },
                        override=False,
                        entity_ref=f"weight:{target_date.isoformat()}",
                        replace_entity_key=_weight_entity_key(row),
                    )
                except conflict_engine.ConflictBlocked:
                    # Deletion itself removes data from the active state. If the
                    # historical fallback is unsafe, leave it superseded rather
                    # than requiring an override merely to remove the current row.
                    next_row = None
        if (
            next_row is not None
            and identity is not None
            and next_row.subject_id is None
        ):
            await _adopt_weight_provenance(
                session,
                next_row,
                identity=identity,
                integration_connection_id=next_row.integration_connection_id,
                raw_payload_id=next_row.raw_payload_id,
            )

    # The replacement's scope, provenance, and conflict state are validated
    # before the selected active fact is deleted.
    await session.delete(row)
    await session.flush()

    if was_active:
        if next_row is not None:
            next_row.superseded = False
            await session.flush()
            await _recompute_lbm_for_date(
                session,
                target_date,
                next_row.weight_kg,
                subject_id=identity.subject_id if identity is not None else None,
                include_legacy_unowned=include_legacy_unowned,
            )
        else:
            await _recompute_lbm_for_date_null(
                session,
                target_date,
                subject_id=identity.subject_id if identity is not None else None,
                include_legacy_unowned=include_legacy_unowned,
            )

        # Keep Garmin cleanup inside the same local transaction, but never make a
        # network call here. The export job will delete only a remote sample that
        # Vitals owns, and its monotonic cursor prevents an older date surfacing as
        # an accidental backfill after this row disappears.
        if context is None:
            await garmin_weight_service.handle_legacy_active_weight_deleted(
                session,
                deleted_id=deleted_id,
                on_date=target_date,
                deleted_weight_kg=deleted_weight_kg,
                replacement=next_row,
            )
        elif effective_prepared.garmin_weight_export is not None:
            await garmin_weight_service.handle_active_weight_deleted_scoped(
                session,
                prepared=effective_prepared.garmin_weight_export,
                deleted_id=deleted_id,
                on_date=target_date,
                deleted_weight_kg=deleted_weight_kg,
                replacement=next_row,
            )
    return True


async def update_weight_note(
    session: AsyncSession,
    log_id: int,
    *,
    note: str,
    identity: WriteIdentity,
    include_legacy_unowned: bool = False,
    prepared_weight_write: PreparedWeightWrite,
) -> WeightLog | None:
    """Update only a scoped weight note without changing fact provenance."""

    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    assert context is not None
    _require_legacy_bridge(
        context,
        include_legacy_unowned=include_legacy_unowned,
    )
    row = await _get_weight_log_for_update(
        session,
        log_id,
        subject_id=identity.subject_id,
        include_legacy_unowned=include_legacy_unowned,
        evaluation_date=context.evaluation_date,
    )
    if row is None:
        return None
    if row.subject_id is None:
        await _adopt_weight_provenance(
            session,
            row,
            identity=identity,
            integration_connection_id=row.integration_connection_id,
            raw_payload_id=row.raw_payload_id,
        )
    row.note = note
    await session.flush()
    return row


async def _recompute_lbm_for_date_null(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> None:
    """Clear LBM for a date because no active weight log remains."""
    stmt = select(BodyMeasurement).where(BodyMeasurement.date == on_date)
    if subject_id is not None:
        scope = _body_measurement_scope_condition(
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )
        invalid = await session.scalar(
            select(BodyMeasurement.id)
            .where(
                BodyMeasurement.date == on_date,
                or_(
                    BodyMeasurement.subject_id == subject_id,
                    BodyMeasurement.subject_id.is_(None),
                ),
                scope.is_not(True),
            )
            .limit(1)
        )
        if invalid is not None:
            raise WeightOwnershipError(
                "body measurement has partial ownership provenance"
            )
        stmt = stmt.where(scope)
    elif include_legacy_unowned:
        raise ValueError("legacy body-measurement compatibility requires a subject_id")
    result = await session.execute(
        stmt.with_for_update().execution_options(populate_existing=True)
    )
    row = result.scalar_one_or_none()
    if row is not None:
        row.lbm_kg = None
        await session.flush()


async def delete_body_measurement(session: AsyncSession, measurement_id: int) -> bool:
    """Delete a body measurement record by ID."""
    result = await session.execute(
        select(BodyMeasurement).where(BodyMeasurement.id == measurement_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def delete_progress_photo(
    session: AsyncSession,
    photo_id: int,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> Optional[str]:
    """Delete a progress photo record by ID. Returns the file_key of the deleted photo."""
    row = await get_progress_photo(
        session,
        photo_id,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    if not row:
        return None
    file_key = row.file_key
    await session.delete(row)
    await session.flush()
    return file_key


async def delete_noise_marker(session: AsyncSession, marker_id: int) -> bool:
    """Delete a noise marker record by ID."""
    result = await session.execute(
        select(NoiseMarker).where(NoiseMarker.id == marker_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def update_weight_log(
    session: AsyncSession,
    log_id: int,
    *,
    on_date: date_type,
    weight_kg: float,
    note: Optional[str] = None,
    override: bool = False,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_weight_write: PreparedWeightWrite | None = None,
) -> Optional[WeightLog]:
    """Edit an existing weight log. If the date has changed, delete the old row
    (triggering reactivation of other rows) and insert a new log."""
    context = _require_scoped_prepared_write(
        session,
        identity=identity,
        prepared=prepared_weight_write,
    )
    if context is not None:
        _require_evaluation_date(context, on_date)
        _require_legacy_bridge(
            context,
            include_legacy_unowned=include_legacy_unowned,
        )
    elif include_legacy_unowned:
        raise ValueError("legacy weight compatibility requires a scoped writer")

    _check_range("weight_kg", weight_kg, _WEIGHT_KG_RANGE)
    from vitals.services import garmin_weight_service

    await garmin_weight_service.lock_active_weight_change(session)
    row = await _get_weight_log_for_update(
        session,
        log_id,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
        evaluation_date=on_date,
    )
    if not row:
        return None

    if row.date != on_date:
        # Insert/evaluate the destination before deleting the source. A hard
        # conflict is therefore write-free, while the caller's transaction keeps
        # the subsequent promotion/deletion atomic without a savepoint-bound
        # prepared capability.
        moved = await log_weight(
            session,
            on_date=on_date,
            weight_kg=weight_kg,
            source=row.source,
            raw_payload_id=row.raw_payload_id,
            note=note,
            override=override,
            identity=identity,
            integration_connection_id=row.integration_connection_id,
            include_legacy_unowned=include_legacy_unowned,
            prepared_weight_write=prepared_weight_write,
            origin_actor_user_id=row.actor_user_id,
        )
        deleted = await delete_weight_log(
            session,
            log_id,
            identity=identity,
            include_legacy_unowned=include_legacy_unowned,
            prepared_weight_write=prepared_weight_write,
        )
        if not deleted:  # pragma: no cover - target is locked above
            raise WeightOwnershipError("weight fact disappeared during date move")
        return moved
    if not row.superseded:
        proposed = {"weight_kg": weight_kg, "source": row.source}
        if context is None:
            await conflict_engine.enforce(
                session,
                Domain.WEIGHT.value,
                proposed,
                override=override,
                entity_ref=f"weight:{on_date.isoformat()}",
            )
        else:
            assert prepared_weight_write is not None
            await conflict_engine.enforce_prepared(
                session,
                prepared=prepared_weight_write.conflict_write,
                domain=Domain.WEIGHT,
                proposed_state=proposed,
                override=override,
                entity_ref=f"weight:{on_date.isoformat()}",
                replace_entity_key=_weight_entity_key(row),
            )
    if row.subject_id is None and identity is not None:
        await _adopt_weight_provenance(
            session,
            row,
            identity=identity,
            integration_connection_id=row.integration_connection_id,
            raw_payload_id=row.raw_payload_id,
        )
    row.weight_kg = weight_kg
    row.note = note
    await session.flush()
    # Editing a retained, superseded fact must not change body composition:
    # LBM is derived from the one active weight for the date.
    if not row.superseded:
        await _recompute_lbm_for_date(
            session,
            on_date,
            weight_kg,
            subject_id=identity.subject_id if identity is not None else None,
            include_legacy_unowned=include_legacy_unowned,
        )
        if context is None:
            await garmin_weight_service.handle_legacy_active_weight_changed(session)
        elif prepared_weight_write.garmin_weight_export is not None:
            await garmin_weight_service.handle_active_weight_changed_scoped(
                session,
                prepared=prepared_weight_write.garmin_weight_export,
            )
    return row


async def update_body_measurement(
    session: AsyncSession,
    measurement_id: int,
    *,
    on_date: date_type,
    neck_cm: Optional[float] = None,
    waist_cm: Optional[float] = None,
    hips_cm: Optional[float] = None,
    note: Optional[str] = None,
    override: bool = False,
    partial: bool = True,
) -> Optional[BodyMeasurement]:
    """Edit an existing body measurement. If the date has changed, delete the old row
    and upsert the new one.

    ``partial`` carries the same meaning as in ``upsert_body_measurement``: the
    default keeps omitted fields (MCP), ``False`` lets the caller blank them
    (the HTML form)."""
    result = await session.execute(
        select(BodyMeasurement).where(BodyMeasurement.id == measurement_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        return None

    if row.date != on_date:
        if partial:
            # Carry the untouched fields off the old row *before* deleting it. The
            # partial merge in upsert_body_measurement reads the row on the target
            # date, which is empty here — so a caller that passed only one field (the
            # MCP edit tool routinely does) would otherwise blank the other two, and
            # body_fat_pct/lbm_kg derived from them, with no way to get them back.
            # Under partial=False the caller sent the whole row, so there is nothing
            # to carry: what it left empty it means to delete.
            neck_cm = neck_cm if neck_cm is not None else row.neck_cm
            waist_cm = waist_cm if waist_cm is not None else row.waist_cm
            hips_cm = hips_cm if hips_cm is not None else row.hips_cm
            note = note if note is not None else row.note
        await session.delete(row)
        await session.flush()

    return await upsert_body_measurement(
        session,
        on_date=on_date,
        neck_cm=neck_cm,
        waist_cm=waist_cm,
        hips_cm=hips_cm,
        note=note,
        override=override,
        partial=partial,
    )
