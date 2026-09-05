"""Federation consumes one exact public-registration account choice."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from vitals.enums import (
    RegistrationAccountKind,
    RegistrationIntentStatus,
    UserRoleName,
)
from vitals.models.identity import (
    HealthSubject,
    User,
    UserFederatedIdentity,
    UserRole,
)
from vitals.models.registration import RegistrationIntent
from vitals.services.authentication import admission, federation
from vitals.services.authentication import registration as registration_policy
from vitals.services.authentication.admission import intents as intent_service
from vitals.services.authentication.oidc import FederatedIdentity

ISSUER = "https://idp.example.test"


async def _open(db_session, monkeypatch) -> None:
    monkeypatch.setenv(registration_policy.REGISTRATION_UNLOCK_ENV, "1")
    await registration_policy.set_stored_mode(
        db_session,
        registration_policy.RegistrationMode.OPEN,
    )


def _identity(subject: str) -> FederatedIdentity:
    return FederatedIdentity(
        issuer=ISSUER,
        subject=subject,
        authenticated_at=datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc),
        acr=None,
        amr=("pwd",),
        email=None,
        email_verified=False,
        preferred_username=subject,
    )


async def _issue(db_session, monkeypatch, kind) -> RegistrationIntent:
    await _open(db_session, monkeypatch)
    return await admission.issue_intent(db_session, account_kind=kind)


async def _decide(db_session, *, subject: str, intent_id):
    return await federation.decide_federated_login(
        db_session,
        identity=_identity(subject),
        bootstrap_subject="",
        invitation_id=None,
        registration_intent_id=intent_id,
        step_up=False,
    )


async def test_open_registration_requires_a_choice_for_an_unknown_identity(
    db_session, legacy_owner_roots, monkeypatch
):
    await _open(db_session, monkeypatch)

    with pytest.raises(federation.RegistrationChoiceRequired):
        await _decide(
            db_session,
            subject="provider-only-without-choice",
            intent_id=None,
        )

    assert await db_session.scalar(
        select(func.count())
        .select_from(UserFederatedIdentity)
        .where(UserFederatedIdentity.subject == "provider-only-without-choice")
    ) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.normalized_username == "provider-only-without-choice")
    ) == 0


async def test_unknown_step_up_stays_an_ordinary_login_refusal(
    db_session, legacy_owner_roots, monkeypatch
):
    await _open(db_session, monkeypatch)

    with pytest.raises(federation.UnknownFederatedIdentity):
        await federation.decide_federated_login(
            db_session,
            identity=_identity("unknown-step-up"),
            bootstrap_subject="",
            invitation_id=None,
            registration_intent_id=None,
            step_up=True,
        )


async def test_member_intent_creates_one_member_with_a_health_subject(
    db_session, legacy_owner_roots, monkeypatch
):
    monkeypatch.setenv("VITALS_TIMEZONE", "Asia/Almaty")
    intent = await _issue(
        db_session,
        monkeypatch,
        RegistrationAccountKind.MEMBER,
    )

    decision = await _decide(
        db_session,
        subject="intent-member",
        intent_id=intent.id,
    )

    assert isinstance(decision, federation.FederatedSessionDecision)
    assert decision.subject_id is not None
    assert intent.status == RegistrationIntentStatus.CONSUMED.value
    roles = list(
        await db_session.scalars(
            select(UserRole.role).where(UserRole.user_id == decision.user_id)
        )
    )
    assert roles == [UserRoleName.MEMBER.value]
    subjects = list(
        await db_session.scalars(
            select(HealthSubject).where(
                HealthSubject.owner_user_id == decision.user_id
            )
        )
    )
    assert [subject.id for subject in subjects] == [decision.subject_id]
    assert subjects[0].timezone == "Asia/Almaty"


@pytest.mark.parametrize(
    ("kind", "expected_role"),
    [
        (RegistrationAccountKind.DOCTOR, UserRoleName.DOCTOR),
        (RegistrationAccountKind.TRAINER, UserRoleName.TRAINER),
    ],
)
async def test_professional_intent_creates_exactly_one_role_and_no_health_subject(
    db_session,
    legacy_owner_roots,
    monkeypatch,
    kind,
    expected_role,
):
    intent = await _issue(db_session, monkeypatch, kind)

    decision = await _decide(
        db_session,
        subject=f"intent-{kind.value}",
        intent_id=intent.id,
    )

    assert isinstance(decision, federation.FederatedSessionDecision)
    assert decision.subject_id is None
    assert intent.status == RegistrationIntentStatus.CONSUMED.value
    roles = list(
        await db_session.scalars(
            select(UserRole.role).where(UserRole.user_id == decision.user_id)
        )
    )
    assert roles == [expected_role.value]
    assert await db_session.scalar(
        select(func.count())
        .select_from(HealthSubject)
        .where(HealthSubject.owner_user_id == decision.user_id)
    ) == 0


async def test_consumed_intent_cannot_create_a_second_identity(
    db_session, legacy_owner_roots, monkeypatch
):
    intent = await _issue(
        db_session,
        monkeypatch,
        RegistrationAccountKind.MEMBER,
    )
    await _decide(
        db_session,
        subject="intent-replay-winner",
        intent_id=intent.id,
    )
    await db_session.commit()

    with pytest.raises(admission.AdmissionRefused):
        await _decide(
            db_session,
            subject="intent-replay-loser",
            intent_id=intent.id,
        )

    assert await db_session.scalar(
        select(func.count())
        .select_from(UserFederatedIdentity)
        .where(
            UserFederatedIdentity.issuer == ISSUER,
            UserFederatedIdentity.subject == "intent-replay-loser",
        )
    ) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.normalized_username == "intent-replay-loser")
    ) == 0


async def test_expired_intent_fails_before_identity_creation(
    db_session, legacy_owner_roots, monkeypatch
):
    intent = await _issue(
        db_session,
        monkeypatch,
        RegistrationAccountKind.DOCTOR,
    )
    intent.expires_at = intent.created_at + timedelta(seconds=1)
    database_time = intent.expires_at + timedelta(microseconds=1)
    await db_session.flush()
    monkeypatch.setattr(
        intent_service,
        "database_now",
        AsyncMock(return_value=database_time),
    )

    with pytest.raises(admission.AdmissionRefused):
        await _decide(
            db_session,
            subject="intent-expired",
            intent_id=intent.id,
        )

    assert intent.status == RegistrationIntentStatus.EXPIRED.value
    assert await db_session.scalar(
        select(func.count())
        .select_from(UserFederatedIdentity)
        .where(UserFederatedIdentity.subject == "intent-expired")
    ) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.normalized_username == "intent-expired")
    ) == 0


async def test_closing_registration_invalidates_a_still_pending_intent(
    db_session, legacy_owner_roots, monkeypatch
):
    intent = await _issue(
        db_session,
        monkeypatch,
        RegistrationAccountKind.TRAINER,
    )
    await db_session.commit()
    await registration_policy.set_stored_mode(
        db_session,
        registration_policy.RegistrationMode.DISABLED,
    )
    await db_session.commit()

    with pytest.raises(admission.AdmissionRefused):
        await _decide(
            db_session,
            subject="intent-after-close",
            intent_id=intent.id,
        )

    persisted = await db_session.get(RegistrationIntent, intent.id)
    assert persisted is not None
    assert persisted.status == RegistrationIntentStatus.PENDING.value
    assert await db_session.scalar(
        select(func.count())
        .select_from(UserFederatedIdentity)
        .where(UserFederatedIdentity.subject == "intent-after-close")
    ) == 0


async def test_existing_link_ignores_intent_without_role_escalation_or_consumption(
    db_session, legacy_owner_roots, monkeypatch
):
    linked_subject = "already-linked-member"
    db_session.add(
        UserFederatedIdentity(
            user_id=legacy_owner_roots.user_id,
            issuer=ISSUER,
            subject=linked_subject,
        )
    )
    await db_session.flush()
    roles_before = set(
        await db_session.scalars(
            select(UserRole.role).where(
                UserRole.user_id == legacy_owner_roots.user_id
            )
        )
    )
    intent = await _issue(
        db_session,
        monkeypatch,
        RegistrationAccountKind.DOCTOR,
    )

    decision = await _decide(
        db_session,
        subject=linked_subject,
        intent_id=intent.id,
    )

    roles_after = set(
        await db_session.scalars(
            select(UserRole.role).where(
                UserRole.user_id == legacy_owner_roots.user_id
            )
        )
    )
    assert isinstance(decision, federation.FederatedSessionDecision)
    assert decision.user_id == legacy_owner_roots.user_id
    assert roles_after == roles_before
    assert UserRoleName.DOCTOR.value not in roles_after
    assert intent.status == RegistrationIntentStatus.PENDING.value
    assert intent.consumed_at is None
