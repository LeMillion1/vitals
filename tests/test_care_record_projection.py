"""The care record reads the consent allowlist, not a full digest then filters."""

from __future__ import annotations

from vitals.services.genetics import queries as genetics_queries
from vitals.services.genetics import validation as genetics_validation

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from freezegun import freeze_time
from sqlalchemy import event

from vitals.access import (
    AccessContext,
    AccessScope,
    PolicyAction,
    PolicyResourceType,
    Principal,
    RelationshipGrant,
    SupportGrant,
)
from vitals.enums import (
    Domain,
    Source,
    SupportAccessMode,
    SupportAccessStatus,
    UserRoleName,
)
from vitals.models.genetics import GeneticVariant
from vitals.models.labs import LabResult
from vitals.models.nutrition import MealLog
from vitals.models.raw_payload import RawPayload
from vitals.models.weight import WeightLog
from vitals.services.modules import preferences as modules_service
from vitals.services.care import record_projection
import vitals.services.labs.results as lab_results
from vitals.services import weight as weight_domain


def test_care_consent_domains_come_from_the_projection_registry():
    enabled = {section.module: True for section in record_projection.SECTIONS}
    enabled["skincare"] = False
    enabled["timeline"] = True

    domains = record_projection.enabled_care_domains(enabled)

    assert Domain.SKINCARE not in domains
    assert Domain.TIMELINE not in domains
    assert Domain.MILESTONES not in domains
    assert domains == tuple(
        section.domain for section in record_projection.SECTIONS if section.module != "skincare"
    )


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


def _support_context(*domains: Domain) -> AccessContext:
    now = datetime.now(timezone.utc)
    admin_id = uuid.uuid4()
    subject_id = uuid.uuid4()
    scopes = frozenset(
        AccessScope(
            resource_type=PolicyResourceType.DOMAIN,
            resource_key=domain.value,
            action=PolicyAction.READ,
        )
        for domain in domains
    )
    return AccessContext(
        principal=Principal(
            user_id=admin_id,
            roles=frozenset({UserRoleName.PLATFORM_SUPERADMIN}),
        ),
        subject_id=subject_id,
        subject_owner_user_id=uuid.uuid4(),
        evaluated_at=now,
        support_grant=SupportGrant(
            grant_id=uuid.uuid4(),
            granted_to_user_id=admin_id,
            subject_id=subject_id,
            mode=SupportAccessMode.READ,
            status=SupportAccessStatus.ACTIVE,
            expires_at=now + timedelta(minutes=30),
            scopes=scopes,
        ),
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
            subject_timezone_name="UTC",
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
            subject_timezone_name="UTC",
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
            subject_timezone_name="UTC",
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
async def test_each_record_card_uses_only_its_authorized_loader(db_session, monkeypatch, section):
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
        subject_timezone_name="UTC",
    )

    assert called == [section.key]
    assert projection.loaded_domains == (section.domain.value,)
    assert set(projection.record) == {section.key}


async def test_support_projection_does_not_name_ungranted_enabled_modules(db_session):
    context = _support_context(Domain.LABS)
    enabled = {key: True for key in modules_service.MODULE_REGISTRY}

    projection = await record_projection.assemble_record_projection(
        db_session,
        context=context,
        enabled_modules=enabled,
        subject_timezone_name="UTC",
    )

    assert projection.restricted is True
    assert projection.withheld_domains == ()
    assert projection.loaded_domains == (Domain.LABS.value,)
    template = (Path(__file__).parents[1] / "web/templates/care/_record.html").read_text()
    assert "care.is_support and record_restricted" in template
    assert "care.record_withheld_support" not in template


async def test_record_window_uses_target_subject_timezone(db_session):
    context = _owner_context()
    enabled = {key: False for key in modules_service.MODULE_REGISTRY}

    with freeze_time("2026-01-01 00:30:00+00:00"):
        projection = await record_projection.assemble_record_projection(
            db_session,
            context=context,
            enabled_modules=enabled,
            subject_timezone_name="America/Los_Angeles",
        )

    assert projection.period["report_date"] == "2025-12-31"
    assert projection.period["period_start"] == "2025-12-25"
    assert projection.period["period_end"] == "2025-12-31"
    assert projection.period["mode"] == "current_period"


