"""Scoped ownership contract for the OpenRouter signal-parser alert."""

from __future__ import annotations

import inspect
import uuid

import pytest
from sqlalchemy import select

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Severity,
    UserStatus,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import alerts_service, signals_service
from vitals.services.proactive import brief, inbound
from vitals.utils.timeutils import now_local
from web.routers import telegram


async def _context(session, subject_id) -> alerts_service.ProviderAlertContext:
    connection = await session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.subject_id == subject_id,
            IntegrationConnection.provider == IntegrationProvider.OPENROUTER.value,
        )
    )
    assert connection is not None
    return alerts_service.ProviderAlertContext(
        identity=WriteIdentity(subject_id=subject_id, actor_user_id=None),
        provider=IntegrationProvider.OPENROUTER,
        integration_connection_id=connection.id,
    )


def _legacy_alert(
    *,
    subject_id=None,
    connection_id=None,
    key=None,
    domain=Domain.SIGNALS,
) -> SystemAlert:
    return SystemAlert(
        subject_id=subject_id,
        integration_connection_id=connection_id,
        domain=domain.value,
        severity=Severity.WARN.value,
        message="synthetic parser failure",
        alert_key=key or signals_service.PARSER_FAILED_ALERT_KEY,
    )


async def test_failure_adopts_only_the_exact_fully_unowned_legacy_alert(
    db_session,
    legacy_owner_roots,
):
    context = await _context(db_session, legacy_owner_roots.subject_id)
    legacy = _legacy_alert()
    wrong_key = _legacy_alert(key="brief_empty_day", domain=Domain.SYSTEM)
    db_session.add_all([legacy, wrong_key])
    await db_session.commit()

    outcome = signals_service.ParserOutcome(failures=1)
    await inbound._reconcile_parser_alert_best_effort(
        db_session,
        context=context,
        outcome=outcome,
    )

    await db_session.refresh(legacy)
    await db_session.refresh(wrong_key)
    assert (legacy.subject_id, legacy.integration_connection_id) == (
        context.identity.subject_id,
        context.integration_connection_id,
    )
    assert legacy.resolved_by_user_id is None
    assert (wrong_key.subject_id, wrong_key.integration_connection_id) == (None, None)


@pytest.mark.parametrize("partial_column", ["subject", "connection"])
async def test_partial_legacy_alert_is_rejected_without_rewriting_it(
    db_session,
    legacy_owner_roots,
    partial_column,
    caplog,
):
    context = await _context(db_session, legacy_owner_roots.subject_id)
    partial = _legacy_alert(
        subject_id=(
            context.identity.subject_id if partial_column == "subject" else None
        ),
        connection_id=(
            context.integration_connection_id
            if partial_column == "connection"
            else None
        ),
    )
    db_session.add(partial)
    await db_session.commit()

    await inbound._reconcile_parser_alert_best_effort(
        db_session,
        context=context,
        outcome=signals_service.ParserOutcome(failures=1),
    )

    await db_session.refresh(partial)
    expected = (
        (context.identity.subject_id, None)
        if partial_column == "subject"
        else (None, context.integration_connection_id)
    )
    assert (partial.subject_id, partial.integration_connection_id) == expected
    assert partial.resolved_at is None
    assert "could not reconcile the OpenRouter signal-parser alert" in caplog.text


