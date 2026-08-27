"""Ownership contract for the Stage-1 conflict-engine read slice.

These tests deliberately exercise only strict reads and the explicit
single-subject compatibility adapter.  Conflict writes/alerts, rule activation
preferences, and the later schema cutover belong to separate stages.
"""
from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.enums import Domain, Source, UserRoleName, UserStatus
from vitals.models.conflict_rule import ConflictRule
from vitals.models.genetics import GeneticVariant
from vitals.models.glp1 import DosePhase
from vitals.models.hrt import HrtCompound, HrtCycle, HrtCycleItem, HrtDose
from vitals.models.identity import HealthSubject, User
from vitals.models.labs import LabResult
from vitals.models.nutrition import MealLog
from vitals.models.raw_payload import RawPayload
from vitals.models.skincare import SkincareLog
from vitals.models.supplements import Supplement
from vitals.services import (
    conflict_activation_service,
    conflict_catalog,
    conflict_engine,
    conflict_registrations,
)
from vitals.services.genetics import variants
from vitals.services.legacy_ownership import (
    LegacyOwnerResolutionError,
    LegacySubjectResolutionError,
)


# These tests seed rows with no owner on purpose: they pin what a scoped
# reader does when the ownership backfill has not reached a row yet, which is
# a state the application itself can no longer create. The schema says so, so
# this module asks for the one that stood before the ownership contract.
pytestmark = pytest.mark.pre_ownership_contract


EVALUATION_DATE = date(2020, 1, 15)
PROBE_DOMAIN = Domain.WEIGHT.value

RESOLVER_DOMAINS = (
    Domain.SUPPLEMENTS.value,
    Domain.GENETICS.value,
    Domain.SKINCARE.value,
    Domain.GLP1.value,
    Domain.LABS.value,
    Domain.NUTRITION.value,
    Domain.HRT.value,
)
REGISTERED_RESOLVER_DOMAINS = (
    *RESOLVER_DOMAINS,
    Domain.WEIGHT.value,
    Domain.BODY_COMPOSITION.value,
)

FACT_CONDITIONS = {
    Domain.SUPPLEMENTS.value: {"key": "scope_probe", "active": True},
    Domain.GENETICS.value: {"marker": "scope_probe"},
    Domain.SKINCARE.value: {"retinoid": True},
    Domain.GLP1.value: {"drug": "scope_probe", "active": True},
    Domain.LABS.value: {"marker": "scope_probe", "value": {"$gte": 42}},
    Domain.NUTRITION.value: {"calories": {"$gte": 640}},
    Domain.HRT.value: {"compound_key": "scope_probe", "active": True},
}


def _strict_scope(subject_id: uuid.UUID):
    """Construct the public strict-read scope without importing future symbols.

    Runtime lookup keeps this contract file collectable while the production
    Stage-1 slice is being implemented in the shared tree.
    """

    return conflict_engine.ConflictScope(
        subject_id=subject_id,
        evaluation_date=EVALUATION_DATE,
        legacy_bridge=conflict_engine.LegacyConflictBridge.REJECT,
    )


async def _empty_scoped_resolver(session, *, scope):
    del session, scope
    return []


async def _matching_scoped_resolver(session, *, scope):
    del session, scope
    return [{"hit": True}]


