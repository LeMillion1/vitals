"""Platform-AI Telegram question-reply lifecycle and PHI boundary contracts."""
from __future__ import annotations


import asyncio
import json
import pickle
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.config import load_config as load_runtime_config
from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    NotificationDeliveryErrorCode,
    NotificationDeliveryStatus,
    Source,
)
from vitals.integrations.llm_client import LLMCallResult
from vitals.i18n import t
from vitals.models.ai import AIInvocation, AIPlatformQuotaPeriod, AISubjectQuotaPeriod
from vitals.models.proactive import Notification, NotificationDeliveryIntent
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection, PlatformIntegrationConnection
from vitals.services import ai_gateway_service, modules_service
from vitals.services.proactive import channels, delivery, inbound, question_ai_service
from vitals.services.proactive.ownership import ProactiveOwnershipContext


pytestmark = pytest.mark.usefixtures("all_modules_on")

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 9, 1)
MODEL = "synthetic/question-model"
SECRET = "synthetic-question-secret"
QUESTION = "Почему пульс ниже обычного?"
ANSWER = "Пульс ниже обычного из-за восстановления после нагрузки."
PROMPT_CONTEXT = "прошлый ответ бота: пульс 52"
PROMPT_FACTS = '{"resting_hr":52}'


@pytest.fixture(autouse=True)
def _runtime(monkeypatch):
    monkeypatch.setattr(ai_gateway_service, "now_utc", lambda: NOW)
    config = replace(
        load_runtime_config(),
        openrouter_api_key=SECRET,
        llm_model_digest=MODEL,
    )
    monkeypatch.setattr(question_ai_service, "load_config", lambda: config)


class _Notifier:
    channel = IntegrationProvider.TELEGRAM.value

    def __init__(self, ownership: ProactiveOwnershipContext):
        self.binding = channels.DeliveryEndpointBinding(
            subject_id=ownership.subject_id,
            recipient_user_id=ownership.recipient_user_id,
            integration_connection_id=ownership.connection_id,
            channel=self.channel,
        )
        self.sent: list[dict[str, object]] = []
        self.edited: list[dict[str, object]] = []

    async def send(self, text, *, buttons=None, reply_to=None) -> str:
        self.sent.append({"text": text, "buttons": buttons, "reply_to": reply_to})
        return str(800 + len(self.sent))

    async def edit(self, external_id, text, *, buttons=None) -> None:
        self.edited.append(
            {"external_id": external_id, "text": text, "buttons": buttons}
        )

    async def answer_callback(self, callback_id, text="") -> None:
        del callback_id, text


def _notifier_builder(notifier, ownership):
    async def _build(_session, resolved, *, config=None):
        del _session, config
        assert (
            resolved.subject_id,
            resolved.recipient_user_id,
            resolved.connection_id,
        ) == (
            ownership.subject_id,
            ownership.recipient_user_id,
            ownership.connection_id,
        )
        assert notifier.binding == channels.DeliveryEndpointBinding(
            subject_id=resolved.subject_id,
            recipient_user_id=resolved.recipient_user_id,
            integration_connection_id=resolved.connection_id,
            channel=IntegrationProvider.TELEGRAM.value,
        )
        return notifier

    return _build


def _notifier_resolver(notifier):
    def _resolve(binding, _credential_ref):
        assert binding == notifier.binding
        return notifier

    return _resolve


async def _configure_platform(session, roots, *, quota_limit=100_000_000, periods=True):
    root = PlatformIntegrationConnection(
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=f"question-{uuid.uuid4().hex}",
        credential_ref="env:VITALS_OPENROUTER_API_KEY",
        status=IntegrationConnectionStatus.ACTIVE.value,
        config_version=1,
        configured_by_user_id=roots.user_id,
    )
    session.add(root)
    if periods:
        session.add_all(
            [
                AIPlatformQuotaPeriod(
                    period_start=PERIOD_START,
                    period_end=PERIOD_END,
                    cost_limit_microunits=quota_limit,
                    unit_limit=10_000_000,
                    configured_by_user_id=roots.user_id,
                ),
                AISubjectQuotaPeriod(
                    subject_id=roots.subject_id,
                    period_start=PERIOD_START,
                    period_end=PERIOD_END,
                    cost_limit_microunits=quota_limit,
                    unit_limit=10_000_000,
                    configured_by_user_id=roots.user_id,
                ),
            ]
        )
    await session.commit()
    return root


async def _ownership(session, roots) -> ProactiveOwnershipContext:
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
    )


async def _raw(session, roots, ownership, *, suffix, payload=None, connection_id=None):
    update_id = int(uuid.uuid4().int % 1_000_000_000)
    message_id = int(uuid.uuid4().int % 1_000_000_000)
    row = RawPayload(
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id,
        integration_connection_id=connection_id or ownership.connection_id,
        domain=Domain.SIGNALS.value,
        source=Source.TELEGRAM.value,
        external_id=f"tg-question-{suffix}-{uuid.uuid4().hex}",
        fetched_at=NOW.replace(tzinfo=None),
        processed_at=None,
        payload=payload
        or {
            "update_id": update_id,
            "message": {
                "message_id": message_id,
                "date": int(NOW.timestamp()),
                "chat": {"id": 424242, "type": "private"},
                "from": {"id": 424242, "is_bot": False},
                "text": QUESTION,
            },
        },
    )
    session.add(row)
    await session.flush()
    return row


def _llm(session, observed, *, behavior="success"):
    class FakeLLM:
        def __init__(self, config):
            observed["credentials"].append(config.openrouter_api_key)

        async def complete_text_with_usage(self, prompt, *, model, system, max_tokens):
            observed["calls"] += 1
            observed["no_tx"].append(not session.in_transaction())
            observed["prompts"].append(prompt)
            observed["models"].append(model)
            observed["systems"].append(system)
            observed["max_tokens"].append(max_tokens)
            if behavior == "provider_error":
                raise RuntimeError("provider leaked detail: " + SECRET)
            return LLMCallResult(
                value="  " if behavior == "blank" else ANSWER,
                upstream_request_id="question-request-1",
                model=model,
                input_tokens=12,
                output_tokens=8,
                cost_microunits=None if behavior == "missing_usage" else 50,
            )

    return FakeLLM


def _observed():
    return {
        "calls": 0,
        "no_tx": [],
        "credentials": [],
        "prompts": [],
        "models": [],
        "systems": [],
        "max_tokens": [],
    }


async def _direct_terminal(session, prepared, *, behavior="success"):
    observed = _observed()
    lease = await question_ai_service.start_question_dispatch(
        session, prepared, credential_resolver=lambda _ref: SECRET
    )
    await session.commit()
    completion = await question_ai_service.render_question_reply(
        prepared, lease, llm_factory=_llm(session, observed, behavior=behavior)
    )
    result = await question_ai_service.persist_question_reply(session, prepared, completion)
    await session.commit()
    return result, observed


