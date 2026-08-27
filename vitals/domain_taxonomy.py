"""Stable vocabulary shared by Vitals' bounded health contexts.

This manifest deliberately does not decide whether a domain is exposed through
MCP, sharing, charts, portability, or scheduling.  Those are audience-specific
policies.  It only names the concepts those policies refer to, so aliases and
module keys cannot drift independently between delivery surfaces.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Mapping

from vitals.enums import Domain


class DomainFieldSemantics(StrEnum):
    """Meaning of a bounded context's principal ``domain`` field."""

    DISCRIMINATOR = "discriminator"
    TARGET_SELECTOR = "target_selector"
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class DomainSpec:
    """Canonical, surface-independent identity of one bounded context."""

    domain: Domain
    module_key: str | None
    aliases: frozenset[str] = frozenset()
    is_record_section: bool = True
    field_semantics: DomainFieldSemantics = DomainFieldSemantics.DISCRIMINATOR

    @property
    def canonical_name(self) -> str:
        return self.domain.value

    @property
    def accepted_names(self) -> frozenset[str]:
        return self.aliases | {self.canonical_name}


_SPECS: tuple[DomainSpec, ...] = (
    DomainSpec(Domain.WEIGHT, "weight"),
    DomainSpec(
        Domain.BODY_COMPOSITION,
        "body_comp",
        aliases=frozenset({"body_composition"}),
    ),
    DomainSpec(Domain.GLP1, "glp1"),
    DomainSpec(Domain.SUPPLEMENTS, "supplements"),
    DomainSpec(Domain.GENETICS, "genetics"),
    DomainSpec(Domain.SKINCARE, "skincare"),
    DomainSpec(Domain.WORKOUTS, "hevy", aliases=frozenset({"hevy"})),
    DomainSpec(Domain.GARMIN, "garmin"),
    DomainSpec(Domain.LABS, "labs"),
    DomainSpec(Domain.NUTRITION, "nutrition"),
    DomainSpec(Domain.HRT, "hrt"),
    DomainSpec(
        Domain.MILESTONES,
        "reports",
        aliases=frozenset({"reports"}),
        # Milestone.domain targets a health area, while WeeklyDigest.domain
        # discriminates the artifact's owning context.
        field_semantics=DomainFieldSemantics.MIXED,
    ),
    DomainSpec(
        Domain.TIMELINE,
        "timeline",
        field_semantics=DomainFieldSemantics.TARGET_SELECTOR,
    ),
    DomainSpec(
        Domain.SYSTEM,
        None,
        is_record_section=False,
        field_semantics=DomainFieldSemantics.TARGET_SELECTOR,
    ),
)

DOMAIN_TAXONOMY: Final[Mapping[Domain, DomainSpec]] = MappingProxyType(
    {spec.domain: spec for spec in _SPECS}
)

_DOMAIN_BY_NAME: Final[Mapping[str, Domain]] = MappingProxyType(
    {name: spec.domain for spec in _SPECS for name in spec.accepted_names}
)


def taxonomy_for(domain: Domain | str) -> DomainSpec:
    """Return the canonical specification for a domain or accepted alias."""

    if isinstance(domain, Domain):
        return DOMAIN_TAXONOMY[domain]
    try:
        return DOMAIN_TAXONOMY[_DOMAIN_BY_NAME[domain]]
    except KeyError as exc:
        raise ValueError(f"unknown domain or alias: {domain!r}") from exc


__all__ = [
    "DOMAIN_TAXONOMY",
    "DomainFieldSemantics",
    "DomainSpec",
    "taxonomy_for",
]
