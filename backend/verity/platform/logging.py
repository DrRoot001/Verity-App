"""Structured logging (PRD §30.1).

Logs are JSON with ``request_id``/``session_id``/``user_ref``/``module``/``event``.
Interview content is never logged: ``user_id`` is emitted as a salted pseudonym
and a redaction processor drops any key on the content deny-list.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

from verity.platform.config import settings

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_session_id: ContextVar[str | None] = ContextVar("session_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("user_id", default=None)

# Keys that may carry interview/candidate content. Never logged. (PRD §30.1)
_REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "transcript",
        "transcript_text",
        "text",
        "raw_text",
        "answer",
        "answer_text",
        "content",
        "resume_text",
        "jd_text",
        "prompt",
        "completion",
        "password",
        "token",
        "refresh_token",
        "access_token",
        "api_key",
        "secret",
        "email",
        "contact",
    }
)


def set_request_context(
    *,
    request_id: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> None:
    if request_id is not None:
        _request_id.set(request_id)
    if session_id is not None:
        _session_id.set(session_id)
    if user_id is not None:
        _user_id.set(user_id)


def clear_request_context() -> None:
    _request_id.set(None)
    _session_id.set(None)
    _user_id.set(None)


def get_request_id() -> str | None:
    return _request_id.get()


def pseudonymize(value: str) -> str:
    """Stable, non-reversible reference for correlating logs without exposing IDs."""
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).hexdigest()
    return f"u_{digest}"


def _inject_context(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    if rid := _request_id.get():
        event_dict.setdefault("request_id", rid)
    if sid := _session_id.get():
        event_dict.setdefault("session_id", sid)
    if uid := _user_id.get():
        event_dict.setdefault("user_ref", pseudonymize(uid))
    event_dict.setdefault("service", settings.service_name)
    event_dict.setdefault("env", str(settings.env))
    return event_dict


def _redact(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    for key in list(event_dict.keys()):
        if key.lower() in _REDACTED_KEYS:
            value = event_dict[key]
            event_dict[key] = (
                f"<redacted:{len(value)}chars>" if isinstance(value, str) else "<redacted>"
            )
    return event_dict


def configure_logging() -> None:
    """Idempotent logging setup. Safe to call from every entrypoint."""
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_context,
        _redact,
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy) through the same sink.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.getLevelNamesMapping()[settings.log_level],
        force=True,
    )
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).handlers = []
        logging.getLogger(noisy).propagate = True


def get_logger(module: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger().bind(module=module)
    return logger
