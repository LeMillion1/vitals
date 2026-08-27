"""Account ownership and secret-handling contracts for browser push endpoints."""

from __future__ import annotations

import base64
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from vitals.enums import UserStatus
from vitals.models.identity import User
from vitals.models.web_push import WebPushSubscription
from vitals.services.credentials import vault
from vitals.services.identity_service import change_user_status
from vitals.services.notifications import web_push_subscriptions as service


def _key(byte: int, size: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * size).decode().rstrip("=")


def _subscription(token: str = "device-a") -> dict[str, str]:
    public_key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return {
        "endpoint": f"https://fcm.googleapis.com/fcm/send/{token}",
        "p256dh": base64.urlsafe_b64encode(public_key).decode().rstrip("="),
        "auth": _key(7, 16),
    }


async def _other_user(db_session) -> User:
    user = User(
        username="push-other",
        normalized_username="push-other",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def test_subscription_secret_is_encrypted_and_account_owned(
    db_session, legacy_owner_roots
):
    values = _subscription()
    row = await service.register(
        db_session, user_id=legacy_owner_roots.user_id, **values
    )
    await db_session.commit()

    stored = await db_session.get(WebPushSubscription, row.id)
    blob = bytes(stored.ciphertext)
    assert values["endpoint"].encode() not in blob
    assert values["p256dh"].encode() not in blob
    assert values["auth"].encode() not in blob
    assert stored.endpoint_hash == service.endpoint_hash(values["endpoint"])

    loaded = await service.load_secret(
        db_session,
        subscription_id=row.id,
        user_id=legacy_owner_roots.user_id,
    )
    assert loaded is not None
    assert loaded.as_webpush_dict() == {
        "endpoint": values["endpoint"],
        "keys": {"p256dh": values["p256dh"], "auth": values["auth"]},
    }


async def test_an_account_cannot_read_or_take_another_accounts_endpoint(
    db_session, legacy_owner_roots
):
    values = _subscription()
    row = await service.register(
        db_session, user_id=legacy_owner_roots.user_id, **values
    )
    other = await _other_user(db_session)

    assert (
        await service.load_secret(
            db_session, subscription_id=row.id, user_id=other.id
        )
        is None
    )
    with pytest.raises(service.SubscriptionBelongsToAnotherAccount):
        await service.register(db_session, user_id=other.id, **values)


async def test_registering_the_same_device_refreshes_one_row(
    db_session, legacy_owner_roots
):
    values = _subscription()
    first = await service.register(
        db_session, user_id=legacy_owner_roots.user_id, **values
    )
    await service.revoke_endpoint(
        db_session,
        user_id=legacy_owner_roots.user_id,
        endpoint=values["endpoint"],
    )
    refreshed = await service.register(
        db_session,
        user_id=legacy_owner_roots.user_id,
        endpoint=values["endpoint"],
        p256dh=values["p256dh"],
        auth=_key(8, 16),
    )
    await db_session.flush()

    assert refreshed.id == first.id
    assert refreshed.revoked_at is None
    assert len((await db_session.scalars(select(WebPushSubscription))).all()) == 1
    loaded = await service.load_secret(
        db_session,
        subscription_id=first.id,
        user_id=legacy_owner_roots.user_id,
    )
    assert loaded is not None and loaded.auth == _key(8, 16)


async def test_revoked_subscription_is_not_loadable(db_session, legacy_owner_roots):
    values = _subscription()
    row = await service.register(
        db_session, user_id=legacy_owner_roots.user_id, **values
    )
    assert await service.revoke_endpoint(
        db_session,
        user_id=legacy_owner_roots.user_id,
        endpoint=values["endpoint"],
    )
    assert not await service.revoke_endpoint(
        db_session,
        user_id=legacy_owner_roots.user_id,
        endpoint=values["endpoint"],
    )
    assert (
        await service.load_secret(
            db_session,
            subscription_id=row.id,
            user_id=legacy_owner_roots.user_id,
        )
        is None
    )
    await db_session.refresh(row)
    assert row.ciphertext is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint", "http://fcm.googleapis.com/fcm/send/x"),
        ("endpoint", "https://127.0.0.1/push"),
        ("endpoint", "https://fcm.googleapis.com.evil.test/push"),
        ("endpoint", "https://updates.push.services.mozilla.com:8443/push"),
        ("p256dh", _key(4, 64)),
        ("p256dh", _key(3, 65)),
        (
            "p256dh",
            base64.urlsafe_b64encode(b"\x04" + b"\x00" * 64)
            .decode()
            .rstrip("="),
        ),
        ("auth", _key(7, 15)),
    ],
)
async def test_malformed_or_ssrf_capable_subscriptions_are_refused(
    db_session, legacy_owner_roots, field, value
):
    values = _subscription()
    values[field] = value
    with pytest.raises(service.InvalidWebPushSubscription):
        await service.register(
            db_session, user_id=legacy_owner_roots.user_id, **values
        )


async def test_no_credential_key_fails_closed_without_plaintext(
    db_session, legacy_owner_roots, monkeypatch
):
    monkeypatch.delenv(vault.CREDENTIAL_KEY_ENV, raising=False)
    with pytest.raises(vault.CredentialVaultUnavailable):
        await service.register(
            db_session,
            user_id=legacy_owner_roots.user_id,
            **_subscription(),
        )
    assert await db_session.scalar(select(WebPushSubscription.id)) is None


async def test_active_device_count_is_bounded(db_session, legacy_owner_roots):
    for index in range(service.MAX_ACTIVE_SUBSCRIPTIONS_PER_USER):
        await service.register(
            db_session,
            user_id=legacy_owner_roots.user_id,
            **_subscription(f"device-{index}"),
        )

    with pytest.raises(service.TooManyWebPushSubscriptions):
        await service.register(
            db_session,
            user_id=legacy_owner_roots.user_id,
            **_subscription("one-too-many"),
        )


async def test_suspending_an_account_erases_its_delivery_credentials(
    db_session, legacy_owner_roots
):
    user = await _other_user(db_session)
    row = await service.register(
        db_session, user_id=user.id, **_subscription("suspended-device")
    )

    await change_user_status(
        db_session,
        user_id=user.id,
        new_status=UserStatus.SUSPENDED,
        actor_user_id=legacy_owner_roots.user_id,
    )
    await db_session.refresh(row)

    assert row.revoked_at is not None
    assert row.ciphertext is None
    assert (
        await service.load_secret(
            db_session, subscription_id=row.id, user_id=user.id
        )
        is None
    )


async def test_tampered_subscription_fails_authenticated_decryption(
    db_session, legacy_owner_roots
):
    row = await service.register(
        db_session,
        user_id=legacy_owner_roots.user_id,
        **_subscription(),
    )
    await db_session.commit()
    tampered = bytearray(bytes(row.ciphertext))
    tampered[-1] ^= 1
    row.ciphertext = bytes(tampered)
    await db_session.commit()

    with pytest.raises(service.CorruptWebPushSubscription):
        await service.load_secret(
            db_session,
            subscription_id=row.id,
            user_id=legacy_owner_roots.user_id,
        )
