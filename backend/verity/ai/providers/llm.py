"""LLM providers (PRD §20.2).

``AnthropicProvider`` speaks the Messages API over HTTP rather than through the
vendor SDK, which keeps the dependency surface small and the wire format
explicit. ``DeterministicProvider`` is a real, seeded implementation used when
no key is configured: it produces schema-valid, reproducible output so the
orchestration, validation, metering and fallback paths are all exercisable
offline. It is never routed in production — ``routing.py`` refuses it there.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx

from verity.ai.providers.base import (
    Capabilities,
    Completion,
    CompletionRequest,
    Delta,
    Usage,
)
from verity.platform.errors import AppError, ErrorCode
from verity.platform.logging import get_logger

log = get_logger("ai.providers.llm")

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self, *, api_key: str, model: str, base_url: str = "https://api.anthropic.com"
    ) -> None:
        if not api_key:
            raise AppError.internal("Anthropic provider is missing its API key.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self.model = model

    def capabilities(self) -> Capabilities:
        return Capabilities(
            streaming=True, json_schema=True, tools=True, max_context_tokens=200_000
        )

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        system = " ".join(m.content for m in request.messages if m.role == "system")
        turns = [
            {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "messages": turns,
        }
        if system:
            # Marked ephemeral so the stable prefix is cached provider-side
            # (PRD §20.4); cache hit rate is a tracked metric.
            payload["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        if request.json_schema:
            payload["messages"] = [
                *turns,
                {
                    "role": "assistant",
                    "content": "{",  # prefill forces a JSON object response
                },
            ]
        return payload

    async def complete(self, request: CompletionRequest) -> Completion:
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/v1/messages",
                    json=self._payload(request),
                    headers={
                        "x-api-key": self._api_key,
                        "anthropic-version": ANTHROPIC_VERSION,
                        "content-type": "application/json",
                    },
                )
        except httpx.TimeoutException as exc:
            raise AppError(ErrorCode.AI_TIMEOUT, "The model provider timed out.") from exc
        except httpx.HTTPError as exc:
            raise AppError(
                ErrorCode.AI_PROVIDER_UNAVAILABLE, "The model provider is unreachable."
            ) from exc

        if response.status_code == 429:
            raise AppError(ErrorCode.RATE_LIMITED, "The model provider is rate limiting us.")
        if response.status_code >= 400:
            raise AppError(
                ErrorCode.AI_PROVIDER_UNAVAILABLE,
                "The model provider rejected the request.",
                detail={"status": response.status_code},
            )

        body = response.json()
        text = "".join(block.get("text", "") for block in body.get("content", []))
        if request.json_schema and not text.lstrip().startswith("{"):
            text = "{" + text  # restore the prefill

        usage = body.get("usage", {})
        return Completion(
            text=text,
            usage=Usage(
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                cached_input_tokens=int(usage.get("cache_read_input_tokens", 0)),
            ),
            model=body.get("model", self.model),
            provider=self.name,
            finish_reason=body.get("stop_reason", "stop"),
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[Delta]:
        payload = {**self._payload(request), "stream": True}
        try:
            async with (
                httpx.AsyncClient(timeout=request.timeout_seconds) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}/v1/messages",
                    json=payload,
                    headers={
                        "x-api-key": self._api_key,
                        "anthropic-version": ANTHROPIC_VERSION,
                        "content-type": "application/json",
                    },
                ) as response,
            ):
                if response.status_code >= 400:
                    raise AppError(
                        ErrorCode.AI_PROVIDER_UNAVAILABLE,
                        "The model provider rejected the stream.",
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    if event.get("type") == "content_block_delta":
                        yield Delta(text=event["delta"].get("text", ""))
                    elif event.get("type") == "message_stop":
                        yield Delta(text="", is_final=True)
        except httpx.HTTPError as exc:
            raise AppError(ErrorCode.AI_PROVIDER_UNAVAILABLE, "The model stream failed.") from exc


class DeterministicProvider:
    """Seeded, schema-valid output for offline development and tests.

    Every response is a pure function of the prompt, so tests are reproducible
    and a rerun cannot flake. It composes text from the *provided context*
    rather than inventing content, which keeps the grounding validator
    meaningful even offline.
    """

    name = "deterministic"
    model = "deterministic-v1"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            streaming=True, json_schema=True, tools=False, max_context_tokens=32_000
        )

    def _seed(self, request: CompletionRequest) -> random.Random:
        digest = hashlib.blake2b(
            "".join(m.content for m in request.messages).encode(), digest_size=8
        ).digest()
        return random.Random(int.from_bytes(digest, "big"))  # noqa: S311 - not cryptographic

    async def complete(self, request: CompletionRequest) -> Completion:
        rng = self._seed(request)
        text = (
            json.dumps(_synthesize(request, rng))
            if request.json_schema
            else _synthesize_text(request, rng)
        )
        prompt_tokens = sum(len(m.content) // 4 for m in request.messages)
        return Completion(
            text=text,
            usage=Usage(input_tokens=prompt_tokens, output_tokens=len(text) // 4),
            model=self.model,
            provider=self.name,
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[Delta]:
        completion = await self.complete(request)
        # Chunked so streaming consumers are exercised, not handed one blob.
        for i in range(0, len(completion.text), 24):
            yield Delta(text=completion.text[i : i + 24])
        yield Delta(text="", is_final=True, usage=completion.usage)


def _last_user_message(request: CompletionRequest) -> str:
    return next(
        (m.content for m in reversed(request.messages) if m.role == "user"),
        "",
    )


def _synthesize_text(request: CompletionRequest, rng: random.Random) -> str:
    prompt = _last_user_message(request)
    lead = prompt.strip().split("\n")[0][:120] if prompt else "the topic"
    openers = (
        "Walk me through",
        "Tell me about a time you handled",
        "How would you approach",
        "What was your role in",
    )
    return f"{rng.choice(openers)} {lead}".strip()


def _synthesize(request: CompletionRequest, rng: random.Random) -> dict[str, Any]:
    """Build an object that satisfies the requested schema's required fields."""
    schema = request.json_schema or {}
    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", list(properties))

    result: dict[str, Any] = {}
    for key in required:
        spec = properties.get(key, {})
        result[key] = _value_for(key, spec, request, rng)
    return result


def _value_for(
    key: str, spec: dict[str, Any], request: CompletionRequest, rng: random.Random
) -> Any:
    kind = spec.get("type", "string")
    if kind == "array":
        item_spec = spec.get("items", {"type": "string"})
        count = max(1, int(spec.get("minItems", 2)))
        return [_value_for(key, item_spec, request, rng) for _ in range(count)]
    if kind == "object":
        return {
            name: _value_for(name, sub, request, rng)
            for name, sub in spec.get("properties", {}).items()
        }
    if kind in ("number", "integer"):
        low, high = spec.get("minimum", 0), spec.get("maximum", 100)
        value = rng.uniform(float(low), float(high))
        return int(value) if kind == "integer" else round(value, 2)
    if kind == "boolean":
        return rng.random() > 0.5
    if enum := spec.get("enum"):
        return rng.choice(enum)
    return _synthesize_text(request, rng)