async def test_current_care_window_includes_today_and_excludes_tomorrow(
    db_session, legacy_owner_roots
):
    subject_id = legacy_owner_roots.subject_id
    today = date(2026, 9, 5)
    tomorrow = today + timedelta(days=1)
    db_session.add_all(
        [
            WeightLog(
                subject_id=subject_id,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                date=today,
                weight_kg=72.5,
            ),
            WeightLog(
                subject_id=subject_id,
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                date=tomorrow,
                weight_kg=99,
            ),
            LabResult(
                subject_id=subject_id,
                date=today,
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="Today marker",
                value=11,
                flag="high",
            ),
            LabResult(
                subject_id=subject_id,
                date=tomorrow,
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="Tomorrow marker",
                value=12,
                flag="high",
            ),
        ]
    )
    await db_session.flush()
    context = AccessContext(
        principal=Principal(user_id=legacy_owner_roots.user_id),
        subject_id=subject_id,
        subject_owner_user_id=legacy_owner_roots.user_id,
        evaluated_at=datetime.now(timezone.utc),
    )
    enabled = {key: False for key in modules_service.MODULE_REGISTRY}
    enabled.update({"weight": True, "labs": True})

    with freeze_time("2026-09-05 06:00:00+00:00"):
        projection = await record_projection.assemble_record_projection(
            db_session,
            context=context,
            enabled_modules=enabled,
            subject_timezone_name="Asia/Almaty",
        )

    assert projection.period == {
        "report_date": "2026-09-05",
        "period_days": 7,
        "mode": "current_period",
        "period_start": "2026-08-30",
        "period_end": "2026-09-05",
        "previous_start": "2026-08-23",
        "previous_end": "2026-08-29",
    }
    assert projection.record["weight"]["latest_kg"] == 72.5
    assert projection.record["weight"]["latest_date"] == "2026-09-05"
    assert [
        row["marker"] for row in projection.record["labs"]["out_of_range"]
    ] == ["Today marker"]
    for domain in ("weight", "labs"):
        assert projection.coverage[domain]["current_rows"] == 1
        assert projection.coverage[domain]["freshness_days"] == 0


async def test_weight_care_history_is_bounded_without_selecting_raw_payload_json(
    db_session, legacy_owner_roots
):
    raw = RawPayload(
        subject_id=legacy_owner_roots.subject_id,
        actor_user_id=None,
        domain=Domain.WEIGHT.value,
        source=Source.MANUAL.value,
        payload={"ungranted_sleep_bytes": "synthetic-sentinel"},
    )
    db_session.add(raw)
    await db_session.flush()
    for offset in range(4):
        db_session.add(
            WeightLog(
                subject_id=legacy_owner_roots.subject_id,
                actor_user_id=None,
                date=date(2026, 1, 1) + timedelta(days=offset),
                domain=Domain.WEIGHT.value,
                source=Source.MANUAL.value,
                weight_kg=80 - offset,
                raw_payload_id=raw.id if offset == 3 else None,
                superseded=False,
            )
        )
    await db_session.flush()

    history, statements = await _record_sql(
        db_session,
        lambda: weight_domain.queries.care_weight_history(
            db_session,
            subject_id=legacy_owner_roots.subject_id,
            end=date(2026, 1, 10),
            history_limit=2,
            noise_limit=2,
        ),
    )

    assert [row.date for row in history.rows] == [date(2026, 1, 3), date(2026, 1, 4)]
    assert history.history_truncated is True
    selected_sql = "\n".join(statement for statement in statements if "select" in statement)
    assert "raw_payloads.payload" not in selected_sql
    assert len(statements) <= 8


