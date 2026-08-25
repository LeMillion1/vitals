"""Consumer compatibility after the Stage-3B manual ownership backfill."""
from __future__ import annotations

from datetime import date

import pytest

from vitals.enums import AnnotationKind, CycleKind, Domain, MilestoneStatus, Source
from vitals.models.glp1 import DosePhase, Injection, SideEffect
from vitals.models.hrt import (
    HrtCycle,
    HrtCycleItem,
    HrtCycleTemplate,
    HrtCycleTemplateItem,
    HrtDose,
    HrtSideEffect,
)
from vitals.models.labs import LabMarker
from vitals.models.milestones import Milestone
from vitals.models.nutrition import MealLog
from vitals.models.skincare import (
    SkincareLog,
    SkincareObservation,
    SkincareProduct,
)
from vitals.models.supplements import Supplement
from vitals.models.timeline import Annotation
from vitals.models.weight import BodyMeasurement, NoiseMarker
from vitals.services import (
    glp1_service,
    hrt_cycle_service,
    hrt_service,
    hrt_template_service,
    labs_service,
    milestones_service,
    nutrition_service,
    skincare_service,
    supplements_service,
    timeline_service,
    weight_service,
)
from vitals.services.conflict_engine import ConflictScopeError
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


async def test_backfilled_actorless_history_is_visible_to_scoped_consumers(
    db_session,
    legacy_owner_roots,
):
    """Top-level S-only history is exact; HRT children retain their bridge."""

    # Stage-3B is ordered after the raw phase even when the reviewed raw
    # snapshot is empty.
    raw_result = await run_raw_ownership_backfill_batch(db_session, batch_size=1)
    assert raw_result.completed

    on_date = date(2026, 8, 21)
    cycle = HrtCycle(
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        name="Synthetic cycle",
        kind=CycleKind.COURSE.value,
        start_date=on_date,
    )
    template = HrtCycleTemplate(
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        name="Synthetic cycle template",
        kind=CycleKind.COURSE.value,
    )
    rows = [
        Annotation(
            date=on_date,
            domain=Domain.TIMELINE.value,
            source=Source.MANUAL.value,
            kind=AnnotationKind.NOTE.value,
            title="synthetic annotation",
        ),
        BodyMeasurement(
            date=on_date,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            waist_cm=90.0,
        ),
        DosePhase(
            domain=Domain.GLP1.value,
            source=Source.MANUAL.value,
            start_date=on_date,
            drug="semaglutide",
            dose_mg=1.0,
        ),
        Injection(
            date=on_date,
            domain=Domain.GLP1.value,
            source=Source.MANUAL.value,
            drug="semaglutide",
            dose_mg=1.0,
        ),
        SideEffect(
            date=on_date,
            domain=Domain.GLP1.value,
            source=Source.MANUAL.value,
            effect_type="nausea",
            severity=2,
        ),
        LabMarker(
            domain=Domain.LABS.value,
            name="Synthetic marker",
        ),
        MealLog(
            date=on_date,
            domain=Domain.NUTRITION.value,
            source=Source.MANUAL.value,
            name="Synthetic meal",
        ),
        Milestone(
            domain=Domain.WEIGHT.value,
            name="Synthetic milestone",
            status=MilestoneStatus.ACTIVE.value,
        ),
        NoiseMarker(
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            start_date=on_date,
            reason="Synthetic maintenance interval",
        ),
        SkincareLog(
            date=on_date,
            domain=Domain.SKINCARE.value,
            source=Source.MANUAL.value,
        ),
        SkincareObservation(
            date=on_date,
            domain=Domain.SKINCARE.value,
            source=Source.MANUAL.value,
            inflammation=2,
        ),
        SkincareProduct(
            name="Synthetic cleanser",
            type="cleanser",
            default_time="evening",
            schedule_days=[],
            active=True,
        ),
        Supplement(
            domain=Domain.SUPPLEMENTS.value,
            source=Source.MANUAL.value,
            name="Synthetic supplement",
            key="synthetic_stage3b_supplement",
            active=True,
        ),
        cycle,
        template,
        HrtDose(
            date=on_date,
            domain=Domain.HRT.value,
            source=Source.MANUAL.value,
            compound_key="synthetic_compound",
            dose=100.0,
            unit="mg",
        ),
        HrtSideEffect(
            date=on_date,
            domain=Domain.HRT.value,
            source=Source.MANUAL.value,
            effect_type="synthetic-effect",
            severity=2,
        ),
    ]
    db_session.add_all(rows)
    await db_session.flush()
    cycle_item = HrtCycleItem(
        cycle_id=cycle.id,
        compound_key="synthetic_compound",
        unit="mg",
        schedule=[],
    )
    template_item = HrtCycleTemplateItem(
        template_id=template.id,
        compound_key="synthetic_compound",
        unit="mg",
        schedule=[],
    )
    db_session.add_all([cycle_item, template_item])
    await db_session.flush()

    for _ in range(32):
        result = await run_normalized_ownership_backfill_batch(
            db_session,
            batch_size=100,
        )
        if result.status is NormalizedOwnershipBackfillStatus.COMPLETED:
            break
    else:  # pragma: no cover - fixed catalog is intentionally much smaller.
        raise AssertionError("normalized ownership backfill did not complete")

    # The operator uses Core updates and a separate short-lived session in
    # production. Expire this test session so consumer reads observe the same
    # committed database snapshot instead of pre-backfill identity-map values.
    row_keys = [(type(row), row.id) for row in rows]
    db_session.expire_all()
    rows = [await db_session.get(model, row_id) for model, row_id in row_keys]
    assert all(row is not None for row in rows)
    assert all(row.subject_id == legacy_owner_roots.subject_id for row in rows)
    assert all(row.actor_user_id is None for row in rows)
    assert cycle_item.subject_id is None
    assert template_item.subject_id is None

    subject_id = legacy_owner_roots.subject_id
    assert list(await timeline_service.list_annotations(
        db_session, subject_id=subject_id
    )) == [rows[0]]
    assert list(await weight_service.list_body_measurements(
        db_session, subject_id=subject_id
    )) == [rows[1]]
    assert list(await glp1_service.list_dose_phases(
        db_session, subject_id=subject_id
    )) == [rows[2]]
    assert list(await glp1_service.list_injections(
        db_session, subject_id=subject_id
    )) == [rows[3]]
    assert list(await glp1_service.list_side_effects(
        db_session, subject_id=subject_id
    )) == [rows[4]]
    assert list(await labs_service.list_markers(
        db_session, subject_id=subject_id
    )) == [rows[5]]
    assert list(await nutrition_service.list_meals(
        db_session, subject_id=subject_id
    )) == [rows[6]]
    assert list(await milestones_service.list_milestones(
        db_session, subject_id=subject_id
    )) == [rows[7]]
    assert list(await weight_service.list_noise_markers(
        db_session, subject_id=subject_id
    )) == [rows[8]]
    assert list(await skincare_service.list_logs(
        db_session, subject_id=subject_id
    )) == [rows[9]]
    assert list(await skincare_service.list_observations(
        db_session, subject_id=subject_id
    )) == [rows[10]]
    assert list(await skincare_service.list_products(
        db_session, subject_id=subject_id
    )) == [rows[11]]
    assert list(await supplements_service.list_supplements(
        db_session, subject_id=subject_id
    )) == [rows[12]]
    # Stage-3B owns only the HRT parents. A cycle whose items are still unowned
    # is a graph the scoped reader refuses rather than half-reads; the strict
    # child phase is what makes it readable again.
    with pytest.raises(ConflictScopeError):
        await hrt_cycle_service.list_cycles(db_session, subject_id=subject_id)
    with pytest.raises(ConflictScopeError):
        await hrt_template_service.list_templates(
            db_session, subject_id=subject_id
        )
    assert list(await hrt_service.list_doses(
        db_session, subject_id=subject_id
    )) == [rows[15]]
    assert list(await hrt_service.list_side_effects(
        db_session, subject_id=subject_id
    )) == [rows[16]]
