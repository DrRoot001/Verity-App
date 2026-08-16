"""Unified error model (PRD §22.3, §25.1, FR-ERR-001).

One error type serialized identically over HTTP and WebSocket. Every error
carries a machine-readable ``code`` and a ``recovery_action`` so no surface can
render a dead end. Generic messages are prohibited by review; the closed
``ErrorCode`` enum makes an un-categorised error impossible to construct.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    # ── Request / validation ─────────────────────────────────────────
    VALIDATION_FAILED = "validation_failed"
    MALFORMED_REQUEST = "malformed_request"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    PAYLOAD_TOO_LARGE = "payload_too_large"

    # ── Auth (PRD §10.2) ─────────────────────────────────────────────
    UNAUTHENTICATED = "unauthenticated"
    INVALID_CREDENTIALS = "invalid_credentials"
    EMAIL_NOT_VERIFIED = "email_not_verified"
    MFA_REQUIRED = "mfa_required"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_INVALID = "token_invalid"
    TOKEN_REUSE_DETECTED = "token_reuse_detected"
    ACCOUNT_LOCKED = "account_locked"
    ACCOUNT_SUSPENDED = "account_suspended"

    # ── Authorization (PRD §28.2) ────────────────────────────────────
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"

    # ── Conflict / state ─────────────────────────────────────────────
    CONFLICT = "conflict"
    STALE_RESOURCE = "stale_resource"
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
    INVALID_STATE_TRANSITION = "invalid_state_transition"

    # ── Entitlements / billing (PRD §19) ─────────────────────────────
    ENTITLEMENT_REQUIRED = "entitlement_required"
    ENTITLEMENT_EXHAUSTED = "entitlement_exhausted"
    SUBSCRIPTION_INACTIVE = "subscription_inactive"
    PAYMENT_REQUIRED = "payment_required"

    # ── Rate limiting (PRD §28.3) ────────────────────────────────────
    RATE_LIMITED = "rate_limited"

    # ── Ingestion (PRD §10.5) ────────────────────────────────────────
    FILE_TOO_LARGE = "file_too_large"
    FILE_TYPE_REJECTED = "file_type_rejected"
    FILE_INFECTED = "file_infected"
    EXTRACTION_FAILED = "extraction_failed"

    # ── AI / providers (PRD §20.3) ───────────────────────────────────
    AI_PROVIDER_UNAVAILABLE = "ai_provider_unavailable"
    AI_TIMEOUT = "ai_timeout"
    AI_SCHEMA_VALIDATION_FAILED = "ai_schema_validation_failed"
    AI_BUDGET_EXCEEDED = "ai_budget_exceeded"
    GROUNDING_EVIDENCE_MISSING = "grounding_evidence_missing"

    # ── Realtime (PRD §26, §34.1) ────────────────────────────────────
    SESSION_NOT_ACTIVE = "session_not_active"
    SESSION_TICKET_INVALID = "session_ticket_invalid"
    STT_UNAVAILABLE = "stt_unavailable"
    SESSION_CONCURRENCY_EXCEEDED = "session_concurrency_exceeded"

    # ── Infrastructure ───────────────────────────────────────────────
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    INTERNAL_ERROR = "internal_error"


RecoveryType = Literal[
    "retry",
    "reauthenticate",
    "verify_email",
    "upgrade",
    "grant_permission",
    "edit_input",
    "contact_support",
    "wait",
    "reconnect",
    "none",
]


class RecoveryAction(BaseModel):
    """What the user can actually do about it. Never omitted."""

    type: RecoveryType
    target: str | None = None
    label: str | None = None


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    recovery_action: RecoveryAction
    request_id: str | None = None
    retryable: bool = False


class ErrorEnvelope(BaseModel):
    error: ErrorBody


_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_FAILED: 400,
    ErrorCode.MALFORMED_REQUEST: 400,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: 415,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.INVALID_CREDENTIALS: 401,
    ErrorCode.EMAIL_NOT_VERIFIED: 403,
    ErrorCode.MFA_REQUIRED: 401,
    ErrorCode.TOKEN_EXPIRED: 401,
    ErrorCode.TOKEN_INVALID: 401,
    ErrorCode.TOKEN_REUSE_DETECTED: 401,
    ErrorCode.ACCOUNT_LOCKED: 423,
    ErrorCode.ACCOUNT_SUSPENDED: 403,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.STALE_RESOURCE: 409,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: 409,
    ErrorCode.INVALID_STATE_TRANSITION: 422,
    ErrorCode.ENTITLEMENT_REQUIRED: 403,
    ErrorCode.ENTITLEMENT_EXHAUSTED: 403,
    ErrorCode.SUBSCRIPTION_INACTIVE: 403,
    ErrorCode.PAYMENT_REQUIRED: 402,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.FILE_TOO_LARGE: 413,
    ErrorCode.FILE_TYPE_REJECTED: 415,
    ErrorCode.FILE_INFECTED: 422,
    ErrorCode.EXTRACTION_FAILED: 422,
    ErrorCode.AI_PROVIDER_UNAVAILABLE: 503,
    ErrorCode.AI_TIMEOUT: 504,
    ErrorCode.AI_SCHEMA_VALIDATION_FAILED: 502,
    ErrorCode.AI_BUDGET_EXCEEDED: 429,
    ErrorCode.GROUNDING_EVIDENCE_MISSING: 422,
    ErrorCode.SESSION_NOT_ACTIVE: 409,
    ErrorCode.SESSION_TICKET_INVALID: 401,
    ErrorCode.STT_UNAVAILABLE: 503,
    ErrorCode.SESSION_CONCURRENCY_EXCEEDED: 409,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}

_DEFAULT_RECOVERY: dict[ErrorCode, RecoveryAction] = {
    ErrorCode.UNAUTHENTICATED: RecoveryAction(type="reauthenticate", target="/login"),
    ErrorCode.INVALID_CREDENTIALS: RecoveryAction(type="edit_input", label="Check your details"),
    ErrorCode.EMAIL_NOT_VERIFIED: RecoveryAction(type="verify_email", target="/verify-email"),
    ErrorCode.TOKEN_EXPIRED: RecoveryAction(type="reauthenticate", target="/login"),
    ErrorCode.TOKEN_INVALID: RecoveryAction(type="reauthenticate", target="/login"),
    ErrorCode.TOKEN_REUSE_DETECTED: RecoveryAction(type="reauthenticate", target="/login"),
    ErrorCode.ACCOUNT_LOCKED: RecoveryAction(type="wait", label="Try again shortly"),
    ErrorCode.ACCOUNT_SUSPENDED: RecoveryAction(type="contact_support", target="/support"),
    ErrorCode.ENTITLEMENT_REQUIRED: RecoveryAction(type="upgrade", target="/settings/billing"),
    ErrorCode.ENTITLEMENT_EXHAUSTED: RecoveryAction(type="upgrade", target="/settings/billing"),
    ErrorCode.SUBSCRIPTION_INACTIVE: RecoveryAction(type="upgrade", target="/settings/billing"),
    ErrorCode.PAYMENT_REQUIRED: RecoveryAction(type="upgrade", target="/settings/billing"),
    ErrorCode.RATE_LIMITED: RecoveryAction(type="wait", label="Retry after the cooldown"),
    ErrorCode.AI_PROVIDER_UNAVAILABLE: RecoveryAction(type="retry", label="Try again"),
    ErrorCode.AI_TIMEOUT: RecoveryAction(type="retry", label="Try again"),
    ErrorCode.STT_UNAVAILABLE: RecoveryAction(type="reconnect", label="Reconnect"),
    ErrorCode.DEPENDENCY_UNAVAILABLE: RecoveryAction(type="retry", label="Try again"),
    ErrorCode.NOT_FOUND: RecoveryAction(type="none"),
    ErrorCode.FORBIDDEN: RecoveryAction(type="none"),
}

_RETRYABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.AI_PROVIDER_UNAVAILABLE,
        ErrorCode.AI_TIMEOUT,
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        ErrorCode.STT_UNAVAILABLE,
        ErrorCode.RATE_LIMITED,
        ErrorCode.INTERNAL_ERROR,
    }
)


class AppError(Exception):
    """The only exception type application code raises deliberately."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        recovery_action: RecoveryAction | None = None,
        status_code: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.recovery_action = (
            recovery_action
            or _DEFAULT_RECOVERY.get(code)
            or RecoveryAction(type="contact_support", target="/support")
        )
        self.status_code = status_code or _STATUS_BY_CODE.get(code, 500)
        self.retryable = retryable if retryable is not None else code in _RETRYABLE

    def to_body(self, request_id: str | None = None) -> ErrorBody:
        return ErrorBody(
            code=self.code,
            message=self.message,
            detail=self.detail,
            recovery_action=self.recovery_action,
            request_id=request_id,
            retryable=self.retryable,
        )

    def to_envelope(self, request_id: str | None = None) -> ErrorEnvelope:
        return ErrorEnvelope(error=self.to_body(request_id))

    # ── Ergonomic constructors used across modules ───────────────────
    @classmethod
    def not_found(cls, resource: str) -> Self:
        """404 for resources the actor may not know about (PRD AC-SEC-001)."""
        return cls(
            ErrorCode.NOT_FOUND,
            f"{resource} not found.",
            detail={"resource": resource},
        )

    @classmethod
    def validation(cls, message: str, *, fields: dict[str, Any] | None = None) -> Self:
        return cls(
            ErrorCode.VALIDATION_FAILED,
            message,
            detail={"fields": fields or {}},
            recovery_action=RecoveryAction(type="edit_input", label="Fix the highlighted fields"),
        )

    @classmethod
    def conflict(cls, message: str, *, detail: dict[str, Any] | None = None) -> Self:
        return cls(ErrorCode.CONFLICT, message, detail=detail)

    @classmethod
    def internal(cls, message: str = "An unexpected error occurred.") -> Self:
        return cls(
            ErrorCode.INTERNAL_ERROR,
            message,
            recovery_action=RecoveryAction(type="retry", label="Try again"),
        )
