"""Validated registry of subject-scoped domain resolvers."""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from vitals.services.conflicts.engine.contracts import (
    DomainResolver,
    LegacyUnownedProbe,
)


@dataclass(frozen=True, slots=True)
class _ResolverRegistration:
    scoped: DomainResolver
    legacy_probe: LegacyUnownedProbe | None = None


_resolvers: dict[str, _ResolverRegistration] = {}


class ConflictResolverUnavailable(RuntimeError):
    """An active scoped rule references a domain without a scoped resolver."""


def register_domain_resolver(
    domain: str,
    resolver: DomainResolver,
    *,
    legacy_probe: LegacyUnownedProbe | None = None,
) -> None:
    """Register one domain's subject-scoped reader.

    Every resolver is scoped; there is no second, unscoped arm any more. A
    resolver proves it is scoped by taking a keyword-only ``scope`` with no
    default, so a function that would happily answer without one cannot be
    registered at all.

    ``legacy_probe`` is required of exactly those resolvers that widen on
    ``scope.include_legacy_unowned``; see :class:`LegacyUnownedProbe`. A resolver
    that never widens has nothing to probe for and passes ``None``.
    """

    scope_parameter = inspect.signature(resolver).parameters.get("scope")
    if scope_parameter is None:
        raise TypeError("a conflict resolver must accept the scope it answers for")
    if (
        scope_parameter.kind is not inspect.Parameter.KEYWORD_ONLY
        or scope_parameter.default is not inspect.Parameter.empty
    ):
        raise TypeError("a scoped conflict resolver requires keyword-only scope")
    _resolvers[domain] = _ResolverRegistration(scoped=resolver, legacy_probe=legacy_probe)


def clear_domain_resolvers() -> None:
    """Drop all registered resolvers (test isolation)."""
    _resolvers.clear()
