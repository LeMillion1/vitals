"""Application startup, worker coordination, and shutdown lifecycle."""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from vitals.process_mode import ProcessMode, load_process_mode
from vitals.services.profile import health as health_profile_service
from vitals.services.alerts.contracts import AlertLegacyBridgeError
from vitals.services.conflicts.activation import ConflictActivationLegacyBridgeError
from vitals.services.conflicts.engine import ConflictLegacyBridgeError
from vitals.services.digest.ownership import DigestOwnershipError
from vitals.services.garmin_weight.contracts import GarminWeightExportLegacyBridgeError
from vitals.services.tenancy.contracts import LegacyOwnershipError
from vitals.services.proactive.preferences import queries as preference_queries
from vitals.services.proactive.preferences import writes as preference_writes
from vitals.services.proactive.preferences.contracts import (
    LegacyProactivePreferencesBridgeClosedError,
)
from vitals.services.settings.contracts import LegacyScopedSettingBridgeClosedError
from vitals.services.share.ownership import ShareOwnershipError
from web.deps import get_redis_client, get_session_factory

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from vitals.services.proactive.preferences.contracts import ProactivePreferencesBundle

async def _bootstrap_legacy_identity(
    session_factory,
    *,
    timezone: str,
) -> ProactivePreferencesBundle:
    """Materialize the environment-backed owner and safe resource roots.

    The compatibility login remains environment-backed in this rollout phase,
    but every deployment must have one durable owner/subject boundary before a
    scheduler, connector, or catalog job can start.  Keep this transaction short
    so the PostgreSQL governance lock is never held while unrelated catalogs are
    synchronized.
    """

    from vitals.services.identity.bootstrap import bootstrap_legacy_owner
    from vitals.services.modules import preferences as modules_service
    from vitals.services.settings.contracts import ScopedSettingKey, SettingScope
    from vitals.services.settings.scoped_store import set_scoped_setting
    from vitals.services.tenancy.bootstrap import bootstrap_legacy_resource_roots
    from web.config import get_web_config

    web_config = get_web_config()
    async with session_factory() as session:
        try:
            identity = await bootstrap_legacy_owner(
                session,
                username=web_config.auth_username,
                password_hash=web_config.auth_password_hash,
                timezone=timezone,
            )
            await bootstrap_legacy_resource_roots(
                session,
                subject_id=identity.subject_id,
                # The one caller allowed to say it. This is the record
                # ``VITALS_AUTH_USERNAME`` names, so the Garmin and Hevy values
                # in ``.env`` are theirs; for anybody else those roots start
                # with no credential at all, because the file describes the
                # operator and not them.
                adopt_environment_credentials=True,
            )
            # Durable delivery deliberately refuses the legacy/default module
            # fallback. Materialize the normalized exact-one value before any
            # scheduler or sender can run, while the bootstrap transaction still
            # holds identity governance and the sole subject root.
            enabled_modules = await modules_service.get_enabled_modules(
                session,
                subject_id=identity.subject_id,
            )
            await set_scoped_setting(
                session,
                scope=SettingScope.SUBJECT,
                key=ScopedSettingKey.ENABLED_MODULES,
                scope_id=identity.subject_id,
                value=enabled_modules,
            )
            preference_scope = await preference_queries.resolve_legacy_preferences_scope(
                session,
                actor_username=None,
            )
            await preference_writes.initialize_legacy_preferences(
                session,
                scope=preference_scope,
            )
            preference_bundle = await preference_queries.get_exact_one_preferences_bundle(
                session,
                scope=preference_scope,
            )
            # Age, sex, height, programme and goals move out of ``.env`` into
            # this owner's own row. Adopted here rather than on first read for
            # the reason every other step in this block exists: while the
            # installation is one person, the unattributed value is
            # unambiguously theirs, and afterwards nothing can say whose it was.
            await health_profile_service.adopt_installation_profile(
                session,
                subject_id=identity.subject_id,
            )
            await session.commit()
            return preference_bundle
        except _LEGACY_BOOTSTRAP_CLOSED:
            # Not a misconfiguration — the destination of this whole migration.
            #
            # Every step above is compatibility scaffolding for an installation
            # that is one person: reconcile the .env credential with the durable
            # identity, materialize that person's module map, seed their
            # notification preferences. All three resolve "the subject" through
            # the sole-owner bridge, which fail-closes the moment a second
            # health subject exists, because it genuinely cannot tell whose
            # record was meant.
            #
            # Refusing to boot was the right answer while that state was
            # impossible. It stopped being right when PR-07 made a second
            # subject the point: the process would not start at all, so the
            # professional features could not be deployed by the installations
            # they were built for.
            #
            # There is nothing to reconcile here and nothing to lose by
            # skipping. Scheduled jobs fall back to their defaults, which is
            # what a shared installation needs anyway — per-subject schedules
            # are PR-09's work, not something to fake from one person's row.
            await session.rollback()
            logger.warning(
                "legacy identity bootstrap skipped: this installation holds "
                "more than one health subject, so there is no sole owner to "
                "reconcile. Scheduled jobs use their defaults."
            )
            return None
        except Exception:
            await session.rollback()
            raise


