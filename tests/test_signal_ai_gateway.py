"""Platform-funded, raw-first Signals parser lifecycle contracts."""
from __future__ import annotations

import asyncio
import pickle
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

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
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Severity,
    Source,
    UserStatus,
)
from vitals.integrations.llm_client import LLMCallResult
from vitals.models.ai import AIInvocation, AIPlatformQuotaPeriod, AISubjectQuotaPeriod
from vitals.models.identity import User
from vitals.models.raw_payload import RawPayload
from vitals.models.signals import Signal
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection, PlatformIntegrationConnection
from vitals.services import ai_gateway_service
from vitals.services.proactive import inbound, signal_ai_service
from vitals.services.proactive.ownership import ProactiveOwnershipContext

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
DAY = date(2026, 8, 20)
PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 9, 1)
MODEL = "synthetic/signal-model"
SECRET = "synthetic-platform-secret"


@pytest.fixture(autouse=True)
def _runtime(monkeypatch):
    monkeypatch.setattr(ai_gateway_service, "now_utc", lambda: NOW)
    config = replace(
        load_runtime_config(),
        openrouter_api_key=SECRET,
        llm_model_parser=MODEL,
    )
    monkeypatch.setattr(signal_ai_service, "load_config", lambda: config)


async def _configure_platform(session, roots) -> PlatformIntegrationConnection:
    root = PlatformIntegrationConnection(
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=f"signal-{uuid.uuid4().hex}",
        credential_ref="env:VITALS_OPENROUTER_API_KEY",
        status=IntegrationConnectionStatus.ACTIVE.value,
        config_version=1,
        configured_by_user_id=roots.user_id,
    )
    session.add_all(
        [
            root,
            AIPlatformQuotaPeriod(
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                cost_limit_microunits=100_000_000,
                unit_limit=10_000_000,
                configured_by_user_id=roots.user_id,
            ),
            AISubjectQuotaPeriod(
                subject_id=roots.subject_id,
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                cost_limit_microunits=100_000_000,
                unit_limit=10_000_000,
                configured_by_user_id=roots.user_id,
            ),
        ]
    )
    await session.commit()
    return root


async def _ownership(session, roots, *, bridge=True) -> ProactiveOwnershipContext:
    connection_id = await session.scalar(
        select(IntegrationConnection.id).where(
            IntegrationConnection.subject_id == roots.subject_id,
            IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.RECIPIENT.value,
        )
    )
    assert connection_id is not None
    return ProactiveOwnershipContext(
        subject_id=roots.subject_id,
        recipient_user_id=roots.user_id,
        connection_id=connection_id,
        include_legacy_unowned=bridge,
    )


async def _raw(
    session,
    roots,
    ownership,
    *,
    suffix: str,
    payload: dict | None = None,
    subject_id=...,
    actor_user_id=...,
    connection_id=...,
    file_asset_id=None,
    fetched_at: datetime | None = None,
) -> RawPayload:
    message = {
        "update_id": int(uuid.uuid4().int % 1_000_000_000),
        "message": {
            "message_id": int(uuid.uuid4().int % 1_000_000_000),
            "date": int(datetime(2026, 8, 20, 6, tzinfo=UTC).timestamp()),
            "chat": {"id": 424242, "type": "private"},
            "from": {"id": 424242, "is_bot": False},
            "text": "голова болит",
        },
    }
    row = RawPayload(
        subject_id=roots.subject_id if subject_id is ... else subject_id,
        actor_user_id=roots.user_id if actor_user_id is ... else actor_user_id,
        integration_connection_id=(
            ownership.connection_id if connection_id is ... else connection_id
        ),
        file_asset_id=file_asset_id,
        domain=Domain.SIGNALS.value,
        source=Source.TELEGRAM.value,
        external_id=f"signal-ai-{suffix}-{uuid.uuid4().hex}",
        payload=payload or message,
        fetched_at=fetched_at or datetime(2026, 8, 20, 12),
    )
    session.add(row)
    await session.flush()
    return row


