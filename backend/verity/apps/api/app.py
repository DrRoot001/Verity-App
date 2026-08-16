"""FastAPI application factory (PRD §22.2).

``apps/`` contains wiring only — no business logic. Routers come from
``verity.modules.*``; this module composes them with middleware, exception
handlers and lifecycle management.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from verity.apps.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from verity.modules.candidate_graph.router import profile_router, stories_router
from verity.modules.health.router import router as health_router
from verity.modules.identity.router import auth_router, users_router
from verity.modules.workspace.router import resumes_router, workspaces_router
from verity.platform.cache import close_redis
from verity.platform.config import settings
from verity.platform.db.session import dispose_engine, warm_pool
from verity.platform.errors import AppError, ErrorCode, RecoveryAction
from verity.platform.logging import configure_logging, get_logger
from verity.platform.telemetry import configure_tracing

log = get_logger("api.app")

API_PREFIX = "/v1"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    configure_tracing()
    log.info("api_starting", env=str(settings.env), region=settings.region)
    try:
        await warm_pool()
        log.info("db_pool_warm")
    except Exception as exc:
        # Do not crash the process: readiness will report down and the
        # orchestrator will hold traffic until the dependency recovers.
        log.error("db_pool_warm_failed", error_type=type(exc).__name__)
    try:
        yield
    finally:
        await close_redis()
        await dispose_engine()
        log.info("api_stopped")


def _error_response(error: AppError, request: Request) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=error.status_code,
        content=error.to_envelope(request_id).model_dump(mode="json"),
        headers={"X-Request-ID": request_id} if request_id else None,
    )


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="Verity API",
        version="1.0.0",
        description="Interview Workspace Platform API",
        docs_url="/docs" if not settings.env.is_production_like else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.env.is_production_like else None,
        lifespan=lifespan,
    )

    # Order matters: security headers wrap the outermost response.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )

    # ── Exception handlers: everything becomes an AppError envelope ──
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            log.error("app_error", code=str(exc.code), status=exc.status_code)
        else:
            log.info("app_error", code=str(exc.code), status=exc.status_code)
        return _error_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = {
            ".".join(str(p) for p in err["loc"][1:]) or "body": err["msg"] for err in exc.errors()
        }
        return _error_response(
            AppError.validation("The request could not be validated.", fields=fields), request
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.INTERNAL_ERROR
        if exc.status_code == 405:
            code = ErrorCode.MALFORMED_REQUEST
        error = AppError(
            code,
            str(exc.detail) if exc.detail else "Request could not be completed.",
            status_code=exc.status_code,
            recovery_action=RecoveryAction(type="none"),
        )
        return _error_response(error, request)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception", error_type=type(exc).__name__, exc_info=exc)
        return _error_response(AppError.internal(), request)

    # ── Routers ──────────────────────────────────────────────────────
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(profile_router)
    app.include_router(stories_router)
    app.include_router(resumes_router)
    app.include_router(workspaces_router)

    return app


app = create_app()
