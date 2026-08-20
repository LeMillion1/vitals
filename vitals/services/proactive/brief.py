"""The morning brief: the first thing the product ever says on its own.

Assembly is the composer's job (:mod:`compose`); this module owns the platform-
funded model paragraph, its durable invocation lifecycle, and what happens when
there is nothing worth saying.

  * **The model can fail and the brief still arrives.** A missing platform root or
    quota and every sanitized terminal invocation state produce a header-only
    artifact without a second paid attempt for the same product key.
  * **An empty day is silence, not a brief.** No fresh Garmin row and no recovery
    numbers → nothing is sent and a passive ``info`` alert shows the gap in the
    web instead.

The brief is stored in ``weekly_digests`` with ``kind='daily_brief'``, so
/reports shows it and MCP can read the history, without a second table.

Sending is deliberately *not* done here. Production callers use the phased
prepare/commit, start/commit, provider-call, finalize-and-persist flow, then
decide whether to display or deliver the artifact. The old :func:`generate_brief`
wrapper is quarantined for zero-subject injected-client compatibility tests.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, replace
from datetime import date as date_type, timedelta, timezone
from enum import StrEnum
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.config import load_config
from vitals.enums import (
    AIInvocationErrorCode,
    AIInvocationPurpose,
    AIInvocationSource,
    AIInvocationStatus,
    DigestKind,
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    Severity,
    Source,
)
from vitals.i18n import t
from vitals.integrations.llm_client import LLMCallResult, LLMClient
from vitals.models.ai import AIInvocation, AIPlatformQuotaPeriod, AISubjectQuotaPeriod
from vitals.models.identity import HealthSubject
from vitals.models.milestones import DOMAIN as DIGEST_DOMAIN, WeeklyDigest
from vitals.models.system_alert import SystemAlert
from vitals.models.tenancy import IntegrationConnection, PlatformIntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services import ai_gateway_service, alerts_service, digest_service
from vitals.services.identity_service import acquire_identity_governance_lock
from vitals.services.proactive import compose, day_plan
from vitals.utils.timeutils import now_local, now_utc, today_local

logger = logging.getLogger(__name__)

EMPTY_DAY_ALERT_KEY = "brief_empty_day"


class BriefOwnershipError(ValueError):
    """A stored brief would cross its subject, actor, or LLM provenance root."""


class BriefInvocationStateError(BriefOwnershipError):
    """A Daily Brief request cannot safely perform another paid dispatch."""


class BriefSurface(StrEnum):
    """Bounded Daily Brief products with independent idempotency semantics."""

    BUILD = "build"
    TEST = "test"
    SCHEDULER = "scheduler"


class BriefAIFallback(StrEnum):
    """Sanitized reasons why a deterministic header has no AI narrative."""

    NONE = "none"
    NOT_CONFIGURED = "not_configured"
    QUOTA = "quota"
    INPUT_TOO_LARGE = "input_too_large"


@dataclass(frozen=True, slots=True)
class BriefAIAvailability:
    """Redacted owner-scoped projection; reserve/start remain authoritative."""

    available: bool
    code: BriefAIFallback


class PreparedBrief:
    """Opaque PHI-bearing snapshot bound to one Daily Brief product request."""

    __slots__ = (
        "_actor_username",
        "_artifact_source",
        "_base_content",
        "_context_json_text",
        "_dispatchable",
        "_existing_artifact_id",
        "_fallback",
        "_fingerprint",
        "_invocation_id",
        "_invocation_source",
        "_model",
        "_on_date",
        "_owner_user_id",
        "_policy_version",
        "_prompt",
        "_request_key",
        "_reservation_status",
        "_seal",
        "_subject_id",
        "_actor_user_id",
        "_surface",
    )

    def __new__(cls, *args, **kwargs):
        del args, kwargs
        raise BriefOwnershipError("prepared briefs are service-issued only")

    @classmethod
    def _issue(cls, **values) -> "PreparedBrief":
        prepared = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(prepared, name, value)
        object.__setattr__(
            prepared,
            "_fingerprint",
            (
                values["_actor_username"],
                values["_subject_id"],
                values["_actor_user_id"],
                values["_artifact_source"],
                values["_invocation_source"],
                values["_surface"],
                values["_on_date"],
                values["_model"],
                values["_request_key"],
                values["_owner_user_id"],
                values["_policy_version"],
                values["_invocation_id"],
                values["_reservation_status"],
                values["_dispatchable"],
                values["_existing_artifact_id"],
                values["_fallback"],
                hashlib.sha256(values["_context_json_text"].encode()).digest(),
                hashlib.sha256(values["_prompt"].encode()).digest(),
                hashlib.sha256(values["_base_content"].encode()).digest(),
            ),
        )
        object.__setattr__(prepared, "_seal", _PREPARED_BRIEF_SEAL)
        return prepared

    def __setattr__(self, name, value) -> None:
        del name, value
        raise AttributeError("PreparedBrief is immutable")

    def __repr__(self) -> str:
        return (
            f"<PreparedBrief invocation_id={self._invocation_id} "
            f"status={getattr(self._reservation_status, 'value', None)} redacted>"
        )

    def __reduce__(self):
        raise TypeError("PreparedBrief is not pickleable")

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
    def existing_artifact_id(self) -> int | None:
        return self._existing_artifact_id

    @property
    def fallback(self) -> BriefAIFallback:
        return self._fallback

    @property
    def base_content(self) -> str:
        return self._base_content

    @property
    def context(self) -> dict:
        return json.loads(self._context_json_text)


_PREPARED_BRIEF_SEAL = object()


@dataclass(frozen=True, slots=True)
class _PreparedBrief:
    """An exact-one legacy snapshot ready for an out-of-transaction LLM call."""

    on_date: date_type
    source: str
    identity: WriteIdentity | None
    llm_connection_id: uuid.UUID | None
    context: dict


@dataclass(frozen=True, slots=True)
class _RenderedBrief:
    """Network-complete brief payload ready for caller-owned persistence."""

    prepared: _PreparedBrief
    content: str
    model: str | None
    used_llm: bool


async def _require_llm_connection_scope(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    connection_id: uuid.UUID,
) -> None:
    connection = await session.scalar(
        select(IntegrationConnection).where(IntegrationConnection.id == connection_id)
    )
    if connection is None:
        raise BriefOwnershipError("LLM integration connection does not exist")
    if connection.subject_id != identity.subject_id:
        raise BriefOwnershipError("LLM integration connection belongs to another subject")
    if (
        connection.provider != IntegrationProvider.OPENROUTER.value
        or connection.connection_type != IntegrationConnectionType.AI_GATEWAY.value
    ):
        raise BriefOwnershipError("brief generation requires an OpenRouter AI gateway")
    known_statuses = {status.value for status in IntegrationConnectionStatus}
    if connection.status not in known_statuses:
        raise BriefOwnershipError("LLM integration connection has unknown lifecycle state")
    if connection.status not in {
        IntegrationConnectionStatus.LEGACY.value,
        IntegrationConnectionStatus.ACTIVE.value,
    }:
        raise BriefOwnershipError(
            "inactive LLM integration connection cannot generate a brief"
        )

# Short by design: the header already carries every number, so a long tail would
# only restate them. Enough headroom that a reasoning model's thinking tokens
# don't eat the visible answer (the bug that truncated the weekly digest in prod).
_BRIEF_MAX_TOKENS = 2000
_BRIEF_POLICY_VERSION = "daily-brief:v2"
# This namespace is the durable product identity, not a prompt-policy version.
# Never rotate it for a model/template/policy deployment: one surface/date/token
# must continue to resolve to one invocation for its entire lifetime.
_BRIEF_IDEMPOTENCY_NAMESPACE = "daily-brief-product:v1"
_BRIEF_RESERVED_COST_MICROUNITS = 2_000_000
_BRIEF_MAX_INPUT_BYTES = 250_000
_BRIEF_RESERVATION_OVERHEAD_UNITS = 512
_BRIEF_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22,96}$")
_BRIEF_CONTEXT_PROVENANCE_KEY = "_daily_brief_generation"

_ARTIFACT_SOURCE_BY_INVOCATION_SOURCE = {
    AIInvocationSource.WEB: Source.MANUAL.value,
    AIInvocationSource.SCHEDULER: Source.SCHEDULER.value,
}
_TERMINAL_HEADER_STATUSES = frozenset(
    {
        AIInvocationStatus.FAILED,
        AIInvocationStatus.AMBIGUOUS,
        AIInvocationStatus.CANCELLED,
    }
)

# The window his personal norm is averaged over, and the fewest days in it that
# still make an average. Two weeks is long enough to absorb one bad night and
# short enough to follow a cut that is actually moving his resting HR.
_BASELINE_DAYS = 14
_BASELINE_MIN_DAYS = 4

# How long past its scheduled time the job keeps re-checking for last night to
# land before it gives up and sends what there is. The brief fires on the clock,
# but the night it is about ends whenever he wakes up — five hours covers a lie-in
# without letting a watch that never syncs hold the morning hostage forever.
BRIEF_WAIT_HOURS = 5

BRIEF_SYSTEM = """\
Ты пишешь короткий утренний разбор для владельца дашборда здоровья Vitals.