async def _add_subject(db_session, *, label: str) -> tuple[HealthSubject, User]:
    token = uuid.uuid4().hex
    username = f"{label}-{token}"
    user = User(
        username=username,
        normalized_username=username,
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(user)
    await db_session.flush()
    subject = HealthSubject(
        owner_user_id=user.id,
        display_name=label,
        timezone="UTC",
    )
    db_session.add(subject)
    await db_session.flush()
    return subject, user


def _subject_probe_rule(
    *,
    fact_domain: str,
    subject_id: uuid.UUID,
    message: str = "scope probe",
) -> ConflictRule:
    return ConflictRule(
        subject_id=subject_id,
        rule_type="soft_warn",
        domain_a=fact_domain,
        condition_a=FACT_CONDITIONS[fact_domain],
        domain_b=PROBE_DOMAIN,
        condition_b={"probe": True},
        severity="warn",
        message=message,
        active=True,
    )


async def _seed_fact(
    db_session,
    *,
    domain: str,
    subject_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    raw_payload_id: int | None = None,
) -> None:
    ownership = {
        "subject_id": subject_id,
        "actor_user_id": actor_user_id,
    }
    if domain == Domain.SUPPLEMENTS.value:
        row = Supplement(
            **ownership,
            domain=domain,
            source=Source.MANUAL.value,
            name="Scope probe",
            key="scope_probe",
            active=True,
        )
    elif domain == Domain.GENETICS.value:
        row = GeneticVariant(
            **ownership,
            raw_payload_id=raw_payload_id,
            domain=domain,
            source=Source.MANUAL.value,
            gene="SCOPE",
            marker="scope_probe",
        )
    elif domain == Domain.SKINCARE.value:
        row = SkincareLog(
            **ownership,
            date=EVALUATION_DATE,
            domain=domain,
            source=Source.MANUAL.value,
            retinoid=True,
        )
    elif domain == Domain.GLP1.value:
        row = DosePhase(
            **ownership,
            domain=domain,
            source=Source.MANUAL.value,
            start_date=EVALUATION_DATE - timedelta(days=7),
            end_date=EVALUATION_DATE + timedelta(days=7),
            drug="scope_probe",
            dose_mg=1.0,
        )
    elif domain == Domain.LABS.value:
        row = LabResult(
            **ownership,
            raw_payload_id=raw_payload_id,
            date=EVALUATION_DATE,
            domain=domain,
            source=Source.MANUAL.value,
            marker="scope_probe",
            value=42.0,
            flag="high",
        )
    elif domain == Domain.NUTRITION.value:
        row = MealLog(
            **ownership,
            date=EVALUATION_DATE,
            domain=domain,
            source=Source.MANUAL.value,
            name="Scope probe meal",
            calories=640.0,
        )
    elif domain == Domain.HRT.value:
        row = HrtDose(
            **ownership,
            date=EVALUATION_DATE,
            domain=domain,
            source=Source.MANUAL.value,
            compound_key="scope_probe",
            dose=1.0,
            unit="mg",
        )
    else:  # pragma: no cover - protects the parameter table itself
        raise AssertionError(f"unhandled resolver domain: {domain}")
    db_session.add(row)
    await db_session.flush()


def test_registered_primary_resolvers_require_keyword_only_scope(monkeypatch):
    """Every production registration must use the strict scoped resolver.

    A legacy resolver may additionally be registered for the named adapter, but
    it must never occupy the primary slot used by ``evaluate_scoped``.
    """

    captured: dict[str, object] = {}

    def capture(domain, resolver, *args, **kwargs):
        del args, kwargs
        captured[domain] = resolver

    monkeypatch.setattr(conflict_engine, "register_domain_resolver", capture)
    conflict_registrations.register_all_resolvers()

    assert set(captured) == set(REGISTERED_RESOLVER_DOMAINS)
    for domain, resolver in captured.items():
        parameter = inspect.signature(resolver).parameters.get("scope")
        assert parameter is not None, f"{domain} resolver has no scope"
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize("fact_domain", RESOLVER_DOMAINS)
async def test_each_resolver_keeps_subject_facts_isolated(
    db_session,
    legacy_owner_roots,
    fact_domain,
):
    """A foreign subject's matching fact must never fire A's global rule.

    The deliberately historical evaluation date also catches resolvers which
    accidentally call installation-wide ``today_local()`` instead of using the
    one date carried by the scope.
    """

    subject_b, user_b = await _add_subject(db_session, label="subject-b")
    db_session.add(
        _subject_probe_rule(
            fact_domain=fact_domain,
            subject_id=legacy_owner_roots.subject_id,
        )
    )
    await _seed_fact(
        db_session,
        domain=fact_domain,
        subject_id=subject_b.id,
        actor_user_id=user_b.id,
    )
    await db_session.commit()

    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)
    proposed = {"probe": True}
    scope_a = _strict_scope(legacy_owner_roots.subject_id)

    assert await conflict_engine.evaluate_scoped(
        db_session,
        scope=scope_a,
        domain=PROBE_DOMAIN,
        proposed_state=proposed,
    ) == []

    await _seed_fact(
        db_session,
        domain=fact_domain,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
    )
    await db_session.commit()

    violations = await conflict_engine.evaluate_scoped(
        db_session,
        scope=scope_a,
        domain=PROBE_DOMAIN,
        proposed_state=proposed,
    )
    assert [violation.message for violation in violations] == ["scope probe"]


async def test_rule_loader_includes_global_and_same_subject_but_not_foreign_custom(
    db_session,
    legacy_owner_roots,
):
    subject_b, _ = await _add_subject(db_session, label="subject-b")
    await conflict_catalog.sync_catalog(db_session)
    common = {
        "rule_type": "soft_warn",
        "domain_a": Domain.SUPPLEMENTS.value,
        "condition_a": {"hit": True},
        "domain_b": Domain.LABS.value,
        "condition_b": {"hit": True},
        "severity": "warn",
        "active": True,
    }
    db_session.add_all(
        [
            ConflictRule(
                subject_id=legacy_owner_roots.subject_id,
                message="subject-a",
                **common,
            ),
            ConflictRule(subject_id=subject_b.id, message="subject-b", **common),
        ]
    )
    await db_session.commit()

    rows = await conflict_engine.load_scoped_rules(
        db_session,
        scope=_strict_scope(legacy_owner_roots.subject_id),
        active_only=False,
    )

    messages = {row.message for row in rows}
    assert "subject-a" in messages
    assert "subject-b" not in messages
    assert {entry["code"] for entry in conflict_catalog.load_rule_catalog()} <= {
        row.code for row in rows
    }