async def test_t1_t2_provider_t3_has_exact_roots_accounting_and_redacted_capabilities(
    db_session, legacy_owner_roots
):
    root = await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, ownership, suffix="flow")
    await db_session.commit()

    prepared = await question_ai_service.prepare_live_question_reply(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        context=PROMPT_CONTEXT,
        facts=PROMPT_FACTS,
    )
    await db_session.commit()
    duplicate = await question_ai_service.prepare_live_question_reply(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        context=PROMPT_CONTEXT,
        facts=PROMPT_FACTS,
    )
    await db_session.commit()
    assert duplicate.invocation_id == prepared.invocation_id
    for value in (QUESTION, PROMPT_CONTEXT, PROMPT_FACTS, SECRET):
        assert value not in repr(prepared)
    with pytest.raises(TypeError):
        pickle.dumps(prepared)

    result, observed = await _direct_terminal(db_session, prepared)
    assert observed == {
        "calls": 1,
        "no_tx": [True],
        "credentials": [SECRET],
        "prompts": [
            f"Последние сообщения бота:\n{PROMPT_CONTEXT}\n\n"
            f"Данные последнего разбора дня (JSON):\n{PROMPT_FACTS}\n\n"
            f"Вопрос:\n{QUESTION}"
        ],
        "models": [MODEL],
        "systems": [question_ai_service._REPLY_SYSTEM],
        "max_tokens": [800],
    }
    assert result.status is AIInvocationStatus.SUCCEEDED and result.text == ANSWER
    assert ANSWER not in repr(result)
    with pytest.raises(TypeError):
        pickle.dumps(result)

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
        invocation.model,
        invocation.input_tokens,
        invocation.output_tokens,
        invocation.cost_microunits,
        invocation.charged_cost_microunits,
    ) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
        raw.id,
        root.id,
        AIInvocationPurpose.QUESTION_REPLY.value,
        AIInvocationSource.TELEGRAM.value,
        AIInvocationStatus.SUCCEEDED.value,
        MODEL,
        12,
        8,
        50,
        invocation.reserved_cost_microunits,
    )
    invocation_id = invocation.id
    await db_session.commit()
    prepared_delivery = await delivery.prepare_delivery_intent(
        db_session,
        _Notifier(ownership),
        text=ANSWER,
        category=delivery.CATEGORY_REPLY,
        idempotency_key=question_ai_service.delivery_dedupe_key(raw.id),
        ownership=ownership,
        raw_payload_id=raw.id,
        ai_invocation_id=invocation_id,
        redact_journal_content=True,
    )
    assert prepared_delivery is not None
    assert ANSWER not in repr(prepared_delivery)
    with pytest.raises(TypeError):
        pickle.dumps(prepared_delivery)
    await db_session.rollback()

    other_raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="wrong-journal-raw",
    )
    other_raw.processed_at = NOW.replace(tzinfo=None)
    await db_session.commit()
    with pytest.raises(delivery.DeliveryScopeError):
        await delivery.prepare_delivery_intent(
            db_session,
            _Notifier(ownership),
            text=ANSWER,
            category=delivery.CATEGORY_REPLY,
            idempotency_key=delivery.make_delivery_idempotency_key(
                "test-question-wrong-raw",
                other_raw.id,
            ),
            ownership=ownership,
            raw_payload_id=other_raw.id,
            ai_invocation_id=invocation_id,
            redact_journal_content=True,
        )


@pytest.mark.parametrize(
    ("behavior", "status"),
    (
        ("blank", AIInvocationStatus.FAILED),
        ("missing_usage", AIInvocationStatus.FAILED),
        ("provider_error", AIInvocationStatus.AMBIGUOUS),
    ),
)
async def test_blank_missing_usage_and_provider_error_are_one_call_sanitized_terminals(
    db_session, legacy_owner_roots, behavior, status
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, ownership, suffix=behavior)
    await db_session.commit()
    prepared = await question_ai_service.prepare_live_question_reply(
        db_session, ownership=ownership, raw_payload_id=raw.id, context="", facts=""
    )
    await db_session.commit()

    result, observed = await _direct_terminal(db_session, prepared, behavior=behavior)
    assert observed["calls"] == 1 and observed["no_tx"] == [True]
    assert result.status is status and result.text is None
    assert SECRET not in repr(result)
    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None
    assert invocation.status == status.value
    assert invocation.error_code in {"invalid_response", "provider_unavailable"}
    assert invocation.charged_cost_microunits == invocation.reserved_cost_microunits


async def test_inbound_reply_journals_only_redacted_payload_and_duplicate_never_reaches_provider(
    db_session, legacy_owner_roots, monkeypatch
):
    root = await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, ownership, suffix="inbound")
    await db_session.commit()
    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    notifier = _Notifier(ownership)

    await inbound._answer_reply(
        db_session, QUESTION, None, notifier=notifier, message_id=123,
        ownership=ownership, raw=raw,
        notifier_resolver=_notifier_resolver(notifier),
    )
    await inbound._answer_reply(
        db_session, QUESTION, None, notifier=notifier, message_id=123,
        ownership=ownership, raw=raw,
        notifier_resolver=_notifier_resolver(notifier),
    )

    assert observed["calls"] == 1
    assert notifier.sent == [{"text": ANSWER, "buttons": None, "reply_to": "123"}]
    row = await db_session.scalar(select(Notification))
    invocation = await db_session.scalar(select(AIInvocation))
    assert row is not None and invocation is not None
    assert (
        row.subject_id, row.actor_user_id, row.recipient_user_id,
        row.integration_connection_id, row.channel, row.category,
        row.ai_invocation_id, row.dedupe_key, row.payload,
    ) == (
        legacy_owner_roots.subject_id, None, legacy_owner_roots.user_id,
        ownership.connection_id, IntegrationProvider.TELEGRAM.value, delivery.CATEGORY_REPLY,
        invocation.id, question_ai_service.delivery_dedupe_key(raw.id),
        {"content_redacted": True, "raw_payload_id": raw.id},
    )
    serialized = repr(row.payload)
    for value in (QUESTION, ANSWER, PROMPT_CONTEXT, PROMPT_FACTS, SECRET):
        assert value not in serialized
    assert root.id == invocation.platform_integration_connection_id
    assert len(row.dedupe_key) == 64
    assert set(row.dedupe_key) <= set("0123456789abcdef")


async def test_crash_after_question_delivery_t1_rearms_only_generic_redacted_fallback(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, ownership, suffix="t1-crash")
    await db_session.commit()
    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    notifier = _Notifier(ownership)
    real_start = delivery.start_delivery_dispatch

    async def _crash_after_t1(*_args, **_kwargs):
        raise RuntimeError("synthetic process death after T1")

    monkeypatch.setattr(delivery, "start_delivery_dispatch", _crash_after_t1)
    with pytest.raises(RuntimeError, match="process death"):
        await inbound._answer_reply(
            db_session,
            QUESTION,
            None,
            notifier=notifier,
            message_id=raw.payload["message"]["message_id"],
            ownership=ownership,
            raw=raw,
            notifier_resolver=_notifier_resolver(notifier),
        )
    await db_session.rollback()
    assert observed["calls"] == 1
    assert notifier.sent == []

    intent = (await db_session.scalars(select(NotificationDeliveryIntent))).one()
    assert intent.status == NotificationDeliveryStatus.PENDING.value
    intent.updated_at = datetime(2000, 1, 1, tzinfo=UTC)
    await db_session.commit()
    monkeypatch.setattr(delivery, "start_delivery_dispatch", real_start)

    recovered = await inbound._recover_claimed_question(
        db_session,
        raw=raw,
        notifier=notifier,
        ownership=ownership,
        notifier_resolver=_notifier_resolver(notifier),
    )

    assert recovered is True
    assert observed["calls"] == 1
    assert notifier.sent == [
        {
            "text": inbound._NO_LLM_REPLY,
            "buttons": None,
            "reply_to": str(raw.payload["message"]["message_id"]),
        }
    ]
    await db_session.refresh(intent)
    assert intent.status == NotificationDeliveryStatus.SENT.value
    journal = (await db_session.scalars(select(Notification))).one()
    assert journal.delivery_intent_id == intent.id
    assert journal.payload == {
        "content_redacted": True,
        "raw_payload_id": raw.id,
    }
    for private in (QUESTION, ANSWER, SECRET):
        assert private not in repr(journal.payload)


