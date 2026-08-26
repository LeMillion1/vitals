"""Read or set how this installation makes accounts, from a shell on the machine.

``authentication.registration`` has said since it was written that the decision to open
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
    python scripts/registration_mode.py --set invite_only \
        --runtime-env .vitals-runtime/vitals.env \
        --confirm-web-recreated 'WEB RECREATED WITH REGISTRATION GATE ENABLED'

Two answers, and the difference is the point. **Stored** is what an operator
configured; **effective** is what anything acts on. This command refuses to
store a non-disabled mode until the owner-only runtime file says the deployment
gate is unlocked and the operator exactly acknowledges that web was recreated
and health-checked with that file.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPOSITORY_ROOT)

from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import vitals.models  # noqa: E402,F401  -- register the metadata graph
from vitals.services.authentication import (  # noqa: E402
    registration as registration_service,
)
from scripts.registration_gate import read_gate_state  # noqa: E402
from vitals.runtime_env import read_env_key  # noqa: E402


WEB_RECREATED_CONFIRMATION = "WEB RECREATED WITH REGISTRATION GATE ENABLED"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read or set how this installation makes accounts.",
    )
    parser.add_argument(
        "--set",
        dest="mode",
        choices=[mode.value for mode in registration_service.RegistrationMode],
        help=(
            "the mode to store. invite_only and admin_approved require their "
            "dedicated proof flows; open admits any eligible provider identity"
        ),
    )
    parser.add_argument(
        "--runtime-env",
        type=Path,
        help=(
            "owner-only application runtime file; required when selecting a "
            "non-disabled mode"
        ),
    )
    parser.add_argument(
        "--confirm-web-recreated",
        help=(
            f"exactly {WEB_RECREATED_CONFIRMATION!r} after recreating and "
            "health-checking vitals_app"
        ),
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    gate_readback: str | None = None
    opens_registration = (
        args.mode is not None
        and args.mode != registration_service.RegistrationMode.DISABLED
    )
    if opens_registration:
        if args.runtime_env is None:
            print(
                "--runtime-env is required before selecting a non-disabled mode",
                file=sys.stderr,
            )
            return 2
        if args.confirm_web_recreated != WEB_RECREATED_CONFIRMATION:
            print(
                "refusing non-disabled mode without exact "
                f"--confirm-web-recreated {WEB_RECREATED_CONFIRMATION!r}",
                file=sys.stderr,
            )
            return 2

    # A supplied runtime file is the authoritative deployment state. Status
    # must not accidentally report the operator shell's exported value, which
    # can differ from what the recreated web process actually received.
    if args.runtime_env is not None and (args.mode is None or opens_registration):
        try:
            gate_readback = read_gate_state(args.runtime_env)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            print(f"registration gate readback failed: {exc}", file=sys.stderr)
            return 2
        if opens_registration and gate_readback != "unlocked":
            print(
                "registration gate readback is locked; refusing non-disabled mode",
                file=sys.stderr,
            )
            return 2

    database_url = (
        read_env_key(
            args.runtime_env,
            "VITALS_DATABASE_URL",
            require_existing=True,
            require_owner_only=True,
        )
        if args.runtime_env is not None
        else os.getenv("VITALS_DATABASE_URL")
    )
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
            previous_gate = os.environ.get(registration_service.REGISTRATION_UNLOCK_ENV)
            if gate_readback is not None:
                os.environ[registration_service.REGISTRATION_UNLOCK_ENV] = (
                    "1" if gate_readback == "unlocked" else "0"
                )
            try:
                effective = await registration_service.effective_mode(session)
            finally:
                if gate_readback is not None:
                    if previous_gate is None:
                        os.environ.pop(
                            registration_service.REGISTRATION_UNLOCK_ENV,
                            None,
                        )
                    else:
                        os.environ[
                            registration_service.REGISTRATION_UNLOCK_ENV
                        ] = previous_gate
    finally:
        await engine.dispose()

    print(f"stored={stored.value}")
    print(f"effective={effective.value}")
    if gate_readback is not None:
        print(f"runtime_gate_readback={gate_readback}")
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
