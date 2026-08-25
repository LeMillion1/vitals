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
# Provider credentials are encrypted per subject, so the seeded patients need a
# vault key to have accounts at all. A fixed development one: a generated key
# would make the database this script writes unreadable by the next run of it.
os.environ.setdefault(
    "VITALS_CREDENTIAL_KEY", "c2VlZC1jYXJlLWRlbW8ta2V5LTMyLWJ5dGVzLWFhYWE="
)

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
from vitals.models.labs import LabResult  # noqa: E402
from vitals.models.nutrition import MealLog  # noqa: E402
from vitals.models.supplements import Supplement  # noqa: E402
from vitals.services import labs_service  # noqa: E402
from vitals.models.weight import WeightLog  # noqa: E402
from vitals.services import provider_credentials_service  # noqa: E402
from vitals.services.authentication import (  # noqa: E402
    provisioning as account_provisioning_service,
)
from vitals.services.care import invitations, professionals, relationships  # noqa: E402
from vitals.utils.timeutils import now_utc  # noqa: E402

#: bcrypt("password") at cost 4 — a dev fixture, never a credential. The
#: password login only ever checks the one username in ``.env`` anyway; this is
#: here so the rows are shaped like real ones.
_DEV_HASH = "$2b$04$V2PTdRXGL2bhQbX8frCBeuQp8X01Cj84UQCRKDsVNGAOU/siMDlha"

TODAY = date.today()

#: Spread across the day line, so a date that follows the reader is visible.
_TIMEZONES = (
    "Europe/Chisinau",
    "Pacific/Kiritimati",
    "America/Los_Angeles",
    "Asia/Almaty",
    "Pacific/Midway",
)


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
    session: AsyncSession, username: str, display_name: str, *, seed: int = 0
) -> tuple[User, HealthSubject]:
    """A patient, through the same call the product uses.

    It used to assemble one here — account, subject, roots, module map — which
    meant this script and the application had two different ideas of what a
    subject needs, and the script's was the one anybody ever looked at. It goes
    through ``authentication.provisioning`` now, so a gap in provisioning shows
    up in the browser check rather than only after registration opens.
    """

    provisioned = await account_provisioning_service.provision_account(
        session,
        username=username,
        email=f"{username}@example.test",
        display_name=display_name,
        # Not all in one zone, on purpose. A roster where everybody shares the
        # server's clock hides an entire class of defect: "today" came from
        # ``VITALS_TIMEZONE`` for years, and with ten identical patients nothing
        # on screen would ever disagree with it.
        timezone=_TIMEZONES[seed % len(_TIMEZONES)],
    )
    user = await session.get(User, provisioned.user_id)
    # The dev hash, so the printed cookie is not the only way in on a machine
    # that has not cut over to the identity provider yet. Provisioning leaves an
    # account locked out of the password path, which is right in production and
    # unhelpful here.
    user.password_hash = _DEV_HASH
    user.email_verified_at = now_utc()
    subject = await session.get(HealthSubject, provisioned.subject_id)
    # A synthetic credential of their own, so the settings, Garmin and Hevy
    # cards render the connected state a real patient's would. Nothing here ever
    # reaches a provider: the demo seeds facts directly, and these strings would
    # fail a real login on purpose.
    await provider_credentials_service.set_garmin_credentials(
        session,
        subject_id=subject.id,
        email=f"{username}@demo.invalid",
        password="demo-not-a-real-password",
    )
    await provider_credentials_service.set_hevy_credentials(
        session,
        subject_id=subject.id,
        api_key=f"demo-not-a-real-key-{seed}",
    )
    await _seed_record(session, subject.id, seed=seed)
    return user, subject


async def _seed_conversation(session, *, owner, subject, professional) -> None:
    """One care-team conversation with something in it.

    The screens for this shipped with nothing to look at, which is the same
    problem the rest of this script exists to solve: an empty state tells you
    the page renders and nothing about whether it reads.

    Both sides speak, because that is the shape worth checking — a thread where
    only the professional has said anything looks identical whether or not the
    patient can reply.
    """

    from vitals.services.care import threads
    from vitals.services.access_resolution import resolve_access_context

    doctor_context = await resolve_access_context(
        session, user_id=professional.id, subject_id=subject.id
    )
    thread = await threads.open_thread(
        session, context=doctor_context, title="Результаты анализов"
    )
    await threads.send_message(
        session,
        context=doctor_context,
        thread_id=thread.id,
        body="Ферритин ниже нормы. Сдайте повторно натощак через две недели.",
    )
    patient_context = await resolve_access_context(
        session, user_id=owner.id, subject_id=subject.id
    )
    await threads.send_message(
        session,
        context=patient_context,
        thread_id=thread.id,
        body="Хорошо, запишусь на утро понедельника.",
    )


