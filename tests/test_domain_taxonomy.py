"""Contract ratchets for the stable domain vocabulary."""
from __future__ import annotations

import pytest

from vitals.domain_taxonomy import (
    DOMAIN_TAXONOMY,
    DomainFieldSemantics,
    taxonomy_for,
)
from vitals.enums import Domain, RECORD_SECTIONS


def test_taxonomy_is_exhaustive_and_agrees_with_record_sections() -> None:
    assert tuple(DOMAIN_TAXONOMY) == tuple(Domain)
    assert tuple(
        spec.domain for spec in DOMAIN_TAXONOMY.values() if spec.is_record_section
    ) == RECORD_SECTIONS


def test_canonical_names_and_aliases_are_globally_unambiguous() -> None:
    accepted: list[str] = []
    for domain, spec in DOMAIN_TAXONOMY.items():
        assert spec.domain is domain
        assert spec.canonical_name == domain.value
        assert domain.value not in spec.aliases
        accepted.extend(spec.accepted_names)

    assert len(accepted) == len(set(accepted))
    assert taxonomy_for("hevy").domain is Domain.WORKOUTS
    assert taxonomy_for("reports").domain is Domain.MILESTONES
    assert taxonomy_for("body_composition").domain is Domain.BODY_COMPOSITION
    with pytest.raises(ValueError, match="unknown domain or alias"):
        taxonomy_for("unknown")


def test_module_keys_exist_without_making_system_a_user_module() -> None:
    from vitals.services.modules_service import MODULE_REGISTRY

    without_module = {
        spec.domain for spec in DOMAIN_TAXONOMY.values() if spec.module_key is None
    }
    assert without_module == {Domain.SYSTEM}
    for spec in DOMAIN_TAXONOMY.values():
        if spec.module_key is not None:
            assert spec.module_key in MODULE_REGISTRY
    assert DOMAIN_TAXONOMY[Domain.SYSTEM].module_key is None


def test_cross_surface_module_maps_use_the_canonical_domain_vocabulary() -> None:
    from vitals.services.digest.projection.contracts import _DOMAIN_MODULE as digest_modules
    from vitals.services.share.snapshot import DOMAIN_MODULE as share_modules

    expected = {
        domain.value: spec.module_key
        for domain, spec in DOMAIN_TAXONOMY.items()
        if spec.module_key is not None
    }
    assert {key: digest_modules[key] for key in expected} == expected
    assert set(share_modules) <= set(expected)
    assert share_modules == {key: expected[key] for key in share_modules}
    # System alerts can contribute to a report, but SYSTEM is not itself a
    # switchable health module.  This remains a digest policy, not taxonomy.
    assert digest_modules[Domain.SYSTEM.value] == "reports"


def test_target_selector_contexts_are_explicit() -> None:
    selectors = {
        spec.domain
        for spec in DOMAIN_TAXONOMY.values()
        if spec.field_semantics is DomainFieldSemantics.TARGET_SELECTOR
    }
    assert selectors == {Domain.TIMELINE, Domain.SYSTEM}
    assert (
        DOMAIN_TAXONOMY[Domain.MILESTONES].field_semantics
        is DomainFieldSemantics.MIXED
    )


def test_mcp_record_scope_covers_every_record_section() -> None:
    from web.routers.mcp import _RECORD_DOMAIN_KEYS

    assert _RECORD_DOMAIN_KEYS == tuple(domain.value for domain in RECORD_SECTIONS)
