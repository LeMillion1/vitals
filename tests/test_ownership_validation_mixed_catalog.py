"""Whole-lake validation around subject facts using global catalogs."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from vitals.enums import Domain, Source
from vitals.models.hrt import HrtCompound, HrtDose
from vitals.operations.ownership import validate as service
from vitals.services import hrt_catalog


@pytest.mark.asyncio
async def test_subject_dose_may_reference_a_global_hrt_compound(
    db_session, legacy_owner_roots
):
    await hrt_catalog.sync_catalog(db_session)
    compound = await db_session.scalar(
        select(HrtCompound)
        .where(HrtCompound.subject_id.is_(None))
        .order_by(HrtCompound.id)
        .limit(1)
    )
    assert compound is not None
    db_session.add(
        HrtDose(
            subject_id=legacy_owner_roots.subject_id,
            actor_user_id=legacy_owner_roots.user_id,
            date=date(2026, 8, 26),
            domain=Domain.HRT.value,
            source=Source.MANUAL.value,
            compound_id=compound.id,
            compound_key=compound.key,
            dose=1.0,
            unit="mg",
        )
    )
    await db_session.flush()

    tables, checks, rows, checksum = await service._run_checks(
        db_session,
        scope=service._Scope(
            subject_id=legacy_owner_roots.subject_id,
            owner_user_id=legacy_owner_roots.user_id,
        ),
    )

    assert tables > 0
    assert checks >= tables
    assert rows > 0
    assert len(checksum) == 64
