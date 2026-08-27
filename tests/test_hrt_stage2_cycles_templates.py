"""Focused Stage-2 ownership contracts for HRT cycles and templates."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from vitals.enums import Source, UserStatus
from vitals.models.hrt import (
    DOMAIN,
    HrtCycle,
    HrtCycleTemplate,
    HrtCycleTemplateItem,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.ownership import WriteIdentity
from vitals.services.conflicts import engine
from vitals.services.hrt import catalog, cycles, templates


ON_DATE = date(2026, 8, 20)
SCHEDULE = [{"dose": 10, "interval_days": 1}]


async def _identity(session, slug: str) -> WriteIdentity:
    user = User(
        username=slug,
        normalized_username=slug,
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    subject = HealthSubject(owner_user_id=user.id, timezone="Asia/Almaty")
    session.add(subject)
    await session.flush()
    return WriteIdentity(subject_id=subject.id, actor_user_id=user.id)


async def _prepared(session, identity: WriteIdentity, *, legacy: bool = False):
    return await engine.prepare_scoped_write(
        session,
        context=engine.ConflictWriteContext(
            identity=identity,
            evaluation_date=ON_DATE,
            legacy_bridge=(
                engine.LegacyConflictBridge.FULLY_UNOWNED
                if legacy
                else engine.LegacyConflictBridge.REJECT
            ),
        ),
    )


async def _owned_cycle(session, identity: WriteIdentity, *, source=Source.MANUAL):
    prepared = await _prepared(session, identity)
    cycle = await cycles.add_cycle(
        session,
        kind="course",
        start_date=ON_DATE,
        source=source,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    item = await cycles.add_cycle_item(
        session,
        cycle.id,
        compound_key="synthetic-free-text",
        schedule=SCHEDULE,
        identity=identity,
        prepared_conflict_write=prepared,
    )
    assert item is not None
    return cycle, item, prepared


async def test_cycle_roots_children_reads_and_auto_close_are_exact_subject(db_session):
    first = await _identity(db_session, "hrt-cycle-first")
    second = await _identity(db_session, "hrt-cycle-second")
    first_cycle, first_item, first_prepared = await _owned_cycle(
        db_session, first, source=Source.MCP
    )
    second_cycle, _, second_prepared = await _owned_cycle(db_session, second)

    assert (
        first_cycle.subject_id,
        first_cycle.actor_user_id,
        first_cycle.source,
        first_item.subject_id,
    ) == (first.subject_id, first.actor_user_id, Source.MCP.value, first.subject_id)
    assert first_cycle.end_date is None
    assert second_cycle.end_date is None
    assert [row.id for row in await cycles.list_cycles(
        db_session, subject_id=first.subject_id
    )] == [first_cycle.id]
    assert [row.id for row in await cycles.list_cycles(
        db_session, subject_id=second.subject_id
    )] == [second_cycle.id]
    assert await cycles.update_cycle_item(
        db_session,
        first_item.id,
        note="foreign write",
        identity=second,
        prepared_conflict_write=second_prepared,
    ) is None
    assert await cycles.delete_cycle(
        db_session,
        second_cycle.id,
        identity=first,
        prepared_conflict_write=first_prepared,
    ) is False




async def test_invalid_prepared_capability_is_rejected_before_cycle_target_use(db_session):
    first = await _identity(db_session, "hrt-capability-first")
    second = await _identity(db_session, "hrt-capability-second")
    cycle, item, first_prepared = await _owned_cycle(db_session, first)

    with pytest.raises(engine.ConflictPreparedWriteError):
        await cycles.update_cycle_item(
            db_session,
            item.id,
            note="forged",
            identity=second,
            prepared_conflict_write=first_prepared,
        )
    assert cycle.subject_id == first.subject_id
    assert item.note is None


async def test_cycle_item_units_are_validated_on_create_and_update(db_session):
    identity = await _identity(db_session, "hrt-cycle-unit")
    cycle, item, prepared = await _owned_cycle(db_session, identity)

    with pytest.raises(ValueError, match="unknown dose unit"):
        await cycles.add_cycle_item(
            db_session,
            cycle.id,
            compound_key="synthetic-free-text",
            schedule=SCHEDULE,
            unit="teaspoon",
            identity=identity,
            prepared_conflict_write=prepared,
        )
    with pytest.raises(ValueError, match="unknown dose unit"):
        await cycles.update_cycle_item(
            db_session,
            item.id,
            unit="teaspoon",
            identity=identity,
            prepared_conflict_write=prepared,
        )

    assert item.unit == "mg"


async def test_cycle_create_rejects_inverted_date_range_before_persistence(db_session):
    identity = await _identity(db_session, "hrt-cycle-range")
    prepared = await _prepared(db_session, identity)

    with pytest.raises(ValueError, match="end_date cannot be before"):
        await cycles.add_cycle(
            db_session,
            kind="course",
            start_date=ON_DATE,
            end_date=ON_DATE - timedelta(days=1),
            identity=identity,
            prepared_conflict_write=prepared,
        )

    assert await db_session.scalar(select(func.count()).select_from(HrtCycle)) == 0


async def test_template_snapshot_export_and_materialization_keep_scope_and_provenance(
    db_session,
):
    first = await _identity(db_session, "hrt-template-first")
    second = await _identity(db_session, "hrt-template-second")
    old_cycle, _, first_prepared = await _owned_cycle(
        db_session, first, source=Source.MCP
    )
    template = await templates.save_cycle_as_template(
        db_session,
        old_cycle.id,
        name="Scoped template",
        source=Source.MCP,
        identity=first,
        prepared_conflict_write=first_prepared,
    )
    assert template is not None
    assert (
        template.subject_id,
        template.actor_user_id,
        template.source,
        template.items[0].subject_id,
    ) == (first.subject_id, first.actor_user_id, Source.MCP.value, first.subject_id)
    assert templates.export_template(
        template, subject_id=first.subject_id
    )["name"] == "Scoped template"
    with pytest.raises(engine.ConflictScopeError):
        templates.export_template(template, subject_id=second.subject_id)

    materialized = await templates.create_cycle_from_template(
        db_session,
        template.id,
        start_date=ON_DATE + timedelta(days=1),
        source=Source.MANUAL,
        identity=first,
        prepared_conflict_write=first_prepared,
    )
    assert materialized is not None
    assert (
        materialized.subject_id,
        materialized.actor_user_id,
        materialized.source,
        materialized.items[0].subject_id,
    ) == (
        first.subject_id,
        first.actor_user_id,
        Source.MANUAL.value,
        first.subject_id,
    )
    assert old_cycle.end_date == ON_DATE
    assert (old_cycle.actor_user_id, old_cycle.source) == (
        first.actor_user_id,
        Source.MCP.value,
    )


async def test_scoped_template_import_is_parse_only_and_subject_isolated(db_session):
    await catalog.sync_catalog(db_session)
    identity = await _identity(db_session, "hrt-template-import")
    prepared = await _prepared(db_session, identity)
    imported = await templates.import_template(
        db_session,
        {
            "format": templates.EXPORT_FORMAT,
            "version": 1,
            "name": "Imported",
            "kind": "course",
            "items": [
                {
                    "compound_key": "oxandrolone",
                    "schedule": SCHEDULE,
                }
            ],
        },
        source=Source.MCP,
        identity=identity,
        prepared_conflict_write=prepared,
    )

    assert (imported.subject_id, imported.actor_user_id, imported.source) == (
        identity.subject_id,
        identity.actor_user_id,
        Source.MCP.value,
    )
    assert imported.items[0].subject_id == identity.subject_id
    assert await db_session.scalar(select(func.count()).select_from(RawPayload)) == 0


async def test_template_graph_rejects_foreign_child_and_partial_legacy_parent(
    db_session,
    legacy_owner_roots,
    unenforced_legacy_write,
):
    first = await _identity(db_session, "hrt-template-graph-first")
    second = await _identity(db_session, "hrt-template-graph-second")
    template = HrtCycleTemplate(
        subject_id=first.subject_id,
        actor_user_id=first.actor_user_id,
        domain=DOMAIN,
        source=Source.MANUAL.value,
        name="Corrupt child",
        kind="course",
    )
    partial = HrtCycleTemplate(subject_id=legacy_owner_roots.subject_id,
        actor_user_id=first.actor_user_id,
        domain=DOMAIN,
        source=Source.MANUAL.value,
        name="Partial",
        kind="course",
    )
    db_session.add_all([template, partial])
    await db_session.flush()
    # The current PostgreSQL schema rejects this graph at the composite FK.
    # Reproduce a pre-0046 row through the explicit historical-data seam so the
    # scoped reader still proves it fails closed on legacy corruption.
    async with unenforced_legacy_write(db_session):
        db_session.add(
            HrtCycleTemplateItem(
                subject_id=second.subject_id,
                template=template,
                compound_key="synthetic-free-text",
                unit="mg",
                schedule=SCHEDULE,
            )
        )

    with pytest.raises(engine.ConflictScopeError):
        await templates.get_template(
            db_session, template.id, subject_id=first.subject_id
        )
    assert await templates.get_template(
        db_session,
        partial.id,
        subject_id=first.subject_id,
    ) is None
