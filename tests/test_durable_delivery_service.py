"""Three-phase, at-most-once Telegram delivery service contracts."""

from __future__ import annotations

from vitals.services.ai_gateway import contracts as ai_gateway_contracts
from vitals.services.ai_gateway import dispatch as ai_gateway_dispatch
from vitals.services.ai_gateway import invocations as ai_gateway_invocations

from vitals.services.proactive.delivery import contracts as delivery_contracts
from vitals.services.proactive.delivery import policy as delivery_policy
from vitals.services.proactive.delivery import queries as delivery_queries
from vitals.services.proactive.delivery import preparation as delivery_preparation
from vitals.services.proactive.delivery import dispatch as delivery_dispatch
from vitals.services.proactive.delivery import reconciliation as delivery_reconciliation
from vitals.services.proactive.delivery import legacy as delivery_legacy

import asyncio
import copy
import pickle
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    NotificationDeliveryStatus,
    Source,
)
from vitals.models.proactive import Notification, NotificationDeliveryIntent
from vitals.models.raw_payload import RawPayload
from vitals.models.scoped_settings import (
    IntegrationConnectionSetting,
    SubjectSetting,
)
from vitals.models.tenancy import IntegrationConnection
from vitals.services.proactive import channels
from vitals.services.proactive.preferences import contracts as preference_contracts
from vitals.services.proactive.preferences import queries as preference_queries
from vitals.services.proactive.ownership import ProactiveOwnershipContext
from vitals.ownership import WriteIdentity
from vitals.persistence import transactions as transaction_outcome


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader or writer does when the ownership backfill has not reached a row yet,
# which is a state the application itself can no longer create. The schema
# says so, so this module asks for the one that stood before the contract.
pytestmark = pytest.mark.pre_ownership_contract


class _BoundFakeNotifier:
    channel = "telegram"

    def __init__(self, binding, *, result="701", error=None):
        self.binding = binding
        self.result = result
        self.error = error
        self.calls = []

    async def send(self, text, *, buttons=None, reply_to=None):
        self.calls.append((text, buttons, reply_to))
        if self.error is not None:
            raise self.error
        return self.result

    async def answer_callback(self, callback_id, text=""):
        del callback_id, text

    async def edit(self, message_id, text, *, buttons=None):
        del message_id, text, buttons


def test_sync_notification_scope_never_allows_omitted_ownership():
    with pytest.raises(TypeError):
        delivery_policy.notification_ownership_scope(None)


@pytest.mark.asyncio
async def test_ai_and_delivery_capabilities_share_exact_root_outcome_registry(
    db_session,
    legacy_owner_roots,
    platform_ai_ready,
    monkeypatch,
):
    del platform_ai_ready
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    identity = WriteIdentity(
        subject_id=ownership.subject_id,
        actor_user_id=ownership.recipient_user_id,
    )
    reservation = await ai_gateway_invocations.reserve_ai_invocation(
        db_session,
        identity=identity,
        purpose=AIInvocationPurpose.WEEKLY_DIGEST,
        source=AIInvocationSource.WEB,
        model="synthetic/model-v1",
        idempotency_key="shared-root-outcome",
        reserved_cost_microunits=10,
        reserved_units=10,
    )
    await db_session.commit()

    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="shared root",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "test", "shared-root"
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    ai_lease = await ai_gateway_dispatch.start_ai_dispatch(
        db_session,
        identity=identity,
        invocation_id=reservation.invocation_id,
        credential_resolver=lambda _ref: "synthetic-secret",
    )
    # The consumed prelock scope keeps one transaction-end invalidator beside
    # the two provider-facing capabilities until this exact root resolves.
    assert transaction_outcome.pending_root_transaction_outcomes(db_session) == 3
    nested = await db_session.begin_nested()
    await nested.commit()

    async def provider_call(_request):
        return {"memory": "payload"}

    with pytest.raises(ai_gateway_contracts.AICapabilityError):
        await ai_gateway_dispatch.dispatch_ai(
            ai_lease,
            provider_call=provider_call,
            usage_extractor=lambda _result: ai_gateway_contracts.SanitizedAIUsage(),
        )
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.start_delivery_dispatch(
            db_session,
            prepared,
            notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
        )
    await db_session.commit()
    assert transaction_outcome.pending_root_transaction_outcomes(db_session) == 0

    ai_completion = await ai_gateway_dispatch.dispatch_ai(
        ai_lease,
        provider_call=provider_call,
        usage_extractor=lambda _result: ai_gateway_contracts.SanitizedAIUsage(
            input_tokens=1,
            output_tokens=1,
            cost_microunits=1,
        ),
    )
    assert ai_completion.payload == {"memory": "payload"}
    delivery_lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        now=datetime(2026, 8, 20, 12, 1),
        notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
    )
    assert delivery_lease is not None
    await db_session.rollback()
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.dispatch_delivery(delivery_lease)


async def _ready(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    *,
    timezone="Asia/Almaty",
    budget=4,
):
    from vitals.models.identity import HealthSubject

    subject = await db_session.get(HealthSubject, legacy_owner_roots.subject_id)
    subject.timezone = timezone
    # No module has to be switched on here any more. The proactive layer used to
    # be gated on ``signals``, which was also the free-text capture domain, and
    # this fixture existed largely to turn it on; both the domain and the gate
    # are gone, and the layer's own preferences decide what it sends.

    async def policy(*_args, **_kwargs):
        return {
            "daily_budget": budget,
            "quiet_start": "02:00",
            "quiet_end": "10:00",
        }

    monkeypatch.setattr(
        preference_queries, "get_locked_delivery_policy", policy, raising=False
    )
    ownership = await channels.resolve_legacy_channel_ownership(
        db_session,
        actor_username=None,
    )
    binding = channels.DeliveryEndpointBinding(
        subject_id=ownership.subject_id,
        recipient_user_id=ownership.recipient_user_id,
        integration_connection_id=ownership.connection_id,
        channel="telegram",
    )
    await db_session.commit()
    return ownership, binding


async def _stale_raw_pending(
    db_session,
    *,
    ownership,
    binding,
    suffix: str,
    category=delivery_contracts.CATEGORY_REPLY,
    text="deterministic reply",
    actor_user_id=None,
    redact_journal_content=False,
):
    raw = RawPayload(
        subject_id=ownership.subject_id,
        actor_user_id=ownership.recipient_user_id,
        integration_connection_id=ownership.connection_id,
        domain="signals",
        source=Source.TELEGRAM.value,
        external_id=f"synthetic-rearm-{suffix}",
        payload={"text": "private inbound payload"},
        processed_at=datetime(2026, 8, 20, 10),
    )
    db_session.add(raw)
    await db_session.flush()
    key = delivery_contracts.make_delivery_idempotency_key(
        f"raw-{category}", raw.id, suffix
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text=text,
        category=category,
        idempotency_key=key,
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
        actor_user_id=actor_user_id,
        raw_payload_id=raw.id,
        redact_journal_content=redact_journal_content,
    )
    assert prepared is not None
    await db_session.commit()
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    intent.updated_at = datetime(2026, 8, 20, 9, tzinfo=UTC)
    await db_session.commit()
    return raw, key, prepared


