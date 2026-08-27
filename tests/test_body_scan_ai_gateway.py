"""Platform-funded, raw-first Body Scan document parser contracts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import pickle
import re
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from io import BytesIO

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

from vitals.config import load_config as load_runtime_config
from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    FileStorageBackend,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.integrations.llm_client import LLMCallResult
from vitals.i18n import STRINGS
from vitals.models.ai import AIInvocation, AIPlatformQuotaPeriod, AISubjectQuotaPeriod
from vitals.models.body_scan import BodyScan
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import (
    FileAsset,
    IntegrationConnection,
    PlatformIntegrationConnection,
)
from vitals.models.weight import WeightLog
from vitals.ownership import WriteIdentity
from vitals.services import ai_gateway_service as gateway
from vitals.services import file_asset_service, weight_service
from vitals.services.conflicts import engine
from vitals.services.body_scan import ai as body_ai
from vitals.services.body_scan import scans
from vitals.services.legacy_ownership import LegacySubjectResolutionError
from web.config import get_web_config


NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
DAY = date(2026, 8, 20)
PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 9, 1)
MODEL = "synthetic/body-vision-model"
SECRET = "synthetic-platform-secret"
FILE_BYTES = b"\x89PNG\r\n\x1a\nsynthetic-private-body-image"
SHA256 = hashlib.sha256(FILE_BYTES).hexdigest()
EXTRACTED = {
    "date": DAY.isoformat(),
    "device": "Synthetic Body Scanner",
    "metrics": [
        {
            "label": "Weight",
            "value": 78.3,
            "unit": "kg",
        },
        {
            "label": "Body Fat",
            "value": 18.7,
            "unit": "%",
        },
    ],
}


@pytest.fixture(autouse=True)
def _runtime(monkeypatch):
    monkeypatch.setattr(gateway, "now_utc", lambda: NOW)
    if hasattr(body_ai, "now_utc"):
        monkeypatch.setattr(body_ai, "now_utc", lambda: NOW)
    config = replace(
        load_runtime_config(),
        openrouter_api_key=SECRET,
        llm_model_parser=MODEL,
    )
    monkeypatch.setattr(body_ai, "load_config", lambda: config)


async def _configure_platform(
    session: AsyncSession,
    roots,
    *,
    status: IntegrationConnectionStatus = IntegrationConnectionStatus.ACTIVE,
    platform_quota: bool = True,
    subject_quota: bool = True,
) -> PlatformIntegrationConnection:
    root = PlatformIntegrationConnection(
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=f"body-{uuid.uuid4().hex}",
        credential_ref="env:VITALS_OPENROUTER_API_KEY",
        status=status.value,
        config_version=1,
        configured_by_user_id=roots.user_id,
    )
    session.add(root)
    if platform_quota:
        session.add(
            AIPlatformQuotaPeriod(
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                cost_limit_microunits=100_000_000,
                unit_limit=10_000_000,
                configured_by_user_id=roots.user_id,
            )
        )
    if subject_quota:
        session.add(
            AISubjectQuotaPeriod(
                subject_id=roots.subject_id,
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                cost_limit_microunits=100_000_000,
                unit_limit=10_000_000,
                configured_by_user_id=roots.user_id,
            )
        )
    await session.commit()
    return root


async def _remove_subject_openrouter(session: AsyncSession, roots) -> None:
    rows = list(
        await session.scalars(
            select(IntegrationConnection).where(
                IntegrationConnection.subject_id == roots.subject_id,
                IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
            )
        )
    )
    for row in rows:
        await session.delete(row)
    await session.commit()


def _storage_ref(suffix: str) -> str:
    return f"body/{suffix}-{uuid.uuid4().hex}.png"


async def _prepare(session: AsyncSession, *, suffix: str = "panel"):
    return await body_ai.prepare_body_scan_parse(
        session,
        actor_username=get_web_config().auth_username,
        storage_ref=_storage_ref(suffix),
        media_type="image/png",
        byte_size=len(FILE_BYTES),
        sha256_hex=SHA256,
    )


async def test_legacy_prepare_retry_reuses_exact_existing_roots(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    storage_ref = _storage_ref("legacy-retry")
    first = await body_ai.prepare_body_scan_parse(
        db_session,
        actor_username=get_web_config().auth_username,
        storage_ref=storage_ref,
        media_type="image/png",
        byte_size=len(FILE_BYTES),
        sha256_hex=SHA256,
    )
    await db_session.commit()
    second = await body_ai.prepare_body_scan_parse(
        db_session,
        actor_username=get_web_config().auth_username,
        storage_ref=storage_ref,
        media_type="image/png",
        byte_size=len(FILE_BYTES),
        sha256_hex=SHA256,
    )
    assert second.file_asset_id == first.file_asset_id
    assert second.raw_payload_id == first.raw_payload_id
    asset = await db_session.get(FileAsset, first.file_asset_id)
    assert asset.storage_backend == FileStorageBackend.LEGACY_LOCAL.value
    assert await db_session.scalar(select(func.count()).select_from(FileAsset)) == 1
    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 1


def _observed() -> dict:
    return {
        "calls": 0,
        "no_tx": [],
        "credentials": [],
        "models": [],
        "kwargs": [],
    }


def _llm(session: AsyncSession, observed: dict, *, behavior: str = "success"):
    class FakeLLM:
        def __init__(self, config):
            observed["credentials"].append(config.openrouter_api_key)

        async def extract_json_with_usage(self, _prompt, **kwargs):
            observed["calls"] += 1
            observed["no_tx"].append(not session.in_transaction())
            observed["models"].append(kwargs.get("model"))
            observed["kwargs"].append(kwargs)
            if behavior == "provider_error":
                raise RuntimeError("sensitive provider failure")
            value = EXTRACTED
            if behavior == "invalid":
                value = {"unexpected": "not a body scan extraction"}
            elif behavior == "empty":
                value = {"date": DAY.isoformat(), "device": None, "metrics": []}
            elif behavior == "bad_date":
                value = {**EXTRACTED, "date": "20 August 2026"}
            return LLMCallResult(
                value=value,
                upstream_request_id="body-request-1",
                model=kwargs.get("model") or MODEL,
                input_tokens=120,
                output_tokens=40,
                cost_microunits=None if behavior == "missing_usage" else 75,
            )

    return FakeLLM


async def _start(session: AsyncSession, prepared):
    content = body_ai.prepare_body_scan_content(
        prepared,
        file_bytes=FILE_BYTES,
    )
    lease = await body_ai.start_body_scan_dispatch(
        session,
        prepared,
        content=content,
        credential_resolver=lambda reference: (
            SECRET if reference == "env:VITALS_OPENROUTER_API_KEY" else None
        ),
    )
    await session.commit()
    return lease, content


async def _render(
    session: AsyncSession,
    prepared,
    lease,
    content,
    *,
    behavior="success",
):
    observed = _observed()
    completion = await body_ai.render_body_scan(
        prepared,
        lease,
        file_bytes=FILE_BYTES,
        content=content,
        llm_factory=_llm(session, observed, behavior=behavior),
    )
    return completion, observed


async def _web_upload(
    session: AsyncSession,
    *,
    file_bytes: bytes = FILE_BYTES,
    filename: str = "scan.png",
    content_type: str = "image/png",
):
    from web.routers import weight as weight_router

    return await weight_router.body_scan_upload(
        request=Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/weight/body-scan/upload",
                "headers": [],
            }
        ),
        file=UploadFile(
            BytesIO(file_bytes),
            filename=filename,
            headers=Headers({"content-type": content_type}),
        ),
        date=None,
        db=session,
        username=get_web_config().auth_username,
    )


async def _persist_and_replay_platform_scan(
    session: AsyncSession,
    roots,
    *,
    suffix: str,
) -> tuple[RawPayload, AIInvocation, BodyScan]:
    prepared = await _prepare(session, suffix=suffix)
    raw_id = prepared.raw_payload_id
    invocation_id = prepared.invocation_id
    await session.commit()
    lease, content = await _start(session, prepared)
    completion, _ = await _render(session, prepared, lease, content)
    await body_ai.persist_body_scan_parse(session, prepared, completion)
    await session.commit()
    context = await engine.resolve_legacy_conflict_write_context(
        session,
        actor_username=None,
        evaluation_date=DAY,
    )
    assert await scans.reparse_owned_pending(
        session,
        identity=context.identity,
    ) == 1
    await session.commit()
    raw = await session.get(RawPayload, raw_id)
    invocation = await session.get(AIInvocation, invocation_id)
    scan = await session.scalar(
        select(BodyScan).where(BodyScan.raw_payload_id == raw_id)
    )
    assert raw is not None and invocation is not None and scan is not None
    assert raw.subject_id == roots.subject_id
    return raw, invocation, scan


async def test_full_flow_is_opaque_c_null_usage_aware_and_transaction_free(
    db_session,
    legacy_owner_roots,
):
    root = await _configure_platform(db_session, legacy_owner_roots)
    await _remove_subject_openrouter(db_session, legacy_owner_roots)

    prepared = await _prepare(db_session)
    prepared_repr = repr(prepared)
    assert "synthetic-private" not in prepared_repr
    assert "body/" not in prepared_repr
    with pytest.raises(TypeError):
        pickle.dumps(prepared)

    raw = await db_session.scalar(select(RawPayload))
    asset = await db_session.scalar(select(FileAsset))
    invocation = await db_session.scalar(select(AIInvocation))
    assert raw is not None and asset is not None and invocation is not None
    raw_id = raw.id
    invocation_id = invocation.id
    assert (
        raw.subject_id,
        raw.actor_user_id,
        raw.integration_connection_id,
        raw.file_asset_id,
        raw.domain,
        raw.source,
    ) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        None,
        asset.id,
        Domain.BODY_COMPOSITION.value,
        Source.BODY_SCAN.value,
    )
    assert (asset.subject_id, asset.uploaded_by_user_id, asset.purpose) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        FileAssetPurpose.BODY_SCAN_DOCUMENT.value,
    )
    assert (
        invocation.subject_id,
        invocation.actor_user_id,
        invocation.raw_payload_id,
        invocation.platform_integration_connection_id,
        invocation.purpose,
        invocation.source,
        invocation.model,
        invocation.status,
    ) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        raw.id,
        root.id,
        AIInvocationPurpose.BODY_SCAN_PARSE.value,
        AIInvocationSource.WEB.value,
        MODEL,
        AIInvocationStatus.PREPARED.value,
    )
    placeholder = raw.payload
    await db_session.commit()

    lease, content = await _start(db_session, prepared)
    assert "body/" not in repr(lease)
    with pytest.raises(TypeError):
        pickle.dumps(lease)
    completion, observed = await _render(
        db_session,
        prepared,
        lease,
        content,
    )
    assert "Weight" not in repr(completion)
    assert SECRET not in repr(completion)
    with pytest.raises(TypeError):
        pickle.dumps(completion)
    assert observed["calls"] == 1
    assert observed["no_tx"] == [True]
    assert observed["credentials"] == [SECRET]
    assert observed["models"] == [MODEL]
    assert db_session.in_transaction() is False

    await body_ai.persist_body_scan_parse(db_session, prepared, completion)
    raw = await db_session.get(RawPayload, raw_id)
    invocation = await db_session.get(AIInvocation, invocation_id)
    assert raw is not None and raw.payload == EXTRACTED and raw.payload != placeholder
    assert invocation is not None
    assert (
        invocation.status,
        invocation.upstream_request_id,
        invocation.input_tokens,
        invocation.output_tokens,
        invocation.cost_microunits,
    ) == (
        AIInvocationStatus.SUCCEEDED.value,
        "body-request-1",
        120,
        40,
        75,
    )
    platform_quota = await db_session.get(
        AIPlatformQuotaPeriod, (PERIOD_START, PERIOD_END)
    )
    subject_quota = await db_session.get(
        AISubjectQuotaPeriod,
        (legacy_owner_roots.subject_id, PERIOD_START, PERIOD_END),
    )
    assert platform_quota is not None and subject_quota is not None
    assert invocation.charged_cost_microunits == invocation.reserved_cost_microunits
    assert invocation.charged_units == invocation.reserved_units
    for quota in (platform_quota, subject_quota):
        assert quota.reserved_cost_microunits == 0
        assert quota.reserved_units == 0
        assert quota.charged_cost_microunits == invocation.reserved_cost_microunits
        assert quota.charged_units == invocation.reserved_units
    await db_session.commit()


async def test_image_preprocessing_finishes_once_before_charge(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    prepared = await _prepare(db_session, suffix="single-preprocess")
    conversions = []

    def convert(file_bytes, *, content_type, filename):
        conversions.append((file_bytes, content_type, filename))
        return ("data:image/png;base64,c3ludGhldGlj",)

    monkeypatch.setattr(scans, "prepare_file_for_extraction", convert)
    content = body_ai.prepare_body_scan_content(
        prepared,
        file_bytes=FILE_BYTES,
    )
    assert len(conversions) == 1
    await db_session.commit()
    lease = await body_ai.start_body_scan_dispatch(
        db_session,
        prepared,
        content=content,
        credential_resolver=lambda _reference: SECRET,
    )
    await db_session.commit()
    observed = _observed()
    completion = await body_ai.render_body_scan(
        prepared,
        lease,
        file_bytes=FILE_BYTES,
        content=content,
        llm_factory=_llm(db_session, observed),
    )
    assert len(conversions) == 1
    assert observed["calls"] == 1
    await body_ai.persist_body_scan_parse(db_session, prepared, completion)
    await db_session.commit()


async def test_web_precommit_failure_rolls_back_roots_and_removes_private_bytes(
    db_session,
    legacy_owner_roots,
    platform_ai_ready,
    _private_file_test_root,
    monkeypatch,
):
    del legacy_owner_roots, platform_ai_ready

    async def fail_reservation(*_args, **_kwargs):
        raise RuntimeError("synthetic body reservation failure")

    monkeypatch.setattr(gateway, "reserve_ai_invocation", fail_reservation)
    with pytest.raises(RuntimeError, match="reservation failure"):
        await _web_upload(db_session)

    assert not any(path.is_file() for path in _private_file_test_root.rglob("*"))
    assert await db_session.scalar(select(func.count()).select_from(FileAsset)) == 0
    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 0


async def test_web_quota_precommit_failure_returns_bounded_reason_and_cleans_bytes(
    db_session,
    legacy_owner_roots,
    platform_ai_ready,
    _private_file_test_root,
    monkeypatch,
):
    del legacy_owner_roots, platform_ai_ready

    async def reject_quota(*_args, **_kwargs):
        raise gateway.AIQuotaExceededError("synthetic quota")

    monkeypatch.setattr(gateway, "reserve_ai_invocation", reject_quota)
    response = await _web_upload(db_session)

    assert response.status_code == 200
    assert json.loads(response.body)["reason"] == "quota"
    assert not any(path.is_file() for path in _private_file_test_root.rglob("*"))
    assert await db_session.scalar(select(func.count()).select_from(FileAsset)) == 0
    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 0


async def test_web_partial_file_write_failure_removes_sensitive_bytes(
    db_session,
    _private_file_test_root,
    monkeypatch,
):
    from vitals.persistence import file_storage

    def fail_publish(_parent_fd, _temporary, _destination):
        raise OSError("synthetic partial body write")

    monkeypatch.setattr(file_storage, "_publish_temporary", fail_publish)
    with pytest.raises(OSError, match="partial body write"):
        await _web_upload(db_session)

    assert not any(path.is_file() for path in _private_file_test_root.rglob("*"))
    assert await db_session.scalar(select(func.count()).select_from(FileAsset)) == 0
    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 0


async def test_web_local_pdf_failure_cancels_without_provider_call(
    db_session,
    legacy_owner_roots,
    platform_ai_ready,
    tmp_path,
    monkeypatch,
):
    del legacy_owner_roots, platform_ai_ready
    from web.routers import weight as weight_router

    provider_calls = []

    def fail_conversion(*_args, **_kwargs):
        raise ValueError("synthetic malformed body PDF")

    async def provider_must_not_run(*_args, **_kwargs):
        provider_calls.append("provider")
        raise AssertionError("provider ran after local body PDF failure")

    monkeypatch.setattr(weight_router, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(
        scans,
        "prepare_file_for_extraction",
        fail_conversion,
    )
    monkeypatch.setattr(body_ai, "render_body_scan", provider_must_not_run)
    response = await _web_upload(
        db_session,
        file_bytes=b"%PDF-not-a-valid-pdf",
        filename="broken.pdf",
        content_type="application/pdf",
    )

    assert response.status_code == 200
    assert json.loads(response.body)["ok"] is False
    assert provider_calls == []
    invocation = await db_session.scalar(select(AIInvocation))
    assert invocation is not None
    assert invocation.status == AIInvocationStatus.CANCELLED.value
    assert invocation.charged_cost_microunits == 0
    assert invocation.charged_units == 0


async def test_web_t3_transient_failure_reuses_one_paid_completion(
    db_session,
    legacy_owner_roots,
    platform_ai_ready,
    tmp_path,
    monkeypatch,
):
    del legacy_owner_roots, platform_ai_ready
    from web.routers import weight as weight_router

    provider_calls = 0
    persist_attempts = 0

    async def extraction_probe(image_urls, *, llm, model, max_tokens):
        del image_urls, llm, max_tokens
        nonlocal provider_calls
        provider_calls += 1
        return LLMCallResult(
            value=EXTRACTED,
            upstream_request_id="body-route-t3-retry",
            model=model,
            input_tokens=10,
            output_tokens=5,
            cost_microunits=1,
        )

    real_persist = body_ai.persist_body_scan_parse

    async def transient_persist(session, prepared, completion):
        nonlocal persist_attempts
        persist_attempts += 1
        result = await real_persist(session, prepared, completion)
        if persist_attempts == 1:
            raise RuntimeError("synthetic body T3 failure")
        return result

    monkeypatch.setattr(weight_router, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(
        scans,
        "extract_prepared_file_with_usage",
        extraction_probe,
    )
    monkeypatch.setattr(body_ai, "persist_body_scan_parse", transient_persist)
    response = await _web_upload(db_session)

    assert response.status_code == 200
    assert json.loads(response.body)["ok"] is True
    assert persist_attempts == 2
    assert provider_calls == 1
    invocation = await db_session.scalar(select(AIInvocation))
    assert invocation is not None
    assert invocation.status == AIInvocationStatus.SUCCEEDED.value


@pytest.mark.parametrize(
    ("behavior", "status", "error_code"),
    (
        (
            "provider_error",
            AIInvocationStatus.AMBIGUOUS,
            AIInvocationErrorCode.PROVIDER_UNAVAILABLE,
        ),
        (
            "missing_usage",
            AIInvocationStatus.FAILED,
            AIInvocationErrorCode.INVALID_RESPONSE,
        ),
        (
            "invalid",
            AIInvocationStatus.FAILED,
            AIInvocationErrorCode.INVALID_RESPONSE,
        ),
        (
            "empty",
            AIInvocationStatus.FAILED,
            AIInvocationErrorCode.INVALID_RESPONSE,
        ),
        (
            "bad_date",
            AIInvocationStatus.FAILED,
            AIInvocationErrorCode.INVALID_RESPONSE,
        ),
    ),
)
async def test_terminal_failure_accounts_attempt_without_persisting_extraction(
    db_session,
    legacy_owner_roots,
    behavior,
    status,
    error_code,
):
    await _configure_platform(db_session, legacy_owner_roots)
    prepared = await _prepare(db_session, suffix=behavior)
    raw = await db_session.scalar(select(RawPayload))
    invocation = await db_session.scalar(select(AIInvocation))
    assert raw is not None and invocation is not None
    raw_id = raw.id
    invocation_id = invocation.id
    placeholder = raw.payload
    await db_session.commit()
    lease, content = await _start(db_session, prepared)
    completion, observed = await _render(
        db_session,
        prepared,
        lease,
        content,
        behavior=behavior,
    )
    result = await body_ai.persist_body_scan_parse(
        db_session, prepared, completion
    )
    await db_session.commit()

    raw = await db_session.get(RawPayload, raw_id)
    invocation = await db_session.get(AIInvocation, invocation_id)
    assert raw is not None and raw.payload == placeholder
    assert invocation is not None
    assert (invocation.status, invocation.error_code) == (
        status.value,
        error_code.value,
    )
    assert result.extracted is None
    assert await db_session.scalar(
        select(func.count()).select_from(BodyScan).where(
            BodyScan.raw_payload_id == raw_id
        )
    ) == 0
    assert observed["calls"] == 1
    assert observed["no_tx"] == [True]


async def test_t3_rollback_keeps_completion_retryable_and_raw_atomic(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    prepared = await _prepare(db_session, suffix="rollback")
    raw = await db_session.scalar(select(RawPayload))
    invocation = await db_session.scalar(select(AIInvocation))
    assert raw is not None and invocation is not None
    raw_id = raw.id
    invocation_id = invocation.id
    placeholder = raw.payload
    await db_session.commit()
    lease, content = await _start(db_session, prepared)
    completion, _observations = await _render(
        db_session,
        prepared,
        lease,
        content,
    )

    await body_ai.persist_body_scan_parse(db_session, prepared, completion)
    assert (await db_session.get(RawPayload, raw_id)).payload == EXTRACTED
    await db_session.rollback()
    rolled_back_raw = await db_session.get(RawPayload, raw_id)
    rolled_back_invocation = await db_session.get(AIInvocation, invocation_id)
    assert rolled_back_raw is not None and rolled_back_raw.payload == placeholder
    assert rolled_back_invocation is not None
    assert rolled_back_invocation.status == AIInvocationStatus.DISPATCHING.value
    await db_session.rollback()

    await body_ai.persist_body_scan_parse(db_session, prepared, completion)
    await db_session.commit()
    stored_raw = await db_session.get(RawPayload, raw_id)
    stored_invocation = await db_session.get(AIInvocation, invocation_id)
    assert stored_raw is not None and stored_raw.payload == EXTRACTED
    assert stored_invocation is not None
    assert stored_invocation.status == AIInvocationStatus.SUCCEEDED.value


async def test_prepare_rejects_foreign_actor_before_file_raw_or_reservation(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    # Still refused before a file, a raw row or a reservation exists, and now
    # refused structurally: the owner is named in the resolver's query, so a
    # foreign actor matches no row rather than loading one and being compared
    # against it.
    with pytest.raises(LegacySubjectResolutionError, match="no health record of its own"):
        await body_ai.prepare_body_scan_parse(
            db_session,
            actor_username="foreign-user",
            storage_ref=_storage_ref("foreign-actor"),
            media_type="image/png",
            byte_size=len(FILE_BYTES),
            sha256_hex=SHA256,
        )
    await db_session.rollback()
    assert await db_session.scalar(select(func.count()).select_from(FileAsset)) == 0
    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 0


async def test_start_rejects_inactive_actor_before_credential_resolution(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    prepared = await _prepare(db_session, suffix="inactive")
    content = body_ai.prepare_body_scan_content(
        prepared,
        file_bytes=FILE_BYTES,
    )
    await db_session.commit()
    owner = await db_session.get(User, legacy_owner_roots.user_id)
    assert owner is not None
    owner.status = UserStatus.SUSPENDED.value
    await db_session.commit()
    credential_calls = []

    with pytest.raises(body_ai.BodyScanAIOwnershipError):
        await body_ai.start_body_scan_dispatch(
            db_session,
            prepared,
            content=content,
            credential_resolver=lambda reference: credential_calls.append(reference)
            or SECRET,
        )
    assert credential_calls == []


@pytest.mark.parametrize("tamper", ("raw", "file", "actor"))
async def test_start_rejects_changed_raw_file_or_actor_graph(
    db_session,
    legacy_owner_roots,
    tamper,
):
    await _configure_platform(db_session, legacy_owner_roots)
    prepared = await _prepare(db_session, suffix=f"tamper-{tamper}")
    content = body_ai.prepare_body_scan_content(
        prepared,
        file_bytes=FILE_BYTES,
    )
    raw = await db_session.scalar(select(RawPayload))
    asset = await db_session.scalar(select(FileAsset))
    assert raw is not None and asset is not None
    await db_session.commit()

    if tamper == "raw":
        raw.external_id = _storage_ref("changed")
    else:
        foreign_user = User(
            username=f"foreign-{tamper}",
            normalized_username=f"foreign-{tamper}",
            password_hash="$synthetic-test-hash",
            status=UserStatus.ACTIVE.value,
        )
        db_session.add(foreign_user)
        await db_session.flush()
        if tamper == "actor":
            raw.actor_user_id = foreign_user.id
        else:
            foreign_subject = HealthSubject(
                owner_user_id=foreign_user.id,
                display_name="Foreign Body Subject",
                timezone="UTC",
            )
            db_session.add(foreign_subject)
            await db_session.flush()
            asset.subject_id = foreign_subject.id
    await db_session.commit()

    credential_calls = []
    with pytest.raises(body_ai.BodyScanAIOwnershipError):
        await body_ai.start_body_scan_dispatch(
            db_session,
            prepared,
            content=content,
            credential_resolver=lambda reference: credential_calls.append(reference)
            or SECRET,
        )
    assert credential_calls == []
    await db_session.rollback()
    invocation = await db_session.scalar(select(AIInvocation))
    assert invocation is not None
    assert invocation.status == AIInvocationStatus.PREPARED.value


async def test_availability_requires_active_root_credential_and_aligned_quotas(
    db_session,
    legacy_owner_roots,
):
    username = get_web_config().auth_username
    unavailable = await body_ai.project_body_scan_ai_availability(
        db_session, actor_username=username
    )
    assert unavailable.available is False
    await db_session.rollback()

    await _configure_platform(
        db_session,
        legacy_owner_roots,
        subject_quota=False,
    )
    no_subject_quota = await body_ai.project_body_scan_ai_availability(
        db_session, actor_username=username
    )
    assert no_subject_quota.available is False
    await db_session.rollback()

    misaligned = AISubjectQuotaPeriod(
        subject_id=legacy_owner_roots.subject_id,
        period_start=date(2026, 8, 10),
        period_end=PERIOD_END,
        cost_limit_microunits=100_000_000,
        unit_limit=10_000_000,
        configured_by_user_id=legacy_owner_roots.user_id,
    )
    db_session.add(misaligned)
    await db_session.commit()
    misaligned_quota = await body_ai.project_body_scan_ai_availability(
        db_session, actor_username=username
    )
    assert misaligned_quota.available is False
    await db_session.rollback()

    await db_session.delete(misaligned)
    db_session.add(
        AISubjectQuotaPeriod(
            subject_id=legacy_owner_roots.subject_id,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            cost_limit_microunits=100_000_000,
            unit_limit=10_000_000,
            configured_by_user_id=legacy_owner_roots.user_id,
        )
    )
    await db_session.commit()
    available = await body_ai.project_body_scan_ai_availability(
        db_session, actor_username=username
    )
    assert available.available is True


@pytest.mark.parametrize(
    ("code", "message_key"),
    (
        (
            body_ai.BodyScanAIAvailabilityCode.NOT_CONFIGURED,
            "body.not_configured",
        ),
        (
            body_ai.BodyScanAIAvailabilityCode.QUOTA,
            "body.quota",
        ),
    ),
)
async def test_measures_page_projects_redacted_unavailable_state_and_disables_upload(
    auth_client,
    monkeypatch,
    code,
    message_key,
):
    async def unavailable(_session, *, actor_username):
        assert actor_username == get_web_config().auth_username
        return body_ai.BodyScanAIAvailability(False, code)

    monkeypatch.setattr(
        body_ai,
        "project_body_scan_ai_availability",
        unavailable,
    )
    await auth_client.post(
        "/settings/modules",
        data={"module": "body_comp", "enabled": "true"},
    )
    response = await auth_client.get(
        "/weight/measures",
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 200
    assert any(
        strings[message_key] in response.text
        for strings in STRINGS.values()
    )
    assert re.search(r'<input type="file"[^>]*\bdisabled\b', response.text)
    assert ':disabled="bsUploading || true"' in response.text


async def test_replay_normalizes_only_successful_platform_extraction(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    successful = await _prepare(db_session, suffix="replay-success")
    success_raw = await db_session.scalar(
        select(RawPayload).where(RawPayload.external_id.contains("replay-success"))
    )
    assert success_raw is not None
    await db_session.commit()
    success_lease, success_content = await _start(db_session, successful)
    success_completion, _ = await _render(
        db_session,
        successful,
        success_lease,
        success_content,
    )
    await body_ai.persist_body_scan_parse(
        db_session, successful, success_completion
    )
    await db_session.commit()
    success_invocation = await db_session.scalar(
        select(AIInvocation).where(AIInvocation.raw_payload_id == success_raw.id)
    )
    assert success_invocation is not None
    assert (
        success_invocation.status,
        success_invocation.purpose,
        success_invocation.source,
    ) == (
        AIInvocationStatus.SUCCEEDED.value,
        AIInvocationPurpose.BODY_SCAN_PARSE.value,
        AIInvocationSource.WEB.value,
    )

    failed = await _prepare(db_session, suffix="replay-failed")
    failed_raw = await db_session.scalar(
        select(RawPayload).where(RawPayload.external_id.contains("replay-failed"))
    )
    assert failed_raw is not None
    await db_session.commit()
    failed_lease, failed_content = await _start(db_session, failed)
    failed_completion, _ = await _render(
        db_session,
        failed,
        failed_lease,
        failed_content,
        behavior="provider_error",
    )
    await body_ai.persist_body_scan_parse(db_session, failed, failed_completion)
    await db_session.commit()
    failed_invocation = await db_session.scalar(
        select(AIInvocation).where(AIInvocation.raw_payload_id == failed_raw.id)
    )
    assert failed_invocation is not None
    assert (
        failed_invocation.status,
        failed_invocation.purpose,
        failed_invocation.source,
    ) == (
        AIInvocationStatus.AMBIGUOUS.value,
        AIInvocationPurpose.BODY_SCAN_PARSE.value,
        AIInvocationSource.WEB.value,
    )

    context = await engine.resolve_legacy_conflict_write_context(
        db_session,
        actor_username=None,
        evaluation_date=DAY,
    )
    done = await scans.reparse_owned_pending(
        db_session,
        identity=context.identity,
    )
    await db_session.commit()

    assert done == 1
    result = await db_session.scalar(
        select(BodyScan).where(BodyScan.raw_payload_id == success_raw.id)
    )
    assert result is not None and result.date == DAY
    weight = await weight_service.get_active_weight(
        db_session,
        DAY,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert weight is not None
    assert (
        weight.subject_id,
        weight.actor_user_id,
        weight.integration_connection_id,
        weight.raw_payload_id,
        weight.source,
    ) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        None,
        success_raw.id,
        Source.BODY_SCAN.value,
    )
    assert await db_session.scalar(
        select(func.count()).select_from(BodyScan).where(
            BodyScan.raw_payload_id == failed_raw.id
        )
    ) == 0
    failed_raw = await db_session.get(RawPayload, failed_raw.id)
    assert failed_raw is not None and failed_raw.processed_at is None


@pytest.mark.parametrize(
    "tamper",
    ("missing_file", "missing_invocation", "mixed_subject_connection"),
)
async def test_derived_weight_rejects_broken_platform_parser_graph(
    db_session,
    legacy_owner_roots,
    tamper,
):
    await _configure_platform(db_session, legacy_owner_roots)
    raw, invocation, _scan = await _persist_and_replay_platform_scan(
        db_session,
        legacy_owner_roots,
        suffix=f"weight-{tamper}",
    )
    weight = await db_session.scalar(
        select(WeightLog).where(
            WeightLog.raw_payload_id == raw.id
        )
    )
    assert weight is not None

    if tamper == "missing_file":
        raw.file_asset_id = None
    elif tamper == "missing_invocation":
        await db_session.delete(invocation)
    else:
        connection = await db_session.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.subject_id == legacy_owner_roots.subject_id,
                IntegrationConnection.provider
                == IntegrationProvider.OPENROUTER.value,
            )
        )
        assert connection is not None
        raw.integration_connection_id = connection.id
        weight.integration_connection_id = connection.id
    await db_session.commit()

    with pytest.raises(engine.ConflictRawOwnershipError):
        await weight_service.get_active_weight(
            db_session,
            DAY,
            subject_id=legacy_owner_roots.subject_id,
        )


@pytest.mark.parametrize("purged", (False, True))
async def test_retained_weight_accepts_monotonically_retired_scan_file(
    db_session,
    legacy_owner_roots,
    purged,
):
    await _configure_platform(db_session, legacy_owner_roots)
    raw, _invocation, scan = await _persist_and_replay_platform_scan(
        db_session,
        legacy_owner_roots,
        suffix=f"retired-{purged}",
    )
    assert raw.file_asset_id is not None
    await db_session.delete(scan)
    await file_asset_service.mark_legacy_local_deleted(
        db_session,
        file_asset_id=raw.file_asset_id,
        subject_id=legacy_owner_roots.subject_id,
        purged=purged,
    )
    await db_session.commit()

    weight = await weight_service.get_active_weight(
        db_session,
        DAY,
        subject_id=legacy_owner_roots.subject_id,
    )
    assert weight is not None and weight.raw_payload_id == raw.id


async def test_mcp_scan_and_derived_weight_reject_mixed_parser_invocation(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    identity = WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    raw = RawPayload(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        integration_connection_id=None,
        file_asset_id=None,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MCP.value,
        external_id=f"mcp-body-{uuid.uuid4().hex}",
        payload=EXTRACTED,
    )
    db_session.add(raw)
    await db_session.flush()
    scan = BodyScan(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        file_asset_id=None,
        raw_payload_id=raw.id,
        date=DAY,
        domain=Domain.BODY_COMPOSITION.value,
        source=Source.MCP.value,
    )
    weight = WeightLog(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        integration_connection_id=None,
        raw_payload_id=raw.id,
        date=DAY,
        weight_kg=78.3,
        domain=Domain.WEIGHT.value,
        source=Source.BODY_SCAN.value,
        superseded=False,
    )
    db_session.add_all([scan, weight])
    await gateway.reserve_ai_invocation(
        db_session,
        identity=identity,
        purpose=AIInvocationPurpose.BODY_SCAN_PARSE,
        source=AIInvocationSource.WEB,
        model=MODEL,
        idempotency_key=f"synthetic-mixed-mcp:{raw.id}",
        reserved_cost_microunits=10_000,
        reserved_units=10_000,
        raw_payload_id=raw.id,
    )
    await db_session.commit()

    with pytest.raises(engine.ConflictRawOwnershipError):
        await scans.list_scans(
            db_session,
            subject_id=identity.subject_id,
        )
    await db_session.rollback()
    with pytest.raises(engine.ConflictRawOwnershipError):
        await weight_service.get_active_weight(
            db_session,
            DAY,
            subject_id=identity.subject_id,
        )


@pytest.mark.integration
async def test_postgres_concurrent_start_issues_one_lease_and_one_provider_call(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    prepared = await _prepare(db_session, suffix="concurrent")
    content = body_ai.prepare_body_scan_content(
        prepared,
        file_bytes=FILE_BYTES,
    )
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async def contender():
        async with factory() as session:
            try:
                lease = await body_ai.start_body_scan_dispatch(
                    session,
                    prepared,
                    content=content,
                    credential_resolver=lambda _reference: SECRET,
                )
                await session.commit()
                return lease
            except gateway.AIInvocationStateError as exc:
                await session.rollback()
                return exc

    outcomes = await asyncio.wait_for(
        asyncio.gather(contender(), contender()),
        timeout=10,
    )
    leases = [item for item in outcomes if isinstance(item, gateway.AIDispatchLease)]
    assert len(leases) == 1
    assert (
        sum(isinstance(item, gateway.AIInvocationStateError) for item in outcomes)
        == 1
    )

    observed = _observed()
    completion = await body_ai.render_body_scan(
        prepared,
        leases[0],
        file_bytes=FILE_BYTES,
        content=content,
        llm_factory=_llm(db_session, observed),
    )
    async with factory() as session:
        await body_ai.persist_body_scan_parse(session, prepared, completion)
        await session.commit()
    assert observed["calls"] == 1
    assert await db_session.scalar(
        select(func.count()).select_from(AIInvocation).where(
            AIInvocation.status == AIInvocationStatus.SUCCEEDED.value
        )
    ) == 1
