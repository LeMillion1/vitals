"""Seed synthetic browser-smoke roles into an empty migrated PostgreSQL DB.

This command is additive and refuses a database that already has an account or
health subject. It exists for a disposable Compose validation stack, not as an
installation bootstrap or demo-data reset command.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv


REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPOSITORY_ROOT)

# Compose bind-mounts the runtime-config directory instead of injecting its
# contents. Load the exact file before importing seed_care_demo.
load_dotenv(os.getenv("VITALS_ENV_FILE") or os.path.join(REPOSITORY_ROOT, ".env"))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from scripts.seed_care_demo import build  # noqa: E402
from vitals.models.identity import HealthSubject, User  # noqa: E402
from vitals.persistence.rls import enter_platform_scope  # noqa: E402


CONFIRMATION_ENV = "VITALS_ALLOW_SYNTHETIC_ROLE_SEED"


def _database_url() -> str:
    url = (os.getenv("VITALS_DATABASE_URL") or "").strip()
    if not url.startswith("postgresql+asyncpg://"):
        raise SystemExit(
            "seed_compose_roles requires a PostgreSQL asyncpg URL; "
            "use seed_care_demo.py for local SQLite"
        )
    return url


async def _assert_empty(session: AsyncSession) -> None:
    await enter_platform_scope(session)
    users = int(await session.scalar(select(func.count()).select_from(User)) or 0)
    subjects = int(
        await session.scalar(select(func.count()).select_from(HealthSubject)) or 0
    )
    if users or subjects:
        raise SystemExit(
            "refusing to seed a non-empty installation "
            f"(users={users}, health_subjects={subjects})"
        )


async def _run(url: str) -> None:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            await _assert_empty(session)
            await build(session, include_provider_credentials=False)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-empty-database",
        action="store_true",
        help="confirm that this is a disposable, already migrated empty database",
    )
    args = parser.parse_args()
    if not args.confirm_empty_database:
        parser.error("--confirm-empty-database is required")
    if os.getenv(CONFIRMATION_ENV) != "1":
        parser.error(f"{CONFIRMATION_ENV}=1 is also required")
    asyncio.run(_run(_database_url()))


if __name__ == "__main__":
    main()
