"""Effective conflict-rule activation across loader and evaluator surfaces."""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select

from vitals.enums import Domain, UserStatus
from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject, User
from vitals.services.conflicts import activation, catalog, engine, registrations


async def test_disabled_subject_rule_is_absent_from_loader_and_evaluation(
    db_session,
    legacy_owner_roots,
):
    await catalog.sync_catalog(db_session)
    await db_session.commit()
    rule = await db_session.scalar(
        select(ConflictRule).where(
            ConflictRule.code == "derm_retinoid_peel_same_day"
        )
    )
    await activation.set_rule_activation(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        rule_id=rule.id,
        active=False,
        legacy_bridge=engine.LegacyConflictBridge.FULLY_UNOWNED,
    )
    # A stale compatibility mirror must not reactivate the subject's rule.
    rule.active = True
    await db_session.commit()

    engine.clear_domain_resolvers()
    registrations.register_all_resolvers()
    try:
        scope = await engine.resolve_legacy_conflict_scope(
            db_session,
            actor_username=None,
            evaluation_date=date(2026, 8, 19),
        )
        active_rows = await engine.load_scoped_rules(
            db_session,
            scope=scope,
            domain=Domain.SKINCARE,
        )
        all_rows = await engine.load_scoped_rules(
            db_session,
            scope=scope,
            domain=Domain.SKINCARE,
            active_only=False,
        )
        violations = await engine.evaluate_scoped(
            db_session,
            scope=scope,
            domain=Domain.SKINCARE,
            proposed_state=[{"retinoid": True}, {"peel": True}],
        )
    finally:
        engine.clear_domain_resolvers()

    assert rule.id not in {row.id for row in active_rows}
    assert rule.id in {row.id for row in all_rows}
    assert rule.id not in {violation.rule_id for violation in violations}


async def test_curated_activation_is_independent_for_two_subjects(
    db_session,
    legacy_owner_roots,
):
    await catalog.sync_catalog(db_session)
    second_user = User(
        id=uuid.uuid4(),
        username="activation-second",
        normalized_username="activation-second",
        password_hash="$2b$04$abcdefghijklmnopqrstuuuuuuuuuuuuuuuuuuuuuuuuuuuuu",
        status=UserStatus.ACTIVE.value,
    )
    second_subject = HealthSubject(
        id=uuid.uuid4(),
        owner_user_id=second_user.id,
        timezone="UTC",
    )
    db_session.add_all([second_user, second_subject])
    await db_session.commit()
    rule = await db_session.scalar(
        select(ConflictRule).where(
            ConflictRule.code == "derm_retinoid_peel_same_day"
        )
    )

    await activation.set_rule_activation(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        rule_id=rule.id,
        active=False,
    )
    await db_session.commit()

    first_rows = await engine.load_scoped_rules(
        db_session,
        scope=engine.ConflictScope(
            subject_id=legacy_owner_roots.subject_id,
            evaluation_date=date(2026, 8, 19),
        ),
    )
    second_rows = await engine.load_scoped_rules(
        db_session,
        scope=engine.ConflictScope(
            subject_id=second_subject.id,
            evaluation_date=date(2026, 8, 19),
        ),
    )

    assert rule.id not in {row.id for row in first_rows}
    assert rule.id in {row.id for row in second_rows}
