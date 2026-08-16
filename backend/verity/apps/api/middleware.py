"""HTTP middleware: request context, metrics, security headers (PRD §28.3, §30)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from verity.platform.config import settings
from verity.platform.logging import (
    clear_request_context,
    get_logger,
    set_request_context,
)
from verity.platform.telemetry import http_latency, http_requests

log = get_logger("api.http")

REQUEST_ID_HEADER = "X-Request-ID"


def _route_label(request: Request) -> str:
    """Template path, not the concrete URL — keeps metric cardinality bounded."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, binds log context, records RED metrics."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming if incoming and len(incoming) <= 64 else f"req_{uuid.uuid4().hex}"
        set_request_context(request_id=request_id)
        request.state.request_id = request_id

        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = _route_label(request)
            http_requests.labels(method=request.method, route=route, status=str(status)).inc()
            http_latency.labels(method=request.method, route=route).observe(elapsed)
            if route != "unmatched" or status != 404:
                log.info(
                    "http_request",
                    method=request.method,
                    route=route,
                    status=status,
                    duration_ms=round(elapsed * 1000, 2),
                )
            clear_request_context()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline hardening headers (PRD §28.3).

    The API serves JSON only, so its CSP is maximally restrictive; the web app
    ships its own nonce-based policy.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
        )
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if settings.env.is_production_like:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"
            )
        return response
