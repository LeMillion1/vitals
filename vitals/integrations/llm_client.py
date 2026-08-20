"""OpenRouter LLM gateway — one provider-agnostic client.

Thin wrapper over the official ``openai`` SDK pointed at OpenRouter
(``base_url`` override). Model ids are per-task and come from config
(``llm_model_digest`` for narrative, ``llm_model_parser`` — vision-capable — for
lab extraction), so switching providers/models is an ``.env`` change, never code.

Two compatibility helpers cover the two shapes the product needs:
  * :meth:`complete_text` — free-text narrative (weekly digest, module 10).
  * :meth:`extract_json`  — structured/JSON extraction, optionally from an image
    (lab parser, module 7).

Their ``*_with_usage`` counterparts retain the same in-memory payload while
also returning bounded provider metadata for the platform AI invocation ledger.
The result deliberately hides its payload from ``repr`` so a routine diagnostic
cannot copy a narrative, document extraction, or other health data into logs.

The underlying client is built lazily, so importing this module (and constructing
``LLMClient``) never requires an API key — only an actual call does.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Generic, Optional, TypeVar

from vitals.config import Config, load_config

logger = logging.getLogger(__name__)

# Hard ceiling on a single LLM request. Lab extraction (`extract_json`) and the
# on-demand digest (`complete_text`) run *inside* an HTTP request, so without this
# a hung upstream would pin the worker for the SDK's ~10-minute default.
_REQUEST_TIMEOUT_SECONDS = 90.0
_MICROUNITS_PER_CREDIT = Decimal("1000000")
_MAX_BIGINT = 2**63 - 1

_PayloadT = TypeVar("_PayloadT")


@dataclass(frozen=True, slots=True)
class LLMCallResult(Generic[_PayloadT]):
    """One in-memory provider result plus non-PHI accounting metadata."""

    value: _PayloadT = field(repr=False)
    upstream_request_id: str | None
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost_microunits: int | None


def _provider_field(value: Any, name: str) -> Any:
    """Read a standard or Pydantic-extra provider field without serialization."""

    if value is None:
        return None
    if isinstance(value, Mapping):
        return value.get(name)
    direct = getattr(value, name, None)
    if direct is not None:
        return direct
    extra = getattr(value, "model_extra", None)
    if isinstance(extra, Mapping):
        return extra.get(name)
    return None


def _bounded_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > _MAX_BIGINT:
        return None
    return value


def _cost_microunits(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        cost = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not cost.is_finite() or cost < 0:
        return None
    microunits = int(
        (cost * _MICROUNITS_PER_CREDIT).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )
    if microunits > _MAX_BIGINT:
        return None
    return microunits


def _call_result(
    response: Any,
    *,
    value: _PayloadT,
    requested_model: str,
) -> LLMCallResult[_PayloadT]:
    usage = _provider_field(response, "usage")
    input_tokens = _provider_field(usage, "prompt_tokens")
    if input_tokens is None:
        input_tokens = _provider_field(usage, "input_tokens")
    output_tokens = _provider_field(usage, "completion_tokens")
    if output_tokens is None:
        output_tokens = _provider_field(usage, "output_tokens")

    response_model = _provider_field(response, "model")
    model = (
        response_model.strip()
        if isinstance(response_model, str) and response_model.strip()
        else requested_model
    )
    request_id = _provider_field(response, "id")
    if isinstance(request_id, str):
        request_id = request_id.strip() or None
    else:
        request_id = None

    return LLMCallResult(
        value=value,
        upstream_request_id=request_id,
        model=model,
        input_tokens=_bounded_nonnegative_int(input_tokens),
        output_tokens=_bounded_nonnegative_int(output_tokens),
        cost_microunits=_cost_microunits(_provider_field(usage, "cost")),
    )


class LLMNotConfigured(RuntimeError):
    """Raised when a call is attempted without ``VITALS_OPENROUTER_API_KEY``."""


class LLMEmptyResponse(RuntimeError):
    """Raised when the upstream returns a 200 with a blank completion — no
    exception, just nothing to show (observed as an intermittent OpenRouter/
    provider hiccup, not tied to any one model)."""


class LLMClient:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or load_config()
        self._client: Any = None  # lazily constructed AsyncOpenAI

    # ── plumbing ───────────────────────────────────────────────────────────────
    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._config.openrouter_api_key:
            raise LLMNotConfigured("VITALS_OPENROUTER_API_KEY is not set")
        from openai import AsyncOpenAI  # imported lazily

        headers: dict[str, str] = {}
        if self._config.openrouter_http_referer:
            headers["HTTP-Referer"] = self._config.openrouter_http_referer
        if self._config.openrouter_x_title:
            headers["X-Title"] = self._config.openrouter_x_title

        self._client = AsyncOpenAI(
            base_url=self._config.openrouter_base_url,
            api_key=self._config.openrouter_api_key,
            default_headers=headers or None,
            timeout=_REQUEST_TIMEOUT_SECONDS,
            # SDK default is 2 retries -> a paid call can cost 3x on transient errors.
            max_retries=0,
        )
        return self._client

    @property
    def digest_model(self) -> str:
        return self._config.llm_model_digest

    @property
    def parser_model(self) -> str:
        return self._config.llm_model_parser

    @property
    def brief_model(self) -> str:
        """Model for the daily brief; falls back to the digest model when unset."""
        return self._config.llm_model_brief or self.digest_model

    # ── helpers ────────────────────────────────────────────────────────────────
    async def complete_text(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Free-text completion (narrative digest). Returns the message content."""
        result = await self.complete_text_with_usage(
            prompt,
            model=model,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return result.value

    async def complete_text_with_usage(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: Optional[int] = None,
    ) -> LLMCallResult[str]:
        """Free-text completion with bounded provider accounting metadata."""

        client = self._ensure_client()
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        requested_model = model or self.digest_model
        resp = await client.chat.completions.create(
            model=requested_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choice = resp.choices[0]
        content = (choice.message.content or "").strip()
        if not content:
            # A blank completion isn't an SDK-level error (no exception to catch),
            # so without this the only trace is "content was empty" — log the one
            # field (finish_reason) that hints at why: length/content_filter/stop.
            logger.warning(
                "LLM returned empty content (model=%s, finish_reason=%s)",
                requested_model,
                choice.finish_reason,
            )
        elif choice.finish_reason == "length":
            # Truncation is silent otherwise: the caller gets a normal-looking
            # string that stops mid-sentence (hit in prod on a reasoning model,
            # whose thinking tokens eat the same max_tokens budget).
            logger.warning(
                "LLM completion truncated by max_tokens (model=%s, max_tokens=%s)",
                requested_model,
                max_tokens,
            )
        return _call_result(
            resp,
            value=content,
            requested_model=requested_model,
        )

    async def extract_json(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        system: Optional[str] = None,
        image_url: Optional[str] = None,
        image_urls: Optional[list[str]] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> dict:
        """Structured extraction → parsed JSON dict. Pass ``image_url`` (a data: or
        https: URL) or ``image_urls`` (a list of data: or https: URLs) to send lab
        scans to a vision-capable model. If the model returns non-JSON, the raw
        text comes back under ``_unparsed`` (caller decides how to handle)."""
        result = await self.extract_json_with_usage(
            prompt,
            model=model,
            system=system,
            image_url=image_url,
            image_urls=image_urls,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return result.value

    async def extract_json_with_usage(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        system: Optional[str] = None,
        image_url: Optional[str] = None,
        image_urls: Optional[list[str]] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> LLMCallResult[dict]:
        """Structured extraction plus bounded provider accounting metadata."""

        client = self._ensure_client()

        user_content: Any
        if image_urls:
            user_content = [{"type": "text", "text": prompt}]
            for url in image_urls:
                user_content.append({"type": "image_url", "image_url": {"url": url}})
        elif image_url:
            user_content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]
        else:
            user_content = prompt

        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})

        requested_model = model or self.parser_model
        resp = await client.chat.completions.create(
            model=requested_model,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
        )
        raw = (resp.choices[0].message.content or "").strip()
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            value = parsed
        else:
            # Hand the unparsed text back instead of an empty dict: callers store
            # this verbatim in raw_payloads, and {} would discard the only
            # artefact that makes a failed extraction reviewable and re-parseable.
            logger.warning("LLM extract_json returned non-JSON content")
            value = {"_unparsed": raw}
        return _call_result(
            resp,
            value=value,
            requested_model=requested_model,
        )

    async def ping(self) -> bool:
        """Lightweight reachability check (one tiny completion). Returns True on a
        non-empty response, False on any failure. Mocked in tests."""
        try:
            text = await self.complete_text(
                "ping", system="Reply with the single word: pong", max_tokens=5
            )
            return bool(text)
        except Exception:
            logger.warning("LLM ping failed", exc_info=True)
            return False


__all__ = [
    "LLMCallResult",
    "LLMClient",
    "LLMEmptyResponse",
    "LLMNotConfigured",
]