@pytest.mark.parametrize(
    "delivery_status",
    (
        NotificationDeliveryStatus.PENDING,
        NotificationDeliveryStatus.DISPATCHING,
        NotificationDeliveryStatus.SENT,
        NotificationDeliveryStatus.AMBIGUOUS,
        NotificationDeliveryStatus.CANCELLED,
    ),
)
async def test_every_durable_claim_state_suppresses_question_recovery_and_network(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    delivery_status,
):
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix=f"delivery-{delivery_status.value}",
    )
    raw.processed_at = NOW.replace(tzinfo=None)
    await db_session.commit()
    notifier = _Notifier(ownership)
    prepared = await delivery.prepare_delivery_intent(
        db_session,
        notifier,
        text=inbound._NO_LLM_REPLY,
        category=delivery.CATEGORY_REPLY,
        idempotency_key=question_ai_service.delivery_dedupe_key(raw.id),
        legacy_dedupe_key=question_ai_service.legacy_delivery_dedupe_key(raw.id),
        reply_to=str(raw.payload["message"]["message_id"]),
        ownership=ownership,
        raw_payload_id=raw.id,
        redact_journal_content=True,
    )
    assert prepared is not None
    await db_session.commit()

    transport_attempts = 0
    if delivery_status is not NotificationDeliveryStatus.PENDING:
        if delivery_status is NotificationDeliveryStatus.CANCELLED:
            lease = await delivery.start_delivery_dispatch(
                db_session,
                prepared,
                notifier_resolver=lambda _binding, _credential_ref: None,
            )
            assert lease is None
            await db_session.commit()
        else:
            lease = await delivery.start_delivery_dispatch(
                db_session,
                prepared,
                notifier_resolver=lambda _binding, _credential_ref: notifier,
            )
            assert lease is not None
            await db_session.commit()
            if delivery_status in {
                NotificationDeliveryStatus.SENT,
                NotificationDeliveryStatus.AMBIGUOUS,
            }:
                if delivery_status is NotificationDeliveryStatus.AMBIGUOUS:
                    async def _ambiguous_send(text, *, buttons=None, reply_to=None):
                        nonlocal transport_attempts
                        del text, buttons, reply_to
                        transport_attempts += 1
                        raise RuntimeError("synthetic transport failure")

                    notifier.send = _ambiguous_send
                completion = await delivery.dispatch_delivery(lease)
                if delivery_status is NotificationDeliveryStatus.SENT:
                    transport_attempts = len(notifier.sent)
                await delivery.finalize_delivery(db_session, completion)
                await db_session.commit()

    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    attempts_before_recovery = transport_attempts + len(notifier.sent)
    recovered = await inbound._recover_claimed_question(
        db_session,
        raw=raw,
        notifier=notifier,
        ownership=ownership,
        notifier_resolver=_notifier_resolver(notifier),
    )

    assert recovered is False
    assert observed["calls"] == 0
    assert transport_attempts + len(notifier.sent) == attempts_before_recovery
    claim = await delivery.delivery_claim_for_raw(
        db_session,
        raw_payload_id=raw.id,
        category=delivery.CATEGORY_REPLY,
        ownership=ownership,
    )
    assert claim is not None and claim.status == delivery_status.value


async def test_reply_to_ai_answer_redacts_nested_bot_text_before_raw_persistence(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    first_raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="nested-reply-first",
    )
    await db_session.commit()
    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    notifier = _Notifier(ownership)
    await inbound._answer_reply(
        db_session,
        QUESTION,
        None,
        notifier=notifier,
        message_id=first_raw.payload["message"]["message_id"],
        ownership=ownership,
        raw=first_raw,
        notifier_resolver=_notifier_resolver(notifier),
    )

    update = {
        "update_id": 91_001,
        "message": {
            "message_id": 91_002,
            "date": int(NOW.timestamp()),
            "chat": {"id": 424242, "type": "private"},
            "from": {"id": 424242, "is_bot": False},
            "text": "А сейчас почему?",
            "reply_to_message": {
                "message_id": 801,
                "date": int(NOW.timestamp()),
                "chat": {"id": 424242, "type": "private"},
                "from": {"id": 111, "is_bot": True},
                "text": ANSWER,
            },
        },
    }
    await inbound.handle_update(
        db_session,
        update,
        notifier=notifier,
        ownership=ownership,
        notifier_resolver=_notifier_resolver(notifier),
    )

    stored = await db_session.scalar(
        select(RawPayload).where(RawPayload.external_id == "tg:91001")
    )
    assert stored is not None
    assert stored.payload["message"]["reply_to_message"] == {"message_id": 801}
    assert ANSWER not in json.dumps(stored.payload, ensure_ascii=False)
    assert observed["calls"] == 2
    journals = list(await db_session.scalars(select(Notification).order_by(Notification.id)))
    assert len(journals) == 2
    assert all(set(row.payload) == {"content_redacted", "raw_payload_id"} for row in journals)


@pytest.mark.parametrize("state", (AIInvocationStatus.PREPARED, AIInvocationStatus.DISPATCHING))
async def test_inherited_prepared_or_dispatching_invocation_is_never_dispatched(
    db_session, legacy_owner_roots, monkeypatch, state
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, ownership, suffix=state.value)
    await db_session.commit()
    prepared = await question_ai_service.prepare_live_question_reply(
        db_session, ownership=ownership, raw_payload_id=raw.id, context="", facts=""
    )
    await db_session.commit()
    if state is AIInvocationStatus.DISPATCHING:
        await question_ai_service.start_question_dispatch(
            db_session, prepared, credential_resolver=lambda _ref: SECRET
        )
        await db_session.commit()
    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    notifier = _Notifier(ownership)

    await inbound._answer_reply(
        db_session, QUESTION, None, notifier=notifier, message_id=1,
        ownership=ownership, raw=raw,
        notifier_resolver=_notifier_resolver(notifier),
    )
    assert observed["calls"] == 0 and notifier.sent == []
    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None and invocation.status == state.value


@pytest.mark.parametrize("mode", ("configuration", "quota"))
async def test_configuration_or_quota_never_calls_provider_and_uses_unpaid_fallback(
    db_session, legacy_owner_roots, monkeypatch, mode
):
    if mode == "configuration":
        await _configure_platform(db_session, legacy_owner_roots, periods=False)
    else:
        await _configure_platform(db_session, legacy_owner_roots, quota_limit=0)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, ownership, suffix=mode)
    await db_session.commit()
    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    notifier = _Notifier(ownership)

    await inbound._answer_reply(
        db_session, QUESTION, None, notifier=notifier, message_id=1,
        ownership=ownership, raw=raw,
        notifier_resolver=_notifier_resolver(notifier),
    )
    assert observed["calls"] == 0
    assert notifier.sent == [{"text": inbound._NO_LLM_REPLY, "buttons": None, "reply_to": "1"}]
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 0