def _llm(session, observed, *, behavior="success"):
    class FakeLLM:
        def __init__(self, config):
            observed["credential"].append(config.openrouter_api_key)

        async def extract_json_with_usage(
            self, _text, *, model, system, max_tokens
        ):
            del system
            observed["calls"] += 1
            observed["no_tx"].append(not session.in_transaction())
            observed["model"].append(model)
            observed["max_tokens"].append(max_tokens)
            if behavior == "provider_exception":
                raise RuntimeError("sensitive provider detail")
            value: object = {
                "signals": [
                    {
                        "kind": "symptom",
                        "key": "headache",
                        "value_num": 3,
                        "note": "голова болит",
                    }
                ]
            }
            if behavior == "empty":
                value = {"signals": []}
            elif behavior == "malformed":
                value = {"unexpected": []}
            return LLMCallResult(
                value=value,
                upstream_request_id="signal-request-1",
                model=model,
                input_tokens=12,
                output_tokens=8,
                cost_microunits=(None if behavior == "missing_usage" else 50),
            )

    return FakeLLM


def _observed() -> dict:
    return {
        "calls": 0,
        "no_tx": [],
        "credential": [],
        "model": [],
        "max_tokens": [],
    }


async def _dispatch_and_persist(session, prepared, *, behavior="success"):
    observed = _observed()
    lease = await signal_ai_service.start_signal_dispatch(
        session,
        prepared,
        credential_resolver=lambda _ref: SECRET,
    )
    await session.commit()
    completion = await signal_ai_service.render_signal_parse(
        prepared,
        lease,
        llm_factory=_llm(session, observed, behavior=behavior),
    )
    result = await signal_ai_service.persist_signal_parse(
        session,
        prepared,
        completion,
    )
    await session.commit()
    return result, observed


async def test_live_gateway_flow_is_idempotent_raw_bound_and_transaction_free(
    db_session,
    legacy_owner_roots,
):
    root = await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    historical_subject_ai = await db_session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == legacy_owner_roots.subject_id,
            IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
        )
    )
    assert historical_subject_ai is not None
    await db_session.delete(historical_subject_ai)
    await db_session.commit()
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="live",
    )
    await db_session.commit()

    prepared = await signal_ai_service.prepare_live_signal_parse(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        on_date=DAY,
    )
    await db_session.commit()
    duplicate = await signal_ai_service.prepare_live_signal_parse(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        on_date=DAY,
    )
    await db_session.commit()
    assert duplicate.invocation_id == prepared.invocation_id
    assert "голова" not in repr(prepared)
    with pytest.raises(TypeError):
        pickle.dumps(prepared)

    result, observed = await _dispatch_and_persist(db_session, prepared)
    assert observed == {
        "calls": 1,
        "no_tx": [True],
        "credential": [SECRET],
        "model": [MODEL],
        "max_tokens": [2048],
    }
    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None
    assert (
        invocation.subject_id,
        invocation.actor_user_id,
        invocation.raw_payload_id,
        invocation.platform_integration_connection_id,
        invocation.purpose,
        invocation.source,
        invocation.status,
    ) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        raw.id,
        root.id,
        AIInvocationPurpose.SIGNAL_PARSE.value,
        AIInvocationSource.TELEGRAM.value,
        AIInvocationStatus.SUCCEEDED.value,
    )
    await db_session.refresh(raw)
    assert raw.processed_at is not None
    assert len(result.signals) == 1
    assert (
        result.signals[0].subject_id,
        result.signals[0].actor_user_id,
        result.signals[0].integration_connection_id,
        result.signals[0].raw_id,
    ) == (
        ownership.subject_id,
        ownership.recipient_user_id,
        ownership.connection_id,
        raw.id,
    )


@pytest.mark.parametrize(
    ("behavior", "expected_status"),
    (
        ("malformed", AIInvocationStatus.FAILED),
        ("missing_usage", AIInvocationStatus.FAILED),
        ("provider_exception", AIInvocationStatus.AMBIGUOUS),
    ),
)
async def test_invalid_or_unavailable_provider_is_one_call_and_keeps_raw_pending(
    db_session,
    legacy_owner_roots,
    behavior,
    expected_status,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix=behavior,
    )
    await db_session.commit()
    prepared = await signal_ai_service.prepare_live_signal_parse(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        on_date=DAY,
    )
    await db_session.commit()

    result, observed = await _dispatch_and_persist(
        db_session,
        prepared,
        behavior=behavior,
    )
    assert observed["calls"] == 1
    assert observed["no_tx"] == [True]
    assert result.status is expected_status
    assert result.processed is False
    assert result.signals == ()
    await db_session.refresh(raw)
    assert raw.processed_at is None
    assert await db_session.scalar(
        select(func.count()).select_from(Signal).where(Signal.raw_id == raw.id)
    ) == 0


