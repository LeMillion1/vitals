"""Signals service — capture free text, keep the original, fold keys on read.

Three things this module is responsible for, in the order they matter:

1. **Nothing is lost.** ``ingest_text`` writes the incoming message to
   ``raw_payloads`` *before* it tries to parse it. If the parser throws, times
   out, or returns junk, the raw row is already committed-able — the message is
   still there to re-parse later. This is the same data-lake rule the Garmin/Hevy
   importers follow, just with the LLM as the "upstream API".
2. **One phrase can be several facts.** «Голова раскалывается, спал 4 часа, кофе
   в 22» is three rows. They share a ``batch_id``, which is the unit the "не то"
   button cancels — a model that misread the sentence usually misread all of it,
   and picking one row out of three inside Telegram is misery. Pinpoint edits
   live on the ``/signals`` page instead.
3. **Keys stay free — for now.** The owner's call: collect a month of real
   phrasings, then consolidate. ``KEY_ALIASES`` is what keeps that
   from being a migration: keys are stored as the parser wrote them and folded to
   a canonical name **on read**, so adding an alias silently fixes every chart and
   export at once. Nothing here needs to know the final registry.

``misparse`` rows are excluded from chart/analysis reads but never deleted: they
are the actual evidence the key registry gets built from.
"""
from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass
from datetime import date as date_type, time as time_type, timedelta
from typing import Awaitable, Callable, Optional, Sequence, Union
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.enums import (
    Domain,
    IntegrationConnectionStatus,
    IntegrationConnectionType,
    IntegrationProvider,
    SignalKind,
    Source,
)
from vitals.models.identity import HealthSubject
from vitals.models.raw_payload import RawPayload
from vitals.models.signals import DayContext, Signal
from vitals.models.tenancy import IntegrationConnection
from vitals.ownership import WriteIdentity
from vitals.services.raw_payload_service import (
    upsert_owned_raw_payload,
)
from vitals.utils.timeutils import now_local, today_local

logger = logging.getLogger(__name__)

DOMAIN = Domain.SIGNALS.value

# One unresolved row while the parser is down, not one per message he sends.
PARSER_FAILED_ALERT_KEY = "signal_parser_failed"

_VALID_KINDS = {k.value for k in SignalKind}


class SignalOwnershipError(ValueError):
    """A signal/day-context operation crosses an explicit ownership root."""


class RawPayloadAlreadyProcessedError(RuntimeError):
    """A concurrently superseded/consumed raw must not normalize or reply."""


_LIVE_RECIPIENT_STATUSES = {
    IntegrationConnectionStatus.LEGACY.value,
    IntegrationConnectionStatus.ACTIVE.value,
}
_HISTORICAL_RECIPIENT_STATUSES = _LIVE_RECIPIENT_STATUSES | {
    IntegrationConnectionStatus.DISABLED.value,
    IntegrationConnectionStatus.RETIRED.value,
}


def _owned_health_row_scope(
    model,
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None = None,
):
    """Stage-2 owner/recipient integrity predicate for subject-owned reads."""

    owner_user_id = (
        select(HealthSubject.owner_user_id)
        .where(HealthSubject.id == subject_id)
        .scalar_subquery()
    )
    valid_connection = (
        select(IntegrationConnection.id)
        .where(
            IntegrationConnection.id == model.integration_connection_id,
            IntegrationConnection.subject_id == subject_id,
            IntegrationConnection.connection_type
            == IntegrationConnectionType.RECIPIENT.value,
            IntegrationConnection.status.in_(_HISTORICAL_RECIPIENT_STATUSES),
        )
        .exists()
    )
    scope = and_(
        model.subject_id == subject_id,
        or_(model.actor_user_id.is_(None), model.actor_user_id == owner_user_id),
        or_(model.integration_connection_id.is_(None), valid_connection),
    )
    if integration_connection_id is not None:
        scope = and_(
            scope,
            model.integration_connection_id == integration_connection_id,
        )
    return scope


def _signal_scope(
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None,
):
    return _owned_health_row_scope(
        Signal,
        subject_id=subject_id,
        integration_connection_id=integration_connection_id,
    )


def _raw_scope(
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None,
):
    owned = RawPayload.subject_id == subject_id
    if integration_connection_id is not None:
        owned = and_(
            owned,
            RawPayload.integration_connection_id == integration_connection_id,
        )
    return owned


def _validate_identity(
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID | None,
) -> None:
    if not isinstance(identity, WriteIdentity):
        raise SignalOwnershipError("identity must be a WriteIdentity")
    if integration_connection_id is not None and not isinstance(
        integration_connection_id, uuid.UUID
    ):
        raise SignalOwnershipError(
            "integration_connection_id must be a UUID or None"
        )


