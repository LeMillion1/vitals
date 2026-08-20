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

Safety-relevant Weight and body-measurement writes run the conflict-engine
override plumbing, while noise ranges use the typed derived-alert lifecycle.
Scoped boundaries validate ownership before target reads and retain the legacy
singleton APIs only for the registration-disabled migration bridge.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import date as date_type, timedelta
from typing import TYPE_CHECKING, Optional, Sequence

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import Config, load_config
from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionType,
    IntegrationProvider,
    IntegrationConnectionStatus,
    Severity,
    Source,
)
from vitals.i18n import t
from vitals.models.ai import AIInvocation
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
from vitals.services import alerts_service, conflict_engine, file_asset_service
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


class BodyMeasurementScopedUniqueCutoverRequiredError(WeightOwnershipError):
    """The global body-measurement date key is occupied by another row."""


class ProgressPhotoOwnershipError(WeightOwnershipError):
    """A progress-photo fact or its private-file graph is not authoritative."""


@dataclass(frozen=True, slots=True)
class ProgressPhotoDeletion:
    """Immutable handoff for post-commit physical-file cleanup."""

    file_key: str
    file_asset_id: uuid.UUID | None


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


def _require_aux_prepared_write(
    session: AsyncSession,
    *,
    identity: WriteIdentity | None,
    prepared: conflict_engine.PreparedConflictWrite | None,
) -> conflict_engine.ConflictWriteContext | None:
    """Separate singleton compatibility calls from scoped auxiliary writes.

    Body measurements, noise markers, and progress-photo metadata do not mutate
    active Weight truth or the Garmin export outbox, so they use the generic
    conflict capability rather than taking Weight's installation-wide outbox
    advisory lock.
    """

    if identity is None and prepared is None:
        return None
    if identity is None or prepared is None:
        raise conflict_engine.ConflictPreparedWriteError(
            "scoped auxiliary weight writes require identity and a prepared "
            "conflict write"
        )
    return conflict_engine.require_prepared_identity(
        session,
        prepared=prepared,
        identity=identity,
    )


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
    exact_fact_raw = exact_raw
    if include_legacy_unowned:
        exact_fact_raw = or_(exact_fact_raw, fully_unowned_raw)
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
                    exact_fact_raw,
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
    include_legacy_unowned: bool,
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
    exact_fact_raw = exact_raw
    if include_legacy_unowned:
        exact_fact_raw = or_(exact_fact_raw, fully_unowned_raw)
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
                            exact_fact_raw,
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
            include_legacy_unowned=include_legacy_unowned,
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


async def _validate_body_scan_ai_origin(
    session: AsyncSession,
    *,
    raw: RawPayload,
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    for_update: bool,
    require_live_file: bool,
) -> None:
    """Prove the mutually exclusive historical-C/platform-AI raw lineage."""

    # The exact-one legacy bridge deliberately retains fully-null historical
    # parser rows. It can never authorize a platform call or file root.
    if all(
        root is None
        for root in (
            raw.subject_id,
            raw.actor_user_id,
            raw.integration_connection_id,
            raw.file_asset_id,
        )
    ):
        return
    if raw.file_asset_id is None:
        raise conflict_engine.ConflictRawOwnershipError(
            "body-scan Weight raw has no document provenance"
        )
    asset_stmt = select(FileAsset).where(FileAsset.id == raw.file_asset_id)
    if for_update:
        asset_stmt = asset_stmt.with_for_update()
    asset = await session.scalar(
        asset_stmt.execution_options(populate_existing=True)
    )
    live_file = (
        asset is not None
        and asset.status
        in {
            FileAssetStatus.LEGACY_PLACEHOLDER.value,
            FileAssetStatus.PENDING.value,
        }
        and asset.deleted_at is None
        and asset.purged_at is None
    )
    retired_file = (
        asset is not None
        and (
            (
                asset.status == FileAssetStatus.DELETED.value
                and asset.deleted_at is not None
                and asset.purged_at is None
            )
            or (
                asset.status == FileAssetStatus.PURGED.value
                and asset.deleted_at is not None
                and asset.purged_at is not None
            )
        )
    )
    if (
        asset is None
        or asset.subject_id != subject_id
        or asset.uploaded_by_user_id != actor_user_id
        or asset.purpose != FileAssetPurpose.BODY_SCAN_DOCUMENT.value
        or asset.storage_backend != FileStorageBackend.LEGACY_LOCAL.value
        or (not live_file and (require_live_file or not retired_file))
        or raw.external_id != asset.storage_ref
    ):
        raise conflict_engine.ConflictRawOwnershipError(
            "body-scan Weight document provenance is invalid"
        )
    stmt = select(AIInvocation).where(
        AIInvocation.raw_payload_id == raw.id,
        AIInvocation.purpose == AIInvocationPurpose.BODY_SCAN_PARSE.value,
    ).order_by(AIInvocation.created_at, AIInvocation.id)
    if for_update:
        stmt = stmt.with_for_update()
    invocations = list(
        await session.scalars(stmt.execution_options(populate_existing=True))
    )
    if raw.integration_connection_id is not None:
        if invocations:
            raise conflict_engine.ConflictRawOwnershipError(
                "body-scan Weight mixes subject and platform parser provenance"
            )
        return
    if len(invocations) != 1:
        raise conflict_engine.ConflictRawOwnershipError(
            "platform body-scan Weight requires one parser invocation"
        )
    invocation = invocations[0]
    if (
        invocation.subject_id != subject_id
        or invocation.actor_user_id != actor_user_id
        or invocation.raw_payload_id != raw.id
        or invocation.source != AIInvocationSource.WEB.value
        or invocation.status != AIInvocationStatus.SUCCEEDED.value
    ):
        raise conflict_engine.ConflictRawOwnershipError(
            "platform body-scan Weight parser provenance is invalid"
        )


