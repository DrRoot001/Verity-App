"""Metered LLM gateway (PRD §20.1 routing, §33 cost control).

Every model call in the product goes through ``AIGateway``. Application code
names a *task class*, never a model, so routing is data rather than scattered
conditionals — and because the provider clients are only reachable from here,
no call path can skip metering (FR-COST-002).

Responsibilities, in order:
  1. Resolve task class → model via the routing table.
  2. Enforce the daily budget before spending anything (§33).
  3. Call the provider, with a fallback on failure (§20.3).
  4. Validate structured output against its schema.
  5. Record tokens and computed cost.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from verity.ai.providers.base import (
    Completion,
    CompletionRequest,
    LLMProvider,
    Message,
    TaskClass,
)
from verity.ai.providers.llm import AnthropicProvider, DeterministicProvider
from verity.platform.cache import RedisRole, get_redis
from verity.platform.config import Environment, settings
from verity.platform.errors import AppError, ErrorCode
from verity.platform.logging import get_logger
from verity.platform.telemetry import ai_cost_usd, ai_tokens, provider_fallbacks, stage_span

log = get_logger("ai.gateway")


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """A routable model and what it costs, in USD per million tokens."""

    provider: str
    model: str
    input_cost_per_mtok: float
    output_cost_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return round(
            (input_tokens / 1_000_000) * self.input_cost_per_mtok
            + (output_tokens / 1_000_000) * self.output_cost_per_mtok,
            6,
        )


#: Capability aliases (PRD §20.1). Application code never names these directly.
MODEL_ALIASES: dict[str, ModelSpec] = {
    "fast_small": ModelSpec("anthropic", "claude-haiku-4-5-20251001", 1.0, 5.0),
    "fast_realtime": ModelSpec("anthropic", "claude-haiku-4-5-20251001", 1.0, 5.0),
    "realtime_primary": ModelSpec("anthropic", "claude-sonnet-5", 3.0, 15.0),
    "reasoning_primary": ModelSpec("anthropic", "claude-opus-5", 15.0, 75.0),
    "reasoning_batch": ModelSpec("anthropic", "claude-sonnet-5", 3.0, 15.0),
    "deterministic": ModelSpec("deterministic", "deterministic-v1", 0.0, 0.0),
}

#: Task class → primary alias, then fallbacks in order (PRD §20.1, §20.3).
ROUTING: dict[TaskClass, tuple[str, ...]] = {
    TaskClass.UTTERANCE_CLASSIFY: ("fast_small",),
    TaskClass.BOUNDARY_SCORE: ("fast_small",),
    TaskClass.QUESTION_CLASSIFY: ("fast_small",),
    TaskClass.INTENT_TO_RETRIEVAL_QUERY: ("fast_small",),
    TaskClass.ANSWER_DIRECTION: ("fast_realtime", "fast_small"),
    TaskClass.COPILOT_ANSWER: ("realtime_primary", "fast_realtime"),
    TaskClass.MOCK_INTERVIEWER_TURN: ("realtime_primary", "fast_small"),
    TaskClass.RUBRIC_EVALUATE_ANSWER: ("fast_small",),
    TaskClass.RESUME_EXTRACT: ("reasoning_batch", "fast_small"),
    TaskClass.JD_EXTRACT: ("reasoning_batch", "fast_small"),
    TaskClass.STORY_GENERATE: ("reasoning_batch", "fast_small"),
    TaskClass.SYSTEM_DESIGN_GUIDANCE: ("reasoning_primary", "realtime_primary"),
    TaskClass.COMPLEX_CODING: ("reasoning_primary", "realtime_primary"),
    TaskClass.SESSION_REPORT: ("reasoning_batch", "fast_small"),
    TaskClass.WEAKNESS_ANALYSIS: ("reasoning_batch", "fast_small"),
    TaskClass.ROLLING_SUMMARY: ("fast_small",),
}

#: Premium models are gated by the model_tier entitlement (PRD §19.1).
PREMIUM_ALIASES = frozenset({"reasoning_primary"})


@dataclass(slots=True)
class GenerationResult:
    content: Any
    text: str
    model: str
    provider: str
    alias: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float
    latency_ms: float
    degraded: bool = False
    degraded_reason: str | None = None
    validation_repaired: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class AIGateway:
    """The only route to a model."""

    def __init__(self, *, model_tier: str = "standard") -> None:
        self._model_tier = model_tier
        self._providers: dict[str, LLMProvider] = {}

    # ── Routing ──────────────────────────────────────────────────────

    def _aliases_for(self, task_class: TaskClass) -> tuple[str, ...]:
        aliases = ROUTING.get(task_class, ("fast_small",))
        if self._model_tier != "premium":
            downgraded = tuple(a for a in aliases if a not in PREMIUM_ALIASES)
            # Never leave a task unroutable: fall back to the cheapest capable
            # tier rather than failing a request over an entitlement.
            aliases = downgraded or ("realtime_primary",)
        if not _real_provider_configured():
            return ("deterministic",)
        return aliases

    def _provider(self, spec: ModelSpec) -> LLMProvider:
        cached = self._providers.get(spec.model)
        if cached is not None:
            return cached

        if spec.provider == "anthropic":
            provider: LLMProvider = AnthropicProvider(
                api_key=settings.anthropic_api_key.get_secret_value(), model=spec.model
            )
        elif spec.provider == "deterministic":
            if settings.env.is_production_like:
                # A deterministic model must never answer a real user.
                raise AppError.internal("Deterministic provider is not permitted in production.")
            provider = DeterministicProvider()
        else:
            raise AppError.internal(f"Unknown provider '{spec.provider}'.")

        self._providers[spec.model] = provider
        return provider

    # ── Budget (PRD §33) ─────────────────────────────────────────────

    @staticmethod
    async def _spend_today() -> float:
        raw = await get_redis(RedisRole.CACHE).get(_budget_key())
        return float(raw) if raw else 0.0

    @staticmethod
    async def _record_spend(amount: float) -> None:
        redis = get_redis(RedisRole.CACHE)
        key = _budget_key()
        await redis.incrbyfloat(key, amount)
        await redis.expire(key, 172_800)

    async def _check_budget(self) -> None:
        spent = await self._spend_today()
        budget = settings.ai_daily_budget_usd
        if spent >= budget:
            raise AppError(
                ErrorCode.AI_BUDGET_EXCEEDED,
                "AI usage has reached today's budget.",
                detail={"spent_usd": round(spent, 2), "budget_usd": budget},
            )
        if spent >= budget * settings.ai_budget_soft_threshold:
            log.warning("ai_budget_soft_threshold", spent_usd=round(spent, 2), budget_usd=budget)

    # ── Generation ───────────────────────────────────────────────────

    async def generate(
        self,
        *,
        task_class: TaskClass,
        messages: list[Message],
        json_schema: dict[str, Any] | None = None,
        max_output_tokens: int = 1024,
        temperature: float = 0.2,
        timeout_seconds: float = 30.0,
        user_id: uuid.UUID | None = None,
        feature: str = "unspecified",
    ) -> GenerationResult:
        await self._check_budget()

        request = CompletionRequest(
            task_class=task_class,
            messages=messages,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )

        aliases = self._aliases_for(task_class)
        last_error: AppError | None = None

        for index, alias in enumerate(aliases):
            spec = MODEL_ALIASES[alias]
            started = time.perf_counter()
            try:
                with stage_span("llm_generate", task_class=str(task_class), alias=alias):
                    completion = await self._provider(spec).complete(request)
            except AppError as exc:
                last_error = exc
                provider_fallbacks.labels(
                    kind="llm", from_provider=spec.provider, reason=str(exc.code)
                ).inc()
                log.warning(
                    "llm_call_failed",
                    alias=alias,
                    task_class=str(task_class),
                    code=str(exc.code),
                    remaining_fallbacks=len(aliases) - index - 1,
                )
                continue

            elapsed_ms = (time.perf_counter() - started) * 1000
            return await self._finalize(
                completion=completion,
                spec=spec,
                alias=alias,
                task_class=task_class,
                json_schema=json_schema,
                elapsed_ms=elapsed_ms,
                degraded=index > 0,
                degraded_reason=str(last_error.code) if last_error else None,
                user_id=user_id,
                feature=feature,
            )

        raise last_error or AppError(
            ErrorCode.AI_PROVIDER_UNAVAILABLE, "No model provider was available."
        )

    async def _finalize(
        self,
        *,
        completion: Completion,
        spec: ModelSpec,
        alias: str,
        task_class: TaskClass,
        json_schema: dict[str, Any] | None,
        elapsed_ms: float,
        degraded: bool,
        degraded_reason: str | None,
        user_id: uuid.UUID | None,
        feature: str,
    ) -> GenerationResult:
        content: Any = completion.text
        repaired = False

        if json_schema is not None:
            content, repaired = _parse_structured(completion.text, json_schema)

        cost = spec.cost(completion.usage.input_tokens, completion.usage.output_tokens)
        await self._record_spend(cost)

        ai_tokens.labels(
            provider=completion.provider,
            model=completion.model,
            task_class=str(task_class),
            direction="input",
        ).inc(completion.usage.input_tokens)
        ai_tokens.labels(
            provider=completion.provider,
            model=completion.model,
            task_class=str(task_class),
            direction="output",
        ).inc(completion.usage.output_tokens)
        ai_cost_usd.labels(
            provider=completion.provider, model=completion.model, feature=feature
        ).inc(cost)

        log.info(
            "llm_generated",
            task_class=str(task_class),
            alias=alias,
            model=completion.model,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cached_input_tokens=completion.usage.cached_input_tokens,
            cost_usd=cost,
            latency_ms=round(elapsed_ms, 1),
            degraded=degraded,
            user_ref=str(user_id) if user_id else None,
        )

        return GenerationResult(
            content=content,
            text=completion.text,
            model=completion.model,
            provider=completion.provider,
            alias=alias,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cached_input_tokens=completion.usage.cached_input_tokens,
            cost_usd=cost,
            latency_ms=elapsed_ms,
            degraded=degraded,
            degraded_reason=degraded_reason,
            validation_repaired=repaired,
        )


def _parse_structured(text: str, schema: dict[str, Any]) -> tuple[Any, bool]:
    """Parse and check required fields, with one repair attempt (PRD §20.6)."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        repaired = _extract_json_object(text)
        if repaired is None:
            raise AppError(
                ErrorCode.AI_SCHEMA_VALIDATION_FAILED,
                "The model returned output we couldn't parse.",
            ) from None
        parsed, was_repaired = repaired, True
    else:
        was_repaired = False

    missing = [key for key in schema.get("required", []) if key not in parsed]
    if missing:
        raise AppError(
            ErrorCode.AI_SCHEMA_VALIDATION_FAILED,
            "The model's response was missing required fields.",
            detail={"missing": missing},
        )
    return parsed, was_repaired


def _extract_json_object(text: str) -> Any | None:
    """Recover an object from a response wrapped in prose or fences."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _real_provider_configured() -> bool:
    return bool(settings.anthropic_api_key.get_secret_value())


def _budget_key() -> str:
    import datetime as dt

    return f"ai:budget:{dt.datetime.now(dt.UTC).date().isoformat()}"


def build_gateway(model_tier: str = "standard") -> AIGateway:
    if settings.env is Environment.PRODUCTION and not _real_provider_configured():
        raise AppError.internal("No model provider is configured.")
    return AIGateway(model_tier=model_tier)
