"""Platform-funded, raw-first Labs document parser contracts."""
from __future__ import annotations

import asyncio
import hashlib
import pickle
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.config import load_config as load_runtime_config
from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.integrations.llm_client import LLMCallResult
from vitals.models.ai import AIInvocation, AIPlatformQuotaPeriod, AISubjectQuotaPeriod
from vitals.models.identity import HealthSubject, User
from vitals.models.labs import LabResult
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import (
    FileAsset,
    IntegrationConnection,
    PlatformIntegrationConnection,
)
from vitals.services import ai_gateway_service as gateway
from vitals.services import conflict_engine, labs_service
from vitals.services import lab_document_ai_service as lab_ai
from vitals.services.legacy_ownership import LegacySubjectResolutionError
from web.config import get_web_config


NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
DAY = date(2026, 8, 20)
PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 9, 1)
MODEL = "synthetic/lab-vision-model"
SECRET = "synthetic-platform-secret"
FILE_BYTES = b"synthetic-private-lab-image"
SHA256 = hashlib.sha256(FILE_BYTES).hexdigest()
EXTRACTED = {
    "date": DAY.isoformat(),
    "lab_name": "Synthetic Lab",
    "results": [
        {
            "marker": "Ferritin",
            "value": 91,
            "unit": "ng/mL",
            "ref_low": 30,
            "ref_high": 300,
        }
    ],
}


@pytest.fixture(autouse=True)
def _runtime(monkeypatch):
    monkeypatch.setattr(gateway, "now_utc", lambda: NOW)
    if hasattr(lab_ai, "now_utc"):
        monkeypatch.setattr(lab_ai, "now_utc", lambda: NOW)
    config = replace(
        load_runtime_config(),
        openrouter_api_key=SECRET,
        llm_model_parser=MODEL,
    )
    monkeypatch.setattr(lab_ai, "load_config", lambda: config)


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
        external_account_discriminator=f"lab-{uuid.uuid4().hex}",
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
    return f"labs/{suffix}-{uuid.uuid4().hex}.png"


async def _prepare(session: AsyncSession, *, suffix: str = "panel"):
    return await lab_ai.prepare_lab_document_parse(
        session,
        actor_username=get_web_config().auth_username,
        storage_ref=_storage_ref(suffix),
        media_type="image/png",
        byte_size=len(FILE_BYTES),
        sha256_hex=SHA256,
    )


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
                value = {"unexpected": "not a lab extraction"}
            return LLMCallResult(
                value=value,
                upstream_request_id="lab-request-1",
                model=kwargs.get("model") or MODEL,
                input_tokens=120,
                output_tokens=40,
                cost_microunits=None if behavior == "missing_usage" else 75,
            )

    return FakeLLM


async def _start(session: AsyncSession, prepared):
    lease = await lab_ai.start_lab_document_dispatch(
        session,
        prepared,
        credential_resolver=lambda reference: (
            SECRET if reference == "env:VITALS_OPENROUTER_API_KEY" else None
        ),
    )
    await session.commit()
    return lease


async def _render(session: AsyncSession, prepared, lease, *, behavior="success"):
    observed = _observed()
    completion = await lab_ai.render_lab_document(
        prepared,
        lease,
        file_bytes=FILE_BYTES,
        llm_factory=_llm(session, observed, behavior=behavior),
    )
    return completion, observed


