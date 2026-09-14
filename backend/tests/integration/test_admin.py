"""Admin surface (PRD §18, AC-ADMIN-001).

The claims worth testing here are all negative: what a non-staff caller sees,
what a staff caller with the wrong role cannot do, and what happens when a
privileged action arrives with no stated reason.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from verity.modules.admin.models import ROLE_PERMISSIONS, StaffRole

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple-7"


class Account:
    def __init__(self, client: Any, role: str | None = None) -> None:
        self.client = client
        self.email = f"admin-{uuid.uuid4().hex[:10]}@verityqa.dev"
        ip = {"X-Forwarded-For": f"203.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"}

        client.post(
            "/v1/auth/signup",
            json={"email": self.email, "password": PASSWORD, "full_name": "Staffer"},
            headers=ip,
        )
        self._verify()
        tokens = client.post(
            "/v1/auth/login", json={"email": self.email, "password": PASSWORD}, headers=ip
        ).json()["tokens"]
        self.headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        if role:
            self._grant(role)

    def _verify(self) -> None:
        matches = sorted(
            Path(".data/outbox").glob(f"{self.email}-*.txt"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        token = re.search(r"verify-email\?token=([\w\-]+)", matches[0].read_text())
        assert token
        self.client.post("/v1/auth/verify-email", json={"token": token.group(1)})

    def _grant(self, role: str) -> None:
        """Granted directly in the database.

        There is deliberately no API that mints staff access, so this is the
        only way in — which is the point: privilege cannot be escalated over
        HTTP even by an existing admin.
        """
        import asyncio

        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from verity.platform.config import settings

        async def run() -> None:
            engine = create_async_engine(settings.database_url, poolclass=NullPool)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO staff_members "
                            "(id, user_id, role, created_at, updated_at) "
                            "SELECT gen_random_uuid(), id, :role, now(), now() "
                            "FROM users WHERE email = :email"
                        ),
                        {"role": role, "email": self.email},
                    )
            finally:
                await engine.dispose()

        # TestClient drives its own loop on a portal thread, so this thread is
        # free to run one of its own.
        asyncio.run(run())


@pytest.fixture
def admin(api_client: Any) -> Account:
    return Account(api_client, StaffRole.ADMIN)


# ── Existence is not confirmed to outsiders ──────────────────────────


def test_a_non_staff_user_gets_404_not_403(api_client: Any) -> None:
    """A 403 would tell a curious customer the admin API is real and reachable."""
    user = Account(api_client)

    response = user.client.get("/v1/admin/metrics", headers=user.headers)

    assert response.status_code == 404


def test_an_anonymous_caller_is_rejected(api_client: Any) -> None:
    assert api_client.get("/v1/admin/metrics").status_code == 401


# ── Least privilege (FR-ADMIN-001) ───────────────────────────────────


def test_support_cannot_suspend_a_user(api_client: Any) -> None:
    support = Account(api_client, StaffRole.SUPPORT)
    victim = Account(api_client)
    victim_id = support.client.get("/v1/users/me", headers=victim.headers).json()["id"]

    response = support.client.post(
        f"/v1/admin/users/{victim_id}/suspend",
        json={"reason": "spam account reported", "case_id": "CASE-1"},
        headers=support.headers,
    )

    assert response.status_code == 403


def test_security_cannot_read_interview_content(api_client: Any) -> None:
    """Roles are sets, not tiers: security outranks support on suspension and
    still has no business reading a transcript."""
    assert "session.read_content" not in ROLE_PERMISSIONS[StaffRole.SECURITY]
    assert "session.read_content" not in ROLE_PERMISSIONS[StaffRole.ADMIN]


def test_only_admin_can_manage_staff(admin: Account, api_client: Any) -> None:
    support = Account(api_client, StaffRole.SUPPORT)
    candidate = Account(api_client)
    candidate_id = candidate.client.get("/v1/users/me", headers=candidate.headers).json()["id"]

    denied = support.client.post(
        "/v1/admin/staff",
        json={"user_id": candidate_id, "role": "ai_ops"},
        headers=support.headers,
    )
    assert denied.status_code == 403

    granted = admin.client.post(
        "/v1/admin/staff",
        json={"user_id": candidate_id, "role": "ai_ops"},
        headers=admin.headers,
    )
    assert granted.status_code == 201
    assert granted.json()["role"] == "ai_ops"


def test_ai_settings_are_persistent_and_audited(admin: Account) -> None:
    current = admin.client.get("/v1/admin/settings/ai", headers=admin.headers)
    assert current.status_code == 200
    payload = current.json()
    original_budget = payload["daily_budget_usd"]
    payload["daily_budget_usd"] = 321.0
    payload.pop("groq_key_configured")
    payload.pop("anthropic_key_configured")

    saved = admin.client.put("/v1/admin/settings/ai", json=payload, headers=admin.headers)
    assert saved.status_code == 200
    assert saved.json()["daily_budget_usd"] == 321.0

    reread = admin.client.get("/v1/admin/settings/ai", headers=admin.headers)
    assert reread.json()["daily_budget_usd"] == 321.0
    audit = admin.client.get("/v1/admin/audit", headers=admin.headers).json()
    assert any(entry["action"] == "ai.settings.update" for entry in audit)
    payload["daily_budget_usd"] = original_budget
    admin.client.put("/v1/admin/settings/ai", json=payload, headers=admin.headers)


def test_feature_flag_changes_are_persistent(admin: Account) -> None:
    flags = admin.client.get("/v1/admin/flags", headers=admin.headers)
    assert flags.status_code == 200
    target = next(flag for flag in flags.json() if flag["key"] == "mock_interviews")

    changed = admin.client.put(
        "/v1/admin/flags/mock_interviews",
        json={**target, "enabled": False, "rollout_percentage": 0},
        headers=admin.headers,
    )
    assert changed.status_code == 200
    assert changed.json()["enabled"] is False
    admin.client.put(
        "/v1/admin/flags/mock_interviews",
        json={**target, "enabled": True, "rollout_percentage": 100},
        headers=admin.headers,
    )


# ── Audit (FR-ADMIN-002) ─────────────────────────────────────────────


def test_suspension_requires_a_reason_and_a_case(admin: Account, api_client: Any) -> None:
    victim = Account(api_client)
    victim_id = admin.client.get("/v1/users/me", headers=victim.headers).json()["id"]

    response = admin.client.post(
        f"/v1/admin/users/{victim_id}/suspend",
        json={"reason": "", "case_id": ""},
        headers=admin.headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_a_suspension_is_recorded_with_its_reason(admin: Account, api_client: Any) -> None:
    victim = Account(api_client)
    victim_id = admin.client.get("/v1/users/me", headers=victim.headers).json()["id"]

    suspended = admin.client.post(
        f"/v1/admin/users/{victim_id}/suspend",
        json={"reason": "confirmed abuse of the trial", "case_id": "CASE-4471"},
        headers=admin.headers,
    )
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "suspended"

    audit = admin.client.get("/v1/admin/audit", headers=admin.headers).json()
    entry = next(e for e in audit if e["resource_id"] == victim_id)
    assert entry["action"] == "user.suspend"
    assert entry["reason"] == "confirmed abuse of the trial"
    assert entry["case_id"] == "CASE-4471"


def test_metrics_report_the_activation_loop(admin: Account) -> None:
    metrics = admin.client.get("/v1/admin/metrics", headers=admin.headers).json()

    assert metrics["users_total"] >= 1
    assert "activated_users" in metrics
