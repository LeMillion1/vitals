"""WeeklyDigest consumer contract for the platform-funded AI gateway."""
from __future__ import annotations

from vitals.services.ai_gateway import config as gateway_config
from vitals.services.ai_gateway import contracts as gateway_contracts
from vitals.services.ai_gateway import dispatch as gateway_dispatch
from vitals.services.ai_gateway import invocations as gateway_invocations
from vitals.services.ai_gateway import jobs as gateway_jobs
from vitals.services.ai_gateway import reconciliation as gateway_reconciliation

from vitals.services.digest import ownership as digest_ownership
from vitals.services.digest import generation as digest_generation
from vitals.services.digest import queries as digest_queries
from vitals.services.digest import prompt as digest_prompt
from vitals.services.digest import jobs as digest_jobs

from tests.job_runner import run_job_for_every_subject

import asyncio
import pickle
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.config import load_config as load_runtime_config
from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationSource,
    AIInvocationStatus,
    DigestKind,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.integrations.llm_client import LLMCallResult
from vitals.models.ai import (
    AIInvocation,
    AIPlatformQuotaPeriod,
    AISubjectQuotaPeriod,
)
from vitals.models.identity import User
from vitals.models.milestones import DOMAIN, WeeklyDigest
from vitals.models.ownership_backfill import OwnershipBackfillCheckpoint
from vitals.models.tenancy import PlatformIntegrationConnection
from vitals.operations.ownership.portability_v1 import import_full
from vitals.ownership import OWNERSHIP_REGISTRY
from vitals.services.portability.v1_contract import _EXCLUDED_TABLES
from vitals.services.portability.v1_export import export_full
from vitals.services.legacy_ownership import LegacyOwnershipError
from web.config import get_web_config

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
DAY = date(2026, 8, 20)
PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 9, 1)
MODEL = "synthetic/digest-model"
SECRET = "synthetic-openrouter-secret"
SENSITIVE_MARKER = "synthetic-sensitive-health-context"


@pytest.fixture(autouse=True)
def _fixed_gateway_clock(monkeypatch):
    for module in (
        gateway_config,
        gateway_dispatch,
        gateway_invocations,
        gateway_jobs,
        gateway_reconciliation,
    ):
        monkeypatch.setattr(module, "now_utc", lambda: NOW)


async def _configure_platform(
    session,
    roots,
    *,
    status: IntegrationConnectionStatus = IntegrationConnectionStatus.ACTIVE,
    include_platform_quota: bool = True,
    include_subject_quota: bool = True,
    cost_limit: int = 100_000_000,
    unit_limit: int = 10_000_000,
):
    root = PlatformIntegrationConnection(
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=f"opaque-{uuid.uuid4().hex}",
        credential_ref="env:VITALS_OPENROUTER_API_KEY",
        status=status.value,
        config_version=1,
        configured_by_user_id=roots.user_id,
    )
    session.add(root)
    await session.flush()
    if include_platform_quota:
        session.add(
            AIPlatformQuotaPeriod(
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                cost_limit_microunits=cost_limit,
                unit_limit=unit_limit,
                configured_by_user_id=roots.user_id,
            )
        )
    if include_subject_quota:
        session.add(
            AISubjectQuotaPeriod(
                subject_id=roots.subject_id,
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                cost_limit_microunits=cost_limit,
                unit_limit=unit_limit,
                configured_by_user_id=roots.user_id,
            )
        )
    await session.commit()
    return root


