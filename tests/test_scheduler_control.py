"""Cross-process scheduler generation and manifest contracts."""

from __future__ import annotations

import json

import pytest

from vitals.scheduler.control import (
    SCHEDULE_GENERATION_KEY,
    WORKER_MANIFEST_TTL_SECONDS,
    WORKER_MANIFEST_KEY,
    SchedulerControlError,
    ensure_schedule_generation,
    publish_worker_manifest,
    read_schedule_generation,
    read_worker_manifest,
    request_schedule_reload,
)


async def test_reload_signal_is_durable_opaque_and_contains_no_settings(redis):
    generation = await request_schedule_reload(redis)

    assert len(generation) == 32
    assert await read_schedule_generation(redis) == generation
    assert await redis.get(SCHEDULE_GENERATION_KEY) == generation
    assert "brief_time" not in generation
    assert "garmin" not in generation


async def test_worker_manifest_round_trip_contains_only_liveness_metadata(redis):
    generation = await ensure_schedule_generation(redis)
    manifest = await publish_worker_manifest(
        redis,
        generation=generation,
        heartbeat_job_ids=["keepalive", "daily_brief"],
    )

    assert manifest is not None
    assert await read_worker_manifest(redis) == manifest
    raw = await redis.get(WORKER_MANIFEST_KEY)
    payload = json.loads(raw)
    assert set(payload) == {
        "version",
        "generation",
        "published_at",
        "heartbeat_job_ids",
    }
    assert payload["heartbeat_job_ids"] == ["daily_brief", "keepalive"]
    assert "heartbeat_budgets" not in raw
    assert "settings" not in raw
    assert "09:00" not in raw
    assert 0 < await redis.ttl(WORKER_MANIFEST_KEY) <= WORKER_MANIFEST_TTL_SECONDS


async def test_existing_generation_wins_worker_startup_race(redis):
    signaled = await request_schedule_reload(redis)

    assert await ensure_schedule_generation(redis) == signaled


async def test_manifest_reader_fails_closed_on_malformed_state(redis):
    await redis.set(
        WORKER_MANIFEST_KEY,
        '{"version":1,"generation":"not-opaque","published_at":1,'
        '"heartbeat_job_ids":["keepalive"]}',
    )

    with pytest.raises(SchedulerControlError, match="generation"):
        await read_worker_manifest(redis)


async def test_stale_worker_cannot_overwrite_a_newer_generation_ack(redis):
    old_generation = await ensure_schedule_generation(redis)
    old_manifest = await publish_worker_manifest(
        redis,
        generation=old_generation,
        heartbeat_job_ids=["keepalive"],
    )
    new_generation = await request_schedule_reload(redis)

    stale_publish = await publish_worker_manifest(
        redis,
        generation=old_generation,
        heartbeat_job_ids=["keepalive", "daily_brief"],
    )

    assert new_generation != old_generation
    assert stale_publish is None
    assert await read_worker_manifest(redis) == old_manifest


async def test_same_generation_worker_cannot_erase_health_obligations(redis):
    generation = await ensure_schedule_generation(redis)
    await publish_worker_manifest(
        redis,
        generation=generation,
        heartbeat_job_ids=["keepalive", "daily_brief"],
    )

    subset_publish = await publish_worker_manifest(
        redis,
        generation=generation,
        heartbeat_job_ids=["keepalive"],
    )

    assert subset_publish is not None
    assert subset_publish.heartbeat_job_ids == ("daily_brief", "keepalive")
    assert (await read_worker_manifest(redis)).heartbeat_job_ids == (
        "daily_brief",
        "keepalive",
    )


def test_registry_trigger_validation_precedes_live_scheduler_mutation():
    from vitals.scheduler import jobs, scheduler as scheduler_module

    jobs.register_all_jobs()
    running = scheduler_module.setup_scheduler(
        lambda: None,
        None,
        timezone="UTC",
    )
    before = {job.id: str(job.trigger) for job in running.get_jobs()}
    invalid = scheduler_module._registry["weekly_digest"]
    invalid.trigger_kwargs = {
        **invalid.trigger_kwargs,
        "hour": "*/24",
    }

    with pytest.raises(ValueError, match=r"step value \(24\)"):
        scheduler_module.apply_registry(running, lambda: None, None)

    assert {job.id: str(job.trigger) for job in running.get_jobs()} == before


def test_reviewed_health_caps_cover_every_heartbeating_job():
    from vitals.scheduler import jobs, scheduler as scheduler_module

    jobs.register_all_jobs(
        {
            "garmin_sync_hours": 24,
            "garmin_weight_export_minutes": 1440,
        }
    )
    actual = scheduler_module.heartbeat_budgets("UTC")
    caps = scheduler_module.heartbeat_budget_caps(
        scheduler_module.heartbeat_job_ids()
    )

    assert set(caps) == set(actual)
    assert all(actual[job_id] <= caps[job_id] for job_id in actual)


async def test_readiness_seed_propagates_unrecorded_heartbeat():
    from vitals.scheduler import scheduler as scheduler_module

    class RefusingRedis:
        async def set(self, key, value):
            del key, value
            return False

    with pytest.raises(
        scheduler_module.SchedulerHeartbeatSeedError,
        match="keepalive",
    ):
        await scheduler_module.seed_heartbeats(
            RefusingRedis(),
            job_ids=["keepalive"],
        )
