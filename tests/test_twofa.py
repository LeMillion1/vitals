"""Two-factor auth: RFC 6238 conformance, enrolment state machine, replay.

The code arithmetic is checked against the published RFC 6238 vectors rather
than against itself — a TOTP implementation that only agrees with its own
``code_at`` is exactly the one that silently disagrees with every authenticator
app on the phone.
"""
from __future__ import annotations

import time

import pytest

from vitals.models.app_settings import AppSetting
from vitals.operations.ownership import portability_v1
from vitals.services import data_portability_service
from vitals.services.authentication import legacy_two_factor as twofa_service
from vitals.services.data_portability_service import _is_secret_setting_key
from web.config import PENDING_2FA_COOKIE, SESSION_COOKIE

# RFC 6238 Appendix B, SHA-1: the shared secret is the ASCII string
# "12345678901234567890" in base32. The published codes are 8 digits; a 6-digit
# TOTP is the same truncated integer mod 10**6, i.e. their last six digits.
# Spelled out rather than imported from conftest: a `tests` package in
# site-packages can shadow `tests.conftest` and break the import (same reason the
# conftest fixtures reference their own globals directly).
TEST_USERNAME = "tester"
TEST_PASSWORD = "password"

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
RFC_VECTORS = [
    (59, "287082"),
    (1111111109, "081804"),
    (1111111111, "050471"),
    (1234567890, "005924"),
    (2000000000, "279037"),
]


