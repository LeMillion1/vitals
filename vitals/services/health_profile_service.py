"""The five facts about a body, stored per subject rather than per process.

Age, sex, height, the programme and the goals lived in ``.env``, and ``.env``
names nobody. One set of them for the whole installation is unambiguous while
the installation is one person, and it stops being a description of anybody the
moment there are two. Two things came out of that, and only one of them looked
like a bug:

* the visible half — the profile was being written into every patient's weekly
  digest, doctor's report and share link as though it were theirs.
  the digest projection has been omitting it since, which costs the owner five
  fields and is a placeholder rather than an answer;
* the quiet half — the Navy body-fat formula takes a height and a sex. Every
  patient's body-fat percentage and lean body mass were computed from the
  installation owner's geometry, and a wrong number in a medical record reads
  exactly like a right one.

So the profile becomes subject-scoped state, in ``subject_settings`` beside the
notification policy, and the readers take a subject.

**Absent is not a default.** A subject who has never opened the settings has no
row, and the honest reading of no row is that nobody has said — not that they
are a 190 cm male of 18. The identity fields therefore come back ``None`` and
every consumer already handles that: the Navy formula is skipped, the report
omits what it does not know. The three nutrition targets are the exception and
deliberately so: a target is a goal rather than a fact about a body, a default
one is a reasonable starting point, and nothing medical is inferred from it.

The installation's own ``.env`` values are adopted into the legacy owner's row
at startup, once, while that owner is still the only subject — which is exactly
when they are unambiguously theirs.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.identity import HealthSubject
from vitals.models.scoped_settings import SubjectSetting

#: The one ``subject_settings`` key this module owns.
PROFILE_KEY = "health_profile"

#: Only these two are a sex for the Navy formula's purposes. Anything else is
#: stored as absent rather than coerced, because coercing it would pick one.
SEXES = ("male", "female")

DEFAULT_PROTEIN_TARGET_G = 150.0
DEFAULT_CALORIES_MIN = 1300
DEFAULT_CALORIES_MAX = 1700


class HealthProfileError(Exception):
    """Base class for a fail-closed subject profile error."""


class HealthProfileValidationError(HealthProfileError):
    """A caller passed something that is not a subject or not a profile."""


@dataclass(frozen=True, slots=True)
class HealthProfile:
    """What this installation knows about one person's body and goals."""

    age: int | None = None
    sex: str | None = None
    height_cm: float | None = None
    program: str | None = None
    goals: tuple[str, ...] = ()
    protein_target_g: float = DEFAULT_PROTEIN_TARGET_G
    calories_min: int = DEFAULT_CALORIES_MIN
    calories_max: int = DEFAULT_CALORIES_MAX

    @property
    def describes_a_body(self) -> bool:
        """Whether there is enough here to compute a body-composition estimate.

        The Navy formula needs both a height and a sex. Having one of them is
        the same as having neither, and returning a number from half a profile
        is the failure this module exists to stop.
        """

        return self.height_cm is not None and self.sex is not None

    def as_report_profile(self) -> dict[str, Any]:
        """The shape every report consumer already reads."""

        return {
            "age": self.age,
            "sex": self.sex,
            "height_cm": self.height_cm,
            "program": self.program,
            "goals": list(self.goals),
        }

    def as_nutrition_goals(self) -> dict[str, Any]:
        return {
            "protein_target_g": self.protein_target_g,
            "calories_min": self.calories_min,
            "calories_max": self.calories_max,
        }

    def as_stored_value(self) -> dict[str, Any]:
        return {
            "age": self.age,
            "sex": self.sex,
            "height_cm": self.height_cm,
            "program": self.program,
            "goals": list(self.goals),
            "protein_target_g": self.protein_target_g,
            "calories_min": self.calories_min,
            "calories_max": self.calories_max,
        }


EMPTY_PROFILE = HealthProfile()


@dataclass(frozen=True, slots=True)
class HealthProfileProjection:
    """Subject-scoped profile plus the subject-owned timezone."""

    profile: HealthProfile
    timezone: str | None


def _optional_int(raw: Any, *, low: int, high: int) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if low <= value <= high else None


def _optional_float(raw: Any, *, low: float, high: float) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        value = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return value if low <= value <= high else None


def _optional_text(raw: Any) -> str | None:
    if raw is None:
        return None
    text = " ".join(str(raw).split())
    return text or None


def _goals(raw: Any) -> tuple[str, ...]:
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        return ()
    cleaned = []
    for part in parts:
        text = _optional_text(part)
        if text and text not in cleaned:
            cleaned.append(text)
    return tuple(cleaned[:12])


