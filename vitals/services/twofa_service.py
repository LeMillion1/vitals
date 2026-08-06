"""Two-factor authentication (TOTP, RFC 6238) — off by default, switched on from
Settings.

Single user, so there is no per-account table: the secret and whether enrolment
was ever completed live in one ``app_settings`` row. The key is deliberately
named ``twofa_secret`` — ``data_portability_service`` drops ``app_settings`` keys
that look like a credential, so the secret never rides along in a downloaded
backup. Renaming this key silently re-opens that leak; the test suite pins it.

The code arithmetic is stdlib. RFC 6238 is an HMAC-SHA1 over a 30-second counter
plus dynamic truncation — the twenty lines below — so an authenticator library
would add a dependency and nothing else.

A freshly minted secret is stored **unconfirmed** and grants nothing. 2FA only
switches on once a code generated from it has been typed back, which is what
stops a secret that never reached an authenticator app from locking the owner
out of their own dashboard.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import secrets
import struct
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

from vitals.models.app_settings import AppSetting

logger = logging.getLogger(__name__)

# app_settings key. Contains "secret" on purpose — see the module docstring.
SETTINGS_KEY = "twofa_secret"

DIGITS = 6
STEP_SECONDS = 30
# ±1 step. The phone's clock and the server's drift apart by a few seconds, and a
# code read at second 29 is typed at second 31. A wider window would just
# multiply how many codes are valid at once.
VALID_WINDOW = 1
# 160 bits — the size RFC 4226 recommends for an HMAC-SHA1 shared secret, and
# what every authenticator app expects.
_SECRET_BYTES = 20

_ISSUER = "Vitals"

# How long a used time-step stays remembered (see ``consume_step``). Must outlive
# the whole acceptance window so a code can't be replayed at its far edge.
_REPLAY_TTL = (VALID_WINDOW * 2 + 1) * STEP_SECONDS + 30


# ── Code arithmetic (pure, no DB) ─────────────────────────────────────────────
def generate_secret() -> str:
    """A fresh base32 shared secret, in the format authenticator apps read."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    """Decode a base32 secret, tolerating the spaces added for readability."""
    raw = secret.strip().replace(" ", "").upper()
    return base64.b32decode(raw + "=" * (-len(raw) % 8))


def code_at(secret: str, counter: int) -> str:
    """The 6-digit code for a given 30-second counter (RFC 4226 truncation)."""
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{truncated % 10 ** DIGITS:0{DIGITS}d}"


def verify_code(
    secret: Optional[str],
    code: str,
    *,
    now: Optional[float] = None,
    window: int = VALID_WINDOW,
) -> Optional[int]:
    """Check ``code`` against ``secret`` within ±``window`` steps.

    Returns the **matched time-step** so the caller can burn it against replay,
    or ``None`` when the code is malformed, wrong, or the secret is unusable.
    Compares in constant time per candidate step, so a near-miss can't be told
    from a far one by timing.
    """
    code = (code or "").strip().replace(" ", "")
    if not secret or len(code) != DIGITS or not code.isdigit():
        return None
    counter = int((time.time() if now is None else now) // STEP_SECONDS)
    try:
        for offset in range(-window, window + 1):
            step = counter + offset
            if step < 0:
                continue
            if hmac.compare_digest(code_at(secret, step), code):
                return step
    except (binascii.Error, ValueError):
        # A corrupt stored secret must read as "no valid code", never as a 500 on
        # the login form.
        logger.warning("2FA secret is not decodable; treating every code as wrong")
        return None
    return None


def provisioning_uri(secret: str, *, account: str) -> str:
    """The ``otpauth://`` URI an authenticator app enrols from.

    Rendered as a QR for another device to scan, and kept as a tappable link for
    the phone itself — a QR shown on the screen you'd be scanning *with* is
    useless, so both exist.
    """
    label = quote(f"{_ISSUER}:{account}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(_ISSUER, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )


def qr_svg(uri: str) -> str:
    """The enrolment URI as an inline SVG QR code.

    Dark-on-white regardless of the app's theme: scanners cope badly with an
    inverted QR, and a code that only reads in light mode is a code that fails
    exactly once, on the one screen where it matters.
    """
    import segno  # module-level import would pull it into every scheduler job

    return segno.make(uri, error="m").svg_inline(
        scale=5, border=2, dark="#1A1520", light="#FFFFFF"
    )


def format_secret(secret: str) -> str:
    """Group the secret into fours — 32 unbroken characters is unreadable to
    anyone typing it into a desktop authenticator by hand."""
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))


# ── Stored state ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TwoFAState:
    secret: Optional[str] = None
    confirmed: bool = False

    @property
    def enabled(self) -> bool:
        """Enrolment finished — the login flow must ask for a code."""
        return bool(self.secret) and self.confirmed

    @property
    def pending(self) -> bool:
        """A secret was minted but never confirmed. Grants nothing."""
        return bool(self.secret) and not self.confirmed


def _parse(raw: Any) -> TwoFAState:
    if not isinstance(raw, dict):
        return TwoFAState()
    secret = raw.get("secret")
    if not isinstance(secret, str) or not secret:
        return TwoFAState()
    return TwoFAState(secret=secret, confirmed=bool(raw.get("confirmed")))


async def get_state(session: AsyncSession) -> TwoFAState:
    """Current 2FA state; a missing row means off.

    Deliberately no try/except: this is read on the login path, and a DB failure
    that quietly answered "2FA is off" would turn an outage into a downgrade of
    the login itself. Failing loudly is correct — without the DB the dashboard
    has nothing to show anyway.
    """
    row = await session.get(AppSetting, SETTINGS_KEY)
    return _parse(row.value if row is not None else None)


async def _store(session: AsyncSession, value: dict) -> None:
    row = await session.get(AppSetting, SETTINGS_KEY)
    if row is None:
        session.add(AppSetting(key=SETTINGS_KEY, value=value))
    else:
        row.value = value  # a new dict, so SQLAlchemy sees the change
    await session.flush()


async def _clear(session: AsyncSession) -> None:
    row = await session.get(AppSetting, SETTINGS_KEY)
    if row is not None:
        await session.delete(row)
    await session.flush()


async def start_enrolment(session: AsyncSession) -> str:
    """Mint a fresh unconfirmed secret and return it. Flushes; caller commits.

    Replaces any previous pending secret, so re-opening the card after losing
    track of a half-finished enrolment starts cleanly.
    """
    secret = generate_secret()
    await _store(session, {"secret": secret, "confirmed": False})
    return secret


async def confirm_enrolment(session: AsyncSession, code: str) -> bool:
    """Switch 2FA on if ``code`` proves the secret reached an authenticator."""
    state = await get_state(session)
    if not state.pending:
        return False
    if verify_code(state.secret, code) is None:
        return False
    await _store(session, {"secret": state.secret, "confirmed": True})
    return True


async def disable(session: AsyncSession, code: str = "") -> bool:
    """Turn 2FA off, or drop a half-finished enrolment.

    A confirmed setup needs a current code: otherwise a stolen session cookie
    could switch off the very factor that exists to make the cookie insufficient.
    A pending one grants nothing yet, so cancelling it needs no proof.
    """
    state = await get_state(session)
    if state.pending:
        await _clear(session)
        return True
    if not state.enabled or verify_code(state.secret, code) is None:
        return False
    await _clear(session)
    return True


# ── Replay protection ─────────────────────────────────────────────────────────
def _replay_key(step: int) -> str:
    return f"twofa:used:{step}"


async def consume_step(redis, step: int) -> bool:
    """Claim a time-step, returning False if it was already spent.

    Without this the same six digits work for the whole ~90-second acceptance
    window: anyone who reads the code over your shoulder (or off a screen
    share) can walk in behind you.

    ponytail: fails open when Redis is unreachable, matching the rate limiter —
    a dead cache must not lock the owner out of their own dashboard. Swap for a
    DB row if replay protection ever has to survive a Redis outage.
    """
    if redis is None:
        return True
    try:
        return bool(await redis.set(_replay_key(step), "1", ex=_REPLAY_TTL, nx=True))
    except Exception:
        logger.warning("2FA replay store unavailable; accepting the code", exc_info=True)
        return True