async def test_unclassified_global_rule_blocks_legacy_activation_bridge(
    db_session,
    legacy_owner_roots,
):
    await conflict_catalog.sync_catalog(db_session)
    common = {
        "rule_type": "soft_warn",
        "domain_a": Domain.SUPPLEMENTS.value,
        "condition_a": {"hit": True},
        "domain_b": Domain.LABS.value,
        "condition_b": {"hit": True},
        "severity": "warn",
        "active": True,
    }
    db_session.add_all(
        [
            ConflictRule(
                code="forged-portable-rule",
                subject_id=None,
                message="forged portable",
                **common,
            ),
            ConflictRule(
                code=None,
                subject_id=None,
                message="legacy custom",
                **common,
            ),
            ConflictRule(
                code=None,
                subject_id=legacy_owner_roots.subject_id,
                message="subject custom",
                **common,
            ),
        ]
    )
    await db_session.commit()

    strict = await conflict_engine.load_scoped_rules(
        db_session,
        scope=_strict_scope(legacy_owner_roots.subject_id),
        active_only=False,
    )
    strict_messages = {row.message for row in strict}
    assert "subject custom" in strict_messages
    assert "legacy custom" not in strict_messages
    assert "forged portable" not in strict_messages

    with pytest.raises(
        conflict_activation_service.ConflictActivationCatalogIntegrityError
    ):
        await conflict_engine.load_scoped_rules(
            db_session,
            scope=conflict_engine.ConflictScope(
                subject_id=legacy_owner_roots.subject_id,
                evaluation_date=EVALUATION_DATE,
                legacy_bridge=conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
            ),
            active_only=False,
        )


async def test_forged_known_catalog_rule_fails_integrity_check(
    db_session,
    legacy_owner_roots,
):
    definition = dict(conflict_catalog.load_rule_catalog()[0])
    code = definition.pop("code")
    definition["message"] = "forged catalog definition"
    db_session.add(ConflictRule(code=code, subject_id=None, **definition))
    await db_session.commit()

    with pytest.raises(conflict_engine.ConflictCatalogIntegrityError):
        await conflict_engine.load_scoped_rules(
            db_session,
            scope=_strict_scope(legacy_owner_roots.subject_id),
            active_only=False,
        )


async def test_tampered_catalog_domains_fail_before_domain_scoped_filter(
    db_session,
    legacy_owner_roots,
):
    await conflict_catalog.sync_catalog(db_session)
    definition = conflict_catalog.load_rule_catalog()[0]
    declared_domain = definition["domain_a"]
    moved_domain = next(
        domain.value for domain in Domain if domain.value != declared_domain
    )
    row = await db_session.scalar(
        select(ConflictRule).where(ConflictRule.code == definition["code"])
    )
    assert row is not None
    row.domain_a = moved_domain
    row.domain_b = moved_domain
    row.active = True
    await db_session.commit()

    scope = _strict_scope(legacy_owner_roots.subject_id)
    with pytest.raises(conflict_engine.ConflictCatalogIntegrityError):
        await conflict_engine.load_scoped_rules(
            db_session,
            scope=scope,
            domain=declared_domain,
            active_only=False,
        )

    with pytest.raises(conflict_engine.ConflictCatalogIntegrityError):
        await conflict_engine.evaluate_scoped(
            db_session,
            scope=scope,
            domain=declared_domain,
        )


async def test_scope_evaluation_date_reaches_every_resolver(
    db_session,
    legacy_owner_roots,
):
    seen = []

    async def left(session, *, scope):
        del session
        seen.append(scope)
        return [{"left": True}]

    async def right(session, *, scope):
        del session
        seen.append(scope)
        return [{"right": True}]

    db_session.add(
        ConflictRule(
            subject_id=legacy_owner_roots.subject_id,
            rule_type="soft_warn",
            domain_a=Domain.SUPPLEMENTS.value,
            condition_a={"left": True},
            domain_b=Domain.LABS.value,
            condition_b={"right": True},
            severity="warn",
            message="date propagated",
            active=True,
        )
    )
    await db_session.commit()
    conflict_engine.register_domain_resolver(Domain.SUPPLEMENTS.value, left)
    conflict_engine.register_domain_resolver(Domain.LABS.value, right)
    scope = _strict_scope(legacy_owner_roots.subject_id)

    violations = await conflict_engine.evaluate_scoped(
        db_session,
        scope=scope,
        domain=Domain.SUPPLEMENTS.value,
    )

    assert [violation.message for violation in violations] == ["date propagated"]
    assert seen == [scope, scope]
    assert all(observed.evaluation_date == EVALUATION_DATE for observed in seen)


async def test_labs_resolver_does_not_read_results_after_evaluation_date(
    db_session,
    legacy_owner_roots,
):
    db_session.add(
        _subject_probe_rule(
            fact_domain=Domain.LABS.value,
            subject_id=legacy_owner_roots.subject_id,
        )
    )
    future = LabResult(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        date=EVALUATION_DATE + timedelta(days=1),
        domain=Domain.LABS.value,
        source=Source.MANUAL.value,
        marker="scope_probe",
        value=42.0,
        flag="high",
    )
    db_session.add(future)
    await db_session.commit()
    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    assert await conflict_engine.evaluate_scoped(
        db_session,
        scope=_strict_scope(legacy_owner_roots.subject_id),
        domain=PROBE_DOMAIN,
        proposed_state={"probe": True},
    ) == []


async def test_active_rule_with_missing_resolver_fails_closed(
    db_session,
    legacy_owner_roots,
):
    db_session.add(
        ConflictRule(
            subject_id=legacy_owner_roots.subject_id,
            rule_type="hard_block",
            domain_a=Domain.SUPPLEMENTS.value,
            condition_a={"hit": True},
            domain_b=Domain.LABS.value,
            condition_b={"hit": True},
            severity="block",
            message="missing resolver must not look safe",
            active=True,
        )
    )
    await db_session.commit()
    conflict_engine.register_domain_resolver(
        Domain.SUPPLEMENTS.value,
        _matching_scoped_resolver,
    )

    with pytest.raises(conflict_engine.ConflictResolverUnavailable, match="labs"):
        await conflict_engine.evaluate_scoped(
            db_session,
            scope=_strict_scope(legacy_owner_roots.subject_id),
            domain=Domain.SUPPLEMENTS.value,
        )


