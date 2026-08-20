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
    Source,
)
from vitals.integrations.llm_client import LLMCallResult
from vitals.i18n import t
from vitals.models.ai import AIInvocation, AIPlatformQuotaPeriod, AISubjectQuotaPeriod
from vitals.models.proactive import Notification
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

    def __init__(self):
        self.sent: list[dict[str, object]] = []
        self.edited: list[dict[str, object]] = []

    async def send(self, text, *, buttons=None, reply_to=None) -> str:
        self.sent.append({"text": text, "buttons": buttons, "reply_to": reply_to})
        return str(800 + len(self.sent))

    async def edit(self, external_id, text, *, buttons=None) -> None:
        self.edited.append(
            {"external_id": external_id, "text": text, "buttons": buttons}
        )


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
    prepared_delivery = await delivery._prepare_delivery(
        db_session,
        _Notifier(),
        text=ANSWER,
        category=delivery.CATEGORY_REPLY,
        ownership=ownership,
        ai_invocation_id=invocation.id,
        redact_journal_content=True,
        journal_raw_payload_id=raw.id,
    )
    assert prepared_delivery is not None
    assert ANSWER not in repr(prepared_delivery)
    with pytest.raises(TypeError):
        pickle.dumps(prepared_delivery)

    other_raw = await _raw(
        db_session,
        legacy_owner_roots,
        ownership,
        suffix="wrong-journal-raw",
    )
    other_raw.processed_at = NOW.replace(tzinfo=None)
    await db_session.flush()
    with pytest.raises(delivery.ProactiveOwnershipScopeError):
        await delivery._prepare_delivery(
            db_session,
            _Notifier(),
            text=ANSWER,
            category=delivery.CATEGORY_REPLY,
            ownership=ownership,
            ai_invocation_id=invocation.id,
            redact_journal_content=True,
            journal_raw_payload_id=other_raw.id,
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
    notifier = _Notifier()

    await inbound._answer_reply(
        db_session, QUESTION, None, notifier=notifier, message_id=123,
        ownership=ownership, raw=raw,
    )
    await inbound._answer_reply(
        db_session, QUESTION, None, notifier=notifier, message_id=123,
        ownership=ownership, raw=raw,
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
    notifier = _Notifier()
    await inbound._answer_reply(
        db_session,
        QUESTION,
        None,
        notifier=notifier,
        message_id=first_raw.payload["message"]["message_id"],
        ownership=ownership,
        raw=first_raw,
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
    notifier = _Notifier()

    await inbound._answer_reply(
        db_session, QUESTION, None, notifier=notifier, message_id=1,
        ownership=ownership, raw=raw,
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
    notifier = _Notifier()

    await inbound._answer_reply(
        db_session, QUESTION, None, notifier=notifier, message_id=1,
        ownership=ownership, raw=raw,
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
            )
            await session.commit()
            return await original_start(session, prepared)
        raise ai_gateway_service.AIGatewayConfigurationError(
            "synthetic credential failure"
        )

    monkeypatch.setattr(question_ai_service, "start_question_dispatch", fail_start)
    notifier = _Notifier()
    await inbound._answer_reply(
        db_session,
        QUESTION,
        None,
        notifier=notifier,
        message_id=1,
        ownership=ownership,
        raw=raw,
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

    await modules_service.set_module_enabled(db_session, key="signals", enabled=False)
    await db_session.commit()
    notifier = _Notifier()
    update = {
        "update_id": 999001,
        "message": {
            "message_id": 77, "date": int(NOW.timestamp()),
            "chat": {"id": 424242, "type": "private"},
            "from": {"id": 424242, "is_bot": False}, "text": QUESTION,
        },
    }
    await inbound.handle_update(db_session, update, notifier=notifier, ownership=ownership)
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
            )
            await db_session.commit()
            return external_id

    notifier = DisablingNotifier()
    await inbound._answer_reply(
        db_session,
        QUESTION,
        None,
        notifier=notifier,
        message_id=1,
        ownership=ownership,
        raw=raw,
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
    await inbound._answer_reply(
        db_session, QUESTION, None, notifier=_Notifier(), message_id=1,
        ownership=ownership, raw=raw,
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
    await inbound._answer_reply(
        db_session, QUESTION, None, notifier=_Notifier(), message_id=1,
        ownership=ownership, raw=raw,
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
    notifier = _Notifier()
    monkeypatch.setattr(channels, "build_notifier", lambda: notifier)

    await inbound.question_reply_recovery_job(session_factory, redis)

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
    notifier = _Notifier()
    monkeypatch.setattr(channels, "build_notifier", lambda: notifier)

    await inbound.question_reply_recovery_job(session_factory, None)

    assert observed["calls"] == 1
    journal = await db_session.scalar(
        select(Notification).where(
            Notification.dedupe_key
            == question_ai_service.delivery_dedupe_key(question_raw.id)
        )
    )
    assert journal is not None


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
                dedupe_key=question_ai_service.delivery_dedupe_key(raw.id),
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
    notifier = _Notifier()
    monkeypatch.setattr(channels, "build_notifier", lambda: notifier)

    await inbound.question_reply_recovery_job(session_factory, None)

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
    notifier = _Notifier()
    monkeypatch.setattr(channels, "build_notifier", lambda: notifier)

    await inbound.question_reply_recovery_job(session_factory, redis)

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