def _install_fake_llm(
    monkeypatch,
    session,
    *,
    behavior: str = "success",
    runtime_secret: str = "",
):
    base_config = replace(
        load_runtime_config(),
        openrouter_api_key=runtime_secret,
        llm_model_digest=MODEL,
    )
    monkeypatch.setattr(digest_ownership, "load_config", lambda: base_config)
    monkeypatch.setattr(digest_generation, "load_config", lambda: base_config)

    async def synthetic_context(*_args, **_kwargs):
        return {"synthetic": SENSITIVE_MARKER}

    monkeypatch.setattr(digest_ownership, "assemble_context", synthetic_context)
    observations = {
        "calls": 0,
        "no_transaction": [],
        "credential_matches": [],
        "models": [],
        "max_tokens": [],
    }

    class FakeLLMClient:
        def __init__(self, config):
            self._config = config

        async def complete_text_with_usage(
            self,
            _prompt,
            *,
            model,
            system,
            max_tokens,
        ):
            del system
            observations["calls"] += 1
            observations["no_transaction"].append(not session.in_transaction())
            observations["credential_matches"].append(
                self._config.openrouter_api_key == SECRET
            )
            observations["models"].append(model)
            observations["max_tokens"].append(max_tokens)
            if behavior == "provider_exception":
                raise RuntimeError("sensitive provider failure text")
            if behavior == "blank":
                value = "  "
            else:
                value = "Synthetic weekly narrative"
            return LLMCallResult(
                value=value,
                upstream_request_id=" opaque-request-1 ",
                model=MODEL,
                input_tokens=12,
                output_tokens=34,
                cost_microunits=None if behavior == "missing_usage" else 56,
            )

    monkeypatch.setattr(digest_generation, "LLMClient", FakeLLMClient)
    return observations


def _actor_username(source: AIInvocationSource) -> str | None:
    if source is AIInvocationSource.SCHEDULER:
        return None
    return get_web_config().auth_username


async def _prepare_and_commit(session, source: AIInvocationSource):
    prepared = await digest_ownership.prepare_digest(
        session,
        actor_username=_actor_username(source),
        invocation_source=source,
        on_date=DAY,
    )
    await session.commit()
    return prepared


async def _start_and_commit(session, prepared):
    lease = await digest_generation.start_digest_dispatch(
        session,
        prepared,
        credential_resolver=lambda reference: (
            SECRET
            if reference == "env:VITALS_OPENROUTER_API_KEY"
            else None
        ),
    )
    await session.commit()
    return lease


@pytest.mark.parametrize(
    ("invocation_source", "artifact_source", "has_actor"),
    (
        (AIInvocationSource.WEB, Source.MANUAL, True),
        (AIInvocationSource.MCP, Source.MCP, True),
        (AIInvocationSource.SCHEDULER, Source.SCHEDULER, False),
    ),
)
async def test_full_gateway_flow_maps_source_actor_and_persists_exact_provenance(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    invocation_source,
    artifact_source,
    has_actor,
):
    root = await _configure_platform(db_session, legacy_owner_roots)
    observations = _install_fake_llm(monkeypatch, db_session)
    prepared = await _prepare_and_commit(db_session, invocation_source)
    assert SENSITIVE_MARKER not in repr(prepared)
    with pytest.raises(TypeError):
        pickle.dumps(prepared)

    lease = await _start_and_commit(db_session, prepared)
    assert SECRET not in repr(lease)
    with pytest.raises(TypeError):
        pickle.dumps(lease)
    completion = await digest_generation.render_digest(prepared, lease)
    assert SENSITIVE_MARKER not in repr(completion)
    assert "Synthetic weekly narrative" not in repr(completion)
    with pytest.raises(TypeError):
        pickle.dumps(completion)

    row = await digest_generation.persist_digest(db_session, prepared, completion)
    assert row is not None
    await db_session.commit()
    assert completion.payload is None
    assert observations == {
        "calls": 1,
        "no_transaction": [True],
        "credential_matches": [True],
        "models": [MODEL],
        "max_tokens": [digest_ownership._DIGEST_MAX_TOKENS],
    }

    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None
    assert invocation.subject_id == legacy_owner_roots.subject_id
    assert (invocation.actor_user_id is not None) is has_actor
    assert invocation.source == invocation_source.value
    assert invocation.status == AIInvocationStatus.SUCCEEDED.value
    assert invocation.platform_integration_connection_id == root.id
    assert invocation.config_version == root.config_version
    assert invocation.upstream_request_id == "opaque-request-1"
    assert invocation.input_tokens == 12
    assert invocation.output_tokens == 34
    assert invocation.cost_microunits == 56
    assert invocation.charged_cost_microunits == invocation.reserved_cost_microunits
    assert invocation.charged_units == invocation.reserved_units
    assert row.subject_id == invocation.subject_id
    assert row.actor_user_id == invocation.actor_user_id
    assert row.source == artifact_source.value
    assert row.integration_connection_id is None
    assert row.ai_invocation_id == invocation.id
    assert row.model == invocation.model == MODEL


