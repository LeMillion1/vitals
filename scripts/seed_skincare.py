#!/usr/bin/env python3
import asyncio
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.database import create_session_factory
from vitals.models.identity import HealthSubject
from vitals.models.skincare import SkincareLog
from vitals.services import skincare_service
from vitals.services.identity_service import acquire_identity_governance_lock


async def seed_skincare(session: AsyncSession) -> None:
    """Replace legacy Skincare demo rows only before identity bootstrap."""

    await acquire_identity_governance_lock(session)
    if await session.scalar(select(HealthSubject.id).limit(1)) is not None:
        raise RuntimeError(
            "seed_skincare is a destructive legacy utility and cannot run "
            "after commercial identity bootstrap"
        )

    # First, clear any existing skincare logs to avoid duplicates / conflicts.
    await session.execute(delete(SkincareLog))

    start_date = date(2026, 6, 4)
    end_date = date(2026, 6, 22)
    current = start_date
    while current <= end_date:
        dow = int(current.strftime("%w"))  # 0 = Sunday, ..., 6 = Saturday

        await skincare_service.upsert_log(
            session,
            on_date=current,
            retinoid=dow in (1, 3, 4, 5, 0),
            azelaic=dow in (1, 3, 4, 5, 0),
            peel=dow in (2, 6),
            niacinamide_spf=True,
            moisturizer=True,
            override=True,
        )
        current += timedelta(days=1)

    await session.flush()


async def main():
    config = load_config()
    factory = create_session_factory(config)

    async with factory() as session:
        await seed_skincare(session)
        await session.commit()
        print("Skincare logs seeded successfully.")


if __name__ == "__main__":
    asyncio.run(main())
