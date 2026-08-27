"""Opaque contracts and pure validation for body-scan AI parsing."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date as date_type
from enum import StrEnum
from typing import Any

from vitals.config import load_config
from vitals.enums import AIInvocationStatus
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset

class BodyScanAIError(RuntimeError):
    """Base class for platform-funded body-scan parsing."""


class BodyScanAIValidationError(ValueError, BodyScanAIError):
    """An upload or provider result is outside the bounded contract."""


class BodyScanAIOwnershipError(BodyScanAIError):
    """A subject, actor, file, or raw provenance root is inconsistent."""


class BodyScanAIInvocationStateError(BodyScanAIError):
    """An invocation cannot safely enter the requested lifecycle phase."""


class BodyScanAIAvailabilityCode(StrEnum):
    AVAILABLE = "available"
    NOT_CONFIGURED = "not_configured"
    QUOTA = "quota"


@dataclass(frozen=True, slots=True)
class BodyScanAIAvailability:
    """Redacted readiness projection; limits and subject data stay private."""

    available: bool
    code: BodyScanAIAvailabilityCode


@dataclass(frozen=True, slots=True)
class BodyScanParseResult:
    """Sanitized terminal projection with an optional in-memory extraction."""

    raw_payload_id: int
    file_asset_id: uuid.UUID
    invocation_id: uuid.UUID
    status: AIInvocationStatus
    extracted: dict[str, Any] | None = field(default=None, repr=False)


_PREPARED_BODY_SCAN_DOCUMENT_SEAL = object()
_PREPARED_BODY_SCAN_CONTENT_SEAL = object()
_PLACEHOLDER = {"_ai_parse": {"version": 1, "state": "prepared"}}
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_BODY_SCAN_MAX_BYTES = 25 * 1024 * 1024
_BODY_SCAN_MAX_TOKENS = 8192
_BODY_SCAN_RESERVATION_OVERHEAD_UNITS = 4096
_BODY_SCAN_RESERVED_COST_MICROUNITS = 10_000_000
_BODY_SCAN_MAX_RESULTS = 1000
_BODY_SCAN_IDEMPOTENCY_VERSION = "body-scan-document:v1"
_ALLOWED_TOP_LEVEL_KEYS = frozenset({"date", "device", "metrics"})
_ALLOWED_RESULT_KEYS = frozenset(
    {"label", "value", "unit", "ref_low", "ref_high", "segment"}
)
_ALLOWED_SEGMENTS = frozenset(
    {"right_arm", "left_arm", "trunk", "right_leg", "left_leg"}
)


class PreparedBodyScanParse:
    """Opaque cross-transaction snapshot for one document parser attempt."""

    __slots__ = (
        "_actor_user_id",
        "_asset_fingerprint",
        "_byte_size",
        "_dispatchable",
        "_existing_extracted",
        "_file_asset_id",
        "_fingerprint",
        "_invocation_id",
        "_media_type",
        "_model",
        "_owner_user_id",
        "_raw_fingerprint",
        "_raw_payload_id",
        "_reservation_status",
        "_seal",
        "_sha256_hex",
        "_storage_ref",
        "_subject_id",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise BodyScanAIOwnershipError(
            "prepared body-scan parses are service-issued only"
        )

    @classmethod
    def _issue(cls, **values) -> "PreparedBodyScanParse":
        prepared = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(prepared, name, value)
        object.__setattr__(
            prepared,
            "_fingerprint",
            (
                values["_subject_id"],
                values["_owner_user_id"],
                values["_actor_user_id"],
                values["_file_asset_id"],
                values["_raw_payload_id"],
                values["_storage_ref"],
                values["_media_type"],
                values["_byte_size"],
                values["_sha256_hex"],
                values["_model"],
                values["_invocation_id"],
                values["_reservation_status"],
                values["_dispatchable"],
                values["_asset_fingerprint"],
                values["_raw_fingerprint"],
                _payload_digest(values["_existing_extracted"]),
            ),
        )
        object.__setattr__(prepared, "_seal", _PREPARED_BODY_SCAN_DOCUMENT_SEAL)
        return prepared

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedBodyScanParse is immutable")

    def __repr__(self) -> str:
        return (
            f"<PreparedBodyScanParse invocation_id={self._invocation_id} "
            f"status={self._reservation_status.value} redacted>"
        )

    def __reduce__(self):
        raise TypeError("PreparedBodyScanParse is not pickleable")

    @property
    def raw_payload_id(self) -> int:
        return self._raw_payload_id

    @property
    def file_asset_id(self) -> uuid.UUID:
        return self._file_asset_id

    @property
    def invocation_id(self) -> uuid.UUID:
        return self._invocation_id

    @property
    def reservation_status(self) -> AIInvocationStatus:
        return self._reservation_status

    @property
    def dispatchable(self) -> bool:
        return self._dispatchable

    @property
    def existing_extracted(self) -> dict[str, Any] | None:
        return deepcopy(self._existing_extracted)


class PreparedBodyScanContent:
    """Opaque memory-only proof that local preprocessing succeeded."""

    __slots__ = (
        "_fingerprint",
        "_image_urls",
        "_is_pdf",
        "_prepared_fingerprint",
        "_seal",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise BodyScanAIValidationError(
            "prepared body-scan content is service-issued only"
        )

    @classmethod
    def _issue(
        cls,
        *,
        prepared_fingerprint: tuple,
        image_urls: tuple[str, ...],
        is_pdf: bool,
    ) -> "PreparedBodyScanContent":
        content = object.__new__(cls)
        object.__setattr__(content, "_prepared_fingerprint", prepared_fingerprint)
        object.__setattr__(content, "_image_urls", image_urls)
        object.__setattr__(content, "_is_pdf", is_pdf)
        object.__setattr__(
            content,
            "_fingerprint",
            (prepared_fingerprint, _image_urls_digest(image_urls), is_pdf),
        )
        object.__setattr__(content, "_seal", _PREPARED_BODY_SCAN_CONTENT_SEAL)
        return content

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedBodyScanContent is immutable")

    def __repr__(self) -> str:
        return "<PreparedBodyScanContent redacted>"

    def __reduce__(self):
        raise TypeError("PreparedBodyScanContent is not pickleable")


@dataclass(frozen=True, slots=True)
class _LockedBodyScanScope:
    subject: HealthSubject = field(repr=False)
    owner: User = field(repr=False)
    asset: FileAsset = field(repr=False)
    raw: RawPayload = field(repr=False)


def _payload_digest(payload: object) -> bytes:
    if payload is None:
        return b""
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise BodyScanAIValidationError("body-scan raw payload is not canonical JSON") from exc
    return hashlib.sha256(encoded).digest()


def _image_urls_digest(image_urls: tuple[str, ...]) -> bytes:
    digest = hashlib.sha256()
    for image_url in image_urls:
        digest.update(len(image_url).to_bytes(8, "big"))
        digest.update(image_url.encode("ascii"))
    return digest.digest()


def _clean_media_type(value: str) -> str:
    if not isinstance(value, str):
        raise BodyScanAIValidationError("media_type must be a string")
    cleaned = value.strip().lower()
    if (
        not cleaned
        or len(cleaned) > 255
        or not (cleaned == "application/pdf" or cleaned.startswith("image/"))
        or any(ord(char) < 32 for char in cleaned)
    ):
        raise BodyScanAIValidationError("unsupported body-scan document media type")
    return cleaned


def _clean_size(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _BODY_SCAN_MAX_BYTES
    ):
        raise BodyScanAIValidationError("body-scan document size is invalid")
    return value


def _clean_sha256(value: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise BodyScanAIValidationError("body-scan document sha256 is invalid")
    return value


def _clean_storage_ref(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not value.startswith("body/")
        or value.startswith("/")
        or ".." in value
        or any(ord(char) < 32 for char in value)
    ):
        raise BodyScanAIValidationError("body-scan document storage reference is invalid")
    return value


def _clean_model() -> str:
    model = load_config().llm_model_parser.strip()
    if not model or len(model) > 128 or any(ord(char) < 32 for char in model):
        raise BodyScanAIValidationError("body-scan parser model is invalid")
    return model


def _asset_fingerprint(asset: FileAsset) -> tuple:
    return (
        asset.id,
        asset.subject_id,
        asset.uploaded_by_user_id,
        asset.purpose,
        asset.storage_backend,
        asset.storage_ref,
        asset.media_type,
        asset.byte_size,
        asset.sha256_hex,
        asset.status,
        asset.deleted_at,
        asset.purged_at,
    )


def _raw_fingerprint(raw: RawPayload) -> tuple:
    return (
        raw.id,
        raw.subject_id,
        raw.actor_user_id,
        raw.integration_connection_id,
        raw.file_asset_id,
        raw.domain,
        raw.source,
        raw.external_id,
        raw.processed_at,
        _payload_digest(raw.payload),
    )


def _require_prepared(
    prepared: PreparedBodyScanParse,
) -> PreparedBodyScanParse:
    if not isinstance(prepared, PreparedBodyScanParse):
        raise BodyScanAIOwnershipError("body-scan parse capability is invalid")
    try:
        expected = (
            prepared._subject_id,
            prepared._owner_user_id,
            prepared._actor_user_id,
            prepared._file_asset_id,
            prepared._raw_payload_id,
            prepared._storage_ref,
            prepared._media_type,
            prepared._byte_size,
            prepared._sha256_hex,
            prepared._model,
            prepared._invocation_id,
            prepared._reservation_status,
            prepared._dispatchable,
            prepared._asset_fingerprint,
            prepared._raw_fingerprint,
            _payload_digest(prepared._existing_extracted),
        )
        valid = (
            prepared._seal is _PREPARED_BODY_SCAN_DOCUMENT_SEAL
            and prepared._fingerprint == expected
        )
    except (AttributeError, TypeError, ValueError, UnicodeError) as exc:
        raise BodyScanAIOwnershipError("body-scan parse capability is invalid") from exc
    if not valid:
        raise BodyScanAIOwnershipError("body-scan parse capability was modified")
    return prepared


def _require_content(
    prepared: PreparedBodyScanParse,
    content: PreparedBodyScanContent | None,
) -> PreparedBodyScanContent:
    is_pdf = prepared._media_type == "application/pdf" or prepared._storage_ref.endswith(
        ".pdf"
    )
    if content is None:
        raise BodyScanAIValidationError(
            "body-scan preprocessing is required before dispatch"
        )
    if not isinstance(content, PreparedBodyScanContent):
        raise BodyScanAIValidationError("prepared body-scan content is invalid")
    try:
        expected = (
            content._prepared_fingerprint,
            _image_urls_digest(content._image_urls),
            content._is_pdf,
        )
        valid = (
            content._seal is _PREPARED_BODY_SCAN_CONTENT_SEAL
            and content._prepared_fingerprint == prepared._fingerprint
            and content._fingerprint == expected
            and content._is_pdf is is_pdf
            and bool(content._image_urls)
        )
    except (AttributeError, TypeError, ValueError, UnicodeError) as exc:
        raise BodyScanAIValidationError(
            "prepared body-scan content is invalid"
        ) from exc
    if not valid:
        raise BodyScanAIValidationError("prepared body-scan content was modified")
    return content

def _idempotency_key(raw_payload_id: int) -> str:
    return f"{_BODY_SCAN_IDEMPOTENCY_VERSION}:{raw_payload_id}"


def _validated_extraction(payload: object) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or "_unparsed" in payload
        or set(payload) - _ALLOWED_TOP_LEVEL_KEYS
    ):
        raise BodyScanAIValidationError("body-scan extraction shape is invalid")
    results = payload.get("metrics")
    if (
        not isinstance(results, list)
        or not results
        or len(results) > _BODY_SCAN_MAX_RESULTS
    ):
        raise BodyScanAIValidationError("body-scan extraction result count is invalid")
    on_date = payload.get("date")
    if on_date is not None and (
        not isinstance(on_date, str)
        or not on_date.strip()
        or len(on_date) > 32
        or any(ord(char) < 32 for char in on_date)
    ):
        raise BodyScanAIValidationError("body-scan extraction date is invalid")
    if on_date is not None:
        try:
            parsed_date = date_type.fromisoformat(on_date)
        except ValueError as exc:
            raise BodyScanAIValidationError(
                "body-scan extraction date is invalid"
            ) from exc
        if parsed_date.isoformat() != on_date:
            raise BodyScanAIValidationError("body-scan extraction date is invalid")
    device = payload.get("device")
    if device is not None and (
        not isinstance(device, str)
        or not device.strip()
        or len(device) > 255
        or any(ord(char) < 32 for char in device)
    ):
        raise BodyScanAIValidationError("body-scan extraction device is invalid")
    for item in results:
        if not isinstance(item, dict) or set(item) - _ALLOWED_RESULT_KEYS:
            raise BodyScanAIValidationError("body-scan extraction metric shape is invalid")
        label = item.get("label")
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label) > 255
            or any(ord(char) < 32 for char in label)
        ):
            raise BodyScanAIValidationError("body-scan extraction label is invalid")
        for key in ("value", "ref_low", "ref_high"):
            value = item.get(key)
            if key != "value" and value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or abs(float(value)) > 1_000_000
            ):
                raise BodyScanAIValidationError(
                    "body-scan extraction numeric value is invalid"
                )
        unit = item.get("unit")
        if unit is not None and (
            not isinstance(unit, str)
            or not unit.strip()
            or len(unit) > 64
            or any(ord(char) < 32 for char in unit)
        ):
            raise BodyScanAIValidationError("body-scan extraction unit is invalid")
        segment = item.get("segment")
        if segment is not None and segment not in _ALLOWED_SEGMENTS:
            raise BodyScanAIValidationError("body-scan extraction segment is invalid")
    return payload