@pytest.mark.parametrize("fact_domain", RESOLVER_DOMAINS)
async def test_legacy_adapter_accepts_only_fully_unowned_facts(
    db_session,
    legacy_owner_roots,
    fact_domain,
):
    """A partial root (S NULL, A non-NULL) is never legacy-owned data.

    This is part of every resolver's contract, not merely an engine-level
    convention: ``FULLY_UNOWNED`` must expand to ``S IS NULL AND A IS NULL``.
    """

    db_session.add(
        _subject_probe_rule(
            fact_domain=fact_domain,
            subject_id=legacy_owner_roots.subject_id,
        )
    )
    await _seed_fact(
        db_session,
        domain=fact_domain,
        subject_id=None,
        actor_user_id=legacy_owner_roots.user_id,
    )
    await db_session.commit()
    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    if fact_domain == Domain.GENETICS.value:
        with pytest.raises(variants.GeneticsOwnershipError, match="partial"):
            await conflict_engine.evaluate_legacy_single_subject(
                db_session,
                domain=PROBE_DOMAIN,
                proposed_state={"probe": True},
                evaluation_date=EVALUATION_DATE,
            )
        return

    partial_only = await conflict_engine.evaluate_legacy_single_subject(
        db_session,
        domain=PROBE_DOMAIN,
        proposed_state={"probe": True},
        evaluation_date=EVALUATION_DATE,
    )
    assert partial_only == []

    await _seed_fact(
        db_session,
        domain=fact_domain,
        subject_id=None,
        actor_user_id=None,
    )
    await db_session.commit()
    fully_unowned = await conflict_engine.evaluate_legacy_single_subject(
        db_session,
        domain=PROBE_DOMAIN,
        proposed_state={"probe": True},
        evaluation_date=EVALUATION_DATE,
    )
    assert [violation.message for violation in fully_unowned] == ["scope probe"]


async def test_legacy_adapter_closes_when_second_subject_exists(
    db_session,
    legacy_owner_roots,
):
    await _add_subject(db_session, label="subject-b")
    await db_session.commit()

    with pytest.raises(LegacySubjectResolutionError):
        await conflict_engine.evaluate_legacy_single_subject(
            db_session,
            domain=Domain.SUPPLEMENTS.value,
            proposed_state={"key": "iron", "active": True},
            evaluation_date=EVALUATION_DATE,
        )


@pytest.mark.parametrize(
    "fact_domain",
    (Domain.GENETICS.value, Domain.LABS.value),
)
async def test_scoped_raw_link_must_match_the_fact_subject(
    db_session,
    legacy_owner_roots,
    fact_domain,
):
    subject_b, user_b = await _add_subject(db_session, label="subject-b")
    foreign_raw = RawPayload(
        subject_id=subject_b.id,
        actor_user_id=user_b.id,
        domain=fact_domain,
        source=Source.MANUAL.value,
        payload={"synthetic": "foreign"},
    )
    db_session.add(foreign_raw)
    await db_session.flush()
    await _seed_fact(
        db_session,
        domain=fact_domain,
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        raw_payload_id=foreign_raw.id,
    )
    db_session.add(
        _subject_probe_rule(
            fact_domain=fact_domain,
            subject_id=legacy_owner_roots.subject_id,
        )
    )
    await db_session.commit()
    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    with pytest.raises(conflict_engine.ConflictRawOwnershipError, match="raw"):
        await conflict_engine.evaluate_scoped(
            db_session,
            scope=_strict_scope(legacy_owner_roots.subject_id),
            domain=PROBE_DOMAIN,
            proposed_state={"probe": True},
        )


@pytest.mark.parametrize(
    "fact_domain",
    (Domain.GENETICS.value, Domain.LABS.value),
)
async def test_legacy_fact_rejects_partial_linked_raw_root(
    db_session,
    legacy_owner_roots,
    fact_domain,
):
    partial_raw = RawPayload(
        subject_id=None,
        actor_user_id=legacy_owner_roots.user_id,
        domain=fact_domain,
        source=Source.MANUAL.value,
        payload={"synthetic": "partial"},
    )
    db_session.add(partial_raw)
    await db_session.flush()
    await _seed_fact(
        db_session,
        domain=fact_domain,
        subject_id=None,
        actor_user_id=None,
        raw_payload_id=partial_raw.id,
    )
    db_session.add(
        _subject_probe_rule(
            fact_domain=fact_domain,
            subject_id=legacy_owner_roots.subject_id,
        )
    )
    await db_session.commit()
    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    with pytest.raises(conflict_engine.ConflictRawOwnershipError, match="raw"):
        await conflict_engine.evaluate_legacy_single_subject(
            db_session,
            domain=PROBE_DOMAIN,
            proposed_state={"probe": True},
            evaluation_date=EVALUATION_DATE,
        )


