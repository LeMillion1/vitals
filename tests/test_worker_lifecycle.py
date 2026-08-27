"""Process-role and scheduler lifecycle contracts."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from vitals.process_mode import ProcessMode, load_process_mode
from vitals.scheduler.lifecycle import WorkerLifecycle, load_worker_settings


@pytest.fixture(autouse=True)
def _isolated_worker_ready_file(tmp_path, monkeypatch):
    from vitals import worker

    ready_path = tmp_path / "worker-ready"
    monkeypatch.setattr(worker, "worker_ready_file", lambda: ready_path)
    return ready_path


def test_process_mode_defaults_to_combined_and_rejects_unknown_values():
    assert load_process_mode({}) is ProcessMode.COMBINED
    assert load_process_mode({"VITALS_PROCESS_MODE": " WEB "}) is ProcessMode.WEB

    with pytest.raises(RuntimeError, match="combined, web, worker"):
        load_process_mode({"VITALS_PROCESS_MODE": "api"})


async def test_worker_lifecycle_preserves_startup_and_shutdown_order(monkeypatch):
    from vitals.scheduler import jobs, scheduler
    from vitals.services.conflicts import registrations

    calls: list[object] = []

    class SchedulerProbe:
        def start(self):
            calls.append("start")

        def shutdown(self):
            calls.append("shutdown")

    redis = object()
    session_factory = object()
    scheduler_probe = SchedulerProbe()
    monkeypatch.setattr(
        jobs,
        "register_all_jobs",
        lambda settings: calls.append(("register", settings)),
    )
    monkeypatch.setattr(
        registrations,
        "register_all_resolvers",
        lambda: calls.append("resolvers"),
    )

    async def seed_probe(redis_client, *, job_ids=None):
        calls.append(("seed", redis_client, job_ids))

    monkeypatch.setattr(scheduler, "seed_heartbeats", seed_probe)
    heartbeat_sets = iter(
        [
            ["keepalive", "existing_job"],
            ["keepalive", "existing_job", "new_job"],
        ]
    )
    monkeypatch.setattr(
        scheduler,
        "heartbeat_job_ids",
        lambda: next(heartbeat_sets),
    )
    monkeypatch.setattr(
        scheduler,
        "setup_scheduler",
        lambda factory, redis_client, *, timezone: (
            calls.append(("setup", factory, redis_client, timezone))
            or scheduler_probe
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "apply_registry",
        lambda running, factory, redis_client: calls.append(
            ("apply", running, factory, redis_client)
        ),
    )

    lifecycle = WorkerLifecycle(
        session_factory=session_factory,
        redis=redis,
        timezone="Asia/Almaty",
    )
    lifecycle.prepare({"daily_digest_time": "08:00"})
    await lifecycle.seed_heartbeats()
    assert lifecycle.start() is scheduler_probe
    lifecycle.reload({"daily_digest_time": "09:00"})
    await lifecycle.seed_pending_heartbeats()
    lifecycle.shutdown()

    assert calls == [
        "resolvers",
        ("register", {"daily_digest_time": "08:00"}),
        ("seed", redis, None),
        ("setup", session_factory, redis, "Asia/Almaty"),
        "start",
        ("register", {"daily_digest_time": "09:00"}),
        ("apply", scheduler_probe, session_factory, redis),
        ("seed", redis, ["new_job"]),
        "shutdown",
    ]


async def test_worker_reload_does_not_refresh_existing_heartbeats(monkeypatch):
    from vitals.scheduler import jobs, scheduler

    monkeypatch.setattr(jobs, "register_all_jobs", lambda settings: None)
    monkeypatch.setattr(
        scheduler,
        "heartbeat_job_ids",
        lambda: ["keepalive", "weekly_digest"],
    )
    monkeypatch.setattr(
        scheduler,
        "apply_registry",
        lambda running, factory, redis_client: None,
    )

    async def forbidden_seed(redis_client, *, job_ids=None):
        pytest.fail("an existing heartbeat must keep its real age during reload")

    monkeypatch.setattr(scheduler, "seed_heartbeats", forbidden_seed)
    lifecycle = WorkerLifecycle(
        session_factory=object(),
        redis=object(),
        timezone="UTC",
    )
    lifecycle._scheduler = object()
    lifecycle._applied_heartbeat_job_ids = frozenset(
        {"keepalive", "weekly_digest"}
    )

    lifecycle.reload({"brief_time": "09:00"})
    await lifecycle.seed_pending_heartbeats()


async def test_pending_heartbeat_seed_is_retained_until_confirmed(monkeypatch):
    from vitals.scheduler import jobs, scheduler

    heartbeat_sets = iter(
        [
            ["keepalive", "existing_job"],
            ["keepalive", "existing_job", "new_job"],
        ]
    )
    monkeypatch.setattr(scheduler, "heartbeat_job_ids", lambda: next(heartbeat_sets))
    monkeypatch.setattr(jobs, "register_all_jobs", lambda settings: None)
    monkeypatch.setattr(
        scheduler,
        "apply_registry",
        lambda running, factory, redis_client: None,
    )
    attempts = 0

    async def flaky_seed(redis_client, *, job_ids=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise scheduler.SchedulerHeartbeatSeedError("synthetic seed failure")
        assert job_ids == ["new_job"]

    monkeypatch.setattr(scheduler, "seed_heartbeats", flaky_seed)
    lifecycle = WorkerLifecycle(
        session_factory=object(),
        redis=object(),
        timezone="UTC",
    )
    lifecycle._scheduler = object()
    lifecycle._applied_heartbeat_job_ids = frozenset(next(heartbeat_sets))
    lifecycle.reload({"brief_time": "09:00"})

    with pytest.raises(scheduler.SchedulerHeartbeatSeedError):
        await lifecycle.seed_pending_heartbeats()
    assert lifecycle._pending_heartbeat_seed_ids == {"new_job"}

    await lifecycle.seed_pending_heartbeats()
    assert lifecycle._pending_heartbeat_seed_ids == set()
    assert attempts == 2


async def test_prepared_web_lifecycle_never_starts_scheduler(monkeypatch):
    from vitals.scheduler import jobs
    from vitals.services.conflicts import registrations

    registered_jobs = []
    monkeypatch.setattr(jobs, "register_all_jobs", registered_jobs.append)
    monkeypatch.setattr(
        registrations,
        "register_all_resolvers",
        lambda: registered_jobs.append("resolvers"),
    )
    lifecycle = WorkerLifecycle(
        session_factory=object(),
        redis=None,
        timezone="UTC",
    )

    lifecycle.prepare(None)
    lifecycle.shutdown()

    assert registered_jobs == ["resolvers", None]
    assert lifecycle.scheduler is None


async def test_worker_settings_use_explicit_platform_scope(monkeypatch):
    from vitals.scheduler import lifecycle as lifecycle_module
    from vitals.services.proactive.preferences import queries as preference_queries

    calls = []

    class SessionProbe:
        async def rollback(self):
            calls.append("rollback")

    class SessionContext:
        async def __aenter__(self):
            self.session = SessionProbe()
            return self.session

        async def __aexit__(self, *args):
            del args

    async def enter_scope_probe(session):
        calls.append(("platform", session))

    async def resolve_scope_probe(session, *, actor_username):
        calls.append(("resolve", session, actor_username))
        return "scope"

    async def get_bundle_probe(session, *, scope):
        calls.append(("bundle", session, scope))
        return SimpleNamespace(as_flat_dict=lambda: {"digest_enabled": True})

    monkeypatch.setattr(
        lifecycle_module,
        "enter_platform_scope",
        enter_scope_probe,
    )
    monkeypatch.setattr(
        preference_queries,
        "resolve_legacy_preferences_scope",
        resolve_scope_probe,
    )
    monkeypatch.setattr(
        preference_queries,
        "get_exact_one_preferences_bundle",
        get_bundle_probe,
    )

    settings = await load_worker_settings(SessionContext)

    assert settings == {"digest_enabled": True}
    assert calls[0][0] == "platform"
    assert calls[1][0] == "resolve"
    assert calls[0][1] is calls[1][1]
    assert calls[2] == ("bundle", calls[0][1], "scope")
    assert calls[3] == "rollback"


async def test_standalone_worker_runs_and_releases_owned_resources(
    monkeypatch,
    _isolated_worker_ready_file: Path,
):
    from vitals import worker

    calls: list[object] = []

    class EngineProbe:
        async def dispose(self):
            calls.append("dispose_engine")

    class RedisProbe:
        async def aclose(self):
            calls.append("close_redis")

    class LifecycleProbe:
        heartbeat_job_ids = ("keepalive",)

        def __init__(self, **kwargs):
            calls.append(("lifecycle", kwargs))

        def prepare(self, settings):
            calls.append(("prepare", settings))

        async def seed_heartbeats(self):
            calls.append("seed")

        def start(self):
            calls.append("start")

        async def seed_pending_heartbeats(self):
            pass

        def shutdown(self):
            calls.append("shutdown")

    engine = EngineProbe()
    redis = RedisProbe()
    session_factory = SimpleNamespace(kw={"bind": engine})
    config = SimpleNamespace(redis_url="redis://synthetic", timezone="UTC")
    monkeypatch.setattr(worker, "load_process_mode", lambda: ProcessMode.WORKER)
    monkeypatch.setattr(worker, "load_config", lambda: config)
    monkeypatch.setattr(
        worker,
        "create_session_factory",
        lambda loaded_config: session_factory,
    )
    monkeypatch.setattr(
        worker.Redis,
        "from_url",
        lambda *args, **kwargs: redis,
    )
    monkeypatch.setattr(
        worker,
        "load_worker_settings",
        lambda factory: asyncio.sleep(0, result={"digest_enabled": True}),
    )
    monkeypatch.setattr(
        worker,
        "ensure_schedule_generation",
        lambda redis_client: asyncio.sleep(0, result="a" * 32),
    )

    stop_event = asyncio.Event()

    async def publish_probe(redis_client, **kwargs):
        calls.append(("manifest", redis_client, kwargs))
        if sum(call[0] == "manifest" for call in calls if isinstance(call, tuple)) == 2:
            assert _isolated_worker_ready_file.read_text() == f"v1:{os.getpid()}\n"
            stop_event.set()
        return object()

    monkeypatch.setattr(worker, "publish_worker_manifest", publish_probe)
    monkeypatch.setattr(worker, "WorkerLifecycle", LifecycleProbe)

    await worker.run_worker(stop_event=stop_event)

    assert calls[1:] == [
        ("prepare", {"digest_enabled": True}),
        "seed",
        "start",
        (
            "manifest",
            redis,
            {
                "generation": "a" * 32,
                "heartbeat_job_ids": ("keepalive",),
            },
        ),
        (
            "manifest",
            redis,
            {
                "generation": "a" * 32,
                "heartbeat_job_ids": ("keepalive",),
            },
        ),
        "shutdown",
        "close_redis",
        "dispose_engine",
    ]
    assert calls[0][0] == "lifecycle"
    assert calls[0][1] == {
        "session_factory": session_factory,
        "redis": redis,
        "timezone": "UTC",
    }
    assert not _isolated_worker_ready_file.exists()


async def test_pre_set_worker_stop_never_starts_or_publishes_readiness(monkeypatch):
    from vitals import worker

    calls = []

    class EngineProbe:
        async def dispose(self):
            calls.append("dispose_engine")

    class RedisProbe:
        async def aclose(self):
            calls.append("close_redis")

    class LifecycleProbe:
        def __init__(self, **_kwargs):
            calls.append("lifecycle")

        def shutdown(self):
            calls.append("shutdown")

    engine = EngineProbe()
    redis = RedisProbe()
    monkeypatch.setattr(worker, "load_process_mode", lambda: ProcessMode.WORKER)
    monkeypatch.setattr(worker, "require_runtime_environment_isolation", lambda: None)
    monkeypatch.setattr(
        worker,
        "load_config",
        lambda: SimpleNamespace(redis_url="redis://synthetic", timezone="UTC"),
    )
    monkeypatch.setattr(
        worker,
        "create_session_factory",
        lambda _config: SimpleNamespace(kw={"bind": engine}),
    )
    monkeypatch.setattr(worker.Redis, "from_url", lambda *_args, **_kwargs: redis)
    monkeypatch.setattr(worker, "WorkerLifecycle", LifecycleProbe)
    monkeypatch.setattr(
        worker,
        "ensure_schedule_generation",
        lambda _redis: pytest.fail("stopped worker must not enter control startup"),
    )
    monkeypatch.setattr(
        worker,
        "publish_worker_manifest",
        lambda *_args, **_kwargs: pytest.fail("stopped worker must not publish"),
    )
    stop_event = asyncio.Event()
    stop_event.set()

    await worker.run_worker(stop_event=stop_event)

    assert calls == ["lifecycle", "shutdown", "close_redis", "dispose_engine"]


async def test_worker_startup_seed_failure_never_publishes_readiness(monkeypatch):
    from vitals import worker

    calls = []

    class EngineProbe:
        async def dispose(self):
            calls.append("dispose_engine")

    class RedisProbe:
        async def aclose(self):
            calls.append("close_redis")

    class LifecycleProbe:
        def __init__(self, **_kwargs):
            pass

        def prepare(self, settings):
            calls.append(("prepare", settings))

        async def seed_heartbeats(self):
            calls.append("seed_failed")
            raise RuntimeError("synthetic readiness seed failure")

        def start(self):
            pytest.fail("scheduler must not start after failed readiness seed")

        def shutdown(self):
            calls.append("shutdown")

    engine = EngineProbe()
    redis = RedisProbe()
    monkeypatch.setattr(worker, "load_process_mode", lambda: ProcessMode.WORKER)
    monkeypatch.setattr(worker, "require_runtime_environment_isolation", lambda: None)
    monkeypatch.setattr(
        worker,
        "load_config",
        lambda: SimpleNamespace(redis_url="redis://synthetic", timezone="UTC"),
    )
    monkeypatch.setattr(
        worker,
        "create_session_factory",
        lambda _config: SimpleNamespace(kw={"bind": engine}),
    )
    monkeypatch.setattr(worker.Redis, "from_url", lambda *_args, **_kwargs: redis)
    monkeypatch.setattr(worker, "WorkerLifecycle", LifecycleProbe)
    monkeypatch.setattr(
        worker,
        "ensure_schedule_generation",
        lambda _redis: asyncio.sleep(0, result="a" * 32),
    )
    monkeypatch.setattr(
        worker,
        "load_worker_settings",
        lambda _factory: asyncio.sleep(0, result={"brief_time": "09:00"}),
    )
    monkeypatch.setattr(
        worker,
        "publish_worker_manifest",
        lambda *_args, **_kwargs: pytest.fail(
            "failed readiness seed must not publish a manifest"
        ),
    )

    with pytest.raises(RuntimeError, match="readiness seed failure"):
        await worker.run_worker(stop_event=asyncio.Event())

    assert calls == [
        ("prepare", {"brief_time": "09:00"}),
        "seed_failed",
        "shutdown",
        "close_redis",
        "dispose_engine",
    ]


async def test_worker_poll_observes_postgres_change_without_redis_notification(
    monkeypatch,
):
    from vitals import worker

    calls = []
    stop_event = asyncio.Event()

    class LifecycleProbe:
        heartbeat_job_ids = ("daily_brief", "keepalive")

        def reload(self, settings):
            calls.append(("reload", settings))

        async def seed_pending_heartbeats(self):
            pass

    monkeypatch.setattr(
        worker,
        "ensure_schedule_generation",
        lambda redis_client: asyncio.sleep(0, result="a" * 32),
    )
    monkeypatch.setattr(
        worker,
        "load_worker_settings",
        lambda factory: asyncio.sleep(0, result={"brief_time": "09:00"}),
    )

    async def publish_probe(redis_client, **kwargs):
        calls.append(("ack", kwargs))
        return object()

    def ready_probe():
        calls.append("ready")
        stop_event.set()

    monkeypatch.setattr(worker, "publish_worker_manifest", publish_probe)

    await worker.monitor_schedule_reloads(
        lifecycle=LifecycleProbe(),
        session_factory=object(),
        redis=object(),
        applied_settings={"brief_time": "08:00"},
        stop_event=stop_event,
        poll_seconds=0.01,
        on_manifest_published=ready_probe,
    )

    assert calls == [
        ("reload", {"brief_time": "09:00"}),
        (
            "ack",
            {
                "generation": "a" * 32,
                "heartbeat_job_ids": ("daily_brief", "keepalive"),
            },
        ),
        "ready",
    ]


async def test_worker_retries_manifest_without_reapplying_schedule(monkeypatch):
    from vitals import worker

    generation = "a" * 32
    stop_event = asyncio.Event()
    reloads = []
    publishes = 0

    class LifecycleProbe:
        heartbeat_job_ids = ("keepalive",)

        def reload(self, settings):
            reloads.append(settings)

        async def seed_pending_heartbeats(self):
            pass

    monkeypatch.setattr(
        worker,
        "ensure_schedule_generation",
        lambda redis_client: asyncio.sleep(0, result=generation),
    )
    monkeypatch.setattr(
        worker,
        "load_worker_settings",
        lambda factory: asyncio.sleep(0, result={"brief_time": "09:00"}),
    )

    async def flaky_publish(redis_client, **kwargs):
        nonlocal publishes
        publishes += 1
        if publishes == 1:
            raise ConnectionError("synthetic Redis interruption")
        stop_event.set()
        return object()

    monkeypatch.setattr(worker, "publish_worker_manifest", flaky_publish)

    await worker.monitor_schedule_reloads(
        lifecycle=LifecycleProbe(),
        session_factory=object(),
        redis=object(),
        applied_settings={"brief_time": "08:00"},
        stop_event=stop_event,
        poll_seconds=0.01,
    )

    assert reloads == [{"brief_time": "09:00"}]
    assert publishes == 2


async def test_worker_retries_heartbeat_seed_without_reapplying_schedule(monkeypatch):
    from vitals import worker

    generation = "a" * 32
    stop_event = asyncio.Event()
    reloads = []
    seed_attempts = 0

    class LifecycleProbe:
        heartbeat_job_ids = ("daily_brief", "keepalive")

        def reload(self, settings):
            reloads.append(settings)

        async def seed_pending_heartbeats(self):
            nonlocal seed_attempts
            seed_attempts += 1
            if seed_attempts == 1:
                raise ConnectionError("synthetic heartbeat seed interruption")

    monkeypatch.setattr(
        worker,
        "ensure_schedule_generation",
        lambda redis_client: asyncio.sleep(0, result=generation),
    )
    monkeypatch.setattr(
        worker,
        "load_worker_settings",
        lambda factory: asyncio.sleep(0, result={"brief_time": "09:00"}),
    )

    async def publish_probe(redis_client, **kwargs):
        stop_event.set()
        return object()

    monkeypatch.setattr(worker, "publish_worker_manifest", publish_probe)

    await worker.monitor_schedule_reloads(
        lifecycle=LifecycleProbe(),
        session_factory=object(),
        redis=object(),
        applied_settings={"brief_time": "08:00"},
        stop_event=stop_event,
        poll_seconds=0.01,
    )

    assert reloads == [{"brief_time": "09:00"}]
    assert seed_attempts == 2


async def test_worker_shutdown_interrupts_reload_poll_wait(monkeypatch):
    from vitals import worker

    generation = "a" * 32
    stop_event = asyncio.Event()
    monkeypatch.setattr(
        worker,
        "ensure_schedule_generation",
        lambda redis_client: asyncio.sleep(0, result=generation),
    )
    monkeypatch.setattr(
        worker,
        "load_worker_settings",
        lambda factory: asyncio.sleep(0, result={"brief_time": "09:00"}),
    )
    monkeypatch.setattr(
        worker,
        "publish_worker_manifest",
        lambda redis_client, **kwargs: asyncio.sleep(0, result=object()),
    )

    class LifecycleProbe:
        heartbeat_job_ids = ("keepalive",)

        async def seed_pending_heartbeats(self):
            pass

    task = asyncio.create_task(
        worker.monitor_schedule_reloads(
            lifecycle=LifecycleProbe(),
            session_factory=object(),
            redis=object(),
            applied_settings={"brief_time": "09:00"},
            stop_event=stop_event,
            poll_seconds=60.0,
        )
    )
    await asyncio.sleep(0)
    stop_event.set()

    await asyncio.wait_for(task, timeout=0.2)


async def test_worker_shutdown_cancels_inflight_settings_poll(monkeypatch):
    from vitals import worker

    generation = "a" * 32
    stop_event = asyncio.Event()
    load_started = asyncio.Event()
    load_cancelled = asyncio.Event()

    async def blocked_settings_load(factory):
        load_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            load_cancelled.set()

    monkeypatch.setattr(
        worker,
        "ensure_schedule_generation",
        lambda redis_client: asyncio.sleep(0, result=generation),
    )
    monkeypatch.setattr(worker, "load_worker_settings", blocked_settings_load)

    task = asyncio.create_task(
        worker.monitor_schedule_reloads(
            lifecycle=object(),
            session_factory=object(),
            redis=object(),
            applied_settings=None,
            stop_event=stop_event,
            poll_seconds=60.0,
        )
    )
    await asyncio.wait_for(load_started.wait(), timeout=0.2)
    stop_event.set()

    await asyncio.wait_for(task, timeout=0.2)
    assert load_cancelled.is_set()


async def test_worker_timeout_awaits_inflight_cancellation_cleanup():
    from vitals import worker

    cleanup_done = asyncio.Event()

    async def blocked_operation():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleanup_done.set()

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await worker._await_or_stop(blocked_operation(), asyncio.Event())

    assert cleanup_done.is_set()


async def test_worker_entrypoint_rejects_combined_mode_before_loading_resources(
    monkeypatch,
):
    from vitals import worker

    monkeypatch.setattr(
        worker,
        "load_process_mode",
        lambda: ProcessMode.COMBINED,
    )
    monkeypatch.setattr(
        worker,
        "load_config",
        lambda: pytest.fail("worker resources must not load in combined mode"),
    )

    with pytest.raises(RuntimeError, match="VITALS_PROCESS_MODE=worker"):
        await worker.run_worker()


async def test_worker_enforces_runtime_isolation_before_loading_resources(
    monkeypatch,
    _isolated_worker_ready_file: Path,
):
    from vitals import worker

    monkeypatch.setattr(worker, "load_process_mode", lambda: ProcessMode.WORKER)
    _isolated_worker_ready_file.write_text("v1:999999\n")

    def reject_runtime():
        assert not _isolated_worker_ready_file.exists()
        raise RuntimeError("runtime environment isolation failed: synthetic")

    monkeypatch.setattr(
        worker,
        "require_runtime_environment_isolation",
        reject_runtime,
    )
    monkeypatch.setattr(
        worker,
        "load_config",
        lambda: pytest.fail("resources must not load before the preflight"),
    )

    with pytest.raises(RuntimeError, match="runtime environment isolation failed"):
        await worker.run_worker()


async def test_worker_cleanup_continues_after_scheduler_shutdown_failure(
    monkeypatch,
):
    from vitals import worker

    calls: list[str] = []

    class EngineProbe:
        async def dispose(self):
            calls.append("dispose_engine")

    class RedisProbe:
        async def aclose(self):
            calls.append("close_redis")

    class LifecycleProbe:
        def __init__(self, **_kwargs):
            pass

        def prepare(self, _settings):
            pass

        async def seed_heartbeats(self):
            pass

        def start(self):
            pass

        def shutdown(self):
            calls.append("shutdown")
            raise RuntimeError("synthetic shutdown failure")

    engine = EngineProbe()
    redis = RedisProbe()
    monkeypatch.setattr(worker, "load_process_mode", lambda: ProcessMode.WORKER)
    monkeypatch.setattr(
        worker,
        "require_runtime_environment_isolation",
        lambda: None,
    )
    monkeypatch.setattr(
        worker,
        "load_config",
        lambda: SimpleNamespace(redis_url="redis://synthetic", timezone="UTC"),
    )
    monkeypatch.setattr(
        worker,
        "create_session_factory",
        lambda _config: SimpleNamespace(kw={"bind": engine}),
    )
    monkeypatch.setattr(worker.Redis, "from_url", lambda *_args, **_kwargs: redis)
    monkeypatch.setattr(
        worker,
        "load_worker_settings",
        lambda _factory: asyncio.sleep(0, result=None),
    )
    monkeypatch.setattr(worker, "WorkerLifecycle", LifecycleProbe)
    stop_event = asyncio.Event()
    stop_event.set()

    with pytest.raises(RuntimeError, match="synthetic shutdown failure"):
        await worker.run_worker(stop_event=stop_event)

    assert calls == ["shutdown", "close_redis", "dispose_engine"]


async def test_web_entrypoint_rejects_worker_mode_before_loading_resources(
    monkeypatch,
):
    from web import app_lifecycle

    monkeypatch.setattr(
        app_lifecycle,
        "load_process_mode",
        lambda: ProcessMode.WORKER,
    )
    monkeypatch.setattr(
        app_lifecycle,
        "get_session_factory",
        lambda: pytest.fail("web resources must not load in worker mode"),
    )

    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(RuntimeError, match="vitals.worker entry point"):
        async with app_lifecycle.lifespan(app):
            pass