@pytest.mark.parametrize("mode", ("credential_failure", "module_disabled"))
async def test_t2_failure_cancels_reservation_and_immediately_journals_fallback(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    mode,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix=f"t2-{mode}",
    )
    await db_session.commit()
    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    original_start = question_ai_service.start_question_dispatch

    async def fail_start(session, prepared):
        if mode == "module_disabled":
            await modules_service.set_module_enabled(
                session,
                key="signals",
                enabled=False,
                subject_id=ownership.subject_id,
            )
            await session.commit()
            return await original_start(session, prepared)
        raise ai_gateway_service.AIGatewayConfigurationError(
            "synthetic credential failure"
        )

    monkeypatch.setattr(question_ai_service, "start_question_dispatch", fail_start)
    notifier = _Notifier(ownership)
    await inbound._answer_reply(
        db_session,
        QUESTION,
        None,
        notifier=notifier,
        message_id=1,
        ownership=ownership,
        raw=raw,
        notifier_resolver=_notifier_resolver(notifier),
    )

    assert observed["calls"] == 0
    invocation = await db_session.scalar(select(AIInvocation))
    journal = await db_session.scalar(select(Notification))
    assert invocation is not None
    assert invocation.status == AIInvocationStatus.CANCELLED.value
    if mode == "module_disabled":
        assert notifier.sent == []
        assert journal is None
    else:
        assert notifier.sent == [
            {"text": inbound._NO_LLM_REPLY, "buttons": None, "reply_to": "1"}
        ]
        assert journal is not None
        assert journal.ai_invocation_id == invocation.id
        assert journal.payload == {
            "content_redacted": True,
            "raw_payload_id": raw.id,
        }


async def test_disable_after_question_t3_cannot_resurrect_fallback_on_reenable(
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
        suffix="disable-after-t3",
    )
    await db_session.commit()
    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    real_persist = question_ai_service.persist_question_reply

    async def _persist_then_disable(session, prepared, completion):
        result = await real_persist(session, prepared, completion)
        await modules_service.set_module_enabled(
            session,
            key="signals",
            enabled=False,
            subject_id=ownership.subject_id,
        )
        return result

    monkeypatch.setattr(
        question_ai_service,
        "persist_question_reply",
        _persist_then_disable,
    )
    notifier = _Notifier(ownership)
    await inbound._answer_reply(
        db_session,
        QUESTION,
        None,
        notifier=notifier,
        message_id=1,
        ownership=ownership,
        raw=raw,
        notifier_resolver=_notifier_resolver(notifier),
    )

    invocation = (await db_session.scalars(select(AIInvocation))).one()
    intent = (await db_session.scalars(select(NotificationDeliveryIntent))).one()
    assert observed["calls"] == 1
    assert invocation.status == AIInvocationStatus.SUCCEEDED.value
    assert intent.status == NotificationDeliveryStatus.CANCELLED.value
    assert intent.error_code == NotificationDeliveryErrorCode.CANCELLED_BY_POLICY.value
    assert notifier.sent == []

    await modules_service.set_module_enabled(
        db_session,
        key="signals",
        enabled=True,
        subject_id=ownership.subject_id,
    )
    await db_session.commit()
    recovered = await inbound._recover_claimed_question(
        db_session,
        raw=raw,
        notifier=notifier,
        ownership=ownership,
        notifier_resolver=_notifier_resolver(notifier),
    )

    assert recovered is False
    await db_session.refresh(intent)
    assert intent.status == NotificationDeliveryStatus.CANCELLED.value
    assert notifier.sent == []


async def test_no_notifier_and_disabled_module_are_zero_network_paths(
    db_session, legacy_owner_roots, monkeypatch
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, ownership, suffix="no-notifier")
    await db_session.commit()
    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    await inbound._answer_reply(
        db_session, QUESTION, None, notifier=None, message_id=1,
        ownership=ownership, raw=raw,
    )
    assert observed["calls"] == 0
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 0

    await modules_service.set_module_enabled(
        db_session,
        key="signals",
        enabled=False,
        subject_id=ownership.subject_id,
    )
    await db_session.commit()
    notifier = _Notifier(ownership)
    update = {
        "update_id": 999001,
        "message": {
            "message_id": 77, "date": int(NOW.timestamp()),
            "chat": {"id": 424242, "type": "private"},
            "from": {"id": 424242, "is_bot": False}, "text": QUESTION,
        },
    }
    await inbound.handle_update(
        db_session,
        update,
        notifier=notifier,
        ownership=ownership,
        notifier_resolver=_notifier_resolver(notifier),
    )
    assert observed["calls"] == 0 and notifier.sent == []
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 0


async def test_module_disabled_during_send_withdraws_answer_and_redacts_journal(
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
        suffix="disable-during-send",
    )
    await db_session.commit()
    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))

    class DisablingNotifier(_Notifier):
        async def send(self, text, *, buttons=None, reply_to=None) -> str:
            external_id = await super().send(
                text,
                buttons=buttons,
                reply_to=reply_to,
            )
            await modules_service.set_module_enabled(
                db_session,
                key="signals",
                enabled=False,
                subject_id=ownership.subject_id,
            )
            await db_session.commit()
            return external_id

    notifier = DisablingNotifier(ownership)
    await inbound._answer_reply(
        db_session,
        QUESTION,
        None,
        notifier=notifier,
        message_id=1,
        ownership=ownership,
        raw=raw,
        notifier_resolver=_notifier_resolver(notifier),
    )

    assert observed["calls"] == 1
    assert notifier.sent[0]["text"] == ANSWER
    assert notifier.edited == [
        {
            "external_id": "801",
            "text": t("telegram.question_reply_withdrawn"),
            "buttons": None,
        }
    ]
    journal = await db_session.scalar(select(Notification))
    assert journal is not None
    assert journal.payload == {
        "content_redacted": True,
        "raw_payload_id": raw.id,
    }


@pytest.mark.parametrize("tamper", ("payload", "actor_root", "raw_link"))
async def test_tampered_question_journal_fails_closed(db_session, legacy_owner_roots, monkeypatch, tamper):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, ownership, suffix=f"journal-{tamper}")
    await db_session.commit()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, _observed()))
    notifier = _Notifier(ownership)
    await inbound._answer_reply(
        db_session, QUESTION, None, notifier=notifier, message_id=1,
        ownership=ownership, raw=raw,
        notifier_resolver=_notifier_resolver(notifier),
    )
    row = await db_session.scalar(select(Notification))
    assert row is not None
    if tamper == "payload":
        row.payload = {"text": ANSWER}
    elif tamper == "actor_root":
        row.actor_user_id = legacy_owner_roots.user_id
    else:
        other_raw = await _raw(
            db_session,
            legacy_owner_roots,
            ownership,
            suffix="journal-other-raw",
        )
        other_raw.processed_at = NOW.replace(tzinfo=None)
        await db_session.flush()
        row.payload = {"content_redacted": True, "raw_payload_id": other_raw.id}
    await db_session.commit()
    with pytest.raises(question_ai_service.QuestionAIOwnershipError):
        await question_ai_service.delivery_is_journaled(
            db_session, raw_payload_id=raw.id, ownership=ownership
        )
    await db_session.rollback()


