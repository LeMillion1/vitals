"""Platform-funded Daily Brief lifecycle and deterministic fallback contracts."""
from __future__ import annotations

import asyncio
import inspect
import pickle
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.config import load_config as load_runtime_config
from vitals.enums import (
    AIInvocationSource,
    AIInvocationStatus,
    DigestKind,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserRoleName,
    UserStatus,
)
from vitals.integrations.llm_client import LLMCallResult
from vitals.models.ai import AIInvocation, AIPlatformQuotaPeriod, AISubjectQuotaPeriod
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.milestones import WeeklyDigest
from vitals.models.proactive import Notification
from vitals.models.tenancy import IntegrationConnection, PlatformIntegrationConnection
from vitals.services import ai_gateway_service, digest_service, garmin_service
from vitals.services.legacy_ownership import LegacyOwnershipError
from vitals.services.proactive import brief, channels, delivery, inbound
from web.config import get_web_config

pytestmark = pytest.mark.usefixtures("all_modules_on")

DAY = date(2026, 8, 20)
NOW = datetime(2026, 8, 20, 10, tzinfo=UTC)
MODEL = "synthetic/brief-model"
SECRET = "synthetic-platform-secret"
WEB_TOKEN = "brief_web_token_1234567890"


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(ai_gateway_service, "now_utc", lambda: NOW)
    monkeypatch.setattr(brief, "now_utc", lambda: NOW)


async def _configure_platform(session, roots, *, cost_limit=100_000_000):
    root = PlatformIntegrationConnection(
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=f"opaque-{uuid.uuid4().hex}",
        credential_ref="env:VITALS_OPENROUTER_API_KEY",
        status=IntegrationConnectionStatus.ACTIVE.value,
        config_version=1,
        configured_by_user_id=roots.user_id,
    )
    session.add(root)
    session.add(
        AIPlatformQuotaPeriod(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 9, 1),
            cost_limit_microunits=cost_limit,
            unit_limit=1_000_000,
            configured_by_user_id=roots.user_id,
        )
    )
    session.add(
        AISubjectQuotaPeriod(
            subject_id=roots.subject_id,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 9, 1),
            cost_limit_microunits=cost_limit,
            unit_limit=1_000_000,
            configured_by_user_id=roots.user_id,
        )
    )
    await session.commit()
    return root


def _install_runtime(monkeypatch, session, *, behavior="success"):
    config = replace(
        load_runtime_config(),
        openrouter_api_key=SECRET,
        llm_model_brief=MODEL,
        llm_model_digest="synthetic/digest",
    )
    monkeypatch.setattr(brief, "load_config", lambda: config)

    async def context(*_args, **_kwargs):
        return {
            "date": DAY.isoformat(),
            "garmin": {
                "date": DAY.isoformat(),
                "sleep_score": 80,
                "hrv_avg": 61,
                "resting_hr": 52,
                "body_battery_high": 84,
            },
            "day": {"answers": {}, "answered": [], "source": "template"},
        }

    monkeypatch.setattr(brief, "build_context", context)
    observed = {"calls": 0, "no_tx": [], "secret": [], "models": []}

    class FakeLLM:
        def __init__(self, runtime_config):
            self.config = runtime_config

        async def complete_text_with_usage(
            self, _prompt, *, model, system, max_tokens
        ):
            del system, max_tokens
            observed["calls"] += 1
            observed["no_tx"].append(not session.in_transaction())
            observed["secret"].append(self.config.openrouter_api_key == SECRET)
            observed["models"].append(model)
            if behavior == "provider_exception":
                raise RuntimeError("sensitive upstream text")
            return LLMCallResult(
                value="  " if behavior == "blank" else "Synthetic narrative",
                upstream_request_id=" request-1 ",
                model=model,
                input_tokens=12,
                output_tokens=8,
                cost_microunits=None if behavior == "missing_usage" else 50,
            )

    monkeypatch.setattr(brief, "LLMClient", FakeLLM)
    return observed


