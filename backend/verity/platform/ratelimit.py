"""Distributed rate limiting (PRD §28.3).

A token bucket in Redis, applied atomically in Lua so concurrent requests across
API instances cannot both observe capacity and both consume it.

Buckets are chosen per route class rather than globally: authentication needs
tight per-identity limits to blunt credential stuffing, while AI endpoints need
limits that reflect cost rather than abuse.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from verity.platform.cache import RedisRole, get_redis
from verity.platform.config import settings
from verity.platform.errors import AppError, ErrorCode, RecoveryAction
from verity.platform.logging import get_logger

log = get_logger("platform.ratelimit")

# Refill and consume in one round trip. Returns remaining tokens and the wait
# in milliseconds until one token is available.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local bucket = redis.call('HMGET', key, 'tokens', 'updated_ms')
local tokens = tonumber(bucket[1])
local updated_ms = tonumber(bucket[2])

if tokens == nil then
  tokens = capacity
  updated_ms = now_ms
end

local elapsed = math.max(0, now_ms - updated_ms) / 1000.0
tokens = math.min(capacity, tokens + elapsed * refill_per_sec)

local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'updated_ms', now_ms)
redis.call('PEXPIRE', key, ttl)

local retry_ms = 0
if allowed == 0 and refill_per_sec > 0 then
  retry_ms = math.ceil(((cost - tokens) / refill_per_sec) * 1000)
end

return {allowed, math.floor(tokens), retry_ms}
"""


class RateLimitScope(StrEnum):
    LOGIN_IDENTITY = "login_identity"
    LOGIN_IP = "login_ip"
    SIGNUP_IP = "signup_ip"
    PASSWORD_RESET = "password_reset"
    EMAIL_VERIFICATION = "email_verification"
    TOKEN_REFRESH = "token_refresh"
    API_GENERAL = "api_general"
    AI_GENERATION = "ai_generation"
    UPLOAD = "upload"
    SEARCH = "search"


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    capacity: int
    refill_per_second: float
    #: Human-readable name used in the error payload so the UI can explain which
    #: limit was hit (PRD §10.1 rate_limited state).
    label: str


#: PRD §10.2: 5/min per identity, 20/hour per IP on login. Bursts are allowed up
#: to capacity, then the refill rate governs sustained throughput.
POLICIES: dict[RateLimitScope, RateLimitPolicy] = {
    RateLimitScope.LOGIN_IDENTITY: RateLimitPolicy(5, 5 / 60, "sign-in attempts"),
    RateLimitScope.LOGIN_IP: RateLimitPolicy(20, 20 / 3600, "sign-in attempts from this network"),
    RateLimitScope.SIGNUP_IP: RateLimitPolicy(10, 10 / 3600, "account creations"),
    RateLimitScope.PASSWORD_RESET: RateLimitPolicy(3, 3 / 3600, "password reset requests"),
    RateLimitScope.EMAIL_VERIFICATION: RateLimitPolicy(5, 5 / 3600, "verification emails"),
    RateLimitScope.TOKEN_REFRESH: RateLimitPolicy(60, 1.0, "token refreshes"),
    RateLimitScope.API_GENERAL: RateLimitPolicy(120, 2.0, "requests"),
    RateLimitScope.AI_GENERATION: RateLimitPolicy(8, 8 / 60, "AI generations"),
    RateLimitScope.UPLOAD: RateLimitPolicy(20, 20 / 3600, "uploads"),
    RateLimitScope.SEARCH: RateLimitPolicy(30, 1.0, "searches"),
}


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class RateLimiter:
    def __init__(self) -> None:
        self._script_sha: str | None = None

    async def check(
        self, scope: RateLimitScope, identifier: str, *, cost: int = 1
    ) -> RateLimitResult:
        if not settings.rate_limit_enabled:
            return RateLimitResult(allowed=True, remaining=999, retry_after_seconds=0)

        policy = POLICIES[scope]
        key = f"rl:{scope}:{identifier}"
        # Keep the key alive well past a full refill so a bucket cannot be reset
        # by simply waiting out a short TTL.
        ttl_ms = int((policy.capacity / max(policy.refill_per_second, 1e-6)) * 1000) + 60_000

        redis = get_redis(RedisRole.CACHE)
        try:
            raw = await redis.eval(
                _TOKEN_BUCKET_LUA,
                1,
                key,
                policy.capacity,
                policy.refill_per_second,
                int(time.time() * 1000),
                cost,
                ttl_ms,
            )
        except Exception as exc:
            # Fail open: a Redis outage must not lock every user out of signing
            # in. The tradeoff is deliberate and logged loudly.
            log.error("rate_limit_backend_unavailable", scope=str(scope), error=type(exc).__name__)
            return RateLimitResult(allowed=True, remaining=0, retry_after_seconds=0)

        allowed, remaining, retry_ms = int(raw[0]), int(raw[1]), int(raw[2])
        return RateLimitResult(
            allowed=bool(allowed),
            remaining=remaining,
            retry_after_seconds=max(1, retry_ms // 1000) if not allowed else 0,
        )

    async def enforce(
        self, scope: RateLimitScope, identifier: str, *, cost: int = 1
    ) -> RateLimitResult:
        result = await self.check(scope, identifier, cost=cost)
        if not result.allowed:
            policy = POLICIES[scope]
            log.info("rate_limited", scope=str(scope))
            raise AppError(
                ErrorCode.RATE_LIMITED,
                f"Too many {policy.label}. Try again shortly.",
                detail={
                    "scope": str(scope),
                    "retry_after_seconds": result.retry_after_seconds,
                    "limit": policy.capacity,
                },
                recovery_action=RecoveryAction(
                    type="wait", label=f"Retry in {result.retry_after_seconds}s"
                ),
            )
        return result

    async def reset(self, scope: RateLimitScope, identifier: str) -> None:
        """Clear a bucket. Called after a successful login so one bad password
        does not count against a legitimate user for the next hour."""
        await get_redis(RedisRole.CACHE).delete(f"rl:{scope}:{identifier}")


limiter = RateLimiter()
