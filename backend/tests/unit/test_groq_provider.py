from __future__ import annotations

from typing import Any

import httpx
import pytest

from verity.ai.gateway import GROQ_MODEL_ALIASES
from verity.ai.providers import llm
from verity.ai.providers.base import CompletionRequest, Message, TaskClass
from verity.ai.providers.llm import GroqProvider
from verity.platform.errors import AppError, ErrorCode


def request(*, structured: bool = False) -> CompletionRequest:
    schema = (
        {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        }
        if structured
        else None
    )
    return CompletionRequest(
        task_class=TaskClass.COPILOT_ANSWER,
        messages=(
            Message(role="system", content="Be concise."),
            Message(role="user", content="Hi"),
        ),
        json_schema=schema,
        max_output_tokens=321,
        temperature=0.1,
    )


def test_groq_payload_uses_chat_completions_and_structured_outputs() -> None:
    provider = GroqProvider(api_key="test-key", model="openai/gpt-oss-20b")

    payload = provider._payload(request(structured=True))

    assert payload["model"] == "openai/gpt-oss-20b"
    assert payload["max_completion_tokens"] == 321
    assert str(payload["messages"][0]["content"]).startswith(
        "Return only a valid JSON object matching this JSON Schema:"
    )
    assert payload["messages"][1] == {"role": "system", "content": "Be concise."}
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["schema"]["required"] == ["answer"]


def test_non_gpt_oss_models_use_json_object_mode() -> None:
    provider = GroqProvider(api_key="test-key", model="llama-3.1-8b-instant")
    assert provider._payload(request(structured=True))["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_groq_completion_maps_content_usage_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAsyncClient:
        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_: Any) -> None:
            pass

        async def post(self, *_: Any, **__: Any) -> httpx.Response:
            return httpx.Response(
                200,
                request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions"),
                json={
                    "id": "chatcmpl-test",
                    "model": "openai/gpt-oss-20b",
                    "choices": [
                        {"message": {"content": '{"answer":"Hello"}'}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 5,
                        "prompt_tokens_details": {"cached_tokens": 2},
                    },
                },
            )

    monkeypatch.setattr(llm.httpx, "AsyncClient", FakeAsyncClient)
    provider = GroqProvider(api_key="test-key", model="openai/gpt-oss-20b")

    completion = await provider.complete(request(structured=True))

    assert completion.text == '{"answer":"Hello"}'
    assert completion.provider == "groq"
    assert completion.model == "openai/gpt-oss-20b"
    assert completion.usage.input_tokens == 12
    assert completion.usage.output_tokens == 5
    assert completion.usage.cached_input_tokens == 2


def test_groq_rate_limit_has_the_platform_error_code() -> None:
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com"))

    with pytest.raises(AppError) as caught:
        llm._raise_for_groq_status(response)

    assert caught.value.code is ErrorCode.RATE_LIMITED


def test_groq_aliases_use_configurable_production_models() -> None:
    assert GROQ_MODEL_ALIASES["fast_small"].provider == "groq"
    assert GROQ_MODEL_ALIASES["fast_small"].model == "llama-3.1-8b-instant"
    assert GROQ_MODEL_ALIASES["reasoning_primary"].model == "openai/gpt-oss-120b"
