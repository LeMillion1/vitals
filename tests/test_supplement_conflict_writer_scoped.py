"""Scoped conflict-writer contract for safe Supplement create/activation paths."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import func, select, update

from vitals.enums import Domain, RuleType, Severity
from vitals.models.conflict_rule import ConflictRule
from vitals.models.supplements import Supplement
from vitals.models.system_alert import SystemAlert
from vitals.ownership import WriteIdentity
from vitals.services import conflict_engine, supplements_service


EVALUATION_DATE = date(2026, 8, 20)


async def _seed_blocking_rule(db_session, subject_id: uuid.UUID) -> ConflictRule:
    rule = ConflictRule(
        subject_id=subject_id,
        rule_type=RuleType.HARD_BLOCK.value,
        domain_a=Domain.LABS.value,
        condition_a={"marker": "synthetic-risk"},
        domain_b=Domain.SUPPLEMENTS.value,
        condition_b={"key": "iron", "active": True},
        severity=Severity.BLOCK.value,
        message="Synthetic scoped supplement conflict.",
        active=True,
    )
    db_session.add(rule)
    await db_session.commit()
    return rule


def _register_resolvers() -> None:
    async def labs(session, *, scope):
        del session, scope
        return [{"marker": "synthetic-risk"}]

    conflict_engine.register_domain_resolver(Domain.LABS.value, labs)
    conflict_engine.register_domain_resolver(
        Domain.SUPPLEMENTS.value,
        supplements_service.resolve_active_scoped,
    )


def _context(legacy_owner_roots) -> conflict_engine.ConflictWriteContext:
    return conflict_engine.ConflictWriteContext(
        identity=WriteIdentity(
            legacy_owner_roots.subject_id,
            legacy_owner_roots.user_id,
        ),
        evaluation_date=EVALUATION_DATE,
    )


async def test_scoped_create_block_is_write_free(db_session, legacy_owner_roots):
    await _seed_blocking_rule(db_session, legacy_owner_roots.subject_id)
    _register_resolvers()
    context = _context(legacy_owner_roots)
    prepared = await conflict_engine.prepare_scoped_write(
        db_session,
        context=context,
    )

    with pytest.raises(conflict_engine.ConflictBlocked):
        await supplements_service.add_supplement(
            db_session,
            name="Iron",
            key="iron",
            identity=context.identity,
            prepared_conflict_write=prepared,
        )

    assert await db_session.scalar(
        select(func.count()).select_from(Supplement)
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == 0


async def test_scoped_create_override_stamps_row_and_alert(
    db_session,
    legacy_owner_roots,
):
    rule = await _seed_blocking_rule(db_session, legacy_owner_roots.subject_id)
    _register_resolvers()
    context = _context(legacy_owner_roots)
    prepared = await conflict_engine.prepare_scoped_write(
        db_session,
        context=context,
    )

    row = await supplements_service.add_supplement(
        db_session,
        name="Iron",
        key="iron",
        override=True,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )

    assert (row.subject_id, row.actor_user_id) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    alert = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.alert_key == f"conflict:{rule.id}"
        )
    )
    assert alert is not None
    assert (alert.subject_id, alert.integration_connection_id) == (
        legacy_owner_roots.subject_id,
        None,
    )
    assert alert.overridden_by_user_id == legacy_owner_roots.user_id
    assert alert.override_at is not None


async def test_scoped_activation_blocks_then_overrides_without_losing_identity(
    db_session,
    legacy_owner_roots,
):
    rule = await _seed_blocking_rule(db_session, legacy_owner_roots.subject_id)
    _register_resolvers()
    context = _context(legacy_owner_roots)
    prepared = await conflict_engine.prepare_scoped_write(
        db_session,
        context=context,
    )
    row = await supplements_service.add_supplement(
        db_session,
        name="Iron",
        key="iron",
        active=False,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )
    await db_session.commit()

    blocked = await conflict_engine.prepare_scoped_write(
        db_session,
        context=context,
    )
    with pytest.raises(conflict_engine.ConflictBlocked):
        await supplements_service.set_active(
            db_session,
            row.id,
            True,
            identity=context.identity,
            prepared_conflict_write=blocked,
        )
    assert row.active is False
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == 0

    overridden = await conflict_engine.prepare_scoped_write(
        db_session,
        context=context,
    )
    result = await supplements_service.set_active(
        db_session,
        row.id,
        True,
        override=True,
        identity=context.identity,
        prepared_conflict_write=overridden,
    )
    assert result is row and row.active is True
    alert = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.alert_key == f"conflict:{rule.id}"
        )
    )
    assert alert is not None
    assert alert.overridden_by_user_id == legacy_owner_roots.user_id


async def test_prepared_identity_mismatch_is_rejected_before_create(
    db_session,
    legacy_owner_roots,
):
    _register_resolvers()
    context = _context(legacy_owner_roots)
    prepared = await conflict_engine.prepare_scoped_write(
        db_session,
        context=context,
    )
    mismatched = WriteIdentity(context.identity.subject_id, uuid.uuid4())

    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await supplements_service.add_supplement(
            db_session,
            name="Magnesium",
            key="magnesium",
            identity=mismatched,
            prepared_conflict_write=prepared,
        )
    assert await db_session.scalar(
        select(func.count()).select_from(Supplement)
    ) == 0


async def test_scoped_identity_without_prepared_capability_is_rejected(
    db_session,
    legacy_owner_roots,
):
    identity = _context(legacy_owner_roots).identity
    row = Supplement(
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        domain=Domain.SUPPLEMENTS.value,
        source="manual",
        name="Existing",
        key="existing",
        active=True,
    )
    db_session.add(row)
    await db_session.flush()

    # A subject without its conflict decision is not a call the service can
    # even be asked to make any more.
    with pytest.raises(TypeError):
        await supplements_service.add_supplement(
            db_session,
            name="Unprepared",
            identity=identity,
        )
    with pytest.raises(TypeError):
        await supplements_service.set_active(
            db_session,
            row.id,
            False,
            identity=identity,
        )
    assert row.active is True


async def test_deactivation_rejects_mismatched_or_committed_capability(
    db_session,
    legacy_owner_roots,
):
    context = _context(legacy_owner_roots)
    row = Supplement(
        subject_id=context.identity.subject_id,
        actor_user_id=context.identity.actor_user_id,
        domain=Domain.SUPPLEMENTS.value,
        source="manual",
        name="Existing",
        key="existing",
        active=True,
    )
    db_session.add(row)
    await db_session.commit()

    mismatched_prepared = await conflict_engine.prepare_scoped_write(
        db_session,
        context=context,
    )
    mismatched = WriteIdentity(context.identity.subject_id, uuid.uuid4())
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await supplements_service.set_active(
            db_session,
            row.id,
            False,
            identity=mismatched,
            prepared_conflict_write=mismatched_prepared,
        )
    assert row.active is True

    await db_session.commit()
    with pytest.raises(conflict_engine.ConflictPreparedWriteError):
        await supplements_service.set_active(
            db_session,
            row.id,
            False,
            identity=context.identity,
            prepared_conflict_write=mismatched_prepared,
        )
    assert row.active is True




async def test_activation_refreshes_locked_row_before_conflict_evaluation(
    db_session,
    legacy_owner_roots,
):
    rule = await _seed_blocking_rule(db_session, legacy_owner_roots.subject_id)
    del rule
    _register_resolvers()
    context = _context(legacy_owner_roots)
    row = Supplement(
        subject_id=context.identity.subject_id,
        actor_user_id=context.identity.actor_user_id,
        domain=Domain.SUPPLEMENTS.value,
        source="manual",
        name="Stale cached name",
        key="not_iron",
        active=False,
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.execute(
        update(Supplement)
        .where(Supplement.id == row.id)
        .values(key="iron"),
        execution_options={"synchronize_session": False},
    )
    assert row.key == "not_iron"
    prepared = await conflict_engine.prepare_scoped_write(
        db_session,
        context=context,
    )

    with pytest.raises(conflict_engine.ConflictBlocked):
        await supplements_service.set_active(
            db_session,
            row.id,
            True,
            identity=context.identity,
            prepared_conflict_write=prepared,
        )
    assert row.key == "iron"
    assert row.active is False


@pytest.mark.parametrize(
    ("new_key", "new_active"),
    (("magnesium", True), ("iron", False)),
)
async def test_scoped_update_replaces_old_resolver_entity_without_false_block(
    db_session,
    legacy_owner_roots,
    new_key,
    new_active,
):
    await _seed_blocking_rule(db_session, legacy_owner_roots.subject_id)
    _register_resolvers()
    context = _context(legacy_owner_roots)
    row = Supplement(
        subject_id=context.identity.subject_id,
        actor_user_id=context.identity.actor_user_id,
        domain=Domain.SUPPLEMENTS.value,
        source="manual",
        name="Iron",
        key="iron",
        active=True,
    )
    db_session.add(row)
    await db_session.commit()
    prepared = await conflict_engine.prepare_scoped_write(
        db_session,
        context=context,
    )

    result = await supplements_service.update_supplement(
        db_session,
        row.id,
        name="Updated",
        key=new_key,
        active=new_active,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )

    assert result is row
    assert (row.key, row.active) == (new_key, new_active)
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == 0


async def test_scoped_update_to_conflicting_state_is_blocked_write_free(
    db_session,
    legacy_owner_roots,
):
    await _seed_blocking_rule(db_session, legacy_owner_roots.subject_id)
    _register_resolvers()
    context = _context(legacy_owner_roots)
    row = Supplement(
        subject_id=context.identity.subject_id,
        actor_user_id=context.identity.actor_user_id,
        domain=Domain.SUPPLEMENTS.value,
        source="manual",
        name="Magnesium",
        key="magnesium",
        active=True,
    )
    db_session.add(row)
    await db_session.commit()
    prepared = await conflict_engine.prepare_scoped_write(
        db_session,
        context=context,
    )

    with pytest.raises(conflict_engine.ConflictBlocked):
        await supplements_service.update_supplement(
            db_session,
            row.id,
            name="Iron",
            key="iron",
            active=True,
            identity=context.identity,
            prepared_conflict_write=prepared,
        )
    assert (row.name, row.key, row.active) == (
        "Magnesium",
        "magnesium",
        True,
    )
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == 0

    overridden = await supplements_service.update_supplement(
        db_session,
        row.id,
        name="Iron",
        key="iron",
        active=True,
        override=True,
        identity=context.identity,
        prepared_conflict_write=prepared,
    )
    assert overridden is row
    alert = await db_session.scalar(select(SystemAlert))
    assert alert is not None
    assert (alert.subject_id, alert.integration_connection_id) == (
        legacy_owner_roots.subject_id,
        None,
    )
    assert alert.entity_ref == "supplement:iron"
    assert alert.overridden_by_user_id == legacy_owner_roots.user_id


async def test_web_create_uses_scoped_block_and_human_override(
    auth_client,
    db_session,
    legacy_owner_roots,
):
    rule = await _seed_blocking_rule(db_session, legacy_owner_roots.subject_id)
    _register_resolvers()

    blocked = await auth_client.post(
        "/supplements/save",
        data={"name": "Iron", "active": "true"},
    )
    assert blocked.status_code == 409
    assert await db_session.scalar(
        select(func.count()).select_from(Supplement)
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == 0

    overridden = await auth_client.post(
        "/supplements/save",
        data={
            "name": "Iron",
            "active": "true",
            "override": "true",
        },
    )
    assert overridden.status_code == 303
    row = await db_session.scalar(select(Supplement))
    assert row is not None
    assert (row.subject_id, row.actor_user_id) == (
        legacy_owner_roots.subject_id,
        legacy_owner_roots.user_id,
    )
    alert = await db_session.scalar(
        select(SystemAlert).where(
            SystemAlert.alert_key == f"conflict:{rule.id}"
        )
    )
    assert alert is not None
    assert alert.overridden_by_user_id == legacy_owner_roots.user_id


async def test_mcp_create_uses_scoped_block_payload(
    db_session,
    session_factory,
    legacy_owner_roots,
    monkeypatch,
):
    from vitals.services import modules_service
    from web.routers import mcp as mcp_router

    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    await modules_service.set_module_enabled(
        db_session,
        key="supplements",
        enabled=True,
        subject_id=legacy_owner_roots.subject_id,
    )
    await _seed_blocking_rule(db_session, legacy_owner_roots.subject_id)
    _register_resolvers()

    result = await mcp_router.add_supplement(
        name="Iron",
        key="iron",
        active=True,
    )
    assert result["blocked"] is True
    assert result["violations"][0]["severity"] == Severity.BLOCK.value
    assert await db_session.scalar(
        select(func.count()).select_from(Supplement)
    ) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(SystemAlert)
    ) == 0


async def test_internal_replacement_marker_is_not_custom_rule_input(
    db_session,
    legacy_owner_roots,
):
    context = _context(legacy_owner_roots)
    row = Supplement(
        subject_id=context.identity.subject_id,
        actor_user_id=context.identity.actor_user_id,
        domain=Domain.SUPPLEMENTS.value,
        source="manual",
        name="Marker probe",
        key="marker_probe",
        active=True,
    )
    db_session.add(row)
    await db_session.flush()
    db_session.add(
        ConflictRule(
            subject_id=context.identity.subject_id,
            rule_type=RuleType.HARD_BLOCK.value,
            domain_a=Domain.LABS.value,
            condition_a={"marker": "synthetic-risk"},
            domain_b=Domain.SUPPLEMENTS.value,
            condition_b={
                conflict_engine.CONFLICT_ENTITY_KEY: str(row.id),
            },
            severity=Severity.BLOCK.value,
            message="Internal resolver identity must stay private.",
            active=True,
        )
    )
    await db_session.commit()
    _register_resolvers()

    assert await conflict_engine.evaluate_scoped(
        db_session,
        scope=context.scope,
        domain=Domain.LABS,
        proposed_state={"marker": "synthetic-risk"},
    ) == []