async def _require_connection_scope(
    session: AsyncSession,
    *,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID,
    allow_historical: bool = False,
) -> None:
    connection = await session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.id == integration_connection_id
        )
    )
    if connection is None:
        raise SignalOwnershipError("integration connection does not exist")
    if connection.subject_id != identity.subject_id:
        raise SignalOwnershipError("integration connection belongs to another subject")
    if connection.connection_type != IntegrationConnectionType.RECIPIENT.value:
        raise SignalOwnershipError(
            "signals ingestion requires a recipient connection"
        )
    known_statuses = {status.value for status in IntegrationConnectionStatus}
    if connection.status not in known_statuses:
        raise SignalOwnershipError("integration connection has unknown lifecycle state")
    allowed = (
        _HISTORICAL_RECIPIENT_STATUSES
        if allow_historical
        else _LIVE_RECIPIENT_STATUSES
    )
    if connection.status not in allowed:
        operation = "historical provenance" if allow_historical else "write signals"
        raise SignalOwnershipError(
            f"{connection.status} integration connection cannot {operation}"
        )


async def _require_raw_ownership_scope(
    session: AsyncSession,
    *,
    raw: RawPayload,
    allow_subject_adopted_unowned: bool = False,
    allow_historical_null_actor_connection: bool = False,
) -> None:
    """Validate an owned channel raw root before copying its provenance.

    Reparsing is a historical read, so a retired recipient remains a
    valid provenance root.  New ingestion goes through
    :func:`_require_connection_scope` first and therefore cannot attach one.
    """

    if raw.domain != DOMAIN or raw.source != Source.TELEGRAM.value:
        raise SignalOwnershipError(
            "historical channel raw has mismatched domain or source"
        )

    if raw.subject_id is None:
        if any(
            value is not None
            for value in (
                raw.actor_user_id,
                raw.integration_connection_id,
                raw.file_asset_id,
            )
        ):
            raise SignalOwnershipError("raw payload has partial ownership roots")
        return
    if not isinstance(allow_subject_adopted_unowned, bool):
        raise SignalOwnershipError(
            "allow_subject_adopted_unowned must be a boolean"
        )
    if not isinstance(allow_historical_null_actor_connection, bool):
        raise SignalOwnershipError(
            "allow_historical_null_actor_connection must be a boolean"
        )
    if raw.file_asset_id is not None:
        raise SignalOwnershipError(
            "owned channel raw payload cannot reference a file asset"
        )
    owner_user_id = await session.scalar(
        select(HealthSubject.owner_user_id).where(
            HealthSubject.id == raw.subject_id
        )
    )
    if owner_user_id is None:
        raise SignalOwnershipError("raw payload subject does not exist")
    if raw.actor_user_id is None and raw.integration_connection_id is None:
        if allow_subject_adopted_unowned:
            await _require_single_subject_adoption(
                session,
                subject_id=raw.subject_id,
            )
            return
        raise SignalOwnershipError(
            "subject-adopted legacy raw requires an explicit bridge"
        )
    historical_null_actor = (
        raw.actor_user_id is None
        and raw.integration_connection_id is not None
    )
    if historical_null_actor:
        if not allow_historical_null_actor_connection:
            raise SignalOwnershipError(
                "owned channel raw payload actor is not the subject owner"
            )
        await _require_single_subject_adoption(
            session,
            subject_id=raw.subject_id,
        )
    elif raw.actor_user_id != owner_user_id:
        raise SignalOwnershipError(
            "owned channel raw payload actor is not the subject owner"
        )
    if raw.integration_connection_id is None:
        raise SignalOwnershipError(
            "owned channel raw payload has no recipient connection"
        )

    connection = await session.scalar(
        select(IntegrationConnection)
        .where(IntegrationConnection.id == raw.integration_connection_id)
        .execution_options(populate_existing=True)
    )
    if connection is None:
        raise SignalOwnershipError("raw payload connection does not exist")
    if connection.subject_id != raw.subject_id:
        raise SignalOwnershipError(
            "raw payload connection belongs to another subject"
        )
    if (
        historical_null_actor
        and (
            raw.source != Source.TELEGRAM.value
            or connection.provider != IntegrationProvider.TELEGRAM.value
        )
    ):
        raise SignalOwnershipError(
            "historical actorless raw is not Telegram recipient provenance"
        )
    if connection.connection_type != IntegrationConnectionType.RECIPIENT.value:
        raise SignalOwnershipError(
            "raw payload does not reference a recipient connection"
        )
    known_statuses = {status.value for status in IntegrationConnectionStatus}
    if connection.status not in known_statuses:
        raise SignalOwnershipError(
            "raw payload connection has unknown lifecycle state"
        )
    if connection.status not in _HISTORICAL_RECIPIENT_STATUSES:
        raise SignalOwnershipError(
            "raw payload connection is not valid historical provenance"
        )