async def test_legacy_lab_fact_accepts_exact_subject_raw_during_backfill(
    db_session,
    legacy_owner_roots,
):
    fact_domain = Domain.LABS.value
    exact_raw = RawPayload(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        domain=fact_domain,
        source=Source.MANUAL.value,
        payload={"synthetic": "owned-first"},
    )
    db_session.add(exact_raw)
    await db_session.flush()
    await _seed_fact(
        db_session,
        domain=fact_domain,
        subject_id=None,
        actor_user_id=None,
        raw_payload_id=exact_raw.id,
    )
    db_session.add(
        _subject_probe_rule(
            fact_domain=fact_domain,
            subject_id=legacy_owner_roots.subject_id,
        )
    )
    await db_session.commit()
    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    violations = await conflict_engine.evaluate_legacy_single_subject(
        db_session,
        domain=PROBE_DOMAIN,
        proposed_state={"probe": True},
        evaluation_date=EVALUATION_DATE,
    )
    assert [violation.message for violation in violations] == ["scope probe"]


async def test_legacy_manual_genetics_fact_rejects_raw_link_during_backfill(
    db_session,
    legacy_owner_roots,
):
    fact_domain = Domain.GENETICS.value
    exact_raw = RawPayload(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        domain=fact_domain,
        source=Source.MANUAL.value,
        payload={"synthetic": "owned-first"},
    )
    db_session.add(exact_raw)
    await db_session.flush()
    await _seed_fact(
        db_session,
        domain=fact_domain,
        subject_id=None,
        actor_user_id=None,
        raw_payload_id=exact_raw.id,
    )
    db_session.add(
        _subject_probe_rule(
            fact_domain=fact_domain,
            subject_id=legacy_owner_roots.subject_id,
        )
    )
    await db_session.commit()
    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    with pytest.raises(
        conflict_engine.ConflictRawOwnershipError,
        match="manual and MCP",
    ):
        await conflict_engine.evaluate_legacy_single_subject(
            db_session,
            domain=PROBE_DOMAIN,
            proposed_state={"probe": True},
            evaluation_date=EVALUATION_DATE,
        )


async def _seed_hrt_linked_fact(
    db_session,
    *,
    roots,
    compound: HrtCompound,
    link_kind: str,
) -> None:
    if link_kind == "dose":
        db_session.add(
            HrtDose(
                subject_id=roots.subject_id,
                actor_user_id=roots.user_id,
                date=EVALUATION_DATE,
                domain=Domain.HRT.value,
                source=Source.MANUAL.value,
                compound_id=compound.id,
                compound_key=compound.key,
                dose=1.0,
                unit="mg",
            )
        )
        await db_session.flush()
        return
    cycle = HrtCycle(
        subject_id=roots.subject_id,
        actor_user_id=roots.user_id,
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        kind="test",
        start_date=EVALUATION_DATE - timedelta(days=1),
        end_date=EVALUATION_DATE + timedelta(days=1),
    )
    cycle.items.append(
        HrtCycleItem(
            subject_id=roots.subject_id,
            compound_id=compound.id,
            compound_key=compound.key,
            unit="mg",
            schedule=[{"dose": 1.0, "interval_days": 1, "duration_days": 1}],
        )
    )
    db_session.add(cycle)
    await db_session.flush()


def _hrt_compound(
    *,
    source: Source,
    actor_user_id=None,
    key: str | None = None,
    compound_class: str = "scope_class",
    route: str = "oral",
    aromatizes=None,
) -> HrtCompound:
    return HrtCompound(
        subject_id=None,
        actor_user_id=actor_user_id,
        domain=Domain.HRT.value,
        source=source.value,
        key=key or f"scope-probe-{uuid.uuid4().hex}",
        name="Scope probe",
        compound_class=compound_class,
        route=route,
        dose_unit="mg",
        aromatizes=(
            str(aromatizes).lower() if aromatizes is not None else None
        ),
        active=True,
    )


def _hrt_probe_rule(
    subject_id: uuid.UUID,
    *,
    compound_class: str = "scope_class",
) -> ConflictRule:
    return ConflictRule(
        subject_id=subject_id,
        rule_type="soft_warn",
        domain_a=Domain.HRT.value,
        condition_a={"compound_class": compound_class},
        domain_b=PROBE_DOMAIN,
        condition_b={"probe": True},
        severity="warn",
        message="HRT scope probe",
        active=True,
    )


@pytest.mark.parametrize("link_kind", ("dose", "cycle"))
async def test_hrt_partial_global_compound_fails_closed(
    db_session,
    legacy_owner_roots,
    link_kind,
):
    partial = _hrt_compound(
        source=Source.MANUAL,
        actor_user_id=legacy_owner_roots.user_id,
    )
    db_session.add(partial)
    await db_session.flush()
    await _seed_hrt_linked_fact(
        db_session,
        roots=legacy_owner_roots,
        compound=partial,
        link_kind=link_kind,
    )
    db_session.add(_hrt_probe_rule(legacy_owner_roots.subject_id))
    await db_session.commit()
    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    with pytest.raises(conflict_engine.ConflictScopeError, match="unavailable"):
        await conflict_engine.evaluate_scoped(
            db_session,
            scope=_strict_scope(legacy_owner_roots.subject_id),
            domain=PROBE_DOMAIN,
            proposed_state={"probe": True},
        )


