"""Redis control plane shared by split web and scheduler processes.

Only opaque generation hints and a leased non-PHI job-id manifest cross this
boundary. Preferences remain authoritative in PostgreSQL and are re-read by
the worker under its reviewed authorization scope on every control poll.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import WatchError

SCHEDULE_GENERATION_KEY = "scheduler:control:generation:v1"
WORKER_MANIFEST_KEY = "scheduler:control:worker_manifest:v1"
RELOAD_POLL_SECONDS = 5.0
WEB_SIGNAL_TIMEOUT_SECONDS = 2.0
WORKER_CONTROL_TIMEOUT_SECONDS = 10.0
WORKER_RELOAD_ATTEMPT_TIMEOUT_SECONDS = 30.0
WORKER_MANIFEST_TTL_SECONDS = 45
MANIFEST_VERSION = 1

_GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_JOB_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_MANIFEST_JOBS = 128


class SchedulerControlError(RuntimeError):
    """Redis scheduler control state is missing, malformed, or unsafe."""


@dataclass(frozen=True, slots=True)
class WorkerManifest:
    generation: str
    published_at: int
    heartbeat_job_ids: tuple[str, ...]


def new_schedule_generation() -> str:
    """Return a random identifier that carries no settings or identity data."""

    return secrets.token_hex(16)


def _validated_generation(value: object) -> str:
    if not isinstance(value, str) or _GENERATION_RE.fullmatch(value) is None:
        raise SchedulerControlError("scheduler generation is malformed")
    return value


async def request_schedule_reload(redis: Redis) -> str:
    """Publish an immediate reload hint and return its opaque generation."""

    generation = new_schedule_generation()
    await redis.set(SCHEDULE_GENERATION_KEY, generation)
    return generation


async def read_schedule_generation(redis: Redis) -> str:
    raw = await redis.get(SCHEDULE_GENERATION_KEY)
    return _validated_generation(raw)


async def ensure_schedule_generation(redis: Redis) -> str:
    """Create the durable generation once, preserving concurrent web signals."""

    raw = await redis.get(SCHEDULE_GENERATION_KEY)
    if raw is not None:
        return _validated_generation(raw)
    candidate = new_schedule_generation()
    await redis.set(SCHEDULE_GENERATION_KEY, candidate, nx=True)
    return await read_schedule_generation(redis)


def _validated_job_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_MANIFEST_JOBS:
        raise SchedulerControlError("worker manifest job ids are malformed")
    job_ids: list[str] = []
    for job_id in value:
        if not isinstance(job_id, str) or _JOB_ID_RE.fullmatch(job_id) is None:
            raise SchedulerControlError("worker manifest job id is malformed")
        job_ids.append(job_id)
    if len(set(job_ids)) != len(job_ids) or job_ids != sorted(job_ids):
        raise SchedulerControlError("worker manifest job ids are not canonical")
    if "keepalive" not in job_ids:
        raise SchedulerControlError("worker manifest is missing keepalive")
    return tuple(job_ids)


async def publish_worker_manifest(
    redis: Redis,
    *,
    generation: str,
    heartbeat_job_ids: Iterable[str],
) -> WorkerManifest | None:
    """Lease one generation with preference-independent job identifiers.

    The WATCH transaction refuses to overwrite a newer desired generation.
    ``None`` tells the caller to observe the new generation instead of
    publishing a stale acknowledgement during a rolling reload.
    """

    validated_generation = _validated_generation(generation)
    requested_job_ids = _validated_job_ids(sorted(set(heartbeat_job_ids)))
    while True:
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(SCHEDULE_GENERATION_KEY, WORKER_MANIFEST_KEY)
                desired_generation = await pipe.get(SCHEDULE_GENERATION_KEY)
                if desired_generation != validated_generation:
                    await pipe.unwatch()
                    return None
                # Two worker binaries may overlap during a rolling restart.
                # For one generation, keep the union of their claimed jobs so
                # an older/subset manifest can only fail closed, never erase a
                # health obligation established by the newer worker.
                merged_job_ids = set(requested_job_ids)
                current_raw = await pipe.get(WORKER_MANIFEST_KEY)
                if current_raw is not None:
                    try:
                        current = _decode_worker_manifest(current_raw)
                    except SchedulerControlError:
                        current = None
                    if (
                        current is not None
                        and current.generation == validated_generation
                    ):
                        merged_job_ids.update(current.heartbeat_job_ids)
                manifest = WorkerManifest(
                    generation=validated_generation,
                    published_at=int(time.time()),
                    heartbeat_job_ids=_validated_job_ids(sorted(merged_job_ids)),
                )
                payload = json.dumps(
                    {
                        "version": MANIFEST_VERSION,
                        "generation": manifest.generation,
                        "published_at": manifest.published_at,
                        "heartbeat_job_ids": manifest.heartbeat_job_ids,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                pipe.multi()
                pipe.set(
                    WORKER_MANIFEST_KEY,
                    payload,
                    ex=WORKER_MANIFEST_TTL_SECONDS,
                )
                await pipe.execute()
                return manifest
            except WatchError:
                # A concurrent signal won. Re-read it under a fresh WATCH; the
                # caller's outer timeout bounds a pathological writer loop.
                continue


def _decode_worker_manifest(raw: object) -> WorkerManifest:
    if not isinstance(raw, str):
        raise SchedulerControlError("worker manifest is missing")
    if len(raw) > 32_768:
        raise SchedulerControlError("worker manifest is too large")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SchedulerControlError("worker manifest is malformed") from exc
    expected_keys = {
        "version",
        "generation",
        "published_at",
        "heartbeat_job_ids",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise SchedulerControlError("worker manifest shape is malformed")
    if payload.get("version") != MANIFEST_VERSION:
        raise SchedulerControlError("worker manifest version is unsupported")
    published_at = payload.get("published_at")
    if (
        isinstance(published_at, bool)
        or not isinstance(published_at, int)
        or published_at <= 0
    ):
        raise SchedulerControlError("worker manifest timestamp is malformed")
    return WorkerManifest(
        generation=_validated_generation(payload.get("generation")),
        published_at=published_at,
        heartbeat_job_ids=_validated_job_ids(payload.get("heartbeat_job_ids")),
    )


async def read_worker_manifest(redis: Redis) -> WorkerManifest:
    return _decode_worker_manifest(await redis.get(WORKER_MANIFEST_KEY))


async def read_worker_health_state(redis: Redis) -> tuple[str, WorkerManifest]:
    """Read desired generation and worker ack in one Redis snapshot."""

    generation, manifest = await redis.mget(
        SCHEDULE_GENERATION_KEY,
        WORKER_MANIFEST_KEY,
    )
    return _validated_generation(generation), _decode_worker_manifest(manifest)


__all__ = [
    "RELOAD_POLL_SECONDS",
    "SCHEDULE_GENERATION_KEY",
    "WEB_SIGNAL_TIMEOUT_SECONDS",
    "WORKER_CONTROL_TIMEOUT_SECONDS",
    "WORKER_MANIFEST_TTL_SECONDS",
    "WORKER_RELOAD_ATTEMPT_TIMEOUT_SECONDS",
    "WORKER_MANIFEST_KEY",
    "SchedulerControlError",
    "WorkerManifest",
    "ensure_schedule_generation",
    "new_schedule_generation",
    "publish_worker_manifest",
    "read_schedule_generation",
    "read_worker_health_state",
    "read_worker_manifest",
    "request_schedule_reload",
]