async def _prepare(session, source):
    prepared = await brief.prepare_brief(
        session,
        actor_username=(
            None if source is AIInvocationSource.SCHEDULER else get_web_config().auth_username
        ),
        invocation_source=source,
        surface=(
            brief.BriefSurface.SCHEDULER
            if source is AIInvocationSource.SCHEDULER
            else brief.BriefSurface.BUILD
        ),
        request_token=None if source is AIInvocationSource.SCHEDULER else WEB_TOKEN,
        on_date=DAY,
    )
    await session.commit()
    assert prepared is not None
    return prepared


def _rotate_runtime_model(monkeypatch, model="synthetic/rotated-brief-model"):
    config = replace(
        load_runtime_config(),
        openrouter_api_key=SECRET,
        llm_model_brief=model,
        llm_model_digest="synthetic/digest",
    )
    monkeypatch.setattr(brief, "load_config", lambda: config)
    return model


async def _rotate_subject_owner(session, roots):
    username = f"replacement-{uuid.uuid4().hex}"
    replacement = User(
        username=username,
        normalized_username=username,
        password_hash="test-only",
        status=UserStatus.ACTIVE.value,
    )
    session.add(replacement)
    await session.flush()
    subject = await session.get(HealthSubject, roots.subject_id)
    assert subject is not None
    subject.owner_user_id = replacement.id
    await session.commit()
    return replacement


def test_product_key_namespace_is_stable_across_policy_and_model_changes(
    monkeypatch,
):
    first = brief._request_key(
        source=AIInvocationSource.WEB,
        surface=brief.BriefSurface.BUILD,
        on_date=DAY,
        request_token=WEB_TOKEN,
    )
    monkeypatch.setattr(brief, "_BRIEF_POLICY_VERSION", "daily-brief:v999")
    second = brief._request_key(
        source=AIInvocationSource.WEB,
        surface=brief.BriefSurface.BUILD,
        on_date=DAY,
        request_token=WEB_TOKEN,
    )
    assert first == second
    assert first.startswith("dbp:v1:")


@pytest.mark.parametrize(
    ("source", "artifact_source", "has_actor"),
    (
        (AIInvocationSource.WEB, Source.MANUAL.value, True),
        (AIInvocationSource.SCHEDULER, Source.SCHEDULER.value, False),
    ),
)
async def test_gateway_flow_has_exact_provenance_and_no_provider_transaction(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    source,
    artifact_source,
    has_actor,
):
    root = await _configure_platform(db_session, legacy_owner_roots)
    observed = _install_runtime(monkeypatch, db_session)
    prepared = await _prepare(db_session, source)
    assert "sleep_score" not in repr(prepared)
    with pytest.raises(TypeError):
        pickle.dumps(prepared)

    lease = await brief.start_brief_dispatch(
        db_session,
        prepared,
        credential_resolver=lambda ref: SECRET,
    )
    await db_session.commit()
    completion = await brief.render_brief(prepared, lease)
    row = await brief.persist_brief(db_session, prepared, completion)
    await db_session.commit()

    assert observed == {
        "calls": 1,
        "no_tx": [True],
        "secret": [True],
        "models": [MODEL],
    }
    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None
    assert invocation.platform_integration_connection_id == root.id
    assert invocation.status == AIInvocationStatus.SUCCEEDED.value
    assert invocation.source == source.value
    assert (invocation.actor_user_id is not None) is has_actor
    assert row.ai_invocation_id == invocation.id
    assert row.integration_connection_id is None
    assert row.source == artifact_source
    assert row.model == MODEL
    assert row.content.endswith("Synthetic narrative")


