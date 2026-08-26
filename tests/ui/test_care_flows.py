"""The care team, and the patient who is in the room with it."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_a_doctor_and_a_patient_hold_a_conversation(sign_in):
    """Both directions, and the order it reads in.

    Order is asserted because it is the thing that broke: two messages written
    in one transaction shared a timestamp, the thread fell back to a
    random-UUID tiebreak, and the patient's reply rendered above the doctor's
    message it was answering.
    """

    first_message = "Ferritin is below range. Repeat fasting in two weeks."
    doctor = sign_in("dr-ivanov")
    doctor.record_of("timur").open_conversation().say(first_message)

    patient = sign_in("timur")
    mine = patient.my_conversations()
    assert mine.says("Conversation with Dr Ivanov"), (
        "the patient cannot identify who the stable conversation is with"
    )
    conversation = mine.open_stable_conversation()
    assert conversation.says("Ferritin is below range"), (
        "the patient cannot read what was said about them"
    )
    conversation.say("Understood, I will book it.")
    assert conversation.said_in_order(
        "Ferritin is below range", "Understood, I will book it."
    ), "a reply renders above the message it answers"


def test_the_patient_reaches_their_own_conversations_without_an_id(sign_in):
    """They have no way to know their subject id, so ``/messages`` is the door."""

    landed = sign_in("timur").my_conversations()
    assert landed.status == 200


def test_a_trainer_cannot_open_a_doctors_patient(sign_in):
    sign_in("coach-orlov").is_refused_from("the record of patient01", status=404)


def test_a_revoked_consent_closes_the_record(sign_in):
    """The relationship is still there; what it authorized is not."""

    coach = sign_in("coach-sokol")
    assert coach.roster().status == 200, "the roster broke for a revoked consent"
    coach.is_refused_from("the record of patient05", status=404)


def test_an_empty_roster_says_so_rather_than_breaking(sign_in):
    roster = sign_in("dr-petrova").roster()
    assert roster.status == 200
    assert roster.text.strip(), "the empty roster rendered nothing at all"


def test_a_doctor_sees_a_record_they_hold(sign_in):
    record = sign_in("dr-ivanov").record_of("timur")
    assert record.status == 200
    assert record.says("Timur")