async def test_historical_raw_telegram_connection_can_answer_through_current_connection(
    db_session, legacy_owner_roots, monkeypatch
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    historical = IntegrationConnection(
        subject_id=legacy_owner_roots.subject_id,
        provider=IntegrationProvider.TELEGRAM.value,
        connection_type=IntegrationConnectionType.RECIPIENT.value,
        external_account_discriminator=f"historical-{uuid.uuid4().hex}",
        status=IntegrationConnectionStatus.RETIRED.value,
        retired_at=NOW,
    )
    db_session.add(historical)
    await db_session.flush()
    raw = await _raw(
        db_session, legacy_owner_roots, ownership, suffix="historical", connection_id=historical.id
    )
    await db_session.commit()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, _observed()))
    notifier = _Notifier(ownership)
    await inbound._answer_reply(
        db_session, QUESTION, None, notifier=notifier, message_id=1,
        ownership=ownership, raw=raw,
        notifier_resolver=_notifier_resolver(notifier),
    )
    invocation = await db_session.scalar(select(AIInvocation))
    journal = await db_session.scalar(select(Notification))
    assert invocation is not None and journal is not None
    assert invocation.raw_payload_id == raw.id
    assert journal.integration_connection_id == ownership.connection_id


async def test_newer_telegram_edit_blocks_stale_completion_and_delivery(
    db_session, legacy_owner_roots
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, ownership, suffix="stale")
    await db_session.commit()
    prepared = await question_ai_service.prepare_live_question_reply(
        db_session, ownership=ownership, raw_payload_id=raw.id, context="", facts=""
    )
    await db_session.commit()
    lease = await question_ai_service.start_question_dispatch(
        db_session, prepared, credential_resolver=lambda _ref: SECRET
    )
    await db_session.commit()
    completion = await question_ai_service.render_question_reply(
        prepared, lease, llm_factory=_llm(db_session, _observed())
    )
    payload = dict(raw.payload)
    payload["update_id"] = int(payload["update_id"]) + 1
    payload["edited_message"] = dict(payload.pop("message"))
    newer = await _raw(
        db_session, legacy_owner_roots, ownership, suffix="newer-edit", payload=payload
    )
    assert newer.id != raw.id
    await db_session.commit()
    with pytest.raises(question_ai_service.QuestionAIStaleError):
        await question_ai_service.persist_question_reply(db_session, prepared, completion)
    await db_session.rollback()
    invocation = await db_session.get(AIInvocation, prepared.invocation_id)
    assert invocation is not None and invocation.status == AIInvocationStatus.DISPATCHING.value
    assert await db_session.scalar(select(func.count()).select_from(Notification)) == 0


async def test_recovery_cursor_scans_past_non_question_head_rows(
    db_session,
    legacy_owner_roots,
    session_factory,
    redis,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    for index in range(120):
        await _raw(
            db_session,
            legacy_owner_roots,
            ownership,
            suffix=f"ordinary-{index}",
            payload={
                "update_id": 10_000 + index,
                "message": {
                    "message_id": 20_000 + index,
                    "date": int(NOW.timestamp()),
                    "chat": {"id": 424242, "type": "private"},
                    "from": {"id": 424242, "is_bot": False},
                    "text": "голова болит",
                },
            },
        )
    question_raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="past-head-question",
    )
    await db_session.commit()
    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    notifier = _Notifier(ownership)
    monkeypatch.setattr(
        channels,
        "build_legacy_bound_notifier",
        _notifier_builder(notifier, ownership),
    )

    await inbound.question_reply_recovery_job(session_factory,
        redis,
        notifier_resolver=_notifier_resolver(notifier),
    )

    assert observed["calls"] == 1
    assert notifier.sent == [
        {"text": ANSWER, "buttons": None, "reply_to": str(
            question_raw.payload["message"]["message_id"]
        )}
    ]
    journal = await db_session.scalar(
        select(Notification).where(
            Notification.dedupe_key
            == question_ai_service.delivery_dedupe_key(question_raw.id)
        )
    )
    assert journal is not None
    assert journal.payload == {
        "content_redacted": True,
        "raw_payload_id": question_raw.id,
    }


async def test_recovery_without_redis_scans_beyond_cached_scan_limit(
    db_session,
    legacy_owner_roots,
    session_factory,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    monkeypatch.setattr(inbound, "_QUESTION_RECOVERY_SCAN_LIMIT", 2)
    for index in range(3):
        await _raw(
            db_session,
            legacy_owner_roots,
            ownership,
            suffix=f"no-redis-ordinary-{index}",
            payload={
                "update_id": 30_000 + index,
                "message": {
                    "message_id": 40_000 + index,
                    "date": int(NOW.timestamp()),
                    "chat": {"id": 424242, "type": "private"},
                    "from": {"id": 424242, "is_bot": False},
                    "text": "голова болит",
                },
            },
        )
    question_raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="no-redis-past-limit",
    )
    await db_session.commit()
    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    notifier = _Notifier(ownership)
    monkeypatch.setattr(
        channels,
        "build_legacy_bound_notifier",
        _notifier_builder(notifier, ownership),
    )

    await inbound.question_reply_recovery_job(session_factory,
        None,
        notifier_resolver=_notifier_resolver(notifier),
    )

    assert observed["calls"] == 1
    journal = await db_session.scalar(
        select(Notification).where(
            Notification.dedupe_key
            == question_ai_service.delivery_dedupe_key(question_raw.id)
        )
    )
    assert journal is not None


async def test_malformed_unclaimed_reply_isolated_before_later_question_recovery(
    db_session,
    legacy_owner_roots,
    session_factory,
    redis,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    malformed_raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="malformed-unclaimed-reply",
        payload={
            "update_id": 45_001,
            "message": {
                "message_id": 45_002,
                "date": int(NOW.timestamp()),
                "chat": {"id": 424242, "type": "private"},
                "from": {"id": 424242, "is_bot": False},
                "text": QUESTION,
                "reply_to_message": "not-an-object",
            },
        },
    )
    valid_raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="question-after-malformed-unclaimed-reply",
    )
    valid_reply_to = str(valid_raw.payload["message"]["message_id"])
    await db_session.commit()
    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    monkeypatch.setattr(inbound, "_QUESTION_RECOVERY_PAGE_SIZE", 2)
    monkeypatch.setattr(inbound, "_QUESTION_RECOVERY_SCAN_LIMIT", 2)
    notifier = _Notifier(ownership)
    monkeypatch.setattr(
        channels,
        "build_legacy_bound_notifier",
        _notifier_builder(notifier, ownership),
    )

    await inbound.question_reply_recovery_job(session_factory,
        redis,
        notifier_resolver=_notifier_resolver(notifier),
    )

    assert observed["calls"] == 1
    assert notifier.sent == [
        {"text": ANSWER, "buttons": None, "reply_to": valid_reply_to}
    ]
    await db_session.refresh(malformed_raw)
    assert malformed_raw.processed_at is None
    journal = await db_session.scalar(
        select(Notification).where(
            Notification.dedupe_key
            == question_ai_service.delivery_dedupe_key(valid_raw.id)
        )
    )
    assert journal is not None


