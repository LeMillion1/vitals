"""Authenticated current-device browser notification contracts."""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select

from vitals.enums import (
    ProfessionalKind,
    ProfessionalVerificationStatus,
    UserRoleName,
    UserStatus,
)
from vitals.models.identity import User, UserRole
from vitals.models.professional import ProfessionalProfile
from vitals.models.web_push import WebPushSubscription
from vitals.services.credentials import vault
from vitals.services.notifications import web_push_config
from vitals.services.notifications import web_push_subscriptions

ROOT = Path(__file__).resolve().parent.parent


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _configure(monkeypatch, *, scalar: int = 42) -> str:
    private = ec.derive_private_key(scalar, ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    monkeypatch.setenv(web_push_config.ENABLED_ENV, "true")
    monkeypatch.setenv(web_push_config.PUBLIC_KEY_ENV, _b64(public))
    monkeypatch.setenv(
        web_push_config.PRIVATE_KEY_ENV, _b64(scalar.to_bytes(32, "big"))
    )
    monkeypatch.setenv(web_push_config.SUBJECT_ENV, "mailto:vitals@example.test")
    return _b64(public)


def _subscription() -> dict[str, object]:
    client_key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "endpoint": "https://fcm.googleapis.com/fcm/send/browser-api-test",
        "keys": {
            "p256dh": _b64(client_key),
            "auth": _b64(b"a" * 16),
        },
    }


def test_vapid_configuration_requires_a_matching_complete_key_pair(monkeypatch):
    public_key = _configure(monkeypatch)
    config = web_push_config.load_config()
    assert config is not None
    assert config.public_key == public_key
    assert config.subject == "mailto:vitals@example.test"

    _configure(monkeypatch, scalar=43)
    monkeypatch.setenv(web_push_config.PUBLIC_KEY_ENV, public_key)
    try:
        web_push_config.load_config()
    except web_push_config.WebPushConfigurationError as exc:
        assert "do not match" in str(exc)
    else:  # pragma: no cover - the assertion above is the contract
        raise AssertionError("a mismatched VAPID key pair was accepted")


async def test_configuration_is_authenticated_and_fail_closed(
    client, legacy_owner_roots, monkeypatch
):
    monkeypatch.delenv(web_push_config.ENABLED_ENV, raising=False)
    anonymous = await client.get("/account/notifications/configuration")
    assert anonymous.status_code == 401

    login = await client.post(
        "/login", data={"username": "tester", "password": "password"}
    )
    assert login.status_code == 303

    disabled = await client.get("/account/notifications/configuration")
    assert disabled.status_code == 200
    assert disabled.json() == {"available": False}

    public_key = _configure(monkeypatch)
    enabled = await client.get("/account/notifications/configuration")
    assert enabled.json() == {
        "available": True,
        "applicationServerKey": public_key,
    }
    assert "private" not in enabled.text.lower()


async def test_current_device_can_register_check_and_revoke_without_identifiers(
    auth_client, db_session, monkeypatch
):
    _configure(monkeypatch)
    payload = _subscription()

    registered = await auth_client.post(
        "/account/notifications/subscription", json=payload
    )
    assert registered.status_code == 200
    assert registered.json() == {"enabled": True}
    assert payload["endpoint"] not in registered.text

    row = await db_session.scalar(select(WebPushSubscription))
    assert row is not None and row.ciphertext is not None

    current = await auth_client.post(
        "/account/notifications/status", json={"endpoint": payload["endpoint"]}
    )
    assert current.json() == {"enabled": True}

    revoked = await auth_client.post(
        "/account/notifications/subscription/revoke",
        json={"endpoint": payload["endpoint"]},
    )
    assert revoked.status_code == 200
    assert revoked.json() == {"enabled": False}
    await db_session.refresh(row)
    assert row.revoked_at is not None
    assert row.ciphertext is None


