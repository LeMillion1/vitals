"""Verified private-file resources for portability-v2 archive writers.

The public graph contains logical file metadata while its prepared resource
objects contain trusted, private storage locators.  This module joins those two
views, applies byte/count caps, and copies only integrity-checked bytes.  A
storage locator or database identity is never returned in serializable output.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO, Final, Protocol

from vitals.enums import FileStorageBackend
from vitals.persistence.file_storage import CHUNK_SIZE, open_verified_file
from vitals.services.portability import contract


_RESOURCE_REF_RE: Final = re.compile(r"f[0-9]{8}\Z")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
_PUBLIC_RESOURCE_KEYS: Final = frozenset(
    {"ref", "purpose", "media_type", "byte_size", "sha256_hex"}
)


class ResourceArchiveError(ValueError):
    """A resource plan, storage backend, cap, or integrity check failed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class PreparedResourceLike(Protocol):
    """Structural boundary implemented by graph.PreparedFileResource."""

    resource_ref: str
    storage_backend: str
    storage_ref: str
    expected_byte_size: int
    expected_sha256_hex: str


@dataclass(frozen=True, slots=True)
class ResourceLocations:
    """Server-side roots used only while opening prepared resources."""

    static_dir: str
    private_root: str

    def __post_init__(self) -> None:
        if not isinstance(self.static_dir, str) or not os.path.isabs(self.static_dir):
            raise ValueError("static_dir must be an absolute path")
        if not isinstance(self.private_root, str) or not os.path.isabs(self.private_root):
            raise ValueError("private_root must be an absolute path")


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Hard caps for logical and physical file resources."""

    max_resources: int = 100_000
    max_resource_bytes: int = 1024 * 1024 * 1024
    max_total_resource_bytes: int = contract.MAX_PLAINTEXT_BYTES

    def __post_init__(self) -> None:
        for name in (
            "max_resources",
            "max_resource_bytes",
            "max_total_resource_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


DEFAULT_RESOURCE_LIMITS: Final = ResourceLimits()


@dataclass(frozen=True, slots=True)
class ResourcePlanItem:
    """One validated private handle paired with its public logical metadata."""

    resource_ref: str
    sha256_hex: str
    byte_size: int
    storage_backend: str
    storage_ref: str

    @property
    def object_path(self) -> str:
        return f"objects/sha256/{self.sha256_hex}"


def _error(code: str, detail: str) -> ResourceArchiveError:
    return ResourceArchiveError(code, detail)


def _write_all(destination: BinaryIO, body: bytes) -> None:
    view = memoryview(body)
    while view:
        written = destination.write(view)
        if written is None:
            raise OSError("resource destination did not report a completed write")
        if type(written) is not int or written <= 0 or written > len(view):
            raise OSError("resource destination refused data")
        view = view[written:]


def build_resource_plan(
    public_resources: object,
    prepared_resources: Iterable[PreparedResourceLike],
    *,
    limits: ResourceLimits = DEFAULT_RESOURCE_LIMITS,
) -> tuple[ResourcePlanItem, ...]:
    """Join public resource metadata to private handles without exposing them."""

    if not isinstance(limits, ResourceLimits):
        raise TypeError("limits must be ResourceLimits")
    if not isinstance(public_resources, list):
        raise _error("resource_manifest_invalid", "resources must be a list")
    if len(public_resources) > limits.max_resources:
        raise _error("resource_count_exceeded", "resource count exceeds the hard limit")

    public_by_ref: dict[str, Mapping[str, Any]] = {}
    for raw in public_resources:
        if not isinstance(raw, Mapping) or frozenset(raw) != _PUBLIC_RESOURCE_KEYS:
            raise _error(
                "resource_manifest_invalid",
                "a public resource has invalid fields",
            )
        ref = raw["ref"]
        size = raw["byte_size"]
        digest = raw["sha256_hex"]
        if type(ref) is not str or _RESOURCE_REF_RE.fullmatch(ref) is None:
            raise _error("resource_manifest_invalid", "a resource ref is invalid")
        if ref in public_by_ref:
            raise _error("resource_ref_duplicate", "a resource ref is duplicated")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > limits.max_resource_bytes
        ):
            raise _error("resource_size_exceeded", "a resource size is invalid or capped")
        if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
            raise _error("resource_manifest_invalid", "a resource digest is invalid")
        if type(raw["purpose"]) is not str or not raw["purpose"]:
            raise _error("resource_manifest_invalid", "a resource purpose is invalid")
        if type(raw["media_type"]) is not str or not raw["media_type"]:
            raise _error("resource_manifest_invalid", "a resource media type is invalid")
        public_by_ref[ref] = raw

    prepared_by_ref: dict[str, PreparedResourceLike] = {}
    for prepared_index, prepared in enumerate(prepared_resources, start=1):
        if prepared_index > limits.max_resources:
            raise _error(
                "resource_count_exceeded", "prepared resource count exceeds the hard limit"
            )
        try:
            ref = prepared.resource_ref
        except AttributeError as exc:
            raise _error(
                "prepared_resource_invalid", "a prepared resource has invalid fields"
            ) from exc
        if type(ref) is not str or _RESOURCE_REF_RE.fullmatch(ref) is None:
            raise _error("prepared_resource_invalid", "a prepared resource ref is invalid")
        if ref in prepared_by_ref:
            raise _error("prepared_resource_duplicate", "a prepared resource is duplicated")
        prepared_by_ref[ref] = prepared

    if set(public_by_ref) != set(prepared_by_ref):
        raise _error(
            "resource_plan_incomplete",
            "public and prepared resource refs do not match",
        )

    total_bytes = 0
    size_by_digest: dict[str, int] = {}
    plan: list[ResourcePlanItem] = []
    for ref in sorted(public_by_ref):
        public = public_by_ref[ref]
        prepared = prepared_by_ref[ref]
        try:
            prepared_size = prepared.expected_byte_size
            prepared_digest = prepared.expected_sha256_hex
            backend = prepared.storage_backend
            storage_ref = prepared.storage_ref
        except AttributeError as exc:
            raise _error(
                "prepared_resource_invalid", "a prepared resource has invalid fields"
            ) from exc
        if (
            isinstance(prepared_size, bool)
            or not isinstance(prepared_size, int)
            or prepared_size < 0
            or type(prepared_digest) is not str
            or _SHA256_RE.fullmatch(prepared_digest) is None
        ):
            raise _error(
                "prepared_resource_invalid", "prepared resource metadata is invalid"
            )
        if prepared_size != public["byte_size"] or prepared_digest != public["sha256_hex"]:
            raise _error(
                "resource_metadata_mismatch",
                "public and prepared resource metadata differ",
            )
        previous_size = size_by_digest.setdefault(prepared_digest, prepared_size)
        if previous_size != prepared_size:
            raise _error(
                "resource_digest_metadata_conflict",
                "one content digest is paired with conflicting byte sizes",
            )
        if backend != FileStorageBackend.PRIVATE_LOCAL.value:
            # Object-store support needs a backend-specific verified descriptor;
            # never reinterpret an object key as a local path.
            raise _error(
                "resource_backend_unsupported",
                "the prepared resource backend cannot be exported locally",
            )
        if type(storage_ref) is not str or not storage_ref:
            raise _error("prepared_resource_invalid", "a storage locator is invalid")
        total_bytes += prepared_size
        if total_bytes > limits.max_total_resource_bytes:
            raise _error(
                "resource_total_exceeded", "logical resource bytes exceed the hard limit"
            )
        plan.append(
            ResourcePlanItem(
                resource_ref=ref,
                sha256_hex=prepared_digest,
                byte_size=prepared_size,
                storage_backend=backend,
                storage_ref=storage_ref,
            )
        )
    return tuple(plan)


def copy_verified_resource(
    item: ResourcePlanItem,
    destination: BinaryIO,
    *,
    locations: ResourceLocations,
    chunk_size: int = CHUNK_SIZE,
) -> int:
    """Open, verify, and copy one resource while hashing the same descriptor."""

    if not isinstance(item, ResourcePlanItem):
        raise TypeError("item must be a ResourcePlanItem")
    if not isinstance(locations, ResourceLocations):
        raise TypeError("locations must be ResourceLocations")
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    try:
        verified = open_verified_file(
            storage_backend=item.storage_backend,
            storage_ref=item.storage_ref,
            static_dir=locations.static_dir,
            private_root=locations.private_root,
            expected_size=item.byte_size,
            expected_sha256=item.sha256_hex,
        )
    except (OSError, ValueError):
        raise _error(
            "resource_integrity_failed", "a prepared resource failed verification"
        ) from None

    digest = hashlib.sha256()
    copied = 0
    try:
        while True:
            try:
                chunk = verified.stream.read(chunk_size)
            except (OSError, ValueError):
                raise _error(
                    "resource_integrity_failed",
                    "a prepared resource could not be read",
                ) from None
            if not chunk:
                break
            if not isinstance(chunk, bytes | bytearray | memoryview):
                raise _error(
                    "resource_integrity_failed", "a prepared resource is not binary"
                )
            body = bytes(chunk)
            copied += len(body)
            if copied > item.byte_size:
                raise _error(
                    "resource_integrity_failed", "a prepared resource changed while read"
                )
            digest.update(body)
            # Destination failures are deliberately not relabeled as resource
            # corruption.  The archive layer owns output caps and abort logic.
            _write_all(destination, body)
    finally:
        verified.stream.close()

    if copied != item.byte_size or digest.hexdigest() != item.sha256_hex:
        raise _error(
            "resource_integrity_failed", "a prepared resource changed while read"
        )
    return copied


def verify_duplicate_resources(
    plan: Iterable[ResourcePlanItem],
    *,
    locations: ResourceLocations,
) -> None:
    """Verify duplicate logical assets whose bytes will be stored only once."""

    first_by_digest: dict[str, ResourcePlanItem] = {}
    for item in plan:
        if item.sha256_hex not in first_by_digest:
            first_by_digest[item.sha256_hex] = item
            continue
        # The sink discards bytes, but the ordinary copy path still verifies the
        # selected descriptor and hashes the exact stream it consumes.
        copy_verified_resource(item, _DiscardSink(), locations=locations)


class _DiscardSink:
    def write(self, body: bytes | bytearray | memoryview) -> int:
        return len(body)


__all__ = [
    "DEFAULT_RESOURCE_LIMITS",
    "PreparedResourceLike",
    "ResourceArchiveError",
    "ResourceLimits",
    "ResourceLocations",
    "ResourcePlanItem",
    "build_resource_plan",
    "copy_verified_resource",
    "verify_duplicate_resources",
]
