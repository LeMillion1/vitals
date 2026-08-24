"""Bind an existing account to its identity at the provider, from a shell.

``provision_account.py`` has pointed at this step since it was written — "this
account becomes reachable when its provider identity is linked, which until
registration opens is an operator step too" — and the step did not exist. An
account the CLI created could use no password (the password login authenticates
exactly one username from ``.env``) and had no way to be linked short of the
one-time bootstrap, which refuses as soon as an installation has more than one
user. So every account provisioned after the first was an account nobody could
sign in to.

    python scripts/link_identity.py --username dr-ivanova \\
        --issuer https://id.example.com --subject 2417...

The subject is the provider's opaque ``sub`` for that person, read from the
provider's own console — never an email address. A provider may let somebody
claim an address later, and a link made on that basis hands over a whole health
record.

Deliberately a shell script rather than a screen, for the same reason as the
rest of this family: a link decides which human being reaches a record, so it
belongs to whoever already has the machine.
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
from vitals.services.authentication import federation  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bind an existing account to a provider identity.",
    )
    parser.add_argument("--username", required=True)
    parser.add_argument(
        "--issuer",
        required=True,
        help="the provider's iss claim, exactly as it issues it",
    )
    parser.add_argument(
        "--subject",
        required=True,
        help="the provider's opaque sub for that person; never an email address",
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
            try:
                link = await federation.link_identity(
                    session,
                    username=args.username,
                    issuer=args.issuer,
                    subject=args.subject,
                )
            except federation.FederatedLoginError as exc:
                await session.rollback()
                print(str(exc), file=sys.stderr)
                return 1
            user_id = link.user_id
            await session.commit()
    finally:
        await engine.dispose()

    # Identifiers only. This output goes into a terminal history and often into
    # a ticket; the subject is the provider's and stays there.
    print(f"user_id={user_id}")
    print("linked=yes")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