# ── Code arithmetic ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("at, expected", RFC_VECTORS)
def test_matches_rfc6238_vectors(at, expected):
    assert twofa_service.code_at(RFC_SECRET, at // twofa_service.STEP_SECONDS) == expected


@pytest.mark.parametrize("at, expected", RFC_VECTORS)
def test_verify_accepts_the_current_code(at, expected):
    assert twofa_service.verify_code(RFC_SECRET, expected, now=at) is not None


def test_verify_returns_the_matched_step_for_replay_tracking():
    at = 1234567890
    step = twofa_service.verify_code(RFC_SECRET, "005924", now=at)
    assert step == at // twofa_service.STEP_SECONDS


def test_neighbouring_steps_are_accepted_for_clock_drift():
    """A code read at second 29 and typed at second 31 must still work."""
    at = 1234567890
    previous = twofa_service.code_at(RFC_SECRET, at // 30 - 1)
    following = twofa_service.code_at(RFC_SECRET, at // 30 + 1)
    assert twofa_service.verify_code(RFC_SECRET, previous, now=at) is not None
    assert twofa_service.verify_code(RFC_SECRET, following, now=at) is not None


def test_codes_further_out_are_rejected():
    at = 1234567890
    stale = twofa_service.code_at(RFC_SECRET, at // 30 - 2)
    assert twofa_service.verify_code(RFC_SECRET, stale, now=at) is None


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "12 34 56", None])
def test_malformed_codes_are_rejected(code):
    assert twofa_service.verify_code(RFC_SECRET, code, now=59) is None


def test_no_secret_never_verifies():
    assert twofa_service.verify_code(None, "287082", now=59) is None
    assert twofa_service.verify_code("", "287082", now=59) is None


def test_corrupt_secret_reads_as_wrong_code_not_a_crash():
    """A mangled stored secret must not 500 the login form."""
    assert twofa_service.verify_code("not-valid-base32!!", "287082", now=59) is None


def test_generated_secret_round_trips():
    secret = twofa_service.generate_secret()
    assert twofa_service.verify_code(secret, twofa_service.code_at(secret, 1), now=59) is not None


def test_provisioning_uri_carries_the_parameters_an_app_needs():
    uri = twofa_service.provisioning_uri(RFC_SECRET, account="timur")
    assert uri.startswith("otpauth://totp/Vitals%3Atimur?")
    assert f"secret={RFC_SECRET}" in uri
    assert "issuer=Vitals" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


def test_formatted_secret_still_decodes():
    """The grouped-for-reading form is what a user retypes by hand."""
    secret = twofa_service.generate_secret()
    grouped = twofa_service.format_secret(secret)
    assert " " in grouped
    assert twofa_service.verify_code(grouped, twofa_service.code_at(secret, 1), now=59) is not None


# ── The secret must never leave in a backup ───────────────────────────────────
def test_settings_key_is_treated_as_a_secret_by_the_exporter():
    """Pins the key name: renaming it away from "secret" would quietly start
    writing the TOTP seed into every downloaded backup."""
    assert _is_secret_setting_key(twofa_service.SETTINGS_KEY) is True


async def test_2fa_survives_a_backup_round_trip(db_session):
    """The export deliberately omits the secret, and the import wipes every table
    before reloading — so without the mirroring guard, restoring any legitimate
    backup switched the second factor off with nothing on screen to say so."""
    await _enable(db_session)
    snapshot = await data_portability_service.export_full(db_session)
    assert not [
        r for r in snapshot["app_settings"] if r["key"] == twofa_service.SETTINGS_KEY
    ], "the secret must not be in the file"

    await portability_v1.import_full(db_session, snapshot)
    assert (await twofa_service.get_state(db_session)).enabled is True


async def test_a_backup_cannot_plant_a_2fa_secret(db_session):
    """The other half of the mirror: an uploaded file must not be a way to write a
    credential either, or /settings/import becomes a route around the code check
    that turning 2FA off requires."""
    secret = await _enable(db_session)
    snapshot = await data_portability_service.export_full(db_session)
    snapshot["app_settings"].append(
        {"key": twofa_service.SETTINGS_KEY, "value": {"secret": RFC_SECRET, "confirmed": True}}
    )

    await portability_v1.import_full(db_session, snapshot)
    state = await twofa_service.get_state(db_session)
    assert state.enabled is True
    assert state.secret == secret, "the owner's secret must survive untouched"


async def test_a_restore_onto_an_empty_db_leaves_2fa_off(db_session):
    """Moving to a fresh server: there is nothing to carry across, because the
    secret is deliberately not in the file. It must fail toward "password still
    works" rather than locking the owner out of their own dashboard."""
    snapshot = await data_portability_service.export_full(db_session)
    await portability_v1.import_full(db_session, snapshot)
    assert (await twofa_service.get_state(db_session)).enabled is False


# ── Enrolment state machine ───────────────────────────────────────────────────
async def test_off_by_default(db_session):
    state = await twofa_service.get_state(db_session)
    assert state.enabled is False
    assert state.pending is False


async def test_starting_enrolment_does_not_turn_it_on(db_session):
    secret = await twofa_service.start_enrolment(db_session)
    state = await twofa_service.get_state(db_session)
    assert state.pending is True
    assert state.enabled is False
    assert state.secret == secret


async def test_wrong_code_does_not_complete_enrolment(db_session):
    await twofa_service.start_enrolment(db_session)
    assert await twofa_service.confirm_enrolment(db_session, "000000") is False
    assert (await twofa_service.get_state(db_session)).enabled is False


async def test_correct_code_turns_it_on(db_session):
    secret = await twofa_service.start_enrolment(db_session)
    assert await twofa_service.confirm_enrolment(db_session, _now_code(secret)) is True
    assert (await twofa_service.get_state(db_session)).enabled is True


async def test_confirming_without_starting_is_a_no_op(db_session):
    assert await twofa_service.confirm_enrolment(db_session, "000000") is False


async def test_disable_needs_a_valid_code(db_session):
    secret = await _enable(db_session)
    assert await twofa_service.disable(db_session, "000000") is False
    assert (await twofa_service.get_state(db_session)).enabled is True

    assert await twofa_service.disable(db_session, _now_code(secret)) is True
    assert (await twofa_service.get_state(db_session)).enabled is False


async def test_pending_enrolment_cancels_without_a_code(db_session):
    """An unconfirmed secret grants nothing, so cancelling needs no proof."""
    await twofa_service.start_enrolment(db_session)
    assert await twofa_service.disable(db_session, "") is True
    assert (await twofa_service.get_state(db_session)).pending is False


async def test_restarting_enrolment_replaces_the_pending_secret(db_session):
    first = await twofa_service.start_enrolment(db_session)
    second = await twofa_service.start_enrolment(db_session)
    assert first != second
    assert (await twofa_service.get_state(db_session)).secret == second


async def test_corrupt_stored_value_reads_as_off(db_session):
    db_session.add(AppSetting(key=twofa_service.SETTINGS_KEY, value={"nope": True}))
    await db_session.flush()
    state = await twofa_service.get_state(db_session)
    assert state.enabled is False
    assert state.pending is False


# ── Replay ────────────────────────────────────────────────────────────────────
async def test_a_time_step_can_only_be_spent_once(redis):
    step = 58_000_000
    assert await twofa_service.consume_step(redis, step) is True
    assert await twofa_service.consume_step(redis, step) is False
    assert await twofa_service.consume_step(redis, step + 1) is True


async def test_replay_check_fails_open_without_redis():
    """A dead cache must not lock the owner out of their own dashboard."""
    assert await twofa_service.consume_step(None, 1) is True


# ── The login flow itself ─────────────────────────────────────────────────────
async def test_login_without_2fa_signs_straight_in(client):
    """The default path must stay a single step."""
    r = await client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert SESSION_COOKIE in r.cookies


async def test_password_alone_grants_no_session_when_2fa_is_on(client, db_session):
    await _enable(db_session)
    await db_session.commit()

    r = await client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login/2fa")
    # The whole point: the password step hands over a pending handle and nothing
    # that opens a single page of health data.
    assert SESSION_COOKIE not in r.cookies
    assert PENDING_2FA_COOKIE in r.cookies


async def test_the_code_completes_the_login(client, db_session):
    secret = await _enable(db_session)
    await db_session.commit()

    await client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    r = await client.post("/login/2fa", data={"code": _now_code(secret), "next": "/weight"})
    assert r.status_code == 303
    assert r.headers["location"] == "/weight"
    assert SESSION_COOKIE in r.cookies


async def test_a_wrong_code_does_not_complete_the_login(client, db_session):
    await _enable(db_session)
    await db_session.commit()

    await client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    r = await client.post("/login/2fa", data={"code": "000000"})
    assert r.status_code == 200  # the form, re-rendered with an error
    assert SESSION_COOKIE not in r.cookies


async def test_the_code_step_is_closed_without_the_password_step(client, db_session):
    """A code alone, with no pending handle, must open nothing."""
    secret = await _enable(db_session)
    await db_session.commit()

    r = await client.post("/login/2fa", data={"code": _now_code(secret)})
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert SESSION_COOKIE not in r.cookies


async def test_the_code_page_bounces_to_login_without_a_pending_handle(client):
    r = await client.get("/login/2fa")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


async def test_a_code_cannot_be_used_twice(client, db_session):
    """Replay: the same six digits must not walk a second person in behind you."""
    secret = await _enable(db_session)
    await db_session.commit()
    code = _now_code(secret)

    await client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    first = await client.post("/login/2fa", data={"code": code})
    assert first.status_code == 303

    await client.post("/login", data={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    second = await client.post("/login/2fa", data={"code": code})
    assert second.status_code == 200
    assert SESSION_COOKIE not in second.cookies


# ── Helpers ───────────────────────────────────────────────────────────────────
def _now_code(secret: str) -> str:
    return twofa_service.code_at(secret, int(time.time()) // twofa_service.STEP_SECONDS)


async def _enable(db_session) -> str:
    secret = await twofa_service.start_enrolment(db_session)
    await twofa_service.confirm_enrolment(db_session, _now_code(secret))
    return secret
