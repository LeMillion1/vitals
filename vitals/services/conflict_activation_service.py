"""Subject-scoped activation state for conflict rules.

Curated conflict definitions remain installation-wide rows, while their active
state belongs to a health subject.  This service stores that state as a compact
deny-list in ``subject_settings`` and keeps the pre-commercial ``active`` flag
only as a temporary exact-one-subject compatibility mirror.

The service deliberately has no router or conflict-engine integration.  Caller
boundaries authorize the actor and own commit/rollback; mutations here acquire
the shared identity-governance lock, lock roots in a stable order, and flush.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.conflict_rule import ConflictRule
from vitals.models.identity import HealthSubject
from vitals.models.scoped_settings import SubjectSetting
from vitals.services.conflict_catalog import load_rule_catalog
from vitals.services.identity_service import (
    UnsupportedIdentityDatabaseError,
    acquire_identity_governance_lock,
)


SETTING_KEY = "conflict_rule_activation"
SETTING_VERSION = 1

_CURATED_FIELDS = (
    "rule_type",
    "domain_a",
    "condition_a",
    "domain_b",
    "condition_b",
    "severity",
    "message",
    "params",
    "category",
    "source",
    "evidence",
)


class ConflictActivationError(RuntimeError):
    """Base class for persisted activation and ownership failures."""


class ConflictActivationValidationError(ValueError):
    """A caller input cannot identify a safe activation operation."""


class ConflictActivationSubjectNotFoundError(ConflictActivationError):
    """The requested health-subject root does not exist."""


class ConflictActivationRuleNotFoundError(ConflictActivationError):
    """The requested conflict-rule row does not exist."""


class ConflictActivationOwnershipError(ConflictActivationError):
    """A rule belongs to another subject or an unavailable legacy scope."""


class ConflictActivationLegacyBridgeError(ConflictActivationError):
    """Fully-unowned compatibility cannot be proved safe."""


class ConflictActivationStateMalformedError(ConflictActivationError):
    """Persisted subject activation JSON is not the exact supported schema."""


class ConflictActivationCatalogIntegrityError(ConflictActivationError):
    """A row claiming catalog provenance is unknown, copied, or tampered."""


class ConflictActivationUnsupportedDatabaseError(ConflictActivationError):
    """The database cannot provide the required governance lock."""


class LegacyConflictActivationBridge(StrEnum):
    """Whether fully-unowned pre-commercial state may be adopted."""

    REJECT = "reject"
    FULLY_UNOWNED = "fully_unowned"


class ConflictActivationStateSource(StrEnum):
    """Origin of the immutable activation snapshot."""

    DEFAULT = "default"
    SCOPED = "scoped"
    LEGACY = "legacy"


class ConflictActivationRuleKind(StrEnum):
    """Persistence strategy used by one rule."""

    CURATED = "curated"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class ConflictActivationState:
    """One detached, immutable activation snapshot for a health subject."""

    subject_id: uuid.UUID
    disabled_codes: tuple[str, ...]
    source: ConflictActivationStateSource
    legacy_bridge: LegacyConflictActivationBridge

    def __post_init__(self) -> None:
        _required_subject_id(self.subject_id)
        if type(self.disabled_codes) is not tuple:
            raise ConflictActivationStateMalformedError(
                "disabled_codes must be an immutable tuple"
            )
        _parse_document(self.to_document(), catalog=_catalog_definitions())
        if not isinstance(self.source, ConflictActivationStateSource):
            raise ConflictActivationStateMalformedError(
                "source must be a ConflictActivationStateSource"
            )
        if not isinstance(self.legacy_bridge, LegacyConflictActivationBridge):
            raise ConflictActivationStateMalformedError(
                "legacy_bridge must be a LegacyConflictActivationBridge"
            )

    def is_code_active(self, code: str) -> bool:
        """Return effective state for one checked-in catalog code."""

        if code not in _catalog_definitions():
            raise ConflictActivationCatalogIntegrityError(
                f"unknown curated conflict-rule code: {code!r}"
            )
        return code not in self.disabled_codes

    def to_document(self) -> dict[str, object]:
        """Return a fresh JSON document suitable for persistence or transport."""

        return {
            "v": SETTING_VERSION,
            "disabled_codes": list(self.disabled_codes),
        }


@dataclass(frozen=True, slots=True)
class ConflictActivationResult:
    """Detached outcome of one caller-owned activation transaction."""

    subject_id: uuid.UUID
    rule_id: int
    code: str | None
    kind: ConflictActivationRuleKind
    previous_active: bool
    active: bool
    adopted_legacy_rule: bool
    previous_state: ConflictActivationState
    state: ConflictActivationState


def _catalog_definitions() -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType(
        {entry["code"]: MappingProxyType(dict(entry)) for entry in load_rule_catalog()}
    )


def _required_subject_id(value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID) or value.int == 0:
        raise ConflictActivationValidationError(
            "subject_id must be a non-zero UUID"
        )
    return value


def _required_rule_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ConflictActivationValidationError("rule_id must be a positive integer")
    return value


def _required_active(value: object) -> bool:
    if type(value) is not bool:
        raise ConflictActivationValidationError("active must be a boolean")
    return value


def _as_bridge(
    value: LegacyConflictActivationBridge | str,
) -> LegacyConflictActivationBridge:
    try:
        return LegacyConflictActivationBridge(value)
    except (TypeError, ValueError) as exc:
        raise ConflictActivationValidationError(
            f"unknown legacy conflict bridge: {value!r}"
        ) from exc


async def _acquire_governance_lock(session: AsyncSession) -> None:
    try:
        await acquire_identity_governance_lock(session)
    except UnsupportedIdentityDatabaseError as exc:
        raise ConflictActivationUnsupportedDatabaseError(str(exc)) from exc


def _parse_document(
    value: Any,
    *,
    catalog: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    if type(value) is not dict or set(value) != {"v", "disabled_codes"}:
        raise ConflictActivationStateMalformedError(
            "conflict activation must contain exactly v and disabled_codes"
        )
    if type(value["v"]) is not int or value["v"] != SETTING_VERSION:
        raise ConflictActivationStateMalformedError(
            "conflict activation has an unsupported version"
        )
    raw_codes = value["disabled_codes"]
    if type(raw_codes) is not list or any(type(code) is not str for code in raw_codes):
        raise ConflictActivationStateMalformedError(
            "disabled_codes must be a list of strings"
        )
    if raw_codes != sorted(raw_codes) or len(raw_codes) != len(set(raw_codes)):
        raise ConflictActivationStateMalformedError(
            "disabled_codes must be sorted and unique"
        )
    unknown = [code for code in raw_codes if code not in catalog]
    if unknown:
        raise ConflictActivationStateMalformedError(
            f"disabled_codes contains unknown catalog code: {unknown[0]!r}"
        )
    return tuple(raw_codes)


def _require_catalog_integrity(
    rule: ConflictRule,
    definition: Mapping[str, Any],
) -> None:
    if rule.subject_id is not None:
        raise ConflictActivationCatalogIntegrityError(
            "a subject-owned rule cannot claim curated catalog provenance"
        )
    if any(
        getattr(rule, field_name) != definition.get(field_name)
        for field_name in _CURATED_FIELDS
    ):
        raise ConflictActivationCatalogIntegrityError(
            "global conflict rule differs from the checked-in catalog"
        )


def _custom_code_is_well_formed(code: object) -> bool:
    return code is None or (isinstance(code, str) and bool(code.strip()))


def _require_rule_shape(rule: ConflictRule) -> None:
    if rule.subject_id is not None and not isinstance(rule.subject_id, uuid.UUID):
        raise ConflictActivationCatalogIntegrityError(
            "conflict rule has a malformed subject root"
        )
    if rule.code is not None and not isinstance(rule.code, str):
        raise ConflictActivationCatalogIntegrityError(
            "conflict rule has a malformed code"
        )
    if type(rule.active) is not bool:
        raise ConflictActivationCatalogIntegrityError(
            "conflict rule has a malformed active flag"
        )


def _rule_kind(
    rule: ConflictRule,
    *,
    state: ConflictActivationState,
    catalog: Mapping[str, Mapping[str, Any]],
) -> ConflictActivationRuleKind:
    _require_rule_shape(rule)
    if rule.subject_id == state.subject_id:
        if rule.code in catalog:
            raise ConflictActivationCatalogIntegrityError(
                "a subject-owned rule cannot duplicate a curated catalog code"
            )
        if not _custom_code_is_well_formed(rule.code):
            raise ConflictActivationCatalogIntegrityError(
                "subject-owned custom rule has a malformed code"
            )
        return ConflictActivationRuleKind.CUSTOM

    if rule.subject_id is not None:
        raise ConflictActivationOwnershipError(
            "conflict rule belongs to another health subject"
        )

    if rule.code in catalog:
        _require_catalog_integrity(rule, catalog[rule.code])
        return ConflictActivationRuleKind.CURATED

    if rule.code is None:
        if (
            state.legacy_bridge
            is not LegacyConflictActivationBridge.FULLY_UNOWNED
        ):
            raise ConflictActivationOwnershipError(
                "fully-unowned custom rule requires the exact-one legacy bridge"
            )
        return ConflictActivationRuleKind.CUSTOM

    raise ConflictActivationCatalogIntegrityError(
        "unrecognized global conflict-rule provenance"
    )


def is_rule_active(
    rule: ConflictRule,
    state: ConflictActivationState,
) -> bool:
    """Return effective activation without mutating the supplied ORM row."""

    if not isinstance(state, ConflictActivationState):
        raise TypeError("state must be a ConflictActivationState")
    catalog = _catalog_definitions()
    kind = _rule_kind(rule, state=state, catalog=catalog)
    if kind is ConflictActivationRuleKind.CURATED:
        assert rule.code is not None
        return state.is_code_active(rule.code)
    return bool(rule.active)


def effective_rule_activation(
    rules: Iterable[ConflictRule],
    state: ConflictActivationState,
) -> Mapping[int, bool]:
    """Return an immutable ``rule_id -> active`` mapping for one scoped loader."""

    activation: dict[int, bool] = {}
    for rule in rules:
        if type(rule.id) is not int or rule.id <= 0:
            raise ConflictActivationCatalogIntegrityError(
                "conflict rule must have a persisted positive id"
            )
        if rule.id in activation:
            raise ConflictActivationCatalogIntegrityError(
                "duplicate conflict rule id in activation input"
            )
        activation[rule.id] = is_rule_active(rule, state)
    return MappingProxyType(activation)


async def _subject(
    session: AsyncSession,
    subject_id: uuid.UUID,
    *,
    for_update: bool,
) -> HealthSubject:
    query = (
        select(HealthSubject)
        .where(HealthSubject.id == subject_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update()
    subject = await session.scalar(query)
    if subject is None:
        raise ConflictActivationSubjectNotFoundError(
            f"health subject {subject_id} does not exist"
        )
    return subject


async def _setting(
    session: AsyncSession,
    subject_id: uuid.UUID,
    *,
    for_update: bool,
) -> SubjectSetting | None:
    query = (
        select(SubjectSetting)
        .where(
            SubjectSetting.subject_id == subject_id,
            SubjectSetting.key == SETTING_KEY,
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def _require_exact_one_subject(
    session: AsyncSession,
    subject_id: uuid.UUID,
) -> None:
    subject_ids = list(
        await session.scalars(
            select(HealthSubject.id).order_by(HealthSubject.id).limit(2)
        )
    )
    if subject_ids != [subject_id]:
        raise ConflictActivationLegacyBridgeError(
            "fully-unowned activation requires exactly one matching health subject"
        )


async def _global_rules(
    session: AsyncSession,
    *,
    for_update: bool,
) -> list[ConflictRule]:
    query = (
        select(ConflictRule)
        .where(ConflictRule.subject_id.is_(None))
        .order_by(ConflictRule.id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update()
    return list(await session.scalars(query))


def _legacy_disabled_codes(
    rules: Iterable[ConflictRule],
    *,
    catalog: Mapping[str, Mapping[str, Any]],
) -> tuple[str, ...]:
    disabled: list[str] = []
    for rule in rules:
        _require_rule_shape(rule)
        if rule.code is None:
            # A fully-unowned custom rule has no catalog activation entry.
            continue
        definition = catalog.get(rule.code)
        if definition is None:
            raise ConflictActivationCatalogIntegrityError(
                "unrecognized global conflict-rule provenance"
            )
        _require_catalog_integrity(rule, definition)
        if not rule.active:
            disabled.append(rule.code)
    return tuple(sorted(disabled))


def _state(
    *,
    subject_id: uuid.UUID,
    disabled_codes: tuple[str, ...],
    source: ConflictActivationStateSource,
    legacy_bridge: LegacyConflictActivationBridge,
) -> ConflictActivationState:
    return ConflictActivationState(
        subject_id=subject_id,
        disabled_codes=disabled_codes,
        source=source,
        legacy_bridge=legacy_bridge,
    )


async def read_activation_state(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    legacy_bridge: LegacyConflictActivationBridge | str = (
        LegacyConflictActivationBridge.REJECT
    ),
) -> ConflictActivationState:
    """Read a detached subject activation snapshot.

    A strict missing setting means every checked-in catalog code is enabled.
    The transitional bridge instead derives disabled codes from the global
    catalog rows, but only while the requested subject is provably the sole
    subject under the shared governance lock.
    """

    parsed_subject_id = _required_subject_id(subject_id)
    parsed_bridge = _as_bridge(legacy_bridge)
    catalog = _catalog_definitions()

    if parsed_bridge is LegacyConflictActivationBridge.REJECT:
        await _subject(session, parsed_subject_id, for_update=False)
        setting = await _setting(session, parsed_subject_id, for_update=False)
        if setting is None:
            return _state(
                subject_id=parsed_subject_id,
                disabled_codes=(),
                source=ConflictActivationStateSource.DEFAULT,
                legacy_bridge=parsed_bridge,
            )
        return _state(
            subject_id=parsed_subject_id,
            disabled_codes=_parse_document(setting.value, catalog=catalog),
            source=ConflictActivationStateSource.SCOPED,
            legacy_bridge=parsed_bridge,
        )

    await _acquire_governance_lock(session)
    with session.no_autoflush:
        await _subject(session, parsed_subject_id, for_update=True)
        await _require_exact_one_subject(session, parsed_subject_id)
        setting = await _setting(session, parsed_subject_id, for_update=True)
        if setting is not None:
            return _state(
                subject_id=parsed_subject_id,
                disabled_codes=_parse_document(setting.value, catalog=catalog),
                source=ConflictActivationStateSource.SCOPED,
                legacy_bridge=parsed_bridge,
            )
        rules = await _global_rules(session, for_update=True)
        disabled_codes = _legacy_disabled_codes(rules, catalog=catalog)
    return _state(
        subject_id=parsed_subject_id,
        disabled_codes=disabled_codes,
        source=ConflictActivationStateSource.LEGACY,
        legacy_bridge=parsed_bridge,
    )


async def set_rule_activation(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    rule_id: int,
    active: bool,
    legacy_bridge: LegacyConflictActivationBridge | str = (
        LegacyConflictActivationBridge.REJECT
    ),
) -> ConflictActivationResult:
    """Set one rule's effective activation and flush without committing.

    Lock order is governance, subject root, subject setting, then rule rows.
    The subject lock serializes concurrent first inserts of the composite-PK
    setting.  Curated rules update only the subject deny-list; while the
    exact-one bridge is explicitly selected they also mirror ``row.active``.
    """

    parsed_subject_id = _required_subject_id(subject_id)
    parsed_rule_id = _required_rule_id(rule_id)
    parsed_active = _required_active(active)
    parsed_bridge = _as_bridge(legacy_bridge)
    catalog = _catalog_definitions()

    await _acquire_governance_lock(session)
    with session.no_autoflush:
        await _subject(session, parsed_subject_id, for_update=True)
        if parsed_bridge is LegacyConflictActivationBridge.FULLY_UNOWNED:
            await _require_exact_one_subject(session, parsed_subject_id)
        setting = await _setting(session, parsed_subject_id, for_update=True)

        locked_globals: list[ConflictRule] | None = None
        if setting is not None:
            disabled_codes = _parse_document(setting.value, catalog=catalog)
            state_source = ConflictActivationStateSource.SCOPED
        elif parsed_bridge is LegacyConflictActivationBridge.FULLY_UNOWNED:
            # Lock the whole legacy catalog in id order before selecting a
            # target.  This gives one coherent derive-and-toggle snapshot.
            locked_globals = await _global_rules(session, for_update=True)
            disabled_codes = _legacy_disabled_codes(
                locked_globals,
                catalog=catalog,
            )
            state_source = ConflictActivationStateSource.LEGACY
        else:
            disabled_codes = ()
            state_source = ConflictActivationStateSource.DEFAULT

        rule = None
        if locked_globals is not None:
            rule = next(
                (candidate for candidate in locked_globals if candidate.id == parsed_rule_id),
                None,
            )
        if rule is None:
            rule = await session.scalar(
                select(ConflictRule)
                .where(ConflictRule.id == parsed_rule_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        if rule is None:
            raise ConflictActivationRuleNotFoundError(
                f"conflict rule {parsed_rule_id} does not exist"
            )

        previous_state = _state(
            subject_id=parsed_subject_id,
            disabled_codes=disabled_codes,
            source=state_source,
            legacy_bridge=parsed_bridge,
        )
        kind = _rule_kind(rule, state=previous_state, catalog=catalog)
        previous_active = is_rule_active(rule, previous_state)
        adopted = False

        if kind is ConflictActivationRuleKind.CURATED:
            assert rule.code is not None
            changed_codes = set(disabled_codes)
            if parsed_active:
                changed_codes.discard(rule.code)
            else:
                changed_codes.add(rule.code)
            disabled_codes = tuple(sorted(changed_codes))
            document = {
                "v": SETTING_VERSION,
                "disabled_codes": list(disabled_codes),
            }
            if setting is None:
                session.add(
                    SubjectSetting(
                        subject_id=parsed_subject_id,
                        key=SETTING_KEY,
                        value=document,
                    )
                )
            else:
                setting.value = document
            if parsed_bridge is LegacyConflictActivationBridge.FULLY_UNOWNED:
                rule.active = parsed_active
            current_state = _state(
                subject_id=parsed_subject_id,
                disabled_codes=disabled_codes,
                source=ConflictActivationStateSource.SCOPED,
                legacy_bridge=parsed_bridge,
            )
        else:
            if rule.subject_id is None:
                # _rule_kind has already proved FULLY_UNOWNED and code=NULL.
                rule.subject_id = parsed_subject_id
                adopted = True
            rule.active = parsed_active
            current_state = previous_state

    await session.flush()
    return ConflictActivationResult(
        subject_id=parsed_subject_id,
        rule_id=parsed_rule_id,
        code=rule.code,
        kind=kind,
        previous_active=previous_active,
        active=parsed_active,
        adopted_legacy_rule=adopted,
        previous_state=previous_state,
        state=current_state,
    )


__all__ = [
    "SETTING_KEY",
    "SETTING_VERSION",
    "ConflictActivationCatalogIntegrityError",
    "ConflictActivationError",
    "ConflictActivationLegacyBridgeError",
    "ConflictActivationOwnershipError",
    "ConflictActivationResult",
    "ConflictActivationRuleKind",
    "ConflictActivationRuleNotFoundError",
    "ConflictActivationState",
    "ConflictActivationStateMalformedError",
    "ConflictActivationStateSource",
    "ConflictActivationSubjectNotFoundError",
    "ConflictActivationUnsupportedDatabaseError",
    "ConflictActivationValidationError",
    "LegacyConflictActivationBridge",
    "effective_rule_activation",
    "is_rule_active",
    "read_activation_state",
    "set_rule_activation",
]