@pytest.mark.asyncio
async def test_preparation_scope_composes_raw_terminal_state_and_intent_atomically(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    raw = RawPayload(
        subject_id=ownership.subject_id,
        actor_user_id=ownership.recipient_user_id,
        integration_connection_id=ownership.connection_id,
        domain="signals",
        source=Source.TELEGRAM.value,
        external_id="synthetic-composed-t1",
        payload={"text": "private inbound payload"},
    )
    db_session.add(raw)
    await db_session.commit()

    notifier = _BoundFakeNotifier(binding)
    scope = await delivery_preparation.lock_delivery_preparation_scope(
        db_session,
        notifier,
        category=delivery_contracts.CATEGORY_ECHO,
        ownership=ownership,
        now=datetime(2026, 8, 20, 12),
    )
    assert isinstance(scope, delivery_contracts.DeliveryPreparationScope)
    assert repr(scope) == "<DeliveryPreparationScope redacted>"
    with pytest.raises(TypeError):
        pickle.dumps(scope)
    with pytest.raises(TypeError):
        copy.copy(scope)

    locked_raw = await db_session.scalar(
        select(RawPayload).where(RawPayload.id == raw.id).with_for_update()
    )
    locked_raw.processed_at = datetime(2026, 8, 20, 12)
    await db_session.flush()
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        notifier,
        text="deterministic echo",
        category=delivery_contracts.CATEGORY_ECHO,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "test", "composed-raw-t1"
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
        raw_payload_id=raw.id,
        preparation_scope=scope,
    )
    assert prepared is not None
    await db_session.commit()

    persisted_raw = await db_session.get(RawPayload, raw.id)
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    assert persisted_raw.processed_at is not None
    assert intent.raw_payload_id == raw.id
    assert intent.status == NotificationDeliveryStatus.PENDING.value


