"""Process-role and scheduler lifecycle contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from vitals.process_mode import ProcessMode, load_process_mode
from vitals.scheduler.lifecycle import WorkerLifecycle, load_worker_settings


def test_process_mode_defaults_to_combined_and_rejects_unknown_values():
    assert load_process_mode({}) is ProcessMode.COMBINED
    assert load_process_mode({"VITALS_PROCESS_MODE": " WEB "}) is ProcessMode.WEB

    with pytest.raises(RuntimeError, match="combined, web, worker"):
        load_process_mode({"VITALS_PROCESS_MODE": "api"})


async def test_worker_lifecycle_preserves_startup_and_shutdown_order(monkeypatch):
    from vitals.scheduler import jobs, scheduler
    from vitals.services import conflict_registrations

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
        conflict_registrations,
        "register_all_resolvers",
        lambda: calls.append("resolvers"),
    )

    async def seed_probe(redis_client):
        calls.append(("seed", redis_client))

    monkeypatch.setattr(scheduler, "seed_heartbeats", seed_probe)
    monkeypatch.setattr(
        scheduler,
        "setup_scheduler",
        lambda factory, redis_client, *, timezone: (
            calls.append(("setup", factory, redis_client, timezone))
            or scheduler_probe
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
    lifecycle.shutdown()

    assert calls == [
        "resolvers",
        ("register", {"daily_digest_time": "08:00"}),
        ("seed", redis),
        ("setup", session_factory, redis, "Asia/Almaty"),
        "start",
        "shutdown",
    ]


async def test_prepared_web_lifecycle_never_starts_scheduler(monkeypatch):
    from vitals.scheduler import jobs
    from vitals.services import conflict_registrations

    registrations = []
    monkeypatch.setattr(jobs, "register_all_jobs", registrations.append)
    monkeypatch.setattr(
        conflict_registrations,
        "register_all_resolvers",
        lambda: registrations.append("resolvers"),
    )
    lifecycle = WorkerLifecycle(
        session_factory=object(),
        redis=None,
        timezone="UTC",
    )

    lifecycle.prepare(None)
    lifecycle.shutdown()

    assert registrations == ["resolvers", None]
    assert lifecycle.scheduler is None


async def test_worker_settings_use_explicit_platform_scope(monkeypatch):
    from vitals.scheduler import lifecycle as lifecycle_module
    from vitals.services.proactive import prefs

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
        prefs,
        "resolve_legacy_preferences_scope",
        resolve_scope_probe,
    )
    monkeypatch.setattr(
        prefs,
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


async def test_standalone_worker_runs_and_releases_owned_resources(monkeypatch):
    from vitals import worker

    calls: list[object] = []

    class EngineProbe:
        async def dispose(self):
            calls.append("dispose_engine")

    class RedisProbe:
        async def aclose(self):
            calls.append("close_redis")

    class LifecycleProbe:
        def __init__(self, **kwargs):
            calls.append(("lifecycle", kwargs))

        def prepare(self, settings):
            calls.append(("prepare", settings))

        async def seed_heartbeats(self):
            calls.append("seed")

        def start(self):
            calls.append("start")

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
    monkeypatch.setattr(worker, "WorkerLifecycle", LifecycleProbe)
    stop_event = asyncio.Event()
    stop_event.set()

    await worker.run_worker(stop_event=stop_event)

    assert calls[1:] == [
        ("prepare", {"digest_enabled": True}),
        "seed",
        "start",
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
):
    from vitals import worker

    monkeypatch.setattr(worker, "load_process_mode", lambda: ProcessMode.WORKER)

    def reject_runtime():
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
    from web import main as web_main

    monkeypatch.setattr(
        web_main,
        "load_process_mode",
        lambda: ProcessMode.WORKER,
    )
    monkeypatch.setattr(
        web_main,
        "get_session_factory",
        lambda: pytest.fail("web resources must not load in worker mode"),
    )

    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(RuntimeError, match="vitals.worker entry point"):
        async with web_main.lifespan(app):
            pass
