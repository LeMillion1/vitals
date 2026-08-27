"""Subject-aware conflict writer and alert-attribution contract."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import Domain, RuleType, Severity, UserStatus
from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject, User
from vitals.models.system_alert import SystemAlert
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine


EVALUATION_DATE = date(2026, 8, 19)


def _context(
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    *,
    bridge: engine.LegacyConflictBridge = (
        engine.LegacyConflictBridge.REJECT
    ),
) -> engine.ConflictWriteContext:
    return engine.ConflictWriteContext(
        identity=WriteIdentity(subject_id, actor_user_id),
        evaluation_date=EVALUATION_DATE,
        legacy_bridge=bridge,
    )


async def _add_subject(
    session: AsyncSession,
    label: str,
) -> tuple[User, HealthSubject]:
    slug = f"{label}-{uuid.uuid4().hex}"
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=label,
        timezone="UTC",
    )
    session.add(subject)
    await session.flush()
    return user, subject


async def _rule(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None,
    severity: Severity = Severity.WARN,
    message: str = "scoped conflict",
    day_end: bool = False,
) -> ConflictRule:
    row = ConflictRule(
        subject_id=subject_id,
        rule_type=(
            RuleType.HARD_BLOCK.value
            if severity is Severity.BLOCK
            else RuleType.SOFT_WARN.value
        ),
        domain_a=Domain.WEIGHT.value,
        condition_a={"candidate": True},
        domain_b=Domain.LABS.value,
        condition_b={"present": True},
        severity=severity.value,
        message=message,
        params={"day_end_only": True} if day_end else None,
        active=True,
    )
    session.add(row)
    await session.flush()
    return row


def _register_matching_pair(*, matching_subject_id: uuid.UUID | None = None) -> None:
    async def weight(session, *, scope):
        del session, scope
        return []

    async def labs(session, *, scope):
        del session
        if matching_subject_id is not None and scope.subject_id != matching_subject_id:
            return []
        return [{"present": True}]

    engine.register_domain_resolver(Domain.WEIGHT.value, weight)
    engine.register_domain_resolver(Domain.LABS.value, labs)


async def test_writer_keeps_rules_and_alerts_in_exact_subject(
    db_session,
    legacy_owner_roots,
):
    user_b, subject_b = await _add_subject(db_session, "subject-b")
    rule_b = await _rule(db_session, subject_id=subject_b.id)
    await db_session.commit()
    _register_matching_pair(matching_subject_id=subject_b.id)

    violations_a = await engine.enforce_scoped(
        db_session,
        context=_context(
            legacy_owner_roots.subject_id,
            legacy_owner_roots.user_id,
        ),
        domain=Domain.WEIGHT,
        proposed_state={"candidate": True},
    )
    assert violations_a == []
    assert list(await db_session.scalars(select(SystemAlert))) == []

    violations_b = await engine.enforce_scoped(
        db_session,
        context=_context(subject_b.id, user_b.id),
        domain=Domain.WEIGHT,
        proposed_state={"candidate": True},
    )
    assert [violation.rule_id for violation in violations_b] == [rule_b.id]
    alert = await db_session.scalar(select(SystemAlert))
    assert alert is not None
    assert (alert.subject_id, alert.integration_connection_id) == (subject_b.id, None)


async def test_legacy_write_context_proves_owner_or_system_under_exact_one(
    db_session,
    legacy_owner_roots,
):
    human = await engine.resolve_legacy_conflict_write_context(
        db_session,
        actor_username="tester",
        evaluation_date=EVALUATION_DATE,
    )
    system = await engine.resolve_legacy_conflict_write_context(
        db_session,
        actor_username=None,
        evaluation_date=EVALUATION_DATE,
    )
    assert human.identity == WriteIdentity(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    assert system.identity == WriteIdentity(legacy_owner_roots.subject_id, None)
    assert human.legacy_bridge is engine.LegacyConflictBridge.FULLY_UNOWNED

    # A second person alone does not close it. The bridge widens to rows nobody
    # owns, and with none of those in the database it widens to nothing — the
    # write is an ordinary scoped one, and demanding a sole subject for it was
    # stopping seven pages that were asking nothing of the bridge.
    await _add_subject(db_session, "second")
    await engine.prepare_scoped_write(db_session, context=human)

    # Give it a rule that belongs to nobody and names nothing, and the refusal
    # comes back — that row genuinely cannot say whose state it is about.
    db_session.add(
        ConflictRule(
            subject_id=None,
            code=None,
            rule_type="soft_warn",
            severity="warn",
            domain_a=Domain.WEIGHT.value,
            condition_a={"candidate": True},
            domain_b=Domain.WEIGHT.value,
            condition_b={"candidate": True},
            message="legacy custom rule",
            active=True,
        )
    )
    await db_session.flush()
    with pytest.raises(engine.ConflictLegacyBridgeError):
        await engine.prepare_scoped_write(db_session, context=human)


async def test_prepare_rejects_missing_and_inactive_actors(
    db_session,
    legacy_owner_roots,
):
    missing = _context(legacy_owner_roots.subject_id, uuid.uuid4())
    with pytest.raises(engine.ConflictActorNotFound):
        await engine.prepare_scoped_write(db_session, context=missing)

    owner = await db_session.get(User, legacy_owner_roots.user_id)
    assert owner is not None
    owner.status = UserStatus.SUSPENDED.value
    await db_session.flush()
    inactive = _context(legacy_owner_roots.subject_id, owner.id)
    with pytest.raises(engine.ConflictActorInactive):
        await engine.prepare_scoped_write(db_session, context=inactive)


async def test_legacy_bridge_rejects_an_active_non_owner_actor(
    db_session,
    legacy_owner_roots,
):
    outsider = User(
        username="active-outsider",
        normalized_username="active-outsider",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(outsider)
    await db_session.flush()
    context = _context(
        legacy_owner_roots.subject_id,
        outsider.id,
        bridge=engine.LegacyConflictBridge.FULLY_UNOWNED,
    )
    with pytest.raises(engine.ConflictActorOwnershipError):
        await engine.prepare_scoped_write(db_session, context=context)


async def test_blocking_write_mutates_zero_alerts(
    db_session,
    legacy_owner_roots,
):
    passive = await _rule(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        message="passive sibling",
    )
    blocking = await _rule(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        severity=Severity.BLOCK,
        message="blocking sibling",
    )
    await db_session.commit()
    _register_matching_pair()

    with pytest.raises(engine.ConflictBlocked) as exc_info:
        await engine.enforce_scoped(
            db_session,
            context=_context(
                legacy_owner_roots.subject_id,
                legacy_owner_roots.user_id,
            ),
            domain=Domain.WEIGHT,
            proposed_state={"candidate": True},
        )
    assert [v.rule_id for v in exc_info.value.violations] == [passive.id, blocking.id]
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 0


async def test_human_override_stamps_actor_on_exact_health_alert(
    db_session,
    legacy_owner_roots,
):
    rule = await _rule(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        severity=Severity.BLOCK,
    )
    await db_session.commit()
    _register_matching_pair()

    await engine.enforce_scoped(
        db_session,
        context=_context(
            legacy_owner_roots.subject_id,
            legacy_owner_roots.user_id,
        ),
        domain=Domain.WEIGHT,
        proposed_state={"candidate": True},
        override=True,
        entity_ref="weight:1",
    )
    alert = await db_session.scalar(
        select(SystemAlert).where(SystemAlert.alert_key == f"conflict:{rule.id}")
    )
    assert alert is not None
    assert (alert.subject_id, alert.integration_connection_id) == (
        legacy_owner_roots.subject_id,
        None,
    )
    assert alert.override_at is not None
    assert alert.overridden_by_user_id == legacy_owner_roots.user_id


async def test_system_override_is_rejected_before_alert_mutation(
    db_session,
    legacy_owner_roots,
):
    await _rule(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        severity=Severity.BLOCK,
    )
    await db_session.commit()
    _register_matching_pair()

    with pytest.raises(engine.ConflictOverrideActorRequired):
        await engine.enforce_scoped(
            db_session,
            context=_context(legacy_owner_roots.subject_id, None),
            domain=Domain.WEIGHT,
            proposed_state={"candidate": True},
            override=True,
        )
    assert await db_session.scalar(select(func.count()).select_from(SystemAlert)) == 0


async def test_passive_system_write_is_actorless_and_rule_order_is_stable(
    db_session,
    legacy_owner_roots,
):
    first = await _rule(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        message="first",
    )
    second = await _rule(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        message="second",
    )
    await db_session.commit()
    _register_matching_pair()

    violations = await engine.enforce_scoped(
        db_session,
        context=_context(legacy_owner_roots.subject_id, None),
        domain=Domain.WEIGHT,
        proposed_state={"candidate": True},
    )
    assert [violation.rule_id for violation in violations] == [first.id, second.id]
    alerts = list(await db_session.scalars(select(SystemAlert).order_by(SystemAlert.id)))
    assert [alert.alert_key for alert in alerts] == [
        f"conflict:{first.id}",
        f"conflict:{second.id}",
    ]
    assert all(
        alert.subject_id == legacy_owner_roots.subject_id
        and alert.integration_connection_id is None
        and alert.override_at is None
        and alert.overridden_by_user_id is None
        for alert in alerts
    )


async def test_day_end_scoped_raises_then_clears_only_subject_alert(
    db_session,
    legacy_owner_roots,
):
    rule = await _rule(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        day_end=True,
    )
    await db_session.commit()
    state = {"fires": True}

    async def weight(session, *, scope):
        del session, scope
        return [{"candidate": True}]

    async def labs(session, *, scope):
        del session, scope
        return [{"present": True}] if state["fires"] else []

    engine.register_domain_resolver(Domain.WEIGHT.value, weight)
    engine.register_domain_resolver(Domain.LABS.value, labs)
    context = _context(legacy_owner_roots.subject_id, None)

    await engine.reconcile_day_end_scoped(
        db_session,
        context=context,
        domain=Domain.WEIGHT,
        entity_ref="2026-08-19",
    )
    alert = await db_session.scalar(
        select(SystemAlert).where(SystemAlert.alert_key == f"conflict:{rule.id}")
    )
    assert alert is not None and alert.resolved_at is None
    assert (alert.subject_id, alert.integration_connection_id) == (
        legacy_owner_roots.subject_id,
        None,
    )

    state["fires"] = False
    await engine.reconcile_day_end_scoped(
        db_session,
        context=context,
        domain=Domain.WEIGHT,
        entity_ref="2026-08-20",
    )
    await db_session.refresh(alert)
    assert alert.resolved_at is not None
    assert alert.resolved_by_user_id is None


async def test_legacy_alert_adoption_rolls_back_without_hidden_commit(
    db_session,
    legacy_owner_roots,
):
    rule = await _rule(db_session, subject_id=None, message="new message")
    legacy = SystemAlert(
        domain=Domain.WEIGHT.value,
        severity=Severity.INFO.value,
        message="old message",
        alert_key=f"conflict:{rule.id}",
        entity_ref="legacy",
    )
    db_session.add(legacy)
    await db_session.commit()
    legacy_id = legacy.id
    _register_matching_pair()
    context = await engine.resolve_legacy_conflict_write_context(
        db_session,
        actor_username=None,
        evaluation_date=EVALUATION_DATE,
    )

    await engine.enforce_scoped(
        db_session,
        context=context,
        domain=Domain.WEIGHT,
        proposed_state={"candidate": True},
        entity_ref="legacy",
    )
    assert legacy.subject_id == legacy_owner_roots.subject_id
    assert legacy.message == "new message"
    await db_session.rollback()

    restored = await db_session.get(SystemAlert, legacy_id)
    assert restored is not None
    assert restored.subject_id is None
    assert restored.integration_connection_id is None
    assert restored.message == "old message"


async def test_prepared_write_cannot_cross_a_transaction_boundary(
    db_session,
    legacy_owner_roots,
):
    prepared = await engine.prepare_scoped_write(
        db_session,
        context=_context(legacy_owner_roots.subject_id, None),
    )
    await db_session.commit()
    with pytest.raises(engine.ConflictPreparedWriteError):
        await engine.enforce_prepared(
            db_session,
            prepared=prepared,
            domain=Domain.WEIGHT,
            proposed_state={"candidate": True},
        )


async def test_prepared_write_cannot_be_constructed_or_context_replaced(
    db_session,
    legacy_owner_roots,
):
    context = _context(legacy_owner_roots.subject_id, None)
    prepared = await engine.prepare_scoped_write(
        db_session,
        context=context,
    )
    forged_context = _context(
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )

    with pytest.raises(engine.ConflictPreparedWriteError):
        engine.PreparedConflictWrite()
    with pytest.raises(TypeError):
        replace(prepared, context=forged_context)


async def test_prepared_write_cannot_escape_its_savepoint(
    db_session,
    legacy_owner_roots,
):
    context = _context(legacy_owner_roots.subject_id, None)
    async with db_session.begin_nested():
        prepared = await engine.prepare_scoped_write(
            db_session,
            context=context,
        )

    with pytest.raises(engine.ConflictPreparedWriteError):
        await engine.enforce_prepared(
            db_session,
            prepared=prepared,
            domain=Domain.WEIGHT,
            proposed_state={"candidate": True},
        )


@pytest.mark.integration
async def test_postgres_same_conflict_key_converges_under_concurrency(
    db_session,
    legacy_owner_roots,
):
    rule = await _rule(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
    )
    await db_session.commit()
    _register_matching_pair()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    context = _context(legacy_owner_roots.subject_id, None)

    async def run_once() -> None:
        async with factory() as session:
            await engine.enforce_scoped(
                session,
                context=context,
                domain=Domain.WEIGHT,
                proposed_state={"candidate": True},
                entity_ref="same",
            )
            await session.commit()

    await asyncio.gather(run_once(), run_once())
    async with factory() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(SystemAlert)
            .where(SystemAlert.alert_key == f"conflict:{rule.id}")
        ) == 1