async def _load_oidc_identity_state(
    session_factory,
) -> ProactivePreferencesBundle:
    """Validate the OIDC destination without consulting legacy credentials.

    The compatibility bootstrap is intentionally absent here: after cutover,
    neither a username nor a bcrypt hash in the process environment may create,
    repair, or select an account. Existing exact-one proactive preferences are
    still read so the first cutover does not unexpectedly change job cadence.
    Once the installation has multiple subjects, scheduled jobs use their
    per-subject/default paths just as the closed legacy bridge already requires.
    """

    from vitals.services.authentication.startup import validate_oidc_startup_state
    from web.config import get_web_config

    web_config = get_web_config()
    async with session_factory() as session:
        try:
            await validate_oidc_startup_state(
                session,
                issuer=web_config.oidc_issuer,
                bootstrap_subject=web_config.oidc_bootstrap_subject,
            )
            preference_scope = await preference_queries.resolve_legacy_preferences_scope(
                session,
                actor_username=None,
            )
            return await preference_queries.get_exact_one_preferences_bundle(
                session,
                scope=preference_scope,
            )
        except _LEGACY_BOOTSTRAP_CLOSED:
            await session.rollback()
            logger.info(
                "OIDC identity state is valid; exact-one startup preferences "
                "are unavailable on this multi-subject installation."
            )
            return None
        except Exception:
            await session.rollback()
            raise


