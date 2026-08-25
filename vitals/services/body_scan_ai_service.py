"""Platform-funded, raw-first AI parsing for body-composition documents.

The upload boundary owns four deliberately separate phases:

* ``prepare_body_scan_parse`` records the private file/raw roots and reserves
  quota in one short transaction;
* ``start_body_scan_dispatch`` freshly revalidates those roots and charges
  one provider attempt in another short transaction;
* ``render_body_scan`` performs exactly one provider await with no database
  transaction;
* ``persist_body_scan_parse`` atomically finalizes sanitized accounting and
  replaces the raw placeholder with the validated verbatim extraction.

The installation-wide OpenRouter root pays for the call but never grants access
to a subject.  Prompt, document bytes, provider credential, and extraction stay
in memory; only the domain raw payload receives the successful extraction.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date as date_type
from enum import StrEnum
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import (
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    FileAssetPurpose,
    FileAssetStatus,
    FileStorageBackend,
    IntegrationConnectionStatus,
    Source,
    UserStatus,
)
from vitals.integrations.llm_client import LLMCallResult, LLMClient
from vitals.models.ai import (
    AIInvocation,
    AIPlatformQuotaPeriod,
    AISubjectQuotaPeriod,
)
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import FileAsset, PlatformIntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import ai_gateway_service, body_scan_service, file_asset_service
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from vitals.utils.timeutils import now_utc


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


async def _lock_owner(
    session: AsyncSession,
    *,
    actor_username: str,
) -> tuple[HealthSubject, User, WriteIdentity]:
    await acquire_identity_governance_lock(session)
    ownership = await resolve_legacy_ownership_context(
        session,
        actor_username=actor_username,
    )
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == ownership.subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    owner = await session.scalar(
        select(User)
        .where(User.id == ownership.owner_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        subject is None
        or owner is None
        or subject.owner_user_id != owner.id
        or owner.status != UserStatus.ACTIVE.value
        or ownership.actor_user_id != owner.id
    ):
        raise BodyScanAIOwnershipError("body-scan upload owner authorization failed")
    return subject, owner, WriteIdentity(subject.id, owner.id)


async def _lock_prepared_scope(
    session: AsyncSession,
    prepared: PreparedBodyScanParse,
    *,
    require_active_owner: bool,
) -> _LockedBodyScanScope:
    await acquire_identity_governance_lock(session)
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == prepared._subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    owner = await session.scalar(
        select(User)
        .where(User.id == prepared._owner_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        subject is None
        or owner is None
        or prepared._actor_user_id != prepared._owner_user_id
    ):
        raise BodyScanAIOwnershipError("prepared body-scan owner provenance is missing")
    if require_active_owner and (
        subject.owner_user_id != prepared._owner_user_id
        or owner.status != UserStatus.ACTIVE.value
    ):
        raise BodyScanAIOwnershipError("prepared body-scan owner is no longer active")
    raw = await session.scalar(
        select(RawPayload)
        .where(RawPayload.id == prepared._raw_payload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    asset = await session.scalar(
        select(FileAsset)
        .where(FileAsset.id == prepared._file_asset_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if asset is None or raw is None:
        raise BodyScanAIOwnershipError("prepared body-scan file/raw roots are missing")
    if (
        _asset_fingerprint(asset) != prepared._asset_fingerprint
        or _raw_fingerprint(raw) != prepared._raw_fingerprint
    ):
        raise BodyScanAIOwnershipError("prepared body-scan file/raw roots changed")
    return _LockedBodyScanScope(subject, owner, asset, raw)


def _validate_existing_roots(
    *,
    asset: FileAsset,
    raw: RawPayload,
    identity: WriteIdentity,
    storage_ref: str,
    media_type: str,
    byte_size: int,
    sha256_hex: str,
    storage_backend: FileStorageBackend,
) -> None:
    expected_status = (
        FileAssetStatus.ACTIVE.value
        if storage_backend is FileStorageBackend.PRIVATE_LOCAL
        else FileAssetStatus.LEGACY_PLACEHOLDER.value
    )
    if (
        asset.subject_id != identity.subject_id
        or asset.uploaded_by_user_id != identity.actor_user_id
        or asset.purpose != FileAssetPurpose.BODY_SCAN_DOCUMENT.value
        or asset.storage_backend != storage_backend.value
        or asset.storage_ref != storage_ref
        or asset.media_type != media_type
        or asset.byte_size != byte_size
        or asset.sha256_hex != sha256_hex
        or asset.status != expected_status
        or asset.deleted_at is not None
        or asset.purged_at is not None
    ):
        raise BodyScanAIOwnershipError("body-scan file provenance is inconsistent")
    if (
        raw.subject_id != identity.subject_id
        or raw.actor_user_id != identity.actor_user_id
        or raw.integration_connection_id is not None
        or raw.file_asset_id != asset.id
        or raw.domain != Domain.BODY_COMPOSITION.value
        or raw.source != Source.BODY_SCAN.value
        or raw.external_id != storage_ref
        or raw.processed_at is not None
    ):
        raise BodyScanAIOwnershipError("body-scan raw provenance is inconsistent")


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


def _resolve_openrouter_credential(credential_ref: str) -> str | None:
    if credential_ref not in ai_gateway_service.ALLOWED_CREDENTIAL_REFS:
        return None
    value = load_config().openrouter_api_key.strip()
    return value or None


async def project_body_scan_ai_availability(
    session: AsyncSession,
    *,
    actor_username: str,
) -> BodyScanAIAvailability:
    """Project exact-owner gateway readiness without exposing limits or PHI."""

    subject, _owner, _identity = await _lock_owner(
        session,
        actor_username=actor_username,
    )
    billing_date = now_utc().date()
    roots = list(
        await session.scalars(
            select(PlatformIntegrationConnection)
            .where(
                PlatformIntegrationConnection.status
                == IntegrationConnectionStatus.ACTIVE.value
            )
            .limit(2)
        )
    )
    if len(roots) != 1 or _resolve_openrouter_credential(roots[0].credential_ref) is None:
        return BodyScanAIAvailability(False, BodyScanAIAvailabilityCode.NOT_CONFIGURED)
    platform_periods = list(
        await session.scalars(
            select(AIPlatformQuotaPeriod).where(
                AIPlatformQuotaPeriod.period_start <= billing_date,
                AIPlatformQuotaPeriod.period_end > billing_date,
            )
        )
    )
    subject_periods = list(
        await session.scalars(
            select(AISubjectQuotaPeriod).where(
                AISubjectQuotaPeriod.subject_id == subject.id,
                AISubjectQuotaPeriod.period_start <= billing_date,
                AISubjectQuotaPeriod.period_end > billing_date,
            )
        )
    )
    if (
        len(platform_periods) != 1
        or len(subject_periods) != 1
        or subject_periods[0].period_start != platform_periods[0].period_start
        or subject_periods[0].period_end != platform_periods[0].period_end
    ):
        return BodyScanAIAvailability(False, BodyScanAIAvailabilityCode.NOT_CONFIGURED)
    if any(
        row.reserved_cost_microunits + row.charged_cost_microunits
        >= row.cost_limit_microunits
        or row.reserved_units + row.charged_units >= row.unit_limit
        for row in (platform_periods[0], subject_periods[0])
    ):
        return BodyScanAIAvailability(False, BodyScanAIAvailabilityCode.QUOTA)
    return BodyScanAIAvailability(True, BodyScanAIAvailabilityCode.AVAILABLE)


async def prepare_body_scan_parse(
    session: AsyncSession,
    *,
    actor_username: str,
    storage_ref: str,
    media_type: str,
    byte_size: int,
    sha256_hex: str,
    storage_backend: FileStorageBackend | str = FileStorageBackend.LEGACY_LOCAL,
) -> PreparedBodyScanParse:
    """Create exact file/raw roots and reserve one paid parser invocation.

    The default preserves retries of legacy in-flight uploads. New HTTP uploads
    explicitly select the private backend.
    """

    cleaned_ref = _clean_storage_ref(storage_ref)
    cleaned_media = _clean_media_type(media_type)
    cleaned_size = _clean_size(byte_size)
    cleaned_sha = _clean_sha256(sha256_hex)
    model = _clean_model()
    _subject, _owner, identity = await _lock_owner(
        session,
        actor_username=actor_username,
    )
    try:
        normalized_backend = FileStorageBackend(storage_backend)
    except (TypeError, ValueError) as exc:
        raise BodyScanAIValidationError(
            "body-scan document storage backend is invalid"
        ) from exc
    if normalized_backend not in {
        FileStorageBackend.LEGACY_LOCAL,
        FileStorageBackend.PRIVATE_LOCAL,
    }:
        raise BodyScanAIValidationError(
            "body-scan document storage backend is invalid"
        )
    register = (
        file_asset_service.register_private_local
        if normalized_backend is FileStorageBackend.PRIVATE_LOCAL
        else file_asset_service.register_legacy_local
    )
    asset = await register(
        session,
        subject_id=identity.subject_id,
        uploaded_by_user_id=identity.actor_user_id,
        purpose=FileAssetPurpose.BODY_SCAN_DOCUMENT,
        storage_ref=cleaned_ref,
        media_type=cleaned_media,
        size_bytes=cleaned_size,
        content_sha256=cleaned_sha,
    )
    raw_rows = list(
        await session.scalars(
            select(RawPayload)
            .where(
                or_(
                    RawPayload.file_asset_id == asset.id,
                    (
                        (RawPayload.domain == Domain.BODY_COMPOSITION.value)
                        & (RawPayload.source == Source.BODY_SCAN.value)
                        & (RawPayload.external_id == cleaned_ref)
                    ),
                )
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(raw_rows) > 1:
        raise BodyScanAIOwnershipError("body-scan upload raw provenance is ambiguous")
    if raw_rows:
        raw = raw_rows[0]
    else:
        raw = RawPayload(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            integration_connection_id=None,
            file_asset_id=asset.id,
            domain=Domain.BODY_COMPOSITION.value,
            source=Source.BODY_SCAN.value,
            external_id=cleaned_ref,
            payload=_PLACEHOLDER,
            processed_at=None,
        )
        session.add(raw)
        await session.flush()
    _validate_existing_roots(
        asset=asset,
        raw=raw,
        identity=identity,
        storage_ref=cleaned_ref,
        media_type=cleaned_media,
        byte_size=cleaned_size,
        sha256_hex=cleaned_sha,
        storage_backend=normalized_backend,
    )
    invocations = list(
        await session.scalars(
            select(AIInvocation)
            .where(
                AIInvocation.subject_id == identity.subject_id,
                AIInvocation.raw_payload_id == raw.id,
                AIInvocation.purpose == AIInvocationPurpose.BODY_SCAN_PARSE.value,
            )
            .order_by(AIInvocation.created_at, AIInvocation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(invocations) > 1:
        raise BodyScanAIInvocationStateError(
            "body-scan document has multiple parser invocations"
        )
    existing = invocations[0] if invocations else None
    if existing is not None and (
        existing.actor_user_id != identity.actor_user_id
        or existing.source != AIInvocationSource.WEB.value
        or existing.idempotency_key != _idempotency_key(raw.id)
    ):
        raise BodyScanAIInvocationStateError(
            "body-scan parser invocation provenance is inconsistent"
        )
    reserved_units = (
        cleaned_size * 4 + _BODY_SCAN_MAX_TOKENS + _BODY_SCAN_RESERVATION_OVERHEAD_UNITS
    )
    if existing is None or existing.status == AIInvocationStatus.PREPARED.value:
        reservation = await ai_gateway_service.reserve_ai_invocation(
            session,
            identity=identity,
            purpose=AIInvocationPurpose.BODY_SCAN_PARSE,
            source=AIInvocationSource.WEB,
            model=model,
            idempotency_key=_idempotency_key(raw.id),
            reserved_cost_microunits=_BODY_SCAN_RESERVED_COST_MICROUNITS,
            reserved_units=reserved_units,
            raw_payload_id=raw.id,
        )
        invocation_id = reservation.invocation_id
        reservation_status = reservation.status
        dispatchable = reservation.dispatchable
    else:
        invocation_id = existing.id
        reservation_status = AIInvocationStatus(existing.status)
        dispatchable = False
    existing_extracted: dict[str, Any] | None = None
    if reservation_status is AIInvocationStatus.SUCCEEDED:
        existing_extracted = _validated_extraction(raw.payload)
    elif raw.payload != _PLACEHOLDER:
        raise BodyScanAIInvocationStateError(
            "unfinished body-scan invocation has a non-placeholder raw payload"
        )
    return PreparedBodyScanParse._issue(
        _subject_id=identity.subject_id,
        _owner_user_id=identity.actor_user_id,
        _actor_user_id=identity.actor_user_id,
        _file_asset_id=asset.id,
        _raw_payload_id=raw.id,
        _storage_ref=cleaned_ref,
        _media_type=cleaned_media,
        _byte_size=cleaned_size,
        _sha256_hex=cleaned_sha,
        _model=(existing.model if existing is not None else model),
        _invocation_id=invocation_id,
        _reservation_status=reservation_status,
        _dispatchable=dispatchable,
        _asset_fingerprint=_asset_fingerprint(asset),
        _raw_fingerprint=_raw_fingerprint(raw),
        _existing_extracted=existing_extracted,
    )


def prepare_body_scan_content(
    prepared: PreparedBodyScanParse,
    *,
    file_bytes: bytes,
) -> PreparedBodyScanContent:
    """Validate and locally convert bytes before any paid dispatch starts."""

    snapshot = _require_prepared(prepared)
    if not isinstance(file_bytes, bytes) or len(file_bytes) != snapshot._byte_size:
        raise BodyScanAIValidationError("body-scan document bytes changed")
    if hashlib.sha256(file_bytes).hexdigest() != snapshot._sha256_hex:
        raise BodyScanAIValidationError("body-scan document hash changed")
    is_pdf = snapshot._media_type == "application/pdf" or snapshot._storage_ref.endswith(
        ".pdf"
    )
    try:
        image_urls = body_scan_service.prepare_file_for_extraction(
            file_bytes,
            content_type=snapshot._media_type,
            filename=snapshot._storage_ref,
        )
    except Exception as exc:
        raise BodyScanAIValidationError(
            "body-scan document local preprocessing failed"
        ) from exc
    if not image_urls:
        raise BodyScanAIValidationError("body-scan document contains no readable pages")
    return PreparedBodyScanContent._issue(
        prepared_fingerprint=snapshot._fingerprint,
        image_urls=image_urls,
        is_pdf=is_pdf,
    )


async def start_body_scan_dispatch(
    session: AsyncSession,
    prepared: PreparedBodyScanParse,
    *,
    content: PreparedBodyScanContent | None = None,
    credential_resolver: Callable[[str], str | None] | None = None,
) -> ai_gateway_service.AIDispatchLease:
    """Freshly authorize and charge one document parse; caller commits."""

    snapshot = _require_prepared(prepared)
    if (
        not snapshot._dispatchable
        or snapshot._reservation_status is not AIInvocationStatus.PREPARED
    ):
        raise BodyScanAIInvocationStateError("body-scan parse is not dispatchable")
    _require_content(snapshot, content)
    await _lock_prepared_scope(session, snapshot, require_active_owner=True)
    return await ai_gateway_service.start_ai_dispatch(
        session,
        identity=WriteIdentity(snapshot._subject_id, snapshot._actor_user_id),
        invocation_id=snapshot._invocation_id,
        credential_resolver=credential_resolver or _resolve_openrouter_credential,
    )


async def cancel_prepared_body_scan_parse(
    session: AsyncSession,
    prepared: PreparedBodyScanParse,
) -> AIInvocation:
    """Release a zero-network reservation after a failed start boundary."""

    snapshot = _require_prepared(prepared)
    if snapshot._reservation_status is not AIInvocationStatus.PREPARED:
        raise BodyScanAIInvocationStateError(
            "only a prepared body-scan invocation can be cancelled"
        )
    await _lock_prepared_scope(session, snapshot, require_active_owner=True)
    return await ai_gateway_service.cancel_reserved_ai_invocation(
        session,
        identity=WriteIdentity(snapshot._subject_id, snapshot._actor_user_id),
        invocation_id=snapshot._invocation_id,
    )


async def render_body_scan(
    prepared: PreparedBodyScanParse,
    lease: ai_gateway_service.AIDispatchLease,
    *,
    file_bytes: bytes,
    content: PreparedBodyScanContent | None = None,
    llm_factory=None,
) -> ai_gateway_service.AICompletion[LLMCallResult[dict]]:
    """Perform exactly one bounded vision extraction with no database access."""

    snapshot = _require_prepared(prepared)
    if not isinstance(file_bytes, bytes) or len(file_bytes) != snapshot._byte_size:
        raise BodyScanAIValidationError("body-scan document bytes changed")
    if hashlib.sha256(file_bytes).hexdigest() != snapshot._sha256_hex:
        raise BodyScanAIValidationError("body-scan document hash changed")
    prepared_content = _require_content(snapshot, content)
    factory = llm_factory or LLMClient
    if not callable(factory):
        raise TypeError("llm_factory must be callable")

    async def provider_call(
        request: ai_gateway_service.AIDispatchRequest,
    ) -> LLMCallResult[dict]:
        if (
            request.invocation_id != snapshot._invocation_id
            or request.raw_payload_id != snapshot._raw_payload_id
            or request.model != snapshot._model
        ):
            raise BodyScanAIInvocationStateError(
                "body-scan dispatch provenance changed"
            )
        config = replace(load_config(), openrouter_api_key=request.credential)
        client = factory(config)
        return await body_scan_service.extract_prepared_file_with_usage(
            prepared_content._image_urls,
            llm=client,
            model=request.model,
            max_tokens=_BODY_SCAN_MAX_TOKENS,
        )

    def usage_extractor(
        result: LLMCallResult[dict],
    ) -> ai_gateway_service.SanitizedAIUsage:
        if not isinstance(result, LLMCallResult):
            raise BodyScanAIValidationError("body-scan provider result is invalid")
        _validated_extraction(result.value)
        if (
            result.input_tokens is None
            or result.output_tokens is None
            or result.cost_microunits is None
        ):
            raise BodyScanAIValidationError("body-scan provider usage is incomplete")
        return ai_gateway_service.SanitizedAIUsage(
            upstream_request_id=result.upstream_request_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_microunits=result.cost_microunits,
        )

    return await ai_gateway_service.dispatch_ai(
        lease,
        provider_call=provider_call,
        usage_extractor=usage_extractor,
    )


async def persist_body_scan_parse(
    session: AsyncSession,
    prepared: PreparedBodyScanParse,
    completion: ai_gateway_service.AICompletion[LLMCallResult[dict]],
) -> BodyScanParseResult:
    """Atomically finalize accounting and persist one validated extraction."""

    snapshot = _require_prepared(prepared)
    if completion.invocation_id != snapshot._invocation_id:
        raise BodyScanAIInvocationStateError(
            "body-scan completion belongs to another invocation"
        )
    # The call is already paid. Preserve its exact historical S/A/F/raw graph
    # and finalize accounting even if the actor was suspended or ownership was
    # administratively rotated after dispatch; T2 was the authorization point.
    locked = await _lock_prepared_scope(
        session,
        snapshot,
        require_active_owner=False,
    )
    invocation = await ai_gateway_service.finalize_ai_invocation(
        session,
        completion=completion,
    )
    if (
        invocation.subject_id != snapshot._subject_id
        or invocation.actor_user_id != snapshot._actor_user_id
        or invocation.raw_payload_id != snapshot._raw_payload_id
        or invocation.purpose != AIInvocationPurpose.BODY_SCAN_PARSE.value
        or invocation.source != AIInvocationSource.WEB.value
        or invocation.model != snapshot._model
    ):
        raise BodyScanAIInvocationStateError(
            "body-scan invocation provenance changed"
        )
    status = AIInvocationStatus(invocation.status)
    extracted: dict[str, Any] | None = None
    if status is AIInvocationStatus.SUCCEEDED:
        payload = completion.payload
        if not isinstance(payload, LLMCallResult):
            raise BodyScanAIInvocationStateError(
                "successful body-scan completion payload is missing"
            )
        extracted = _validated_extraction(payload.value)
        locked.raw.payload = extracted
        await session.flush()
    return BodyScanParseResult(
        raw_payload_id=locked.raw.id,
        file_asset_id=locked.asset.id,
        invocation_id=invocation.id,
        status=status,
        extracted=extracted,
    )
