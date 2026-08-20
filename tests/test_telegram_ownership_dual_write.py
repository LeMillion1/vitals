"""Ownership/provenance seam for Telegram capture and proactive delivery."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import (
    DigestKind,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    SignalKind,
    Source,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.milestones import DOMAIN as INSIGHTS_DOMAIN, WeeklyDigest
from vitals.models.proactive import Notification, NotificationDeliveryIntent
from vitals.models.raw_payload import RawPayload
from vitals.models.scoped_settings import IntegrationConnectionSetting, SubjectSetting
from vitals.models.signals import DayContext, Signal
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.services import alerts_service, digest_service, signals_service
from vitals.services.proactive import channels, day_plan, delivery, inbound, nudges, prefs
from vitals.services.proactive.ownership import ProactiveOwnershipContext


DAY = date(2026, 8, 19)


@dataclass(slots=True)
class _Graph:
    user: User
    subject: HealthSubject
    connection: IntegrationConnection
    ownership: ProactiveOwnershipContext


class _Notifier:
    channel = "telegram"

    def __init__(self) -> None:
        self._next_id = 700

    async def send(self, text, *, buttons=None, reply_to=None) -> str:
        self._next_id += 1
        return str(self._next_id)

    async def edit(self, message_id, text, *, buttons=None) -> None:
        pass

    async def answer_callback(self, callback_id, text="") -> None:
        pass


def _bind_notifier(graph: _Graph, notifier: _Notifier) -> _Notifier:
    notifier.binding = channels.DeliveryEndpointBinding(
        subject_id=graph.subject.id,
        recipient_user_id=graph.user.id,
        integration_connection_id=graph.connection.id,
        channel=IntegrationProvider.TELEGRAM.value,
    )
    return notifier


async def _durably_send(
    session: AsyncSession,
    graph: _Graph,
    notifier: _Notifier,
    *,
    text: str,
    category: str,
    occurrence: str,
    legacy_dedupe_key: str | None = None,
    now: datetime | None = None,
    actor_user_id: uuid.UUID | None = None,
    raw_payload_id: int | None = None,
) -> Notification:
    bound = _bind_notifier(graph, notifier)
    policy_key = (
        delivery.make_delivery_policy_key("ownership-test", occurrence)
        if category == delivery.CATEGORY_NUDGE
        else None
    )
    prepared = await delivery.prepare_delivery_intent(
        session,
        bound,
        text=text,
        category=category,
        idempotency_key=delivery.make_delivery_idempotency_key(
            "ownership-test",
            occurrence,
        ),
        policy_key=policy_key,
        legacy_dedupe_key=legacy_dedupe_key,
        now=now,
        ownership=graph.ownership,
        actor_user_id=actor_user_id,
        raw_payload_id=raw_payload_id,
    )
    assert prepared is not None
    await session.commit()
    lease = await delivery.start_delivery_dispatch(
        session,
        prepared,
        now=now,
        notifier_resolver=lambda binding, _credential_ref: (
            notifier if notifier.binding == binding else None
        ),
    )
    assert lease is not None
    await session.commit()
    completion = await delivery.dispatch_delivery(lease)
    journal = await delivery.finalize_delivery(session, completion)
    assert journal is not None
    await session.commit()
    return journal


async def _graph(session, label: str) -> _Graph:
    user = User(
        username=label,
        normalized_username=label.casefold(),
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=f"Subject {label}",
        timezone="UTC",
    )
    session.add(subject)
    await session.flush()
    session.add(
        SubjectSetting(
            subject_id=subject.id,
            key="enabled_modules",
            value={"signals": True},
        )
    )
    connection = IntegrationConnection(
        subject_id=subject.id,
        provider=IntegrationProvider.TELEGRAM.value,
        connection_type=IntegrationConnectionType.RECIPIENT.value,
        external_account_discriminator=f"synthetic:{label}",
        credential_ref=channels.LEGACY_TELEGRAM_CREDENTIAL_REF,
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    session.add(connection)
    await session.flush()
    session.add(
        IntegrationConnectionSetting(
            integration_connection_id=connection.id,
            key=prefs.TELEGRAM_DELIVERY_POLICY_KEY,
            value={
                "quiet_start": prefs.DEFAULTS["quiet_start"],
                "quiet_end": prefs.DEFAULTS["quiet_end"],
                "daily_budget": prefs.DEFAULTS["daily_budget"],
            },
        )
    )
    return _Graph(
        user=user,
        subject=subject,
        connection=connection,
        ownership=ProactiveOwnershipContext(
            subject_id=subject.id,
            recipient_user_id=user.id,
            connection_id=connection.id,
        ),
    )


async def _gateway(
    session: AsyncSession,
    graph: _Graph,
    label: str,
) -> IntegrationConnection:
    connection = IntegrationConnection(
        subject_id=graph.subject.id,
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator=f"synthetic:{label}",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    session.add(connection)
    await session.flush()
    return connection


def _parser_context(
    graph: _Graph,
    connection: IntegrationConnection,
) -> alerts_service.ProviderAlertContext:
    return alerts_service.ProviderAlertContext(
        identity=graph.ownership.system_action(),
        provider=IntegrationProvider.OPENROUTER,
        integration_connection_id=connection.id,
    )


def _one_signal(_text: str) -> list[dict]:
    return [
        {
            "kind": SignalKind.SYMPTOM.value,
            "key": "headache",
            "value_num": 3,
            "note": "голова болит",
        }
    ]


def _telegram_text_update(
    update_id: int,
    text: str,
    *,
    message_id: int,
    edited: bool = False,
) -> dict:
    message = {
        "message_id": message_id,
        "date": 1785612345,
        "chat": {"id": 424242, "type": "private"},
        "from": {"id": 424242, "is_bot": False},
        "text": text,
    }
    return {
        "update_id": update_id,
        "edited_message" if edited else "message": message,
    }


class _RaceNotifier(_Notifier):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    async def send(self, text, *, buttons=None, reply_to=None) -> str:
        self.messages.append(text)
        return await super().send(text, buttons=buttons, reply_to=reply_to)


async def test_owned_ingest_copies_raw_roots_to_every_signal(db_session):
    graph = await _graph(db_session, "owner-one")

    rows = await signals_service.ingest_text(
        db_session,
        text="голова болит",
        parse=_one_signal,
        external_id="tg:owned-1",
        on_date=DAY,
        identity=graph.ownership.owner_action(),
        integration_connection_id=graph.connection.id,
    )
    await db_session.commit()

    raw = await db_session.scalar(
        select(RawPayload).where(RawPayload.external_id == "tg:owned-1")
    )
    assert raw is not None
    assert (raw.subject_id, raw.actor_user_id, raw.integration_connection_id) == (
        graph.subject.id,
        graph.user.id,
        graph.connection.id,
    )
    assert len(rows) == 1
    assert (rows[0].subject_id, rows[0].actor_user_id, rows[0].integration_connection_id) == (
        raw.subject_id,
        raw.actor_user_id,
        raw.integration_connection_id,
    )


async def test_same_telegram_external_id_is_isolated_between_subjects(db_session):
    first = await _graph(db_session, "first")
    second = await _graph(db_session, "second")

    for graph in (first, second):
        await signals_service.ingest_text(
            db_session,
            text="голова болит",
            parse=_one_signal,
            external_id="tg:duplicate",
            on_date=DAY,
            identity=graph.ownership.owner_action(),
            integration_connection_id=graph.connection.id,
        )
    await db_session.commit()

    raws = list(
        await db_session.scalars(
            select(RawPayload)
            .where(RawPayload.external_id == "tg:duplicate")
            .order_by(RawPayload.id)
        )
    )
    rows = list(await db_session.scalars(select(Signal).order_by(Signal.id)))
    assert {row.subject_id for row in raws} == {first.subject.id, second.subject.id}
    assert {row.integration_connection_id for row in raws} == {
        first.connection.id,
        second.connection.id,
    }
    assert {row.subject_id for row in rows} == {first.subject.id, second.subject.id}
    assert {row.raw_id for row in rows} == {raw.id for raw in raws}


async def test_new_raw_rejects_wrong_provider_subject_status_and_missing_root(
    db_session,
):
    first = await _graph(db_session, "first")
    second = await _graph(db_session, "second")
    garmin = IntegrationConnection(
        subject_id=first.subject.id,
        provider=IntegrationProvider.GARMIN.value,
        connection_type=IntegrationConnectionType.ACCOUNT.value,
        external_account_discriminator="synthetic:garmin",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add(garmin)
    await db_session.flush()

    with pytest.raises(signals_service.SignalOwnershipError, match="recipient"):
        await signals_service.store_raw_text(
            db_session,
            text="provider mismatch",
            identity=first.ownership.owner_action(),
            integration_connection_id=garmin.id,
        )
    with pytest.raises(signals_service.SignalOwnershipError, match="another subject"):
        await signals_service.store_raw_text(
            db_session,
            text="subject mismatch",
            identity=first.ownership.owner_action(),
            integration_connection_id=second.connection.id,
        )
    with pytest.raises(signals_service.SignalOwnershipError, match="requires a recipient"):
        await signals_service.store_raw_text(
            db_session,
            text="missing connection",
            identity=first.ownership.owner_action(),
        )

    first.connection.status = "future_state"
    with db_session.no_autoflush:
        with pytest.raises(signals_service.SignalOwnershipError, match="unknown lifecycle"):
            await signals_service._require_connection_scope(
                db_session,
                identity=first.ownership.owner_action(),
                integration_connection_id=first.connection.id,
            )
    await db_session.rollback()
    assert list(await db_session.scalars(select(RawPayload))) == []


@pytest.mark.parametrize(
    "status",
    [
        IntegrationConnectionStatus.PENDING.value,
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    ],
)
async def test_new_raw_rejects_non_live_telegram_recipient(db_session, status):
    graph = await _graph(db_session, f"non-live-{status}")
    graph.connection.status = status
    graph.connection.retired_at = (
        datetime.now(timezone.utc)
        if status == IntegrationConnectionStatus.RETIRED.value
        else None
    )
    await db_session.commit()

    with pytest.raises(signals_service.SignalOwnershipError, match=status):
        await signals_service.store_raw_text(
            db_session,
            text="must not attach",
            identity=graph.ownership.owner_action(),
            integration_connection_id=graph.connection.id,
        )
    assert list(await db_session.scalars(select(RawPayload))) == []


async def test_normalization_rejects_a_raw_root_from_another_subject(db_session):
    first = await _graph(db_session, "first")
    second = await _graph(db_session, "second")
    raw = await signals_service.store_raw_text(
        db_session,
        text="голова болит",
        external_id="tg:mismatch",
        identity=first.ownership.owner_action(),
        integration_connection_id=first.connection.id,
    )

    with pytest.raises(signals_service.SignalOwnershipError, match="another subject"):
        await signals_service.create_signals(
            db_session,
            items=_one_signal(""),
            raw_id=raw.id,
            identity=second.ownership.owner_action(),
            integration_connection_id=second.connection.id,
        )
    assert list(await db_session.scalars(select(Signal))) == []


async def test_signal_and_day_context_reads_reject_partial_or_cross_roots(
    db_session,
):
    first = await _graph(db_session, "read-scope-first")
    second = await _graph(db_session, "read-scope-second")
    valid = Signal(
        subject_id=first.subject.id,
        actor_user_id=first.user.id,
        integration_connection_id=first.connection.id,
        date=DAY,
        domain=signals_service.DOMAIN,
        source=Source.TELEGRAM.value,
        kind=SignalKind.SYMPTOM.value,
        key="valid",
        batch_id="valid-batch",
    )
    foreign_actor = Signal(
        subject_id=first.subject.id,
        actor_user_id=second.user.id,
        integration_connection_id=first.connection.id,
        date=DAY,
        domain=signals_service.DOMAIN,
        source=Source.TELEGRAM.value,
        kind=SignalKind.SYMPTOM.value,
        key="foreign_actor",
        batch_id="foreign-actor-batch",
    )
    foreign_connection = Signal(
        subject_id=first.subject.id,
        actor_user_id=first.user.id,
        integration_connection_id=second.connection.id,
        date=DAY,
        domain=signals_service.DOMAIN,
        source=Source.TELEGRAM.value,
        kind=SignalKind.SYMPTOM.value,
        key="foreign_connection",
        batch_id="foreign-connection-batch",
    )
    contexts = [
        DayContext(
            subject_id=first.subject.id,
            actor_user_id=first.user.id,
            integration_connection_id=first.connection.id,
            date=DAY,
            domain=signals_service.DOMAIN,
            source=Source.TELEGRAM.value,
            answers={"kind": "valid"},
        ),
        DayContext(
            subject_id=first.subject.id,
            actor_user_id=second.user.id,
            integration_connection_id=first.connection.id,
            date=DAY + timedelta(days=1),
            domain=signals_service.DOMAIN,
            source=Source.TELEGRAM.value,
            answers={"kind": "foreign_actor"},
        ),
        DayContext(
            subject_id=first.subject.id,
            actor_user_id=first.user.id,
            integration_connection_id=second.connection.id,
            date=DAY + timedelta(days=2),
            domain=signals_service.DOMAIN,
            source=Source.TELEGRAM.value,
            answers={"kind": "foreign_connection"},
        ),
    ]
    db_session.add_all([valid, foreign_actor, foreign_connection, *contexts])
    await db_session.commit()

    assert [row.key for row in await signals_service.list_signals(
        db_session,
        subject_id=first.subject.id,
    )] == ["valid"]
    assert await signals_service.mark_misparse(
        db_session,
        "foreign-actor-batch",
        subject_id=first.subject.id,
    ) == 0
    assert await signals_service.delete_signal(
        db_session,
        foreign_connection.id,
        subject_id=first.subject.id,
    ) is False
    assert await signals_service.get_day_context(
        db_session,
        DAY,
        subject_id=first.subject.id,
    ) is contexts[0]
    assert await signals_service.get_day_context(
        db_session,
        DAY + timedelta(days=1),
        subject_id=first.subject.id,
    ) is None
    assert await signals_service.get_day_context(
        db_session,
        DAY + timedelta(days=2),
        subject_id=first.subject.id,
    ) is None
    assert await signals_service.list_day_contexts(
        db_session,
        subject_id=first.subject.id,
    ) == [contexts[0]]
    with pytest.raises(signals_service.SignalOwnershipError, match="origin actor"):
        await signals_service.set_day_context(
            db_session,
            DAY + timedelta(days=1),
            answers={"must": "not mutate"},
            identity=first.ownership.system_action(),
        )
    await db_session.rollback()
    with pytest.raises(
        signals_service.SignalOwnershipError,
        match="historical connection",
    ):
        await signals_service.set_day_context(
            db_session,
            DAY + timedelta(days=2),
            answers={"must": "not mutate"},
            identity=first.ownership.system_action(),
        )


async def test_day_facts_fails_closed_on_foreign_actor_and_gateway_roots(db_session):
    first = await _graph(db_session, "digest-scope-first")
    second = await _graph(db_session, "digest-scope-second")
    first_gateway = IntegrationConnection(
        subject_id=first.subject.id,
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator="synthetic:first-gateway",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    second_gateway = IntegrationConnection(
        subject_id=second.subject.id,
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator="synthetic:second-gateway",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add_all([first_gateway, second_gateway])
    await db_session.flush()
    db_session.add_all(
        [
            WeeklyDigest(
                subject_id=first.subject.id,
                actor_user_id=None,
                integration_connection_id=first_gateway.id,
                date=DAY,
                domain=INSIGHTS_DOMAIN,
                kind=DigestKind.DAILY_BRIEF.value,
                content="valid",
                context_json={"safe": "visible"},
            ),
            WeeklyDigest(
                subject_id=first.subject.id,
                actor_user_id=second.user.id,
                integration_connection_id=first_gateway.id,
                date=DAY + timedelta(days=1),
                domain=INSIGHTS_DOMAIN,
                kind=DigestKind.DAILY_BRIEF.value,
                content="foreign actor",
                context_json={"foreign_actor": "must not leak"},
            ),
            WeeklyDigest(
                subject_id=first.subject.id,
                actor_user_id=first.user.id,
                integration_connection_id=second_gateway.id,
                date=DAY + timedelta(days=2),
                domain=INSIGHTS_DOMAIN,
                kind=DigestKind.DAILY_BRIEF.value,
                content="foreign gateway",
                context_json={"foreign_gateway": "must not leak"},
            ),
        ]
    )
    await db_session.commit()

    with pytest.raises(digest_service.DigestOwnershipError):
        await inbound._day_facts(
            db_session,
            ownership=first.ownership,
        )


@pytest.mark.parametrize(
    "status",
    [
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    ],
)
async def test_reparse_copies_allowed_historical_telegram_provenance(
    db_session,
    status,
):
    graph = await _graph(db_session, f"historical-{status}")
    raw = await signals_service.store_raw_text(
        db_session,
        text="голова болит",
        external_id="tg:historical",
        identity=graph.ownership.owner_action(),
        integration_connection_id=graph.connection.id,
    )
    graph.connection.status = status
    graph.connection.retired_at = (
        datetime.now(timezone.utc)
        if status == IntegrationConnectionStatus.RETIRED.value
        else None
    )
    await db_session.commit()

    rows = await signals_service.reparse_unparsed(
        db_session,
        parse=_one_signal,
        subject_id=graph.subject.id,
        integration_connection_id=graph.connection.id,
    )
    await db_session.commit()

    assert len(rows) == 1
    assert (rows[0].subject_id, rows[0].actor_user_id, rows[0].integration_connection_id) == (
        raw.subject_id,
        raw.actor_user_id,
        raw.integration_connection_id,
    )


async def test_reparse_rejects_pending_connection_as_historical_provenance(
    db_session,
):
    graph = await _graph(db_session, "pending-history")
    await signals_service.store_raw_text(
        db_session,
        text="голова болит",
        external_id="tg:pending-history",
        identity=graph.ownership.owner_action(),
        integration_connection_id=graph.connection.id,
    )
    graph.connection.status = IntegrationConnectionStatus.PENDING.value
    await db_session.commit()

    with pytest.raises(
        signals_service.SignalOwnershipError,
        match="historical provenance",
    ):
        await signals_service.reparse_unparsed(
            db_session,
            parse=_one_signal,
            subject_id=graph.subject.id,
        )


async def test_day_context_plan_answer_plan_preserves_human_provenance(db_session):
    graph = await _graph(db_session, "owner")

    pure_system = await day_plan.record_plan(
        db_session,
        DAY,
        {"where": "office"},
        ownership=graph.ownership,
    )
    assert (pure_system.subject_id, pure_system.actor_user_id, pure_system.integration_connection_id) == (
        graph.subject.id,
        None,
        None,
    )

    answered = await day_plan.record_answer(
        db_session,
        DAY,
        "gym",
        True,
        ownership=graph.ownership,
    )
    assert (answered.actor_user_id, answered.integration_connection_id) == (
        graph.user.id,
        graph.connection.id,
    )

    planned_again = await day_plan.record_plan(
        db_session,
        DAY,
        {"where": "remote", "gym": False},
        ownership=graph.ownership,
    )
    assert (planned_again.actor_user_id, planned_again.integration_connection_id) == (
        graph.user.id,
        graph.connection.id,
    )
    assert planned_again.answers == {"gym": True}


async def test_day_context_rejects_foreign_actor_and_allows_channel_rotation(db_session):
    graph = await _graph(db_session, "owner")
    await day_plan.record_answer(
        db_session,
        DAY,
        "gym",
        True,
        ownership=graph.ownership,
    )

    other_user = User(
        username="delegate",
        normalized_username="delegate",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    other_connection = IntegrationConnection(
        subject_id=graph.subject.id,
        provider=IntegrationProvider.TELEGRAM.value,
        connection_type=IntegrationConnectionType.RECIPIENT.value,
        external_account_discriminator="synthetic:second-recipient",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add_all([other_user, other_connection])
    await db_session.flush()

    wrong_actor = ProactiveOwnershipContext(
        subject_id=graph.subject.id,
        recipient_user_id=other_user.id,
        connection_id=graph.connection.id,
    )
    with pytest.raises(signals_service.SignalOwnershipError, match="another origin actor"):
        await day_plan.record_answer(
            db_session,
            DAY,
            "gym",
            False,
            ownership=wrong_actor,
        )

    wrong_connection = ProactiveOwnershipContext(
        subject_id=graph.subject.id,
        recipient_user_id=graph.user.id,
        connection_id=other_connection.id,
    )
    graph.connection.status = IntegrationConnectionStatus.RETIRED.value
    graph.connection.retired_at = datetime.now(timezone.utc)
    rotated = await day_plan.record_answer(
        db_session,
        DAY,
        "load",
        "heavy",
        ownership=wrong_connection,
    )
    assert rotated.answers == {"gym": True, "load": "heavy"}
    assert rotated.integration_connection_id == graph.connection.id


async def test_day_context_preserves_channel_provenance_across_mcp_updates(
    db_session,
):
    graph = await _graph(db_session, "cross-channel")
    telegram_first = await day_plan.record_answer(
        db_session,
        DAY,
        "gym",
        True,
        ownership=graph.ownership,
    )
    mcp_after = await day_plan.record_answer(
        db_session,
        DAY,
        "load",
        "heavy",
        source=Source.MCP.value,
        identity=graph.ownership.owner_action(),
    )
    assert mcp_after is telegram_first
    assert mcp_after.integration_connection_id == graph.connection.id
    assert mcp_after.actor_user_id == graph.user.id
    assert mcp_after.source == Source.MCP.value

    next_day = DAY + timedelta(days=1)
    mcp_first = await day_plan.record_answer(
        db_session,
        next_day,
        "where",
        "remote",
        source=Source.MCP.value,
        identity=graph.ownership.owner_action(),
    )
    assert mcp_first.integration_connection_id is None
    telegram_after = await day_plan.record_answer(
        db_session,
        next_day,
        "gym",
        False,
        ownership=graph.ownership,
    )
    assert telegram_after is mcp_first
    assert telegram_after.integration_connection_id == graph.connection.id
    assert telegram_after.actor_user_id == graph.user.id


async def test_late_plan_never_downgrades_answer_source_or_first_guess(db_session):
    graph = await _graph(db_session, "late-plan")
    answered = await day_plan.record_answer(
        db_session,
        DAY,
        "gym",
        True,
        ownership=graph.ownership,
        source=Source.TELEGRAM.value,
    )
    first_guess = dict(answered.planned)

    planned = await day_plan.record_plan(
        db_session,
        DAY,
        {"where": "remote", "gym": True},
        ownership=graph.ownership,
    )
    assert planned is answered
    assert planned.source == Source.TELEGRAM.value
    assert planned.planned == first_guess
    assert planned.answers == {"gym": True}


async def test_legacy_null_rows_are_visible_only_through_verified_bridge(
    db_session,
    signals_module_on,
):
    graph = await _graph(db_session, "legacy-bridge")
    legacy = ProactiveOwnershipContext(
        subject_id=graph.subject.id,
        recipient_user_id=graph.user.id,
        connection_id=graph.connection.id,
        include_legacy_unowned=True,
    )
    legacy_signal = Signal(
        date=DAY,
        domain="signals",
        source=Source.TELEGRAM.value,
        kind=SignalKind.STATE.value,
        key="energy",
        batch_id="legacy-null",
    )
    partial_signal = Signal(
        actor_user_id=graph.user.id,
        date=DAY,
        domain="signals",
        source=Source.TELEGRAM.value,
        kind=SignalKind.STATE.value,
        key="partial",
        batch_id="partial-roots",
    )
    legacy_day = DayContext(
        date=DAY,
        domain="signals",
        source=Source.MANUAL.value,
        answers={"gym": True},
        planned={},
    )
    legacy_notification = Notification(
        sent_at=datetime(2026, 8, 19, 12, 0),
        category=delivery.CATEGORY_BRIEF,
        channel="telegram",
        external_id="legacy-message",
        payload={"text": "legacy"},
    )
    partial_notification = Notification(
        actor_user_id=graph.user.id,
        sent_at=datetime(2026, 8, 19, 12, 1),
        category=delivery.CATEGORY_REPLY,
        channel="telegram",
        external_id="partial-message",
        payload={"text": "partial"},
    )
    db_session.add_all(
        [
            legacy_signal,
            partial_signal,
            legacy_day,
            legacy_notification,
            partial_notification,
        ]
    )
    await db_session.flush()

    assert await signals_service.list_signals(
        db_session,
        subject_id=graph.subject.id,
    ) == []
    bridged_signals = await signals_service.list_signals(
        db_session,
        subject_id=graph.subject.id,
        include_legacy_unowned=True,
    )
    assert bridged_signals == [legacy_signal]
    assert await signals_service.get_day_context(
        db_session,
        DAY,
        subject_id=graph.subject.id,
    ) is None
    assert await signals_service.get_day_context(
        db_session,
        DAY,
        subject_id=graph.subject.id,
        include_legacy_unowned=True,
    ) is legacy_day
    assert await delivery.sent_today(
        db_session,
        on_date=DAY,
        ownership=graph.ownership,
    ) == 0
    assert await delivery.sent_today(
        db_session,
        on_date=DAY,
        ownership=legacy,
    ) == 1
    assert await delivery.find_sent(
        db_session,
        "legacy-message",
        ownership=legacy,
    ) is legacy_notification
    with pytest.raises(delivery.DeliveryStateError, match="malformed ownership"):
        await delivery.find_sent(
            db_session,
            "partial-message",
            ownership=legacy,
        )


async def test_context_callback_records_telegram_provenance(db_session):
    graph = await _graph(db_session, "callback-source")

    await inbound._handle_callback(
        db_session,
        {
            "id": "callback-source",
            "data": f"{inbound.CB_CONTEXT}{DAY.isoformat()}:gym:1",
            "message": {"message_id": 7, "text": "question"},
        },
        notifier=None,
        external_id=None,
        ownership=graph.ownership,
    )

    recorded = await signals_service.get_day_context(
        db_session,
        DAY,
        subject_id=graph.subject.id,
    )
    assert recorded is not None
    assert recorded.answers == {"gym": True}
    assert recorded.source == Source.TELEGRAM.value


async def test_partial_root_day_context_is_never_adopted(db_session):
    graph = await _graph(db_session, "partial-day")
    partial = DayContext(
        actor_user_id=graph.user.id,
        date=DAY,
        domain="signals",
        source=Source.MANUAL.value,
        answers={"where": "office"},
    )
    db_session.add(partial)
    await db_session.flush()

    bridge = ProactiveOwnershipContext(
        subject_id=graph.subject.id,
        recipient_user_id=graph.user.id,
        connection_id=graph.connection.id,
        include_legacy_unowned=True,
    )
    with pytest.raises(signals_service.SignalOwnershipError, match="partial-root"):
        await day_plan.record_plan(
            db_session,
            DAY,
            {"where": "remote"},
            ownership=bridge,
        )
    assert partial.subject_id is None
    assert partial.actor_user_id == graph.user.id


async def test_file_only_raw_is_not_eligible_for_the_legacy_null_bridge(db_session):
    graph = await _graph(db_session, "partial-file-raw")
    asset = FileAsset(
        subject_id=graph.subject.id,
        uploaded_by_user_id=None,
        purpose=FileAssetPurpose.LAB_DOCUMENT.value,
        storage_backend=FileStorageBackend.LEGACY_LOCAL.value,
        storage_ref="uploads/synthetic-partial-file.pdf",
        status=FileAssetStatus.LEGACY_PLACEHOLDER.value,
    )
    db_session.add(asset)
    await db_session.flush()
    raw = RawPayload(
        subject_id=None,
        actor_user_id=None,
        integration_connection_id=None,
        file_asset_id=asset.id,
        domain="signals",
        source=Source.TELEGRAM.value,
        external_id="tg:file-only-partial",
        payload={"text": "must not bridge"},
    )
    db_session.add(raw)
    await db_session.flush()
    bridge = ProactiveOwnershipContext(
        subject_id=graph.subject.id,
        recipient_user_id=graph.user.id,
        connection_id=graph.connection.id,
        include_legacy_unowned=True,
    )

    with pytest.raises(inbound.InboundOwnershipError, match="legacy roots"):
        await inbound._validate_raw_root(db_session, raw, ownership=bridge)
    with pytest.raises(signals_service.SignalOwnershipError, match="partial ownership"):
        await signals_service._require_raw_ownership_scope(db_session, raw=raw)

    owned_with_file = RawPayload(
        subject_id=graph.subject.id,
        actor_user_id=graph.user.id,
        integration_connection_id=graph.connection.id,
        file_asset_id=asset.id,
        domain="signals",
        source=Source.TELEGRAM.value,
        external_id="tg:owned-with-file",
        payload={"text": "also invalid"},
    )
    db_session.add(owned_with_file)
    await db_session.flush()
    with pytest.raises(inbound.InboundOwnershipError, match="file asset"):
        await inbound._validate_raw_root(
            db_session,
            owned_with_file,
            ownership=bridge,
        )
    with pytest.raises(signals_service.SignalOwnershipError, match="file asset"):
        await signals_service._require_raw_ownership_scope(
            db_session,
            raw=owned_with_file,
        )


@pytest.mark.parametrize("actor_kind", ["null", "foreign"])
async def test_owned_telegram_raw_requires_subject_owner_actor_before_parse(
    db_session,
    actor_kind,
):
    graph = await _graph(db_session, f"raw-actor-{actor_kind}")
    foreign = await _graph(db_session, f"raw-actor-other-{actor_kind}")
    raw = RawPayload(
        subject_id=graph.subject.id,
        actor_user_id=(foreign.user.id if actor_kind == "foreign" else None),
        integration_connection_id=graph.connection.id,
        domain=signals_service.DOMAIN,
        source=Source.TELEGRAM.value,
        external_id=f"tg:raw-actor-{actor_kind}",
        payload=_telegram_text_update(7001, "голова болит", message_id=701),
    )
    db_session.add(raw)
    await db_session.flush()
    parsed = False

    def _must_not_parse(_text):
        nonlocal parsed
        parsed = True
        return _one_signal(_text)

    with pytest.raises(signals_service.SignalOwnershipError, match="subject owner"):
        await signals_service.ingest_stored_text(
            db_session,
            raw=raw,
            text="голова болит",
            parse=_must_not_parse,
            identity=graph.ownership.owner_action(),
            integration_connection_id=graph.connection.id,
        )
    assert parsed is False


async def test_callback_capture_survives_failure_and_replays(
    db_session,
    monkeypatch,
):
    graph = await _graph(db_session, "callback-replay")
    original_record_answer = day_plan.record_answer

    async def _fail_once(*args, **kwargs):
        raise RuntimeError("simulated action failure")

    monkeypatch.setattr(day_plan, "record_answer", _fail_once)
    callback = {
        "id": "callback-1",
        "data": f"{inbound.CB_CONTEXT}{DAY.isoformat()}:gym:1",
        "message": {
            "message_id": 7,
            "date": 1_785_612_200,
            "chat": {"id": 424242, "type": "private"},
            "text": "memory-only generated answer",
        },
    }
    with pytest.raises(RuntimeError, match="simulated action failure"):
        await inbound._handle_callback(
            db_session,
            callback,
            notifier=None,
            external_id="tg:durable-callback",
            ownership=graph.ownership,
        )
    await db_session.rollback()

    raw = await db_session.scalar(
        select(RawPayload).where(
            RawPayload.external_id == "tg:durable-callback"
        )
    )
    assert raw is not None
    assert raw.processed_at is None
    assert raw.payload == {
        "callback_query": {
            "id": "callback-1",
            "data": f"{inbound.CB_CONTEXT}{DAY.isoformat()}:gym:1",
            "message": {
                "message_id": 7,
                "date": 1_785_612_200,
                "chat": {"id": 424242, "type": "private"},
            },
        }
    }
    assert "memory-only generated answer" not in str(raw.payload)
    assert await signals_service.get_day_context(db_session, DAY) is None

    monkeypatch.setattr(day_plan, "record_answer", original_record_answer)
    assert await inbound._replay_pending_callbacks(
        db_session,
        ownership=graph.ownership,
    ) == 1
    await db_session.commit()

    recovered = await signals_service.get_day_context(
        db_session,
        DAY,
        subject_id=graph.subject.id,
    )
    assert recovered is not None and recovered.answers == {"gym": True}
    assert recovered.source == Source.TELEGRAM.value
    await db_session.refresh(raw)
    assert raw.processed_at is not None


async def test_recovery_crosses_retired_telegram_connection_rotation(
    db_session,
    monkeypatch,
):
    graph = await _graph(db_session, "recovery-rotation")
    text_update = {
        "update_id": 901,
        "message": {
            "message_id": 41,
            "date": 1785612345,
            "chat": {"id": 424242, "type": "private"},
            "from": {"id": 424242, "is_bot": False},
            "text": "голова болит",
        },
    }
    text_claim = await inbound._claim_update_raw(
        db_session,
        external_id="tg:901",
        payload=text_update,
        ownership=graph.ownership,
    )
    callback_update = {
        "update_id": 902,
        "callback_query": {
            "id": "rotation-callback",
            "from": {"id": 424242, "is_bot": False},
            "data": f"{inbound.CB_CONTEXT}{DAY.isoformat()}:gym:1",
            "message": {
                "message_id": 42,
                "date": 1785612345,
                "chat": {"id": 424242, "type": "private"},
            },
        },
    }
    callback_claim = await inbound._claim_update_raw(
        db_session,
        external_id="tg:902",
        payload=callback_update,
        ownership=graph.ownership,
    )
    old_connection_id = graph.connection.id
    graph.connection.status = IntegrationConnectionStatus.RETIRED.value
    graph.connection.retired_at = datetime.now(timezone.utc)
    replacement = IntegrationConnection(
        subject_id=graph.subject.id,
        provider=IntegrationProvider.TELEGRAM.value,
        connection_type=IntegrationConnectionType.RECIPIENT.value,
        external_account_discriminator="synthetic:recovery-replacement",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add(replacement)
    await db_session.commit()
    current = ProactiveOwnershipContext(
        subject_id=graph.subject.id,
        recipient_user_id=graph.user.id,
        connection_id=replacement.id,
    )
    rows = await inbound.reparse_pending(
        db_session,
        ownership=current,
        parse=_one_signal,
    )
    await db_session.commit()

    assert len(rows) == 1
    assert rows[0].integration_connection_id == old_connection_id
    context = await signals_service.get_day_context(
        db_session,
        DAY,
        subject_id=graph.subject.id,
    )
    assert context is not None and context.answers == {"gym": True}
    assert context.integration_connection_id == old_connection_id
    await db_session.refresh(text_claim.raw)
    await db_session.refresh(callback_claim.raw)
    assert text_claim.raw.processed_at is not None
    assert callback_claim.raw.processed_at is not None


async def test_fresh_undo_tap_can_target_batch_from_retired_connection(db_session):
    graph = await _graph(db_session, "undo-rotation")
    rows = await signals_service.ingest_text(
        db_session,
        text="голова болит",
        parse=_one_signal,
        external_id="tg:old-batch",
        on_date=DAY,
        identity=graph.ownership.owner_action(),
        integration_connection_id=graph.connection.id,
    )
    old_connection_id = graph.connection.id
    graph.connection.status = IntegrationConnectionStatus.RETIRED.value
    graph.connection.retired_at = datetime.now(timezone.utc)
    replacement = IntegrationConnection(
        subject_id=graph.subject.id,
        provider=IntegrationProvider.TELEGRAM.value,
        connection_type=IntegrationConnectionType.RECIPIENT.value,
        external_account_discriminator="synthetic:undo-replacement",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add(replacement)
    await db_session.commit()
    current = ProactiveOwnershipContext(
        subject_id=graph.subject.id,
        recipient_user_id=graph.user.id,
        connection_id=replacement.id,
    )

    await inbound._handle_callback(
        db_session,
        {
            "id": "fresh-undo",
            "data": f"{inbound.CB_MISPARSE}{rows[0].batch_id}",
            "message": {"message_id": 88},
        },
        notifier=None,
        external_id=None,
        ownership=current,
    )

    assert rows[0].misparse is True
    assert rows[0].integration_connection_id == old_connection_id


async def test_pending_text_rows_cannot_starve_callback_recovery(db_session):
    graph = await _graph(db_session, "callback-starvation")
    for index in range(signals_service.REPARSE_BATCH):
        await inbound._claim_update_raw(
            db_session,
            external_id=f"tg:text-before-{index}",
            payload={
                "update_id": index,
                "message": {"message_id": index, "text": "pending text"},
            },
            ownership=graph.ownership,
        )
    callback = await inbound._claim_update_raw(
        db_session,
        external_id="tg:callback-after-texts",
        payload={
            "update_id": 999,
            "callback_query": {
                "id": "after-texts",
                "data": f"{inbound.CB_CONTEXT}{DAY.isoformat()}:gym:1",
                "message": {"message_id": 99},
            },
        },
        ownership=graph.ownership,
    )

    assert await inbound._replay_pending_callbacks(
        db_session,
        ownership=graph.ownership,
    ) == 1
    await db_session.refresh(callback.raw)
    assert callback.raw.processed_at is not None


async def test_pending_callbacks_cannot_starve_text_reparse(
    db_session,
    monkeypatch,
):
    graph = await _graph(db_session, "text-starvation")
    for index in range(signals_service.REPARSE_BATCH + 5):
        await inbound._claim_update_raw(
            db_session,
            external_id=f"tg:callback-before-{index}",
            payload={
                "update_id": index,
                "callback_query": {
                    "id": f"callback-{index}",
                    "data": f"{inbound.CB_CONTEXT}{DAY.isoformat()}:gym:1",
                    "message": {"message_id": index},
                },
            },
            ownership=graph.ownership,
        )
    text = await inbound._claim_update_raw(
        db_session,
        external_id="tg:text-after-callbacks",
        payload={
            "update_id": 1000,
            "message": {"message_id": 1000, "text": "голова болит"},
        },
        ownership=graph.ownership,
    )
    rows = await inbound.reparse_pending(
        db_session,
        ownership=graph.ownership,
        parse=_one_signal,
    )

    assert len(rows) == 1 and rows[0].raw_id == text.raw.id


async def test_recovery_classifies_commands_questions_and_bot_replies_before_parser(
    db_session,
    monkeypatch,
):
    graph = await _graph(db_session, "recovery-classifier")
    notifier = _Notifier()
    brief = await _durably_send(
        db_session,
        graph,
        notifier,
        text="brief",
        category=delivery.CATEGORY_BRIEF,
        occurrence="recovery-brief",
    )
    evening = await _durably_send(
        db_session,
        graph,
        notifier,
        text="how was the day?",
        category=delivery.CATEGORY_EVENING,
        occurrence="recovery-evening",
    )
    assert brief is not None and evening is not None
    updates = [
        _telegram_text_update(5001, "/start", message_id=501),
        _telegram_text_update(5002, "почему hrv упал?", message_id=502),
        _telegram_text_update(5003, "понял", message_id=503),
        _telegram_text_update(5004, "весь день за компом", message_id=504),
    ]
    updates[2]["message"]["reply_to_message"] = {
        "message_id": int(brief.external_id)
    }
    updates[3]["message"]["reply_to_message"] = {
        "message_id": int(evening.external_id)
    }
    claims = []
    for update in updates:
        claims.append(
            await inbound._claim_update_raw(
                db_session,
                external_id=f"tg:{update['update_id']}",
                payload=update,
                ownership=graph.ownership,
            )
        )
    parsed: list[str] = []

    async def _parse(text):
        parsed.append(text)
        assert text == "весь день за компом"
        return _one_signal(text)

    rows = await inbound.reparse_pending(
        db_session,
        ownership=graph.ownership,
        parse=_parse,
    )

    assert parsed == ["весь день за компом"]
    assert len(rows) == 1
    for claim in claims:
        await db_session.refresh(claim.raw)
        assert claim.raw.processed_at is not None


async def test_recovery_closes_all_database_work_before_parser_await(db_session):
    graph = await _graph(db_session, "recovery-no-transaction")
    gateway = await _gateway(db_session, graph, "recovery-no-transaction")
    claim = await inbound._claim_update_raw(
        db_session,
        external_id="tg:recovery-no-transaction",
        payload=_telegram_text_update(
            1299,
            "голова болит",
            message_id=1299,
        ),
        ownership=graph.ownership,
    )
    transaction_states: list[bool] = []

    async def _parse(text: str):
        transaction_states.append(db_session.in_transaction())
        return _one_signal(text)

    rows = await inbound.reparse_pending(
        db_session,
        ownership=graph.ownership,
        parse=_parse,
        parser_alert_context=_parser_context(graph, gateway),
    )

    assert len(rows) == 1
    assert transaction_states == [False]
    await db_session.refresh(claim.raw)
    assert claim.raw.processed_at is not None


async def test_recovery_batch_any_failure_raises_scoped_provider_alert(db_session):
    graph = await _graph(db_session, "recovery-failure-wins")
    gateway = await _gateway(db_session, graph, "recovery-failure-wins")
    for update_id, text in ((1300, "junk"), (1301, "valid")):
        await inbound._claim_update_raw(
            db_session,
            external_id=f"tg:{update_id}",
            payload=_telegram_text_update(update_id, text, message_id=update_id),
            ownership=graph.ownership,
        )

    def _parse(text: str):
        if text == "junk":
            return [{"kind": "not-a-signal", "key": "ignored"}]
        return _one_signal(text)

    rows = await inbound.reparse_pending(
        db_session,
        ownership=graph.ownership,
        parse=_parse,
        parser_alert_context=_parser_context(graph, gateway),
    )

    assert len(rows) == 1
    alert = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.alert_key == signals_service.PARSER_FAILED_ALERT_KEY
        )
    )
    assert alert is not None and alert.resolved_at is None
    assert (
        alert.subject_id,
        alert.integration_connection_id,
        alert.resolved_by_user_id,
    ) == (graph.subject.id, gateway.id, None)


@pytest.mark.integration
async def test_postgres_concurrent_same_update_is_claimed_and_processed_once(
    db_session,
):
    graph = await _graph(db_session, "concurrent-webhook")
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    parse_calls = 0

    async def _parse(_text):
        nonlocal parse_calls
        parse_calls += 1
        await asyncio.sleep(0.05)
        return _one_signal(_text)

    class _RecordingNotifier(_Notifier):
        def __init__(self):
            super().__init__()
            self.sent = 0

        async def send(self, text, *, buttons=None, reply_to=None) -> str:
            self.sent += 1
            return await super().send(
                text,
                buttons=buttons,
                reply_to=reply_to,
            )

    notifier = _RecordingNotifier()
    update = {
        "update_id": 4242,
        "message": {"message_id": 77, "text": "голова болит"},
    }

    async def run_once() -> None:
        async with factory() as session:
            await inbound.handle_update(
                session,
                update,
                notifier=notifier,
                parse=_parse,
                ownership=graph.ownership,
            )
            await session.commit()

    await asyncio.gather(run_once(), run_once())

    async with factory() as verify:
        raws = list(
            await verify.scalars(
                select(RawPayload).where(RawPayload.external_id == "tg:4242")
            )
        )
        signals = list(await verify.scalars(select(Signal)))
    assert len(raws) == 1
    assert len(signals) == 1
    assert parse_calls == 1
    assert notifier.sent == 1


@pytest.mark.integration
async def test_postgres_recovery_parser_holds_no_subject_or_telegram_root_lock(
    db_session,
):
    graph = await _graph(db_session, "recovery-unlocked")
    gateway = await _gateway(db_session, graph, "recovery-unlocked")
    await inbound._claim_update_raw(
        db_session,
        external_id="tg:recovery-unlocked",
        payload=_telegram_text_update(5301, "голова болит", message_id=530),
        ownership=graph.ownership,
    )
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    parser_started = asyncio.Event()
    release_parser = asyncio.Event()
    transaction_states: list[bool] = []

    async def _recover() -> None:
        async with factory() as session:
            async def _parse(text: str):
                transaction_states.append(session.in_transaction())
                parser_started.set()
                await release_parser.wait()
                return _one_signal(text)

            await inbound.reparse_pending(
                session,
                ownership=graph.ownership,
                parse=_parse,
                parser_alert_context=_parser_context(graph, gateway),
            )

    recovery = asyncio.create_task(_recover())
    await asyncio.wait_for(parser_started.wait(), timeout=5)
    async with factory() as contender:
        await asyncio.wait_for(
            contender.execute(
                select(HealthSubject)
                .where(HealthSubject.id == graph.subject.id)
                .with_for_update()
            ),
            timeout=1,
        )
        await asyncio.wait_for(
            contender.execute(
                select(IntegrationConnection)
                .where(IntegrationConnection.id == graph.connection.id)
                .with_for_update()
            ),
            timeout=1,
        )
        await contender.commit()
    release_parser.set()
    await asyncio.wait_for(recovery, timeout=5)
    assert transaction_states == [False]


@pytest.mark.integration
async def test_postgres_openrouter_rotation_in_flight_never_rebinds_parser_alert(
    db_session,
):
    graph = await _graph(db_session, "parser-rotation")
    old_gateway = await _gateway(db_session, graph, "parser-rotation-old")
    await inbound._claim_update_raw(
        db_session,
        external_id="tg:parser-rotation",
        payload=_telegram_text_update(5302, "голова болит", message_id=531),
        ownership=graph.ownership,
    )
    await db_session.commit()
    old_gateway_id = old_gateway.id
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    parser_started = asyncio.Event()
    release_parser = asyncio.Event()

    async def _recover() -> None:
        async with factory() as session:
            async def _parse(_text: str):
                parser_started.set()
                await release_parser.wait()
                raise RuntimeError("synthetic OpenRouter outage")

            await inbound.reparse_pending(
                session,
                ownership=graph.ownership,
                parse=_parse,
                parser_alert_context=_parser_context(graph, old_gateway),
            )

    recovery = asyncio.create_task(_recover())
    await asyncio.wait_for(parser_started.wait(), timeout=5)
    async with factory() as rotation:
        old = await rotation.get(IntegrationConnection, old_gateway_id)
        assert old is not None
        old.status = IntegrationConnectionStatus.RETIRED.value
        old.retired_at = datetime.now(timezone.utc)
        replacement = IntegrationConnection(
            subject_id=graph.subject.id,
            provider=IntegrationProvider.OPENROUTER.value,
            connection_type=IntegrationConnectionType.AI_GATEWAY.value,
            external_account_discriminator="synthetic:parser-rotation-new",
            status=IntegrationConnectionStatus.ACTIVE.value,
        )
        rotation.add(replacement)
        await rotation.commit()
        replacement_id = replacement.id
    release_parser.set()
    await asyncio.wait_for(recovery, timeout=5)

    async with factory() as verify:
        alerts = list(
            await verify.scalars(
                select(SystemAlert).where(
                    SystemAlert.alert_key
                    == signals_service.PARSER_FAILED_ALERT_KEY
                )
            )
        )
        raw = await verify.scalar(
            select(RawPayload).where(
                RawPayload.external_id == "tg:parser-rotation"
            )
        )
    assert alerts == []
    assert raw is not None and raw.processed_at is None
    assert replacement_id != old_gateway_id


@pytest.mark.integration
async def test_postgres_preparse_validation_ignores_stale_identity_map(
    db_session,
):
    graph = await _graph(db_session, "parser-stale-root")
    old_gateway = await _gateway(db_session, graph, "parser-stale-root-old")
    await inbound._claim_update_raw(
        db_session,
        external_id="tg:parser-stale-root",
        payload=_telegram_text_update(5303, "голова болит", message_id=532),
        ownership=graph.ownership,
    )
    await db_session.commit()
    old_gateway_id = old_gateway.id
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    preloaded = asyncio.Event()
    rotated = asyncio.Event()
    parser_calls = 0

    async def _recover() -> None:
        nonlocal parser_calls
        async with factory() as session:
            stale = await session.get(IntegrationConnection, old_gateway_id)
            assert stale is not None
            assert stale.status == IntegrationConnectionStatus.ACTIVE.value
            preloaded.set()
            await rotated.wait()

            async def _parse(_text: str):
                nonlocal parser_calls
                parser_calls += 1
                return _one_signal(_text)

            with pytest.raises(inbound.InboundOwnershipError, match="cannot parse"):
                await inbound.reparse_pending(
                    session,
                    ownership=graph.ownership,
                    parse=_parse,
                    parser_alert_context=_parser_context(graph, old_gateway),
                )
            await session.rollback()

    recovery = asyncio.create_task(_recover())
    await asyncio.wait_for(preloaded.wait(), timeout=5)
    async with factory() as rotation:
        old = await rotation.get(IntegrationConnection, old_gateway_id)
        assert old is not None
        old.status = IntegrationConnectionStatus.RETIRED.value
        old.retired_at = datetime.now(timezone.utc)
        rotation.add(
            IntegrationConnection(
                subject_id=graph.subject.id,
                provider=IntegrationProvider.OPENROUTER.value,
                connection_type=IntegrationConnectionType.AI_GATEWAY.value,
                external_account_discriminator="synthetic:parser-stale-root-new",
                status=IntegrationConnectionStatus.ACTIVE.value,
            )
        )
        await rotation.commit()
    rotated.set()
    await asyncio.wait_for(recovery, timeout=5)
    assert parser_calls == 0


@pytest.mark.integration
@pytest.mark.parametrize("older_is_edit", [False, True])
async def test_postgres_newer_edit_wins_while_older_parser_is_in_flight(
    db_session,
    older_is_edit,
):
    graph = await _graph(db_session, f"edit-race-{older_is_edit}")
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    older_started = asyncio.Event()
    release_older = asyncio.Event()

    async def _parse(text):
        if text == "older version":
            older_started.set()
            await release_older.wait()
            key = "headache"
        else:
            key = "sleepiness"
        return [
            {
                "kind": SignalKind.SYMPTOM.value,
                "key": key,
                "value_num": 3,
                "note": text,
            }
        ]

    notifier = _bind_notifier(graph, _RaceNotifier())
    older = _telegram_text_update(
        5101,
        "older version",
        message_id=510,
        edited=older_is_edit,
    )
    newer = _telegram_text_update(
        5102,
        "newest version",
        message_id=510,
        edited=True,
    )

    async def _run(update: dict) -> None:
        async with factory() as session:
            await inbound.handle_update(
                session,
                update,
                notifier=notifier,
                parse=_parse,
                ownership=graph.ownership,
            )
            await session.commit()

    older_task = asyncio.create_task(_run(older))
    await asyncio.wait_for(older_started.wait(), timeout=2)
    await _run(newer)
    release_older.set()
    await asyncio.wait_for(older_task, timeout=2)

    async with factory() as verify:
        rows = list(await verify.scalars(select(Signal).order_by(Signal.id)))
        raws = list(
            await verify.scalars(
                select(RawPayload)
                .where(RawPayload.external_id.in_(["tg:5101", "tg:5102"]))
                .order_by(RawPayload.id)
            )
        )
    assert [(row.key, row.misparse) for row in rows] == [
        ("sleepiness", False)
    ]
    assert len(raws) == 2 and all(raw.processed_at is not None for raw in raws)
    assert len(notifier.messages) == 1
    assert "sleepiness" in notifier.messages[0]


@pytest.mark.integration
async def test_postgres_edit_processed_before_late_original_cannot_resurrect_fact(
    db_session,
):
    graph = await _graph(db_session, "inverse-edit-race")
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    notifier = _bind_notifier(graph, _RaceNotifier())

    async def _parse(text):
        return [
            {
                "kind": SignalKind.SYMPTOM.value,
                "key": "sleepiness" if text == "newest version" else "headache",
                "value_num": 3,
                "note": text,
            }
        ]

    async def _run(update: dict) -> None:
        async with factory() as session:
            await inbound.handle_update(
                session,
                update,
                notifier=notifier,
                parse=_parse,
                ownership=graph.ownership,
            )
            await session.commit()

    await _run(
        _telegram_text_update(
            5202,
            "newest version",
            message_id=520,
            edited=True,
        )
    )
    await _run(
        _telegram_text_update(
            5201,
            "late original",
            message_id=520,
        )
    )

    async with factory() as verify:
        rows = list(await verify.scalars(select(Signal).order_by(Signal.id)))
        raws = list(
            await verify.scalars(
                select(RawPayload)
                .where(RawPayload.external_id.in_(["tg:5201", "tg:5202"]))
                .order_by(RawPayload.id)
            )
        )
    assert [(row.key, row.misparse) for row in rows] == [
        ("sleepiness", False)
    ]
    assert len(raws) == 2 and all(raw.processed_at is not None for raw in raws)
    assert len(notifier.messages) == 1


async def test_delivery_journal_and_reply_lookup_are_fully_scoped(
    db_session,
    signals_module_on,
):
    first = await _graph(db_session, "first")
    second = await _graph(db_session, "second")
    first_row = Notification(
        subject_id=first.subject.id,
        recipient_user_id=first.user.id,
        integration_connection_id=first.connection.id,
        sent_at=datetime(2026, 8, 19, 12),
        category=delivery.CATEGORY_REPLY,
        dedupe_key="first-key",
        channel=IntegrationProvider.TELEGRAM.value,
        external_id="701",
        payload={"text": "first"},
    )
    second_row = Notification(
        subject_id=second.subject.id,
        recipient_user_id=second.user.id,
        integration_connection_id=second.connection.id,
        sent_at=datetime(2026, 8, 19, 12),
        category=delivery.CATEGORY_REPLY,
        dedupe_key="second-key",
        channel=IntegrationProvider.TELEGRAM.value,
        external_id="701",
        payload={"text": "second"},
    )
    manual_row = Notification(
        subject_id=first.subject.id,
        actor_user_id=first.user.id,
        recipient_user_id=first.user.id,
        integration_connection_id=first.connection.id,
        sent_at=datetime(2026, 8, 19, 12, 1),
        category=delivery.CATEGORY_TEST,
        channel=IntegrationProvider.TELEGRAM.value,
        external_id="702",
        payload={"text": "manual test"},
    )
    db_session.add_all([first_row, second_row, manual_row])
    await db_session.commit()

    assert first_row is not None and second_row is not None and manual_row is not None
    assert first_row.external_id == second_row.external_id == "701"
    assert (
        first_row.subject_id,
        first_row.recipient_user_id,
        first_row.integration_connection_id,
        first_row.actor_user_id,
    ) == (first.subject.id, first.user.id, first.connection.id, None)
    assert manual_row.actor_user_id == first.user.id
    assert await delivery.find_sent(
        db_session,
        "701",
        ownership=first.ownership,
    ) is first_row
    assert await delivery.find_sent(
        db_session,
        "701",
        ownership=second.ownership,
    ) is second_row
    assert await delivery.already_sent(
        db_session,
        "first-key",
        ownership=second.ownership,
    ) is False
    forged = _bind_notifier(first, _Notifier())
    with pytest.raises(ValueError, match="actor must be the recipient"):
        await delivery.prepare_delivery_intent(
            db_session,
            forged,
            text="forged actor",
            category=delivery.CATEGORY_TEST,
            idempotency_key=delivery.make_delivery_idempotency_key(
                "ownership-test",
                "forged-actor",
            ),
            ownership=first.ownership,
            actor_user_id=second.user.id,
        )


async def test_multi_subject_legacy_transport_fails_before_network_send(
    db_session,
    signals_module_on,
):
    first = await _graph(db_session, "dedupe-first")
    await _graph(db_session, "dedupe-second")

    notifier = _bind_notifier(first, _Notifier())
    with pytest.raises(delivery.DeliveryPolicyUnavailableError, match="exactly one"):
        await delivery.prepare_delivery_intent(
            db_session,
            notifier,
            text="must not leave the process",
            category=delivery.CATEGORY_BRIEF,
            idempotency_key=delivery.make_delivery_idempotency_key(
                "ownership-test",
                "multi-subject",
            ),
            ownership=first.ownership,
        )
    assert notifier._next_id == 700
    assert list(await db_session.scalars(select(Notification))) == []


async def test_partial_root_dedupe_is_rejected_by_the_schema(db_session):
    graph = await _graph(db_session, "partial-dedupe")
    db_session.add(
        Notification(
            subject_id=graph.subject.id,
            recipient_user_id=graph.user.id,
            integration_connection_id=None,
            category=delivery.CATEGORY_BRIEF,
            dedupe_key="brief:partial-root",
            channel="telegram",
            external_id="legacy-partial",
            payload={"text": "legacy"},
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
    assert list(await db_session.scalars(select(Notification))) == []


async def test_cross_subject_notification_connection_cannot_leak_or_send(db_session):
    first = await _graph(db_session, "cross-c-first")
    second = await _graph(db_session, "cross-c-second")
    db_session.add(
        Notification(
            subject_id=first.subject.id,
            recipient_user_id=first.user.id,
            integration_connection_id=second.connection.id,
            category=delivery.CATEGORY_BRIEF,
            dedupe_key="brief:cross-c",
            channel="telegram",
            external_id="cross-c",
            payload={"text": "invalid roots"},
        )
    )
    await db_session.commit()
    assert await delivery.sent_today(
        db_session,
        on_date=DAY,
        ownership=first.ownership,
    ) == 0
    with pytest.raises(delivery.DeliveryStateError, match="connection graph"):
        await delivery.already_sent(
            db_session,
            "brief:cross-c",
            ownership=first.ownership,
        )
    notifier = _bind_notifier(first, _Notifier())
    with pytest.raises(delivery.DeliveryPolicyUnavailableError, match="exactly one"):
        await delivery.prepare_delivery_intent(
            db_session,
            notifier,
            text="must not leave the process",
            category=delivery.CATEGORY_BRIEF,
            idempotency_key=delivery.make_delivery_idempotency_key(
                "ownership-test",
                "cross-subject-connection",
            ),
            ownership=first.ownership,
        )
    assert notifier._next_id == 700


@pytest.mark.parametrize("invalid_kind", ["pending", "wrong_channel"])
async def test_invalid_historical_notification_cannot_suppress_or_start_cooldown(
    db_session,
    signals_module_on,
    invalid_kind,
):
    graph = await _graph(db_session, f"notification-{invalid_kind}")
    connection = graph.connection
    channel = IntegrationProvider.TELEGRAM.value
    if invalid_kind == "pending":
        connection = IntegrationConnection(
            subject_id=graph.subject.id,
            provider=IntegrationProvider.TELEGRAM.value,
            connection_type=IntegrationConnectionType.RECIPIENT.value,
            external_account_discriminator="synthetic:pending-notification",
            status=IntegrationConnectionStatus.PENDING.value,
        )
        db_session.add(connection)
        await db_session.flush()
    else:
        channel = IntegrationProvider.OPENROUTER.value
    key = f"invalid-{invalid_kind}"
    dedupe = f"nudge:{key}:2026-08-19T12"
    db_session.add(
        Notification(
            subject_id=graph.subject.id,
            actor_user_id=None,
            recipient_user_id=graph.user.id,
            integration_connection_id=connection.id,
            sent_at=datetime(2026, 8, 19, 12, 0),
            category=delivery.CATEGORY_NUDGE,
            dedupe_key=dedupe,
            channel=channel,
            external_id=f"invalid-{invalid_kind}",
            payload={"text": "must not suppress"},
        )
    )
    await db_session.commit()

    with pytest.raises(delivery.DeliveryStateError, match="connection graph"):
        await delivery.sent_today(
            db_session,
            on_date=DAY,
            ownership=graph.ownership,
        )
    with pytest.raises(delivery.DeliveryStateError, match="connection graph"):
        await delivery.already_sent(
            db_session,
            dedupe,
            ownership=graph.ownership,
        )
    assert await nudges.last_sent_at(
        db_session,
        key,
        ownership=graph.ownership,
    ) is None

    notifier = _Notifier()
    policy_key = delivery.make_delivery_policy_key("nudge", key)
    with pytest.raises(delivery.DeliveryError):
        await delivery.delivery_policy_claimed_since(
            db_session,
            policy_key=policy_key,
            not_before=datetime(2026, 8, 19, 11, tzinfo=timezone.utc),
            ownership=graph.ownership,
            legacy_dedupe_prefix=f"nudge:{key}:",
        )
    assert notifier._next_id == 700


async def test_delivery_revalidates_subject_recipient_and_connection_before_network(
    db_session,
):
    first = await _graph(db_session, "scope-first")
    foreign_user = User(
        username="scope-foreign-user",
        normalized_username="scope-foreign-user",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(foreign_user)
    wrong_provider = IntegrationConnection(
        subject_id=first.subject.id,
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator="synthetic:wrong-provider",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add(wrong_provider)
    await db_session.flush()

    invalid_contexts = (
        ProactiveOwnershipContext(
            subject_id=first.subject.id,
            recipient_user_id=foreign_user.id,
            connection_id=first.connection.id,
        ),
        ProactiveOwnershipContext(
            subject_id=first.subject.id,
            recipient_user_id=first.user.id,
            connection_id=uuid.uuid4(),
        ),
        ProactiveOwnershipContext(
            subject_id=first.subject.id,
            recipient_user_id=first.user.id,
            connection_id=wrong_provider.id,
        ),
    )
    for index, ownership in enumerate(invalid_contexts):
        notifier = _Notifier()
        notifier.binding = channels.DeliveryEndpointBinding(
            subject_id=ownership.subject_id,
            recipient_user_id=ownership.recipient_user_id,
            integration_connection_id=ownership.connection_id,
            channel=IntegrationProvider.TELEGRAM.value,
        )
        with pytest.raises(delivery.DeliveryScopeError):
            await delivery.prepare_delivery_intent(
                db_session,
                notifier,
                text="forged scope",
                category=delivery.CATEGORY_REPLY,
                idempotency_key=delivery.make_delivery_idempotency_key(
                    "ownership-test",
                    "forged-scope",
                    index,
                ),
                ownership=ownership,
            )
        assert notifier._next_id == 700

    for inactive_status in (
        IntegrationConnectionStatus.PENDING.value,
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    ):
        first.connection.status = inactive_status
        first.connection.retired_at = (
            datetime.now(timezone.utc)
            if inactive_status == IntegrationConnectionStatus.RETIRED.value
            else None
        )
        await db_session.flush()
        notifier = _bind_notifier(first, _Notifier())
        with pytest.raises(delivery.DeliveryScopeError):
            await delivery.prepare_delivery_intent(
                db_session,
                notifier,
                text="inactive channel",
                category=delivery.CATEGORY_REPLY,
                idempotency_key=delivery.make_delivery_idempotency_key(
                    "ownership-test",
                    "inactive-channel",
                    inactive_status,
                ),
                ownership=first.ownership,
            )
        assert notifier._next_id == 700


@pytest.mark.parametrize(
    "historical_status",
    [
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    ],
)
async def test_connection_rotation_does_not_reset_budget_or_dedupe(
    db_session,
    signals_module_on,
    historical_status,
):
    graph = await _graph(db_session, "rotated")
    legacy_key = "brief:rotation-day"
    delivery_key = delivery.make_delivery_idempotency_key(
        "ownership-test",
        "rotation-day",
    )
    sent = await _durably_send(
        db_session,
        graph,
        _Notifier(),
        text="before rotation",
        category=delivery.CATEGORY_BRIEF,
        occurrence="rotation-day",
        legacy_dedupe_key=legacy_key,
        now=datetime(2026, 8, 19, 12, 0),
    )
    replacement = IntegrationConnection(
        subject_id=graph.subject.id,
        provider=IntegrationProvider.TELEGRAM.value,
        connection_type=IntegrationConnectionType.RECIPIENT.value,
        external_account_discriminator="synthetic:replacement",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add(replacement)
    await db_session.flush()
    rotated = ProactiveOwnershipContext(
        subject_id=graph.subject.id,
        recipient_user_id=graph.user.id,
        connection_id=replacement.id,
    )
    graph.connection.status = historical_status
    graph.connection.retired_at = (
        datetime.now(timezone.utc)
        if historical_status == IntegrationConnectionStatus.RETIRED.value
        else None
    )
    await db_session.flush()

    assert await delivery.sent_today(
        db_session,
        on_date=DAY,
        ownership=rotated,
    ) == 1
    assert await delivery.already_sent(
        db_session,
        delivery_key,
        ownership=rotated,
        legacy_dedupe_key=legacy_key,
    ) is True
    assert await delivery.find_sent(
        db_session,
        sent.external_id,
        ownership=rotated,
    ) is sent
    assert await delivery.recent_sent(
        db_session,
        ownership=rotated,
    ) == [sent]


async def test_raw_commit_survives_normalized_and_journal_rollback(
    db_session,
    signals_module_on,
):
    graph = await _graph(db_session, "rollback")
    expected_roots = (
        graph.subject.id,
        graph.user.id,
        graph.connection.id,
    )
    raw = await signals_service.store_raw_text(
        db_session,
        text="голова болит",
        external_id="tg:rollback",
        identity=graph.ownership.owner_action(),
        integration_connection_id=graph.connection.id,
    )
    await db_session.commit()
    await signals_service.create_signals(
        db_session,
        items=_one_signal(""),
        raw_id=raw.id,
        identity=graph.ownership.owner_action(),
        integration_connection_id=graph.connection.id,
    )
    notifier = _bind_notifier(graph, _Notifier())
    prepared = await delivery.prepare_delivery_intent(
        db_session,
        notifier,
        text="will roll back",
        category=delivery.CATEGORY_ECHO,
        idempotency_key=delivery.make_delivery_idempotency_key(
            "ownership-test",
            "rollback-echo",
        ),
        ownership=graph.ownership,
        raw_payload_id=raw.id,
    )
    assert prepared is not None

    await db_session.rollback()

    with pytest.raises(delivery.DeliveryCapabilityError):
        await delivery.start_delivery_dispatch(
            db_session,
            prepared,
            notifier_resolver=lambda *_: notifier,
        )
    await db_session.rollback()

    kept_raw = await db_session.scalar(
        select(RawPayload).where(RawPayload.external_id == "tg:rollback")
    )
    assert kept_raw is not None
    assert (
        kept_raw.subject_id,
        kept_raw.actor_user_id,
        kept_raw.integration_connection_id,
    ) == expected_roots
    assert list(await db_session.scalars(select(Signal))) == []
    assert list(await db_session.scalars(select(Notification))) == []
    assert list(
        await db_session.scalars(select(NotificationDeliveryIntent))
    ) == []