async def _reject_body_scan_ai_invocation(
    session: AsyncSession,
    *,
    raw_payload_id: int,
    for_update: bool,
) -> None:
    stmt = (
        select(AIInvocation.id)
        .where(
            AIInvocation.raw_payload_id == raw_payload_id,
            AIInvocation.purpose == AIInvocationPurpose.BODY_SCAN_PARSE.value,
        )
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    if await session.scalar(stmt) is not None:
        raise conflict_engine.ConflictRawOwnershipError(
            "MCP body-composition raw cannot claim an AI parser invocation"
        )


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
    allowed_raw_sources = {source}
    if source == Source.BODY_SCAN.value:
        # Structured MCP body composition is raw-first as MCP, while its
        # derived Weight fact remains BODY_SCAN so source priority and chart
        # semantics describe the measurement rather than the transport.
        allowed_raw_sources.add(Source.MCP.value)
    if raw.source not in allowed_raw_sources:
        raise conflict_engine.ConflictRawOwnershipError(
            "weight source does not match durable raw provenance"
        )
    if source == Source.BODY_SCAN.value and raw.source == Source.MCP.value and (
        raw.actor_user_id is None
        or integration_connection_id is not None
        or raw.file_asset_id is not None
    ):
        raise conflict_engine.ConflictRawOwnershipError(
            "MCP body-composition lineage must have null connection and file roots"
        )
    if source == Source.BODY_SCAN.value and raw.source == Source.MCP.value:
        await _reject_body_scan_ai_invocation(
            session,
            raw_payload_id=raw.id,
            for_update=True,
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
    if source == Source.BODY_SCAN.value and raw.source == Source.BODY_SCAN.value:
        await _validate_body_scan_ai_origin(
            session,
            raw=raw,
            subject_id=context.identity.subject_id,
            actor_user_id=requested_actor,
            for_update=True,
            require_live_file=True,
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
        raw_is_fully_unowned = all(
            root is None
            for root in (
                raw.subject_id,
                raw.actor_user_id,
                raw.integration_connection_id,
                raw.file_asset_id,
            )
        )
        if is_legacy:
            if not raw_is_fully_unowned:
                raise conflict_engine.ConflictRawOwnershipError(
                    "legacy weight fact links to partially owned raw provenance"
                )
        elif raw.subject_id != subject_id:
            if not include_legacy_unowned or not raw_is_fully_unowned:
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
            or raw.source not in {Source.BODY_SCAN.value, Source.MCP.value}
            or (
                raw.source == Source.MCP.value
                and (
                    raw.actor_user_id is None
                    or connection is not None
                    or raw.integration_connection_id is not None
                    or raw.file_asset_id is not None
                )
            )
        ):
            raise conflict_engine.ConflictRawOwnershipError(
                "body-scan weight raw provenance is incompatible"
            )
        if raw is not None and raw.source == Source.BODY_SCAN.value:
            await _validate_body_scan_ai_origin(
                session,
                raw=raw,
                subject_id=subject_id,
                actor_user_id=row.actor_user_id,
                for_update=False,
                require_live_file=False,
            )
        elif raw is not None and raw.source == Source.MCP.value:
            await _reject_body_scan_ai_invocation(
                session,
                raw_payload_id=raw.id,
                for_update=False,
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
        include_legacy_unowned=include_legacy_unowned,
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
                include_legacy_unowned=include_legacy_unowned,
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
            include_legacy_unowned=include_legacy_unowned,
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
        include_legacy_unowned=include_legacy_unowned,
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
        BodyMeasurement.domain == DOMAIN,
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
            BodyMeasurement.domain == DOMAIN,
            BodyMeasurement.subject_id.is_(None),
            BodyMeasurement.actor_user_id.is_(None),
        ),
    )


def _noise_marker_scope_condition(
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
        NoiseMarker.domain == DOMAIN,
        NoiseMarker.subject_id == subject_id,
        or_(
            NoiseMarker.actor_user_id.is_(None),
            NoiseMarker.actor_user_id == owner_user_id,
        ),
    )
    if not include_legacy_unowned:
        return exact
    return or_(
        exact,
        and_(
            NoiseMarker.domain == DOMAIN,
            NoiseMarker.subject_id.is_(None),
            NoiseMarker.actor_user_id.is_(None),
        ),
    )


async def _assert_body_measurement_scope_integrity(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    filters: Sequence = (),
) -> None:
    valid = _body_measurement_scope_condition(
        subject_id=subject_id,
        include_legacy_unowned=True,
    )
    invalid = await session.scalar(
        select(BodyMeasurement.id)
        .where(
            or_(
                BodyMeasurement.subject_id == subject_id,
                BodyMeasurement.subject_id.is_(None),
            ),
            *filters,
            valid.is_not(True),
        )
        .limit(1)
    )
    if invalid is not None:
        raise WeightOwnershipError(
            "body measurement has partial or conflicting ownership provenance"
        )


async def _assert_noise_marker_scope_integrity(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    filters: Sequence = (),
) -> None:
    valid = _noise_marker_scope_condition(
        subject_id=subject_id,
        include_legacy_unowned=True,
    )
    invalid = await session.scalar(
        select(NoiseMarker.id)
        .where(
            or_(
                NoiseMarker.subject_id == subject_id,
                NoiseMarker.subject_id.is_(None),
            ),
            *filters,
            valid.is_not(True),
        )
        .limit(1)
    )
    if invalid is not None:
        raise WeightOwnershipError(
            "noise marker has partial or conflicting ownership provenance"
        )


async def _get_noise_marker_for_update(
    session: AsyncSession,
    marker_id: int,
    *,
    subject_id: uuid.UUID | None,
    include_legacy_unowned: bool,
) -> NoiseMarker | None:
    stmt = select(NoiseMarker).where(NoiseMarker.id == marker_id)
    if subject_id is not None:
        await _assert_noise_marker_scope_integrity(
            session,
            subject_id=subject_id,
            filters=(NoiseMarker.id == marker_id,),
        )
        stmt = stmt.where(
            _noise_marker_scope_condition(
                subject_id=subject_id,
                include_legacy_unowned=include_legacy_unowned,
            )
        )
    elif include_legacy_unowned:
        raise ValueError("legacy noise compatibility requires a subject_id")
    return await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )


async def _get_body_measurement_for_update(
    session: AsyncSession,
    measurement_id: int,
    *,
    subject_id: uuid.UUID | None,
    include_legacy_unowned: bool,
) -> BodyMeasurement | None:
    stmt = select(BodyMeasurement).where(BodyMeasurement.id == measurement_id)
    if subject_id is not None:
        await _assert_body_measurement_scope_integrity(
            session,
            subject_id=subject_id,
            filters=(BodyMeasurement.id == measurement_id,),
        )
        stmt = stmt.where(
            _body_measurement_scope_condition(
                subject_id=subject_id,
                include_legacy_unowned=include_legacy_unowned,
            )
        )
    elif include_legacy_unowned:
        raise ValueError("legacy body-measurement compatibility requires a subject_id")
    return await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )


async def _get_body_measurement_for_date_update(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID | None,
    include_legacy_unowned: bool,
) -> BodyMeasurement | None:
    stmt = select(BodyMeasurement).where(BodyMeasurement.date == on_date)
    if subject_id is not None:
        await _assert_body_measurement_scope_integrity(
            session,
            subject_id=subject_id,
            filters=(BodyMeasurement.date == on_date,),
        )
        stmt = stmt.where(
            _body_measurement_scope_condition(
                subject_id=subject_id,
                include_legacy_unowned=include_legacy_unowned,
            )
        )
    elif include_legacy_unowned:
        raise ValueError("legacy body-measurement compatibility requires a subject_id")
    row = await session.scalar(
        stmt.with_for_update().execution_options(populate_existing=True)
    )
    if row is not None or subject_id is None:
        return row
    occupied = await session.scalar(
        select(BodyMeasurement.id)
        .where(BodyMeasurement.date == on_date)
        .with_for_update()
    )
    if occupied is not None:
        raise BodyMeasurementScopedUniqueCutoverRequiredError(
            "body-measurement date is occupied outside the selected subject scope"
        )
    return None


def _require_aux_source(source: str | Source) -> str:
    value = source.value if isinstance(source, Source) else source
    if value not in {Source.MANUAL.value, Source.MCP.value}:
        raise ValueError("body measurement/noise source must be manual or mcp")
    return value