async def test_explicit_empty_is_a_successful_terminal_parse(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="empty",
    )
    await db_session.commit()
    prepared = await signal_ai_service.prepare_live_signal_parse(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        on_date=DAY,
    )
    await db_session.commit()
    result, observed = await _dispatch_and_persist(
        db_session,
        prepared,
        behavior="empty",
    )
    await db_session.refresh(raw)
    assert observed["calls"] == 1
    assert result.status is AIInvocationStatus.SUCCEEDED
    assert result.processed is True and result.signals == ()
    assert raw.processed_at is not None


async def test_live_date_mismatch_is_rejected_before_reservation(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="wrong-date",
    )
    await db_session.commit()

    with pytest.raises(
        signal_ai_service.SignalAIValidationError,
        match="does not match",
    ):
        await signal_ai_service.prepare_live_signal_parse(
            db_session,
            ownership=ownership,
            raw_payload_id=raw.id,
            on_date=DAY + timedelta(days=1),
        )
    await db_session.rollback()
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 0


async def test_fully_unowned_bridge_adopts_only_subject_and_preserves_history(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="legacy",
        subject_id=None,
        actor_user_id=None,
        connection_id=None,
    )
    await db_session.commit()
    prepared = await signal_ai_service.prepare_live_signal_parse(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        on_date=DAY,
    )
    await db_session.commit()
    await db_session.refresh(raw)
    assert (
        raw.subject_id,
        raw.actor_user_id,
        raw.integration_connection_id,
        raw.file_asset_id,
    ) == (ownership.subject_id, None, None, None)

    result, _ = await _dispatch_and_persist(db_session, prepared)
    assert len(result.signals) == 1
    assert (
        result.signals[0].subject_id,
        result.signals[0].actor_user_id,
        result.signals[0].integration_connection_id,
    ) == (ownership.subject_id, None, None)


async def test_recovery_can_resume_a_subject_adopted_legacy_raw(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="legacy-recovery",
        subject_id=None,
        actor_user_id=None,
        connection_id=None,
    )
    await db_session.commit()
    live = await signal_ai_service.prepare_live_signal_parse(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        on_date=DAY,
    )
    await db_session.commit()
    failed, _ = await _dispatch_and_persist(
        db_session,
        live,
        behavior="provider_exception",
    )
    assert failed.status is AIInvocationStatus.AMBIGUOUS
    await db_session.refresh(raw)
    assert (raw.subject_id, raw.actor_user_id, raw.integration_connection_id) == (
        ownership.subject_id,
        None,
        None,
    )

    observed = _observed()
    monkeypatch.setattr(signal_ai_service, "LLMClient", _llm(db_session, observed))
    recovered = await inbound.reparse_pending(db_session, ownership=ownership)
    assert observed["calls"] == 1
    assert len(recovered) == 1
    assert (
        recovered[0].subject_id,
        recovered[0].actor_user_id,
        recovered[0].integration_connection_id,
    ) == (ownership.subject_id, None, None)
    invocations = list(
        await db_session.scalars(
            select(AIInvocation)
            .where(AIInvocation.raw_payload_id == raw.id)
            .order_by(AIInvocation.created_at, AIInvocation.id)
        )
    )
    assert {(row.source, row.actor_user_id, row.status) for row in invocations} == {
        (
            AIInvocationSource.TELEGRAM.value,
            ownership.recipient_user_id,
            AIInvocationStatus.AMBIGUOUS.value,
        ),
        (
            AIInvocationSource.SCHEDULER.value,
            None,
            AIInvocationStatus.SUCCEEDED.value,
        ),
    }


@pytest.mark.parametrize(
    ("subject", "actor", "connection"),
    (
        (None, "owner", None),
        ("subject", None, "telegram"),
        ("subject", "owner", None),
    ),
)
async def test_partial_raw_roots_fail_before_reservation(
    db_session,
    legacy_owner_roots,
    subject,
    actor,
    connection,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix=f"partial-{subject}-{actor}-{connection}",
        subject_id=(legacy_owner_roots.subject_id if subject == "subject" else None),
        actor_user_id=(legacy_owner_roots.user_id if actor == "owner" else None),
        connection_id=(ownership.connection_id if connection == "telegram" else None),
    )
    await db_session.commit()
    with pytest.raises(signal_ai_service.SignalAIOwnershipError):
        await signal_ai_service.prepare_live_signal_parse(
            db_session,
            ownership=ownership,
            raw_payload_id=raw.id,
            on_date=DAY,
        )
    await db_session.rollback()
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 0


