"""Platform-funded, raw-first AI parsing for Telegram health signals.

The database phases in this module are deliberately separated from provider I/O:

* ``prepare_*`` authorizes one subject/raw graph and reserves quota;
* ``start_signal_dispatch`` freshly revalidates it and charges one call;
* ``render_signal_parse`` performs exactly one provider await with no DB session;
* ``persist_signal_parse`` atomically finalizes accounting and normalizes the raw.

Opaque capabilities carry PHI only in memory and redact it from ``repr``.  The
platform OpenRouter root pays for the call but never grants access to a subject.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from datetime import date as date_type, datetime, timedelta, timezone
from enum import StrEnum
from typing import Callable

from sqlalchemy import and_, func, or_, select
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
    Severity,
    SignalKind,
    Source,
    UserStatus,
)
from vitals.integrations.llm_client import LLMCallResult, LLMClient
from vitals.i18n import t
from vitals.models.ai import AIInvocation
from vitals.models.identity import HealthSubject, User
from vitals.models.raw_payload import RawPayload
from vitals.models.signals import Signal
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import ai_gateway_service, alerts_service, signals_service
from vitals.services.proactive import delivery
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.proactive.ownership import ProactiveOwnershipContext
from vitals.utils.timeutils import now_local, to_local_naive


class SignalAIError(RuntimeError):
    """Base class for the platform-funded signal parser."""


class SignalAIValidationError(ValueError, SignalAIError):
    """A parser request or response is outside the bounded contract."""


class SignalAIOwnershipError(SignalAIError):
    """A subject, actor, channel, or raw provenance root is inconsistent."""


class SignalAIInvocationStateError(SignalAIError):
    """An invocation history cannot safely obtain or persist another attempt."""


class SignalParseFallback(StrEnum):
    NONE = "none"
    NOT_CONFIGURED = "not_configured"
    QUOTA = "quota"
    INPUT_TOO_LARGE = "input_too_large"
    PENDING = "pending"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    ALREADY_PROCESSED = "already_processed"


@dataclass(frozen=True, slots=True)
class SignalParseResult:
    """Sanitized terminal projection; normalized rows remain repr-hidden."""

    invocation_id: uuid.UUID | None
    status: AIInvocationStatus | None
    processed: bool
    stale: bool
    fallback: SignalParseFallback
    signals: tuple[Signal, ...] = field(default=(), repr=False)


_PREPARED_SIGNAL_SEAL = object()


class PreparedSignalParse:
    """Opaque cross-transaction snapshot for one raw-backed parser attempt."""

    __slots__ = (
        "_actor_user_id",
        "_connection_id",
        "_dispatchable",
        "_fallback",
        "_fingerprint",
        "_include_legacy_unowned",
        "_invocation_id",
        "_invocation_source",
        "_model",
        "_on_date",
        "_owner_user_id",
        "_prompt",
        "_raw_fingerprint",
        "_raw_payload_id",
        "_reservation_status",
        "_seal",
        "_subject_id",
        "_system_prompt",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise SignalAIOwnershipError("prepared signal parses are service-issued only")

    @classmethod
    def _issue(cls, **values) -> "PreparedSignalParse":
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
                values["_connection_id"],
                values["_include_legacy_unowned"],
                values["_invocation_source"],
                values["_on_date"],
                values["_model"],
                values["_raw_payload_id"],
                values["_raw_fingerprint"],
                values["_invocation_id"],
                values["_reservation_status"],
                values["_dispatchable"],
                values["_fallback"],
                hashlib.sha256(values["_system_prompt"].encode()).digest(),
                hashlib.sha256(values["_prompt"].encode()).digest(),
            ),
        )
        object.__setattr__(prepared, "_seal", _PREPARED_SIGNAL_SEAL)
        return prepared

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedSignalParse is immutable")

    def __repr__(self) -> str:
        return (
            f"<PreparedSignalParse invocation_id={self._invocation_id} "
            f"status={getattr(self._reservation_status, 'value', None)} redacted>"
        )

    def __reduce__(self):
        raise TypeError("PreparedSignalParse is not pickleable")

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
    def fallback(self) -> SignalParseFallback:
        return self._fallback

    @property
    def raw_payload_id(self) -> int:
        return self._raw_payload_id


@dataclass(frozen=True, slots=True)
class _LockedSignalScope:
    subject: HealthSubject = field(repr=False)
    owner: User = field(repr=False)
    raw: RawPayload = field(repr=False)
    raw_fingerprint: tuple
    adopt_subject: bool = False


PARSER_SYSTEM = """\
Ты разбираешь короткие сообщения владельца дашборда здоровья на отдельные факты.

Верни JSON вида {"signals": [...]}. Каждый элемент:
- kind: "state" | "symptom" | "exposure"
    state — как человеку и как ему шёл день; имеет интенсивность. Это и самочувствие
      («энергии ноль», «спать хочу»), и то, каким день был по факту:
      «нихуя не делал, весь день за компом» → sedentary,
      «мотался по городу весь день» → on_feet,
      «работал допоздна» → long_work_day, «завал на работе» → workload_high,
      «нервный день» → stress. Про день отвечают именно так — не теряй это.
    symptom — то, что случилось и имеет тяжесть («голова раскалывается», «тошнит»)
    exposure — то, что человек принял или сделал разово («кофе в 22», «выпил два бокала»)
- key: короткий английский слаг (sleepiness, headache, caffeine_late, alcohol,
  sedentary, on_feet, long_work_day, workload_high, stress)
- value_num: для exposure — количество; для state/symptom — сила по шкале ниже.
  Шкалу выводи ИЗ САМИХ СЛОВ, а не из темы. 3 — это «мешает», а не «я не знаю»:
    1 — вскользь, почти незаметно («чуть-чуть клонит в сон»)
    2 — заметно, но не мешает; смягчение («устал немного», «че-то хочу спать»,
        «какая-то апатия» — «какой-то», «немного», «слегка», «чуть» = 2)
    3 — голая констатация без усилителя и без смягчения («болит голова»,
        «поругались», «устал»)
    4 — усилитель («очень», «сильно», «весь день», «еле», «жутко»)
    5 — предел: мат, гипербола, «не могу», «чуть не» («пиздец устал»,
        «раскалывается», «чуть не расплакался», «вырубает»)
  Несколько усилителей подряд не поднимают выше 5 и не опускают ниже 1.
- unit: единица для exposure ("mg", "ml", "min"), иначе null
- at_time: "HH:MM", если время названо или однозначно следует из фразы, иначе null
- note: кусок исходной фразы, из которого взят этот факт