@pytest.mark.asyncio
async def test_preparation_scope_savepoint_root_rollback_and_close_boundaries(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    notifier = _BoundFakeNotifier(binding)
    scope = await delivery_preparation.lock_delivery_preparation_scope(
        db_session,
        notifier,
        category=delivery_contracts.CATEGORY_TEST,
        ownership=ownership,
        now=datetime(2026, 8, 20, 12),
    )
    nested = await db_session.begin_nested()
    await nested.commit()
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        notifier,
        text="savepoint stays inside root",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "test", "scope-savepoint"
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
        preparation_scope=scope,
    )
    assert prepared is not None
    await db_session.rollback()
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.start_delivery_dispatch(
            db_session,
            prepared,
            notifier_resolver=lambda *_: notifier,
        )
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_preparation.prepare_delivery_intent(
            db_session,
            notifier,
            text="cannot reuse rolled-back scope",
            category=delivery_contracts.CATEGORY_TEST,
            idempotency_key=delivery_contracts.make_delivery_idempotency_key(
                "test", "scope-rollback-reuse"
            ),
            ownership=ownership,
            preparation_scope=scope,
        )

    closed_scope = await delivery_preparation.lock_delivery_preparation_scope(
        db_session,
        notifier,
        category=delivery_contracts.CATEGORY_TEST,
        ownership=ownership,
    )
    await db_session.close()
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_preparation.prepare_delivery_intent(
            db_session,
            notifier,
            text="cannot reuse closed scope",
            category=delivery_contracts.CATEGORY_TEST,
            idempotency_key=delivery_contracts.make_delivery_idempotency_key(
                "test", "scope-close-reuse"
            ),
            ownership=ownership,
            preparation_scope=closed_scope,
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_preparation_scope_rejects_forgery_and_another_session(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    notifier = _BoundFakeNotifier(binding)
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        delivery_contracts.DeliveryPreparationScope()

    scope = await delivery_preparation.lock_delivery_preparation_scope(
        db_session,
        notifier,
        category=delivery_contracts.CATEGORY_TEST,
        ownership=ownership,
    )
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    async with factory() as other_session:
        with pytest.raises(delivery_contracts.DeliveryCapabilityError):
            await delivery_preparation.prepare_delivery_intent(
                other_session,
                notifier,
                text="foreign session",
                category=delivery_contracts.CATEGORY_TEST,
                idempotency_key=delivery_contracts.make_delivery_idempotency_key(
                    "test", "scope-foreign-session"
                ),
                ownership=ownership,
                preparation_scope=scope,
            )
        await other_session.rollback()

    object.__setattr__(scope, "_category", delivery_contracts.CATEGORY_REPLY)
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_preparation.prepare_delivery_intent(
            db_session,
            notifier,
            text="tampered scope",
            category=delivery_contracts.CATEGORY_REPLY,
            idempotency_key=delivery_contracts.make_delivery_idempotency_key(
                "test", "scope-forged-field"
            ),
            ownership=ownership,
            preparation_scope=scope,
        )
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("early_exit", ["dedupe"])
async def test_preparation_scope_is_consumed_by_non_dispatchable_continuation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    early_exit,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    notifier = _BoundFakeNotifier(binding)
    key = delivery_contracts.make_delivery_idempotency_key(
        "test", f"scope-early-{early_exit}"
    )
    if early_exit == "dedupe":
        first = await delivery_preparation.prepare_delivery_intent(
            db_session,
            notifier,
            text="same immutable occurrence",
            category=delivery_contracts.CATEGORY_TEST,
            idempotency_key=key,
            ownership=ownership,
        )
        assert first is not None
        await db_session.commit()
    else:
        module_policy = await db_session.scalar(
            select(SubjectSetting).where(
                SubjectSetting.subject_id == ownership.subject_id,
                SubjectSetting.key == "enabled_modules",
            )
        )
        module_policy.value = {**module_policy.value, preference_contracts.MODULE_KEY: False}
        await db_session.commit()

    scope = await delivery_preparation.lock_delivery_preparation_scope(
        db_session,
        notifier,
        category=delivery_contracts.CATEGORY_TEST,
        ownership=ownership,
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        notifier,
        text="same immutable occurrence",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=key,
        ownership=ownership,
        preparation_scope=scope,
    )
    assert prepared is None
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_preparation.prepare_delivery_intent(
            db_session,
            notifier,
            text="scope cannot be repurposed",
            category=delivery_contracts.CATEGORY_TEST,
            idempotency_key=delivery_contracts.make_delivery_idempotency_key(
                "test", f"scope-early-reuse-{early_exit}"
            ),
            ownership=ownership,
            preparation_scope=scope,
        )
    await db_session.rollback()




@pytest.mark.asyncio
# "module" was a third case: the layer's master switch could be flipped
# between preparing a send and committing it. There is no switch any more.
@pytest.mark.parametrize("tamper", ["connection", "policy"])
async def test_preparation_scope_rejects_same_session_authority_tamper(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    tamper,
):
    real_policy_getter = preference_queries.get_locked_delivery_policy
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    if tamper == "policy":
        monkeypatch.setattr(
            preference_queries,
            "get_locked_delivery_policy",
            real_policy_getter,
        )
    notifier = _BoundFakeNotifier(binding)
    scope = await delivery_preparation.lock_delivery_preparation_scope(
        db_session,
        notifier,
        category=delivery_contracts.CATEGORY_TEST,
        ownership=ownership,
        now=datetime(2026, 8, 20, 12),
    )
    if tamper == "connection":
        connection = await db_session.get(
            IntegrationConnection, ownership.connection_id
        )
        connection.status = IntegrationConnectionStatus.DISABLED.value
    elif tamper == "module":
        module_policy = await db_session.scalar(
            select(SubjectSetting).where(
                SubjectSetting.subject_id == ownership.subject_id,
                SubjectSetting.key == "enabled_modules",
            )
        )
        module_policy.value = {**module_policy.value, preference_contracts.MODULE_KEY: False}
    else:
        policy_row = await db_session.get(
            IntegrationConnectionSetting,
            {
                "integration_connection_id": ownership.connection_id,
                "key": preference_contracts.TELEGRAM_DELIVERY_POLICY_KEY,
            },
        )
        assert policy_row is not None
        policy_row.value = {
            **policy_row.value,
            "daily_budget": int(policy_row.value["daily_budget"]) + 1,
        }

    expected = (
        delivery_contracts.DeliveryScopeError
        if tamper == "connection"
        else delivery_contracts.DeliveryPolicyUnavailableError
    )
    with pytest.raises(expected):
        await delivery_preparation.prepare_delivery_intent(
            db_session,
            notifier,
            text="must remain local",
            category=delivery_contracts.CATEGORY_TEST,
            idempotency_key=delivery_contracts.make_delivery_idempotency_key(
                "test", f"scope-tamper-{tamper}"
            ),
            now=datetime(2026, 8, 20, 12),
            ownership=ownership,
            preparation_scope=scope,
        )
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_preparation.prepare_delivery_intent(
            db_session,
            notifier,
            text="consumed after failed revalidation",
            category=delivery_contracts.CATEGORY_TEST,
            idempotency_key=delivery_contracts.make_delivery_idempotency_key(
                "test", f"scope-tamper-reuse-{tamper}"
            ),
            ownership=ownership,
            preparation_scope=scope,
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_happy_path_requires_each_commit_and_links_exact_graph(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    availability = _BoundFakeNotifier(binding)
    key = delivery_contracts.make_delivery_idempotency_key("brief", "2026-08-20")
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        availability,
        text="sensitive health summary",
        category=delivery_contracts.CATEGORY_BRIEF,
        idempotency_key=key,
        legacy_dedupe_key="brief:2026-08-20",
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    assert prepared is not None
    assert "sensitive" not in repr(prepared)
    with pytest.raises(TypeError):
        pickle.dumps(prepared)
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.start_delivery_dispatch(
            db_session,
            prepared,
            notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
        )
    await db_session.commit()

    transport = _BoundFakeNotifier(binding, result="702")
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        now=datetime(2026, 8, 20, 12, 1),
        notifier_resolver=lambda *_: transport,
    )
    assert lease is not None
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.dispatch_delivery(lease)
    await db_session.commit()

    completion = await delivery_dispatch.dispatch_delivery(lease)
    assert completion.status is NotificationDeliveryStatus.SENT
    assert transport.calls == [("sensitive health summary", None, None)]
    journal = await delivery_dispatch.finalize_delivery(db_session, completion)
    assert journal is not None
    assert journal.subject_id == ownership.subject_id
    assert journal.recipient_user_id == ownership.recipient_user_id
    assert journal.integration_connection_id == ownership.connection_id
    assert journal.actor_user_id is None
    assert journal.ai_invocation_id is None
    assert journal.delivery_intent_id == prepared.intent_id
    assert journal.dedupe_key == key
    assert journal.external_id == "702"
    assert journal.sent_at.tzinfo is None
    await db_session.commit()

    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    assert intent.status == NotificationDeliveryStatus.SENT.value
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.dispatch_delivery(lease)


@pytest.mark.asyncio
async def test_existing_pending_never_reconstructs_payload_or_dispatches(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    key = delivery_contracts.make_delivery_idempotency_key("evening", "2026-08-20")
    first = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="first payload",
        category=delivery_contracts.CATEGORY_EVENING,
        idempotency_key=key,
        now=datetime(2026, 8, 20, 20),
        ownership=ownership,
    )
    assert first is not None
    await db_session.commit()
    inherited = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="different caller payload",
        category=delivery_contracts.CATEGORY_EVENING,
        idempotency_key=key,
        now=datetime(2026, 8, 20, 20, 5),
        ownership=ownership,
    )
    assert inherited is None
    assert (
        await db_session.scalar(
            select(NotificationDeliveryIntent.status).where(
                NotificationDeliveryIntent.id == first.intent_id
            )
        )
        == NotificationDeliveryStatus.PENDING.value
    )


@pytest.mark.asyncio
async def test_stale_raw_pending_is_domain_rearmed_and_never_scheduler_cancelled(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    raw, key, original = await _stale_raw_pending(
        db_session,
        ownership=ownership,
        binding=binding,
        suffix="crash-after-t1",
    )

    assert await delivery_reconciliation.reconcile_stale_pending_deliveries(
        db_session,
        stale_before=datetime(2026, 8, 20, 11, tzinfo=UTC),
    ) == 0
    await db_session.commit()

    recovered = await delivery_preparation.rearm_stale_raw_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="deterministic reply",
        category=delivery_contracts.CATEGORY_REPLY,
        idempotency_key=key,
        stale_before=datetime(2026, 8, 20, 11, tzinfo=UTC),
        now=datetime(2026, 8, 20, 12, tzinfo=UTC),
        ownership=ownership,
        raw_payload_id=raw.id,
    )
    assert recovered is not None

    nested = await db_session.begin_nested()
    await nested.commit()
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.start_delivery_dispatch(
            db_session,
            recovered,
            notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
        )
    await db_session.commit()

    # Recovery rotates a sealed timestamp, so a still-live pre-crash
    # capability cannot race the re-rendered payload after recovery committed.
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.start_delivery_dispatch(
            db_session,
            original,
            notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
        )
    await db_session.rollback()

    transport = _BoundFakeNotifier(binding, result="7401")
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        recovered,
        notifier_resolver=lambda *_: transport,
    )
    assert lease is not None
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)
    journal = await delivery_dispatch.finalize_delivery(db_session, completion)
    await db_session.commit()

    assert transport.calls == [("deterministic reply", None, None)]
    assert journal is not None and journal.delivery_intent_id == original.intent_id
    assert journal.payload == {"text": "deterministic reply", "buttons": None}