async def _seed_record(session: AsyncSession, subject_id, *, seed: int) -> None:
    """Enough of a record for a professional's screen to be worth looking at.

    A subject with only weight logs made the care view look finished when it was
    not: every other section rendered as "no data" and the empty state hid the
    fact that nothing was reading them. Varied per patient — ``seed`` shifts the
    numbers — so a roster of ten does not read as ten copies of one person.
    """

    for days_ago in (0, 3, 7, 14):
        session.add(
            WeightLog(
                subject_id=subject_id,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                date=TODAY - timedelta(days=days_ago),
                weight_kg=round(72.0 + seed * 1.5 + days_ago * 0.15, 1),
            )
        )

    # One marker inside its range and one outside, because a report that never
    # shows a flag never shows whether flagging works.
    #
    # Names go through the service's own normalizer rather than in as typed.
    # Every read path normalizes the marker it looks up, so a row written
    # around it is found by the table (which lists whatever is stored) and
    # missed by the chart (which asks for the normalized name): "tsh" is
    # stored, "Tsh" is asked for, and the per-marker graph renders empty next
    # to a value that is plainly there. Seeded data that the app itself could
    # not have written tests the wrong app.
    #
    # Four dates rather than one, so the chart has a line to draw. A single
    # point renders as an empty grid, which is indistinguishable from the bug
    # above — and an installation seeded for looking at should not need the
    # database opened to tell those apart.
    markers = (
        ("ferritin", 30.0 + seed * 9, "ng/mL", 30.0, 400.0),
        ("tsh", 0.2 + seed * 0.05, "mIU/L", 0.4, 4.0),
    )
    for marker, value, unit, low, high in markers:
        for index, days_ago in enumerate((84, 56, 28, 2)):
            # Drifting towards the latest value, so the series reads as a
            # measurement over time rather than four copies of one number.
            drift = 1.0 + (len(range(4)) - index - 1) * 0.08
            reading = round(value * drift, 2)
            flag = "normal"
            if reading < low:
                flag = "low"
            elif reading > high:
                flag = "high"
            session.add(
                LabResult(
                    subject_id=subject_id,
                    domain=Domain.LABS.value,
                    source=Source.MANUAL.value,
                    date=TODAY - timedelta(days=days_ago),
                    marker=labs_service.normalize_marker(marker),
                    value=reading,
                    unit=unit,
                    ref_low=low,
                    ref_high=high,
                    flag=flag,
                    lab_name="Synevo",
                )
            )

    for days_ago in (0, 1, 2):
        session.add(
            MealLog(
                subject_id=subject_id,
                domain=Domain.NUTRITION.value,
                source=Source.MANUAL.value,
                date=TODAY - timedelta(days=days_ago),
                name="Овсянка с ягодами" if days_ago % 2 else "Курица с рисом",
                calories=520 + seed * 10,
                protein_g=38.0 + seed,
                fat_g=14.0,
                carbs_g=61.0,
            )
        )

    session.add(
        Supplement(
            subject_id=subject_id,
            domain=Domain.SUPPLEMENTS.value,
            source=Source.MANUAL.value,
            key="vitamin_d3",
            name="Витамин D3",
            dose="4000 IU",
            timing="morning",
            active=True,
        )
    )
    await session.flush()


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
    await professionals.submit_profile(
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
    issued = await invitations.invite(
        session,
        subject_id=subject.id,
        actor_user_id=owner.id,
        kind=kind,
        email=f"{professional.username}@example.test",
    )
    await invitations.accept(
        session,
        token=issued.token,
        accepting_user_id=professional.id,
        verified_email=f"{professional.username}@example.test",
    )
    return await relationships.establish_from_invitation(
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
        await professionals.decide(
            session,
            profile_id=profile_id,
            reviewer_user_id=operator.id,
            expected_status=ProfessionalVerificationStatus.PENDING,
            status=ProfessionalVerificationStatus.VERIFIED,
        )

    patients = [(timur, timur_subject)]
    for index in range(1, 10):
        patients.append(
            await _patient(
                session,
                f"patient{index:02d}",
                f"Patient {index:02d}",
                seed=index,
            )
        )
    who.append(("patient01", "patient — sees a doctor and a trainer at once"))
    who.append(("patient05", "patient — withdrew consent, relationship still live"))

    # ── Every state the screens have to draw ─────────────────────────────────
    # Open: relationship and consent both live.
    for index, (owner, subject) in enumerate(patients[:3]):
        relationship = await _take_into_care(
            session,
            owner=owner,
            subject=subject,
            professional=doctor_a,
            kind=ProfessionalKind.DOCTOR,
        )
        await relationships.grant_consent(
            session, relationship_id=relationship.id, actor_user_id=owner.id
        )
        if index == 0:
            await _seed_conversation(
                session, owner=owner, subject=subject, professional=doctor_a
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
    await relationships.grant_consent(
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
    await relationships.grant_consent(
        session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    await relationships.set_consent_paused(
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
    await relationships.grant_consent(
        session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    await relationships.revoke_consent(
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
    await relationships.grant_consent(
        session, relationship_id=relationship.id, actor_user_id=owner.id
    )
    await relationships.end_relationship(
        session, relationship_id=relationship.id, actor_user_id=owner.id
    )

    # An offer nobody has taken up, for the pending list.
    owner, subject = patients[7]
    await invitations.invite(
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