Одно сообщение может дать несколько фактов — верни их все.
События дня (болезнь, поездка, смена протокола) сюда НЕ идут — для них есть
отдельный раздел, пропускай их.
Если фактов нет (болтовня, вопрос, благодарность) — верни {"signals": []}.
Ничего не выдумывай: чего нет в сообщении, того нет в ответе.\
"""

_SIGNAL_POLICY_VERSION = "signal-parse:v1"
_SIGNAL_MAX_ATTEMPTS = 3
_SIGNAL_MAX_TOKENS = 2048
_SIGNAL_MAX_INPUT_BYTES = 32_768
_SIGNAL_MAX_SIGNALS = 32
_SIGNAL_MAX_NOTE_LENGTH = 2_000
_SIGNAL_MAX_KNOWN_KEYS = 40
_SIGNAL_RESERVATION_OVERHEAD_UNITS = 512
_SIGNAL_RESERVED_COST_MICROUNITS = 1_000_000
_PARSER_ALERT_KEY = signals_service.PARSER_FAILED_ALERT_KEY
_LIVE_CONNECTION_STATUSES = frozenset(
    {IntegrationConnectionStatus.LEGACY.value, IntegrationConnectionStatus.ACTIVE.value}
)
_HISTORICAL_CONNECTION_STATUSES = _LIVE_CONNECTION_STATUSES | frozenset(
    {IntegrationConnectionStatus.DISABLED.value, IntegrationConnectionStatus.RETIRED.value}
)
_ALLOWED_ITEM_KEYS = frozenset(
    {"kind", "key", "value_num", "unit", "note", "at_time"}
)
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


def _has_forbidden_control(value: str) -> bool:
    return any(
        unicodedata.category(char).startswith("C")
        for char in value
        if char not in {"\n", "\r", "\t"}
    )


def _attempt_key(raw_payload_id: int, attempt: int) -> str:
    material = f"{_SIGNAL_POLICY_VERSION}|{raw_payload_id}|{attempt}"
    return hashlib.sha256(material.encode()).hexdigest()


def _require_prepared(prepared: PreparedSignalParse) -> PreparedSignalParse:
    if (
        not isinstance(prepared, PreparedSignalParse)
        or prepared._seal is not _PREPARED_SIGNAL_SEAL
        or prepared._fingerprint
        != (
            prepared._subject_id,
            prepared._owner_user_id,
            prepared._actor_user_id,
            prepared._connection_id,
            prepared._include_legacy_unowned,
            prepared._invocation_source,
            prepared._on_date,
            prepared._model,
            prepared._raw_payload_id,
            prepared._raw_fingerprint,
            prepared._invocation_id,
            prepared._reservation_status,
            prepared._dispatchable,
            prepared._fallback,
            hashlib.sha256(prepared._system_prompt.encode()).digest(),
            hashlib.sha256(prepared._prompt.encode()).digest(),
        )
    ):
        raise SignalAIOwnershipError("prepared signal parse is forged or corrupted")
    return prepared


def _payload_hash(payload: object) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError) as exc:
        raise SignalAIOwnershipError("signal raw payload is not canonical JSON") from exc
    return hashlib.sha256(encoded).digest()


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
        raw.fetched_at,
        _payload_hash(raw.payload),
    )


def _raw_text(raw: RawPayload) -> str:
    payload = raw.payload if isinstance(raw.payload, dict) else {}
    message = payload.get("message")
    if not isinstance(message, dict):
        message = payload.get("edited_message")
    if isinstance(message, dict):
        value = message.get("text")
    else:
        value = payload.get("text")
    text = str(value or "").strip()
    if not text:
        raise SignalAIValidationError("signal raw has no parseable text")
    if _has_forbidden_control(text):
        raise SignalAIValidationError("signal raw text contains control characters")
    if len(text.encode("utf-8")) > _SIGNAL_MAX_INPUT_BYTES:
        raise SignalAIValidationError("signal raw text exceeds the parser input limit")
    return text


def _conversation_day(moment: datetime) -> date_type:
    return moment.date() - timedelta(days=1) if moment.hour < 4 else moment.date()


def _raw_day(raw: RawPayload) -> date_type:
    payload = raw.payload if isinstance(raw.payload, dict) else {}
    message = payload.get("message")
    if not isinstance(message, dict):
        message = payload.get("edited_message")
    if isinstance(message, dict):
        value = message.get("date")
        if not isinstance(value, bool):
            try:
                timestamp = float(value)
                if timestamp > 0:
                    local = to_local_naive(
                        datetime.fromtimestamp(timestamp, tz=timezone.utc)
                    )
                    if local is not None:
                        return _conversation_day(local)
            except (TypeError, ValueError, OverflowError, OSError):
                pass
    return _conversation_day(raw.fetched_at)


async def _lock_connection(
    session: AsyncSession,
    *,
    connection_id: uuid.UUID,
    subject_id: uuid.UUID,
    require_live: bool,
) -> IntegrationConnection:
    row = await session.scalar(
        select(IntegrationConnection)
        .where(IntegrationConnection.id == connection_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise SignalAIOwnershipError("Telegram connection does not exist")
    if row.subject_id != subject_id:
        raise SignalAIOwnershipError("Telegram connection belongs to another subject")
    if (
        row.provider != IntegrationProvider.TELEGRAM.value
        or row.connection_type != IntegrationConnectionType.RECIPIENT.value
    ):
        raise SignalAIOwnershipError("signal parsing requires a Telegram recipient")
    known = {status.value for status in IntegrationConnectionStatus}
    if row.status not in known:
        raise SignalAIOwnershipError("Telegram connection lifecycle is unknown")
    allowed = _LIVE_CONNECTION_STATUSES if require_live else _HISTORICAL_CONNECTION_STATUSES
    if row.status not in allowed:
        raise SignalAIOwnershipError("Telegram connection cannot authorize parsing")
    return row


async def _require_exact_one_subject(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> None:
    subject_ids = list(
        await session.scalars(
            select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
        )
    )
    if subject_ids != [subject_id]:
        raise SignalAIOwnershipError(
            "fully-unowned signal raws require the exact-one legacy bridge"
        )


async def _lock_signal_scope(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    live: bool,
    allow_adoption: bool,
    require_active_owner: bool,
) -> _LockedSignalScope:
    """Lock governance -> S -> owner -> Telegram C(s) -> raw."""

    if not isinstance(ownership, ProactiveOwnershipContext):
        raise TypeError("ownership must be a ProactiveOwnershipContext")
    if (
        isinstance(raw_payload_id, bool)
        or not isinstance(raw_payload_id, int)
        or raw_payload_id < 1
    ):
        raise SignalAIValidationError("raw_payload_id must be a positive integer")
    await acquire_identity_governance_lock(session)
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == ownership.subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if subject is None or subject.owner_user_id != ownership.recipient_user_id:
        raise SignalAIOwnershipError("Telegram recipient is not the subject owner")
    owner = await session.scalar(
        select(User)
        .where(User.id == subject.owner_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if owner is None or (require_active_owner and owner.status != UserStatus.ACTIVE.value):
        raise SignalAIOwnershipError("signal parser owner is unavailable")

    # The current recipient root is part of the frozen authorization even when a
    # recovery row retains a retired historical recipient connection.
    projection = (
        await session.execute(
            select(
                RawPayload.subject_id,
                RawPayload.actor_user_id,
                RawPayload.integration_connection_id,
                RawPayload.file_asset_id,
            ).where(RawPayload.id == raw_payload_id)
        )
    ).one_or_none()
    if projection is None:
        raise SignalAIOwnershipError("signal raw does not exist")
    projected_subject, projected_actor, projected_connection, projected_file = projection
    fully_unowned = all(
        value is None
        for value in (
            projected_subject,
            projected_actor,
            projected_connection,
            projected_file,
        )
    )
    connection_ids = {ownership.connection_id}
    if projected_connection is not None:
        connection_ids.add(projected_connection)
    locked_connections: dict[uuid.UUID, IntegrationConnection] = {}
    for connection_id in sorted(connection_ids, key=str):
        locked_connections[connection_id] = await _lock_connection(
            session,
            connection_id=connection_id,
            subject_id=ownership.subject_id,
            require_live=connection_id == ownership.connection_id,
        )

    raw = await session.scalar(
        select(RawPayload)
        .where(RawPayload.id == raw_payload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if raw is None:
        raise SignalAIOwnershipError("signal raw does not exist")
    if raw.file_asset_id is not None:
        raise SignalAIOwnershipError("Telegram signal raw cannot reference a file")
    if raw.domain != Domain.SIGNALS.value or raw.source != Source.TELEGRAM.value:
        raise SignalAIOwnershipError("raw is not Telegram signal provenance")

    current_fully_unowned = all(
        value is None
        for value in (
            raw.subject_id,
            raw.actor_user_id,
            raw.integration_connection_id,
            raw.file_asset_id,
        )
    )
    if current_fully_unowned:
        if (
            not allow_adoption
            or not ownership.include_legacy_unowned
            or not fully_unowned
        ):
            raise SignalAIOwnershipError("fully-unowned signal raw cannot be adopted")
        await _require_exact_one_subject(session, subject_id=ownership.subject_id)
    elif raw.subject_id != ownership.subject_id:
        raise SignalAIOwnershipError("signal raw belongs to another subject")
    if current_fully_unowned:
        pass
    elif raw.actor_user_id is None and raw.integration_connection_id is None:
        if not ownership.include_legacy_unowned:
            raise SignalAIOwnershipError("subject-adopted legacy raw requires the bridge")
        await _require_exact_one_subject(session, subject_id=ownership.subject_id)
    elif raw.actor_user_id is None or raw.integration_connection_id is None:
        raise SignalAIOwnershipError("signal raw has partial actor/connection roots")
    else:
        if raw.actor_user_id != ownership.recipient_user_id:
            raise SignalAIOwnershipError("signal raw actor is not the subject owner")
        if raw.integration_connection_id not in locked_connections:
            raise SignalAIOwnershipError("signal raw connection changed during locking")
        if live and raw.integration_connection_id != ownership.connection_id:
            raise SignalAIOwnershipError("live signal raw uses a stale recipient root")

    return _LockedSignalScope(
        subject=subject,
        owner=owner,
        raw=raw,
        raw_fingerprint=_raw_fingerprint(raw),
        adopt_subject=current_fully_unowned,
    )


def _validate_item(item: object) -> dict:
    if not isinstance(item, dict) or set(item) - _ALLOWED_ITEM_KEYS:
        raise SignalAIValidationError("signal parser item shape is invalid")
    kind = item.get("kind")
    if kind not in {member.value for member in SignalKind}:
        raise SignalAIValidationError("signal parser kind is invalid")
    key = item.get("key")
    if (
        not isinstance(key, str)
        or key != key.strip().lower()
        or len(key) > 64
        or _SLUG_RE.fullmatch(key) is None
    ):
        raise SignalAIValidationError("signal parser key is invalid")
    value = item.get("value_num")
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SignalAIValidationError("signal parser numeric value is invalid")
        value = float(value)
        if not math.isfinite(value) or abs(value) > 1_000_000_000:
            raise SignalAIValidationError("signal parser numeric value is out of range")
        if kind in {SignalKind.STATE.value, SignalKind.SYMPTOM.value} and not 1 <= value <= 5:
            raise SignalAIValidationError("state and symptom intensity must be 1..5")
    unit = item.get("unit")
    if unit is not None and (
        not isinstance(unit, str)
        or not unit.strip()
        or unit != unit.strip()
        or len(unit) > 16
        or _has_forbidden_control(unit)
    ):
        raise SignalAIValidationError("signal parser unit is invalid")
    note = item.get("note")
    if note is not None and (
        not isinstance(note, str)
        or not note.strip()
        or note != note.strip()
        or len(note) > _SIGNAL_MAX_NOTE_LENGTH
        or _has_forbidden_control(note)
    ):
        raise SignalAIValidationError("signal parser note is invalid")
    at_time = item.get("at_time")
    if at_time is not None:
        if not isinstance(at_time, str) or len(at_time) not in {5, 8}:
            raise SignalAIValidationError("signal parser time is invalid")
        try:
            from datetime import time as time_type

            time_type.fromisoformat(at_time)
        except ValueError as exc:
            raise SignalAIValidationError("signal parser time is invalid") from exc
    return {
        "kind": kind,
        "key": key,
        "value_num": value,
        "unit": unit,
        "note": note,
        "at_time": at_time,
    }


def _validated_items(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or set(payload) != {"signals"}:
        raise SignalAIValidationError("signal parser response must contain only signals")
    items = payload.get("signals")
    if not isinstance(items, list) or len(items) > _SIGNAL_MAX_SIGNALS:
        raise SignalAIValidationError("signal parser response has an invalid item count")
    return [_validate_item(item) for item in items]


async def _known_keys(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    include_legacy_unowned: bool,
) -> tuple[str, ...]:
    stats = await signals_service.key_frequency(
        session,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    keys: list[str] = []
    for stat in stats[:_SIGNAL_MAX_KNOWN_KEYS]:
        key = stat.key
        if (
            not isinstance(key, str)
            or len(key) > 64
            or _SLUG_RE.fullmatch(key) is None
        ):
            raise SignalAIValidationError("known signal vocabulary is malformed")
        keys.append(key)
    return tuple(keys)


def _system_prompt(keys: tuple[str, ...]) -> str:
    vocabulary = ", ".join(keys) or "пока пусто"
    prompt = (
        f"{PARSER_SYSTEM}\n\n"
        "Уже использованные ключи — переиспользуй подходящий, новый заводи "
        f"только если ни один не подходит: {vocabulary}"
    )
    if len(prompt.encode("utf-8")) > _SIGNAL_MAX_INPUT_BYTES:
        raise SignalAIValidationError("signal parser system prompt is too large")
    return prompt


@dataclass(frozen=True, slots=True)
class _Attempt:
    number: int
    row: AIInvocation = field(repr=False)


async def _attempts_for_raw(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    raw_payload_id: int,
) -> dict[int, _Attempt]:
    rows = list(
        await session.scalars(
            select(AIInvocation)
            .where(
                AIInvocation.subject_id == subject_id,
                AIInvocation.raw_payload_id == raw_payload_id,
                AIInvocation.purpose == AIInvocationPurpose.SIGNAL_PARSE.value,
            )
            .order_by(AIInvocation.created_at, AIInvocation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    expected = {
        _attempt_key(raw_payload_id, number): number
        for number in range(1, _SIGNAL_MAX_ATTEMPTS + 1)
    }
    result: dict[int, _Attempt] = {}
    for row in rows:
        number = expected.get(row.idempotency_key)
        if number is None or number in result:
            raise SignalAIInvocationStateError("signal parse attempt history is malformed")
        if not (
            (
                row.source == AIInvocationSource.TELEGRAM.value
                and row.actor_user_id == owner_user_id
            )
            or (
                row.source == AIInvocationSource.SCHEDULER.value
                and row.actor_user_id is None
            )
        ):
            raise SignalAIInvocationStateError(
                "signal parse attempt actor/source provenance is invalid"
            )
        result[number] = _Attempt(number=number, row=row)
    return result


async def _linked_signal_count(
    session: AsyncSession,
    *,
    raw_payload_id: int,
) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(Signal).where(Signal.raw_id == raw_payload_id)
        )
        or 0
    )


def _prepared_values(
    *,
    ownership: ProactiveOwnershipContext,
    owner_user_id: uuid.UUID,
    identity: WriteIdentity,
    invocation_source: AIInvocationSource,
    on_date: date_type,
    model: str,
    raw_payload_id: int,
    raw_fingerprint: tuple,
    invocation_id: uuid.UUID | None,
    reservation_status: AIInvocationStatus | None,
    dispatchable: bool,
    fallback: SignalParseFallback,
    system_prompt: str,
    prompt: str,
) -> PreparedSignalParse:
    return PreparedSignalParse._issue(
        _subject_id=identity.subject_id,
        _owner_user_id=owner_user_id,
        _actor_user_id=identity.actor_user_id,
        _connection_id=ownership.connection_id,
        _include_legacy_unowned=ownership.include_legacy_unowned,
        _invocation_source=invocation_source,
        _on_date=on_date,
        _model=model,
        _raw_payload_id=raw_payload_id,
        _raw_fingerprint=raw_fingerprint,
        _invocation_id=invocation_id,
        _reservation_status=reservation_status,
        _dispatchable=dispatchable,
        _fallback=fallback,
        _system_prompt=system_prompt,
        _prompt=prompt,
    )


async def _prepare_signal_parse(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    on_date: date_type | None,
    invocation_source: AIInvocationSource,
    allow_adoption: bool,
) -> PreparedSignalParse:
    live = invocation_source is AIInvocationSource.TELEGRAM
    identity = ownership.owner_action() if live else ownership.system_action()
    locked = await _lock_signal_scope(
        session,
        ownership=ownership,
        raw_payload_id=raw_payload_id,
        live=live,
        allow_adoption=allow_adoption,
        require_active_owner=True,
    )
    raw_date = _raw_day(locked.raw)
    if on_date is not None and (
        not isinstance(on_date, date_type)
        or isinstance(on_date, datetime)
        or on_date != raw_date
    ):
        raise SignalAIValidationError("signal parse date does not match its raw")
    frozen_date = raw_date
    if not isinstance(frozen_date, date_type):
        raise SignalAIValidationError("signal parse date is invalid")
    prompt = _raw_text(locked.raw)
    keys = await _known_keys(
        session,
        subject_id=ownership.subject_id,
        include_legacy_unowned=ownership.include_legacy_unowned,
    )
    system_prompt = _system_prompt(keys)
    total_input_bytes = len((system_prompt + "\n" + prompt).encode("utf-8"))
    config = load_config()
    model = config.llm_model_parser.strip()
    if not model or len(model) > 128 or _has_forbidden_control(model):
        raise SignalAIValidationError("signal parser model is invalid")
    if total_input_bytes > _SIGNAL_MAX_INPUT_BYTES:
        return _prepared_values(
            ownership=ownership,
            owner_user_id=locked.owner.id,
            identity=identity,
            invocation_source=invocation_source,
            on_date=frozen_date,
            model=model,
            raw_payload_id=raw_payload_id,
            raw_fingerprint=locked.raw_fingerprint,
            invocation_id=None,
            reservation_status=None,
            dispatchable=False,
            fallback=SignalParseFallback.INPUT_TOO_LARGE,
            system_prompt=system_prompt,
            prompt=prompt,
        )
    linked = await _linked_signal_count(session, raw_payload_id=raw_payload_id)
    attempts = await _attempts_for_raw(
        session,
        subject_id=ownership.subject_id,
        owner_user_id=locked.owner.id,
        raw_payload_id=raw_payload_id,
    )
    if locked.raw.processed_at is not None:
        succeeded = [
            attempt.row
            for attempt in attempts.values()
            if attempt.row.status == AIInvocationStatus.SUCCEEDED.value
        ]
        if len(succeeded) > 1:
            raise SignalAIInvocationStateError("raw has multiple successful parses")
        return _prepared_values(
            ownership=ownership,
            owner_user_id=locked.owner.id,
            identity=identity,
            invocation_source=invocation_source,
            on_date=frozen_date,
            model=(succeeded[0].model if succeeded else model),
            raw_payload_id=raw_payload_id,
            raw_fingerprint=locked.raw_fingerprint,
            invocation_id=(succeeded[0].id if succeeded else None),
            reservation_status=(AIInvocationStatus.SUCCEEDED if succeeded else None),
            dispatchable=False,
            fallback=SignalParseFallback.ALREADY_PROCESSED,
            system_prompt=system_prompt,
            prompt=prompt,
        )
    if linked:
        raise SignalAIInvocationStateError("pending raw already has normalized signals")

    if locked.adopt_subject:
        # All request/prompt/history validation is now complete. Preserve
        # unknown historical A/C/F and add only the exact-one subject required
        # by the AIInvocation composite raw FK.
        locked.raw.subject_id = ownership.subject_id
        await session.flush()
        locked = replace(
            locked,
            raw_fingerprint=_raw_fingerprint(locked.raw),
            adopt_subject=False,
        )

    reserved_units = total_input_bytes + _SIGNAL_MAX_TOKENS + _SIGNAL_RESERVATION_OVERHEAD_UNITS
    terminal = {
        AIInvocationStatus.FAILED,
        AIInvocationStatus.AMBIGUOUS,
        AIInvocationStatus.CANCELLED,
    }
    last_row: AIInvocation | None = None
    for number in range(1, _SIGNAL_MAX_ATTEMPTS + 1):
        existing = attempts.get(number)
        if existing is not None:
            row = existing.row
            last_row = row
            status = AIInvocationStatus(row.status)
            if status is AIInvocationStatus.SUCCEEDED:
                raise SignalAIInvocationStateError(
                    "successful signal parse left its raw pending"
                )
            if status is AIInvocationStatus.DISPATCHING:
                return _prepared_values(
                    ownership=ownership,
                    owner_user_id=locked.owner.id,
                    identity=identity,
                    invocation_source=invocation_source,
                    on_date=frozen_date,
                    model=row.model,
                    raw_payload_id=raw_payload_id,
                    raw_fingerprint=locked.raw_fingerprint,
                    invocation_id=row.id,
                    reservation_status=status,
                    dispatchable=False,
                    fallback=SignalParseFallback.PENDING,
                    system_prompt=system_prompt,
                    prompt=prompt,
                )
            if status in terminal:
                continue
            if status is AIInvocationStatus.PREPARED and (
                row.actor_user_id != identity.actor_user_id
                or row.source != invocation_source.value
            ):
                # Do not impersonate a human from scheduler recovery (or vice
                # versa). The generic stale-reservation job owns that release.
                return _prepared_values(
                    ownership=ownership,
                    owner_user_id=locked.owner.id,
                    identity=identity,
                    invocation_source=invocation_source,
                    on_date=frozen_date,
                    model=row.model,
                    raw_payload_id=raw_payload_id,
                    raw_fingerprint=locked.raw_fingerprint,
                    invocation_id=row.id,
                    reservation_status=status,
                    dispatchable=False,
                    fallback=SignalParseFallback.PENDING,
                    system_prompt=system_prompt,
                    prompt=prompt,
                )
        key = _attempt_key(raw_payload_id, number)
        try:
            reservation = await ai_gateway_service.reserve_ai_invocation(
                session,
                identity=identity,
                purpose=AIInvocationPurpose.SIGNAL_PARSE,
                source=invocation_source,
                model=model,
                idempotency_key=key,
                reserved_cost_microunits=_SIGNAL_RESERVED_COST_MICROUNITS,
                reserved_units=reserved_units,
                raw_payload_id=raw_payload_id,
            )
        except ai_gateway_service.AIIdempotencyConflictError as exc:
            if existing is None or existing.row.status != AIInvocationStatus.PREPARED.value:
                raise SignalAIInvocationStateError(
                    "signal parse idempotency history changed unexpectedly"
                ) from exc
            cancelled = await ai_gateway_service.cancel_reserved_ai_invocation(
                session,
                identity=identity,
                invocation_id=existing.row.id,
                error_code=AIInvocationErrorCode.CANCELLED_BY_POLICY,
            )
            last_row = cancelled
            continue
        except ai_gateway_service.AIQuotaExceededError:
            return _prepared_values(
                ownership=ownership,
                owner_user_id=locked.owner.id,
                identity=identity,
                invocation_source=invocation_source,
                on_date=frozen_date,
                model=model,
                raw_payload_id=raw_payload_id,
                raw_fingerprint=locked.raw_fingerprint,
                invocation_id=None,
                reservation_status=None,
                dispatchable=False,
                fallback=SignalParseFallback.QUOTA,
                system_prompt=system_prompt,
                prompt=prompt,
            )
        except ai_gateway_service.AIGatewayConfigurationError:
            if existing is not None and existing.row.status == AIInvocationStatus.PREPARED.value:
                await ai_gateway_service.cancel_reserved_ai_invocation(
                    session,
                    identity=identity,
                    invocation_id=existing.row.id,
                    error_code=AIInvocationErrorCode.PROVIDER_UNCONFIGURED,
                )
            return _prepared_values(
                ownership=ownership,
                owner_user_id=locked.owner.id,
                identity=identity,
                invocation_source=invocation_source,
                on_date=frozen_date,
                model=model,
                raw_payload_id=raw_payload_id,
                raw_fingerprint=locked.raw_fingerprint,
                invocation_id=None,
                reservation_status=None,
                dispatchable=False,
                fallback=SignalParseFallback.NOT_CONFIGURED,
                system_prompt=system_prompt,
                prompt=prompt,
            )
        return _prepared_values(
            ownership=ownership,
            owner_user_id=locked.owner.id,
            identity=identity,
            invocation_source=invocation_source,
            on_date=frozen_date,
            model=model,
            raw_payload_id=raw_payload_id,
            raw_fingerprint=locked.raw_fingerprint,
            invocation_id=reservation.invocation_id,
            reservation_status=reservation.status,
            dispatchable=reservation.dispatchable,
            fallback=SignalParseFallback.NONE,
            system_prompt=system_prompt,
            prompt=prompt,
        )

    return _prepared_values(
        ownership=ownership,
        owner_user_id=locked.owner.id,
        identity=identity,
        invocation_source=invocation_source,
        on_date=frozen_date,
        model=(last_row.model if last_row is not None else model),
        raw_payload_id=raw_payload_id,
        raw_fingerprint=locked.raw_fingerprint,
        invocation_id=(last_row.id if last_row is not None else None),
        reservation_status=(
            AIInvocationStatus(last_row.status) if last_row is not None else None
        ),
        dispatchable=False,
        fallback=SignalParseFallback.ATTEMPTS_EXHAUSTED,
        system_prompt=system_prompt,
        prompt=prompt,
    )


async def prepare_live_signal_parse(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
    on_date: date_type,
) -> PreparedSignalParse:
    return await _prepare_signal_parse(
        session,
        ownership=ownership,
        raw_payload_id=raw_payload_id,
        on_date=on_date,
        invocation_source=AIInvocationSource.TELEGRAM,
        allow_adoption=ownership.include_legacy_unowned,
    )


async def prepare_signal_recovery(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    raw_payload_id: int,
) -> PreparedSignalParse:
    return await _prepare_signal_parse(
        session,
        ownership=ownership,
        raw_payload_id=raw_payload_id,
        on_date=None,
        invocation_source=AIInvocationSource.SCHEDULER,
        allow_adoption=ownership.include_legacy_unowned,
    )


def validate_signal_raw_input(raw: RawPayload) -> None:
    """Reject text that can never enter the bounded parser request."""

    if not isinstance(raw, RawPayload):
        raise TypeError("raw must be a RawPayload")
    _raw_text(raw)


def _recovery_raw_scope(ownership: ProactiveOwnershipContext):
    historical_connections = select(IntegrationConnection.id).where(
        IntegrationConnection.subject_id == ownership.subject_id,
        IntegrationConnection.provider == IntegrationProvider.TELEGRAM.value,
        IntegrationConnection.connection_type
        == IntegrationConnectionType.RECIPIENT.value,
        IntegrationConnection.status.in_(_HISTORICAL_CONNECTION_STATUSES),
    )
    exact_owned = and_(
        RawPayload.subject_id == ownership.subject_id,
        RawPayload.actor_user_id == ownership.recipient_user_id,
        RawPayload.integration_connection_id.in_(historical_connections),
        RawPayload.file_asset_id.is_(None),
    )
    if not ownership.include_legacy_unowned:
        return exact_owned
    subject_adopted = and_(
        RawPayload.subject_id == ownership.subject_id,
        RawPayload.actor_user_id.is_(None),
        RawPayload.integration_connection_id.is_(None),
        RawPayload.file_asset_id.is_(None),
    )
    fully_unowned = and_(
        RawPayload.subject_id.is_(None),
        RawPayload.actor_user_id.is_(None),
        RawPayload.integration_connection_id.is_(None),
        RawPayload.file_asset_id.is_(None),
    )
    return or_(exact_owned, subject_adopted, fully_unowned)


def _recovery_terminal_count(subject_id: uuid.UUID):
    return (
        select(func.count(AIInvocation.id))
        .where(
            AIInvocation.subject_id == subject_id,
            AIInvocation.raw_payload_id == RawPayload.id,
            AIInvocation.purpose == AIInvocationPurpose.SIGNAL_PARSE.value,
            AIInvocation.status.in_(
                (
                    AIInvocationStatus.FAILED.value,
                    AIInvocationStatus.AMBIGUOUS.value,
                    AIInvocationStatus.CANCELLED.value,
                )
            ),
        )
        .correlate(RawPayload)
        .scalar_subquery()
    )


def _recovery_nonresumable(subject_id: uuid.UUID):
    return (
        select(AIInvocation.id)
        .where(
            AIInvocation.subject_id == subject_id,
            AIInvocation.raw_payload_id == RawPayload.id,
            AIInvocation.purpose == AIInvocationPurpose.SIGNAL_PARSE.value,
            or_(
                AIInvocation.status == AIInvocationStatus.DISPATCHING.value,
                AIInvocation.status == AIInvocationStatus.SUCCEEDED.value,
                and_(
                    AIInvocation.status == AIInvocationStatus.PREPARED.value,
                    or_(
                        AIInvocation.source
                        != AIInvocationSource.SCHEDULER.value,
                        AIInvocation.actor_user_id.is_not(None),
                    ),
                ),
            ),
        )
        .correlate(RawPayload)
        .exists()
    )


def _recovery_candidate_filters(ownership: ProactiveOwnershipContext) -> tuple:
    return (
        _recovery_raw_scope(ownership),
        RawPayload.domain == Domain.SIGNALS.value,
        RawPayload.source == Source.TELEGRAM.value,
        RawPayload.processed_at.is_(None),
        _recovery_terminal_count(ownership.subject_id) < _SIGNAL_MAX_ATTEMPTS,
        ~_recovery_nonresumable(ownership.subject_id),
    )


async def signal_recovery_high_water_id(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
) -> int | None:
    """Freeze the newest eligible raw visible at one recovery-run boundary."""

    if not isinstance(ownership, ProactiveOwnershipContext):
        raise TypeError("ownership must be a ProactiveOwnershipContext")
    return await session.scalar(
        select(func.max(RawPayload.id)).where(
            *_recovery_candidate_filters(ownership)
        )
    )


async def pending_signal_recovery_ids(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
    limit: int = signals_service.REPARSE_BATCH,
    after_id: int | None = None,
    through_id: int | None = None,
) -> list[int]:
    """Project one keyset page without exhausted head-of-line rows."""

    if not isinstance(ownership, ProactiveOwnershipContext):
        raise TypeError("ownership must be a ProactiveOwnershipContext")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise SignalAIValidationError("recovery limit must be between 1 and 100")
    if after_id is not None and (
        isinstance(after_id, bool)
        or not isinstance(after_id, int)
        or after_id < 1
    ):
        raise SignalAIValidationError("recovery cursor must be a positive integer")
    if through_id is not None and (
        isinstance(through_id, bool)
        or not isinstance(through_id, int)
        or through_id < 1
    ):
        raise SignalAIValidationError("recovery high-water mark must be positive")
    query = select(RawPayload.id).where(*_recovery_candidate_filters(ownership))
    if after_id is not None:
        query = query.where(RawPayload.id > after_id)
    if through_id is not None:
        query = query.where(RawPayload.id <= through_id)
    return list(
        await session.scalars(query.order_by(RawPayload.id).limit(limit))
    )


def _ownership_from_prepared(prepared: PreparedSignalParse) -> ProactiveOwnershipContext:
    return ProactiveOwnershipContext(
        subject_id=prepared._subject_id,
        recipient_user_id=prepared._owner_user_id,
        connection_id=prepared._connection_id,
        include_legacy_unowned=prepared._include_legacy_unowned,
    )


def _identity_from_prepared(prepared: PreparedSignalParse) -> WriteIdentity:
    return WriteIdentity(
        subject_id=prepared._subject_id,
        actor_user_id=prepared._actor_user_id,
    )


def _resolve_openrouter_credential(credential_ref: str) -> str | None:
    if credential_ref not in ai_gateway_service.ALLOWED_CREDENTIAL_REFS:
        return None
    value = load_config().openrouter_api_key.strip()
    return value or None


async def _revalidate_prepared_scope(
    session: AsyncSession,
    prepared: PreparedSignalParse,
    *,
    require_active_owner: bool,
) -> _LockedSignalScope:
    live = prepared._invocation_source is AIInvocationSource.TELEGRAM
    locked = await _lock_signal_scope(
        session,
        ownership=_ownership_from_prepared(prepared),
        raw_payload_id=prepared._raw_payload_id,
        live=live,
        allow_adoption=False,
        require_active_owner=require_active_owner,
    )
    if (
        locked.owner.id != prepared._owner_user_id
        or locked.raw_fingerprint != prepared._raw_fingerprint
        or _raw_text(locked.raw) != prepared._prompt
    ):
        raise SignalAIOwnershipError("prepared signal raw provenance changed")
    return locked


async def start_signal_dispatch(
    session: AsyncSession,
    prepared: PreparedSignalParse,
    *,
    credential_resolver: Callable[[str], str | None] | None = None,
) -> ai_gateway_service.AIDispatchLease:
    """Freshly authorize and charge one raw-backed parser call; caller commits."""

    snapshot = _require_prepared(prepared)
    if not snapshot._dispatchable or snapshot._invocation_id is None:
        raise SignalAIInvocationStateError("signal parse is not dispatchable")
    locked = await _revalidate_prepared_scope(
        session,
        snapshot,
        require_active_owner=True,
    )
    if locked.raw.processed_at is not None:
        raise SignalAIInvocationStateError("signal raw became terminal before dispatch")
    return await ai_gateway_service.start_ai_dispatch(
        session,
        identity=_identity_from_prepared(snapshot),
        invocation_id=snapshot._invocation_id,
        credential_resolver=credential_resolver or _resolve_openrouter_credential,
    )


async def render_signal_parse(
    prepared: PreparedSignalParse,
    lease: ai_gateway_service.AIDispatchLease,
    *,
    llm_factory=None,
) -> ai_gateway_service.AICompletion[LLMCallResult[dict]]:
    """Perform exactly one bounded OpenRouter extraction with no DB access."""

    snapshot = _require_prepared(prepared)
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
            raise SignalAIInvocationStateError("signal dispatch provenance changed")
        config = replace(load_config(), openrouter_api_key=request.credential)
        client = factory(config)
        return await client.extract_json_with_usage(
            snapshot._prompt,
            model=request.model,
            system=snapshot._system_prompt,
            max_tokens=_SIGNAL_MAX_TOKENS,
        )

    def usage_extractor(
        result: LLMCallResult[dict],
    ) -> ai_gateway_service.SanitizedAIUsage:
        if not isinstance(result, LLMCallResult):
            raise SignalAIValidationError("signal provider result is invalid")
        _validated_items(result.value)
        if (
            result.input_tokens is None
            or result.output_tokens is None
            or result.cost_microunits is None
        ):
            raise SignalAIValidationError("signal provider usage is incomplete")
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


async def persist_signal_parse(
    session: AsyncSession,
    prepared: PreparedSignalParse,
    completion: ai_gateway_service.AICompletion[LLMCallResult[dict]],
) -> SignalParseResult:
    """Atomically finalize accounting and consume one raw on valid success."""

    snapshot = _require_prepared(prepared)
    if completion.invocation_id != snapshot._invocation_id:
        raise SignalAIInvocationStateError("signal completion belongs to another call")
    # Lock C before raw. ``finalize_ai_invocation`` then re-enters governance/S/raw
    # and continues root -> quota -> invocation, preserving the global order.
    locked = await _revalidate_prepared_scope(
        session,
        snapshot,
        require_active_owner=False,
    )
    linked_before = await _linked_signal_count(
        session,
        raw_payload_id=snapshot._raw_payload_id,
    )
    if locked.raw.processed_at is None and linked_before:
        raise SignalAIInvocationStateError("pending raw already has normalized facts")
    invocation = await ai_gateway_service.finalize_ai_invocation(
        session,
        completion=completion,
    )
    if (
        invocation.subject_id != snapshot._subject_id
        or invocation.actor_user_id != snapshot._actor_user_id
        or invocation.raw_payload_id != snapshot._raw_payload_id
        or invocation.purpose != AIInvocationPurpose.SIGNAL_PARSE.value
        or invocation.source != snapshot._invocation_source.value
        or invocation.model != snapshot._model
    ):
        raise SignalAIInvocationStateError("signal invocation provenance changed")
    status = AIInvocationStatus(invocation.status)
    if locked.raw.processed_at is not None:
        # A Telegram edit or a concurrently completed exact attempt owns the
        # terminal raw. The paid result is accounted but never duplicates facts.
        return SignalParseResult(
            invocation_id=invocation.id,
            status=status,
            processed=True,
            stale=True,
            fallback=SignalParseFallback.ALREADY_PROCESSED,
        )
    if status is not AIInvocationStatus.SUCCEEDED:
        return SignalParseResult(
            invocation_id=invocation.id,
            status=status,
            processed=False,
            stale=False,
            fallback=SignalParseFallback.NONE,
        )
    payload = completion.payload
    if not isinstance(payload, LLMCallResult):
        raise SignalAIInvocationStateError("successful signal payload is missing")
    items = _validated_items(payload.value)
    rows = await signals_service.create_signals(
        session,
        items=items,
        on_date=snapshot._on_date,
        source=locked.raw.source,
        raw_id=locked.raw.id,
        allow_historical_connection=True,
        allow_subject_adopted_unowned=True,
    )
    if len(rows) != len(items):
        raise SignalAIInvocationStateError("validated signal items were not persisted")
    locked.raw.processed_at = now_local()
    await session.flush()
    return SignalParseResult(
        invocation_id=invocation.id,
        status=status,
        processed=True,
        stale=False,
        fallback=SignalParseFallback.NONE,
        signals=tuple(rows),
    )


async def cancel_prepared_signal_parse(
    session: AsyncSession,
    prepared: PreparedSignalParse,
) -> AIInvocation:
    """Release a zero-network reservation through its exact frozen identity."""

    snapshot = _require_prepared(prepared)
    if snapshot._invocation_id is None:
        raise SignalAIInvocationStateError("signal parse has no reservation")
    await _revalidate_prepared_scope(
        session,
        snapshot,
        require_active_owner=True,
    )
    return await ai_gateway_service.cancel_reserved_ai_invocation(
        session,
        identity=_identity_from_prepared(snapshot),
        invocation_id=snapshot._invocation_id,
        error_code=AIInvocationErrorCode.CANCELLED_BY_POLICY,
    )


def parser_alert_entity_ref(subject_id: uuid.UUID) -> str:
    """Opaque per-subject natural key; never expose a raw/message identifier."""

    if not isinstance(subject_id, uuid.UUID):
        raise TypeError("subject_id must be a UUID")
    digest = hashlib.sha256(subject_id.bytes).hexdigest()[:32]
    return f"subject:{digest}"


def _looks_like_question(text: str) -> bool:
    stripped = text.strip()
    if stripped.endswith("?"):
        return True
    words = stripped.lower().replace(",", " ").split()
    if not words:
        return False
    return words[0] in {
        "почему",
        "что",
        "чем",
        "как",
        "сколько",
        "когда",
        "зачем",
    } or words[:2] == ["стоит", "ли"]


async def _raw_is_signal_candidate(
    session: AsyncSession,
    *,
    raw: RawPayload,
    ownership: ProactiveOwnershipContext,
) -> bool:
    payload = raw.payload if isinstance(raw.payload, dict) else {}
    if isinstance(payload.get("callback_query"), dict):
        return False
    message = payload.get("message")
    if not isinstance(message, dict):
        message = payload.get("edited_message")
    text = _raw_text(raw)
    if text.startswith("/"):
        return False
    reply_id = (
        (message.get("reply_to_message") or {}).get("message_id")
        if isinstance(message, dict)
        else None
    )
    answered = (
        await delivery.find_sent(session, str(reply_id), ownership=ownership)
        if reply_id is not None
        else None
    )
    if answered is not None and answered.category == delivery.CATEGORY_EVENING:
        return True
    return answered is None and not _looks_like_question(text)


async def _eligible_pending_raws(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
) -> tuple[list[RawPayload], bool]:
    # S is already locked by the caller. Project C roots before raw locks.
    # Corrupt roots are never materialized as parser input. They still count as
    # pending alert state, so one bad immutable raw cannot either roll back
    # reconciliation or falsely clear the subject warning.
    alert_scope = RawPayload.subject_id == ownership.subject_id
    if ownership.include_legacy_unowned:
        alert_scope = or_(alert_scope, RawPayload.subject_id.is_(None))
    projections = list(
        await session.execute(
            select(
                RawPayload.id,
                RawPayload.subject_id,
                RawPayload.actor_user_id,
                RawPayload.integration_connection_id,
                RawPayload.file_asset_id,
            ).where(
                RawPayload.domain == Domain.SIGNALS.value,
                RawPayload.source == Source.TELEGRAM.value,
                RawPayload.processed_at.is_(None),
                alert_scope,
            )
        )
    )
    candidate_connections: dict[int, uuid.UUID | None] = {}
    connection_ids: set[uuid.UUID] = set()
    invalid_pending = False
    for raw_id, subject_id, actor_id, connection_id, file_id in projections:
        if subject_id is None:
            if any(value is not None for value in (actor_id, connection_id, file_id)):
                invalid_pending = True
                continue
            if not ownership.include_legacy_unowned:
                invalid_pending = True
                continue
            try:
                await _require_exact_one_subject(
                    session,
                    subject_id=ownership.subject_id,
                )
            except SignalAIOwnershipError:
                invalid_pending = True
                continue
            candidate_connections[raw_id] = None
            continue
        if file_id is not None:
            invalid_pending = True
            continue
        if actor_id is None and connection_id is None:
            if not ownership.include_legacy_unowned:
                invalid_pending = True
                continue
            try:
                await _require_exact_one_subject(
                    session,
                    subject_id=ownership.subject_id,
                )
            except SignalAIOwnershipError:
                invalid_pending = True
                continue
        elif actor_id is None or connection_id is None:
            invalid_pending = True
            continue
        else:
            if actor_id != ownership.recipient_user_id:
                invalid_pending = True
                continue
            connection_ids.add(connection_id)
        candidate_connections[raw_id] = connection_id
    invalid_connections: set[uuid.UUID] = set()
    for connection_id in sorted(connection_ids, key=str):
        try:
            await _lock_connection(
                session,
                connection_id=connection_id,
                subject_id=ownership.subject_id,
                require_live=False,
            )
        except SignalAIOwnershipError:
            invalid_connections.add(connection_id)
            invalid_pending = True
    candidate_ids = [
        raw_id
        for raw_id, connection_id in candidate_connections.items()
        if connection_id not in invalid_connections
    ]
    raws = list(
        await session.scalars(
            select(RawPayload)
            .where(RawPayload.id.in_(candidate_ids))
            .order_by(RawPayload.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ) if candidate_ids else []
    eligible: list[RawPayload] = []
    for raw in raws:
        try:
            is_candidate = await _raw_is_signal_candidate(
                session,
                raw=raw,
                ownership=ownership,
            )
        except SignalAIValidationError:
            # Empty, oversized, or control-character input is not a parser
            # outage. Keep the immutable raw for audit/reparse-policy changes,
            # but do not let it roll back unrelated alert reconciliation.
            invalid_pending = True
            continue
        if is_candidate:
            eligible.append(raw)
    return eligible, invalid_pending


async def _validate_alert_invocation(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    invocation_id: uuid.UUID,
) -> AIInvocation:
    row = await session.scalar(
        select(AIInvocation)
        .where(AIInvocation.id == invocation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        row is None
        or row.subject_id != subject_id
        or row.purpose != AIInvocationPurpose.SIGNAL_PARSE.value
        or row.raw_payload_id is None
        or row.status
        not in {
            AIInvocationStatus.FAILED.value,
            AIInvocationStatus.AMBIGUOUS.value,
        }
    ):
        raise SignalAIInvocationStateError("parser alert invocation is invalid")
    return row


async def _resolve_legacy_parser_alerts(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
) -> None:
    active = list(
        await session.scalars(
            select(SystemAlert)
            .where(
                SystemAlert.alert_key == _PARSER_ALERT_KEY,
                SystemAlert.resolved_at.is_(None),
            )
            .order_by(SystemAlert.id)
        )
    )
    for row in active:
        if row.subject_id is None and row.integration_connection_id is not None:
            raise SignalAIOwnershipError("parser alert has partial legacy roots")
        if row.subject_id is None:
            if not ownership.include_legacy_unowned:
                raise SignalAIOwnershipError("legacy parser alert requires the bridge")
            await alerts_service.resolve_fully_unowned_by_key_preserving_roots(
                session,
                context=alerts_service.HealthAlertContext(
                    identity=ownership.system_action()
                ),
                alert_key=_PARSER_ALERT_KEY,
                entity_ref=row.entity_ref,
            )
            continue
        if row.subject_id != ownership.subject_id:
            continue
        if row.integration_connection_id is None:
            if row.ai_invocation_id is not None:
                await _validate_alert_invocation(
                    session,
                    subject_id=ownership.subject_id,
                    invocation_id=row.ai_invocation_id,
                )
            continue
        connection = await session.scalar(
            select(IntegrationConnection)
            .where(IntegrationConnection.id == row.integration_connection_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            connection is None
            or connection.subject_id != ownership.subject_id
            or connection.provider != IntegrationProvider.OPENROUTER.value
            or connection.connection_type != IntegrationConnectionType.AI_GATEWAY.value
            or connection.status not in _HISTORICAL_CONNECTION_STATUSES
        ):
            raise SignalAIOwnershipError("historical parser alert root is invalid")
        await alerts_service.resolve_scoped_by_key(
            session,
            context=alerts_service.ProviderAlertContext(
                identity=ownership.system_action(),
                provider=IntegrationProvider.OPENROUTER,
                integration_connection_id=connection.id,
            ),
            alert_key=_PARSER_ALERT_KEY,
            entity_ref=row.entity_ref,
        )


async def reconcile_signal_parser_alert(
    session: AsyncSession,
    *,
    ownership: ProactiveOwnershipContext,
) -> SystemAlert | None:
    """Reconcile one scoped parser alert from the actual pending raw backlog."""

    if not isinstance(ownership, ProactiveOwnershipContext):
        raise TypeError("ownership must be a ProactiveOwnershipContext")
    await acquire_identity_governance_lock(session)
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == ownership.subject_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    owner = await session.scalar(
        select(User)
        .where(User.id == ownership.recipient_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        subject is None
        or subject.owner_user_id != ownership.recipient_user_id
        or owner is None
        or owner.status != UserStatus.ACTIVE.value
    ):
        raise SignalAIOwnershipError("parser alert owner scope is unavailable")
    await _lock_connection(
        session,
        connection_id=ownership.connection_id,
        subject_id=ownership.subject_id,
        require_live=True,
    )
    eligible, invalid_pending = await _eligible_pending_raws(
        session,
        ownership=ownership,
    )
    await _resolve_legacy_parser_alerts(session, ownership=ownership)
    context = alerts_service.HealthAlertContext(identity=ownership.system_action())
    entity_ref = parser_alert_entity_ref(ownership.subject_id)
    current = await session.scalar(
        select(SystemAlert)
        .where(
            SystemAlert.subject_id == ownership.subject_id,
            SystemAlert.integration_connection_id.is_(None),
            SystemAlert.alert_key == _PARSER_ALERT_KEY,
            SystemAlert.entity_ref == entity_ref,
            SystemAlert.resolved_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not eligible and not invalid_pending:
        if current is not None:
            await alerts_service.resolve_scoped_by_key(
                session,
                context=context,
                alert_key=_PARSER_ALERT_KEY,
                entity_ref=entity_ref,
            )
        return None

    latest_failed: AIInvocation | None = None
    for raw in eligible:
        rows = list(
            await session.scalars(
                select(AIInvocation)
                .where(
                    AIInvocation.subject_id == ownership.subject_id,
                    AIInvocation.raw_payload_id == raw.id,
                    AIInvocation.purpose == AIInvocationPurpose.SIGNAL_PARSE.value,
                    AIInvocation.status.in_(
                        (
                            AIInvocationStatus.FAILED.value,
                            AIInvocationStatus.AMBIGUOUS.value,
                        )
                    ),
                )
                .order_by(AIInvocation.created_at.desc(), AIInvocation.id.desc())
                .limit(1)
            )
        )
        if rows and (
            latest_failed is None
            or (rows[0].created_at, str(rows[0].id))
            > (latest_failed.created_at, str(latest_failed.id))
        ):
            latest_failed = rows[0]
    desired_invocation_id = latest_failed.id if latest_failed is not None else None
    if current is not None and current.ai_invocation_id != desired_invocation_id:
        await alerts_service.resolve_scoped_by_key(
            session,
            context=context,
            alert_key=_PARSER_ALERT_KEY,
            entity_ref=entity_ref,
        )
        current = None
    if current is None:
        current = await alerts_service.raise_scoped_alert(
            session,
            context=context,
            domain=Domain.SIGNALS,
            severity=Severity.WARN,
            message=t("alert.signal_parser_failed"),
            alert_key=_PARSER_ALERT_KEY,
            entity_ref=entity_ref,
        )
    if desired_invocation_id is not None:
        await _validate_alert_invocation(
            session,
            subject_id=ownership.subject_id,
            invocation_id=desired_invocation_id,
        )
    current.ai_invocation_id = desired_invocation_id
    await session.flush()
    return current


__all__ = [
    "PARSER_SYSTEM",
    "PreparedSignalParse",
    "SignalAIError",
    "SignalAIInvocationStateError",
    "SignalAIOwnershipError",
    "SignalAIValidationError",
    "SignalParseFallback",
    "SignalParseResult",
    "cancel_prepared_signal_parse",
    "parser_alert_entity_ref",
    "persist_signal_parse",
    "prepare_live_signal_parse",
    "prepare_signal_recovery",
    "pending_signal_recovery_ids",
    "render_signal_parse",
    "reconcile_signal_parser_alert",
    "signal_recovery_high_water_id",
    "start_signal_dispatch",
    "validate_signal_raw_input",
]
