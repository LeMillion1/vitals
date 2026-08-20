"""Bounded OpenRouter usage metadata without leaking in-memory payloads."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from vitals.integrations.llm_client import LLMCallResult, LLMClient


class _Completions:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _client(response) -> tuple[LLMClient, _Completions]:
    completions = _Completions(response)
    client = LLMClient()
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    return client, completions


async def test_text_envelope_retains_usage_and_hides_payload_from_repr():
    secret_narrative = "Synthetic private health narrative"
    response = SimpleNamespace(
        id="  gen-synthetic-123  ",
        model="provider/resolved-model",
        usage=SimpleNamespace(
            prompt_tokens=12,
            completion_tokens=7,
            model_extra={"cost": "0.0015005"},
        ),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=f"  {secret_narrative}  "),
                finish_reason="stop",
            )
        ],
    )
    client, completions = _client(response)

    result = await client.complete_text_with_usage(
        "synthetic prompt",
        model="requested/model",
        max_tokens=99,
    )

    assert isinstance(result, LLMCallResult)
    assert result.value == secret_narrative
    assert result.upstream_request_id == "gen-synthetic-123"
    assert result.model == "provider/resolved-model"
    assert (result.input_tokens, result.output_tokens) == (12, 7)
    assert result.cost_microunits == 1501
    assert secret_narrative not in repr(result)
    assert completions.calls == [
        {
            "model": "requested/model",
            "messages": [{"role": "user", "content": "synthetic prompt"}],
            "temperature": 0.4,
            "max_tokens": 99,
        }
    ]


async def test_legacy_text_helper_keeps_the_original_return_shape():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=" legacy text "),
                finish_reason="stop",
            )
        ]
    )
    client, _completions = _client(response)

    assert await client.complete_text("synthetic prompt") == "legacy text"


async def test_json_envelope_accepts_responses_style_usage_names():
    private_payload = {"marker": "synthetic", "value": 42}
    response = SimpleNamespace(
        id="gen-json",
        model="provider/parser",
        usage={
            "input_tokens": 101,
            "output_tokens": 23,
            "cost": 0.0000005,
        },
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content='{"marker":"synthetic","value":42}'),
                finish_reason="stop",
            )
        ],
    )
    client, _completions = _client(response)

    result = await client.extract_json_with_usage(
        "synthetic extraction prompt",
        model="requested/parser",
    )

    assert result.value == private_payload
    assert (result.input_tokens, result.output_tokens) == (101, 23)
    assert result.cost_microunits == 1
    assert repr(private_payload) not in repr(result)


async def test_invalid_provider_metadata_is_ignored_not_persisted():
    response = SimpleNamespace(
        id=123,
        model="   ",
        usage={
            "prompt_tokens": True,
            "completion_tokens": -1,
            "cost": "NaN",
        },
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok"),
                finish_reason="stop",
            )
        ],
    )
    client, _completions = _client(response)

    result = await client.complete_text_with_usage(
        "synthetic prompt",
        model="requested/fallback",
    )

    assert result.upstream_request_id is None
    assert result.model == "requested/fallback"
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.cost_microunits is None


@pytest.mark.parametrize("cost", ["-0.1", "Infinity", object(), 10**20])
async def test_unbounded_or_invalid_cost_is_not_exposed(cost):
    response = SimpleNamespace(
        usage={"cost": cost},
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok"),
                finish_reason="stop",
            )
        ],
    )
    client, _completions = _client(response)

    result = await client.complete_text_with_usage("synthetic prompt")

    assert result.cost_microunits is None


async def test_non_json_payload_is_preserved_but_not_rendered_in_result_repr():
    private_raw = "Synthetic unparsed document response"
    response = SimpleNamespace(
        id="gen-unparsed",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=private_raw),
                finish_reason="stop",
            )
        ],
    )
    client, _completions = _client(response)

    result = await client.extract_json_with_usage("synthetic prompt")

    assert result.value == {"_unparsed": private_raw}
    assert private_raw not in repr(result)