Пользователь — молодой парень, который разбирается в теме (рекомпозиция, силовые,
Garmin). Базовые понятия объяснять не надо.

РОЛЬ: напарник, который шарит. Не врач, не коуч. Прямо, без воды, без паники.

ЗАДАЧА: 2-3 предложения. Что сегодня с организмом и что с этим делать сегодня.
Шапка сообщения уже напечатала и числа, и что сегодня за день (`day`) — пересказ
любого из них тратит одно из трёх предложений на то, что он прочитал строкой выше.
Не «сегодня тренировочный день», а что это значит при сегодняшних числах.
Если данных мало — скажи это одним предложением и не тяни.

`garmin.baseline` — его собственные средние за 14 дней по тем же метрикам.
Это ЕДИНСТВЕННОЕ, с чем можно сравнивать сегодняшние числа. «Просел», «повышен»,
«упал», «пробило восстановление» — только про метрику, у которой baseline есть и
от которой сегодня реально отличается. Метрику без baseline не сравнивай ни с
чем: у неё нет нормы, и «просадка» по ней — выдуманный факт, а не оценка.
Если сегодня всё близко к норме — скажи это прямо одним предложением. Ровный
день — это результат, а не повод сочинить динамику.

Если `garmin.night_pending` = true — Garmin ещё не разметил прошедшую ночь (часы
на спящей руке в момент сборки). Сна, HRV, пульса покоя и Body Battery за сегодня
НЕТ, и вывести их из соседних блоков нельзя. Скажи одним предложением, что ночь
ещё не подгрузилась, и дальше говори только про то, что в данных есть. Про
восстановление, тяжесть тренировки и «организм не отдохнул» в этом случае не
рассуждай вообще — это ровно тот выдуманный факт, который здесь запрещён.

Блок `day` — что за день сегодня (удалёнка, зал). Если его `source` = "template",
это догадка шаблона недели, а не ответ пользователя: учитывай мягко, не утверждай
как факт.

`day.yesterday` — каким вчерашний день оказался по факту, включая нагрузку
(лёгкий/обычный/тяжёлый). Это уже не догадка, а его ответ, и это первое
объяснение сегодняшних цифр: тяжёлый вчера и просевший HRV сегодня — связка,
а не совпадение. Про сегодняшнюю нагрузку данных нет и быть не может — не
выдумывай её.

Блок `signals` — что пользователь сам писал про себя, за вчера и сегодня. kind:
state (состояние, 1-5), symptom (симптом, 1-5), exposure (сделал/принял, at_time —
время). Здесь и лежит объяснение утренних чисел: вчерашний вечерний exposure
(«кофе в 22») — первое, с чем стоит сверить просевший сон или HRV. Это его слова,
а не измерение: одна запись — повод связать, а не поставить диагноз.