@pytest.mark.parametrize("link_kind", ("dose", "cycle"))
async def test_forged_system_source_does_not_make_hrt_compound_global(
    db_session,
    legacy_owner_roots,
    link_kind,
):
    system_compound = _hrt_compound(source=Source.SYSTEM)
    db_session.add(system_compound)
    await db_session.flush()
    await _seed_hrt_linked_fact(
        db_session,
        roots=legacy_owner_roots,
        compound=system_compound,
        link_kind=link_kind,
    )
    db_session.add(_hrt_probe_rule(legacy_owner_roots.subject_id))
    await db_session.commit()
    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    with pytest.raises(conflict_engine.ConflictScopeError, match="unavailable"):
        await conflict_engine.evaluate_scoped(
            db_session,
            scope=_strict_scope(legacy_owner_roots.subject_id),
            domain=PROBE_DOMAIN,
            proposed_state={"probe": True},
        )


@pytest.mark.parametrize("link_kind", ("dose", "cycle"))
async def test_checked_in_hrt_catalog_is_global_without_bridge(
    db_session,
    legacy_owner_roots,
    link_kind,
):
    from vitals.services.hrt import catalog

    key, definition = catalog.load_compound_catalog()[0]
    await catalog.sync_catalog(db_session)
    catalog_compound = await db_session.scalar(
        select(HrtCompound).where(HrtCompound.key == key)
    )
    assert catalog_compound is not None
    await _seed_hrt_linked_fact(
        db_session,
        roots=legacy_owner_roots,
        compound=catalog_compound,
        link_kind=link_kind,
    )
    db_session.add(
        _hrt_probe_rule(
            legacy_owner_roots.subject_id,
            compound_class=definition["compound_class"],
        )
    )
    await db_session.commit()
    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    violations = await conflict_engine.evaluate_scoped(
        db_session,
        scope=_strict_scope(legacy_owner_roots.subject_id),
        domain=PROBE_DOMAIN,
        proposed_state={"probe": True},
    )
    assert [violation.message for violation in violations] == ["HRT scope probe"]


async def test_hrt_catalog_metadata_comes_from_checked_in_definition(
    db_session,
    legacy_owner_roots,
):
    from vitals.services.hrt.catalog import load_compound_catalog

    key, _definition = load_compound_catalog()[0]
    forged = _hrt_compound(
        source=Source.SYSTEM,
        key=key,
        compound_class="scope_class",
    )
    db_session.add(forged)
    await db_session.flush()
    await _seed_hrt_linked_fact(
        db_session,
        roots=legacy_owner_roots,
        compound=forged,
        link_kind="dose",
    )
    db_session.add(_hrt_probe_rule(legacy_owner_roots.subject_id))
    await db_session.commit()
    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    assert await conflict_engine.evaluate_scoped(
        db_session,
        scope=_strict_scope(legacy_owner_roots.subject_id),
        domain=PROBE_DOMAIN,
        proposed_state={"probe": True},
    ) == []


async def test_hrt_foreign_compound_is_not_materialized_before_rejection(
    db_session,
    legacy_owner_roots,
):
    subject_b, user_b = await _add_subject(db_session, label="subject-b")
    foreign = HrtCompound(
        subject_id=subject_b.id,
        actor_user_id=user_b.id,
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        key=f"foreign-{uuid.uuid4().hex}",
        name="foreign private compound",
        compound_class="foreign_private_class",
        route="oral",
        dose_unit="mg",
        active=True,
    )
    db_session.add(foreign)
    await db_session.flush()
    foreign_id = foreign.id
    await _seed_hrt_linked_fact(
        db_session,
        roots=legacy_owner_roots,
        compound=foreign,
        link_kind="dose",
    )
    db_session.add(_hrt_probe_rule(legacy_owner_roots.subject_id))
    await db_session.commit()
    db_session.expunge_all()
    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    with pytest.raises(conflict_engine.ConflictScopeError, match="unavailable"):
        await conflict_engine.evaluate_scoped(
            db_session,
            scope=_strict_scope(legacy_owner_roots.subject_id),
            domain=PROBE_DOMAIN,
            proposed_state={"probe": True},
        )
    assert not any(
        isinstance(row, HrtCompound) and row.id == foreign_id
        for row in db_session.identity_map.values()
    )


async def test_hrt_foreign_cycle_item_is_not_materialized_before_rejection(
    db_session,
    legacy_owner_roots,
):
    subject_b, _ = await _add_subject(db_session, label="subject-b")
    cycle = HrtCycle(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=legacy_owner_roots.user_id,
        domain=Domain.HRT.value,
        source=Source.MANUAL.value,
        kind="test",
        start_date=EVALUATION_DATE - timedelta(days=1),
        end_date=EVALUATION_DATE + timedelta(days=1),
    )
    foreign_item = HrtCycleItem(
        subject_id=subject_b.id,
        compound_key="foreign-private-item",
        unit="mg",
        schedule=[{"dose": 1.0, "interval_days": 1, "duration_days": 1}],
    )
    cycle.items.append(foreign_item)
    db_session.add_all(
        [cycle, _hrt_probe_rule(legacy_owner_roots.subject_id)]
    )

    # The state this exercises is one PostgreSQL will not store.
    # ``fk_hrt_cycle_items_cycle_subject`` is a composite key over
    # ``(cycle_id, subject_id)``, so an item whose subject differs from its
    # cycle's cannot be written at all — a stronger guarantee than the reader
    # refusing to materialize it, and the one production has. SQLite does not
    # enforce the composite key, which is why the row can be built there and why
    # the refusal below is what the fast path exercises. Asserting it both ways
    # is the point: unreachable on the database that ships, still refused on the
    # one the suite runs.
    from sqlalchemy.exc import IntegrityError

    try:
        await db_session.commit()
    except IntegrityError:
        await db_session.rollback()
        return
    foreign_item_id = foreign_item.id
    db_session.expunge_all()
    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    with pytest.raises(conflict_engine.ConflictScopeError, match="outside"):
        await conflict_engine.evaluate_scoped(
            db_session,
            scope=_strict_scope(legacy_owner_roots.subject_id),
            domain=PROBE_DOMAIN,
            proposed_state={"probe": True},
        )
    assert not any(
        isinstance(row, HrtCycleItem) and row.id == foreign_item_id
        for row in db_session.identity_map.values()
    )


