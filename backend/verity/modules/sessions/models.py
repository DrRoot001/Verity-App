"""Interview sessions and their reports (PRD §24.6).

``mock_sessions`` and ``live_sessions`` are separate tables because their
lifecycles genuinely differ — a mock is driven by our interviewer state machine,
a live session by an external conversation we only observe. They share the
transcript and feedback shapes so reports and the learning loop can treat them
uniformly.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from verity.platform.db.base import (
    Base,
    SoftDeletable,
    Timestamped,
    UserOwned,
    UUIDPrimaryKey,
)


class MockMode(StrEnum):
    GENERAL = "general"
    BEHAVIORAL = "behavioral"
    TECHNICAL = "technical"
    CODING = "coding"
    SYSTEM_DESIGN = "system_design"
    CASE = "case"
    HIRING_MANAGER = "hiring_manager"
    RECRUITER_SCREEN = "recruiter_screen"
    EXECUTIVE = "executive"
    CUSTOM = "custom"


class InterviewerPersona(StrEnum):
    """Original personas (PRD §13.1).

    Persona is a set of state-machine parameters — probe rate, warmth,
    follow-up depth — not a free-text personality instruction, so behaviour
    stays predictable across models.
    """

    NEUTRAL_EVALUATOR = "neutral_evaluator"
    FRIENDLY_PEER = "friendly_peer"
    TIME_PRESSURED_MANAGER = "time_pressured_manager"
    SKEPTICAL_EXPERT = "skeptical_expert"
    EXECUTIVE_SPONSOR = "executive_sponsor"
    STRUCTURED_PANELIST = "structured_panelist"


class SessionStatus(StrEnum):
    CREATED = "created"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    FAILED = "failed"


class SpeakerRole(StrEnum):
    CANDIDATE = "candidate"
    INTERVIEWER = "interviewer"
    UNKNOWN = "unknown"


class ReportStatus(StrEnum):
    PROCESSING = "processing"
    PARTIAL = "partial"
    READY = "ready"
    FAILED = "failed"


class MockSession(Base, UUIDPrimaryKey, Timestamped, SoftDeletable, UserOwned):
    __tablename__ = "mock_sessions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )

    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    persona: Mapped[str] = mapped_column(String(32), nullable=False)
    difficulty: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    planned_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False, server_default="en-US")

    #: Live feedback is off by default — it must not appear mid-answer
    #: (PRD FR-MOCK-007).
    live_feedback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    #: Bundle version this session ran against, so a report is reproducible.
    context_version: Mapped[int | None] = mapped_column(Integer, default=None)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=SessionStatus.CREATED
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    ended_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    end_reason: Mapped[str | None] = mapped_column(String(48), default=None)

    #: Rolling interviewer state: asked questions, probe budget, coverage.
    interviewer_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint("difficulty BETWEEN 1 AND 5", name="mock_sessions_difficulty_range"),
        CheckConstraint(
            "planned_duration_seconds BETWEEN 60 AND 7200",
            name="mock_sessions_duration_range",
        ),
        Index(
            "ix_mock_sessions_user_recent",
            "user_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    @property
    def is_running(self) -> bool:
        return self.status in (SessionStatus.ACTIVE, SessionStatus.PAUSED)


class TranscriptSegment(Base, UUIDPrimaryKey, UserOwned):
    """One utterance. Shared by mock and live sessions (PRD §24.6)."""

    __tablename__ = "transcript_segments"

    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    session_kind: Mapped[str] = mapped_column(String(8), nullable=False)

    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker_role: Mapped[str] = mapped_column(String(16), nullable=False)
    speaker_label: Mapped[str | None] = mapped_column(String(64), default=None)

    start_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), default=None)
    language: Mapped[str | None] = mapped_column(String(20), default=None)

    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_transcript_segments_session_seq"),
        CheckConstraint("session_kind IN ('mock','live')", name="transcript_segments_kind_valid"),
        CheckConstraint(
            "speaker_role IN ('candidate','interviewer','unknown')",
            name="transcript_segments_role_valid",
        ),
        Index("ix_transcript_segments_session", "session_id", "seq"),
    )


class QuestionAttempt(Base, UUIDPrimaryKey, Timestamped, UserOwned):
    """A question asked and how the answer scored (PRD §24.5)."""

    __tablename__ = "question_attempts"

    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    session_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )

    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(32), default=None)
    intent: Mapped[str] = mapped_column(String(16), nullable=False, server_default="question")

    answer_text: Mapped[str | None] = mapped_column(Text, default=None)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, default=None)

    #: Rubric dimensions with their rationale — a score with no reason is not
    #: usable feedback (PRD FR-MOCK-020).
    scores: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: Measured, not inferred: wpm, filler rate, STAR completeness.
    delivery: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    story_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("candidate_stories.id", ondelete="SET NULL"), default=None
    )
    prompt_ref: Mapped[str | None] = mapped_column(String(120), default=None)

    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_question_attempts_session_seq"),
    )


class SessionFeedback(Base, UUIDPrimaryKey, Timestamped, UserOwned):
    """The session report (PRD §27)."""

    __tablename__ = "session_feedback"

    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    session_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )

    overall_score: Mapped[float | None] = mapped_column(Numeric(5, 2), default=None)
    dimension_scores: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    delivery_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    summary: Mapped[str | None] = mapped_column(Text, default=None)
    strengths: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    #: Structured so the preparation engine can consume them (FR-MOCK-022).
    weaknesses: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    recommendations: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    per_question: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    #: Attribution for every generated report (PRD FR-AI-022).
    rubric_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_ref: Mapped[str | None] = mapped_column(String(120), default=None)
    model: Mapped[str | None] = mapped_column(String(120), default=None)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=ReportStatus.PROCESSING
    )

    __table_args__ = (
        UniqueConstraint("session_id", "session_kind", name="uq_session_feedback_session"),
        CheckConstraint(
            "overall_score IS NULL OR (overall_score >= 0 AND overall_score <= 100)",
            name="session_feedback_score_range",
        ),
    )
