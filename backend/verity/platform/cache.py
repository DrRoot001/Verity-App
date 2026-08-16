"""Redis clients (PRD §22.1).

Three logical roles share a server locally but are separate URLs so they can be
split onto separate instances in production without code changes: general
cache, realtime session state, and the job queue.
"""

from __future__ import annotations

from enum import StrEnum

from redis.asyncio import Redis

from verity.platform.config import settings


class RedisRole(StrEnum):
    CACHE = "cache"
    SESSION = "session"
    QUEUE = "queue"


_clients: dict[RedisRole, Redis] = {}

_URLS: dict[RedisRole, str] = {
    RedisRole.CACHE: settings.redis_url,
    RedisRole.SESSION: settings.redis_session_url,
    RedisRole.QUEUE: settings.redis_queue_url,
}


def get_redis(role: RedisRole = RedisRole.CACHE) -> Redis:
    client = _clients.get(role)
    if client is None:
        client = Redis.from_url(
            _URLS[role],
            decode_responses=True,
            health_check_interval=30,
            socket_connect_timeout=2,
            socket_keepalive=True,
        )
        _clients[role] = client
    return client


async def close_redis() -> None:
    for client in _clients.values():
        await client.aclose()
    _clients.clear()