@pytest.mark.parametrize("cursor_mode", ("none", "persistent", "write_failure"))
async def test_stale_delivery_recovery_scans_past_unclassifiable_intent_pages(
    db_session,
    legacy_owner_roots,
    session_factory,
    redis,
    monkeypatch,
    cursor_mode,
):
    ownership = await _ownership(db_session, legacy_owner_roots)
    notifier = _Notifier(ownership)
    monkeypatch.setattr(inbound, "_QUESTION_RECOVERY_PAGE_SIZE", 2)
    monkeypatch.setattr(inbound, "_QUESTION_RECOVERY_SCAN_LIMIT", 2)
    monkeypatch.setattr(
        channels,
        "build_legacy_bound_notifier",
        _notifier_builder(notifier, ownership),
    )

    invalid_intent_ids = []
    for index in range(3):
        raw = await _raw(
            db_session,
            legacy_owner_roots,
            ownership,
            suffix=f"unclassifiable-stale-intent-{index}",
            payload={
                "update_id": 50_000 + index,
                "message": {
                    "message_id": 60_000 + index,
                    "date": int(NOW.timestamp()),
                    "chat": {"id": 424242, "type": "private"},
                    "from": {"id": 424242, "is_bot": False},
                    "text": "голова болит",
                },
            },
        )
        raw.processed_at = NOW.replace(tzinfo=None)
        await db_session.commit()
        prepared = await delivery.prepare_delivery_intent(
            db_session,
            notifier,
            text=inbound._NO_LLM_REPLY,
            category=delivery.CATEGORY_REPLY,
            idempotency_key=question_ai_service.delivery_dedupe_key(raw.id),
            legacy_dedupe_key=question_ai_service.legacy_delivery_dedupe_key(
                raw.id
            ),
            reply_to=str(raw.payload["message"]["message_id"]),
            ownership=ownership,
            raw_payload_id=raw.id,
            redact_journal_content=True,
        )
        assert prepared is not None
        await db_session.commit()
        intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
        assert intent is not None
        invalid_intent_ids.append(intent.id)
        intent.updated_at = datetime(1990, 1, 1, tzinfo=UTC)
        await db_session.commit()

    command_raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="valid-command-past-stale-intent-head",
        payload={
            "update_id": 50_100,
            "message": {
                "message_id": 60_100,
                "date": int(NOW.timestamp()),
                "chat": {"id": 424242, "type": "private"},
                "from": {"id": 424242, "is_bot": False},
                "text": "/start",
            },
        },
    )
    command_raw.processed_at = NOW.replace(tzinfo=None)
    command_reply_to = str(command_raw.payload["message"]["message_id"])
    await db_session.commit()
    command_key = delivery.make_delivery_idempotency_key(
        "telegram-command-reply",
        command_raw.id,
    )
    prepared_command = await delivery.prepare_delivery_intent(
        db_session,
        notifier,
        text=inbound.COMMAND_REPLY,
        category=delivery.CATEGORY_REPLY,
        idempotency_key=command_key,
        reply_to=command_reply_to,
        ownership=ownership,
        raw_payload_id=command_raw.id,
    )
    assert prepared_command is not None
    await db_session.commit()
    command_intent = await db_session.get(
        NotificationDeliveryIntent,
        prepared_command.intent_id,
    )
    assert command_intent is not None
    command_intent.updated_at = datetime(2000, 1, 1, tzinfo=UTC)
    await db_session.commit()

    first_page = await inbound._recoverable_raw_delivery_candidates(
        db_session,
        ownership=ownership,
        stale_before=datetime(2020, 1, 1, tzinfo=UTC),
        after=None,
        limit=2,
    )
    second_page = await inbound._recoverable_raw_delivery_candidates(
        db_session,
        ownership=ownership,
        stale_before=datetime(2020, 1, 1, tzinfo=UTC),
        after=(first_page[-1].updated_at, first_page[-1].intent_id),
        limit=2,
    )
    assert [row.intent_id for row in [*first_page, *second_page]] == [
        *sorted(invalid_intent_ids),
        prepared_command.intent_id,
    ]
    await db_session.commit()

    if cursor_mode == "write_failure":
        class WriteFailingRedis:
            async def get(self, _key):
                return b""

            async def set(self, _key, _value):
                raise RuntimeError("synthetic Redis write failure")

        cursor_store = WriteFailingRedis()
    else:
        cursor_store = redis if cursor_mode == "persistent" else None
    await inbound.question_reply_recovery_job(session_factory,
        cursor_store,
        notifier_resolver=_notifier_resolver(notifier),
    )
    if cursor_mode == "persistent":
        assert notifier.sent == []
        await inbound.question_reply_recovery_job(session_factory,
            cursor_store,
            notifier_resolver=_notifier_resolver(notifier),
        )

    assert notifier.sent == [
        {
            "text": inbound.COMMAND_REPLY,
            "buttons": None,
            "reply_to": command_reply_to,
        }
    ]
    db_session.expire_all()
    command_intent = await db_session.get(
        NotificationDeliveryIntent,
        prepared_command.intent_id,
    )
    assert command_intent is not None
    assert command_intent.status == NotificationDeliveryStatus.SENT.value
    invalid_statuses = list(
        await db_session.scalars(
            select(NotificationDeliveryIntent.status).where(
                NotificationDeliveryIntent.id.in_(invalid_intent_ids)
            )
        )
    )
    assert invalid_statuses == [NotificationDeliveryStatus.PENDING.value] * 3


@pytest.mark.parametrize("missing_notifier", (False, True))
async def test_disabled_recovery_policy_cancels_stale_claim_and_blocks_reenable(
    db_session,
    legacy_owner_roots,
    session_factory,
    monkeypatch,
    missing_notifier,
):
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="disabled-stale-delivery",
    )
    raw.processed_at = NOW.replace(tzinfo=None)
    await db_session.commit()
    notifier = _Notifier(ownership)
    prepared = await delivery.prepare_delivery_intent(
        db_session,
        notifier,
        text=inbound._NO_LLM_REPLY,
        category=delivery.CATEGORY_REPLY,
        idempotency_key=question_ai_service.delivery_dedupe_key(raw.id),
        legacy_dedupe_key=question_ai_service.legacy_delivery_dedupe_key(raw.id),
        reply_to=str(raw.payload["message"]["message_id"]),
        ownership=ownership,
        raw_payload_id=raw.id,
        redact_journal_content=True,
    )
    assert prepared is not None
    await db_session.commit()
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    assert intent is not None
    intent.updated_at = datetime(2000, 1, 1, tzinfo=UTC)
    await db_session.commit()
    await modules_service.set_module_enabled(
        db_session,
        key="signals",
        enabled=False,
        subject_id=ownership.subject_id,
    )
    await db_session.commit()
    builder = _notifier_builder(notifier, ownership)
    if missing_notifier:
        async def builder(_session, _ownership, *, config=None):
            del _session, _ownership, config
            return None

    monkeypatch.setattr(channels, "build_legacy_bound_notifier", builder)

    await inbound.question_reply_recovery_job(session_factory,
        None,
        notifier_resolver=_notifier_resolver(notifier),
    )

    db_session.expire_all()
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    assert intent is not None
    assert intent.status == NotificationDeliveryStatus.CANCELLED.value
    assert intent.error_code == NotificationDeliveryErrorCode.CANCELLED_BY_POLICY.value
    assert intent.completed_at is not None
    assert notifier.sent == []
    assert await db_session.scalar(select(func.count()).select_from(Notification)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 0

    monkeypatch.setattr(
        channels,
        "build_legacy_bound_notifier",
        _notifier_builder(notifier, ownership),
    )
    await modules_service.set_module_enabled(
        db_session,
        key="signals",
        enabled=True,
        subject_id=ownership.subject_id,
    )
    await db_session.commit()
    await inbound.question_reply_recovery_job(session_factory,
        None,
        notifier_resolver=_notifier_resolver(notifier),
    )

    db_session.expire_all()
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    assert intent is not None
    assert intent.status == NotificationDeliveryStatus.CANCELLED.value
    assert intent.error_code == NotificationDeliveryErrorCode.CANCELLED_BY_POLICY.value
    assert notifier.sent == []
    assert await db_session.scalar(select(func.count()).select_from(Notification)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AIInvocation)) == 0