@pytest.mark.parametrize("rotation", ("owner", "connection"))
async def test_t2_revalidates_owner_and_telegram_connection_before_charge(
    db_session,
    legacy_owner_roots,
    rotation,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix=f"t2-{rotation}",
    )
    await db_session.commit()
    prepared = await signal_ai_service.prepare_live_signal_parse(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        on_date=DAY,
    )
    await db_session.commit()
    if rotation == "owner":
        owner = await db_session.get(User, legacy_owner_roots.user_id)
        assert owner is not None
        owner.status = UserStatus.SUSPENDED.value
    else:
        connection = await db_session.get(IntegrationConnection, ownership.connection_id)
        assert connection is not None
        connection.status = IntegrationConnectionStatus.RETIRED.value
        connection.retired_at = datetime(2026, 8, 20, 12)
    await db_session.commit()

    with pytest.raises(signal_ai_service.SignalAIOwnershipError):
        await signal_ai_service.start_signal_dispatch(
            db_session,
            prepared,
            credential_resolver=lambda _ref: SECRET,
        )
    await db_session.rollback()
    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None
    assert invocation.status == AIInvocationStatus.PREPARED.value
    assert invocation.charged_cost_microunits == 0


async def test_cross_actor_prepared_waits_then_attempts_are_bounded(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="attempts",
    )
    await db_session.commit()
    live = await signal_ai_service.prepare_live_signal_parse(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        on_date=DAY,
    )
    await db_session.commit()
    recovery = await signal_ai_service.prepare_signal_recovery(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
    )
    await db_session.commit()
    assert recovery.dispatchable is False
    assert recovery.invocation_id == live.invocation_id
    assert recovery.fallback is signal_ai_service.SignalParseFallback.PENDING

    await signal_ai_service.cancel_prepared_signal_parse(db_session, live)
    await db_session.commit()
    for _ in range(2):
        recovery = await signal_ai_service.prepare_signal_recovery(
            db_session,
            ownership=ownership,
            raw_payload_id=raw.id,
        )
        await db_session.commit()
        result, observed = await _dispatch_and_persist(
            db_session,
            recovery,
            behavior="provider_exception",
        )
        assert result.status is AIInvocationStatus.AMBIGUOUS
        assert observed["calls"] == 1
    exhausted = await signal_ai_service.prepare_signal_recovery(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
    )
    await db_session.commit()
    assert exhausted.dispatchable is False
    assert exhausted.fallback is signal_ai_service.SignalParseFallback.ATTEMPTS_EXHAUSTED
    assert await db_session.scalar(
        select(func.count()).select_from(AIInvocation).where(
            AIInvocation.raw_payload_id == raw.id
        )
    ) == 3


async def test_parser_alert_is_subject_scoped_and_legacy_roots_are_not_fabricated(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    legacy = SystemAlert(
        subject_id=None,
        integration_connection_id=None,
        domain=Domain.SIGNALS.value,
        severity=Severity.WARN.value,
        message="legacy parser warning",
        alert_key="signal_parser_failed",
        entity_ref="",
    )
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="alert",
    )
    db_session.add(legacy)
    await db_session.commit()
    prepared = await signal_ai_service.prepare_live_signal_parse(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        on_date=DAY,
    )
    await db_session.commit()
    result, _ = await _dispatch_and_persist(
        db_session,
        prepared,
        behavior="provider_exception",
    )
    alert = await signal_ai_service.reconcile_signal_parser_alert(
        db_session,
        ownership=ownership,
    )
    await db_session.commit()
    assert alert is not None
    assert (
        alert.subject_id,
        alert.integration_connection_id,
        alert.ai_invocation_id,
        alert.entity_ref,
    ) == (
        ownership.subject_id,
        None,
        result.invocation_id,
        signal_ai_service.parser_alert_entity_ref(ownership.subject_id),
    )
    await db_session.refresh(legacy)
    assert legacy.resolved_at is not None
    assert (legacy.subject_id, legacy.integration_connection_id) == (None, None)


async def test_recovery_uses_same_pre_four_am_health_day(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="night",
        payload={"text": "голова болит"},
        fetched_at=datetime(2026, 8, 20, 1),
    )
    await db_session.commit()
    prepared = await signal_ai_service.prepare_signal_recovery(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
    )
    await db_session.commit()
    result, _ = await _dispatch_and_persist(db_session, prepared)
    assert len(result.signals) == 1
    assert result.signals[0].date == DAY - timedelta(days=1)
    invocation = await db_session.get(AIInvocation, result.invocation_id)
    assert invocation is not None
    assert (invocation.source, invocation.actor_user_id) == (
        AIInvocationSource.SCHEDULER.value,
        None,
    )


