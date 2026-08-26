"""Standalone worker readiness and liveness health contract."""

from __future__ import annotations

import os
import time

import pytest

from vitals.scheduler.control import publish_worker_manifest, request_schedule_reload
from vitals.worker_health import (
    WorkerHealthError,
    check_worker_health,
    clear_worker_ready,
    mark_worker_ready,
)


class _Session:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, statement):
        assert str(statement) == "SELECT 1"
        if self.fail:
            raise ConnectionError("synthetic database failure")


class _SessionFactory:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def __call__(self):
        return _Session(fail=self.fail)


async def _healthy_control_state(redis):
    generation = await request_schedule_reload(redis)
    manifest = await publish_worker_manifest(
        redis,
        generation=generation,
        heartbeat_job_ids=["keepalive", "daily_brief"],
    )
    now = str(int(time.time()))
    await redis.set("scheduler:last_run:keepalive", now)
    await redis.set("scheduler:last_run:daily_brief", now)
    return generation, manifest


async def test_worker_health_accepts_local_process_and_fresh_control_state(
    redis,
    tmp_path,
):
    ready_path = tmp_path / "ready"
    mark_worker_ready(ready_path)
    generation, manifest = await _healthy_control_state(redis)

    snapshot = await check_worker_health(
        session_factory=_SessionFactory(),
        redis=redis,
        ready_path=ready_path,
        now=float(manifest.published_at),
    )

    assert snapshot.generation == generation
    assert snapshot.manifest_age_seconds == 0
    assert snapshot.heartbeat_job_count == 2
    assert ready_path.read_text() == f"v1:{os.getpid()}\n"


async def test_worker_health_rejects_missing_local_readiness(redis, tmp_path):
    await _healthy_control_state(redis)

    with pytest.raises(WorkerHealthError, match="readiness marker"):
        await check_worker_health(
            session_factory=_SessionFactory(),
            redis=redis,
            ready_path=tmp_path / "missing",
        )


async def test_worker_health_rejects_database_failure(redis, tmp_path):
    ready_path = tmp_path / "ready"
    mark_worker_ready(ready_path)
    await _healthy_control_state(redis)

    with pytest.raises(WorkerHealthError, match="database check failed"):
        await check_worker_health(
            session_factory=_SessionFactory(fail=True),
            redis=redis,
            ready_path=ready_path,
        )


async def test_worker_health_rejects_pending_generation(redis, tmp_path):
    ready_path = tmp_path / "ready"
    mark_worker_ready(ready_path)
    await _healthy_control_state(redis)
    await request_schedule_reload(redis)

    with pytest.raises(WorkerHealthError, match="reload is pending"):
        await check_worker_health(
            session_factory=_SessionFactory(),
            redis=redis,
            ready_path=ready_path,
        )


async def test_worker_health_rejects_expired_manifest_lease(redis, tmp_path):
    from vitals.scheduler.control import WORKER_MANIFEST_TTL_SECONDS

    ready_path = tmp_path / "ready"
    mark_worker_ready(ready_path)
    _generation, manifest = await _healthy_control_state(redis)

    with pytest.raises(WorkerHealthError, match="manifest lease is stale"):
        await check_worker_health(
            session_factory=_SessionFactory(),
            redis=redis,
            ready_path=ready_path,
            now=manifest.published_at + WORKER_MANIFEST_TTL_SECONDS + 1,
        )


async def test_worker_health_rejects_stale_heartbeat(redis, tmp_path):
    ready_path = tmp_path / "ready"
    mark_worker_ready(ready_path)
    await _healthy_control_state(redis)
    await redis.delete("scheduler:last_run:daily_brief")

    with pytest.raises(WorkerHealthError, match="heartbeat is stale: daily_brief"):
        await check_worker_health(
            session_factory=_SessionFactory(),
            redis=redis,
            ready_path=ready_path,
        )


def test_ready_marker_clear_removes_stale_process_marker(tmp_path):
    ready_path = tmp_path / "ready"
    mark_worker_ready(ready_path)

    clear_worker_ready(ready_path)

    assert not ready_path.exists()