async def test_labs_latest_per_marker_is_not_displaced_and_reports_truncation(
    db_session, legacy_owner_roots
):
    subject_id = legacy_owner_roots.subject_id
    db_session.add_all(
        [
            LabResult(
                subject_id=subject_id,
                date=date(2026, 1, 1),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="A",
                value=9,
                flag="high",
            ),
            LabResult(
                subject_id=subject_id,
                date=date(2026, 1, 2),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="A",
                value=5,
                flag="normal",
            ),
            LabResult(
                subject_id=subject_id,
                date=date(2026, 1, 2),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="B",
                value=11,
                flag="high",
            ),
        ]
    )
    await db_session.flush()

    full = await lab_results.bounded_latest_results_by_marker(
        db_session,
        subject_id=subject_id,
        end=date(2026, 1, 3),
        marker_limit=2,
    )
    bounded = await lab_results.bounded_latest_results_by_marker(
        db_session,
        subject_id=subject_id,
        end=date(2026, 1, 3),
        marker_limit=1,
    )

    assert [(row.marker, row.flag) for row in full.rows] == [
        ("A", "normal"),
        ("B", "high"),
    ]
    assert full.truncated is False
    assert bounded.truncated is True


async def test_care_labs_separate_recent_unevaluated_results(
    db_session,
    legacy_owner_roots,
):
    subject_id = legacy_owner_roots.subject_id
    db_session.add_all(
        [
            LabResult(
                subject_id=subject_id,
                date=date(2026, 1, 10),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="Explicit abnormal",
                value=11,
                flag="high",
            ),
            LabResult(
                subject_id=subject_id,
                date=date(2026, 1, 10),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="Missing reference",
                value=7,
                flag=None,
            ),
            LabResult(
                subject_id=subject_id,
                date=date(2026, 1, 10),
                domain=Domain.LABS.value,
                source=Source.MANUAL.value,
                marker="Explicit normal",
                value=5,
                flag="normal",
            ),
        ]
    )
    await db_session.flush()
    context = AccessContext(
        principal=Principal(user_id=legacy_owner_roots.user_id),
        subject_id=subject_id,
        subject_owner_user_id=legacy_owner_roots.user_id,
        evaluated_at=datetime.now(timezone.utc),
    )
    enabled = {key: False for key in modules_service.MODULE_REGISTRY}
    enabled[Domain.LABS.value] = True

    projection = await record_projection.assemble_record_projection(
        db_session,
        context=context,
        enabled_modules=enabled,
        subject_timezone_name="UTC",
        on_date=date(2026, 1, 16),
    )

    labs = projection.record["labs"]
    assert [row["marker"] for row in labs["out_of_range"]] == [
        "Explicit abnormal"
    ]
    assert [row["marker"] for row in labs["not_evaluated"]] == [
        "Missing reference"
    ]
    assert labs["not_evaluated"][0]["flag"] is None

    from vitals.i18n import current_lang
    from web.templating import templates

    language = current_lang.set("en")
    try:
        rendered = templates.get_template("care/_record.html").render(
            record={"labs": labs},
            coverage={"labs": {"status": "available", "truncated": False}},
            period={"period_start": "2026-01-10", "period_end": "2026-01-16"},
            care=SimpleNamespace(is_support=False),
            record_restricted=False,
            withheld_domains=[],
        )
    finally:
        current_lang.reset(language)
    assert "Missing reference" in rendered
    assert "not evaluated" in rendered

    template = (
        Path(__file__).parents[1] / "web/templates/care/_record.html"
    ).read_text()
    assert "row.flag or 'normal'" not in template
    assert 't("labs.not_evaluated") if f is none' in template


