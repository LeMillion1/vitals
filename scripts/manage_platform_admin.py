"""Provision a recordless OIDC operator or revoke platform authority.

This is a host-only boundary.  Public registration and invitations cannot call
it, and the exact confirmation keeps a pasted command from silently changing
installation-wide authority.
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
from vitals.services import identity_service, platform_admin_service  # noqa: E402
from vitals.services.authentication import platform_operators  # noqa: E402

PROVISION_CONFIRMATION = "PROVISION RECORDLESS PLATFORM OPERATOR"
REVOKE_CONFIRMATION = "REVOKE PLATFORM ADMIN ROLE"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage recordless platform-operator authority.",
    )
    commands = parser.add_subparsers(dest="action", required=True)

    provision = commands.add_parser("provision")
    provision.add_argument("--actor-username", required=True)
    provision.add_argument("--username", required=True)
    provision.add_argument("--issuer", required=True)
    provision.add_argument("--subject", required=True)
    provision.add_argument("--confirm", required=True)

    revoke = commands.add_parser("revoke")
    revoke.add_argument("--actor-username", required=True)
    revoke.add_argument("--target-username", required=True)
    revoke.add_argument("--issuer", required=True)
    revoke.add_argument("--confirm", required=True)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    database_url = os.getenv("VITALS_DATABASE_URL")
    if not database_url:
        print("VITALS_DATABASE_URL is not set", file=sys.stderr)
        return 2
    expected_confirmation = (
        PROVISION_CONFIRMATION if args.action == "provision" else REVOKE_CONFIRMATION
    )
    if args.confirm != expected_confirmation:
        print("exact confirmation is required", file=sys.stderr)
        return 2

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            try:
                if args.action == "provision":
                    provisioned = (
                        await platform_operators.provision_platform_operator(
                            session,
                            actor_username=args.actor_username,
                            username=args.username,
                            issuer=args.issuer,
                            subject=args.subject,
                        )
                    )
                    target_user_id = provisioned.user_id
                    changed = True
                else:
                    target_user_id, changed = (
                        await platform_operators.revoke_health_owner_platform_admin(
                            session,
                            actor_username=args.actor_username,
                            target_username=args.target_username,
                            issuer=args.issuer,
                        )
                    )
                await session.commit()
            except (
                identity_service.IdentityServiceError,
                platform_admin_service.PlatformAdminError,
                platform_operators.PlatformOperatorError,
            ) as exc:
                await session.rollback()
                print(str(exc), file=sys.stderr)
                return 1
    finally:
        await engine.dispose()

    print(f"user_id={target_user_id}")
    print(
        "platform_superadmin="
        f"{'provisioned' if args.action == 'provision' else 'revoked'}"
    )
    print(f"changed={'yes' if changed else 'no'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