@pytest.mark.parametrize(
    ("behavior", "status"),
    (
        ("provider_exception", AIInvocationStatus.AMBIGUOUS.value),
        ("blank", AIInvocationStatus.FAILED.value),
        ("missing_usage", AIInvocationStatus.FAILED.value),
    ),
)
async def test_provider_failures_are_one_call_sanitized_and_store_linked_header(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    behavior,
    status,
):
    await _configure_platform(db_session, legacy_owner_roots)
    observed = _install_runtime(monkeypatch, db_session, behavior=behavior)
    prepared = await _prepare(db_session, AIInvocationSource.WEB)
    lease = await brief.start_brief_dispatch(
        db_session, prepared, credential_resolver=lambda ref: SECRET
    )
    await db_session.commit()
    completion = await brief.render_brief(prepared, lease)
    row = await brief.persist_brief(db_session, prepared, completion)
    await db_session.commit()

    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None and invocation.status == status
    assert invocation.error_code in {"provider_unavailable", "invalid_response"}
    assert invocation.charged_cost_microunits == invocation.reserved_cost_microunits
    assert row.ai_invocation_id == invocation.id
    assert row.integration_connection_id is None
    assert row.model is None
    assert "Synthetic narrative" not in row.content
    assert observed["calls"] == 1
    assert "sensitive upstream text" not in repr(completion)

    duplicate = await _prepare(db_session, AIInvocationSource.WEB)
    assert duplicate.existing_artifact_id == row.id
    assert duplicate.dispatchable is False
    assert observed["calls"] == 1