async def test_nutrition_unknown_macro_stays_unknown_with_sample_counts(
    db_session, legacy_owner_roots
):
    subject_id = legacy_owner_roots.subject_id
    db_session.add(
        MealLog(
            subject_id=subject_id,
            date=date(2026, 1, 2),
            domain=Domain.NUTRITION.value,
            source=Source.MANUAL.value,
            name="Synthetic meal",
            calories=500,
            protein_g=None,
        )
    )
    await db_session.flush()
    context = AccessContext(
        principal=Principal(user_id=legacy_owner_roots.user_id),
        subject_id=subject_id,
        subject_owner_user_id=legacy_owner_roots.user_id,
        evaluated_at=datetime.now(timezone.utc),
    )
    enabled = {key: False for key in modules_service.MODULE_REGISTRY}
    enabled["nutrition"] = True

    projection = await record_projection.assemble_record_projection(
        db_session,
        context=context,
        enabled_modules=enabled,
        subject_timezone_name="UTC",
        on_date=date(2026, 1, 3),
    )

    nutrition = projection.record["nutrition"]
    assert nutrition["avg_calories_per_day"] == 500
    assert nutrition["avg_protein_per_day_g"] is None
    assert nutrition["metric_samples"] == {"calories": 1, "protein_g": 0}


async def test_bounded_genetics_order_is_deterministic_with_null_rsid(
    db_session, legacy_owner_roots
):
    subject_id = legacy_owner_roots.subject_id
    db_session.add_all(
        [
            GeneticVariant(
                subject_id=subject_id,
                domain=Domain.GENETICS.value,
                source=Source.MANUAL.value,
                gene="GENE",
                rsid=None,
                marker="without_rsid",
            ),
            GeneticVariant(
                subject_id=subject_id,
                domain=Domain.GENETICS.value,
                source=Source.MANUAL.value,
                gene="GENE",
                rsid="rs2",
                marker="with_rsid",
            ),
        ]
    )
    await db_session.flush()

    page = await genetics_queries.bounded_variants(
        db_session,
        subject_id=subject_id,
        limit=2,
    )

    assert [row.marker for row in page.rows] == ["with_rsid", "without_rsid"]
    assert page.truncated is False


async def test_genetics_bounded_validation_caches_shared_raw_parse(db_session, monkeypatch):
    subject_id = uuid.uuid4()
    owner_user_id = uuid.uuid4()
    payload = {
        "filename": "synthetic.vcf",
        "variants": [
            ["rs1", "A", "G", "A/G"],
            ["rs2", "C", "T", "C/T"],
        ],
        "truncated": False,
    }
    raw = SimpleNamespace(
        id=7,
        subject_id=subject_id,
        actor_user_id=owner_user_id,
        integration_connection_id=None,
        file_asset_id=None,
        domain=Domain.GENETICS.value,
        source=Source.VCF_IMPORT.value,
        external_id=genetics_validation._vcf_external_id(payload),
        payload=payload,
    )
    rows = [
        SimpleNamespace(
            subject_id=subject_id,
            actor_user_id=owner_user_id,
            domain=Domain.GENETICS.value,
            source=Source.VCF_IMPORT.value,
            raw_payload_id=7,
            rsid=rsid,
        )
        for rsid in ("rs1", "rs2")
    ]
    raw_loads = 0
    raw_parses = 0
    original_parse = genetics_validation._raw_normalization_variants

    async def load_raw(_session, _raw_payload_id, *, for_update):
        nonlocal raw_loads
        assert for_update is False
        raw_loads += 1
        return raw

    def parse_raw(value):
        nonlocal raw_parses
        raw_parses += 1
        return original_parse(value)

    monkeypatch.setattr(genetics_validation, "_load_raw", load_raw)
    monkeypatch.setattr(genetics_validation, "_raw_normalization_variants", parse_raw)
    raw_cache = {}
    raw_rsid_cache = {}
    for row in rows:
        await genetics_validation._validate_variant_graph(
            db_session,
            row=row,
            subject_id=subject_id,
            for_update=False,
            raw_cache=raw_cache,
            raw_rsid_cache=raw_rsid_cache,
            owner_user_id=owner_user_id,
        )

    assert raw_loads == 1
    assert raw_parses == 1
