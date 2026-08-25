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

import asyncio

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import UserRoleName
from vitals.models.identity import (
    HealthSubject,
    User,
    UserFederatedIdentity,
    UserRole,
)
from vitals.models.scoped_settings import SubjectSetting
from vitals.models.tenancy import IntegrationConnection
from vitals.services.authentication import (
    provisioning as account_provisioning_service,
    registration as registration_service,
)


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


async def test_changing_the_mode_takes_governance_before_the_setting_row_lock(
    db_session, monkeypatch
):
    """Closing the door and admitting somebody must share one lock order.

    A row lock on ``platform_settings`` is not enough: an admission reads the
    setting and then creates identity rows, so it can otherwise observe
    ``open`` immediately before the operator commits ``disabled``.  The shared
    identity-governance fence has to be the first database boundary here, as it
    is in provisioning.
    """

    assert db_session.bind is not None
    observed: list[str] = []

    async def governance(_session: AsyncSession) -> None:
        observed.append("governance")

    def statement(_conn, _cursor, sql, _parameters, _context, _many) -> None:
        if "platform_settings" in sql:
            observed.append("setting-row")

    monkeypatch.setattr(
        registration_service,
        "acquire_identity_governance_lock",
        governance,
        raising=False,
    )
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", statement)
    try:
        await registration_service.set_stored_mode(
            db_session, registration_service.RegistrationMode.OPEN
        )
    finally:
        event.remove(
            db_session.bind.sync_engine,
            "before_cursor_execute",
            statement,
        )

    assert observed[0] == "governance"
    assert observed.index("governance") < observed.index("setting-row")


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


@pytest.mark.integration
async def test_postgres_closing_registration_fences_an_unknown_oidc_admission(
    db_session, legacy_owner_roots, monkeypatch
):
    """Once ``disabled`` wins governance, no later account may appear.

    PostgreSQL's ordinary MVCC read would still see the previously committed
    ``open`` value while the setting row is being changed.  This test therefore
    proves the stronger contract: the close transaction holds the same advisory
    fence admission needs, and the waiter re-reads the mode only after closure
    commits.
    """

    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory-lock semantics")

    from vitals.services.authentication import federation

    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")
    await registration_service.set_stored_mode(
        db_session, registration_service.RegistrationMode.OPEN
    )
    await db_session.commit()

    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    attempted = asyncio.Event()

    async def admit() -> User | None:
        async with factory() as session:
            attempted.set()
            try:
                user = await federation.resolve_federated_user(
                    session,
                    issuer="https://idp.example.test",
                    subject="registration-close-race",
                    preferred_username="registration-close-race",
                )
            except federation.UnknownFederatedIdentity:
                await session.rollback()
                return None
            await session.commit()
            return user

    async with factory() as closer:
        await registration_service.set_stored_mode(
            closer, registration_service.RegistrationMode.DISABLED
        )
        admission = asyncio.create_task(admit())
        try:
            await asyncio.wait_for(attempted.wait(), timeout=2)
            await asyncio.sleep(0.2)
            assert not admission.done(), (
                "unknown OIDC admission must wait behind the mode-change "
                "governance fence"
            )
            await closer.commit()
            assert await asyncio.wait_for(admission, timeout=5) is None
        finally:
            if not admission.done():
                admission.cancel()
            await asyncio.gather(admission, return_exceptions=True)

    async with factory() as verify:
        assert await verify.scalar(
            select(func.count())
            .select_from(UserFederatedIdentity)
            .where(
                UserFederatedIdentity.issuer == "https://idp.example.test",
                UserFederatedIdentity.subject == "registration-close-race",
            )
        ) == 0
        assert await verify.scalar(
            select(func.count())
            .select_from(User)
            .where(User.normalized_username == "registration-close-race")
        ) == 0
        assert (
            await registration_service.get_stored_mode(verify)
            is registration_service.RegistrationMode.DISABLED
        )


@pytest.mark.integration
async def test_postgres_duplicate_unknown_oidc_callbacks_share_one_account_graph(
    db_session, legacy_owner_roots, monkeypatch
):
    """Two callbacks for one new ``(issuer, sub)`` are idempotent.

    Both requests are allowed to reach the governance fence before either may
    commit.  The winner creates the graph; after waiting, the loser must re-read
    the immutable federated identity and return that same user rather than fail
    on the already-taken display username.
    """

    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory-lock semantics")

    from vitals.services.authentication import federation
    from vitals.services.identity_service import acquire_identity_governance_lock

    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")
    await registration_service.set_stored_mode(
        db_session, registration_service.RegistrationMode.OPEN
    )
    await db_session.commit()

    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    started = [asyncio.Event(), asyncio.Event()]

    async def admit(index: int) -> User:
        async with factory() as session:
            started[index].set()
            user = await federation.resolve_federated_user(
                session,
                issuer="https://idp.example.test",
                subject="duplicate-registration-callback",
                preferred_username="duplicate-registration-callback",
            )
            await session.commit()
            return user

    async with factory() as fence:
        await acquire_identity_governance_lock(fence)
        callbacks = [
            asyncio.create_task(admit(0)),
            asyncio.create_task(admit(1)),
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in started)), timeout=2
            )
            await asyncio.sleep(0.2)
            assert all(not callback.done() for callback in callbacks)
            await fence.commit()
            users = await asyncio.wait_for(asyncio.gather(*callbacks), timeout=8)
        finally:
            for callback in callbacks:
                if not callback.done():
                    callback.cancel()
            await asyncio.gather(*callbacks, return_exceptions=True)

    assert users[0].id == users[1].id
    async with factory() as verify:
        links = list(
            await verify.scalars(
                select(UserFederatedIdentity).where(
                    UserFederatedIdentity.issuer == "https://idp.example.test",
                    UserFederatedIdentity.subject
                    == "duplicate-registration-callback",
                )
            )
        )
        accounts = list(
            await verify.scalars(
                select(User).where(
                    User.normalized_username == "duplicate-registration-callback"
                )
            )
        )
        subjects = list(
            await verify.scalars(
                select(HealthSubject).where(
                    HealthSubject.owner_user_id == users[0].id
                )
            )
        )

    assert len(accounts) == len(links) == len(subjects) == 1
    assert links[0].user_id == accounts[0].id == subjects[0].owner_user_id