@pytest.mark.asyncio
async def test_raw_rearm_requires_strict_staleness_and_root_commit(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    raw, key, _original = await _stale_raw_pending(
        db_session,
        ownership=ownership,
        binding=binding,
        suffix="rollback",
    )
    raw_id = raw.id
    not_stale = await delivery_preparation.rearm_stale_raw_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="deterministic reply",
        category=delivery_contracts.CATEGORY_REPLY,
        idempotency_key=key,
        stale_before=datetime(2026, 8, 20, 8, tzinfo=UTC),
        now=datetime(2026, 8, 20, 12, tzinfo=UTC),
        ownership=ownership,
        raw_payload_id=raw_id,
    )
    assert not_stale is None
    await db_session.rollback()

    rolled_back = await delivery_preparation.rearm_stale_raw_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="deterministic reply",
        category=delivery_contracts.CATEGORY_REPLY,
        idempotency_key=key,
        stale_before=datetime(2026, 8, 20, 11, tzinfo=UTC),
        now=datetime(2026, 8, 20, 12, tzinfo=UTC),
        ownership=ownership,
        raw_payload_id=raw_id,
    )
    assert rolled_back is not None
    await db_session.rollback()
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.start_delivery_dispatch(
            db_session,
            rolled_back,
            notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
        )

    intent = await db_session.get(NotificationDeliveryIntent, rolled_back.intent_id)
    assert intent.status == NotificationDeliveryStatus.PENDING.value
    assert intent.policy_at != datetime(2026, 8, 20, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_raw_rearm_reopens_only_proven_zero_io_cancellations(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    raw, key, original = await _stale_raw_pending(
        db_session,
        ownership=ownership,
        binding=binding,
        suffix="scope-invalid",
    )
    raw_id = raw.id
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        original,
        now=datetime(2026, 8, 20, 12, tzinfo=UTC),
        notifier_resolver=lambda *_: None,
    )
    assert lease is None
    await db_session.commit()
    cancelled = await db_session.get(NotificationDeliveryIntent, original.intent_id)
    assert cancelled.status == NotificationDeliveryStatus.CANCELLED.value
    assert cancelled.error_code == "scope_invalid"
    # Even an old updated_at cannot reopen until the zero-I/O completion itself
    # is strictly older than the caller's recovery cutoff.
    cancelled.updated_at = datetime(2026, 8, 20, 11, tzinfo=UTC)
    await db_session.commit()

    assert await delivery_preparation.rearm_stale_raw_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="deterministic reply",
        category=delivery_contracts.CATEGORY_REPLY,
        idempotency_key=key,
        stale_before=datetime(2026, 8, 20, 12, tzinfo=UTC),
        now=datetime(2026, 8, 20, 12, 5, tzinfo=UTC),
        ownership=ownership,
        raw_payload_id=raw_id,
    ) is None
    await db_session.rollback()

    recovered = await delivery_preparation.rearm_stale_raw_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="deterministic reply",
        category=delivery_contracts.CATEGORY_REPLY,
        idempotency_key=key,
        stale_before=datetime(2026, 8, 20, 12, 1, tzinfo=UTC),
        now=datetime(2026, 8, 20, 12, 5, tzinfo=UTC),
        ownership=ownership,
        raw_payload_id=raw_id,
    )
    assert recovered is not None
    assert cancelled.status == NotificationDeliveryStatus.PENDING.value
    assert cancelled.completed_at is None and cancelled.error_code is None
    await db_session.rollback()

    cancelled.status = NotificationDeliveryStatus.CANCELLED.value
    cancelled.completed_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    cancelled.error_code = "cancelled_by_policy"
    await db_session.commit()
    with pytest.raises(delivery_contracts.DeliveryStateError):
        await delivery_preparation.rearm_stale_raw_delivery_intent(
            db_session,
            _BoundFakeNotifier(binding),
            text="deterministic reply",
            category=delivery_contracts.CATEGORY_REPLY,
            idempotency_key=key,
            stale_before=datetime(2026, 8, 20, 11, tzinfo=UTC),
            now=datetime(2026, 8, 20, 12, 10, tzinfo=UTC),
            ownership=ownership,
            raw_payload_id=raw_id,
        )




@pytest.mark.asyncio
@pytest.mark.parametrize("journal_key_kind", ["opaque", "legacy"])
async def test_raw_rearm_honors_both_historical_journal_keys(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    journal_key_kind,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    raw, key, original = await _stale_raw_pending(
        db_session,
        ownership=ownership,
        binding=binding,
        suffix=f"legacy-journal-{journal_key_kind}",
    )
    raw_id = raw.id
    legacy_key = f"legacy-reply:{raw_id}"
    db_session.add(
        Notification(
            sent_at=datetime(2026, 8, 20, 10),
            category=delivery_contracts.CATEGORY_REPLY,
            dedupe_key=(key if journal_key_kind == "opaque" else legacy_key),
            channel=IntegrationProvider.TELEGRAM.value,
            external_id="7399",
            payload={"text": "historical reply"},
        )
    )
    await db_session.commit()

    assert await delivery_preparation.rearm_stale_raw_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="must not duplicate historical send",
        category=delivery_contracts.CATEGORY_REPLY,
        idempotency_key=key,
        legacy_dedupe_key=legacy_key,
        stale_before=datetime(2026, 8, 20, 11, tzinfo=UTC),
        now=datetime(2026, 8, 20, 12, tzinfo=UTC),
        ownership=ownership,
        raw_payload_id=raw_id,
    ) is None
    await db_session.commit()
    intent = await db_session.get(NotificationDeliveryIntent, original.intent_id)
    assert intent.status == NotificationDeliveryStatus.PENDING.value


@pytest.mark.asyncio
async def test_raw_rearm_metadata_and_terminal_tampering_fail_closed(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    raw, key, original = await _stale_raw_pending(
        db_session,
        ownership=ownership,
        binding=binding,
        suffix="tamper",
    )
    raw_id = raw.id
    with pytest.raises(delivery_contracts.DeliveryIdempotencyConflictError):
        await delivery_preparation.rearm_stale_raw_delivery_intent(
            db_session,
            _BoundFakeNotifier(binding),
            text="different occurrence",
            category=delivery_contracts.CATEGORY_REPLY,
            idempotency_key=delivery_contracts.make_delivery_idempotency_key(
                "raw-reply", raw_id, "wrong-key"
            ),
            stale_before=datetime(2026, 8, 20, 11, tzinfo=UTC),
            now=datetime(2026, 8, 20, 12, tzinfo=UTC),
            ownership=ownership,
            raw_payload_id=raw_id,
        )
    await db_session.rollback()
    with pytest.raises(delivery_contracts.DeliveryScopeError):
        await delivery_preparation.rearm_stale_raw_delivery_intent(
            db_session,
            _BoundFakeNotifier(binding),
            text="echo",
            category=delivery_contracts.CATEGORY_ECHO,
            idempotency_key=key,
            stale_before=datetime(2026, 8, 20, 11, tzinfo=UTC),
            now=datetime(2026, 8, 20, 12, tzinfo=UTC),
            ownership=ownership,
            raw_payload_id=raw_id,
            redact_journal_content=True,
        )

    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        original,
        notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
    )
    assert lease is not None
    await db_session.commit()
    with pytest.raises(delivery_contracts.DeliveryStateError):
        await delivery_preparation.rearm_stale_raw_delivery_intent(
            db_session,
            _BoundFakeNotifier(binding),
            text="must not resume dispatching",
            category=delivery_contracts.CATEGORY_REPLY,
            idempotency_key=key,
            stale_before=datetime(2026, 8, 20, 11, tzinfo=UTC),
            now=datetime(2026, 8, 20, 12, tzinfo=UTC),
            ownership=ownership,
            raw_payload_id=raw_id,
        )


