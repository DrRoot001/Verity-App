"""Health surface and the global error contract (PRD §30, §38.3, FR-ERR-001)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


def test_healthz_is_dependency_free(api_client: Any) -> None:
    """Liveness must answer even when Postgres and Redis are unreachable."""
    response = api_client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"]
    assert body["env"]


def test_request_id_is_returned_on_every_response(api_client: Any) -> None:
    response = api_client.get("/healthz")
    assert response.headers["X-Request-ID"].startswith("req_")


def test_supplied_request_id_is_propagated(api_client: Any) -> None:
    response = api_client.get("/healthz", headers={"X-Request-ID": "req_client_supplied"})
    assert response.headers["X-Request-ID"] == "req_client_supplied"


def test_oversized_request_id_is_replaced(api_client: Any) -> None:
    response = api_client.get("/healthz", headers={"X-Request-ID": "x" * 500})
    assert response.headers["X-Request-ID"].startswith("req_")


@pytest.mark.parametrize(
    "header",
    [
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Content-Security-Policy",
        "Permissions-Policy",
    ],
)
def test_security_headers_present(api_client: Any, header: str) -> None:
    assert header in api_client.get("/healthz").headers


def test_unknown_route_returns_error_envelope(api_client: Any) -> None:
    """FR-ERR-001: even a 404 carries a code, recovery action and request id."""
    response = api_client.get("/v1/does-not-exist")
    assert response.status_code == 404

    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["recovery_action"]["type"]
    assert error["request_id"]
    assert error["retryable"] is False


def test_method_not_allowed_returns_envelope(api_client: Any) -> None:
    response = api_client.post("/healthz")
    assert response.status_code == 405
    assert "error" in response.json()


def test_metrics_endpoint_exposes_prometheus_format(api_client: Any) -> None:
    response = api_client.get("/metrics")
    assert response.status_code == 200
    assert "verity_http_requests_total" in response.text


def test_metrics_are_recorded_with_templated_route_label(api_client: Any) -> None:
    """Metric labels must use the route template, not the concrete URL."""
    api_client.get("/healthz")
    body = api_client.get("/metrics").text
    assert 'route="/healthz"' in body


@pytest.mark.integration
def test_readyz_reports_dependency_health(api_client: TestClient) -> None:
    """Requires Postgres and Redis. Verifies the probe reports real state."""
    response = api_client.get("/readyz")
    body = response.json()

    names = {check["name"] for check in body["checks"]}
    assert {"postgres", "redis:cache", "redis:session"} <= names

    if body["status"] == "ok":
        assert response.status_code == 200
        assert all(c["latency_ms"] is not None for c in body["checks"])
    else:
        # A failing probe must return 503 so the rollout health gate reacts.
        assert response.status_code == 503
