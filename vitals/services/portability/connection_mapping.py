"""Explicit, fail-closed integration mapping for portability-v2 imports.

Portable archives carry only logical connection descriptors.  They never carry
credentials, provider account discriminators, or installation-local UUIDs.  An
import boundary must therefore ask an authenticated operator for an explicit
``c-ref -> live connection UUID`` mapping and validate that choice against the
target subject before any imported row can use it.

This module only reads the connection roots.  It does not create, update, flush,
commit, or lock them, and its result contains none of their private resolver
metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import IntegrationConnectionStatus
from vitals.models.tenancy import IntegrationConnection


FORMAT_NAME: Final = "vitals-portability-connection-map"
FORMAT_VERSION: Final = 1

_CONNECTION_REF_RE: Final = re.compile(r"c[0-9]{8}\Z")
_USABLE_STATUSES: Final = frozenset(
    {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }
)


class ConnectionMappingError(ValueError):
    """An explicit import mapping failed validation without exposing secrets."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class ConnectionDescriptorLike(Protocol):
    """Persistence-free shape an archive validator may expose immutably."""

    @property
    def ref(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def connection_type(self) -> str: ...


@dataclass(frozen=True, slots=True)
class ArchiveConnectionDescriptor:
    """Immutable validated archive value accepted by the mapping boundary."""

    ref: str
    provider: str
    connection_type: str


@dataclass(frozen=True, slots=True)
class ConnectionBinding:
    """One canonical public descriptor bound to one installation-local root."""

    ref: str
    connection_id: uuid.UUID
    provider: str
    connection_type: str


@dataclass(frozen=True, slots=True)
class CanonicalConnectionMapping(Mapping[str, uuid.UUID]):
    """Immutable, ref-sorted import mapping and its canonical SHA-256 digest."""

    target_subject_id: uuid.UUID
    bindings: tuple[ConnectionBinding, ...]
    sha256_hex: str

    def __getitem__(self, ref: str) -> uuid.UUID:
        for binding in self.bindings:
            if binding.ref == ref:
                return binding.connection_id
        raise KeyError(ref)

    def __iter__(self) -> Iterator[str]:
        return (binding.ref for binding in self.bindings)

    def __len__(self) -> int:
        return len(self.bindings)


@dataclass(frozen=True, slots=True)
class _Descriptor:
    ref: str
    provider: str
    connection_type: str


def _error(code: str, detail: str) -> ConnectionMappingError:
    return ConnectionMappingError(code, detail)


def _require_uuid(value: object, *, field: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise _error("connection_mapping_invalid", f"{field} must be a non-zero UUID")
    return value


def _descriptors(
    raw_descriptors: Sequence[ConnectionDescriptorLike],
) -> tuple[_Descriptor, ...]:
    if isinstance(raw_descriptors, (str, bytes, bytearray)) or not isinstance(
        raw_descriptors, Sequence
    ):
        raise _error("connection_descriptors_invalid", "connection descriptors must be a sequence")

    by_ref: dict[str, _Descriptor] = {}
    for raw in raw_descriptors:
        try:
            ref = raw.ref
            provider = raw.provider
            connection_type = raw.connection_type
        except AttributeError as exc:
            raise _error(
                "connection_descriptor_invalid",
                "a connection descriptor has invalid fields",
            ) from exc
        if type(ref) is not str or _CONNECTION_REF_RE.fullmatch(ref) is None:
            raise _error("connection_descriptor_invalid", "a connection ref is invalid")
        if (
            type(provider) is not str
            or not provider
            or provider != provider.strip()
            or len(provider) > 32
            or type(connection_type) is not str
            or not connection_type
            or connection_type != connection_type.strip()
            or len(connection_type) > 32
        ):
            raise _error(
                "connection_descriptor_invalid",
                "a logical connection descriptor is invalid",
            )
        if ref in by_ref:
            raise _error("connection_descriptor_duplicate", "a connection ref is duplicated")
        by_ref[ref] = _Descriptor(
            ref=ref,
            provider=provider,
            connection_type=connection_type,
        )
    return tuple(by_ref[ref] for ref in sorted(by_ref))


def _explicit_mapping(
    raw_mapping: Mapping[str, uuid.UUID],
    *,
    expected_refs: frozenset[str],
) -> dict[str, uuid.UUID]:
    if not isinstance(raw_mapping, Mapping):
        raise _error("connection_mapping_invalid", "connection mapping must be an object")

    copied: dict[str, uuid.UUID] = {}
    for ref, connection_id in raw_mapping.items():
        if type(ref) is not str or _CONNECTION_REF_RE.fullmatch(ref) is None:
            raise _error("connection_mapping_invalid", "a mapped connection ref is invalid")
        copied[ref] = _require_uuid(connection_id, field="connection id")
    if frozenset(copied) != expected_refs:
        raise _error(
            "connection_mapping_incomplete",
            "the explicit mapping must contain every archive connection ref exactly once",
        )
    if len(set(copied.values())) != len(copied):
        raise _error(
            "connection_mapping_not_one_to_one",
            "one live connection cannot satisfy more than one archive ref",
        )
    return copied


def _canonical_body(
    *,
    target_subject_id: uuid.UUID,
    bindings: tuple[ConnectionBinding, ...],
) -> bytes:
    value = {
        "connections": [
            {
                "connection_id": str(binding.connection_id),
                "connection_type": binding.connection_type,
                "provider": binding.provider,
                "ref": binding.ref,
            }
            for binding in bindings
        ],
        "format": FORMAT_NAME,
        "target_subject_id": str(target_subject_id),
        "version": FORMAT_VERSION,
    }
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


async def resolve_connection_mapping(
    session: AsyncSession,
    *,
    target_subject_id: uuid.UUID,
    archive_connections: Sequence[ConnectionDescriptorLike],
    connection_ids_by_ref: Mapping[str, uuid.UUID],
) -> CanonicalConnectionMapping:
    """Validate an explicit archive-ref mapping without mutating persistence.

    ``archive_connections`` is the already-validated public descriptor list from
    a portability-v2 record.  The small shape checks here keep this boundary safe
    if a future caller accidentally bypasses the archive reader.
    """

    subject_id = _require_uuid(target_subject_id, field="target subject id")
    descriptors = _descriptors(archive_connections)
    explicit = _explicit_mapping(
        connection_ids_by_ref,
        expected_refs=frozenset(descriptor.ref for descriptor in descriptors),
    )

    connection_ids = tuple(
        sorted(
            (explicit[descriptor.ref] for descriptor in descriptors),
            key=lambda item: item.hex,
        )
    )
    rows_by_id: dict[uuid.UUID, object] = {}
    if connection_ids:
        statement = select(
            IntegrationConnection.id,
            IntegrationConnection.subject_id,
            IntegrationConnection.provider,
            IntegrationConnection.connection_type,
            IntegrationConnection.status,
        ).where(IntegrationConnection.id.in_(connection_ids))
        with session.no_autoflush:
            rows = (await session.execute(statement)).all()
        rows_by_id = {row.id: row for row in rows}

    bindings: list[ConnectionBinding] = []
    for descriptor in descriptors:
        connection_id = explicit[descriptor.ref]
        row = rows_by_id.get(connection_id)
        if row is None:
            raise _error(
                "mapped_connection_missing", "a mapped integration connection does not exist"
            )
        if row.subject_id != subject_id:
            raise _error(
                "mapped_connection_cross_subject",
                "a mapped integration connection belongs to another subject",
            )
        if row.status not in _USABLE_STATUSES:
            raise _error(
                "mapped_connection_not_usable",
                "a mapped integration connection is not active or usable",
            )
        if row.provider != descriptor.provider or row.connection_type != descriptor.connection_type:
            raise _error(
                "mapped_connection_descriptor_mismatch",
                "a mapped integration connection has a different logical descriptor",
            )
        bindings.append(
            ConnectionBinding(
                ref=descriptor.ref,
                connection_id=connection_id,
                provider=descriptor.provider,
                connection_type=descriptor.connection_type,
            )
        )

    canonical_bindings = tuple(bindings)
    digest = hashlib.sha256(
        _canonical_body(
            target_subject_id=subject_id,
            bindings=canonical_bindings,
        )
    ).hexdigest()
    return CanonicalConnectionMapping(
        target_subject_id=subject_id,
        bindings=canonical_bindings,
        sha256_hex=digest,
    )


__all__ = [
    "ArchiveConnectionDescriptor",
    "CanonicalConnectionMapping",
    "ConnectionBinding",
    "ConnectionDescriptorLike",
    "ConnectionMappingError",
    "FORMAT_NAME",
    "FORMAT_VERSION",
    "resolve_connection_mapping",
]
