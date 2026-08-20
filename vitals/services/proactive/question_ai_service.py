"""Platform-funded, raw-backed Telegram question replies.

The reply body is PHI and therefore exists only in an opaque in-memory
capability between the provider await and final accounting.  A later recovery
can account for and journal a terminal invocation, but deliberately falls back
instead of trying to recreate a successful response from the database.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from dataclasses import dataclass, field
from dataclasses import replace
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Source,
    UserStatus,
)
from vitals.integrations.llm_client import LLMCallResult, LLMClient
from vitals.models.ai import AIInvocation
from vitals.models.identity import HealthSubject, User
from vitals.models.proactive import Notification
from vitals.models.raw_payload import RawPayload
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import ai_gateway_service
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.proactive import prefs
from vitals.services.proactive.ownership import ProactiveOwnershipContext
from vitals.utils.timeutils import now_local


class QuestionAIError(RuntimeError):
    """Base class for a bounded Telegram question-reply operation."""


class QuestionAIOwnershipError(QuestionAIError):
    """The current subject, Telegram connection, or raw graph is invalid."""


class QuestionAIStateError(QuestionAIError):
    """A question invocation cannot safely progress its single attempt."""


class QuestionAIInputError(QuestionAIError):
    """A bounded local input cannot be sent to the provider."""


class QuestionAIStaleError(QuestionAIError):
    """A later immutable Telegram edit owns this logical message."""


class QuestionAIModuleDisabledError(QuestionAIError):
    """The Signals emergency gate closed before paid dispatch."""


_PREPARED_SEAL = object()
_POLICY_VERSION = "question-reply:v1"
_MAX_INPUT_BYTES = 32_768
_MAX_TOKENS = 800
_RESERVATION_OVERHEAD_UNITS = 512
_RESERVED_COST_MICROUNITS = 1_000_000
_REPLY_SYSTEM = """\
Ты отвечаешь на вопрос владельца дашборда здоровья.
Перед тобой могут быть последние сообщения самого бота (по порядку, последнее —
внизу) и JSON с данными последнего разбора дня. Короткий вопрос без пояснений
почти всегда про то, что бот только что написал — сначала ищи ответ там, и
только потом в JSON. Отвечай по-русски, коротко (2-4 предложения); числа бери
только из этих двух источников.
Если ответа в них нет — так и скажи. Никаких выдуманных чисел.\
"""


@dataclass(frozen=True, slots=True)
class QuestionReplyResult:
    """Sanitized terminal projection; answer text is never retained in repr."""

    invocation_id: uuid.UUID | None
    status: AIInvocationStatus | None
    text: str | None = field(default=None, repr=False)
    stale: bool = False

    def __reduce__(self):
        raise TypeError("QuestionReplyResult is not pickleable")


class PreparedQuestionReply:
    """Opaque T1 snapshot. Prompt/context/facts cannot leak via diagnostics."""

    __slots__ = (
        "_actor_user_id", "_connection_id", "_dispatchable", "_fingerprint",
        "_invocation_id", "_model", "_owner_user_id", "_prompt",
        "_raw_fingerprint", "_raw_payload_id", "_reservation_status", "_seal",
        "_subject_id", "_system_prompt",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise QuestionAIError("prepared question replies are service-issued only")

    @classmethod
    def _issue(cls, **values) -> "PreparedQuestionReply":
        prepared = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(prepared, name, value)
        object.__setattr__(
            prepared,
            "_fingerprint",
            (
                values["_subject_id"], values["_owner_user_id"],
                values["_actor_user_id"], values["_connection_id"],
                values["_raw_payload_id"], values["_raw_fingerprint"],
                values["_invocation_id"], values["_reservation_status"],
                values["_dispatchable"], values["_model"],
                hashlib.sha256(values["_system_prompt"].encode()).digest(),
                hashlib.sha256(values["_prompt"].encode()).digest(),
            ),
        )
        object.__setattr__(prepared, "_seal", _PREPARED_SEAL)
        return prepared

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedQuestionReply is immutable")

    def __repr__(self) -> str:
        return (
            f"<PreparedQuestionReply invocation_id={self._invocation_id} "
            f"status={getattr(self._reservation_status, 'value', None)} redacted>"
        )

    def __reduce__(self):
        raise TypeError("PreparedQuestionReply is not pickleable")

    @property
    def invocation_id(self) -> uuid.UUID | None:
        return self._invocation_id

    @property
    def reservation_status(self) -> AIInvocationStatus | None:
        return self._reservation_status

    @property
    def dispatchable(self) -> bool:
        return self._dispatchable

    @property
    def raw_payload_id(self) -> int:
        return self._raw_payload_id


def _require_prepared(prepared: PreparedQuestionReply) -> PreparedQuestionReply:
    if (
        not isinstance(prepared, PreparedQuestionReply)
        or prepared._seal is not _PREPARED_SEAL
        or prepared._fingerprint
        != (
            prepared._subject_id, prepared._owner_user_id,
            prepared._actor_user_id, prepared._connection_id,
            prepared._raw_payload_id, prepared._raw_fingerprint,
            prepared._invocation_id, prepared._reservation_status,
            prepared._dispatchable, prepared._model,
            hashlib.sha256(prepared._system_prompt.encode()).digest(),
            hashlib.sha256(prepared._prompt.encode()).digest(),
        )
    ):
        raise QuestionAIError("prepared question reply is forged or corrupted")
    return prepared


def _has_forbidden_control(value: str) -> bool:
    return any(
        unicodedata.category(char).startswith("C")
        for char in value
        if char not in {"\n", "\r", "\t"}
    )


def _payload_hash(payload: object) -> bytes:
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError) as exc:
        raise QuestionAIOwnershipError("question raw is not canonical JSON") from exc
    return hashlib.sha256(encoded).digest()


def _raw_fingerprint(raw: RawPayload) -> tuple:
    return (
        raw.id, raw.subject_id, raw.actor_user_id, raw.integration_connection_id,
        raw.file_asset_id, raw.domain, raw.source, raw.external_id, raw.fetched_at,
        _payload_hash(raw.payload),
    )


def _raw_question(raw: RawPayload) -> str:
    payload = raw.payload if isinstance(raw.payload, dict) else {}
    message = payload.get("message")
    if not isinstance(message, dict):
        message = payload.get("edited_message")
    value = message.get("text") if isinstance(message, dict) else payload.get("text")
    question = str(value or "").strip()
    if not question or _has_forbidden_control(question):
        raise QuestionAIInputError("question raw has invalid text")
    return question


def _prompt(question: str, context: str, facts: str) -> str:
    parts: list[str] = []
    if context:
        parts.append(f"Последние сообщения бота:\n{context}")
    if facts:
        parts.append(f"Данные последнего разбора дня (JSON):\n{facts}")
    parts.append(f"Вопрос:\n{question}")
    return "\n\n".join(parts)


def _idempotency_key(raw_payload_id: int) -> str:
    return hashlib.sha256(
        f"{_POLICY_VERSION}|{raw_payload_id}".encode()
    ).hexdigest()


async def _lock_scope(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    require_active_owner: bool,
    require_processed: bool,
) -> tuple[HealthSubject, User, RawPayload, tuple]:
    """Lock governance -> S -> active owner -> current Telegram C -> raw."""

    if not isinstance(ownership, ProactiveOwnershipContext):
        raise TypeError("ownership must be a ProactiveOwnershipContext")
    if isinstance(raw_payload_id, bool) or not isinstance(raw_payload_id, int) or raw_payload_id < 1:
        raise QuestionAIError("raw_payload_id must be a positive integer")
    await acquire_identity_governance_lock(session)
    subject = await session.scalar(
        select(HealthSubject).where(HealthSubject.id == ownership.subject_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if subject is None or subject.owner_user_id != ownership.recipient_user_id:
        raise QuestionAIOwnershipError("Telegram recipient is not the subject owner")
    owner = await session.scalar(
        select(User).where(User.id == subject.owner_user_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if owner is None or (require_active_owner and owner.status != UserStatus.ACTIVE.value):
        raise QuestionAIOwnershipError("question owner is unavailable")
    raw_projection = await session.execute(
        select(RawPayload.integration_connection_id).where(RawPayload.id == raw_payload_id)
    )
    raw_connection_id = raw_projection.scalar_one_or_none()
    if raw_connection_id is None:
        raise QuestionAIOwnershipError("question raw has no recipient connection")
    connections: dict[uuid.UUID, IntegrationConnection] = {}
    for connection_id in sorted({ownership.connection_id, raw_connection_id}, key=str):
        connection = await session.scalar(
            select(IntegrationConnection).where(IntegrationConnection.id == connection_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if (
            connection is None
            or connection.subject_id != ownership.subject_id
            or connection.provider != IntegrationProvider.TELEGRAM.value
            or connection.connection_type != IntegrationConnectionType.RECIPIENT.value
        ):
            raise QuestionAIOwnershipError("Telegram recipient provenance is invalid")
        connections[connection_id] = connection
    current_connection = connections[ownership.connection_id]
    if current_connection.status not in {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }:
        raise QuestionAIOwnershipError("current Telegram recipient is unavailable")
    raw_connection = connections[raw_connection_id]
    if raw_connection.status not in {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
        IntegrationConnectionStatus.DISABLED.value,
        IntegrationConnectionStatus.RETIRED.value,
    }:
        raise QuestionAIOwnershipError("historical Telegram recipient is unavailable")
    raw = await session.scalar(
        select(RawPayload).where(RawPayload.id == raw_payload_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if raw is None:
        raise QuestionAIOwnershipError("question raw does not exist")
    if (
        raw.subject_id != ownership.subject_id
        or raw.actor_user_id != ownership.recipient_user_id
        or raw.integration_connection_id != raw_connection_id
        or raw.file_asset_id is not None
        or raw.domain != Domain.SIGNALS.value
        or raw.source != Source.TELEGRAM.value
        or (require_processed and raw.processed_at is None)
    ):
        raise QuestionAIOwnershipError("question raw provenance is invalid")
    await _require_current_logical_message(session, raw=raw, ownership=ownership)
    return subject, owner, raw, _raw_fingerprint(raw)


def _prepared(
    *, ownership: ProactiveOwnershipContext, owner: User, raw: RawPayload,
    raw_fingerprint: tuple, model: str, prompt: str,
    invocation_id: uuid.UUID | None, status: AIInvocationStatus | None,
    dispatchable: bool,
) -> PreparedQuestionReply:
    return PreparedQuestionReply._issue(
        _subject_id=ownership.subject_id,
        _owner_user_id=owner.id,
        _actor_user_id=ownership.recipient_user_id,
        _connection_id=ownership.connection_id,
        _raw_payload_id=raw.id,
        _raw_fingerprint=raw_fingerprint,
        _invocation_id=invocation_id,
        _reservation_status=status,
        _dispatchable=dispatchable,
        _model=model,
        _system_prompt=_REPLY_SYSTEM,
        _prompt=prompt,
    )


async def prepare_live_question_reply(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    context: str,
    facts: str,
) -> PreparedQuestionReply:
    """Snapshot one claimed question and reserve its sole possible AI attempt."""

    _, owner, raw, fingerprint = await _lock_scope(
        session, ownership=ownership, raw_payload_id=raw_payload_id,
        require_active_owner=True, require_processed=False,
    )
    question = _raw_question(raw)
    if not isinstance(context, str) or not isinstance(facts, str):
        raise TypeError("question context and facts must be strings")
    prompt = _prompt(question, context, facts)
    if len(prompt.encode("utf-8")) > _MAX_INPUT_BYTES:
        raise QuestionAIInputError("question prompt exceeds the input limit")
    key = _idempotency_key(raw.id)
    existing_rows = list(await session.scalars(
        select(AIInvocation).where(
            AIInvocation.subject_id == ownership.subject_id,
            AIInvocation.raw_payload_id == raw.id,
            AIInvocation.purpose == AIInvocationPurpose.QUESTION_REPLY.value,
        ).with_for_update().execution_options(populate_existing=True)
    ))
    if len(existing_rows) > 1:
        raise QuestionAIStateError("question raw has multiple AI invocations")
    if existing_rows:
        existing = existing_rows[0]
        if (
            raw.processed_at is None
            or
            existing.actor_user_id != ownership.recipient_user_id
            or existing.raw_payload_id != raw.id
            or existing.source != AIInvocationSource.TELEGRAM.value
            or existing.idempotency_key != key
        ):
            raise QuestionAIStateError("question idempotency provenance changed")
        status = AIInvocationStatus(existing.status)
        return _prepared(
            ownership=ownership, owner=owner, raw=raw, raw_fingerprint=fingerprint,
            model=existing.model, prompt=prompt, invocation_id=existing.id,
            # The prompt is intentionally not persisted.  A new process cannot
            # prove that mutable conversation/digest context reconstructs the
            # byte-identical request reserved by a prior T1, so it must never
            # consume an inherited PREPARED lease.
            status=status, dispatchable=False,
        )
    model = load_config().llm_model_digest.strip()
    if not model or len(model) > 128 or _has_forbidden_control(model):
        raise QuestionAIInputError("question model is invalid")
    # The raw classification and its sole possible reservation are one durable
    # T1 outcome. A crash can leave either both absent or both present, never a
    # processed question with no DB-discoverable recovery lineage.
    raw.processed_at = raw.processed_at or now_local()
    await session.flush()
    reservation = await ai_gateway_service.reserve_ai_invocation(
        session,
        identity=WriteIdentity(
            subject_id=ownership.subject_id, actor_user_id=ownership.recipient_user_id
        ),
        purpose=AIInvocationPurpose.QUESTION_REPLY,
        source=AIInvocationSource.TELEGRAM,
        model=model,
        idempotency_key=key,
        reserved_cost_microunits=_RESERVED_COST_MICROUNITS,
        reserved_units=len(prompt.encode("utf-8")) + _MAX_TOKENS + _RESERVATION_OVERHEAD_UNITS,
        raw_payload_id=raw.id,
    )
    return _prepared(
        ownership=ownership, owner=owner, raw=raw, raw_fingerprint=fingerprint,
        model=model, prompt=prompt, invocation_id=reservation.invocation_id,
        status=reservation.status, dispatchable=reservation.dispatchable,
    )


def _identity(prepared: PreparedQuestionReply) -> WriteIdentity:
    return WriteIdentity(
        subject_id=prepared._subject_id, actor_user_id=prepared._actor_user_id
    )


async def _revalidate(
    session: AsyncSession, prepared: PreparedQuestionReply, *, active: bool
) -> None:
    _, owner, raw, fingerprint = await _lock_scope(
        session,
        ownership=ProactiveOwnershipContext(
            subject_id=prepared._subject_id,
            recipient_user_id=prepared._owner_user_id,
            connection_id=prepared._connection_id,
        ),
        raw_payload_id=prepared._raw_payload_id,
        require_active_owner=active,
        require_processed=True,
    )
    if owner.id != prepared._owner_user_id or fingerprint != prepared._raw_fingerprint:
        raise QuestionAIOwnershipError("question raw provenance changed")
    _raw_question(raw)


def _credential(credential_ref: str) -> str | None:
    if credential_ref not in ai_gateway_service.ALLOWED_CREDENTIAL_REFS:
        return None
    value = load_config().openrouter_api_key.strip()
    return value or None


async def start_question_dispatch(
    session: AsyncSession,
    prepared: PreparedQuestionReply,
    *, credential_resolver: Callable[[str], str | None] | None = None,
) -> ai_gateway_service.AIDispatchLease:
    snapshot = _require_prepared(prepared)
    if not snapshot._dispatchable or snapshot._invocation_id is None:
        raise QuestionAIStateError("question reply is not dispatchable")
    await _revalidate(session, snapshot, active=True)
    # `_revalidate` holds the subject lock, so a concurrent module change is
    # serialized with this final pre-charge decision.
    if not await prefs.bot_enabled(
        session,
        subject_id=snapshot._subject_id,
        strict=True,
    ):
        raise QuestionAIModuleDisabledError(
            "question replies are disabled before dispatch"
        )
    return await ai_gateway_service.start_ai_dispatch(
        session, identity=_identity(snapshot), invocation_id=snapshot._invocation_id,
        credential_resolver=credential_resolver or _credential,
    )


async def cancel_prepared_question_reply(
    session: AsyncSession, prepared: PreparedQuestionReply
) -> AIInvocation:
    """Release an unstarted reservation without authorizing a replacement."""

    snapshot = _require_prepared(prepared)
    if snapshot._invocation_id is None:
        raise QuestionAIStateError("question reply has no reservation")
    await _revalidate(session, snapshot, active=True)
    return await ai_gateway_service.cancel_reserved_ai_invocation(
        session,
        identity=_identity(snapshot),
        invocation_id=snapshot._invocation_id,
        error_code=AIInvocationErrorCode.CANCELLED_BY_POLICY,
    )


async def render_question_reply(
    prepared: PreparedQuestionReply,
    lease: ai_gateway_service.AIDispatchLease,
    *, llm_factory=None,
) -> ai_gateway_service.AICompletion[LLMCallResult[str]]:
    """Perform the single provider await, with no DB session or retry."""

    snapshot = _require_prepared(prepared)
    factory = llm_factory or LLMClient
    if not callable(factory):
        raise TypeError("llm_factory must be callable")

    async def provider_call(request: ai_gateway_service.AIDispatchRequest) -> LLMCallResult[str]:
        if (
            request.invocation_id != snapshot._invocation_id
            or request.raw_payload_id != snapshot._raw_payload_id
            or request.model != snapshot._model
        ):
            raise QuestionAIStateError("question dispatch provenance changed")
        client = factory(replace(load_config(), openrouter_api_key=request.credential))
        return await client.complete_text_with_usage(
            snapshot._prompt, model=request.model, system=snapshot._system_prompt,
            max_tokens=_MAX_TOKENS,
        )

    def usage(result: LLMCallResult[str]) -> ai_gateway_service.SanitizedAIUsage:
        if (
            not isinstance(result, LLMCallResult)
            or not isinstance(result.value, str)
            or not result.value.strip()
            or result.input_tokens is None
            or result.output_tokens is None
            or result.cost_microunits is None
        ):
            raise QuestionAIError("question provider result is invalid")
        return ai_gateway_service.SanitizedAIUsage(
            upstream_request_id=result.upstream_request_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_microunits=result.cost_microunits,
        )

    return await ai_gateway_service.dispatch_ai(
        lease, provider_call=provider_call, usage_extractor=usage
    )


async def persist_question_reply(
    session: AsyncSession,
    prepared: PreparedQuestionReply,
    completion: ai_gateway_service.AICompletion[LLMCallResult[str]],
) -> QuestionReplyResult:
    snapshot = _require_prepared(prepared)
    if completion.invocation_id != snapshot._invocation_id:
        raise QuestionAIStateError("question completion belongs to another call")
    await _revalidate(session, snapshot, active=False)
    invocation = await ai_gateway_service.finalize_ai_invocation(
        session, completion=completion
    )
    if (
        invocation.subject_id != snapshot._subject_id
        or invocation.actor_user_id != snapshot._actor_user_id
        or invocation.raw_payload_id != snapshot._raw_payload_id
        or invocation.purpose != AIInvocationPurpose.QUESTION_REPLY.value
        or invocation.source != AIInvocationSource.TELEGRAM.value
        or invocation.model != snapshot._model
    ):
        raise QuestionAIStateError("question invocation provenance changed")
    status = AIInvocationStatus(invocation.status)
    text = None
    if status is AIInvocationStatus.SUCCEEDED:
        payload = completion.payload
        if not isinstance(payload, LLMCallResult) or not payload.value.strip():
            raise QuestionAIStateError("successful question response is missing")
        text = payload.value.strip()
    return QuestionReplyResult(
        invocation_id=invocation.id,
        status=status,
        text=text,
        stale=await raw_is_superseded(
            session,
            subject_id=snapshot._subject_id,
            raw_payload_id=snapshot._raw_payload_id,
        ),
    )


async def invocation_is_journaled(
    session: AsyncSession, *, invocation_id: uuid.UUID,
    ownership: ProactiveOwnershipContext,
) -> bool:
    row = await session.execute(
        select(
            Notification.subject_id, Notification.actor_user_id,
            Notification.recipient_user_id, Notification.channel,
            Notification.category, Notification.payload,
            Notification.ai_invocation_id,
            IntegrationConnection.subject_id, IntegrationConnection.provider,
            IntegrationConnection.connection_type, IntegrationConnection.status,
            AIInvocation.subject_id, AIInvocation.actor_user_id,
            AIInvocation.purpose, AIInvocation.source, AIInvocation.status,
            AIInvocation.raw_payload_id,
        ).join(
            IntegrationConnection,
            IntegrationConnection.id == Notification.integration_connection_id,
        ).outerjoin(
            AIInvocation,
            AIInvocation.id == Notification.ai_invocation_id,
        )
        .where(Notification.ai_invocation_id == invocation_id)
    )
    value = row.one_or_none()
    if value is None:
        return False
    raw_payload_id, invocation_raw_payload_id = _validate_owned_historical_reply_journal(
        value,
        ownership=ownership,
        expected_invocation_id=invocation_id,
    )
    await _validate_journal_raw_scope(
        session,
        ownership=ownership,
        raw_payload_id=raw_payload_id,
        invocation_raw_payload_id=invocation_raw_payload_id,
    )
    return True


async def delivery_is_journaled(
    session: AsyncSession, *, raw_payload_id: int, ownership: ProactiveOwnershipContext
) -> bool:
    """Validate an existing stable reply/fallback journal before another T1."""

    row = await session.execute(
        select(
            Notification.subject_id, Notification.actor_user_id,
            Notification.recipient_user_id, Notification.channel,
            Notification.category, Notification.payload,
            Notification.ai_invocation_id,
            IntegrationConnection.subject_id, IntegrationConnection.provider,
            IntegrationConnection.connection_type, IntegrationConnection.status,
            AIInvocation.subject_id, AIInvocation.actor_user_id,
            AIInvocation.purpose, AIInvocation.source, AIInvocation.status,
            AIInvocation.raw_payload_id,
        ).join(
            IntegrationConnection,
            IntegrationConnection.id == Notification.integration_connection_id,
        ).outerjoin(
            AIInvocation,
            AIInvocation.id == Notification.ai_invocation_id,
        )
        .where(Notification.dedupe_key == delivery_dedupe_key(raw_payload_id))
    )
    value = row.one_or_none()
    if value is None:
        return False
    journal_raw_payload_id, invocation_raw_payload_id = (
        _validate_owned_historical_reply_journal(
            value,
            ownership=ownership,
            expected_raw_payload_id=raw_payload_id,
        )
    )
    await _validate_journal_raw_scope(
        session,
        ownership=ownership,
        raw_payload_id=journal_raw_payload_id,
        invocation_raw_payload_id=invocation_raw_payload_id,
    )
    return True


def _validate_owned_historical_reply_journal(
    row: tuple,
    *,
    ownership: ProactiveOwnershipContext,
    expected_invocation_id: uuid.UUID | None = None,
    expected_raw_payload_id: int | None = None,
) -> tuple[int, int | None]:
    """Validate a redacted reply journal and its optional paid invocation."""

    (
        notification_subject_id,
        notification_actor_user_id,
        recipient_user_id,
        channel,
        category,
        payload,
        notification_invocation_id,
        connection_subject_id,
        connection_provider,
        connection_type,
        connection_status,
        invocation_subject_id,
        invocation_actor_user_id,
        invocation_purpose,
        invocation_source,
        invocation_status,
        invocation_raw_payload_id,
    ) = row
    if (
        notification_subject_id != ownership.subject_id
        or notification_actor_user_id is not None
        or recipient_user_id != ownership.recipient_user_id
        or channel != IntegrationProvider.TELEGRAM.value
        or category != "reply"
        or connection_subject_id != ownership.subject_id
        or connection_provider != IntegrationProvider.TELEGRAM.value
        or connection_type != IntegrationConnectionType.RECIPIENT.value
        or connection_status
        not in {
            IntegrationConnectionStatus.LEGACY.value,
            IntegrationConnectionStatus.ACTIVE.value,
            IntegrationConnectionStatus.DISABLED.value,
            IntegrationConnectionStatus.RETIRED.value,
        }
        or not isinstance(payload, dict)
        or set(payload) != {"content_redacted", "raw_payload_id"}
        or payload.get("content_redacted") is not True
        or isinstance(payload.get("raw_payload_id"), bool)
        or not isinstance(payload.get("raw_payload_id"), int)
        or payload["raw_payload_id"] < 1
    ):
        raise QuestionAIOwnershipError("question journal provenance is invalid")
    raw_payload_id = payload["raw_payload_id"]
    if (
        expected_raw_payload_id is not None
        and raw_payload_id != expected_raw_payload_id
    ):
        raise QuestionAIOwnershipError("question journal raw provenance changed")
    if (
        expected_invocation_id is not None
        and notification_invocation_id != expected_invocation_id
    ):
        raise QuestionAIOwnershipError(
            "question journal invocation provenance changed"
        )
    if notification_invocation_id is None:
        if expected_invocation_id is not None or any(
            value is not None
            for value in (
                invocation_subject_id,
                invocation_actor_user_id,
                invocation_purpose,
                invocation_source,
                invocation_status,
                invocation_raw_payload_id,
            )
        ):
            raise QuestionAIOwnershipError(
                "question journal has partial AI provenance"
            )
        return raw_payload_id, None
    if (
        invocation_subject_id != ownership.subject_id
        or invocation_actor_user_id != ownership.recipient_user_id
        or invocation_purpose != AIInvocationPurpose.QUESTION_REPLY.value
        or invocation_source != AIInvocationSource.TELEGRAM.value
        or invocation_status
        not in {
            AIInvocationStatus.SUCCEEDED.value,
            AIInvocationStatus.FAILED.value,
            AIInvocationStatus.AMBIGUOUS.value,
            AIInvocationStatus.CANCELLED.value,
        }
        or invocation_raw_payload_id != raw_payload_id
    ):
        raise QuestionAIOwnershipError(
            "question journal AI provenance is invalid"
        )
    return raw_payload_id, invocation_raw_payload_id


async def _validate_journal_raw_scope(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    invocation_raw_payload_id: int | None,
) -> None:
    """Fail closed if a redacted JSON marker points outside its Telegram graph."""

    row = (
        await session.execute(
            select(
                RawPayload.subject_id,
                RawPayload.actor_user_id,
                RawPayload.file_asset_id,
                RawPayload.domain,
                RawPayload.source,
                RawPayload.processed_at,
                IntegrationConnection.subject_id,
                IntegrationConnection.provider,
                IntegrationConnection.connection_type,
                IntegrationConnection.status,
            )
            .join(
                IntegrationConnection,
                IntegrationConnection.id == RawPayload.integration_connection_id,
            )
            .where(RawPayload.id == raw_payload_id)
        )
    ).one_or_none()
    if row is None:
        raise QuestionAIOwnershipError("question journal raw does not exist")
    (
        raw_subject_id,
        raw_actor_user_id,
        raw_file_asset_id,
        raw_domain,
        raw_source,
        raw_processed_at,
        connection_subject_id,
        connection_provider,
        connection_type,
        connection_status,
    ) = row
    if (
        raw_subject_id != ownership.subject_id
        or raw_actor_user_id != ownership.recipient_user_id
        or raw_file_asset_id is not None
        or raw_domain != Domain.SIGNALS.value
        or raw_source != Source.TELEGRAM.value
        or raw_processed_at is None
        or connection_subject_id != ownership.subject_id
        or connection_provider != IntegrationProvider.TELEGRAM.value
        or connection_type != IntegrationConnectionType.RECIPIENT.value
        or connection_status
        not in {
            IntegrationConnectionStatus.LEGACY.value,
            IntegrationConnectionStatus.ACTIVE.value,
            IntegrationConnectionStatus.DISABLED.value,
            IntegrationConnectionStatus.RETIRED.value,
        }
        or (
            invocation_raw_payload_id is not None
            and invocation_raw_payload_id != raw_payload_id
        )
    ):
        raise QuestionAIOwnershipError("question journal raw provenance is invalid")


def recovered_terminal_result(prepared: PreparedQuestionReply) -> QuestionReplyResult | None:
    """Return a terminal result without pretending a prior PHI answer survived."""

    snapshot = _require_prepared(prepared)
    if snapshot._invocation_id is None or snapshot._reservation_status not in {
        AIInvocationStatus.SUCCEEDED,
        AIInvocationStatus.FAILED,
        AIInvocationStatus.AMBIGUOUS,
        AIInvocationStatus.CANCELLED,
    }:
        return None
    return QuestionReplyResult(
        invocation_id=snapshot._invocation_id, status=snapshot._reservation_status
    )


def delivery_dedupe_key(raw_payload_id: int) -> str:
    """A stable opaque key for every one-off reply/fallback for this raw."""

    if isinstance(raw_payload_id, bool) or not isinstance(raw_payload_id, int) or raw_payload_id < 1:
        raise QuestionAIError("raw_payload_id must be a positive integer")
    return "question-reply:" + hashlib.sha256(
        f"delivery|{raw_payload_id}".encode()
    ).hexdigest()


def _message_identity(raw: RawPayload) -> tuple[str | None, str] | None:
    payload = raw.payload if isinstance(raw.payload, dict) else {}
    message = payload.get("message")
    if not isinstance(message, dict):
        message = payload.get("edited_message")
    if not isinstance(message, dict) or message.get("message_id") is None:
        return None
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    return (str(chat.get("id")) if chat.get("id") is not None else None, str(message["message_id"]))


def _update_sequence(raw: RawPayload) -> int | None:
    payload = raw.payload if isinstance(raw.payload, dict) else {}
    value = payload.get("update_id")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _require_current_logical_message(
    session: AsyncSession,
    *, raw: RawPayload,
    ownership: ProactiveOwnershipContext,
) -> None:
    """Lock and validate all versions of one Telegram message before spending."""

    identity = _message_identity(raw)
    if identity is None:
        return
    # JSON message identity is intentionally parsed in Python so this contract is
    # identical on PostgreSQL and the SQLite fast path. Scope to S: Telegram ids
    # are not a cross-subject authorization key.
    candidates = await session.scalars(
        select(RawPayload).where(
            RawPayload.subject_id == ownership.subject_id,
            RawPayload.domain == Domain.SIGNALS.value,
            RawPayload.source == Source.TELEGRAM.value,
        ).order_by(RawPayload.id).with_for_update().execution_options(populate_existing=True)
    )
    current_sequence = _update_sequence(raw)
    for candidate in candidates:
        if _message_identity(candidate) != identity or candidate.id == raw.id:
            continue
        if (
            candidate.subject_id != ownership.subject_id
            or candidate.actor_user_id != ownership.recipient_user_id
            or candidate.integration_connection_id is None
            or candidate.file_asset_id is not None
        ):
            raise QuestionAIOwnershipError("logical Telegram message has foreign roots")
        candidate_sequence = _update_sequence(candidate)
        if current_sequence is not None and candidate_sequence is not None:
            if candidate_sequence > current_sequence:
                raise QuestionAIStaleError("question raw has a newer Telegram edit")
        elif candidate.id > raw.id:
            raise QuestionAIStaleError("question raw has a newer Telegram edit")


async def raw_is_superseded(
    session: AsyncSession, *, subject_id: uuid.UUID, raw_payload_id: int
) -> bool:
    """Check the immutable Telegram update order before sending an answer."""

    raw = await session.scalar(select(RawPayload).where(RawPayload.id == raw_payload_id))
    if raw is None or raw.subject_id != subject_id:
        raise QuestionAIOwnershipError("question raw does not exist in subject scope")
    identity = _message_identity(raw)
    if identity is None:
        return False
    current_sequence = _update_sequence(raw)
    candidates = await session.scalars(
        select(RawPayload).where(
            RawPayload.subject_id == subject_id,
            RawPayload.domain == Domain.SIGNALS.value,
            RawPayload.source == Source.TELEGRAM.value,
            RawPayload.id != raw.id,
        )
    )
    for candidate in candidates:
        if _message_identity(candidate) != identity:
            continue
        candidate_sequence = _update_sequence(candidate)
        if current_sequence is not None and candidate_sequence is not None:
            if candidate_sequence > current_sequence:
                return True
        elif candidate.id > raw.id:
            return True
    return False


__all__ = [
    "PreparedQuestionReply", "QuestionAIError", "QuestionAIInputError", "QuestionAIOwnershipError",
    "QuestionAIModuleDisabledError", "QuestionAIStateError", "QuestionReplyResult", "invocation_is_journaled",
    "cancel_prepared_question_reply", "delivery_dedupe_key", "delivery_is_journaled", "persist_question_reply", "prepare_live_question_reply",
    "raw_is_superseded",
    "recovered_terminal_result", "render_question_reply", "start_question_dispatch",
]
