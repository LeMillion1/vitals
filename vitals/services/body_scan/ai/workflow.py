"""Transactional body-scan AI preparation, dispatch, and persistence."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    FileStorageBackend,
    Source,
)
from vitals.integrations.llm_client import LLMCallResult, LLMClient
from vitals.models.ai import AIInvocation
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services.files import lifecycle as file_lifecycle
from vitals.services.ai_gateway import contracts as ai_gateway_service_contracts
from vitals.services.ai_gateway import dispatch as ai_gateway_service_dispatch
from vitals.services.ai_gateway import invocations as ai_gateway_service_invocations
from vitals.services.body_scan.scans import normalization as scan_normalization

from .contracts import (
    BodyScanAIInvocationStateError,
    BodyScanAIOwnershipError,
    BodyScanAIValidationError,
    BodyScanParseResult,
    PreparedBodyScanContent,
    PreparedBodyScanParse,
    _BODY_SCAN_MAX_TOKENS,
    _BODY_SCAN_RESERVATION_OVERHEAD_UNITS,
    _BODY_SCAN_RESERVED_COST_MICROUNITS,
    _PLACEHOLDER,
    _asset_fingerprint,
    _clean_media_type,
    _clean_model,
    _clean_sha256,
    _clean_size,
    _clean_storage_ref,
    _idempotency_key,
    _raw_fingerprint,
    _require_content,
    _require_prepared,
    _validated_extraction,
)
from .projection import _resolve_openrouter_credential
from .scope import _lock_owner, _lock_prepared_scope, _validate_existing_roots

async def prepare_body_scan_parse(
    session: AsyncSession,
    *,
    actor_username: str,
    storage_ref: str,
    media_type: str,
    byte_size: int,
    sha256_hex: str,
    storage_backend: FileStorageBackend | str = FileStorageBackend.LEGACY_LOCAL,
) -> PreparedBodyScanParse:
    """Create exact file/raw roots and reserve one paid parser invocation.

    The default preserves retries of legacy in-flight uploads. New HTTP uploads
    explicitly select the private backend.
    """

    cleaned_ref = _clean_storage_ref(storage_ref)
    cleaned_media = _clean_media_type(media_type)
    cleaned_size = _clean_size(byte_size)
    cleaned_sha = _clean_sha256(sha256_hex)
    model = _clean_model()
    _subject, _owner, identity = await _lock_owner(
        session,
        actor_username=actor_username,
    )
    try:
        normalized_backend = FileStorageBackend(storage_backend)
    except (TypeError, ValueError) as exc:
        raise BodyScanAIValidationError(
            "body-scan document storage backend is invalid"
        ) from exc
    if normalized_backend not in {
        FileStorageBackend.LEGACY_LOCAL,
        FileStorageBackend.PRIVATE_LOCAL,
    }:
        raise BodyScanAIValidationError(
            "body-scan document storage backend is invalid"
        )
    register = (
        file_lifecycle.register_private_local
        if normalized_backend is FileStorageBackend.PRIVATE_LOCAL
        else file_lifecycle.register_legacy_local
    )
    asset = await register(
        session,
        subject_id=identity.subject_id,
        uploaded_by_user_id=identity.actor_user_id,
        purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT,
        storage_ref=cleaned_ref,
        media_type=cleaned_media,
        size_bytes=cleaned_size,
        content_sha256=cleaned_sha,
    )
    raw_rows = list(
        await session.scalars(
            select(RawPayload)
            .where(
                or_(
                    RawPayload.file_asset_id == asset.id,
                    (
                        (RawPayload.domain == Domain.BODY_COMPOSITION.value)
                        & (RawPayload.source == Source.BODY_SCAN.value)
                        & (RawPayload.external_id == cleaned_ref)
                    ),
                )
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(raw_rows) > 1:
        raise BodyScanAIOwnershipError("body-scan upload raw provenance is ambiguous")
    if raw_rows:
        raw = raw_rows[0]
    else:
        raw = RawPayload(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            integration_connection_id=None,
            file_asset_id=asset.id,
            domain=Domain.BODY_COMPOSITION.value,
            source=Source.BODY_SCAN.value,
            external_id=cleaned_ref,
            payload=_PLACEHOLDER,
            processed_at=None,
        )
        session.add(raw)
        await session.flush()
    _validate_existing_roots(
        asset=asset,
        raw=raw,
        identity=identity,
        storage_ref=cleaned_ref,
        media_type=cleaned_media,
        byte_size=cleaned_size,
        sha256_hex=cleaned_sha,
        storage_backend=normalized_backend,
    )
    invocations = list(
        await session.scalars(
            select(AIInvocation)
            .where(
                AIInvocation.subject_id == identity.subject_id,
                AIInvocation.raw_payload_id == raw.id,
                AIInvocation.purpose == AIInvocationPurpose.BODY_SCAN_PARSE.value,
            )
            .order_by(AIInvocation.created_at, AIInvocation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(invocations) > 1:
        raise BodyScanAIInvocationStateError(
            "body-scan document has multiple parser invocations"
        )
    existing = invocations[0] if invocations else None
    if existing is not None and (
        existing.actor_user_id != identity.actor_user_id
        or existing.source != AIInvocationSource.WEB.value
        or existing.idempotency_key != _idempotency_key(raw.id)
    ):
        raise BodyScanAIInvocationStateError(
            "body-scan parser invocation provenance is inconsistent"
        )
    reserved_units = (
        cleaned_size * 4 + _BODY_SCAN_MAX_TOKENS + _BODY_SCAN_RESERVATION_OVERHEAD_UNITS
    )
    if existing is None or existing.status == AIInvocationStatus.PREPARED.value:
        reservation = await ai_gateway_service_invocations.reserve_ai_invocation(
            session,
            identity=identity,
            purpose=AIInvocationPurpose.BODY_SCAN_PARSE,
            source=AIInvocationSource.WEB,
            model=model,
            idempotency_key=_idempotency_key(raw.id),
            reserved_cost_microunits=_BODY_SCAN_RESERVED_COST_MICROUNITS,
            reserved_units=reserved_units,
            raw_payload_id=raw.id,
        )
        invocation_id = reservation.invocation_id
        reservation_status = reservation.status
        dispatchable = reservation.dispatchable
    else:
        invocation_id = existing.id
        reservation_status = AIInvocationStatus(existing.status)
        dispatchable = False
    existing_extracted: dict[str, Any] | None = None
    if reservation_status is AIInvocationStatus.SUCCEEDED:
        existing_extracted = _validated_extraction(raw.payload)
    elif raw.payload != _PLACEHOLDER:
        raise BodyScanAIInvocationStateError(
            "unfinished body-scan invocation has a non-placeholder raw payload"
        )
    return PreparedBodyScanParse._issue(
        _subject_id=identity.subject_id,
        _owner_user_id=identity.actor_user_id,
        _actor_user_id=identity.actor_user_id,
        _file_asset_id=asset.id,
        _raw_payload_id=raw.id,
        _storage_ref=cleaned_ref,
        _media_type=cleaned_media,
        _byte_size=cleaned_size,
        _sha256_hex=cleaned_sha,
        _model=(existing.model if existing is not None else model),
        _invocation_id=invocation_id,
        _reservation_status=reservation_status,
        _dispatchable=dispatchable,
        _asset_fingerprint=_asset_fingerprint(asset),
        _raw_fingerprint=_raw_fingerprint(raw),
        _existing_extracted=existing_extracted,
    )


def prepare_body_scan_content(
    prepared: PreparedBodyScanParse,
    *,
    file_bytes: bytes,
) -> PreparedBodyScanContent:
    """Validate and locally convert bytes before any paid dispatch starts."""

    snapshot = _require_prepared(prepared)
    if not isinstance(file_bytes, bytes) or len(file_bytes) != snapshot._byte_size:
        raise BodyScanAIValidationError("body-scan document bytes changed")
    if hashlib.sha256(file_bytes).hexdigest() != snapshot._sha256_hex:
        raise BodyScanAIValidationError("body-scan document hash changed")
    is_pdf = snapshot._media_type == "application/pdf" or snapshot._storage_ref.endswith(
        ".pdf"
    )
    try:
        image_urls = scan_normalization.prepare_file_for_extraction(
            file_bytes,
            content_type=snapshot._media_type,
            filename=snapshot._storage_ref,
        )
    except Exception as exc:
        raise BodyScanAIValidationError(
            "body-scan document local preprocessing failed"
        ) from exc
    if not image_urls:
        raise BodyScanAIValidationError("body-scan document contains no readable pages")
    return PreparedBodyScanContent._issue(
        prepared_fingerprint=snapshot._fingerprint,
        image_urls=image_urls,
        is_pdf=is_pdf,
    )


async def start_body_scan_dispatch(
    session: AsyncSession,
    prepared: PreparedBodyScanParse,
    *,
    content: PreparedBodyScanContent | None = None,
    credential_resolver: Callable[[str], str | None] | None = None,
) -> ai_gateway_service_contracts.AIDispatchLease:
    """Freshly authorize and charge one document parse; caller commits."""

    snapshot = _require_prepared(prepared)
    if (
        not snapshot._dispatchable
        or snapshot._reservation_status is not AIInvocationStatus.PREPARED
    ):
        raise BodyScanAIInvocationStateError("body-scan parse is not dispatchable")
    _require_content(snapshot, content)
    await _lock_prepared_scope(session, snapshot, require_active_owner=True)
    return await ai_gateway_service_dispatch.start_ai_dispatch(
        session,
        identity=WriteIdentity(snapshot._subject_id, snapshot._actor_user_id),
        invocation_id=snapshot._invocation_id,
        credential_resolver=credential_resolver or _resolve_openrouter_credential,
    )


async def cancel_prepared_body_scan_parse(
    session: AsyncSession,
    prepared: PreparedBodyScanParse,
) -> AIInvocation:
    """Release a zero-network reservation after a failed start boundary."""

    snapshot = _require_prepared(prepared)
    if snapshot._reservation_status is not AIInvocationStatus.PREPARED:
        raise BodyScanAIInvocationStateError(
            "only a prepared body-scan invocation can be cancelled"
        )
    await _lock_prepared_scope(session, snapshot, require_active_owner=True)
    return await ai_gateway_service_dispatch.cancel_reserved_ai_invocation(
        session,
        identity=WriteIdentity(snapshot._subject_id, snapshot._actor_user_id),
        invocation_id=snapshot._invocation_id,
    )


async def render_body_scan(
    prepared: PreparedBodyScanParse,
    lease: ai_gateway_service_contracts.AIDispatchLease,
    *,
    file_bytes: bytes,
    content: PreparedBodyScanContent | None = None,
    llm_factory=None,
) -> ai_gateway_service_contracts.AICompletion[LLMCallResult[dict]]:
    """Perform exactly one bounded vision extraction with no database access."""

    snapshot = _require_prepared(prepared)
    if not isinstance(file_bytes, bytes) or len(file_bytes) != snapshot._byte_size:
        raise BodyScanAIValidationError("body-scan document bytes changed")
    if hashlib.sha256(file_bytes).hexdigest() != snapshot._sha256_hex:
        raise BodyScanAIValidationError("body-scan document hash changed")
    prepared_content = _require_content(snapshot, content)
    factory = llm_factory or LLMClient
    if not callable(factory):
        raise TypeError("llm_factory must be callable")

    async def provider_call(
        request: ai_gateway_service_contracts.AIDispatchRequest,
    ) -> LLMCallResult[dict]:
        if (
            request.invocation_id != snapshot._invocation_id
            or request.raw_payload_id != snapshot._raw_payload_id
            or request.model != snapshot._model
        ):
            raise BodyScanAIInvocationStateError(
                "body-scan dispatch provenance changed"
            )
        config = replace(load_config(), openrouter_api_key=request.credential)
        client = factory(config)
        return await scan_normalization.extract_prepared_file_with_usage(
            prepared_content._image_urls,
            llm=client,
            model=request.model,
            max_tokens=_BODY_SCAN_MAX_TOKENS,
        )

    def usage_extractor(
        result: LLMCallResult[dict],
    ) -> ai_gateway_service_contracts.SanitizedAIUsage:
        if not isinstance(result, LLMCallResult):
            raise BodyScanAIValidationError("body-scan provider result is invalid")
        _validated_extraction(result.value)
        if (
            result.input_tokens is None
            or result.output_tokens is None
            or result.cost_microunits is None
        ):
            raise BodyScanAIValidationError("body-scan provider usage is incomplete")
        return ai_gateway_service_contracts.SanitizedAIUsage(
            upstream_request_id=result.upstream_request_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_microunits=result.cost_microunits,
        )

    return await ai_gateway_service_dispatch.dispatch_ai(
        lease,
        provider_call=provider_call,
        usage_extractor=usage_extractor,
    )


async def persist_body_scan_parse(
    session: AsyncSession,
    prepared: PreparedBodyScanParse,
    completion: ai_gateway_service_contracts.AICompletion[LLMCallResult[dict]],
) -> BodyScanParseResult:
    """Atomically finalize accounting and persist one validated extraction."""

    snapshot = _require_prepared(prepared)
    if completion.invocation_id != snapshot._invocation_id:
        raise BodyScanAIInvocationStateError(
            "body-scan completion belongs to another invocation"
        )
    # The call is already paid. Preserve its exact historical S/A/F/raw graph
    # and finalize accounting even if the actor was suspended or ownership was
    # administratively rotated after dispatch; T2 was the authorization point.
    locked = await _lock_prepared_scope(
        session,
        snapshot,
        require_active_owner=False,
    )
    invocation = await ai_gateway_service_dispatch.finalize_ai_invocation(
        session,
        completion=completion,
    )
    if (
        invocation.subject_id != snapshot._subject_id
        or invocation.actor_user_id != snapshot._actor_user_id
        or invocation.raw_payload_id != snapshot._raw_payload_id
        or invocation.purpose != AIInvocationPurpose.BODY_SCAN_PARSE.value
        or invocation.source != AIInvocationSource.WEB.value
        or invocation.model != snapshot._model
    ):
        raise BodyScanAIInvocationStateError(
            "body-scan invocation provenance changed"
        )
    status = AIInvocationStatus(invocation.status)
    extracted: dict[str, Any] | None = None
    if status is AIInvocationStatus.SUCCEEDED:
        payload = completion.payload
        if not isinstance(payload, LLMCallResult):
            raise BodyScanAIInvocationStateError(
                "successful body-scan completion payload is missing"
            )
        extracted = _validated_extraction(payload.value)
        locked.raw.payload = extracted
        await session.flush()
    return BodyScanParseResult(
        raw_payload_id=locked.raw.id,
        file_asset_id=locked.asset.id,
        invocation_id=invocation.id,
        status=status,
        extracted=extracted,
    )
