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
import json

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import AuditOutcome, UserRoleName
from vitals.models.identity import (
    AuditEvent,
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


async def test_web_member_provisioning_binds_before_subject_roots(
    db_session, legacy_owner_roots, monkeypatch
):
    """The runtime path narrows to its new subject before any RLS-owned insert."""

    from vitals.persistence.rls import bound_subject, in_platform_scope
    from vitals.services import tenancy_bootstrap

    original = tenancy_bootstrap.bootstrap_legacy_resource_roots
    observed: list[object] = []

    async def assert_bound_before_roots(session, *, subject_id, **kwargs):
        observed.append(bound_subject(session))
        assert bound_subject(session) == subject_id
        assert not in_platform_scope(session)
        return await original(session, subject_id=subject_id, **kwargs)

    monkeypatch.setattr(
        tenancy_bootstrap,
        "bootstrap_legacy_resource_roots",
        assert_bound_before_roots,
    )

    provisioned = await account_provisioning_service.provision_bound_member_account(
        db_session,
        username="subject-bound-member",
    )

    assert observed == [provisioned.subject_id]
    assert bound_subject(db_session) == provisioned.subject_id
    assert not in_platform_scope(db_session)
    assert await db_session.scalar(
        select(func.count()).select_from(IntegrationConnection).where(
            IntegrationConnection.subject_id == provisioned.subject_id
        )
    ) == 4
    assert await db_session.scalar(
        select(func.count()).select_from(SubjectSetting).where(
            SubjectSetting.subject_id == provisioned.subject_id
        )
    ) == 1
    assert await db_session.scalar(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.subject_id == provisioned.subject_id,
            AuditEvent.event_type == "tenancy.legacy_resource_roots.bootstrap",
        )
    ) == 1


@pytest.mark.parametrize("ambient_scope", ["subject", "platform"])
async def test_web_member_provisioning_rejects_ambient_authority_before_mutation(
    db_session, legacy_owner_roots, ambient_scope, monkeypatch
):
    from vitals.persistence import rls

    before = await db_session.scalar(select(func.count()).select_from(User))
    if ambient_scope == "subject":
        await rls.bind_session_subject(db_session, legacy_owner_roots.subject_id)
    else:
        # Inject only the application-level ambient state this service guard is
        # meant to reject. A web database login must not be able to acquire the
        # underlying PostgreSQL platform capability.
        monkeypatch.setitem(db_session.info, rls._PLATFORM_KEY, True)

    with pytest.raises(account_provisioning_service.AccountProvisioningScopeError):
        await account_provisioning_service.provision_bound_member_account(
            db_session,
            username=f"refused-{ambient_scope}-member",
        )

    assert await db_session.scalar(select(func.count()).select_from(User)) == before


async def test_operator_provisioning_can_create_multiple_subjects_without_binding(
    db_session, legacy_owner_roots
):
    """CLI/demo provisioning retains its caller-managed, multi-subject contract."""

    from vitals.persistence.rls import bound_subject

    first = await account_provisioning_service.provision_account(
        db_session, username="operator-first-member"
    )
    second = await account_provisioning_service.provision_account(
        db_session, username="operator-second-member"
    )

    assert first.subject_id != second.subject_id
    assert bound_subject(db_session) is None


async def test_unconfigured_subject_proactive_jobs_are_clean_noops(
    db_session,
    legacy_owner_roots,
    monkeypatch,
):
    """Joining the service must not immediately create two failure alerts.

    Proactive preferences are intentionally absent until the owner saves that
    settings form. Missing is therefore an ordinary opt-in state; malformed or
    partially stored preferences remain strict failures.
    """

    from vitals.services import garmin_service
    from vitals.services.proactive import brief, channels, nudges
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    provisioned = await account_provisioning_service.provision_account(
        db_session,
        username="quiet-new-patient",
    )
    await db_session.commit()
    assert provisioned.subject_id is not None
    session_factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    async def unexpected_sync(*args, **kwargs):
        raise AssertionError("an unconfigured brief must stop before network work")

    monkeypatch.setattr(garmin_service, "sync_job", unexpected_sync)

    await brief.brief_job(session_factory, subject_id=provisioned.subject_id)
    await nudges.nudges_job(session_factory, subject_id=provisioned.subject_id)

    async with session_factory() as session:
        owner_channel = await channels.resolve_subject_channel_ownership(
            session,
            subject_id=legacy_owner_roots.subject_id,
        )
        assert (
            await channels.build_legacy_bound_notifier(session, owner_channel)
            is None
        )


async def test_compatibility_whitespace_display_name_uses_username_fallback(
    db_session, legacy_owner_roots
):
    provisioned = await account_provisioning_service.provision_account(
        db_session,
        username="fallback-display",
        display_name="\u3000\u3000",
    )
    subject = await db_session.get(HealthSubject, provisioned.subject_id)
    assert subject.display_name == "fallback-display"


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


async def test_provisioning_normalizes_email_and_record_display_name(
    db_session, legacy_owner_roots
):
    provisioned = await account_provisioning_service.provision_account(
        db_session,
        username="normalized-member",
        email="  Person@Example.TEST  ",
        display_name="  Ｎｅｗ Ｐａｔｉｅｎｔ  ",
    )
    await db_session.flush()

    user = await db_session.get(User, provisioned.user_id)
    subject = await db_session.get(HealthSubject, provisioned.subject_id)
    assert user.email == "Person@Example.TEST"
    assert user.normalized_email == "person@example.test"
    assert subject.display_name == "New Patient"


