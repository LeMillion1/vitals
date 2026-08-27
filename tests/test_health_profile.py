"""The profile belongs to a person, and the number it produces is medical.

Two defects sat behind the same fact — age, sex, height, programme and goals
lived in ``.env``, which names nobody. One was visible: those five were printed
on every patient's weekly digest and doctor's report as though they were theirs,
and were eventually omitted outright rather than misattributed, which cost the
owner five fields and answered nothing.

The other was not visible at all. The Navy formula takes a height and a sex, so
every patient's body-fat percentage and lean body mass were computed from the
installation owner's geometry. A wrong number in a medical record reads exactly
like a right one.
"""

from __future__ import annotations

from vitals.services.nutrition import analytics as nutrition_analytics

from vitals.services.digest.projection import assembly as digest_projection

import pytest

from vitals.enums import UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.services.profile import health as health_profile_service


@pytest.fixture
async def other_subject(db_session):
    """A second person, with no profile of their own."""

    owner = User(
        username="other-body",
        normalized_username="other-body",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(owner)
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=owner.id,
        display_name="Other body",
        timezone="Europe/Chisinau",
    )
    db_session.add(subject)
    await db_session.commit()
    return subject.id


async def test_a_subject_who_has_never_said_has_no_body(db_session, other_subject):
    """Absent is not a default.

    The old shape had one: 190 cm, male, 18. Returning it for somebody who never
    entered anything is not a convenience, it is a claim about their body — and
    it is the claim the Navy formula then computes a percentage from.
    """

    profile = await health_profile_service.get_profile(
        db_session, subject_id=other_subject
    )
    assert profile.age is None
    assert profile.sex is None
    assert profile.height_cm is None
    assert profile.program is None
    assert profile.goals == ()
    assert not profile.describes_a_body


async def test_a_target_keeps_a_default_where_a_measurement_does_not(
    db_session, other_subject
):
    """The three nutrition numbers are goals, not facts about a body.

    A default protein target is a reasonable starting point that nobody is
    misdescribed by. A default height is not, and the distinction is why these
    two groups behave differently in the same object.
    """

    profile = await health_profile_service.get_profile(
        db_session, subject_id=other_subject
    )
    assert profile.protein_target_g == health_profile_service.DEFAULT_PROTEIN_TARGET_G
    assert profile.calories_min == health_profile_service.DEFAULT_CALORIES_MIN
    assert profile.calories_max == health_profile_service.DEFAULT_CALORIES_MAX


