"""Error model contract (PRD §22.3, FR-ERR-001)."""

from __future__ import annotations

import pytest

from verity.platform.errors import AppError, ErrorCode, RecoveryAction


def test_every_error_carries_a_recovery_action() -> None:
    """FR-ERR-001: no error may be a dead end, including ones with no default."""
    for code in ErrorCode:
        error = AppError(code, "message")
        assert error.recovery_action is not None
        assert error.recovery_action.type


def test_status_codes_are_mapped_for_every_code() -> None:
    for code in ErrorCode:
        error = AppError(code, "message")
        assert 400 <= error.status_code <= 599, code


def test_not_found_is_used_for_cross_tenant_access() -> None:
    """AC-SEC-001: resources owned by another user return 404, never 403."""
    error = AppError.not_found("Workspace")
    assert error.status_code == 404
    assert error.code is ErrorCode.NOT_FOUND


def test_envelope_shape_matches_api_contract() -> None:
    error = AppError(
        ErrorCode.ENTITLEMENT_EXHAUSTED,
        "You've used all live interview minutes for this period.",
        detail={"entitlement": "live_minutes", "remaining": 0},
    )
    payload = error.to_envelope("req_123").model_dump(mode="json")

    assert set(payload) == {"error"}
    body = payload["error"]
    assert body["code"] == "entitlement_exhausted"
    assert body["detail"]["entitlement"] == "live_minutes"
    assert body["recovery_action"]["type"] == "upgrade"
    assert body["request_id"] == "req_123"
    assert body["retryable"] is False


def test_transient_failures_are_marked_retryable() -> None:
    assert AppError(ErrorCode.AI_TIMEOUT, "timeout").retryable is True
    assert AppError(ErrorCode.VALIDATION_FAILED, "bad").retryable is False


def test_explicit_recovery_action_overrides_default() -> None:
    error = AppError(
        ErrorCode.RATE_LIMITED,
        "Too many requests.",
        recovery_action=RecoveryAction(type="wait", label="Retry in 30s"),
    )
    assert error.recovery_action.label == "Retry in 30s"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ErrorCode.UNAUTHENTICATED, 401),
        (ErrorCode.FORBIDDEN, 403),
        (ErrorCode.PAYMENT_REQUIRED, 402),
        (ErrorCode.RATE_LIMITED, 429),
        (ErrorCode.AI_PROVIDER_UNAVAILABLE, 503),
        (ErrorCode.AI_TIMEOUT, 504),
    ],
)
def test_representative_status_mappings(code: ErrorCode, expected: int) -> None:
    assert AppError(code, "m").status_code == expected
