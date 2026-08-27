"""Strict HRT child-scope compatibility after the Stage-3C backfill."""
from __future__ import annotations

from datetime import date

import pytest

from vitals.enums import CycleKind, Domain, Source
from vitals.models.hrt import (
    HrtCycle,
    HrtCycleItem,
    HrtCycleTemplate,
    HrtCycleTemplateItem,
)
from vitals.services.hrt import cycles, templates
from vitals.services.conflict_engine import ConflictScopeError
from vitals.operations.ownership.hrt_child import (
    HrtChildOwnershipBackfillStatus,
    run_hrt_child_ownership_backfill_batch,
)
from vitals.operations.ownership.normalized import (
    NormalizedOwnershipBackfillStatus,
    run_normalized_ownership_backfill_batch,
)
from vitals.operations.ownership.raw import (
    run_raw_ownership_backfill_batch,
)


# Every test here writes or inspects a row with no owner, which is the whole
# subject of the ownership backfill: these services exist to give such rows an
# owner. The application can no longer produce that state, so this module asks
# for the schema as it stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract


async def test_hrt_child_backfill_closes_the_strict_child_scope_bridge(
    db_session,
    legacy_owner_roots,
):
    raw_result = await run_raw_ownership_backfill_batch(
        db_session,
        batch_size=1,
    )
    assert raw_result.completed

    cycle = HrtCycle(
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        name="Synthetic child-backfill cycle",
        kind=CycleKind.COURSE.value,
        start_date=date(2026, 8, 21),
    )
    template = HrtCycleTemplate(
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        name="Synthetic child-backfill template",
        kind=CycleKind.COURSE.value,
    )
    db_session.add_all([cycle, template])
    await db_session.flush()
    cycle_item = HrtCycleItem(
        cycle_id=cycle.id,
        compound_key="synthetic_compound",
        unit="mg",
        schedule=[{"dose": 100, "interval_days": 7, "duration_days": 28}],
    )
    template_item = HrtCycleTemplateItem(
        template_id=template.id,
        compound_key="synthetic_compound",
        unit="mg",
        schedule=[{"dose": 100, "interval_days": 7, "duration_days": 28}],
    )
    db_session.add_all([cycle_item, template_item])
    await db_session.flush()
    cycle_item_updated_at = cycle_item.updated_at
    template_item_updated_at = template_item.updated_at

    for _ in range(32):
        normalized = await run_normalized_ownership_backfill_batch(
            db_session,
            batch_size=100,
        )
        if normalized.status is NormalizedOwnershipBackfillStatus.COMPLETED:
            break
    else:  # pragma: no cover - the catalog is fixed and much smaller.
        raise AssertionError("Stage-3B dependency did not complete")

    subject_id = legacy_owner_roots.subject_id
    with pytest.raises(ConflictScopeError):
        await cycles.list_cycles(db_session, subject_id=subject_id)
    with pytest.raises(ConflictScopeError):
        await templates.list_templates(
            db_session,
            subject_id=subject_id,
        )

    for _ in range(8):
        child_result = await run_hrt_child_ownership_backfill_batch(
            db_session,
            batch_size=1,
        )
        if child_result.status is HrtChildOwnershipBackfillStatus.COMPLETED:
            break
    else:  # pragma: no cover - exactly two one-row child tables are seeded.
        raise AssertionError("Stage-3C HRT child backfill did not complete")

    child_keys = [
        (HrtCycleItem, cycle_item.id),
        (HrtCycleTemplateItem, template_item.id),
    ]
    db_session.expire_all()
    cycle_item, template_item = [
        await db_session.get(model, row_id) for model, row_id in child_keys
    ]
    assert cycle_item is not None and template_item is not None
    assert cycle_item.subject_id == subject_id
    assert template_item.subject_id == subject_id
    assert cycle_item.updated_at == cycle_item_updated_at
    assert template_item.updated_at == template_item_updated_at
    assert cycle_item.schedule == [
        {"dose": 100, "interval_days": 7, "duration_days": 28}
    ]
    assert template_item.schedule == cycle_item.schedule

    assert list(
        await cycles.list_cycles(
            db_session,
            subject_id=subject_id,
        )
    ) == [cycle]
    assert list(
        await templates.list_templates(
            db_session,
            subject_id=subject_id,
        )
    ) == [template]
