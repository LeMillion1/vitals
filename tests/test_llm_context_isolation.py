"""What leaves for the model is one person's, and only the domains agreed to.

``assemble_context`` is the external-model boundary: the weekly digest, the
daily brief, the doctor's report and the MCP composition tool all reason over
what it returns, and what it returns is serialized whole into a prompt. Its
subject has been mandatory since it was written and every read inside it is
scoped — the boundary is there. What was not there is the proof, and a boundary
nobody re-checks is one a single new query can quietly step around: adding an
unscoped `select` to a 3000-line assembler is a two-line mistake that no
existing test would notice.

So these seed a second person with values that could not belong to the first,
compose for the first, and search everything that comes out — the structure, and
the prompt string built from it — for anything of the second's. The sentinels
are deliberately absurd: a real leak of plausible numbers is invisible in a
diff, and one of ``999.9`` is not.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from vitals.enums import Domain, Source, UserStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.labs import LabResult
from vitals.models.nutrition import MealLog
from vitals.models.supplements import Supplement
from vitals.models.timeline import Annotation
from vitals.models.weight import BodyMeasurement, WeightLog
from vitals.services import digest_service, modules_service

pytestmark = pytest.mark.usefixtures("all_modules_on", "owned_by_legacy_subject")

DAY = date(2026, 8, 4)

#: Strings and numbers that cannot occur for the person being composed for.
#: Absurd on purpose: a leak of plausible values hides in a diff.
SENTINELS = (
    "SENTINEL-MEAL-DO-NOT-LEAK",
    "SENTINEL-MARKER-DO-NOT-LEAK",
    "SENTINEL-SUPPLEMENT-DO-NOT-LEAK",
    "SENTINEL-NOTE-DO-NOT-LEAK",
    "999.9",
    "888.8",
    "77777",
)


async def _second_person(session) -> HealthSubject:
    """Somebody else, with data in every domain the context reads."""

    owner = User(
        username="the-other-person",
        normalized_username="the-other-person",
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(owner)
    await session.flush()
    subject = HealthSubject(
        owner_user_id=owner.id, display_name="The Other Person", timezone="UTC"
    )
    session.add(subject)
    await session.flush()

    session.add_all(
        [
            WeightLog(
                subject_id=subject.id,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                date=DAY,
                weight_kg=999.9,
            ),
            BodyMeasurement(
                subject_id=subject.id,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                date=DAY,
                waist_cm=888.8,
            ),
            MealLog(
                subject_id=subject.id,
                domain=Domain.NUTRITION.value,
                date=DAY,
                name="SENTINEL-MEAL-DO-NOT-LEAK",
                calories=77777,
                protein_g=999.9,
            ),
            LabResult(
                subject_id=subject.id,
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                date=DAY,
                marker="SENTINEL-MARKER-DO-NOT-LEAK",
                value=999.9,
                unit="mIU/L",
            ),
            Supplement(
                subject_id=subject.id,
                domain=Domain.SUPPLEMENTS.value,
                name="SENTINEL-SUPPLEMENT-DO-NOT-LEAK",
                key="sentinel-supplement",
                dose="999.9 mg",
                active=True,
            ),
            Annotation(
                subject_id=subject.id,
                domain=Domain.TIMELINE.value,
                date=DAY,
                title="SENTINEL-NOTE-DO-NOT-LEAK",
            ),
        ]
    )
    await session.flush()
    return subject


def _leaks(haystack: str) -> list[str]:
    return [sentinel for sentinel in SENTINELS if sentinel in haystack]


async def _compose_for(session, subject_id, **kwargs):
    return await digest_service.assemble_context(
        session,
        subject_id=subject_id,
        on_date=DAY,
        period_days=kwargs.pop("period_days", 7),
        # The closed weekly window, which is the report an external model
        # actually receives. ``daily_brief`` is a one-day mode and would make
        # "bounded to a week" a claim about a different thing.
        mode=kwargs.pop("mode", digest_service.REPORT_MODE_CLOSED),
        **kwargs,
    )


# ── One person ───────────────────────────────────────────────────────────────


async def test_the_context_carries_nothing_of_anybody_else(
    db_session, legacy_owner_roots
):
    """Composed for one record while another exists, and searched whole.

    The assertion is on the serialized structure rather than on named keys
    because the risk is a section nobody thought to check. A new unscoped query
    lands somewhere, and "somewhere" is exactly what a key-by-key test misses.
    """

    await _second_person(db_session)
    db_session.add(
        WeightLog(
            subject_id=legacy_owner_roots.subject_id,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            date=DAY,
            weight_kg=72.5,
        )
    )
    await db_session.commit()

    context = await _compose_for(db_session, legacy_owner_roots.subject_id)
    found = _leaks(digest_service.build_prompt(context))
    assert not found, f"another person's data reached the model: {found}"


async def test_the_prompt_carries_nothing_of_anybody_else(
    db_session, legacy_owner_roots
):
    """The prompt is the thing that actually leaves, in both languages.

    ``build_prompt`` serializes the whole context, so anything the structure
    holds is something an external model receives verbatim.
    """

    await _second_person(db_session)
    await db_session.commit()

    context = await _compose_for(db_session, legacy_owner_roots.subject_id)
    for language in ("ru", "en"):
        prompt = digest_service.build_prompt(context, lang=language)
        found = _leaks(prompt)
        assert not found, f"the {language} prompt carries somebody else's: {found}"


async def test_composing_for_the_other_person_returns_theirs(
    db_session, legacy_owner_roots
):
    """The mirror, so the test above cannot pass by composing nothing at all.

    Without this, an ``assemble_context`` that returned an empty dictionary
    would satisfy every isolation assertion here perfectly.
    """

    other = await _second_person(db_session)
    await db_session.commit()

    context = await _compose_for(db_session, other.id)
    prompt = digest_service.build_prompt(context)
    assert _leaks(prompt), (
        "composing for the second person returned none of their data — the "
        "isolation tests above are passing on an empty context"
    )
    del legacy_owner_roots


async def test_the_subject_is_mandatory_rather_than_inferred(
    db_session, legacy_owner_roots
):
    """Never "the only subject in the database".

    An assembler that falls back to a sole subject is one that starts composing
    somebody's report from whoever happens to be alone in the table, and stops
    being correct the moment they are not.
    """

    for absent in (None, "not-a-uuid", 0):
        with pytest.raises(TypeError):
            await _compose_for(db_session, absent)
    del legacy_owner_roots


# ── Bounded domains ──────────────────────────────────────────────────────────


async def test_a_disabled_module_contributes_nothing_to_the_prompt(
    db_session, legacy_owner_roots
):
    """Turning a section off is a promise about what leaves, not just what renders.

    A module the person switched off is one whose data they have said should not
    be part of this; a context that gates the *section* but leaves its numbers in
    some summary elsewhere keeps that promise on screen and breaks it at the
    boundary that matters.
    """

    db_session.add_all(
        [
            MealLog(
                subject_id=legacy_owner_roots.subject_id,
                domain=Domain.NUTRITION.value,
                date=DAY,
                name="SENTINEL-MEAL-DO-NOT-LEAK",
                calories=77777,
                protein_g=999.9,
            ),
            Supplement(
                subject_id=legacy_owner_roots.subject_id,
                domain=Domain.SUPPLEMENTS.value,
                name="SENTINEL-SUPPLEMENT-DO-NOT-LEAK",
                key="sentinel-supplement",
                dose="999.9 mg",
                active=True,
            ),
        ]
    )
    await db_session.commit()

    everything_on = {key: True for key in modules_service.MODULE_REGISTRY}
    with_them = await _compose_for(
        db_session,
        legacy_owner_roots.subject_id,
        enabled_modules=everything_on,
    )
    assert _leaks(digest_service.build_prompt(with_them)), (
        "the sentinels are not in the report even with every module on — this "
        "test would pass without proving anything"
    )

    switched_off = dict(everything_on, nutrition=False, supplements=False)
    without_them = await _compose_for(
        db_session,
        legacy_owner_roots.subject_id,
        enabled_modules=switched_off,
    )
    found = _leaks(digest_service.build_prompt(without_them))
    assert not found, f"a switched-off module still reached the model: {found}"


async def test_the_window_bounds_what_leaves(db_session, legacy_owner_roots):
    """A report about a week is about that week.

    Dated rows outside the window are as much a disclosure as another person's
    when the document says it covers seven days.
    """

    db_session.add_all(
        [
            Annotation(
                subject_id=legacy_owner_roots.subject_id,
                domain=Domain.TIMELINE.value,
                date=DAY - timedelta(days=400),
                title="SENTINEL-NOTE-DO-NOT-LEAK",
            ),
            LabResult(
                subject_id=legacy_owner_roots.subject_id,
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                date=DAY + timedelta(days=30),
                marker="SENTINEL-MARKER-DO-NOT-LEAK",
                value=999.9,
                unit="mIU/L",
            ),
        ]
    )
    await db_session.commit()

    context = await _compose_for(db_session, legacy_owner_roots.subject_id)
    prompt = digest_service.build_prompt(context)
    assert "SENTINEL-NOTE-DO-NOT-LEAK" not in prompt, (
        "a note from more than a year before the window reached the model"
    )
    assert "SENTINEL-MARKER-DO-NOT-LEAK" not in prompt, (
        "a result dated after the report's window reached the model"
    )


# ── Nothing that names the person ────────────────────────────────────────────


async def test_the_context_says_nothing_about_who_this_is(
    db_session, legacy_owner_roots
):
    """Clinical attributes cross the boundary; identifiers do not.

    ``user_profile`` carries age, sex, height, programme and goals, which is
    what makes a report about a body worth reading. It carries no name, no
    username, no email and no row id — none of which would improve the
    narrative, and all of which would turn an external model's logs into a
    register of who is being reported on.

    Asserted as an absence because that is how it would be lost: somebody adds
    a display name so the model can say "Timur has been sleeping badly", which
    reads better and is a disclosure.
    """

    subject = await db_session.get(HealthSubject, legacy_owner_roots.subject_id)
    owner = await db_session.get(User, legacy_owner_roots.user_id)
    await db_session.commit()

    prompt = digest_service.build_prompt(
        await _compose_for(db_session, legacy_owner_roots.subject_id)
    )

    identifiers = {
        "the subject's display name": subject.display_name,
        "the owner's username": owner.username,
        "the subject's row id": str(subject.id),
        "the owner's row id": str(owner.id),
    }
    leaked = [
        what
        for what, value in identifiers.items()
        if value and str(value) in prompt
    ]
    assert not leaked, f"the prompt names the person: {leaked}"


# ── The whole-lake export ────────────────────────────────────────────────────


async def test_the_llm_export_carries_one_persons_lake_and_no_others(
    db_session, legacy_owner_roots
):
    """``export_llm`` had twenty-two selects and not one subject among them.

    Written when the installation held one person, correct then, and a
    cross-subject export the moment it held two — in the worst possible shape,
    because the result is a single LLM-ready document of everybody's record.
    Both callers made it worse by resolving a subject and then not passing it:
    the MCP ``export_everything`` tool, and the ``/settings/export-llm``
    download in the browser.
    """

    from vitals.services import data_portability_service

    other = await _second_person(db_session)
    db_session.add(
        WeightLog(
            subject_id=legacy_owner_roots.subject_id,
            domain=Domain.WEIGHT.value,
            source=Source.MANUAL.value,
            date=DAY,
            weight_kg=72.5,
        )
    )
    await db_session.commit()

    mine = await data_portability_service.export_llm(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    found = _leaks(str(mine))
    assert not found, f"the export carries another person's record: {found}"

    # The mirror, so this cannot pass by exporting nothing at all.
    theirs = await data_portability_service.export_llm(
        db_session, subject_id=other.id
    )
    assert _leaks(str(theirs)), (
        "exporting the second person's record returned none of their data — the "
        "assertion above is passing on an empty document"
    )


async def test_the_export_refuses_to_run_without_a_subject(
    db_session, legacy_owner_roots
):
    """No default, deliberately.

    An omittable scope is the shape ``vitals/legacy_scope.py`` exists to keep
    out of this codebase, and this function is the reason why: both of its
    callers had a subject in hand and neither passed one, which a default
    parameter would have hidden forever.
    """

    from vitals.services import data_portability_service

    for absent in (None, "not-a-uuid"):
        with pytest.raises(data_portability_service.PortabilityError):
            await data_portability_service.export_llm(db_session, subject_id=absent)
    del legacy_owner_roots
