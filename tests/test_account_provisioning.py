"""A second person can exist without a demo seeder, and only if somebody said so.

Two facts that had been one. Until now `identity_bootstrap` made the
installation's own owner and `scripts/seed_care_demo.py` made everybody else,
which meant a real installation could never gain a second person — and that
"registration is closed" was a property of there being nowhere for an account to
come from, rather than a decision anything had made. The first is why the
professional features had nobody to be about; the second is how a door gets
opened by accident.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from vitals.enums import UserRoleName
from vitals.models.identity import HealthSubject, User, UserRole
from vitals.models.scoped_settings import SubjectSetting
from vitals.models.tenancy import IntegrationConnection
from vitals.services import account_provisioning_service, registration_service


# ── Provisioning ─────────────────────────────────────────────────────────────


async def test_a_provisioned_subject_is_not_one_row(db_session, legacy_owner_roots):
    """The four things a subject needs, and the reason they are collected here.

    A subject missing any of them does not fail loudly. It fails on the fourth
    page somebody visits: no integration roots means every provider path refuses
    and `/settings` answers 409 for a reason that has nothing to do with the
    reader, and no module map means they inherit whichever sections the
    installation-wide default happens to name.
    """

    from vitals.services import modules_service

    provisioned = await account_provisioning_service.provision_account(
        db_session,
        username="new-patient",
        email="new-patient@example.test",
        display_name="New Patient",
        timezone="Europe/Berlin",
    )
    await db_session.commit()

    assert provisioned.subject_id is not None
    subject = await db_session.get(HealthSubject, provisioned.subject_id)
    assert subject.timezone == "Europe/Berlin"
    assert subject.display_name == "New Patient"

    roles = list(
        await db_session.scalars(
            select(UserRole.role).where(UserRole.user_id == provisioned.user_id)
        )
    )
    assert roles == [UserRoleName.MEMBER.value]

    roots = list(
        await db_session.scalars(
            select(IntegrationConnection.provider).where(
                IntegrationConnection.subject_id == provisioned.subject_id
            )
        )
    )
    assert sorted(roots) == ["garmin", "hevy", "openrouter", "telegram"]

    modules = await db_session.scalar(
        select(SubjectSetting.value).where(
            SubjectSetting.subject_id == provisioned.subject_id,
            SubjectSetting.key == modules_service.SETTINGS_KEY,
        )
    )
    assert modules is not None


async def test_a_new_subject_never_claims_the_environments_provider_accounts(
    db_session, legacy_owner_roots, monkeypatch
):
    """The defect the credential work fixed, asserted at the other end.

    The tenancy bootstrap used to write ``legacy_env:garmin`` on every root it
    created. A subject provisioned with that ref says "my Garmin password is in
    ``.env``", and the one in there is the operator's.
    """

    from vitals.services import provider_credentials_service

    monkeypatch.setenv("VITALS_GARMIN_EMAIL", "owner@example.test")
    monkeypatch.setenv("VITALS_GARMIN_PASSWORD", "owner-secret")

    provisioned = await account_provisioning_service.provision_account(
        db_session, username="new-patient"
    )
    await db_session.commit()

    account = await provider_credentials_service.resolve_garmin_account(
        db_session, subject_id=provisioned.subject_id
    )
    assert account is not None
    assert account.configured is False
    assert account.config.garmin_email == ""


async def test_a_professional_keeps_no_record_of_their_own(
    db_session, legacy_owner_roots
):
    """Not a degraded state. Half of what this product is now."""

    provisioned = await account_provisioning_service.provision_account(
        db_session,
        username="dr-ivanova",
        roles=(UserRoleName.DOCTOR.value,),
        with_health_record=False,
    )
    await db_session.commit()

    assert provisioned.subject_id is None
    assert (
        await db_session.scalar(
            select(HealthSubject.id).where(
                HealthSubject.owner_user_id == provisioned.user_id
            )
        )
        is None
    )


async def test_provisioning_cannot_mint_an_administrator(
    db_session, legacy_owner_roots
):
    """Creating an account and making it an administrator stay two decisions."""

    with pytest.raises(
        account_provisioning_service.AccountProvisioningValidationError
    ):
        await account_provisioning_service.provision_account(
            db_session,
            username="sneaky",
            roles=(UserRoleName.PLATFORM_SUPERADMIN.value,),
        )


async def test_a_taken_name_is_a_refusal_not_a_second_account(
    db_session, legacy_owner_roots
):
    await account_provisioning_service.provision_account(
        db_session, username="maria"
    )
    await db_session.commit()

    with pytest.raises(account_provisioning_service.AccountAlreadyExists):
        await account_provisioning_service.provision_account(
            db_session, username="  MARIA  "
        )


async def test_an_unknown_timezone_is_refused_before_it_is_stored(
    db_session, legacy_owner_roots
):
    """Stored, it would raise on every later request that asks what day it is."""

    with pytest.raises(
        account_provisioning_service.AccountProvisioningValidationError
    ):
        await account_provisioning_service.provision_account(
            db_session, username="traveller", timezone="Mars/Olympus_Mons"
        )


# ── The decision ─────────────────────────────────────────────────────────────


async def test_registration_is_closed_by_default(db_session):
    assert (
        await registration_service.effective_mode(db_session)
        is registration_service.RegistrationMode.DISABLED
    )
    with pytest.raises(registration_service.RegistrationClosed):
        await registration_service.require_open_registration(db_session)


async def test_a_stored_mode_means_nothing_without_the_deployment_gate(
    db_session, monkeypatch
):
    """Two switches, and the second one is not a settings page.

    Opening registration is a deployment decision that comes after a security
    review. A mode an administrator can flip from a screen is not that, so the
    stored value can be configured and reviewed ahead of the release that makes
    it mean anything.
    """

    monkeypatch.delenv(registration_service.REGISTRATION_UNLOCK_ENV, raising=False)
    await registration_service.set_stored_mode(
        db_session, registration_service.RegistrationMode.OPEN
    )
    await db_session.commit()

    assert (
        await registration_service.get_stored_mode(db_session)
        is registration_service.RegistrationMode.OPEN
    )
    assert (
        await registration_service.effective_mode(db_session)
        is registration_service.RegistrationMode.DISABLED
    )
    with pytest.raises(registration_service.RegistrationClosed):
        await registration_service.require_open_registration(db_session)


async def test_a_half_built_mode_refuses_rather_than_behaving_like_open(
    db_session, monkeypatch
):
    """The failure this module exists to prevent."""

    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")
    for mode in (
        registration_service.RegistrationMode.INVITE_ONLY,
        registration_service.RegistrationMode.ADMIN_APPROVED,
    ):
        await registration_service.set_stored_mode(db_session, mode)
        await db_session.commit()
        with pytest.raises(registration_service.RegistrationClosed) as caught:
            await registration_service.require_open_registration(db_session)
        assert "not implemented" in str(caught.value)


async def test_a_stored_value_this_build_does_not_understand_reads_as_closed(
    db_session, monkeypatch
):
    from vitals.models.scoped_settings import PlatformSetting

    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")
    db_session.add(
        PlatformSetting(
            key=registration_service.REGISTRATION_MODE_KEY,
            value={"mode": "everyone-welcome"},
        )
    )
    await db_session.commit()

    assert (
        await registration_service.effective_mode(db_session)
        is registration_service.RegistrationMode.DISABLED
    )


# ── The two, together ────────────────────────────────────────────────────────


async def test_a_stranger_with_a_valid_login_gets_no_account(
    db_session, legacy_owner_roots
):
    """A valid provider login by somebody with no account is a refusal."""

    from vitals.services.authentication import federation as federated_login_service

    with pytest.raises(federated_login_service.UnknownFederatedIdentity):
        await federated_login_service.resolve_federated_user(
            db_session,
            issuer="https://idp.example.test",
            subject="opaque-stranger",
            email="stranger@example.test",
            preferred_username="stranger",
        )
    assert (
        await db_session.scalar(
            select(User.id).where(User.normalized_username == "stranger")
        )
        is None
    )


async def test_an_open_installation_provisions_and_links_in_one_go(
    db_session, legacy_owner_roots, monkeypatch
):
    """What opening registration will do, exercised while it is still shut.

    Deliberately tested rather than left until the release that turns it on: a
    path that has never run is a path nobody knows the shape of, and this one
    creates accounts.
    """

    from vitals.models.identity import UserFederatedIdentity
    from vitals.services.authentication import federation as federated_login_service

    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")
    await registration_service.set_stored_mode(
        db_session, registration_service.RegistrationMode.OPEN
    )
    await db_session.commit()

    user = await federated_login_service.resolve_federated_user(
        db_session,
        issuer="https://idp.example.test",
        subject="opaque-newcomer",
        email="newcomer@example.test",
        preferred_username="newcomer",
    )
    await db_session.commit()

    assert user.username == "newcomer"
    link = await db_session.scalar(
        select(UserFederatedIdentity).where(
            UserFederatedIdentity.subject == "opaque-newcomer"
        )
    )
    assert link is not None and link.user_id == user.id
    subject_id = await db_session.scalar(
        select(HealthSubject.id).where(HealthSubject.owner_user_id == user.id)
    )
    assert subject_id is not None


async def test_an_open_installation_still_refuses_a_name_somebody_holds(
    db_session, legacy_owner_roots, monkeypatch
):
    """Picking ``newcomer-2`` would hand a stranger a name implying a relationship."""

    from vitals.services.authentication import federation as federated_login_service

    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")
    await registration_service.set_stored_mode(
        db_session, registration_service.RegistrationMode.OPEN
    )
    await account_provisioning_service.provision_account(
        db_session, username="newcomer"
    )
    await db_session.commit()

    with pytest.raises(federated_login_service.UnknownFederatedIdentity):
        await federated_login_service.resolve_federated_user(
            db_session,
            issuer="https://idp.example.test",
            subject="opaque-impostor",
            preferred_username="newcomer",
        )


async def test_the_name_falls_back_to_the_subject_when_the_provider_offers_nothing(
    db_session, legacy_owner_roots, monkeypatch
):
    from vitals.services.authentication import federation as federated_login_service

    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")
    await registration_service.set_stored_mode(
        db_session, registration_service.RegistrationMode.OPEN
    )
    await db_session.commit()

    user = await federated_login_service.resolve_federated_user(
        db_session,
        issuer="https://idp.example.test",
        subject="opaque-nameless-identity",
    )
    await db_session.commit()
    assert user.username.startswith("user-")
