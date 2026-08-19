"""Focused contracts for subject-scoped conflict-rule activation."""
from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import FrozenInstanceError

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import UserStatus
from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject, User
from vitals.models.scoped_settings import SubjectSetting
from vitals.services import conflict_activation_service as activation
from vitals.services import conflict_catalog


LegacyConflictBridge = activation.LegacyConflictActivationBridge


async def _subject(session: AsyncSession, slug: str = "activation-owner") -> HealthSubject:
    user = User(
        username=slug,
        normalized_username=slug.casefold(),
        password_hash="$synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    session.add(user)
    await session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=f"Synthetic {slug}",
        timezone="Asia/Almaty",
    )
    session.add(subject)
    await session.flush()
    return subject


def _rule_values(entry: dict, *, message: str | None = None) -> dict:
    return {
        "rule_type": entry["rule_type"],
        "domain_a": entry["domain_a"],
        "condition_a": entry["condition_a"],
        "domain_b": entry["domain_b"],
        "condition_b": entry["condition_b"],
        "severity": entry["severity"],
        "message": message if message is not None else entry["message"],
        "params": entry.get("params"),
        "category": entry.get("category"),
        "source": entry.get("source"),
        "evidence": entry.get("evidence"),
        "active": True,
    }


def _custom_rule(
    *,
    subject_id: uuid.UUID | None,
    code: str | None = None,
    active: bool = True,
) -> ConflictRule:
    return ConflictRule(
        subject_id=subject_id,
        code=code,
        rule_type="soft_warn",
        domain_a="supplements",
        condition_a={"key": "synthetic-a"},
        domain_b="nutrition",
        condition_b={"key": "synthetic-b"},
        severity="warn",
        message="synthetic custom rule",
        active=active,
    )


async def _catalog_rows(session: AsyncSession) -> list[ConflictRule]:
    await conflict_catalog.sync_catalog(session)
    return list(
        await session.scalars(
            select(ConflictRule)
            .where(ConflictRule.code.is_not(None))
            .order_by(ConflictRule.code)
        )
    )


async def test_strict_missing_setting_enables_the_catalog_and_requires_subject(db_session):
    subject = await _subject(db_session)
    rows = await _catalog_rows(db_session)
    rows[0].active = False
    await db_session.flush()

    state = await activation.read_activation_state(
        db_session,
        subject_id=subject.id,
    )

    assert state.subject_id == subject.id
    assert state.disabled_codes == ()
    assert state.source is activation.ConflictActivationStateSource.DEFAULT
    assert state.legacy_bridge is LegacyConflictBridge.REJECT
    assert state.is_code_active(rows[0].code) is True
    with pytest.raises(activation.ConflictActivationSubjectNotFoundError):
        await activation.read_activation_state(
            db_session,
            subject_id=uuid.uuid4(),
        )


async def test_scoped_document_is_exact_detached_and_state_is_immutable(db_session):
    subject = await _subject(db_session)
    codes = sorted(entry["code"] for entry in conflict_catalog.load_rule_catalog())[:2]
    persisted = {"v": 1, "disabled_codes": codes}
    db_session.add(
        SubjectSetting(
            subject_id=subject.id,
            key=activation.SETTING_KEY,
            value=persisted,
        )
    )
    await db_session.flush()

    state = await activation.read_activation_state(
        db_session,
        subject_id=subject.id,
    )
    document = state.to_document()
    document["disabled_codes"].clear()

    assert state.disabled_codes == tuple(codes)
    assert state.source is activation.ConflictActivationStateSource.SCOPED
    assert persisted == {"v": 1, "disabled_codes": codes}
    with pytest.raises(FrozenInstanceError):
        state.source = activation.ConflictActivationStateSource.DEFAULT


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"v": 1},
        {"v": 1, "disabled_codes": [], "extra": True},
        {"v": True, "disabled_codes": []},
        {"v": 2, "disabled_codes": []},
        {"v": 1, "disabled_codes": "code"},
        {"v": 1, "disabled_codes": [1]},
        {"v": 1, "disabled_codes": ["z", "a"]},
        {"v": 1, "disabled_codes": ["a", "a"]},
        {"v": 1, "disabled_codes": ["not-in-the-catalog"]},
    ],
)
async def test_malformed_scoped_documents_fail_closed(db_session, value):
    subject = await _subject(db_session)
    db_session.add(
        SubjectSetting(
            subject_id=subject.id,
            key=activation.SETTING_KEY,
            value=value,
        )
    )
    await db_session.flush()

    with pytest.raises(activation.ConflictActivationStateMalformedError):
        await activation.read_activation_state(
            db_session,
            subject_id=subject.id,
        )