async def test_subscription_api_rejects_cross_origin_invalid_and_oversized_input(
    auth_client, monkeypatch
):
    _configure(monkeypatch)
    cross_origin = await auth_client.post(
        "/account/notifications/subscription",
        json=_subscription(),
        headers={"Origin": "https://evil.example"},
    )
    assert cross_origin.status_code == 403

    invalid = await auth_client.post(
        "/account/notifications/subscription",
        json={"endpoint": "https://internal.example/push", "keys": {}},
    )
    assert invalid.status_code == 400

    oversized = await auth_client.post(
        "/account/notifications/status",
        content=b"{" + b" " * 9000 + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413

    wrong_media = await auth_client.post(
        "/account/notifications/status",
        content=b'{}',
        headers={"Content-Type": "text/plain"},
    )
    assert wrong_media.status_code == 415


async def test_registration_fails_closed_without_complete_server_secrets(
    auth_client, monkeypatch
):
    _configure(monkeypatch)
    monkeypatch.delenv(web_push_config.PRIVATE_KEY_ENV)
    response = await auth_client.post(
        "/account/notifications/subscription", json=_subscription()
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "notifications_unavailable"

    _configure(monkeypatch)
    monkeypatch.setattr(vault, "is_available", lambda: False)
    response = await auth_client.post(
        "/account/notifications/subscription", json=_subscription()
    )
    assert response.status_code == 503


async def test_shared_endpoint_and_concurrent_unique_race_are_generic_conflicts(
    auth_client, db_session, legacy_owner_roots, monkeypatch
):
    _configure(monkeypatch)
    payload = _subscription()
    other = User(
        username="push-api-other",
        normalized_username="push-api-other",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(other)
    await db_session.flush()
    await web_push_subscriptions.register(
        db_session,
        user_id=other.id,
        endpoint=payload["endpoint"],
        p256dh=payload["keys"]["p256dh"],
        auth=payload["keys"]["auth"],
    )
    await db_session.commit()

    conflict = await auth_client.post(
        "/account/notifications/subscription", json=payload
    )
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "device_linked_elsewhere"}
    assert payload["endpoint"] not in conflict.text

    async def _race(*_args, **_kwargs):
        raise IntegrityError("insert", {}, RuntimeError("unique"))

    monkeypatch.setattr(web_push_subscriptions, "register", _race)
    race = await auth_client.post(
        "/account/notifications/subscription", json=_subscription()
    )
    assert race.status_code == 409
    assert race.json() == {"detail": "device_linked_elsewhere"}


async def test_care_inboxes_offer_the_same_explicit_device_control(
    auth_client, db_session, legacy_owner_roots
):
    patient_page = await auth_client.get("/messages", follow_redirects=True)
    assert patient_page.status_code == 200
    assert patient_page.text.count("data-web-push-card") == 1
    assert "data-web-push-enable" in patient_page.text

    from vitals.utils.timeutils import now_utc
    from web.auth import create_session
    from web.config import SESSION_COOKIE

    doctor = User(
        username="push-roster-doctor",
        normalized_username="push-roster-doctor",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(doctor)
    await db_session.flush()
    db_session.add_all(
        [
            UserRole(user_id=doctor.id, role=UserRoleName.DOCTOR.value),
            ProfessionalProfile(
                user_id=doctor.id,
                kind=ProfessionalKind.DOCTOR.value,
                verification_status=ProfessionalVerificationStatus.VERIFIED.value,
                display_name="Dr Push Roster",
                verified_at=now_utc(),
                verified_by_user_id=legacy_owner_roots.user_id,
            ),
        ]
    )
    await db_session.commit()
    auth_client.cookies.set(SESSION_COOKIE, create_session(doctor.username))

    roster_page = await auth_client.get("/care")
    assert roster_page.status_code == 200
    assert roster_page.text.count("data-web-push-card") == 1


def test_notification_permission_is_requested_only_inside_the_enable_handler():
    script = (ROOT / "web/static/web_push.js").read_text(encoding="utf-8")
    assert script.count("Notification.requestPermission()") == 1
    click_handler = script.index("addEventListener('click'")
    request_permission = script.index("Notification.requestPermission()")
    # The permission call lives in enable(), which is referenced only by the
    # click handler. There is no load-time call or prompt-on-load fallback.
    assert "enable(card, config)" in script[click_handler:]
    assert request_permission < click_handler
    assert "await enable(card, config)" not in script[:click_handler]


def test_button_visibility_cannot_be_overridden_by_the_ghost_button_css():
    partial = (ROOT / "web/templates/partials/web_push_card.html").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "web/static/web_push.js").read_text(encoding="utf-8")
    assert partial.count('style="display: none"') == 2
    assert "enable.style.display = settings.enable ? '' : 'none'" in script
    assert "disable.style.display = settings.disable ? '' : 'none'" in script


def test_locale_is_shared_only_after_current_account_proves_device_ownership():
    script = (ROOT / "web/static/web_push.js").read_text(encoding="utf-8")
    assert script.count("await rememberOwnedLocale(registration)") == 2
    status_proof = script.index("if (serverState.enabled)")
    status_sync = script.index("await rememberOwnedLocale(registration)")
    conflict = script.index("setState(card, 'conflict'")
    assert status_proof < status_sync < conflict

    registration_proof = script.index("await jsonPost('/subscription', {")
    registration_sync = script.rindex("await rememberOwnedLocale(registration)")
    assert registration_proof < registration_sync
    assert "kind: 'set_locale'" in script
    assert "document.documentElement.lang === 'ru' ? 'ru' : 'en'" in script
