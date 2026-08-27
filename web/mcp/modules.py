"""Optional-module classification and direct-call enforcement for MCP tools."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from vitals.services.modules import preferences as modules_service
from web.mcp.server import TOOL_MODULES


async def module_enabled(
    session,
    key: str,
    *,
    owner_resolver: Callable[[Any], Awaitable[Any]],
) -> bool:
    """Return whether the resolved subject enabled an optional module."""

    ownership = await owner_resolver(session)
    state = await modules_service.get_enabled_modules(
        session,
        subject_id=ownership.subject_id,
    )
    return bool(state.get(key))


def module_gate(
    module_key: str,
    *,
    session_factory_provider: Callable[[], Any],
    owner_resolver: Callable[[Any], Awaitable[Any]],
):
    """Classify a tool and reject direct calls when its module is disabled."""

    def decorator(fn):
        TOOL_MODULES[fn.__name__] = module_key

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            async with session_factory_provider()() as session:
                if not await module_enabled(
                    session,
                    module_key,
                    owner_resolver=owner_resolver,
                ):
                    return {"error": f"module '{module_key}' is disabled"}
            return await fn(*args, **kwargs)

        return wrapper

    return decorator


__all__ = ["module_enabled", "module_gate"]