@pytest.mark.asyncio
async def test_stale_raw_rearm_rebinds_only_before_dispatch_after_c_rotation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    old_ownership, old_binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    raw, key, original = await _stale_raw_pending(
        db_session,
        ownership=old_ownership,
        binding=old_binding,
        suffix="connection-rotation",
        redact_journal_content=True,
    )
    old_connection = await db_session.get(
        IntegrationConnection, old_ownership.connection_id
    )
    old_connection.status = IntegrationConnectionStatus.RETIRED.value
    old_connection.retired_at = datetime(2026, 8, 20, 10, tzinfo=UTC)
    current_connection = IntegrationConnection(
        subject_id=old_ownership.subject_id,
        provider=IntegrationProvider.TELEGRAM.value,
        connection_type=IntegrationConnectionType.RECIPIENT.value,
        external_account_discriminator="rearm-rotated-recipient",
        credential_ref=channels.LEGACY_TELEGRAM_CREDENTIAL_REF,
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add(current_connection)
    await db_session.flush()
    ownership = ProactiveOwnershipContext(
        subject_id=old_ownership.subject_id,
        recipient_user_id=old_ownership.recipient_user_id,
        connection_id=current_connection.id,
        include_legacy_unowned=True,
    )
    binding = channels.DeliveryEndpointBinding(
        subject_id=ownership.subject_id,
        recipient_user_id=ownership.recipient_user_id,
        integration_connection_id=ownership.connection_id,
        channel=IntegrationProvider.TELEGRAM.value,
    )
    await db_session.commit()

    recovered = await delivery_preparation.rearm_stale_raw_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="answer on the current recipient",
        category=delivery_contracts.CATEGORY_REPLY,
        idempotency_key=key,
        stale_before=datetime(2026, 8, 20, 11, tzinfo=UTC),
        now=datetime(2026, 8, 20, 12, tzinfo=UTC),
        ownership=ownership,
        raw_payload_id=raw.id,
        redact_journal_content=True,
    )
    assert recovered is not None
    await db_session.commit()
    intent = await db_session.get(NotificationDeliveryIntent, original.intent_id)
    assert intent.integration_connection_id == current_connection.id
    assert raw.integration_connection_id == old_connection.id

    transport = _BoundFakeNotifier(binding, result="7402")
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        recovered,
        notifier_resolver=lambda *_: transport,
    )
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)
    journal = await delivery_dispatch.finalize_delivery(db_session, completion)
    await db_session.commit()
    assert journal is not None
    assert journal.integration_connection_id == current_connection.id
    assert journal.payload == {
        "content_redacted": True,
        "raw_payload_id": raw.id,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_original_capability_racing_rearm_sends_once(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    raw, key, original = await _stale_raw_pending(
        db_session,
        ownership=ownership,
        binding=binding,
        suffix="postgres-race",
    )
    recovered = await delivery_preparation.rearm_stale_raw_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="deterministic reply",
        category=delivery_contracts.CATEGORY_REPLY,
        idempotency_key=key,
        stale_before=datetime(2026, 8, 20, 11, tzinfo=UTC),
        now=datetime(2026, 8, 20, 12, tzinfo=UTC),
        ownership=ownership,
        raw_payload_id=raw.id,
    )
    assert recovered is not None
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    transport = _BoundFakeNotifier(binding, result="7403")

    async def attempt(prepared):
        async with factory() as session:
            try:
                lease = await delivery_dispatch.start_delivery_dispatch(
                    session,
                    prepared,
                    notifier_resolver=lambda *_: transport,
                )
                await session.commit()
                return lease
            except delivery_contracts.DeliveryError:
                await session.rollback()
                return None

    leases = [
        lease
        for lease in await asyncio.gather(attempt(original), attempt(recovered))
        if lease is not None
    ]
    assert len(leases) == 1
    completion = await delivery_dispatch.dispatch_delivery(leases[0])
    assert await delivery_dispatch.finalize_delivery(db_session, completion) is not None
    await db_session.commit()
    assert transport.calls == [("deterministic reply", None, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("result,error", [("", None), ("abc", None), (None, RuntimeError("secret sentinel"))])
async def test_uncertain_or_invalid_transport_is_ambiguous_without_journal(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    caplog,
    result,
    error,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="private sentinel",
        category=delivery_contracts.CATEGORY_REPLY,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "reply", repr(result), type(error).__name__
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    assert prepared is not None
    await db_session.commit()
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        notifier_resolver=lambda *_: _BoundFakeNotifier(
            binding, result=result, error=error
        ),
    )
    assert lease is not None
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)
    assert completion.status is NotificationDeliveryStatus.AMBIGUOUS
    assert await delivery_dispatch.finalize_delivery(db_session, completion) is None
    await db_session.commit()
    assert await db_session.scalar(select(Notification.id)) is None
    logs = caplog.text
    assert "private sentinel" not in logs
    assert "secret sentinel" not in logs


@pytest.mark.asyncio
async def test_stale_reconciliation_never_calls_provider(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="will be abandoned",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key("test", "stale"),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    assert prepared is not None
    await db_session.commit()
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    intent.updated_at = datetime(2026, 8, 20, 10, tzinfo=UTC)
    await db_session.commit()
    changed = await delivery_reconciliation.reconcile_stale_pending_deliveries(
        db_session,
        stale_before=datetime(2026, 8, 20, 11, tzinfo=UTC),
    )
    assert changed == 1
    await db_session.commit()
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    assert intent.status == NotificationDeliveryStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_owned_monolithic_send_is_banned(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    with pytest.raises(delivery_contracts.DurableDeliveryRequiredError):
        await delivery_legacy.send(
            db_session,
            _BoundFakeNotifier(binding),
            text="must not cross network",
            category=delivery_contracts.CATEGORY_REPLY,
            ownership=ownership,
        )


@pytest.mark.asyncio
async def test_cancelled_error_is_captured_after_dispatch_started(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="cancel sentinel",
        category=delivery_contracts.CATEGORY_REPLY,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key("reply", "cancel"),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.commit()
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        notifier_resolver=lambda *_: _BoundFakeNotifier(
            binding, error=asyncio.CancelledError()
        ),
    )
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)
    assert completion.status is NotificationDeliveryStatus.AMBIGUOUS
    assert await delivery_dispatch.finalize_delivery(db_session, completion) is None
    await db_session.commit()


@pytest.mark.asyncio
async def test_identity_database_cannot_use_omitted_ownership_compatibility(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    _ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    with pytest.raises(delivery_contracts.DurableDeliveryRequiredError):
        await delivery_legacy.send(
            db_session,
            _BoundFakeNotifier(binding),
            text="omitted ownership",
            category=delivery_contracts.CATEGORY_REPLY,
            ownership=None,
        )
    await db_session.rollback()
    with pytest.raises(delivery_contracts.DurableDeliveryRequiredError):
        await delivery_queries.already_sent(
            db_session,
            "legacy-key",
            ownership=None,
        )


@pytest.mark.asyncio
async def test_text_is_clipped_before_dispatch_and_journal(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    long_text = "x" * 5000
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text=long_text,
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key("test", "clip"),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.commit()
    transport = _BoundFakeNotifier(binding)
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        notifier_resolver=lambda *_: transport,
    )
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)
    journal = await delivery_dispatch.finalize_delivery(db_session, completion)
    await db_session.commit()
    physically_sent = transport.calls[0][0]
    assert len(physically_sent) == 4096
    assert physically_sent.endswith("…")
    assert journal.payload["text"] == physically_sent


@pytest.mark.asyncio
async def test_malformed_buttons_fail_before_intent_or_network(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    transport = _BoundFakeNotifier(binding)
    with pytest.raises(ValueError):
        await delivery_preparation.prepare_delivery_intent(
            db_session,
            transport,
            text="safe",
            category=delivery_contracts.CATEGORY_TEST,
            idempotency_key=delivery_contracts.make_delivery_idempotency_key(
                "test", "bad-buttons"
            ),
            buttons=[("label", "x" * 65)],
            now=datetime(2026, 8, 20, 12),
            ownership=ownership,
        )
    assert transport.calls == []
    assert await db_session.scalar(select(NotificationDeliveryIntent.id)) is None


@pytest.mark.asyncio
async def test_midnight_expires_initiative_but_not_reply(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    availability = _BoundFakeNotifier(binding)
    reply = await delivery_preparation.prepare_delivery_intent(
        db_session,
        availability,
        text="reply",
        category=delivery_contracts.CATEGORY_REPLY,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "reply", "midnight"
        ),
        now=datetime(2026, 8, 20, 23, 59),
        ownership=ownership,
    )
    brief = await delivery_preparation.prepare_delivery_intent(
        db_session,
        availability,
        text="brief",
        category=delivery_contracts.CATEGORY_BRIEF,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "brief", "midnight"
        ),
        now=datetime(2026, 8, 20, 23, 59),
        ownership=ownership,
    )
    await db_session.commit()
    reply_lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        reply,
        now=datetime(2026, 8, 21, 0, 1),
        notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
    )
    brief_lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        brief,
        now=datetime(2026, 8, 21, 0, 1),
        notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
    )
    assert reply_lease is not None
    assert brief_lease is None
    await db_session.commit()
    brief_intent = await db_session.get(
        NotificationDeliveryIntent,
        brief.intent_id,
    )
    assert brief_intent.status == NotificationDeliveryStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_savepoint_commit_never_arms_t1_or_t2_capability(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    first = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="t1",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key("test", "nested-t1"),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    nested = await db_session.begin_nested()
    await nested.commit()
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.start_delivery_dispatch(
            db_session,
            first,
            notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
        )
    await db_session.rollback()
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.start_delivery_dispatch(
            db_session,
            first,
            notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
        )

    second = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="t2",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key("test", "nested-t2"),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.commit()
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        second,
        notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
    )
    nested = await db_session.begin_nested()
    await nested.commit()
    await db_session.rollback()
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.dispatch_delivery(lease)
    intent = await db_session.get(NotificationDeliveryIntent, second.intent_id)
    assert intent.status == NotificationDeliveryStatus.PENDING.value


@pytest.mark.asyncio
async def test_session_close_invalidates_uncommitted_t1_and_t2_capabilities(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    first = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="t1 close",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "test", "close-t1"
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.close()
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.start_delivery_dispatch(
            db_session,
            first,
            notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
        )
    assert await db_session.get(NotificationDeliveryIntent, first.intent_id) is None
    await db_session.rollback()

    second = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="t2 close",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "test", "close-t2"
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.commit()
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        second,
        notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
    )
    await db_session.close()
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.dispatch_delivery(lease)
    intent = await db_session.get(NotificationDeliveryIntent, second.intent_id)
    assert intent.status == NotificationDeliveryStatus.PENDING.value


@pytest.mark.asyncio
async def test_t3_savepoint_then_outer_rollback_keeps_completion_retryable(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="one physical send",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key("test", "t3-rollback"),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.commit()
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
    )
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)
    assert await delivery_dispatch.finalize_delivery(db_session, completion) is not None
    nested = await db_session.begin_nested()
    await nested.commit()
    await db_session.rollback()
    assert await delivery_dispatch.finalize_delivery(db_session, completion) is not None
    await db_session.commit()
    assert (
        await db_session.scalar(select(NotificationDeliveryIntent.status))
        == NotificationDeliveryStatus.SENT.value
    )