@pytest.mark.parametrize("link_kind", ("dose", "cycle"))
async def test_hrt_legacy_custom_compound_needs_exact_one_bridge(
    db_session,
    legacy_owner_roots,
    link_kind,
):
    legacy_compound = _hrt_compound(source=Source.MANUAL)
    db_session.add(legacy_compound)
    await db_session.flush()
    await _seed_hrt_linked_fact(
        db_session,
        roots=legacy_owner_roots,
        compound=legacy_compound,
        link_kind=link_kind,
    )
    db_session.add(_hrt_probe_rule(legacy_owner_roots.subject_id))
    await db_session.commit()
    conflict_registrations.register_all_resolvers()
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    with pytest.raises(conflict_engine.ConflictScopeError, match="unavailable"):
        await conflict_engine.evaluate_scoped(
            db_session,
            scope=_strict_scope(legacy_owner_roots.subject_id),
            domain=PROBE_DOMAIN,
            proposed_state={"probe": True},
        )
    bridged = await conflict_engine.evaluate_legacy_single_subject(
        db_session,
        domain=PROBE_DOMAIN,
        proposed_state={"probe": True},
        evaluation_date=EVALUATION_DATE,
    )
    assert [violation.message for violation in bridged] == ["HRT scope probe"]


async def test_mcp_v1_conflict_read_closes_only_when_a_row_is_unowned(
    db_session,
    session_factory,
    monkeypatch,
    legacy_owner_roots,
):
    """The refusal is about an unowned rule, not about a second person.

    Two versions of this have now been wrong in opposite directions. The legacy
    resolver used to reject any installation holding a second subject, which
    took every page down once professional features made a second subject the
    point. The conflict bridge then kept refusing a layer lower — and this test
    read that as correct, because an unowned rule genuinely does not say whose
    state it was evaluated against.

    Both are true and they are not the same claim. An unowned rule is what
    cannot be attributed; a second subject is what makes attributing it wrong.
    With no unowned rule anywhere, the bridge widens to nothing and the read is
    an ordinary scoped one.
    """

    mcp_router = pytest.importorskip("web.routers.mcp")
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    await _add_subject(db_session, label="subject-b")
    await db_session.commit()

    calls = (
        lambda: mcp_router.check_conflicts(
            Domain.SUPPLEMENTS.value, {"key": "iron", "active": True}
        ),
        lambda: mcp_router.check_supplement_conflicts("iron"),
        lambda: mcp_router.list_conflict_rules(),
    )
    for call in calls:
        await call()

    # Now give it something nobody owns, and the refusal comes back.
    db_session.add(
        ConflictRule(
            subject_id=None,
            code=None,
            rule_type="soft_warn",
            severity="warn",
            domain_a=Domain.SUPPLEMENTS.value,
            condition_a={"key": "iron", "active": True},
            domain_b=Domain.SUPPLEMENTS.value,
            condition_b={"key": "iron", "active": True},
            message="legacy custom rule",
            active=True,
        )
    )
    await db_session.commit()

    for call in calls:
        with pytest.raises(conflict_engine.ConflictLegacyBridgeError):
            await call()


async def test_mcp_missing_resolver_returns_explicit_fail_closed_error(
    db_session,
    session_factory,
    monkeypatch,
    legacy_owner_roots,
):
    del legacy_owner_roots
    mcp_router = pytest.importorskip("web.routers.mcp")
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: session_factory)
    db_session.add(
        ConflictRule(
            subject_id=None,
            rule_type="hard_block",
            domain_a=Domain.SUPPLEMENTS.value,
            condition_a={"key": "iron", "active": True},
            domain_b=Domain.MILESTONES.value,
            condition_b={},
            severity="block",
            message="missing resolver",
            active=True,
        )
    )
    await db_session.commit()
    conflict_registrations.register_all_resolvers()

    supplement_result = await mcp_router.check_supplement_conflicts("iron")
    generic_result = await mcp_router.check_conflicts(
        Domain.SUPPLEMENTS.value,
        {"key": "iron", "active": True},
    )
    assert supplement_result == [{"error": supplement_result[0]["error"]}]
    assert generic_result == [{"error": generic_result[0]["error"]}]
    assert "no scoped conflict resolver" in supplement_result[0]["error"]
    assert "milestones" in supplement_result[0]["error"]
    assert generic_result == supplement_result


