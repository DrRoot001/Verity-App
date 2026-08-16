"""Auth flows end to end (PRD §10.2, §28.2, §28.3).

Exercised against the running app with real Postgres and Redis. The security
properties here — enumeration resistance, refresh rotation, immediate session
revocation — are the ones that fail silently if only unit-tested.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.integration

STRONG_PASSWORD = "correct-horse-battery-staple-7"


def _email() -> str:
    # Not a reserved TLD: EmailStr rejects .test/.invalid/.local, which is
    # correct production validation and must not be weakened for tests.
    return f"auth-{uuid.uuid4().hex[:12]}@verityqa.dev"


def _client_ip() -> dict[str, str]:
    """A distinct source IP per caller.

    Per-IP login limits are real and must stay enabled, but every test would
    otherwise share the TestClient's single address and exhaust one bucket.
    """
    octet = uuid.uuid4().int
    return {"X-Forwarded-For": f"198.51.{(octet >> 8) % 256}.{octet % 256}"}


def _signup(client: Any, email: str, password: str = STRONG_PASSWORD, **kw: Any) -> Any:
    return client.post(
        "/v1/auth/signup",
        json={"email": email, "password": password, "full_name": "Test Candidate"},
        headers=kw.pop("headers", None) or _client_ip(),
    )


def _login(client: Any, email: str, password: str = STRONG_PASSWORD, **kw: Any) -> Any:
    return client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
        headers=kw.pop("headers", None) or _client_ip(),
    )


# ── Registration ─────────────────────────────────────────────────────


def test_signup_succeeds_and_returns_neutral_message(api_client: Any) -> None:
    response = _signup(api_client, _email())
    assert response.status_code == 202
    assert "sent an email" in response.json()["message"]


def test_signup_with_existing_email_is_indistinguishable(api_client: Any) -> None:
    """PRD §28.3: signup must not reveal whether an address is registered."""
    email = _email()
    first = _signup(api_client, email)
    second = _signup(api_client, email)

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()


def test_weak_password_is_rejected_with_field_detail(api_client: Any) -> None:
    response = _signup(api_client, _email(), password="short")
    assert response.status_code == 400

    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert "password" in error["detail"]["fields"]
    assert error["recovery_action"]["type"] == "edit_input"


def test_invalid_timezone_is_rejected(api_client: Any) -> None:
    response = api_client.post(
        "/v1/auth/signup",
        json={"email": _email(), "password": STRONG_PASSWORD, "timezone": "Mars/Olympus"},
    )
    assert response.status_code == 400


# ── Login ────────────────────────────────────────────────────────────


def test_login_returns_tokens_and_user(api_client: Any) -> None:
    email = _email()
    _signup(api_client, email)

    response = _login(api_client, email)
    assert response.status_code == 200

    body = response.json()
    assert body["user"]["email"] == email
    assert body["user"]["email_verified"] is False
    assert body["tokens"]["token_type"] == "Bearer"
    assert body["tokens"]["expires_in"] > 0
    assert body["tokens"]["access_token"]
    assert body["tokens"]["refresh_token"]


def test_wrong_password_and_unknown_account_return_identical_errors(api_client: Any) -> None:
    """The response must not distinguish 'no such user' from 'bad password'."""
    email = _email()
    _signup(api_client, email)

    wrong_password = _login(api_client, email, password="definitely-not-the-password")
    unknown_account = _login(api_client, _email(), password="definitely-not-the-password")

    assert wrong_password.status_code == unknown_account.status_code == 401
    assert wrong_password.json()["error"]["code"] == unknown_account.json()["error"]["code"]
    assert wrong_password.json()["error"]["message"] == unknown_account.json()["error"]["message"]


# ── Authenticated access ─────────────────────────────────────────────


def test_me_requires_authentication(api_client: Any) -> None:
    response = api_client.get("/v1/users/me")
    assert response.status_code == 401
    assert response.json()["error"]["recovery_action"]["type"] == "reauthenticate"


def test_me_returns_the_authenticated_user(api_client: Any) -> None:
    email = _email()
    _signup(api_client, email)
    tokens = _login(api_client, email).json()["tokens"]

    response = api_client.get(
        "/v1/users/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert response.status_code == 200
    assert response.json()["email"] == email


def test_malformed_bearer_token_is_rejected(api_client: Any) -> None:
    response = api_client.get("/v1/users/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] in {"token_invalid", "unauthenticated"}


def test_profile_update_persists(api_client: Any) -> None:
    email = _email()
    _signup(api_client, email)
    tokens = _login(api_client, email).json()["tokens"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    updated = api_client.patch(
        "/v1/users/me",
        json={"full_name": "Updated Name", "timezone": "Europe/Berlin"},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["full_name"] == "Updated Name"

    refetched = api_client.get("/v1/users/me", headers=headers)
    assert refetched.json()["timezone"] == "Europe/Berlin"


# ── Refresh rotation and reuse detection (PRD §28.2) ─────────────────


def test_refresh_rotates_the_token(api_client: Any) -> None:
    email = _email()
    _signup(api_client, email)
    original = _login(api_client, email).json()["tokens"]

    rotated = api_client.post(
        "/v1/auth/refresh",
        json={"refresh_token": original["refresh_token"]},
        headers=_client_ip(),
    )
    assert rotated.status_code == 200
    assert rotated.json()["tokens"]["refresh_token"] != original["refresh_token"]


def test_replaying_a_rotated_refresh_token_revokes_the_family(api_client: Any) -> None:
    """A captured token replayed after rotation means theft; kill everything."""
    email = _email()
    _signup(api_client, email)
    original = _login(api_client, email).json()["tokens"]

    rotated = api_client.post(
        "/v1/auth/refresh",
        json={"refresh_token": original["refresh_token"]},
        headers=_client_ip(),
    ).json()["tokens"]

    replay = api_client.post(
        "/v1/auth/refresh",
        json={"refresh_token": original["refresh_token"]},
        headers=_client_ip(),
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "token_reuse_detected"

    # The token issued to the legitimate holder is dead too — we cannot tell
    # which party is the attacker.
    after = api_client.post(
        "/v1/auth/refresh",
        json={"refresh_token": rotated["refresh_token"]},
        headers=_client_ip(),
    )
    assert after.status_code == 401


def test_unknown_refresh_token_is_rejected(api_client: Any) -> None:
    response = api_client.post(
        "/v1/auth/refresh", json={"refresh_token": "x" * 40}, headers=_client_ip()
    )
    assert response.status_code == 401


# ── Session revocation ───────────────────────────────────────────────


def test_logout_invalidates_the_access_token_immediately(api_client: Any) -> None:
    """Revocation must not wait for access-token expiry (FR-AUTH-007)."""
    email = _email()
    _signup(api_client, email)
    tokens = _login(api_client, email).json()["tokens"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    assert api_client.get("/v1/users/me", headers=headers).status_code == 200
    assert api_client.post("/v1/auth/logout", headers=headers).status_code == 204
    assert api_client.get("/v1/users/me", headers=headers).status_code == 401


def test_logout_all_revokes_every_session(api_client: Any) -> None:
    email = _email()
    _signup(api_client, email)
    first = _login(api_client, email).json()["tokens"]
    second = _login(api_client, email).json()["tokens"]

    revoke = api_client.post(
        "/v1/auth/devices/revoke-all",
        headers={"Authorization": f"Bearer {first['access_token']}"},
    )
    assert revoke.status_code == 200

    for tokens in (first, second):
        response = api_client.get(
            "/v1/users/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        assert response.status_code == 401


def test_devices_are_recorded_per_platform(api_client: Any) -> None:
    email = _email()
    _signup(api_client, email)
    api_client.post(
        "/v1/auth/login",
        json={
            "email": email,
            "password": STRONG_PASSWORD,
            "platform": "macos",
            "device_name": "Studio Mac",
            "app_version": "1.2.0",
        },
        headers=_client_ip(),
    )
    tokens = _login(api_client, email).json()["tokens"]

    devices = api_client.get(
        "/v1/auth/devices", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    ).json()

    platforms = {d["platform"] for d in devices}
    assert {"macos", "web"} <= platforms
    assert any(d["name"] == "Studio Mac" and d["app_version"] == "1.2.0" for d in devices)


# ── Password reset ───────────────────────────────────────────────────


def test_password_reset_request_is_neutral_for_unknown_addresses(api_client: Any) -> None:
    known = _email()
    _signup(api_client, known)

    for address in (known, _email()):
        response = api_client.post(
            "/v1/auth/password/reset-request", json={"email": address}, headers=_client_ip()
        )
        assert response.status_code == 200
        assert "sent an email" in response.json()["message"]


def test_reset_with_an_invalid_token_offers_a_recovery_path(api_client: Any) -> None:
    response = api_client.post(
        "/v1/auth/password/reset", json={"token": "y" * 40, "password": STRONG_PASSWORD}
    )
    assert response.status_code == 401
    assert response.json()["error"]["recovery_action"]["type"] == "retry"


def test_email_verification_with_invalid_token_is_recoverable(api_client: Any) -> None:
    response = api_client.post("/v1/auth/verify-email", json={"token": "z" * 40})
    assert response.status_code == 401
    assert response.json()["error"]["recovery_action"]["type"] == "verify_email"