@pytest.mark.asyncio
async def test_session_close_rolls_back_t3_and_keeps_completion_retryable(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="one physical send",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "test", "t3-close-rollback"
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.commit()
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
    )
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)

    assert await delivery_dispatch.finalize_delivery(db_session, completion) is not None
    await db_session.close()

    assert await delivery_dispatch.finalize_delivery(db_session, completion) is not None
    await db_session.commit()
    assert (
        await db_session.scalar(select(NotificationDeliveryIntent.status))
        == NotificationDeliveryStatus.SENT.value
    )


@pytest.mark.asyncio
async def test_pending_historical_connection_cannot_finalize_provider_success(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="sent before graph tamper",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "test", "pending-historical-c"
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.commit()
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
    )
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)
    connection = await db_session.get(
        IntegrationConnection, ownership.connection_id
    )
    connection.status = IntegrationConnectionStatus.PENDING.value
    await db_session.commit()

    with pytest.raises(delivery_contracts.DeliveryScopeError):
        await delivery_dispatch.finalize_delivery(db_session, completion)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_t3_rejects_actor_tamper_in_persisted_intent(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="frozen actor graph",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "test", "actor-tamper"
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.commit()
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
    )
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    intent.actor_user_id = ownership.recipient_user_id
    await db_session.commit()

    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.finalize_delivery(db_session, completion)
    await db_session.rollback()
    assert await db_session.scalar(select(Notification.id)) is None