ОГРАНИЧЕНИЯ (нарушение = баг):
- Опирайся ТОЛЬКО на JSON. Ничего не выдумывай, новых чисел не вводи.
- Никаких заголовков, списков и разметки — обычный текст, его читают в мессенджере.
- Язык: русский.\
"""


# ── Assembly ──────────────────────────────────────────────────────────────────
async def build_context(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> dict:
    """Today's cross-domain snapshot, minus the protocol, plus the day context.

    The day context is the difference between "спал плохо — отдохни" and advice
    that knows there is a gym session and a heavy workday ahead, so it goes into
    the model's JSON as well as onto the header line.
    """
    ctx = await digest_service.assemble_context(
        session,
        on_date=on_date,
        period_days=1,
        mode=digest_service.REPORT_MODE_BRIEF,
    )
    ctx = compose.strip_protocol(ctx)
    # One-day window would cut the signals in half: "кофе в 22" is *yesterday's*
    # row and this morning's HRV is the thing it explains. Widened here rather
    # than in ``assemble_context`` because nothing else in the brief wants two
    # days — the header is strictly about today.
    #
    # Deliberately after ``strip_protocol``: that keeps the *stored* protocol out of
    # Telegram, and a signal is not stored protocol — it is a sentence he typed
    # into this very chat. Stripping it here would hide his own words from him.
    ctx["signals"] = await _signals_since_yesterday(
        session,
        on_date or today_local(),
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    today = on_date or today_local()
    # The one thing the brief could never do: compare. Handed a single day of
    # absolute numbers and asked what they mean, the model supplied the missing
    # half itself — "просадка SpO2 и повышенный пульс покоя" on a resting HR that
    # had not moved a beat. His own fortnight is what those words have to be true
    # against, so it goes in beside the numbers rather than being left implied.
    if ctx.get("garmin"):
        ctx["garmin"]["baseline"] = await _baseline(session, today)
    answers, answered = await day_plan.resolve(
        session,
        today,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    # Yesterday's answers, and only the ones he actually gave. How heavy a day was
    # is answered in the evening about the day just spent, so at 11:00 the newest
    # real load in the lake is yesterday's — and it is the first thing this
    # morning's HRV should be read against. The template's guess is filtered out:
    # a guess about a day that is already over explains nothing.
    yesterday_answers, yesterday_answered = await day_plan.resolve(
        session,
        today - timedelta(days=1),
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    ctx["day"] = {
        "answers": answers,
        # Which of them are his words rather than the template's guess. Stored as
        # a sorted list because this dict is persisted as JSON — and it is what
        # the brief's buttons read to re-ask only the questions still open.
        "answered": sorted(answered),
        # His answer or the template's guess — the model is told which, so it can
        # hedge on a guess instead of asserting it.
        "source": Source.MANUAL.value if answered else Source.TEMPLATE.value,
        "yesterday": {k: yesterday_answers[k] for k in sorted(yesterday_answered)} or None,
    }
    return ctx


async def _baseline(session: AsyncSession, on_date: date_type) -> Optional[dict]:
    """His own mean per metric over the days *before* today.

    Strictly before: today's number is the thing being judged, and folding it into
    the yardstick pulls the yardstick toward it — worst exactly on the outlier
    mornings the comparison exists for. ``None`` until there is enough history for
    a mean to mean anything; a "norm" off two nights is noise wearing the word.
    """
    from vitals.services import garmin_service

    rows = [
        row
        for row in await garmin_service.list_daily(session, limit=_BASELINE_DAYS + 1)
        if 0 < (on_date - row.date).days <= _BASELINE_DAYS
    ]
    baseline = {}
    for key in compose.BASELINE_KEYS:
        values = [v for v in (getattr(row, key, None) for row in rows) if v is not None]
        if len(values) >= _BASELINE_MIN_DAYS:
            baseline[key] = round(sum(values) / len(values), 1)
    return baseline or None


async def _signals_since_yesterday(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID | None = None,
    include_legacy_unowned: bool = False,
) -> Optional[list]:
    """Today's signals plus yesterday's — the evening before is where exposures
    live, and the metric they explain is this morning's."""
    from vitals.services import signals_service

    rows = await signals_service.list_signals(
        session,
        start=on_date - timedelta(days=1),
        end=on_date,
        subject_id=subject_id,
        include_legacy_unowned=include_legacy_unowned,
    )
    return [digest_service.signal_row(s) for s in reversed(rows)] or None


def build_prompt(ctx: dict) -> str:
    return (
        "Данные за сегодня (JSON):\n\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
        + "\n\nНапиши утренний разбор: 2-3 предложения."
    )


def _as_invocation_source(value: AIInvocationSource | str) -> AIInvocationSource:
    try:
        source = AIInvocationSource(value)
    except (TypeError, ValueError) as exc:
        raise BriefOwnershipError("unsupported Daily Brief invocation source") from exc
    if source not in _ARTIFACT_SOURCE_BY_INVOCATION_SOURCE:
        raise BriefOwnershipError("surface cannot generate a Daily Brief")
    return source


def _as_surface(value: BriefSurface | str) -> BriefSurface:
    try:
        return BriefSurface(value)
    except (TypeError, ValueError) as exc:
        raise BriefOwnershipError("unsupported Daily Brief surface") from exc


def _request_key(
    *,
    source: AIInvocationSource,
    surface: BriefSurface,
    on_date: date_type,
    request_token: str | None,
) -> str:
    if source is AIInvocationSource.SCHEDULER:
        if surface is not BriefSurface.SCHEDULER or request_token is not None:
            raise BriefOwnershipError("scheduled briefs use the deterministic product key")
        token_part = "scheduled"
    else:
        if surface not in {BriefSurface.BUILD, BriefSurface.TEST}:
            raise BriefOwnershipError("web briefs require a manual surface")
        token_part = validate_request_token(request_token)
    material = "|".join(
        (
            _BRIEF_IDEMPOTENCY_NAMESPACE,
            surface.value,
            on_date.isoformat(),
            token_part,
        )
    )
    return f"dbp:v1:{hashlib.sha256(material.encode()).hexdigest()}"


def validate_request_token(request_token: str | None) -> str:
    """Return one bounded opaque web token before any hash/query use."""

    if not isinstance(request_token, str) or not _BRIEF_TOKEN_RE.fullmatch(
        request_token
    ):
        raise BriefOwnershipError("Daily Brief request token is invalid")
    return request_token


def _require_prepared_brief(prepared: PreparedBrief) -> PreparedBrief:
    if not isinstance(prepared, PreparedBrief) or prepared._seal is not _PREPARED_BRIEF_SEAL:
        raise BriefOwnershipError("prepared Daily Brief capability is invalid")
    expected = (
        prepared._actor_username,
        prepared._subject_id,
        prepared._actor_user_id,
        prepared._artifact_source,
        prepared._invocation_source,
        prepared._surface,
        prepared._on_date,
        prepared._model,
        prepared._request_key,
        prepared._owner_user_id,
        prepared._policy_version,
        prepared._invocation_id,
        prepared._reservation_status,
        prepared._dispatchable,
        prepared._existing_artifact_id,
        prepared._fallback,
        hashlib.sha256(prepared._context_json_text.encode()).digest(),
        hashlib.sha256(prepared._prompt.encode()).digest(),
        hashlib.sha256(prepared._base_content.encode()).digest(),
    )
    if prepared._fingerprint != expected:
        raise BriefOwnershipError("prepared Daily Brief capability was modified")
    return prepared


def _resolve_openrouter_credential(credential_ref: str) -> str | None:
    if credential_ref not in ai_gateway_service.ALLOWED_CREDENTIAL_REFS:
        return None
    credential = load_config().openrouter_api_key.strip()
    return credential or None