def _effective_measurement_values(
    row: BodyMeasurement | None,
    *,
    neck_cm: Optional[float],
    waist_cm: Optional[float],
    hips_cm: Optional[float],
    note: Optional[str],
    partial: bool,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    if not partial or row is None:
        return neck_cm, waist_cm, hips_cm, note
    return (
        neck_cm if neck_cm is not None else row.neck_cm,
        waist_cm if waist_cm is not None else row.waist_cm,
        hips_cm if hips_cm is not None else row.hips_cm,
        note if note is not None else row.note,
    )


async def _apply_body_measurement_values(
    session: AsyncSession,
    row: BodyMeasurement,
    *,
    on_date: date_type,
    neck_cm: Optional[float],
    waist_cm: Optional[float],
    hips_cm: Optional[float],
    note: Optional[str],
    subject_id: uuid.UUID | None,
    include_legacy_unowned: bool,
) -> None:
    height_cm, sex = _body_config()
    body_fat_pct = None
    if neck_cm and waist_cm:
        try:
            body_fat_pct = navy_body_fat_pct(
                waist_cm=waist_cm,
                neck_cm=neck_cm,
                height_cm=height_cm,
                sex=sex,
                hips_cm=hips_cm,
            )
        except ValueError:
            body_fat_pct = None

    lbm_kg = None
    if body_fat_pct is not None:
        active = await get_active_weight(
            session,
            on_date,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )
        if active is not None:
            lbm_kg = lean_body_mass_kg(active.weight_kg, body_fat_pct)

    row.date = on_date
    row.neck_cm = neck_cm
    row.waist_cm = waist_cm
    row.hips_cm = hips_cm
    row.body_fat_pct = body_fat_pct
    row.lbm_kg = lbm_kg
    row.note = note


async def _enforce_body_measurement_write(
    session: AsyncSession,
    *,
    context: conflict_engine.ConflictWriteContext | None,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None,
    on_date: date_type,
    override: bool,
) -> None:
    proposed = {"measurement": True}
    if context is None:
        await conflict_engine.enforce(
            session,
            Domain.WEIGHT.value,
            proposed,
            override=override,
            entity_ref=f"body_measurement:{on_date.isoformat()}",
        )
        return
    assert prepared_conflict_write is not None
    await conflict_engine.enforce_prepared(
        session,
        prepared=prepared_conflict_write,
        domain=Domain.WEIGHT,
        proposed_state=proposed,
        override=override,
        entity_ref=f"body_measurement:{on_date.isoformat()}",
    )


async def upsert_body_measurement(
    session: AsyncSession,
    *,
    on_date: date_type,
    neck_cm: Optional[float] = None,
    waist_cm: Optional[float] = None,
    hips_cm: Optional[float] = None,
    note: Optional[str] = None,
    source: str | Source = Source.MANUAL.value,
    override: bool = False,
    partial: bool = True,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
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
    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if context is not None:
        _require_evaluation_date(context, on_date)
        _require_legacy_bridge(
            context,
            include_legacy_unowned=include_legacy_unowned,
        )
        source_value = _require_aux_source(source)
    else:
        if include_legacy_unowned:
            raise ValueError(
                "legacy body-measurement compatibility requires a scoped writer"
            )
        source_value = source.value if isinstance(source, Source) else source

    _check_range("neck_cm", neck_cm, _CIRCUMFERENCE_CM_RANGE)
    _check_range("waist_cm", waist_cm, _CIRCUMFERENCE_CM_RANGE)
    _check_range("hips_cm", hips_cm, _CIRCUMFERENCE_CM_RANGE)
    row = await _get_body_measurement_for_date_update(
        session,
        on_date,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    )
    effective_neck, effective_waist, effective_hips, effective_note = (
        _effective_measurement_values(
            row,
            neck_cm=neck_cm,
            waist_cm=waist_cm,
            hips_cm=hips_cm,
            note=note,
            partial=partial,
        )
    )
    await _enforce_body_measurement_write(
        session,
        context=context,
        prepared_conflict_write=prepared_conflict_write,
        on_date=on_date,
        override=override,
    )

    if row is None:
        row = BodyMeasurement(
            subject_id=identity.subject_id if identity is not None else None,
            actor_user_id=identity.actor_user_id if identity is not None else None,
            date=on_date,
            domain=DOMAIN,
            source=source_value,
        )
        session.add(row)
    elif row.subject_id is None and identity is not None:
        row.subject_id = identity.subject_id

    await _apply_body_measurement_values(
        session,
        row,
        on_date=on_date,
        neck_cm=effective_neck,
        waist_cm=effective_waist,
        hips_cm=effective_hips,
        note=effective_note,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    )
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
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
    start: date_type | None = None,
    end: date_type | None = None,
    has_note: bool = False,
    limit: int | None = None,
) -> Sequence[BodyMeasurement]:
    filters = []
    if start is not None:
        filters.append(BodyMeasurement.date >= start)
    if end is not None:
        filters.append(BodyMeasurement.date <= end)
    if has_note:
        filters.extend((BodyMeasurement.note.is_not(None), BodyMeasurement.note != ""))
    stmt = select(BodyMeasurement).where(*filters)
    if subject_id is not None:
        await _assert_body_measurement_scope_integrity(
            session,
            subject_id=subject_id,
            filters=tuple(filters),
        )
        stmt = stmt.where(
            _body_measurement_scope_condition(
                subject_id=subject_id,
                include_legacy_unowned=include_legacy_unowned,
            )
        )
    elif include_legacy_unowned:
        raise ValueError("legacy body-measurement compatibility requires a subject_id")
    stmt = stmt.order_by(BodyMeasurement.date)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


# ── Noise markers ─────────────────────────────────────────────────────────────
async def add_noise_marker(
    session: AsyncSession,
    *,
    start_date: date_type,
    end_date: Optional[date_type] = None,
    reason: str,
    direction: Optional[str] = None,
    source: str | Source = Source.MANUAL.value,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> NoiseMarker:
    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if context is not None:
        _require_legacy_bridge(
            context,
            include_legacy_unowned=include_legacy_unowned,
        )
        source_value = _require_aux_source(source)
    else:
        if include_legacy_unowned:
            raise ValueError("legacy noise compatibility requires a scoped writer")
        source_value = source.value if isinstance(source, Source) else source
    reason = reason.strip()
    if not reason:
        raise ValueError("noise marker reason must not be blank")
    if end_date is not None and end_date < start_date:
        raise ValueError("noise marker end_date must not precede start_date")
    if direction not in {None, "up", "down", "neutral"}:
        raise ValueError("noise marker direction must be up, down, neutral, or null")
    marker = NoiseMarker(
        subject_id=identity.subject_id if identity is not None else None,
        actor_user_id=identity.actor_user_id if identity is not None else None,
        domain=DOMAIN,
        source=source_value,
        start_date=start_date,
        end_date=end_date,
        reason=reason,
        direction=direction,
    )
    session.add(marker)
    await session.flush()
    if context is not None:
        assert identity is not None and prepared_conflict_write is not None
        await refresh_noise_alert(
            session,
            on_date=context.evaluation_date,
            identity=identity,
            prepared_conflict_write=prepared_conflict_write,
        )
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
    filters = []
    if start is not None:
        filters.append(
            or_(NoiseMarker.end_date.is_(None), NoiseMarker.end_date >= start)
        )
    if end is not None:
        filters.append(NoiseMarker.start_date <= end)
    if subject_id is not None:
        await _assert_noise_marker_scope_integrity(
            session,
            subject_id=subject_id,
            filters=tuple(filters),
        )
        stmt = stmt.where(
            _noise_marker_scope_condition(
                subject_id=subject_id,
                include_legacy_unowned=include_legacy_unowned,
            )
        )
    elif include_legacy_unowned:
        raise ValueError("legacy weight compatibility requires a subject_id")
    if filters:
        stmt = stmt.where(*filters)
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
_PROGRESS_PHOTO_LIVE_ASSET_STATUSES = (
    FileAssetStatus.LEGACY_PLACEHOLDER.value,
    FileAssetStatus.PENDING.value,
)


def _progress_photo_document_alias(file_key: str) -> str | None:
    """Return the lab/body metadata locator sharing this local disk path."""

    if file_key.startswith(("uploads/labs/", "uploads/body/")):
        return file_key.removeprefix("uploads/")
    return None


async def _progress_photo_scope_rows(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    include_legacy_unowned: bool,
    filters: Sequence = (),
    for_update: bool = False,
) -> list[ProgressPhoto]:
    """Load and validate every photo that can affect one subject scope.

    The compatibility arm deliberately samples every ``S IS NULL`` candidate,
    not only the fully-null rows that would be returned. That makes a partial
    legacy root a typed integrity failure instead of silently hiding it.
    """

    from vitals.models.identity import HealthSubject

    if not isinstance(subject_id, uuid.UUID):
        raise ProgressPhotoOwnershipError("progress-photo subject_id must be a UUID")
    candidate_scope = ProgressPhoto.subject_id == subject_id
    if include_legacy_unowned:
        candidate_scope = or_(
            candidate_scope,
            ProgressPhoto.subject_id.is_(None),
        )
    stmt = select(ProgressPhoto).where(candidate_scope, *filters)
    if for_update:
        stmt = stmt.with_for_update()
    rows = list(
        (
            await session.scalars(
                stmt.execution_options(populate_existing=True)
            )
        ).all()
    )
    if not rows:
        return []

    owner_user_id = await session.scalar(
        select(HealthSubject.owner_user_id).where(HealthSubject.id == subject_id)
    )
    if owner_user_id is None:
        raise ProgressPhotoOwnershipError("progress-photo subject does not exist")

    file_asset_ids = {
        row.file_asset_id for row in rows if row.file_asset_id is not None
    }
    legacy_file_keys = {
        row.file_key for row in rows if row.subject_id is None
    }
    document_aliases_by_key = {
        row.file_key: alias
        for row in rows
        if (alias := _progress_photo_document_alias(row.file_key)) is not None
    }
    assets: dict[uuid.UUID, FileAsset] = {}
    counts: dict[uuid.UUID, int] = {}
    shadowed_legacy_keys: set[str] = set()
    shadowed_document_aliases: set[str] = set()
    key_counts = {
        file_key: count
        for file_key, count in (
            await session.execute(
                select(ProgressPhoto.file_key, func.count(ProgressPhoto.id))
                .where(ProgressPhoto.file_key.in_({row.file_key for row in rows}))
                .group_by(ProgressPhoto.file_key)
            )
        ).all()
    }
    if file_asset_ids:
        asset_rows = (
            await session.scalars(
                select(FileAsset)
                .where(FileAsset.id.in_(file_asset_ids))
                .execution_options(populate_existing=True)
            )
        ).all()
        assets = {row.id: row for row in asset_rows}
        counts = {
            file_asset_id: count
            for file_asset_id, count in (
                await session.execute(
                    select(ProgressPhoto.file_asset_id, func.count(ProgressPhoto.id))
                    .where(ProgressPhoto.file_asset_id.in_(file_asset_ids))
                    .group_by(ProgressPhoto.file_asset_id)
                )
            ).all()
            if file_asset_id is not None
        }
    if legacy_file_keys:
        shadowed_legacy_keys = set(
            (
                await session.scalars(
                    select(FileAsset.storage_ref).where(
                        FileAsset.storage_ref.in_(legacy_file_keys)
                    )
                )
            ).all()
        )
    if document_aliases_by_key:
        shadowed_document_aliases = set(
            (
                await session.scalars(
                    select(FileAsset.storage_ref).where(
                        FileAsset.storage_ref.in_(document_aliases_by_key.values())
                    )
                )
            ).all()
        )

    for row in rows:
        if key_counts.get(row.file_key) != 1:
            raise ProgressPhotoOwnershipError(
                "progress-photo file key is linked by more than one fact"
            )
        if row.domain != DOMAIN or row.source != Source.MANUAL.value:
            raise ProgressPhotoOwnershipError(
                "progress photo has invalid domain or source provenance"
            )
        document_alias = document_aliases_by_key.get(row.file_key)
        if document_alias in shadowed_document_aliases:
            raise ProgressPhotoOwnershipError(
                "progress photo aliases document file metadata"
            )
        if row.subject_id is None:
            if row.actor_user_id is not None or row.file_asset_id is not None:
                raise ProgressPhotoOwnershipError(
                    "progress photo has partial legacy ownership roots"
                )
            if row.file_key in shadowed_legacy_keys:
                raise ProgressPhotoOwnershipError(
                    "legacy progress photo conflicts with file-asset metadata"
                )
            continue
        if row.subject_id != subject_id:
            raise ProgressPhotoOwnershipError(
                "progress photo belongs to another subject"
            )
        if row.actor_user_id != owner_user_id:
            raise ProgressPhotoOwnershipError(
                "progress photo actor does not match the subject owner"
            )
        if row.file_asset_id is None:
            raise ProgressPhotoOwnershipError(
                "owned progress photo is missing its file asset"
            )
        asset = assets.get(row.file_asset_id)
        if asset is None:
            raise ProgressPhotoOwnershipError(
                "progress photo links to a missing file asset"
            )
        if (
            asset.subject_id != subject_id
            or asset.uploaded_by_user_id != owner_user_id
            or asset.purpose != FileAssetPurpose.PROGRESS_PHOTO.value
            or asset.storage_backend != FileStorageBackend.LEGACY_LOCAL.value
            or asset.status not in _PROGRESS_PHOTO_LIVE_ASSET_STATUSES
            or asset.storage_ref != row.file_key
        ):
            raise ProgressPhotoOwnershipError(
                "progress photo file asset has conflicting ownership or lifecycle"
            )
        if counts.get(row.file_asset_id) != 1:
            raise ProgressPhotoOwnershipError(
                "progress photo file asset is linked by more than one fact"
            )
    return rows


async def add_progress_photo(
    session: AsyncSession,
    *,
    on_date: date_type,
    file_key: str | None = None,
    note: Optional[str] = None,
    identity: WriteIdentity | None = None,
    file_asset_id: uuid.UUID | None = None,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> ProgressPhoto:
    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if context is None:
        if file_asset_id is not None:
            raise ProgressPhotoOwnershipError(
                "legacy progress photos cannot carry a file asset root"
            )
        if not isinstance(file_key, str) or not file_key:
            raise ValueError("legacy progress photo requires a file_key")
        conflicting_refs = [file_key]
        document_alias = _progress_photo_document_alias(file_key)
        if document_alias is not None:
            conflicting_refs.append(document_alias)
        shadow_asset_id = await session.scalar(
            select(FileAsset.id)
            .where(FileAsset.storage_ref.in_(conflicting_refs))
            .with_for_update()
        )
        if shadow_asset_id is not None:
            raise ProgressPhotoOwnershipError(
                "legacy progress photo conflicts with file-asset metadata"
            )
        existing_photo_id = await session.scalar(
            select(ProgressPhoto.id)
            .where(ProgressPhoto.file_key == file_key)
            .with_for_update()
        )
        if existing_photo_id is not None:
            raise ProgressPhotoOwnershipError(
                "progress-photo file key already has a fact"
            )
        authoritative_file_key = file_key
    else:
        assert identity is not None
        from vitals.models.identity import HealthSubject

        _require_evaluation_date(context, on_date)
        if identity.actor_user_id is None:
            raise ProgressPhotoOwnershipError(
                "progress photo creation requires a human owner actor"
            )
        owner_user_id = await session.scalar(
            select(HealthSubject.owner_user_id).where(
                HealthSubject.id == identity.subject_id
            )
        )
        if owner_user_id != identity.actor_user_id:
            raise ProgressPhotoOwnershipError(
                "progress photo actor does not match the subject owner"
            )
        if not isinstance(file_asset_id, uuid.UUID):
            raise ProgressPhotoOwnershipError(
                "owned progress photo requires a file_asset_id"
            )
        asset = await session.scalar(
            select(FileAsset)
            .where(FileAsset.id == file_asset_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if asset is None or (
            asset.subject_id != identity.subject_id
            or asset.uploaded_by_user_id != identity.actor_user_id
            or asset.purpose != FileAssetPurpose.PROGRESS_PHOTO.value
            or asset.storage_backend != FileStorageBackend.LEGACY_LOCAL.value
            or asset.status not in _PROGRESS_PHOTO_LIVE_ASSET_STATUSES
        ):
            raise ProgressPhotoOwnershipError(
                "progress photo file asset is not authoritative in subject scope"
            )
        if file_key is not None and file_key != asset.storage_ref:
            raise ProgressPhotoOwnershipError(
                "progress photo file key conflicts with its file asset"
            )
        document_alias = _progress_photo_document_alias(asset.storage_ref)
        if document_alias is not None:
            aliased_asset_id = await session.scalar(
                select(FileAsset.id)
                .where(FileAsset.storage_ref == document_alias)
                .with_for_update()
            )
            if aliased_asset_id is not None:
                raise ProgressPhotoOwnershipError(
                    "progress photo aliases document file metadata"
                )
        existing = await session.scalar(
            select(ProgressPhoto.id)
            .where(
                or_(
                    ProgressPhoto.file_asset_id == file_asset_id,
                    ProgressPhoto.file_key == asset.storage_ref,
                )
            )
            .with_for_update()
        )
        if existing is not None:
            raise ProgressPhotoOwnershipError(
                "progress photo file asset already has a fact"
            )
        authoritative_file_key = asset.storage_ref

    photo = ProgressPhoto(
        subject_id=identity.subject_id if identity is not None else None,
        actor_user_id=identity.actor_user_id if identity is not None else None,
        file_asset_id=file_asset_id,
        date=on_date,
        domain=DOMAIN,
        source=Source.MANUAL.value,
        file_key=authoritative_file_key,
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
    start: date_type | None = None,
    end: date_type | None = None,
) -> Sequence[ProgressPhoto]:
    filters = []
    if start is not None:
        filters.append(ProgressPhoto.date >= start)
    if end is not None:
        filters.append(ProgressPhoto.date <= end)
    if subject_id is None:
        if include_legacy_unowned:
            raise ValueError("legacy progress-photo compatibility requires a subject_id")
        stmt = select(ProgressPhoto).where(*filters)
        result = await session.execute(stmt.order_by(ProgressPhoto.date.desc()))
        return result.scalars().all()
    rows = await _progress_photo_scope_rows(
        session,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
        filters=tuple(filters),
    )
    return sorted(rows, key=lambda row: (row.date, row.id), reverse=True)


async def get_progress_photo(
    session: AsyncSession,
    photo_id: int,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> ProgressPhoto | None:
    if subject_id is None:
        if include_legacy_unowned:
            raise ValueError("legacy progress-photo compatibility requires a subject_id")
        return await session.get(ProgressPhoto, photo_id)
    rows = await _progress_photo_scope_rows(
        session,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
        filters=(ProgressPhoto.id == photo_id,),
    )
    return rows[0] if rows else None


async def get_progress_photo_by_file_key(
    session: AsyncSession,
    *,
    file_key: str,
    subject_id: uuid.UUID,
    include_legacy_unowned: bool = False,
) -> ProgressPhoto | None:
    if not isinstance(file_key, str) or not file_key:
        raise ProgressPhotoOwnershipError("progress-photo file_key must be non-blank")
    rows = await _progress_photo_scope_rows(
        session,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
        filters=(ProgressPhoto.file_key == file_key,),
    )
    if len(rows) > 1:
        raise ProgressPhotoOwnershipError(
            "progress-photo file key resolves to more than one fact"
        )
    return rows[0] if rows else None


# ── Alerts ────────────────────────────────────────────────────────────────────
async def refresh_noise_alert(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    identity: WriteIdentity | None = None,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> Optional[object]:
    """Raise an ``info`` alert while today sits inside a noise range; resolve it
    once it doesn't. Idempotent (safe to call on every dashboard load / tick)."""
    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    today = on_date or today_local()
    if context is not None:
        _require_evaluation_date(context, today)
    include_legacy_unowned = bool(
        context is not None
        and context.legacy_bridge
        is conflict_engine.LegacyConflictBridge.FULLY_UNOWNED
    )
    active_reason = None
    for marker in await list_noise_markers(
        session,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    ):
        end = marker.end_date
        if (end is None and today >= marker.start_date) or (
            end is not None and marker.start_date <= today <= end
        ):
            active_reason = marker.reason
            break

    if active_reason is not None:
        # Don't re-raise if the user already dismissed this alert today — it will
        # reappear automatically the next calendar day.
        if context is None:
            if await alerts_service._was_dismissed_today(
                session, NOISE_ALERT_KEY, ""
            ):
                return None
            return await alerts_service.raise_alert(
                session,
                domain=Domain.WEIGHT.value,
                severity=Severity.INFO.value,
                message=t("alert.weight_noisy", reason=active_reason),
                alert_key=NOISE_ALERT_KEY,
            )
        system_context = alerts_service.HealthAlertContext(
            WriteIdentity(context.identity.subject_id, None)
        )
        alert_bridge = (
            alerts_service.LegacyAlertBridge.FULLY_UNOWNED
            if include_legacy_unowned
            else alerts_service.LegacyAlertBridge.REJECT
        )
        if await alerts_service.was_scoped_dismissed_today(
            session,
            context=system_context,
            alert_key=NOISE_ALERT_KEY,
            entity_ref="",
            on_date=today,
            legacy_bridge=alert_bridge,
        ):
            return None
        return await alerts_service.raise_scoped_alert(
            session,
            context=system_context,
            domain=Domain.WEIGHT,
            severity=Severity.INFO,
            message=t("alert.weight_noisy", reason=active_reason),
            alert_key=NOISE_ALERT_KEY,
            legacy_bridge=alert_bridge,
        )
    if context is None:
        return await alerts_service.resolve_by_key(
            session, alert_key=NOISE_ALERT_KEY
        )
    system_context = alerts_service.HealthAlertContext(
        WriteIdentity(context.identity.subject_id, None)
    )
    return await alerts_service.resolve_scoped_by_key(
        session,
        context=system_context,
        alert_key=NOISE_ALERT_KEY,
        legacy_bridge=(
            alerts_service.LegacyAlertBridge.FULLY_UNOWNED
            if include_legacy_unowned
            else alerts_service.LegacyAlertBridge.REJECT
        ),
    )


# ── Chart series ──────────────────────────────────────────────────────────────
async def chart_series(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
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
    weights = await list_active_weights(
        session,
        end=end,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    raw_points = [(w.date, w.weight_kg) for w in weights]

    # Noise ranges fully drop out of the MA / regression / projection (a core
    # invariant): the trend must reflect real trajectory, not water-weight spikes.
    # The raw scatter keeps every point (shown under the noise overlay).
    ranges = [
        (start, range_end)
        for start, range_end in await _noise_ranges(
            session,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
            end=end,
        )
        if end is None or start <= end
    ]
    clean_points = exclude_ranges(raw_points, ranges)
    ma = rolling_mean_by_date(clean_points, window_days=7)

    measurements = [
        row
        for row in await list_body_measurements(
            session,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
            end=end,
        )
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

    phases = (
        await _glp1_phase_overlays(
            session,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )
        if include_glp1
        else []
    )

    # BIA overlay (InBody/МедАсс) — a second source for body-fat % / LBM shown
    # alongside the Navy series. Lazily imported so the weight module never hard-
    # depends on body_comp; only assembled when the module is enabled.
    bia = None
    if include_bia:
        from vitals.services import body_scan_service

        bia = await body_scan_service.bia_chart_points(
            session,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
        )

    # Timeline flags (manual annotations) — lazy import, only when the
    # optional timeline module is on (a disabled module behaves as absent).
    annotations = None
    if include_timeline:
        from vitals.services import timeline_service

        annotations = await timeline_service.overlays_for(
            session,
            subject_id=subject_id,
            include_legacy_unowned=include_legacy_unowned,
            domain=DOMAIN,
        )

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


async def _glp1_phase_overlays(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> list[dict]:
    """GLP-1 dose phases for the chart overlay. Imported lazily so the weight
    module never depends on glp1 at import time (the cross-module link only
    exists for this one read, populated once Phase 2 lands)."""
    from vitals.services import glp1_service

    phases = await glp1_service.list_dose_phases(
        session,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
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
                include_legacy_unowned=include_legacy_unowned,
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


async def delete_body_measurement(
    session: AsyncSession,
    measurement_id: int,
    *,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> bool:
    """Delete a body measurement record by ID."""
    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if context is not None:
        _require_legacy_bridge(
            context,
            include_legacy_unowned=include_legacy_unowned,
        )
    elif include_legacy_unowned:
        raise ValueError(
            "legacy body-measurement compatibility requires a scoped writer"
        )
    row = await _get_body_measurement_for_update(
        session,
        measurement_id,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    )
    if not row:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def delete_progress_photo(
    session: AsyncSession,
    photo_id: int,
    *,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> ProgressPhotoDeletion | None:
    """Delete a photo fact and retire its file metadata in one transaction."""

    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if context is not None:
        _require_legacy_bridge(
            context,
            include_legacy_unowned=include_legacy_unowned,
        )
        assert identity is not None
        from vitals.models.identity import HealthSubject

        owner_user_id = await session.scalar(
            select(HealthSubject.owner_user_id).where(
                HealthSubject.id == identity.subject_id
            )
        )
        if identity.actor_user_id is None or owner_user_id != identity.actor_user_id:
            raise ProgressPhotoOwnershipError(
                "progress photo deletion requires the subject owner actor"
            )
        candidate = (
            await session.execute(
                select(
                    ProgressPhoto.subject_id,
                    ProgressPhoto.actor_user_id,
                    ProgressPhoto.file_asset_id,
                    ProgressPhoto.file_key,
                ).where(
                    ProgressPhoto.id == photo_id,
                    or_(
                        ProgressPhoto.subject_id == identity.subject_id,
                        ProgressPhoto.subject_id.is_(None),
                    ),
                )
            )
        ).one_or_none()
    else:
        if include_legacy_unowned:
            raise ValueError(
                "legacy progress-photo compatibility requires a scoped writer"
            )
        candidate = (
            await session.execute(
                select(
                    ProgressPhoto.subject_id,
                    ProgressPhoto.actor_user_id,
                    ProgressPhoto.file_asset_id,
                    ProgressPhoto.file_key,
                ).where(ProgressPhoto.id == photo_id)
            )
        ).one_or_none()
    if candidate is None:
        return None

    candidate_subject_id, candidate_actor_id, candidate_file_id, candidate_key = (
        candidate
    )
    if context is None:
        if any(
            value is not None
            for value in (
                candidate_subject_id,
                candidate_actor_id,
                candidate_file_id,
            )
        ):
            raise ProgressPhotoOwnershipError(
                "unscoped deletion is limited to fully-unowned progress photos"
            )
        row = await session.scalar(
            select(ProgressPhoto)
            .where(ProgressPhoto.id == photo_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            return None
        if (
            row.subject_id is not None
            or row.actor_user_id is not None
            or row.file_asset_id is not None
            or row.domain != DOMAIN
            or row.source != Source.MANUAL.value
        ):
            raise ProgressPhotoOwnershipError(
                "legacy progress photo changed ownership before deletion"
            )
        conflicting_refs = [row.file_key]
        document_alias = _progress_photo_document_alias(row.file_key)
        if document_alias is not None:
            conflicting_refs.append(document_alias)
        shadow_asset_id = await session.scalar(
            select(FileAsset.id).where(FileAsset.storage_ref.in_(conflicting_refs))
        )
        if shadow_asset_id is not None:
            raise ProgressPhotoOwnershipError(
                "legacy progress photo conflicts with file-asset metadata"
            )
        receipt = ProgressPhotoDeletion(row.file_key, None)
        await session.delete(row)
        await session.flush()
        return receipt

    assert identity is not None
    asset = None
    if candidate_file_id is not None:
        asset = await session.scalar(
            select(FileAsset)
            .where(FileAsset.id == candidate_file_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    rows = await _progress_photo_scope_rows(
        session,
        subject_id=identity.subject_id,
        include_legacy_unowned=include_legacy_unowned,
        filters=(ProgressPhoto.id == photo_id,),
        for_update=True,
    )
    if not rows:
        return None
    row = rows[0]
    if (
        row.subject_id != candidate_subject_id
        or row.actor_user_id != candidate_actor_id
        or row.file_asset_id != candidate_file_id
        or row.file_key != candidate_key
    ):
        raise ProgressPhotoOwnershipError(
            "progress photo provenance changed while deletion was being authorized"
        )

    receipt = ProgressPhotoDeletion(row.file_key, row.file_asset_id)
    if row.file_asset_id is not None:
        if asset is None or asset.id != row.file_asset_id:
            raise ProgressPhotoOwnershipError(
                "progress photo file asset disappeared during deletion"
            )
        await file_asset_service.mark_legacy_local_deleted(
            session,
            file_asset_id=row.file_asset_id,
            subject_id=identity.subject_id,
            purged=False,
        )
    await session.delete(row)
    await session.flush()
    return receipt


async def delete_noise_marker(
    session: AsyncSession,
    marker_id: int,
    *,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> bool:
    """Delete a noise marker record by ID."""
    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if context is not None:
        _require_legacy_bridge(
            context,
            include_legacy_unowned=include_legacy_unowned,
        )
    elif include_legacy_unowned:
        raise ValueError("legacy noise compatibility requires a scoped writer")
    row = await _get_noise_marker_for_update(
        session,
        marker_id,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    )
    if not row:
        return False
    await session.delete(row)
    await session.flush()
    if context is not None:
        assert identity is not None and prepared_conflict_write is not None
        await refresh_noise_alert(
            session,
            on_date=context.evaluation_date,
            identity=identity,
            prepared_conflict_write=prepared_conflict_write,
        )
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
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite | None = None,
) -> Optional[BodyMeasurement]:
    """Edit an existing body measurement. If the date has changed, delete the old row
    and upsert the new one.

    ``partial`` carries the same meaning as in ``upsert_body_measurement``: the
    default keeps omitted fields (MCP), ``False`` lets the caller blank them
    (the HTML form)."""
    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    if context is not None:
        _require_evaluation_date(context, on_date)
        _require_legacy_bridge(
            context,
            include_legacy_unowned=include_legacy_unowned,
        )
    elif include_legacy_unowned:
        raise ValueError(
            "legacy body-measurement compatibility requires a scoped writer"
        )
    _check_range("neck_cm", neck_cm, _CIRCUMFERENCE_CM_RANGE)
    _check_range("waist_cm", waist_cm, _CIRCUMFERENCE_CM_RANGE)
    _check_range("hips_cm", hips_cm, _CIRCUMFERENCE_CM_RANGE)

    row = await _get_body_measurement_for_update(
        session,
        measurement_id,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    )
    if not row:
        return None

    if row.date != on_date:
        occupied = await _get_body_measurement_for_date_update(
            session,
            on_date,
            subject_id=identity.subject_id if identity is not None else None,
            include_legacy_unowned=include_legacy_unowned,
        )
        if occupied is not None and occupied.id != row.id:
            raise BodyMeasurementScopedUniqueCutoverRequiredError(
                "body-measurement destination date already has a row"
            )

    effective_neck, effective_waist, effective_hips, effective_note = (
        _effective_measurement_values(
            row,
            neck_cm=neck_cm,
            waist_cm=waist_cm,
            hips_cm=hips_cm,
            note=note,
            partial=partial,
        )
    )
    await _enforce_body_measurement_write(
        session,
        context=context,
        prepared_conflict_write=prepared_conflict_write,
        on_date=on_date,
        override=override,
    )
    if row.subject_id is None and identity is not None:
        row.subject_id = identity.subject_id
    await _apply_body_measurement_values(
        session,
        on_date=on_date,
        row=row,
        neck_cm=effective_neck,
        waist_cm=effective_waist,
        hips_cm=effective_hips,
        note=effective_note,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    )
    await session.flush()
    return row


async def update_body_measurement_note(
    session: AsyncSession,
    measurement_id: int,
    *,
    note: str,
    identity: WriteIdentity,
    include_legacy_unowned: bool = False,
    prepared_conflict_write: conflict_engine.PreparedConflictWrite,
) -> BodyMeasurement | None:
    """Update only a measurement note inside one prepared subject scope."""

    context = _require_aux_prepared_write(
        session,
        identity=identity,
        prepared=prepared_conflict_write,
    )
    assert context is not None
    _require_legacy_bridge(
        context,
        include_legacy_unowned=include_legacy_unowned,
    )
    row = await _get_body_measurement_for_update(
        session,
        measurement_id,
        subject_id=identity.subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    if row is None:
        return None
    if row.subject_id is None:
        row.subject_id = identity.subject_id
    row.note = note
    await session.flush()
    return row
