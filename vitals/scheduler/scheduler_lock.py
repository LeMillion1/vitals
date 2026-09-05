"""Redis-based distributed lock + liveness heartbeat for scheduler jobs.

Ported near-verbatim from Boxly's ``bot/services/scheduler_lock.py``. Vitals runs
one ``vitals_app`` container today, but the lock keeps every scheduled job
single-runner if that's ever scaled to >1 worker (defence in depth), and the
heartbeat lets ``/health`` flag a stalled scheduler.

    SET scheduler:lock:<job_id> NX EX <ttl>  → first worker wins; others skip.
    Release is compare-and-delete by token so we never drop a lock we no longer
    own (TTL expired + another worker took over). TTL is the crash safety net.
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


# A retained occurrence claim is deliberately not a lock.  Releasing it after
# work would let a rolling worker or a repeated DST wall-clock minute run the
# same occurrence again.  Eight days is long enough to cover every local-time
# replay hazard in the registry (including the weekly job) while keeping the
# Redis footprint bounded.
SUBJECT_SLOT_CLAIM_TTL_SECONDS = 8 * 24 * 60 * 60


class SchedulerSlotClaimError(RuntimeError):
    """A subject-local occurrence could not be claimed safely."""


def _subject_slot_claim_digest(
    job_id: str,
    subject_id: uuid.UUID,
    local_slot: datetime,
) -> str:
    """Opaque identity for one logical job/subject/local wall-clock minute."""

    if local_slot.tzinfo is not None:
        raise ValueError("local_slot must be a naive wall-clock datetime")
    material = "\0".join(
        (
            "v1",
            job_id,
            str(subject_id),
            local_slot.strftime("%Y-%m-%dT%H:%M"),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def claim_subject_schedule_slot(
    redis: Redis,
    *,
    job_id: str,
    subject_id: uuid.UUID,
    local_slot: datetime,
    ttl_seconds: int = SUBJECT_SLOT_CLAIM_TTL_SECONDS,
) -> str | None:
    """Retain an at-most-once claim for a subject's current local slot.

    The returned digest is safe to reuse in an in-memory APScheduler job id.
    ``None`` means another worker already claimed the occurrence.  Redis errors
    propagate as a bounded scheduler error: running without a claim would turn
    an infrastructure outage into duplicate scheduled work.
    """

    if not isinstance(subject_id, uuid.UUID) or subject_id.int == 0:
        raise ValueError("subject_id must be a non-zero UUID")
    if (
        not isinstance(ttl_seconds, int)
        or isinstance(ttl_seconds, bool)
        or ttl_seconds <= 0
    ):
        raise ValueError("ttl_seconds must be a positive integer")
    digest = _subject_slot_claim_digest(job_id, subject_id, local_slot)
    key = f"scheduler:subject_slot:v1:{digest}"
    try:
        acquired = await redis.set(key, "1", nx=True, ex=ttl_seconds)
    except Exception as exc:
        raise SchedulerSlotClaimError(
            f"could not claim subject-local slot for {job_id}"
        ) from exc
    return digest if acquired else None


# ── Heartbeat ────────────────────────────────────────────────────────────────
# Each tick stamps a Redis key with the current epoch; /health compares its age
# and reports the scheduler as stale once a tick is overdue. Epoch seconds keep
# the age maths trivial and timezone-free.
def _heartbeat_key(job_id: str) -> str:
    return f"scheduler:last_run:{job_id}"


async def record_scheduler_heartbeat(redis: Redis, job_id: str) -> bool:
    """Stamp ``scheduler:last_run:{job_id}`` with now. Best-effort: a Redis hiccup
    must never break the job whose liveness we're recording.

    The boolean lets startup/control-plane seeding require confirmation without
    changing normal job ticks from best-effort into a failure source.
    """
    try:
        recorded = await redis.set(_heartbeat_key(job_id), str(int(time.time())))
    except Exception:
        logger.warning("Could not record scheduler heartbeat for %s", job_id)
        return False
    return bool(recorded)


async def scheduler_heartbeat_age(redis: Redis, job_id: str) -> Optional[float]:
    """Seconds since ``job_id`` last recorded a heartbeat, or None if it never has
    (key missing / unreadable) — which /health treats as 'stale' too."""
    try:
        raw = await redis.get(_heartbeat_key(job_id))
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return max(0.0, time.time() - float(raw))
    except (ValueError, TypeError):
        return None


# Release only if we still own the lock. A blind DEL would delete a *different*
# worker's lock if ours had already expired (TTL) and been re-acquired. Compare-
# and-delete by token closes that.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


async def with_scheduler_lock(
    redis: Redis,
    job_id: str,
    ttl_seconds: int,
    fn: Callable[..., Awaitable[Any]],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Acquire ``scheduler:lock:{job_id}``, run ``fn``, release.

    Returns the function result, or None if the lock could not be acquired
    (another worker is running the same job). The lock value is a per-acquire
    random token; release is compare-and-delete.
    """
    lock_key = f"scheduler:lock:{job_id}"
    token = uuid.uuid4().hex
    acquired = await redis.set(lock_key, token, nx=True, ex=ttl_seconds)
    if not acquired:
        logger.info("Scheduler lock busy: %s — another worker holds it", job_id)
        return None

    logger.info("Acquired scheduler lock: %s (ttl=%ds)", job_id, ttl_seconds)
    try:
        return await fn(*args, **kwargs)
    finally:
        try:
            await redis.eval(_RELEASE_SCRIPT, 1, lock_key, token)
        except Exception:
            logger.warning("Could not release scheduler lock %s — relying on TTL", job_id)