async def test_unconfigured_header_is_deduplicated_by_same_request_key(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    _install_runtime(monkeypatch, db_session)
    first = await _prepare(db_session, AIInvocationSource.WEB)
    assert first.invocation_id is None
    row = await brief.persist_brief(db_session, first, None)
    await db_session.commit()

    second = await _prepare(db_session, AIInvocationSource.WEB)
    assert second.existing_artifact_id == row.id
    existing = await brief.existing_brief_for_prepared(db_session, second)
    await db_session.commit()
    assert existing is not None and existing.id == row.id
    assert len((await db_session.scalars(select(WeeklyDigest))).all()) == 1
    assert (await db_session.scalars(select(AIInvocation))).all() == []


async def test_stale_or_rotated_prepared_request_cancels_to_one_header_only(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    old_root = await _configure_platform(db_session, legacy_owner_roots)
    observed = _install_runtime(monkeypatch, db_session)
    first = await _prepare(db_session, AIInvocationSource.WEB)

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

    duplicate = await _prepare(db_session, AIInvocationSource.WEB)
    assert duplicate.invocation_id == first.invocation_id
    assert duplicate.reservation_status is AIInvocationStatus.CANCELLED
    assert duplicate.dispatchable is False
    row = await brief.persist_brief(db_session, duplicate, None)
    await db_session.commit()
    assert row.ai_invocation_id == first.invocation_id
    assert row.model is None
    assert observed["calls"] == 0
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 1


@pytest.mark.parametrize(
    "source",
    (AIInvocationSource.WEB, AIInvocationSource.SCHEDULER),
)
async def test_model_rotation_cancels_prepared_product_without_second_invocation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    source,
):
    await _configure_platform(db_session, legacy_owner_roots)
    observed = _install_runtime(monkeypatch, db_session)
    first = await _prepare(db_session, source)

    _rotate_runtime_model(monkeypatch)
    duplicate = await _prepare(db_session, source)

    assert duplicate.invocation_id == first.invocation_id
    assert duplicate.reservation_status is AIInvocationStatus.CANCELLED
    assert duplicate.dispatchable is False
    row = await brief.persist_brief(db_session, duplicate, None)
    await db_session.commit()
    assert row.ai_invocation_id == first.invocation_id
    assert row.model is None
    assert row.context_json["_daily_brief_generation"]["model"] == MODEL
    assert observed["calls"] == 0
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 1


async def test_terminal_product_reuses_frozen_model_after_configuration_rotation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    observed = _install_runtime(monkeypatch, db_session)
    first = await _prepare(db_session, AIInvocationSource.WEB)
    lease = await brief.start_brief_dispatch(
        db_session,
        first,
        credential_resolver=lambda ref: SECRET,
    )
    await db_session.commit()
    completion = await brief.render_brief(first, lease)
    artifact = await brief.persist_brief(db_session, first, completion)
    await db_session.commit()

    _rotate_runtime_model(monkeypatch)
    duplicate = await _prepare(db_session, AIInvocationSource.WEB)
    existing = await brief.existing_brief_for_prepared(db_session, duplicate)
    await db_session.commit()

    assert duplicate.invocation_id == first.invocation_id
    assert duplicate.existing_artifact_id == artifact.id
    assert existing is not None and existing.id == artifact.id
    assert existing.model == MODEL
    assert observed["calls"] == 1
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 1


async def test_stale_prepared_request_is_cancelled_not_replaced(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    observed = _install_runtime(monkeypatch, db_session)
    first = await _prepare(db_session, AIInvocationSource.WEB)
    invocation = await db_session.get(AIInvocation, first.invocation_id)
    assert invocation is not None
    invocation.created_at = NOW - ai_gateway_service.PREPARED_STALE_AFTER - timedelta(
        seconds=1
    )
    await db_session.commit()

    duplicate = await _prepare(db_session, AIInvocationSource.WEB)
    assert duplicate.invocation_id == first.invocation_id
    assert duplicate.reservation_status is AIInvocationStatus.CANCELLED
    row = await brief.persist_brief(db_session, duplicate, None)
    await db_session.commit()
    assert row.ai_invocation_id == first.invocation_id
    assert observed["calls"] == 0
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 1


async def test_finalize_and_artifact_rollback_retries_same_completion(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    observed = _install_runtime(monkeypatch, db_session)
    prepared = await _prepare(db_session, AIInvocationSource.WEB)
    lease = await brief.start_brief_dispatch(
        db_session, prepared, credential_resolver=lambda ref: SECRET
    )
    await db_session.commit()
    completion = await brief.render_brief(prepared, lease)

    first = await brief.persist_brief(db_session, prepared, completion)
    assert first.ai_invocation_id == prepared.invocation_id
    await db_session.rollback()
    assert completion.payload is not None

    second = await brief.persist_brief(db_session, prepared, completion)
    await db_session.commit()
    assert second.ai_invocation_id == prepared.invocation_id
    assert completion.payload is None
    assert observed["calls"] == 1
    assert await db_session.scalar(select(func.count()).select_from(WeeklyDigest)) == 1


async def test_t1_commit_ambiguity_reconciles_same_key_without_second_invocation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    from web.routers import reports

    await _configure_platform(db_session, legacy_owner_roots)
    observed = _install_runtime(monkeypatch, db_session)
    monkeypatch.setattr(brief, "now_utc", lambda: datetime.now(UTC))
    real_commit = db_session.commit
    commits = 0

    async def ambiguous_first_commit():
        nonlocal commits
        commits += 1
        await real_commit()
        if commits == 1:
            raise RuntimeError("synthetic ambiguous T1")

    monkeypatch.setattr(db_session, "commit", ambiguous_first_commit)
    row, outcome = await reports._run_brief_generation(
        db_session,
        actor_username=get_web_config().auth_username,
        surface=brief.BriefSurface.BUILD,
        request_token=WEB_TOKEN,
        on_date=DAY,
    )
    invocation = (await db_session.scalars(select(AIInvocation))).one()
    assert row is not None and outcome == "ok", (
        observed,
        invocation.status,
        invocation.error_code,
    )
    assert observed["calls"] == 1
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 1


async def test_t2_commit_ambiguity_never_dispatches_and_reconciles_to_header(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    from web.routers import reports

    await _configure_platform(db_session, legacy_owner_roots)
    observed = _install_runtime(monkeypatch, db_session)
    real_commit = db_session.commit
    commits = 0

    async def ambiguous_second_commit():
        nonlocal commits
        commits += 1
        await real_commit()
        if commits == 2:
            raise RuntimeError("synthetic ambiguous T2")

    monkeypatch.setattr(db_session, "commit", ambiguous_second_commit)
    row, outcome = await reports._run_brief_generation(
        db_session,
        actor_username=get_web_config().auth_username,
        surface=brief.BriefSurface.BUILD,
        request_token=WEB_TOKEN,
        on_date=DAY,
    )
    assert row is None and outcome == "pending"
    assert observed["calls"] == 0
    invocation = (await db_session.scalars(select(AIInvocation))).one()
    assert invocation.status == AIInvocationStatus.DISPATCHING.value

    await ai_gateway_service.reconcile_stale_dispatches(
        db_session,
        stale_before=NOW + timedelta(seconds=1),
    )
    await real_commit()
    row, outcome = await reports._run_brief_generation(
        db_session,
        actor_username=get_web_config().auth_username,
        surface=brief.BriefSurface.BUILD,
        request_token=WEB_TOKEN,
        on_date=DAY,
    )
    assert row is not None and outcome == "header"
    assert row.ai_invocation_id == invocation.id
    assert row.model is None
    assert observed["calls"] == 0


async def test_paid_completion_finalizes_after_actor_suspension(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    _install_runtime(monkeypatch, db_session)
    prepared = await _prepare(db_session, AIInvocationSource.WEB)
    lease = await brief.start_brief_dispatch(
        db_session, prepared, credential_resolver=lambda ref: SECRET
    )
    await db_session.commit()
    completion = await brief.render_brief(prepared, lease)

    actor = await db_session.get(User, legacy_owner_roots.user_id)
    assert actor is not None
    actor.status = UserStatus.SUSPENDED.value
    await db_session.commit()

    row = await brief.persist_brief(db_session, prepared, completion)
    await db_session.commit()
    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None
    assert invocation.status == AIInvocationStatus.SUCCEEDED.value
    assert row.ai_invocation_id == invocation.id
    with pytest.raises((digest_service.DigestOwnershipError, LegacyOwnershipError)):
        await digest_service.prepare_digest_owner(
            db_session,
            actor_username=get_web_config().auth_username,
        )


async def test_scheduler_start_revalidates_frozen_owner_before_platform_spend(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    observed = _install_runtime(monkeypatch, db_session)
    prepared = await _prepare(db_session, AIInvocationSource.SCHEDULER)

    owner = await db_session.get(User, legacy_owner_roots.user_id)
    assert owner is not None
    owner.status = UserStatus.SUSPENDED.value
    await db_session.commit()

    with pytest.raises(digest_service.DigestOwnershipError):
        await brief.start_brief_dispatch(
            db_session,
            prepared,
            credential_resolver=lambda ref: SECRET,
        )
    await db_session.rollback()

    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None
    assert invocation.status == AIInvocationStatus.PREPARED.value
    assert invocation.charged_cost_microunits == 0
    assert invocation.charged_units == 0
    assert observed["calls"] == 0


async def test_scheduler_owner_rotation_blocks_invocation_null_fallback_write(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    _install_runtime(monkeypatch, db_session)
    prepared = await _prepare(db_session, AIInvocationSource.SCHEDULER)
    assert prepared.invocation_id is None
    await _rotate_subject_owner(db_session, legacy_owner_roots)

    with pytest.raises(brief.BriefOwnershipError):
        await brief.persist_brief(db_session, prepared, None)
    await db_session.rollback()
    assert await db_session.scalar(select(func.count()).select_from(WeeklyDigest)) == 0


async def test_scheduler_owner_rotation_blocks_stale_cancellation_and_read(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    _install_runtime(monkeypatch, db_session)
    prepared = await _prepare(db_session, AIInvocationSource.SCHEDULER)
    await _rotate_subject_owner(db_session, legacy_owner_roots)

    with pytest.raises(brief.BriefOwnershipError):
        await brief.cancel_and_persist_header_brief(db_session, prepared)
    await db_session.rollback()
    with pytest.raises(brief.BriefOwnershipError):
        await brief.existing_brief_for_prepared(db_session, prepared)
    await db_session.rollback()

    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None
    assert invocation.status == AIInvocationStatus.PREPARED.value
    assert await db_session.scalar(select(func.count()).select_from(WeeklyDigest)) == 0


async def test_scheduler_owner_rotation_blocks_existing_artifact_read(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    _install_runtime(monkeypatch, db_session)
    prepared = await _prepare(db_session, AIInvocationSource.SCHEDULER)
    artifact = await brief.cancel_and_persist_header_brief(db_session, prepared)
    artifact_id = artifact.id
    await db_session.commit()
    await _rotate_subject_owner(db_session, legacy_owner_roots)

    with pytest.raises(brief.BriefOwnershipError):
        await brief.existing_brief_for_prepared(db_session, prepared)
    await db_session.rollback()
    stored = await db_session.get(WeeklyDigest, artifact_id)
    assert stored is not None


async def test_redacted_availability_requires_platform_root_periods_and_credential(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    _install_runtime(monkeypatch, db_session)
    unavailable = await brief.project_ai_availability(
        db_session,
        actor_username=get_web_config().auth_username,
    )
    assert unavailable.available is False
    await db_session.rollback()
    await _configure_platform(db_session, legacy_owner_roots)
    available = await brief.project_ai_availability(
        db_session,
        actor_username=get_web_config().auth_username,
    )
    assert available.available is True
    assert not hasattr(available, "subject_id")
    assert SECRET not in repr(available)


async def test_platform_superadmin_role_alone_cannot_prepare_subject_phi(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    _install_runtime(monkeypatch, db_session)
    admin = User(
        username="platform-only",
        normalized_username="platform-only",
        password_hash="test-only",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(admin)
    await db_session.flush()
    db_session.add(
        UserRole(user_id=admin.id, role=UserRoleName.PLATFORM_SUPERADMIN.value)
    )
    await db_session.commit()

    with pytest.raises(LegacyOwnershipError):
        await brief.prepare_brief(
            db_session,
            actor_username=admin.username,
            invocation_source=AIInvocationSource.WEB,
            surface=brief.BriefSurface.BUILD,
            request_token=WEB_TOKEN,
            on_date=DAY,
        )
    assert (await db_session.scalars(select(AIInvocation))).all() == []


async def test_legacy_subject_connection_rows_remain_readable(
    db_session,
    legacy_owner_roots,
):
    connection_id = await db_session.scalar(
        select(IntegrationConnection.id).where(
            IntegrationConnection.subject_id == legacy_owner_roots.subject_id,
            IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
        )
    )
    assert connection_id is not None
    legacy = WeeklyDigest(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        integration_connection_id=connection_id,
        ai_invocation_id=None,
        date=DAY,
        domain="milestones",
        source=Source.MANUAL.value,
        kind=DigestKind.DAILY_BRIEF.value,
        content="legacy narrative",
        context_json={"legacy": True},
        model="legacy/model",
    )
    db_session.add(legacy)
    await db_session.commit()
    owner = await digest_service.prepare_digest_owner(
        db_session,
        actor_username=get_web_config().auth_username,
    )
    row = await digest_service.latest_digest(
        db_session,
        kind=DigestKind.DAILY_BRIEF.value,
        prepared_owner=owner,
    )
    assert row is not None and row.id == legacy.id


async def test_inbound_fails_closed_on_newer_corrupt_invocation_artifact(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    _install_runtime(monkeypatch, db_session, behavior="blank")
    prepared = await _prepare(db_session, AIInvocationSource.SCHEDULER)
    lease = await brief.start_brief_dispatch(
        db_session, prepared, credential_resolver=lambda ref: SECRET
    )
    await db_session.commit()
    completion = await brief.render_brief(prepared, lease)
    corrupt = await brief.persist_brief(db_session, prepared, completion)
    await db_session.commit()
    assert corrupt.model is None

    # A failed invocation may only back a model-null deterministic header. The
    # inbound reader must reject this latest corrupt row, not silently fall back.
    corrupt.model = MODEL
    await db_session.commit()
    ownership = await channels.resolve_legacy_channel_ownership(
        db_session,
        actor_username=None,
    )
    with pytest.raises(digest_service.DigestOwnershipError):
        await inbound._day_facts(db_session, ownership=ownership)


async def test_scheduler_uses_platform_gateway_without_subject_openrouter_root(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    observed = _install_runtime(monkeypatch, db_session)
    legacy_ai = await db_session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == legacy_owner_roots.subject_id,
            IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
        )
    )
    assert legacy_ai is not None
    await db_session.delete(legacy_ai)
    await db_session.commit()

    class Notifier:
        channel = IntegrationProvider.TELEGRAM.value

        def __init__(self):
            self.sent = []

        async def send(self, text, *, buttons=None, reply_to=None):
            self.sent.append(text)
            return "903"

        async def edit(self, message_id, text, *, buttons=None):
            return None

        async def answer_callback(self, callback_id, text=""):
            return None

    notifier = Notifier()
    parser_calls = 0

    async def no_sync(*_args, **_kwargs):
        return None

    async def no_reparse(*_args, **_kwargs):
        nonlocal parser_calls
        parser_calls += 1
        return []

    async def build_bound(session, ownership, **kwargs):
        del session, kwargs
        notifier.binding = channels.DeliveryEndpointBinding(
            subject_id=ownership.subject_id,
            recipient_user_id=ownership.recipient_user_id,
            integration_connection_id=ownership.connection_id,
            channel=notifier.channel,
        )
        return notifier

    def resolve_bound(binding, credential_ref, **kwargs):
        del credential_ref, kwargs
        notifier.binding = binding
        return notifier

    monkeypatch.setattr(brief, "today_local", lambda: DAY)
    monkeypatch.setattr(channels, "build_legacy_bound_notifier", build_bound)
    monkeypatch.setattr(channels, "resolve_legacy_bound_notifier", resolve_bound)
    monkeypatch.setattr(garmin_service, "sync_job", no_sync)
    monkeypatch.setattr(inbound, "reparse_pending", no_reparse)

    await brief.brief_job(session_factory)

    # Signal recovery is now independently platform-funded and no longer
    # disappears merely because the historical subject OpenRouter root is gone.
    assert parser_calls == 1
    assert len(notifier.sent) == 1
    assert observed["calls"] == 1
    invocation = (await db_session.scalars(select(AIInvocation))).one()
    artifact = (await db_session.scalars(select(WeeklyDigest))).one()
    assert invocation.source == AIInvocationSource.SCHEDULER.value
    assert invocation.actor_user_id is None
    assert artifact.ai_invocation_id == invocation.id
    assert artifact.integration_connection_id is None


async def test_web_tokens_are_bounded_and_request_date_is_frozen_once(
    auth_client,
    db_session,
    monkeypatch,
):
    from web.routers import reports

    _install_runtime(monkeypatch, db_session)
    for invalid_token in ("too_short", "x" * 97, "!" * 22):
        invalid = await auth_client.post(
            "/reports/brief",
            data={"request_token": invalid_token},
        )
        assert invalid.status_code == 422
    assert (await db_session.scalars(select(WeeklyDigest))).all() == []

    samples = iter((DAY, DAY + timedelta(days=1)))
    monkeypatch.setattr(reports, "today_local", lambda: next(samples))
    response = await auth_client.post(
        "/reports/brief",
        data={"request_token": WEB_TOKEN},
    )
    assert response.status_code == 303
    row = (await db_session.scalars(select(WeeklyDigest))).one()
    assert row.date == DAY


async def test_test_send_uses_one_frozen_date_for_artifact_and_delivery_key(
    auth_client,
    db_session,
    monkeypatch,
):
    from web.routers import reports

    _install_runtime(monkeypatch, db_session)

    class Notifier:
        channel = IntegrationProvider.TELEGRAM.value

        async def send(self, text, *, buttons=None, reply_to=None):
            return "902"

        async def edit(self, message_id, text, *, buttons=None):
            return None

        async def answer_callback(self, callback_id, text=""):
            return None

    notifier = Notifier()

    async def build_bound(session, ownership, **kwargs):
        del session, kwargs
        notifier.binding = channels.DeliveryEndpointBinding(
            subject_id=ownership.subject_id,
            recipient_user_id=ownership.recipient_user_id,
            integration_connection_id=ownership.connection_id,
            channel=notifier.channel,
        )
        return notifier

    def resolve_bound(binding, credential_ref, **kwargs):
        del credential_ref, kwargs
        notifier.binding = binding
        return notifier

    monkeypatch.setattr(channels, "build_legacy_bound_notifier", build_bound)
    monkeypatch.setattr(channels, "resolve_legacy_bound_notifier", resolve_bound)
    samples = iter((DAY, DAY + timedelta(days=1)))
    monkeypatch.setattr(reports, "today_local", lambda: next(samples))
    response = await auth_client.post(
        "/reports/brief/test",
        data={"request_token": WEB_TOKEN},
    )
    assert response.status_code == 303
    artifact = (await db_session.scalars(select(WeeklyDigest))).one()
    notification = (await db_session.scalars(select(Notification))).one()
    assert artifact.date == DAY
    assert notification.dedupe_key == delivery.make_delivery_idempotency_key(
        "brief-test",
        DAY,
        WEB_TOKEN,
    )


def test_identity_production_callsites_cannot_reach_legacy_complete_text():
    from web.routers import reports

    assert "generate_brief(" not in inspect.getsource(reports)
    assert "generate_brief(" not in inspect.getsource(brief.brief_job)
    assert "complete_text(" not in inspect.getsource(brief.render_brief)
    assert "complete_text_with_usage(" in inspect.getsource(brief.render_brief)
    source = inspect.getsource(brief.generate_brief)
    assert "HealthSubject.id" in source
    assert "phased gateway APIs" in source

    platform_flow = "\n".join(
        inspect.getsource(function)
        for function in (
            brief.prepare_brief,
            brief.start_brief_dispatch,
            brief.render_brief,
            brief.persist_brief,
            brief._run_scheduled_brief_generation,
            reports._run_brief_generation,
        )
    )
    assert "IntegrationConnection" not in platform_flow
    assert "resolve_legacy_ownership_context" not in platform_flow


@pytest.mark.integration
async def test_postgres_same_token_concurrent_start_gets_one_lease_and_artifact(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    # PostgreSQL owns server-side created_at; keep the service clock aligned so
    # a freshly inserted reservation is not mistaken for a stale fixture row.
    live_now = datetime.now(UTC)
    monkeypatch.setattr(ai_gateway_service, "now_utc", lambda: live_now)
    monkeypatch.setattr(brief, "now_utc", lambda: live_now)
    await _configure_platform(db_session, legacy_owner_roots)
    observed = _install_runtime(monkeypatch, db_session)
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async def prepare_one():
        async with factory() as session:
            prepared = await brief.prepare_brief(
                session,
                actor_username=get_web_config().auth_username,
                invocation_source=AIInvocationSource.WEB,
                surface=brief.BriefSurface.BUILD,
                request_token=WEB_TOKEN,
                on_date=DAY,
            )
            await session.commit()
            return prepared

    first, second = await asyncio.gather(prepare_one(), prepare_one())
    assert first is not None and second is not None
    assert first.invocation_id == second.invocation_id

    async def start_one(prepared):
        async with factory() as session:
            try:
                lease = await brief.start_brief_dispatch(
                    session,
                    prepared,
                    credential_resolver=lambda ref: SECRET,
                )
                await session.commit()
                return lease
            except ai_gateway_service.AIInvocationStateError:
                await session.rollback()
                return None

    leases = await asyncio.gather(start_one(first), start_one(second))
    leases = [lease for lease in leases if lease is not None]
    assert len(leases) == 1
    completion = await brief.render_brief(first, leases[0])
    async with factory() as session:
        row = await brief.persist_brief(session, first, completion)
        await session.commit()
        assert row.ai_invocation_id == first.invocation_id
    assert observed["calls"] == 1


@pytest.mark.integration
async def test_postgres_invocation_null_fallback_is_concurrently_deduplicated(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    # No platform root/period: both callers receive deterministic header powers.
    _install_runtime(monkeypatch, db_session)
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async def build_one():
        async with factory() as session:
            prepared = await brief.prepare_brief(
                session,
                actor_username=get_web_config().auth_username,
                invocation_source=AIInvocationSource.WEB,
                surface=brief.BriefSurface.BUILD,
                request_token=WEB_TOKEN,
                on_date=DAY,
            )
            await session.commit()
        assert prepared is not None
        async with factory() as session:
            row = await brief.persist_brief(session, prepared, None)
            await session.commit()
            return row.id

    ids = await asyncio.gather(build_one(), build_one())
    assert ids[0] == ids[1]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(WeeklyDigest)) == 1