@pytest.mark.asyncio
async def test_raw_backed_reply_survives_telegram_connection_rotation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    old_ownership, _old_binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    old_connection = await db_session.get(
        IntegrationConnection, old_ownership.connection_id
    )
    old_connection.status = IntegrationConnectionStatus.RETIRED.value
    old_connection.retired_at = datetime(2026, 8, 20, 10, tzinfo=UTC)
    current_connection = IntegrationConnection(
        subject_id=old_ownership.subject_id,
        provider=IntegrationProvider.TELEGRAM.value,
        connection_type=IntegrationConnectionType.RECIPIENT.value,
        external_account_discriminator="rotated-recipient-root",
        credential_ref=channels.LEGACY_TELEGRAM_CREDENTIAL_REF,
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add(current_connection)
    await db_session.flush()
    raw = RawPayload(
        subject_id=old_ownership.subject_id,
        actor_user_id=old_ownership.recipient_user_id,
        integration_connection_id=old_connection.id,
        domain="signals",
        source=Source.TELEGRAM.value,
        external_id="synthetic-rotated-question",
        payload={"text": "private question"},
        processed_at=datetime(2026, 8, 20, 10),
    )
    db_session.add(raw)
    await db_session.flush()
    ownership = ProactiveOwnershipContext(
        subject_id=old_ownership.subject_id,
        recipient_user_id=old_ownership.recipient_user_id,
        connection_id=current_connection.id,
        include_legacy_unowned=True,
    )
    binding = channels.DeliveryEndpointBinding(
        subject_id=ownership.subject_id,
        recipient_user_id=ownership.recipient_user_id,
        integration_connection_id=ownership.connection_id,
        channel=IntegrationProvider.TELEGRAM.value,
    )
    await db_session.commit()

    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="answer after rotation",
        category=delivery_contracts.CATEGORY_REPLY,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "reply", raw.id
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
        actor_user_id=ownership.recipient_user_id,
        raw_payload_id=raw.id,
        redact_journal_content=True,
    )
    duplicate = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="different payload must not reconstruct the occurrence",
        category=delivery_contracts.CATEGORY_REPLY,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "reply", raw.id, "different-key"
        ),
        now=datetime(2026, 8, 20, 12, 1),
        ownership=ownership,
        actor_user_id=ownership.recipient_user_id,
        raw_payload_id=raw.id,
        redact_journal_content=True,
    )
    assert duplicate is None
    await db_session.commit()
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
    )
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)
    journal = await delivery_dispatch.finalize_delivery(db_session, completion)
    await db_session.commit()

    assert journal.integration_connection_id == current_connection.id
    assert journal.payload == {
        "content_redacted": True,
        "raw_payload_id": raw.id,
    }
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    assert intent.raw_payload_id == raw.id
    assert raw.integration_connection_id == old_connection.id


@pytest.mark.asyncio
async def test_ai_question_reply_cannot_opt_out_of_redacted_journal(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    with pytest.raises(delivery_contracts.DeliveryScopeError):
        await delivery_preparation.prepare_delivery_intent(
            db_session,
            _BoundFakeNotifier(binding),
            text="model-generated health answer",
            category=delivery_contracts.CATEGORY_REPLY,
            idempotency_key=delivery_contracts.make_delivery_idempotency_key(
                "reply", "unredacted-ai"
            ),
            now=datetime(2026, 8, 20, 12),
            ownership=ownership,
            actor_user_id=ownership.recipient_user_id,
            raw_payload_id=1,
            ai_invocation_id=uuid.uuid4(),
            redact_journal_content=False,
        )
    assert await db_session.scalar(select(NotificationDeliveryIntent.id)) is None


@pytest.mark.asyncio
async def test_ambiguous_nudge_claim_suppresses_policy_cooldown_retry(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    policy_key = delivery_contracts.make_delivery_policy_key("nudge", "steps-short")
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="initiative",
        category=delivery_contracts.CATEGORY_NUDGE,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "nudge", "steps-short", "2026-08-20T12"
        ),
        policy_key=policy_key,
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.commit()
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        now=datetime(2026, 8, 20, 12, 1),
        notifier_resolver=lambda *_: _BoundFakeNotifier(
            binding, error=RuntimeError("provider uncertainty")
        ),
    )
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)
    assert completion.status is NotificationDeliveryStatus.AMBIGUOUS
    assert await delivery_dispatch.finalize_delivery(db_session, completion) is None
    await db_session.commit()

    assert await delivery_queries.delivery_policy_claimed_since(
        db_session,
        policy_key=policy_key,
        not_before=datetime(2026, 8, 19, tzinfo=UTC),
        ownership=ownership,
    )
    assert await db_session.scalar(select(Notification.id)) is None


@pytest.mark.asyncio
async def test_quiet_hours_and_pending_claim_enforce_subject_daily_budget(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch, budget=1
    )
    quiet_nudge = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="quiet initiative",
        category=delivery_contracts.CATEGORY_NUDGE,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "nudge", "quiet"
        ),
        policy_key=delivery_contracts.make_delivery_policy_key("nudge", "quiet"),
        now=datetime(2026, 8, 20, 3),
        ownership=ownership,
    )
    assert quiet_nudge is None
    brief = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="first initiative claim",
        category=delivery_contracts.CATEGORY_BRIEF,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "brief", "budget-day"
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    assert brief is not None
    over_budget = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="second initiative claim",
        category=delivery_contracts.CATEGORY_EVENING,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "evening", "budget-day"
        ),
        now=datetime(2026, 8, 20, 12, 1),
        ownership=ownership,
    )
    assert over_budget is None
    await db_session.commit()
    assert (
        await db_session.scalar(select(NotificationDeliveryIntent.id))
        == brief.intent_id
    )


@pytest.mark.asyncio
async def test_expired_lease_becomes_ambiguous_without_network(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="must expire locally",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "test", "expired-lease"
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.commit()
    monotonic_values = iter(
        [100.0, 100.0 + delivery_contracts.DISPATCHING_STALE_AFTER.total_seconds()]
    )
    monkeypatch.setattr(
        delivery_contracts,
        "monotonic",
        lambda: next(monotonic_values),
    )
    monkeypatch.setattr(
        delivery_dispatch,
        "monotonic",
        lambda: next(monotonic_values),
    )
    transport = _BoundFakeNotifier(binding)
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        notifier_resolver=lambda *_: transport,
    )
    await db_session.commit()

    completion = await delivery_dispatch.dispatch_delivery(lease)
    assert completion.status is NotificationDeliveryStatus.AMBIGUOUS
    assert completion.error_code.value == "stale_dispatch"
    assert transport.calls == []
    assert await delivery_dispatch.finalize_delivery(db_session, completion) is None
    await db_session.commit()