async def test_second_subject_blocks_fully_unowned_adoption(
    db_session,
    legacy_owner_roots,
):
    context = await _context(db_session, legacy_owner_roots.subject_id)
    legacy = _legacy_alert()
    other_owner = User(
        username="other-parser-owner",
        normalized_username="other-parser-owner",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add_all([legacy, other_owner])
    await db_session.flush()
    db_session.add(
        HealthSubject(
            owner_user_id=other_owner.id,
            display_name="Other parser subject",
            timezone="UTC",
        )
    )
    await db_session.commit()

    await inbound._reconcile_parser_alert_best_effort(
        db_session,
        context=context,
        outcome=signals_service.ParserOutcome(failures=1),
    )

    await db_session.refresh(legacy)
    assert (legacy.subject_id, legacy.integration_connection_id) == (None, None)
    assert legacy.resolved_at is None


async def test_success_resolves_historical_provider_alert_actorlessly(
    db_session,
    legacy_owner_roots,
):
    context = await _context(db_session, legacy_owner_roots.subject_id)
    alert = await alerts_service.raise_scoped_alert(
        db_session,
        context=context,
        domain=Domain.SIGNALS,
        severity=Severity.WARN,
        message="synthetic parser failure",
        alert_key=signals_service.PARSER_FAILED_ALERT_KEY,
        legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
    )
    await db_session.commit()
    connection = await db_session.get(
        IntegrationConnection, context.integration_connection_id
    )
    assert connection is not None
    connection.status = IntegrationConnectionStatus.RETIRED.value
    connection.retired_at = now_local()
    replacement = IntegrationConnection(
        subject_id=context.identity.subject_id,
        provider=IntegrationProvider.OPENROUTER.value,
        connection_type=IntegrationConnectionType.AI_GATEWAY.value,
        external_account_discriminator="synthetic:replacement-parser",
        status=IntegrationConnectionStatus.ACTIVE.value,
    )
    db_session.add(replacement)
    await db_session.commit()
    replacement_context = alerts_service.ProviderAlertContext(
        identity=context.identity,
        provider=IntegrationProvider.OPENROUTER,
        integration_connection_id=replacement.id,
    )

    await inbound._reconcile_parser_alert_best_effort(
        db_session,
        context=replacement_context,
        outcome=signals_service.ParserOutcome(successes=1),
    )

    await db_session.refresh(alert)
    assert alert.resolved_at is not None
    assert alert.resolved_by_user_id is None
    assert (alert.subject_id, alert.integration_connection_id) == (
        context.identity.subject_id,
        context.integration_connection_id,
    )


@pytest.mark.parametrize(
    ("variant", "message"),
    [
        ("pending", "cannot parse"),
        ("wrong_provider_and_type", "not OpenRouter"),
        ("foreign", "another subject"),
    ],
)
async def test_preparse_rejects_invalid_provider_roots(
    db_session,
    legacy_owner_roots,
    variant,
    message,
):
    context = await _context(db_session, legacy_owner_roots.subject_id)
    if variant == "wrong_provider_and_type":
        connection = await db_session.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.subject_id == legacy_owner_roots.subject_id,
                IntegrationConnection.provider
                == IntegrationProvider.TELEGRAM.value,
            )
        )
        assert connection is not None
        context = alerts_service.ProviderAlertContext(
            identity=context.identity,
            provider=IntegrationProvider.OPENROUTER,
            integration_connection_id=connection.id,
        )
    connection = await db_session.get(
        IntegrationConnection, context.integration_connection_id
    )
    assert connection is not None
    if variant == "pending":
        connection.status = IntegrationConnectionStatus.PENDING.value
    if variant == "foreign":
        owner = User(
            username="foreign-parser-owner",
            normalized_username="foreign-parser-owner",
            password_hash="synthetic-test-hash",
            status=UserStatus.ACTIVE.value,
        )
        db_session.add(owner)
        await db_session.flush()
        subject = HealthSubject(
            owner_user_id=owner.id,
            display_name="Foreign parser subject",
            timezone="UTC",
        )
        db_session.add(subject)
        await db_session.flush()
        connection.subject_id = subject.id
    await db_session.commit()

    with pytest.raises(inbound.InboundOwnershipError, match=message):
        await inbound._validate_parser_alert_connection(
            db_session,
            context=context,
            subject_id=context.identity.subject_id,
        )


async def test_preparse_rejects_wrong_connection_type_before_parser():
    subject_id = uuid.uuid4()
    context = alerts_service.ProviderAlertContext(
        identity=WriteIdentity(subject_id=subject_id, actor_user_id=None),
        provider=IntegrationProvider.OPENROUTER,
        integration_connection_id=uuid.uuid4(),
    )

    class _Result:
        def one_or_none(self):
            return (
                subject_id,
                IntegrationProvider.OPENROUTER.value,
                IntegrationConnectionType.ACCOUNT.value,
                IntegrationConnectionStatus.ACTIVE.value,
            )

    class _Session:
        async def execute(self, _statement):
            return _Result()

    with pytest.raises(inbound.InboundOwnershipError, match="AI gateway"):
        await inbound._validate_parser_alert_connection(
            _Session(),  # type: ignore[arg-type]
            context=context,
            subject_id=subject_id,
        )


def test_parser_outcome_api_and_no_legacy_alert_calls_are_static_contracts():
    assert "parser_outcome" in inspect.signature(signals_service.ingest_text).parameters
    assert "parser_outcome" in inspect.signature(
        signals_service.ingest_stored_text
    ).parameters
    assert "parser_outcome" in inspect.signature(
        signals_service.reparse_unparsed
    ).parameters
    assert "parser_alert_context" in inspect.signature(inbound.handle_update).parameters
    assert "parser_alert_context" in inspect.signature(inbound.handle_text).parameters
    assert "parser_alert_context" in inspect.signature(inbound.reparse_pending).parameters

    paths = (
        inspect.getsource(signals_service),
        inspect.getsource(inbound),
        inspect.getsource(brief),
        inspect.getsource(telegram),
    )
    for source in paths:
        assert "alerts_service.raise_alert(" not in source
        assert "alerts_service.resolve_by_key(" not in source
