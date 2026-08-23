"""A populated multi-tenant installation, for looking at in a browser.

The suite runs every web test against a database holding exactly one health
subject. That is why a whole class of defect — the app refusing to start, every
page answering 409 — was invisible to 4739 passing tests and obvious within a
minute of opening a browser. This script exists so that minute is cheap to
repeat.

It builds what a real shared installation looks like: an operator, two doctors,
two trainers, ten patients, and care relationships in every state the screens
have to draw — open, awaiting consent, paused, revoked, ended, and an offer
nobody has taken up yet.

It also prints a signed session cookie per account, because the password login
authenticates exactly one username from ``.env``: there is no other way to be
somebody else in a browser here, and inventing one in the app for the sake of
testing would be a worse idea than printing a cookie in a dev script.

Refuses to run against anything but a local SQLite file. A real installation is
PostgreSQL, so that one check is the whole guard — and it fails closed rather
than asking.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, timedelta

REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPOSITORY_ROOT)

os.environ.setdefault("VITALS_DATABASE_URL", "sqlite+aiosqlite:///local_vitals.db")
os.environ.setdefault("VITALS_SESSION_SECRET", "local-secret-key-1234567890")
os.environ.setdefault("VITALS_AUTH_USERNAME", "timur")
os.environ.setdefault(
    "VITALS_AUTH_PASSWORD_HASH",
    "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha",
)
os.environ.setdefault("VITALS_TIMEZONE", "Europe/Chisinau")

from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import vitals.models  # noqa: E402,F401  -- register the metadata graph
from vitals.enums import (  # noqa: E402
    Domain,
    ProfessionalKind,
    ProfessionalVerificationStatus,
    Source,
    UserRoleName,
    UserStatus,
)
from vitals.models.base import Base  # noqa: E402
from vitals.models.identity import HealthSubject, User, UserRole  # noqa: E402
from vitals.models.weight import WeightLog  # noqa: E402
from vitals.services import care_service, invitation_service  # noqa: E402
from vitals.services import professional_service  # noqa: E402
from vitals.utils.timeutils import now_utc  # noqa: E402

#: bcrypt("password") at cost 4 — a dev fixture, never a credential. The
#: password login only ever checks the one username in ``.env`` anyway; this is
#: here so the rows are shaped like real ones.
_DEV_HASH = "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"

TODAY = date.today()


def _require_local_sqlite() -> str:
    url = os.environ["VITALS_DATABASE_URL"]
    if not url.startswith("sqlite"):
        raise SystemExit(
            "seed_care_demo writes a synthetic population and only runs against "
            f"a local SQLite file. VITALS_DATABASE_URL is {url!r}."
        )
    return url


async def _account(
    session: AsyncSession,
    username: str,
    *,
    email: str,
    roles: tuple[UserRoleName, ...] = (),
    verified_email: bool = True,
) -> User:
    user = User(
        username=username,
        normalized_username=username,
        email=email,
        normalized_email=email,
        password_hash=_DEV_HASH,
        status=UserStatus.ACTIVE.value,
        email_verified_at=now_utc() if verified_email else None,
    )
    session.add(user)
    await session.flush()
    for role in roles:
        session.add(UserRole(user_id=user.id, role=role.value))
    await session.flush()
    return user


async def _patient(
    session: AsyncSession, username: str, display_name: str
) -> tuple[User, HealthSubject]:
    user = await _account(session, username, email=f"{username}@example.test")
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=display_name,
        timezone="Europe/Chisinau",
    )
    session.add(subject)
    await session.flush()
    # A little real data, so the screens have something to draw.
    for days_ago in (0, 3, 7, 14):
        session.add(
            WeightLog(
                subject_id=subject.id,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                date=TODAY - timedelta(days=days_ago),
                weight_kg=round(78.0 + days_ago * 0.15, 1),
            )
        )
    await session.flush()
    return user, subject


async def _professional(
    session: AsyncSession, username: str, display_name: str, kind: ProfessionalKind
) -> User:
    user = await _account(
        session,
        username,
        email=f"{username}@example.test",
        roles=(
            UserRoleName.DOCTOR
            if kind is ProfessionalKind.DOCTOR
            else UserRoleName.TRAINER,
        ),
    )
    await professional_service.submit_profile(
        session,
        user_id=user.id,
        kind=kind,
        display_name=display_name,
        credential_reference=f"LIC-{username.upper()}",
    )
    return user


async def _take_into_care(
    session: AsyncSession,
    *,
    owner: User,
    subject: HealthSubject,
    professional: User,
    kind: ProfessionalKind,
):
    issued = await invitation_service.invite(
        session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=kind,
        email=f"{professional.username}@example.test",
    )
    await invitation_service.accept(
        session,
        token=issued.token,
        accepting_user_id=professional.id,
        verified_email=f"{professional.username}@example.test",
    )
    return await care_service.establish_from_invitation(
        session, invitation=issued.invitation
    )


async def build(session: AsyncSession) -> list[tuple[str, str]]:
    """Return (username, description) for everybody worth signing in as."""

    from web.auth import create_session

    who: list[tuple[str, str]] = []

    operator = await _account(
        session,
        "admin",
        email="admin@example.test",
        roles=(UserRoleName.PLATFORM_SUPERADMIN,),
    )
    who.append(("admin", "platform superadmin — verifies professionals, restores"))

    # The account ``.env`` names, so the password login works for one of them.
    timur, timur_subject = await _patient(session, "timur", "Timur")
    who.append(("timur", "patient — the account the password login signs in as"))

    doctor_a = await _professional(
        session, "dr-ivanov", "Dr Ivanov", ProfessionalKind.DOCTOR
    )
    doctor_b = await _professional(
        session, "dr-petrova", "Dr Petrova", ProfessionalKind.DOCTOR
    )
    trainer_a = await _professional(
        session, "coach-orlov", "Coach Orlov", ProfessionalKind.TRAINER
    )
    trainer_b = await _professional(
        session, "coach-sokol", "Coach Sokol", ProfessionalKind.TRAINER
    )
    for name, label in (
        ("dr-ivanov", "doctor — holds four patients, in every consent state"),
        ("dr-petrova", "doctor — verified, holds nobody: the empty roster"),
        ("coach-orlov", "trainer — holds two patients"),
        ("coach-sokol", "trainer — unverified profile, holds one patient"),
    ):
        who.append((name, label))

    # Three of the four are verified; one is left in the queue on purpose, so
    # the operator's review screen has something in it and the consent centre
    # has an unverified professional to draw.
    for professional in (doctor_a, doctor_b, trainer_a):
        from sqlalchemy import select

        from vitals.models.professional import ProfessionalProfile

        profile_id = await session.scalar(
            select(ProfessionalProfile.id).where(
                ProfessionalProfile.user_id == professional.id
            )
        )
        await professional_service.decide(
            session,
            profile_id=profile_id,
            reviewer_user_id=operator.id,
            status=ProfessionalVerificationStatus.VERIFIED,
        )

    patients = [(timur, timur_subject)]
    for index in range(1, 10):
        patients.append(
            await _patient(session, f"patient{index:02d}", f"Patient {index:02d}")
        )
    who.append(("patient01", "patient — sees a doctor and a trainer at once"))
    who.append(("patient05", "patient — withdrew consent, relationship still live"))

    # ── Every state the screens have to draw ─────────────────────────────────
    # Open: relationship and consent both live.
    for owner, subject in patients[:3]:
        relationship = await _take_into_care(
            session,
            owner=owner,
            subject=subject,
            professional=doctor_a,
            kind=ProfessionalKind.DOCTOR,
        )
        await care_service.grant_consent(
            session, relationship_id=relationship.id, actor_user_id=owner.id
        )

    # In care, no consent yet: the ordinary state right after accepting.
    owner, subject = patients[3]
    await _take_into_care(
        session,
        owner=owner,
        subject=subject,
        professional=doctor_a,
        kind=ProfessionalKind.DOCTOR,
    )

    # A trainer alongside a doctor on the same patient.
    owner, subject = patients[0]
    relationship = await _take_into_care(
        session,
        owner=owner,
        subject=subject,
        professional=trainer_a,
        kind=ProfessionalKind.TRAINER,
    )
    await care_service.grant_consent(
        session, relationship_id=relationship.id, actor_user_id=owner.id
    )

    # Paused: the patient stepped back without tearing anything down.
    owner, subject = patients[4]
    relationship = await _take_into_care(
        session,
        owner=owner,
        subject=subject,
        professional=trainer_a,
        kind=ProfessionalKind.TRAINER,
    )
    await care_service.grant_consent(
        session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    await care_service.set_consent_paused(
        session,
        relationship_id=relationship.id,
        actor_user_id=owner.id,
        paused=True,
    )

    # Revoked: consent withdrawn, relationship still standing.
    owner, subject = patients[5]
    relationship = await _take_into_care(
        session,
        owner=owner,
        subject=subject,
        professional=trainer_b,
        kind=ProfessionalKind.TRAINER,
    )
    await care_service.grant_consent(
        session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    await care_service.revoke_consent(
        session, relationship_id=relationship.id, actor_user_id=owner.id
    )

    # Ended: care over, and every consent under it revoked with it.
    owner, subject = patients[6]
    relationship = await _take_into_care(
        session,
        owner=owner,
        subject=subject,
        professional=doctor_a,
        kind=ProfessionalKind.DOCTOR,
    )
    await care_service.grant_consent(
        session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    await care_service.end_relationship(
        session, relationship_id=relationship.id, actor_user_id=owner.id
    )

    # An offer nobody has taken up, for the pending list.
    owner, subject = patients[7]
    await invitation_service.invite(
        session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=ProfessionalKind.DOCTOR,
        email="dr-petrova@example.test",
    )

    await session.commit()

    print("\n  Sign in as somebody by setting this cookie on http://localhost:8000")
    print("  (DevTools → Application → Cookies, name 'vitals_session'):\n")
    width = max(len(name) for name, _ in who)
    for username, label in who:
        print(f"    {username:<{width}}  {label}")
        print(f"    {'':<{width}}  {create_session(username)}\n")
    return who


async def main() -> None:
    url = _require_local_sqlite()
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        await build(session)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