async def test_malformed_echo_candidate_isolated_before_later_command_recovery(
    db_session,
    legacy_owner_roots,
    session_factory,
    redis,
    monkeypatch,
):
    ownership = await _ownership(db_session, legacy_owner_roots)
    notifier = _Notifier(ownership)
    monkeypatch.setattr(inbound, "_QUESTION_RECOVERY_PAGE_SIZE", 2)
    monkeypatch.setattr(inbound, "_QUESTION_RECOVERY_SCAN_LIMIT", 2)
    monkeypatch.setattr(
        channels,
        "build_legacy_bound_notifier",
        _notifier_builder(notifier, ownership),
    )
    malformed_raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="malformed-stale-echo",
        payload={
            "update_id": 70_001,
            "message": {
                "message_id": 70_002,
                "date": int(NOW.timestamp()),
                "chat": {"id": 424242, "type": "private"},
                "from": {"id": 424242, "is_bot": False},
                "text": "голова болит",
                "reply_to_message": "not-an-object",
            },
        },
    )
    malformed_raw.processed_at = NOW.replace(tzinfo=None)
    await db_session.commit()
    malformed_prepared = await delivery.prepare_delivery_intent(
        db_session,
        notifier,
        text=inbound._NO_SIGNAL_FACTS_REPLY,
        category=delivery.CATEGORY_ECHO,
        idempotency_key=delivery.make_delivery_idempotency_key(
            "telegram-signal-echo",
            malformed_raw.id,
        ),
        reply_to=str(malformed_raw.payload["message"]["message_id"]),
        ownership=ownership,
        raw_payload_id=malformed_raw.id,
    )
    assert malformed_prepared is not None
    await db_session.commit()
    malformed_intent = await db_session.get(
        NotificationDeliveryIntent,
        malformed_prepared.intent_id,
    )
    assert malformed_intent is not None
    malformed_intent.updated_at = datetime(1990, 1, 1, tzinfo=UTC)
    await db_session.commit()

    command_raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="command-after-malformed-echo",
        payload={
            "update_id": 70_003,
            "message": {
                "message_id": 70_004,
                "date": int(NOW.timestamp()),
                "chat": {"id": 424242, "type": "private"},
                "from": {"id": 424242, "is_bot": False},
                "text": "/start",
            },
        },
    )
    command_raw.processed_at = NOW.replace(tzinfo=None)
    await db_session.commit()
    command_prepared = await delivery.prepare_delivery_intent(
        db_session,
        notifier,
        text=inbound.COMMAND_REPLY,
        category=delivery.CATEGORY_REPLY,
        idempotency_key=delivery.make_delivery_idempotency_key(
            "telegram-command-reply",
            command_raw.id,
        ),
        reply_to=str(command_raw.payload["message"]["message_id"]),
        ownership=ownership,
        raw_payload_id=command_raw.id,
    )
    assert command_prepared is not None
    await db_session.commit()
    command_intent = await db_session.get(
        NotificationDeliveryIntent,
        command_prepared.intent_id,
    )
    assert command_intent is not None
    command_intent.updated_at = datetime(2000, 1, 1, tzinfo=UTC)
    await db_session.commit()

    await inbound.question_reply_recovery_job(session_factory,
        redis,
        notifier_resolver=_notifier_resolver(notifier),
    )

    assert notifier.sent == [
        {
            "text": inbound.COMMAND_REPLY,
            "buttons": None,
            "reply_to": str(command_raw.payload["message"]["message_id"]),
        }
    ]
    db_session.expire_all()
    malformed_intent = await db_session.get(
        NotificationDeliveryIntent,
        malformed_prepared.intent_id,
    )
    command_intent = await db_session.get(
        NotificationDeliveryIntent,
        command_prepared.intent_id,
    )
    assert malformed_intent is not None
    assert malformed_intent.status == NotificationDeliveryStatus.PENDING.value
    assert command_intent is not None
    assert command_intent.status == NotificationDeliveryStatus.SENT.value


async def test_recovery_without_redis_does_not_count_journaled_questions_as_work(
    db_session,
    legacy_owner_roots,
    session_factory,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    for index in range(inbound._QUESTION_RECOVERY_WORK_LIMIT + 1):
        raw = await _raw(
            db_session,
            legacy_owner_roots,
            ownership,
            suffix=f"journaled-history-{index}",
        )
        raw.processed_at = NOW.replace(tzinfo=None)
        db_session.add(
            Notification(
                subject_id=ownership.subject_id,
                actor_user_id=None,
                recipient_user_id=ownership.recipient_user_id,
                integration_connection_id=ownership.connection_id,
                sent_at=NOW.replace(tzinfo=None),
                category=delivery.CATEGORY_REPLY,
                dedupe_key=question_ai_service.legacy_delivery_dedupe_key(raw.id),
                channel=IntegrationProvider.TELEGRAM.value,
                external_id=f"historical-reply-{index}",
                payload={"content_redacted": True, "raw_payload_id": raw.id},
            )
        )
    question_raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="after-journaled-history",
    )
    await db_session.commit()
    observed = _observed()
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    notifier = _Notifier(ownership)
    monkeypatch.setattr(
        channels,
        "build_legacy_bound_notifier",
        _notifier_builder(notifier, ownership),
    )

    await inbound.question_reply_recovery_job(session_factory,
        None,
        notifier_resolver=_notifier_resolver(notifier),
    )

    assert observed["calls"] == 1
    assert await db_session.scalar(
        select(Notification.id).where(
            Notification.dedupe_key
            == question_ai_service.delivery_dedupe_key(question_raw.id)
        )
    ) is not None


