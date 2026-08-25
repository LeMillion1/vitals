"""The care record reads the consent allowlist, not a full digest then filters."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from vitals.access import (
    AccessContext,
    AccessScope,
    PolicyAction,
    PolicyResourceType,
    Principal,
    RelationshipGrant,
)
from vitals.enums import Domain, UserRoleName
from vitals.services import modules_service
from vitals.services.care import record_projection


def _professional_context(*domains: Domain) -> AccessContext:
    now = datetime.now(timezone.utc)
    professional_id = uuid.uuid4()
    subject_id = uuid.uuid4()
    return AccessContext(
        principal=Principal(
            user_id=professional_id,
            roles=frozenset({UserRoleName.DOCTOR}),
        ),
        subject_id=subject_id,
        subject_owner_user_id=uuid.uuid4(),
        evaluated_at=now,
        relationship_grant=RelationshipGrant(
            relationship_id=uuid.uuid4(),
            consent_grant_id=uuid.uuid4(),
            professional_user_id=professional_id,
            subject_id=subject_id,
            consent_version=1,
            expires_at=now + timedelta(hours=1),
            scopes=frozenset(
                AccessScope(
                    resource_type=PolicyResourceType.DOMAIN,
                    resource_key=domain.value,
                    action=PolicyAction.READ,
                )
                for domain in domains
            ),
        ),
    )


def _owner_context() -> AccessContext:
    user_id = uuid.uuid4()
    subject_id = uuid.uuid4()
    return AccessContext(
        principal=Principal(user_id=user_id),
        subject_id=subject_id,
        subject_owner_user_id=user_id,
        evaluated_at=datetime.now(timezone.utc),
    )


async def _record_sql(db_session, operation):
    statements: list[str] = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.lower().replace('"', "").split()))

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = await operation()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    return result, statements


async def test_labs_only_consent_never_queries_other_record_domains(db_session):
    context = _professional_context(Domain.LABS)
    enabled = {key: True for key in modules_service.MODULE_REGISTRY}

    projection, statements = await _record_sql(
        db_session,
        lambda: record_projection.assemble_record_projection(
            db_session,
            context=context,
            enabled_modules=enabled,
        ),
    )
    sql = "\n".join(statements)

    assert projection.loaded_domains == (Domain.LABS.value,)
    assert set(projection.record) == {"labs"}
    assert "lab_results" in sql
    for forbidden_table in (
        "health_profiles",
        "weight_logs",
        "body_measurements",
        "body_scans",
        "garmin_daily",
        "garmin_activities",
        "meal_logs",
        "hrt_cycles",
        "glp1_dose_phases",
        "supplements",
        "skincare_products",
        "skincare_observations",
        "genetic_variants",
        "hevy_workouts",
        "system_alerts",
        "milestones",
    ):
        assert forbidden_table not in sql, forbidden_table


async def test_disabled_core_and_optional_modules_are_not_queried(db_session):
    context = _owner_context()
    enabled = {key: False for key in modules_service.MODULE_REGISTRY}
    enabled["nutrition"] = True

    projection, statements = await _record_sql(
        db_session,
        lambda: record_projection.assemble_record_projection(
            db_session,
            context=context,
            enabled_modules=enabled,
        ),
    )
    sql = "\n".join(statements)

    assert projection.loaded_domains == (Domain.NUTRITION.value,)
    assert set(projection.record) == {"nutrition"}
    assert "meal_logs" in sql
    for forbidden_table in (
        "weight_logs",
        "body_measurements",
        "lab_results",
        "garmin_daily",
        "body_scans",
        "hrt_cycles",
        "glp1_dose_phases",
        "supplements",
        "skincare_products",
        "genetic_variants",
        "hevy_workouts",
    ):
        assert forbidden_table not in sql, forbidden_table


async def test_weight_consent_does_not_read_body_composition_tables(db_session):
    context = _professional_context(Domain.WEIGHT)
    enabled = {key: False for key in modules_service.MODULE_REGISTRY}
    enabled["weight"] = True

    projection, statements = await _record_sql(
        db_session,
        lambda: record_projection.assemble_record_projection(
            db_session,
            context=context,
            enabled_modules=enabled,
        ),
    )
    sql = "\n".join(statements)

    assert projection.loaded_domains == (Domain.WEIGHT.value,)
    assert set(projection.record) == {"weight"}
    assert "weight_logs" in sql
    assert "body_measurements" not in sql
    assert "body_scans" not in sql
    assert "glp1_dose_phases" not in sql


@pytest.mark.parametrize(
    "section",
    record_projection.SECTIONS,
    ids=lambda section: section.key,
)
async def test_each_record_card_uses_only_its_authorized_loader(
    db_session, monkeypatch, section
):
    context = _professional_context(section.domain)
    enabled = {key: False for key in modules_service.MODULE_REGISTRY}
    enabled[section.module] = True
    called: list[str] = []

    async def loaded(_session, _subject_id, _window):
        called.append(section.key)
        return record_projection._LoadedSection(value={}, row_count=0)

    monkeypatch.setitem(record_projection._LOADERS, section.key, loaded)

    projection = await record_projection.assemble_record_projection(
        db_session,
        context=context,
        enabled_modules=enabled,
    )

    assert called == [section.key]
    assert projection.loaded_domains == (section.domain.value,)
    assert set(projection.record) == {section.key}
