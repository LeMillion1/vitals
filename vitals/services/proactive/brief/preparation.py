"""Daily Brief product-key validation, availability, and quota reservation."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import date as date_type, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    DigestKind,
    IntegrationConnectionStatus,
)
from vitals.models.ai import AIInvocation, AIPlatformQuotaPeriod, AISubjectQuotaPeriod
from vitals.models.milestones import WeeklyDigest
from vitals.models.tenancy import PlatformIntegrationConnection
from vitals.services.ai_gateway import contracts as ai_gateway_service_contracts
from vitals.services.ai_gateway import dispatch as ai_gateway_service_dispatch
from vitals.services.ai_gateway import invocations as ai_gateway_service_invocations
from vitals.services.digest import ownership as digest_ownership
from vitals.services.proactive import compose
from vitals.utils.timeutils import now_utc, today_local

from .context import build_context
from .contracts import (
    BRIEF_SYSTEM,
    BriefAIAvailability,
    BriefAIFallback,
    BriefInvocationStateError,
    BriefOwnershipError,
    BriefSurface,
    PreparedBrief,
    _ARTIFACT_SOURCE_BY_INVOCATION_SOURCE,
    _BRIEF_CONTEXT_PROVENANCE_KEY,
    _BRIEF_IDEMPOTENCY_NAMESPACE,
    _BRIEF_MAX_INPUT_BYTES,
    _BRIEF_MAX_TOKENS,
    _BRIEF_POLICY_VERSION,
    _BRIEF_RESERVATION_OVERHEAD_UNITS,
    _BRIEF_RESERVED_COST_MICROUNITS,
    _BRIEF_TOKEN_RE,
    _PREPARED_BRIEF_SEAL,
)
from .prompt import _render_base_content, build_prompt

logger = logging.getLogger(__name__)

def _as_invocation_source(value: AIInvocationSource | str) -> AIInvocationSource:
    try:
        source = AIInvocationSource(value)
    except (TypeError, ValueError) as exc:
        raise BriefOwnershipError("unsupported Daily Brief invocation source") from exc
    if source not in _ARTIFACT_SOURCE_BY_INVOCATION_SOURCE:
        raise BriefOwnershipError("surface cannot generate a Daily Brief")
    return source


def _as_surface(value: BriefSurface | str) -> BriefSurface:
    try:
        return BriefSurface(value)
    except (TypeError, ValueError) as exc:
        raise BriefOwnershipError("unsupported Daily Brief surface") from exc


def _request_key(
    *,
    source: AIInvocationSource,
    surface: BriefSurface,
    on_date: date_type,
    request_token: str | None,
) -> str:
    if source is AIInvocationSource.SCHEDULER:
        if surface is not BriefSurface.SCHEDULER or request_token is not None:
            raise BriefOwnershipError("scheduled briefs use the deterministic product key")
        token_part = "scheduled"
    else:
        if surface not in {BriefSurface.BUILD, BriefSurface.TEST}:
            raise BriefOwnershipError("web briefs require a manual surface")
        token_part = validate_request_token(request_token)
    material = "|".join(
        (
            _BRIEF_IDEMPOTENCY_NAMESPACE,
            surface.value,
            on_date.isoformat(),
            token_part,
        )
    )
    return f"dbp:v1:{hashlib.sha256(material.encode()).hexdigest()}"


def validate_request_token(request_token: str | None) -> str:
    """Return one bounded opaque web token before any hash/query use."""

    if not isinstance(request_token, str) or not _BRIEF_TOKEN_RE.fullmatch(
        request_token
    ):
        raise BriefOwnershipError("Daily Brief request token is invalid")
    return request_token


def _require_prepared_brief(prepared: PreparedBrief) -> PreparedBrief:
    if not isinstance(prepared, PreparedBrief) or prepared._seal is not _PREPARED_BRIEF_SEAL:
        raise BriefOwnershipError("prepared Daily Brief capability is invalid")
    expected = (
        prepared._actor_username,
        prepared._subject_id,
        prepared._actor_user_id,
        prepared._artifact_source,
        prepared._invocation_source,
        prepared._surface,
        prepared._on_date,
        prepared._model,
        prepared._request_key,
        prepared._owner_user_id,
        prepared._policy_version,
        prepared._invocation_id,
        prepared._reservation_status,
        prepared._dispatchable,
        prepared._existing_artifact_id,
        prepared._fallback,
        hashlib.sha256(prepared._context_json_text.encode()).digest(),
        hashlib.sha256(prepared._prompt.encode()).digest(),
        hashlib.sha256(prepared._base_content.encode()).digest(),
    )
    if prepared._fingerprint != expected:
        raise BriefOwnershipError("prepared Daily Brief capability was modified")
    return prepared


def _resolve_openrouter_credential(credential_ref: str) -> str | None:
    if credential_ref not in ai_gateway_service_contracts.ALLOWED_CREDENTIAL_REFS:
        return None
    credential = load_config().openrouter_api_key.strip()
    return credential or None


async def project_ai_availability(
    session: AsyncSession,
    *,
    actor_username: str,
) -> BriefAIAvailability:
    """Project redacted current owner capacity without exposing limits or PHI."""

    owner = await digest_ownership.prepare_digest_owner(
        session,
        actor_username=actor_username,
    )
    billing_date = now_utc().date()
    roots = list(
        await session.scalars(
            select(PlatformIntegrationConnection)
            .where(
                PlatformIntegrationConnection.status
                == IntegrationConnectionStatus.ACTIVE.value
            )
            .limit(2)
        )
    )
    if len(roots) != 1 or _resolve_openrouter_credential(roots[0].credential_ref) is None:
        return BriefAIAvailability(False, BriefAIFallback.NOT_CONFIGURED)
    platform_periods = list(
        await session.scalars(
            select(AIPlatformQuotaPeriod).where(
                AIPlatformQuotaPeriod.period_start <= billing_date,
                AIPlatformQuotaPeriod.period_end > billing_date,
            )
        )
    )
    subject_periods = list(
        await session.scalars(
            select(AISubjectQuotaPeriod).where(
                AISubjectQuotaPeriod.subject_id == owner.identity.subject_id,
                AISubjectQuotaPeriod.period_start <= billing_date,
                AISubjectQuotaPeriod.period_end > billing_date,
            )
        )
    )
    if (
        len(platform_periods) != 1
        or len(subject_periods) != 1
        or subject_periods[0].period_start != platform_periods[0].period_start
        or subject_periods[0].period_end != platform_periods[0].period_end
    ):
        return BriefAIAvailability(False, BriefAIFallback.NOT_CONFIGURED)
    # This projection cannot know the next PHI-bearing prompt size. It reports
    # root/credential/aligned-period readiness only; reserve is authoritative for
    # the actual conservative per-request capacity check.
    return BriefAIAvailability(True, BriefAIFallback.NONE)



async def _existing_unfunded_artifact(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    artifact_source: str,
    on_date: date_type,
    request_key: str,
) -> WeeklyDigest | None:
    rows = list(
        await session.scalars(
            select(WeeklyDigest)
            .where(
                WeeklyDigest.subject_id == subject_id,
                WeeklyDigest.actor_user_id.is_(actor_user_id)
                if actor_user_id is None
                else WeeklyDigest.actor_user_id == actor_user_id,
                WeeklyDigest.integration_connection_id.is_(None),
                WeeklyDigest.ai_invocation_id.is_(None),
                WeeklyDigest.date == on_date,
                WeeklyDigest.kind == DigestKind.DAILY_BRIEF.value,
                WeeklyDigest.source == artifact_source,
            )
            .order_by(WeeklyDigest.id)
        )
    )
    matches = [
        row
        for row in rows
        if isinstance(row.context_json, dict)
        and isinstance(row.context_json.get(_BRIEF_CONTEXT_PROVENANCE_KEY), dict)
        and row.context_json[_BRIEF_CONTEXT_PROVENANCE_KEY].get("request_key")
        == request_key
    ]
    if len(matches) > 1:
        raise BriefInvocationStateError("Daily Brief fallback is duplicated")
    return matches[0] if matches else None


async def prepare_brief(
    session: AsyncSession,
    *,
    actor_username: str | None,
    invocation_source: AIInvocationSource | str,
    surface: BriefSurface | str,
    request_token: str | None = None,
    on_date: date_type | None = None,
) -> PreparedBrief | None:
    """Freeze exact-S PHI and reserve one platform-funded narrative call."""

    source = _as_invocation_source(invocation_source)
    product_surface = _as_surface(surface)
    if source is AIInvocationSource.SCHEDULER:
        if actor_username is not None:
            raise BriefOwnershipError("scheduled Daily Brief must be actorless")
    elif actor_username is None:
        raise BriefOwnershipError("web Daily Brief requires its human actor")
    owner = await digest_ownership.prepare_digest_owner(
        session,
        actor_username=actor_username,
    )
    identity = owner.identity
    owner_user_id = owner.owner_user_id
    artifact_source = _ARTIFACT_SOURCE_BY_INVOCATION_SOURCE[source]
    frozen_date = on_date or today_local()
    config = load_config()
    model = (config.llm_model_brief or config.llm_model_digest).strip()
    if not model or len(model) > 128:
        raise BriefOwnershipError("Daily Brief model is invalid")
    product_key = _request_key(
        source=source,
        surface=product_surface,
        on_date=frozen_date,
        request_token=request_token,
    )
    policy_version = _BRIEF_POLICY_VERSION
    ctx = await build_context(
        session,
        on_date=frozen_date,
        subject_id=identity.subject_id,
    )
    if compose.is_empty_day(ctx, on_date=frozen_date):
        logger.info("Daily Brief skipped: empty day")
        return None
    if compose.night_pending(ctx, on_date=frozen_date):
        logger.info("Daily Brief recovery omitted: night is not scored")
        ctx = compose.drop_unscored_night(ctx)
    prompt = build_prompt(ctx)
    prompt_units = len((BRIEF_SYSTEM + "\n" + prompt).encode())
    reserved_units = (
        prompt_units + _BRIEF_MAX_TOKENS + _BRIEF_RESERVATION_OVERHEAD_UNITS
    )
    context_text = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
    base_content = _render_base_content(ctx)

    unfunded = await _existing_unfunded_artifact(
        session,
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        artifact_source=artifact_source,
        on_date=frozen_date,
        request_key=product_key,
    )
    if unfunded is not None:
        return PreparedBrief._issue(
            _actor_username=actor_username,
            _subject_id=identity.subject_id,
            _actor_user_id=identity.actor_user_id,
            _artifact_source=artifact_source,
            _invocation_source=source,
            _surface=product_surface,
            _on_date=frozen_date,
            _model=model,
            _request_key=product_key,
            _owner_user_id=owner_user_id,
            _policy_version=policy_version,
            _invocation_id=None,
            _reservation_status=None,
            _dispatchable=False,
            _existing_artifact_id=unfunded.id,
            _fallback=BriefAIFallback.NOT_CONFIGURED,
            _context_json_text=context_text,
            _prompt=prompt,
            _base_content=base_content,
        )

    invocation = await session.scalar(
        select(AIInvocation)
        .where(
            AIInvocation.subject_id == identity.subject_id,
            AIInvocation.purpose == AIInvocationPurpose.DAILY_BRIEF.value,
            AIInvocation.idempotency_key == product_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invocation is not None:
        if (
            invocation.actor_user_id != identity.actor_user_id
            or invocation.source != source.value
        ):
            raise BriefInvocationStateError(
                "Daily Brief request provenance is inconsistent"
            )
        status = AIInvocationStatus(invocation.status)
        frozen_model = invocation.model
        artifact_id = await session.scalar(
            select(WeeklyDigest.id).where(
                WeeklyDigest.subject_id == identity.subject_id,
                WeeklyDigest.ai_invocation_id == invocation.id,
            )
        )
        if artifact_id is not None:
            return PreparedBrief._issue(
                _actor_username=actor_username,
                _subject_id=identity.subject_id,
                _actor_user_id=identity.actor_user_id,
                _artifact_source=artifact_source,
                _invocation_source=source,
                _surface=product_surface,
                _on_date=frozen_date,
                _model=frozen_model,
                _request_key=product_key,
                _owner_user_id=owner_user_id,
                _policy_version=policy_version,
                _invocation_id=invocation.id,
                _reservation_status=status,
                _dispatchable=False,
                _existing_artifact_id=artifact_id,
                _fallback=BriefAIFallback.NONE,
                _context_json_text=context_text,
                _prompt=prompt,
                _base_content=base_content,
            )
        if status is AIInvocationStatus.SUCCEEDED:
            raise BriefInvocationStateError(
                "succeeded Daily Brief invocation is missing its artifact"
            )
        if status is AIInvocationStatus.PREPARED:
            created_at = invocation.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            stale = (
                created_at < now_utc() - ai_gateway_service_contracts.PREPARED_STALE_AFTER
                or model != frozen_model
            )
            if not stale:
                try:
                    reservation = await ai_gateway_service_invocations.reserve_ai_invocation(
                        session,
                        identity=identity,
                        purpose=AIInvocationPurpose.DAILY_BRIEF,
                        source=source,
                        model=frozen_model,
                        idempotency_key=product_key,
                        reserved_cost_microunits=_BRIEF_RESERVED_COST_MICROUNITS,
                        reserved_units=reserved_units,
                    )
                except (
                    ai_gateway_service_contracts.AIGatewayConfigurationError,
                    ai_gateway_service_contracts.AIIdempotencyConflictError,
                    ai_gateway_service_contracts.AIQuotaExceededError,
                ):
                    stale = True
                else:
                    status = reservation.status
            if stale:
                invocation = await ai_gateway_service_dispatch.cancel_reserved_ai_invocation(
                    session,
                    identity=identity,
                    invocation_id=invocation.id,
                    error_code=AIInvocationErrorCode.CANCELLED_BY_POLICY,
                )
                status = AIInvocationStatus(invocation.status)
        return PreparedBrief._issue(
            _actor_username=actor_username,
            _subject_id=identity.subject_id,
            _actor_user_id=identity.actor_user_id,
            _artifact_source=artifact_source,
            _invocation_source=source,
            _surface=product_surface,
            _on_date=frozen_date,
            _model=frozen_model,
            _request_key=product_key,
            _owner_user_id=owner_user_id,
            _policy_version=policy_version,
            _invocation_id=invocation.id,
            _reservation_status=status,
            _dispatchable=status is AIInvocationStatus.PREPARED,
            _existing_artifact_id=None,
            _fallback=BriefAIFallback.NONE,
            _context_json_text=context_text,
            _prompt=prompt,
            _base_content=base_content,
        )

    fallback = BriefAIFallback.NONE
    if prompt_units > _BRIEF_MAX_INPUT_BYTES:
        fallback = BriefAIFallback.INPUT_TOO_LARGE
        reservation = None
    else:
        try:
            reservation = await ai_gateway_service_invocations.reserve_ai_invocation(
                session,
                identity=identity,
                purpose=AIInvocationPurpose.DAILY_BRIEF,
                source=source,
                model=model,
                idempotency_key=product_key,
                reserved_cost_microunits=_BRIEF_RESERVED_COST_MICROUNITS,
                reserved_units=reserved_units,
            )
        except ai_gateway_service_contracts.AIQuotaExceededError:
            fallback = BriefAIFallback.QUOTA
            reservation = None
        except ai_gateway_service_contracts.AIGatewayConfigurationError:
            fallback = BriefAIFallback.NOT_CONFIGURED
            reservation = None
    return PreparedBrief._issue(
        _actor_username=actor_username,
        _subject_id=identity.subject_id,
        _actor_user_id=identity.actor_user_id,
        _artifact_source=artifact_source,
        _invocation_source=source,
        _surface=product_surface,
        _on_date=frozen_date,
        _model=model,
        _request_key=product_key,
        _owner_user_id=owner_user_id,
        _policy_version=policy_version,
        _invocation_id=reservation.invocation_id if reservation is not None else None,
        _reservation_status=reservation.status if reservation is not None else None,
        _dispatchable=reservation.dispatchable if reservation is not None else False,
        _existing_artifact_id=None,
        _fallback=fallback,
        _context_json_text=context_text,
        _prompt=prompt,
        _base_content=base_content,
    )
