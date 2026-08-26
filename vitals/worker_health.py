"""Instance-local health contract for the standalone scheduler worker."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy import text

from vitals.config import load_config
from vitals.database import create_session_factory
from vitals.process_mode import ProcessMode, load_process_mode
from vitals.runtime_env import require_runtime_environment_isolation
from vitals.scheduler.control import (
    WORKER_MANIFEST_TTL_SECONDS,
    read_worker_health_state,
)
from vitals.scheduler.scheduler import (
    SchedulerHealthClassificationError,
    heartbeat_budget_caps,
)
from vitals.scheduler.scheduler_lock import scheduler_heartbeat_age

DEFAULT_WORKER_READY_FILE = Path("/tmp/vitals-worker-ready")
WORKER_HEALTH_TIMEOUT_SECONDS = 8.0
_READY_MARKER_VERSION = "v1"
_MAX_MARKER_BYTES = 64


class WorkerHealthError(RuntimeError):
    """The local worker instance cannot prove that it is healthy."""


@dataclass(frozen=True)
class WorkerHealthSnapshot:
    """Non-PHI evidence accepted by the worker health check."""

    generation: str
    manifest_age_seconds: float
    heartbeat_job_count: int


def worker_ready_file() -> Path:
    """Return the container-local readiness marker path."""

    return DEFAULT_WORKER_READY_FILE


def clear_worker_ready(path: Path) -> None:
    """Remove readiness, including a marker left by a restarted process."""

    path.unlink(missing_ok=True)


def mark_worker_ready(path: Path) -> None:
    """Atomically publish readiness after this instance leased its manifest."""

    payload = f"{_READY_MARKER_VERSION}:{os.getpid()}\n".encode("ascii")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ready_worker_pid(path: Path) -> int:
    try:
        with path.open("rb") as marker:
            payload = marker.read(_MAX_MARKER_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise WorkerHealthError("worker readiness marker is unavailable") from exc
    if len(payload) > _MAX_MARKER_BYTES:
        raise WorkerHealthError("worker readiness marker is malformed")
    try:
        version, raw_pid = payload.decode("ascii").strip().split(":", 1)
        pid = int(raw_pid)
    except (UnicodeDecodeError, ValueError) as exc:
        raise WorkerHealthError("worker readiness marker is malformed") from exc
    if version != _READY_MARKER_VERSION or pid <= 0:
        raise WorkerHealthError("worker readiness marker is malformed")
    try:
        os.kill(pid, 0)
    except (OSError, ValueError) as exc:
        raise WorkerHealthError("ready worker process is not running") from exc
    return pid


async def check_worker_health(
    *,
    session_factory,
    redis,
    ready_path: Path,
    now: float | None = None,
) -> WorkerHealthSnapshot:
    """Verify local readiness, DB/Redis, manifest lease, and every heartbeat."""

    _ready_worker_pid(ready_path)
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise WorkerHealthError("worker database check failed") from exc

    try:
        if not await redis.ping():
            raise WorkerHealthError("worker Redis ping failed")
        desired_generation, manifest = await read_worker_health_state(redis)
    except WorkerHealthError:
        raise
    except Exception as exc:
        raise WorkerHealthError("worker Redis control state is unavailable") from exc

    if manifest.generation != desired_generation:
        raise WorkerHealthError("worker schedule reload is pending")
    checked_at = time.time() if now is None else now
    manifest_age = checked_at - manifest.published_at
    if manifest_age < 0 or manifest_age > WORKER_MANIFEST_TTL_SECONDS:
        raise WorkerHealthError("worker manifest lease is stale")

    try:
        budgets = heartbeat_budget_caps(manifest.heartbeat_job_ids)
    except SchedulerHealthClassificationError as exc:
        raise WorkerHealthError("worker heartbeat manifest is unsupported") from exc
    for job_id, budget in budgets.items():
        age = await scheduler_heartbeat_age(redis, job_id)
        if age is None or age > budget:
            raise WorkerHealthError(f"worker heartbeat is stale: {job_id}")

    return WorkerHealthSnapshot(
        generation=desired_generation,
        manifest_age_seconds=manifest_age,
        heartbeat_job_count=len(budgets),
    )


async def check_configured_worker_health() -> WorkerHealthSnapshot:
    """Build and close the resources owned by one executable health probe."""

    if load_process_mode() is not ProcessMode.WORKER:
        raise WorkerHealthError("worker health check requires worker process mode")
    require_runtime_environment_isolation()
    config = load_config()
    session_factory = create_session_factory(config)
    engine = session_factory.kw["bind"]
    async with AsyncExitStack() as stack:
        stack.push_async_callback(engine.dispose)
        redis = Redis.from_url(config.redis_url, decode_responses=True)
        stack.push_async_callback(redis.aclose)
        async with asyncio.timeout(WORKER_HEALTH_TIMEOUT_SECONDS):
            return await check_worker_health(
                session_factory=session_factory,
                redis=redis,
                ready_path=worker_ready_file(),
            )


def main() -> None:
    try:
        asyncio.run(check_configured_worker_health())
    except Exception:
        print("worker health: error")
        raise SystemExit(1) from None
    print("worker health: ok")


if __name__ == "__main__":
    main()
