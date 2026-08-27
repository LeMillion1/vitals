"""MCP SDK runtime, authentication verifier, and surface filtering."""

from __future__ import annotations

import logging

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError

from vitals.services import modules_service
from web.config import get_web_config
from web.deps import get_session_factory
import web.mcp.identity as identity
from web.mcp.access import (
    PROMPT_ACCESS,
    RESOURCE_ACCESS,
    required_tool_scopes,
    surface_allowed,
    tool_listing_allowed,
)
from web.mcp.errors import McpActorUnresolved, visible_tool_failure
from web.mcp.ownership import legacy_owner


logger = logging.getLogger("web.routers.mcp")

MCP_SERVER_VERSION = "2.0.0"
TOKEN_MAX_AGE = 31536000

# The same mutable catalog is populated while domain adapters register tools.
TOOL_MODULES: dict[str, str] = {}


class ConnectorTokenVerifier:
    """Verify the signed token and its live, subject-scoped database grant."""

    def __init__(self, session_factory_provider=get_session_factory):
        self._session_factory_provider = session_factory_provider

    async def verify_token(self, token: str) -> AccessToken | None:
        from itsdangerous import BadSignature, SignatureExpired

        from vitals.services.authentication import mcp_tokens
        from web.authentication.tokens import _get_mcp_serializer

        cfg = get_web_config()
        try:
            payload, signed_at = _get_mcp_serializer().loads(
                token, max_age=TOKEN_MAX_AGE, return_timestamp=True
            )
        except (SignatureExpired, BadSignature):
            return None
        if not isinstance(payload, dict):
            return None

        async with self._session_factory_provider()() as session:
            verified = await mcp_tokens.verify(
                session,
                payload=payload,
                token=token,
                expected_client_id=cfg.mcp_client_id,
                expected_audience=mcp_tokens.audience_for(cfg.public_url),
                expected_issuer=cfg.public_url,
                signed_at=signed_at,
            )
            if verified is None:
                return None
            await session.commit()

        named = verified.username or None
        return AccessToken(
            token=token,
            client_id=verified.client_id,
            scopes=sorted(
                f"{scope.resource_type.value}:{scope.resource_key}:{scope.action.value}"
                for scope in verified.scopes
            ),
            subject=named,
            claims={
                "username": named,
                "sub": str(verified.user_id),
                "health_subject": str(verified.subject_id),
                "relationship": (
                    str(verified.relationship_id)
                    if verified.relationship_id is not None
                    else None
                ),
                "consent_grant": (
                    str(verified.consent_grant_id)
                    if verified.consent_grant_id is not None
                    else None
                ),
                "consent_version": verified.consent_version,
                "jti": str(verified.jti),
            }
            if named
            else {},
        )


def described_for_a_model(tool):
    """Expose only the first docstring paragraph as model-facing description."""

    description = (tool.description or "").strip()
    if not description:
        return tool
    summary = description.split("\n\n", 1)[0].strip()
    if summary == description:
        return tool
    return tool.model_copy(update={"description": summary})


def _granted_scopes():
    binding = identity.current_grant_binding()
    return binding.scopes if binding else None


class VitalsMCPServer(MCPServer):
    """Apply authorization and optional-module policy to every SDK surface."""

    def __init__(self, *args, session_factory_provider=get_session_factory, **kwargs):
        self._session_factory_provider = session_factory_provider
        super().__init__(*args, **kwargs)

    async def list_tools(self, *args, **kwargs):
        tools = [
            described_for_a_model(tool)
            for tool in await super().list_tools(*args, **kwargs)
        ]
        try:
            granted = _granted_scopes()
            tools = [
                tool
                for tool in tools
                if tool_listing_allowed(tool.name, granted)
            ]
        except McpActorUnresolved:
            logger.warning("mcp: connector grant unavailable; listing no tools")
            return []
        try:
            async with self._session_factory_provider()() as session:
                ownership = await legacy_owner(session)
                enabled = await modules_service.get_enabled_modules(
                    session,
                    subject_id=ownership.subject_id,
                )
        except Exception:
            logger.warning(
                "mcp: module state unavailable; listing grant-authorized core tools only",
                exc_info=True,
            )
            return [tool for tool in tools if tool.name not in TOOL_MODULES]
        return [
            tool
            for tool in tools
            if enabled.get(TOOL_MODULES.get(tool.name, ""), True)
        ]

    async def call_tool(self, name, arguments, context=None):
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        try:
            allowed = surface_allowed(
                required_tool_scopes(name, arguments),
                _granted_scopes(),
            )
        except McpActorUnresolved as exc:
            return visible_tool_failure(
                name,
                exc,
                output_schema=tool.output_schema,
                logger=logger,
            )
        if not allowed:
            raise ToolError(f"Unknown tool: {name}")
        try:
            return await super().call_tool(name, arguments, context)
        except MCPError:
            raise
        except Exception as exc:
            return visible_tool_failure(
                name,
                exc,
                output_schema=tool.output_schema,
                logger=logger,
            )

    async def list_resources(self, *args, **kwargs):
        resources = await super().list_resources(*args, **kwargs)
        granted = _granted_scopes()
        return [
            resource
            for resource in resources
            if surface_allowed(RESOURCE_ACCESS.get(str(resource.uri)), granted)
        ]

    async def read_resource(self, uri, context=None):
        if not surface_allowed(
            RESOURCE_ACCESS.get(str(uri)),
            _granted_scopes(),
        ):
            raise ToolError(f"Unknown resource: {uri}")
        return await super().read_resource(uri, context)

    async def list_prompts(self, *args, **kwargs):
        prompts = await super().list_prompts(*args, **kwargs)
        granted = _granted_scopes()
        return [
            prompt
            for prompt in prompts
            if surface_allowed(PROMPT_ACCESS.get(prompt.name), granted)
        ]

    async def get_prompt(self, name, arguments=None, context=None):
        if not surface_allowed(PROMPT_ACCESS.get(name), _granted_scopes()):
            raise ToolError(f"Unknown prompt: {name}")
        return await super().get_prompt(name, arguments, context)


def build_server(*, session_factory_provider=get_session_factory) -> VitalsMCPServer:
    cfg = get_web_config()
    return VitalsMCPServer(
        name="Vitals",
        version=MCP_SERVER_VERSION,
        token_verifier=ConnectorTokenVerifier(session_factory_provider),
        session_factory_provider=session_factory_provider,
        auth=AuthSettings(
            issuer_url=cfg.public_url,
            resource_server_url=f"{cfg.public_url}/mcp",
        ),
    )


__all__ = [
    "ConnectorTokenVerifier",
    "MCP_SERVER_VERSION",
    "TOKEN_MAX_AGE",
    "TOOL_MODULES",
    "VitalsMCPServer",
    "build_server",
    "described_for_a_model",
]
