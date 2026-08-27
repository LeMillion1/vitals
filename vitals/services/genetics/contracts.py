"""Stable types and constants for subject-scoped Genetics services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


from vitals.models.genetics import GeneticVariant
from vitals.models.raw_payload import RawPayload
from vitals.services.conflicts import engine

MAX_RAW_VARIANTS = 50_000
MAX_LIST_LIMIT = 100
VCF_RAW_FORMAT_VERSION = 2
PATCH_UNSET: Final = object()


class GeneticsServiceError(ValueError):
    """Base class for typed, fail-closed genetics service failures."""


class GeneticsValidationError(GeneticsServiceError):
    """A caller supplied an invalid genetics value or capability combination."""


class GeneticsOwnershipError(GeneticsServiceError):
    """A genetics fact is outside the requested ownership scope."""


class GeneticsRawProvenanceError(
    GeneticsOwnershipError,
    engine.ConflictRawOwnershipError,
):
    """A VCF raw/fact provenance graph is missing or inconsistent."""


class GeneticsRsidOccupiedError(GeneticsOwnershipError):
    """This subject already holds a variant for the requested rsID."""


class GeneticsNotFoundError(GeneticsServiceError):
    """A requested scoped genetic variant does not exist."""


@dataclass(frozen=True, slots=True)
class VcfIngestSummary:
    raw: RawPayload | None
    imported: int
    markers: int


@dataclass(frozen=True, slots=True)
class BoundedVariantPage:
    """A provenance-validated, bounded genetics projection."""

    rows: tuple[GeneticVariant, ...]
    truncated: bool
