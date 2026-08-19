"""Ownership/provenance seam for Telegram capture and proactive delivery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
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
from vitals.models.proactive import Notification
from vitals.models.raw_payload import RawPayload
from vitals.models.scoped_settings import SubjectSetting
from vitals.models.signals import DayContext, Signal
from vitals.models.tenancy import FileAsset, IntegrationConnection
from vitals.services import signals_service
from vitals.services.proactive import day_plan, delivery, inbound, nudges
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
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    session.add(connection)
    await session.flush()
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


async def test_day_facts_reject_foreign_actor_and_gateway_roots(db_session):
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

    facts = await inbound._day_facts(
        db_session,
        ownership=first.ownership,
    )

    assert "visible" in facts
    assert "must not leak" not in facts


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
    assert await delivery.find_sent(
        db_session,
        "partial-message",
        ownership=legacy,
    ) is None


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
        "message": {"message_id": 7, "text": "question"},
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
    monkeypatch.setattr(
        inbound,
        "make_signal_parser",
        lambda known=None: _one_signal,
    )

    rows = await inbound.reparse_pending(db_session, ownership=current)
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
    monkeypatch.setattr(
        inbound,
        "make_signal_parser",
        lambda known=None: _one_signal,
    )

    rows = await inbound.reparse_pending(db_session, ownership=graph.ownership)

    assert len(rows) == 1 and rows[0].raw_id == text.raw.id


async def test_recovery_classifies_commands_questions_and_bot_replies_before_parser(
    db_session,
    monkeypatch,
):
    graph = await _graph(db_session, "recovery-classifier")
    notifier = _Notifier()
    brief = await delivery.send(
        db_session,
        notifier,
        text="brief",
        category=delivery.CATEGORY_BRIEF,
        ownership=graph.ownership,
    )
    evening = await delivery.send(
        db_session,
        notifier,
        text="how was the day?",
        category=delivery.CATEGORY_EVENING,
        ownership=graph.ownership,
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

    monkeypatch.setattr(inbound, "make_signal_parser", lambda known=None: _parse)

    rows = await inbound.reparse_pending(
        db_session,
        ownership=graph.ownership,
    )

    assert parsed == ["весь день за компом"]
    assert len(rows) == 1
    for claim in claims:
        await db_session.refresh(claim.raw)
        assert claim.raw.processed_at is not None


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

    notifier = _RaceNotifier()
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
    notifier = _RaceNotifier()

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
    first_notifier = _Notifier()
    second_notifier = _Notifier()

    first_row = await delivery.send(
        db_session,
        first_notifier,
        text="first",
        category=delivery.CATEGORY_REPLY,
        dedupe_key="first-key",
        ownership=first.ownership,
    )
    second_row = await delivery.send(
        db_session,
        second_notifier,
        text="second",
        category=delivery.CATEGORY_REPLY,
        dedupe_key="second-key",
        ownership=second.ownership,
    )
    manual_row = await delivery.send(
        db_session,
        first_notifier,
        text="manual test",
        category=delivery.CATEGORY_TEST,
        ownership=first.ownership,
        actor_user_id=first.user.id,
    )
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
    with pytest.raises(ValueError, match="actor must be the recipient"):
        await delivery.send(
            db_session,
            first_notifier,
            text="forged actor",
            category=delivery.CATEGORY_TEST,
            ownership=first.ownership,
            actor_user_id=second.user.id,
        )


async def test_foreign_global_dedupe_fails_before_network_send(
    db_session,
    signals_module_on,
):
    first = await _graph(db_session, "dedupe-first")
    second = await _graph(db_session, "dedupe-second")
    await delivery.send(
        db_session,
        _Notifier(),
        text="first subject",
        category=delivery.CATEGORY_BRIEF,
        dedupe_key="brief:shared-date",
        ownership=first.ownership,
    )
    await db_session.commit()

    second_notifier = _Notifier()
    with pytest.raises(
        delivery.NotificationOwnershipConflictError,
        match="another ownership scope",
    ):
        await delivery.send(
            db_session,
            second_notifier,
            text="second subject",
            category=delivery.CATEGORY_BRIEF,
            dedupe_key="brief:shared-date",
            ownership=second.ownership,
        )
    assert second_notifier._next_id == 700
    assert len(list(await db_session.scalars(select(Notification)))) == 1


async def test_partial_root_dedupe_fails_before_network_send(db_session):
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
    await db_session.commit()
    assert await delivery.sent_today(
        db_session,
        on_date=DAY,
        ownership=graph.ownership,
    ) == 0

    notifier = _Notifier()
    with pytest.raises(
        delivery.NotificationOwnershipConflictError,
        match="another ownership scope",
    ):
        await delivery.send(
            db_session,
            notifier,
            text="must not leave the process",
            category=delivery.CATEGORY_BRIEF,
            dedupe_key="brief:partial-root",
            ownership=graph.ownership,
        )
    assert notifier._next_id == 700


async def test_cross_subject_connection_cannot_suppress_delivery(db_session):
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

    notifier = _Notifier()
    with pytest.raises(delivery.NotificationOwnershipConflictError):
        await delivery.send(
            db_session,
            notifier,
            text="must still fail before send",
            category=delivery.CATEGORY_BRIEF,
            dedupe_key="brief:cross-c",
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

    assert await delivery.sent_today(
        db_session,
        on_date=DAY,
        ownership=graph.ownership,
    ) == 0
    assert await delivery.already_sent(
        db_session,
        dedupe,
        ownership=graph.ownership,
    ) is False
    assert await nudges.last_sent_at(
        db_session,
        key,
        ownership=graph.ownership,
    ) is None

    notifier = _Notifier()
    with pytest.raises(delivery.NotificationOwnershipConflictError):
        await delivery.send(
            db_session,
            notifier,
            text="must fail before network",
            category=delivery.CATEGORY_NUDGE,
            dedupe_key=dedupe,
            now=datetime(2026, 8, 19, 12, 30),
            ownership=graph.ownership,
        )
    assert notifier._next_id == 700


async def test_delivery_revalidates_subject_recipient_and_connection_before_network(
    db_session,
):
    first = await _graph(db_session, "scope-first")
    second = await _graph(db_session, "scope-second")
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
            recipient_user_id=second.user.id,
            connection_id=first.connection.id,
        ),
        ProactiveOwnershipContext(
            subject_id=first.subject.id,
            recipient_user_id=first.user.id,
            connection_id=second.connection.id,
        ),
        ProactiveOwnershipContext(
            subject_id=first.subject.id,
            recipient_user_id=first.user.id,
            connection_id=wrong_provider.id,
        ),
    )
    for ownership in invalid_contexts:
        notifier = _Notifier()
        with pytest.raises(delivery.ProactiveOwnershipScopeError):
            await delivery.send(
                db_session,
                notifier,
                text="forged scope",
                category=delivery.CATEGORY_REPLY,
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
        notifier = _Notifier()
        with pytest.raises(delivery.ProactiveOwnershipScopeError, match="inactive"):
            await delivery.send(
                db_session,
                notifier,
                text="inactive channel",
                category=delivery.CATEGORY_REPLY,
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
    sent = await delivery.send(
        db_session,
        _Notifier(),
        text="before rotation",
        category=delivery.CATEGORY_BRIEF,
        dedupe_key="brief:rotation-day",
        now=datetime(2026, 8, 19, 12, 0),
        ownership=graph.ownership,
    )
    assert sent is not None
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
        "brief:rotation-day",
        ownership=rotated,
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
    await signals_service.create_signals(
        db_session,
        items=_one_signal(""),
        raw_id=raw.id,
        identity=graph.ownership.owner_action(),
        integration_connection_id=graph.connection.id,
    )
    sent = await delivery.send(
        db_session,
        _Notifier(),
        text="will roll back",
        category=delivery.CATEGORY_ECHO,
        ownership=graph.ownership,
    )
    assert sent is not None

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
