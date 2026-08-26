"""What each role can reach, and what it is told when it cannot."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_a_superadmin_on_a_personal_page_gets_a_page_not_a_sentence(sign_in):
    """They keep no record of their own, and can open exactly one address.

    The refusal used to be one unstyled line on a white page — correct
    information and a dead end, with the name of the section they wanted
    written out in prose and no way to reach it.
    """

    refusal = sign_in("admin").is_refused_from("the dashboard", status=409)
    refusal.assert_is_a_page_with_a_way_out()
    assert refusal.offers("/care"), (
        "the refusal does not point at the one page this account can open"
    )


def test_the_console_link_is_offered_to_an_administrator(sign_in):
    """The rail exposes one platform hub to an administrator."""

    admin = sign_in("admin")
    assert admin.roster().offers("/settings/platform"), (
        "the administrator has no link to the platform hub"
    )


@pytest.mark.parametrize("who", ["timur", "dr-ivanov", "patient01"])
def test_nobody_else_is_offered_a_console_they_cannot_open(sign_in, who):
    """A link that answers 403 is worse than no link."""

    assert not sign_in(who).visit("/weight").offers("/settings/platform")


def test_two_patients_keep_separate_profiles(sign_in):
    """The Navy body-fat estimate read one installation-wide height for
    everybody, so this is asserted from both sides rather than once."""

    first = sign_in("timur")
    second = sign_in("patient01")
    first.settings().set_height(191)
    second.settings().set_height(158)

    assert first.settings().height == "191", "one profile overwrote another"
    assert second.settings().height == "158", "the second profile did not persist"


@pytest.mark.parametrize(
    "path",
    [
        "/today",
        "/weight",
        "/nutrition",
        "/labs",
        "/settings/access",
        "/settings/care",
        "/messages",
        "/more",
    ],
)
def test_the_patients_pages_fit_a_phone(sign_in, path):
    """Overflow is collected by the fixture; this only has to open them.

    A page that scrolls sideways looks fine in a screenshot and is unusable in
    a hand, which is why nobody catches it by eye.
    """

    assert sign_in("timur", phone=True).visit(path).status == 200


@pytest.mark.parametrize("screen", ["roster", "record", "conversations"])
def test_the_doctors_pages_fit_a_phone(sign_in, screen):
    doctor = sign_in("dr-ivanov", phone=True)
    page = (
        doctor.roster()
        if screen == "roster"
        else doctor.record_of("timur")
        if screen == "record"
        else doctor.conversations_about("timur")
    )
    assert page.status == 200


def test_a_key_issued_on_the_settings_page_opens_the_external_api(sign_in):
    """The screen and the endpoint have to agree, which is the only thing that
    matters about a credential.

    One minted by a settings page and refused by the API — or the reverse — is
    the shape this branch keeps finding: each half correct, the product broken.
    The secret is read off the rendered page because that is its only
    appearance; nothing stores it, including the row it authenticates against.
    """

    import urllib.request

    patient = sign_in("timur")
    settings = patient.settings()
    secret = settings.external_keys.issue("Kitchen dashboard")

    request = urllib.request.Request(
        f"{patient.installation.base_url}/external/summary",
        headers={"Authorization": f"Bearer {secret}"},
    )
    with urllib.request.urlopen(request, timeout=10) as answer:
        assert answer.status == 200


def test_stopping_a_key_closes_it_immediately(sign_in):
    import urllib.error
    import urllib.request

    patient = sign_in("timur")
    settings = patient.settings()
    secret = settings.external_keys.issue("Kitchen dashboard")
    patient.settings().external_keys.stop_the_first()

    request = urllib.request.Request(
        f"{patient.installation.base_url}/external/summary",
        headers={"Authorization": f"Bearer {secret}"},
    )
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=10)
    assert refused.value.code in (401, 503)
