"""Create one account on a real installation, from a shell on the machine.

Registration is closed and stays closed until the release gates pass, but an
installation still has to be able to gain a second person — otherwise the
professional features shipped in PR-07 and PR-08 have nobody to be about, and
the only way to make a second subject is a demo seeder that refuses to run
against anything but a local SQLite file.

This is that way. It is deliberately **not** registration: there is no form, no
route and no token. Whoever runs it already has a shell on the host and the
database credentials, which is a strictly larger capability than any account it
can create — so the authorization question the web layer has to answer carefully
does not arise here.

    python scripts/provision_account.py --username dr-ivanova --role doctor
    python scripts/provision_account.py --username maria --email maria@example.com \\
        --display-name "Maria K." --timezone Europe/Berlin

A doctor or a trainer keeps no health record of their own unless ``--own-record``
says otherwise: that is the ordinary shape for them, and a subject nobody uses is
a subject the fan-out still runs jobs for.

**It prints no credential**, because there is none to print. Under the OIDC
cutover the provider owns sign-in, and this account becomes reachable when its
provider identity is linked with ``scripts/link_identity.py`` — which, until
registration opens, is an operator step too. Before the cutover, an account
created here cannot use the password login either: that authenticates exactly
one username from ``.env``.
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
from vitals.enums import UserRoleName  # noqa: E402
from vitals.services.authentication import (  # noqa: E402
    provisioning as account_provisioning_service,
)

_ROLES = (
    UserRoleName.MEMBER.value,
    UserRoleName.DOCTOR.value,
    UserRoleName.TRAINER.value,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create one account, and the health record it owns.",
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--email", default=None)
    parser.add_argument("--display-name", default=None)
    parser.add_argument(
        "--timezone",
        default=None,
        help=(
            "IANA timezone for an owned health record; defaults to the "
            "deployment VITALS_TIMEZONE"
        ),
    )
    parser.add_argument(
        "--role",
        action="append",
        choices=_ROLES,
        help=(
            "repeatable; defaults to member. platform_superadmin is deliberately "
            "not offered: granting it is a separate decision about an account "
            "that already exists."
        ),
    )
    parser.add_argument(
        "--own-record",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "whether this account owns a health record. Defaults to yes for a "
            "member and no for a doctor or trainer, which is the ordinary shape "
            "for each."
        ),
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    database_url = os.getenv("VITALS_DATABASE_URL")
    if not database_url:
        print("VITALS_DATABASE_URL is not set", file=sys.stderr)
        return 2

    roles = tuple(args.role or (UserRoleName.MEMBER.value,))
    if args.own_record is None:
        owns_record = UserRoleName.MEMBER.value in roles
    else:
        owns_record = bool(args.own_record)

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with factory() as session:
            try:
                if owns_record:
                    provisioned = (
                        await account_provisioning_service.provision_bound_account(
                            session,
                            username=args.username,
                            email=args.email,
                            display_name=args.display_name,
                            timezone=args.timezone,
                            roles=roles,
                        )
                    )
                else:
                    provisioned = await account_provisioning_service.provision_account(
                        session,
                        username=args.username,
                        email=args.email,
                        display_name=args.display_name,
                        timezone=args.timezone,
                        roles=roles,
                        with_health_record=False,
                    )
            except account_provisioning_service.AccountProvisioningError as exc:
                await session.rollback()
                print(str(exc), file=sys.stderr)
                return 1
            await session.commit()
    finally:
        await engine.dispose()

    # Identifiers and roles only. No name, no email, no credential: this output
    # goes into a terminal history and often into a ticket.
    print(f"user_id={provisioned.user_id}")
    print(f"subject_id={provisioned.subject_id or '-'}")
    print(f"roles={','.join(provisioned.roles)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
