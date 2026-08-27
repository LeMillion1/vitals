"""Hard quota, authorization, and transaction seams for platform-funded AI."""
from __future__ import annotations

from vitals.services.ai_gateway import config as gateway_config
from vitals.services.ai_gateway import contracts as gateway_contracts
from vitals.services.ai_gateway import dispatch as gateway_dispatch
from vitals.services.ai_gateway import invocations as gateway_invocations
from vitals.services.ai_gateway import quota as gateway_quota
from vitals.services.ai_gateway import reconciliation as gateway_reconciliation

import asyncio
import pickle
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.ai import (
    AIInvocation,
    AIPlatformQuotaPeriod,
    AISubjectQuotaPeriod,
)
from vitals.models.identity import User
from vitals.models.tenancy import PlatformIntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import platform_admin_service
from vitals.persistence import transactions as transaction_outcome
from vitals.services.identity_service import assign_role, change_user_status
from web.config import get_web_config

FIXED_NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 9, 1)


def _identity(roots) -> WriteIdentity:
    return WriteIdentity(
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id,
    )


async def _prepared_admin(session):
    return await platform_admin_service.prepare_platform_admin(
        session,
        actor_username=get_web_config().auth_username,
    )


async def _configure(
    session,
    roots,
    *,
    platform_cost: int = 10_000,
    platform_units: int = 10_000,
    subject_cost: int = 10_000,
    subject_units: int = 10_000,
):
    prepared = await _prepared_admin(session)
    root = await gateway_config.create_gateway(
        session,
        prepared=prepared,
        external_account_discriminator="opaque-platform-v1",
        credential_ref="env:VITALS_OPENROUTER_API_KEY",
    )
    await gateway_quota.configure_platform_quota_period(
        session,
        prepared=prepared,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        cost_limit_microunits=platform_cost,
        unit_limit=platform_units,
    )
    await gateway_quota.configure_subject_quota_period(
        session,
        prepared=prepared,
        subject_id=roots.subject_id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        cost_limit_microunits=subject_cost,
        unit_limit=subject_units,
    )
    await session.commit()
    return root


async def _reserve(session, roots, *, key="opaque-call-1", cost=100, units=500):
    return await gateway_invocations.reserve_ai_invocation(
        session,
        identity=_identity(roots),
        purpose=AIInvocationPurpose.WEEKLY_DIGEST,
        source=AIInvocationSource.WEB,
        model="synthetic/model-v1",
        idempotency_key=key,
        reserved_cost_microunits=cost,
        reserved_units=units,
    )


@pytest.fixture(autouse=True)
def _fixed_utc(monkeypatch):
    for module in (
        gateway_config,
        gateway_dispatch,
        gateway_invocations,
        gateway_reconciliation,
    ):
        monkeypatch.setattr(module, "now_utc", lambda: FIXED_NOW)