#: Every "this needs exactly one health subject" refusal, from the several
#: compatibility bridges that each grew their own. They share no base class,
#: which is why this is a list rather than a catch — and the list is useful as
#: itself: it is the porting backlog. A module leaves it by resolving through
#: ``resolve_access_context`` instead of a sole-subject bridge.
#:
#: Kept narrow on purpose. Every *other* error still fails closed, at startup
#: and in a request, because a half-reconciled identity must not go on serving.
_LEGACY_BOOTSTRAP_CLOSED = (
    LegacyOwnershipError,
    LegacyScopedSettingBridgeClosedError,
    ConflictLegacyBridgeError,
    ShareOwnershipError,
    DigestOwnershipError,
    AlertLegacyBridgeError,
    ConflictActivationLegacyBridgeError,
    LegacyProactivePreferencesBridgeClosedError,
    GarminWeightExportLegacyBridgeError,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    process_mode = load_process_mode()
    if process_mode is ProcessMode.WORKER:
        raise RuntimeError(
            "VITALS_PROCESS_MODE=worker must use the vitals.worker entry point"
        )
    session_factory = get_session_factory()
    redis = None
    try:
        redis = get_redis_client()
    except Exception as e:
        logger.warning("Redis client could not be loaded at startup: %s", e)

    # Worker lifecycle setup. Combined mode remains the compatibility default;
    # web mode prepares the registry for health reporting but never starts
    # process-local background execution.
    from vitals.config import load_config
    from vitals.scheduler.lifecycle import WorkerLifecycle
    from vitals.services.conflicts import catalog as conflict_catalog
    from vitals.services.conflicts import engine
    from vitals.services.hrt import catalog

    config = load_config()
    worker_lifecycle = WorkerLifecycle(
        session_factory=session_factory,
        redis=redis,
        timezone=config.timezone,
    )

    def drill_stage(stage: str) -> None:
        if os.getenv("VITALS_RESTORE_DRILL") == "true":
            logger.info("vitals_restore_drill_stage=%s", stage)

    # Fail startup closed before catalogs, scheduler registration, or connector
    # work. Password mode reconciles its environment credential; OIDC mode
    # proves that the configured issuer has a safe durable destination without
    # reading or mutating legacy password material.
    from web.config import get_web_config

    drill_stage("identity_started")
    if get_web_config().oidc_enabled:
        preference_bundle = await _load_oidc_identity_state(session_factory)
    else:
        preference_bundle = await _bootstrap_legacy_identity(
            session_factory,
            timezone=config.timezone,
        )
    drill_stage("identity_completed")

    # Upsert the curated rule catalog (vitals/data/conflict_rules.yaml) — cheap,
    # idempotent, and keeps the DB in sync with the checked-in YAML on every
    # deploy without a data migration per rule change.
    drill_stage("catalogs_started")
    async with session_factory() as session:
        # Process-local conflict resolvers and job schedules are prepared
        # together. A standalone worker uses the same boundary and therefore
        # cannot start conflict-aware jobs with an empty resolver registry.
        worker_lifecycle.prepare(
            preference_bundle.as_flat_dict()
            if preference_bundle is not None
            else None
        )
        await conflict_catalog.sync_catalog(session)
        # Upsert the curated HRT compound catalog (vitals/data/hrt_compounds.yaml).
        await catalog.sync_catalog(session)
        await session.commit()
    drill_stage("catalogs_completed")

    # The panel seed adopts the pre-tenancy catalog only under the shared
    # governance lock. Keep it in a fresh transaction so catalog row locks are
    # never acquired before governance/subject locks.
    from vitals.services.hrt import reminders
    from vitals.utils.timeutils import today_local

    drill_stage("seed_started")
    async with session_factory() as session:
        try:
            conflict_context = (
                await engine.resolve_legacy_conflict_write_context(
                    session,
                    actor_username=None,
                    evaluation_date=today_local(),
                )
            )
            prepared = await engine.prepare_scoped_write(
                session,
                context=conflict_context,
            )
            await reminders.seed_hormone_panel(
                session,
                identity=conflict_context.identity,
                prepared_conflict_write=prepared,
            )
            await session.commit()
        except _LEGACY_BOOTSTRAP_CLOSED:
            # Seeding one person's hormone panel from the curated catalog, in an
            # installation that has more than one person. There is no "the
            # person" to seed for, and picking one would be inventing a fact
            # about somebody's treatment. Skipped, like the identity bootstrap
            # above and for the same reason.
            await session.rollback()
            logger.warning(
                "hormone panel seed skipped: more than one health subject, so "
                "there is no sole owner to seed for"
            )
    drill_stage("seed_completed")

    drill_stage("heartbeats_started")
    if process_mode is ProcessMode.COMBINED:
        await worker_lifecycle.seed_heartbeats()
    drill_stage("heartbeats_completed")

    if process_mode is ProcessMode.COMBINED:
        app.state.scheduler = worker_lifecycle.start()
    else:
        app.state.scheduler = None
    drill_stage("scheduler_completed")

    async with AsyncExitStack() as stack:
        # The mounted MCP app builds its streamable-HTTP session manager in its own
        # lifespan, which app.mount() never runs — without this every /mcp/ request
        # fails with "manager not initialized".
        mcp_lifespan = getattr(app.state, "mcp_lifespan", None)
        if mcp_lifespan is not None:
            drill_stage("mcp_started")
            await stack.enter_async_context(mcp_lifespan(app))
        drill_stage("mcp_completed")
        yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    worker_lifecycle.shutdown()

# Public name for registration code; the underscored alias remains the stable
# compatibility seam used by migration-audit tests through web.main.
LEGACY_BOOTSTRAP_CLOSED = _LEGACY_BOOTSTRAP_CLOSED

__all__ = [
    "LEGACY_BOOTSTRAP_CLOSED",
    "_LEGACY_BOOTSTRAP_CLOSED",
    "_bootstrap_legacy_identity",
    "_load_oidc_identity_state",
    "lifespan",
]
