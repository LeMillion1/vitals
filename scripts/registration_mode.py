"""Read or set how this installation makes accounts, from a shell on the machine.

``registration_service`` has said since it was written that the decision to open
registration is a deployment decision rather than a settings screen — and then
nothing anywhere called ``set_stored_mode``. The door was described, gated and
left without a handle: an installation could be unlocked with
``VITALS_REGISTRATION_UNLOCKED`` and still had no way to move off ``disabled``.

This is the handle, and it is deliberately here rather than in the web layer.
Whoever runs it already has a shell on the host and the database credentials,
which is a strictly larger capability than any account registration could
create, so the authorization question does not arise — the same reasoning as
``provision_account.py``.

    python scripts/registration_mode.py                 # what is configured, and what applies
    python scripts/registration_mode.py --set open

Two answers, and the difference is the point. **Stored** is what an operator
configured; **effective** is what anything acts on, which is ``disabled``
whenever the deployment gate is unset no matter what is stored. That is what
makes the stored value safe to configure, review and test ahead of the release
that makes it mean anything.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPOSITORY_ROOT)

from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import vitals.models  # noqa: E402,F401  -- register the metadata graph
from vitals.services import registration_service  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read or set how this installation makes accounts.",
    )
    parser.add_argument(
        "--set",
        dest="mode",
        choices=[mode.value for mode in registration_service.RegistrationMode],
        help=(
            "the mode to store. invite_only and admin_approved are accepted and "
            "stored, and refuse at the door until they are implemented — storing "
            "one is not the same as it working."
        ),
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    database_url = os.getenv("VITALS_DATABASE_URL")
    if not database_url:
        print("VITALS_DATABASE_URL is not set", file=sys.stderr)
        return 2

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            if args.mode is not None:
                try:
                    await registration_service.set_stored_mode(session, args.mode)
                except registration_service.RegistrationValidationError as exc:
                    await session.rollback()
                    print(str(exc), file=sys.stderr)
                    return 1
                await session.commit()
            stored = await registration_service.get_stored_mode(session)
            effective = await registration_service.effective_mode(session)
    finally:
        await engine.dispose()

    print(f"stored={stored.value}")
    print(f"effective={effective.value}")
    if stored is not effective:
        # Said out loud, because "I set it to open and nothing changed" is
        # otherwise a puzzle whose answer is one environment variable.
        print(
            f"the deployment gate {registration_service.REGISTRATION_UNLOCK_ENV} is "
            "not set, so the stored mode does not apply",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