def sanitize(raw: Any) -> HealthProfile:
    """Coerce whatever arrived into a profile, keeping absent absent.

    Out-of-range and unparseable values become ``None`` rather than being
    clamped to a boundary: a height of 3 cm is somebody mistyping, and 3 cm
    clamped to 120 cm is a plausible-looking wrong answer, which is worse.
    """

    if not isinstance(raw, dict):
        raw = {}
    sex = _optional_text(raw.get("sex"))
    if sex is not None:
        sex = sex.lower()
        if sex not in SEXES:
            sex = None
    return HealthProfile(
        age=_optional_int(raw.get("age"), low=1, high=120),
        sex=sex,
        height_cm=_optional_float(raw.get("height_cm"), low=50.0, high=260.0),
        program=_optional_text(raw.get("program")),
        goals=_goals(raw.get("goals")),
        protein_target_g=(
            _optional_float(raw.get("protein_target_g"), low=0.0, high=1000.0)
            or DEFAULT_PROTEIN_TARGET_G
        ),
        calories_min=(
            _optional_int(raw.get("calories_min"), low=0, high=20000)
            or DEFAULT_CALORIES_MIN
        ),
        calories_max=(
            _optional_int(raw.get("calories_max"), low=0, high=20000)
            or DEFAULT_CALORIES_MAX
        ),
    )


def _require_subject_id(subject_id: Any) -> uuid.UUID:
    if not isinstance(subject_id, uuid.UUID) or subject_id.int == 0:
        raise HealthProfileValidationError("subject_id must be a non-zero UUID")
    return subject_id


async def get_profile(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> HealthProfile:
    """This subject's profile, or the empty one if they have never said."""

    subject_id = _require_subject_id(subject_id)
    with session.no_autoflush:
        raw = await session.scalar(
            select(SubjectSetting.value).where(
                SubjectSetting.subject_id == subject_id,
                SubjectSetting.key == PROFILE_KEY,
            )
        )
    if raw is None:
        return EMPTY_PROFILE
    return sanitize(raw)


async def get_profile_projection(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> HealthProfileProjection:
    """Return the reusable identity/profile slice used by delivery adapters."""

    subject_id = _require_subject_id(subject_id)
    profile = await get_profile(session, subject_id=subject_id)
    timezone = await session.scalar(
        select(HealthSubject.timezone).where(HealthSubject.id == subject_id)
    )
    return HealthProfileProjection(profile=profile, timezone=timezone)


async def get_subject_timezone(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> str | None:
    """Read the subject-owned timezone without loading profile settings."""

    subject_id = _require_subject_id(subject_id)
    return await session.scalar(
        select(HealthSubject.timezone).where(HealthSubject.id == subject_id)
    )


async def set_subject_timezone(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    timezone: str,
) -> bool:
    """Update one subject's durable timezone without owning the commit."""

    subject_id = _require_subject_id(subject_id)
    subject = await session.get(HealthSubject, subject_id)
    if subject is None:
        return False
    subject.timezone = timezone
    await session.flush()
    return True


async def set_subject_timezone_if_valid(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    timezone: str,
) -> bool:
    """Validate an IANA timezone and update it, or leave state unchanged."""

    value = timezone.strip()
    if not value:
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    return await set_subject_timezone(
        session,
        subject_id=subject_id,
        timezone=value,
    )


async def set_profile(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    raw: Any,
) -> HealthProfile:
    """Replace this subject's profile. Never commits."""

    subject_id = _require_subject_id(subject_id)
    owner = await session.scalar(
        select(HealthSubject.id).where(HealthSubject.id == subject_id)
    )
    if owner is None:
        raise HealthProfileValidationError("health subject does not exist")

    profile = sanitize(raw)
    row = await session.scalar(
        select(SubjectSetting)
        .where(
            SubjectSetting.subject_id == subject_id,
            SubjectSetting.key == PROFILE_KEY,
        )
        .with_for_update()
    )
    if row is None:
        session.add(
            SubjectSetting(
                subject_id=subject_id,
                key=PROFILE_KEY,
                value=profile.as_stored_value(),
            )
        )
    else:
        row.value = profile.as_stored_value()
    await session.flush()
    return profile


async def adopt_installation_profile(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
) -> HealthProfile:
    """Give the legacy owner the ``.env`` profile, once, if they have no row.

    Called from the startup bootstrap, which already runs only while this is the
    installation's sole subject — the one state in which an unattributed profile
    is unambiguously somebody's. It never overwrites: once the owner has edited
    their profile, the environment is stale history and the row is the answer.
    """

    subject_id = _require_subject_id(subject_id)
    existing = await session.scalar(
        select(SubjectSetting.value).where(
            SubjectSetting.subject_id == subject_id,
            SubjectSetting.key == PROFILE_KEY,
        )
    )
    if existing is not None:
        return sanitize(existing)

    from vitals.config import load_config

    cfg = load_config()
    return await set_profile(
        session,
        subject_id=subject_id,
        raw={
            "age": cfg.user_age,
            "sex": cfg.sex,
            "height_cm": cfg.height_cm,
            "program": cfg.user_program,
            "goals": list(cfg.user_goals),
            "protein_target_g": cfg.nutrition_protein_target_g,
            "calories_min": cfg.nutrition_calories_min,
            "calories_max": cfg.nutrition_calories_max,
        },
    )


__all__ = [
    "DEFAULT_CALORIES_MAX",
    "DEFAULT_CALORIES_MIN",
    "DEFAULT_PROTEIN_TARGET_G",
    "EMPTY_PROFILE",
    "HealthProfile",
    "HealthProfileError",
    "HealthProfileValidationError",
    "PROFILE_KEY",
    "SEXES",
    "adopt_installation_profile",
    "get_profile",
    "sanitize",
    "set_profile",
]
