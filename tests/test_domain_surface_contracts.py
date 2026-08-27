"""Explicit dispositions for domain-aware projections and delivery surfaces.

The taxonomy owns stable vocabulary only.  Each surface keeps its own inclusion
policy, while these tests make additions and removals deliberate and verify that
the local policy still refers to the shared module vocabulary.
"""
from __future__ import annotations

from vitals.domain_taxonomy import DOMAIN_TAXONOMY
from vitals.enums import Domain, RECORD_SECTIONS


SHARE_DOMAINS = frozenset(
    {
        Domain.WEIGHT,
        Domain.BODY_COMPOSITION,
        Domain.LABS,
        Domain.GLP1,
        Domain.HRT,
        Domain.SUPPLEMENTS,
        Domain.GARMIN,
        Domain.WORKOUTS,
        Domain.NUTRITION,
        Domain.SKINCARE,
        Domain.GENETICS,
    }
)

CARE_PROJECTION_DOMAINS = SHARE_DOMAINS

CHART_DOMAINS = frozenset(
    {
        Domain.WEIGHT,
        Domain.BODY_COMPOSITION,
        Domain.GLP1,
        Domain.SKINCARE,
        Domain.WORKOUTS,
        Domain.GARMIN,
        Domain.LABS,
        Domain.NUTRITION,
    }
)


def _module_key(domain: Domain) -> str:
    key = DOMAIN_TAXONOMY[domain].module_key
    assert key is not None, f"record domain {domain.value!r} has no module key"
    return key


def test_share_policy_is_exact_and_uses_taxonomy_module_keys() -> None:
    from vitals.services.share import snapshot

    selected = {Domain(value) for value in snapshot.DOMAIN_MODULE}
    assert selected == SHARE_DOMAINS
    assert set(snapshot._BUILDERS) == set(snapshot.DOMAIN_MODULE)
    assert snapshot.DOMAIN_ORDER == tuple(snapshot.DOMAIN_MODULE)
    assert snapshot.DOMAIN_MODULE == {
        domain.value: _module_key(domain)
        for domain in map(Domain, snapshot.DOMAIN_MODULE)
    }


def test_care_projection_policy_is_exact_and_uses_taxonomy_module_keys() -> None:
    from vitals.services.care import record_projection

    sections = record_projection.SECTIONS
    assert frozenset(record_projection.CARE_DOMAINS) == CARE_PROJECTION_DOMAINS
    assert tuple(section.domain for section in sections) == (
        record_projection.CARE_DOMAINS
    )
    assert len(sections) == len({section.domain for section in sections})
    assert len(sections) == len({section.key for section in sections})
    for section in sections:
        expected_module = _module_key(section.domain)
        assert section.module == expected_module
        assert section.key == expected_module


def test_care_consent_defaults_cover_every_record_section() -> None:
    from vitals.services.care import relationships

    assert relationships.DEFAULT_DOMAINS == RECORD_SECTIONS


def test_chart_policy_is_exact_and_uses_taxonomy_module_keys() -> None:
    from vitals.analytics import chart_registry

    module_to_domain = {
        spec.module_key: domain
        for domain, spec in DOMAIN_TAXONOMY.items()
        if spec.module_key is not None
    }
    chart_module_keys = set(chart_registry.all_domains())

    assert chart_module_keys == {_module_key(domain) for domain in CHART_DOMAINS}
    assert {module_to_domain[key] for key in chart_module_keys} == CHART_DOMAINS
    assert set(chart_registry.DOMAIN_LABELS) == chart_module_keys
    for metric in chart_registry.REGISTRY.values():
        assert metric.domain in module_to_domain
        assert metric.module_key in {None, metric.domain}


def test_portability_dispositions_keep_control_artifacts_out_of_v2() -> None:
    from vitals.ownership import OWNERSHIP_REGISTRY
    from vitals.services.portability.graph import EXCLUDED_PORTABLE_TABLES
    from vitals.services.portability.schema import EXPLICIT_EXCLUDED_TABLES

    # Backup v1 derives its generic inclusion boundary from ``user_portable``.
    # These two historical subject tables therefore remain v1-eligible even
    # though v2 explicitly treats them as derived control/outbox state.
    v1_eligible_but_v2_explicitly_excluded = frozenset(
        {"garmin_weight_exports", "system_alerts"}
    )
    assert {
        table
        for table in v1_eligible_but_v2_explicitly_excluded
        if OWNERSHIP_REGISTRY[table].user_portable
    } == v1_eligible_but_v2_explicitly_excluded
    assert EXPLICIT_EXCLUDED_TABLES == v1_eligible_but_v2_explicitly_excluded
    assert EXCLUDED_PORTABLE_TABLES == EXPLICIT_EXCLUDED_TABLES

    # Generated narratives, public snapshots, and delivery state retain local
    # ownership/provenance and are excluded by the shared reviewed registry in
    # both portability formats.
    retained_local = frozenset(
        {
            "weekly_digests",
            "shared_reports",
            "notifications",
            "notification_delivery_intents",
        }
    )
    assert {
        table for table in retained_local if not OWNERSHIP_REGISTRY[table].user_portable
    } == retained_local