async def test_exact_one_bridge_derives_legacy_disabled_codes(db_session):
    subject = await _subject(db_session)
    rows = await _catalog_rows(db_session)
    rows[0].active = False
    rows[1].active = True
    await db_session.flush()

    state = await activation.read_activation_state(
        db_session,
        subject_id=subject.id,
        legacy_bridge=LegacyConflictBridge.FULLY_UNOWNED,
    )

    assert state.disabled_codes == (rows[0].code,)
    assert state.source is activation.ConflictActivationStateSource.LEGACY
    assert activation.is_rule_active(rows[0], state) is False
    assert activation.is_rule_active(rows[1], state) is True


async def test_exact_one_bridge_closes_when_a_second_subject_exists(db_session):
    subject = await _subject(db_session, "subject-a")
    await _subject(db_session, "subject-b")
    legacy_custom = _custom_rule(subject_id=None)
    db_session.add(legacy_custom)
    await db_session.flush()

    with pytest.raises(activation.ConflictActivationLegacyBridgeError):
        await activation.read_activation_state(
            db_session,
            subject_id=subject.id,
            legacy_bridge=LegacyConflictBridge.FULLY_UNOWNED,
        )
    with pytest.raises(activation.ConflictActivationLegacyBridgeError):
        await activation.set_rule_activation(
            db_session,
            subject_id=subject.id,
            rule_id=legacy_custom.id,
            active=False,
            legacy_bridge=LegacyConflictBridge.FULLY_UNOWNED,
        )
    assert legacy_custom.subject_id is None
    assert legacy_custom.active is True


async def test_strict_curated_toggle_writes_exact_setting_without_global_mutation(
    db_session,
):
    subject = await _subject(db_session)
    rule = (await _catalog_rows(db_session))[0]
    assert rule.active is True

    result = await activation.set_rule_activation(
        db_session,
        subject_id=subject.id,
        rule_id=rule.id,
        active=False,
    )

    setting = await db_session.get(
        SubjectSetting,
        (subject.id, activation.SETTING_KEY),
    )
    assert result.kind is activation.ConflictActivationRuleKind.CURATED
    assert result.previous_active is True
    assert result.active is False
    assert result.adopted_legacy_rule is False
    assert result.previous_state.source is activation.ConflictActivationStateSource.DEFAULT
    assert result.state.source is activation.ConflictActivationStateSource.SCOPED
    assert setting is not None
    assert setting.value == {"v": 1, "disabled_codes": [rule.code]}
    assert rule.active is True


async def test_bridge_curated_toggle_derives_then_dual_writes_global_flag(db_session):
    subject = await _subject(db_session)
    first, second = (await _catalog_rows(db_session))[:2]
    first.active = False
    second.active = True
    await db_session.flush()

    result = await activation.set_rule_activation(
        db_session,
        subject_id=subject.id,
        rule_id=second.id,
        active=False,
        legacy_bridge=LegacyConflictBridge.FULLY_UNOWNED,
    )

    expected = sorted([first.code, second.code])
    setting = await db_session.get(
        SubjectSetting,
        (subject.id, activation.SETTING_KEY),
    )
    assert result.previous_state.disabled_codes == (first.code,)
    assert result.state.disabled_codes == tuple(expected)
    assert setting is not None
    assert setting.value == {"v": 1, "disabled_codes": expected}
    assert first.active is False
    assert second.active is False


@pytest.mark.parametrize("code", [None, "my_private_rule"])
async def test_exact_subject_custom_rule_uses_row_active(db_session, code):
    subject = await _subject(db_session)
    rule = _custom_rule(subject_id=subject.id, code=code)
    db_session.add(rule)
    await db_session.flush()

    result = await activation.set_rule_activation(
        db_session,
        subject_id=subject.id,
        rule_id=rule.id,
        active=False,
    )

    assert result.kind is activation.ConflictActivationRuleKind.CUSTOM
    assert result.previous_active is True
    assert result.adopted_legacy_rule is False
    assert rule.subject_id == subject.id
    assert rule.active is False
    assert await db_session.get(
        SubjectSetting,
        (subject.id, activation.SETTING_KEY),
    ) is None


