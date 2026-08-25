"""Platform support reaching a record, from both sides of the decision.

The service tests next door prove the rules. These prove the product: that a
patient can see what is being asked and say no, that a yes is visible to them
everywhere afterwards, and that the access it grants is one an administrator can
actually use. Each of those was broken at some point in this branch while every
rule underneath it was right.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

REASON = "Investigating a failed lab import, ticket SUP-4471."


def test_a_patient_is_asked_before_anything_opens(sign_in):
    """Asking is not access, and the patient is told in the asker's own words."""

    admin = sign_in("admin")
    admin.support_console().ask_for(
        record="Patient 01", reason=REASON, domains=("labs",)
    )

    patient = sign_in("patient01")
    history = patient.access_history()
    assert history.says(REASON), "the patient is not shown why they are being asked"
    assert history.says("admin"), "the patient is not told who is asking"
    assert history.has_a_pending_ask, "the ask offers the patient no decision"

    # Nothing has opened, and no banner claims otherwise.
    assert not admin.support_console().holds_a_grant
    dashboard = patient.visit("/weight")
    assert not dashboard.says("grant you approved"), "a banner before any approval"


def test_an_approved_grant_is_visible_everywhere_and_endable_from_anywhere(sign_in):
    """The banner is the consent staying informed after the click.

    A grant somebody has to go hunting through settings to discover is not
    meaningfully agreed to, so it is drawn on every page for as long as it lasts
    and carries the link that ends it.
    """

    admin = sign_in("admin")
    admin.support_console().ask_for(
        record="Patient 01", reason=REASON, domains=("labs",)
    )

    patient = sign_in("patient01")
    patient.access_history().allow()

    elsewhere = patient.visit("/weight")
    assert elsewhere.offers("/settings/access"), (
        "a live grant draws no banner on an ordinary page"
    )

    patient.access_history().stop_the_live_grant()
    after = patient.visit("/weight")
    assert not after.offers("/settings/access/grant/"), "the banner survived the revoke"


def test_the_granted_record_opens_and_shows_only_what_was_granted(sign_in):
    """The link the console offers, followed.

    It answered 404 for most of this PR: ``/care`` is the professional's
    surface and a support grant was not a basis for it, so a read grant
    authorized reads nobody could perform.
    """

    admin = sign_in("admin")
    admin.support_console().ask_for(
        record="Patient 01", reason=REASON, domains=("labs",)
    )
    sign_in("patient01").access_history().allow()

    record = admin.support_console().open_the_record()
    assert record.says("Platform support"), "the basis is not named"
    assert not record.says("(Doctor)"), "support is described as a doctor"
    assert "Nutrition" in record.withheld_line, (
        f"an ungranted domain is not withheld: {record.withheld_line!r}"
    )
    assert not record.offers_the_note_form, "a read grant is offered a write form"


def test_a_revoked_grant_shuts_the_record_immediately(sign_in):
    admin = sign_in("admin")
    admin.support_console().ask_for(
        record="Patient 01", reason=REASON, domains=("labs",)
    )
    patient = sign_in("patient01")
    patient.access_history().allow()
    assert admin.support_console().holds_a_grant

    patient.access_history().stop_the_live_grant()
    admin.is_refused_from("the record of patient01", status=404)


def test_an_administrator_can_hand_a_grant_back(sign_in):
    """Rather than wait for it to lapse. Either side may end it."""

    admin = sign_in("admin")
    admin.support_console().ask_for(
        record="Patient 01", reason=REASON, domains=("labs",)
    )
    sign_in("patient01").access_history().allow()

    console = admin.support_console()
    assert console.holds_a_grant
    console.hand_the_grant_back()
    assert not admin.support_console().holds_a_grant
    admin.is_refused_from("the record of patient01", status=404)


def test_an_ask_can_be_taken_back_before_it_is_answered(sign_in):
    admin = sign_in("admin")
    console = admin.support_console().ask_for(
        record="Patient 02", reason=REASON, domains=("weight",)
    )
    assert console.has_an_unanswered_ask
    console.withdraw_the_ask()
    assert not admin.support_console().has_an_unanswered_ask


def test_a_refusal_is_kept_where_the_patient_can_find_it(sign_in):
    """A history of only the yeses cannot answer "has anybody been reading me"."""

    admin = sign_in("admin")
    admin.support_console().ask_for(
        record="Patient 01", reason=REASON, domains=("labs",)
    )
    patient = sign_in("patient01")
    patient.access_history().refuse()

    history = patient.access_history()
    assert history.says(REASON), "a refused ask vanished from the history"
    assert not history.has_a_pending_ask, "a refused ask is still offering a decision"
    assert not admin.support_console().holds_a_grant


def test_one_patient_cannot_see_an_ask_about_another(sign_in):
    admin = sign_in("admin")
    admin.support_console().ask_for(
        record="Patient 02",
        reason="A sentence patient01 must never read.",
        domains=("weight",),
    )
    onlooker = sign_in("patient01").access_history()
    assert not onlooker.says("must never read"), (
        "one patient can read a support request about another"
    )


@pytest.mark.parametrize("who", ["dr-ivanov", "timur", "patient01"])
def test_the_console_is_for_administrators_only(sign_in, who):
    sign_in(who).is_refused_from("the support console", status=403)


def test_a_professional_holding_patients_is_sent_to_their_roster(sign_in):
    """``/settings/access`` is about *my* record, and a trainer keeps none.

    Redirected rather than refused: their work is on the roster, and a refusal
    page would be a dead end with the answer written on it.
    """

    coach = sign_in("coach-sokol")
    landed = coach.access_history()
    assert landed.status == 200, "the trainer was stranded"
    assert "/care" in landed.url, f"not sent to the roster: {landed.url}"


def test_a_professional_holding_nobody_gets_a_page_with_a_way_out(sign_in):
    landed = sign_in("dr-petrova").access_history()
    assert landed.status == 200
    assert "/care" in landed.url, f"not sent to the professional home: {landed.url}"
    assert landed.text.strip(), "the empty professional home rendered no guidance"