@pytest.mark.integration
async def test_postgres_exact_one_bridge_serializes_subject_creation(db_session):
    from vitals.services.identity_service import acquire_identity_governance_lock

    subject_a, _ = await _add_subject(db_session, label="subject-a")
    db_session.add(
        ConflictRule(
            subject_id=subject_a.id,
            rule_type="soft_warn",
            domain_a=Domain.SUPPLEMENTS.value,
            condition_a={"hit": True},
            domain_b=PROBE_DOMAIN,
            condition_b={"probe": True},
            severity="warn",
            message="bridge lock probe",
            active=True,
        )
    )
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    resolver_started = asyncio.Event()
    release_resolver = asyncio.Event()
    writer_started = asyncio.Event()

    async def waiting_resolver(session, *, scope):
        del session, scope
        resolver_started.set()
        await release_resolver.wait()
        return [{"hit": True}]

    conflict_engine.register_domain_resolver(
        Domain.SUPPLEMENTS.value,
        waiting_resolver,
    )
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    async def evaluate_bridge():
        async with factory() as session:
            result = await conflict_engine.evaluate_scoped(
                session,
                scope=conflict_engine.ConflictScope(
                    subject_id=subject_a.id,
                    evaluation_date=EVALUATION_DATE,
                    legacy_bridge=conflict_engine.LegacyConflictBridge.FULLY_UNOWNED,
                ),
                domain=PROBE_DOMAIN,
                proposed_state={"probe": True},
            )
            await session.commit()
            return result

    async def create_second_subject():
        async with factory() as session:
            writer_started.set()
            await acquire_identity_governance_lock(session)
            await _add_subject(session, label="subject-b")
            await session.commit()

    evaluation_task = asyncio.create_task(evaluate_bridge())
    await asyncio.wait_for(resolver_started.wait(), timeout=5)
    writer_task = asyncio.create_task(create_second_subject())
    await asyncio.wait_for(writer_started.wait(), timeout=5)
    await asyncio.sleep(0.2)
    assert not writer_task.done(), "subject writer must wait for bridge proof/read"

    release_resolver.set()
    violations = await asyncio.wait_for(evaluation_task, timeout=5)
    await asyncio.wait_for(writer_task, timeout=5)
    assert [violation.message for violation in violations] == ["bridge lock probe"]

    async with factory() as session:
        with pytest.raises(LegacySubjectResolutionError):
            await conflict_engine.evaluate_legacy_single_subject(
                session,
                domain=PROBE_DOMAIN,
                proposed_state={"probe": True},
                evaluation_date=EVALUATION_DATE,
            )


@pytest.mark.integration
async def test_postgres_mcp_conflict_read_serializes_owner_suspension(
    db_session,
    monkeypatch,
    legacy_owner_roots,
):
    from vitals.services.identity_service import assign_role, change_user_status

    mcp_router = pytest.importorskip("web.routers.mcp")
    backup_admin = User(
        username="backup-admin",
        normalized_username="backup-admin",
        password_hash="synthetic-test-hash",
        status=UserStatus.ACTIVE.value,
    )
    db_session.add(backup_admin)
    await db_session.flush()
    await assign_role(
        db_session,
        user_id=backup_admin.id,
        role=UserRoleName.PLATFORM_SUPERADMIN,
        assigned_by_user_id=legacy_owner_roots.user_id,
    )
    db_session.add(
        ConflictRule(
            subject_id=legacy_owner_roots.subject_id,
            rule_type="soft_warn",
            domain_a=Domain.SUPPLEMENTS.value,
            condition_a={"hit": True},
            domain_b=PROBE_DOMAIN,
            condition_b={"probe": True},
            severity="warn",
            message="owner lock probe",
            active=True,
        )
    )
    await db_session.commit()
    assert db_session.bind is not None
    factory = async_sessionmaker(
        db_session.bind,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    monkeypatch.setattr(mcp_router, "get_session_factory", lambda: factory)
    resolver_started = asyncio.Event()
    release_resolver = asyncio.Event()
    suspension_started = asyncio.Event()

    async def waiting_resolver(session, *, scope):
        del session, scope
        resolver_started.set()
        await release_resolver.wait()
        return [{"hit": True}]

    conflict_engine.register_domain_resolver(
        Domain.SUPPLEMENTS.value,
        waiting_resolver,
    )
    conflict_engine.register_domain_resolver(PROBE_DOMAIN, _empty_scoped_resolver)

    async def suspend_owner():
        async with factory() as session:
            suspension_started.set()
            await change_user_status(
                session,
                user_id=legacy_owner_roots.user_id,
                new_status=UserStatus.SUSPENDED,
                actor_user_id=backup_admin.id,
            )
            await session.commit()

    evaluation_task = asyncio.create_task(
        mcp_router.check_conflicts(PROBE_DOMAIN, {"probe": True})
    )
    await asyncio.wait_for(resolver_started.wait(), timeout=5)
    suspension_task = asyncio.create_task(suspend_owner())
    await asyncio.wait_for(suspension_started.wait(), timeout=5)
    await asyncio.sleep(0.2)
    assert not suspension_task.done(), "owner suspension must wait for MCP read"

    release_resolver.set()
    result = await asyncio.wait_for(evaluation_task, timeout=5)
    await asyncio.wait_for(suspension_task, timeout=5)
    assert [row["message"] for row in result] == ["owner lock probe"]

    with pytest.raises(LegacyOwnerResolutionError):
        await mcp_router.check_conflicts(PROBE_DOMAIN, {"probe": True})