async def test_fully_unowned_code_null_custom_requires_bridge_and_is_adopted(db_session):
    subject = await _subject(db_session)
    rule = _custom_rule(subject_id=None)
    db_session.add(rule)
    await db_session.flush()

    with pytest.raises(activation.ConflictActivationOwnershipError):
        await activation.set_rule_activation(
            db_session,
            subject_id=subject.id,
            rule_id=rule.id,
            active=False,
        )

    result = await activation.set_rule_activation(
        db_session,
        subject_id=subject.id,
        rule_id=rule.id,
        active=False,
        legacy_bridge=LegacyConflictBridge.FULLY_UNOWNED,
    )

    assert result.kind is activation.ConflictActivationRuleKind.CUSTOM
    assert result.adopted_legacy_rule is True
    assert rule.subject_id == subject.id
    assert rule.active is False


async def test_foreign_rule_fails_without_mutation(db_session):
    subject_a = await _subject(db_session, "subject-a")
    subject_b = await _subject(db_session, "subject-b")
    rule = _custom_rule(subject_id=subject_b.id)
    db_session.add(rule)
    await db_session.flush()

    with pytest.raises(activation.ConflictActivationOwnershipError):
        await activation.set_rule_activation(
            db_session,
            subject_id=subject_a.id,
            rule_id=rule.id,
            active=False,
        )
    assert rule.subject_id == subject_b.id
    assert rule.active is True


async def test_catalog_copy_unknown_global_and_tampered_global_fail_closed(db_session):
    subject = await _subject(db_session)
    entries = conflict_catalog.load_rule_catalog()
    copied = ConflictRule(
        subject_id=subject.id,
        code=entries[0]["code"],
        **_rule_values(entries[0]),
    )
    unknown = _custom_rule(subject_id=None, code="unknown_global_code")
    tampered = ConflictRule(
        subject_id=None,
        code=entries[1]["code"],
        **_rule_values(entries[1], message="tampered"),
    )
    db_session.add_all([copied, unknown, tampered])
    await db_session.flush()

    for rule in (copied, unknown, tampered):
        with pytest.raises(activation.ConflictActivationCatalogIntegrityError):
            await activation.set_rule_activation(
                db_session,
                subject_id=subject.id,
                rule_id=rule.id,
                active=False,
            )
        assert rule.active is True


async def test_malformed_custom_code_and_unknown_rule_fail_closed(db_session):
    subject = await _subject(db_session)
    malformed = _custom_rule(subject_id=subject.id, code=" ")
    db_session.add(malformed)
    await db_session.flush()

    with pytest.raises(activation.ConflictActivationCatalogIntegrityError):
        await activation.set_rule_activation(
            db_session,
            subject_id=subject.id,
            rule_id=malformed.id,
            active=False,
        )
    with pytest.raises(activation.ConflictActivationRuleNotFoundError):
        await activation.set_rule_activation(
            db_session,
            subject_id=subject.id,
            rule_id=malformed.id + 10_000,
            active=False,
        )
    assert malformed.active is True


@pytest.mark.parametrize(
    ("subject_id", "rule_id", "active", "legacy_bridge"),
    [
        (None, 1, True, LegacyConflictBridge.REJECT),
        (uuid.UUID(int=0), 1, True, LegacyConflictBridge.REJECT),
        (uuid.uuid4(), True, True, LegacyConflictBridge.REJECT),
        (uuid.uuid4(), 0, True, LegacyConflictBridge.REJECT),
        (uuid.uuid4(), 1, 1, LegacyConflictBridge.REJECT),
        (uuid.uuid4(), 1, True, "unknown"),
    ],
)
async def test_invalid_toggle_inputs_fail_before_database_access(
    db_session,
    subject_id,
    rule_id,
    active,
    legacy_bridge,
):
    with pytest.raises(activation.ConflictActivationValidationError):
        await activation.set_rule_activation(
            db_session,
            subject_id=subject_id,
            rule_id=rule_id,
            active=active,
            legacy_bridge=legacy_bridge,
        )


