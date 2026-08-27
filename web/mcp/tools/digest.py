"""Digest-generation MCP adapter preserving quota, lease, and compensation flow."""
from __future__ import annotations

from vitals.services.ai_gateway import contracts as ai_gateway_service_contracts

from vitals.services.milestones import governance as milestone_governance

from vitals.services.digest import ownership as digest_ownership
from vitals.services.digest import generation as digest_generation
from vitals.services.digest import queries as digest_queries

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from vitals.enums import AIInvocationSource, AIInvocationStatus


@dataclass(frozen=True)
class DigestToolDependencies:
    get_session_factory: Callable[[], Any]
    actor_username: Callable[[Any], Awaitable[str]]
    serialize_row: Callable[[Any], dict]
    serialize_written: Callable[[Any, Any], Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredDigestTools:
    generate_digest_now: Callable[..., Awaitable[dict]]


@dataclass(frozen=True)
class RegisteredDigestReadTools:
    get_weekly_digests: Callable[..., Awaitable[list[dict]]]


def register_digest_read_tools(
    server: Any,
    deps: DigestToolDependencies,
) -> RegisteredDigestReadTools:
    """Register historical weekly digests at their frozen early position."""

    @server.tool()
    async def get_weekly_digests(limit: int = 5) -> list[dict]:
        """Retrieves historical Claude-generated weekly summaries for continuity."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            owner = await digest_ownership.prepare_digest_owner(
                session,
                actor_username=await deps.actor_username(session),
            )
            digests = await digest_queries.list_digests(
                session,
                limit=limit,
                prepared_owner=owner,
            )
            return [deps.serialize_row(digest) for digest in digests]

    return RegisteredDigestReadTools(get_weekly_digests=get_weekly_digests)


def register_digest_tools(
    server: Any,
    deps: DigestToolDependencies,
) -> RegisteredDigestTools:
    """Register immediate digest generation at its frozen position."""

    @server.tool()
    async def generate_digest_now(period_days: int = 7) -> dict:
        """Generates a fresh weekly AI digest right now (assembles the cross-domain
        context, asks the configured LLM for the narrative, saves it) and returns it.
        Errors cleanly if platform AI is unavailable. WRITE tool."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            prepared = None

            async def release_reservation() -> None:
                await session.rollback()
                if prepared is None or not prepared.dispatchable:
                    return
                if await digest_generation.release_prepared_digest(session, prepared):
                    await session.commit()
                else:
                    await session.rollback()

            try:
                prepared = await digest_ownership.prepare_digest(
                    session,
                    actor_username=await deps.actor_username(session),
                    invocation_source=AIInvocationSource.MCP,
                    period_days=period_days,
                )
                await session.commit()
                if prepared.existing_artifact_id is not None:
                    owner = await digest_ownership.prepare_digest_owner(
                        session,
                        actor_username=await deps.actor_username(session),
                    )
                    row = await digest_generation.existing_digest_for_prepared(
                        session,
                        prepared,
                        prepared_owner=owner,
                    )
                    if row is None:
                        return {"error": "digest provenance is unavailable"}
                    return await deps.serialize_written(session, row)
                if not prepared.dispatchable:
                    if prepared.reservation_status is AIInvocationStatus.DISPATCHING:
                        return {
                            "error": "digest generation is already pending",
                            "code": "dispatching",
                        }
                    return {
                        "error": "digest generation attempt failed",
                        "code": prepared.reservation_status.value,
                    }
                lease = await digest_generation.start_digest_dispatch(session, prepared)
                await session.commit()
                completion = await digest_generation.render_digest(prepared, lease)
                row = await digest_generation.persist_digest(
                    session,
                    prepared,
                    completion,
                )
                await session.commit()
                if row is None:
                    return {
                        "error": "AI provider did not produce a digest",
                        "code": (
                            completion.error_code.value
                            if completion.error_code is not None
                            else "invalid_response"
                        ),
                    }
                return await deps.serialize_written(session, row)
            except ai_gateway_service_contracts.AIQuotaExceededError:
                await session.rollback()
                return {"error": "AI quota is unavailable", "code": "quota_exceeded"}
            except ai_gateway_service_contracts.AIGatewayConfigurationError:
                await release_reservation()
                return {
                    "error": "platform AI is not configured",
                    "code": "provider_unconfigured",
                }
            except (
                ai_gateway_service_contracts.AIGatewayAuthorizationError,
                digest_ownership.DigestOwnershipError,
                milestone_governance.MilestoneOwnershipError,
            ):
                await release_reservation()
                raise
            except ai_gateway_service_contracts.AIInvocationStateError:
                await session.rollback()
                return {"error": "digest generation is already pending"}
            except ValueError:
                await session.rollback()
                return {"error": "invalid digest request"}

    return RegisteredDigestTools(generate_digest_now=generate_digest_now)


__all__ = [
    "DigestToolDependencies",
    "RegisteredDigestReadTools",
    "RegisteredDigestTools",
    "register_digest_read_tools",
    "register_digest_tools",
]
