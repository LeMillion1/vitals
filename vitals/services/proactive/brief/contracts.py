"""Daily Brief immutable capabilities, enums, and policy constants."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date as date_type
from enum import StrEnum

from vitals.enums import AIInvocationSource, AIInvocationStatus, Source
from vitals.ownership import WriteIdentity

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

ОГРАНИЧЕНИЯ (нарушение = баг):
- Опирайся ТОЛЬКО на JSON. Ничего не выдумывай, новых чисел не вводи.
- Никаких заголовков, списков и разметки — обычный текст, его читают в мессенджере.
- Язык: русский.\
"""


# ── Assembly ──────────────────────────────────────────────────────────────────
