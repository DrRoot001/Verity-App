"""Hot runtime configuration shared by feature modules.

This platform contract knows only Redis and environment defaults. Admin owns
the durable database records and publishes validated values into this bridge.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from verity.platform.cache import RedisRole, get_redis
from verity.platform.config import settings

AI_RUNTIME_KEY = "admin:runtime:ai"
FLAG_PREFIX = "admin:flag:"


def default_ai_settings() -> dict[str, Any]:
    return {
        "provider": str(settings.llm_primary_provider),
        "fast_model": settings.groq_fast_model,
        "realtime_model": settings.groq_realtime_model,
        "reasoning_model": settings.groq_primary_model,
        "daily_budget_usd": settings.ai_daily_budget_usd,
        "budget_soft_threshold": settings.ai_budget_soft_threshold,
        "session_max_generations": settings.session_max_generations,
        "session_max_generations_per_minute": settings.session_max_generations_per_minute,
        "session_max_duration_minutes": settings.session_max_duration_minutes,
    }


async def ai_settings() -> dict[str, Any]:
    raw = await get_redis(RedisRole.CACHE).get(AI_RUNTIME_KEY)
    if not raw:
        return default_ai_settings()
    try:
        return {**default_ai_settings(), **json.loads(raw)}
    except (TypeError, json.JSONDecodeError):
        return default_ai_settings()


async def publish_ai_settings(value: dict[str, Any]) -> None:
    await get_redis(RedisRole.CACHE).set(AI_RUNTIME_KEY, json.dumps(value, separators=(",", ":")))


async def publish_feature_flag(key: str, enabled: bool, rollout_percentage: int) -> None:
    payload = {"enabled": enabled, "rollout_percentage": rollout_percentage}
    await get_redis(RedisRole.CACHE).set(
        f"{FLAG_PREFIX}{key}", json.dumps(payload, separators=(",", ":"))
    )


async def feature_enabled(key: str, *, user_id: uuid.UUID | None = None) -> bool:
    raw = await get_redis(RedisRole.CACHE).get(f"{FLAG_PREFIX}{key}")
    if not raw:
        return True
    try:
        flag = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return True
    if not bool(flag.get("enabled", True)):
        return False
    percentage = int(flag.get("rollout_percentage", 100))
    if percentage >= 100 or user_id is None:
        return percentage > 0
    digest = hashlib.sha256(f"{key}:{user_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100 < percentage
