"""Skincare tests — checklist upsert, retinoid+peel same-domain block, the
isotretinoin→peel cross-domain block, observations, and the resolver."""
from __future__ import annotations

from datetime import date

import pytest

from vitals.models.conflict_rule import ConflictRule
from vitals.services import (
    conflict_registrations,
    skincare_service,
    supplements_service,
)
from vitals.services.conflict_engine import ConflictBlocked
from vitals.utils.timeutils import today_local



async def test_checklist_upsert_is_one_per_day(db_session, owner_write):
    d = date(2026, 6, 1)
    await skincare_service.upsert_log(db_session, on_date=d, retinoid=True,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await skincare_service.upsert_log(db_session, on_date=d, retinoid=False, moisturizer=True,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(d),
    )
    await db_session.commit()
    logs = await skincare_service.list_logs(db_session,
        subject_id=owner_write.subject_id,
    )
    assert len(logs) == 1
    assert logs[0].retinoid is False and logs[0].moisturizer is True


async def _seed_retinoid_peel_rule(db_session):
    # A scoped write consults the registered resolvers for its domain.
    conflict_registrations.register_all_resolvers()
    db_session.add(
        ConflictRule(
            rule_type="hard_block",
            domain_a="skincare",
            condition_a={"retinoid": True},
            domain_b="skincare",
            condition_b={"peel": True},
            severity="block",
            message="Ретиноид и пилинг в один вечер — высокий риск раздражения.",
            active=True,
        )
    )
    await db_session.commit()


async def test_retinoid_plus_peel_blocked(db_session, owner_write):
    await _seed_retinoid_peel_rule(db_session)
    with pytest.raises(ConflictBlocked):
        await skincare_service.upsert_log(
            db_session, on_date=date(2026, 6, 2), retinoid=True, peel=True,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 2)),
    )
    await db_session.rollback()


async def test_retinoid_plus_peel_override_saves(db_session, owner_write):
    await _seed_retinoid_peel_rule(db_session)
    row = await skincare_service.upsert_log(
        db_session, on_date=date(2026, 6, 2), retinoid=True, peel=True, override=True,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 2)),
    )
    await db_session.commit()
    assert row.retinoid and row.peel


async def test_isotretinoin_blocks_peel_cross_domain(db_session, owner_write):
    conflict_registrations.register_all_resolvers()
    db_session.add(
        ConflictRule(
            rule_type="hard_block",
            domain_a="supplements",
            condition_a={"key": "isotretinoin", "active": True},
            domain_b="skincare",
            condition_b={"peel": True},
            severity="block",
            message="Системный изотретиноин активен — химический пилинг противопоказан.",
            active=True,
        )
    )
    await supplements_service.add_supplement(
        db_session, name="Изотретиноин", key="isotretinoin", active=True,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    )
    await db_session.commit()

    # Peel today while isotretinoin is active → blocked.
    with pytest.raises(ConflictBlocked):
        await skincare_service.upsert_log(db_session, on_date=today_local(), peel=True,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(today_local()),
    )
    await db_session.rollback()


async def test_observation_crud(db_session, owner_write):
    o = await skincare_service.add_observation(
        db_session, on_date=date(2026, 6, 1), inflammation=3, pih=2, zone="лоб",
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(date(2026, 6, 1)),
    )
    await db_session.commit()
    rows = await skincare_service.list_observations(db_session,
        subject_id=owner_write.subject_id,
    )
    assert len(rows) == 1 and rows[0].inflammation == 3
    assert await skincare_service.delete_observation(
        db_session,
        o.id,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(),
    ) is True
    await db_session.commit()
    assert len(await skincare_service.list_observations(db_session,
        subject_id=owner_write.subject_id,
    )) == 0


async def test_resolve_today(db_session, owner_write):
    conflict_registrations.register_all_resolvers()
    await skincare_service.upsert_log(db_session, on_date=today_local(), peel=True,
        identity=owner_write.identity,
        prepared_conflict_write=await owner_write.write(today_local()),
    )
    await db_session.commit()
    # The scoped resolver is the one a scoped write consults.
    items = await skincare_service.resolve_today_scoped(
        db_session,
        scope=owner_write.context.scope,
    )
    assert items and items[0]["peel"] is True