async def test_the_installation_profile_is_adopted_once_and_never_overwritten(
    db_session, legacy_owner_roots
):
    """``.env`` is history the moment the owner has edited their own row.

    Adoption runs on every start. If it overwrote, an owner who corrected their
    height in Settings would find the environment's value back after the next
    deploy — silently, and with the corrected one gone.
    """

    adopted = await health_profile_service.get_profile(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert adopted.height_cm is not None

    await health_profile_service.set_profile(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        raw={"height_cm": "171", "sex": "female", "age": "34"},
    )
    await db_session.commit()

    again = await health_profile_service.adopt_installation_profile(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert again.height_cm == 171
    assert again.sex == "female"


async def test_one_persons_profile_is_not_the_others(
    db_session, legacy_owner_roots, other_subject
):
    owner_profile = await health_profile_service.get_profile(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    other_profile = await health_profile_service.get_profile(
        db_session, subject_id=other_subject
    )
    assert owner_profile.height_cm is not None
    assert other_profile.height_cm is None


async def test_profile_projection_keeps_profile_and_timezone_on_one_subject(
    db_session, other_subject
):
    projection = await health_profile_service.get_profile_projection(
        db_session,
        subject_id=other_subject,
    )

    assert projection.profile.height_cm is None
    assert projection.timezone == "Europe/Chisinau"


async def test_a_mistyped_measurement_is_absent_rather_than_clamped(db_session):
    """3 cm is somebody's finger slipping, and 120 cm is a plausible child.

    Clamping turns an obvious mistake into a number a formula will happily use.
    """

    profile = health_profile_service.sanitize(
        {"height_cm": "3", "age": "900", "sex": "unspecified"}
    )
    assert profile.height_cm is None
    assert profile.age is None
    assert profile.sex is None


async def test_the_report_carries_this_subjects_profile_and_not_the_owners(
    db_session, legacy_owner_roots, other_subject
):
    """What the omission placeholder was standing in for.

    The owner gets their five fields back, and the other patient's document says
    nothing about a body nobody described — rather than describing the owner's.
    """


    owner_context = await digest_projection.assemble_context(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert owner_context["user_profile"]["height_cm"] is not None
    assert owner_context["user_profile"]["program"] is not None

    other_context = await digest_projection.assemble_context(
        db_session, subject_id=other_subject
    )
    assert other_context["user_profile"]["height_cm"] is None
    assert other_context["user_profile"]["age"] is None
    assert other_context["user_profile"]["program"] is None


async def test_body_fat_is_not_computed_from_somebody_elses_height(
    db_session, legacy_owner_roots, other_subject
):
    """The quiet half, pinned.

    Both people record the same tape measurements. The owner has a height and a
    sex on file and gets an estimate; the other has neither and gets nothing —
    where before, both got a number computed from the owner's body, and only one
    of them was right.
    """

    from vitals.services import weight as weight_domain

    owner_height, owner_sex = await weight_domain.measurements._body_config(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert owner_height is not None and owner_sex is not None

    other_height, other_sex = await weight_domain.measurements._body_config(
        db_session, subject_id=other_subject
    )
    assert other_height is None and other_sex is None


async def test_half_a_profile_produces_no_estimate(db_session, other_subject):
    """A height without a sex is the same as neither, for this formula."""

    await health_profile_service.set_profile(
        db_session, subject_id=other_subject, raw={"height_cm": "168"}
    )
    await db_session.commit()

    from vitals.services import weight as weight_domain

    height, sex = await weight_domain.measurements._body_config(
        db_session, subject_id=other_subject
    )
    assert height is None and sex is None


async def test_nutrition_goals_follow_the_subject(
    db_session, legacy_owner_roots, other_subject
):


    await health_profile_service.set_profile(
        db_session,
        subject_id=other_subject,
        raw={"protein_target_g": "95", "calories_min": "1800", "calories_max": "2400"},
    )
    await db_session.commit()

    owner_goals = await nutrition_analytics.get_goals(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    other_goals = await nutrition_analytics.get_goals(
        db_session, subject_id=other_subject
    )
    assert other_goals["protein_target_g"] == 95
    assert other_goals["calories_min"] == 1800
    assert owner_goals["protein_target_g"] != 95


async def test_saving_the_profile_writes_the_record_not_the_environment(
    auth_client, db_session, legacy_owner_roots
):
    """The form wrote ``.env`` for every field, including the timezone.

    The timezone one was the sharpest: the day a page shows has been read from
    ``health_subjects.timezone`` since the per-subject clock landed, so this
    form was writing the one place nothing reads. Changing it in Settings did
    nothing at all.
    """

    from web.services.env_writer import read_key

    before = read_key("VITALS_HEIGHT_CM")

    response = await auth_client.post(
        "/settings/profile",
        data={
            "height_cm": "173.5",
            "sex": "female",
            "user_age": "41",
            "timezone": "Asia/Tbilisi",
            "user_program": "  recomposition   on  GLP-1 ",
            "user_goals": "fat loss, muscle retention, fat loss",
            "nutrition_protein_target_g": "120",
            "nutrition_calories_min": "1500",
            "nutrition_calories_max": "1900",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    db_session.expire_all()
    profile = await health_profile_service.get_profile(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert profile.height_cm == 173.5
    assert profile.sex == "female"
    assert profile.age == 41
    # Collapsed whitespace, and a repeated goal is stored once.
    assert profile.program == "recomposition on GLP-1"
    assert profile.goals == ("fat loss", "muscle retention")
    assert profile.protein_target_g == 120

    subject = await db_session.get(HealthSubject, legacy_owner_roots.subject_id)
    await db_session.refresh(subject)
    assert subject.timezone == "Asia/Tbilisi"

    # The environment is left exactly as it was: it is what an installation that
    # has not upgraded yet still adopts from, and a second answer that disagrees
    # with the row is worse than a stale one that is never read.
    assert read_key("VITALS_HEIGHT_CM") == before


async def test_an_unknown_timezone_is_refused_rather_than_stored(
    auth_client, db_session, legacy_owner_roots
):
    """Stored, it would raise on every later request that asks what day it is."""

    subject = await db_session.get(HealthSubject, legacy_owner_roots.subject_id)
    before = subject.timezone

    response = await auth_client.post(
        "/settings/profile",
        data={"timezone": "Mars/Olympus_Mons"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    await db_session.refresh(subject)
    assert subject.timezone == before
