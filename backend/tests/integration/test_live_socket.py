"""Live session over the real WebSocket (PRD §26, AC-COP-001/002).

Drives the socket the way the desktop client does: create a session, mint a
ticket, connect, speak, and read the guidance back. This is the first test that
proves Phases 7-9 compose into something a user could actually use.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from verity.realtime import protocol

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple-7"

RESUME = """
Dana Whitfield

WORK EXPERIENCE

Site Reliability Engineer at Northwind Systems
March 2021 - Present
- Led incident response for a payments outage, reducing MTTR from 42 minutes to 9 minutes
- Operated Kubernetes clusters and Python services in production

TECHNICAL SKILLS
Python, PostgreSQL, Kubernetes
"""

JD = """
Senior Backend Engineer

Requirements
- 5+ years of backend engineering experience
- Experience operating Kubernetes in production
- Track record of leading incident response
"""


class LiveClient:
    """A signed-in user with an approved profile and a live session."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.email = f"live-{uuid.uuid4().hex[:10]}@verityqa.dev"
        ip = {"X-Forwarded-For": f"198.51.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"}

        client.post(
            "/v1/auth/signup",
            json={"email": self.email, "password": PASSWORD, "full_name": "Dana"},
            headers=ip,
        )
        self._verify()
        tokens = client.post(
            "/v1/auth/login", json={"email": self.email, "password": PASSWORD}, headers=ip
        ).json()["tokens"]
        self.headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    def _verify(self) -> None:
        import pathlib
        import re

        matches = sorted(
            pathlib.Path(".data/outbox").glob(f"{self.email}-*.txt"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        assert matches, "signup should have written a verification email"
        token = re.search(r"verify-email\?token=([\w\-]+)", matches[0].read_text())
        assert token
        self.client.post("/v1/auth/verify-email", json={"token": token.group(1)})

    def prepare_workspace(self) -> str:
        """Ingest a resume, approve it, and open a workspace with a JD."""
        self.client.post("/v1/resumes/paste", json={"raw_text": RESUME}, headers=self.headers)
        review = self.client.get("/v1/profile/review", headers=self.headers).json()

        for entity_type in ("experience", "skill", "achievement"):
            ids = [n["id"] for n in review["pending"] if n["entity_type"] == entity_type]
            if ids:
                self.client.post(
                    "/v1/profile/bulk-approve",
                    json={"entity_type": entity_type, "ids": ids},
                    headers=self.headers,
                )

        workspace = self.client.post(
            "/v1/workspaces",
            json={
                "company_name": "Northwind Systems",
                "role_title": "Senior Backend Engineer",
                "jd_text": JD,
            },
            headers=self.headers,
        ).json()
        return str(workspace["id"])

    def start_session(self, workspace_id: str) -> tuple[str, str]:
        session = self.client.post(
            "/v1/live-sessions", json={"workspace_id": workspace_id}, headers=self.headers
        )
        assert session.status_code == 201, session.text
        session_id = session.json()["id"]

        ticket = self.client.post(
            f"/v1/live-sessions/{session_id}/ticket", headers=self.headers
        ).json()
        return session_id, ticket["ticket"]


@pytest.fixture
def live(api_client: Any) -> LiveClient:
    return LiveClient(api_client)


def _send(ws: Any, session_id: str, event: str, payload: dict[str, Any]) -> None:
    ws.send_text(
        protocol.Envelope(session_id=session_id, type=event, payload=payload).model_dump_json()
    )


def _drain(ws: Any, session_id: str, limit: int = 24) -> list[dict[str, Any]]:
    """Read every frame the server produced for the work sent so far.

    TestClient's receive_text blocks with no timeout, so draining by "read
    until empty" deadlocks. A ping is sent as a sentinel and the pong marks the
    end of the server's output — the socket preserves order, so anything
    generated before the ping has already arrived.
    """
    _send(ws, session_id, protocol.ClientEvent.PING, {})

    events: list[dict[str, Any]] = []
    for _ in range(limit):
        event = json.loads(ws.receive_text())
        if event["type"] == protocol.ServerEvent.PONG:
            break
        events.append(event)
    return events


# ── Ticket authentication (PRD §26.1) ────────────────────────────────


def test_socket_rejects_a_connection_without_a_ticket(live: LiveClient) -> None:
    workspace_id = live.prepare_workspace()
    session_id, _ = live.start_session(workspace_id)

    # starlette raises when the server closes with 4401 before accepting.
    with pytest.raises(Exception), live.client.websocket_connect(f"/v1/rt/live/{session_id}"):  # noqa: B017
        pass


def test_a_ticket_cannot_be_replayed(live: LiveClient) -> None:
    """Single-use: the key is deleted on redemption."""
    workspace_id = live.prepare_workspace()
    session_id, ticket = live.start_session(workspace_id)

    with live.client.websocket_connect(f"/v1/rt/live/{session_id}?ticket={ticket}") as ws:
        assert _drain(ws, session_id) is not None

    # starlette raises when the server closes with 4401 before accepting.
    with (
        pytest.raises(Exception),  # noqa: B017
        live.client.websocket_connect(f"/v1/rt/live/{session_id}?ticket={ticket}"),
    ):
        pass


def test_a_ticket_is_bound_to_its_session(live: LiveClient) -> None:
    workspace_id = live.prepare_workspace()
    first_id, ticket = live.start_session(workspace_id)
    second_id, _ = live.start_session(workspace_id)
    assert first_id != second_id

    # starlette raises when the server closes with 4401 before accepting.
    with (
        pytest.raises(Exception),  # noqa: B017
        live.client.websocket_connect(f"/v1/rt/live/{second_id}?ticket={ticket}"),
    ):
        pass


# ── The session (AC-COP-001) ─────────────────────────────────────────


def test_a_question_produces_grounded_guidance_over_the_socket(live: LiveClient) -> None:
    workspace_id = live.prepare_workspace()
    session_id, ticket = live.start_session(workspace_id)

    with live.client.websocket_connect(f"/v1/rt/live/{session_id}?ticket={ticket}") as ws:
        _send(
            ws,
            session_id,
            protocol.ClientEvent.SESSION_START,
            {"workspace_id": workspace_id, "interview_type": "general"},
        )
        ready = _drain(ws, session_id)
        assert ready[0]["type"] == protocol.ServerEvent.SESSION_READY

        _send(
            ws,
            session_id,
            protocol.ClientEvent.TRANSCRIPT_TEXT,
            {
                "channel": "system",
                "content": "Tell me about a time you handled a production incident.",
                "is_final": True,
                "start_ms": 0,
                "end_ms": 4000,
            },
        )
        events = _drain(ws, session_id)

    types = [e["type"] for e in events]
    assert protocol.ServerEvent.STT_FINAL in types
    assert protocol.ServerEvent.QUESTION_FINALIZED in types
    assert protocol.ServerEvent.ANSWER_COMPLETE in types

    answer = next(e for e in events if e["type"] == protocol.ServerEvent.ANSWER_COMPLETE)
    content = answer["payload"]["content"]

    assert content["answer_direction"] or content["key_points"]
    # Every candidate_fact must cite evidence; the validator downgrades the rest.
    for point in content["key_points"]:
        if point["claim_type"] == "candidate_fact":
            assert point["evidence_ids"], "a fact without evidence must not survive"

    assert "e2e" in answer["payload"]["latency_ms"]


def test_small_talk_does_not_produce_guidance(live: LiveClient) -> None:
    """FR-RT-002: the expensive path stays shut for chatter."""
    workspace_id = live.prepare_workspace()
    session_id, ticket = live.start_session(workspace_id)

    with live.client.websocket_connect(f"/v1/rt/live/{session_id}?ticket={ticket}") as ws:
        _send(
            ws,
            session_id,
            protocol.ClientEvent.SESSION_START,
            {"workspace_id": workspace_id},
        )
        _drain(ws, session_id)

        _send(
            ws,
            session_id,
            protocol.ClientEvent.TRANSCRIPT_TEXT,
            {
                "channel": "system",
                "content": "Hi, how are you doing today?",
                "is_final": True,
                "start_ms": 0,
                "end_ms": 2000,
            },
        )
        events = _drain(ws, session_id)

    assert protocol.ServerEvent.ANSWER_COMPLETE not in [e["type"] for e in events]


def test_a_manual_question_always_generates(live: LiveClient) -> None:
    """FR-RT-007: the user can always force guidance."""
    workspace_id = live.prepare_workspace()
    session_id, ticket = live.start_session(workspace_id)

    with live.client.websocket_connect(f"/v1/rt/live/{session_id}?ticket={ticket}") as ws:
        _send(ws, session_id, protocol.ClientEvent.SESSION_START, {"workspace_id": workspace_id})
        _drain(ws, session_id)

        _send(
            ws,
            session_id,
            protocol.ClientEvent.QUESTION_MANUAL,
            {"content": "Why do you want to work here?"},
        )
        events = _drain(ws, session_id)

    types = [e["type"] for e in events]
    assert protocol.ServerEvent.QUESTION_FINALIZED in types
    assert protocol.ServerEvent.ANSWER_COMPLETE in types


def test_events_are_sequenced_for_resume(live: LiveClient) -> None:
    workspace_id = live.prepare_workspace()
    session_id, ticket = live.start_session(workspace_id)

    with live.client.websocket_connect(f"/v1/rt/live/{session_id}?ticket={ticket}") as ws:
        _send(ws, session_id, protocol.ClientEvent.SESSION_START, {"workspace_id": workspace_id})
        events = _drain(ws, session_id)

        _send(
            ws,
            session_id,
            protocol.ClientEvent.TRANSCRIPT_TEXT,
            {
                "channel": "system",
                "content": "What did you own on that team?",
                "is_final": True,
                "start_ms": 0,
                "end_ms": 3000,
            },
        )
        events.extend(_drain(ws, session_id))

    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_the_timeline_records_questions_and_guidance(live: LiveClient) -> None:
    """PRD §28: every question and suggestion is inspectable afterwards."""
    workspace_id = live.prepare_workspace()
    session_id, ticket = live.start_session(workspace_id)

    with live.client.websocket_connect(f"/v1/rt/live/{session_id}?ticket={ticket}") as ws:
        _send(ws, session_id, protocol.ClientEvent.SESSION_START, {"workspace_id": workspace_id})
        _drain(ws, session_id)
        _send(
            ws,
            session_id,
            protocol.ClientEvent.QUESTION_MANUAL,
            {"content": "Tell me about a production incident you handled."},
        )
        _drain(ws, session_id)

    timeline = live.client.get(
        f"/v1/live-sessions/{session_id}/timeline", headers=live.headers
    ).json()

    assert timeline["questions"]
    question = timeline["questions"][0]
    assert question["trigger"] == "manual"
    assert question["suggestions"], "guidance must be recorded against its question"
    assert "grounding" in question["suggestions"][0]


def test_proctored_workspaces_refuse_a_live_session(live: LiveClient) -> None:
    """FR-PRIV-011."""
    workspace = live.client.post(
        "/v1/workspaces",
        json={
            "company_name": "Northwind",
            "role_title": "Engineer",
            "integrity_mode": "proctored",
        },
        headers=live.headers,
    ).json()

    response = live.client.post(
        "/v1/live-sessions", json={"workspace_id": workspace["id"]}, headers=live.headers
    )

    assert response.status_code == 403
    assert "proctored" in response.json()["error"]["message"]


def test_another_users_session_is_not_reachable(api_client: Any) -> None:
    owner = LiveClient(api_client)
    workspace_id = owner.prepare_workspace()
    session_id, _ = owner.start_session(workspace_id)

    intruder = LiveClient(api_client)
    response = intruder.client.get(f"/v1/live-sessions/{session_id}", headers=intruder.headers)
    assert response.status_code == 404


# ── Report and the loop (PRD §27, Phase 10) ──────────────────────────


def test_a_live_session_produces_a_coverage_report(live: LiveClient) -> None:
    """The report says what was covered, not what score the candidate got."""
    workspace_id = live.prepare_workspace()
    session_id, ticket = live.start_session(workspace_id)

    with live.client.websocket_connect(f"/v1/rt/live/{session_id}?ticket={ticket}") as ws:
        _send(ws, session_id, protocol.ClientEvent.SESSION_START, {"workspace_id": workspace_id})
        _drain(ws, session_id)
        _send(
            ws,
            session_id,
            protocol.ClientEvent.QUESTION_MANUAL,
            {"content": "How have you operated Kubernetes in production?"},
        )
        _drain(ws, session_id)

    report = live.client.get(f"/v1/live-sessions/{session_id}/report", headers=live.headers).json()

    assert report["status"] == "ready"
    assert report["metrics"]["questions_detected"] >= 1

    # The JD requires Kubernetes and the question asked about it.
    probed = [c for c in report["coverage"] if c["probed"]]
    assert any("kubernetes" in c["requirement"].lower() for c in probed)


def test_an_unbacked_requirement_becomes_a_prep_task(live: LiveClient) -> None:
    """AC-WS-002: the loop closes without the user relaying anything."""
    workspace_id = live.prepare_workspace()
    session_id, ticket = live.start_session(workspace_id)

    with live.client.websocket_connect(f"/v1/rt/live/{session_id}?ticket={ticket}") as ws:
        _send(ws, session_id, protocol.ClientEvent.SESSION_START, {"workspace_id": workspace_id})
        _drain(ws, session_id)
        _send(
            ws,
            session_id,
            protocol.ClientEvent.QUESTION_MANUAL,
            {"content": "Tell me about leading incident response."},
        )
        _drain(ws, session_id)

    report = live.client.get(f"/v1/live-sessions/{session_id}/report", headers=live.headers).json()

    assert report["weaknesses"], "an unbacked requirement should be measured"

    plan = live.client.post(
        f"/v1/preparation/plans/generate?workspace_id={workspace_id}",
        headers=live.headers,
    ).json()
    titles = " ".join(t["title"].lower() for t in plan["tasks"])

    theme = report["weaknesses"][0]["theme"].lower()
    assert theme[:15] in titles, "a measured gap must reach the next plan"


def test_the_report_is_generated_once(live: LiveClient) -> None:
    workspace_id = live.prepare_workspace()
    session_id, ticket = live.start_session(workspace_id)

    with live.client.websocket_connect(f"/v1/rt/live/{session_id}?ticket={ticket}") as ws:
        _send(ws, session_id, protocol.ClientEvent.SESSION_START, {"workspace_id": workspace_id})
        _drain(ws, session_id)
        _send(ws, session_id, protocol.ClientEvent.QUESTION_MANUAL, {"content": "Why us?"})
        _drain(ws, session_id)

    first = live.client.get(f"/v1/live-sessions/{session_id}/report", headers=live.headers).json()
    second = live.client.get(f"/v1/live-sessions/{session_id}/report", headers=live.headers).json()

    assert first == second


def test_session_history_lists_only_my_sessions(live: LiveClient, api_client: Any) -> None:
    workspace_id = live.prepare_workspace()
    mine, _ = live.start_session(workspace_id)

    stranger = LiveClient(api_client)
    history = stranger.client.get("/v1/live-sessions", headers=stranger.headers).json()

    assert mine not in [s["id"] for s in history]


def test_another_users_report_is_not_reachable(live: LiveClient, api_client: Any) -> None:
    workspace_id = live.prepare_workspace()
    session_id, _ = live.start_session(workspace_id)

    intruder = LiveClient(api_client)
    response = intruder.client.get(
        f"/v1/live-sessions/{session_id}/report", headers=intruder.headers
    )

    assert response.status_code == 404