async def test_effective_mapping_is_immutable_and_rejects_foreign_rows(db_session):
    subject_a = await _subject(db_session, "subject-a")
    subject_b = await _subject(db_session, "subject-b")
    curated = (await _catalog_rows(db_session))[0]
    own = _custom_rule(subject_id=subject_a.id, active=False)
    foreign = _custom_rule(subject_id=subject_b.id)
    db_session.add_all([own, foreign])
    await db_session.flush()
    db_session.add(
        SubjectSetting(
            subject_id=subject_a.id,
            key=activation.SETTING_KEY,
            value={"v": 1, "disabled_codes": [curated.code]},
        )
    )
    await db_session.flush()
    state = await activation.read_activation_state(
        db_session,
        subject_id=subject_a.id,
    )

    mapping = activation.effective_rule_activation([curated, own], state)
    assert dict(mapping) == {curated.id: False, own.id: False}
    with pytest.raises(TypeError):
        mapping[curated.id] = True
    with pytest.raises(activation.ConflictActivationOwnershipError):
        activation.effective_rule_activation([foreign], state)


async def test_toggle_flushes_but_does_not_commit(db_session):
    subject = await _subject(db_session)
    rule = (await _catalog_rows(db_session))[0]
    subject_id = subject.id
    rule_id = rule.id
    await db_session.commit()

    await activation.set_rule_activation(
        db_session,
        subject_id=subject_id,
        rule_id=rule_id,
        active=False,
    )
    assert await db_session.get(
        SubjectSetting,
        (subject_id, activation.SETTING_KEY),
    ) is not None
    await db_session.rollback()

    assert await db_session.get(
        SubjectSetting,
        (subject_id, activation.SETTING_KEY),
    ) is None
    restored = await db_session.get(ConflictRule, rule_id)
    assert restored is not None and restored.active is True
    assert ".commit(" not in inspect.getsource(activation.set_rule_activation)


async def test_toggle_lock_order_is_governance_subject_setting_rules(
    db_session,
    monkeypatch,
):
    subject = await _subject(db_session)
    rule = (await _catalog_rows(db_session))[0]
    events: list[str] = []
    original_governance = activation.acquire_identity_governance_lock
    original_subject = activation._subject
    original_setting = activation._setting
    original_globals = activation._global_rules

    async def governance(session):
        events.append("governance")
        return await original_governance(session)

    async def subject_lock(session, subject_id, *, for_update):
        if for_update:
            events.append("subject")
        return await original_subject(session, subject_id, for_update=for_update)

    async def setting_lock(session, subject_id, *, for_update):
        if for_update:
            events.append("setting")
        return await original_setting(session, subject_id, for_update=for_update)

    async def global_locks(session, *, for_update):
        if for_update:
            events.append("rules")
        return await original_globals(session, for_update=for_update)

    monkeypatch.setattr(activation, "acquire_identity_governance_lock", governance)
    monkeypatch.setattr(activation, "_subject", subject_lock)
    monkeypatch.setattr(activation, "_setting", setting_lock)
    monkeypatch.setattr(activation, "_global_rules", global_locks)

    await activation.set_rule_activation(
        db_session,
        subject_id=subject.id,
        rule_id=rule.id,
        active=False,
        legacy_bridge=LegacyConflictBridge.FULLY_UNOWNED,
    )

    assert events == ["governance", "subject", "setting", "rules"]


@pytest.mark.integration
async def test_postgres_concurrent_first_toggles_preserve_both_codes(db_session):
    subject = await _subject(db_session)
    first, second = (await _catalog_rows(db_session))[:2]
    subject_id = subject.id
    first_id = first.id
    second_id = second.id
    expected = sorted([first.code, second.code])
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )

    session_a = factory()
    await activation.set_rule_activation(
        session_a,
        subject_id=subject_id,
        rule_id=first_id,
        active=False,
    )

    async def toggle_second() -> None:
        async with factory() as session_b:
            await activation.set_rule_activation(
                session_b,
                subject_id=subject_id,
                rule_id=second_id,
                active=False,
            )
            await session_b.commit()

    task_b = asyncio.create_task(toggle_second())
    await asyncio.sleep(0.25)
    assert not task_b.done(), "second toggle must wait for the locked root"

    await session_a.commit()
    await session_a.close()
    await asyncio.wait_for(task_b, timeout=5)

    async with factory() as verify:
        setting = await verify.get(
            SubjectSetting,
            (subject_id, activation.SETTING_KEY),
        )
        assert setting is not None
        assert setting.value == {"v": 1, "disabled_codes": expected}