@pytest.mark.asyncio
async def test_stale_reconciler_rejects_late_mismatched_completion(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="provider outcome races recovery",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "test", "reconcile-race"
        ),
        now=datetime(2026, 8, 20, 9),
        ownership=ownership,
    )
    await db_session.commit()
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        now=datetime(2026, 8, 20, 10),
        notifier_resolver=lambda *_: _BoundFakeNotifier(
            binding, error=RuntimeError("uncertain provider")
        ),
    )
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)
    changed = await delivery_reconciliation.reconcile_stale_delivery_dispatches(
        db_session,
        stale_before=datetime(2026, 8, 20, 11, tzinfo=UTC),
    )
    assert changed == 1
    await db_session.commit()

    with pytest.raises(delivery_contracts.DeliveryStateError):
        await delivery_dispatch.finalize_delivery(db_session, completion)
    await db_session.rollback()
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.finalize_delivery(db_session, completion)


@pytest.mark.asyncio
async def test_mutated_t2_notifier_binding_is_one_shot_ambiguous_without_send(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="binding must remain exact",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "test", "mutable-binding"
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.commit()
    transport = _BoundFakeNotifier(binding)
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        notifier_resolver=lambda *_: transport,
    )
    await db_session.commit()
    transport.binding = channels.DeliveryEndpointBinding(
        subject_id=binding.subject_id,
        recipient_user_id=binding.recipient_user_id,
        integration_connection_id=uuid.uuid4(),
        channel=binding.channel,
    )

    completion = await delivery_dispatch.dispatch_delivery(lease)
    assert completion.status is NotificationDeliveryStatus.AMBIGUOUS
    assert completion.error_code.value == "internal_error"
    assert transport.calls == []
    with pytest.raises(delivery_contracts.DeliveryCapabilityError):
        await delivery_dispatch.dispatch_delivery(lease)
    assert await delivery_dispatch.finalize_delivery(db_session, completion) is None
    await db_session.commit()


@pytest.mark.asyncio
async def test_future_t2_timestamp_can_finalize_without_clock_order_failure(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="future clock",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "test", "future-t2"
        ),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.commit()
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        now=datetime(2099, 8, 20, 12),
        notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
    )
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)
    journal = await delivery_dispatch.finalize_delivery(db_session, completion)
    await db_session.commit()

    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    assert journal.sent_at == datetime(2099, 8, 20, 12)
    assert intent.completed_at >= intent.dispatch_started_at


@pytest.mark.asyncio
async def test_subject_timezone_rejects_nonexistent_naive_dst_wall_time(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        timezone="America/New_York",
    )
    with pytest.raises(delivery_contracts.DeliveryPolicyUnavailableError):
        await delivery_preparation.prepare_delivery_intent(
            db_session,
            _BoundFakeNotifier(binding),
            text="nonexistent wall time",
            category=delivery_contracts.CATEGORY_TEST,
            idempotency_key=delivery_contracts.make_delivery_idempotency_key(
                "test", "dst-gap"
            ),
            now=datetime(2026, 3, 8, 2, 30),
            ownership=ownership,
        )
    await db_session.rollback()
    assert await db_session.scalar(select(NotificationDeliveryIntent.id)) is None


@pytest.mark.asyncio
async def test_aware_now_converts_once_to_subject_local_policy_date(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session,
        legacy_owner_roots,
        monkeypatch,
        timezone="America/New_York",
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="aware instant",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key(
            "test", "aware-dst"
        ),
        now=datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
        ownership=ownership,
    )
    await db_session.commit()
    intent = await db_session.get(NotificationDeliveryIntent, prepared.intent_id)
    assert intent.policy_date.isoformat() == "2026-03-08"
    policy_at = intent.policy_at
    if policy_at.tzinfo is None:
        policy_at = policy_at.replace(tzinfo=UTC)
    assert policy_at.astimezone(UTC) == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_zero_subject_send_starts_governance_guard_before_policy_reads(
    db_session,
):
    # This path is about a database with no subjects at all, so the module
    # state is the legacy installation-wide row rather than anybody's.
    from vitals.models.app_settings import AppSetting
    from vitals.services.modules_service import (
        DEFAULT_STATE,
        SETTINGS_KEY as MODULES_KEY,
    )

    await db_session.merge(
        AppSetting(key=MODULES_KEY, value={**DEFAULT_STATE, "signals": True})
    )
    # The zero-subject path insists on a fresh guarded transaction, so the seed
    # must be committed rather than left open.
    await db_session.commit()
    statements: list[str] = []

    def record_statement(_conn, _cursor, statement, *_args):
        statements.append(statement.strip())

    sync_engine = db_session.get_bind()
    event.listen(sync_engine, "before_cursor_execute", record_statement)
    notifier = _BoundFakeNotifier(None, result="811")
    try:
        journal = await delivery_legacy.send(
            db_session,
            notifier,
            text="legacy-only message",
            category=delivery_contracts.CATEGORY_REPLY,
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", record_statement)

    assert journal is not None
    assert notifier.calls == [("legacy-only message", None, None)]
    assert statements
    if db_session.get_bind().dialect.name == "sqlite":
        assert statements[0].upper() == "BEGIN IMMEDIATE"
    else:
        assert "pg_advisory_xact_lock" in statements[0].lower()
    await db_session.commit()


@pytest.mark.asyncio
async def test_unrecognized_preopened_transaction_cannot_authorize_legacy_send(
    db_session,
    signals_module_on,
):
    del signals_module_on
    await db_session.scalar(select(Notification.id).limit(1))
    notifier = _BoundFakeNotifier(None)
    with pytest.raises(delivery_contracts.DurableDeliveryRequiredError):
        await delivery_legacy.send(
            db_session,
            notifier,
            text="must stay local",
            category=delivery_contracts.CATEGORY_REPLY,
        )
    assert notifier.calls == []
    await db_session.rollback()


# ``test_telegram_transport_exception_is_sterile`` lived here: a transport
# failure had to carry neither the bot token (which sits in the request URL) nor
# the message text (which is PHI). The Telegram client is gone, and the property
# is not — whichever transport web push brings will need the same test, written
# against its own client. The delivery journal below already records only an
# allowlisted outcome code, which is the half of it that survives.


@pytest.mark.asyncio
async def test_journal_time_comes_from_t2_not_t1_reservation(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    ownership, binding = await _ready(
        db_session, legacy_owner_roots, monkeypatch
    )
    prepared = await delivery_preparation.prepare_delivery_intent(
        db_session,
        _BoundFakeNotifier(binding),
        text="delayed",
        category=delivery_contracts.CATEGORY_TEST,
        idempotency_key=delivery_contracts.make_delivery_idempotency_key("test", "delayed"),
        now=datetime(2026, 8, 20, 12),
        ownership=ownership,
    )
    await db_session.commit()
    lease = await delivery_dispatch.start_delivery_dispatch(
        db_session,
        prepared,
        now=datetime(2026, 8, 20, 14, 30),
        notifier_resolver=lambda *_: _BoundFakeNotifier(binding),
    )
    await db_session.commit()
    completion = await delivery_dispatch.dispatch_delivery(lease)
    journal = await delivery_dispatch.finalize_delivery(db_session, completion)
    assert journal.sent_at == datetime(2026, 8, 20, 14, 30)
    await db_session.commit()