@pytest.mark.parametrize(
    ("email", "display_name"),
    [
        ("not-an-address", "Patient"),
        ("person@example.test", "Patient\x00hidden"),
        ("person@example.test", "x" * 161),
    ],
)
async def test_invalid_account_labels_are_refused_before_identity_rows(
    db_session, legacy_owner_roots, email, display_name
):
    with pytest.raises(
        account_provisioning_service.AccountProvisioningValidationError
    ):
        await account_provisioning_service.provision_account(
            db_session,
            username="invalid-label-member",
            email=email,
            display_name=display_name,
        )

    assert await db_session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.normalized_username == "invalid-label-member")
    ) == 0


async def test_normalized_email_collision_is_a_domain_refusal_without_partial_rows(
    db_session, legacy_owner_roots
):
    await account_provisioning_service.provision_account(
        db_session,
        username="first-email-owner",
        email="Person@Example.test",
    )
    await db_session.commit()

    with pytest.raises(account_provisioning_service.AccountAlreadyExists) as caught:
        await account_provisioning_service.provision_account(
            db_session,
            username="second-email-owner",
            email="  person@example.TEST ",
        )
    assert "Person@Example.test" not in str(caught.value)
    assert await db_session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.normalized_username == "second-email-owner")
    ) == 0


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


async def test_proof_bound_mode_refuses_generic_open_admission(
    db_session, monkeypatch
):
    """A mode-specific proof may never fall through to generic open admission."""

    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")
    for mode in (
        registration_service.RegistrationMode.INVITE_ONLY,
        registration_service.RegistrationMode.ADMIN_APPROVED,
    ):
        await registration_service.set_stored_mode(db_session, mode)
        await db_session.commit()
        with pytest.raises(registration_service.RegistrationClosed) as caught:
            await registration_service.require_open_registration(db_session)
        assert "requires its dedicated admission flow" in str(caught.value)


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


async def test_setting_registration_mode_writes_a_redacted_operational_audit(
    db_session
):
    await registration_service.set_stored_mode(
        db_session, registration_service.RegistrationMode.ADMIN_APPROVED
    )
    await db_session.flush()

    recorded = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == "registration.mode.changed"
        )
    )
    assert recorded is not None
    assert recorded.actor_user_id is None
    assert recorded.subject_id is None
    assert recorded.outcome == AuditOutcome.SUCCESS.value
    assert recorded.resource_type == "platform_setting"
    assert recorded.resource_id == registration_service.REGISTRATION_MODE_KEY
    assert recorded.metadata_json == {
        "source_surface": "operator_cli",
        "result_code": "disabled_to_admin_approved",
        "resource_type": "platform_setting",
        "resource_id": registration_service.REGISTRATION_MODE_KEY,
        "changed_fields": ["mode"],
    }

    await registration_service.set_stored_mode(
        db_session, registration_service.RegistrationMode.ADMIN_APPROVED
    )
    await db_session.flush()
    assert await db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.event_type == "registration.mode.changed")
    ) == 1


async def test_noncanonical_but_equivalent_mode_does_not_claim_a_transition(
    db_session
):
    from vitals.models.scoped_settings import PlatformSetting

    db_session.add(
        PlatformSetting(
            key=registration_service.REGISTRATION_MODE_KEY,
            value=" OPEN ",
        )
    )
    await db_session.flush()

    await registration_service.set_stored_mode(
        db_session, registration_service.RegistrationMode.OPEN
    )
    assert await db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.event_type == "registration.mode.changed")
    ) == 0


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
    audit = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.event_type == "registration.account.provisioned"
        )
    )
    assert audit is not None
    assert audit.actor_user_id is None
    assert audit.subject_id == subject_id
    assert audit.resource_id == str(user.id)
    assert audit.metadata_json == {
        "source_surface": "authentication.federation",
        "result_code": "open_registration_admitted",
        "resource_type": "user",
        "resource_id": str(user.id),
        "changed_fields": ["federated_identity", "roles", "subject"],
    }
    envelope = json.dumps(audit.metadata_json, sort_keys=True)
    assert "newcomer@example.test" not in envelope
    assert "opaque-newcomer" not in envelope
    assert "newcomer" not in envelope

    again = await federated_login_service.resolve_federated_user(
        db_session,
        issuer="https://idp.example.test",
        subject="opaque-newcomer",
        email="newcomer@example.test",
        preferred_username="renamed-claim-is-not-an-identity-key",
    )
    assert again.id == user.id
    assert await db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.event_type == "registration.account.provisioned")
    ) == 1


async def test_invalid_oidc_naming_claim_is_a_uniform_refusal_without_an_account(
    db_session, legacy_owner_roots, monkeypatch
):
    from vitals.services.authentication import federation

    monkeypatch.setenv(registration_service.REGISTRATION_UNLOCK_ENV, "1")
    await registration_service.set_stored_mode(
        db_session, registration_service.RegistrationMode.OPEN
    )
    await db_session.commit()

    with pytest.raises(federation.UnknownFederatedIdentity):
        await federation.resolve_federated_user(
            db_session,
            issuer="https://idp.example.test",
            subject="hostile-naming-claim",
            preferred_username="hostile\x00name",
        )

    assert await db_session.scalar(
        select(func.count())
        .select_from(UserFederatedIdentity)
        .where(UserFederatedIdentity.subject == "hostile-naming-claim")
    ) == 0
    assert await db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.event_type == "registration.account.provisioned")
    ) == 0


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
        admission_audits = await verify.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == "registration.account.provisioned")
        )

    assert len(accounts) == len(links) == len(subjects) == 1
    assert links[0].user_id == accounts[0].id == subjects[0].owner_user_id
    assert admission_audits == 1