async def test_full_flow_is_opaque_c_null_usage_aware_and_transaction_free(
    db_session,
    legacy_owner_roots,
):
    root = await _configure_platform(db_session, legacy_owner_roots)
    await _remove_subject_openrouter(db_session, legacy_owner_roots)

    prepared = await _prepare(db_session)
    prepared_repr = repr(prepared)
    assert "synthetic-private" not in prepared_repr
    assert "labs/" not in prepared_repr
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
        Domain.LABS.value,
        Source.LAB_PARSER.value,
    )
    assert (asset.subject_id, asset.uploaded_by_user_id, asset.purpose) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        FileAssetPurpose.LAB_DOCUMENT.value,
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
        AIInvocationPurpose.LAB_DOCUMENT_PARSE.value,
        AIInvocationSource.WEB.value,
        MODEL,
        AIInvocationStatus.PREPARED.value,
    )
    placeholder = raw.payload
    await db_session.commit()

    lease = await _start(db_session, prepared)
    assert "labs/" not in repr(lease)
    with pytest.raises(TypeError):
        pickle.dumps(lease)
    completion, observed = await _render(db_session, prepared, lease)
    assert "Ferritin" not in repr(completion)
    assert SECRET not in repr(completion)
    with pytest.raises(TypeError):
        pickle.dumps(completion)
    assert observed["calls"] == 1
    assert observed["no_tx"] == [True]
    assert observed["credentials"] == [SECRET]
    assert observed["models"] == [MODEL]
    assert db_session.in_transaction() is False

    await lab_ai.persist_lab_document_parse(db_session, prepared, completion)
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
        "lab-request-1",
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
    lease = await _start(db_session, prepared)
    completion, observed = await _render(
        db_session, prepared, lease, behavior=behavior
    )
    result = await lab_ai.persist_lab_document_parse(
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
        select(func.count()).select_from(LabResult).where(
            LabResult.raw_payload_id == raw_id
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
    lease = await _start(db_session, prepared)
    completion, _observations = await _render(db_session, prepared, lease)

    await lab_ai.persist_lab_document_parse(db_session, prepared, completion)
    assert (await db_session.get(RawPayload, raw_id)).payload == EXTRACTED
    await db_session.rollback()
    rolled_back_raw = await db_session.get(RawPayload, raw_id)
    rolled_back_invocation = await db_session.get(AIInvocation, invocation_id)
    assert rolled_back_raw is not None and rolled_back_raw.payload == placeholder
    assert rolled_back_invocation is not None
    assert rolled_back_invocation.status == AIInvocationStatus.DISPATCHING.value
    await db_session.rollback()

    await lab_ai.persist_lab_document_parse(db_session, prepared, completion)
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
        await lab_ai.prepare_lab_document_parse(
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
    await db_session.commit()
    owner = await db_session.get(User, legacy_owner_roots.user_id)
    assert owner is not None
    owner.status = UserStatus.SUSPENDED.value
    await db_session.commit()
    credential_calls = []

    with pytest.raises(lab_ai.LabDocumentAIOwnershipError):
        await lab_ai.start_lab_document_dispatch(
            db_session,
            prepared,
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
                display_name="Foreign Lab Subject",
                timezone="UTC",
            )
            db_session.add(foreign_subject)
            await db_session.flush()
            asset.subject_id = foreign_subject.id
    await db_session.commit()

    credential_calls = []
    with pytest.raises(lab_ai.LabDocumentAIOwnershipError):
        await lab_ai.start_lab_document_dispatch(
            db_session,
            prepared,
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
    unavailable = await lab_ai.project_lab_ai_availability(
        db_session, actor_username=username
    )
    assert unavailable.available is False
    await db_session.rollback()

    await _configure_platform(
        db_session,
        legacy_owner_roots,
        subject_quota=False,
    )
    no_subject_quota = await lab_ai.project_lab_ai_availability(
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
    misaligned_quota = await lab_ai.project_lab_ai_availability(
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
    available = await lab_ai.project_lab_ai_availability(
        db_session, actor_username=username
    )
    assert available.available is True


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
    success_lease = await _start(db_session, successful)
    success_completion, _ = await _render(db_session, successful, success_lease)
    await lab_ai.persist_lab_document_parse(
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
        AIInvocationPurpose.LAB_DOCUMENT_PARSE.value,
        AIInvocationSource.WEB.value,
    )

    failed = await _prepare(db_session, suffix="replay-failed")
    failed_raw = await db_session.scalar(
        select(RawPayload).where(RawPayload.external_id.contains("replay-failed"))
    )
    assert failed_raw is not None
    await db_session.commit()
    failed_lease = await _start(db_session, failed)
    failed_completion, _ = await _render(
        db_session, failed, failed_lease, behavior="provider_error"
    )
    await lab_ai.persist_lab_document_parse(db_session, failed, failed_completion)
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
        AIInvocationPurpose.LAB_DOCUMENT_PARSE.value,
        AIInvocationSource.WEB.value,
    )

    context = await conflict_engine.resolve_legacy_conflict_write_context(
        db_session,
        actor_username=None,
        evaluation_date=DAY,
    )
    prepared_write = await conflict_engine.prepare_scoped_write(
        db_session, context=context
    )
    done = await labs_service.reparse_owned_pending(
        db_session,
        identity=context.identity,
        prepared_conflict_write=prepared_write,
    )
    await db_session.commit()

    assert done == 1
    result = await db_session.scalar(
        select(LabResult).where(LabResult.raw_payload_id == success_raw.id)
    )
    assert result is not None and result.marker == "Ferritin"
    assert await db_session.scalar(
        select(func.count()).select_from(LabResult).where(
            LabResult.raw_payload_id == failed_raw.id
        )
    ) == 0
    failed_raw = await db_session.get(RawPayload, failed_raw.id)
    assert failed_raw is not None and failed_raw.processed_at is None


@pytest.mark.integration
async def test_postgres_concurrent_start_issues_one_lease_and_one_provider_call(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    prepared = await _prepare(db_session, suffix="concurrent")
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
                lease = await lab_ai.start_lab_document_dispatch(
                    session,
                    prepared,
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
    completion = await lab_ai.render_lab_document(
        prepared,
        leases[0],
        file_bytes=FILE_BYTES,
        llm_factory=_llm(db_session, observed),
    )
    async with factory() as session:
        await lab_ai.persist_lab_document_parse(session, prepared, completion)
        await session.commit()
    assert observed["calls"] == 1
    assert await db_session.scalar(
        select(func.count()).select_from(AIInvocation).where(
            AIInvocation.status == AIInvocationStatus.SUCCEEDED.value
        )
    ) == 1