async def test_dispatch_has_no_db_transaction_and_persists_only_sanitized_usage(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(db_session, legacy_owner_roots)
    await db_session.commit()

    lease = await gateway_dispatch.start_ai_dispatch(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
        credential_resolver=lambda ref: (
            "synthetic-secret" if ref == "env:VITALS_OPENROUTER_API_KEY" else None
        ),
    )
    assert "synthetic-secret" not in repr(lease)
    with pytest.raises(TypeError):
        pickle.dumps(lease)
    with pytest.raises(gateway_contracts.AICapabilityError):
        await gateway_dispatch.dispatch_ai(
            lease,
            provider_call=lambda _request: None,  # type: ignore[arg-type]
            usage_extractor=lambda _result: gateway_contracts.SanitizedAIUsage(),
        )
    await db_session.commit()

    transaction_states: list[bool] = []
    payload = {"synthetic_health_payload": "memory-only"}
    provider_requests = []

    async def provider_call(request):
        provider_requests.append(request)
        transaction_states.append(db_session.in_transaction())
        assert request.credential == "synthetic-secret"
        assert "synthetic-secret" not in repr(request)
        return payload

    completion = await gateway_dispatch.dispatch_ai(
        lease,
        provider_call=provider_call,
        usage_extractor=lambda _result: gateway_contracts.SanitizedAIUsage(
            upstream_request_id="opaque-upstream-1",
            input_tokens=100,
            output_tokens=200,
            cost_microunits=80,
        ),
    )
    assert transaction_states == [False]
    assert lease._credential is None
    assert lease._session is None
    assert provider_requests[0]._credential is None
    assert completion.payload is payload
    assert "memory-only" not in repr(completion)
    with pytest.raises(TypeError):
        pickle.dumps(completion)
    with pytest.raises(gateway_contracts.AICapabilityError):
        await gateway_dispatch.dispatch_ai(
            lease,
            provider_call=provider_call,
            usage_extractor=lambda _result: gateway_contracts.SanitizedAIUsage(),
        )

    invocation = await gateway_dispatch.finalize_ai_invocation(
        db_session,
        completion=completion,
    )
    assert invocation.status == AIInvocationStatus.SUCCEEDED.value
    assert invocation.upstream_request_id == "opaque-upstream-1"
    assert invocation.cost_microunits == 80
    assert not hasattr(invocation, "payload")
    with pytest.raises(gateway_contracts.AICapabilityError):
        await gateway_dispatch.finalize_ai_invocation(db_session, completion=completion)
    await db_session.rollback()
    assert completion.payload is payload

    retried = await gateway_dispatch.finalize_ai_invocation(
        db_session,
        completion=completion,
    )
    assert retried.status == AIInvocationStatus.SUCCEEDED.value
    await db_session.commit()
    assert completion.payload is None
    with pytest.raises(gateway_contracts.AICapabilityError):
        await gateway_dispatch.finalize_ai_invocation(db_session, completion=completion)


async def test_savepoint_commit_cannot_arm_dispatch_before_outer_commit(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(
        db_session,
        legacy_owner_roots,
        key="savepoint-dispatch",
    )
    await db_session.commit()

    lease = await gateway_dispatch.start_ai_dispatch(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
        credential_resolver=lambda _ref: "synthetic-secret",
    )
    nested = await db_session.begin_nested()
    await nested.commit()

    async def provider_call(_request):
        raise AssertionError("provider must not run before the root commits")

    with pytest.raises(gateway_contracts.AICapabilityError):
        await gateway_dispatch.dispatch_ai(
            lease,
            provider_call=provider_call,
            usage_extractor=lambda _result: gateway_contracts.SanitizedAIUsage(),
        )
    await db_session.rollback()
    with pytest.raises(gateway_contracts.AICapabilityError):
        await gateway_dispatch.dispatch_ai(
            lease,
            provider_call=provider_call,
            usage_extractor=lambda _result: gateway_contracts.SanitizedAIUsage(),
        )

    invocation = await db_session.get(AIInvocation, reservation.invocation_id)
    assert invocation.status == AIInvocationStatus.PREPARED.value
    assert lease._credential is None


async def test_t3_savepoint_commit_then_outer_rollback_keeps_payload_retryable(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(
        db_session,
        legacy_owner_roots,
        key="savepoint-finalize",
    )
    await db_session.commit()
    lease = await gateway_dispatch.start_ai_dispatch(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
        credential_resolver=lambda _ref: "synthetic-secret",
    )
    await db_session.commit()

    payload = {"health": "memory-only"}

    async def provider_call(_request):
        return payload

    completion = await gateway_dispatch.dispatch_ai(
        lease,
        provider_call=provider_call,
        usage_extractor=lambda _result: gateway_contracts.SanitizedAIUsage(
            input_tokens=1,
            output_tokens=1,
            cost_microunits=1,
        ),
    )
    await gateway_dispatch.finalize_ai_invocation(db_session, completion=completion)
    nested = await db_session.begin_nested()
    await nested.commit()
    assert completion.payload is payload
    await db_session.rollback()
    assert completion.payload is payload

    await gateway_dispatch.finalize_ai_invocation(db_session, completion=completion)
    await db_session.commit()
    assert completion.payload is None
    assert transaction_outcome.pending_root_transaction_outcomes(db_session) == 0


async def test_session_close_invalidates_uncommitted_ai_lease(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(
        db_session,
        legacy_owner_roots,
        key="close-dispatch",
    )
    await db_session.commit()
    lease = await gateway_dispatch.start_ai_dispatch(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
        credential_resolver=lambda _ref: "synthetic-secret",
    )
    await db_session.close()

    with pytest.raises(gateway_contracts.AICapabilityError):
        await gateway_dispatch.dispatch_ai(
            lease,
            provider_call=lambda _request: None,  # type: ignore[arg-type]
            usage_extractor=lambda _result: gateway_contracts.SanitizedAIUsage(),
        )
    assert lease._credential is None


async def test_session_close_rolls_back_ai_finalization_and_allows_retry(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(
        db_session,
        legacy_owner_roots,
        key="close-finalize",
    )
    await db_session.commit()
    lease = await gateway_dispatch.start_ai_dispatch(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
        credential_resolver=lambda _ref: "synthetic-secret",
    )
    await db_session.commit()
    payload = {"health": "retry-only"}

    async def provider_call(_request):
        return payload

    completion = await gateway_dispatch.dispatch_ai(
        lease,
        provider_call=provider_call,
        usage_extractor=lambda _result: gateway_contracts.SanitizedAIUsage(
            input_tokens=1,
            output_tokens=1,
            cost_microunits=1,
        ),
    )
    await gateway_dispatch.finalize_ai_invocation(db_session, completion=completion)
    await db_session.close()
    assert completion.payload is payload

    await gateway_dispatch.finalize_ai_invocation(db_session, completion=completion)
    await db_session.commit()
    assert completion.payload is None


async def test_missing_budget_fails_before_any_dispatch_surface(
    db_session,
    legacy_owner_roots,
):
    prepared = await _prepared_admin(db_session)
    await gateway_config.create_gateway(
        db_session,
        prepared=prepared,
        external_account_discriminator="opaque-no-budget",
        credential_ref="env:VITALS_OPENROUTER_API_KEY",
    )
    await db_session.commit()

    with pytest.raises(gateway_contracts.AIGatewayConfigurationError, match="exactly one"):
        await _reserve(db_session, legacy_owner_roots)
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 0


async def test_duplicate_terminal_reservation_is_non_dispatchable(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    first = await _reserve(db_session, legacy_owner_roots)
    await db_session.commit()
    lease = await gateway_dispatch.start_ai_dispatch(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=first.invocation_id,
        credential_resolver=lambda _ref: "synthetic-secret",
    )
    await db_session.commit()
    completion = await gateway_dispatch.dispatch_ai(
        lease,
        provider_call=lambda _request: asyncio.sleep(0, result="ok"),
        usage_extractor=lambda _result: gateway_contracts.SanitizedAIUsage(
            input_tokens=1,
            output_tokens=1,
            cost_microunits=1,
        ),
    )
    await gateway_dispatch.finalize_ai_invocation(db_session, completion=completion)
    await db_session.commit()

    duplicate = await _reserve(db_session, legacy_owner_roots)
    assert duplicate.invocation_id == first.invocation_id
    assert duplicate.status is AIInvocationStatus.SUCCEEDED
    assert duplicate.created is False
    assert duplicate.dispatchable is False


async def test_revoked_owner_and_rotated_root_fail_before_credential_resolution(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    revoked = await _reserve(db_session, legacy_owner_roots, key="revoked")
    rotated = await gateway_invocations.reserve_ai_invocation(
        db_session,
        identity=WriteIdentity(legacy_owner_roots.subject_id, None),
        purpose=AIInvocationPurpose.WEEKLY_DIGEST,
        source=AIInvocationSource.SCHEDULER,
        model="synthetic/model-v1",
        idempotency_key="rotated",
        reserved_cost_microunits=100,
        reserved_units=500,
    )
    await db_session.commit()

    second_admin = User(
        username="Second Admin",
        normalized_username="second admin",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(second_admin)
    await db_session.flush()
    await assign_role(
        db_session,
        user_id=second_admin.id,
        role=UserRoleName.PLATFORM_SUPERADMIN,
        assigned_by_user_id=legacy_owner_roots.user_id,
    )
    await change_user_status(
        db_session,
        user_id=legacy_owner_roots.user_id,
        new_status=UserStatus.SUSPENDED,
        actor_user_id=second_admin.id,
    )
    await db_session.commit()
    resolver_calls = 0

    def resolver(_ref):
        nonlocal resolver_calls
        resolver_calls += 1
        return "synthetic-secret"

    with pytest.raises(gateway_contracts.AIGatewayAuthorizationError):
        await gateway_dispatch.start_ai_dispatch(
            db_session,
            identity=_identity(legacy_owner_roots),
            invocation_id=revoked.invocation_id,
            credential_resolver=resolver,
        )
    await db_session.rollback()
    assert resolver_calls == 0

    # System authorization is still exact-S but independent of the suspended
    # human. Rotate the root under the surviving platform admin.
    prepared = await platform_admin_service.prepare_platform_admin(
        db_session,
        actor_username="Second Admin",
    )
    await gateway_config.rotate_gateway(
        db_session,
        prepared=prepared,
        external_account_discriminator="opaque-platform-v2",
        credential_ref="legacy_env:openrouter",
    )
    await db_session.commit()
    with pytest.raises(gateway_contracts.AIGatewayConfigurationError):
        await gateway_dispatch.start_ai_dispatch(
            db_session,
            identity=WriteIdentity(legacy_owner_roots.subject_id, None),
            invocation_id=rotated.invocation_id,
            credential_resolver=resolver,
        )
    assert resolver_calls == 0

    disabled = await gateway_invocations.reserve_ai_invocation(
        db_session,
        identity=WriteIdentity(legacy_owner_roots.subject_id, None),
        purpose=AIInvocationPurpose.WEEKLY_DIGEST,
        source=AIInvocationSource.SCHEDULER,
        model="synthetic/model-v2",
        idempotency_key="disabled",
        reserved_cost_microunits=100,
        reserved_units=500,
    )
    await db_session.commit()
    prepared = await platform_admin_service.prepare_platform_admin(
        db_session,
        actor_username="Second Admin",
    )
    await gateway_config.disable_gateway(db_session, prepared=prepared)
    await db_session.commit()
    with pytest.raises(gateway_contracts.AIGatewayConfigurationError):
        await gateway_dispatch.start_ai_dispatch(
            db_session,
            identity=WriteIdentity(legacy_owner_roots.subject_id, None),
            invocation_id=disabled.invocation_id,
            credential_resolver=resolver,
        )
    assert resolver_calls == 0


async def test_cancel_releases_both_reservations_before_dispatch(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(db_session, legacy_owner_roots)
    invocation = await gateway_dispatch.cancel_reserved_ai_invocation(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
    )
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
    assert platform.charged_cost_microunits == 0
    assert subject.charged_cost_microunits == 0


async def test_provider_exception_is_sanitized_and_fully_charged(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(db_session, legacy_owner_roots)
    await db_session.commit()
    lease = await gateway_dispatch.start_ai_dispatch(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
        credential_resolver=lambda _ref: "synthetic-secret",
    )
    await db_session.commit()

    async def provider_call(_request):
        raise RuntimeError("sensitive upstream exception text")

    completion = await gateway_dispatch.dispatch_ai(
        lease,
        provider_call=provider_call,
        usage_extractor=lambda _result: gateway_contracts.SanitizedAIUsage(),
    )
    assert completion.status is AIInvocationStatus.AMBIGUOUS
    assert "sensitive" not in repr(completion)
    invocation = await gateway_dispatch.finalize_ai_invocation(
        db_session,
        completion=completion,
    )
    assert invocation.error_code == AIInvocationErrorCode.PROVIDER_UNAVAILABLE.value
    assert invocation.charged_cost_microunits == 100
    assert invocation.cost_microunits == 0


async def test_rolled_back_dispatch_lease_wipes_secret_and_cannot_run(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(db_session, legacy_owner_roots)
    await db_session.commit()
    lease = await gateway_dispatch.start_ai_dispatch(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
        credential_resolver=lambda _ref: "synthetic-secret",
    )
    await db_session.rollback()
    assert lease._credential is None
    assert lease._session is None
    with pytest.raises(gateway_contracts.AICapabilityError):
        await gateway_dispatch.dispatch_ai(
            lease,
            provider_call=lambda _request: asyncio.sleep(0, result="never"),
            usage_extractor=lambda _result: gateway_contracts.SanitizedAIUsage(),
        )


async def test_stale_dispatch_reconciliation_is_no_network_and_keeps_charge(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(db_session, legacy_owner_roots)
    await db_session.commit()
    await gateway_dispatch.start_ai_dispatch(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
        credential_resolver=lambda _ref: "synthetic-secret",
    )
    await db_session.commit()

    invocation = await db_session.get(AIInvocation, reservation.invocation_id)
    assert invocation is not None
    invocation.started_at = datetime(2026, 8, 19, 12, tzinfo=UTC)
    await db_session.commit()
    changed = await gateway_reconciliation.reconcile_stale_dispatches(
        db_session,
        stale_before=FIXED_NOW,
    )
    assert changed == 1
    assert invocation.status == AIInvocationStatus.AMBIGUOUS.value
    assert invocation.charged_cost_microunits == invocation.reserved_cost_microunits


async def test_quota_periods_reject_overlap_and_subject_misalignment(
    db_session,
    legacy_owner_roots,
):
    prepared = await _prepared_admin(db_session)
    await gateway_quota.configure_platform_quota_period(
        db_session,
        prepared=prepared,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        cost_limit_microunits=100,
        unit_limit=100,
    )
    with pytest.raises(gateway_contracts.AIQuotaImmutableError, match="overlap"):
        await gateway_quota.configure_platform_quota_period(
            db_session,
            prepared=prepared,
            period_start=date(2026, 8, 15),
            period_end=date(2026, 9, 15),
            cost_limit_microunits=100,
            unit_limit=100,
        )
    with pytest.raises(gateway_contracts.AIGatewayConfigurationError, match="align"):
        await gateway_quota.configure_subject_quota_period(
            db_session,
            prepared=prepared,
            subject_id=legacy_owner_roots.subject_id,
            period_start=date(2026, 9, 1),
            period_end=date(2026, 10, 1),
            cost_limit_microunits=100,
            unit_limit=100,
        )


async def test_cancelled_invocation_keeps_platform_quota_limits_immutable(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(db_session, legacy_owner_roots)
    await db_session.commit()
    await gateway_dispatch.cancel_reserved_ai_invocation(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
    )
    await db_session.commit()

    period = await db_session.get(
        AIPlatformQuotaPeriod,
        (PERIOD_START, PERIOD_END),
    )
    assert period is not None
    assert period.reserved_cost_microunits == 0
    prepared = await _prepared_admin(db_session)
    same = await gateway_quota.configure_platform_quota_period(
        db_session,
        prepared=prepared,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        cost_limit_microunits=10_000,
        unit_limit=10_000,
    )
    assert same is period
    with pytest.raises(gateway_contracts.AIQuotaImmutableError, match="used"):
        await gateway_quota.configure_platform_quota_period(
            db_session,
            prepared=prepared,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            cost_limit_microunits=10_001,
            unit_limit=10_000,
        )


async def test_cancelled_invocation_keeps_subject_quota_limits_immutable(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(db_session, legacy_owner_roots)
    await db_session.commit()
    await gateway_dispatch.cancel_reserved_ai_invocation(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
    )
    await db_session.commit()

    period = await db_session.get(
        AISubjectQuotaPeriod,
        (legacy_owner_roots.subject_id, PERIOD_START, PERIOD_END),
    )
    assert period is not None
    assert period.reserved_units == 0
    prepared = await _prepared_admin(db_session)
    same = await gateway_quota.configure_subject_quota_period(
        db_session,
        prepared=prepared,
        subject_id=legacy_owner_roots.subject_id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        cost_limit_microunits=10_000,
        unit_limit=10_000,
    )
    assert same is period
    with pytest.raises(gateway_contracts.AIQuotaImmutableError, match="used"):
        await gateway_quota.configure_subject_quota_period(
            db_session,
            prepared=prepared,
            subject_id=legacy_owner_roots.subject_id,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            cost_limit_microunits=10_000,
            unit_limit=10_001,
        )


@pytest.mark.parametrize(
    "credential_ref",
    ["sk-or-secret-looking", "secret_store:unreviewed/path"],
)
async def test_gateway_rejects_unreviewed_or_secret_credential_refs(
    db_session,
    legacy_owner_roots,
    credential_ref,
):
    prepared = await _prepared_admin(db_session)
    with pytest.raises(ValueError, match="resolver registry"):
        await gateway_config.create_gateway(
            db_session,
            prepared=prepared,
            external_account_discriminator="opaque-bad-ref",
            credential_ref=credential_ref,
        )
    assert await db_session.scalar(
        select(func.count()).select_from(PlatformIntegrationConnection)
    ) == 0


async def test_source_actor_semantics_are_core_authorization_invariants(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    with pytest.raises(gateway_contracts.AIGatewayAuthorizationError, match="source"):
        await gateway_invocations.reserve_ai_invocation(
            db_session,
            identity=_identity(legacy_owner_roots),
            purpose=AIInvocationPurpose.WEEKLY_DIGEST,
            source=AIInvocationSource.SCHEDULER,
            model="synthetic/model-v1",
            idempotency_key="scheduler-with-human",
            reserved_cost_microunits=1,
            reserved_units=1,
        )
    with pytest.raises(gateway_contracts.AIGatewayAuthorizationError, match="source"):
        await gateway_invocations.reserve_ai_invocation(
            db_session,
            identity=WriteIdentity(legacy_owner_roots.subject_id, None),
            purpose=AIInvocationPurpose.WEEKLY_DIGEST,
            source=AIInvocationSource.WEB,
            model="synthetic/model-v1",
            idempotency_key="web-without-human",
            reserved_cost_microunits=1,
            reserved_units=1,
        )


async def test_oversized_reservation_is_rejected_before_database_arithmetic(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    with pytest.raises(ValueError, match="signed bigint"):
        await _reserve(
            db_session,
            legacy_owner_roots,
            cost=gateway_contracts.MAX_SIGNED_BIGINT + 1,
        )
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 0


async def test_idempotency_and_start_bind_exact_actor_and_call_fingerprint(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(db_session, legacy_owner_roots, key="fingerprint")
    await db_session.commit()

    with pytest.raises(gateway_contracts.AIIdempotencyConflictError):
        await _reserve(
            db_session,
            legacy_owner_roots,
            key="fingerprint",
            cost=101,
        )
    await db_session.rollback()
    with pytest.raises(gateway_contracts.AIIdempotencyConflictError):
        await gateway_invocations.reserve_ai_invocation(
            db_session,
            identity=WriteIdentity(legacy_owner_roots.subject_id, None),
            purpose=AIInvocationPurpose.WEEKLY_DIGEST,
            source=AIInvocationSource.SCHEDULER,
            model="synthetic/model-v1",
            idempotency_key="fingerprint",
            reserved_cost_microunits=100,
            reserved_units=500,
        )
    await db_session.rollback()
    resolver_calls = 0

    def resolver(_ref):
        nonlocal resolver_calls
        resolver_calls += 1
        return "synthetic-secret"

    with pytest.raises(gateway_contracts.AIGatewayAuthorizationError):
        await gateway_dispatch.start_ai_dispatch(
            db_session,
            identity=WriteIdentity(legacy_owner_roots.subject_id, None),
            invocation_id=reservation.invocation_id,
            credential_resolver=resolver,
        )
    assert resolver_calls == 0


async def test_usage_metadata_is_canonical_and_extractor_errors_are_sanitized(
    db_session,
    legacy_owner_roots,
):
    assert (
        gateway_contracts.SanitizedAIUsage(upstream_request_id="  opaque-id  ")
        .upstream_request_id
        == "opaque-id"
    )
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(db_session, legacy_owner_roots)
    await db_session.commit()
    lease = await gateway_dispatch.start_ai_dispatch(
        db_session,
        identity=_identity(legacy_owner_roots),
        invocation_id=reservation.invocation_id,
        credential_resolver=lambda _ref: "synthetic-secret",
    )
    await db_session.commit()

    def bad_extractor(_result):
        raise RuntimeError("sensitive extractor exception")

    completion = await gateway_dispatch.dispatch_ai(
        lease,
        provider_call=lambda _request: asyncio.sleep(0, result="memory payload"),
        usage_extractor=bad_extractor,
    )
    assert completion.status is AIInvocationStatus.FAILED
    assert completion.error_code is AIInvocationErrorCode.INVALID_RESPONSE
    assert "sensitive" not in repr(completion)
    await gateway_dispatch.finalize_ai_invocation(db_session, completion=completion)


async def test_stale_prepared_reconciliation_releases_exact_ledgers(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(db_session, legacy_owner_roots)
    await db_session.commit()
    invocation = await db_session.get(AIInvocation, reservation.invocation_id)
    assert invocation is not None
    invocation.created_at = FIXED_NOW - timedelta(days=2)
    await db_session.commit()

    changed = await gateway_reconciliation.reconcile_stale_reservations(
        db_session,
        stale_before=FIXED_NOW - timedelta(days=1),
    )
    assert changed == 1
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
    assert platform.charged_cost_microunits == 0
    assert subject.charged_cost_microunits == 0


async def test_stale_reconciliation_requires_aware_utc_threshold(
    db_session,
):
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        await gateway_reconciliation.reconcile_stale_dispatches(
            db_session,
            stale_before=datetime(2026, 8, 20, 12),
        )
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        await gateway_reconciliation.reconcile_stale_reservations(
            db_session,
            stale_before=datetime(2026, 8, 20, 12),
        )


@pytest.mark.integration
async def test_postgres_concurrent_reservations_hard_stop_at_shared_limit(
    db_session,
    legacy_owner_roots,
):
    await _configure(
        db_session,
        legacy_owner_roots,
        platform_cost=100,
        subject_cost=100,
        platform_units=500,
        subject_units=500,
    )
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async def reserve(key):
        async with factory() as session:
            try:
                result = await _reserve(
                    session,
                    legacy_owner_roots,
                    key=key,
                    cost=100,
                    units=500,
                )
                await session.commit()
                return result
            except gateway_contracts.AIQuotaExceededError as exc:
                await session.rollback()
                return exc

    outcomes = await asyncio.wait_for(
        asyncio.gather(reserve("race-1"), reserve("race-2")),
        timeout=10,
    )
    assert sum(isinstance(item, gateway_contracts.AIReservationResult) for item in outcomes) == 1
    assert sum(isinstance(item, gateway_contracts.AIQuotaExceededError) for item in outcomes) == 1


@pytest.mark.integration
async def test_postgres_concurrent_idempotency_and_start_issue_one_lease(
    db_session,
    legacy_owner_roots,
):
    await _configure(db_session, legacy_owner_roots)
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async def reserve():
        async with factory() as session:
            result = await _reserve(session, legacy_owner_roots, key="same-key")
            await session.commit()
            return result

    first, second = await asyncio.wait_for(
        asyncio.gather(reserve(), reserve()),
        timeout=10,
    )
    assert first.invocation_id == second.invocation_id
    assert sorted((first.created, second.created)) == [False, True]

    async def start():
        async with factory() as session:
            try:
                lease = await gateway_dispatch.start_ai_dispatch(
                    session,
                    identity=_identity(legacy_owner_roots),
                    invocation_id=first.invocation_id,
                    credential_resolver=lambda _ref: "synthetic-secret",
                )
                await session.commit()
                return lease
            except gateway_contracts.AIInvocationStateError as exc:
                await session.rollback()
                return exc

    outcomes = await asyncio.wait_for(
        asyncio.gather(start(), start()),
        timeout=10,
    )
    assert sum(isinstance(item, gateway_contracts.AIDispatchLease) for item in outcomes) == 1
    assert sum(isinstance(item, gateway_contracts.AIInvocationStateError) for item in outcomes) == 1


@pytest.mark.integration
async def test_postgres_concurrent_overlap_configuration_is_serialized(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    first = factory()
    await first.__aenter__()
    try:
        prepared = await _prepared_admin(first)
        await gateway_quota.configure_platform_quota_period(
            first,
            prepared=prepared,
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            cost_limit_microunits=100,
            unit_limit=100,
        )
        attempted = asyncio.Event()
        original = platform_admin_service.acquire_identity_governance_lock

        async def observed_lock(session):
            attempted.set()
            await original(session)

        monkeypatch.setattr(
            platform_admin_service,
            "acquire_identity_governance_lock",
            observed_lock,
        )

        async def configure_overlap():
            async with factory() as session:
                prepared_second = await _prepared_admin(session)
                return await gateway_quota.configure_platform_quota_period(
                    session,
                    prepared=prepared_second,
                    period_start=date(2026, 8, 15),
                    period_end=date(2026, 9, 15),
                    cost_limit_microunits=100,
                    unit_limit=100,
                )

        contender = asyncio.create_task(configure_overlap())
        await asyncio.wait_for(attempted.wait(), timeout=5)
        await first.commit()
        with pytest.raises(gateway_contracts.AIQuotaImmutableError):
            await asyncio.wait_for(contender, timeout=5)
    finally:
        await first.__aexit__(None, None, None)


@pytest.mark.integration
async def test_postgres_rotation_and_revocation_win_before_fresh_start(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure(db_session, legacy_owner_roots)
    rotation_call = await _reserve(db_session, legacy_owner_roots, key="rotation-race")
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    rotation_session = factory()
    await rotation_session.__aenter__()
    try:
        prepared = await _prepared_admin(rotation_session)
        await gateway_config.rotate_gateway(
            rotation_session,
            prepared=prepared,
            external_account_discriminator="opaque-race-v2",
            credential_ref="legacy_env:openrouter",
        )
        attempted = asyncio.Event()
        original = gateway_config.acquire_identity_governance_lock

        async def observed_lock(session):
            attempted.set()
            await original(session)

        monkeypatch.setattr(
            gateway_dispatch,
            "acquire_identity_governance_lock",
            observed_lock,
        )
        resolver_calls = 0

        def resolver(_ref):
            nonlocal resolver_calls
            resolver_calls += 1
            return "synthetic-secret"

        async def start():
            async with factory() as session:
                return await gateway_dispatch.start_ai_dispatch(
                    session,
                    identity=_identity(legacy_owner_roots),
                    invocation_id=rotation_call.invocation_id,
                    credential_resolver=resolver,
                )

        contender = asyncio.create_task(start())
        await asyncio.wait_for(attempted.wait(), timeout=5)
        await rotation_session.commit()
        with pytest.raises(gateway_contracts.AIGatewayConfigurationError):
            await asyncio.wait_for(contender, timeout=5)
        assert resolver_calls == 0
    finally:
        await rotation_session.__aexit__(None, None, None)


@pytest.mark.integration
async def test_postgres_actor_revocation_serializes_before_fresh_start(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure(db_session, legacy_owner_roots)
    reservation = await _reserve(db_session, legacy_owner_roots, key="revoke-race")
    second_admin = User(
        username="Race Admin",
        normalized_username="race admin",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(second_admin)
    await db_session.flush()
    await assign_role(
        db_session,
        user_id=second_admin.id,
        role=UserRoleName.PLATFORM_SUPERADMIN,
        assigned_by_user_id=legacy_owner_roots.user_id,
    )
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    revocation_session = factory()
    await revocation_session.__aenter__()
    try:
        await change_user_status(
            revocation_session,
            user_id=legacy_owner_roots.user_id,
            new_status=UserStatus.SUSPENDED,
            actor_user_id=second_admin.id,
        )
        attempted = asyncio.Event()
        original = gateway_config.acquire_identity_governance_lock

        async def observed_lock(session):
            attempted.set()
            await original(session)

        monkeypatch.setattr(
            gateway_dispatch,
            "acquire_identity_governance_lock",
            observed_lock,
        )
        resolver_calls = 0

        def resolver(_ref):
            nonlocal resolver_calls
            resolver_calls += 1
            return "synthetic-secret"

        async def start():
            async with factory() as session:
                return await gateway_dispatch.start_ai_dispatch(
                    session,
                    identity=_identity(legacy_owner_roots),
                    invocation_id=reservation.invocation_id,
                    credential_resolver=resolver,
                )

        contender = asyncio.create_task(start())
        await asyncio.wait_for(attempted.wait(), timeout=5)
        await revocation_session.commit()
        with pytest.raises(gateway_contracts.AIGatewayAuthorizationError):
            await asyncio.wait_for(contender, timeout=5)
        assert resolver_calls == 0
    finally:
        await revocation_session.__aexit__(None, None, None)