@pytest.mark.parametrize(
    ("behavior", "expected_status", "expected_error"),
    (
        (
            "blank",
            AIInvocationStatus.FAILED,
            AIInvocationErrorCode.INVALID_RESPONSE,
        ),
        (
            "missing_usage",
            AIInvocationStatus.FAILED,
            AIInvocationErrorCode.INVALID_RESPONSE,
        ),
        (
            "provider_exception",
            AIInvocationStatus.AMBIGUOUS,
            AIInvocationErrorCode.PROVIDER_UNAVAILABLE,
        ),
    ),
)
async def test_invalid_or_failed_provider_call_is_once_sanitized_and_fully_charged(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    behavior,
    expected_status,
    expected_error,
):
    await _configure_platform(db_session, legacy_owner_roots)
    observations = _install_fake_llm(
        monkeypatch,
        db_session,
        behavior=behavior,
    )
    prepared = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    lease = await _start_and_commit(db_session, prepared)
    completion = await digest_generation.render_digest(prepared, lease)
    assert completion.status is expected_status
    assert completion.error_code is expected_error
    assert "sensitive provider failure" not in repr(completion)
    assert await digest_generation.persist_digest(
        db_session, prepared, completion
    ) is None
    await db_session.commit()

    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None
    assert observations["calls"] == 1
    assert invocation.status == expected_status.value
    assert invocation.error_code == expected_error.value
    assert invocation.charged_cost_microunits == invocation.reserved_cost_microunits
    assert invocation.charged_units == invocation.reserved_units
    assert await db_session.scalar(
        select(func.count()).select_from(WeeklyDigest)
    ) == 0


