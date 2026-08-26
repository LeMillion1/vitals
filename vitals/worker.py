"""Standalone APScheduler entry point for the split-runtime transition.

Run it only with ``VITALS_PROCESS_MODE=worker``. Compose deliberately continues
to use the default combined process until the database-role and deployment
cutover is implemented separately.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress

from redis.asyncio import Redis

from vitals.config import load_config
from vitals.database import create_session_factory
from vitals.process_mode import ProcessMode, load_process_mode
from vitals.runtime_env import require_runtime_environment_isolation
from vitals.scheduler.control import (
    RELOAD_POLL_SECONDS,
    WORKER_CONTROL_TIMEOUT_SECONDS,
    WORKER_RELOAD_ATTEMPT_TIMEOUT_SECONDS,
    ensure_schedule_generation,
    publish_worker_manifest,
)
from vitals.scheduler.lifecycle import WorkerLifecycle, load_worker_settings
from vitals.worker_health import (
    clear_worker_ready,
    mark_worker_ready,
    worker_ready_file,
)

logger = logging.getLogger(__name__)
_STOP_REQUESTED = object()


def _install_stop_handlers(event: asyncio.Event) -> tuple[signal.Signals, ...]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, event.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(signum)
    return tuple(installed)


async def _wait_for_poll(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def _await_or_stop(awaitable, stop_event: asyncio.Event):
    """Cancel one bounded control await as soon as shutdown is requested."""

    work = asyncio.create_task(awaitable)
    stopped = asyncio.create_task(stop_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {work, stopped},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stopped in done and stop_event.is_set():
            if not work.done():
                work.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await work
            return _STOP_REQUESTED
        stopped.cancel()
        with suppress(asyncio.CancelledError):
            await stopped
        return await work
    finally:
        for task in (work, stopped):
            if not task.done():
                task.cancel()
        # An outer asyncio.timeout must not return while a cancelled DB/Redis
        # operation is still unwinding against resources the ExitStack may now
        # close. Await both cancellation paths before handing control back.
        await asyncio.gather(work, stopped, return_exceptions=True)


async def monitor_schedule_reloads(
    *,
    lifecycle: WorkerLifecycle,
    session_factory,
    redis: Redis,
    applied_settings: dict | None,
    stop_event: asyncio.Event,
    poll_seconds: float = RELOAD_POLL_SECONDS,
    on_manifest_published: Callable[[], None] | None = None,
) -> None:
    """Poll PostgreSQL truth until shutdown and lease the applied worker state.

    Redis generations make an acknowledged save visible immediately, but they
    are only a hint: the committed settings projection is read every poll. A
    failed web→Redis signal therefore cannot strand the worker indefinitely.
    """

    while not stop_event.is_set():
        try:
            async with asyncio.timeout(WORKER_RELOAD_ATTEMPT_TIMEOUT_SECONDS):
                desired_generation = await _await_or_stop(
                    ensure_schedule_generation(redis), stop_event
                )
                if desired_generation is _STOP_REQUESTED:
                    return
                settings = await _await_or_stop(
                    load_worker_settings(session_factory), stop_event
                )
                if settings is _STOP_REQUESTED:
                    return
                if settings != applied_settings:
                    lifecycle.reload(settings)
                    # Update local applied state before the Redis lease write.
                    # If publication fails, the next poll retries only the ack;
                    # replacing interval jobs again could indefinitely rephase
                    # their next fire.
                    applied_settings = settings

                seeded = await _await_or_stop(
                    lifecycle.seed_pending_heartbeats(), stop_event
                )
                if seeded is _STOP_REQUESTED:
                    return

                manifest = await _await_or_stop(
                    publish_worker_manifest(
                        redis,
                        generation=desired_generation,
                        heartbeat_job_ids=lifecycle.heartbeat_job_ids,
                    ),
                    stop_event,
                )
                if manifest is _STOP_REQUESTED:
                    return
                if manifest is None:
                    # A newer signal won the WATCH race. Observe it immediately
                    # rather than keeping an avoidable pending window.
                    continue
                if on_manifest_published is not None:
                    on_manifest_published()
        except asyncio.CancelledError:
            raise
        except Exception:
            # The short manifest lease expires if DB/control polling cannot
            # complete, so web health cannot remain green on a stale schedule.
            logger.exception("scheduler control poll failed; retaining applied schedule")
        await _wait_for_poll(stop_event, poll_seconds)


async def run_worker(*, stop_event: asyncio.Event | None = None) -> None:
    """Run the standalone scheduler until a signal or supplied event stops it."""

    mode = load_process_mode()
    if mode is not ProcessMode.WORKER:
        raise RuntimeError(
            "the standalone worker requires VITALS_PROCESS_MODE=worker"
        )

    ready_path = worker_ready_file()
    clear_worker_ready(ready_path)
    # The worker is more privileged than web once the database roles split, so
    # it must enforce the same owner/control-plane credential boundary before
    # it opens either database or Redis resources.
    require_runtime_environment_isolation()
    config = load_config()
    session_factory = create_session_factory(config)
    engine = session_factory.kw["bind"]
    redis = Redis.from_url(config.redis_url, decode_responses=True)
    lifecycle = WorkerLifecycle(
        session_factory=session_factory,
        redis=redis,
        timezone=config.timezone,
    )
    owned_event = stop_event is None
    event = stop_event or asyncio.Event()
    installed_handlers: tuple[signal.Signals, ...] = ()

    if owned_event:
        # Install before the first startup await. SIGTERM during settings or
        # heartbeat I/O is then remembered and the process exits immediately
        # after the bounded startup step instead of missing the signal.
        installed_handlers = _install_stop_handlers(event)

    try:
        async with AsyncExitStack() as stack:
            # ExitStack runs every callback even if an earlier cleanup raises.
            # LIFO gives scheduler → Redis → engine, matching ownership.
            stack.push_async_callback(engine.dispose)
            stack.push_async_callback(redis.aclose)
            stack.callback(lifecycle.shutdown)

            if event.is_set():
                return
            # Capture desired state before the DB read. If web commits/signals
            # concurrently, the later generation cannot be acknowledged with
            # the older settings snapshot; the monitor observes and reloads it.
            async with asyncio.timeout(WORKER_CONTROL_TIMEOUT_SECONDS):
                generation = await _await_or_stop(
                    ensure_schedule_generation(redis), event
                )
            if generation is _STOP_REQUESTED:
                return
            async with asyncio.timeout(WORKER_RELOAD_ATTEMPT_TIMEOUT_SECONDS):
                settings = await _await_or_stop(
                    load_worker_settings(session_factory), event
                )
            if settings is _STOP_REQUESTED:
                return
            lifecycle.prepare(settings)
            seeded = await _await_or_stop(lifecycle.seed_heartbeats(), event)
            if seeded is _STOP_REQUESTED:
                return
            lifecycle.start()
            if event.is_set():
                return
            async with asyncio.timeout(WORKER_CONTROL_TIMEOUT_SECONDS):
                published = await _await_or_stop(
                    publish_worker_manifest(
                        redis,
                        generation=generation,
                        heartbeat_job_ids=lifecycle.heartbeat_job_ids,
                    ),
                    event,
                )
                if published is _STOP_REQUESTED:
                    return
                if published is not None:
                    mark_worker_ready(ready_path)
            await monitor_schedule_reloads(
                lifecycle=lifecycle,
                session_factory=session_factory,
                redis=redis,
                applied_settings=settings,
                stop_event=event,
                on_manifest_published=lambda: mark_worker_ready(ready_path),
            )
    finally:
        clear_worker_ready(ready_path)
        loop = asyncio.get_running_loop()
        for signum in installed_handlers:
            with suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signum)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