async def project_ai_availability(
    session: AsyncSession,
    *,
    actor_username: str,
) -> BriefAIAvailability:
    """Project redacted current owner capacity without exposing limits or PHI."""

    owner = await digest_service.prepare_digest_owner(
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
        return BriefAIAvailability(False, BriefAIFallback.NOT_CONFIGURED)
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
                AISubjectQuotaPeriod.subject_id == owner.identity.subject_id,
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
        return BriefAIAvailability(False, BriefAIFallback.NOT_CONFIGURED)
    # This projection cannot know the next PHI-bearing prompt size. It reports
    # root/credential/aligned-period readiness only; reserve is authoritative for
    # the actual conservative per-request capacity check.
    return BriefAIAvailability(True, BriefAIFallback.NONE)


def _render_base_content(ctx: dict) -> str:
    blocks = compose.header_blocks(ctx)
    day = day_plan.day_block(ctx.get("day"))
    if day is not None:
        blocks.append(day)
    return compose.render(blocks)


def _context_with_provenance(
    prepared: PreparedBrief,
    *,
    mode: str,
    status: AIInvocationStatus | None,
) -> dict:
    context = json.loads(prepared._context_json_text)
    context[_BRIEF_CONTEXT_PROVENANCE_KEY] = {
        "policy": prepared._policy_version,
        "surface": prepared._surface.value,
        "request_key": prepared._request_key,
        "model": prepared._model,
        "mode": mode,
        "invocation_status": status.value if status is not None else None,
        "fallback": prepared._fallback.value,
    }
    return context


async def _existing_unfunded_artifact(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    artifact_source: str,
    on_date: date_type,
    request_key: str,
) -> WeeklyDigest | None:
    rows = list(
        await session.scalars(
            select(WeeklyDigest)
            .where(
                WeeklyDigest.subject_id == subject_id,
                WeeklyDigest.actor_user_id.is_(actor_user_id)
                if actor_user_id is None
                else WeeklyDigest.actor_user_id == actor_user_id,
                WeeklyDigest.integration_connection_id.is_(None),
                WeeklyDigest.ai_invocation_id.is_(None),
                WeeklyDigest.date == on_date,
                WeeklyDigest.kind == DigestKind.DAILY_BRIEF.value,
                WeeklyDigest.source == artifact_source,
            )
            .order_by(WeeklyDigest.id)
        )
    )
    matches = [
        row
        for row in rows
        if isinstance(row.context_json, dict)
        and isinstance(row.context_json.get(_BRIEF_CONTEXT_PROVENANCE_KEY), dict)
        and row.context_json[_BRIEF_CONTEXT_PROVENANCE_KEY].get("request_key")
        == request_key
    ]
    if len(matches) > 1:
        raise BriefInvocationStateError("Daily Brief fallback is duplicated")
    return matches[0] if matches else None


async def prepare_brief(
    session: AsyncSession,
    *,
    actor_username: str | None,
    invocation_source: AIInvocationSource | str,
    surface: BriefSurface | str,
    request_token: str | None = None,
    on_date: date_type | None = None,
) -> PreparedBrief | None:
    """Freeze exact-S PHI and reserve one platform-funded narrative call."""

    source = _as_invocation_source(invocation_source)
    product_surface = _as_surface(surface)
    if source is AIInvocationSource.SCHEDULER:
        if actor_username is not None:
            raise BriefOwnershipError("scheduled Daily Brief must be actorless")
    elif actor_username is None:
        raise BriefOwnershipError("web Daily Brief requires its human actor")
    owner = await digest_service.prepare_digest_owner(
        session,
        actor_username=actor_username,
    )
    identity = owner.identity
    owner_user_id = owner.owner_user_id
    artifact_source = _ARTIFACT_SOURCE_BY_INVOCATION_SOURCE[source]
    frozen_date = on_date or today_local()
    config = load_config()
    model = (config.llm_model_brief or config.llm_model_digest).strip()
    if not model or len(model) > 128:
        raise BriefOwnershipError("Daily Brief model is invalid")
    product_key = _request_key(
        source=source,
        surface=product_surface,
        on_date=frozen_date,
        request_token=request_token,
    )
    policy_version = _BRIEF_POLICY_VERSION
    ctx = await build_context(
        session,
        on_date=frozen_date,
        subject_id=identity.subject_id,
        include_legacy_unowned=True,
    )
    if compose.is_empty_day(ctx, on_date=frozen_date):
        logger.info("Daily Brief skipped: empty day")
        return None
    if compose.night_pending(ctx, on_date=frozen_date):
        logger.info("Daily Brief recovery omitted: night is not scored")
        ctx = compose.drop_unscored_night(ctx)
    prompt = build_prompt(ctx)
    prompt_units = len((BRIEF_SYSTEM + "\n" + prompt).encode())
    reserved_units = (
        prompt_units + _BRIEF_MAX_TOKENS + _BRIEF_RESERVATION_OVERHEAD_UNITS
    )
    context_text = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
    base_content = _render_base_content(ctx)

    unfunded = await _existing_unfunded_artifact(
        session,
        subject_id=identity.subject_id,
        actor_user_id=identity.actor_user_id,
        artifact_source=artifact_source,
        on_date=frozen_date,
        request_key=product_key,
    )
    if unfunded is not None:
        return PreparedBrief._issue(
            _actor_username=actor_username,
            _subject_id=identity.subject_id,
            _actor_user_id=identity.actor_user_id,
            _artifact_source=artifact_source,
            _invocation_source=source,
            _surface=product_surface,
            _on_date=frozen_date,
            _model=model,
            _request_key=product_key,
            _owner_user_id=owner_user_id,
            _policy_version=policy_version,
            _invocation_id=None,
            _reservation_status=None,
            _dispatchable=False,
            _existing_artifact_id=unfunded.id,
            _fallback=BriefAIFallback.NOT_CONFIGURED,
            _context_json_text=context_text,
            _prompt=prompt,
            _base_content=base_content,
        )

    invocation = await session.scalar(
        select(AIInvocation)
        .where(
            AIInvocation.subject_id == identity.subject_id,
            AIInvocation.purpose == AIInvocationPurpose.DAILY_BRIEF.value,
            AIInvocation.idempotency_key == product_key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invocation is not None:
        if (
            invocation.actor_user_id != identity.actor_user_id
            or invocation.source != source.value
        ):
            raise BriefInvocationStateError(
                "Daily Brief request provenance is inconsistent"
            )
        status = AIInvocationStatus(invocation.status)
        frozen_model = invocation.model
        artifact_id = await session.scalar(
            select(WeeklyDigest.id).where(
                WeeklyDigest.subject_id == identity.subject_id,
                WeeklyDigest.ai_invocation_id == invocation.id,
            )
        )
        if artifact_id is not None:
            return PreparedBrief._issue(
                _actor_username=actor_username,
                _subject_id=identity.subject_id,
                _actor_user_id=identity.actor_user_id,
                _artifact_source=artifact_source,
                _invocation_source=source,
                _surface=product_surface,
                _on_date=frozen_date,
                _model=frozen_model,
                _request_key=product_key,
                _owner_user_id=owner_user_id,
                _policy_version=policy_version,
                _invocation_id=invocation.id,
                _reservation_status=status,
                _dispatchable=False,
                _existing_artifact_id=artifact_id,
                _fallback=BriefAIFallback.NONE,
                _context_json_text=context_text,
                _prompt=prompt,
                _base_content=base_content,
            )
        if status is AIInvocationStatus.SUCCEEDED:
            raise BriefInvocationStateError(
                "succeeded Daily Brief invocation is missing its artifact"
            )
        if status is AIInvocationStatus.PREPARED:
            created_at = invocation.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            stale = (
                created_at < now_utc() - ai_gateway_service.PREPARED_STALE_AFTER
                or model != frozen_model
            )
            if not stale:
                try:
                    reservation = await ai_gateway_service.reserve_ai_invocation(
                        session,
                        identity=identity,
                        purpose=AIInvocationPurpose.DAILY_BRIEF,
                        source=source,
                        model=frozen_model,
                        idempotency_key=product_key,
                        reserved_cost_microunits=_BRIEF_RESERVED_COST_MICROUNITS,
                        reserved_units=reserved_units,
                    )
                except (
                    ai_gateway_service.AIGatewayConfigurationError,
                    ai_gateway_service.AIIdempotencyConflictError,
                    ai_gateway_service.AIQuotaExceededError,
                ):
                    stale = True
                else:
                    status = reservation.status
            if stale:
                invocation = await ai_gateway_service.cancel_reserved_ai_invocation(
                    session,
                    identity=identity,
                    invocation_id=invocation.id,
                    error_code=AIInvocationErrorCode.CANCELLED_BY_POLICY,
                )
                status = AIInvocationStatus(invocation.status)
        return PreparedBrief._issue(
            _actor_username=actor_username,
            _subject_id=identity.subject_id,
            _actor_user_id=identity.actor_user_id,
            _artifact_source=artifact_source,
            _invocation_source=source,
            _surface=product_surface,
            _on_date=frozen_date,
            _model=frozen_model,
            _request_key=product_key,
            _owner_user_id=owner_user_id,
            _policy_version=policy_version,
            _invocation_id=invocation.id,
            _reservation_status=status,
            _dispatchable=status is AIInvocationStatus.PREPARED,
            _existing_artifact_id=None,
            _fallback=BriefAIFallback.NONE,
            _context_json_text=context_text,
            _prompt=prompt,
            _base_content=base_content,
        )

    fallback = BriefAIFallback.NONE
    if prompt_units > _BRIEF_MAX_INPUT_BYTES:
        fallback = BriefAIFallback.INPUT_TOO_LARGE
        reservation = None
    else:
        try:
            reservation = await ai_gateway_service.reserve_ai_invocation(
                session,
                identity=identity,
                purpose=AIInvocationPurpose.DAILY_BRIEF,
                source=source,
                model=model,
                idempotency_key=product_key,
                reserved_cost_microunits=_BRIEF_RESERVED_COST_MICROUNITS,
                reserved_units=reserved_units,
            )
        except ai_gateway_service.AIQuotaExceededError:
            fallback = BriefAIFallback.QUOTA
            reservation = None
        except ai_gateway_service.AIGatewayConfigurationError:
            fallback = BriefAIFallback.NOT_CONFIGURED
            reservation = None
    return PreparedBrief._issue(
        _actor_username=actor_username,
        _subject_id=identity.subject_id,
        _actor_user_id=identity.actor_user_id,
        _artifact_source=artifact_source,
        _invocation_source=source,
        _surface=product_surface,
        _on_date=frozen_date,
        _model=model,
        _request_key=product_key,
        _owner_user_id=owner_user_id,
        _policy_version=policy_version,
        _invocation_id=reservation.invocation_id if reservation is not None else None,
        _reservation_status=reservation.status if reservation is not None else None,
        _dispatchable=reservation.dispatchable if reservation is not None else False,
        _existing_artifact_id=None,
        _fallback=fallback,
        _context_json_text=context_text,
        _prompt=prompt,
        _base_content=base_content,
    )


async def start_brief_dispatch(
    session: AsyncSession,
    prepared: PreparedBrief,
    *,
    credential_resolver=None,
) -> ai_gateway_service.AIDispatchLease:
    snapshot = _require_prepared_brief(prepared)
    if not snapshot._dispatchable or snapshot._invocation_id is None:
        raise BriefInvocationStateError("Daily Brief is not dispatchable")
    identity = WriteIdentity(
        subject_id=snapshot._subject_id,
        actor_user_id=snapshot._actor_user_id,
    )
    if snapshot._invocation_source is AIInvocationSource.SCHEDULER:
        # The gateway correctly keeps scheduler provenance actorless. Revalidate
        # the frozen owner separately so an owner suspension/rotation between T1
        # and T2 cannot authorize platform spend. This takes canonical
        # governance -> subject -> owner locks before gateway root/quota locks.
        await digest_service.prepare_digest_owner_for_identity(
            session,
            identity=identity,
            owner_user_id=snapshot._owner_user_id,
        )
    return await ai_gateway_service.start_ai_dispatch(
        session,
        identity=identity,
        invocation_id=snapshot._invocation_id,
        credential_resolver=credential_resolver or _resolve_openrouter_credential,
    )


async def render_brief(
    prepared: PreparedBrief,
    lease: ai_gateway_service.AIDispatchLease,
) -> ai_gateway_service.AICompletion[LLMCallResult[str]]:
    """Perform exactly one platform-funded provider call without DB access."""

    snapshot = _require_prepared_brief(prepared)

    async def provider_call(
        request: ai_gateway_service.AIDispatchRequest,
    ) -> LLMCallResult[str]:
        if (
            request.invocation_id != snapshot._invocation_id
            or request.model != snapshot._model
        ):
            raise BriefInvocationStateError("Daily Brief dispatch provenance changed")
        config = replace(load_config(), openrouter_api_key=request.credential)
        return await LLMClient(config).complete_text_with_usage(
            snapshot._prompt,
            model=request.model,
            system=BRIEF_SYSTEM,
            max_tokens=_BRIEF_MAX_TOKENS,
        )

    def usage_extractor(
        result: LLMCallResult[str],
    ) -> ai_gateway_service.SanitizedAIUsage:
        if (
            not isinstance(result, LLMCallResult)
            or not isinstance(result.value, str)
            or not result.value.strip()
            or result.input_tokens is None
            or result.output_tokens is None
            or result.cost_microunits is None
        ):
            raise ValueError("Daily Brief provider usage is incomplete")
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


async def _require_fresh_owner(
    session: AsyncSession,
    prepared: PreparedBrief,
) -> WriteIdentity:
    owner = await digest_service.prepare_digest_owner(
        session,
        actor_username=prepared._actor_username,
    )
    identity = owner.identity
    if (
        identity.subject_id != prepared._subject_id
        or identity.actor_user_id != prepared._actor_user_id
        or owner.owner_user_id != prepared._owner_user_id
    ):
        raise BriefOwnershipError("Daily Brief owner changed")
    return identity


async def _existing_for_prepared(
    session: AsyncSession,
    prepared: PreparedBrief,
) -> WeeklyDigest | None:
    if prepared._invocation_id is not None:
        return await session.scalar(
            select(WeeklyDigest).where(
                WeeklyDigest.subject_id == prepared._subject_id,
                WeeklyDigest.ai_invocation_id == prepared._invocation_id,
            )
        )
    return await _existing_unfunded_artifact(
        session,
        subject_id=prepared._subject_id,
        actor_user_id=prepared._actor_user_id,
        artifact_source=prepared._artifact_source,
        on_date=prepared._on_date,
        request_key=prepared._request_key,
    )


async def _insert_brief_artifact(
    session: AsyncSession,
    prepared: PreparedBrief,
    *,
    invocation: AIInvocation | None,
    narrative: str | None,
) -> WeeklyDigest:
    status = AIInvocationStatus(invocation.status) if invocation is not None else None
    if narrative is not None:
        content = f"{prepared._base_content}\n\n{narrative.strip()}"
        model = prepared._model
        mode = "ai"
    else:
        content = prepared._base_content
        model = None
        mode = "header_only"
    row = WeeklyDigest(
        subject_id=prepared._subject_id,
        actor_user_id=prepared._actor_user_id,
        integration_connection_id=None,
        ai_invocation_id=invocation.id if invocation is not None else None,
        date=prepared._on_date,
        domain=DIGEST_DOMAIN,
        source=prepared._artifact_source,
        kind=DigestKind.DAILY_BRIEF.value,
        content=content,
        context_json=_context_with_provenance(prepared, mode=mode, status=status),
        model=model,
    )
    session.add(row)
    await session.flush()
    return row


async def persist_brief(
    session: AsyncSession,
    prepared: PreparedBrief,
    completion: ai_gateway_service.AICompletion[LLMCallResult[str]] | None,
) -> WeeklyDigest:
    """Finalize accounting and persist one narrative or deterministic header."""

    snapshot = _require_prepared_brief(prepared)
    # A sealed paid completion must always reach terminal accounting, even when
    # the human was suspended or ownership changed during provider I/O. Current
    # authorization still gates T1/T2, non-AI writes, cancellation, and reads.
    if completion is None:
        await _require_fresh_owner(session, snapshot)
    existing = await _existing_for_prepared(session, snapshot)
    if existing is not None:
        return existing
    invocation = None
    narrative = None
    if snapshot._invocation_id is not None:
        if completion is not None:
            if completion.invocation_id != snapshot._invocation_id:
                raise BriefInvocationStateError(
                    "Daily Brief completion belongs to another invocation"
                )
            invocation = await ai_gateway_service.finalize_ai_invocation(
                session,
                completion=completion,
            )
        else:
            invocation = await session.scalar(
                select(AIInvocation)
                .where(AIInvocation.id == snapshot._invocation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if invocation is None or (
            invocation.subject_id != snapshot._subject_id
            or invocation.actor_user_id != snapshot._actor_user_id
            or invocation.purpose != AIInvocationPurpose.DAILY_BRIEF.value
            or invocation.source != snapshot._invocation_source.value
            or invocation.model != snapshot._model
        ):
            raise BriefInvocationStateError("Daily Brief invocation provenance changed")
        status = AIInvocationStatus(invocation.status)
        if status is AIInvocationStatus.SUCCEEDED:
            if completion is None:
                raise BriefInvocationStateError(
                    "succeeded Daily Brief payload is unavailable"
                )
            result = completion.payload
            if (
                not isinstance(result, LLMCallResult)
                or not isinstance(result.value, str)
                or not result.value.strip()
            ):
                raise BriefInvocationStateError(
                    "successful Daily Brief payload is missing"
                )
            narrative = result.value.strip()
        elif status not in _TERMINAL_HEADER_STATUSES:
            raise BriefInvocationStateError(
                "live Daily Brief invocation cannot have an artifact"
            )
    elif completion is not None:
        raise BriefInvocationStateError("unfunded Daily Brief cannot finalize AI")
    return await _insert_brief_artifact(
        session,
        snapshot,
        invocation=invocation,
        narrative=narrative,
    )


async def cancel_and_persist_header_brief(
    session: AsyncSession,
    prepared: PreparedBrief,
) -> WeeklyDigest:
    """Release a zero-network reservation and preserve its header provenance."""

    snapshot = _require_prepared_brief(prepared)
    await _require_fresh_owner(session, snapshot)
    existing = await _existing_for_prepared(session, snapshot)
    if existing is not None:
        return existing
    if snapshot._invocation_id is None:
        return await _insert_brief_artifact(
            session,
            snapshot,
            invocation=None,
            narrative=None,
        )
    invocation = await session.scalar(
        select(AIInvocation)
        .where(AIInvocation.id == snapshot._invocation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if invocation is None:
        raise BriefInvocationStateError("Daily Brief invocation is missing")
    status = AIInvocationStatus(invocation.status)
    if status is AIInvocationStatus.PREPARED:
        invocation = await ai_gateway_service.cancel_reserved_ai_invocation(
            session,
            identity=WriteIdentity(
                subject_id=snapshot._subject_id,
                actor_user_id=snapshot._actor_user_id,
            ),
            invocation_id=snapshot._invocation_id,
            error_code=AIInvocationErrorCode.CANCELLED_BY_POLICY,
        )
    elif status not in _TERMINAL_HEADER_STATUSES:
        raise BriefInvocationStateError(
            "paid or succeeded Daily Brief cannot be cancelled"
        )
    return await _insert_brief_artifact(
        session,
        snapshot,
        invocation=invocation,
        narrative=None,
    )


async def existing_brief_for_prepared(
    session: AsyncSession,
    prepared: PreparedBrief,
) -> WeeklyDigest | None:
    snapshot = _require_prepared_brief(prepared)
    await _require_fresh_owner(session, snapshot)
    return await _existing_for_prepared(session, snapshot)


async def narrative(llm: Any, ctx: dict) -> str:
    """The model's one block. Returns "" on any failure — never raises."""
    try:
        return await llm.complete_text(
            build_prompt(ctx),
            model=getattr(llm, "brief_model", None),
            system=BRIEF_SYSTEM,
            max_tokens=_BRIEF_MAX_TOKENS,
        )
    except Exception:
        logger.warning("brief narrative unavailable (code=provider_error)")
        return ""


async def generate_brief(
    session: AsyncSession,
    llm: Any,
    *,
    on_date: Optional[date_type] = None,
    source: str = Source.MANUAL.value,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    llm_connection_id: uuid.UUID | None = None,
) -> Optional[WeeklyDigest]:
    """Zero-subject injected-client compatibility; never a production AI path."""
    await acquire_identity_governance_lock(session)
    if identity is not None or llm_connection_id is not None:
        raise BriefOwnershipError(
            "identity-bearing Daily Brief generation requires phased gateway APIs"
        )
    if await session.scalar(select(HealthSubject.id).limit(1)) is not None:
        raise BriefOwnershipError(
            "identity-bearing Daily Brief generation requires phased gateway APIs"
        )
    prepared = await _prepare_brief(
        session,
        on_date=on_date,
        source=source,
        identity=None,
        include_legacy_unowned=include_legacy_unowned,
        llm_connection_id=None,
    )
    if prepared is None:
        return None
    rendered = await _render_brief(llm, prepared)
    return await _persist_brief(session, rendered)


async def _prepare_brief(
    session: AsyncSession,
    *,
    on_date: Optional[date_type] = None,
    source: str = Source.MANUAL.value,
    identity: WriteIdentity | None = None,
    include_legacy_unowned: bool = False,
    llm_connection_id: uuid.UUID | None = None,
) -> _PreparedBrief | None:
    """Read and freeze one brief context without calling an external service."""
    if identity is None:
        if llm_connection_id is not None:
            raise BriefOwnershipError(
                "LLM provenance requires an explicit write identity"
            )
    else:
        if not isinstance(identity, WriteIdentity):
            raise BriefOwnershipError("identity must be a WriteIdentity or None")
        if not isinstance(llm_connection_id, uuid.UUID):
            raise BriefOwnershipError(
                "owned brief generation requires an LLM connection UUID"
            )
        await _require_llm_connection_scope(
            session,
            identity=identity,
            connection_id=llm_connection_id,
        )
    on_date = on_date or today_local()
    ctx = await build_context(
        session,
        on_date=on_date,
        subject_id=identity.subject_id if identity is not None else None,
        include_legacy_unowned=include_legacy_unowned,
    )
    if compose.is_empty_day(ctx, on_date=on_date):
        logger.info("no brief for %s: no sleep and nothing new", on_date)
        return None
    # Unconditional, not a flag the caller may forget: whether to *wait* for the
    # night is the job's call, but nobody — job, web button, MCP — gets to build a
    # brief on numbers taken mid-night.
    if compose.night_pending(ctx, on_date=on_date):
        logger.info("brief for %s: last night is not scored, recovery dropped", on_date)
        ctx = compose.drop_unscored_night(ctx)

    return _PreparedBrief(
        on_date=on_date,
        source=source,
        identity=identity,
        llm_connection_id=llm_connection_id,
        context=ctx,
    )


async def _render_brief(llm: Any, prepared: _PreparedBrief) -> _RenderedBrief:
    """Call the model and render text; this function performs no database I/O."""
    if not isinstance(prepared, _PreparedBrief):
        raise BriefOwnershipError("prepared brief must be a _PreparedBrief")

    blocks = compose.header_blocks(prepared.context)
    day = day_plan.day_block(prepared.context.get("day"))
    if day is not None:
        blocks.append(day)
    tail = await narrative(llm, prepared.context)
    if tail:
        blocks.append(compose.Block(compose.KIND_NARRATIVE, tail, 90))

    return _RenderedBrief(
        prepared=prepared,
        content=compose.render(blocks),
        model=getattr(llm, "brief_model", None) if tail else None,
        used_llm=bool(tail),
    )


async def _persist_brief(
    session: AsyncSession,
    rendered: _RenderedBrief,
) -> WeeklyDigest:
    """Persist one rendered payload after revalidating its immutable roots."""
    if not isinstance(rendered, _RenderedBrief):
        raise BriefOwnershipError("rendered brief must be a _RenderedBrief")
    prepared = rendered.prepared
    identity = prepared.identity
    if identity is not None:
        assert prepared.llm_connection_id is not None
        await _require_llm_connection_scope(
            session,
            identity=identity,
            connection_id=prepared.llm_connection_id,
        )

    row = WeeklyDigest(
        subject_id=identity.subject_id if identity is not None else None,
        actor_user_id=identity.actor_user_id if identity is not None else None,
        integration_connection_id=(
            prepared.llm_connection_id if rendered.used_llm else None
        ),
        date=prepared.on_date,
        domain=DIGEST_DOMAIN,
        source=prepared.source,
        kind=DigestKind.DAILY_BRIEF.value,
        content=rendered.content,
        context_json=prepared.context,
        model=rendered.model,
    )
    session.add(row)
    await session.flush()
    return row


async def _reconcile_empty_day_alert(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    empty: bool,
) -> SystemAlert | None:
    """Raise or clear the actorless subject alert for the durable brief outcome."""
    if not isinstance(identity, WriteIdentity) or identity.actor_user_id is not None:
        raise BriefOwnershipError("empty-day alert reconciliation must be actorless")
    context = alerts_service.HealthAlertContext(identity=identity)
    if empty:
        return await alerts_service.raise_scoped_alert(
            session,
            context=context,
            domain=Domain.SYSTEM,
            severity=Severity.INFO,
            message=t("alert.brief_empty_day"),
            alert_key=EMPTY_DAY_ALERT_KEY,
            legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
        )
    return await alerts_service.resolve_scoped_by_key(
        session,
        context=context,
        alert_key=EMPTY_DAY_ALERT_KEY,
        legacy_bridge=alerts_service.LegacyAlertBridge.FULLY_UNOWNED,
    )


def dedupe_key(on_date: date_type) -> str:
    """One brief per day, enforced in the delivery journal rather than hoped for."""
    return f"brief:{on_date.isoformat()}"


def last_attempt_hour(brief_hour: int) -> int:
    """The final hour of the retry window. Clamped to 23 so a late brief time can
    never schedule a fire past midnight — that one would be about the wrong day."""
    return min(brief_hour + BRIEF_WAIT_HOURS, 23)


async def night_scored(session: AsyncSession, on_date: date_type) -> bool:
    """Has Garmin closed last night yet? ``False`` = worth waiting for.

    No row for the day at all counts as *scored*: that is a watch that has not
    synced, or is not used at all, and holding the brief for it would make an
    optional device a hard dependency of the one proactive feature.
    """
    from vitals.services import garmin_service

    row = await garmin_service.get_daily(session, on_date)
    if row is None:
        return True
    return any(getattr(row, key, None) is not None for key in compose.NIGHT_SCORED_KEYS)


async def _run_scheduled_brief_generation(
    session_factory,
    *,
    on_date: date_type,
) -> tuple[WeeklyDigest | None, PreparedBrief | None, str]:
    """Run scheduler T1/T2/provider/T3 with ambiguous-commit reconciliation."""

    prepared = None
    for prepare_try in range(2):
        async with session_factory() as session:
            prepared = await prepare_brief(
                session,
                actor_username=None,
                invocation_source=AIInvocationSource.SCHEDULER,
                surface=BriefSurface.SCHEDULER,
                on_date=on_date,
            )
            try:
                await session.commit()
                break
            except Exception:
                await session.rollback()
                if prepare_try:
                    raise
    if prepared is None:
        return None, None, "empty"

    if prepared.existing_artifact_id is not None:
        async with session_factory() as session:
            row = await existing_brief_for_prepared(session, prepared)
            await session.commit()
        return row, prepared, "existing"
    if not prepared.dispatchable:
        if prepared.reservation_status is AIInvocationStatus.DISPATCHING:
            return None, prepared, "pending"
        async with session_factory() as session:
            row = await persist_brief(session, prepared, None)
            await session.commit()
        return row, prepared, "header"

    lease = None
    for start_try in range(2):
        async with session_factory() as session:
            try:
                lease = await start_brief_dispatch(session, prepared)
            except ai_gateway_service.AIGatewayConfigurationError:
                await session.rollback()
                async with session_factory() as fallback_session:
                    row = await cancel_and_persist_header_brief(
                        fallback_session,
                        prepared,
                    )
                    await fallback_session.commit()
                return row, prepared, "header"
            except ai_gateway_service.AIInvocationStateError:
                await session.rollback()
                lease = None
            else:
                try:
                    await session.commit()
                    break
                except Exception:
                    # Drop the credential-bearing lease on an ambiguous COMMIT.
                    lease = None
                    await session.rollback()
        if lease is None:
            async with session_factory() as recovery_session:
                prepared = await prepare_brief(
                    recovery_session,
                    actor_username=None,
                    invocation_source=AIInvocationSource.SCHEDULER,
                    surface=BriefSurface.SCHEDULER,
                    on_date=on_date,
                )
                await recovery_session.commit()
            if prepared is None:
                return None, None, "empty"
            if prepared.existing_artifact_id is not None:
                async with session_factory() as reload_session:
                    row = await existing_brief_for_prepared(
                        reload_session,
                        prepared,
                    )
                    await reload_session.commit()
                return row, prepared, "existing"
            if not prepared.dispatchable:
                if prepared.reservation_status is AIInvocationStatus.DISPATCHING:
                    return None, prepared, "pending"
                async with session_factory() as header_session:
                    row = await persist_brief(header_session, prepared, None)
                    await header_session.commit()
                return row, prepared, "header"
            if start_try:
                return None, prepared, "pending"
    if lease is None:  # pragma: no cover - all ordinary branches return or assign
        return None, prepared, "pending"

    completion = await render_brief(prepared, lease)
    for persist_try in range(2):
        async with session_factory() as session:
            try:
                row = await persist_brief(session, prepared, completion)
                await session.commit()
                return row, prepared, "ok" if row.model is not None else "header"
            except Exception:
                await session.rollback()
                if persist_try:
                    raise
    raise RuntimeError("scheduled Daily Brief persistence did not resolve")


# ── Scheduler job ─────────────────────────────────────────────────────────────
async def brief_job(session_factory, redis=None) -> None:
    """The 11:00 brief — fired hourly across the wait window, sent once.

    Pulls Garmin first, on its own, instead of hoping the poll schedule happened
    to run this morning — last night's sleep is the whole point of the message.
    A Garmin failure is not a reason to stay quiet: the brief goes out with
    whatever is in the lake.

    11:00 is a guess at when he is up, and one morning it was wrong: the brief
    went out while he was still asleep, read the middle of the night as a wrecked
    recovery and advised skipping the gym over it — then stored that, where the
    weekly digest reads it back as what the morning actually was. So
    the job no longer assumes: with today's row present but the night un-scored it
    sends nothing and lets the next hourly fire look again, up to
    ``BRIEF_WAIT_HOURS``. In practice the brief now lands within the hour of
    waking rather than on the hour of the clock. The last fire gives up and sends
    what there is, minus the numbers the night never produced.
    """
    from vitals.services import garmin_service
    from vitals.services.language_service import get_language
    from vitals.i18n import current_lang
    from vitals.services.proactive import channels, delivery, inbound, prefs

    today = today_local()
    # Before the Garmin pull, not after: on a normal day the brief left at 11:00
    # and every later fire in the window is a no-op that must not cost a login.
    async with session_factory() as session:
        ownership = await channels.resolve_legacy_channel_ownership(
            session,
            actor_username=None,
        )
        already_sent = await delivery.already_sent(
            session,
            dedupe_key(today),
            ownership=ownership,
        )
        brief_hour = None
        if not already_sent:
            brief_hour, _ = prefs.hhmm(
                (await prefs.get_prefs(session))["brief_time"]
            )
        # End all ownership/settings reads before Garmin can touch the network.
        await session.commit()
    if already_sent:
        # The journal is durable evidence that generation succeeded, but alert
        # reconciliation may have failed in a later transaction. Retry that local
        # bookkeeping on every replay without calling Garmin, OpenRouter, or the
        # delivery channel. Keep its governance/S/key locks in a fresh transaction.
        async with session_factory() as session:
            await _reconcile_empty_day_alert(
                session,
                identity=ownership.system_action(),
                empty=False,
            )
            await session.commit()
        return
    assert brief_hour is not None
    out_of_patience = now_local().hour >= last_attempt_hour(brief_hour)

    try:
        await garmin_service.sync_job(session_factory, redis)
    except Exception:
        logger.warning("garmin sync before the brief failed; using stored data", exc_info=True)

    notifier = channels.build_notifier()
    async with session_factory() as session:
        ownership = await channels.resolve_legacy_channel_ownership(
            session,
            actor_username=None,
        )
        current_lang.set(
            await get_language(
                session,
                redis,
                user_id=ownership.recipient_user_id,
            )
        )

        # Second pass at yesterday's unparsed messages, in its own transaction and
        # behind its own guard: a recovered row belongs in the lake before the
        # brief reads it, and a model that is still down must not cost the brief.
        try:
            recovered = await inbound.reparse_pending(
                session,
                ownership=ownership,
            )
            await session.commit()
            if recovered:
                logger.info(
                    "re-parsed %d stored message(s) before the brief",
                    len(recovered),
                )
        except Exception:
            await session.rollback()
            logger.warning("pre-brief signal reparse failed (code=internal_error)")

        # Re-establish the exact channel/language read roots after an optional
        # legacy reparse rollback; platform Daily Brief auth is independent.
        ownership = await channels.resolve_legacy_channel_ownership(
            session,
            actor_username=None,
        )
        current_lang.set(
            await get_language(
                session,
                redis,
                user_id=ownership.recipient_user_id,
            )
        )

        # Nothing is built, so nothing is stored and no model call is spent: an
        # un-scored night is not an empty day either, so it raises no alert — the
        # next fire is an hour away and this is the normal state of a lie-in.
        if not out_of_patience and not await night_scored(session, today):
            logger.info("brief for %s postponed: last night is not scored yet", today)
            await session.commit()
            return

        await session.commit()

    system_identity = ownership.system_action()
    row, prepared, generation_outcome = await _run_scheduled_brief_generation(
        session_factory,
        on_date=today,
    )
    if generation_outcome == "empty":
        async with session_factory() as session:
            await _reconcile_empty_day_alert(
                session,
                identity=system_identity,
                empty=True,
            )
            await session.commit()
        return
    if generation_outcome == "pending" or row is None or prepared is None:
        return

    # Nothing answered for today → the header shows the template's guess and
    # the buttons are how it gets corrected in one tap.
    stored_context = row.context_json if isinstance(row.context_json, dict) else {}
    buttons = day_plan.buttons_from_context(stored_context.get("day"), today)
    # The hint rides on the *sent* message, not on the stored brief: /reports
    # shows the same content with no keyboard under it, and a line pointing at
    # buttons that aren't there is worse than no line at all.
    text = (
        f"{row.content}\n\n{day_plan.HINT_FIX}"
        if buttons
        else row.content
    )

    async with session_factory() as session:
        prepared_delivery = await delivery._prepare_delivery(
            session,
            notifier,
            text=text,
            category=delivery.CATEGORY_BRIEF,
            dedupe_key=dedupe_key(today),
            buttons=buttons,
            ownership=ownership,
        )
        await session.commit()

    if prepared_delivery is not None:
        delivered = await delivery._transmit_prepared_delivery(
            notifier,
            prepared_delivery,
        )
        if delivered is not None:
            async with session_factory() as session:
                await delivery._journal_prepared_delivery(
                    session,
                    prepared_delivery,
                    external_id=delivered.external_id,
                )
                await session.commit()

    # Successful generation clears only this subject's actorless empty-day alert.
    # It is intentionally separate from both durable brief storage and delivery.
    async with session_factory() as session:
        await _reconcile_empty_day_alert(
            session,
            identity=system_identity,
            empty=False,
        )
        await session.commit()
