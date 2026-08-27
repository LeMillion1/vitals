"""Lifecycle boundary for the independently runnable scheduler process."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vitals.persistence.rls import enter_platform_scope

logger = logging.getLogger(__name__)


async def load_worker_settings(
    session_factory: async_sessionmaker[AsyncSession],
) -> Optional[dict[str, Any]]:
    """Load the exact-one compatibility schedule, or shared-install defaults.

    The web startup materializes this bundle before the historical combined
    scheduler starts.  A standalone worker is intentionally only a consumer: it
    never bootstraps an identity or repairs ownership.  Once a second subject
    exists there is no installation-wide person's schedule to infer, matching
    the existing web lifespan's fallback to registry defaults.
    """

    from vitals.services.legacy_ownership import (
        LegacyOwnershipError,
        NoPersonalRecordError,
    )
    from vitals.services.proactive import prefs

    async with session_factory() as session:
        try:
            # This pre-auth compatibility read has to discover whether there is
            # exactly one subject before it can bind to one. Declare the bounded
            # installation-level lookup explicitly under the runtime RLS role.
            await enter_platform_scope(session)
            scope = await prefs.resolve_legacy_preferences_scope(
                session,
                actor_username=None,
            )
            bundle = await prefs.get_exact_one_preferences_bundle(
                session,
                scope=scope,
            )
        except (
            LegacyOwnershipError,
            NoPersonalRecordError,
            prefs.LegacyProactivePreferencesBridgeClosedError,
        ):
            await session.rollback()
            logger.info(
                "exact-one worker schedule is unavailable; using shared defaults"
            )
            return None
        else:
            # The strict read locks the canonical preference roots. A worker
            # needs only the immutable projection, so release those locks before
            # APScheduler is prepared.
            await session.rollback()
            return bundle.as_flat_dict()


@dataclass(slots=True)
class WorkerLifecycle:
    """Prepare, start, and stop the process-local APScheduler exactly once."""

    session_factory: async_sessionmaker[AsyncSession]
    redis: Optional[Redis]
    timezone: str
    _prepared: bool = field(default=False, init=False)
    _heartbeats_seeded: bool = field(default=False, init=False)
    _scheduler: Optional[AsyncIOScheduler] = field(default=None, init=False)
    _applied_heartbeat_job_ids: frozenset[str] = field(
        default_factory=frozenset,
        init=False,
    )
    _pending_heartbeat_seed_ids: set[str] = field(default_factory=set, init=False)

    @property
    def scheduler(self) -> Optional[AsyncIOScheduler]:
        return self._scheduler

    @property
    def heartbeat_job_ids(self) -> tuple[str, ...]:
        """The job ids successfully attached to this lifecycle's scheduler."""

        return tuple(sorted(self._applied_heartbeat_job_ids))

    def prepare(self, settings: Optional[dict[str, Any]]) -> None:
        """Build process-local domain and job registries without starting."""

        if self._prepared:
            raise RuntimeError("worker lifecycle is already prepared")
        from vitals.scheduler.jobs import register_all_jobs
        from vitals.scheduler.scheduler import heartbeat_job_ids
        from vitals.services.conflicts.registrations import register_all_resolvers

        # Conflict resolvers are process-local just like scheduled jobs. A
        # standalone worker cannot inherit the web process's registry, and
        # conflict-aware jobs must never run with an empty safety catalog.
        register_all_resolvers()
        register_all_jobs(settings)
        self._applied_heartbeat_job_ids = frozenset(heartbeat_job_ids())
        self._prepared = True

    async def seed_heartbeats(self) -> None:
        """Seed monitored heartbeats after registration and before startup."""

        if not self._prepared:
            raise RuntimeError(
                "worker lifecycle must be prepared before heartbeat seeding"
            )
        if self._heartbeats_seeded:
            raise RuntimeError("worker lifecycle heartbeats are already seeded")

        from vitals.scheduler.scheduler import seed_heartbeats

        if self.redis is not None:
            await seed_heartbeats(self.redis)
        self._heartbeats_seeded = True

    def start(self) -> AsyncIOScheduler:
        """Attach the prepared registry and start process-local scheduling."""

        if not self._prepared:
            raise RuntimeError("worker lifecycle must be prepared before start")
        if not self._heartbeats_seeded:
            raise RuntimeError(
                "worker lifecycle heartbeats must be seeded before start"
            )
        if self._scheduler is not None:
            raise RuntimeError("worker lifecycle is already started")

        from vitals.scheduler.scheduler import setup_scheduler

        scheduler = setup_scheduler(
            self.session_factory,
            self.redis,
            timezone=self.timezone,
        )
        scheduler.start()
        self._scheduler = scheduler
        return scheduler

    def reload(self, settings: Optional[dict[str, Any]]) -> None:
        """Atomically validate and attach a rebuilt registry to the scheduler."""

        scheduler = self._scheduler
        if scheduler is None:
            raise RuntimeError("worker lifecycle must be started before reload")

        from vitals.scheduler.jobs import register_all_jobs
        from vitals.scheduler.scheduler import (
            apply_registry,
            heartbeat_job_ids,
        )

        previous_job_ids = self._applied_heartbeat_job_ids
        register_all_jobs(settings)
        current_job_ids = frozenset(heartbeat_job_ids())
        apply_registry(scheduler, self.session_factory, self.redis)
        # Only a successful live apply advances the lifecycle-owned projection.
        # The module-global registry may already contain an attempted rebuild,
        # so it is not authoritative after an exception.
        self._applied_heartbeat_job_ids = current_job_ids
        newly_enabled_job_ids = sorted(current_job_ids - previous_job_ids)
        self._pending_heartbeat_seed_ids.update(newly_enabled_job_ids)

    async def seed_pending_heartbeats(self) -> None:
        """Seed newly attached jobs without reapplying their live triggers."""

        if self.redis is None or not self._pending_heartbeat_seed_ids:
            return
        from vitals.scheduler.scheduler import seed_heartbeats

        job_ids = sorted(self._pending_heartbeat_seed_ids)
        await seed_heartbeats(self.redis, job_ids=job_ids)
        self._pending_heartbeat_seed_ids.difference_update(job_ids)

    def shutdown(self) -> None:
        """Stop a started scheduler; a prepared-only web process is a no-op."""

        scheduler = self._scheduler
        if scheduler is None:
            return
        self._scheduler = None
        scheduler.shutdown()


__all__ = ["WorkerLifecycle", "load_worker_settings"]