async def _require_single_subject_adoption(
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
        raise SignalOwnershipError(
            "unowned legacy signal state cannot be adopted after multi-subject activation"
        )

# ── Key aliases ───────────────────────────────────────────────────────────────
# alias (as some parse once wrote it) → canonical key. Applied on **read** only.
# Grow this dict during the shake-out month; once consolidation happens the registry
# gets closed and the parser is forbidden from inventing new keys. Until then the
# stored value is left exactly as written so the drift stays visible.
KEY_ALIASES: dict[str, str] = {
    "sleepy": "sleepiness",
    "sleepy_af": "sleepiness",
    "drowsiness": "sleepiness",
    "head_ache": "headache",
    "headaches": "headache",
    "coffee_late": "caffeine_late",
    "late_coffee": "caffeine_late",
    "energy": "energy_level",
    # How the day went. The parser is told to reach for these five anchors, and
    # these are the spellings it reaches for instead when it forgets.
    "computer_day": "sedentary",
    "desk_day": "sedentary",
    "inactive": "sedentary",
    "low_activity": "sedentary",
    "walking_day": "on_feet",
    "on_the_move": "on_feet",
    "worked_late": "long_work_day",
    "overtime": "long_work_day",
    "busy_day": "workload_high",
    "work_overload": "workload_high",
    "stressful_day": "stress",
}


def normalize_key(key: str) -> str:
    """Canonical form of a stored key. Cheap slug hygiene + the alias table."""
    slug = slug_key(key)
    return KEY_ALIASES.get(slug, slug)


def slug_key(key: str) -> str:
    """Write-side hygiene: lowercase, trimmed, spaces/dashes → underscore.

    Keeps the column tidy without deciding anything about *which* key it is —
    that is ``normalize_key``'s job, and it happens on read.
    """
    return "_".join(str(key).strip().lower().replace("-", " ").split())


def _stored_keys_for(canonical: str) -> set[str]:
    """Every stored spelling that folds to ``canonical`` — for WHERE key IN (…)."""
    canonical = normalize_key(canonical)
    return {canonical} | {a for a, c in KEY_ALIASES.items() if c == canonical}


# ── Capture ───────────────────────────────────────────────────────────────────
Parser = Callable[[str], Union[Sequence[dict], Awaitable[Sequence[dict]]]]
RawTextExtractor = Callable[[RawPayload], Optional[str]]
RawDateExtractor = Callable[[RawPayload], Optional[date_type]]
RawBeforeParse = Callable[[AsyncSession, RawPayload], Awaitable[None]]
RawBeforeNormalize = Callable[[AsyncSession, RawPayload], Awaitable[None]]
RawAfterNormalize = Callable[[AsyncSession, RawPayload], Awaitable[None]]


@dataclass(slots=True)
class ParserOutcome:
    """Mutable batch summary; parsing stays separate from alert bookkeeping."""

    successes: int = 0
    failures: int = 0

    @property
    def attempted(self) -> int:
        return self.successes + self.failures

    def record_success(self) -> None:
        self.successes += 1

    def record_failure(self) -> None:
        self.failures += 1


def _validate_parser_outcome(outcome: ParserOutcome | None) -> None:
    if outcome is not None and not isinstance(outcome, ParserOutcome):
        raise SignalOwnershipError("parser_outcome must be a ParserOutcome or None")


async def _lock_pending_raw_for_normalization(
    session: AsyncSession,
    *,
    raw: RawPayload,
    allow_historical_null_actor_connection: bool = False,
) -> RawPayload:
    """Refresh the terminal marker under a row lock before creating facts."""

    locked = await session.scalar(
        select(RawPayload)
        .where(RawPayload.id == raw.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise SignalOwnershipError("raw payload does not exist")
    await _require_raw_ownership_scope(
        session,
        raw=locked,
        allow_historical_null_actor_connection=(
            allow_historical_null_actor_connection
        ),
    )
    if locked.processed_at is not None:
        raise RawPayloadAlreadyProcessedError(
            "raw payload was already processed or superseded"
        )
    return locked


async def store_raw_text(
    session: AsyncSession,
    *,
    text: str,
    external_id: Optional[str] = None,
    source: str = Source.TELEGRAM.value,
    processed: bool = False,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID | None = None,
) -> RawPayload:
    """Park the incoming message in ``raw_payloads`` before anything can fail.

    ``external_id`` is the channel's own message id when there is one, so a
    webhook retry refreshes the same raw row instead of adding a duplicate.

    ``processed`` is for text that is not a message waiting to become signals —
    a question, a slash command — so the re-parse sweep never hands it to the
    parser. Stamped here rather than by the caller because it has to land in the
    same commit as the row itself.

    Committed here rather than at the end of the request, which is the only
    thing that makes "parked" true: the very next step is a model call of 5-20
    seconds, and Telegram re-sends an update it hasn't been answered about. An
    uncommitted row is invisible to that retry, so it finds no trace of the first
    attempt and pays for a second parse and a second reply to the same message.
    """
    _validate_identity(identity, integration_connection_id)
    subject: HealthSubject | None = None
    if integration_connection_id is None:
        raise SignalOwnershipError(
            "owned channel raw payload requires a recipient connection"
        )
    await _require_connection_scope(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
    )
    payload = {
        "text": text,
        "received_at": now_local().isoformat(timespec="seconds"),
    }
    raw_external_id = external_id or uuid4().hex
    raw = await upsert_owned_raw_payload(
        session,
        identity=identity,
        integration_connection_id=integration_connection_id,
        domain=DOMAIN,
        source=source,
        external_id=raw_external_id,
        payload=payload,
    )
    if processed:
        raw.processed_at = now_local()
    await session.commit()
    return raw


def _coerce_item(item: dict) -> Optional[dict]:
    """Validate one parsed fact. ``None`` = unusable, drop it (raw still has it).

    The parser is an LLM, so this is a trust boundary: a bad ``kind`` or a missing
    ``key`` means the row would be unreadable downstream, and inventing a default
    would quietly poison the charts.
    """
    kind = str(item.get("kind") or "").strip().lower()
    if kind not in _VALID_KINDS:
        return None
    key = slug_key(item.get("key") or "")
    if not key:
        return None

    value_num = item.get("value_num")
    try:
        value_num = float(value_num) if value_num is not None else None
    except (TypeError, ValueError):
        value_num = None

    at_time = item.get("at_time")
    if isinstance(at_time, str):
        try:
            at_time = time_type.fromisoformat(at_time)
        except ValueError:
            at_time = None
    elif not isinstance(at_time, time_type):
        at_time = None

    unit = item.get("unit")
    note = item.get("note")
    return {
        "kind": kind,
        "key": key[:64],
        "value_num": value_num,
        "unit": str(unit)[:16] if unit else None,
        "note": str(note) if note else None,
        "at_time": at_time,
    }


def _parser_items(parsed: object) -> tuple[list[dict], bool]:
    """Return safe items and whether the parser explicitly found no facts.

    An empty sequence is a valid terminal answer. Any other non-sequence shape,
    or a non-empty sequence whose elements are not objects, is untrusted parser
    output and must remain recoverable instead of being mistaken for "no facts".
    Individual dictionaries still go through ``_coerce_item`` below.
    """

    if isinstance(parsed, (str, bytes, bytearray, dict)) or not isinstance(
        parsed, SequenceABC
    ):
        return [], False
    if not parsed:
        return [], True
    return [item for item in parsed if isinstance(item, dict)], False


async def create_signals(
    session: AsyncSession,
    *,
    items: Sequence[dict],
    on_date: Optional[date_type] = None,
    source: str = Source.TELEGRAM.value,
    raw_id: Optional[int] = None,
    batch_id: Optional[str] = None,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID | None = None,
    allow_historical_connection: bool = False,
    allow_subject_adopted_unowned: bool = False,
    allow_historical_null_actor_connection: bool = False,
) -> list[Signal]:
    """Persist a parsed batch. Every row of one message shares a ``batch_id``."""
    _validate_identity(identity, integration_connection_id)
    if not isinstance(allow_historical_connection, bool):
        raise SignalOwnershipError("allow_historical_connection must be a bool")
    if not isinstance(allow_subject_adopted_unowned, bool):
        raise SignalOwnershipError(
            "allow_subject_adopted_unowned must be a bool"
        )
    if not isinstance(allow_historical_null_actor_connection, bool):
        raise SignalOwnershipError(
            "allow_historical_null_actor_connection must be a bool"
        )
    if integration_connection_id is not None:
        await _require_connection_scope(
            session,
            identity=identity,
            integration_connection_id=integration_connection_id,
            allow_historical=allow_historical_connection,
        )
    raw: RawPayload | None = None
    if raw_id is not None:
        raw = await session.scalar(
            select(RawPayload)
            .where(RawPayload.id == raw_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if raw is None:
            raise SignalOwnershipError("raw payload does not exist")
        await _require_raw_ownership_scope(
            session,
            raw=raw,
            allow_subject_adopted_unowned=allow_subject_adopted_unowned,
            allow_historical_null_actor_connection=(
                allow_historical_null_actor_connection
            ),
        )
        if raw.subject_id != identity.subject_id:
            raise SignalOwnershipError("raw payload belongs to another subject")
        if (
            integration_connection_id is not None
            and raw.integration_connection_id != integration_connection_id
        ):
            raise SignalOwnershipError(
                "raw payload belongs to another integration connection"
            )
    on_date = on_date or today_local()
    batch_id = batch_id or uuid4().hex
    rows: list[Signal] = []
    for item in items:
        fields = _coerce_item(item)
        if fields is None:
            continue
        row = Signal(
            subject_id=(
                raw.subject_id if raw is not None else identity.subject_id
            ),
            actor_user_id=(
                raw.actor_user_id if raw is not None else identity.actor_user_id
            ),
            integration_connection_id=(
                raw.integration_connection_id
                if raw is not None
                else integration_connection_id
            ),
            date=on_date,
            domain=DOMAIN,
            source=source,
            raw_id=raw_id,
            batch_id=batch_id,
            **fields,
        )
        session.add(row)
        rows.append(row)
    if rows:
        await session.flush()
    return rows


async def ingest_text(
    session: AsyncSession,
    *,
    text: str,
    parse: Parser,
    external_id: Optional[str] = None,
    on_date: Optional[date_type] = None,
    source: str = Source.TELEGRAM.value,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID | None = None,
    parser_outcome: ParserOutcome | None = None,
) -> list[Signal]:
    """The one entry point for incoming free text.

    Raw first, parse second — deliberately in that order and in *this* function
    rather than at the call site, so no future caller can get the order wrong.
    A parser that raises costs the batch, never the message.
    """
    raw = await store_raw_text(
        session,
        text=text,
        external_id=external_id,
        source=source,
        identity=identity,
        integration_connection_id=integration_connection_id,
    )

    return await ingest_stored_text(
        session,
        raw=raw,
        text=text,
        parse=parse,
        on_date=on_date,
        source=source,
        identity=identity,
        integration_connection_id=integration_connection_id,
        parser_outcome=parser_outcome,
    )


async def ingest_stored_text(
    session: AsyncSession,
    *,
    raw: RawPayload,
    text: str,
    parse: Parser,
    on_date: Optional[date_type] = None,
    source: str = Source.TELEGRAM.value,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID | None = None,
    before_parse: RawBeforeParse | None = None,
    before_normalize: RawBeforeNormalize | None = None,
    parser_outcome: ParserOutcome | None = None,
    allow_historical_null_actor_connection: bool = False,
) -> list[Signal]:
    """Normalize one raw row that was durably claimed by a channel boundary.

    Webhook transports must park their complete upstream envelope before an LLM
    or any outbound call.  This continuation keeps the signals layer transport
    neutral: the caller projects text from that envelope and passes the already
    committed raw row here. A successful parser response, including an explicitly
    empty sequence, consumes the raw row. Exceptions and unusable parser output
    remain pending.
    """

    _validate_parser_outcome(parser_outcome)
    if not isinstance(raw, RawPayload):
        raise SignalOwnershipError("raw must be a RawPayload")
    await _require_raw_ownership_scope(
        session,
        raw=raw,
        allow_historical_null_actor_connection=(
            allow_historical_null_actor_connection
        ),
    )
    if raw.subject_id != identity.subject_id:
        raise SignalOwnershipError("raw payload belongs to another subject")
    if (
        integration_connection_id is not None
        and raw.integration_connection_id != integration_connection_id
    ):
        raise SignalOwnershipError(
            "raw payload belongs to another integration connection"
        )

    if before_parse is not None:
        await before_parse(session, raw)

    parse_error: Exception | None = None
    try:
        parsed = parse(text)
        if inspect.isawaitable(parsed):
            parsed = await parsed
    except Exception as exc:
        parse_error = exc
        logger.warning("signal parser failed; message kept raw", exc_info=True)

    # A Telegram edit can supersede this row while an LLM call is in flight.
    # The channel callback acquires Subject -> connection before this raw lock;
    # the refreshed terminal marker is therefore authoritative, not the stale
    # ORM object that existed before the await above.
    if before_normalize is not None:
        await before_normalize(session, raw)
    raw = await _lock_pending_raw_for_normalization(
        session,
        raw=raw,
        allow_historical_null_actor_connection=(
            allow_historical_null_actor_connection
        ),
    )
    if raw.subject_id != identity.subject_id:
        raise SignalOwnershipError("raw payload belongs to another subject")
    if (
        integration_connection_id is not None
        and raw.integration_connection_id != integration_connection_id
    ):
        raise SignalOwnershipError(
            "raw payload belongs to another integration connection"
        )

    if parse_error is not None:
        # The durable raw stays pending so a later sweep can try again.
        if parser_outcome is not None:
            parser_outcome.record_failure()
        return []

    items, explicitly_empty = _parser_items(parsed)
    rows = await create_signals(
        session,
        items=items,
        on_date=on_date,
        source=source,
        raw_id=raw.id,
        identity=identity,
        integration_connection_id=integration_connection_id,
        allow_historical_connection=True,
        allow_historical_null_actor_connection=(
            allow_historical_null_actor_connection
        ),
    )
    if not rows and not explicitly_empty:
        logger.warning("signal parser returned no usable facts; message kept raw")
        if parser_outcome is not None:
            parser_outcome.record_failure()
        return []

    if parser_outcome is not None:
        parser_outcome.record_success()
    # An empty, successful parse is final too. Leaving it pending would pay for
    # the same model call forever and could starve actionable rows behind it.
    raw.processed_at = now_local()
    await session.flush()
    return rows


# How far back a second attempt is worth making, and how many messages one sweep
# may cost. A phrase still unparseable after a few days is material for the key
# registry, not something to keep paying a model for.
REPARSE_WINDOW_DAYS = 7
REPARSE_BATCH = 20


async def reparse_unparsed(
    session: AsyncSession,
    *,
    parse: Parser,
    limit: int = REPARSE_BATCH,
    since_days: int = REPARSE_WINDOW_DAYS,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None = None,
    text_from_raw: RawTextExtractor | None = None,
    date_from_raw: RawDateExtractor | None = None,
    before_parse: RawBeforeParse | None = None,
    before_normalize: RawBeforeNormalize | None = None,
    after_normalize: RawAfterNormalize | None = None,
    parser_outcome: ParserOutcome | None = None,
    allow_historical_null_actor_connection: bool = False,
) -> list[Signal]:
    """Second pass over messages that never became rows (R3).

    Which is the promise the echo already makes out loud — «Сохранил как есть —
    разобрать не смог. Посмотрю позже» — and that nothing was keeping until now.

    A candidate is a stored message with **no signals of its own**: the model was
    down, timed out, or returned junk. Rows that already produced signals are
    excluded in SQL, so running this twice cannot duplicate anything. A second
    failure leaves the row pending rather than burning it — the model being down
    is not the message's fault — and the window is what stops that from being
    forever.
    """
    _validate_parser_outcome(parser_outcome)
    cutoff = now_local() - timedelta(days=since_days)
    if not isinstance(subject_id, uuid.UUID):
        raise SignalOwnershipError("subject_id must be a UUID")
    if integration_connection_id is not None and not isinstance(
        integration_connection_id, uuid.UUID
    ):
        raise SignalOwnershipError(
            "integration_connection_id must be a UUID or None"
        )

    has_signals = select(Signal.id).where(Signal.raw_id == RawPayload.id).exists()
    stmt = select(RawPayload).where(
        RawPayload.domain == DOMAIN,
        RawPayload.processed_at.is_(None),
        RawPayload.fetched_at >= cutoff,
        ~has_signals,
    )
    stmt = stmt.where(
        _raw_scope(
            subject_id=subject_id,
            integration_connection_id=integration_connection_id,
        )
    )

    def _default_text(raw: RawPayload) -> Optional[str]:
        payload = raw.payload if isinstance(raw.payload, dict) else {}
        return str(payload.get("text") or "") if "text" in payload else None

    extract_text = text_from_raw or _default_text
    made: list[Signal] = []
    attempted = 0
    last_id = 0
    page_size = max(REPARSE_BATCH, min(max(limit, 1), 100))
    dirty = False
    while attempted < limit:
        page = list(
            await session.scalars(
                stmt.where(RawPayload.id > last_id)
                .order_by(RawPayload.id)
                .limit(page_size)
            )
        )
        if not page:
            break
        for raw in page:
            last_id = raw.id
            projected = extract_text(raw)
            if projected is None:
                # Another raw shape (for example a callback) must not consume the
                # text budget. Pagination keeps rows after it reachable.
                continue
            await _require_raw_ownership_scope(
                session,
                raw=raw,
                allow_historical_null_actor_connection=(
                    allow_historical_null_actor_connection
                ),
            )
            text = projected.strip()
            if not text:
                raw.processed_at = now_local()
                dirty = True
                continue
            if before_parse is not None:
                try:
                    await before_parse(session, raw)
                except RawPayloadAlreadyProcessedError:
                    continue
            attempted += 1
            try:
                parsed = parse(text)
                if inspect.isawaitable(parsed):
                    parsed = await parsed
            except Exception:
                logger.warning("re-parse failed for raw %s", raw.id, exc_info=True)
                if parser_outcome is not None:
                    parser_outcome.record_failure()
                if attempted >= limit:
                    break
                continue
            try:
                if before_normalize is not None:
                    await before_normalize(session, raw)
                raw = await _lock_pending_raw_for_normalization(
                    session,
                    raw=raw,
                    allow_historical_null_actor_connection=(
                        allow_historical_null_actor_connection
                    ),
                )
            except RawPayloadAlreadyProcessedError:
                # Another worker or a newer edited update won while the parser
                # was running. It owns the terminal state and normalized rows.
                if after_normalize is not None:
                    await after_normalize(session, raw)
                continue
            items, explicitly_empty = _parser_items(parsed)
            rows = await create_signals(
                session,
                items=items,
                on_date=(date_from_raw(raw) if date_from_raw is not None else None)
                or raw.fetched_at.date(),
                source=raw.source,
                raw_id=raw.id,
                # The replay writes on behalf of the raw's own roots, which
                # _require_raw_ownership_scope has already proved belong here.
                identity=WriteIdentity(subject_id, raw.actor_user_id),
                allow_historical_connection=True,
                allow_subject_adopted_unowned=True,
                allow_historical_null_actor_connection=(
                    allow_historical_null_actor_connection
                ),
            )
            if not rows and not explicitly_empty:
                logger.warning(
                    "re-parser returned no usable facts for raw %s; kept pending",
                    raw.id,
                )
                if parser_outcome is not None:
                    parser_outcome.record_failure()
                if after_normalize is not None:
                    await after_normalize(session, raw)
                if attempted >= limit:
                    break
                continue

            if parser_outcome is not None:
                parser_outcome.record_success()
            raw.processed_at = now_local()
            dirty = True
            made.extend(rows)
            await session.flush()
            if after_normalize is not None:
                await after_normalize(session, raw)
            if attempted >= limit:
                break
        if len(page) < page_size:
            break
    if dirty:
        await session.flush()
    return made


async def delete_signal(
    session: AsyncSession,
    signal_id: int,
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None = None,
) -> bool:
    """Pinpoint removal from the ``/signals`` page — the counterpart of the "не то"
    button, which cancels a whole batch.

    A real delete, unlike ``misparse``: this is for a row that is simply *wrong*
    and shouldn't feed the key registry either. The raw message stays in
    ``raw_payloads`` regardless, so nothing said is ever lost.
    """
    stmt = select(Signal).where(Signal.id == signal_id)
    stmt = stmt.where(
        _signal_scope(
            subject_id=subject_id,
            integration_connection_id=integration_connection_id,
        )
    )
    row = await session.scalar(stmt)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def mark_misparse(
    session: AsyncSession,
    batch_id: str,
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None = None,
) -> int:
    """"Не то" — drop the whole batch out of charts, keep the rows and the raw text."""
    stmt = update(Signal).where(Signal.batch_id == batch_id)
    stmt = stmt.where(
        _signal_scope(
            subject_id=subject_id,
            integration_connection_id=integration_connection_id,
        )
    )
    result = await session.execute(stmt.values(misparse=True))
    await session.flush()
    return result.rowcount or 0


# ── Read ──────────────────────────────────────────────────────────────────────
async def list_signals(
    session: AsyncSession,
    *,
    key: Optional[str] = None,
    kind: Optional[str] = None,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    include_misparse: bool = False,
    limit: int = 200,
    subject_id: uuid.UUID,
) -> list[Signal]:
    """Newest first. ``key`` matches every stored spelling that folds to it."""
    stmt = select(Signal).where(Signal.domain == DOMAIN)
    stmt = stmt.where(
        _signal_scope(
            subject_id=subject_id,
            integration_connection_id=None,
        )
    )
    if not include_misparse:
        stmt = stmt.where(Signal.misparse.is_(False))
    if key is not None:
        stmt = stmt.where(Signal.key.in_(_stored_keys_for(key)))
    if kind is not None:
        stmt = stmt.where(Signal.kind == kind)
    if start is not None:
        stmt = stmt.where(Signal.date >= start)
    if end is not None:
        stmt = stmt.where(Signal.date <= end)
    stmt = stmt.order_by(Signal.date.desc(), Signal.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


@dataclass(frozen=True)
class KeyStat:
    """One canonical key, as the revision screen needs to see it (R1).

    ``count`` alone cannot answer "is this the same thing as that?" — that call is
    made by reading the phrasings the key came from, and by seeing which stored
    spellings already fold into it. So all three travel together.
    """

    key: str
    count: int
    variants: tuple[str, ...]   # stored spellings that fold into ``key``
    examples: tuple[str, ...]   # his own wording, newest first


EXAMPLES_PER_KEY = 3

# Everything folds in Python (aliases can't be expressed in a GROUP BY), so the
# scan is bounded rather than unbounded. A year of one person's messages is well
# under this.
# ponytail: full scan of the recent window, per-key SQL aggregates if it ever grows.
_SCAN_LIMIT = 3000


async def key_frequency(
    session: AsyncSession,
    *,
    start: Optional[date_type] = None,
    end: Optional[date_type] = None,
    include_misparse: bool = True,
    subject_id: uuid.UUID,
) -> list[KeyStat]:
    """Canonical keys, most-used first — the raw material for consolidating them.

    Counts default to **including** misparses: the point of this list is to see
    what the parser actually emits, mistakes included.
    """
    stmt = select(Signal.key, Signal.note).where(Signal.domain == DOMAIN)
    stmt = stmt.where(
        _signal_scope(
            subject_id=subject_id,
            integration_connection_id=None,
        )
    )
    if not include_misparse:
        stmt = stmt.where(Signal.misparse.is_(False))
    if start is not None:
        stmt = stmt.where(Signal.date >= start)
    if end is not None:
        stmt = stmt.where(Signal.date <= end)
    stmt = stmt.order_by(Signal.id.desc()).limit(_SCAN_LIMIT)

    counts: dict[str, int] = {}
    variants: dict[str, set[str]] = {}
    examples: dict[str, list[str]] = {}
    for stored_key, note in (await session.execute(stmt)).all():
        canonical = normalize_key(stored_key)
        counts[canonical] = counts.get(canonical, 0) + 1
        if stored_key != canonical:
            variants.setdefault(canonical, set()).add(stored_key)
        seen = examples.setdefault(canonical, [])
        if note and note not in seen and len(seen) < EXAMPLES_PER_KEY:
            seen.append(note)

    return sorted(
        (
            KeyStat(
                key=key,
                count=count,
                variants=tuple(sorted(variants.get(key, ()))),
                examples=tuple(examples.get(key, ())),
            )
            for key, count in counts.items()
        ),
        key=lambda s: (-s.count, s.key),
    )


# ── Day context ───────────────────────────────────────────────────────────────
async def set_day_context(
    session: AsyncSession,
    on_date: date_type,
    *,
    answers: dict,
    planned: Optional[dict] = None,
    source: str = Source.MANUAL.value,
    identity: WriteIdentity,
    integration_connection_id: uuid.UUID | None = None,
    merge_answers: bool = False,
    preserve_source: bool = False,
    planned_if_missing: bool = False,
    allow_historical_connection: bool = False,
) -> DayContext:
    """Upsert the day's context — the latest answer wins, nothing is versioned.

    ``planned`` (what the week template had guessed) is kept alongside so the
    template can later learn from where it was wrong; passing ``None`` leaves any
    existing guess in place rather than erasing it.
    """
    _validate_identity(identity, integration_connection_id)
    for flag_name, flag in (
        ("merge_answers", merge_answers),
        ("preserve_source", preserve_source),
        ("planned_if_missing", planned_if_missing),
        ("allow_historical_connection", allow_historical_connection),
    ):
        if not isinstance(flag, bool):
            raise SignalOwnershipError(f"{flag_name} must be a bool")
    # Serialize the whole read/merge/write operation on the durable subject
    # root.  Locking only the DayContext row cannot protect the first two
    # concurrent answers for a date where the row does not exist yet.
    subject = await session.scalar(
        select(HealthSubject)
        .where(HealthSubject.id == identity.subject_id)
        .with_for_update()
    )
    if subject is None:
        raise SignalOwnershipError("day context subject does not exist")
    if integration_connection_id is not None:
        await _require_connection_scope(
            session,
            identity=identity,
            integration_connection_id=integration_connection_id,
            allow_historical=allow_historical_connection,
        )

    # One answered day per person, and the subject is the whole lookup: a row
    # that belongs to nobody is nobody's day to answer, so it is not found and
    # not claimed.
    scoped = select(DayContext).where(
        DayContext.date == on_date,
        DayContext.subject_id == identity.subject_id,
    )
    row = await session.scalar(scoped.with_for_update())
    if row is not None:
        if (
            row.actor_user_id is not None
            and row.actor_user_id != subject.owner_user_id
        ):
            raise SignalOwnershipError(
                "day context has invalid origin actor provenance"
            )
        historical: IntegrationConnection | None = None
        if row.integration_connection_id is not None:
            historical = await session.scalar(
                select(IntegrationConnection).where(
                    IntegrationConnection.id
                    == row.integration_connection_id
                )
            )
            if (
                historical is None
                or historical.subject_id != identity.subject_id
                or historical.connection_type
                != IntegrationConnectionType.RECIPIENT.value
                or historical.status
                not in _HISTORICAL_RECIPIENT_STATUSES
            ):
                raise SignalOwnershipError(
                    "day context has invalid historical connection provenance"
                )
        system_plan = (
            identity.actor_user_id is None
            and integration_connection_id is None
        )
        if not system_plan:
            if (
                row.actor_user_id is not None
                and identity.actor_user_id is not None
                and row.actor_user_id != identity.actor_user_id
            ):
                raise SignalOwnershipError(
                    "day context belongs to another origin actor"
                )
            if row.actor_user_id is None:
                row.actor_user_id = identity.actor_user_id
            if (
                row.integration_connection_id is None
                and integration_connection_id is not None
            ):
                row.integration_connection_id = integration_connection_id

    if row is None:
        row = DayContext(
            subject_id=identity.subject_id,
            actor_user_id=identity.actor_user_id,
            integration_connection_id=integration_connection_id,
            date=on_date,
            domain=DOMAIN,
            source=source,
            answers=answers,
        )
        session.add(row)
    else:
        row.answers = (
            {**dict(row.answers or {}), **answers}
            if merge_answers
            else answers
        )
        if not preserve_source:
            row.source = source
    if planned is not None and (not planned_if_missing or not row.planned):
        row.planned = planned
    await session.flush()
    return row


async def get_day_context(
    session: AsyncSession,
    on_date: date_type,
    *,
    subject_id: uuid.UUID,
    integration_connection_id: uuid.UUID | None = None,
) -> Optional[DayContext]:
    stmt = select(DayContext).where(DayContext.date == on_date)
    owned = _owned_health_row_scope(
        DayContext,
        subject_id=subject_id,
        integration_connection_id=integration_connection_id,
    )
    stmt = stmt.where(owned)
    result = await session.execute(stmt)
    return result.scalars().first()


async def list_day_contexts(
    session: AsyncSession,
    *,
    start: date_type | None = None,
    end: date_type | None = None,
    limit: int = 100,
    subject_id: uuid.UUID,
) -> list[DayContext]:
    """Newest day contexts within one explicit subject compatibility scope."""

    stmt = select(DayContext)
    owned = _owned_health_row_scope(
        DayContext,
        subject_id=subject_id,
    )
    stmt = stmt.where(owned)
    if start is not None:
        stmt = stmt.where(DayContext.date >= start)
    if end is not None:
        stmt = stmt.where(DayContext.date <= end)
    stmt = stmt.order_by(DayContext.date.desc()).limit(limit)
    return list(await session.scalars(stmt))
