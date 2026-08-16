"""Liveness, readiness and metrics endpoints (PRD §30, §38.3 health gates).

``/healthz`` answers "is this process alive" and must never touch a dependency.
``/readyz`` answers "can this process serve traffic" and checks Postgres and
Redis for real — a readiness probe that lies is worse than none, because the
rollout health gate depends on it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from verity.platform.cache import RedisRole, get_redis
from verity.platform.config import settings
from verity.platform.db.session import get_sessionmaker
from verity.platform.logging import get_logger
from verity.platform.telemetry import REGISTRY

log = get_logger("health")
router = APIRouter(tags=["health"])

CheckStatus = Literal["ok", "degraded", "down"]
_CHECK_TIMEOUT_SECONDS = 2.0


class DependencyCheck(BaseModel):
    name: str
    status: CheckStatus
    latency_ms: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    version: str
    env: str
    region: str


class ReadinessResponse(BaseModel):
    status: CheckStatus
    checks: list[DependencyCheck] = Field(default_factory=list)


async def _check_database() -> DependencyCheck:
    started = time.perf_counter()
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS):
            async with get_sessionmaker()() as session:
                await session.execute(text("SELECT 1"))
        return DependencyCheck(
            name="postgres",
            status="ok",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    except TimeoutError:
        return DependencyCheck(name="postgres", status="down", detail="timeout")
    except Exception as exc:
        return DependencyCheck(name="postgres", status="down", detail=type(exc).__name__)


async def _check_redis(role: RedisRole) -> DependencyCheck:
    started = time.perf_counter()
    name = f"redis:{role}"
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS):
            await get_redis(role).ping()
        return DependencyCheck(
            name=name, status="ok", latency_ms=round((time.perf_counter() - started) * 1000, 2)
        )
    except TimeoutError:
        return DependencyCheck(name=name, status="down", detail="timeout")
    except Exception as exc:
        return DependencyCheck(name=name, status="down", detail=type(exc).__name__)


@router.get("/healthz", response_model=HealthResponse, summary="Liveness probe")
async def healthz() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        version="1.0.0",
        env=str(settings.env),
        region=settings.region,
    )


@router.get("/readyz", response_model=ReadinessResponse, summary="Readiness probe")
async def readyz(response: Response) -> ReadinessResponse:
    checks = list(
        await asyncio.gather(
            _check_database(),
            _check_redis(RedisRole.CACHE),
            _check_redis(RedisRole.SESSION),
        )
    )
    overall: CheckStatus = "ok" if all(c.status == "ok" for c in checks) else "down"
    if overall != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        log.warning("readiness_failed", failing=[c.name for c in checks if c.status != "ok"])
    return ReadinessResponse(status=overall, checks=checks)


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
