"""Standalone APScheduler entry point for the split-runtime transition.

Run it only with ``VITALS_PROCESS_MODE=worker``. Compose deliberately continues
to use the default combined process until the database-role and deployment
cutover is implemented separately.
"""

from __future__ import annotations

import asyncio
import signal
from contextlib import AsyncExitStack, suppress

from redis.asyncio import Redis

from vitals.config import load_config
from vitals.database import create_session_factory
from vitals.process_mode import ProcessMode, load_process_mode
from vitals.runtime_env import require_runtime_environment_isolation
from vitals.scheduler.lifecycle import WorkerLifecycle, load_worker_settings


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


async def run_worker(*, stop_event: asyncio.Event | None = None) -> None:
    """Run the standalone scheduler until a signal or supplied event stops it."""

    mode = load_process_mode()
    if mode is not ProcessMode.WORKER:
        raise RuntimeError(
            "the standalone worker requires VITALS_PROCESS_MODE=worker"
        )

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

            settings = await load_worker_settings(session_factory)
            lifecycle.prepare(settings)
            await lifecycle.seed_heartbeats()
            lifecycle.start()
            await event.wait()
    finally:
        loop = asyncio.get_running_loop()
        for signum in installed_handlers:
            with suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(signum)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
