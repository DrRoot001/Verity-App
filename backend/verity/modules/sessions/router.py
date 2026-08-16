"""Mock interview endpoints (PRD §25.2 /mock-sessions)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from verity.modules.identity.dependencies import CurrentUser, SessionDep, VerifiedUser
from verity.modules.sessions.service import MockInterviewService

mock_router = APIRouter(prefix="/v1/mock-sessions", tags=["mock-sessions"])


class SessionCreate(BaseModel):
    workspace_id: uuid.UUID
    mode: Literal[
        "general",
        "behavioral",
        "technical",
        "coding",
        "system_design",
        "case",
        "hiring_manager",
        "recruiter_screen",
        "executive",
        "custom",
    ] = "behavioral"
    persona: Literal[
        "neutral_evaluator",
        "friendly_peer",
        "time_pressured_manager",
        "skeptical_expert",
        "executive_sponsor",
        "structured_panelist",
    ] = "neutral_evaluator"
    difficulty: int = Field(default=3, ge=1, le=5)
    duration_minutes: int = Field(default=20, ge=1, le=120)
    live_feedback: bool = False


class SessionResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    mode: str
    persona: str
    difficulty: int
    status: str
    planned_duration_seconds: int
    live_feedback: bool


class TurnResponse(BaseModel):
    utterance: str
    intent: str
    expects_answer: bool
    sequence: int
    difficulty: int


class AnswerSubmit(BaseModel):
    answer: str = Field(min_length=1, max_length=20_000)
    duration_seconds: int = Field(ge=0, le=3600)


class ReportResponse(BaseModel):
    session_id: uuid.UUID
    status: str
    overall_score: float | None
    dimension_scores: dict[str, Any]
    delivery_metrics: dict[str, Any]
    summary: str | None
    strengths: list[Any]
    weaknesses: list[Any]
    recommendations: list[Any]
    per_question: list[Any]


def _session_response(session: Any) -> SessionResponse:
    return SessionResponse(
        id=session.id,
        workspace_id=session.workspace_id,
        mode=session.mode,
        persona=session.persona,
        difficulty=session.difficulty,
        status=session.status,
        planned_duration_seconds=session.planned_duration_seconds,
        live_feedback=session.live_feedback,
    )


def _report_response(report: Any) -> ReportResponse:
    return ReportResponse(
        session_id=report.session_id,
        status=report.status,
        overall_score=float(report.overall_score) if report.overall_score is not None else None,
        dimension_scores=report.dimension_scores,
        delivery_metrics=report.delivery_metrics,
        summary=report.summary,
        strengths=report.strengths,
        weaknesses=report.weaknesses,
        recommendations=report.recommendations,
        per_question=report.per_question,
    )


@mock_router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreate, principal: VerifiedUser, session: SessionDep
) -> SessionResponse:
    created = await MockInterviewService(session).create(
        user_id=principal.user.id,
        workspace_id=payload.workspace_id,
        mode=payload.mode,
        persona=payload.persona,
        difficulty=payload.difficulty,
        duration_minutes=payload.duration_minutes,
        live_feedback=payload.live_feedback,
    )
    return _session_response(created)


@mock_router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: uuid.UUID, principal: CurrentUser, session: SessionDep
) -> SessionResponse:
    found = await MockInterviewService(session).get(
        user_id=principal.user.id, session_id=session_id
    )
    return _session_response(found)


@mock_router.post("/{session_id}/turn", response_model=TurnResponse)
async def next_turn(
    session_id: uuid.UUID, principal: VerifiedUser, session: SessionDep
) -> TurnResponse:
    turn = await MockInterviewService(session).next_turn(
        user_id=principal.user.id, session_id=session_id
    )
    return TurnResponse(
        utterance=turn.utterance,
        intent=turn.intent,
        expects_answer=turn.expects_answer,
        sequence=turn.sequence,
        difficulty=turn.difficulty,
    )


@mock_router.post("/{session_id}/answer", response_model=dict[str, Any])
async def submit_answer(
    session_id: uuid.UUID,
    payload: AnswerSubmit,
    principal: VerifiedUser,
    session: SessionDep,
) -> dict[str, Any]:
    """Returns scores only when live feedback was enabled (FR-MOCK-007)."""
    return await MockInterviewService(session).submit_answer(
        user_id=principal.user.id,
        session_id=session_id,
        answer=payload.answer,
        duration_seconds=payload.duration_seconds,
    )


@mock_router.post("/{session_id}/end", response_model=ReportResponse)
async def end_session(
    session_id: uuid.UUID, principal: VerifiedUser, session: SessionDep
) -> ReportResponse:
    report = await MockInterviewService(session).end(
        user_id=principal.user.id, session_id=session_id
    )
    return _report_response(report)


@mock_router.get("/{session_id}/report", response_model=ReportResponse)
async def get_report(
    session_id: uuid.UUID, principal: CurrentUser, session: SessionDep
) -> ReportResponse:
    report = await MockInterviewService(session).generate_report(
        user_id=principal.user.id, session_id=session_id
    )
    return _report_response(report)
