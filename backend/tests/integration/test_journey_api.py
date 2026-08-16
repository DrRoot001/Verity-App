"""End-to-end journey over HTTP (PRD §43 E1).

signup → verify-gated ingestion → review/approve → Workspace → JD →
ContextBundle. This is the first test that exercises the whole spine through
the public API rather than through services, so it is what proves the phases
actually compose.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple-7"

RESUME = """
Dana Whitfield
dana@example.org

SUMMARY
Backend engineer focused on payments reliability.

WORK EXPERIENCE

Senior Backend Engineer at Northwind Systems
March 2021 - Present
- Operated Kubernetes clusters and Python services in production
- Cut checkout latency from 800ms to 120ms with a read-through cache

EDUCATION
BSc Computer Science, Fairhaven University, 2014 - 2018

TECHNICAL SKILLS
Python, PostgreSQL, k8s, Kafka
"""

JD = """
Senior Backend Engineer

Requirements
- 5+ years of backend engineering experience
- Strong experience with Python and PostgreSQL
- Experience operating Kubernetes in production

Nice to have
- Experience with Kafka and event-driven architectures
"""


def _ip() -> dict[str, str]:
    octet = uuid.uuid4().int
    return {"X-Forwarded-For": f"198.51.{(octet >> 8) % 256}.{octet % 256}"}


class Journey:
    """A signed-in client, so each test reads as a user story."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.email = f"journey-{uuid.uuid4().hex[:10]}@verityqa.dev"
        client.post(
            "/v1/auth/signup",
            json={"email": self.email, "password": PASSWORD, "full_name": "Dana"},
            headers=_ip(),
        )
        tokens = client.post(
            "/v1/auth/login", json={"email": self.email, "password": PASSWORD}, headers=_ip()
        ).json()["tokens"]
        self.headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    def get(self, path: str, **kw: Any) -> Any:
        return self.client.get(path, headers=self.headers, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.client.post(path, headers=self.headers, **kw)

    def patch(self, path: str, **kw: Any) -> Any:
        return self.client.patch(path, headers=self.headers, **kw)


@pytest.fixture
def journey(api_client: Any) -> Journey:
    return Journey(api_client)


# ── Verification gating (FR-AUTH-001) ────────────────────────────────


def test_ai_operations_require_a_verified_email(journey: Journey) -> None:
    """Verification gates AI work, not browsing."""
    assert journey.get("/v1/profile").status_code == 200  # browsing is fine

    blocked = journey.post("/v1/resumes/paste", json={"raw_text": RESUME})
    assert blocked.status_code == 403

    error = blocked.json()["error"]
    assert error["code"] == "email_not_verified"
    assert error["recovery_action"]["type"] == "verify_email"


# ── The journey ──────────────────────────────────────────────────────


def _verify(journey: Journey) -> None:
    """Complete verification the way a real user does.

    The console email provider writes each message to ``.data/outbox``; reading
    the link from there exercises the genuine signup → email → verify path
    instead of reaching into the database to fake the end state.
    """
    import pathlib
    import re

    outbox = pathlib.Path(".data/outbox")
    matches = sorted(
        outbox.glob(f"{journey.email}-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    assert matches, f"no verification email was written for {journey.email}"

    token = re.search(r"verify-email\?token=([\w\-]+)", matches[0].read_text())
    assert token, "verification email did not contain a token link"

    response = journey.client.post("/v1/auth/verify-email", json={"token": token.group(1)})
    assert response.status_code == 200, response.text
    assert response.json()["email_verified"] is True


def test_full_journey_to_context_bundle(journey: Journey) -> None:
    _verify(journey)

    # 1. Ingest a resume.
    ingestion = journey.post("/v1/resumes/paste", json={"raw_text": RESUME})
    assert ingestion.status_code == 201
    body = ingestion.json()
    assert body["experiences_found"] >= 1
    assert body["version"]["status"] == "needs_review"

    # 2. Nothing is usable until reviewed.
    review = journey.get("/v1/profile/review").json()
    assert review["pending_count"] > 0
    assert review["approved_count"] == 0
    assert all(n["provenance"]["status"] == "pending_review" for n in review["pending"])

    # 3. Approve the experiences and skills.
    for entity_type in ("experience", "skill"):
        ids = [n["id"] for n in review["pending"] if n["entity_type"] == entity_type]
        if ids:
            approved = journey.post(
                "/v1/profile/bulk-approve", json={"entity_type": entity_type, "ids": ids}
            )
            assert approved.status_code == 200
            assert approved.json()["approved"] == len(ids)

    # 4. Create a Workspace with the JD.
    workspace = journey.post(
        "/v1/workspaces",
        json={
            "company_name": "Northwind Systems",
            "role_title": "Senior Backend Engineer",
            "jd_text": JD,
        },
    )
    assert workspace.status_code == 201
    workspace_id = workspace.json()["id"]
    assert workspace.json()["live_copilot_allowed"] is True

    # 5. The ContextBundle carries approved evidence and a bound match.
    context = journey.get(f"/v1/workspaces/{workspace_id}/context")
    assert context.status_code == 200

    bundle = context.json()
    assert bundle["candidate"]["experiences"], "approved experience must reach the bundle"
    assert bundle["opportunity"]["jd_present"] is True
    assert bundle["inferred_opportunity"] is False
    assert bundle["match"]["overall_score"] > 0
    assert bundle["match"]["requirements"], "requirements must be bound to evidence"
    assert all(r["rationale"] for r in bundle["match"]["requirements"])

    # Raw documents never enter the bundle.
    assert "read-through cache" not in str(bundle["candidate"])


def test_workspace_without_jd_reports_inferred_context(journey: Journey) -> None:
    _verify(journey)
    workspace = journey.post(
        "/v1/workspaces", json={"company_name": "Undisclosed", "role_title": "Engineer"}
    ).json()

    bundle = journey.get(f"/v1/workspaces/{workspace['id']}/context").json()

    assert bundle["inferred_opportunity"] is True
    assert "no_job_description" in bundle["warnings"]
    assert bundle["match"]["computed_without_jd"] is True
    assert bundle["match"]["overall_score"] == 0.0


def test_proctored_workspace_disables_live_copilot(journey: Journey) -> None:
    _verify(journey)
    workspace = journey.post(
        "/v1/workspaces",
        json={
            "company_name": "Northwind",
            "role_title": "Engineer",
            "integrity_mode": "proctored",
        },
    ).json()

    assert workspace["live_copilot_allowed"] is False


def test_editing_a_node_records_a_user_correction(journey: Journey) -> None:
    _verify(journey)
    journey.post("/v1/resumes/paste", json={"raw_text": RESUME})

    experiences = journey.get("/v1/profile/experience").json()
    assert experiences

    updated = journey.patch(
        f"/v1/profile/experience/{experiences[0]['id']}",
        json={"title": "Staff Backend Engineer"},
    )
    assert updated.status_code == 200
    assert updated.json()["detail"]["title"] == "Staff Backend Engineer"
    assert updated.json()["provenance"]["has_user_corrections"] is True


def test_rejected_nodes_are_retained_not_deleted(journey: Journey) -> None:
    """FR-ONB-003: rejection is a recorded decision, not a delete."""
    _verify(journey)
    journey.post("/v1/resumes/paste", json={"raw_text": RESUME})

    experiences = journey.get("/v1/profile/experience").json()
    rejected = journey.post(f"/v1/profile/experience/{experiences[0]['id']}/reject")

    assert rejected.status_code == 200
    assert rejected.json()["provenance"]["status"] == "rejected"

    still_there = journey.get("/v1/profile/experience?status=rejected").json()
    assert len(still_there) == 1


def test_story_needs_a_result_before_approval(journey: Journey) -> None:
    """FR-STORY-003: an interview story without an outcome is not usable."""
    _verify(journey)
    story = journey.post(
        "/v1/stories",
        json={"title": "A story with no result", "situation": "Something happened"},
    ).json()

    blocked = journey.post(f"/v1/stories/{story['id']}/approve")
    assert blocked.status_code == 400
    assert "result" in blocked.json()["error"]["detail"]["missing"]


def test_approved_story_appears_in_the_bundle(journey: Journey) -> None:
    _verify(journey)
    story = journey.post(
        "/v1/stories",
        json={
            "title": "Recovering a failed migration",
            "situation": "A schema migration locked the primary table",
            "result": "Restored service in eleven minutes",
            "categories": ["crisis_recovery"],
        },
    ).json()
    assert story["speak_time_seconds"] > 0

    approved = journey.post(f"/v1/stories/{story['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    workspace = journey.post(
        "/v1/workspaces", json={"company_name": "Northwind", "role_title": "Engineer"}
    ).json()
    bundle = journey.get(f"/v1/workspaces/{workspace['id']}/context").json()

    titles = {s["title"] for s in bundle["candidate"]["story_index"]}
    assert "Recovering a failed migration" in titles


def test_workspace_of_another_user_returns_404(api_client: Any) -> None:
    """AC-SEC-001: cross-tenant access is indistinguishable from absent."""
    owner = Journey(api_client)
    _verify(owner)
    workspace = owner.post(
        "/v1/workspaces", json={"company_name": "Northwind", "role_title": "Engineer"}
    ).json()

    intruder = Journey(api_client)
    response = intruder.get(f"/v1/workspaces/{workspace['id']}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_stage_advance_opens_a_round(journey: Journey) -> None:
    _verify(journey)
    workspace = journey.post(
        "/v1/workspaces", json={"company_name": "Northwind", "role_title": "Engineer"}
    ).json()

    journey.post(f"/v1/workspaces/{workspace['id']}/stage", json={"stage": "technical"})
    refreshed = journey.get(f"/v1/workspaces/{workspace['id']}").json()

    assert refreshed["stage"] == "technical"
    assert refreshed["round_index"] == 2