async def test_finalize_and_artifact_rollback_can_retry_once(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    _install_fake_llm(monkeypatch, db_session)
    prepared = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    lease = await _start_and_commit(db_session, prepared)
    completion = await digest_generation.render_digest(prepared, lease)

    first = await digest_generation.persist_digest(db_session, prepared, completion)
    assert first is not None
    await db_session.rollback()
    assert completion.payload is not None
    retried = await digest_generation.persist_digest(db_session, prepared, completion)
    assert retried is not None
    await db_session.commit()
    assert completion.payload is None
    assert await db_session.scalar(
        select(func.count()).select_from(WeeklyDigest)
    ) == 1


async def test_deterministic_terminal_duplicate_reuses_existing_artifact_no_network(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    observations = _install_fake_llm(monkeypatch, db_session)
    first = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    lease = await _start_and_commit(db_session, first)
    completion = await digest_generation.render_digest(first, lease)
    artifact = await digest_generation.persist_digest(db_session, first, completion)
    assert artifact is not None
    await db_session.commit()

    duplicate = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    assert duplicate.invocation_id == first.invocation_id
    assert duplicate.reservation_status is AIInvocationStatus.SUCCEEDED
    assert duplicate.dispatchable is False
    assert duplicate.existing_artifact_id == artifact.id
    resolver_calls = 0

    def resolver(_reference):
        nonlocal resolver_calls
        resolver_calls += 1
        return SECRET

    with pytest.raises(digest_ownership.DigestInvocationStateError):
        await digest_generation.start_digest_dispatch(
            db_session,
            duplicate,
            credential_resolver=resolver,
        )
    await db_session.rollback()
    owner = await digest_ownership.prepare_digest_owner(
        db_session,
        actor_username=get_web_config().auth_username,
    )
    existing = await digest_generation.existing_digest_for_prepared(
        db_session,
        duplicate,
        prepared_owner=owner,
    )
    assert existing is not None and existing.id == artifact.id
    assert observations["calls"] == 1
    assert resolver_calls == 0
    assert await db_session.scalar(
        select(func.count()).select_from(AIInvocation)
    ) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(WeeklyDigest)
    ) == 1


async def test_succeeded_product_reuses_artifact_after_root_and_context_change(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    old_root = await _configure_platform(db_session, legacy_owner_roots)
    observations = _install_fake_llm(monkeypatch, db_session)
    first = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    lease = await _start_and_commit(db_session, first)
    completion = await digest_generation.render_digest(first, lease)
    artifact = await digest_generation.persist_digest(db_session, first, completion)
    assert artifact is not None
    await db_session.commit()

    old_root = await db_session.get(PlatformIntegrationConnection, old_root.id)
    assert old_root is not None
    old_root.status = IntegrationConnectionStatus.RETIRED.value
    old_root.retired_at = NOW
    db_session.add(
        PlatformIntegrationConnection(
            provider=IntegrationProvider.OPENROUTER.value,
            connection_type=IntegrationConnectionType.AI_GATEWAY.value,
            external_account_discriminator=f"opaque-{uuid.uuid4().hex}",
            credential_ref="env:VITALS_OPENROUTER_API_KEY",
            status=IntegrationConnectionStatus.ACTIVE.value,
            config_version=2,
            configured_by_user_id=legacy_owner_roots.user_id,
        )
    )
    await db_session.commit()

    async def larger_context(*_args, **_kwargs):
        return {"synthetic": SENSITIVE_MARKER * 20}

    monkeypatch.setattr(digest_ownership, "assemble_context", larger_context)
    duplicate = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    assert duplicate.invocation_id == first.invocation_id
    assert duplicate.reservation_status is AIInvocationStatus.SUCCEEDED
    assert duplicate.existing_artifact_id == artifact.id
    assert duplicate.dispatchable is False
    assert observations["calls"] == 1
    assert await db_session.scalar(
        select(func.count()).select_from(AIInvocation)
    ) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(WeeklyDigest)
    ) == 1


async def test_dispatching_product_stays_pending_after_root_and_context_change(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    old_root = await _configure_platform(db_session, legacy_owner_roots)
    observations = _install_fake_llm(monkeypatch, db_session)
    first = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    await _start_and_commit(db_session, first)

    old_root = await db_session.get(PlatformIntegrationConnection, old_root.id)
    assert old_root is not None
    old_root.status = IntegrationConnectionStatus.RETIRED.value
    old_root.retired_at = NOW
    db_session.add(
        PlatformIntegrationConnection(
            provider=IntegrationProvider.OPENROUTER.value,
            connection_type=IntegrationConnectionType.AI_GATEWAY.value,
            external_account_discriminator=f"opaque-{uuid.uuid4().hex}",
            credential_ref="env:VITALS_OPENROUTER_API_KEY",
            status=IntegrationConnectionStatus.ACTIVE.value,
            config_version=2,
            configured_by_user_id=legacy_owner_roots.user_id,
        )
    )
    await db_session.commit()

    async def larger_context(*_args, **_kwargs):
        return {"synthetic": SENSITIVE_MARKER * 20}

    monkeypatch.setattr(digest_ownership, "assemble_context", larger_context)
    duplicate = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    assert duplicate.invocation_id == first.invocation_id
    assert duplicate.reservation_status is AIInvocationStatus.DISPATCHING
    assert duplicate.existing_artifact_id is None
    assert duplicate.dispatchable is False
    assert observations["calls"] == 0
    assert await db_session.scalar(
        select(func.count()).select_from(AIInvocation)
    ) == 1


async def test_changed_prepared_fingerprint_releases_old_reservation_first(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    _install_fake_llm(monkeypatch, db_session)
    first = await _prepare_and_commit(db_session, AIInvocationSource.WEB)

    async def larger_context(*_args, **_kwargs):
        return {"synthetic": SENSITIVE_MARKER * 20}

    monkeypatch.setattr(digest_ownership, "assemble_context", larger_context)
    replacement = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    assert replacement.attempt == 1
    assert replacement.invocation_id != first.invocation_id
    assert replacement.dispatchable is True

    old = await db_session.get(AIInvocation, first.invocation_id)
    new = await db_session.get(AIInvocation, replacement.invocation_id)
    platform = await db_session.get(
        AIPlatformQuotaPeriod,
        (PERIOD_START, PERIOD_END),
    )
    subject = await db_session.get(
        AISubjectQuotaPeriod,
        (legacy_owner_roots.subject_id, PERIOD_START, PERIOD_END),
    )
    assert old is not None and new is not None
    assert old.status == AIInvocationStatus.CANCELLED.value
    assert new.status == AIInvocationStatus.PREPARED.value
    assert new.reserved_units > old.reserved_units
    assert platform is not None and subject is not None
    assert platform.reserved_cost_microunits == new.reserved_cost_microunits
    assert subject.reserved_cost_microunits == new.reserved_cost_microunits
    assert platform.reserved_units == new.reserved_units
    assert subject.reserved_units == new.reserved_units


async def test_terminal_failure_advances_to_one_new_bounded_attempt(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    failed_observations = _install_fake_llm(
        monkeypatch,
        db_session,
        behavior="blank",
    )
    failed = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    failed_lease = await _start_and_commit(db_session, failed)
    failed_completion = await digest_generation.render_digest(failed, failed_lease)
    assert await digest_generation.persist_digest(
        db_session,
        failed,
        failed_completion,
    ) is None
    await db_session.commit()
    assert failed.attempt == 0
    assert failed_observations["calls"] == 1

    success_observations = _install_fake_llm(monkeypatch, db_session)
    retried = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    assert retried.attempt == 1
    assert retried.invocation_id != failed.invocation_id
    assert retried.dispatchable is True
    lease = await _start_and_commit(db_session, retried)
    completion = await digest_generation.render_digest(retried, lease)
    artifact = await digest_generation.persist_digest(
        db_session,
        retried,
        completion,
    )
    assert artifact is not None
    await db_session.commit()
    assert success_observations["calls"] == 1
    assert await db_session.scalar(
        select(func.count()).select_from(AIInvocation)
    ) == 2
    assert await db_session.scalar(
        select(func.count()).select_from(WeeklyDigest)
    ) == 1


async def test_rotated_root_start_failure_releases_old_reservation_and_retries(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    old_root = await _configure_platform(db_session, legacy_owner_roots)
    _install_fake_llm(monkeypatch, db_session)
    prepared = await _prepare_and_commit(db_session, AIInvocationSource.WEB)

    old_root = await db_session.get(PlatformIntegrationConnection, old_root.id)
    assert old_root is not None
    old_root.status = IntegrationConnectionStatus.RETIRED.value
    old_root.retired_at = NOW
    db_session.add(
        PlatformIntegrationConnection(
            provider=IntegrationProvider.OPENROUTER.value,
            connection_type=IntegrationConnectionType.AI_GATEWAY.value,
            external_account_discriminator=f"opaque-{uuid.uuid4().hex}",
            credential_ref="env:VITALS_OPENROUTER_API_KEY",
            status=IntegrationConnectionStatus.ACTIVE.value,
            config_version=2,
            configured_by_user_id=legacy_owner_roots.user_id,
        )
    )
    await db_session.commit()

    with pytest.raises(gateway_contracts.AIGatewayConfigurationError):
        await digest_generation.start_digest_dispatch(
            db_session,
            prepared,
            credential_resolver=lambda _reference: SECRET,
        )
    await db_session.rollback()
    assert await digest_generation.release_prepared_digest(db_session, prepared)
    await db_session.commit()
    cancelled = await db_session.get(AIInvocation, prepared.invocation_id)
    assert cancelled is not None
    assert cancelled.status == AIInvocationStatus.CANCELLED.value

    retried = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    assert retried.attempt == 1
    assert retried.invocation_id != prepared.invocation_id
    assert retried.dispatchable is True


async def test_platform_reconciliation_job_releases_stale_prepared_reservation(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    _install_fake_llm(monkeypatch, db_session)
    prepared = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None
    invocation.created_at = NOW - gateway_contracts.PREPARED_STALE_AFTER - timedelta(seconds=1)
    await db_session.commit()

    await gateway_jobs.reconciliation_job(session_factory)
    await db_session.refresh(invocation)
    assert invocation.status == AIInvocationStatus.CANCELLED.value
    platform = await db_session.get(
        AIPlatformQuotaPeriod,
        (PERIOD_START, PERIOD_END),
    )
    subject = await db_session.get(
        AISubjectQuotaPeriod,
        (legacy_owner_roots.subject_id, PERIOD_START, PERIOD_END),
    )
    assert platform is not None and subject is not None
    assert platform.reserved_cost_microunits == 0
    assert subject.reserved_cost_microunits == 0


@pytest.mark.parametrize(
    "failure",
    ("missing_root", "missing_quota", "disabled_root", "revoked_actor"),
)
async def test_authorization_or_configuration_failure_has_zero_network(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    failure,
):
    if failure == "missing_quota":
        await _configure_platform(
            db_session,
            legacy_owner_roots,
            include_platform_quota=False,
            include_subject_quota=False,
        )
    elif failure == "disabled_root":
        await _configure_platform(
            db_session,
            legacy_owner_roots,
            status=IntegrationConnectionStatus.DISABLED,
        )
    elif failure == "revoked_actor":
        await _configure_platform(db_session, legacy_owner_roots)
        user = await db_session.get(User, legacy_owner_roots.user_id)
        assert user is not None
        user.status = UserStatus.SUSPENDED.value
        await db_session.commit()
    observations = _install_fake_llm(monkeypatch, db_session)

    with pytest.raises(
        (
            digest_ownership.DigestOwnershipError,
            gateway_contracts.AIGatewayError,
            LegacyOwnershipError,
        )
    ):
        await digest_ownership.prepare_digest(
            db_session,
            actor_username=get_web_config().auth_username,
            invocation_source=AIInvocationSource.WEB,
            on_date=DAY,
        )
    await db_session.rollback()
    assert observations["calls"] == 0
    assert await db_session.scalar(
        select(func.count()).select_from(AIInvocation)
    ) == 0


async def test_reservation_formula_is_bounded_and_low_budget_fails_before_network(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    observations = _install_fake_llm(monkeypatch, db_session)
    prepared = await digest_ownership.prepare_digest(
        db_session,
        actor_username=get_web_config().auth_username,
        invocation_source=AIInvocationSource.WEB,
        on_date=DAY,
    )
    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None
    system = (
        digest_prompt.DIGEST_SYSTEM_EN
        if prepared._lang == "en"
        else digest_prompt.DIGEST_SYSTEM
    )
    expected_units = (
        len(
            (system + "\n" + prepared._prompt).encode("utf-8")
        )
        + digest_ownership._DIGEST_MAX_TOKENS
        + digest_ownership._DIGEST_RESERVATION_OVERHEAD_UNITS
    )
    assert invocation.reserved_units == expected_units
    assert 0 < invocation.reserved_units <= gateway_contracts.MAX_SIGNED_BIGINT
    assert (
        invocation.reserved_cost_microunits
        == digest_ownership._DIGEST_RESERVED_COST_MICROUNITS
    )
    await db_session.rollback()
    platform = await db_session.get(
        AIPlatformQuotaPeriod,
        (PERIOD_START, PERIOD_END),
    )
    subject = await db_session.get(
        AISubjectQuotaPeriod,
        (legacy_owner_roots.subject_id, PERIOD_START, PERIOD_END),
    )
    assert platform is not None and subject is not None
    platform.cost_limit_microunits = (
        digest_ownership._DIGEST_RESERVED_COST_MICROUNITS - 1
    )
    subject.cost_limit_microunits = (
        digest_ownership._DIGEST_RESERVED_COST_MICROUNITS - 1
    )
    await db_session.commit()
    with pytest.raises(gateway_contracts.AIQuotaExceededError):
        await digest_ownership.prepare_digest(
            db_session,
            actor_username=get_web_config().auth_username,
            invocation_source=AIInvocationSource.WEB,
            on_date=DAY,
        )
    await db_session.rollback()
    assert observations["calls"] == 0


async def test_cancel_prepared_digest_releases_both_ledgers(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    observations = _install_fake_llm(monkeypatch, db_session)
    prepared = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    invocation = await digest_generation.cancel_prepared_digest(db_session, prepared)
    assert invocation.status == AIInvocationStatus.CANCELLED.value
    await db_session.commit()
    platform = await db_session.get(
        AIPlatformQuotaPeriod,
        (PERIOD_START, PERIOD_END),
    )
    subject = await db_session.get(
        AISubjectQuotaPeriod,
        (legacy_owner_roots.subject_id, PERIOD_START, PERIOD_END),
    )
    assert platform is not None and subject is not None
    assert platform.reserved_cost_microunits == 0
    assert subject.reserved_cost_microunits == 0
    assert platform.reserved_units == 0
    assert subject.reserved_units == 0
    assert observations["calls"] == 0


async def test_legacy_subject_connection_and_platform_invocation_rows_validate_together(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    legacy = WeeklyDigest(
        subject_id=legacy_owner_roots.subject_id,
        # Stage-3R can prove the historical subject but deliberately does not
        # invent either missing provenance root on a pre-tenancy artifact.
        actor_user_id=None,
        integration_connection_id=None,
        ai_invocation_id=None,
        date=date(2026, 8, 13),
        domain=DOMAIN,
        source=Source.MANUAL.value,
        kind=DigestKind.WEEKLY.value,
        content="Legacy synthetic narrative",
        context_json={"legacy": True},
        model=MODEL,
    )
    db_session.add(legacy)
    await db_session.flush()
    db_session.add(
        OwnershipBackfillCheckpoint(
            phase_key="stage3.retained_artifact.weekly_digests.v1.weekly_digests",
            subject_id=legacy_owner_roots.subject_id,
            status="completed",
            scan_high_watermark_id=legacy.id,
            snapshot_rows=1,
            last_scanned_id=legacy.id,
            scanned_rows=1,
            updated_rows=1,
            unchanged_rows=0,
            data_checksum_before="a" * 64,
            data_checksum_after="b" * 64,
            ownership_checksum_after="b" * 64,
            started_at=NOW,
            updated_at=NOW,
            completed_at=NOW,
        )
    )
    await db_session.commit()
    await _configure_platform(db_session, legacy_owner_roots)
    _install_fake_llm(monkeypatch, db_session)
    prepared = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    lease = await _start_and_commit(db_session, prepared)
    completion = await digest_generation.render_digest(prepared, lease)
    new_row = await digest_generation.persist_digest(db_session, prepared, completion)
    assert new_row is not None
    await db_session.commit()

    owner = await digest_ownership.prepare_digest_owner(
        db_session,
        actor_username=get_web_config().auth_username,
    )
    rows = await digest_queries.list_digests(
        db_session,
        prepared_owner=owner,
    )
    assert {row.content for row in rows} == {
        "Legacy synthetic narrative",
        "Synthetic weekly narrative",
    }


async def test_web_mcp_and_scheduler_boundaries_use_platform_gateway_phases(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    from web.routers import mcp as mcp_router
    from web.routers import reports as reports_router

    await _configure_platform(db_session, legacy_owner_roots)
    observations = _install_fake_llm(
        monkeypatch,
        db_session,
        runtime_secret=SECRET,
    )
    monkeypatch.setattr(digest_ownership, "today_local", lambda: DAY)
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)

    response = await reports_router.generate_digest_now(
        request=SimpleNamespace(headers={}),
        period_days=7,
        db=db_session,
        username=get_web_config().auth_username,
        _rl=None,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/reports?digest=ok"

    mcp_result = await mcp_router.generate_digest_now(period_days=7)
    assert mcp_result["content"] == "Synthetic weekly narrative"
    assert "ai_invocation_id" not in mcp_result

    await run_job_for_every_subject(digest_jobs.digest_job, session_factory)
    rows = list(
        await db_session.scalars(
            select(WeeklyDigest).order_by(WeeklyDigest.id)
        )
    )
    invocations = list(await db_session.scalars(select(AIInvocation)))
    invocation_by_source = {row.source: row for row in invocations}
    assert [row.source for row in rows] == [
        Source.MANUAL.value,
        Source.MCP.value,
        Source.SCHEDULER.value,
    ]
    assert [row.actor_user_id is not None for row in rows] == [True, True, False]
    assert [row.integration_connection_id for row in rows] == [None, None, None]
    assert [row.ai_invocation_id for row in rows] == [
        invocation_by_source[source].id
        for source in (
            AIInvocationSource.WEB.value,
            AIInvocationSource.MCP.value,
            AIInvocationSource.SCHEDULER.value,
        )
    ]
    assert set(invocation_by_source) == {
        AIInvocationSource.WEB.value,
        AIInvocationSource.MCP.value,
        AIInvocationSource.SCHEDULER.value,
    }
    assert observations["calls"] == 3
    assert observations["no_transaction"] == [True, True, True]


async def test_backup_v1_preserves_ai_digests_in_place_and_never_exports_links(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    _install_fake_llm(monkeypatch, db_session)
    prepared = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    lease = await _start_and_commit(db_session, prepared)
    completion = await digest_generation.render_digest(prepared, lease)
    artifact = await digest_generation.persist_digest(
        db_session,
        prepared,
        completion,
    )
    assert artifact is not None
    await db_session.commit()

    assert OWNERSHIP_REGISTRY["weekly_digests"].user_portable is False
    assert "weekly_digests" in _EXCLUDED_TABLES
    snapshot = await export_full(db_session)
    assert "weekly_digests" not in snapshot
    rendered = repr(snapshot)
    assert str(artifact.ai_invocation_id) not in rendered

    await import_full(db_session, snapshot)
    await db_session.commit()
    preserved = await db_session.get(WeeklyDigest, artifact.id)
    assert preserved is not None
    assert preserved.ai_invocation_id == prepared.invocation_id


@pytest.mark.integration
async def test_postgres_concurrent_consumer_start_issues_one_lease_and_artifact(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    observations = _install_fake_llm(monkeypatch, db_session)
    prepared = await _prepare_and_commit(db_session, AIInvocationSource.WEB)
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async def contender():
        async with factory() as session:
            try:
                lease = await digest_generation.start_digest_dispatch(
                    session,
                    prepared,
                    credential_resolver=lambda _reference: SECRET,
                )
                await session.commit()
                return lease
            except gateway_contracts.AIInvocationStateError as exc:
                await session.rollback()
                return exc

    outcomes = await asyncio.gather(contender(), contender())
    leases = [item for item in outcomes if isinstance(item, gateway_contracts.AIDispatchLease)]
    assert len(leases) == 1
    assert sum(
        isinstance(item, gateway_contracts.AIInvocationStateError) for item in outcomes
    ) == 1
    completion = await digest_generation.render_digest(prepared, leases[0])
    async with factory() as session:
        artifact = await digest_generation.persist_digest(
            session,
            prepared,
            completion,
        )
        assert artifact is not None
        await session.commit()
    assert observations["calls"] == 1
    assert await db_session.scalar(
        select(func.count()).select_from(WeeklyDigest)
    ) == 1