async def test_terminal_unjournaled_invocation_bypasses_raw_scan_cursor(
    db_session,
    legacy_owner_roots,
    session_factory,
    redis,
    monkeypatch,
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="terminal-gap",
    )
    await db_session.commit()
    prepared = await question_ai_service.prepare_live_question_reply(
        db_session,
        ownership=ownership,
        raw_payload_id=raw.id,
        context="",
        facts="",
    )
    await db_session.commit()
    _result, observed = await _direct_terminal(db_session, prepared)
    await redis.set(
        inbound._question_recovery_cursor_key(legacy_owner_roots.subject_id),
        str(raw.id + 1000),
    )
    monkeypatch.setattr(question_ai_service, "LLMClient", _llm(db_session, observed))
    notifier = _Notifier(ownership)
    monkeypatch.setattr(
        channels,
        "build_legacy_bound_notifier",
        _notifier_builder(notifier, ownership),
    )

    await inbound.question_reply_recovery_job(session_factory,
        redis,
        notifier_resolver=_notifier_resolver(notifier),
    )

    assert observed["calls"] == 1
    assert notifier.sent == [
        {"text": inbound._NO_LLM_REPLY, "buttons": None, "reply_to": str(
            raw.payload["message"]["message_id"]
        )}
    ]
    journal = await db_session.scalar(select(Notification))
    assert journal is not None
    assert journal.ai_invocation_id == prepared.invocation_id
    assert journal.payload == {
        "content_redacted": True,
        "raw_payload_id": raw.id,
    }


@pytest.mark.integration
async def test_postgres_concurrent_stale_delivery_recovery_sends_once(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="concurrent-delivery-recovery",
    )
    raw.processed_at = NOW.replace(tzinfo=None)
    notifier = _Notifier(ownership)
    prepared = await delivery.prepare_delivery_intent(
        db_session,
        notifier,
        text=ANSWER,
        category=delivery.CATEGORY_REPLY,
        idempotency_key=question_ai_service.delivery_dedupe_key(raw.id),
        legacy_dedupe_key=question_ai_service.legacy_delivery_dedupe_key(raw.id),
        reply_to=str(raw.payload["message"]["message_id"]),
        ownership=ownership,
        raw_payload_id=raw.id,
        redact_journal_content=True,
    )
    assert prepared is not None
    await db_session.commit()
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    assert intent is not None
    intent.updated_at = datetime(2000, 1, 1, tzinfo=UTC)
    await db_session.commit()
    monkeypatch.setattr(
        channels,
        "build_legacy_bound_notifier",
        _notifier_builder(notifier, ownership),
    )
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    results = await asyncio.gather(
        inbound._run_question_recovery_raw(
            factory,
            raw_payload_id=raw.id,
            stale_before=datetime(2001, 1, 1, tzinfo=UTC),
            notifier_resolver=_notifier_resolver(notifier),
        ),
        inbound._run_question_recovery_raw(
            factory,
            raw_payload_id=raw.id,
            stale_before=datetime(2001, 1, 1, tzinfo=UTC),
            notifier_resolver=_notifier_resolver(notifier),
        ),
    )

    assert sum(results) == 1
    assert notifier.sent == [
        {
            "text": inbound._NO_LLM_REPLY,
            "buttons": None,
            "reply_to": str(raw.payload["message"]["message_id"]),
        }
    ]
    db_session.expire_all()
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    assert intent is not None and intent.status == NotificationDeliveryStatus.SENT.value
    assert await db_session.scalar(select(func.count()).select_from(Notification)) == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("transport_mode", "expected_error_code"),
    (
        ("exception", NotificationDeliveryErrorCode.TRANSPORT_ERROR.value),
        ("invalid_response", NotificationDeliveryErrorCode.INVALID_RESPONSE.value),
    ),
)
async def test_postgres_concurrent_stale_delivery_recovery_attempts_ambiguous_once(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    transport_mode,
    expected_error_code,
):
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix=f"concurrent-ambiguous-{transport_mode}",
    )
    raw.processed_at = NOW.replace(tzinfo=None)
    notifier = _Notifier(ownership)
    prepared = await delivery.prepare_delivery_intent(
        db_session,
        notifier,
        text=ANSWER,
        category=delivery.CATEGORY_REPLY,
        idempotency_key=question_ai_service.delivery_dedupe_key(raw.id),
        legacy_dedupe_key=question_ai_service.legacy_delivery_dedupe_key(raw.id),
        reply_to=str(raw.payload["message"]["message_id"]),
        ownership=ownership,
        raw_payload_id=raw.id,
        redact_journal_content=True,
    )
    assert prepared is not None
    await db_session.commit()
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    assert intent is not None
    intent.updated_at = datetime(2000, 1, 1, tzinfo=UTC)
    await db_session.commit()

    transport_calls = 0

    async def _ambiguous_send(text, *, buttons=None, reply_to=None):
        nonlocal transport_calls
        del text, buttons, reply_to
        transport_calls += 1
        if transport_mode == "exception":
            raise RuntimeError("synthetic Telegram transport failure")
        return "invalid-message-id"

    notifier.send = _ambiguous_send
    monkeypatch.setattr(
        channels,
        "build_legacy_bound_notifier",
        _notifier_builder(notifier, ownership),
    )
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    results = await asyncio.gather(
        inbound._run_question_recovery_raw(
            factory,
            raw_payload_id=raw.id,
            stale_before=datetime(2001, 1, 1, tzinfo=UTC),
            notifier_resolver=_notifier_resolver(notifier),
        ),
        inbound._run_question_recovery_raw(
            factory,
            raw_payload_id=raw.id,
            stale_before=datetime(2001, 1, 1, tzinfo=UTC),
            notifier_resolver=_notifier_resolver(notifier),
        ),
    )

    assert sum(results) == 1
    assert transport_calls == 1
    db_session.expire_all()
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    assert intent is not None
    assert intent.status == NotificationDeliveryStatus.AMBIGUOUS.value
    assert intent.error_code == expected_error_code
    assert await db_session.scalar(select(func.count()).select_from(Notification)) == 0


@pytest.mark.integration
async def test_postgres_same_raw_concurrent_start_has_one_lease_and_provider_call(
    db_session, legacy_owner_roots
):
    await _configure_platform(db_session, legacy_owner_roots)
    ownership = await _ownership(db_session, legacy_owner_roots)
    raw = await _raw(db_session, legacy_owner_roots, ownership, suffix="concurrent")
    await db_session.commit()
    prepared = await question_ai_service.prepare_live_question_reply(
        db_session, ownership=ownership, raw_payload_id=raw.id, context="", facts=""
    )
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False, class_=AsyncSession)
    observed = _observed()

    async def start_render_persist():
        async with factory() as session:
            try:
                lease = await question_ai_service.start_question_dispatch(
                    session, prepared, credential_resolver=lambda _ref: SECRET
                )
                await session.commit()
            except ai_gateway_service.AIInvocationStateError as exc:
                await session.rollback()
                return exc
            completion = await question_ai_service.render_question_reply(
                prepared, lease, llm_factory=_llm(session, observed)
            )
            result = await question_ai_service.persist_question_reply(session, prepared, completion)
            await session.commit()
            return result

    outcomes = await asyncio.wait_for(
        asyncio.gather(start_render_persist(), start_render_persist()), timeout=10
    )
    assert sum(isinstance(item, question_ai_service.QuestionReplyResult) for item in outcomes) == 1
    assert sum(isinstance(item, ai_gateway_service.AIInvocationStateError) for item in outcomes) == 1
    assert observed["calls"] == 1