async def test_stale_raw_after_provider_is_accounted_without_normalization(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="stale",
    )
    raw_id = raw.id
    await db_session.commit()
    prepared = await signal_ai_service.prepare_live_signal_parse(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        on_date=DAY,
    )
    await db_session.commit()
    lease = await signal_ai_service.start_signal_dispatch(
        db_session,
        prepared,
        credential_resolver=lambda _ref: SECRET,
    )
    await db_session.commit()
    completion = await signal_ai_service.render_signal_parse(
        prepared,
        lease,
        llm_factory=_llm(db_session, _observed()),
    )
    raw.payload = {"text": "changed after provider"}
    await db_session.commit()
    with pytest.raises(signal_ai_service.SignalAIOwnershipError):
        await signal_ai_service.persist_signal_parse(
            db_session,
            prepared,
            completion,
        )
    await db_session.rollback()
    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None
    assert invocation.status == AIInvocationStatus.DISPATCHING.value
    assert await db_session.scalar(
        select(func.count()).select_from(Signal).where(Signal.raw_id == raw_id)
    ) == 0


async def test_exhausted_head_rows_do_not_starve_later_recovery_candidates(
    db_session,
    legacy_owner_roots,
):
    root = await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raws = []
    for number in range(25):
        raws.append(
            await _raw(
                db_session,
                legacy_owner_roots,
                ownership,
                suffix=f"fair-{number}",
            )
        )
    await db_session.flush()
    for raw in raws[:20]:
        for attempt in range(1, 4):
            db_session.add(
                AIInvocation(
                    subject_id=legacy_owner_roots.subject_id,
                    actor_user_id=None,
                    raw_payload_id=raw.id,
                    platform_integration_connection_id=root.id,
                    purpose=AIInvocationPurpose.SIGNAL_PARSE.value,
                    source=AIInvocationSource.SCHEDULER.value,
                    model=MODEL,
                    config_version=root.config_version,
                    idempotency_key=signal_ai_service._attempt_key(raw.id, attempt),
                    quota_period_start=PERIOD_START,
                    quota_period_end=PERIOD_END,
                    reserved_cost_microunits=1,
                    reserved_units=1,
                    charged_cost_microunits=1,
                    charged_units=1,
                    status=AIInvocationStatus.FAILED.value,
                    error_code=AIInvocationErrorCode.INVALID_RESPONSE.value,
                    started_at=NOW,
                    finished_at=NOW,
                )
            )
    await db_session.commit()

    selected = await signal_ai_service.pending_signal_recovery_ids(
        db_session,
        ownership=ownership,
        limit=20,
    )
    assert selected == [raw.id for raw in raws[20:]]


async def test_partial_pending_head_rows_do_not_starve_later_platform_recovery(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    """Partial legacy roots stay immutable but never block a later valid raw."""

    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    partials = []
    for number in range(21):
        partials.append(
            await _raw(
                db_session,
                legacy_owner_roots,
                ownership,
                suffix=f"partial-head-{number}",
                actor_user_id=None,
            )
        )
    valid = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="after-partial-head",
    )
    partial_ids = [partial.id for partial in partials]
    valid_id = valid.id
    await db_session.commit()

    observed = _observed()
    monkeypatch.setattr(signal_ai_service, "LLMClient", _llm(db_session, observed))
    reconciliation_failures = []
    reconcile = signal_ai_service.reconcile_signal_parser_alert

    async def record_reconciliation(*args, **kwargs):
        try:
            return await reconcile(*args, **kwargs)
        except Exception as exc:
            reconciliation_failures.append(exc)
            raise

    monkeypatch.setattr(
        signal_ai_service,
        "reconcile_signal_parser_alert",
        record_reconciliation,
    )

    recovered = await inbound.reparse_pending(db_session, ownership=ownership)

    assert observed["calls"] == 1
    assert len(recovered) == 1
    assert reconciliation_failures == []
    alert = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.subject_id == ownership.subject_id,
            SystemAlert.alert_key == "signal_parser_failed",
            SystemAlert.resolved_at.is_(None),
        )
    )
    assert alert is not None
    assert (alert.integration_connection_id, alert.ai_invocation_id) == (None, None)
    valid = await db_session.get(RawPayload, valid_id)
    assert valid is not None and valid.processed_at is not None
    assert await db_session.scalar(
        select(func.count()).select_from(Signal).where(Signal.raw_id == valid_id)
    ) == 1
    for partial_id in partial_ids:
        partial = await db_session.get(RawPayload, partial_id)
        assert partial is not None and partial.processed_at is None
        assert await db_session.scalar(
            select(func.count())
            .select_from(AIInvocation)
            .where(AIInvocation.raw_payload_id == partial_id)
        ) == 0


