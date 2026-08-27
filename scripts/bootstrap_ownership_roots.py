#!/usr/bin/env python3
"""Materialize the roots required by the ownership backfill at revision 0048.

This intentionally does not start FastAPI, the scheduler, Redis, or an external
integration. Current application code expects the head schema and must not be
started against an intermediate migration merely to run this bounded database
operation.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from sqlalchemy.ext.asyncio import AsyncSession


async def bootstrap(
    session: AsyncSession,
    *,
    username: str,
    password_hash: str,
    timezone: str,
) -> None:
    """Create the legacy owner, resource roots, and checked-in catalogs."""

    from vitals.services.conflicts import catalog as conflict_catalog
    from vitals.services.hrt import catalog
    from vitals.services.identity.bootstrap import bootstrap_legacy_owner
    from vitals.services.tenancy.bootstrap import bootstrap_legacy_resource_roots

    identity = await bootstrap_legacy_owner(
        session,
        username=username,
        password_hash=password_hash,
        timezone=timezone,
    )
    await bootstrap_legacy_resource_roots(
        session,
        subject_id=identity.subject_id,
        adopt_environment_credentials=True,
    )
    await conflict_catalog.sync_catalog(session)
    await catalog.sync_catalog(session)
    await session.flush()


async def _main() -> None:
    from vitals.config import load_config
    from vitals.database import create_session_factory
    from web.config import get_web_config

    config = load_config()
    web_config = get_web_config()
    factory = create_session_factory(config)
    engine = None
    try:
        async with factory() as session:
            engine = session.bind
            await bootstrap(
                session,
                username=web_config.auth_username,
                password_hash=web_config.auth_password_hash,
                timezone=config.timezone,
            )
            await session.commit()
        print(json.dumps({"status": "completed", "revision": "0048"}))
    finally:
        if engine is not None:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
