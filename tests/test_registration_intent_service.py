"""Open-registration intent issuance, locking, and one-time consumption."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from vitals.enums import RegistrationAccountKind, RegistrationIntentStatus
from vitals.models.identity import AuditEvent
from vitals.models.registration import RegistrationIntent
from vitals.services.authentication import admission
from vitals.services.authentication import registration as registration_policy
from vitals.services.authentication.admission import intents as intent_service


async def _mode(db_session, monkeypatch, mode) -> None:
    monkeypatch.setenv(registration_policy.REGISTRATION_UNLOCK_ENV, "1")
    await registration_policy.set_stored_mode(db_session, mode)


async def _open(db_session, monkeypatch) -> None:
    await _mode(db_session, monkeypatch, registration_policy.RegistrationMode.OPEN)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@pytest.mark.parametrize("kind", list(RegistrationAccountKind))
async def test_issue_intent_accepts_only_the_three_non_privileged_account_kinds(
    db_session, monkeypatch, kind
):
    await _open(db_session, monkeypatch)
    intent = await admission.issue_intent(db_session, account_kind=kind)

    assert intent.account_kind == kind.value
    assert intent.status == RegistrationIntentStatus.PENDING.value
    assert _utc(intent.expires_at) > _utc(intent.created_at)
    assert _utc(intent.expires_at) - _utc(intent.created_at) <= admission.INTENT_TTL


@pytest.mark.parametrize(
    "kind",
    ["platform_superadmin", "member ", "", None, True],
)
async def test_issue_intent_rejects_unknown_or_non_exact_account_kinds(
    db_session, monkeypatch, kind
):
    await _open(db_session, monkeypatch)
    with pytest.raises(admission.AdmissionValidationError):
        await admission.issue_intent(db_session, account_kind=kind)
    assert await db_session.scalar(
        select(func.count()).select_from(RegistrationIntent)
    ) == 0


@pytest.mark.parametrize(
    "ttl",
    [
        timedelta(0),
        timedelta(seconds=-1),
        admission.MAX_INTENT_TTL + timedelta(microseconds=1),
    ],
)
async def test_issue_intent_ttl_is_positive_and_cannot_outlive_browser_handoff(
    db_session, monkeypatch, ttl
):
    await _open(db_session, monkeypatch)
    with pytest.raises(admission.AdmissionValidationError):
        await admission.issue_intent(
            db_session,
            account_kind=RegistrationAccountKind.MEMBER,
            ttl=ttl,
        )


async def test_issue_requires_effective_open_mode_and_does_not_commit(
    db_session, monkeypatch
):
    monkeypatch.setenv(registration_policy.REGISTRATION_UNLOCK_ENV, "1")
    commit = AsyncMock(side_effect=AssertionError("service must not commit"))
    monkeypatch.setattr(db_session, "commit", commit)

    with pytest.raises(admission.AdmissionRefused):
        await admission.issue_intent(
            db_session,
            account_kind=RegistrationAccountKind.MEMBER,
        )
    commit.assert_not_awaited()
    assert await db_session.scalar(
        select(func.count()).select_from(RegistrationIntent)
    ) == 0


async def test_issue_and_consume_are_audited_without_pii(db_session, monkeypatch):
    await _open(db_session, monkeypatch)
    intent = await admission.issue_intent(
        db_session,
        account_kind=RegistrationAccountKind.DOCTOR,
    )
    consumed = await admission.consume_intent(db_session, intent_id=intent.id)

    assert consumed.status == RegistrationIntentStatus.CONSUMED.value
    assert consumed.consumed_at is not None
    events = list(
        await db_session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type.like("registration.intent.%"))
        )
    )
    assert {event.event_type for event in events} == {
        "registration.intent.issued",
        "registration.intent.consumed",
    }
    assert len(events) == 2
    assert all(event.actor_user_id is None and event.subject_id is None for event in events)
    envelope = json.dumps([event.metadata_json for event in events], sort_keys=True)
    for forbidden in ("email", "issuer", "subject", "username", "password"):
        assert forbidden not in envelope.casefold()


async def test_lock_does_not_consume_and_mode_closure_invalidates_pending_intent(
    db_session, monkeypatch
):
    await _open(db_session, monkeypatch)
    intent = await admission.issue_intent(
        db_session,
        account_kind=RegistrationAccountKind.TRAINER,
    )

    locked = await admission.lock_intent(db_session, intent_id=intent.id)
    assert locked.id == intent.id
    assert locked.status == RegistrationIntentStatus.PENDING.value

    await _mode(
        db_session,
        monkeypatch,
        registration_policy.RegistrationMode.DISABLED,
    )
    with pytest.raises(admission.AdmissionRefused):
        await admission.consume_intent(db_session, intent_id=intent.id)
    assert intent.status == RegistrationIntentStatus.PENDING.value


async def test_replay_unknown_and_invalid_ids_are_uniform_and_do_not_mutate_twice(
    db_session, monkeypatch
):
    await _open(db_session, monkeypatch)
    intent = await admission.issue_intent(
        db_session,
        account_kind=RegistrationAccountKind.MEMBER,
    )
    await admission.consume_intent(db_session, intent_id=intent.id)

    messages = set()
    for intent_id in (intent.id, uuid.uuid4(), uuid.UUID(int=0), "not-a-uuid"):
        with pytest.raises(admission.AdmissionRefused) as caught:
            await admission.consume_intent(db_session, intent_id=intent_id)
        messages.add(str(caught.value))
    assert len(messages) == 1
    assert await db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.event_type == "registration.intent.consumed")
    ) == 1


async def test_expiry_uses_database_clock_and_records_one_terminal_transition(
    db_session, monkeypatch
):
    await _open(db_session, monkeypatch)
    intent = await admission.issue_intent(
        db_session,
        account_kind=RegistrationAccountKind.MEMBER,
    )
    database_time = intent.expires_at + timedelta(microseconds=1)
    clock = AsyncMock(return_value=database_time)
    monkeypatch.setattr(intent_service, "database_now", clock)

    with pytest.raises(admission.AdmissionRefused):
        await admission.lock_intent(db_session, intent_id=intent.id)
    assert intent.status == RegistrationIntentStatus.EXPIRED.value
    assert intent.expired_at == database_time
    clock.assert_awaited_once_with(db_session)

    with pytest.raises(admission.AdmissionRefused):
        await admission.lock_intent(db_session, intent_id=intent.id)
    assert await db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.event_type == "registration.intent.expired")
    ) == 1


async def test_each_transition_takes_the_identity_governance_lock(
    db_session, monkeypatch
):
    await _open(db_session, monkeypatch)
    lock = AsyncMock()
    monkeypatch.setattr(intent_service, "acquire_identity_governance_lock", lock)

    intent = await admission.issue_intent(
        db_session,
        account_kind=RegistrationAccountKind.MEMBER,
    )
    await admission.lock_intent(db_session, intent_id=intent.id)
    await admission.consume_intent(db_session, intent_id=intent.id)

    assert lock.await_count == 3
