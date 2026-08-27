"""Optional-module settings MCP tools without a common-runtime dependency."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from vitals.services import modules_service


@dataclass(frozen=True)
class ModuleSettingsToolDependencies:
    """Router-owned ownership, session, and Redis seams."""

    get_session_factory: Callable[[], Any]
    legacy_owner: Callable[[Any], Awaitable[Any]]
    get_redis_client: Callable[[], Any]


@dataclass(frozen=True)
class RegisteredModuleSettingsTools:
    get_modules: Callable[..., Awaitable[dict]]
    set_module: Callable[..., Awaitable[dict]]


def register_module_settings_tools(
    server: Any,
    deps: ModuleSettingsToolDependencies,
) -> RegisteredModuleSettingsTools:
    """Register module read/toggle tools in their frozen order."""

    @server.tool()
    async def get_modules() -> dict:
        """Returns which optional domains are enabled, plus which module keys are core
        (always-on, locked) vs optional (toggleable). Check this before calling a
        module-gated write tool (log_body_scan, log_event) so you know if it's on."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            ownership = await deps.legacy_owner(session)
            enabled = await modules_service.get_enabled_modules(
                session,
                subject_id=ownership.subject_id,
            )
        return {
            "enabled": enabled,
            "core": sorted(modules_service.CORE_KEYS),
            "optional": sorted(modules_service.OPTIONAL_KEYS),
        }

    @server.tool()
    async def set_module(key: str, enabled: bool) -> dict:
        """Enables or disables an optional module (e.g. body_comp, timeline, glp1,
        nutrition). Core modules are locked and return an error. WRITE tool — returns
        the new enabled-module map."""
        session_factory = deps.get_session_factory()
        async with session_factory() as session:
            ownership = await deps.legacy_owner(session)
            try:
                state = await modules_service.set_module_enabled(
                    session,
                    key=key,
                    enabled=enabled,
                    subject_id=ownership.subject_id,
                )
            except modules_service.ModuleToggleError as exc:
                return {"error": str(exc)}
            await session.commit()
            await modules_service.prime_cache(
                deps.get_redis_client(),
                state,
                subject_id=ownership.subject_id,
            )
            return {"enabled": state}

    return RegisteredModuleSettingsTools(
        get_modules=get_modules,
        set_module=set_module,
    )


__all__ = [
    "ModuleSettingsToolDependencies",
    "RegisteredModuleSettingsTools",
    "register_module_settings_tools",
]
