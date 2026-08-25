"""Platform-funded, raw-first AI parsing for laboratory documents.

The upload boundary owns four deliberately separate phases:

* ``prepare_lab_document_parse`` records the private file/raw roots and reserves
  quota in one short transaction;
* ``start_lab_document_dispatch`` freshly revalidates those roots and charges
  one provider attempt in another short transaction;
* ``render_lab_document`` performs exactly one provider await with no database
  transaction;
* ``persist_lab_document_parse`` atomically finalizes sanitized accounting and
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
from vitals.services import ai_gateway_service, file_asset_service, labs_service
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.legacy_ownership import resolve_legacy_ownership_context
from vitals.utils.timeutils import now_utc


class LabDocumentAIError(RuntimeError):
    """Base class for platform-funded lab-document parsing."""


class LabDocumentAIValidationError(ValueError, LabDocumentAIError):
    """An upload or provider result is outside the bounded contract."""


class LabDocumentAIOwnershipError(LabDocumentAIError):
    """A subject, actor, file, or raw provenance root is inconsistent."""


class LabDocumentAIInvocationStateError(LabDocumentAIError):
    """An invocation cannot safely enter the requested lifecycle phase."""


class LabAIAvailabilityCode(StrEnum):
    AVAILABLE = "available"
    NOT_CONFIGURED = "not_configured"
    QUOTA = "quota"


@dataclass(frozen=True, slots=True)
class LabAIAvailability:
    """Redacted readiness projection; limits and subject data stay private."""

    available: bool
    code: LabAIAvailabilityCode


@dataclass(frozen=True, slots=True)
class LabDocumentParseResult:
    """Sanitized terminal projection with an optional in-memory extraction."""

    raw_payload_id: int
    file_asset_id: uuid.UUID
    invocation_id: uuid.UUID
    status: AIInvocationStatus
    extracted: dict[str, Any] | None = field(default=None, repr=False)


_PREPARED_LAB_DOCUMENT_SEAL = object()
_PREPARED_LAB_CONTENT_SEAL = object()
_PLACEHOLDER = {"_ai_parse": {"version": 1, "state": "prepared"}}
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_LAB_MAX_BYTES = 25 * 1024 * 1024
_LAB_MAX_TOKENS = 8192
_LAB_RESERVATION_OVERHEAD_UNITS = 4096
_LAB_RESERVED_COST_MICROUNITS = 10_000_000
_LAB_MAX_RESULTS = 1000
_LAB_IDEMPOTENCY_VERSION = "lab-document:v1"
_ALLOWED_TOP_LEVEL_KEYS = frozenset({"date", "lab_name", "results"})
_ALLOWED_RESULT_KEYS = frozenset(
    {"marker", "value", "unit", "ref_low", "ref_high"}
)


class PreparedLabDocumentParse:
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
        raise LabDocumentAIOwnershipError(
            "prepared lab parses are service-issued only"
        )

    @classmethod
    def _issue(cls, **values) -> "PreparedLabDocumentParse":
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
        object.__setattr__(prepared, "_seal", _PREPARED_LAB_DOCUMENT_SEAL)
        return prepared

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedLabDocumentParse is immutable")

    def __repr__(self) -> str:
        return (
            f"<PreparedLabDocumentParse invocation_id={self._invocation_id} "
            f"status={self._reservation_status.value} redacted>"
        )

    def __reduce__(self):
        raise TypeError("PreparedLabDocumentParse is not pickleable")

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


class PreparedLabDocumentContent:
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
        raise LabDocumentAIValidationError(
            "prepared lab content is service-issued only"
        )

    @classmethod
    def _issue(
        cls,
        *,
        prepared_fingerprint: tuple,
        image_urls: tuple[str, ...],
        is_pdf: bool,
    ) -> "PreparedLabDocumentContent":
        content = object.__new__(cls)
        object.__setattr__(content, "_prepared_fingerprint", prepared_fingerprint)
        object.__setattr__(content, "_image_urls", image_urls)
        object.__setattr__(content, "_is_pdf", is_pdf)
        object.__setattr__(
            content,
            "_fingerprint",
            (prepared_fingerprint, _image_urls_digest(image_urls), is_pdf),
        )
        object.__setattr__(content, "_seal", _PREPARED_LAB_CONTENT_SEAL)
        return content

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedLabDocumentContent is immutable")

    def __repr__(self) -> str:
        return "<PreparedLabDocumentContent redacted>"

    def __reduce__(self):
        raise TypeError("PreparedLabDocumentContent is not pickleable")


@dataclass(frozen=True, slots=True)
class _LockedLabDocumentScope:
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
        raise LabDocumentAIValidationError("lab raw payload is not canonical JSON") from exc
    return hashlib.sha256(encoded).digest()


def _image_urls_digest(image_urls: tuple[str, ...]) -> bytes:
    digest = hashlib.sha256()
    for image_url in image_urls:
        digest.update(len(image_url).to_bytes(8, "big"))
        digest.update(image_url.encode("ascii"))
    return digest.digest()


def _clean_media_type(value: str) -> str:
    if not isinstance(value, str):
        raise LabDocumentAIValidationError("media_type must be a string")
    cleaned = value.strip().lower()
    if (
        not cleaned
        or len(cleaned) > 255
        or not (cleaned == "application/pdf" or cleaned.startswith("image/"))
        or any(ord(char) < 32 for char in cleaned)
    ):
        raise LabDocumentAIValidationError("unsupported lab document media type")
    return cleaned


def _clean_size(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _LAB_MAX_BYTES
    ):
        raise LabDocumentAIValidationError("lab document size is invalid")
    return value


def _clean_sha256(value: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise LabDocumentAIValidationError("lab document sha256 is invalid")
    return value


def _clean_storage_ref(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not value.startswith("labs/")
        or value.startswith("/")
        or ".." in value
        or any(ord(char) < 32 for char in value)
    ):
        raise LabDocumentAIValidationError("lab document storage reference is invalid")
    return value


def _clean_model() -> str:
    model = load_config().llm_model_parser.strip()
    if not model or len(model) > 128 or any(ord(char) < 32 for char in model):
        raise LabDocumentAIValidationError("lab parser model is invalid")
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
    prepared: PreparedLabDocumentParse,
) -> PreparedLabDocumentParse:
    if not isinstance(prepared, PreparedLabDocumentParse):
        raise LabDocumentAIOwnershipError("lab parse capability is invalid")
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
            prepared._seal is _PREPARED_LAB_DOCUMENT_SEAL
            and prepared._fingerprint == expected
        )
    except (AttributeError, TypeError, ValueError, UnicodeError) as exc:
        raise LabDocumentAIOwnershipError("lab parse capability is invalid") from exc
    if not valid:
        raise LabDocumentAIOwnershipError("lab parse capability was modified")
    return prepared


def _require_content(
    prepared: PreparedLabDocumentParse,
    content: PreparedLabDocumentContent | None,
) -> PreparedLabDocumentContent | None:
    is_pdf = prepared._media_type == "application/pdf" or prepared._storage_ref.endswith(
        ".pdf"
    )
    if content is None:
        if is_pdf:
            raise LabDocumentAIValidationError(
                "PDF preprocessing is required before dispatch"
            )
        return None
    if not isinstance(content, PreparedLabDocumentContent):
        raise LabDocumentAIValidationError("prepared lab content is invalid")
    try:
        expected = (
            content._prepared_fingerprint,
            _image_urls_digest(content._image_urls),
            content._is_pdf,
        )
        valid = (
            content._seal is _PREPARED_LAB_CONTENT_SEAL
            and content._prepared_fingerprint == prepared._fingerprint
            and content._fingerprint == expected
            and content._is_pdf is is_pdf
            and bool(content._image_urls)
        )
    except (AttributeError, TypeError, ValueError, UnicodeError) as exc:
        raise LabDocumentAIValidationError(
            "prepared lab content is invalid"
        ) from exc
    if not valid:
        raise LabDocumentAIValidationError("prepared lab content was modified")
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
        raise LabDocumentAIOwnershipError("lab upload owner authorization failed")
    return subject, owner, WriteIdentity(subject.id, owner.id)


async def _lock_prepared_scope(
    session: AsyncSession,
    prepared: PreparedLabDocumentParse,
    *,
    require_active_owner: bool,
) -> _LockedLabDocumentScope:
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
        raise LabDocumentAIOwnershipError("prepared lab owner provenance is missing")
    if require_active_owner and (
        subject.owner_user_id != prepared._owner_user_id
        or owner.status != UserStatus.ACTIVE.value
    ):
        raise LabDocumentAIOwnershipError("prepared lab owner is no longer active")
    asset = await session.scalar(
        select(FileAsset)
        .where(FileAsset.id == prepared._file_asset_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    raw = await session.scalar(
        select(RawPayload)
        .where(RawPayload.id == prepared._raw_payload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if asset is None or raw is None:
        raise LabDocumentAIOwnershipError("prepared lab file/raw roots are missing")
    if (
        _asset_fingerprint(asset) != prepared._asset_fingerprint
        or _raw_fingerprint(raw) != prepared._raw_fingerprint
    ):
        raise LabDocumentAIOwnershipError("prepared lab file/raw roots changed")
    return _LockedLabDocumentScope(subject, owner, asset, raw)


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
        or asset.purpose != FileAssetPurpose.LAB_DOCUMENT.value
        or asset.storage_backend != storage_backend.value
        or asset.storage_ref != storage_ref
        or asset.media_type != media_type
        or asset.byte_size != byte_size
        or asset.sha256_hex != sha256_hex
        or asset.status != expected_status
        or asset.deleted_at is not None
        or asset.purged_at is not None
    ):
        raise LabDocumentAIOwnershipError("lab file provenance is inconsistent")
    if (
        raw.subject_id != identity.subject_id
        or raw.actor_user_id != identity.actor_user_id
        or raw.integration_connection_id is not None
        or raw.file_asset_id != asset.id
        or raw.domain != Domain.LABS.value
        or raw.source != Source.LAB_PARSER.value
        or raw.external_id != storage_ref
        or raw.processed_at is not None
    ):
        raise LabDocumentAIOwnershipError("lab raw provenance is inconsistent")


def _idempotency_key(raw_payload_id: int) -> str:
    return f"{_LAB_IDEMPOTENCY_VERSION}:{raw_payload_id}"


def _validated_extraction(payload: object) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or "_unparsed" in payload
        or set(payload) - _ALLOWED_TOP_LEVEL_KEYS
    ):
        raise LabDocumentAIValidationError("lab extraction shape is invalid")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) > _LAB_MAX_RESULTS:
        raise LabDocumentAIValidationError("lab extraction result count is invalid")
    on_date = payload.get("date")
    if on_date is not None and (
        not isinstance(on_date, str)
        or not on_date.strip()
        or len(on_date) > 32
        or any(ord(char) < 32 for char in on_date)
    ):
        raise LabDocumentAIValidationError("lab extraction date is invalid")
    lab_name = payload.get("lab_name")
    if lab_name is not None and (
        not isinstance(lab_name, str)
        or not lab_name.strip()
        or len(lab_name) > 255
        or any(ord(char) < 32 for char in lab_name)
    ):
        raise LabDocumentAIValidationError("lab extraction name is invalid")
    for item in results:
        if not isinstance(item, dict) or set(item) - _ALLOWED_RESULT_KEYS:
            raise LabDocumentAIValidationError("lab extraction marker shape is invalid")
        marker = item.get("marker")
        if (
            not isinstance(marker, str)
            or not marker.strip()
            or len(marker) > 255
            or any(ord(char) < 32 for char in marker)
        ):
            raise LabDocumentAIValidationError("lab extraction marker is invalid")
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
                raise LabDocumentAIValidationError(
                    "lab extraction numeric value is invalid"
                )
        unit = item.get("unit")
        if unit is not None and (
            not isinstance(unit, str)
            or not unit.strip()
            or len(unit) > 64
            or any(ord(char) < 32 for char in unit)
        ):
            raise LabDocumentAIValidationError("lab extraction unit is invalid")
    return payload


def _resolve_openrouter_credential(credential_ref: str) -> str | None:
    if credential_ref not in ai_gateway_service.ALLOWED_CREDENTIAL_REFS:
        return None
    value = load_config().openrouter_api_key.strip()
    return value or None


async def project_lab_ai_availability(
    session: AsyncSession,
    *,
    actor_username: str,
) -> LabAIAvailability:
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
        return LabAIAvailability(False, LabAIAvailabilityCode.NOT_CONFIGURED)
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
        return LabAIAvailability(False, LabAIAvailabilityCode.NOT_CONFIGURED)
    if any(
        row.reserved_cost_microunits + row.charged_cost_microunits
        >= row.cost_limit_microunits
        or row.reserved_units + row.charged_units >= row.unit_limit
        for row in (platform_periods[0], subject_periods[0])
    ):
        return LabAIAvailability(False, LabAIAvailabilityCode.QUOTA)
    return LabAIAvailability(True, LabAIAvailabilityCode.AVAILABLE)


async def prepare_lab_document_parse(
    session: AsyncSession,
    *,
    actor_username: str,
    storage_ref: str,
    media_type: str,
    byte_size: int,
    sha256_hex: str,
    storage_backend: FileStorageBackend | str = FileStorageBackend.LEGACY_LOCAL,
) -> PreparedLabDocumentParse:
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
        raise LabDocumentAIValidationError(
            "lab document storage backend is invalid"
        ) from exc
    if normalized_backend not in {
        FileStorageBackend.LEGACY_LOCAL,
        FileStorageBackend.PRIVATE_LOCAL,
    }:
        raise LabDocumentAIValidationError("lab document storage backend is invalid")
    register = (
        file_asset_service.register_private_local
        if normalized_backend is FileStorageBackend.PRIVATE_LOCAL
        else file_asset_service.register_legacy_local
    )
    asset = await register(
        session,
        subject_id=identity.subject_id,
        uploaded_by_user_id=identity.actor_user_id,
        purpose=FileAssetPurpose.LAB_DOCUMENT,
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
                        (RawPayload.domain == Domain.LABS.value)
                        & (RawPayload.source == Source.LAB_PARSER.value)
                        & (RawPayload.external_id == cleaned_ref)
                    ),
                )
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(raw_rows) > 1:
        raise LabDocumentAIOwnershipError("lab upload raw provenance is ambiguous")
    if raw_rows:
        raw = raw_rows[0]
    else:
        raw = RawPayload(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            integration_connection_id=None,
            file_asset_id=asset.id,
            domain=Domain.LABS.value,
            source=Source.LAB_PARSER.value,
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
                AIInvocation.purpose == AIInvocationPurpose.LAB_DOCUMENT_PARSE.value,
            )
            .order_by(AIInvocation.created_at, AIInvocation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(invocations) > 1:
        raise LabDocumentAIInvocationStateError(
            "lab document has multiple parser invocations"
        )
    existing = invocations[0] if invocations else None
    if existing is not None and (
        existing.actor_user_id != identity.actor_user_id
        or existing.source != AIInvocationSource.WEB.value
        or existing.idempotency_key != _idempotency_key(raw.id)
    ):
        raise LabDocumentAIInvocationStateError(
            "lab parser invocation provenance is inconsistent"
        )
    reserved_units = (
        cleaned_size * 4 + _LAB_MAX_TOKENS + _LAB_RESERVATION_OVERHEAD_UNITS
    )
    if existing is None or existing.status == AIInvocationStatus.PREPARED.value:
        reservation = await ai_gateway_service.reserve_ai_invocation(
            session,
            identity=identity,
            purpose=AIInvocationPurpose.LAB_DOCUMENT_PARSE,
            source=AIInvocationSource.WEB,
            model=model,
            idempotency_key=_idempotency_key(raw.id),
            reserved_cost_microunits=_LAB_RESERVED_COST_MICROUNITS,
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
        raise LabDocumentAIInvocationStateError(
            "unfinished lab invocation has a non-placeholder raw payload"
        )
    return PreparedLabDocumentParse._issue(
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


def prepare_lab_document_content(
    prepared: PreparedLabDocumentParse,
    *,
    file_bytes: bytes,
) -> PreparedLabDocumentContent:
    """Validate and locally convert bytes before any paid dispatch starts."""

    snapshot = _require_prepared(prepared)
    if not isinstance(file_bytes, bytes) or len(file_bytes) != snapshot._byte_size:
        raise LabDocumentAIValidationError("lab document bytes changed")
    if hashlib.sha256(file_bytes).hexdigest() != snapshot._sha256_hex:
        raise LabDocumentAIValidationError("lab document hash changed")
    is_pdf = snapshot._media_type == "application/pdf" or snapshot._storage_ref.endswith(
        ".pdf"
    )
    try:
        image_urls = labs_service.prepare_file_for_extraction(
            file_bytes,
            content_type=snapshot._media_type,
            filename=snapshot._storage_ref,
        )
    except Exception as exc:
        raise LabDocumentAIValidationError(
            "lab document local preprocessing failed"
        ) from exc
    if not image_urls:
        raise LabDocumentAIValidationError("lab document contains no readable pages")
    return PreparedLabDocumentContent._issue(
        prepared_fingerprint=snapshot._fingerprint,
        image_urls=image_urls,
        is_pdf=is_pdf,
    )


async def start_lab_document_dispatch(
    session: AsyncSession,
    prepared: PreparedLabDocumentParse,
    *,
    content: PreparedLabDocumentContent | None = None,
    credential_resolver: Callable[[str], str | None] | None = None,
) -> ai_gateway_service.AIDispatchLease:
    """Freshly authorize and charge one document parse; caller commits."""

    snapshot = _require_prepared(prepared)
    if (
        not snapshot._dispatchable
        or snapshot._reservation_status is not AIInvocationStatus.PREPARED
    ):
        raise LabDocumentAIInvocationStateError("lab parse is not dispatchable")
    _require_content(snapshot, content)
    await _lock_prepared_scope(session, snapshot, require_active_owner=True)
    return await ai_gateway_service.start_ai_dispatch(
        session,
        identity=WriteIdentity(snapshot._subject_id, snapshot._actor_user_id),
        invocation_id=snapshot._invocation_id,
        credential_resolver=credential_resolver or _resolve_openrouter_credential,
    )


async def cancel_prepared_lab_document_parse(
    session: AsyncSession,
    prepared: PreparedLabDocumentParse,
) -> AIInvocation:
    """Release a zero-network reservation after a failed start boundary."""

    snapshot = _require_prepared(prepared)
    if snapshot._reservation_status is not AIInvocationStatus.PREPARED:
        raise LabDocumentAIInvocationStateError(
            "only a prepared lab invocation can be cancelled"
        )
    await _lock_prepared_scope(session, snapshot, require_active_owner=True)
    return await ai_gateway_service.cancel_reserved_ai_invocation(
        session,
        identity=WriteIdentity(snapshot._subject_id, snapshot._actor_user_id),
        invocation_id=snapshot._invocation_id,
    )


async def render_lab_document(
    prepared: PreparedLabDocumentParse,
    lease: ai_gateway_service.AIDispatchLease,
    *,
    file_bytes: bytes,
    content: PreparedLabDocumentContent | None = None,
    llm_factory=None,
) -> ai_gateway_service.AICompletion[LLMCallResult[dict]]:
    """Perform exactly one bounded vision extraction with no database access."""

    snapshot = _require_prepared(prepared)
    if not isinstance(file_bytes, bytes) or len(file_bytes) != snapshot._byte_size:
        raise LabDocumentAIValidationError("lab document bytes changed")
    if hashlib.sha256(file_bytes).hexdigest() != snapshot._sha256_hex:
        raise LabDocumentAIValidationError("lab document hash changed")
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
            raise LabDocumentAIInvocationStateError(
                "lab dispatch provenance changed"
            )
        config = replace(load_config(), openrouter_api_key=request.credential)
        client = factory(config)
        if prepared_content is not None and prepared_content._is_pdf:
            return await labs_service.extract_prepared_file_with_usage(
                prepared_content._image_urls,
                llm=client,
                model=request.model,
                max_tokens=_LAB_MAX_TOKENS,
                is_document=True,
            )
        return await labs_service.extract_from_file_with_usage(
            file_bytes,
            llm=client,
            content_type=snapshot._media_type,
            filename=snapshot._storage_ref,
            model=request.model,
            max_tokens=_LAB_MAX_TOKENS,
        )

    def usage_extractor(
        result: LLMCallResult[dict],
    ) -> ai_gateway_service.SanitizedAIUsage:
        if not isinstance(result, LLMCallResult):
            raise LabDocumentAIValidationError("lab provider result is invalid")
        _validated_extraction(result.value)
        if (
            result.input_tokens is None
            or result.output_tokens is None
            or result.cost_microunits is None
        ):
            raise LabDocumentAIValidationError("lab provider usage is incomplete")
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


async def persist_lab_document_parse(
    session: AsyncSession,
    prepared: PreparedLabDocumentParse,
    completion: ai_gateway_service.AICompletion[LLMCallResult[dict]],
) -> LabDocumentParseResult:
    """Atomically finalize accounting and persist one validated extraction."""

    snapshot = _require_prepared(prepared)
    if completion.invocation_id != snapshot._invocation_id:
        raise LabDocumentAIInvocationStateError(
            "lab completion belongs to another invocation"
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
        or invocation.purpose != AIInvocationPurpose.LAB_DOCUMENT_PARSE.value
        or invocation.source != AIInvocationSource.WEB.value
        or invocation.model != snapshot._model
    ):
        raise LabDocumentAIInvocationStateError(
            "lab invocation provenance changed"
        )
    status = AIInvocationStatus(invocation.status)
    extracted: dict[str, Any] | None = None
    if status is AIInvocationStatus.SUCCEEDED:
        payload = completion.payload
        if not isinstance(payload, LLMCallResult):
            raise LabDocumentAIInvocationStateError(
                "successful lab completion payload is missing"
            )
        extracted = _validated_extraction(payload.value)
        locked.raw.payload = extracted
        await session.flush()
    return LabDocumentParseResult(
        raw_payload_id=locked.raw.id,
        file_asset_id=locked.asset.id,
        invocation_id=invocation.id,
        status=status,
        extracted=extracted,
    )
