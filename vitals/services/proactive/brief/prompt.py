"""Daily Brief prompt and deterministic content projection."""

from __future__ import annotations

import json

from vitals.enums import AIInvocationStatus
from vitals.services.proactive import compose

from .contracts import PreparedBrief, _BRIEF_CONTEXT_PROVENANCE_KEY

def build_prompt(ctx: dict) -> str:
    return (
        "Данные за сегодня (JSON):\n\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
        + "\n\nНапиши утренний разбор: 2-3 предложения."
    )



def _render_base_content(ctx: dict) -> str:
    return compose.render(compose.header_blocks(ctx))


def _context_with_provenance(
    prepared: PreparedBrief,
    *,
    mode: str,
    status: AIInvocationStatus | None,
) -> dict:
    context = json.loads(prepared._context_json_text)
    context[_BRIEF_CONTEXT_PROVENANCE_KEY] = {
        "policy": prepared._policy_version,
        "surface": prepared._surface.value,
        "request_key": prepared._request_key,
        "model": prepared._model,
        "mode": mode,
        "invocation_status": status.value if status is not None else None,
        "fallback": prepared._fallback.value,
    }
    return context