async def test_invalid_input_recovery_is_bounded_and_progresses_next_run(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    invalid = [
        await _raw(
            db_session,
            legacy_owner_roots,
            ownership,
            suffix=f"invalid-input-{number}",
            payload={"text": ""},
        )
        for number in range(21)
    ]
    valid = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="after-invalid-input",
    )
    valid_id = valid.id
    await db_session.commit()

    observed = _observed()
    monkeypatch.setattr(signal_ai_service, "LLMClient", _llm(db_session, observed))
    assert await inbound.reparse_pending(db_session, ownership=ownership) == []
    assert observed["calls"] == 0
    invalid_after_first = [
        await db_session.get(RawPayload, raw.id) for raw in invalid
    ]
    processed_after_first = sum(
        row is not None and row.processed_at is not None
        for row in invalid_after_first
    )
    assert processed_after_first == 20
    valid = await db_session.get(RawPayload, valid_id)
    assert valid is not None and valid.processed_at is None

    recovered = await inbound.reparse_pending(db_session, ownership=ownership)
    assert observed["calls"] == 1
    assert len(recovered) == 1
    invalid_after_second = [
        await db_session.get(RawPayload, raw.id) for raw in invalid
    ]
    assert all(
        row is not None and row.processed_at is not None
        for row in invalid_after_second
    )
    valid = await db_session.get(RawPayload, valid_id)
    assert valid is not None and valid.processed_at is not None


async def test_recovery_high_water_excludes_rows_appended_after_run_start(
    db_session,
    legacy_owner_roots,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    first = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="before-high-water",
    )
    await db_session.commit()
    high_water = await signal_ai_service.signal_recovery_high_water_id(
        db_session,
        ownership=ownership,
    )
    assert high_water == first.id
    await db_session.commit()
    later = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="after-high-water",
    )
    await db_session.commit()

    selected = await signal_ai_service.pending_signal_recovery_ids(
        db_session,
        ownership=ownership,
        through_id=high_water,
    )
    assert selected == [first.id]
    assert later.id > high_water


@pytest.mark.integration
async def test_postgres_concurrent_signal_starts_issue_one_provider_call_and_result(
    db_session,
    legacy_owner_roots,
):
    """The raw/invocation locks allow exactly one starter to reach the provider."""

    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="concurrent-start",
    )
    await db_session.commit()
    prepared = await signal_ai_service.prepare_live_signal_parse(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        on_date=DAY,
    )
    await db_session.commit()

    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    observed = _observed()

    async def start_render_and_persist():
        async with factory() as session:
            try:
                lease = await signal_ai_service.start_signal_dispatch(
                    session,
                    prepared,
                    credential_resolver=lambda _ref: SECRET,
                )
                await session.commit()
            except ai_gateway_service.AIInvocationStateError as exc:
                await session.rollback()
                return exc
            completion = await signal_ai_service.render_signal_parse(
                prepared,
                lease,
                llm_factory=_llm(session, observed),
            )
            result = await signal_ai_service.persist_signal_parse(
                session,
                prepared,
                completion,
            )
            await session.commit()
            return result

    outcomes = await asyncio.wait_for(
        asyncio.gather(start_render_and_persist(), start_render_and_persist()),
        timeout=10,
    )
    results = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, signal_ai_service.SignalParseResult)
    ]
    rejected = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, ai_gateway_service.AIInvocationStateError)
    ]
    assert len(results) == 1
    assert len(rejected) == 1
    assert results[0].processed is True
    assert len(results[0].signals) == 1
    assert observed["calls"] == 1

    async with factory() as verify:
        assert await verify.scalar(
            select(func.count()).select_from(Signal).where(Signal.raw_id == raw.id)
        ) == 1
        invocations = list(
            await verify.scalars(
                select(AIInvocation).where(AIInvocation.raw_payload_id == raw.id)
            )
        )
        assert len(invocations) == 1
        assert invocations[0].status == AIInvocationStatus.SUCCEEDED.value
