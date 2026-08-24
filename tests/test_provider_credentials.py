"""A Garmin account belongs to one patient, and so does everything around it.

``VITALS_GARMIN_EMAIL``/``_PASSWORD`` and ``VITALS_HEVY_API_KEY`` are one watch
and one workout account for the whole process. That is the shape that kept four
scheduled jobs from being run per subject: doing so would have written the
operator's own watch data into everybody else's record, which is an outage
turned into a disclosure.

The credential is the obvious half. The quiet half is everything the Garmin
client keeps beside it — the cached token session in Redis, the login breaker's
counters, the token store on disk — all of which were flat process-wide keys.
Two subjects sharing those means one person's session resuming as another's, and
one person's three failed logins pausing everybody else's sync for six hours.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from vitals.enums import (
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    UserStatus,
)
from vitals.models.credentials import IntegrationCredential
from vitals.models.identity import HealthSubject, User
from vitals.models.tenancy import IntegrationConnection
from vitals.services import credential_vault_service, provider_credentials_service


@pytest.fixture
async def second_patient(db_session, legacy_owner_roots):
    """Somebody who is not the installation owner, with their own roots."""

    from vitals.services.tenancy_bootstrap import bootstrap_legacy_resource_roots
    from vitals.services import rls_session

    user = User(
        username="second-athlete",
        normalized_username="second-athlete",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name="Second athlete",
        timezone="Europe/Chisinau",
    )
    db_session.add(subject)
    await db_session.flush()
    await bootstrap_legacy_resource_roots(db_session, subject_id=subject.id)
    await db_session.commit()
    db_session.info.pop(rls_session._SUBJECT_KEY, None)
    return subject.id


async def _garmin_connection_id(session, subject_id) -> uuid.UUID:
    return await session.scalar(
        select(IntegrationConnection.id).where(
            IntegrationConnection.subject_id == subject_id,
            IntegrationConnection.provider == IntegrationProvider.GARMIN.value,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.ACCOUNT.value,
        )
    )


# ── The vault ────────────────────────────────────────────────────────────────


async def test_the_stored_row_holds_no_readable_credential(
    db_session, legacy_owner_roots, garmin_connection_id
):
    """Nothing in this table should be greppable.

    A readable identifier beside an encrypted secret tells whoever reaches the
    database which rows are worth attacking — which is also why the connection's
    ``external_account_discriminator`` is opaque.
    """

    await credential_vault_service.store(
        db_session,
        integration_connection_id=garmin_connection_id,
        subject_id=legacy_owner_roots.subject_id,
        secret={"email": "athlete@example.test", "password": "hunter2"},
    )
    await db_session.commit()

    row = await db_session.get(IntegrationCredential, garmin_connection_id)
    blob = bytes(row.ciphertext)
    assert b"athlete@example.test" not in blob
    assert b"hunter2" not in blob


async def test_a_tampered_credential_fails_rather_than_decrypting(
    db_session, legacy_owner_roots, garmin_connection_id
):
    """Authenticated encryption, and the reason it matters here.

    Without the tag a flipped byte would decrypt to *something*, and that
    something would be handed to a login form. A row that is not the row we
    wrote is worth failing on.
    """

    await credential_vault_service.store(
        db_session,
        integration_connection_id=garmin_connection_id,
        subject_id=legacy_owner_roots.subject_id,
        secret={"email": "athlete@example.test", "password": "hunter2"},
    )
    await db_session.commit()

    row = await db_session.get(IntegrationCredential, garmin_connection_id)
    tampered = bytearray(bytes(row.ciphertext))
    tampered[-1] ^= 0x01
    row.ciphertext = bytes(tampered)
    await db_session.commit()
    db_session.expire_all()

    with pytest.raises(credential_vault_service.CredentialVaultCorrupt):
        await credential_vault_service.load(
            db_session, integration_connection_id=garmin_connection_id
        )


async def test_no_key_means_no_vault_rather_than_plaintext(
    db_session, legacy_owner_roots, garmin_connection_id, monkeypatch
):
    """Refusing is the only safe answer; storing it in the clear is not one."""

    monkeypatch.delenv(credential_vault_service.CREDENTIAL_KEY_ENV, raising=False)
    assert credential_vault_service.is_available() is False
    with pytest.raises(credential_vault_service.CredentialVaultUnavailable):
        await credential_vault_service.store(
            db_session,
            integration_connection_id=garmin_connection_id,
            subject_id=legacy_owner_roots.subject_id,
            secret={"email": "a@b.test", "password": "x"},
        )


# ── The resolver ─────────────────────────────────────────────────────────────


async def test_the_environment_is_only_the_installation_owners(
    db_session, legacy_owner_roots, second_patient, monkeypatch
):
    """The defect this whole change exists to stop.

    Every subject's roots are created carrying ``legacy_env:garmin`` — the
    bootstrap writes it without knowing who it is for. Honouring that ref by its
    text alone hands the operator's Garmin account to every patient in the
    installation, and a scheduled sync running under it would file the
    operator's steps, sleep and weight as theirs.
    """

    monkeypatch.setenv("VITALS_GARMIN_EMAIL", "owner@example.test")
    monkeypatch.setenv("VITALS_GARMIN_PASSWORD", "owner-secret")

    owner_account = await provider_credentials_service.resolve_garmin_account(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert owner_account is not None and owner_account.configured
    assert owner_account.config.garmin_email == "owner@example.test"

    other_account = await provider_credentials_service.resolve_garmin_account(
        db_session, subject_id=second_patient
    )
    assert other_account is not None
    assert other_account.configured is False
    assert other_account.config.garmin_email == ""
    assert other_account.config.garmin_password == ""


async def test_two_accounts_share_no_session_no_breaker_and_no_token_store(
    db_session, legacy_owner_roots, second_patient
):
    """The quiet half, and the reason it is not only about the password.

    The cached token session, the login breaker's counters and the disk token
    store were flat process-wide keys. Shared between two subjects that means
    one person's session resuming as another's, and — worse because nothing on
    screen says so — one person's three failed logins pausing everybody else's
    sync for six hours. The breaker is per account because that is what Garmin
    rate-limits.

    The installation owner is namespaced too, deliberately: an exception for
    them would have to be decided on every lookup from facts that are ambiguous
    on exactly the installations this matters on. It costs them one login on the
    first sync after the upgrade.
    """

    from vitals.integrations.garmin_client import (
        LOGIN_ATTEMPTS_KEY,
        LOGIN_PAUSE_KEY,
        REDIS_SESSION_KEY,
        namespaced,
    )

    owner = await provider_credentials_service.resolve_garmin_account(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    other = await provider_credentials_service.resolve_garmin_account(
        db_session, subject_id=second_patient
    )
    assert owner.namespace and other.namespace
    assert owner.namespace != other.namespace
    assert owner.config.garmin_token_dir != other.config.garmin_token_dir

    for key in (REDIS_SESSION_KEY, LOGIN_ATTEMPTS_KEY, LOGIN_PAUSE_KEY):
        assert namespaced(key, owner.namespace) != namespaced(key, other.namespace)
    assert provider_credentials_service.sync_marker_key(
        IntegrationProvider.GARMIN, owner.namespace
    ) != provider_credentials_service.sync_marker_key(
        IntegrationProvider.GARMIN, other.namespace
    )


async def test_a_second_patient_signs_in_as_themselves(
    db_session, legacy_owner_roots, second_patient, monkeypatch
):
    monkeypatch.setenv("VITALS_GARMIN_EMAIL", "owner@example.test")
    monkeypatch.setenv("VITALS_GARMIN_PASSWORD", "owner-secret")

    await provider_credentials_service.set_garmin_credentials(
        db_session,
        subject_id=second_patient,
        email="second@example.test",
        password="second-secret",
    )
    await db_session.commit()

    other = await provider_credentials_service.resolve_garmin_account(
        db_session, subject_id=second_patient
    )
    assert other.configured
    assert other.config.garmin_email == "second@example.test"

    # And the owner is untouched by it.
    owner = await provider_credentials_service.resolve_garmin_account(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert owner.config.garmin_email == "owner@example.test"


async def test_saving_supersedes_the_environment_for_the_owner_too(
    db_session, legacy_owner_roots, monkeypatch
):
    """``.env`` is an adoption source, not a second answer.

    Once the owner has typed a password into the card, the environment is stale
    history. Leaving the ref on ``legacy_env:`` would have the resolver keep
    preferring the file over what they just entered.
    """

    monkeypatch.setenv("VITALS_GARMIN_EMAIL", "owner@example.test")
    monkeypatch.setenv("VITALS_GARMIN_PASSWORD", "owner-secret")

    await provider_credentials_service.set_garmin_credentials(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        email="new@example.test",
        password="new-secret",
    )
    await db_session.commit()

    account = await provider_credentials_service.resolve_garmin_account(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert account.config.garmin_email == "new@example.test"
    # And on the same paths as before the save: the namespace comes off the
    # connection, which a credential change does not move — otherwise typing a
    # new password would silently discard the token session that goes with it.
    before = await provider_credentials_service.resolve_garmin_account(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert account.namespace == before.namespace


async def test_forgetting_an_account_keeps_the_history_it_produced(
    db_session, legacy_owner_roots
):
    """The connection row is the provenance root of every fact it produced.

    Deleting it would orphan a history that is still true: the account is gone,
    the workouts happened.
    """

    await provider_credentials_service.set_garmin_credentials(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        email="a@example.test",
        password="x",
    )
    await db_session.commit()

    connection_id = await _garmin_connection_id(
        db_session, legacy_owner_roots.subject_id
    )
    assert await provider_credentials_service.forget_credentials(
        db_session,
        subject_id=legacy_owner_roots.subject_id,
        provider=IntegrationProvider.GARMIN,
    )
    await db_session.commit()

    assert await db_session.get(IntegrationCredential, connection_id) is None
    assert (
        await db_session.get(IntegrationConnection, connection_id)
    ) is not None


async def test_the_fanout_list_holds_only_accounts_that_can_sign_in(
    db_session, legacy_owner_roots, second_patient, monkeypatch
):
    """What a per-connection scheduled job will iterate.

    A subject with a root and no credential is not an error and not a failure to
    report — they have simply not connected a watch — so they are absent rather
    than present and broken.
    """

    monkeypatch.setenv("VITALS_GARMIN_EMAIL", "")
    monkeypatch.setenv("VITALS_GARMIN_PASSWORD", "")

    assert (
        await provider_credentials_service.list_live_accounts(
            db_session, provider=IntegrationProvider.GARMIN
        )
        == []
    )

    await provider_credentials_service.set_garmin_credentials(
        db_session, subject_id=second_patient, email="s@example.test", password="x"
    )
    await db_session.commit()

    accounts = await provider_credentials_service.list_live_accounts(
        db_session, provider=IntegrationProvider.GARMIN
    )
    assert [account.subject_id for account in accounts] == [second_patient]


async def test_a_retired_connection_is_provenance_and_not_a_login(
    db_session, legacy_owner_roots, monkeypatch
):
    from datetime import datetime, timezone

    monkeypatch.setenv("VITALS_GARMIN_EMAIL", "owner@example.test")
    monkeypatch.setenv("VITALS_GARMIN_PASSWORD", "owner-secret")

    connection_id = await _garmin_connection_id(
        db_session, legacy_owner_roots.subject_id
    )
    connection = await db_session.get(IntegrationConnection, connection_id)
    connection.status = IntegrationConnectionStatus.RETIRED.value
    connection.retired_at = datetime.now(timezone.utc)
    await db_session.commit()

    assert (
        await provider_credentials_service.resolve_garmin_account(
            db_session, subject_id=legacy_owner_roots.subject_id
        )
        is None
    )


# ── The settings card ────────────────────────────────────────────────────────


async def test_saving_the_card_stores_against_the_signed_in_record(
    auth_client, db_session, legacy_owner_roots, tmp_path, monkeypatch
):
    """It wrote ``.env`` and then ``os.environ``, which is the installation."""

    from web.services.env_writer import read_key

    env_file = tmp_path / "test.env"
    env_file.write_text("VITALS_GARMIN_EMAIL=\n", encoding="utf-8")
    monkeypatch.setenv("VITALS_ENV_FILE", str(env_file))

    response = await auth_client.post(
        "/settings/garmin",
        data={"garmin_email": "typed@example.test", "garmin_password": "typed"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "saved=garmin" in response.headers["location"]

    db_session.expire_all()
    account = await provider_credentials_service.resolve_garmin_account(
        db_session, subject_id=legacy_owner_roots.subject_id
    )
    assert account.config.garmin_email == "typed@example.test"
    assert read_key("VITALS_GARMIN_EMAIL") == ""
